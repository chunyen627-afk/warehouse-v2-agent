# -*- coding: utf-8 -*-
"""ZH 鏡射驗證（同連線多輪 + 單句 + 鄰居防誤傷）。"""
import asyncio
import io
import json
import ssl
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import websockets

URI = "wss://localhost:8001/ws?fast=1"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


async def ask(ws, payload):
    await ws.send(json.dumps(payload, ensure_ascii=False))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
        if m.get("type") == "done":
            return m.get("result") or {}


async def main():
    print("== 多輪（同連線）==")
    async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
        r = await ask(ws, {"type": "chat", "text": "匯出昨天的進出紀錄"})
        print("  1 匯出昨天 →", r.get("view"))
        d = r.get("data") or {}
        r = await ask(ws, {"type": "confirm", "action": "run_script",
                           "script_id": d.get("script_id", ""),
                           "days": d.get("days")})
        print("  2 confirm →", r.get("view"))
        r = await ask(ws, {"type": "chat", "text": "可以下載嗎"})
        print("  3 可以下載嗎 →", r.get("view"), "|", (r.get("summary") or "")[:44])
        r = await ask(ws, {"type": "chat", "text": "那上週的呢"})
        print("  4 那上週的呢 →", r.get("view"), "|", (r.get("summary") or "")[:44])
        r = await ask(ws, {"type": "chat", "text": "上個月的呢"})
        print("  5 上個月的呢 →", r.get("view"), "|", (r.get("summary") or "")[:44])
    print("== 單句與鄰居 ==")
    for q in ["數字怎麼一直在變", "這是即時資料嗎", "庫存為什麼自己動",
              "上週的出貨量", "昨天有出貨嗎", "藍牙耳機庫存"]:
        async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
            r = await ask(ws, {"type": "chat", "text": q})
            print(f"  {q:14} → {r.get('view'):16} | {(r.get('summary') or '')[:44]}")


asyncio.run(main())
