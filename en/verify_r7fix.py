# -*- coding: utf-8 -*-
"""驗證第七輪破口修復（同連線多輪 + 單句）。--en / 預設 zh 不適用此版。"""
import asyncio
import io
import json
import ssl
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import websockets

URI = "wss://localhost:8002/ws?fast=1"
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
    # ── 多輪：匯出 → confirm → 下載追問 → 期間追問 ──
    print("== 多輪（同連線）==")
    async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
        r = await ask(ws, {"type": "chat", "text": "export movements yesterday"})
        print("  1 export yesterday →", r.get("view"))
        d = r.get("data") or {}
        r = await ask(ws, {"type": "confirm", "action": "run_script",
                           "script_id": d.get("script_id", ""),
                           "days": d.get("days")})
        print("  2 confirm →", r.get("view"))
        r = await ask(ws, {"type": "chat", "text": "can i download it"})
        print("  3 can i download it →", r.get("view"), "|",
              (r.get("summary") or "")[:60])
        r = await ask(ws, {"type": "chat", "text": "and last week too"})
        print("  4 and last week too →", r.get("view"), "|",
              (r.get("summary") or "")[:60])
        r = await ask(ws, {"type": "chat", "text": "what about last month"})
        print("  5 what about last month →", r.get("view"), "|",
              (r.get("summary") or "")[:60])
    # ── 單句 ──
    print("== 單句 ==")
    for q in ["why do the numbers keep changing?", "is this real time data",
              "cool", "who made you", "confirm",
              "compare last two months", "what's running low"]:
        async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
            r = await ask(ws, {"type": "chat", "text": q})
            print(f"  {q[:38]:40} → {r.get('view'):16} | "
                  f"{(r.get('summary') or '')[:52]}")


asyncio.run(main())
