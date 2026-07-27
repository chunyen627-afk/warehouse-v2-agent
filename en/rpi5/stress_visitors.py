# -*- coding: utf-8 -*-
"""展場多訪客壓測：模擬 N 支手機各自連進來、各自持續操作。

跟 stress_both.py 的差別：
  - 那支測「同一瞬間 N 個請求」（瞬時尖峰）
  - 這支測「N 位訪客各開一條 WebSocket 長連線、連續問好幾句」
    ← 這才是展場真實樣態（每支手機一條 WS，停留數分鐘）

⚠️ scp 上去執行。用法：python3 stress_visitors.py [人數] [每人句數]
"""
import asyncio
import json
import ssl
import statistics
import sys
import time

import websockets

N = int(sys.argv[1]) if len(sys.argv) > 1 else 6
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 5

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

EN = ["bluetooth earphones stock", "whats running low",
      "best sellers this month", "compare north and south",
      "what is expiring soon", "power bank stock",
      "what came in today", "any stock discrepancies"]
ZH = ["藍牙耳機庫存", "哪些快缺貨", "本月熱銷", "北倉跟南倉比較",
      "快到期的有哪些", "行動電源庫存", "今天進了什麼", "帳有沒有對不上"]


async def visitor(vid, port, sents, out):
    """一位訪客＝一條 WS 長連線，連續問 ROUNDS 句（模擬真實停留）。"""
    lat, errs = [], []
    try:
        async with websockets.connect(f"wss://localhost:{port}/ws?fast=1",
                                      ssl=ctx, max_size=None,
                                      open_timeout=40) as ws:
            for i in range(ROUNDS):
                text = sents[(vid + i) % len(sents)]
                t0 = time.perf_counter()
                await ws.send(json.dumps({"type": "chat", "text": text},
                                         ensure_ascii=False))
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
                    if m.get("type") == "done":
                        lat.append(time.perf_counter() - t0)
                        break
                    if m.get("type") == "error":
                        errs.append(f"{text}: {m.get('text')}")
                        lat.append(time.perf_counter() - t0)
                        break
                await asyncio.sleep(1.2)     # 訪客看回答、想下一句
    except Exception as e:
        errs.append(f"連線層 {type(e).__name__}: {e}")
    out.append((vid, port, lat, errs))


async def main():
    half = N // 2
    tasks, out = [], []
    for v in range(N):
        port, sents = (8002, EN) if v < N - half else (8001, ZH)
        tasks.append(visitor(v, port, sents, out))

    print(f"模擬 {N} 位訪客（英文 {N-half} / 中文 {half}），每人 {ROUNDS} 句")
    print(f"總請求數 {N*ROUNDS}\n")
    t0 = time.perf_counter()
    await asyncio.gather(*tasks)
    wall = time.perf_counter() - t0

    all_lat = [x for _, _, l, _ in out for x in l]
    all_err = [(v, e) for v, _, _, es in out for e in es]
    print(f"總耗時 {wall:.1f}s")
    if all_lat:
        s = sorted(all_lat)
        print(f"回應時間  中位 {statistics.median(s):.2f}s | "
              f"p90 {s[int(len(s)*0.9)]:.2f}s | max {max(s):.2f}s")
    print(f"完成 {len(all_lat)}/{N*ROUNDS} 個請求")
    if all_err:
        print(f"\n❌ 錯誤 {len(all_err)}:")
        for v, e in all_err[:10]:
            print(f"   訪客{v}: {e}")
    else:
        print("✅ 零錯誤")
    print(f"\nload average: {open('/proc/loadavg').read().split()[0]}")


asyncio.run(main())
