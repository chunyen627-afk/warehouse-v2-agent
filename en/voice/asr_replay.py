#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""asr_replay.py — 把三層跑批**實際產生的 ASR 錯法**全部重放一次。

為什麼這批素材最有價值：
  它們是系統**真實會遇到的輸入**（whisper 對 user 真人聲的實際輸出），
  不是我憑空想的變體。含各種怪東西：
    自動加的引號 `anything below "safety stark"`
    句首編號     `13. yoga mats from south to central`
    整詞聽錯     `laptop pack` / `facial tensile` / `electric monk's`
    語意翻轉     `set an error for yoga med`（alert→error）
  ⇒ 用它們當語料，等於免費得到一批「展場真的會發生」的測試句。

判準：拿**原句在語料中的期望 view**（read100_en.txt 第 3 欄）比對。
  這裡不要求 ASR 字面正確——只問「容錯層能不能把它救回正確意圖」。
  ⇒ 通過＝訪客講錯／被聽錯，系統仍答對；未過＝真的答錯。

用法（RPI5 ~/voice_poc）：
  python3 asr_replay.py            # 全部
  python3 asr_replay.py --fail     # 只印未過的（找可修的規律）
"""
import asyncio
import json
import ssl
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CORPUS = BASE / "read100_en.txt"
SRCS = ["_rerun_en_v2.txt", "_rerun_en_v2_light.txt", "_rerun_en_v2_heavy.txt"]


def load_expect():
    exp = {}
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        p = line.split("|")
        if len(p) >= 3 and p[0].isdigit():
            exp[int(p[0])] = (p[1], p[2], p[3] if len(p) > 3 else "")
    return exp


def load_variants():
    """收集所有「ASR 輸出 != 原句」的實際錯法（去重）。"""
    seen = {}
    for name in SRCS:
        f = BASE / name
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            p = line.split("|")
            if len(p) < 5 or not p[0].isdigit():
                continue
            n, sent, asr = int(p[0]), p[1], p[2]
            if asr and asr != sent and asr != "ASR空":
                seen.setdefault(asr, (n, sent))
    return seen


_FIX_CACHE = {}


def preload_asr_fix(texts):
    """一次性批次套用 `_ASR_FIX_EN`（`/api/asr` 出口的正規化）。

    ⚠️ **這一步不能省**：`_asr_normalize` 只掛在 `/api/asr`，
    走 WebSocket 送純文字**不會**經過它 → 少了這步，重放測到的是
    「打字路徑」而非「語音路徑」，ASR 規則的效果完全反映不出來
    （實測：加了 7 條規則後救回率仍停在 54%，就是踩到這個）。
    ⚠️ 用**批次**不要逐句呼叫——server.py 很大，每句 import 一次會慢到不能用。
    """
    import subprocess
    payload = json.dumps(list(texts), ensure_ascii=False)
    code = (
        "import sys,os,json;"
        "os.chdir(os.path.expanduser('~/warehouse_v2_en'));"
        "sys.path.insert(0,'.');"
        "import server;"
        "print(json.dumps({t: server._asr_normalize(t) "
        "for t in json.loads(sys.argv[1])}, ensure_ascii=False))"
    )
    try:
        r = subprocess.run(["python3", "-c", code, payload],
                           capture_output=True, text=True, timeout=300)
        line = [l for l in (r.stdout or "").splitlines() if l.startswith("{")]
        if line:
            _FIX_CACHE.update(json.loads(line[-1]))
    except Exception as e:
        print(f"(ASR 正規化預載失敗，改用原文: {e})", file=sys.stderr)


def apply_asr_fix(text):
    return _FIX_CACHE.get(text, text)


async def ask(text):
    import websockets
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    async with websockets.connect("wss://localhost:8002/ws?fast=1", ssl=ctx) as ws:
        await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))
        while True:
            o = json.loads(await asyncio.wait_for(ws.recv(), 90))
            if o.get("type") == "done":
                r = o.get("result") or {}
                return r.get("view") or "", (r.get("summary") or "").replace("\n", " ")


def main():
    only_fail = "--fail" in sys.argv
    exp = load_expect()
    variants = load_variants()

    preload_asr_fix(variants.keys())

    ok = bad = 0
    fails = []
    for asr, (n, sent) in sorted(variants.items(), key=lambda x: x[1][0]):
        if n not in exp:
            continue
        _, want, kw = exp[n]
        fixed = apply_asr_fix(asr)
        view, summ = asyncio.run(ask(fixed))
        hit = True
        if want == "*":
            if view == "error" or not view:
                hit = False
        elif want not in view:
            hit = False
        if hit and kw and kw not in summ:
            hit = False

        if hit:
            ok += 1
            if not only_fail:
                print(f"[{n}] ✅ {asr[:52]:54} → {view}")
        else:
            bad += 1
            print(f"[{n}] ❌ {asr[:52]:54} → {view}（期望 {want}）")
            print(f"        原句：{sent}")
            fails.append((n, sent, asr, want, view))

    total = ok + bad
    print()
    print("=" * 70)
    print(f"真實 ASR 錯法 {total} 條：救回 {ok}、未過 {bad}"
          f"（救回率 {ok * 100 // total if total else 0}%）")
    print("=" * 70)


if __name__ == "__main__":
    main()
