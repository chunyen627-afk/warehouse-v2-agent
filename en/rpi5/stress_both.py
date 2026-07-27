# -*- coding: utf-8 -*-
"""展場情境壓測：中英兩版**同時**被訪客使用，量穩定度與速度。

情境（越後面越嚴苛）：
  A. 基準——各自單獨跑（對照組）
  B. 中英各 1 位訪客同時查詢
  C. 中英各 2 位（共 4 位）同時
  D. 一邊語音辨識（吃滿 4 核）+ 另一邊查詢 ← 最壞情況

⚠️ scp 上去執行（中文在 SSH heredoc 會被吃掉）。
"""
import asyncio
import json
import ssl
import statistics
import subprocess
import time
from pathlib import Path

import websockets

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

ZH = [("藍牙耳機庫存", 8001), ("哪些快缺貨", 8001),
      ("本月熱銷", 8001), ("北倉跟南倉比較", 8001)]
EN = [("bluetooth earphones stock", 8002), ("whats running low", 8002),
      ("best sellers this month", 8002), ("compare north and south", 8002)]


async def ask(text, port):
    t0 = time.perf_counter()
    try:
        async with websockets.connect(f"wss://localhost:{port}/ws?fast=1",
                                      ssl=ctx, max_size=None,
                                      open_timeout=30) as ws:
            await ws.send(json.dumps({"type": "chat", "text": text},
                                     ensure_ascii=False))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
                if m.get("type") == "done":
                    r = m.get("result") or {}
                    return (time.perf_counter() - t0, r.get("view", "?"), None)
                if m.get("type") == "error":
                    return (time.perf_counter() - t0, "error", m.get("text"))
    except Exception as e:
        return (time.perf_counter() - t0, "EXC", f"{type(e).__name__}: {e}")


def load_avg():
    return open("/proc/loadavg").read().split()[0]


async def scenario(name, tasks):
    t0 = time.perf_counter()
    res = await asyncio.gather(*[ask(t, p) for t, p in tasks])
    wall = time.perf_counter() - t0
    lat = [r[0] for r in res]
    bad = [(t, r) for (t, _), r in zip(tasks, res)
           if r[1] in ("error", "EXC")]
    print(f"\n【{name}】{len(tasks)} 個請求")
    print(f"  總耗時 {wall:.2f}s | 各別 min {min(lat):.2f} / "
          f"中位 {statistics.median(lat):.2f} / max {max(lat):.2f}s "
          f"| load {load_avg()}")
    if bad:
        print(f"  ❌ 失敗 {len(bad)}:")
        for t, r in bad:
            print(f"     {t} → {r[1]} {r[2]}")
    else:
        print("  ✅ 全部成功")
    return max(lat), len(bad)


def asr_bg(port, wav):
    """背景丟一個語音辨識（會吃滿 4 核）"""
    return subprocess.Popen(
        ["curl", "-sk", "-X", "POST", f"https://localhost:{port}/api/asr",
         "--data-binary", f"@{wav}", "-H",
         "Content-Type: application/octet-stream", "-m", "120"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def main():
    print("=" * 60)
    print("展場壓測：中英雙開，訪客同時使用")
    print("=" * 60)

    await scenario("A1 基準：只有中文 1 人", ZH[:1])
    await scenario("A2 基準：只有英文 1 人", EN[:1])
    await scenario("B  中英各 1 人同時", [ZH[0], EN[0]])
    await scenario("C  中英各 2 人同時（4 請求）", ZH[:2] + EN[:2])
    await scenario("C2 中英各 4 人同時（8 請求）", ZH + EN)

    # D：一邊語音（吃滿 CPU）+ 另一邊查詢
    wav = Path.home() / "voice_poc/audio/user_clean/1.wav"
    if wav.exists():
        print("\n【D  中文語音辨識中 + 英文訪客查詢】")
        p = asr_bg(8001, wav)
        await asyncio.sleep(0.3)          # 讓 ASR 先吃上 CPU
        await scenario("   └ 英文查詢（此時中文正在跑 whisper）", EN[:2])
        p.wait()
    print("\n" + "=" * 60)


asyncio.run(main())
