"""
月底盤點腳本：掃全倉庫存，和安全庫存比較，產出 CSV 到 audit/
用法：python stock_audit.py [--data-dir <path>]
"""
import sys, csv, pathlib, datetime, argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(pathlib.Path(__file__).parent.parent))
    args = parser.parse_args()

    dd = pathlib.Path(args.data_dir)
    master = dd / "master"

    # 從 items.csv 讀商品和安全庫存
    items = {}
    safety = {}
    for r in csv.DictReader(open(master / "items.csv", encoding="utf-8-sig")):
        items[r["sku_id"]] = r
        safety[r["sku_id"]] = int(r.get("safety_stock") or 0)

    # 從 stock.csv 讀各倉庫存
    stock_map = {}  # (sku, warehouse) -> qty
    sku_total = {}
    for r in csv.DictReader(open(master / "stock.csv", encoding="utf-8-sig")):
        sku, wh, qty = r["sku_id"], r["warehouse"], int(r.get("qty") or 0)
        stock_map[(sku, wh)] = qty
        sku_total[sku] = sku_total.get(sku, 0) + qty

    now      = datetime.datetime.now()
    ts       = now.strftime("%Y%m%d_%H%M%S")
    out_dir  = dd / "audit"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"stock_audit_{ts}.csv"

    warehouses = ["north", "central", "south"]

    with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["SKU", "商品名稱", "類別", "安全庫存",
                    "北區倉", "中區倉", "南區倉", "總量", "狀態"])
        low_count = 0
        for sku_id, item in sorted(items.items()):
            ss    = safety.get(sku_id, 0)
            total = sku_total.get(sku_id, 0)
            wh_qtys = [stock_map.get((sku_id, wh), 0) for wh in warehouses]
            status = "缺貨警示" if total < ss else ("低庫存" if total < ss * 1.2 else "正常")
            if status != "正常":
                low_count += 1
            w.writerow([sku_id, item["name"], item.get("category", ""),
                        ss, *wh_qtys, total, status])

    print(f"OUTPUT:{out_file}")
    print(f"SUMMARY:共 {len(items)} 個 SKU，{low_count} 個需注意，報告已存至 audit/{out_file.name}")

if __name__ == "__main__":
    main()
