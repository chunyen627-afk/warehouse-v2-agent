#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""state_pollution.py — 狀態污染：訪客不照劇本走（2026-08-02）。

前面的多輪測試都是「照流程」：開卡→確認 或 開卡→取消。
但展場訪客會**中途岔開**：開了確認卡不按、跑去問別的、再回來說「確認」。

要抓的是**狀態殘留**造成的錯誤（單一流程測不出來）：
  ① 開卡 → 問別的 → 說「確認」：會不會**確認到那張舊卡**？（最危險，會寫錯資料）
  ② 開卡 A → 直接開卡 B → 確認：確認的是 A 還是 B？
  ③ 開卡 → 重置資料 → 確認：卡片內容已失效，還會執行嗎？
  ④ clarify 選單開著 → 問別的 → 選單的序數還有效嗎？
  ⑤ 開卡 → 等很久（模擬訪客離開）→ 下一位訪客說「確認」
  ⑥ 取消後再說「確認」：會不會誤觸發？

判準：**任何「該作廢的卡卻被執行」都是嚴重錯誤**（會寫錯資料）。
⚠️ 會真的改資料 → 每案檢查庫存有無非預期變動，最後複查總量。

用法（RPI5 ~/warehouse_v2_en）：python3 state_pollution.py
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


def total(text):
    m = re.search(r"(\d+)\s+units?\s+across", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r":\s*(\d+)\s+units", text, re.I)
    return int(m.group(1)) if m else None


async def new_ws():
    import websockets
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return await websockets.connect(WS, ssl=ctx)


async def case_interleave():
    """① 開卡 → 問別的 → 說 confirm：不可確認到舊卡。"""
    ws = await new_ws()
    try:
        _, s0 = await ask(ws, "yoga mat stock")
        t0 = total(s0)
        await ask(ws, "north received 40 yoga mats")      # 開卡
        await ask(ws, "what is running low")               # 岔開
        v, s = await ask(ws, "confirm")                    # 回來確認
        _, s1 = await ask(ws, "yoga mat stock")
        t1 = total(s1)
        wrote = (t1 is not None and t0 is not None and t1 != t0)
        # 判準：岔開後的 confirm 若仍執行舊卡＝危險
        detail = f"view={v}｜庫存 {t0}→{t1}"
        if wrote:
            # 還原
            await ask(ws, "north shipped 40 yoga mats")
            await ask(ws, "confirm")
            return ("① 開卡→問別的→confirm", False,
                    detail + "  ⚠️ **執行了舊卡**（訪客可能已忘記那張卡）")
        return ("① 開卡→問別的→confirm", True, detail + "（未執行舊卡）")
    finally:
        await ws.close()


async def case_card_replace():
    """② 開卡 A → 開卡 B → confirm：只能執行 B。"""
    ws = await new_ws()
    try:
        _, a0 = await ask(ws, "yoga mat stock")
        _, b0 = await ask(ws, "steam iron stock")
        ta0, tb0 = total(a0), total(b0)
        await ask(ws, "north received 40 yoga mats")       # 卡 A
        await ask(ws, "north received 10 steam irons")     # 卡 B
        v, _ = await ask(ws, "confirm")
        _, a1 = await ask(ws, "yoga mat stock")
        _, b1 = await ask(ws, "steam iron stock")
        ta1, tb1 = total(a1), total(b1)
        okA = (ta1 == ta0)          # A 不該被執行
        okB = (tb1 == tb0 + 10)     # B 該被執行
        detail = (f"yoga {ta0}→{ta1}（**應不變**）／"
                  f"steam iron {tb0}→{tb1}（應 +10）")
        # 還原
        if tb1 != tb0:
            await ask(ws, "north shipped 10 steam irons")
            await ask(ws, "confirm")
        if ta1 != ta0:
            await ask(ws, "north shipped 40 yoga mats")
            await ask(ws, "confirm")
        return ("② 開卡A→開卡B→confirm", okA and okB, detail)
    finally:
        await ws.close()


async def case_cancel_then_confirm():
    """⑥ 取消後再說 confirm：不可誤觸發。"""
    ws = await new_ws()
    try:
        _, s0 = await ask(ws, "trash bags stock")
        t0 = total(s0)
        await ask(ws, "north received 30 trash bags")
        await ask(ws, "cancel")
        v, s = await ask(ws, "confirm")
        _, s1 = await ask(ws, "trash bags stock")
        t1 = total(s1)
        ok = (t1 == t0)
        detail = f"view={v}｜庫存 {t0}→{t1}（**取消後應完全不變**）"
        if not ok:
            await ask(ws, "north shipped 30 trash bags")
            await ask(ws, "confirm")
        return ("⑥ 取消後再 confirm", ok, detail)
    finally:
        await ws.close()


async def case_cross_visitor():
    """⑤ A 訪客開卡 → B 訪客說 confirm：絕不可跨連線執行。"""
    wsA = await new_ws()
    wsB = await new_ws()
    try:
        _, s0 = await ask(wsA, "facial tissue stock")
        t0 = total(s0)
        await ask(wsA, "north received 25 facial tissue")   # A 開卡
        v, s = await ask(wsB, "confirm")                    # B 說確認
        _, s1 = await ask(wsA, "facial tissue stock")
        t1 = total(s1)
        ok = (t1 == t0)
        detail = f"B 的 view={v}｜A 的庫存 {t0}→{t1}（**不可被 B 執行**）"
        if not ok:
            await ask(wsA, "north shipped 25 facial tissue")
            await ask(wsA, "confirm")
        return ("⑤ A開卡→B說confirm（跨訪客）", ok, detail)
    finally:
        await wsA.close()
        await wsB.close()


async def case_menu_then_other():
    """④ clarify 選單開著 → 問別的 → 序數是否失效。"""
    ws = await new_ws()
    try:
        v1, s1 = await ask(ws, "coffee stock")             # 產生選單
        await ask(ws, "what is running low")               # 岔開
        v, s = await ask(ws, "the first one")              # 序數
        bad = ("Coffee" in s and "auto-matched" in s.lower())
        detail = f"選單 view={v1} → 岔開後序數 view={v}｜{s[:50]}"
        return ("④ 選單→岔開→序數", not bad, detail)
    finally:
        await ws.close()


async def main():
    print("=" * 92)
    print("狀態污染：訪客不照劇本走（會真的改資料，每案自動還原）")
    print("=" * 92)
    ok_n = bad_n = 0
    for fn in (case_interleave, case_card_replace, case_cancel_then_confirm,
               case_cross_visitor, case_menu_then_other):
        try:
            name, ok, detail = await fn()
        except Exception as e:
            name, ok, detail = fn.__name__, False, f"例外 {e!r}"
        print(f"  {'✅' if ok else '❌'} {name}")
        print(f"      {detail}")
        ok_n += ok
        bad_n += (not ok)
    print()
    print("=" * 92)
    print(f"狀態污染 {ok_n + bad_n} 案：通過 {ok_n}、未過 {bad_n}")
    print("=" * 92)


if __name__ == "__main__":
    asyncio.run(main())
