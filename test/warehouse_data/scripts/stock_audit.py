"""
月底盤點腳本：掃全倉庫存，和安全庫存比較，產出 CSV 到 audit/
用法：python stock_audit.py [--data-dir <path>]
"""
import sys, json, csv, pathlib, datetime, argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(pathlib.Path(__file__).parent.parent))
    args = parser.parse_args()

    dd = pathlib.Path(args.data_dir)
    seed = json.loads((dd.parent / "seed_data.json").read_text("utf-8"))

    items      = {it["sku_id"]: it for it in seed["items"]}
    safety     = {it["sku_id"]: it.get("safety_stock", 0) for it in seed["items"]}
    stock_rows = seed.get("stock", [])

    # stock 格式：{warehouse: {sku: qty}}
    stock_map = {}  # (sku, warehouse) -> qty
    sku_total = {}
    for wh, sku_dict in stock_rows.items():
        for sku, qty in sku_dict.items():
            stock_map[(sku, wh)] = qty
            sku_total[sku] = sku_total.get(sku, 0) + qty

    now      = datetime.datetime.now()
    ts       = now.strftime("%Y%m%d_%H%M%S")
    out_dir  = dd / "audit"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"stock_audit_{ts}.csv"

    WH_LABEL = {"north": "北區倉", "central": "中區倉", "south": "南區倉"}
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
            w.writerow([sku_id, item["name"], item.get("category",""),
                        ss, *wh_qtys, total, status])

    print(f"OUTPUT:{out_file}")
    print(f"SUMMARY:共 {len(items)} 個 SKU，{low_count} 個需注意，報告已存至 audit/{out_file.name}")

if __name__ == "__main__":
    main()
