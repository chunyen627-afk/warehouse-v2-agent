"""
Export movements: merge transactions/*.csv into a single CSV
Usage: python export_movements.py [--data-dir <path>] [--days <n>]
"""
import sys, csv, pathlib, datetime, argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(pathlib.Path(__file__).parent.parent))
    parser.add_argument("--days", type=int, default=30, help="how many recent days (default 30)")
    args = parser.parse_args()

    dd      = pathlib.Path(args.data_dir)
    tx_dir  = dd / "transactions"
    cutoff  = (datetime.date.today() - datetime.timedelta(days=args.days)).isoformat()

    rows = []
    for f in sorted(tx_dir.glob("*.csv")):
        # filename format: 2026-02-26_in.csv
        date_str = f.stem.split("_")[0]
        if date_str < cutoff:
            continue
        try:
            with open(f, encoding="utf-8-sig") as fp:
                reader = csv.reader(fp)
                for row in reader:
                    if len(row) >= 5:
                        rows.append(row)
        except Exception:
            pass

    now      = datetime.datetime.now()
    ts       = now.strftime("%Y%m%d_%H%M%S")
    out_dir  = dd / "audit"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"movements_{ts}.csv"

    with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Date", "SKU", "Warehouse", "Direction", "Qty"])
        for row in rows:
            w.writerow(row)

    print(f"OUTPUT:{out_file}")
    print(f"SUMMARY:Exported {len(rows)} movements from the last {args.days} days to audit/{out_file.name}")

if __name__ == "__main__":
    main()
