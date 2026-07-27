# -*- coding: utf-8 -*-
"""中文 ASR 對照：Fun-ASR（現行，阿里）vs whisper tiny/small（OpenAI）。

用 user 錄的**真人**音檔（voice_poc/audio/user_clean/），不是合成音——
中文版經驗：合成音 clean 100%、真人首測只有 35/52，合成音會嚴重高估。

⚠️ 要 scp 上去執行（中文在 SSH heredoc 會被吃掉）。
"""
import re
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
WHISPER = HOME / "whisper.cpp/build/bin/whisper-cli"
FUNASR = HOME / "voice_poc/src/runtime/llama.cpp/build/bin/llama-funasr-cli"
ENC = HOME / "voice_poc/gguf/funasr-encoder-f16.gguf"
LLM = HOME / "voice_poc/gguf/qwen3-0.6b-q4km.gguf"
AUDIO = HOME / "voice_poc/audio/user_clean"

# read100.txt: 編號|句子|view|關鍵字
expect = {}
for ln in (HOME / "voice_poc/read100.txt").read_text(encoding="utf-8").splitlines():
    ln = ln.strip()
    if not ln or ln.startswith("#"):
        continue
    p = ln.split("|")
    if len(p) >= 2 and p[0].isdigit():
        expect[p[0]] = p[1].strip()

ids = [i for i in sys.argv[1:] if i in expect]
if not ids:
    ids = [str(i) for i in range(1, 21) if str(i) in expect]


def run(cmd, timeout=180):
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.stdout, round(time.time() - t0, 2)


def whisper(model, wav):
    out, dt = run([str(WHISPER), "-m", str(model), "-f", str(wav),
                   "-nt", "-l", "zh"])
    txt = " ".join(l.strip() for l in out.splitlines() if l.strip())
    return txt.strip(), dt


def funasr(wav):
    out, dt = run([str(FUNASR), "--enc", str(ENC), "-m", str(LLM),
                   "-a", str(wav)])
    txt = ""
    for l in reversed([x.strip() for x in out.splitlines() if x.strip()]):
        if re.search(r"[一-鿿]", l):
            txt = l
            break
    try:
        from opencc import OpenCC
        txt = OpenCC("s2twp").convert(txt)
    except Exception:
        pass
    return txt.strip(" 。，？！、.,?!~～"), dt


def norm(s):
    return re.sub(r"[\s。，？！、.,?!~～]", "", s)


rows = []
for i in ids:
    wav = AUDIO / f"{i}.wav"
    if not wav.exists():
        continue
    want = expect[i]
    r = {"id": i, "want": want}
    for name, fn in (("funasr", lambda: funasr(wav)),
                     ("tiny", lambda: whisper(HOME / "whisper.cpp/models/ggml-tiny.bin", wav)),
                     ("small", lambda: whisper(HOME / "whisper.cpp/models/ggml-small.bin", wav))):
        try:
            txt, dt = fn()
        except Exception as e:
            txt, dt = f"ERR {e}", 0
        r[name] = (txt, dt, norm(txt) == norm(want))
    rows.append(r)
    print(f"[{i}] 期望：{want}")
    for k in ("funasr", "tiny", "small"):
        t, d, ok = r[k]
        print(f"    {k:6} {'✅' if ok else '  '} {d:5.2f}s  {t}")

print("\n===== 統計 =====")
for k in ("funasr", "tiny", "small"):
    hit = sum(1 for r in rows if r[k][2])
    avg = sum(r[k][1] for r in rows) / max(len(rows), 1)
    print(f"{k:6} 完全正確 {hit}/{len(rows)}  平均 {avg:.2f}s")
