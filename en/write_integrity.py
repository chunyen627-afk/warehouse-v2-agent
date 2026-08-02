#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""write_integrity.py — 寫入操作的**資料正確性**（2026-08-02，全新角度）。

先前所有測試驗的都是 **view 對不對**（走對路由、開對卡），
但沒有一支驗過**數字有沒有真的寫對**。這是展場最不能出錯的一環：
訪客說「北倉進 50 個滑鼠」，系統開卡說 +50，**實際庫存有沒有 +50？**

要抓的類型（view 全對也可能發生）：
  ① 開卡顯示的數量與實際寫入不符
  ② 寫錯倉別（卡片寫北倉、實際扣中倉）
  ③ 調撥只加不減（或只減不加）→ 總量憑空變動
  ④ 取消後仍然寫入
  ⑤ 重複確認造成雙重寫入

做法：每個案例都 **查詢前值 → 執行 → 查詢後值 → 驗算**，
用系統自己的查詢介面取值（訪客看到什麼就驗什麼）。

⚠️ 會真的改資料 → 每個案例結束後**用反向操作還原**，
   並在最後複查總量是否回到起點。

用法（RPI5 ~/warehouse_v2_en）：python3 write_integrity.py
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


def num_in(text, label):
    """從 summary 抓某倉別的數量，例如 'North 44' → 44。"""
    m = re.search(label + r"\s+(\d+)", text, re.I)
    return int(m.group(1)) if m else None


def total_in(text):
    m = re.search(r"(\d+)\s+units? across", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r":\s*(\d+)\s+units", text, re.I)
    return int(m.group(1)) if m else None


async def case_inbound():
    """進貨 +30 → 驗北倉與總量都 +30 → 反向出貨還原。"""
    import websockets
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    name = "① 進貨 30（北倉）"
    async with websockets.connect(WS, ssl=ctx) as ws:
        _, s0 = await ask(ws, "wireless mouse stock")
        n0, t0 = num_in(s0, "North"), total_in(s0)
        if n0 is None or t0 is None:
            return name, False, f"取不到前值: {s0[:60]}"

        v, s = await ask(ws, "north received 30 wireless mouse")
        if "movement_confirm" not in v:
            return name, False, f"沒開卡: view={v}"
        if "30" not in s:
            return name, False, f"卡片沒顯示 30: {s[:60]}"
        v, s = await ask(ws, "confirm")
        if "done" not in v:
            return name, False, f"確認沒落地: view={v} {s[:50]}"

        _, s1 = await ask(ws, "wireless mouse stock")
        n1, t1 = num_in(s1, "North"), total_in(s1)
        ok = (n1 == n0 + 30) and (t1 == t0 + 30)
        detail = f"北倉 {n0}→{n1}（期望 {n0 + 30}）／總量 {t0}→{t1}（期望 {t0 + 30}）"

        # 還原
        await ask(ws, "north shipped 30 wireless mouse")
        await ask(ws, "confirm")
        _, s2 = await ask(ws, "wireless mouse stock")
        n2 = num_in(s2, "North")
        if n2 != n0:
            detail += f"｜⚠️ 還原失敗 {n2} != {n0}"
            ok = False
        return name, ok, detail


async def case_transfer():
    """調撥 20：北倉 -20、南倉 +20、**總量不變** → 反向還原。"""
    import websockets
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    name = "② 調撥 20（北→南）"
    async with websockets.connect(WS, ssl=ctx) as ws:
        _, s0 = await ask(ws, "trash bags stock")
        n0, so0, t0 = num_in(s0, "North"), num_in(s0, "South"), total_in(s0)
        if None in (n0, so0, t0):
            return name, False, f"取不到前值: {s0[:70]}"

        v, s = await ask(ws, "transfer 20 trash bags from north to south")
        if "transfer_confirm" not in v:
            return name, False, f"沒開卡: view={v}"
        v, s = await ask(ws, "confirm")
        if "done" not in v:
            return name, False, f"確認沒落地: view={v}"

        _, s1 = await ask(ws, "trash bags stock")
        n1, so1, t1 = num_in(s1, "North"), num_in(s1, "South"), total_in(s1)
        ok = (n1 == n0 - 20) and (so1 == so0 + 20) and (t1 == t0)
        detail = (f"北 {n0}→{n1}（期望 {n0 - 20}）／"
                  f"南 {so0}→{so1}（期望 {so0 + 20}）／"
                  f"總量 {t0}→{t1}（**應不變**）")

        await ask(ws, "transfer 20 trash bags from south to north")
        await ask(ws, "confirm")
        _, s2 = await ask(ws, "trash bags stock")
        if num_in(s2, "North") != n0:
            detail += "｜⚠️ 還原失敗"
            ok = False
        return name, ok, detail


async def case_cancel():
    """開卡後取消 → **庫存必須完全不動**。"""
    import websockets
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    name = "③ 開卡後取消（不可寫入）"
    async with websockets.connect(WS, ssl=ctx) as ws:
        _, s0 = await ask(ws, "yoga mat stock")
        t0 = total_in(s0)
        await ask(ws, "north received 999 yoga mats")
        await ask(ws, "cancel")
        _, s1 = await ask(ws, "yoga mat stock")
        t1 = total_in(s1)
        ok = (t0 == t1)
        return name, ok, f"總量 {t0}→{t1}（**取消後應完全不變**）"


async def case_double_confirm():
    """確認後再說一次 confirm → **不可重複寫入**。"""
    import websockets
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    name = "④ 重複 confirm（不可雙重寫入）"
    async with websockets.connect(WS, ssl=ctx) as ws:
        _, s0 = await ask(ws, "steam iron stock")
        t0 = total_in(s0)
        await ask(ws, "north received 10 steam irons")
        await ask(ws, "confirm")
        await ask(ws, "confirm")          # 再確認一次
        _, s1 = await ask(ws, "steam iron stock")
        t1 = total_in(s1)
        ok = (t1 == t0 + 10)
        detail = f"總量 {t0}→{t1}（期望 {t0 + 10}，若為 {t0 + 20} 即雙重寫入）"
        await ask(ws, "north shipped 10 steam irons")
        await ask(ws, "confirm")
        _, s2 = await ask(ws, "steam iron stock")
        if total_in(s2) != t0:
            detail += "｜⚠️ 還原失敗"
            ok = False
        return name, ok, detail


async def main():
    print("=" * 78)
    print("寫入資料正確性（會真的改資料，每案自動還原）")
    print("=" * 78)
    ok_n = bad_n = 0
    for fn in (case_inbound, case_transfer, case_cancel, case_double_confirm):
        try:
            name, ok, detail = await fn()
        except Exception as e:
            name, ok, detail = fn.__name__, False, f"例外 {e!r}"
        if ok:
            ok_n += 1
            print(f"  ✅ {name}")
        else:
            bad_n += 1
            print(f"  ❌ {name}")
        print(f"      {detail}")
    print()
    print("=" * 78)
    print(f"寫入正確性 {ok_n + bad_n} 案：通過 {ok_n}、未過 {bad_n}")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
