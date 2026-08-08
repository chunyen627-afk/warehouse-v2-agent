# -*- coding: utf-8 -*-
"""create100 結果判定器 — 讀 create100_run.py 的 jsonl，出四級判定表。

判定四級（交接定調）：
  ✅ 對          歸到期望類別（或 dup/noname 句被正確擋下）
  ⚠️ 可接受      跨類商品歸到另一合理類（ambig 出卡即算）
  ❌ 錯          明顯不合理（要修）
  ❓ 反問        判不出來去問（保守派設計，不算錯）

用法：python create100_judge.py <out.jsonl> [--lang en]
"""
import json
import re
import sys

sys.path.insert(0, ".")
from categories import CATEGORIES

LABEL2KEY = {}
for k, v in CATEGORIES.items():
    LABEL2KEY[v["label_zh"]] = k
    LABEL2KEY[v["label_en"].lower()] = k


def parse_card(reply: str, lang: str):
    """從確認卡 innerText 抽 名稱/類別key。抽不到回 (None, None)。

    r30b：類別**以 SKU 前綴反查為主**——r28 卡上行內編輯把 19 類下拉整包
    攤進 innerText，「類別」文字解析會吃到下拉第一項「電子產品」（r8 全批
    誤判 electronics 的假 ❌）。SKU 由所選類別產生、r28d 改類還會重發號，
    是卡上唯一可靠的類別訊號；文字標籤降為備援（老斷面相容）。"""
    if lang == "zh":
        nm = re.search(r'名稱\s*\t?\s*([^\n\t]+)', reply)
        cm = re.search(r'類別\s*\t?\s*([^\n\t（(]+)', reply)
    else:
        nm = re.search(r'Name\s*\t?\s*([^\n\t]+)', reply, re.I)
        cm = re.search(r'Category\s*\t?\s*([^\n\t(（]+)', reply, re.I)
    name = nm.group(1).strip() if nm else None
    cat = None
    sm = re.search(r'SKU\s*\t?\s*([A-Z]{3})-\d+', reply)
    if sm:
        from categories import CATEGORY_PREFIX
        _p2c = {v: k for k, v in CATEGORY_PREFIX.items()}
        cat = _p2c.get(sm.group(1))
    if cat is None and cm:
        # en 卡片類別欄可能帶「· auto-detected, change if wrong」尾註
        lbl = cm.group(1).split("·")[0].strip()
        cat = LABEL2KEY.get(lbl) or LABEL2KEY.get(lbl.lower())
    return name, cat


def judge(rec: dict, lang: str):
    exp, reply, card = rec["expected"], rec["reply"], rec["has_card"]
    name, cat = parse_card(reply, lang)
    _rl = reply.lower()
    # r10：has_card 的 DOM slice 有邊界 race → 卡片以**文字**佐證即可
    #   （en 卡「please confirm」＋類別列解析成功；zh 卡「請確認」）
    if not card and cat and ("please confirm" in _rl or "請確認" in reply):
        card = True
    asked_cat = ("步驟 2/4" in reply or "step 2/4" in _rl
                 or "哪一類" in reply or "which category" in _rl
                 or "what category" in _rl)
    asked_name = ("步驟 1/4" in reply or "step 1/4" in _rl
                  or "what is the item called" in _rl
                  or "商品叫什麼名字" in reply or "說商品名稱就好" in reply
                  or "just say the name" in _rl)
    dup_hit = ("已存在" in reply or "already exists" in reply.lower())
    clarify = ("你想查" in reply or "請問" in reply or "❓" in reply)

    if exp == "dup":
        return ("✅" if dup_hit else "❌", name, cat, "dup 擋下" if dup_hit else "dup 沒擋")
    if exp == "noname":
        return ("✅" if asked_name and not card else "❌", name, cat,
                "問名字" if asked_name else "沒問名字")
    if exp == "any":
        if card:
            return ("✅", name, cat, "出卡")
        return ("❓", name, cat, "沒出卡（看細節）")
    if exp == "ambig":
        if card and cat:
            return ("⚠️", name, cat, f"歸 {cat}")
        if asked_cat or clarify:
            return ("❓", name, cat, "反問")
        return ("❌", name, cat, "非卡非反問")
    # 具體類別期望
    if card and cat:
        if cat == exp:
            return ("✅", name, cat, "")
        if cat == "other":
            # r25：一步設計下猜不到歸「其他」出卡（可改）——保守而非錯
            return ("❓", name, cat, "歸其他（保守）")
        return ("❌", name, cat, f"期望 {exp} 判成 {cat}")
    if asked_cat:
        return ("❓", name, cat, "反問類別")
    if dup_hit:
        return ("❌", name, cat, "誤判重複")
    return ("❌", name, cat, "未進建檔流程")


def main():
    path = sys.argv[1]
    lang = "en" if "--lang" in sys.argv and "en" in sys.argv else "zh"
    recs = [json.loads(l) for l in open(path, encoding="utf-8")]
    tally = {"✅": 0, "⚠️": 0, "❌": 0, "❓": 0}
    fails = []
    for r in recs:
        v, name, cat, why = judge(r, lang)
        tally[v] += 1
        mark = f"[{r['n']:3}] {v} {r['expected']:<18} {r['sent'][:36]}"
        if name:
            mark += f"  → 名稱[{name}] 類[{cat}]"
        if why:
            mark += f"  ({why})"
        print(mark)
        if v == "❌":
            fails.append((r["n"], r["sent"], why, r["reply"][:200]))
    print("\n==== 統計 ====")
    total = len(recs)
    for k in ("✅", "⚠️", "❓", "❌"):
        print(f"{k}  {tally[k]:3}  ({tally[k]*100//max(total,1)}%)")
    print(f"總數 {total}")
    if fails:
        print("\n==== ❌ 待修明細 ====")
        for n, s, why, rep in fails:
            print(f"[{n}] {s}\n    {why}\n    {rep}\n")


main()
