#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""session_en.py — 用**同一條 WebSocket 連線**連續送多句，驗證多輪脈絡。

為什麼需要：`read100_en.sh` / `rerun_en_v2.sh` **每句開一條新連線**，
  代稱句（its / what about north / and south）拿不到前句脈絡 → 必然失敗。
  第 77/78/82/83 句的 FAIL 全是這個原因造成的**測試架構假象**，
  ASR 一字不差（見 _rerun_en_v2.txt 第 4 欄）。
  中文版當年踩過同一個坑（read100.sh 第 80/90/92 句）。

用法（RPI5 ~/voice_poc）：
  python3 session_en.py 76 84          # 用 ASR 實際辨識文字（預設，最真實）
  python3 session_en.py 76 84 --ideal  # 用語料原句（排除 ASR 因素，只驗脈絡）

判定與 read100_en.sh 一致：view 比對 + summary 關鍵字。
"""
import asyncio
import json
import re
import ssl
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CORPUS = BASE / "read100_en.txt"
RESULT = BASE / "_rerun_en_v2.txt"   # 取 ASR 實際辨識文字用


def load_corpus(lo, hi):
    rows = []
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        n = int(parts[0])
        if lo <= n <= hi:
            rows.append({
                "n": n,
                "sent": parts[1],
                "want": parts[2],
                "kw": parts[3] if len(parts) > 3 else "",
            })
    return rows


def load_asr_text():
    """從 rerun 結果檔取每句的 ASR 實際辨識文字。"""
    out = {}
    if RESULT.exists():
        for line in RESULT.read_text(encoding="utf-8").splitlines():
            p = line.split("|")
            if len(p) >= 3 and p[0].isdigit():
                out[int(p[0])] = p[2]
    return out


async def run(rows, use_ideal):
    import websockets
    asr = {} if use_ideal else load_asr_text()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    ok = bad = 0
    # ⚠️ 關鍵：**整批共用一條連線**＝模擬同一位訪客連續發問
    async with websockets.connect("wss://localhost:8002/ws?fast=1", ssl=ctx) as ws:
        for r in rows:
            text = asr.get(r["n"], r["sent"]) if not use_ideal else r["sent"]
            if not text or text == "ASR空":
                text = r["sent"]
            await ws.send(json.dumps({"type": "chat", "text": text},
                                     ensure_ascii=False))
            view = summary = ""
            while True:
                o = json.loads(await asyncio.wait_for(ws.recv(), 90))
                if o.get("type") == "done":
                    res = o.get("result") or {}
                    view = res.get("view") or ""
                    summary = (res.get("summary") or "").replace("\n", " ")
                    break

            hit = True
            if r["want"] == "*":
                if view == "error" or not view:
                    hit = False
            elif r["want"] not in view:
                hit = False
            if hit and r["kw"] and r["kw"] not in summary:
                hit = False

            mark = "" if text == r["sent"] else f"  [送入：{text}]"
            if hit:
                ok += 1
                print(f"[{r['n']}] ✅ {view}{mark}")
            else:
                bad += 1
                print(f"[{r['n']}] ❌ {view}（期望 {r['want']}）{mark}")
                print(f"        回答：{summary[:70]}")

    total = ok + bad
    print()
    print("=" * 46)
    mode = "語料原句(排除 ASR)" if use_ideal else "ASR 實際辨識"
    print(f"同連線多輪｜{mode}｜{total} 句：通過 {ok}、未過 {bad}")
    if total:
        print(f"通過率 {ok * 100 // total}%")
    print("=" * 46)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_ideal = "--ideal" in sys.argv
    lo = int(args[0]) if args else 76
    hi = int(args[1]) if len(args) > 1 else 84
    rows = load_corpus(lo, hi)
    if not rows:
        print("找不到語料", file=sys.stderr)
        sys.exit(1)
    asyncio.run(run(rows, use_ideal))


if __name__ == "__main__":
    main()
