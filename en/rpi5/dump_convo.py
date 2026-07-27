# -*- coding: utf-8 -*-
"""撈訪客對話紀錄 → 直接產出可跑的測試劇本。

為什麼要這支（user 定調 2026-07-27）：
  展場的**真實訪客對話是最有價值的測試素材**——比我們自己造的句子真實得多。
  但目前對話只存在 journald：
    ① 不會永久保留（SystemMaxUse=10G，滿了自動輪替刪舊的）
    ② 要撈很麻煩（journalctl + grep + 正規表達式）
    ③ 問答分離（User/Answer 各一行，要自己按 vid 配對）

輸出兩種格式：
  --format convo  → ws_convo.py 劇本（每位訪客一個 ### 區塊，可直接回放）
  --format tsv    → 時間 / vid / 語言 / 問 / view / 答（給人看、給 Excel）

用法：
  python3 dump_convo.py --since '2 hours ago'
  python3 dump_convo.py --since today --format convo -o /tmp/_conv_real.txt
  python3 dump_convo.py --since '3 days ago' --port 8001   # 只撈中文版

⚠️ scp 上去執行（中文在 SSH heredoc 會被吃掉）。
"""
import argparse
import re
import subprocess
import sys
from collections import defaultdict

UNIT = {8001: "warehouse-v2", 8002: "warehouse-v2-en"}
LANG = {"warehouse-v2": "中文", "warehouse-v2-en": "英文"}
RE_USER = re.compile(r"User vid=(\d+): (.*)$")
RE_ANS = re.compile(r"Answer vid=(\d+): \[([a-z_]+)\] (.*)$")
# ⚠️ 用 -o short-iso 統一時間格式——journald 預設是**中文月份**（「7月 27」），
#   用 `\S+\s+\d+` 那種 regex 對不上（踩過，時間欄整欄空白）。
RE_HEAD = re.compile(r"^(\S+)\s+\S+\s+(\S+)\[\d+\]:")


def fetch(unit, since):
    """一次撈一個服務——這樣不必從 pid 反查來源（服務重啟後 pid 會變，
    log 裡的舊 pid 對不到現在的服務，來源欄會全是 ?）。"""
    cmd = ["sudo", "journalctl", "--no-pager", "-o", "short-iso",
           "--since", since, "-u", unit]
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def parse(raw, unit):
    """→ [(ts, vid, unit, question, view, answer)]，按時間序。"""
    turns = []
    pending = {}          # vid → (ts, question)
    for ln in raw.splitlines():
        m_head = RE_HEAD.match(ln)
        ts = m_head.group(1) if m_head else ""

        m = RE_USER.search(ln)
        if m:
            vid, q = m.group(1), m.group(2).strip()
            # 前一句沒等到 Answer（訪客中途斷線）也要留紀錄
            if vid in pending:
                t0, q0 = pending[vid]
                turns.append((t0, vid, unit, q0, "(無回應)", ""))
            pending[vid] = (ts, q)
            continue
        m = RE_ANS.search(ln)
        if m:
            vid, view, ans = m.group(1), m.group(2), m.group(3).strip()
            if vid in pending:
                t0, q = pending.pop(vid)
                turns.append((t0, vid, unit, q, view, ans))
    for vid, (ts, q) in pending.items():
        turns.append((ts, vid, unit, q, "(無回應)", ""))
    return turns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="today")
    ap.add_argument("--port", type=int, choices=[8001, 8002])
    ap.add_argument("--format", choices=["convo", "tsv"], default="tsv")
    ap.add_argument("-o", "--out")
    ap.add_argument("--skip-junk", action="store_true",
                    help="濾掉亂打（rejected 且長度<3 或全同字元）")
    a = ap.parse_args()

    units = [UNIT[a.port]] if a.port else list(UNIT.values())
    turns = []
    for u in units:
        turns += parse(fetch(u, a.since), u)
    turns.sort(key=lambda t: t[0])

    if a.skip_junk:
        def junk(q, view):
            s = q.strip()
            return view == "rejected" and (len(s) < 3 or len(set(s)) <= 2)
        turns = [t for t in turns if not junk(t[3], t[4])]

    lines = []
    if a.format == "tsv":
        lines.append("時間\tvid\t語言\t訪客說\tview\t回答")
        for ts, vid, unit, q, view, ans in turns:
            lines.append(f"{ts}\t{vid}\t{LANG.get(unit, unit)}\t"
                         f"{q[:60]}\t{view}\t{ans[:70]}")
    else:
        # ws_convo.py 劇本格式：每位訪客一個 ### 區塊 = 一條連線
        by_key = defaultdict(list)
        for t in turns:
            by_key[(t[2], t[1])].append(t)     # (服務, vid)
        lines.append("# 由 dump_convo.py 從**真實訪客對話**產生（journald）")
        lines.append(f"# 來源：{', '.join(units)} · 期間：{a.since}")
        lines.append("# ⚠️ 第二欄是**當時實際的 view**，不是「應該的 view」——")
        lines.append("#    回放前請人工檢查，把答錯的改成正確期望值再當回歸批用。")
        lines.append("# ⚠️ 中英文各自要用對應的 port 回放：")
        lines.append("#    ws_convo.py --file <本檔> --rpi5   （工具內寫死 8002）")
        for (unit, vid), rows in by_key.items():
            lines.append(f"\n### {LANG.get(unit, unit)} 訪客 vid={vid}"
                         f"（{rows[0][0]}）")
            for ts, _v, _u, q, view, _a in rows:
                q1 = q.replace("|", "／").replace("\n", " ")[:120]
                lines.append(f"> {q1} | {view}")

    text = "\n".join(lines) + "\n"
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        n_vid = len({(t[2], t[1]) for t in turns})
        print(f"{len(turns)} 輪對話 / {n_vid} 位訪客 → {a.out}")
    else:
        sys.stdout.write(text)


main()
