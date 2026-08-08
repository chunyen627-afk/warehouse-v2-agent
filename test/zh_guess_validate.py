# -*- coding: utf-8 -*-
"""r25 zh head-noun 分類的離線驗證電池：
① seed 60 商品（真類別=主檔） ② create100_gen 名稱池 152（真類別=池）
③ 歧義池 16（正解=None 反問）。鐵則：判錯必須 0 才可接線。"""
import sys

sys.path.insert(0, ".")
import warehouse as W

W.init("seed_data.json")
import tools_v2 as T
from create100_gen import POOLS, AMBIG

ok = wrong = ask = 0
wrongs = []

for it in W.state().items:
    got, why = T._zh_guess_category(it["name"])
    if got is None:
        ask += 1
    elif got == it["category"]:
        ok += 1
    else:
        wrong += 1
        wrongs.append((it["name"], it["category"], got, why))

pool_ok = pool_wrong = pool_ask = 0
for cat, pairs in POOLS.items():
    for zh, _ in pairs:
        got, why = T._zh_guess_category(zh)
        if got is None:
            pool_ask += 1
        elif got == cat:
            pool_ok += 1
        else:
            pool_wrong += 1
            wrongs.append((zh, cat, got, why))

amb_ask = amb_hit = 0
amb_hits = []
for zh, _ in AMBIG:
    got, why = T._zh_guess_category(zh)
    if got is None:
        amb_ask += 1
    else:
        amb_hit += 1
        amb_hits.append((zh, got, why))

sys.stdout.reconfigure(encoding="utf-8")
print(f"seed60   判對 {ok} / 判錯 {wrong} / 反問 {ask}")
print(f"pool152  判對 {pool_ok} / 判錯 {pool_wrong} / 反問 {pool_ask}")
print(f"ambig16  反問 {amb_ask} / 誤判 {amb_hit}")
if wrongs:
    print("== 判錯明細 ==")
    for n, exp, got, why in wrongs:
        print(f"  {n}: 期望 {exp} 判成 {got} ({why})")
if amb_hits:
    print("== 歧義誤判 ==")
    for n, got, why in amb_hits:
        print(f"  {n}: 判成 {got} ({why})")
