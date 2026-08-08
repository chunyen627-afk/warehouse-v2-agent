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
4. **預設開機自動啟動、200×、60 商品全動**（user 定調 2026-08-03）。
   ⚠️ 守衛 892 句與所有寫入測試都假設「除非我寫入否則數字不動」
   ⇒ **跑測試前務必先關**（`POST /api/live_mode {"action":"stop"}`，
   `run_guard_en.sh` 已內建）。

## 節奏依據（實測這份 seed 的真實模式，`_churn.py`）
| | 入庫 | 出庫 |
|---|---|---|
| 筆數占比 | 21% | **79%** |
| 單筆數量 | 中位 **49** 件（少而大）| 中位 **3** 件（多而小）|
真實節奏是每天 63.6 筆 ⇒ 營業 10 小時約**每 9 分鐘一筆**。
展場訪客只待幾分鐘 ⇒ 用 `speedup` 把時間軸加速（**預設 200×** ＝ 2.7 秒一輪），
**比例維持真實**，只是快轉。可對外說「一天濃縮成幾分鐘」。
"""
import asyncio
import random
import threading
import time

import warehouse as W


class LiveConfig:
    # ── 節奏 ──
    #   ⚠️ 2026-08-03 改版：原本「每輪動 1 筆」＝180 個(商品,倉別)組合
    #   平均 **81 分鐘**才輪到同一個 ⇒ 訪客盯著某商品看幾乎不會動。
    #   改成**每輪同時動多筆**（`batch`），讓畫面上大量商品一起跳。
    #   實測單筆 `_do_one` 只要 **2ms**（含真實寫檔），一輪幾十筆零壓力。
    # 預設 **200×**（約 2.7 秒一輪 × 60 商品全動）——user 定調 2026-08-03：
    # 開機就要看到數據在跳，不必手動調。實測資源負擔極低（中英同時全速跑，
    # 問答延遲 0.39s→0.37s 沒被拖慢、load 1.15、記憶體 avail 5.7GB）。
    speedup = 200                 # 時間加速倍率（可現場調，1-400）
    base_interval_s = 9 * 60      # 真實世界的平均間隔（實測 seed 真值）
    jitter = 0.45                 # 間隔隨機抖動 ±45%，避免機械感
    batch = 8                     # 每輪同時動幾筆（可現場調，1-60）
    tick_s = 2.0                  # 最短輪詢間隔（速度拉到最大時的下限）
    # **恆為真**（user 定調 2026-08-03，前端選項已移除）：真實倉庫是所有商品
    # 同時各自進出。**包含訪客後來新增的商品**——`_do_batch` 每輪重讀
    # `W.state().items`，新商品下一輪就納入。
    sweep_all = True

    # ── 進出比例（實測：出庫 79% / 入庫 21%）──
    out_ratio = 0.79

    # ── 單筆數量（實測中位數；小範圍先試）──
    out_qty_range = (1, 4)        # 出庫：多而小
    in_qty_range = (25, 60)       # 入庫：少而大

    # ── 安全護欄：只在安全庫存的這個區間內波動 ──
    # r24c（user 實測回報）：0.80 的地板讓商品在 0.8~1.0×安全庫存間漂——
    #   那段**就是缺貨警示區**；新建商品從 1.0×起跳、79% 先賣 → 一開模擬
    #   數百個新品瞬間集體跌進警示（489 條異常實況）。地板拉到 1.15×：
    #   低於它一律先進貨（新品前幾次自然全是進貨、補到高水位才開賣），
    #   出貨單筆最多 4 件吃不穿 15% 緩衝，警示只留給真的異常。
    floor_ratio = 1.15            # 低於 安全庫存×1.15 → 強制進貨
    ceil_ratio = 1.60             # 高於 安全庫存×1.6 → 強制出貨
    min_qty_floor = 5             # 任何倉別不讓它掉到 5 件以下
    default_safety = 50           # 商品沒設安全庫存時的護欄基準（新增商品常是 0）

    # ── 來源（訪客看得到，證明是「別的系統」在動）──
    actors = ("pda_scan", "wms_sync", "ecom_order")


_state = {
    "on": False,
    "task": None,
    "count": 0,
    "last": "",
    "wake": None,          # asyncio.Event —— 調速時打斷睡眠，讓新間隔**立即生效**
}
_lock = threading.Lock()


def is_on() -> bool:
    return _state["on"]


def status() -> dict:
    return {"on": _state["on"], "count": _state["count"], "last": _state["last"],
            "speedup": LiveConfig.speedup, "batch": LiveConfig.batch,
            "sweep_all": LiveConfig.sweep_all,
            "interval_s": round(_avg_interval(), 1)}


def _avg_interval() -> float:
    """一輪的間隔（秒）。

    ⚠️ 2026-08-03 修：原本把 batch 乘進間隔（想維持「真實的每分鐘筆數」），
    結果 sweep 模式變成 **270 秒才跑一輪** ⇒ 測試 60 秒一格都沒動。
    但那個設計本來就跟需求衝突——展場要的是「所有商品同時在動」，
    不是「維持真實筆數」。
    ⇒ 改成：**間隔只由 speedup 決定**，batch/sweep 決定「一輪動多少商品」。
      speedup 20× → 27 秒一輪；120× → 4.5 秒一輪；200× → 2.7 秒一輪。
    """
    return max(LiveConfig.tick_s,
               LiveConfig.base_interval_s / max(1, LiveConfig.speedup))


def tune(speedup=None, batch=None, sweep_all=None) -> dict:
    """現場調速（滑桿用）。立即生效，不需重開模擬。"""
    if speedup is not None:
        try:
            LiveConfig.speedup = max(1, min(400, int(speedup)))
        except Exception:
            pass
    if batch is not None:
        try:
            LiveConfig.batch = max(1, min(60, int(batch)))
        except Exception:
            pass
    if sweep_all is not None:
        LiveConfig.sweep_all = bool(sweep_all)
    # 🚨 打斷正在睡的迴圈，讓新速度**立刻生效**。
    #   （user 2026-08-03 回報：改速度後要先暫停再執行才會動——因為背景
    #    正 `await sleep(舊間隔)`，20× 要等 27 秒才睡醒，看起來像沒反應。）
    ev = _state.get("wake")
    if ev is not None:
        try:
            ev.set()
        except Exception:
            pass
    return status()


def _pick_move(only_sku: str = ""):
    """挑一筆合理的異動：回 (sku, wh_key, direction, qty, item) 或 None。

    護欄邏輯：先看這個 (商品,倉別) 目前落在安全區間的哪裡，
    低了就補、高了就出，區間內才照真實比例隨機。
    `only_sku`：指定商品（sweep_all 模式用，確保每個商品都輪到）。
    """
    s = W.state()
    items = list(s.items)          # 全收（新商品 safety_stock 可能是 0）
    if only_sku:
        items = [it for it in items if it["sku_id"] == only_sku]
    if not items:
        return None
    random.shuffle(items)
    whs = [w["key"] for w in s.warehouses]

    for it in (items if only_sku else items[:25]):
        sku = it["sku_id"]
        # 安全庫存 0（訪客新增的商品）→ 用預設值當護欄基準，讓它也會動
        ss = it.get("safety_stock") or LiveConfig.default_safety
        floor = max(LiveConfig.min_qty_floor, int(ss * LiveConfig.floor_ratio))
        ceil = int(ss * LiveConfig.ceil_ratio)

        # ⚠️ sweep 模式要保證這個商品真的動得到 → 試過**所有倉別**才放棄
        #   （單抽一個倉別可能剛好卡在地板，那個商品這輪就靜止不動）
        for wh in random.sample(whs, len(whs)):
            cur = s.stock.get(wh, {}).get(sku, 0)
            if cur < floor:
                direction = "in"
            elif cur > ceil:
                direction = "out"
            else:
                direction = "out" if random.random() < LiveConfig.out_ratio else "in"

            if direction == "out":
                qty = random.randint(*LiveConfig.out_qty_range)
                if cur - qty < LiveConfig.min_qty_floor:
                    continue                    # 這倉出不動，換下一個倉
            else:
                qty = random.randint(*LiveConfig.in_qty_range)
            return sku, wh, direction, qty, it
    return None


def _do_one(only_sku: str = "") -> dict | None:
    """執行一筆異動（走現成 commit_movement，含鎖與熱更新）。"""
    picked = _pick_move(only_sku)
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
    """一輪的間隔（帶隨機抖動，避免機械感）。見 `_avg_interval` 的說明。"""
    j = LiveConfig.jitter
    return max(LiveConfig.tick_s,
               _avg_interval() * random.uniform(1 - j, 1 + j))


def _do_batch() -> list:
    """一輪動一批。回實際成功的清單。

    兩種模式：
    - `sweep_all=True`：**每個商品都動一次**（隨機挑倉別）——訪客要的
      「所有商品同時獨立變動」。60 筆 × 2ms ≈ 120ms，零效能壓力。
    - 否則：隨機挑 `batch` 筆（比較貼近真實：不是每個商品每分鐘都在動）。
    """
    out = []
    if LiveConfig.sweep_all:
        s = W.state()
        # ⚠️ 不能用 `it.get("safety_stock")` 過濾——**訪客新增的商品安全庫存
        #   可能是 0**（實測 item_create 流程預設 0），那樣會被排除、永遠不動。
        #   改成全收，護欄用 `_DEFAULT_SS` 兜底。
        items = list(s.items)
        random.shuffle(items)
        for it in items:
            mv = _do_one(only_sku=it["sku_id"])
            if mv:
                out.append(mv)
        return out
    for _ in range(max(1, LiveConfig.batch)):
        mv = _do_one()
        if mv:
            out.append(mv)
    return out


async def _loop(push):
    """背景迴圈：跑一批 → 推畫面 → 睡一個隨機間隔（可被調速打斷）。"""
    _state["wake"] = asyncio.Event()
    while _state["on"]:
        try:
            mvs = await asyncio.get_event_loop().run_in_executor(None, _do_batch)
            if mvs and push:
                await push(mvs)                 # 一次推整批（前端只刷一次畫面）
        except asyncio.CancelledError:
            raise
        except Exception:
            pass                                # 單批失敗不能中斷整條模擬
        # 睡到「時間到」或「有人調速」為止 —— 調速後不必等這輪睡完
        try:
            _state["wake"].clear()
            await asyncio.wait_for(_state["wake"].wait(), timeout=_next_delay())
        except asyncio.TimeoutError:
            pass                                # 正常睡滿一輪
        except asyncio.CancelledError:
            raise
    _state["wake"] = None


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
