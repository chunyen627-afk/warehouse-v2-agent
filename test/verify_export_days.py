# -*- coding: utf-8 -*-
"""verify_export_days.py — 驗**最終產出**的天數，不是確認卡文字（2026-08-04）。

user 抓到我的檢查失職：「打開的都是7天 你沒認真檢查到底」
我先前只驗確認卡預覽（「合併最近 30 天」）就宣稱通過,
實際執行結果全是 7 天 486 筆。

⇒ 這支模擬**完整 HITL 流程**：
   ① 送觸發句 → 拿到 script_confirm 卡（含 script_id / days）
   ② 送 confirm（payload 比照前端：script_id + days）
   ③ 讀 script_done 的 summary,**驗實際匯出天數**

判準：昨天→1 天 / 前一週→7 / 前一個月→30 / 前一季→90（±容許實際有資料天數較少）
用法：python3 verify_export_days.py --rpi5 [--en]
"""
import asyncio
import io
import json
import re
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

CASES = (
    [("export movements yesterday", 1), ("export movements last week", 7),
     ("export movements last month", 30), ("export movements last quarter", 90)]
    if EN else
    [("匯出昨天的進出紀錄", 1), ("匯出前一週的進出紀錄", 7),
     ("匯出前一個月的進出紀錄", 30), ("匯出前一季的進出紀錄", 90)])


async def ask(ws, payload):
    await ws.send(json.dumps(payload, ensure_ascii=False))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
        if m.get("type") == "done":
            return m.get("result") or {}


async def main():
    print(f"{'='*70}\n  驗最終產出天數 ({'EN' if EN else 'ZH'})\n{'='*70}")
    good = 0
    for q, want in CASES:
        async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
            r1 = await ask(ws, {"type": "chat", "text": q})
            d1 = r1.get("data") or {}
            if r1.get("view") != "script_confirm":
                print(f"  ❌ {q}\n      沒開確認卡 → {r1.get('view')} | {(r1.get('summary') or '')[:60]}")
                continue
            sid, days = d1.get("script_id", ""), d1.get("days")
            # ② 比照前端送 confirm（前端 payload = {script_id, days}）
            r2 = await ask(ws, {"type": "confirm", "action": "run_script",
                                "script_id": sid, "days": days})
            summ = (r2.get("summary") or "").replace("\n", " ")
            # ③ 從實際產出訊息抓天數
            m = re.search(r"(\d+)\s*(?:天|days?)", summ)
            got = int(m.group(1)) if m else None
            okk = (got == want) or (want == 1 and got in (1, None) and "昨天" in summ)
            good += bool(okk)
            print(f"  {'✅' if okk else '❌'} {q}")
            print(f"      卡片 days={days} → 實際產出: {summ[:88]}")
    print(f"{'='*70}\n  最終產出正確 {good}/{len(CASES)}\n{'='*70}")


asyncio.run(main())
