#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data_edge.py — 資料邊界：庫存歸零 / 安全庫存設 0（2026-08-03）。

先前所有測試都跑在「正常區間」的 demo 資料上（零庫存 0 筆、安全庫存為 0 的 0 筆）。
本測刻意把資料推到邊界，看整條鏈還說不說得通。

判準不是「有沒有回答」，是**歸零之後每個介面的說法都要成立**：
查詢 / 缺貨清單 / 倉庫比較 / 異常偵測 / 撐天計算 / 再出貨保護，
任一處出現除零、NaN、負數、空白或誤導文字都算 FAIL。

⚠️ 會真的寫入資料 → 跑完必須 reset（見結尾提示）。
走 WS 訪客真實路徑，不直接呼叫 tool。

用法（RPI5 ~/warehouse_v2_en）：python3 data_edge.py
"""
import asyncio
import json
import re
import ssl
import sys

import websockets

WS = "wss://localhost:8002/ws?fast=1"
RESULTS = []

BAD_TOKENS = ["nan", "NaN", "inf", "Infinity", "undefined",
              "ZeroDivision", "Traceback", "None units", "null"]


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


async def ask(ws, text):
    await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))
    while True:
        o = json.loads(await asyncio.wait_for(ws.recv(), 120))
        if o.get("type") == "done":
            r = o.get("result") or {}
            return (r.get("view") or "",
                    (r.get("summary") or "").replace("\n", " "))


def has_bad(text):
    return [b for b in BAD_TOKENS if b in text]


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    if detail:
        print(f"       {detail[:230]}")


def num_after(text, label):
    m = re.search(label + r"\s+(\d+)", text, re.I)
    return int(m.group(1)) if m else None


async def part_a(ws):
    print("=" * 72)
    print("PART A — 庫存歸零（Camping Tent / north）")
    print("=" * 72)

    v, s = await ask(ws, "camping tent stock")
    print(f"  起始狀態: {s[:170]}")
    start_north = num_after(s, "North")
    print(f"  北倉現量 = {start_north}")

    if not start_north:
        check("A0 取得北倉起始量", False, f"抓不到數量: {s[:200]}")
        return
    check("A0 取得北倉起始量", True, f"North={start_north}")

    # 全部出掉 → 歸零
    v, s = await ask(ws, f"north ship out {start_north} camping tent")
    if "confirm" in v:
        v, s = await ask(ws, "confirm")
    check("A1 出清到零有正確落地", v not in ("error",) and not has_bad(s),
          f"view={v} | {s[:200]}")

    v, s = await ask(ws, "camping tent stock")
    now_north = num_after(s, "North")
    check("A2 歸零後查詢：北倉顯示 0", now_north == 0,
          f"North={now_north} | {s[:200]}")
    check("A3 歸零後查詢無異常值", not has_bad(s), f"{s[:200]}")

    v, s = await ask(ws, "camping tent stock in north")
    check("A4 指定零庫存倉查詢正常", v != "error" and not has_bad(s),
          f"view={v} | {s[:200]}")

    v, s = await ask(ws, "compare warehouses for camping tent")
    check("A5 倉庫比較含零庫存倉不炸", v != "error" and not has_bad(s),
          f"view={v} | {s[:200]}")

    v, s = await ask(ws, "what is running out")
    check("A6 缺貨清單含零庫存不炸", v != "error" and not has_bad(s),
          f"view={v} | {s[:200]}")

    v, s = await ask(ws, "how many days of camping tent left in north")
    check("A7 零庫存撐天計算不除零", not has_bad(s),
          f"view={v} | {s[:200]}")

    # 再出貨 → 必須擋
    v, s = await ask(ws, "north ship out 10 camping tent")
    if "confirm" in v:
        v, s = await ask(ws, "confirm")
    low = s.lower()
    blocked = any(k in low for k in
                  ("not enough", "insufficient", "only", "cannot", "no stock",
                   "exceeds", "available")) or v == "error"
    check("A8 零庫存再出貨被擋下", blocked, f"view={v} | {s[:220]}")

    v, s = await ask(ws, "camping tent stock in north")
    n = num_after(s, "North")
    check("A9 被擋後仍為 0（未寫成負數）", n == 0 or n is None,
          f"North={n} | {s[:200]}")


async def part_b(ws):
    print()
    print("=" * 72)
    print("PART B — 安全庫存設 0（Wireless Mouse）")
    print("=" * 72)

    v, s = await ask(ws, "wireless mouse safety stock")
    print(f"  起始: view={v} | {s[:170]}")

    v, s = await ask(ws, "set wireless mouse safety stock to 0")
    opened = "confirm" in v
    if opened:
        v, s = await ask(ws, "confirm")
    check("B1 設 0 有明確結果（不可靜默無反應）",
          v != "" and not has_bad(s), f"view={v} | {s[:230]}")

    v, s = await ask(ws, "wireless mouse safety stock")
    check("B2 設 0 後查詢無異常值", not has_bad(s), f"view={v} | {s[:200]}")

    v, s = await ask(ws, "is wireless mouse below safety stock")
    check("B3 safety=0 的比較判定不炸（除零風險點）",
          v != "error" and not has_bad(s), f"view={v} | {s[:230]}")

    v, s = await ask(ws, "what is running out")
    check("B4 缺貨清單在 safety=0 下不炸",
          v != "error" and not has_bad(s), f"view={v} | {s[:200]}")

    v, s = await ask(ws, "compare warehouses for wireless mouse")
    check("B5 倉庫比較在 safety=0 下不炸",
          v != "error" and not has_bad(s), f"view={v} | {s[:200]}")


def part_c():
    print()
    print("=" * 72)
    print("PART C — 異常偵測在邊界資料下")
    print("=" * 72)
    import urllib.request
    with urllib.request.urlopen("https://localhost:8002/anomalies",
                                context=_ctx(), timeout=90) as r:
        d = json.loads(r.read())
    al = d.get("alerts", [])
    txt = json.dumps(al, ensure_ascii=False)
    check("C1 異常偵測在邊界資料下無異常值", not has_bad(txt),
          f"alerts={len(al)} bad={has_bad(txt)}")

    dls = [a["data"].get("days_left") for a in al
           if a.get("type") == "low_stock" and isinstance(a.get("data"), dict)]
    bad_dl = [x for x in dls if x is not None and (x < 0 or x > 100000)]
    check("C2 撐天無負數/爆量", not bad_dl, f"異常值: {bad_dl[:5]}")

    stars = [a for a in al if a.get("title", "").count("⭐") > 1]
    check("C3 ⭐ 未重複（本輪修復複驗）", not stars, f"{len(stars)} 筆")

    raw = {"north", "central", "south"}
    badwh = [a for a in al if a.get("detail", "").split()
             and a["detail"].split()[0] in raw]
    check("C4 倉名已英文化（本輪修復複驗）", not badwh, f"{len(badwh)} 筆")


async def main():
    ctx = _ctx()
    async with websockets.connect(WS, ssl=ctx, max_size=None) as ws:
        await part_a(ws)
        await part_b(ws)
    part_c()

    print()
    print("=" * 72)
    ok = sum(1 for _, c, _ in RESULTS if c)
    print(f"總計 {ok}/{len(RESULTS)} PASS")
    for n, c, d in RESULTS:
        if not c:
            print(f"  FAIL: {n}")
            print(f"        {d[:320]}")
    print()
    print("⚠️ 本測已改資料 → 記得 reset_demo（密碼 0000）")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
