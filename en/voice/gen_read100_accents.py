# -*- coding: utf-8 -*-
"""gen_read100_accents.py — 產 read100_en 的**多腔調**音檔（edge-tts）

為什麼：展場訪客不會只有一種腔。既有的 audio/en/ 只有 20 句舊語料，
read100 這 100 句涵蓋寫入 / RCA / 多輪等各類，值得跑多腔調端到端。

⚠️ 與 gen_read100_demo.py 的差別：那支是**給 user 聽的範讀**（美式、放慢），
   這支是**評測素材**（多腔調、正常語速）。

輸出：audio/read100_en_<TAG>/001.mp3 ~ 100.mp3

用法：
    python gen_read100_accents.py            # 產四個腔調
    python gen_read100_accents.py GB-male    # 只產一個
"""
import asyncio
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import edge_tts

HERE = Path(__file__).parent
CORPUS = HERE / "read100_en.txt"

# 四個非美式腔調（US 已有範讀那批可用）——男女混，音高會影響 ASR
VOICES = [
    ("en-GB-RyanNeural",    "GB-male"),
    ("en-AU-NatashaNeural", "AU-female"),
    ("en-IN-PrabhatNeural", "IN-male"),
    ("en-SG-WayneNeural",   "SG-male"),
]


def load():
    rows = []
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        p = t.split("|")
        if len(p) >= 2 and p[0].strip().isdigit():
            rows.append((int(p[0]), p[1].strip()))
    return rows


async def main():
    want = sys.argv[1] if len(sys.argv) > 1 else None
    rows = load()
    if not rows:
        print("讀不到 read100_en.txt")
        return
    for sn, tag in VOICES:
        if want and tag != want:
            continue
        out = HERE / "audio" / f"read100_en_{tag}"
        out.mkdir(parents=True, exist_ok=True)
        ok = 0
        for num, sent in rows:
            p = out / f"{num:03d}.mp3"
            try:
                await edge_tts.Communicate(sent, sn).save(str(p))
                ok += 1
            except Exception as e:
                print(f"  ✗ [{tag} {num}] {e}")
        print(f"{tag:12s} {ok}/{len(rows)} → {out.name}")


asyncio.run(main())
