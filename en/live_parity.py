#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_parity.py — 中英文版**跑同一套項目**，確保功能對等。

起因：中文版「展不開」——因為我只測英文版就宣稱完成，中文版 server
根本沒有 `/api/live_grid` 端點（前端 fetch 回 404，靜默失敗）。
⇒ 以後動態倉庫相關改動，一律兩版跑這支。

用法：python3 live_parity.py            （兩版都跑）
      python3 live_parity.py 8002       （單跑一版）
"""
import asyncio
import csv
import json
import ssl
import sys
import urllib.request
from pathlib import Path

PORTS = [sys.argv[1]] if len(sys.argv) > 1 else ["8002", "8001"]
ROOTS = {"8002": Path("/home/p400/warehouse_v2_en"),
         "8001": Path("/home/p400/warehouse_v2")}
NAMES = {"8002": "英文版", "8001": "中文版"}
R = []


def check(port, name, cond, detail=""):
    R.append((port, name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if detail:
        print(f"         {detail[:210]}")


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def post(port, payload):
    req = urllib.request.Request(
        f"https://localhost:{port}/api/live_mode",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, context=_ctx(), timeout=30) as r:
        return json.loads(r.read())


def get_grid(port):
    with urllib.request.urlopen(f"https://localhost:{port}/api/live_grid",
                                context=_ctx(), timeout=30) as r:
        return json.loads(r.read())["grid"]


def csv_ok(port):
    p = ROOTS[port] / "warehouse_data/master/stock.csv"
    try:
        rows = list(csv.DictReader(open(p, encoding="utf-8-sig")))
        for r in rows:
            int(r["qty"]); r["warehouse"]; r["sku_id"]
        return len(rows) > 100, f"{len(rows)} 行"
    except Exception as e:
        return False, str(e)[:80]


async def ask(ws, text):
    await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))
    while True:
        o = json.loads(await asyncio.wait_for(ws.recv(), 120))
        if o.get("type") == "done":
            r = o.get("result") or {}
            return (r.get("view") or ""), (r.get("summary") or "").replace("\n", " ")


async def run_one(port):
    import websockets
    print(f"\n{'='*66}\n{NAMES[port]}（{port}）\n{'='*66}")
    post(port, {"action": "stop"})

    # ① 端點存在（中文版就是這裡壞掉）
    try:
        g = get_grid(port)
        check(port, "① /api/live_grid 端點可用", len(g) >= 60,
              f"{len(g)} 商品、範例 {g[0]['name']}")
        shape = all(isinstance(r.get("per"), dict) and
                    all(w in r["per"] for w in ("north", "central", "south")) for r in g)
        check(port, "① grid 每筆都有三倉數量", shape,
              json.dumps(g[0], ensure_ascii=False)[:150])
    except Exception as e:
        check(port, "① /api/live_grid 端點可用", False, f"{type(e).__name__}: {e}")
        return

    # ② 預設 sweep_all=True
    st = post(port, {})
    check(port, "② 預設每輪 60 商品全動（sweep_all=True）",
          st.get("sweep_all") is True, str(st))

    # ③ 調速即時生效（不必暫停再播）
    post(port, {"action": "start", "speedup": 10, "sweep_all": True})
    await asyncio.sleep(2)
    c0 = post(port, {})["count"]
    post(port, {"action": "tune", "speedup": 200})
    await asyncio.sleep(9)
    c1 = post(port, {})["count"]
    check(port, "③ 調速即時生效（10x→200x，9 秒內應大增）", c1 - c0 >= 60,
          f"count {c0} → {c1}（+{c1-c0}）")

    # ④ WS 推 live_batch 且帶 grid
    batches = []
    try:
        async with websockets.connect(f"wss://localhost:{port}/ws?fast=1",
                                      ssl=_ctx(), max_size=None) as ws:
            dl = asyncio.get_event_loop().time() + 25
            while asyncio.get_event_loop().time() < dl and len(batches) < 4:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), 15))
                except asyncio.TimeoutError:
                    break
                if m.get("type") == "live_batch":
                    batches.append(m)
    except Exception as e:
        print(f"    (WS: {type(e).__name__})")
    check(port, "④ WS 推 live_batch 且帶 grid",
          len(batches) >= 2 and all(b.get("grid") for b in batches),
          f"收到 {len(batches)} 批")

    # ⑤ 三倉各自獨立變動
    if len(batches) >= 2:
        def flat(b):
            return {f"{r['sku']}-{w}": r["per"][w]
                    for r in b["grid"] for w in ("north", "central", "south")}
        a, b = flat(batches[0]), flat(batches[-1])
        ch = [k for k in a if a[k] != b[k]]
        check(port, "⑤ 倉別格各自變動", len(ch) >= 30,
              f"{len(ch)}/{len(a)} 格變過，例：" +
              ", ".join(f"{k} {a[k]}→{b[k]}" for k in ch[:3]))

    # ⑥ 背景跑著時訪客寫入仍正確（進貨精確 +N）
    async with websockets.connect(f"wss://localhost:{port}/ws?fast=1",
                                  ssl=_ctx(), max_size=None) as ws:
        gm = {x["sku"]: x for x in get_grid(port)}
        sku = "e07"
        before = gm[sku]["per"]["north"]
        # ⚠️ 中文句要照系統示範的語序（「進貨50件」），數量放動詞後、量詞結尾
        q = "北倉無線滑鼠進貨40件" if port == "8001" else "north received 40 wireless mouse"
        v, s = await ask(ws, q)
        if "confirm" in v:
            v, s = await ask(ws, "confirm" if port == "8002" else "確認")
        await asyncio.sleep(0.4)
        after = {x["sku"]: x for x in get_grid(port)}[sku]["per"]["north"]
        check(port, "⑥ 背景跑著時進貨仍落地", "done" in v, f"view={v} | {s[:130]}")
        check(port, "⑥ 進貨後數量增加", after > before, f"{before} → {after}")

        # ⑦ 調貨總量守恆
        g3 = {x["sku"]: x for x in get_grid(port)}
        t0 = g3[sku]["total"]
        q2 = ("把20件無線滑鼠從北倉調到南倉" if port == "8001"
              else "transfer 20 wireless mouse from north to south")
        v, s = await ask(ws, q2)
        if "confirm" in v:
            v, s = await ask(ws, "confirm" if port == "8002" else "確認")
        await asyncio.sleep(0.4)
        t1 = {x["sku"]: x for x in get_grid(port)}[sku]["total"]
        check(port, "⑦ 調貨有落地", "done" in v, f"view={v} | {s[:130]}")
        check(port, "⑦ 調貨後總量沒暴走", abs(t1 - t0) < 400, f"{t0} → {t1}")

    # ⑧ 收尾
    post(port, {"action": "stop"})
    await asyncio.sleep(0.5)
    ok, d = csv_ok(port)
    check(port, "⑧ stock.csv 未毀損", ok, d)
    g = get_grid(port)
    neg = [(r["name"], w) for r in g for w in ("north", "central", "south")
           if r["per"][w] < 0]
    check(port, "⑧ 無負數庫存", not neg, f"{len(neg)} 筆")
    post(port, {"action": "tune", "speedup": 20})


async def main():
    for p in PORTS:
        await run_one(p)
    print(f"\n{'='*66}")
    for p in PORTS:
        sub = [x for x in R if x[0] == p]
        n = sum(1 for x in sub if x[2])
        print(f"{NAMES[p]}: {n}/{len(sub)} PASS")
        for _, nm, c in sub:
            if not c:
                print(f"   FAIL: {nm}")
    total = sum(1 for x in R if x[2])
    print(f"\n總計 {total}/{len(R)} PASS")
    print("⚠️ 本測改了資料 → 兩版都要 reset_demo")
    return 0 if total == len(R) else 1


sys.exit(asyncio.run(main()))
