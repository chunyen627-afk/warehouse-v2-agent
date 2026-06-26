"""
報告產生腳本：產出 Markdown 全倉體檢報告
用法：python generate_report.py [--data-dir <path>] [--type full|low_stock|hot]
"""
import sys, json, pathlib, datetime, argparse

WH_LABEL = {"north": "北區倉", "central": "中區倉", "south": "南區倉"}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(pathlib.Path(__file__).parent.parent))
    parser.add_argument("--type", default="full", choices=["full", "low_stock", "hot"])
    args = parser.parse_args()

    dd   = pathlib.Path(args.data_dir)
    seed = json.loads((dd.parent / "seed_data.json").read_text("utf-8"))

    items   = {it["sku_id"]: it for it in seed["items"]}
    safety  = {it["sku_id"]: it.get("safety_stock", 0) for it in seed["items"]}
    stock_rows = seed.get("stock", [])
    movements  = seed.get("movements", [])

    # stock 格式：{warehouse: {sku: qty}}
    sku_total = {}
    for wh, sku_dict in stock_rows.items():
        for sku, qty in sku_dict.items():
            sku_total[sku] = sku_total.get(sku, 0) + qty

    # movements 格式：list of {date, sku_id, warehouse, direction, qty}
    sku_sales = {}
    for m in movements:
        if m.get("direction") == "out":
            sku = m["sku_id"]
            sku_sales[sku] = sku_sales.get(sku, 0) + m.get("qty", 0)

    now     = datetime.datetime.now()
    ts      = now.strftime("%Y-%m-%dT%H:%M:%S")
    ts_file = now.strftime("%Y%m%d_%H%M%S")

    lines = []
    lines.append(f"# 倉庫體檢報告")
    lines.append(f"產生時間：{ts}  |  類型：{args.type}\n")

    if args.type in ("full", "low_stock"):
        low_items = [(sku, items[sku]["name"], sku_total.get(sku,0), safety.get(sku,0))
                     for sku in items if sku_total.get(sku,0) < safety.get(sku,0)]
        lines.append(f"## 缺貨警示（共 {len(low_items)} 項）\n")
        lines.append("| SKU | 商品 | 現量 | 安全庫存 | 缺口 |")
        lines.append("|-----|------|------|----------|------|")
        for sku, name, qty, ss in sorted(low_items, key=lambda x: x[2]-x[3]):
            lines.append(f"| {sku} | {name} | {qty} | {ss} | {ss-qty} |")
        lines.append("")

    if args.type in ("full", "hot"):
        hot = sorted(sku_sales.items(), key=lambda x: x[1], reverse=True)[:10]
        lines.append(f"## 熱銷前10\n")
        lines.append("| 排名 | SKU | 商品 | 出貨量 | 現存量 |")
        lines.append("|------|-----|------|--------|--------|")
        for i, (sku, sales) in enumerate(hot, 1):
            name = items.get(sku, {}).get("name", sku)
            lines.append(f"| {i} | {sku} | {name} | {sales} | {sku_total.get(sku,0)} |")
        lines.append("")

    if args.type == "full":
        total_skus  = len(items)
        total_stock = sum(sku_total.values())
        low_count   = sum(1 for sku in items if sku_total.get(sku,0) < safety.get(sku,0))
        lines.append(f"## 總覽\n")
        lines.append(f"- 商品種類：{total_skus} 個 SKU")
        lines.append(f"- 總庫存量：{total_stock} 件")
        lines.append(f"- 缺貨警示：{low_count} 項")
        lines.append(f"- 健康度：{round((1-low_count/total_skus)*100)}%")

    out_dir  = dd / "reports"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"{ts_file}_{args.type}_report.md"
    out_file.write_text("\n".join(lines), encoding="utf-8")

    print(f"OUTPUT:{out_file}")
    print(f"SUMMARY:報告已產出，共 {len(lines)} 行，存至 reports/{out_file.name}")

if __name__ == "__main__":
    main()
