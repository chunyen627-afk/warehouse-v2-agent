"""
月底盤點：掃全倉庫存、和安全庫存比較，產出 CSV + HTML 到 audit/
用法：python stock_audit.py [--data-dir <path>]
"""
import sys, csv, pathlib, datetime, argparse

# 類別 slug → 顯示名。訪客會下載這份 CSV，`appliance_kitchen` 不好讀。
# ⚠️ 這支腳本是獨立執行（不 import app 模組），所以這裡內嵌一份；
#    與 warehouse.py 的 CATEGORY_LABEL 保持一致。
CATEGORY_LABEL = {
    "electronics":       "電子產品",
    "appliance_kitchen": "家電廚具",
    "food_beverage":     "食品飲料",
    "daily_goods":       "日用品",
    "apparel":           "服飾",
    "sports":            "運動戶外",
}

WH_LABEL = {"north": "北區倉", "central": "中區倉", "south": "南區倉"}


def _status(safety_stock, wh_qtys, wh_keys=("north", "central", "south")):
    """狀態判定 —— 看**每個倉**，不是三倉總量。

    ⚠️ 2026-08-03（user 指出）：原本比「三倉總量 vs 安全庫存」，
    結果 60 個商品全是 OK，但異常橫幅同時說有缺貨 → 兩邊對不起來。
    根因：**單倉缺貨才是倉管真正在意的**——總量 500 個但中倉只剩 20 個，
    中倉照樣要斷貨、要調撥。總量看起來夠會掩蓋問題。
    ⇒ 改成逐倉判定，並標出是哪個倉（`低於安全：中區倉`）。
    """
    if not safety_stock:
        return "OK"
    bad = [WH_LABEL.get(k, k) for k, q in zip(wh_keys, wh_qtys) if q < safety_stock]
    if bad:
        return "低於安全：" + "、".join(bad)
    low = [WH_LABEL.get(k, k) for k, q in zip(wh_keys, wh_qtys)
           if q < safety_stock * 1.2]
    if low:
        return "偏低：" + "、".join(low)
    return "OK"


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
                    "北區倉", "中區倉", "南區倉", "合計", "狀態"])
        low_count = 0
        for sku_id, item in sorted(items.items()):
            ss    = safety.get(sku_id, 0)
            total = sku_total.get(sku_id, 0)
            wh_qtys = [stock_map.get((sku_id, wh), 0) for wh in warehouses]
            status = _status(ss, wh_qtys)
            if status != "OK":
                low_count += 1
            _cat = item.get("category", "")
            w.writerow([sku_id, item["name"], CATEGORY_LABEL.get(_cat, _cat),
                        ss, *wh_qtys, total, status])

    # ── 同時產一份 HTML（RPI5 上 LibreOffice 開 CSV 要 3 分鐘還卡住）──
    #   訪客點連結直接在瀏覽器看，不用開任何額外程式。CSV 保留給帶走分析。
    html_file = out_dir / f"stock_audit_{ts}.html"
    _rows = []
    for sku_id, item in sorted(items.items()):
        ss = safety.get(sku_id, 0)
        total = sku_total.get(sku_id, 0)
        wh_qtys = [stock_map.get((sku_id, wh), 0) for wh in warehouses]
        status = _status(ss, wh_qtys)
        cls = ("bad" if status.startswith("低於安全")
               else "warn" if status.startswith("偏低") else "ok")
        _cat = item.get("category", "")
        _rows.append(
            f'<tr class="{cls}"><td>{sku_id}</td><td>{item["name"]}</td>'
            f'<td>{CATEGORY_LABEL.get(_cat, _cat)}</td><td class="n">{ss}</td>'
            + "".join(f'<td class="n">{q}</td>' for q in wh_qtys)
            + f'<td class="n b">{total}</td><td>{status}</td></tr>')
    html_file.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<title>盤點報告 {ts}</title><style>
body{{font-family:system-ui,-apple-system,"Noto Sans TC",sans-serif;margin:0;padding:16px;
background:#131820;color:#e6edf3}}
h1{{font-size:19px;margin:0 0 4px}} .sub{{color:#8b98a5;font-size:13px;margin-bottom:14px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th{{position:sticky;top:0;background:#1a2130;text-align:left;padding:7px 9px;
border-bottom:2px solid #2d3748;font-size:11.5px;letter-spacing:.4px;text-transform:uppercase}}
/* 數字欄：表頭與數值都靠右，否則標題靠左、數字靠右會對不齊（user 回報） */
th.n{{text-align:right}}
/* 欄寬固定，避免每欄寬度隨內容跳動 */
col.w-num{{width:78px}} col.w-sku{{width:64px}} col.w-cat{{width:170px}} col.w-st{{width:110px}}
td{{padding:5px 9px;border-bottom:1px solid #1e2530}}
tr:nth-child(even){{background:rgba(255,255,255,.025)}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}} td.b{{font-weight:700;color:#90cdf4}}
tr.bad td{{background:rgba(229,62,62,.14)}} tr.warn td{{background:rgba(246,173,85,.12)}}
.legend{{margin-top:12px;font-size:12px;color:#8b98a5}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:2px;margin:0 4px 0 12px}}
</style></head><body>
<h1>庫存盤點報告</h1>
<div class="sub">{now.strftime('%Y-%m-%d %H:%M')} &middot; {len(items)} 個商品 &middot;
 {low_count} 項需注意</div>
<table>
<colgroup><col class="w-sku"><col><col class="w-cat"><col class="w-num">
<col class="w-num"><col class="w-num"><col class="w-num"><col class="w-num">
<col class="w-st"></colgroup>
<thead><tr><th>SKU</th><th>商品</th><th>類別</th><th class="n">安全庫存</th>
<th class="n">北區倉</th><th class="n">中區倉</th><th class="n">南區倉</th>
<th class="n">合計</th><th>狀態</th></tr></thead>
<tbody>{''.join(_rows)}</tbody></table>
<div class="legend"><span class="dot" style="background:#e53e3e"></span>低於安全庫存
<span class="dot" style="background:#f6ad55"></span>偏低（&lt;1.2 倍）
<span class="dot" style="background:#2d3748"></span>正常</div>
</body></html>""", encoding="utf-8")

    print(f"OUTPUT:{out_file}")
    print(f"VIEW:{html_file}")
    print(f"SUMMARY:共掃 {len(items)} 個商品、{low_count} 項需注意，報告已存至 audit/{out_file.name}")

if __name__ == "__main__":
    main()
