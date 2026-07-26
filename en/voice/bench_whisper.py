# -*- coding: utf-8 -*-
"""bench_whisper.py — whisper.cpp 在 RPI5 的延遲 / WER 實測（英文版選型用）

背景：user 定調語音模型**只用歐美、拒絕大陸**（現行 Fun-ASR 是阿里的要換掉）。
換之前要有數據：延遲能不能撐展場、錯誤率夠不夠用。

⚠️ TTS 音檔是**下限估計**——合成音比真人清楚、無口音變異。中文版經驗：
   合成音 clean 100%、真人首測只有 35/52。所以 TTS 全過不代表可用，
   TTS 就掛才是真的不行。

用法（在 RPI5 上跑）：
    python3 bench_whisper.py                      # tiny.en + base.en，全腔調
    python3 bench_whisper.py --model base.en      # 只測一個
    python3 bench_whisper.py --level clean        # 只測乾淨層
"""
import subprocess, time, sys, io, re, json
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

WHISPER = Path.home() / "whisper.cpp"
CLI = WHISPER / "build" / "bin" / "whisper-cli"
# --dir 可指定音檔集（en=edge-tts / en_gcp=Chirp3-HD），方便兩批對比
_dir = sys.argv[sys.argv.index("--dir") + 1] if "--dir" in sys.argv else "en"
AUDIO = Path.home() / "voice_poc" / "audio" / _dir
MODELS = ["tiny.en", "base.en"]
LEVELS = ["clean", "light", "heavy"]


def norm(s: str) -> str:
    """比對前正規化：小寫、去標點、數字轉英文字（TTS 唸 fifty、ASR 可能吐 50）。"""
    s = s.lower().strip()
    _num = {"50": "fifty", "30": "thirty", "20": "twenty", "80": "eighty"}
    for k, v in _num.items():
        s = re.sub(rf"\b{k}\b", v, s)
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def wer(ref: str, hyp: str) -> float:
    """逐字錯誤率（Levenshtein on words）。"""
    r, h = norm(ref).split(), norm(hyp).split()
    if not r:
        return 0.0
    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        d[i][0] = i
    for j in range(len(h) + 1):
        d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i][j] = min(d[i-1][j] + 1, d[i][j-1] + 1,
                          d[i-1][j-1] + (r[i-1] != h[j-1]))
    return d[len(r)][len(h)] / len(r)


def transcribe(model: str, wav: Path):
    """回傳 (文字, 秒數)。"""
    t0 = time.perf_counter()
    p = subprocess.run(
        [str(CLI), "-m", str(WHISPER / "models" / f"ggml-{model}.bin"),
         "-f", str(wav), "-nt", "-l", "en", "-t", "4"],
        capture_output=True, text=True, timeout=180)
    el = time.perf_counter() - t0
    txt = " ".join(l.strip() for l in p.stdout.splitlines() if l.strip())
    return txt, el


def main():
    models = MODELS
    if "--model" in sys.argv:
        models = [sys.argv[sys.argv.index("--model") + 1]]
    levels = LEVELS
    if "--level" in sys.argv:
        levels = [sys.argv[sys.argv.index("--level") + 1]]

    expected = {}
    for line in (AUDIO / "expected.txt").read_text(encoding="utf-8").splitlines():
        if "|" in line:
            sid, txt = line.split("|", 1)
            expected[sid] = txt

    accents = sorted([d.name for d in AUDIO.iterdir() if d.is_dir()])
    results = []
    for model in models:
        print(f"\n{'='*72}\n模型 {model}\n{'='*72}")
        for level in levels:
            suf = "" if level == "clean" else f"_{level}"
            per_acc = {}
            for acc in accents:
                wers, times, bad = [], [], []
                for sid, ref in expected.items():
                    wav = AUDIO / acc / f"{sid}{suf}.wav"
                    if not wav.exists():
                        continue
                    hyp, el = transcribe(model, wav)
                    w = wer(ref, hyp)
                    wers.append(w); times.append(el)
                    if w > 0:
                        bad.append((sid, ref, hyp, w))
                if not wers:
                    continue
                aw = sum(wers) / len(wers)
                at = sum(times) / len(times)
                per_acc[acc] = (aw, at, len([x for x in wers if x == 0]), len(wers))
                results.append({"model": model, "level": level, "accent": acc,
                                "wer": aw, "sec": at,
                                "exact": len([x for x in wers if x == 0]),
                                "n": len(wers), "bad": bad[:5]})
            print(f"\n── {level} ──")
            for acc, (aw, at, ex, n) in per_acc.items():
                print(f"  {acc:12} WER {aw*100:5.1f}%   全對 {ex:2}/{n}   {at:.2f}s/句")
            if per_acc:
                mw = sum(v[0] for v in per_acc.values()) / len(per_acc)
                mt = sum(v[1] for v in per_acc.values()) / len(per_acc)
                print(f"  {'平均':12} WER {mw*100:5.1f}%              {mt:.2f}s/句")

    out = Path.home() / "voice_poc" / f"_bench_whisper_{_dir}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明細 → {out}")


main()
