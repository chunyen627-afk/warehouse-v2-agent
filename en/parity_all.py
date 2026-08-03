#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""parity_all.py — 中英文版**全面對等檢查**（前端 + 後端 + 模組 + 腳本）。

## 為什麼要有這支
user 只在中文版操作、由我負責英文版把關，**兩邊功能必須同步**。
但我已經犯過三次「只改/只測一版」：
  ① 中文版少 `/api/live_grid` 端點 → 商品清單展不開
  ② 中文版 `slice(0, 5)` 沒替換 → 異常清單說 70 幾項卻只列 5 筆
  ③ 中文版 `server.py` 沒 scp → HTML 報告被當 CSV 強制下載

⇒ 這支一次比對四層，秒級、不必開瀏覽器。**每次改動後必跑。**
   (前端細項在 `ui_parity.py`，這支會一併呼叫。)

用法（RPI5 ~/warehouse_v2_en）：python3 parity_all.py
"""
import re
import subprocess
import sys
from pathlib import Path

EN = Path("/home/p400/warehouse_v2_en")
ZH = Path("/home/p400/warehouse_v2")
BAD = []


def cmp(label, a, b, note=""):
    ok = a == b
    if not ok:
        BAD.append(label)
    print(f"  {'✅' if ok else '❌'} {label:<38} EN={a!s:<8} ZH={b!s:<8}{note}")
    return ok


def count(p: Path, pat: str) -> int:
    if not p.exists():
        return -1
    return len(re.findall(pat, p.read_text(encoding="utf-8"), re.S))


def main():
    print("=" * 72)
    print("① 前端結構（委派 ui_parity.py）")
    print("=" * 72)
    r = subprocess.run([sys.executable, str(EN / "ui_parity.py")],
                       capture_output=True, text=True)
    tail = [l for l in r.stdout.splitlines() if "同步" in l or "不同步" in l]
    print("  " + (tail[-1] if tail else "(ui_parity 無輸出)"))
    if r.returncode != 0:
        BAD.append("前端結構")
        for l in r.stdout.splitlines():
            if "❌" in l:
                print("   ", l.strip())

    print()
    print("=" * 72)
    print("② 後端 server.py：關鍵函式與端點")
    print("=" * 72)
    for pat, label in [
        (r"def _live_grid", "_live_grid 函式"),
        (r"def _live_push", "_live_push 函式"),
        (r"def _live_autostart", "開機自動啟動"),
        (r"def _demo_kick", "展場即時觸發"),
        (r"def _demo_run_schedule", "排程立即執行"),
        (r'@app\.get\("/api/live_grid"\)', "/api/live_grid 端點"),
        (r'@app\.post\("/api/live_mode"\)', "/api/live_mode 端點"),
        (r'fname\.endswith\("\.html"\)', "HTML 直接顯示"),
        (r"only_rule_id", "警示只檢查新規則"),
        (r"alert_checked_ok", "沒觸發也回報"),
        (r"list\(display_sockets\) \+ list\(all_sockets\)", "推播送訪客"),
        (r"import date_shift", "日期平移掛載"),
    ]:
        cmp(label, count(EN / "server.py", pat), count(ZH / "server.py", pat))

    print()
    print("=" * 72)
    print("③ 核心模組（邏輯應完全相同 → 行數也該一致）")
    print("=" * 72)
    for f in ("live_sim.py", "date_shift.py", "anomaly.py", "loader_v2.py"):
        a, b = EN / f, ZH / f
        cmp(f, len(a.read_text(encoding="utf-8").splitlines()) if a.exists() else -1,
            len(b.read_text(encoding="utf-8").splitlines()) if b.exists() else -1)

    print()
    print("=" * 72)
    print("④ live_sim 設定值（節奏/護欄必須一致）")
    print("=" * 72)
    for key in ("speedup", "sweep_all", "out_ratio", "out_qty_range",
                "in_qty_range", "floor_ratio", "ceil_ratio", "min_qty_floor",
                "default_safety"):
        pat = rf"^\s*{key}\s*=\s*(.+?)\s*(?:#.*)?$"
        def val(p):
            m = re.search(pat, (p / "live_sim.py").read_text(encoding="utf-8"), re.M)
            return m.group(1).strip() if m else "?"
        cmp(key, val(EN), val(ZH))

    print()
    print("=" * 72)
    print("⑤ anomaly 門檻（警示判準必須一致）")
    print("=" * 72)
    for key in ("burst_single_mult", "burst_single_min", "burst_min_history",
                "days_left_critical", "expiry_warning_days", "suppress_hours",
                "log_max_bytes"):
        pat = rf"^\s*{key}\s*=\s*(.+?)\s*(?:#.*)?$"
        def val(p):
            m = re.search(pat, (p / "anomaly.py").read_text(encoding="utf-8"), re.M)
            return m.group(1).strip() if m else "?"
        cmp(key, val(EN), val(ZH))

    print()
    print("=" * 72)
    print("⑥ 腳本（產出格式必須一致）")
    print("=" * 72)
    for f in ("stock_audit", "export_movements", "generate_report"):
        a = EN / "warehouse_data/scripts" / f"{f}.py"
        b = ZH / "warehouse_data/scripts" / f"{f}.py"
        cmp(f"{f} 存在", a.exists(), b.exists())
    for pat, label in [(r"VIEW:", "stock_audit 產 HTML"),
                       (r"def _status", "stock_audit 單倉判定"),
                       (r"colgroup", "stock_audit 表頭對齊"),
                       (r"_days_left", "stock_audit 撐天欄"),
                       (r"price\.get", "stock_audit 市值欄"),
                       (r"within 30 days|30 天內", "到期收窄 30 天")]:
        cmp(label,
            count(EN / "warehouse_data/scripts/stock_audit.py", pat),
            count(ZH / "warehouse_data/scripts/stock_audit.py", pat))
    for pat, label in [(r"VIEW:", "export_movements 產 HTML"),
                       (r"csv\.DictReader", "export 跳過表頭")]:
        cmp(label,
            count(EN / "warehouse_data/scripts/export_movements.py", pat),
            count(ZH / "warehouse_data/scripts/export_movements.py", pat))

    print()
    print("=" * 72)
    print("⑦ tools_v2：報告統一 + PO 網頁化")
    print("=" * 72)
    for pat, label in [
        (r'return commit_run_script\("stock_audit"', "generate_report 導向盤點"),
        (r"_generate_report_legacy", "舊版報告已停用保留"),
        (r"_po_html", "採購單產 HTML"),
        (r'"view_file"', "PO 回傳 view_file"),
    ]:
        cmp(label, count(EN / "tools_v2.py", pat), count(ZH / "tools_v2.py", pat))
    # baseline 也要同步（reset 會覆蓋回去）
    for side, root in (("EN", EN), ("ZH", ZH)):
        cur = root / "warehouse_data/scripts/stock_audit.py"
        base = root / "warehouse_data_baseline/scripts/stock_audit.py"
        ok = base.exists() and base.read_bytes() == cur.read_bytes()
        if not ok:
            BAD.append(f"{side} baseline 腳本")
        print(f"  {'✅' if ok else '❌'} {side} baseline 腳本與現行一致"
              f"{'' if ok else '  ← reset 後會變回舊版！'}")

    print()
    print("=" * 72)
    if BAD:
        print(f"❌ {len(BAD)} 項不同步：")
        for b in BAD:
            print(f"   - {b}")
    else:
        print("✅ 中英文版全面同步")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
