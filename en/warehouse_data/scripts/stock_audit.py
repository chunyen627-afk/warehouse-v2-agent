"""
Month-end stocktake: scan all warehouses, compare against safety stock,
write a CSV report to audit/
Usage: python stock_audit.py [--data-dir <path>]
"""
import sys, csv, pathlib, datetime, argparse

# 類別 slug → 顯示名。訪客會下載這份 CSV，`appliance_kitchen` 不好讀。
# ⚠️ 這支腳本是獨立執行（不 import app 模組），所以這裡內嵌一份；
#    與 warehouse.py 的 CATEGORY_LABEL 保持一致。
CATEGORY_LABEL = {
    "electronics":       "Electronics",
    "appliance_kitchen": "Appliance & Kitchen",
    "food_beverage":     "Food & Beverage",
    "daily_goods":       "Daily Goods",
    "apparel":           "Apparel",
    "sports":            "Sports",
}

WH_LABEL = {"north": "North", "central": "Central", "south": "South"}


def _status(safety_stock, wh_qtys, wh_keys=("north", "central", "south")):
    """狀態判定 —— 看**每個倉**，不是三倉總量。

    ⚠️ 2026-08-03（user 指出）：原本比「三倉總量 vs 安全庫存」，
    結果 60 個商品全是 OK，但異常橫幅同時說有缺貨 → 兩邊對不起來。
    根因：**單倉缺貨才是倉管真正在意的**——總量 500 個但中倉只剩 20 個，
    中倉照樣要斷貨、要調撥。總量看起來夠會掩蓋問題。
    ⇒ 改成逐倉判定，並標出是哪個倉（`Below: Central`）。
    """
    if not safety_stock:
        return "OK"
    bad = [WH_LABEL.get(k, k) for k, q in zip(wh_keys, wh_qtys) if q < safety_stock]
    if bad:
        return "Below: " + ", ".join(bad)
    low = [WH_LABEL.get(k, k) for k, q in zip(wh_keys, wh_qtys)
           if q < safety_stock * 1.2]
    if low:
        return "Low: " + ", ".join(low)
    return "OK"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(pathlib.Path(__file__).parent.parent))
    args = parser.parse_args()

    dd = pathlib.Path(args.data_dir)
    master = dd / "master"

    # read items + safety stock from items.csv
    items = {}
    safety = {}
    for r in csv.DictReader(open(master / "items.csv", encoding="utf-8-sig")):
        items[r["sku_id"]] = r
        safety[r["sku_id"]] = int(r.get("safety_stock") or 0)

    # read per-warehouse quantities from stock.csv
    stock_map = {}  # (sku, warehouse) -> qty
    sku_total = {}
    for r in csv.DictReader(open(master / "stock.csv", encoding="utf-8-sig")):
        sku, wh, qty = r["sku_id"], r["warehouse"], int(r.get("qty") or 0)
        stock_map[(sku, wh)] = qty
        sku_total[sku] = sku_total.get(sku, 0) + qty

    # 單價（庫存市值用）
    price = {}
    for r in csv.DictReader(open(master / "items.csv", encoding="utf-8-sig")):
        try:
            price[r["sku_id"]] = int(float(r.get("unit_price") or 0))
        except Exception:
            price[r["sku_id"]] = 0

    # 效期批次（到期清單用）。⚠️ 同一商品可能有 **11 個批次**（新貨舊貨混）
    #   ⇒ 倉管實務是 FEFO（先出效期最早的），所以**一列一個批次**、
    #   按剩餘天數排序。不做平均（無意義）、不取最晚（會掩蓋快過期的量）。
    batches = []
    bpath = master / "batches.csv"
    if bpath.exists():
        for r in csv.DictReader(open(bpath, encoding="utf-8-sig")):
            try:
                exp = datetime.date.fromisoformat(r["expire_date"])
            except Exception:
                continue
            batches.append({"sku": r["sku_id"], "wh": r["warehouse"],
                            "qty": int(r.get("qty") or 0), "exp": exp})

    # 出貨累計（熱銷排行用）——與 generate_report 同一套來源
    sku_sales = {}
    tx_dir = dd / "transactions"
    if tx_dir.exists():
        for f in sorted(tx_dir.glob("*.csv")):
            for r in csv.DictReader(open(f, encoding="utf-8-sig")):
                if r.get("direction") == "out":
                    k = r["sku_id"]
                    sku_sales[k] = sku_sales.get(k, 0) + int(r.get("qty") or 0)

    # 撐幾天 = 現有量 ÷ 日均出貨。比「低於安全庫存」更直觀。
    #   ⚠️ 兩個坑（2026-08-03 實測）：
    #   ① **今天必須排除**——動態模擬把一天壓成幾分鐘，今天出貨量
    #      是平常的數十倍（實測 133,377 件 vs 平常 ~150），拿來算日均
    #      會讓每個商品都「只撐 1 天」。
    #   ② transactions/ 的**檔名是原始日期**（未平移），所以不能用
    #      「近 30 天」篩——會篩到空的。改用**全部歷史 ÷ 實際天數**。
    _today0 = datetime.date.today()
    _today_s = _today0.isoformat()
    sku_hist = {}
    hist_days = set()
    if tx_dir.exists():
        for f in sorted(tx_dir.glob("*.csv")):
            for r in csv.DictReader(open(f, encoding="utf-8-sig")):
                d = r.get("date") or ""
                if r.get("direction") != "out" or d == _today_s:
                    continue                      # 跳過今天（模擬灌的）
                hist_days.add(d)
                k = r["sku_id"]
                sku_hist[k] = sku_hist.get(k, 0) + int(r.get("qty") or 0)
    _ndays = max(1, len(hist_days))

    def _days_left(sku, qty):
        burn = sku_hist.get(sku, 0) / _ndays
        if burn <= 0:
            return None
        return int(qty / burn)

    now      = datetime.datetime.now()
    ts       = now.strftime("%Y%m%d_%H%M%S")
    out_dir  = dd / "audit"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"stock_audit_{ts}.csv"

    warehouses = ["north", "central", "south"]

    with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["SKU", "Item", "Category", "Safety stock",
                    "North", "Central", "South", "Total",
                    "Days left", "Value NT$", "Status"])
        low_count = 0
        for sku_id, item in sorted(items.items()):
            ss    = safety.get(sku_id, 0)
            total = sku_total.get(sku_id, 0)
            wh_qtys = [stock_map.get((sku_id, wh), 0) for wh in warehouses]
            status = _status(ss, wh_qtys)
            if status != "OK":
                low_count += 1
            _cat = item.get("category", "")
            _dl = _days_left(sku_id, total)
            w.writerow([sku_id, item["name"], CATEGORY_LABEL.get(_cat, _cat),
                        ss, *wh_qtys, total,
                        "" if _dl is None else _dl,
                        total * price.get(sku_id, 0), status])

    # ── 同時產一份 HTML（RPI5 上 LibreOffice 開 CSV 要 3 分鐘還卡住）──
    #   訪客點連結直接在瀏覽器看，不用開任何額外程式。CSV 保留給帶走分析。
    # ── 合併報告 HTML：總覽 / 需注意 / 熱銷 TOP10 / 完整庫存表 ──
    #   user 定調 2026-08-03：盤點與體檢報告資料來源相同、只是呈現不同
    #   ⇒ 合成一份，訪客不用分辨要跑哪一個。
    #   ⚠️ 判定一律用**單倉**（`_status`）——體檢報告原本用三倉總量，
    #     會顯示 Health 100% 但實際有倉別缺貨。
    html_file = out_dir / f"stock_audit_{ts}.html"
    _rows, _attention = [], []
    for sku_id, item in sorted(items.items()):
        ss = safety.get(sku_id, 0)
        total = sku_total.get(sku_id, 0)
        wh_qtys = [stock_map.get((sku_id, wh), 0) for wh in warehouses]
        status = _status(ss, wh_qtys)
        cls = ("bad" if status.startswith("Below")
               else "warn" if status.startswith("Low") else "ok")
        _cat = item.get("category", "")
        _dl = _days_left(sku_id, total)
        _val = total * price.get(sku_id, 0)
        _dl_cls = "n" + (" bad" if _dl is not None and _dl <= 14 else "")
        cells = (f'<td>{sku_id}</td><td>{item["name"]}</td>'
                 f'<td>{CATEGORY_LABEL.get(_cat, _cat)}</td><td class="n">{ss}</td>'
                 + "".join(f'<td class="n">{q}</td>' for q in wh_qtys)
                 + f'<td class="n b">{total}</td>'
                 + f'<td class="{_dl_cls}">{"-" if _dl is None else _dl}</td>'
                 + f'<td class="n">{_val:,}</td><td>{status}</td>')
        _rows.append(f'<tr class="{cls}">{cells}</tr>')
        if status != "OK":
            _attention.append(f'<tr class="{cls}">{cells}</tr>')

    _top = sorted(sku_sales.items(), key=lambda kv: -kv[1])[:10]
    _top_rows = "".join(
        f'<tr><td class="n">{i}</td><td>{items.get(k,{}).get("name",k)}</td>'
        f'<td class="n">{v}</td><td class="n b">{sku_total.get(k,0)}</td></tr>'
        for i, (k, v) in enumerate(_top, 1))

    # 到期清單：90 天內（一季）且還有量。門檻比警示的 14 天寬——
    #   報告是「看全貌」，14 天在這裡幾乎是空的；14 天那層由異常警示負責。
    _today = now.date()
    _exp = sorted(
        ({**b, "days": (b["exp"] - _today).days} for b in batches
         if b["qty"] > 0 and 0 <= (b["exp"] - _today).days <= 30),
        key=lambda x: x["days"])
    _exp_rows = "".join(
        f'<tr class="{"bad" if b["days"] <= 14 else "warn" if b["days"] <= 30 else ""}">'
        f'<td>{items.get(b["sku"], {}).get("name", b["sku"])}</td>'
        f'<td>{WH_LABEL.get(b["wh"], b["wh"])}</td>'
        f'<td class="n b">{b["qty"]}</td><td class="n">{b["exp"]}</td>'
        f'<td class="n">{b["days"]}</td></tr>' for b in _exp)

    _health = round((1 - low_count / max(1, len(items))) * 100)
    _thead = ('<tr><th>SKU</th><th>Item</th><th>Category</th><th class="n">Safety</th>'
              '<th class="n">North</th><th class="n">Central</th><th class="n">South</th>'
              '<th class="n">Total</th><th class="n">Days left</th>'
              '<th class="n">Value NT$</th><th>Status</th></tr>')
    _cols = ('<colgroup><col class="w-sku"><col><col class="w-cat"><col class="w-num">'
             '<col class="w-num"><col class="w-num"><col class="w-num"><col class="w-num">'
             '<col class="w-num"><col class="w-val"><col class="w-st"></colgroup>')
    html_file.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<title>Warehouse Report {ts}</title><style>
body{{font-family:system-ui,-apple-system,"Noto Sans TC",sans-serif;margin:0;padding:16px;
background:#131820;color:#e6edf3}}
h1{{font-size:19px;margin:0 0 4px}} .sub{{color:#8b98a5;font-size:13px;margin-bottom:14px}}
h2{{font-size:14px;margin:22px 0 8px;color:#90cdf4;letter-spacing:.3px}}
.kpis{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:6px}}
.kpi{{background:rgba(255,255,255,.05);border-radius:8px;padding:9px 14px;min-width:110px}}
.kpi .k{{font-size:11px;color:#8b98a5;text-transform:uppercase;letter-spacing:.4px}}
.kpi .v{{font-size:20px;font-weight:700;margin-top:2px}}
.kpi .v.good{{color:#68d391}} .kpi .v.bad{{color:#fc8181}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:4px}}
th{{position:sticky;top:0;background:#1a2130;text-align:left;padding:7px 9px;
border-bottom:2px solid #2d3748;font-size:11.5px;letter-spacing:.4px;text-transform:uppercase}}
th.n{{text-align:right}}
col.w-num{{width:74px}} col.w-val{{width:96px}} col.w-sku{{width:64px}} col.w-cat{{width:170px}} col.w-st{{width:120px}}
col.w-date{{width:110px}} col.w-day{{width:88px}} col.w-wh{{width:100px}}
td{{padding:5px 9px;border-bottom:1px solid #1e2530;white-space:nowrap}}
/* 每列只佔一行：品名太長用省略號截斷，日期/數字一律不換行
   （user 回報「2026-08-03」曾被折成兩行）*/
td:first-child{{overflow:hidden;text-overflow:ellipsis;max-width:340px}}
tr:nth-child(even){{background:rgba(255,255,255,.025)}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}} td.b{{font-weight:700;color:#90cdf4}}
tr.bad td{{background:rgba(229,62,62,.16)}} tr.warn td{{background:rgba(246,173,85,.13)}}
/* 有問題的列左側加粗色條，在 60 列裡也一眼找得到（user 建議） */
tr.bad td:first-child{{box-shadow:inset 3px 0 0 #e53e3e}}
tr.warn td:first-child{{box-shadow:inset 3px 0 0 #f6ad55}}
td.bad{{color:#fc8181;font-weight:700}}
.empty{{color:#68d391;font-size:13px;padding:8px 2px}}
.legend{{margin-top:10px;font-size:12px;color:#8b98a5}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:2px;margin:0 4px 0 12px}}
</style></head><body>
<h1>Warehouse Report</h1>
<div class="sub">{now.strftime('%Y-%m-%d %H:%M')}</div>

<div class="kpis">
  <div class="kpi"><div class="k">SKUs</div><div class="v">{len(items)}</div></div>
  <div class="kpi"><div class="k">Total units</div>
    <div class="v">{sum(sku_total.values()):,}</div></div>
  <div class="kpi"><div class="k">Stock value</div>
    <div class="v">NT$ {sum(sku_total.get(k, 0) * price.get(k, 0) for k in items):,}</div></div>
  <div class="kpi"><div class="k">Need attention</div>
    <div class="v {'bad' if low_count else 'good'}">{low_count}</div></div>
  <div class="kpi"><div class="k">Expiring 30d</div>
    <div class="v {'bad' if _exp else 'good'}">{len(_exp)}</div></div>
  <div class="kpi"><div class="k">Health</div>
    <div class="v {'good' if _health >= 90 else 'bad'}">{_health}%</div></div>
</div>

<h2>Needs attention</h2>
{f'<table>{_cols}<thead>{_thead}</thead><tbody>{"".join(_attention)}</tbody></table>'
 if _attention else '<div class="empty">All warehouses are above safety stock.</div>'}

<h2>Top 10 by outbound</h2>
<table><thead><tr><th class="n">#</th><th>Item</th><th class="n">Shipped</th>
<th class="n">On hand</th></tr></thead><tbody>{_top_rows}</tbody></table>

<h2>Expiring soon (within 30 days)</h2>
{f'<table><colgroup><col><col class="w-wh"><col class="w-num"><col class="w-date">'
 f'<col class="w-day"></colgroup><thead><tr><th>Item</th><th>Warehouse</th>'
 f'<th class="n">Qty</th><th class="n">Expires</th><th class="n">Days left</th>'
 f'</tr></thead><tbody>{_exp_rows}</tbody></table>'
 if _exp else '<div class="empty">No batches expiring within 30 days.</div>'}

<h2>Full inventory ({len(items)} items)</h2>
<table>{_cols}<thead>{_thead}</thead><tbody>{''.join(_rows)}</tbody></table>
<div class="legend"><span class="dot" style="background:#e53e3e"></span>Below safety
<span class="dot" style="background:#f6ad55"></span>Low (&lt;1.2x)
<span class="dot" style="background:#2d3748"></span>OK</div>
</body></html>""", encoding="utf-8")

    print(f"OUTPUT:{out_file}")
    print(f"VIEW:{html_file}")
    print(f"SUMMARY:{len(items)} SKUs scanned, {low_count} need attention. Report saved to audit/{out_file.name}")

if __name__ == "__main__":
    main()
