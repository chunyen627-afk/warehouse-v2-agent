#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""anomaly_probe.py — 背景 Agent（anomaly.py）的體檢（2026-08-03）。

先前所有測試都測「訪客問了什麼」，這支測**訪客沒問、系統自己做的事**：
5 個偵測器 + AlertManager 去重抑制 + 訪客看得到的告警文字。

要抓的類型（訪客看得到、但問答測試碰不到）：
  ① 偵測器靜默壞掉（零筆有兩種可能：資料前提不成立 vs 邏輯壞掉）
  ② 告警文字品質（⭐ 疊加、倉名沒英文化、異常值）
  ③ 抑制窗失效 → alerts.log 無限膨脹

⚠️ 判準重點：**零筆不等於正常**。本測用構造資料驗證邏輯本身，
   確保 demo 資料一變動（訪客玩到某個狀態）偵測器仍然正確。
   實測：po_short / dormant 在 demo 資料下都是 0 筆，但邏輯都正確。

⚠️ 本測**不改真實資料**（構造資料只存在記憶體 / 暫存目錄）。

用法（RPI5 ~/warehouse_v2_en）：python3 anomaly_probe.py
"""
import collections
import copy
import json
import tempfile
from datetime import timedelta
from pathlib import Path

import warehouse as W
import anomaly

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    if detail:
        print(f"       {detail[:220]}")


class _Fake:
    """包一層 state，只覆寫指定屬性（不動真實資料）。"""

    def __init__(self, real, **over):
        self._real = real
        self._over = over

    def __getattr__(self, k):
        if k in self._over:
            return self._over[k]
        return getattr(self._real, k)


def main():
    W.init(Path(__file__).parent / "seed_data.json")
    s = W.state()

    print("=" * 72)
    print("① 偵測器活性（零筆要分清：資料前提不成立 vs 邏輯壞掉）")
    print("=" * 72)
    counts = {}
    for det in anomaly._DETECTORS:
        try:
            counts[det.__name__] = len(det(s))
        except Exception as e:
            counts[det.__name__] = f"EXCEPTION {type(e).__name__}: {e}"
    for k, v in counts.items():
        print(f"   {k}: {v} 筆")
    check("① 五個偵測器都沒有拋例外",
          all(isinstance(v, int) for v in counts.values()), str(counts))

    print()
    print("=" * 72)
    print("② 零筆偵測器的邏輯驗證（構造資料）")
    print("=" * 72)

    # po_short：造一個標了 short_received 的 PO
    tmp = tempfile.mkdtemp()
    podir = Path(tmp) / "orders" / "PO"
    podir.mkdir(parents=True)
    real_po = Path(s.v2_data_dir) / "orders" / "PO"
    sample = sorted(real_po.glob("*.json"))[0]
    fake = copy.deepcopy(json.load(open(sample, encoding="utf-8")))
    fake["po_id"] = "PO_PROBE"
    ln = dict(fake["lines"][0])
    ln.update({"note": "short_received", "order_qty": 100, "received_qty": 88})
    fake["lines"] = [ln]
    json.dump(fake, open(podir / "PO_PROBE.json", "w", encoding="utf-8"),
              ensure_ascii=False)
    r_po = anomaly._detect_po_short(_Fake(s, v2_data_dir=tmp))
    check("② po_short 邏輯正確（構造短收 → 認得出）",
          len(r_po) == 1 and r_po[0]["level"] == "critical",
          r_po[0]["title"] if r_po else "沒偵測到")

    # dormant：把某高值品的出庫全推到很久以前
    target = None
    for it in s.items:
        tot = sum(s.stock.get(w["key"], {}).get(it["sku_id"], 0) for w in s.warehouses)
        if tot * it["unit_price"] >= anomaly.AnomalyConfig.dormant_min_value and tot > 0:
            target = it
            break
    old = (anomaly._today()
           - timedelta(days=anomaly.AnomalyConfig.dormant_days + 30)).isoformat()
    moves = [({**m, "date": old} if (m["sku_id"] == target["sku_id"]
                                     and m["direction"] == "out") else m)
             for m in s.movements]
    r_dm = anomaly._detect_dormant(_Fake(s, movements=moves))
    check("② dormant 邏輯正確（構造呆滯 → 認得出）",
          any(a["data"]["sku_id"] == target["sku_id"] for a in r_dm),
          r_dm[0]["title"] if r_dm else "沒偵測到")

    print()
    print("=" * 72)
    print("③ 訪客看得到的告警文字品質")
    print("=" * 72)
    alerts = anomaly._MANAGER.scan()
    print(f"   目前告警數: {len(alerts)}  分級: {anomaly._count_levels(alerts)}")

    stars = [a for a in alerts if a.get("title", "").count("⭐") > 1]
    check("③ ⭐ 不重複疊加（多條規則命中同一告警）", not stars,
          f"{len(stars)} 筆重複" + (f" 例: {stars[0]['title'][:60]}" if stars else ""))

    raw_keys = {w["key"] for w in s.warehouses}
    badwh = [a for a in alerts if a.get("detail", "").split()
             and a["detail"].split()[0] in raw_keys]
    check("③ 倉名已轉顯示名（不出現原始 key）", not badwh,
          f"{len(badwh)} 筆" + (f" 例: {badwh[0]['detail'][:60]}" if badwh else ""))

    txt = json.dumps(alerts, ensure_ascii=False)
    bad = [b for b in ("NaN", "Infinity", "None units", "undefined") if b in txt]
    check("③ 無異常值（NaN/Infinity/None）", not bad, str(bad))

    dls = [a["data"].get("days_left") for a in alerts
           if a.get("type") == "low_stock" and isinstance(a.get("data"), dict)]
    bad_dl = [x for x in dls if x is not None and (x < 0 or x > 100000)]
    check("③ 撐天無負數/爆量", not bad_dl, f"異常值 {bad_dl[:5]}")

    print()
    print("=" * 72)
    print("④ 去重與告警抑制")
    print("=" * 72)
    mgr = anomaly.AlertManager()
    a1 = mgr.filter_new(mgr.scan())
    a2 = mgr.filter_new(mgr.scan())      # 立刻再掃一次 → 應全被抑制
    check("④ 抑制窗有效（同一批立刻重掃 → 0 筆新告警）",
          len(a1) > 0 and len(a2) == 0,
          f"第一次 {len(a1)} 筆、第二次 {len(a2)} 筆")

    keys = collections.Counter(a["key"] for a in mgr.scan())
    dup = [k for k, c in keys.items() if c > 1]
    check("④ 同一次掃描內 key 不重複", not dup, f"重複 key: {dup[:3]}")

    print()
    print("=" * 72)
    ok = sum(1 for _, c, _ in RESULTS if c)
    print(f"總計 {ok}/{len(RESULTS)} PASS")
    for n, c, d in RESULTS:
        if not c:
            print(f"  FAIL: {n}\n        {d[:250]}")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
