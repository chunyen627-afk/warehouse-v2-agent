#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_bs.py — 把被 heredoc 吃掉的正則 \\b 還原（記憶坑 31）。

症狀：程式碼看起來完全正確、服務也載入新碼，但正則永遠不成立。
真兇：shell heredoc 把 `\\b` 解讀成 0x08（backspace）寫進檔案。
`cat -A` 會顯示成 `^H`。清除必須用 **binary 模式**（文字模式清不掉）。

⚠️ 這支本身要用 Write 工具寫成檔案再執行，不能再用 heredoc（會重蹈覆轍）。
"""
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\pjunm\OneDrive\Desktop\FunctionGemma_Finetune\warehouse_v2")
FILES = [
    "en/server.py", "test/server.py",
    "en/tools_v2.py", "test/tools_v2.py",
    "en/warehouse_data/scripts/stock_audit.py",
    "test/warehouse_data/scripts/stock_audit.py",
    "en/warehouse_data/scripts/export_movements.py",
    "test/warehouse_data/scripts/export_movements.py",
]

BS = b"\x08"
REPL = b"\\" + b"b"          # 兩個 byte：反斜線 + b

total = 0
for rel in FILES:
    p = ROOT / rel
    if not p.exists():
        print(f"  (跳過，不存在) {rel}")
        continue
    data = p.read_bytes()
    n = data.count(BS)
    if n:
        p.write_bytes(data.replace(BS, REPL))
        total += n
    after = p.read_bytes().count(BS)
    mark = "OK" if after == 0 else "!! 仍有殘留"
    print(f"  {rel}: {n} -> {after}  {mark}")

print(f"\n共修復 {total} 個 backspace 字元")

# 語法檢查
import ast
bad = []
for rel in FILES:
    p = ROOT / rel
    if not p.exists():
        continue
    try:
        ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError as e:
        bad.append(f"{rel}: {e}")
print("語法檢查:", "全部 OK" if not bad else bad)
sys.exit(1 if bad else 0)
