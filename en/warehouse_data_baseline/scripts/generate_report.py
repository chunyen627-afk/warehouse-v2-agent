"""
Report generator: produce a Markdown warehouse health report
Usage: python generate_report.py [--data-dir <path>] [--type full|low_stock|hot]
"""
import sys, csv, pathlib, datetime, argparse

WH_LABEL = {"north": "North", "central": "Central", "south": "South"}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(pathlib.Path(__file__).parent.parent))
    parser.add_argument("--type", default="full", choices=["full", "low_stock", "hot"])
    args = parser.parse_args()

    dd     = pathlib.Path(args.data_dir)
    master = dd / "master"

    # read items + safety stock from items.csv
    items  = {}
    safety = {}
    for r in csv.DictReader(open(master / "items.csv", encoding="utf-8-sig")):
        items[r["sku_id"]]  = r
        safety[r["sku_id"]] = int(r.get("safety_stock") or 0)

    # read per-warehouse quantities from stock.csv
    sku_total = {}
    for r in csv.DictReader(open(master / "stock.csv", encoding="utf-8-sig")):
        sku = r["sku_id"]
        sku_total[sku] = sku_total.get(sku, 0) + int(r.get("qty") or 0)

    # read outbound records from transactions/
    sku_sales = {}
    tx_dir = dd / "transactions"
    if tx_dir.exists():
        for f in sorted(tx_dir.glob("*.csv")):
            for r in csv.DictReader(open(f, encoding="utf-8-sig")):
                if r.get("direction") == "out":
                    sku = r["sku_id"]
                    sku_sales[sku] = sku_sales.get(sku, 0) + int(r.get("qty") or 0)

    now     = datetime.datetime.now()
    ts      = now.strftime("%Y-%m-%dT%H:%M:%S")
    ts_file = now.strftime("%Y%m%d_%H%M%S")

    lines = []
    lines.append(f"# Warehouse Health Report")
    lines.append(f"Generated: {ts}  |  Type: {args.type}\n")

    if args.type in ("full", "low_stock"):
        low_items = [(sku, items[sku]["name"], sku_total.get(sku,0), safety.get(sku,0))
                     for sku in items if sku_total.get(sku,0) < safety.get(sku,0)]
        lines.append(f"## Low stock alerts ({len(low_items)} items)\n")
        lines.append("| SKU | Item | On hand | Safety stock | Gap |")
        lines.append("|-----|------|------|----------|------|")
        for sku, name, qty, ss in sorted(low_items, key=lambda x: x[2]-x[3]):
            lines.append(f"| {sku} | {name} | {qty} | {ss} | {ss-qty} |")
        lines.append("")

    if args.type in ("full", "hot"):
        hot = sorted(sku_sales.items(), key=lambda x: x[1], reverse=True)[:10]
        lines.append(f"## Top 10 best sellers\n")
        lines.append("| Rank | SKU | Item | Shipped | On hand |")
        lines.append("|------|-----|------|--------|--------|")
        for i, (sku, sales) in enumerate(hot, 1):
            name = items.get(sku, {}).get("name", sku)
            lines.append(f"| {i} | {sku} | {name} | {sales} | {sku_total.get(sku,0)} |")
        lines.append("")

    if args.type == "full":
        total_skus  = len(items)
        total_stock = sum(sku_total.values())
        low_count   = sum(1 for sku in items if sku_total.get(sku,0) < safety.get(sku,0))
        lines.append(f"## Overview\n")
        lines.append(f"- SKUs: {total_skus}")
        lines.append(f"- Total units: {total_stock}")
        lines.append(f"- Low stock alerts: {low_count}")
        lines.append(f"- Health score: {round((1-low_count/total_skus)*100)}%")

    out_dir  = dd / "reports"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"{ts_file}_{args.type}_report.md"
    out_file.write_text("\n".join(lines), encoding="utf-8")

    print(f"OUTPUT:{out_file}")
    print(f"SUMMARY:Report generated ({len(lines)} lines), saved to reports/{out_file.name}")

if __name__ == "__main__":
    main()
