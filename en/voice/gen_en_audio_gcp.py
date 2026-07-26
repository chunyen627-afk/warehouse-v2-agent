# -*- coding: utf-8 -*-
"""gen_en_audio_gcp.py — 用 Google Chirp 3 HD 產英文測試音檔（對比 edge-tts）

為什麼要這批：edge-tts 那批已測出 whisper tiny.en 0.94s / WER 9.3%。
Chirp 3 HD 是目前最擬真的 TTS（LLM-based，2025），用來回答一個問題：
**更自然的語調會不會讓 ASR 更準？**（韻律更接近訓練資料，理論上會）

⚠️ 但擬真 TTS 仍是**下限估計**——咬字比真人清楚穩定。中文版經驗：
   合成音 clean 100%、真人首測 35/52。TTS 全過不代表展場可用。

計費：Chirp3-HD 屬 HD 類，免費額度 100 萬 byte/月。
   本腳本 20 句 × 4 腔調 ≈ 3,400 byte ＝ 額度的 0.34%，跑 50 輪也免費。

金鑰：voice_poc/gcp-tts-key.json（已在 .gitignore，不進 git）

用法：
    python gen_en_audio_gcp.py            # 4 腔調 × 20 句 + 混噪
    python gen_en_audio_gcp.py --no-noise # 只產乾淨層
    python gen_en_audio_gcp.py --list     # 列出所有 Chirp3-HD 英文語音
"""
import os, sys, io
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

HERE = Path(__file__).parent
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", str(HERE / "gcp-tts-key.json"))
from google.cloud import texttospeech as tts

OUT = HERE / "audio" / "en_gcp"
NOISE = HERE / "noise" / "mall_ambience.mp3"
NOISE_LEVELS = {"light": -18, "heavy": -8}

# 四個腔調，男女混（音高影響 ASR）。Chirp3-HD 沒有 SG 腔——
#   edge-tts 那批 SG 表現最差(23% WER)，兩邊剛好互補
VOICES = [
    ("en-US", "en-US-Chirp3-HD-Charon",     "US-male"),
    ("en-GB", "en-GB-Chirp3-HD-Aoede",      "GB-female"),
    ("en-AU", "en-AU-Chirp3-HD-Puck",       "AU-male"),
    ("en-IN", "en-IN-Chirp3-HD-Leda",       "IN-female"),
]

# 與 gen_en_audio.py **完全相同的 20 句**，數據才可比
SENTENCES = [
    ("q01", "how many bluetooth earphones are left"),
    ("q02", "whats the stock of yoga mat"),
    ("q03", "wireless mouse count"),
    ("q04", "power bank inventory"),
    ("q05", "do we have any camping tent"),
    ("q06", "which items are running low"),
    ("q07", "what is expiring soon"),
    ("q08", "best sellers this month"),
    ("q09", "compare north and south warehouse"),
    ("q10", "how much stock is in central warehouse"),
    ("w01", "north received fifty wireless mouse"),
    ("w02", "south shipped thirty yoga mat"),
    ("w03", "transfer twenty bluetooth earphones from north to central"),
    ("w04", "set safety stock for yoga mat to eighty"),
    ("q11", "what else do power bank buyers get"),
    ("q12", "why is the toothbrush count off"),
    ("f01", "north"),
    ("f02", "and central"),
    ("f03", "the most urgent one"),
    ("f04", "make it twenty instead"),
]


def mix_noise(wav: Path, level: str):
    db = NOISE_LEVELS[level]
    out = wav.with_name(f"{wav.stem}_{level}.wav")
    dur = os.popen(
        f'ffprobe -v error -show_entries format=duration -of csv=p=0 "{wav}"'
    ).read().strip().split(".")[0] or "4"
    os.system(
        f'ffmpeg -y -loglevel error -i "{wav}" -i "{NOISE}" -filter_complex '
        f'"[1:a]atrim=0:{dur},volume={db}dB,aresample=16000[bg];'
        f'[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0,'
        f'aecho=0.8:0.7:6:0.15,aresample=16000" -ac 1 -c:a pcm_s16le "{out}"')


def main():
    client = tts.TextToSpeechClient()

    if "--list" in sys.argv:
        vs = client.list_voices()
        for v in sorted(vs.voices, key=lambda x: x.name):
            if any(lc.startswith("en-") for lc in v.language_codes) and "Chirp3-HD" in v.name:
                g = {1: "M", 2: "F"}.get(v.ssml_gender, "?")
                print(f"  {v.name:32} {g}")
        return

    noise = () if "--no-noise" in sys.argv else ("light", "heavy")
    if noise and not NOISE.exists():
        print(f"⚠️ 無噪音素材 {NOISE}，只產乾淨層")
        noise = ()

    # Chirp3-HD 只支援 LINEAR16／MP3，直接要 16k LINEAR16 省一次轉檔
    cfg = tts.AudioConfig(audio_encoding=tts.AudioEncoding.LINEAR16,
                          sample_rate_hertz=16000)
    total = chars = 0
    for lang, voice, tag in VOICES:
        d = OUT / tag
        d.mkdir(parents=True, exist_ok=True)
        print(f"\n▶ {tag}  ({voice})")
        for sid, text in SENTENCES:
            try:
                resp = client.synthesize_speech(
                    input=tts.SynthesisInput(text=text),
                    voice=tts.VoiceSelectionParams(language_code=lang, name=voice),
                    audio_config=cfg)
                wav = d / f"{sid}.wav"
                wav.write_bytes(resp.audio_content)
                for lv in noise:
                    mix_noise(wav, lv)
                total += 1
                chars += len(text)
                print(f"   {sid} {text[:40]:42} → {wav.name}")
            except Exception as e:
                print(f"   ✗ {sid} {type(e).__name__}: {str(e)[:120]}")

    (OUT / "expected.txt").write_text(
        "\n".join(f"{s}|{t}" for s, t in SENTENCES), encoding="utf-8")
    print(f"\n共 {total} 檔（+混噪 ×{len(noise)}） → {OUT}")
    print(f"用量 {chars} bytes ＝ 免費額度(1M/月) 的 {chars/10000:.2f}%")


main()
