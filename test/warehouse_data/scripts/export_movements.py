"""
匯出進出紀錄：合併 transactions/*.csv 成單一 CSV + HTML
Usage: python export_movements.py [--data-dir <path>] [--days <n>]
"""
import sys, csv, pathlib, datetime, argparse, json


def _load_offset(dd: pathlib.Path) -> int:
    """時間軸平移天數（與 date_shift.py 同一套規則）。

    ⚠️ `transactions/` 的**檔名與內容都是原始日期**（2026-05-26），
    平移只在 loader_v2 載入到記憶體時做——這支腳本是獨立執行的、讀不到記憶體
    ⇒ **必須自己算一次**，否則匯出的日期會顯示三個月前
    （user 2026-08-03 回報「紀錄裡面日期都不對，還是 5 月多」）。
    """
    try:
        cfg = json.load(open(dd / "master" / "config.json", encoding="utf-8"))
        base = datetime.date.fromisoformat(str(cfg.get("snapshot_date", ""))[:10])
    except Exception:
        return 0
    today = datetime.date.today()
    # 與 date_shift._effective_today() 一致：只進不退（時鐘倒退時沿用較晚的）
    try:
        f = dd.parent / ".last_demo_date"
        if f.exists():
            seen = datetime.date.fromisoformat(f.read_text(encoding="utf-8").strip()[:10])
            if seen > today:
                today = seen
    except Exception:
        pass
    return (today - base).days


def _shift(ds: str, days: int) -> str:
    """原始日期 → 平移後日期。非日期或 offset=0 原樣回傳。"""
    if not ds or days == 0 or len(ds) < 10:
        return ds
    try:
        return str(datetime.date.fromisoformat(ds[:10]) + datetime.timedelta(days=days))
    except Exception:
        return ds


def _prune_outputs(out_dir, prefix, keep=8):
    """同類產出檔只保留最新 keep 份（2026-08-04，user 要求設上限）。

    起因：實測 audit/ 累積 46 個 stock_audit 檔、**零清理機制**，展場跑
    幾天會無限長大。訪客看完報告就關掉，不需要永久保留。
    ⚠️ 只清帶時間戳的產出檔；`*_changes.log`/`*_alerts.log` 是稽核記錄，
       絕不可刪（本函式靠 prefix 精確比對，碰不到它們）。
    ⚠️ 同一次產出有 .csv + .html 兩個檔 ⇒ 切片要 keep*2。
    清理失敗只吞例外不中斷 —— 產出比清理重要。
    """
    try:
        fs = sorted(out_dir.glob(prefix + "_*"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
        for old in fs[keep * 2:]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(pathlib.Path(__file__).parent.parent))
    parser.add_argument("--days", type=int, default=7, help="最近幾天（預設 7）")
    args = parser.parse_args()

    dd      = pathlib.Path(args.data_dir)
    tx_dir  = dd / "transactions"
    offset  = _load_offset(dd)

    # ⚠️ 兩個坑，都是「原始日期 vs 平移後日期」混用造成的：
    #   ① 不能用「今天往前 N 天」篩檔名——檔名是**原始日期**，日曆篩會篩到 0 筆。
    #      ⇒ 改用「最新的 N 個**有資料的日子**」。
    #   ② 動態模擬灌的今天（十幾萬筆 / 4.4MB）不能收——它的檔名已是
    #      平移後日期，會落在 seed 範圍之外，③ 的條件自然排除掉。
    #   ③ **只收「原始 seed 範圍內」的日子**（≤ 原始 snapshot_date）。
    #      執行期新寫入的檔（訪客操作、動態模擬）**檔名已經是平移後的日期**
    #      （commit_movement 用的 snap_date 就是平移後的今天）⇒ 再平移一次
    #      會變成未來（實測 2026-08-03 → 2026-10-11，15,240 筆假資料）。
    try:
        _cfg = json.load(open(dd / "master" / "config.json", encoding="utf-8"))
        _base_s = str(_cfg.get("snapshot_date", ""))[:10]
    except Exception:
        _base_s = ""
    #   ④ 原始 snapshot_date 平移後**正好落在今天**，而今天的真實異動是動態模擬
    #      在跑的（③ 已排除）⇒ seed 的那一天也要排除，否則「昨天」會給到今天。
    #      排除後：--days 1 = 昨天、--days 7 = 前七天，與引導選單的字面一致。
    _all_days = sorted({f.stem.split("_")[0] for f in tx_dir.glob("*.csv")
                        if (not _base_s or f.stem.split("_")[0] < _base_s)},
                       reverse=True)
    _keep = set(_all_days[:max(1, args.days)])

    rows = []
    for f in sorted(tx_dir.glob("*.csv")):
        if f.stem.split("_")[0] not in _keep:
            continue
        try:
            with open(f, encoding="utf-8-sig") as fp:
                # ⚠️ 用 DictReader 才會跳過表頭——原本 csv.reader 把
                #   `date,sku_id,warehouse,direction,qty` 這行也當成資料收進去。
                for r in csv.DictReader(fp):
                    rows.append([_shift(r.get("date", ""), offset), r.get("sku_id", ""),
                                 r.get("warehouse", ""), r.get("direction", ""),
                                 r.get("qty", "")])
        except Exception:
            pass

    now      = datetime.datetime.now()
    ts       = now.strftime("%Y%m%d_%H%M%S")
    out_dir  = dd / "audit"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"movements_{ts}.csv"

    ITEMS = {}
    ipath = dd / "master" / "items.csv"
    if ipath.exists():
        for r in csv.DictReader(open(ipath, encoding="utf-8-sig")):
            ITEMS[r["sku_id"]] = r["name"]
    WH = {"north": "北區倉", "central": "中區倉", "south": "南區倉"}
    # ⚠️ CSV 要與網頁 HTML 欄位一致（2026-08-04,user 抓到「csv 沒商品名稱」）：
    #   原本只寫 SKU（c03/d01）,訪客下載後看不懂是什麼商品。
    #   ITEMS/WH 對照表已在上面載入 ⇒ 補「商品」欄 + 倉別用中文標籤。
    #   保留 SKU 欄供對帳。
    _DIRW = {"in": "進貨", "out": "出貨"}
    with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["日期", "SKU", "商品", "倉別", "類型", "數量"])
        for row in rows:
            _d, _sku, _wh, _dir, _qty = row[0], row[1], row[2], row[3], row[4]
            w.writerow([_d, _sku, ITEMS.get(_sku, _sku),
                        WH.get(_wh, _wh), _DIRW.get(_dir, _dir), _qty])

    # ── HTML 版（訪客點連結直接看，不必下載）──
    #   同 stock_audit 的做法：RPI5 上用 LibreOffice 開 CSV 會卡死。
    html_file = out_dir / f"movements_{ts}.html"
    _in = sum(int(r[4] or 0) for r in rows if r[3] == "in")
    _out = sum(int(r[4] or 0) for r in rows if r[3] == "out")
    # 標題顯示**實際涵蓋的日期**（平移後），訪客一眼看得出是哪幾天
    _shown = sorted(_shift(d, offset) for d in _keep)
    _days_txt = _shown[0] if len(_shown) == 1 else f"{_shown[0]} ~ {_shown[-1]}"
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
td{{padding:5px 9px;border-bottom:1px solid #1e2530;white-space:nowrap}}
tr:nth-child(even){{background:rgba(255,255,255,.025)}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}} td.b{{font-weight:700}}
tr.i td.b{{color:#68d391}} tr.o td.b{{color:#fc8181}}
</style></head><body>
<h1>進出紀錄</h1>
<div class="sub">{now.strftime('%Y-%m-%d %H:%M')} &middot; 共 {len(_keep)} 天：
 {_days_txt} &middot; {len(rows):,} 筆</div>
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
    _prune_outputs(out_dir, "movements")   # 只保留最新 8 份（2026-08-04）
    print(f"SUMMARY:已匯出 {len(_keep)} 天共 {len(rows)} 筆進出紀錄"
          f"（{_days_txt}）至 audit/{out_file.name}")


if __name__ == "__main__":
    main()
