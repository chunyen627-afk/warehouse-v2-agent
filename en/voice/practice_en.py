# -*- coding: utf-8 -*-
"""practice_en.py — 本機練唸 100 句英文（不錄音、不判定，純跟讀）

用途：正式在 RPI5 錄之前先熟悉句子，避免「看拼字唸錯詞」浪費錄音次數。
      按 Enter 播放示範 → 你跟著唸 → 再 Enter 下一句。

用法：
    python practice_en.py            從第 1 句
    python practice_en.py 30         從第 30 句開始
    python practice_en.py 21 40      只練 21-40 句
    python practice_en.py --list     只印出全部句子（不播音）

操作：
    Enter      播放示範 → 換下一句
    r + Enter  重播這句
    s + Enter  跳過
    q + Enter  結束
"""
import io
import subprocess
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

HERE = Path(__file__).parent
CORPUS = HERE / "read100_en.txt"
AUDIO = HERE / "audio" / "read100_en_demo"

SECTION_HINT = {
    1: "A. 基本庫存查詢（最高頻，展場訪客最常問）",
    21: "B. 進出貨寫入（最重要——會真的改資料）",
    41: "C. 缺貨與警示",
    51: "D. 排行與比較",
    66: "E. 帳務追查 RCA / 報表",
    76: "F. 多輪追問（要連續唸才有意義，不要跳著唸）",
    86: "G. 澄清與口語確認",
    94: "H. 招呼閒聊與邊界",
}


def load():
    """回 (編號, 英文句, 中文意思)。中文是第 5 欄，只給人看。"""
    rows = []
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = line.split("|")
        if len(p) >= 2 and p[0].strip().isdigit():
            zh = p[4].strip() if len(p) >= 5 else ""
            rows.append((int(p[0]), p[1].strip(), zh))
    return rows


def play(num):
    """用系統預設播放器播 mp3（Windows 用 PowerShell 的 MediaPlayer，等它播完）"""
    f = AUDIO / f"{num:03d}.mp3"
    if not f.exists():
        print(f"   （沒有示範音檔 {f.name}）")
        return
    ps = (
        f"$p = New-Object -ComObject WMPlayer.OCX;"
        f"$m = $p.newMedia('{f}');"
        f"$p.currentPlaylist.appendItem($m);"
        f"$p.controls.play();"
        f"Start-Sleep -Milliseconds 300;"
        f"while ($p.playState -ne 1) {{ Start-Sleep -Milliseconds 120 }};"
        f"$p.close()"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=30)
    except Exception as e:
        print(f"   （播放失敗：{e}）")


def main():
    args = [a for a in sys.argv[1:]]
    rows = load()
    if not rows:
        print("讀不到 read100_en.txt")
        return

    if "--list" in args:
        for num, sent, zh in rows:
            if num in SECTION_HINT:
                print(f"\n── {SECTION_HINT[num]} ──")
            print(f"{num:3d}. {sent}")
            if zh:
                print(f"     （{zh}）")
        return

    nums = [a for a in args if a.isdigit()]
    start = int(nums[0]) if nums else 1
    end = int(nums[1]) if len(nums) > 1 else 100

    todo = [(n, s, z) for n, s, z in rows if start <= n <= end]
    print("=" * 62)
    print(f"英文 100 句練唸　第 {start} 到 {end} 句（共 {len(todo)} 句）")
    print("=" * 62)
    print("Enter=播放並下一句　r=重播　s=跳過　q=結束")
    print("⚠️ 不用模仿腔調——展場訪客本來就不是母語者，")
    print("   你的腔就是我們要測的真實情境。示範只是讓你知道唸什麼字。")
    print()

    i = 0
    while i < len(todo):
        num, sent, zh = todo[i]
        if num in SECTION_HINT:
            print(f"\n── {SECTION_HINT[num]} ──")
        print(f"[{num}/100] {sent}")
        if zh:
            print(f"          （中文意思：{zh}）")
        cmd = input("        ▶ Enter 播放 / r 重播 / s 跳過 / q 離開：").strip().lower()
        if cmd == "q":
            print("\n結束練習。")
            return
        if cmd == "s":
            i += 1
            continue
        play(num)
        if cmd == "r":
            continue          # 停在同一句，可再按 r
        i += 1
    print("\n" + "=" * 62)
    print("練完了。正式錄音在 RPI5：bash read100_en.sh")
    print("=" * 62)


main()
