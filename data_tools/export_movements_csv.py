"""
export_movements_csv.py — 把 seed_data.json 的 movements 匯成 CSV

用 Excel 開可查看完整進出記錄、按日期/商品/倉/方向篩選統計都好用。

用法:
    py -3.11 export_movements_csv.py
        → 輸出 movements_export.csv (UTF-8 BOM、Excel 可直接開)

    py -3.11 export_movements_csv.py --sku e01
        → 只匯 e01 藍牙耳機的進出

    py -3.11 export_movements_csv.py --warehouse north
        → 只匯北倉

    py -3.11 export_movements_csv.py --from 2026-05-01 --to 2026-05-25
        → 只匯日期區間

    py -3.11 export_movements_csv.py --direction out
        → 只匯出貨記錄
"""
import argparse
import csv
import json
from pathlib import Path

HERE = Path(__file__).parent          # = warehouse/data_tools/
WAREHOUSE = HERE.parent               # = warehouse/
SEED = WAREHOUSE / "test" / "seed_data.json"
OUT  = HERE / "movements_export.csv"  # 匯出到 data_tools/ 內


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sku",       type=str, default=None, help="只匯指定 SKU")
    ap.add_argument("--warehouse", type=str, default=None, help="只匯指定倉 (north/central/south)")
    ap.add_argument("--direction", type=str, default=None, help="只匯指定方向 (in/out)")
    ap.add_argument("--from",      type=str, default=None, dest="date_from", help="起始日 YYYY-MM-DD")
    ap.add_argument("--to",        type=str, default=None, dest="date_to",   help="結束日 YYYY-MM-DD")
    ap.add_argument("--out",       type=str, default=str(OUT), help="輸出檔路徑")
    args = ap.parse_args()

    seed = json.loads(SEED.read_text(encoding="utf-8"))
    items = {it["sku_id"]: it for it in seed["items"]}
    cat_label = {c["key"]: c["label"] for c in seed["categories"]}
    wh_label  = {w["key"]: w["label"] for w in seed["warehouses"]}

    rows = []
    for m in seed["movements"]:
        if args.sku       and m["sku_id"]    != args.sku:       continue
        if args.warehouse and m["warehouse"] != args.warehouse: continue
        if args.direction and m["direction"] != args.direction: continue
        if args.date_from and m["date"]      <  args.date_from: continue
        if args.date_to   and m["date"]      >  args.date_to:   continue

        it = items.get(m["sku_id"], {})
        rows.append({
            "日期":      m["date"],
            "SKU":       m["sku_id"],
            "商品名":    it.get("name", "?"),
            "類別":      cat_label.get(it.get("category"), "?"),
            "倉庫":      wh_label.get(m["warehouse"], m["warehouse"]),
            "方向":      "進貨" if m["direction"] == "in" else "出貨",
            "數量":      m["qty"],
            "單價":      it.get("unit_price", 0),
            "金額":      m["qty"] * it.get("unit_price", 0),
        })

    out_path = Path(args.out)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    print(f"OK 已匯出: {out_path}")
    print(f"  總筆數: {len(rows)}")
    if rows:
        print(f"  日期範圍: {rows[0]['日期']} ~ {rows[-1]['日期']}")
        in_qty  = sum(r["數量"] for r in rows if r["方向"] == "進貨")
        out_qty = sum(r["數量"] for r in rows if r["方向"] == "出貨")
        print(f"  進貨總量: {in_qty:,} 件")
        print(f"  出貨總量: {out_qty:,} 件")
        print(f"  淨變動:   {in_qty - out_qty:+,} 件")
    print()
    print("用 Excel 開啟即可（已加 UTF-8 BOM、中文不亂碼）。")


if __name__ == "__main__":
    main()
