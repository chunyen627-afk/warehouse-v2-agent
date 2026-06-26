"""
regenerate_seed_from_csv.py — 從 items_editable.csv 重生 seed_data.json

修改流程：
  1. 用 Excel / 試算表開啟 items_editable.csv（保持 UTF-8 CSV 格式存檔）
  2. 改 name / unit_price / safety_stock / stock_north / stock_central / stock_south
  3. 跑 `py -3.11 regenerate_seed_from_csv.py`
  4. 重啟 server 或 POST /reset

只動 stock（庫存）+ 30 SKU 屬性 + 重生 movements 部分：
  - movements 會根據新的 stock + HOT_ITEMS / SLOW_ITEMS 重生（過去 90 天）
  - categories / warehouses 結構不變、要改請編 generate_seed_data.py

可選 flag：
  --keep-movements   保留原 seed_data.json 的 movements（只更新 stock + 屬性）
  --csv FILE         指定 CSV 路徑（預設 items_editable.csv）
  --out FILE         指定輸出路徑（預設 test/seed_data.json）
"""

import argparse
import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent          # = warehouse/data_tools/
WAREHOUSE = HERE.parent               # = warehouse/
DEFAULT_CSV = HERE / "items_editable.csv"
DEFAULT_OUT = WAREHOUSE / "test" / "seed_data.json"

# ────────────────────────────────────────────────
# 固定設定（要改類別 / 倉庫請改 generate_seed_data.py）
# ────────────────────────────────────────────────
SNAPSHOT_DATE = date(2026, 5, 26)
RANDOM_SEED   = 42

CATEGORIES = [
    {"key": "electronics",       "label": "電子產品"},
    {"key": "appliance_kitchen", "label": "家電廚具"},
    {"key": "food_beverage",     "label": "食品飲料"},
    {"key": "daily_goods",       "label": "日用品"},
    {"key": "apparel",           "label": "服飾"},
    {"key": "sports",            "label": "運動用品"},
]
WAREHOUSES = [
    {"key": "north",   "label": "北區倉"},
    {"key": "central", "label": "中區倉"},
    {"key": "south",   "label": "南區倉"},
]

# 滯銷 / 熱銷 SKU（重生 movements 時用、可隨意改 SKU id）
SLOW_ITEMS = {"a02", "d04"}                                # 蒸氣電熨斗、蚊香液
HOT_ITEMS  = {"f01", "e01", "d02", "f03", "s01"}           # 氣泡水、藍牙耳機、衛生紙、檸檬茶、瑜珈墊


def regenerate_movements(items: list[dict], n_days: int = 90) -> list[dict]:
    """重生過去 N 天 movements (預設 90 天、可用 --days 改)。"""
    random.seed(RANDOM_SEED)
    movements = []
    for days_ago in range(n_days, 0, -1):
        d = SNAPSHOT_DATE - timedelta(days=days_ago)
        is_weekend = d.weekday() >= 5
        n_records = random.randint(18, 28)
        for _ in range(n_records):
            item = random.choice(items)
            sku = item["sku_id"]
            wh_key = random.choices(
                ["north", "central", "south"],
                weights=[0.45, 0.30, 0.25],
            )[0]
            if sku in SLOW_ITEMS:
                direction = "out" if random.random() < 0.15 else "in"
                qty = random.randint(2, 8)
            elif sku in HOT_ITEMS:
                direction = "out" if (is_weekend or random.random() < 0.65) else "in"
                qty = random.randint(20, 80)
            else:
                direction = random.choice(["in", "out"])
                qty = random.randint(5, 30)
            movements.append({
                "date":      d.isoformat(),
                "sku_id":    sku,
                "warehouse": wh_key,
                "direction": direction,
                "qty":       qty,
            })
    movements.sort(key=lambda r: (r["date"], r["sku_id"]))
    return movements


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=str, default=str(DEFAULT_CSV))
    ap.add_argument("--out", type=str, default=str(DEFAULT_OUT))
    ap.add_argument("--keep-movements", action="store_true",
                    help="保留原 seed_data 的 movements（只更新 stock + 屬性）")
    ap.add_argument("--days", type=int, default=90,
                    help="重生過去 N 天進出記錄（預設 90、可設 7 / 30 / 180 / 365）")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    out_path = Path(args.out)
    if not csv_path.exists():
        print(f"✗ 找不到 {csv_path}")
        return 1

    # ─── 讀 CSV ───
    items = []
    stock = {wh["key"]: {} for wh in WAREHOUSES}
    valid_cats = {c["key"] for c in CATEGORIES}

    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row_i, row in enumerate(reader, 2):  # row 2 = 第一筆資料
            sku = row["sku_id"].strip()
            cat = row["category"].strip()
            if cat not in valid_cats:
                print(f"✗ Row {row_i} category={cat!r} 不在 enum {sorted(valid_cats)}")
                return 1
            try:
                items.append({
                    "sku_id":       sku,
                    "name":         row["name"].strip(),
                    "category":     cat,
                    "unit_price":   int(row["unit_price"]),
                    "safety_stock": int(row["safety_stock"]),
                })
                stock["north"][sku]   = int(row["stock_north"])
                stock["central"][sku] = int(row["stock_central"])
                stock["south"][sku]   = int(row["stock_south"])
            except (ValueError, KeyError) as e:
                print(f"✗ Row {row_i} 格式錯誤: {e}")
                return 1

    print(f"OK 讀到 {len(items)} 個 SKU")
    print(f"  3 倉庫存總量: north={sum(stock['north'].values()):,} / "
          f"central={sum(stock['central'].values()):,} / "
          f"south={sum(stock['south'].values()):,}")

    # ─── Movements ───
    if args.keep_movements and out_path.exists():
        old = json.loads(out_path.read_text(encoding="utf-8"))
        movements = old.get("movements", [])
        print(f"  movements: 保留原 {len(movements)} 筆")
    else:
        movements = regenerate_movements(items, n_days=args.days)
        print(f"  movements: 重生 {len(movements)} 筆 ({args.days} 天)")

    # ─── Orders + 連鎖網 meta（連帶備貨分析用）───
    from generate_orders import build_orders, build_association_meta
    orders = build_orders(items)
    association_meta = build_association_meta()
    print(f"  orders: 生成 {len(orders)} 張(連帶分析用)")

    # ─── 寫檔 ───
    seed = {
        "snapshot_date":  SNAPSHOT_DATE.isoformat(),
        "schema_version": 2,   # v2: 加 orders + association_meta(連鎖網)
        "categories":     CATEGORIES,
        "warehouses":     WAREHOUSES,
        "items":          items,
        "stock":          stock,
        "movements":      movements,
        "orders":         orders,
        "association_meta": association_meta,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(seed, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = out_path.stat().st_size / 1024
    print(f"OK 已寫出 {out_path} ({size_kb:.1f} KB)")

    # ─── 驗證低庫存項目 ───
    n_low = sum(
        1
        for it in items
        for wh in WAREHOUSES
        if stock[wh["key"]][it["sku_id"]] < it["safety_stock"]
    )
    print(f"  低庫存項目數: {n_low}")
    if n_low < 3:
        print("  ⚠️ 低庫存項目 < 3、list_low_stock demo 紅警示表格會空、建議調整")

    print()
    print("下一步:")
    print("  方案 A: 重啟 server")
    print("    cd test && py -3.11 server.py")
    print("  方案 B: server 還在跑 → 觸發 /reset")
    print("    curl -X POST http://localhost:8000/reset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
