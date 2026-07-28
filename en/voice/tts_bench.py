# -*- coding: utf-8 -*-
"""tts_bench.py — 拿 read100_en 的**示範音檔**跑完整語音鏈，當真人錄音的基準線。

⚠️ 這是 TTS 合成音，是**下限估計**：合成音比真人清楚、無口音變異。
   中文版經驗：合成音 clean 100% vs 真人首測 35/52。
   TTS 全過不代表展場可用；TTS 就掛才是真的不行。

用法（RPI5 ~/voice_poc）：
    python3 tts_bench.py              乾淨
    python3 tts_bench.py light        混賣場噪音 -18dB
    python3 tts_bench.py heavy        混賣場噪音 -8dB
"""
import asyncio, json, os, ssl, subprocess, sys, tempfile, time
from pathlib import Path
import urllib.request

import websockets

HERE = Path(__file__).parent
# r13：支援多腔調——第 2 參數指定 tag（demo/GB-male/AU-female/IN-male/SG-male）
ACC = sys.argv[2] if len(sys.argv) > 2 else "demo"
AUD = HERE / "audio" / f"read100_en_{ACC}"
NOISE = HERE / "noise" / "mall_ambience.mp3"
CORPUS = HERE / "read100_en.txt"
LV = sys.argv[1] if len(sys.argv) > 1 else ""
DB = {"heavy": "-8", "light": "-18"}.get(LV)
OUT = HERE / f"_tts_bench_{ACC}_{LV or 'clean'}.txt"

rows = []
for line in CORPUS.read_text(encoding="utf-8").splitlines():
    t = line.strip()
    if not t or t.startswith("#"):
        continue
    p = t.split("|")
    if len(p) >= 3 and p[0].strip().isdigit():
        rows.append((int(p[0]), p[1].strip(), p[2].strip(),
                     p[3].strip() if len(p) > 3 else ""))

def asr(wav):
    """送 /api/asr → 回辨識文字"""
    req = urllib.request.Request(
        "https://127.0.0.1:8002/api/asr", data=wav.read_bytes(),
        headers={"Content-Type": "application/octet-stream"}, method="POST")
    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=180, context=ctx) as r:
        d = json.loads(r.read())
    return d.get("text", "") if d.get("ok") else ""

def prep(num):
    """mp3 → 16k wav（要混噪就混）"""
    src = AUD / f"{num:03d}.mp3"
    out = Path(tempfile.mktemp(suffix=".wav"))
    if DB:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
               "-i", str(NOISE), "-filter_complex",
               f"[1:a]atrim=0:12,volume={DB}dB,aresample=16000[bg];"
               f"[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0,"
               f"aresample=16000", "-ac", "1", "-ar", "16000", str(out)]
    else:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
               "-ar", "16000", "-ac", "1", str(out)]
    subprocess.run(cmd, check=True)
    return out

async def main():
    c = ssl.create_default_context(); c.check_hostname=False; c.verify_mode=ssl.CERT_NONE
    ok = bad = 0
    lines = []
    async with websockets.connect("wss://localhost:8002/ws?fast=1", ssl=c,
                                  max_size=None) as ws:
        for num, sent, want, kw in rows:
            w = prep(num)
            try:
                heard = asr(w)
            except Exception as e:
                heard = ""
            finally:
                w.unlink(missing_ok=True)
            if not heard:
                bad += 1
                lines.append(f"{num}|{sent}|(ASR空)||FAIL")
                print(f"[{num:3d}] ❌ ASR 無輸出 | {sent}")
                continue
            await ws.send(json.dumps({"type": "chat", "text": heard},
                                     ensure_ascii=False))
            view = summ = ""
            while True:
                o = json.loads(await asyncio.wait_for(ws.recv(), 90))
                if o.get("type") == "done":
                    r = o.get("result") or {}
                    view = r.get("view") or ""
                    summ = (r.get("summary") or "").replace("\n", " ")
                    break
            hit = True
            if want == "*":
                if view == "error" or not view: hit = False
            elif want not in view:
                hit = False
            if hit and kw and kw.lower() not in summ.lower():
                hit = False
            mark = "" if heard.strip().lower() == sent.lower() else f"  [聽成: {heard}]"
            if hit:
                ok += 1
                lines.append(f"{num}|{sent}|{heard}|{view}|PASS")
                print(f"[{num:3d}] ✅ {view}{mark}")
            else:
                bad += 1
                lines.append(f"{num}|{sent}|{heard}|{view}|FAIL")
                print(f"[{num:3d}] ❌ {view}（期望 {want}）{mark}")
                print(f"        {summ[:70]}")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    n = ok + bad
    print(f"\n{'='*58}")
    print(f"TTS 基準（{ACC} / {LV or 'clean'}）：PASS {ok} / FAIL {bad}"
          f"　通過率 {ok/n*100:.0f}%" if n else "")
    print(f"結果：{OUT}")

asyncio.run(main())
