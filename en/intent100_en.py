# -*- coding: utf-8 -*-
"""intent100_en.py — 驗「不同問法能否產出訪客要的東西」（2026-08-04）。

user：「用不同的問法看能否正確產出訪客要的報告 / 採購單 / 進出紀錄」

前一版 run100_new_en.py 只驗「沒出錯 + 有渲染器」⇒ 102/102 太寬鬆,
**沒驗到「訪客要 A 有沒有拿到 A」**。這支補上：每句標期望產出類型。

期望類型：
  EXPORT  進出紀錄匯出（script_confirm/script_done + export_movements）
  REPORT  報告（stock_audit,報告統一 ⇒ script_* + stock_audit）
  PO      採購單（po_confirm / po_done）
  ASK     該反問期間（clarify + 選單）
  YDAY    今天類 → 導向昨天（movement + yesterday 字樣）
  ITEM    單一商品統計卡（movement,不轉匯出）
  SCHED   排程（schedule_confirm / clarify 已存在）
  CMP     跨期/倉庫比較（period_compare / compare_warehouses）
  OTHER   低庫存/到期等（不可被匯出誤搶）

用法：python3 intent100_en.py --rpi5
"""
import asyncio
import io
import json
import pathlib
import re
import ssl
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import websockets

ROOT = pathlib.Path("/home/p400/warehouse_v2_en")
URI = "wss://localhost:8002/ws?fast=1"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

CASES = [
    # ── EXPORT：進出紀錄匯出（各種問法）──
    ("export movements yesterday", "EXPORT"),
    ("export movements last week", "EXPORT"),
    ("export movements last month", "EXPORT"),
    ("export movements last quarter", "EXPORT"),
    ("export the movement log for last week", "EXPORT"),
    ("export movement records for the last month", "EXPORT"),
    ("download movements last quarter", "EXPORT"),
    ("give me the movement log for yesterday", "EXPORT"),
    ("i need the movement export for last week", "EXPORT"),
    ("can you export the last 3 months of movements", "EXPORT"),
    ("export movements for the past quarter", "EXPORT"),
    ("movement log export last month", "EXPORT"),
    ("export movements previous week", "EXPORT"),
    ("export movements past month", "EXPORT"),
    ("export movements last 7 days", "EXPORT"),
    ("export movements last 30 days", "EXPORT"),
    ("export movements last 90 days", "EXPORT"),
    ("i'd like to see last week's movements", "EXPORT"),
    ("give me a csv of the movements", "ASK"),
    ("export everything from last quarter", "EXPORT"),
    ("can i get the movement records please", "ASK"),
    # ── ASK：沒講期間該反問 ──
    ("export movements", "ASK"),
    ("export the movement log", "ASK"),
    ("i want to export the movement records", "ASK"),
    ("download the movement log", "ASK"),
    # ── REPORT：報告（報告統一 → stock_audit）──
    ("give me a full warehouse report", "REPORT"),
    ("generate a report", "REPORT"),
    ("run a stock audit", "REPORT"),
    ("show me the daily report", "REPORT"),
    ("i want the health report", "REPORT"),
    ("create a warehouse health report", "REPORT"),
    ("run the month-end stocktake", "REPORT"),
    ("warehouse health check", "REPORT"),
    ("full inventory report", "REPORT"),
    ("stocktake report", "REPORT"),
    ("audit report", "REPORT"),
    # ── PO：採購單 ──
    ("create a purchase order for low stock items", "PO"),
    ("generate a PO", "PO"),
    ("make a purchase order", "PO"),
    ("i need a purchase order for items running out", "PO"),
    # ── YDAY：今天類 → 導向昨天 ──
    ("what movements happened today", "YDAY"),
    ("any movements today", "YDAY"),
    ("show me today's in and out", "YDAY"),
    # ── ITEM：單一商品 → 統計卡 ──
    ("wireless mouse movements this month", "ITEM"),
    ("how many bluetooth earphones moved last week", "ITEM"),
    ("power bank in and out this month", "ITEM"),
    ("yoga mat movement history", "ITEM"),
    # ── SCHED：排程（不可被匯出搶）──
    ("schedule a daily report at 9am", "SCHED"),
    ("schedule the movement export every monday", "SCHED"),
    ("set up a weekly stocktake", "SCHED"),
    ("schedule a monthly report", "SCHED"),
    # ── CMP：比較（不可被匯出讓路誤傷）──
    ("compare last two months", "CMP"),
    ("which items grew the most", "CMP"),
    ("month over month trend", "CMP"),
    ("compare north and central", "CMP"),
    ("which warehouse has the most stock", "CMP"),
    # ── OTHER：低庫存/到期（不可被誤搶）──
    ("what items are low on stock", "OTHER"),
    ("show me items below safety stock", "OTHER"),
    ("what is expiring soon", "OTHER"),
    ("list low stock items", "OTHER"),
    # ── 錯字/大小寫（容錯）──
    ("exprot movemnts last week", "EXPORT"),
    ("EXPORT MOVEMENTS LAST MONTH", "EXPORT"),
    ("export movements last week please thanks", "EXPORT"),
]

OK = {
    "EXPORT": lambda v, s, d: (v in ("script_confirm", "script_done")
                               and "movement" in str(d.get("script_id", "")
                                                     or s).lower()),
    "ASK":    lambda v, s, d: v == "clarify" and bool(d.get("options")),
    "REPORT": lambda v, s, d: (v in ("script_confirm", "script_done")
                               and ("stocktake" in s.lower()
                                    or "audit" in str(d.get("script_id", "")).lower()
                                    or "report" in str(d.get("script_id", "")).lower())),
    "PO":     lambda v, s, d: v in ("po_confirm", "po_done"),
    "YDAY":   lambda v, s, d: v == "movement" and "yesterday" in s.lower(),
    "ITEM":   lambda v, s, d: v in ("movement", "inventory_single"),
    "SCHED":  lambda v, s, d: (v in ("schedule_confirm", "schedule_list")
                               or (v == "clarify" and "already exists" in s.lower())),
    "CMP":    lambda v, s, d: v in ("period_compare", "compare_warehouses",
                                    "hot_items", "inventory"),
    "OTHER":  lambda v, s, d: v in ("low_stock", "expiring", "inventory"),
}


async def ask(ws, text):
    await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
        if m.get("type") == "done":
            return m.get("result") or {}


async def main():
    print(f"{'='*76}\n  訪客要什麼就給什麼？（{len(CASES)} 句 × 9 類意圖）\n{'='*76}")
    bad, bycat = [], {}
    for i, (q, want) in enumerate(CASES, 1):
        async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
            try:
                r = await ask(ws, q)
            except Exception as e:
                bad.append((i, q, want, "TIMEOUT", str(e)[:40]))
                continue
        v = r.get("view") or ""
        s = (r.get("summary") or "").replace("\n", " ")
        d = r.get("data") or {}
        good = OK[want](v, s, d)
        bycat.setdefault(want, [0, 0])
        bycat[want][1] += 1
        if good:
            bycat[want][0] += 1
        else:
            bad.append((i, q, want, v, s[:70]))
            print(f"  [{i:3}] ❌ 要 {want:6} 得 {v:18} | {q[:44]}\n         {s[:74]}")

    print(f"\n{'='*76}")
    print(f"  總計 {len(CASES)-len(bad)}/{len(CASES)}")
    for k, (g, t) in sorted(bycat.items()):
        mark = "✅" if g == t else "❌"
        print(f"    {mark} {k:7} {g}/{t}")
    print(f"{'='*76}")


asyncio.run(main())
