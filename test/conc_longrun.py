# -*- coding: utf-8 -*-
"""多裝置齊發長測（r26d）——展場擬真：N 條並發 WS 混合負載跑 M 分鐘。

量測：每請求延遲（送出→done）、逾時/斷線數、答非所問粗篩（done view 空）。
建檔句一律不按確認（零寫入）；含 1 條「亂打字訪客」流（展場鐵則）。

用法（RPI5 上）：python3 conc_longrun.py 8001 4 30   # port, 並發數, 分鐘
"""
import asyncio
import json
import random
import ssl
import sys
import time

import websockets

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
NCLI = int(sys.argv[2]) if len(sys.argv) > 2 else 4
MINS = int(sys.argv[3]) if len(sys.argv) > 3 else 30

ZH = PORT == 8001
POOL_MAIN = ([
    "藍牙耳機庫存", "庫存警示", "本週熱銷", "北倉跟南倉比較", "快到期有哪些",
    "無線滑鼠多少錢", "咖啡機庫存", "新增商品壓測小物一號電子590元",
    "運動用品類庫存", "瑜珈墊安全庫存設多少", "彈珠多少錢", "本月進出貨",
] if ZH else [
    "bluetooth earphones stock", "whats running low", "best sellers this week",
    "compare north and south", "what is expiring soon", "wireless mouse price",
    "add item stress probe one electronics 590", "sports category stock",
    "yoga mat safety stock", "movements this month",
])
POOL_MASH = ([
    "ㄋㄧㄠ", "asdfgh", "庫庫庫存存", "藍牙耳耳機機", "hhhh哈", "新增增商品",
] if ZH else [
    "asdfgh", "stok plz", "wat", "hhhh", "blutooth earfones", "addd item",
])


async def client(cid: int, stats: dict, deadline: float):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    uri = f"wss://localhost:{PORT}/ws?fast=1"
    mash = cid == NCLI - 1          # 最後一條當亂打字訪客
    while time.time() < deadline:
        try:
            async with websockets.connect(uri, ssl=ctx, max_size=None,
                                          open_timeout=20) as ws:
                stats["connects"] += 1
                while time.time() < deadline:
                    q = random.choice(POOL_MASH if (mash and random.random() < 0.6)
                                      else POOL_MAIN)
                    t0 = time.time()
                    await ws.send(json.dumps({"type": "chat", "text": q},
                                             ensure_ascii=False))
                    view = None
                    try:
                        while True:
                            fr = json.loads(await asyncio.wait_for(
                                ws.recv(), timeout=120))
                            if fr.get("type") == "done":
                                view = (fr.get("result") or {}).get("view")
                                break
                    except asyncio.TimeoutError:
                        stats["timeouts"] += 1
                        break               # 斷線重連
                    dt = time.time() - t0
                    stats["n"] += 1
                    stats["sum"] += dt
                    stats["max"] = max(stats["max"], dt)
                    if dt > 30:
                        stats["slow30"] += 1
                    if not view:
                        stats["noview"] += 1
                    # 建檔卡留著會佔 session state → 主動取消
                    if view in ("item_confirm", "item_create_step1",
                                "item_create_step2"):
                        await ws.send(json.dumps(
                            {"type": "chat",
                             "text": "取消" if ZH else "cancel"},
                            ensure_ascii=False))
                        try:
                            while True:
                                fr = json.loads(await asyncio.wait_for(
                                    ws.recv(), timeout=60))
                                if fr.get("type") == "done":
                                    break
                        except asyncio.TimeoutError:
                            stats["timeouts"] += 1
                            break
                    await asyncio.sleep(random.uniform(4, 10))
        except Exception:
            stats["drops"] += 1
            await asyncio.sleep(3)


async def main():
    deadline = time.time() + MINS * 60
    stats = {"n": 0, "sum": 0.0, "max": 0.0, "slow30": 0, "noview": 0,
             "timeouts": 0, "drops": 0, "connects": 0}
    tasks = [asyncio.create_task(client(i, stats, deadline))
             for i in range(NCLI)]
    while time.time() < deadline:
        await asyncio.sleep(60)
        avg = stats["sum"] / max(stats["n"], 1)
        print(f"[{int((deadline-time.time())/60)}m left] n={stats['n']} "
              f"avg={avg:.1f}s max={stats['max']:.1f}s slow30={stats['slow30']} "
              f"noview={stats['noview']} timeout={stats['timeouts']} "
              f"drop={stats['drops']}", flush=True)
    for t in tasks:
        t.cancel()
    avg = stats["sum"] / max(stats["n"], 1)
    print(f"FINAL n={stats['n']} avg={avg:.1f}s max={stats['max']:.1f}s "
          f"slow30={stats['slow30']} noview={stats['noview']} "
          f"timeout={stats['timeouts']} drop={stats['drops']} "
          f"connects={stats['connects']}", flush=True)


asyncio.run(main())
