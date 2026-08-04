# -*- coding: utf-8 -*-
"""export_parity.py — 匯出產出物一致性檢查（2026-08-04）。

起因：user 實測抓到「CSV 沒商品名稱,好像比網頁資訊少」——
同一次匯出的 CSV 與 HTML **資訊不對等**（CSV 只有 SKU、HTML 有商品名）。
先前所有測試都只驗「有沒有產出」「天數對不對」,**沒有人比對兩種格式的內容**。

這支補上那一格。四類檢查：
  ① 欄位對等：HTML 有的欄位,CSV 不能缺（CSV 可以多,例如 SKU 供對帳）
  ② 筆數一致：CSV 資料列數 == HTML 資料列數
  ③ 內容可讀：CSV 不可只有代號（商品欄要是名字、倉別要是標籤,不是 north/in）
  ④ 天數符合：--days N 產出的實際天數 == N（或資料上限）

用法：python3 export_parity.py            # 中文版
      python3 export_parity.py --en       # 英文版
"""
import csv
import io
import pathlib
import re
import subprocess
import sys

EN = "--en" in sys.argv
ROOT = pathlib.Path("/home/p400/warehouse_v2_en" if EN else "/home/p400/warehouse_v2")
DD = ROOT / "warehouse_data"
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

fails = []


def check(name, ok, detail=""):
    print(("  ✅ " if ok else "  ❌ ") + name + (f"  — {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def newest(pat):
    fs = sorted((DD / "audit").glob(pat), key=lambda p: p.stat().st_mtime)
    return fs[-1] if fs else None


print(f"{'='*66}\n  匯出產出一致性 ({'EN' if EN else 'ZH'})\n{'='*66}")

for days in (1, 7, 30):
    r = subprocess.run([sys.executable, str(DD / "scripts" / "export_movements.py"),
                        "--data-dir", str(DD), "--days", str(days)],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        check(f"--days {days} 執行", False, r.stderr[:120])
        continue
    cf, hf = newest("movements_*.csv"), newest("movements_*.html")
    if not cf or not hf:
        check(f"--days {days} 產出兩種格式", False, "缺 CSV 或 HTML")
        continue

    rows = list(csv.reader(open(cf, encoding="utf-8-sig")))
    head, body = rows[0], rows[1:]
    html = hf.read_text(encoding="utf-8")
    hheads = [re.sub(r"<[^>]*>", "", m) for m in
              re.findall(r"<th[^>]*>.*?</th>", html, re.S)]
    hrows = len(re.findall(r"<tr[^>]*>\s*<td", html))

    print(f"\n[--days {days}]  CSV {len(body)} 列 · HTML {hrows} 列")
    # ① 欄位對等
    missing = [h for h in hheads if h.strip() and h.strip() not in
               [c.strip() for c in head]]
    check("欄位對等（HTML 有的 CSV 不缺）", not missing,
          f"CSV 缺: {missing}" if missing else f"CSV={head}")
    # ② 筆數一致
    check("筆數一致", len(body) == hrows, f"CSV {len(body)} vs HTML {hrows}")
    # ③ 內容可讀（不可只有代號）
    if body:
        joined = ",".join(body[0])
        raw_code = bool(re.search(r"\b(north|central|south)\b", joined)) or \
                   bool(re.fullmatch(r"[a-z]\d{2}", body[0][2].strip())
                        if len(body[0]) > 2 else False)
        check("內容可讀（非原始代號）", not raw_code, f"首列: {joined[:70]}")
    # ④ 天數符合
    if body:
        got = len({r[0] for r in body if r and r[0]})
        check(f"天數 == {days}", got == days or got < days,
              f"實際 {got} 天（資料上限可能較少）")

print(f"\n{'='*66}")
print(f"  結果：{'✅ 全部通過' if not fails else '❌ ' + str(len(fails)) + ' 項失敗 → ' + str(fails)}")
print(f"{'='*66}")
sys.exit(1 if fails else 0)
