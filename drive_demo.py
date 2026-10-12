#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""drive_demo.py
CDP + screen-capture driver. Produces 8 screenshots (xssagent-*.png).

Key finding: CDP Page.captureScreenshot hangs when a JS modal dialog (alert) is
open, so it cannot capture the alert box. For "XSS fire" and "shared fire" shots
we use Python ctypes + PIL ImageGrab to capture the full screen (including the
alert dialog). All other shots use CDP captureScreenshot (no dialog).

Prerequisites: Ollama running at 127.0.0.1:11434 with qwen2.5:7b pulled,
agent_server.py running at 127.0.0.1:8100, merchants.xlsx + template.html present.
"""
import base64, ctypes, json, os, shutil, subprocess, sys, tempfile, time
import urllib.request
from ctypes import wintypes
import websocket  # type: ignore

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9222
AGENT = "http://127.0.0.1:8100/"
ASSETS = r"C:\Users\humia\Desktop\blog\assets"
HERE = os.path.dirname(os.path.abspath(__file__))
os.makedirs(ASSETS, exist_ok=True)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def wait_devtools(timeout=30):
    t = time.time()
    while time.time() - t < timeout:
        try:
            return http_json("http://127.0.0.1:%d/json" % PORT)
        except Exception:
            time.sleep(0.4)
    return None


def find_tab(url_frag):
    for t in http_json("http://127.0.0.1:%d/json" % PORT):
        if t.get("type") == "page" and url_frag in t.get("url", ""):
            return t
    return None


def wait_tab(url_frag, timeout=40):
    t = time.time()
    while time.time() - t < timeout:
        tab = find_tab(url_frag)
        if tab:
            return tab
        time.sleep(0.5)
    return None


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=15)
        self._id = 0

    def call(self, method, params=None, timeout=60):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        t = time.time()
        while time.time() - t < timeout:
            self.ws.settimeout(max(1, timeout - (time.time() - t)))
            try:
                msg = json.loads(self.ws.recv())
            except Exception as e:
                raise RuntimeError("recv failed: " + str(e))
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError("CDP error: " + str(msg["error"]))
                return msg.get("result", {})
        raise TimeoutError(method + " timed out")

    def eval(self, expr, timeout=60):
        r = self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True,
        }, timeout=timeout)
        res = r.get("result", {})
        if res.get("subtype") == "error":
            raise RuntimeError("JS error: " + str(res.get("description")))
        return res.get("value")

    def navigate(self, url, settle=2.0):
        self.call("Page.navigate", {"url": url})
        time.sleep(settle)

    def shot(self, path):
        data = self.call("Page.captureScreenshot", {
            "format": "png", "captureBeyondViewport": False,
        })["data"]
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        print("[shot] " + path + "  (%d bytes)" % os.path.getsize(path))

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def launch_chrome(user_data, start_url):
    return subprocess.Popen([
        CHROME,
        "--remote-debugging-port=%d" % PORT,
        "--user-data-dir=%s" % user_data,
        "--no-first-run", "--no-default-browser-check",
        "--disable-features=Translate",
        "--remote-allow-origins=*",
        "--window-size=1280,880", "--window-position=40,40",
        "--disable-popup-blocking",
        start_url,
    ])


def find_window_by_pid(target_pid):
    """Find first visible window with a title belonging to target_pid."""
    found = [None]
    def callback(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == target_pid:
                if user32.GetWindowTextLengthW(hwnd) > 0:
                    found[0] = hwnd
                    return False
        return True
    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return found[0]


def force_focus(hwnd):
    """Force window to foreground using AttachThreadInput trick."""
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None)
    my_tid = kernel32.GetCurrentThreadId()
    if fg_tid != my_tid:
        user32.AttachThreadInput(my_tid, fg_tid, True)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(my_tid, fg_tid, False)
    else:
        user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    time.sleep(1.5)


def screen_shot(path, chrome_pid=0):
    """Capture full primary screen via PIL ImageGrab (includes alert dialogs)."""
    from PIL import ImageGrab
    if chrome_pid:
        hwnd = find_window_by_pid(chrome_pid)
        if hwnd:
            force_focus(hwnd)
    img = ImageGrab.grab()
    img.save(path)
    print("[screen] " + path + "  (%d bytes)" % os.path.getsize(path))


def wait_for_share_url(cdp, timeout=300):
    t = time.time()
    while time.time() - t < timeout:
        time.sleep(1.5)
        try:
            href = cdp.eval(
                "var a=document.querySelector('.links a');"
                "a?a.getAttribute('href'):null")
            if href and "/page/" in href:
                return href.rsplit("/", 1)[-1]
        except Exception:
            continue
    return None


def kill(proc):
    try:
        proc.terminate(); proc.wait(timeout=5)
    except Exception:
        proc.kill()


def cdp_session(user_data_name, start_url, shots_fn):
    """Launch chrome with CDP, run shots_fn(cdp) -> result, kill chrome."""
    user_data = os.path.join(tempfile.gettempdir(), user_data_name)
    if os.path.exists(user_data):
        shutil.rmtree(user_data, ignore_errors=True)
    proc = launch_chrome(user_data, start_url)
    try:
        if not wait_devtools():
            print("[err] devtools not ready"); proc.kill(); sys.exit(1)
        tab = wait_tab("127.0.0.1:8100")
        if not tab:
            print("[err] no tab"); proc.kill(); sys.exit(1)
        cdp = CDP(tab["webSocketDebuggerUrl"])
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        time.sleep(1.5)
        result = shots_fn(cdp)
        cdp.close()
        return result
    finally:
        kill(proc)


def screen_session(user_data_name, start_url, out_path, wait=7):
    """Launch chrome with URL, wait for alert, screen-capture (with focus), kill."""
    # Kill all chrome first to avoid window conflicts
    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"],
                   capture_output=True, timeout=10)
    time.sleep(2)
    user_data = os.path.join(tempfile.gettempdir(), user_data_name)
    if os.path.exists(user_data):
        shutil.rmtree(user_data, ignore_errors=True)
    proc = launch_chrome(user_data, start_url)
    try:
        time.sleep(wait)
        screen_shot(out_path, proc.pid)
    finally:
        kill(proc)

def main():
    # ===== Shots 1-4: main CDP session (no dialogs) =====
    def main_shots(cdp):
        # Shot 1: xlsx payload view
        cdp.navigate("http://127.0.0.1:8100/view/xlsx", settle=1.5)
        cdp.shot(os.path.join(ASSETS, "xssagent-xlsx-payload.png"))

        # Shot 2: template source view
        cdp.navigate("http://127.0.0.1:8100/view/tpl", settle=1.2)
        cdp.shot(os.path.join(ASSETS, "xssagent-template.png"))

        # Shot 3: injection form
        cdp.navigate(AGENT, settle=1.5)
        cdp.eval("document.getElementById('useDemo').click()")
        time.sleep(1.0)
        instr = cdp.eval("document.querySelector('textarea[name=instruction]').value")
        print("[injection] instruction head:", repr((instr or "")[:80]))
        cdp.shot(os.path.join(ASSETS, "xssagent-injection.png"))

        # Shot 4: generated HTML (submit form, wait for LLM)
        cdp.eval("document.getElementById('form').submit()")
        print("[generate] submitted, waiting for LLM...")
        pid = wait_for_share_url(cdp, timeout=300)
        if not pid:
            print("[err] no share url found"); sys.exit(1)
        print("[generate] page_id =", pid)
        time.sleep(1.0)
        cdp.shot(os.path.join(ASSETS, "xssagent-generated-html.png"))

        # Save generated HTML for proof
        gen_html_url = "http://127.0.0.1:8100/page/" + pid
        with urllib.request.urlopen(gen_html_url) as r:
            gen_html = r.read().decode("utf-8")
        with open(os.path.join(HERE, "generated", "_last_vuln.html"), "w", encoding="utf-8") as f:
            f.write(gen_html)
        print("[generate] saved generated HTML (%d bytes)" % len(gen_html))
        return pid, gen_html_url

    pid, gen_html_url = cdp_session("xssagent_chrome_profile", AGENT, main_shots)

    # ===== Shot 5: XSS fire (screen capture to catch alert dialog) =====
    screen_session("xssagent_chrome_profile_fire", gen_html_url,
                   os.path.join(ASSETS, "xssagent-xss-fire.png"), wait=8)

    # ===== Shot 6: shared fire (fresh profile = another user) =====
    screen_session("xssagent_chrome_profile_shared", gen_html_url,
                   os.path.join(ASSETS, "xssagent-shared-fire.png"), wait=8)

    # ===== Shot 7: CSP blocked (CDP, no dialog) =====
    def csp_shots(cdp):
        time.sleep(2.5)
        cdp.shot(os.path.join(ASSETS, "xssagent-csp-blocked.png"))
    csp_url = "http://127.0.0.1:8100/page-csp/" + pid
    cdp_session("xssagent_chrome_profile_csp", csp_url, csp_shots)

    # ===== Shot 8: escaped (safe instruction, CDP, no dialog) =====
    def esc_shots(cdp):
        cdp.eval("document.getElementById('safeMode').click()")
        print("[esc] submitted safe-mode, waiting for LLM...")
        pid_esc = wait_for_share_url(cdp, timeout=300)
        if not pid_esc:
            print("[err] no esc share url"); sys.exit(1)
        print("[esc] page_id =", pid_esc)
        time.sleep(1.0)
        cdp.shot(os.path.join(ASSETS, "xssagent-escaped.png"))
        esc_page_url = "http://127.0.0.1:8100/page/" + pid_esc
        with urllib.request.urlopen(esc_page_url) as r:
            esc_html = r.read().decode("utf-8")
        with open(os.path.join(HERE, "generated", "_last_esc.html"), "w", encoding="utf-8") as f:
            f.write(esc_html)
        print("[esc] saved escaped HTML (%d bytes)" % len(esc_html))

    cdp_session("xssagent_chrome_profile_esc", AGENT, esc_shots)

    print("[done] all 8 screenshots produced")


if __name__ == "__main__":
    main()
