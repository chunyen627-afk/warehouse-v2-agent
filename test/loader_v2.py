"""
loader_v2.py — 從 warehouse_data/ 多檔讀回，重組成「seed 等價 dict」。

設計關鍵（零回歸）：
  v1 的 7 個 function 全部依賴 State 的記憶體結構（items/stock/movements/orders/...）。
  為了不動那些 function，loader 的責任是「把多檔讀回成跟 seed_data.json 一模一樣的 dict」，
  再餵給現有的 State。v1 function 完全無感，但資料來源已經是 warehouse_data/。

  額外回傳 v2 專屬區塊（config / suppliers / manifest），給三金剛（search_log/
  manage_config/run_script）用，不影響 v1。

stock 從哪來？
  v1 seed 內含「最終 stock 快照」。多檔結構沒有獨立 stock 檔（刻意：stock 應該是
  movements 的衍生量，存兩份會不一致）。loader 用 movements 累加重算 stock：
    stock[wh][sku] = Σ in − Σ out
  並回填 safety_stock 的「分倉 override」（config.json）讓 v1 low_stock 邏輯吃得到。
"""
import csv
import json
from collections import defaultdict
from pathlib import Path


def _read_csv(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_as_seed(wd: Path, seed_fallback: Path | None = None) -> dict:
    """從 warehouse_data/ 組出 seed 等價 dict（含 v2 區塊）。"""
    wd = Path(wd)

    # ── master ──────────────────────────────────────────
    items_rows = _read_csv(wd / "master" / "items.csv")
    items = [{
        "sku_id":       r["sku_id"],
        "name":         r["name"],
        "category":     r["category"],
        "unit_price":   int(r["unit_price"]),
        "safety_stock": int(r["safety_stock"]),
    } for r in items_rows]

    # categories：從 items 的 category + category_label 還原（保序、去重）
    cat_seen, categories = set(), []
    for r in items_rows:
        k = r["category"]
        if k not in cat_seen:
            cat_seen.add(k)
            categories.append({"key": k, "label": r.get("category_label", k)})

    config = json.load(open(wd / "master" / "config.json", encoding="utf-8"))
    suppliers = _read_csv(wd / "master" / "suppliers.csv")

    # ── transactions → movements（攤平所有日_方向 CSV）────────
    movements = []
    for csv_path in sorted((wd / "transactions").glob("*.csv")):
        for r in _read_csv(csv_path):
            movements.append({
                "date":      r["date"],
                "sku_id":    r["sku_id"],
                "warehouse": r["warehouse"],
                "direction": r["direction"],
                "qty":       int(r["qty"]),
            })

    # ── orders/SO → orders（v1 connected analysis 用）────────
    orders = []
    for jp in sorted((wd / "orders" / "SO").glob("*.json")):
        o = json.load(open(jp, encoding="utf-8"))
        orders.append({"order_id": o["order_id"], "date": o["date"], "lines": o["lines"]})

    # ── stock：讀『當前快照』真值（master/stock.csv）。
    #    絕不從 movements 累加重算——當前庫存 = 期初存量 + 流水，而期初未知。
    stock: dict[str, dict[str, int]] = defaultdict(dict)
    for r in _read_csv(wd / "master" / "stock.csv"):
        stock[r["warehouse"]][r["sku_id"]] = int(r["qty"])
    stock = dict(stock)

    # warehouses：從 stock keys + config override keys 還原（保序用 north/central/south 慣例）
    WH_LABEL = {"north": "北區倉", "central": "中區倉", "south": "南區倉"}
    wh_keys = list(config.get("safety_stock_override", {}).keys()) or list(stock.keys())
    warehouses = [{"key": k, "label": WH_LABEL.get(k, k)} for k in wh_keys]

    # ── seed 裡 multi 檔沒搬的東西（batches / shelf_life / snapshot_date /
    #    association_meta）：從原 seed_data.json 借（它仍是 source of truth）──
    extra = {}
    if seed_fallback and Path(seed_fallback).exists():
        sd = json.load(open(seed_fallback, encoding="utf-8"))
        extra = {
            "snapshot_date":    sd.get("snapshot_date", ""),
            "schema_version":   sd.get("schema_version", 1),
            "batches":          sd.get("batches", []),
            "shelf_life":       sd.get("shelf_life", {}),
            "association_meta": sd.get("association_meta", {}),
        }

    seed_equiv = {
        "snapshot_date":    extra.get("snapshot_date", ""),
        "schema_version":   extra.get("schema_version", 1),
        "categories":       categories,
        "warehouses":       warehouses,
        "items":            items,
        "stock":            stock,
        "movements":        movements,
        "orders":           orders,
        "batches":          extra.get("batches", []),
        "shelf_life":       extra.get("shelf_life", {}),
        "association_meta": extra.get("association_meta", {}),
        # ── v2 專屬（v1 不讀）──
        "_v2_config":     config,
        "_v2_suppliers":  suppliers,
        "_v2_data_dir":   str(wd),
    }
    return seed_equiv
