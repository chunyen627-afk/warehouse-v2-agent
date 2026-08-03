#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui_parity.py — 中英文版**前端**是否真的同步（結構層級比對）。

## 為什麼要有這支
我連續犯了兩次同樣的錯：改前端時只驗英文版就宣稱完成，中文版其實沒改到。
  ① 中文版「展不開」——server 少了 `/api/live_grid` 端點
  ② 中文版異常清單只顯示 5 筆——`slice(0, 5)` 沒被替換掉
兩次都是「某個替換的 assert 失敗 → 只補了看得到的部分 → 沒回頭核對兩版一致」。

⇒ 這支比對**兩版前端該有的結構特徵**，不看語言文字、只看程式結構。
   改完 index.html 一律跑這支（不必開瀏覽器，秒級）。

用法：python3 ui_parity.py
"""
import re
import sys
from pathlib import Path

EN = Path("/home/p400/warehouse_v2_en/templates/index.html")
ZH = Path("/home/p400/warehouse_v2/templates/index.html")

# (檢查名, 正則, 期望出現次數)  —— 只驗結構，不驗中英文字
CHECKS = [
    # 動態倉庫
    ("live 按鈕",              r'id="live-btn"', 1),
    ("live 看板列",            r'id="live-bar"', 1),
    ("60 商品清單容器",        r'id="live-grid"', 1),
    ("速度滑桿",               r'id="lb-speed"', 1),
    ("滑桿預設 200x",          r'id="lb-speed"[^>]*value="200"', 1),
    ("⛔ 不該有 sweep 勾選框",  r'id="lb-sweep"', 0),
    ("展開清單按鈕",           r'id="lb-grid-btn"', 1),
    ("toggleLive 函式",        r'function toggleLive\(\)', 1),
    ("toggleGrid 函式",        r'function toggleGrid\(\)', 1),
    ("tuneLive 函式",          r'function tuneLive\(\)', 1),
    ("renderGrid 函式",        r'function renderGrid\(', 1),
    ("三倉常數 _WH",           r"const _WH = \['north', 'central', 'south'\]", 1),
    ("live_batch handler",     r"msg\.type === 'live_batch'", 1),
    ("applySnapshot 函式",     r'function applySnapshot\(', 1),
    ("snapshot handler",       r"msg\.type === 'snapshot'", 1),
    # 異常清單
    ("異常清單全部列出",       r"const top = all\.filter\(a => a\.level !== 'info'\);", 1),
    ("⛔ 不該有 slice(0, 5)",  r"slice\(0, 5\)", 0),
    ("⛔ 不該有 ab-more",      r"ab-more", 0),
    ("清單可捲動",             r"\.ab-list \{[^}]*overflow-y:auto", 1),
    ("父層不擋捲動",           r"overflow-y:visible", 1),
    ("一行一筆(ab-txt)",       r"ab-txt", 2),
    ("異常橫幅節流刷新",       r"function pullAnomaliesThrottled\(\)", 1),
    ("live_batch 觸發刷新",    r"pullAnomaliesThrottled\(\);", 1),
    ("採購單開啟按鈕",        r'poView', 2),
    ("報告開啟按鈕",          r'viewHtml', 2),
    # 警示回報
    ("alert_checked_ok",       r"msg\.type === 'alert_checked_ok'", 1),
    ("alert-ok-banner CSS",    r"\.alert-ok-banner \{", 1),
    # 死碼不該殘留
    ("⛔ 不該有 live_movement", r"msg\.type === 'live_movement'", 0),
    ("⛔ 不該有 .live-move",    r"\.live-move", 0),
]


def scan(p: Path):
    s = p.read_text(encoding="utf-8")
    return {name: len(re.findall(pat, s, re.S)) for name, pat, _ in CHECKS}


def main():
    if not EN.exists() or not ZH.exists():
        print(f"❌ 找不到檔案 EN={EN.exists()} ZH={ZH.exists()}")
        return 1
    en, zh = scan(EN), scan(ZH)
    bad = 0
    print(f"{'檢查項':<28}{'英文':>6}{'中文':>6}{'期望':>6}  結果")
    print("-" * 62)
    for name, _, want in CHECKS:
        e, z = en[name], zh[name]
        ok = (e == want and z == want)
        if not ok:
            bad += 1
        print(f"{name:<28}{e:>6}{z:>6}{want:>6}  {'✅' if ok else '❌ 不同步'}")
    print("-" * 62)
    if bad:
        print(f"❌ {bad} 項不同步 —— 兩版前端必須一致")
    else:
        print(f"✅ 全部 {len(CHECKS)} 項中英同步")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
