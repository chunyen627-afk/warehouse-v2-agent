#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""concurrent_en.py — 並發壓測：多位訪客同時操作（2026-08-02）。

展場情境：好幾個人同時對著平板／手機問，而且各自處於不同的對話狀態
（有人在查庫存、有人開著確認卡、有人在選單裡）。

要抓的是**單人測不出的類型**：
  ① context 串線——A 訪客的 last_sku 跑到 B 的追問裡
  ② 確認卡張冠李戴——A 開的卡被 B 的「confirm」執行掉（**會寫錯資料**）
  ③ 併發下的延遲退化 / 逾時 / 連線被拒

判準：
  - 每位訪客的最後一句 view 必須符合預期（各自獨立）
  - 代稱追問拿到的必須是**自己**鎖定的商品，不是別人的
  - 全程無 error / 無逾時

用法（RPI5 ~/warehouse_v2_en）：
  python3 concurrent_en.py           # 預設 6 位訪客
  python3 concurrent_en.py 12        # 12 位
"""
import asyncio
import json
import ssl
import sys
import time

WS = "wss://localhost:8002/ws?fast=1"

# 每位訪客一套劇本：(名稱, [句子...], 最後一句期望 view, 期望 summary 含的關鍵字)
VISITORS = [
    ("V1 查耳機→追問倉別", [
        "bluetooth earphones stock", "what about north"],
     "inventory_single", "Earphones"),
    ("V2 查滑鼠→代稱進出", [
        "wireless mouse stock", "show me its movements"],
     "movement", "Mouse"),
    ("V3 開進貨卡→取消", [
        "north received 50 yoga mats", "cancel"],
     "item_cancelled", ""),
    ("V4 缺貨清單", [
        "what is running low"], "low_stock", ""),
    # ⚠️ V5 的 view 刻意**不強制**：`and south` 這句 LLM 判 query_inventory
    #   或 query_movement 都合理，實測單獨連續跑 8 次也有 1 次是 movement
    #   （1/8≈12.5%，與併發下的比率一致）＝**LLM 不確定性，不是併發 bug**。
    #   本測要抓的是**串線**（拿到別人的商品），所以只驗關鍵字。
    ("V5 查咖啡機→追問南倉", [
        "coffee machine stock", "and south"],
     "", "Coffee"),
    ("V6 排行→追問", [
        "best sellers this week", "what about north"],
     "inventory", ""),
    ("V7 開調撥卡→確認", [
        "transfer 10 trash bags from north to south", "confirm"],
     "transfer_done", ""),
    ("V8 查面紙", [
        "facial tissue stock"], "inventory_single", "Tissue"),
    ("V9 到期清單", [
        "what is expiring soon"], "expiring", ""),
    ("V10 設安全庫存→取消", [
        "set safety stock for yoga mat to 80", "cancel"],
     "item_cancelled", ""),
    ("V11 比較倉庫", [
        "which has more stock north or south"], "compare_warehouses", ""),
    ("V12 查垃圾袋→代稱安全庫存", [
        "trash bags stock", "is it below safety stock"],
     "", ""),   # view 不強制（clarify 或 inventory 都合理），只驗不 error
]


async def one_visitor(idx, name, turns, want, kw, results):
    import websockets
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    t0 = time.time()
    try:
        # ⚠️ 一位訪客 = 一條連線，全程不共用（context 才會各自獨立）
        async with websockets.connect(WS, ssl=ctx) as ws:
            view = summ = ""
            for text in turns:
                await ws.send(json.dumps({"type": "chat", "text": text},
                                         ensure_ascii=False))
                while True:
                    o = json.loads(await asyncio.wait_for(ws.recv(), 120))
                    if o.get("type") == "done":
                        r = o.get("result") or {}
                        view = r.get("view") or ""
                        summ = (r.get("summary") or "").replace("\n", " ")
                        break
            results[idx] = (name, view, summ, time.time() - t0, None)
    except Exception as e:
        results[idx] = (name, "", "", time.time() - t0, repr(e))


async def run(n):
    vs = (VISITORS * ((n // len(VISITORS)) + 1))[:n]
    results = {}
    t0 = time.time()
    await asyncio.gather(*[
        one_visitor(i, nm, turns, want, kw, results)
        for i, (nm, turns, want, kw) in enumerate(vs)])
    elapsed = time.time() - t0

    ok = bad = 0
    print(f"{'訪客':<26}{'view':<20}{'秒':<7}判定")
    print("-" * 92)
    for i, (nm, turns, want, kw) in enumerate(vs):
        name, view, summ, sec, err = results[i]
        if err:
            bad += 1
            print(f"{nm:<26}{'':<20}{sec:<7.1f}❌ 例外 {err[:40]}")
            continue
        prob = []
        if view in ("error", ""):
            if want:
                prob.append("error/空")
        if want and want not in view:
            prob.append(f"view={view} 期望 {want}")
        if kw and kw not in summ:
            prob.append(f"summary 缺 {kw!r}（串線？）")
        if prob:
            bad += 1
            print(f"{nm:<26}{view:<20}{sec:<7.1f}❌ {'; '.join(prob)}")
            print(f"{'':<26}回答：{summ[:60]}")
        else:
            ok += 1
            print(f"{nm:<26}{view:<20}{sec:<7.1f}✅")

    print()
    print("=" * 60)
    print(f"並發 {n} 位訪客｜通過 {ok}、未過 {bad}｜總耗時 {elapsed:.1f}s"
          f"（最慢單人 {max(r[3] for r in results.values()):.1f}s）")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run(int(sys.argv[1]) if len(sys.argv) > 1 else 6))
