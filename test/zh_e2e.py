# -*- coding: utf-8 -*-
"""中文語音端到端：真人音檔 → /api/asr（whisper small）→ 倉管系統 → 判定。

⚠️ 判準是**端到端答對率**不是 WER——既有教訓：
   Fun-ASR 字面率較高但 whisper 聽錯的都被文字端容錯層救回。

⚠️ 要 scp 上去執行（中文在 SSH heredoc 會被吃掉）。
用法：python3 zh_e2e.py [起始編號] [結束編號]
"""
import asyncio
import json
import ssl
import subprocess
import sys
import time
from pathlib import Path

import websockets

HOME = Path.home()
AUDIO = HOME / "voice_poc/audio/user_clean"
BASE = "https://localhost:8001"
WS = "wss://localhost:8001/ws?fast=1"

lo = int(sys.argv[1]) if len(sys.argv) > 1 else 1
hi = int(sys.argv[2]) if len(sys.argv) > 2 else 20

# read100.txt: 編號|句子|期望view|必含關鍵字
cases = {}
for ln in (HOME / "voice_poc/read100.txt").read_text(encoding="utf-8").splitlines():
    ln = ln.strip()
    if not ln or ln.startswith("#"):
        continue
    p = [x.strip() for x in ln.split("|")]
    if len(p) >= 3 and p[0].isdigit():
        cases[int(p[0])] = (p[1], p[2], p[3] if len(p) > 3 else "")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def asr(wav):
    t0 = time.time()
    out = subprocess.run(
        ["curl", "-sk", "-X", "POST", f"{BASE}/api/asr",
         "--data-binary", f"@{wav}", "-H", "Content-Type: application/octet-stream",
         "-m", "150"],
        capture_output=True, text=True).stdout
    try:
        d = json.loads(out)
    except Exception:
        return "", 0
    return d.get("text", ""), round(time.time() - t0, 1)


async def ask(text):
    async with websockets.connect(WS, ssl=ctx, max_size=None) as ws:
        await ws.send(json.dumps({"type": "chat", "text": text},
                                 ensure_ascii=False))
        toks = []
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
            if m.get("type") == "token":
                toks.append(m.get("text", ""))
            elif m.get("type") == "error":
                return "error", m.get("text", "")
            elif m.get("type") == "done":
                r = m.get("result") or {}
                return r.get("view", "?"), (r.get("summary") or "".join(toks))


async def main():
    ok = miss = 0
    for i in range(lo, hi + 1):
        wav = AUDIO / f"{i}.wav"
        if not wav.exists() or i not in cases:
            continue
        want_txt, want_view, want_kw = cases[i]
        heard, dt = asr(wav)
        if not heard:
            print(f"[{i:3}] ASR 失敗 | 原句：{want_txt}")
            miss += 1
            continue
        view, ans = await ask(heard)
        view_ok = view in [v.strip() for v in want_view.split(",")]
        kw_ok = (not want_kw) or (want_kw in ans)
        good = view_ok and kw_ok
        ok += good
        miss += not good
        mark = "✅" if good else "❌"
        print(f"[{i:3}] {mark} {dt:4.1f}s  聽成：{heard}")
        if not good:
            print(f"        原句：{want_txt}")
            print(f"        view={view}（期望 {want_view}）"
                  f"{'' if kw_ok else ' / 缺關鍵字「%s」' % want_kw}")
            print(f"        答：{ans[:90]}")
    print(f"\n===== 端到端 {ok}/{ok + miss} =====")


asyncio.run(main())
