"""
匯出進出紀錄：合併 transactions/*.csv 成單一 CSV + HTML
Usage: python export_movements.py [--data-dir <path>] [--days <n>]
"""
import sys, csv, pathlib, datetime, argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(pathlib.Path(__file__).parent.parent))
    parser.add_argument("--days", type=int, default=7, help="最近幾天（預設 7）")
    args = parser.parse_args()

    dd      = pathlib.Path(args.data_dir)
    tx_dir  = dd / "transactions"
    # ⚠️ 不能用「今天往前 N 天」篩：transactions/ 的**檔名是原始日期**
    #   （未經時間軸平移），所以日曆篩會篩到空的（實測 0 筆）。
    #   改用「**最新的 N 個有資料的日子**」——語意一樣是「最近 N 天」，
    #   但不受平移影響。
    _today_s = datetime.date.today().isoformat()
    _all_days = sorted({f.stem.split("_")[0] for f in tx_dir.glob("*.csv")
                        if f.stem.split("_")[0] != _today_s}, reverse=True)
    _keep = set(_all_days[:args.days])

    # ⚠️ **排除今天**：動態模擬把一天壓成幾分鐘，今天會累積十幾萬筆
    #   （實測 16 萬 / 4.4MB），匯出檔會被灌爆、訪客也只看到滿滿今天的資料。
    #   進出紀錄要看的是**歷史軌跡**，今天的即時狀況用查詢就好。
    rows = []
    for f in sorted(tx_dir.glob("*.csv")):
        # ⚠️ 排除今天：動態模擬把一天壓成幾分鐘，今天會累積十幾萬筆
        #   （實測 16 萬 / 4.4MB），匯出檔會被灌爆、訪客也只看到今天的資料。
        if f.stem.split("_")[0] not in _keep:
            continue
        try:
            with open(f, encoding="utf-8-sig") as fp:
                # ⚠️ 用 DictReader 才會跳過表頭——原本 csv.reader 把
                #   `date,sku_id,warehouse,direction,qty` 這行也當成資料收進去。
                for r in csv.DictReader(fp):
                    rows.append([r.get("date", ""), r.get("sku_id", ""),
                                 r.get("warehouse", ""), r.get("direction", ""),
                                 r.get("qty", "")])
        except Exception:
            pass

    now      = datetime.datetime.now()
    ts       = now.strftime("%Y%m%d_%H%M%S")
    out_dir  = dd / "audit"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"movements_{ts}.csv"

    with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["日期", "SKU", "倉別", "類型", "數量"])
        for row in rows:
            w.writerow(row)

    # ── HTML 版（訪客點連結直接看，不必下載）──
    #   同 stock_audit 的做法：RPI5 上用 LibreOffice 開 CSV 會卡死。
    ITEMS = {}
    ipath = dd / "master" / "items.csv"
    if ipath.exists():
        for r in csv.DictReader(open(ipath, encoding="utf-8-sig")):
            ITEMS[r["sku_id"]] = r["name"]
    WH = {"north": "北區倉", "central": "中區倉", "south": "南區倉"}
    html_file = out_dir / f"movements_{ts}.html"
    _in = sum(int(r[4] or 0) for r in rows if r[3] == "in")
    _out = sum(int(r[4] or 0) for r in rows if r[3] == "out")
    _tr = "".join(
        f'<tr class="{"i" if r[3] == "in" else "o"}"><td>{r[0]}</td>'
        f'<td>{ITEMS.get(r[1], r[1])}</td><td>{WH.get(r[2], r[2])}</td>'
        f'<td>{"進貨" if r[3] == "in" else "出貨"}</td>'
        f'<td class="n b">{"+" if r[3] == "in" else "-"}{r[4]}</td></tr>'
        for r in sorted(rows, key=lambda x: x[0], reverse=True)[:2000])
    html_file.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<title>進出紀錄 {ts}</title><style>
body{{font-family:system-ui,-apple-system,"Noto Sans TC",sans-serif;margin:0;padding:16px;
background:#131820;color:#e6edf3}}
h1{{font-size:19px;margin:0 0 4px}} .sub{{color:#8b98a5;font-size:13px;margin-bottom:14px}}
.kpis{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}}
.kpi{{background:rgba(255,255,255,.05);border-radius:8px;padding:9px 14px;min-width:110px}}
.kpi .k{{font-size:11px;color:#8b98a5;text-transform:uppercase;letter-spacing:.4px}}
.kpi .v{{font-size:20px;font-weight:700;margin-top:2px}}
.kpi .v.i{{color:#68d391}} .kpi .v.o{{color:#fc8181}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th{{position:sticky;top:0;background:#1a2130;text-align:left;padding:7px 9px;
border-bottom:2px solid #2d3748;font-size:11.5px;letter-spacing:.4px;text-transform:uppercase}}
th.n{{text-align:right}}
td{{padding:5px 9px;border-bottom:1px solid #1e2530}}
tr:nth-child(even){{background:rgba(255,255,255,.025)}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}} td.b{{font-weight:700}}
tr.i td.b{{color:#68d391}} tr.o td.b{{color:#fc8181}}
</style></head><body>
<h1>進出紀錄</h1>
<div class="sub">{now.strftime('%Y-%m-%d %H:%M')} &middot; 最近 {args.days} 天
 &middot; {len(rows):,} 筆</div>
<div class="kpis">
  <div class="kpi"><div class="k">筆數</div><div class="v">{len(rows):,}</div></div>
  <div class="kpi"><div class="k">進貨</div><div class="v i">+{_in:,}</div></div>
  <div class="kpi"><div class="k">出貨</div><div class="v o">-{_out:,}</div></div>
  <div class="kpi"><div class="k">淨變動</div><div class="v">{_in - _out:+,}</div></div>
</div>
<table><thead><tr><th>日期</th><th>商品</th><th>倉別</th><th>類型</th>
<th class="n">數量</th></tr></thead><tbody>{_tr}</tbody></table>
{f'<div class="sub" style="margin-top:10px">顯示最新 2,000 筆（共 {len(rows):,} 筆），完整資料請看 CSV。</div>' if len(rows) > 2000 else ''}
</body></html>""", encoding="utf-8")

    print(f"OUTPUT:{out_file}")
    print(f"VIEW:{html_file}")
    print(f"SUMMARY:已匯出最近 {args.days} 天共 {len(rows)} 筆進出紀錄（不含今天）至 audit/{out_file.name}")

if __name__ == "__main__":
    main()
