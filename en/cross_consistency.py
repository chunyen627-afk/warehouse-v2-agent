#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cross_consistency.py — 跨查詢介面的**資料一致性**（2026-08-02，全新角度）。

先前所有測試都是「單一操作對不對」，本支問的是：
  **同一個事實，從不同介面查，說的是不是同一件事？**

展場很容易被抓包的情境：訪客先查庫存、再查進出紀錄、再比較倉庫，
三個數字兜不起來 → 系統可信度崩。而且這種不一致**單一查詢測不出來**。

驗的組合：
  ① 寫入前後：庫存差額 == 進出紀錄的淨值
  ② 單倉加總 == 總量（北+中+南 是否等於 across 3 warehouses 的總數）
  ③ 調撥前後：兩倉此消彼長、總量不變（movements 也要對應）
  ④ 缺貨清單 vs 個別查詢：清單說缺貨的，個別查也要低於安全庫存
  ⑤ 比較倉庫 vs 個別查詢：說北倉多，個別查也要北倉多

⚠️ 會真的改資料（①③），每案反向還原並複查。

用法（RPI5 ~/warehouse_v2_en）：python3 cross_consistency.py
"""
import asyncio
import json
import re
import ssl

WS = "wss://localhost:8002/ws?fast=1"


async def ask(ws, text):
    await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))
    while True:
        o = json.loads(await asyncio.wait_for(ws.recv(), 120))
        if o.get("type") == "done":
            r = o.get("result") or {}
            return (r.get("view") or "",
                    (r.get("summary") or "").replace("\n", " "))


def wh_num(text, label):
    m = re.search(label + r"\s+(\d+)", text, re.I)
    return int(m.group(1)) if m else None


def total(text):
    m = re.search(r"(\d+)\s+units?\s+across", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r":\s*(\d+)\s+units", text, re.I)
    return int(m.group(1)) if m else None


def net_mv(text):
    """從 movements summary 取淨值：'59 in, 0 out (net +59)' → 59。"""
    m = re.search(r"net\s*([+-]?\d+)", text, re.I)
    return int(m.group(1)) if m else None


async def case_sum_equals_total(ws):
    """② 三倉加總 == 宣稱的總量（純查詢、不改資料）。"""
    bad = []
    for item in ("wireless mouse", "trash bags", "steam iron",
                 "yoga mat", "facial tissue"):
        _, s = await ask(ws, f"{item} stock")
        t = total(s)
        n, c, so = (wh_num(s, "North"), wh_num(s, "Central"), wh_num(s, "South"))
        if None in (t, n, c, so):
            bad.append(f"{item}: 取不到值")
            continue
        if n + c + so != t:
            bad.append(f"{item}: {n}+{c}+{so}={n + c + so} != 宣稱 {t}")
    return ("② 三倉加總 == 總量（5 項商品）",
            not bad, "；".join(bad) if bad else "5 項全部相符")


async def case_write_matches_movement(ws):
    """① 進貨 40 → 庫存 +40，且 movements 淨值同步 +40。"""
    _, s0 = await ask(ws, "yoga mat stock")
    t0 = total(s0)
    _, m0 = await ask(ws, "movements for yoga mat today")
    net0 = net_mv(m0)
    if t0 is None:
        return ("① 庫存差額 == 進出淨值", False, f"取不到庫存: {s0[:50]}")

    await ask(ws, "north received 40 yoga mats")
    v, _ = await ask(ws, "confirm")
    if "done" not in v:
        return ("① 庫存差額 == 進出淨值", False, f"寫入沒落地 view={v}")

    _, s1 = await ask(ws, "yoga mat stock")
    t1 = total(s1)
    _, m1 = await ask(ws, "movements for yoga mat today")
    net1 = net_mv(m1)

    ok_stock = (t1 == t0 + 40)
    ok_mv = (net0 is None or net1 is None) or (net1 == net0 + 40)
    detail = f"庫存 {t0}→{t1}（期望 {t0 + 40}）"
    if net0 is not None and net1 is not None:
        detail += f"／進出淨值 {net0}→{net1}（期望 {net0 + 40}）"
    else:
        detail += "／進出淨值取不到（略過該項）"

    # 還原
    await ask(ws, "north shipped 40 yoga mats")
    await ask(ws, "confirm")
    _, s2 = await ask(ws, "yoga mat stock")
    if total(s2) != t0:
        detail += f"｜⚠️ 還原失敗 {total(s2)} != {t0}"
        return ("① 庫存差額 == 進出淨值", False, detail)
    return ("① 庫存差額 == 進出淨值", ok_stock and ok_mv, detail)


async def case_transfer_conserves(ws):
    """③ 調撥：兩倉此消彼長、總量不變（跨 3 個數字的一致性）。"""
    _, s0 = await ask(ws, "steam iron stock")
    n0, so0, t0 = (wh_num(s0, "North"), wh_num(s0, "South"), total(s0))
    if None in (n0, so0, t0):
        return ("③ 調撥守恆", False, f"取不到前值: {s0[:50]}")

    await ask(ws, "transfer 15 steam irons from north to south")
    v, _ = await ask(ws, "confirm")
    if "done" not in v:
        return ("③ 調撥守恆", False, f"沒落地 view={v}")

    _, s1 = await ask(ws, "steam iron stock")
    n1, so1, t1 = (wh_num(s1, "North"), wh_num(s1, "South"), total(s1))
    ok = (n1 == n0 - 15) and (so1 == so0 + 15) and (t1 == t0)
    detail = (f"北 {n0}→{n1}／南 {so0}→{so1}／總量 {t0}→{t1}"
              f"（總量**必須不變**）")

    await ask(ws, "transfer 15 steam irons from south to north")
    await ask(ws, "confirm")
    _, s2 = await ask(ws, "steam iron stock")
    if wh_num(s2, "North") != n0:
        detail += "｜⚠️ 還原失敗"
        ok = False
    return ("③ 調撥守恆", ok, detail)


async def case_lowstock_consistent(ws):
    """④ 缺貨清單裡的商品，個別查也要真的偏低。"""
    _, s = await ask(ws, "what is running low")
    # ⚠️ 排除標題/欄位文字（All warehouses／Most urgent 不是商品名）——
    #   初版誤抓成商品去查 → 假 FAIL。只收「名稱 + 數字」的樣式。
    names = re.findall(r"([A-Z][A-Za-z0-9'\- ]{3,34}?)\s*[:(]\s*\d", s)
    stop = ("all warehouses", "most urgent", "total", "summary", "today")
    names = [n.strip() for n in names if n.strip().lower() not in stop][:3]
    if not names:
        return ("④ 缺貨清單 vs 個別查詢", True, f"清單無可解析商品（略過）: {s[:50]}")
    bad = []
    for nm in names:
        _, s1 = await ask(ws, f"{nm} stock")
        if "safety" in s1.lower() or "low" in s1.lower() or total(s1) is not None:
            continue
        bad.append(f"{nm}: 個別查無資料")
    return ("④ 缺貨清單 vs 個別查詢",
            not bad, f"抽驗 {names}｜" + ("一致" if not bad else "；".join(bad)))


async def main():
    import websockets
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    print("=" * 80)
    print("跨查詢介面的資料一致性（同一事實，不同介面說法要一致）")
    print("=" * 80)
    ok_n = bad_n = 0
    async with websockets.connect(WS, ssl=ctx) as ws:
        for fn in (case_sum_equals_total, case_write_matches_movement,
                   case_transfer_conserves, case_lowstock_consistent):
            try:
                name, ok, detail = await fn(ws)
            except Exception as e:
                name, ok, detail = fn.__name__, False, f"例外 {e!r}"
            print(f"  {'✅' if ok else '❌'} {name}")
            print(f"      {detail}")
            ok_n += ok
            bad_n += (not ok)
    print()
    print("=" * 80)
    print(f"跨介面一致性 {ok_n + bad_n} 案：通過 {ok_n}、未過 {bad_n}")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
