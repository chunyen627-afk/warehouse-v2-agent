#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_ir.py — 產生指定 RT60 的**擴散殘響**脈衝響應（2026-08-02）。

為什麼不用 `aecho`：它是「回音」濾波器，產生**離散的重複**；
真實殘響是**密集擴散的反射尾巴**。用 aecho 模擬會**高估傷害**
（離散回音對 ASR 的干擾比擴散殘響大得多）。
實測 aecho RT60≈1.4s → ASR 直接回 None（完全失敗），
那個數字不可信，所以改用卷積殘響。

方法：指數衰減的高斯白雜訊 = 標準的合成 IR 做法
  h(t) = noise(t) × exp(-6.91·t/RT60)
  （6.91 = ln(1000)，即 60dB 衰減）
再加：
  · 直達聲（t=0 的脈衝）——保留原始語音清晰度
  · 前期延遲（early delay）——聲音從音源到牆面再反射回來的時間
  · 高頻多衰減（大空間空氣吸收，符合真實聲學）

用法：python3 gen_ir.py <RT60秒> <輸出檔>
  python3 gen_ir.py 1.4 ir_hall_14.wav
"""
import math
import random
import struct
import sys
import wave

SR = 16000


def gen_ir(rt60, path, direct=0.7, early_ms=12):
    n = int(SR * (rt60 + 0.1))
    random.seed(42)          # 固定種子 → 結果可重現
    samples = []
    early = int(SR * early_ms / 1000)
    for i in range(n):
        t = i / SR
        if i == 0:
            v = direct                       # 直達聲
        elif i < early:
            v = 0.0                          # 前期延遲（還沒反射回來）
        else:
            # 指數衰減包絡 × 白雜訊 = 擴散反射尾巴
            env = math.exp(-6.91 * t / rt60)
            # 高頻隨時間多衰減（空氣吸收）：用簡單的一階低通近似
            v = random.gauss(0, 1) * env * 0.35
        samples.append(v)

    # 一階低通讓尾巴的高頻先消失（真實空間的特性）
    a = 0.35
    prev = 0.0
    out = []
    for i, v in enumerate(samples):
        if i == 0:
            out.append(v)                    # 直達聲不濾
            prev = v
        else:
            prev = a * v + (1 - a) * prev
            out.append(prev)

    peak = max(abs(x) for x in out) or 1.0
    pcm = b"".join(struct.pack("<h", int(max(-1, min(1, x / peak)) * 32000))
                   for x in out)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    print(f"{path}: RT60={rt60}s, {n} samples ({n/SR:.2f}s)")


if __name__ == "__main__":
    rt = float(sys.argv[1]) if len(sys.argv) > 1 else 1.4
    out = sys.argv[2] if len(sys.argv) > 2 else f"ir_{rt}.wav"
    gen_ir(rt, out)
