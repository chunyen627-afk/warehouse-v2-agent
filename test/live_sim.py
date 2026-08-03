"""live_sim.py — 動態倉庫模擬（2026-08-03）。

## 為什麼要有這個
Demo 資料是 2026-05-26 的凍結快照，訪客盯著看**永遠不動** ⇒ 不像真的倉庫。
而業界現代倉儲是 **perpetual inventory（永續盤存制）**：庫存由條碼槍、
RFID、電商訂單等**多個來源即時更新**，絕大多數變動沒有人「對系統下指令」。
⇒ 背景自己在動**才是真實架構**，不是作弊。

## 設計原則
1. **掛不同的 actor**（`pda_scan` / `wms_sync`）——不偽裝成訪客操作。
   訪客查異動時看得到來源，正好證明「Agent 看得到整個倉庫」。
2. **走現成的 `commit_movement`**——它有 `_STOCK_LOCK`、防 TOCTOU 陳舊寫入、
   熱更新記憶體（含坑 33 修好的 `s.movements`）。不另寫一套寫入邏輯。
3. **只在安全區間波動**——低於 `floor_ratio`×安全庫存就強制進貨、
   高於 `ceil_ratio` 就強制出貨 ⇒ 跑三天也不會把資料玩壞。
4. **預設關閉**——守衛 892 句與所有寫入測試都假設「除非我寫入否則數字不動」，
   背景一直改資料會讓它們隨機 FAIL。跑測試前務必關掉。

## 節奏依據（實測這份 seed 的真實模式，`_churn.py`（英文版量測，中文版同一份 seed 結構））
| | 入庫 | 出庫 |
|---|---|---|
| 筆數占比 | 21% | **79%** |
| 單筆數量 | 中位 **49** 件（少而大）| 中位 **3** 件（多而小）|
真實節奏是每天 63.6 筆 ⇒ 營業 10 小時約**每 9 分鐘一筆**。
展場訪客只待幾分鐘 ⇒ 用 `SPEEDUP` 把時間軸加速（預設 20×），
**比例維持真實**，只是快轉。可對外說「一天濃縮成幾分鐘」。
"""
import asyncio
import random
import threading
import time

import warehouse as W


class LiveConfig:
    # ── 節奏 ──
    speedup = 20                  # 時間加速倍率（真實每 9 分鐘一筆 → 約 27 秒）
    base_interval_s = 9 * 60      # 真實世界的平均間隔（實測值）
    jitter = 0.45                 # 間隔隨機抖動 ±45%，避免機械感

    # ── 進出比例（實測：出庫 79% / 入庫 21%）──
    out_ratio = 0.79

    # ── 單筆數量（實測中位數；小範圍先試）──
    out_qty_range = (1, 4)        # 出庫：多而小
    in_qty_range = (25, 60)       # 入庫：少而大

    # ── 安全護欄：只在安全庫存的這個區間內波動 ──
    floor_ratio = 0.80            # 低於 安全庫存×0.8 → 強制進貨
    ceil_ratio = 1.60             # 高於 安全庫存×1.6 → 強制出貨
    min_qty_floor = 5             # 任何倉別不讓它掉到 5 件以下

    # ── 來源（訪客看得到，證明是「別的系統」在動）──
    actors = ("pda_scan", "wms_sync", "ecom_order")


_state = {
    "on": False,
    "task": None,
    "count": 0,
    "last": "",
}
_lock = threading.Lock()


def is_on() -> bool:
    return _state["on"]


def status() -> dict:
    return {"on": _state["on"], "count": _state["count"], "last": _state["last"],
            "speedup": LiveConfig.speedup}


def _pick_move():
    """挑一筆合理的異動：回 (sku, wh_key, direction, qty, item) 或 None。

    護欄邏輯：先看這個 (商品,倉別) 目前落在安全區間的哪裡，
    低了就補、高了就出，區間內才照真實比例隨機。
    """
    s = W.state()
    items = [it for it in s.items if it.get("safety_stock")]
    if not items:
        return None
    random.shuffle(items)
    whs = [w["key"] for w in s.warehouses]

    for it in items[:25]:                      # 抽樣找一個可動的，不掃全表
        sku = it["sku_id"]
        ss = it.get("safety_stock") or 0
        if ss <= 0:
            continue
        wh = random.choice(whs)
        cur = s.stock.get(wh, {}).get(sku, 0)

        floor = max(LiveConfig.min_qty_floor, int(ss * LiveConfig.floor_ratio))
        ceil = int(ss * LiveConfig.ceil_ratio)

        if cur < floor:
            direction = "in"
        elif cur > ceil:
            direction = "out"
        else:
            direction = "out" if random.random() < LiveConfig.out_ratio else "in"

        if direction == "out":
            qty = random.randint(*LiveConfig.out_qty_range)
            # 不讓它出到低於地板（護欄優先於「照比例」）
            if cur - qty < LiveConfig.min_qty_floor:
                continue
        else:
            qty = random.randint(*LiveConfig.in_qty_range)

        return sku, wh, direction, qty, it
    return None


def _do_one() -> dict | None:
    """執行一筆異動（走現成 commit_movement，含鎖與熱更新）。"""
    picked = _pick_move()
    if not picked:
        return None
    sku, wh, direction, qty, it = picked
    s = W.state()
    wh_label = next((w.get("label", wh) for w in s.warehouses if w["key"] == wh), wh)
    pending = {
        "sku": sku, "name": it["name"], "warehouse": wh,
        "warehouse_label": wh_label, "direction": direction,
        "direction_label": "Inbound" if direction == "in" else "Outbound",
        "qty": qty,
    }
    import tools_v2
    actor = random.choice(LiveConfig.actors)
    res = tools_v2.commit_movement(pending, actor=actor,
                                   trace_id=f"live-{int(time.time())}")
    if not res.get("ok"):
        return None
    with _lock:
        _state["count"] += 1
        _state["last"] = (f"{'+' if direction == 'in' else '-'}{qty} "
                          f"{it['name']} @ {wh_label} ({actor})")
    return {"sku": sku, "name": it["name"], "warehouse": wh,
            "warehouse_label": wh_label, "direction": direction,
            "qty": qty, "actor": actor}


def _next_delay() -> float:
    base = LiveConfig.base_interval_s / max(1, LiveConfig.speedup)
    j = LiveConfig.jitter
    return max(3.0, base * random.uniform(1 - j, 1 + j))


async def _loop(push):
    """背景迴圈：跑一筆 → 推畫面 → 睡一個隨機間隔。"""
    while _state["on"]:
        try:
            mv = await asyncio.get_event_loop().run_in_executor(None, _do_one)
            if mv and push:
                await push(mv)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass                                # 單筆失敗不能中斷整條模擬
        try:
            await asyncio.sleep(_next_delay())
        except asyncio.CancelledError:
            raise


def start(loop, push=None) -> dict:
    """開啟模擬。push 是 async function(movement_dict)，由 server 注入。"""
    if _state["on"]:
        return status()
    _state["on"] = True
    _state["task"] = asyncio.run_coroutine_threadsafe(
        _loop(push), loop) if loop else None
    return status()


def start_in_loop(push=None) -> dict:
    """在目前的 event loop 內開啟（server 的 WS handler 用）。"""
    if _state["on"]:
        return status()
    _state["on"] = True
    _state["task"] = asyncio.create_task(_loop(push))
    return status()


def stop() -> dict:
    _state["on"] = False
    t = _state["task"]
    if t is not None:
        try:
            t.cancel()
        except Exception:
            pass
    _state["task"] = None
    return status()
