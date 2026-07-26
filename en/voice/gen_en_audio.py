# -*- coding: utf-8 -*-
"""gen_en_audio.py — 產生**英文版**倉管測試音檔（edge-tts）

為什麼要這支：英文版語音要換掉阿里的 Fun-ASR（user 定調只用歐美模型），
換之前要有評測集。user 目前不方便自己錄英文，先用 TTS 產生。

⚠️ TTS 音檔是**下限估計**：合成音比真人清楚、無口音變異、無環境噪音。
   實際展場表現一定更差，所以 TTS 全過不代表可用，TTS 就掛才是真的不行。
   （中文版的經驗：合成音 clean 100%、真人首測只有 35/52）

腔調選擇（展場外國訪客不會只有美式）：
    en-US 美式 / en-GB 英式 / en-AU 澳洲 / en-IN 印度 / en-SG 新加坡
    完整清單 47 個語音、14 地區，用 --list 看

用法：
    python gen_en_audio.py                 # 預設 5 腔調 × 20 句
    python gen_en_audio.py --list          # 列出所有英文語音
    python gen_en_audio.py --voice en-GB-RyanNeural   # 只產一個腔調
"""
import asyncio, sys, io, os
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import edge_tts

OUT = Path(__file__).parent / "audio" / "en"

# 五個代表腔調（展場常見）——男女混，因為音高影響 ASR
VOICES = [
    ("en-US-AndrewNeural",  "US-male"),
    ("en-GB-SoniaNeural",   "GB-female"),
    ("en-AU-NatashaNeural", "AU-female"),
    ("en-IN-PrabhatNeural", "IN-male"),
    ("en-SG-WayneNeural",   "SG-male"),
]

# 20 句：涵蓋倉管主要意圖 + 商品名（ASR 最容易錯的地方）
SENTENCES = [
    # 基本查詢（商品名是重點）
    ("q01", "how many bluetooth earphones are left"),
    ("q02", "whats the stock of yoga mat"),
    ("q03", "wireless mouse count"),
    ("q04", "power bank inventory"),
    ("q05", "do we have any camping tent"),
    # 缺貨 / 到期 / 熱銷
    ("q06", "which items are running low"),
    ("q07", "what is expiring soon"),
    ("q08", "best sellers this month"),
    # 倉別 / 比較
    ("q09", "compare north and south warehouse"),
    ("q10", "how much stock is in central warehouse"),
    # 寫入（最關鍵——聽錯會寫錯資料）
    ("w01", "north received fifty wireless mouse"),
    ("w02", "south shipped thirty yoga mat"),
    ("w03", "transfer twenty bluetooth earphones from north to central"),
    ("w04", "set safety stock for yoga mat to eighty"),
    # 連帶 / RCA
    ("q11", "what else do power bank buyers get"),
    ("q12", "why is the toothbrush count off"),
    # 追問（短句，ASR 最容易吃掉）
    ("f01", "north"),
    ("f02", "and central"),
    ("f03", "the most urgent one"),
    ("f04", "make it twenty instead"),
]


# 噪音層（與中文版 noise_retest.sh / read100.sh **完全相同的參數**，
#   這樣中英數據才可比）：light=一般展場 -18dB、heavy=尖峰吵雜 -8dB
NOISE = Path(__file__).parent / "noise" / "mall_ambience.mp3"
NOISE_LEVELS = {"light": -18, "heavy": -8}


def _mix_noise(wav: Path, level: str) -> Path:
    """把賣場人潮背景混進乾淨音檔（含 aecho 空間感，同中文版參數）。"""
    db = NOISE_LEVELS[level]
    out = wav.with_name(f"{wav.stem}_{level}.wav")
    dur = os.popen(
        f'ffprobe -v error -show_entries format=duration -of csv=p=0 "{wav}"'
    ).read().strip().split(".")[0] or "4"
    os.system(
        f'ffmpeg -y -loglevel error -i "{wav}" -i "{NOISE}" -filter_complex '
        f'"[1:a]atrim=0:{dur},volume={db}dB,aresample=16000[bg];'
        f'[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0,'
        f'aecho=0.8:0.7:6:0.15,aresample=16000" '
        f'-ac 1 -c:a pcm_s16le "{out}"'
    )
    return out


async def gen_one(voice, tag, sid, text, noise=()):
    d = OUT / tag
    d.mkdir(parents=True, exist_ok=True)
    mp3 = d / f"{sid}.mp3"
    wav = d / f"{sid}.wav"
    await edge_tts.Communicate(text, voice).save(str(mp3))
    # ASR 吃 16k mono PCM16
    os.system(f'ffmpeg -y -loglevel error -i "{mp3}" -ar 16000 -ac 1 -c:a pcm_s16le "{wav}"')
    mp3.unlink(missing_ok=True)
    for lv in noise:
        _mix_noise(wav, lv)
    return wav


async def main():
    if "--list" in sys.argv:
        vs = await edge_tts.list_voices()
        en = sorted([v for v in vs if v["Locale"].startswith("en-")],
                    key=lambda v: (v["Locale"], v["ShortName"]))
        print(f"英文語音 {len(en)} 個：")
        for v in en:
            print(f"  {v['ShortName']:34} {v['Gender']:6} {v['Locale']}")
        return

    voices = VOICES
    if "--voice" in sys.argv:
        vn = sys.argv[sys.argv.index("--voice") + 1]
        voices = [(vn, vn.split("-")[1] + "-custom")]

    # --noise：一併產混噪版（light/heavy），與中文版同參數
    noise = ()
    if "--noise" in sys.argv:
        noise = ("light", "heavy")
        if not NOISE.exists():
            print(f"⚠️ 找不到噪音素材 {NOISE}，跳過混噪")
            noise = ()

    total = 0
    for voice, tag in voices:
        print(f"\n▶ {tag}  ({voice})")
        for sid, text in SENTENCES:
            try:
                w = await gen_one(voice, tag, sid, text, noise)
                total += 1
                _n = f" +{'/'.join(noise)}" if noise else ""
                print(f"   {sid} {text[:40]:42} → {w.name}{_n}")
            except Exception as e:
                print(f"   ✗ {sid} {e}")
    print(f"\n共 {total} 檔 → {OUT}")
    # 期望文字表（給 WER 計算用）
    ans = OUT / "expected.txt"
    ans.write_text("\n".join(f"{s}|{t}" for s, t in SENTENCES), encoding="utf-8")
    print(f"期望文字 → {ans}")


asyncio.run(main())
