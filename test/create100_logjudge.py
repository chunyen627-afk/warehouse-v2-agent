# -*- coding: utf-8 -*-
"""create100 —— 以**伺服器日誌**為權威的判定器（DOM 擷取層抖動的解藥）。

en r7/r8 實抓：kiosk DOM 擷取受 打字動畫/推論靜默/commit阻塞 三重干擾，
card=False 大多是擷取窗沒對準；但 journalctl 的 User/Answer/[confirm] 行
是伺服器親口說的事實。判定改讀日誌：
  User vid=N: <句子>            → 配對語料
  Answer vid=N: [view] ...      → 該句的回覆 view
  [confirm] ... (SKU: XXX-0001) → 真建立 + SKU 前綴反查類別
  Answer [inventory_single] (auto-matched to "name") → 查詢驗證

用法：
  ssh RPI5 "journalctl -u warehouse-v2-en --since '<time>' --no-pager" > log.txt
  python create100_logjudge.py corpus.txt log.txt [--lang en]
"""
import re
import sys

sys.path.insert(0, ".")
from categories import CATEGORIES

PREFIX2CAT = {v["prefix"]: k for k, v in CATEGORIES.items()}


def load_corpus(path):
    rows = []
    for ln in open(path, encoding="utf-8"):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        exp, _, sent = ln.partition("|")
        rows.append((exp.strip(), sent.strip()))
    return rows


def parse_log(path):
    """回 [(kind, text)]：user/answer/confirm 三種。"""
    ev = []
    for ln in open(path, encoding="utf-8", errors="replace"):
        m = re.search(r"User vid=\d+: (.+)$", ln)
        if m:
            ev.append(("user", m.group(1).strip()))
            continue
        m = re.search(r"Answer vid=\d+: \[([a-z_0-9]+)\] (.*)$", ln)
        if m:
            ev.append(("answer", (m.group(1), m.group(2)[:160])))
            continue
        m = re.search(r"\[confirm\] vid=\d+ item_create → .*SKU: ([A-Z]{3})-\d+",
                      ln)
        if m:
            ev.append(("confirm", m.group(1)))
            continue
    return ev


def main():
    corpus_path, log_path = sys.argv[1], sys.argv[2]
    rows = load_corpus(corpus_path)
    ev = parse_log(log_path)
    sys.stdout.reconfigure(encoding="utf-8")

    tally = {"✅": 0, "⚠️": 0, "❌": 0, "❓": 0, "?": 0}
    created_n, query_ok_n = 0, 0
    fails = []
    ei = 0
    for n, (exp, sent) in enumerate(rows, 1):
        # 找到這句的 user 事件
        ui = None
        for j in range(ei, len(ev)):
            if ev[j][0] == "user" and ev[j][1] == sent:
                ui = j
                break
        if ui is None:
            tally["?"] += 1
            fails.append((n, exp, sent, "句子沒到伺服器（送出層丟失）", ""))
            continue
        # 這句之後、下一個 user 之前的事件
        seg = []
        for j in range(ui + 1, len(ev)):
            if ev[j][0] == "user":
                break
            seg.append(ev[j])
        ei = ui + 1
        view, summ = ("", "")
        for k, v in seg:
            if k == "answer":
                view, summ = v
                break
        sku_pfx = next((v for k, v in seg if k == "confirm"), None)
        cat = PREFIX2CAT.get(sku_pfx or "", None)
        if sku_pfx:
            created_n += 1
            # 查詢驗證：後面兩個事件窗內找 inventory_single
            for j in range(ui + 1, min(ui + 8, len(ev))):
                if (ev[j][0] == "answer"
                        and ev[j][1][0] in ("inventory_single", "inventory")):
                    query_ok_n += 1
                    break

        asked_cat = view in ("item_create_step2",)
        asked_name = view in ("item_create_step1",)
        dup_hit = ("already exists" in summ or "已存在" in summ)
        clarify = view in ("clarify",)

        if exp == "dup":
            v = "✅" if (dup_hit or asked_name) else "❌"
            why = "dup 擋下" if v == "✅" else f"view={view}"
        elif exp == "noname":
            v = "✅" if asked_name else "❌"
            why = "" if v == "✅" else f"view={view}"
        elif exp == "any":
            v = "✅" if cat else ("❓" if (asked_cat or asked_name or clarify)
                                 else "❌")
            why = "" if v != "❌" else f"view={view} {summ[:60]}"
        elif exp == "ambig":
            v = "⚠️" if cat else ("❓" if (asked_cat or clarify) else "❌")
            why = f"歸 {cat}" if cat else ("反問" if v == "❓" else f"view={view}")
        else:
            if cat == exp:
                v, why = "✅", ""
            elif cat and cat != exp:
                v, why = "❌", f"期望 {exp} 建成 {cat}"
            elif asked_cat or clarify:
                v, why = "❓", "反問"
            elif dup_hit:
                v, why = "❌", "誤判重複"
            else:
                v, why = "❌", f"view={view} {summ[:60]}"
        tally[v] += 1
        line = f"[{n:3}] {v} {exp:<18} {sent[:40]}"
        if cat:
            line += f" → {cat}"
        if why:
            line += f"  ({why})"
        print(line)
        if v == "❌":
            fails.append((n, exp, sent, why, summ))

    print("\n==== 統計（log 權威判定）====")
    for k in ("✅", "⚠️", "❓", "❌", "?"):
        print(f"{k}  {tally[k]:3}")
    print(f"真建立 {created_n}  查詢驗證過 {query_ok_n}")
    if fails:
        print("\n==== ❌/? 明細 ====")
        for n, e, s, w, sm in fails:
            print(f"[{n}] {e} | {s}\n    {w} | {sm[:100]}")


main()
