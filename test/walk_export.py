# -*- coding: utf-8 -*-
"""walk_export.py — 匯出期間選單四個分支實走（2026-08-04）。

user：「你實際在我網頁選這四種選項,很多都錯,只有一周的 7 天是對的」
⇒ 先前我只測「送出對應句子」,那**不等於真的點按鈕**。
   訪客實際走的是：出選單 → 同連線點第 N 個選項 → 看結果。

驗兩條路（記憶 [[review_to_the_screen]]：選項＋序數路都要實走）：
  ① 點選項（送 actions 字串）
  ② 序數路（送「第 N 個」/ 數字）

判準：回答要出現對應天數
  昨天→1 天 / 前一週→7 / 前一個月→30 / 前一季→90
用法：python3 walk_export.py --rpi5 [--zh|--en]
"""
import asyncio
import io
import json
import ssl
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import websockets

EN = "--en" in sys.argv
PORT = 8002 if EN else 8001
URI = f"wss://localhost:{PORT}/ws?fast=1"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

TRIGGER = "export movements" if EN else "匯出進出紀錄"
# 期望：選項索引 → (標籤, 該出現的天數關鍵字群)
EXPECT = [
    ("昨天/yesterday", ["昨天", "yesterday", "1 天", "1 day"]),
    ("前一週/last week", ["7 天", "7 days", "一週", "last week"]),
    ("前一個月/last month", ["30 天", "30 days", "一個月", "last month"]),
    ("前一季/last quarter", ["90 天", "90 days", "一季", "quarter", "3 months"]),
]


async def ask(ws, text):
    await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
        if m.get("type") == "done":
            return m.get("result") or {}


async def walk(mode):
    """mode: 'action'（點選項）或 'ordinal'（說第N個）"""
    print(f"\n{'='*66}\n  {mode.upper()}  ({'EN' if EN else 'ZH'})\n{'='*66}")
    rows = []
    for idx in range(4):
        async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
            r1 = await ask(ws, TRIGGER)
            opts = (r1.get("data") or {}).get("options") or []
            acts = (r1.get("data") or {}).get("actions") or []
            if r1.get("view") != "clarify" or not opts:
                print(f"  [{idx+1}] 觸發句沒出選單 → view={r1.get('view')}")
                rows.append((idx, False, "無選單"))
                continue
            if mode == "action":
                send = acts[idx] if idx < len(acts) else opts[idx]
            else:
                send = str(idx + 1)
            r2 = await ask(ws, send)
            summ = (r2.get("summary") or "").replace("\n", " ")
            label, keys = EXPECT[idx]
            hit = any(k in summ for k in keys)
            view = r2.get("view")
            # error / clarify 一律算失敗
            bad = view in ("error",) or (view == "clarify" and "期間" in summ)
            okk = hit and not bad
            rows.append((idx, okk, f"{view} | {summ[:78]}"))
            print(f"  [{idx+1}] {label}\n      送: {send!r}\n      {'✅' if okk else '❌'} {view} | {summ[:88]}")
    return rows


async def main():
    a = await walk("action")
    b = await walk("ordinal")
    print(f"\n{'='*66}")
    print(f"  點選項 {sum(1 for r in a if r[1])}/4   序數路 {sum(1 for r in b if r[1])}/4")
    print(f"{'='*66}")


asyncio.run(main())
