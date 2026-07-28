# -*- coding: utf-8 -*-
"""gen_read100_demo.py — 產 read100_en.txt 的**示範朗讀音檔**（edge-tts）

用途：user 要在吵雜環境錄英文真人音，但不確定某些句子怎麼唸。
      先聽示範再唸，避免「看拼字唸錯詞」變成測試雜訊。

⚠️ 這批音檔**不是評測素材**——評測要用 user 的真人聲。
   示範只解決「唸什麼字」，**不要求模仿腔調**：
   展場訪客本來就不是母語者，非母語腔正是要測的真實情境。

輸出：audio/read100_en_demo/001.mp3 ~ 100.mp3
      外加 _all.mp3（全部串起來，可整段播放跟著唸）

用法：
    python gen_read100_demo.py              # 產 100 句
    python gen_read100_demo.py --voice en-GB-RyanNeural
    python gen_read100_demo.py --rate -10%  # 放慢（預設 -8%，比正常稍慢好跟讀）
"""
import asyncio
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import edge_tts

HERE = Path(__file__).parent
CORPUS = HERE / "read100_en.txt"
OUT = HERE / "audio" / "read100_en_demo"

# 預設用美式男聲：咬字清楚、語速穩，適合當範讀。
#   （評測用的多腔調在 gen_en_audio.py，那是另一件事）
VOICE = "en-US-AndrewNeural"
RATE = "-8%"   # 稍慢，方便跟讀


def load_sentences():
    rows = []
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 2:
            continue
        num, sent = parts[0].strip(), parts[1].strip()
        if num.isdigit() and sent:
            rows.append((int(num), sent))
    return rows


async def synth(text, path, voice, rate):
    tts = edge_tts.Communicate(text, voice, rate=rate)
    await tts.save(str(path))


async def main():
    voice, rate = VOICE, RATE
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--voice" and i + 1 < len(args):
            voice = args[i + 1]
        elif a == "--rate" and i + 1 < len(args):
            rate = args[i + 1]

    rows = load_sentences()
    if not rows:
        print("讀不到語料，檢查 read100_en.txt")
        return
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"語音：{voice}｜語速：{rate}｜共 {len(rows)} 句")
    print(f"輸出：{OUT}")

    ok = 0
    for num, sent in rows:
        p = OUT / f"{num:03d}.mp3"
        try:
            await synth(sent, p, voice, rate)
            ok += 1
            if num % 10 == 0:
                print(f"  ... {num}/{len(rows)}")
        except Exception as e:
            print(f"  ✗ [{num}] {sent!r}: {e}")
    print(f"完成 {ok}/{len(rows)} 句")

    # 串成一個檔，方便整段播放跟讀（每句之間留空檔）
    #   用 ffmpeg concat；沒有 ffmpeg 就跳過，單句檔已經夠用
    try:
        import subprocess
        sil = OUT / "_sil.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-t", "2.2", "-i", "anullsrc=r=24000:cl=mono", str(sil)],
            check=True)
        lst = OUT / "_list.txt"
        with open(lst, "w", encoding="utf-8") as f:
            for num, _ in rows:
                f.write(f"file '{num:03d}.mp3'\n")
                f.write(f"file '_sil.mp3'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat",
             "-safe", "0", "-i", str(lst), "-c", "copy", str(OUT / "_all.mp3")],
            check=True, cwd=str(OUT))
        sil.unlink(missing_ok=True)
        lst.unlink(missing_ok=True)
        print(f"整段跟讀檔：{OUT / '_all.mp3'}")
    except Exception as e:
        print(f"（整段檔略過：{e}）單句檔已可用")


asyncio.run(main())
