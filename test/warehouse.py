"""
warehouse.py — 倉管業務邏輯 (v3.8)

職責：
  1. 從 seed_data.json 載入冷凍快照
  2. 提供 5 個 function 的純 Python 實作
  3. 提供 match_items() keyword → SKU substring match 引擎
  4. 提供 reset() 重新載入種子資料

設計原則：
  - 不依賴 server / FastAPI、純資料層
  - 所有 function 回傳 dict {ok, summary, data, view}
  - SKU 不在 system prompt 內、靠 match_items() 找
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date as _date, timedelta as _td
from pathlib import Path
from typing import Any

_log = logging.getLogger("warehouse")

# ────────────────────────────────────────────────
# v2 資料來源開關：seed（單檔，相容 v1）/ multi（warehouse_data/ 多檔）
#   環境變數 WAREHOUSE_DATA_MODE 控制；預設 multi（v2 上線取代 v1）。
# ────────────────────────────────────────────────
import os as _os

DATA_MODE = _os.getenv("WAREHOUSE_DATA_MODE", "multi").lower()


def _load_seed_dict(seed_path: Path) -> dict:
    """依 DATA_MODE 回傳 seed 等價 dict。multi 模式從 warehouse_data/ 組回。"""
    if DATA_MODE == "multi":
        wd = Path(seed_path).parent / "warehouse_data"
        if wd.exists():
            try:
                import loader_v2
                return loader_v2.load_as_seed(wd, seed_fallback=seed_path)
            except Exception as e:   # multi 載入失敗 → fallback seed，永不讓 app 掛掉
                _log.warning(f"[loader_v2] multi 載入失敗，fallback seed：{e}")
    with open(seed_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ────────────────────────────────────────────────
# Label 對照表
# ────────────────────────────────────────────────

CATEGORY_LABEL = {
    "electronics":       "電子產品",
    "appliance_kitchen": "家電廚具",
    "food_beverage":     "食品飲料",
    "daily_goods":       "日用品",
    "apparel":           "服飾",
    "sports":            "運動用品",
}

WAREHOUSE_LABEL = {
    "north":   "北區倉",
    "central": "中區倉",
    "south":   "南區倉",
    "all":     "全部倉",
}

PERIOD_LABEL = {
    "today":      "今天",
    "this_week":  "本週",
    "this_month": "本月",
}

_KW_TO_CAT = {
    "電子": "electronics",  "藍牙": "electronics",  "耳機": "electronics",
    "喇叭": "electronics",  "手機": "electronics",  "充電": "electronics",
    "3c":   "electronics",  "電腦": "electronics",
    "廚具": "appliance_kitchen", "家電": "appliance_kitchen", "鍋": "appliance_kitchen",
    "果汁機": "appliance_kitchen", "烤": "appliance_kitchen",
    "食品": "food_beverage", "飲料": "food_beverage", "零食": "food_beverage",
    "水": "food_beverage",  "茶": "food_beverage",  "咖啡": "food_beverage",
    "日用": "daily_goods",  "清潔": "daily_goods",  "洗衣": "daily_goods",
    "洗碗": "daily_goods",  "衛生": "daily_goods",  "紙": "daily_goods",
    "服飾": "apparel",      "衣": "apparel",         "褲": "apparel",
    "運動": "sports",       "球": "sports",          "健身": "sports",
}


def _suggest_on_empty(keyword: str, action: str = "庫存") -> dict:
    """
    查無商品時，根據 keyword 猜類別，回 clarify 結構（view="clarify"）。
    action: 要做的動作文字，如「庫存」「進出貨」「帳差異」
    """
    # 猜類別
    guessed_cat = None
    guessed_label = None
    for kw_frag, cat in _KW_TO_CAT.items():
        if kw_frag in (keyword or ""):
            guessed_cat = cat
            guessed_label = CATEGORY_LABEL.get(cat, cat)
            break

    opts = []
    if guessed_label:
        opts.append(f"{guessed_label}類 {action}")
        opts.append(f"{guessed_label}類 缺貨警示")
        opts.append(f"{guessed_label}類 熱銷排行")
    else:
        opts.append(f"所有商品 {action}")
        opts.append("哪些商品快缺貨")
        opts.append("本月熱銷商品")
    opts.append("查倉管")  # 永遠有個「看功能列表」兜底

    hint_kw = f"「{keyword}」" if keyword else ""
    question = f"找不到 {hint_kw}相關商品，你是想查："

    return {
        "ok": True,
        "summary": question,
        "view": "clarify",
        "data": {
            "question": question,
            "options":  opts,
            "hint":     "輸入數字選擇，或直接輸入完整商品名稱",
        },
    }


DIRECTION_LABEL = {
    "in":   "進貨",
    "out":  "出貨",
    "both": "進出",
}

METRIC_LABEL = {
    "stock_value": "庫存價值",
    "item_count":  "商品數量",
    "turnover":    "週轉率",
}

RANK_LABEL = {
    "hot":  "熱銷",
    "slow": "滯銷",
}


# ────────────────────────────────────────────────
# 購物籃索引（market basket / 連帶分析）
# ────────────────────────────────────────────────
def _build_basket_index(orders: list[dict]) -> dict:
    """從 orders 算共現,回:
      {
        "single": {sku: 出現訂單數},
        "pair":   {sku: {co_sku: 同捆訂單數}},
        "total":  訂單總數,
      }
    啟動 / reset 時算一次,query_related_items 直接查。
    """
    single: dict[str, int] = defaultdict(int)
    pair: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for o in orders:
        skus = o.get("lines", [])
        for a in skus:
            single[a] += 1
        for i in range(len(skus)):
            for j in range(i + 1, len(skus)):
                a, b = skus[i], skus[j]
                pair[a][b] += 1
                pair[b][a] += 1
    return {
        "single": dict(single),
        "pair":   {k: dict(v) for k, v in pair.items()},
        "total":  len(orders),
    }


# ────────────────────────────────────────────────
# State (單例)
# ────────────────────────────────────────────────
class State:
    def __init__(self, seed_path: Path):
        self.seed_path = seed_path
        self.reset()

    def reset(self):
        seed = _load_seed_dict(self.seed_path)
        self.snapshot_date  = seed.get("snapshot_date", "")
        self.schema_version = seed.get("schema_version", 1)
        self.categories     = seed.get("categories", [])
        self.warehouses     = seed.get("warehouses", [])
        self.items          = seed.get("items", [])
        self.stock          = seed.get("stock", {})
        self.movements      = seed.get("movements", [])
        self.orders         = seed.get("orders", [])
        self.association_meta = seed.get("association_meta", {})
        self.batches        = seed.get("batches", [])      # 保存期限批次 (v3.9+)
        self.shelf_life     = seed.get("shelf_life", {})   # {sku: shelf_life_days}
        # 索引（給 match_items 快）
        self._items_by_sku  = {it["sku_id"]: it for it in self.items}
        # 購物籃索引（給 query_related_items 用、reset 時重算）
        self._basket_index  = _build_basket_index(self.orders)
        # ── v2 專屬區塊（multi 模式才有；給三金剛用，v1 function 不讀）──
        self.v2_config    = seed.get("_v2_config", {})
        self.v2_suppliers = seed.get("_v2_suppliers", [])
        self.v2_data_dir  = seed.get("_v2_data_dir", "")


_STATE: State | None = None


def init(seed_path: Path | str):
    global _STATE
    _STATE = State(Path(seed_path))


def reset():
    if _STATE is None:
        raise RuntimeError("State not initialized — call init() first")
    _STATE.reset()


def state() -> State:
    if _STATE is None:
        raise RuntimeError("State not initialized — call init() first")
    return _STATE


# ────────────────────────────────────────────────
# Dashboard 快照（給前端初始載入用）
# ────────────────────────────────────────────────
def dashboard_snapshot() -> dict:
    s = state()
    # 各倉庫存價值總覽
    wh_summary = []
    for wh in s.warehouses:
        wh_key = wh["key"]
        total_value = 0
        total_items = 0
        for sku, qty in s.stock.get(wh_key, {}).items():
            item = s._items_by_sku.get(sku)
            if item:
                total_value += item["unit_price"] * qty
                total_items += qty
        wh_summary.append({
            "warehouse":   wh_key,
            "label":       wh["label"],
            "stock_value": total_value,
            "item_count":  total_items,
        })

    # 低庫存項目數
    n_low = sum(
        1
        for it in s.items
        for wh in s.warehouses
        if s.stock.get(wh["key"], {}).get(it["sku_id"], 0) < it["safety_stock"]
    )

    return {
        "snapshot_date":   s.snapshot_date,
        "warehouse_count": len(s.warehouses),
        "sku_count":       len(s.items),
        "low_stock_count": n_low,
        "warehouse_summary": wh_summary,
    }


# ────────────────────────────────────────────────
# Helper
# ────────────────────────────────────────────────
def _snapshot_date() -> _date:
    s = state().snapshot_date
    if s:
        try:
            return _date.fromisoformat(s)
        except Exception:
            pass
    return _date.today()


def _period_range(period: str) -> tuple[_date, _date]:
    today = _snapshot_date()
    if period == "today":
        return today, today
    if period == "this_week":
        return today - _td(days=today.weekday()), today
    if period == "this_month":
        return today.replace(day=1), today
    return today, today


def _err(msg: str, view: str = "error") -> dict:
    return {"ok": False, "summary": msg, "data": {}, "view": view}


# ────────────────────────────────────────────────
# match_items — keyword → SKU substring match 引擎
# ────────────────────────────────────────────────

def _tokenize(keyword: str) -> list[str]:
    """切 keyword 成 token list。
    "藍牙耳機"     → ["藍牙耳機"]
    "黑咖啡 1kg"   → ["黑咖啡", "1kg"]
    "USB-C 快充線" → ["USB-C", "快充線"]
    """
    if not keyword:
        return []
    raw = keyword.replace("，", " ").replace("、", " ").replace("/", " ")
    return [t.strip() for t in raw.split() if t.strip()]


def match_items(keyword: str, category: str | None = None) -> list[dict]:
    """根據 keyword + category 找 SKU。回 [{item, score}, ...]、按 score 倒序。
    Score 算法：
      - keyword 每個 token 出現在 item.name → +len(token)
      - 整個 keyword 出現在 item.name → +5 bonus
      - category 過濾在前
    """
    s = state()
    items = s.items
    if category:
        items = [it for it in items if it["category"] == category]

    if not keyword:
        return [{"item": it, "score": 1} for it in items]

    tokens = _tokenize(keyword)
    if not tokens:
        return [{"item": it, "score": 1} for it in items]

    # 空白不敏感:把 keyword 跟商品名的所有空白都去掉再比
    # (解「運動毛巾100x30cm」沒空白 vs 商品名「運動毛巾 100x30cm」有空白 → match 不到的 bug)
    def _nospace(t: str) -> str:
        return "".join(t.split())

    kw_ns = _nospace(keyword)

    results = []
    for it in items:
        name = it["name"]
        name_ns = _nospace(name)
        score = 0
        for tok in tokens:
            if tok in name:
                score += len(tok)
            else:
                # token 帶空白雜訊時、用去空白版再比一次(半個 token 也給分)
                tok_ns = _nospace(tok)
                if tok_ns and tok_ns in name_ns:
                    score += len(tok_ns)
        # 整串 keyword 命中(含去空白版)→ bonus
        if keyword in name or (kw_ns and kw_ns in name_ns):
            score += 5
        if score > 0:
            results.append({"item": it, "score": score})

    results.sort(key=lambda r: -r["score"])
    return results


# ────────────────────────────────────────────────
# Stock & movement aggregation helpers
# ────────────────────────────────────────────────

def _sku_total_stock(sku: str, warehouse: str = "all") -> tuple[int, dict]:
    """回 (總量, 各倉 dict)。warehouse='all' 就加總、否則只看單倉。"""
    s = state()
    per_wh = {}
    if warehouse == "all":
        for wh in s.warehouses:
            wh_key = wh["key"]
            per_wh[wh_key] = s.stock.get(wh_key, {}).get(sku, 0)
    else:
        per_wh[warehouse] = s.stock.get(warehouse, {}).get(sku, 0)
    return sum(per_wh.values()), per_wh


def _aggregate_movements(
    period: str,
    sku_filter: set[str] | None = None,
    direction_filter: str | None = None,
    warehouse_filter: str | None = None,
) -> dict:
    """聚合 movements。回 {in_qty, out_qty, daily: [{date, in, out}, ...]}。"""
    s = state()
    start, end = _period_range(period)
    in_qty = 0
    out_qty = 0
    daily = defaultdict(lambda: {"in": 0, "out": 0})

    for m in s.movements:
        try:
            d = _date.fromisoformat(m["date"])
        except Exception:
            continue
        if d < start or d > end:
            continue
        if sku_filter and m["sku_id"] not in sku_filter:
            continue
        if warehouse_filter and warehouse_filter != "all" and m["warehouse"] != warehouse_filter:
            continue
        if direction_filter and direction_filter != "both" and m["direction"] != direction_filter:
            continue
        qty = m["qty"]
        if m["direction"] == "in":
            in_qty += qty
            daily[m["date"]]["in"] += qty
        elif m["direction"] == "out":
            out_qty += qty
            daily[m["date"]]["out"] += qty

    return {
        "in_qty":  in_qty,
        "out_qty": out_qty,
        "daily":   [
            {"date": d, "in": v["in"], "out": v["out"]}
            for d, v in sorted(daily.items())
        ],
    }


# ────────────────────────────────────────────────
# 5 個 function 實作
# ────────────────────────────────────────────────

def query_inventory(
    keyword: str | None = None,
    category: str | None = None,
    warehouse: str = "all",
) -> dict:
    """1. 查庫存（keyword + category + warehouse 任意組合、server 端 fuzzy match）"""
    s = state()
    # keyword 和 category 都空 → 查全倉概覽（前10筆）
    if not keyword and not category:
        wh_f = warehouse if warehouse in ("north", "central", "south") else "all"
        wh_label_all = WAREHOUSE_LABEL.get(wh_f, "全部倉")
        all_items = state().items
        rows = []
        for it in all_items[:10]:
            total, _ = _sku_total_stock(it["sku_id"], wh_f)
            rows.append({"sku_id": it["sku_id"], "name": it["name"],
                         "category": CATEGORY_LABEL.get(it["category"], it["category"]),
                         "qty": total, "unit": it.get("unit", "件")})
        return {"ok": True,
                "summary": f"目前共 {len(all_items)} 項商品，以下為前 {len(rows)} 筆庫存概覽",
                "view": "inventory",
                "data": {"warehouse": wh_f, "warehouse_label": wh_label_all,
                         "keyword": "", "category": None,
                         "total_items": len(all_items), "rows": rows}}

    if warehouse not in ("north", "central", "south", "all"):
        warehouse = "all"
    wh_label = WAREHOUSE_LABEL.get(warehouse, warehouse)

    matches = match_items(keyword or "", category=category)
    if not matches:
        return _suggest_on_empty(keyword or category or "", action="庫存查詢")

    # 純 category 查詢 → 列出該類別所有商品（不進 clarify 選單）
    if category and not keyword:
        rows = []
        for r in matches:
            it = r["item"]
            total, per_wh = _sku_total_stock(it["sku_id"], warehouse)
            rows.append({"sku_id": it["sku_id"], "name": it["name"],
                         "category": CATEGORY_LABEL.get(it["category"], it["category"]),
                         "qty": total, "unit": it.get("unit", "件"),
                         "per_warehouse": per_wh})
        cat_label = CATEGORY_LABEL.get(category, category)
        wh_label_f = WAREHOUSE_LABEL.get(warehouse, "全部倉")
        return {"ok": True,
                "summary": f"{cat_label}類別（{wh_label_f}）：共 {len(rows)} 項，總庫存 {sum(r['qty'] for r in rows)} 件",
                "view": "inventory",
                "data": {"category": category, "category_label": cat_label,
                         "warehouse": warehouse, "warehouse_label": wh_label_f,
                         "total_items": len(rows), "rows": rows}}

    # 多筆 match → clarify 讓使用者選哪個商品
    if len(matches) > 1:
        scope = CATEGORY_LABEL.get(category, "") if category else (keyword or "")
        opts = [r["item"]["name"] + " 庫存" for r in matches[:3]]
        # 加同類別查詢選項
        first_cat = matches[0]["item"]["category"]
        cat_label = CATEGORY_LABEL.get(first_cat, first_cat)
        if len(set(r["item"]["category"] for r in matches)) == 1:
            opts.append(f"{cat_label}類 全部庫存")
        else:
            opts.append("全部商品庫存")
        question = f"找到 {len(matches)} 筆「{scope}」相關商品，你想查哪個？"
        return {
            "ok": True,
            "summary": question,
            "view": "clarify",
            "data": {
                "question": question,
                "options":  opts,
                "hint":     "輸入數字選擇，或直接輸入完整商品名稱",
            },
        }

    # 單筆 match
    it = matches[0]["item"]
    total, per_wh = _sku_total_stock(it["sku_id"], warehouse)
    value = total * it["unit_price"]
    is_low = total < it["safety_stock"] and warehouse == "all"

    if warehouse == "all":
        per_wh_text = "、".join(
            f"{WAREHOUSE_LABEL[k]} {v}" for k, v in per_wh.items()
        )
        summary = (
            f"{it['name']}：三倉共 {total} 件（{per_wh_text}），"
            f"價值約 NT$ {value:,}"
        )
        if is_low:
            summary += f"\n⚠️ 低於安全庫存（{it['safety_stock']}）"
    else:
        summary = (
            f"{it['name']} 在{wh_label}:{per_wh.get(warehouse, 0)} 件，"
            f"價值約 NT$ {value:,}"
        )

    # 到期警示(若該品有保存期限、找最快到期的批)
    next_exp = _next_expiring_batch(it["sku_id"])
    if next_exp and next_exp["level"] in ("red", "orange", "yellow"):
        summary += (
            f"\n{next_exp['level_emoji']} {next_exp['warehouse_label']}有一批"
            f"{next_exp['qty']} 件、{next_exp['days_to_expire']} 天到期"
        )

    return {
        "ok": True,
        "summary": summary,
        "data": {
            "keyword":         keyword,
            "category":        category,
            "warehouse":       warehouse,
            "warehouse_label": wh_label,
            "sku_id":          it["sku_id"],
            "name":            it["name"],
            "category_label":  CATEGORY_LABEL.get(it["category"], it["category"]),
            "unit_price":      it["unit_price"],
            "safety_stock":    it["safety_stock"],
            "total_qty":       total,
            "per_warehouse":   per_wh,
            "value":           value,
            "is_low_stock":    is_low,
            "next_expiring":   next_exp,  # 給前端顯示徽章用(None 表示無保存期限)
        },
        "view": "inventory_single",
    }


def query_movement(
    period: str = "today",
    keyword: str | None = None,
    direction: str = "both",
    warehouse: str = "all",
) -> dict:
    """2. 進出貨記錄"""
    if period not in ("today", "this_week", "this_month"):
        period = "today"
    if direction not in ("in", "out", "both"):
        direction = "both"
    period_label = PERIOD_LABEL[period]
    dir_label    = DIRECTION_LABEL[direction]

    sku_filter = None
    matched_item_label = ""
    if keyword:
        matches = match_items(keyword)
        if matches:
            sku_filter = {r["item"]["sku_id"] for r in matches[:5]}
            if len(matches) == 1:
                matched_item_label = matches[0]["item"]["name"]
            else:
                matched_item_label = f"{len(matches)} 筆相關商品"
        else:
            return _suggest_on_empty(keyword, action="進出貨紀錄")

    agg = _aggregate_movements(
        period,
        sku_filter=sku_filter,
        direction_filter=direction,
        warehouse_filter=warehouse,
    )

    scope = matched_item_label or "全部商品"
    in_qty, out_qty = agg["in_qty"], agg["out_qty"]
    delta = in_qty - out_qty

    if direction == "in":
        summary = f"{period_label}{scope}進貨 {in_qty:,} 件"
    elif direction == "out":
        summary = f"{period_label}{scope}出貨 {out_qty:,} 件"
    else:
        summary = (
            f"{period_label}{scope}:進貨 {in_qty:,} 件、出貨 {out_qty:,} 件"
            f"（淨變動 {delta:+,}）"
        )

    return {
        "ok": True,
        "summary": summary,
        "data": {
            "period":          period,
            "period_label":    period_label,
            "keyword":         keyword,
            "direction":       direction,
            "direction_label": dir_label,
            "warehouse":       warehouse,
            "in_qty":          in_qty,
            "out_qty":         out_qty,
            "delta":           delta,
            "daily":           agg["daily"],
            "matched_scope":   scope,
        },
        "view": "movement",
    }


def list_low_stock(
    warehouse: str = "all",
    category: str | None = None,
) -> dict:
    """3. 列低庫存商品（一鍵警示）"""
    s = state()
    if warehouse not in ("north", "central", "south", "all"):
        warehouse = "all"
    wh_label = WAREHOUSE_LABEL.get(warehouse, warehouse)
    cat_label = CATEGORY_LABEL.get(category, "") if category else ""

    warnings = []
    for it in s.items:
        if category and it["category"] != category:
            continue
        sku = it["sku_id"]
        safety = it["safety_stock"]

        # 先算一次該品的撐幾天 + 建議補貨(用全倉預測,該品所有倉警示共用)
        fc = _stock_forecast(sku)

        def _wh_burn_days_left(wh_key, qty):
            """該倉撐幾天 = 該倉現量 ÷ 該倉日均消耗(沒消耗就 999)"""
            wh_series = _daily_out_series(sku, 30, warehouse=wh_key)
            burn = sum(wh_series) / len(wh_series) if wh_series else 0
            if burn <= 0:
                return 999, 0
            return int(qty / burn), round(burn, 1)

        def _make_warning(wh_key, qty):
            days_left, burn = _wh_burn_days_left(wh_key, qty)
            # 建議補貨:該倉補到 14 天 + 2 天緩衝
            target = max(safety, 14 * burn) + 2 * burn
            suggest = max(0, int(round(target - qty)))
            return {
                "sku_id":          sku,
                "name":            it["name"],
                "category":        it["category"],
                "category_label":  CATEGORY_LABEL.get(it["category"], it["category"]),
                "warehouse":       wh_key,
                "warehouse_label": WAREHOUSE_LABEL.get(wh_key, wh_key),
                "qty":             qty,
                "safety_stock":    safety,
                "shortage_pct":    round((safety - qty) / safety * 100, 1),
                "days_left":       days_left,
                "daily_burn":      burn,
                "suggest_qty":     suggest,
            }

        if warehouse == "all":
            _, per_wh = _sku_total_stock(sku, "all")
            for wh_key, qty in per_wh.items():
                if qty < safety:
                    warnings.append(_make_warning(wh_key, qty))
        else:
            qty = s.stock.get(warehouse, {}).get(sku, 0)
            if qty < safety:
                warnings.append(_make_warning(warehouse, qty))

    # 排序:撐天數少的(最急)排前面、平手用缺口 %
    warnings.sort(key=lambda w: (w["days_left"], -w["shortage_pct"]))

    scope_text = f"{wh_label}"
    if cat_label:
        scope_text += f"{cat_label}類"

    if not warnings:
        return {
            "ok": True,
            "summary": f"{scope_text}目前沒有低於安全庫存的商品 ✅",
            "data": {
                "warehouse": warehouse,
                "category":  category,
                "warnings":  [],
            },
            "view": "low_stock",
        }

    top = warnings[0]
    # 撐幾天提示:< 60 天顯示具體,>=60 天標示「庫存撐得住」
    if top["days_left"] < 60:
        urgent_text = f"剩 {top['qty']} 件、再 {top['days_left']} 天斷貨、建議補 {top['suggest_qty']} 件"
    else:
        urgent_text = f"剩 {top['qty']} 件、缺口 {top['shortage_pct']:.0f}%、建議補 {top['suggest_qty']} 件"
    summary = (
        f"⚠️ {scope_text}有 {len(warnings)} 項商品低於安全庫存\n"
        f"最緊急:{top['name']}({top['warehouse_label']}, {urgent_text})"
    )
    return {
        "ok": True,
        "summary": summary,
        "data": {
            "warehouse":       warehouse,
            "warehouse_label": wh_label,
            "category":        category,
            "category_label":  cat_label,
            "warnings":        warnings,
            "count":           len(warnings),
        },
        "view": "low_stock",
    }


def compare_warehouses(
    warehouse_a: str,
    warehouse_b: str,
    metric: str = "stock_value",
) -> dict:
    """4. 兩倉比較"""
    s = state()
    valid_wh = {"north", "central", "south"}
    if warehouse_a not in valid_wh or warehouse_b not in valid_wh:
        return _err(f"倉庫只支援 north/central/south，請重新指定")
    if warehouse_a == warehouse_b:
        return _err(f"兩個倉相同（{WAREHOUSE_LABEL[warehouse_a]}）、無法比較")
    if metric not in METRIC_LABEL:
        metric = "stock_value"
    metric_label = METRIC_LABEL[metric]
    a_label = WAREHOUSE_LABEL[warehouse_a]
    b_label = WAREHOUSE_LABEL[warehouse_b]

    def _calc(wh_key):
        stock = s.stock.get(wh_key, {})
        if metric == "stock_value":
            return sum(
                qty * s._items_by_sku.get(sku, {}).get("unit_price", 0)
                for sku, qty in stock.items()
            )
        elif metric == "item_count":
            return sum(stock.values())
        elif metric == "turnover":
            total_stock = sum(stock.values()) or 1
            start = _snapshot_date() - _td(days=30)
            out_qty = sum(
                m["qty"]
                for m in s.movements
                if m["warehouse"] == wh_key
                and m["direction"] == "out"
                and _date.fromisoformat(m["date"]) >= start
            )
            return round(out_qty / total_stock, 3)
        return 0

    val_a = _calc(warehouse_a)
    val_b = _calc(warehouse_b)
    if val_a > val_b:
        winner, gap = a_label, val_a - val_b
    elif val_b > val_a:
        winner, gap = b_label, val_b - val_a
    else:
        winner, gap = "平手", 0

    if metric == "stock_value":
        a_text = f"NT$ {val_a:,}"
        b_text = f"NT$ {val_b:,}"
        gap_text = f"NT$ {gap:,}"
    elif metric == "item_count":
        a_text = f"{val_a:,} 件"
        b_text = f"{val_b:,} 件"
        gap_text = f"{gap:,} 件"
    else:
        a_text = f"{val_a:.3f}"
        b_text = f"{val_b:.3f}"
        gap_text = f"{gap:.3f}"

    if winner == "平手":
        summary = f"{a_label} vs {b_label} {metric_label}相同（{a_text}）"
    else:
        summary = (
            f"{a_label} {metric_label} {a_text}、"
            f"{b_label} {metric_label} {b_text}\n"
            f"{winner}領先 {gap_text}"
        )

    return {
        "ok": True,
        "summary": summary,
        "data": {
            "warehouse_a":       warehouse_a,
            "warehouse_a_label": a_label,
            "warehouse_b":       warehouse_b,
            "warehouse_b_label": b_label,
            "metric":            metric,
            "metric_label":      metric_label,
            "value_a":           val_a,
            "value_b":           val_b,
            "winner":            winner,
            "gap":               gap,
        },
        "view": "compare_warehouses",
    }


def list_hot_items(
    rank_type: str = "hot",
    period: str = "this_week",
    category: str | None = None,
) -> dict:
    """5. 熱銷 / 滯銷排行"""
    s = state()
    # ── 庫存排行（不看出貨，只看現有庫存量）──
    if rank_type == "stock":
        cat_label = CATEGORY_LABEL.get(category, "") if category else ""
        rankings = []
        for it in s.items:
            if category and it["category"] != category:
                continue
            total_qty, _ = _sku_total_stock(it["sku_id"], "all")
            rankings.append({
                "sku_id": it["sku_id"], "name": it["name"],
                "category": it["category"],
                "category_label": CATEGORY_LABEL.get(it["category"], it["category"]),
                "stock_qty": total_qty,
                "unit_price": it["unit_price"],
                "stock_value": total_qty * it["unit_price"],
            })
        rankings.sort(key=lambda r: -r["stock_qty"])
        top = rankings[:10]
        if top:
            scope = f"{cat_label}類" if cat_label else "全類別"
            top_item = top[0]
            summary = (f"📦 {scope}庫存排行 TOP {len(top)}\n"
                       f"第 1 名: {top_item['name']}（庫存 {top_item['stock_qty']:,} 件，"
                       f"價值 NT$ {top_item['stock_value']:,}）")
        else:
            summary = "目前沒有庫存資料"
        return {"ok": True, "summary": summary, "view": "hot_items",
                "data": {"rank_type": "stock", "rank_label": "庫存排行",
                         "category": category, "rankings": top}}

    if rank_type not in ("hot", "slow"):
        rank_type = "hot"
    if period not in ("this_week", "this_month"):
        period = "this_week"
    rank_label = RANK_LABEL[rank_type]
    period_label = PERIOD_LABEL[period]
    cat_label = CATEGORY_LABEL.get(category, "") if category else ""

    start, end = _period_range(period)
    out_qty = defaultdict(int)
    for m in s.movements:
        try:
            d = _date.fromisoformat(m["date"])
        except Exception:
            continue
        if d < start or d > end:
            continue
        if m["direction"] != "out":
            continue
        sku = m["sku_id"]
        item = s._items_by_sku.get(sku)
        if not item:
            continue
        if category and item["category"] != category:
            continue
        out_qty[sku] += m["qty"]

    if not out_qty:
        scope = f"{cat_label}類" if cat_label else ""
        return {
            "ok": True,
            "summary": f"{period_label}{scope}沒有出貨記錄",
            "data": {
                "rank_type": rank_type,
                "period":    period,
                "category":  category,
                "rankings":  [],
            },
            "view": "hot_items",
        }

    reverse = rank_type == "hot"
    sorted_items = sorted(out_qty.items(), key=lambda x: -x[1] if reverse else x[1])
    top = sorted_items[:10]

    rankings = []
    for sku, qty in top:
        it = s._items_by_sku[sku]
        rankings.append({
            "sku_id":         sku,
            "name":           it["name"],
            "category":       it["category"],
            "category_label": CATEGORY_LABEL.get(it["category"], it["category"]),
            "out_qty":        qty,
            "unit_price":     it["unit_price"],
            "revenue":        qty * it["unit_price"],
        })

    scope = f"{cat_label}類" if cat_label else "全類別"
    top_item = rankings[0]
    summary = (
        f"{period_label}{scope}{rank_label} TOP {len(rankings)}\n"
        f"第 1 名:{top_item['name']}（出 {top_item['out_qty']:,} 件，"
        f"營收約 NT$ {top_item['revenue']:,}）"
    )

    return {
        "ok": True,
        "summary": summary,
        "data": {
            "rank_type":      rank_type,
            "rank_label":     rank_label,
            "period":         period,
            "period_label":   period_label,
            "category":       category,
            "category_label": cat_label,
            "rankings":       rankings,
            "count":          len(rankings),
        },
        "view": "hot_items",
    }


# ────────────────────────────────────────────────
# 趨勢 / 庫存可撐幾天 / 建議補貨日 helper
# ────────────────────────────────────────────────
def _daily_out_series(sku: str, days: int = 30, warehouse: str = "all") -> list[int]:
    """回近 N 天「每日出貨量」list(長度 N、補 0)。給趨勢斜率用。
    warehouse='all' 算三倉合計、否則只算單倉(逐倉補貨建議用)。"""
    s = state()
    end = _snapshot_date()
    start = end - _td(days=days - 1)
    by_day: dict[str, int] = {}
    for m in s.movements:
        if m["sku_id"] != sku or m["direction"] != "out":
            continue
        if warehouse != "all" and m["warehouse"] != warehouse:
            continue
        try:
            d = _date.fromisoformat(m["date"])
        except Exception:
            continue
        if start <= d <= end:
            by_day[m["date"]] = by_day.get(m["date"], 0) + m["qty"]
    series = []
    for i in range(days):
        d = (start + _td(days=i)).isoformat()
        series.append(by_day.get(d, 0))
    return series


def _recent_out_qty(sku: str, days: int = 30) -> int:
    """近 N 天出貨總量。"""
    return sum(_daily_out_series(sku, days))


def _trend(sku: str, days: int = 30) -> dict:
    """銷售趨勢:比前半段 vs 後半段平均日銷,回 {dir, arrow, pct, daily_avg}。
      dir: up / flat / down ; arrow: ↗ → ↘ ; pct: 後半段相對前半段的變化%
    """
    series = _daily_out_series(sku, days)
    if not series:
        return {"dir": "flat", "arrow": "→", "pct": 0.0, "daily_avg": 0.0}
    half = len(series) // 2
    first = series[:half]
    second = series[half:]
    avg_first = sum(first) / len(first) if first else 0
    avg_second = sum(second) / len(second) if second else 0
    daily_avg = sum(series) / len(series)
    if avg_first == 0:
        pct = 100.0 if avg_second > 0 else 0.0
    else:
        pct = (avg_second - avg_first) / avg_first * 100
    if pct > 15:
        d, arrow = "up", "↗"
    elif pct < -15:
        d, arrow = "down", "↘"
    else:
        d, arrow = "flat", "→"
    return {"dir": d, "arrow": arrow, "pct": round(pct, 1), "daily_avg": round(daily_avg, 1)}


# 補貨前置天數(下單到入庫的緩衝)
_RESTOCK_LEAD_DAYS = 2     # 下單到入庫的前置天數
_RESTOCK_TARGET_DAYS = 14  # 建議補貨量 = 補到能再撐 14 天(2 週週轉)
_RESTOCK_ALERT_DAYS = 7    # 撐不到這天數 → 亮補貨提醒(配合「跨安全庫存」雙條件)


def _per_warehouse_restock(sku: str, safety: int) -> list[dict]:
    """逐倉算建議補貨量(每倉各自看自己的庫存 + 自己的日均消耗)。
    安全庫存定義 = 每倉各自的線(每倉都要 >= safety)。
    回 [{warehouse, label, stock, daily_burn, suggest_qty}, ...](只列要補的倉)。
    """
    s = state()
    out = []
    for wh in s.warehouses:
        whk = wh["key"]
        stock = s.stock.get(whk, {}).get(sku, 0)
        # 該倉日均消耗(近 30 天該倉出貨 ÷ 30)
        wh_series = _daily_out_series(sku, 30, warehouse=whk)
        wh_burn = max(0.1, sum(wh_series) / len(wh_series)) if wh_series else 0.1
        # 該倉目標水位 = max(安全線, 14天該倉消耗) + 前置緩衝
        target = max(safety, _RESTOCK_TARGET_DAYS * wh_burn) + _RESTOCK_LEAD_DAYS * wh_burn
        qty = max(0, int(round(target - stock)))
        if qty > 0:
            out.append({
                "warehouse":  whk,
                "label":      WAREHOUSE_LABEL.get(whk, whk),
                "stock":      stock,
                "daily_burn": round(wh_burn, 1),
                "suggest_qty": qty,
            })
    # 缺最多的倉排前面
    out.sort(key=lambda x: -x["suggest_qty"])
    return out


# ────────────────────────────────────────────────
# 保存期限 / 到期警示
# ────────────────────────────────────────────────

# 三階燈號門檻(天數)
EXPIRY_RED_DAYS    = 7    # ≤7 天紅燈 緊急
EXPIRY_ORANGE_DAYS = 14   # ≤14 天 橙燈 警示
EXPIRY_YELLOW_DAYS = 30   # ≤30 天 黃燈 注意


def _expiry_level(days_to_expire: int) -> tuple[str, str, str]:
    """回 (level, label, emoji)。level=red/orange/yellow/green/expired"""
    if days_to_expire < 0:
        return ("expired", "已過期", "❌")
    if days_to_expire <= EXPIRY_RED_DAYS:
        return ("red", "緊急", "🔴")
    if days_to_expire <= EXPIRY_ORANGE_DAYS:
        return ("orange", "警示", "🟠")
    if days_to_expire <= EXPIRY_YELLOW_DAYS:
        return ("yellow", "注意", "🟡")
    return ("green", "新鮮", "🟢")


def _next_expiring_batch(sku: str) -> dict | None:
    """回某 SKU 最快到期的那批貨。沒有則 None(不在 shelf_life 清單裡)。"""
    s = state()
    if sku not in s.shelf_life:
        return None
    today = _snapshot_date()
    candidates = []
    for b in s.batches:
        if b["sku_id"] != sku:
            continue
        try:
            exp = _date.fromisoformat(b["expire_date"])
        except Exception:
            continue
        candidates.append((exp, b))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    exp, b = candidates[0]
    days = (exp - today).days
    level, label, emoji = _expiry_level(days)
    return {
        "warehouse":     b["warehouse"],
        "warehouse_label": WAREHOUSE_LABEL.get(b["warehouse"], b["warehouse"]),
        "qty":           b["qty"],
        "expire_date":   b["expire_date"],
        "days_to_expire": days,
        "level":         level,
        "level_label":   label,
        "level_emoji":   emoji,
    }


def list_expiring_items(
    within_days: int = 30,
    warehouse: str = "all",
    category: str | None = None,
) -> dict:
    """7. 列出在 within_days 天內到期的批次,按到期日由近到遠排序。
    三階燈號:7天紅 / 14天橙 / 30天黃。
    """
    s = state()
    if warehouse not in ("north", "central", "south", "all"):
        warehouse = "all"
    today = _snapshot_date()

    rows = []
    for b in s.batches:
        if warehouse != "all" and b["warehouse"] != warehouse:
            continue
        item = s._items_by_sku.get(b["sku_id"])
        if not item:
            continue
        if category and item["category"] != category:
            continue
        try:
            exp = _date.fromisoformat(b["expire_date"])
        except Exception:
            continue
        days = (exp - today).days
        if days > within_days:
            continue
        level, label, emoji = _expiry_level(days)
        rows.append({
            "sku_id":         b["sku_id"],
            "name":           item["name"],
            "category":       item["category"],
            "category_label": CATEGORY_LABEL.get(item["category"], item["category"]),
            "warehouse":      b["warehouse"],
            "warehouse_label": WAREHOUSE_LABEL.get(b["warehouse"], b["warehouse"]),
            "qty":            b["qty"],
            "expire_date":    b["expire_date"],
            "days_to_expire": days,
            "level":          level,
            "level_label":    label,
            "level_emoji":    emoji,
            "value":          b["qty"] * item["unit_price"],
        })

    # 按到期天數升序(最急的排最前)
    rows.sort(key=lambda r: r["days_to_expire"])

    wh_label = WAREHOUSE_LABEL.get(warehouse, warehouse)
    cat_label = CATEGORY_LABEL.get(category, "") if category else ""
    scope = wh_label + (cat_label + "類" if cat_label else "")

    if not rows:
        return {
            "ok": True,
            "summary": f"{scope}最近 {within_days} 天內沒有快到期的批次 ✅",
            "data": {
                "within_days": within_days,
                "warehouse":   warehouse,
                "category":    category,
                "rows":        [],
                "counts":      {"red": 0, "orange": 0, "yellow": 0},
            },
            "view": "expiring",
        }

    # 統計三階
    counts = {"red": 0, "orange": 0, "yellow": 0, "expired": 0}
    for r in rows:
        counts[r["level"]] = counts.get(r["level"], 0) + 1
    total_value = sum(r["value"] for r in rows)

    top = rows[0]
    summary_parts = []
    if counts.get("expired"):  summary_parts.append(f"❌ 已過期 {counts['expired']} 批")
    if counts.get("red"):      summary_parts.append(f"🔴 7 天內 {counts['red']} 批")
    if counts.get("orange"):   summary_parts.append(f"🟠 14 天內 {counts['orange']} 批")
    if counts.get("yellow"):   summary_parts.append(f"🟡 30 天內 {counts['yellow']} 批")

    summary = (
        f"⏰ {scope}到期警示({within_days} 天內共 {len(rows)} 批 / 約 NT$ {total_value:,})\n"
        + " · ".join(summary_parts)
        + f"\n最急:{top['level_emoji']} {top['name']}({top['warehouse_label']} {top['qty']} 件,"
        + f"剩 {top['days_to_expire']} 天)"
    )

    return {
        "ok": True,
        "summary": summary,
        "data": {
            "within_days":   within_days,
            "warehouse":     warehouse,
            "warehouse_label": wh_label,
            "category":      category,
            "category_label": cat_label,
            "rows":          rows,
            "counts":        counts,
            "total_value":   total_value,
        },
        "view": "expiring",
    }


def _stock_forecast(sku: str) -> dict:
    """庫存可撐幾天 + 建議補貨日(趨勢加權消耗)。
    回 {total_stock, daily_burn, days_left, suggest_after_days, suggest_date, urgent}
      - daily_burn: 趨勢加權後的日均消耗(上升趨勢 ×1.3、下滑 ×0.8)
      - days_left:  撐到「庫存歸零」還有幾天
      - suggest_after_days: 撐到「跌破安全庫存」還有幾天,再扣前置天數
      - urgent: 是否已經很急(建議補貨日 <= 0)
    """
    s = state()
    item = s._items_by_sku.get(sku, {})
    safety = item.get("safety_stock", 0)
    total_stock, _ = _sku_total_stock(sku, "all")
    tr = _trend(sku)
    base_burn = tr["daily_avg"]
    # 趨勢加權:賣越來越快 → 消耗放大、別低估
    factor = 1.3 if tr["dir"] == "up" else (0.8 if tr["dir"] == "down" else 1.0)
    daily_burn = max(0.1, base_burn * factor)

    days_left = int(total_stock / daily_burn) if daily_burn > 0 else 999
    # 跌破安全庫存還有幾天
    days_to_safety = int((total_stock - safety) / daily_burn) if daily_burn > 0 else 999
    suggest_after = max(0, days_to_safety - _RESTOCK_LEAD_DAYS)
    suggest_date = (_snapshot_date() + _td(days=suggest_after)).isoformat()

    # 逐倉建議補貨量(每倉各自看、安全線=每倉各自的線)
    per_wh_restock = _per_warehouse_restock(sku, safety)
    # 總建議補貨量 = 各倉加總(跟分倉一致、不另算)
    suggest_qty = sum(w["suggest_qty"] for w in per_wh_restock)

    # 補貨提醒門檻:跌破安全庫存 或 撐不到 _RESTOCK_ALERT_DAYS 天
    need_reorder = (total_stock < safety) or (days_left <= _RESTOCK_ALERT_DAYS)

    return {
        "total_stock":       total_stock,
        "safety_stock":      safety,
        "daily_burn":        round(daily_burn, 1),
        "days_left":         days_left,
        "suggest_after_days": suggest_after,
        "suggest_date":      suggest_date,
        "suggest_qty":       suggest_qty,        # 建議補多少件(各倉加總)
        "per_warehouse_restock": per_wh_restock, # 逐倉補多少 [{label,stock,suggest_qty}]
        "target_days":       _RESTOCK_TARGET_DAYS,
        "lead_days":         _RESTOCK_LEAD_DAYS,
        "need_reorder":      need_reorder,       # 是否該補(跨安全線 or 撐<7天)
        "urgent":            suggest_after <= 1,
        "trend":             tr,
    }


# 近 30 天出貨量 >= 此門檻 → 視為「近期熱銷」(觸發連動補貨提醒)
# 註:v3.9 movements 改成從 orders 推、每張訂單買 1-4 件,熱銷錨點 30 天出貨約
#     60-110 件、滯銷品 ~15 件。門檻設 50 讓熱銷錨點過、慢動商品不過。
_HOT_THRESHOLD = 50

import random as _random


# 同捆率強度標籤:跟「純隨機同單 ~5%」比,讓訪客秒懂高低
# (60 SKU 任兩品純巧合同單約 3-5%,40%+ 已是約 10 倍 = 很強)
def _confidence_label(conf: float) -> dict:
    """回 {label, level, emoji}。level 給前端上色。"""
    if conf >= 60:
        return {"label": "黃金組合", "level": "gold",   "emoji": "🔥"}
    if conf >= 45:
        return {"label": "高度連帶", "level": "high",   "emoji": "💪"}
    if conf >= 30:
        return {"label": "常一起買", "level": "good",   "emoji": "👍"}
    if conf >= 15:
        return {"label": "偶爾搭配", "level": "mid",    "emoji": "🤝"}
    return {"label": "偶然同框", "level": "low",    "emoji": ""}


def _pick_quip(anchor_sku: str, co_sku: str) -> tuple[str, str | None]:
    """回 (理由梗, scenario_key)。專屬梗優先、否則情境通用梗。"""
    s = state()
    meta = s.association_meta
    a, b = sorted([anchor_sku, co_sku])
    pair_key = f"{a}|{b}"
    pair_quips = meta.get("pair_quips", {})
    if pair_key in pair_quips and pair_quips[pair_key]:
        return _random.choice(pair_quips[pair_key]), None
    # 找兩品共同情境
    sku_scn = meta.get("sku_scenarios", {})
    common = set(sku_scn.get(anchor_sku, [])) & set(sku_scn.get(co_sku, []))
    scn_quips = meta.get("scenario_quips", {})
    for scn in common:
        if scn in scn_quips and scn_quips[scn]:
            return _random.choice(scn_quips[scn]), scn
    return "常常被一起買走", None


_OPENING_QUIPS = [
    "買「{a}」的人,通常還順手帶了:",
    "買「{a}」的人,八成也扛了這些走:",
    "數據顯示,買「{a}」的人購物車裡還有:",
    "買「{a}」的人,十之八九也買了:",
    "跟「{a}」最麻吉的好夥伴是:",
]


def query_related_items(
    keyword: str | None = None,
    category: str | None = None,
    warehouse: str = "all",
) -> dict:
    """6. 連帶備貨分析:買 A 的人也買 B(購物籃) + 趨勢 + 撐幾天 + 建議補貨日

    商業價值(老闆要的):
      - 「買 A 的人也買 B」→ 連帶推薦(附好玩又有道理的理由梗)
      - 每個連帶品帶:同捆率 + 趨勢↗→↘ + 庫存撐幾天 + 建議補貨日
      - 若 A 近期熱銷、而連帶品 B 快缺貨 → 紅燈「建議 X 天內補 B」
        (A 賣得好會拉動 B 的需求、別等 B 缺了才補)
    """
    s = state()
    if not keyword:
        return _err(
            "想看哪個商品的連帶備貨分析？講商品名（例「藍牙耳機」「咖啡」），"
            "我幫你找「買的人通常也會買什麼、要不要一起補貨」",
            view="related_help",
        )

    matches = match_items(keyword, category=category)
    if not matches:
        return {
            "ok": True,
            "summary": f"找不到「{keyword}」相關商品,無法分析連帶購買",
            "data": {"keyword": keyword, "related": []},
            "view": "related_empty",
        }

    anchor = matches[0]["item"]
    anchor_sku = anchor["sku_id"]
    idx = s._basket_index
    anchor_orders = idx["single"].get(anchor_sku, 0)
    co_map = idx["pair"].get(anchor_sku, {})

    if anchor_orders == 0 or not co_map:
        return {
            "ok": True,
            "summary": f"{anchor['name']} 訂單樣本不足,還算不出穩定的連帶關係",
            "data": {
                "anchor_sku":  anchor_sku,
                "anchor_name": anchor["name"],
                "related":     [],
            },
            "view": "related",
        }

    # 主商品趨勢 + 是否近期熱銷
    anchor_recent_out = _recent_out_qty(anchor_sku)
    anchor_trend = _trend(anchor_sku)
    anchor_is_hot = anchor_recent_out >= _HOT_THRESHOLD

    # 隨機基準:任兩品純巧合同單的平均率(給「點數字看算式」的對比用)
    # = 全部品平均訂單佔有率(单品出現張數 / 總訂單)再平方近似,實務上直接用經驗值
    total_orders = idx["total"] or 1
    avg_single = (sum(idx["single"].values()) / len(idx["single"])) if idx["single"] else 0
    random_baseline = round((avg_single / total_orders) * 100, 1) if total_orders else 5.0

    # 算連帶品 confidence,取 top 5
    ranked = sorted(co_map.items(), key=lambda x: -x[1])[:5]
    related = []
    restock_alerts = []
    for co_sku, co_cnt in ranked:
        co_item = s._items_by_sku.get(co_sku)
        if not co_item:
            continue
        confidence = round(co_cnt / anchor_orders * 100, 1)
        conf_label = _confidence_label(confidence)
        fc = _stock_forecast(co_sku)
        # is_low:任一倉低於安全線(safety 定義為每倉各自的線)
        _, per_wh = _sku_total_stock(co_sku, "all")
        safety_per_wh = co_item["safety_stock"]
        is_low = any(q < safety_per_wh for q in per_wh.values())
        # 連動補貨判定:主商品熱銷 + 連帶品撐不久
        needs_restock = anchor_is_hot and (is_low or fc["days_left"] <= 7)
        quip, scn = _pick_quip(anchor_sku, co_sku)
        next_exp = _next_expiring_batch(co_sku)  # 連帶品的到期警示(若有保存期限)
        rel = {
            "sku_id":         co_sku,
            "next_expiring":  next_exp,  # 給前端顯示燈號徽章用
            "name":           co_item["name"],
            "category":       co_item["category"],
            "category_label": CATEGORY_LABEL.get(co_item["category"], co_item["category"]),
            "co_count":       co_cnt,            # 同單張數(算式分子)
            "anchor_orders":  anchor_orders,     # 主商品訂單數(算式分母)
            "random_baseline": random_baseline,  # 隨機基準%(對比用)
            "confidence":     confidence,
            "conf_label":     conf_label["label"],   # 黃金組合/高度連帶/...
            "conf_level":     conf_label["level"],   # 給前端上色
            "conf_emoji":     conf_label["emoji"],
            "quip":           quip,            # 好玩的理由梗(訪客看得到)
            "total_stock":    fc["total_stock"],
            "safety_stock":   co_item["safety_stock"],
            "days_left":      fc["days_left"],
            "daily_burn":     fc["daily_burn"],       # 日均消耗(撐幾天算式分母)
            "trend_arrow":    fc["trend"]["arrow"],
            "trend_dir":      fc["trend"]["dir"],
            "per_warehouse":  per_wh,            # 三倉庫存細項 {north, central, south}
            "suggest_after_days": fc["suggest_after_days"],
            "suggest_date":   fc["suggest_date"],
            "suggest_qty":    fc["suggest_qty"],       # 建議補多少件
            "per_warehouse_restock": fc["per_warehouse_restock"],  # 逐倉補多少
            "target_days":    fc["target_days"],       # 補到撐幾天(14)
            "lead_days":      fc["lead_days"],
            "need_reorder":   fc["need_reorder"],      # 是否該補(跨安全線 or 撐<7天)
            "is_low_stock":   is_low,
            "needs_restock":  needs_restock,
        }
        related.append(rel)
        if needs_restock:
            restock_alerts.append(rel)

    # 文字 summary(開場白隨機 + 第一連帶品的梗 + 強度標籤)
    top = related[0]
    opening = _random.choice(_OPENING_QUIPS).format(a=anchor["name"])
    summary = (
        f"{opening}\n"
        f"🔗 {top['name']}（同捆率 {top['confidence']:.0f}% · {top['conf_emoji']}{top['conf_label']}）{top['trend_arrow']}\n"
        f"　「{top['quip']}」"
    )
    if len(related) >= 2:
        summary += (
            f"\n🔗 {related[1]['name']}（{related[1]['confidence']:.0f}% · "
            f"{related[1]['conf_emoji']}{related[1]['conf_label']}）{related[1]['trend_arrow']}"
        )
    # 基準說明(讓訪客知道數字怎麼看)
    summary += "\n📊 註:隨機商品同單約 5%、越高代表關係越強"

    if anchor_is_hot and restock_alerts:
        first = restock_alerts[0]
        if first["suggest_after_days"] <= 1:
            when = "建議今天就補"
        else:
            when = f"建議 {first['suggest_after_days']} 天內補（{first['suggest_date']} 前）"
        # 缺貨狀態描述:列出低於安全線的倉 vs 還能撐幾天
        if first["is_low_stock"]:
            # 找出哪些倉低
            _, per_wh = _sku_total_stock(first["sku_id"], "all")
            safety_one = first["safety_stock"]
            low_whs = [WAREHOUSE_LABEL[k] for k, q in per_wh.items() if q < safety_one]
            if low_whs:
                state_text = f"{'、'.join(low_whs)}已低於安全線({safety_one})"
            else:
                state_text = f"庫存 {first['total_stock']} 件已低於安全水位"
        else:
            state_text = f"只撐 {first['days_left']} 天"
        qty_text = f"、建議補 {first['suggest_qty']} 件" if first.get("suggest_qty") else ""
        summary += (
            f"\n⚠️ {anchor['name']} 出貨量高{anchor_trend['arrow']},"
            f"連帶品「{first['name']}」{state_text},{when}{qty_text}"
        )

    return {
        "ok": True,
        "summary": summary,
        "data": {
            "anchor_sku":        anchor_sku,
            "anchor_name":       anchor["name"],
            "anchor_category":   anchor["category"],
            "anchor_orders":     anchor_orders,
            "anchor_recent_out": anchor_recent_out,
            "anchor_trend":      anchor_trend,
            "anchor_is_hot":     anchor_is_hot,
            "related":           related,
            "restock_alerts":    restock_alerts,
            "total_orders":      idx["total"],
            "random_baseline":   random_baseline,
        },
        "view": "related",
    }


# ────────────────────────────────────────────────
# Dispatch
# ────────────────────────────────────────────────

FUNCTIONS = {
    "query_inventory":     query_inventory,
    "query_movement":      query_movement,
    "list_low_stock":      list_low_stock,
    "compare_warehouses":  compare_warehouses,
    "list_hot_items":      list_hot_items,
    "query_related_items": query_related_items,
    "list_expiring_items": list_expiring_items,
}

# ── v2 三金剛（Agentic 工具）：延遲註冊，避免 import 循環 ──
def _register_v2_tools():
    try:
        import tools_v2
        FUNCTIONS["search_log"]      = tools_v2.search_log
        FUNCTIONS["manage_config"]   = tools_v2.manage_config
        FUNCTIONS["run_script"]      = tools_v2.run_script
        FUNCTIONS["generate_report"] = tools_v2.generate_report   # A 波：寫報告
        FUNCTIONS["list_files"]      = tools_v2.list_files         # B 波：動態找檔
        FUNCTIONS["set_alert"]           = tools_v2.set_alert           # 第四金剛：警示規則
        FUNCTIONS["generate_po"]         = tools_v2.generate_po         # 閉環：產採購單草稿
        FUNCTIONS["compare_periods"]     = tools_v2.compare_periods     # 跨期比較
        FUNCTIONS["set_schedule"]        = tools_v2.set_schedule        # 定時排程設定
        FUNCTIONS["commit_schedule_set"] = tools_v2.commit_schedule_set # 排程確認寫入
        FUNCTIONS["list_schedules"]      = tools_v2.list_schedules      # 查排程
        FUNCTIONS["delete_schedule"]     = tools_v2.delete_schedule     # 刪排程
        FUNCTIONS["list_alerts"]         = tools_v2.list_alerts         # 查警示規則
        FUNCTIONS["delete_alert"]        = tools_v2.delete_alert        # 刪警示規則
    except Exception as e:
        _log.warning(f"[v2] 三金剛註冊失敗：{e}")

_register_v2_tools()


def execute(name: str, args: dict) -> dict:
    """從 server 呼叫。把 dict 化的 args 解到 function。"""
    fn = FUNCTIONS.get(name)
    if not fn:
        return _err(f"不支援的 function: {name}")
    try:
        return fn(**(args or {}))
    except (ValueError, KeyError, TypeError) as e:
        _log.warning(f"[warehouse] {name}({args}) 參數錯誤: {e}")
        return _err(f"查詢參數有誤：{e}")
    except Exception as e:
        _log.exception(f"[warehouse] {name}({args}) 執行異常")
        return _err(f"系統忙碌、請稍候重試")
