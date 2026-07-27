# -*- coding: utf-8 -*-
"""展場混沌測試：反覆連線／亂打／各種離開方式，測長時間穩定度。

為什麼需要這支（user 定調 2026-07-27）：
  功能測試（守衛/劇情批）測的是「單一訪客好好操作」，
  展場真實樣態是**訪客不斷連進來、亂打、關掉、下一個又來**。
  這支專測「連線層 + 狀態層」會不會隨時間累積問題。

每位訪客隨機：
  - 語言（中/英）
  - 問 1-6 句，內容從「正常查詢 / 亂打 / 半句 / 超長 / 純符號 / 寫入」隨機取
  - 離開方式：好好關 / RST 硬斷 / 問到一半就斷（最容易留 pending 卡）

每輪結束檢查：連線數、fd 數、記憶體、server 記的訪客數 → 有沒有單調成長。

⚠️ scp 上去執行。用法：python3 chaos_test.py [輪數] [每輪同時訪客數]
"""
import asyncio
import json
import random
import ssl
import subprocess
import sys
import time

import websockets

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 10
PER = int(sys.argv[2]) if len(sys.argv) > 2 else 4

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

EN_OK = ["bluetooth earphones stock", "whats running low", "best sellers",
         "compare north and south", "power bank stock", "what came in today"]
ZH_OK = ["藍牙耳機庫存", "哪些快缺貨", "本月熱銷", "北倉跟南倉比較",
         "行動電源庫存", "今天進了什麼"]
# 展場訪客真的會打的東西
JUNK = ["asdkjhasd", "?????", "aaaaaaaaaa", "12345", "。。。。", "!!!!",
        "ㄅㄆㄇㄈ", "test", "hello???", "   ", "\\n\\n", "🎉🎉🎉",
        "a" * 300, "查" * 100, "'; DROP TABLE items; --", "<script>x</script>"]
HALF = ["藍牙", "北倉", "進貨", "stock", "how many", "compare", "設定"]
WRITE = ["北倉進50個滑鼠", "north received 50 wireless mouse",
         "南倉出10個啤酒", "set yoga mat safety stock to 80"]


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True,
                          text=True).stdout.strip()


def snapshot():
    pid = sh("pgrep -f 'warehouse_v2_en/server_https.py' | head -1")
    pid_zh = sh("pgrep -f 'warehouse_v2/server_https.py' | head -1")
    conn = sh("ss -tn state established '( sport = :8001 or sport = :8002 )'")
    return {
        "conn": max(0, len(conn.splitlines()) - 1),
        "fd_en": sh(f"ls /proc/{pid}/fd 2>/dev/null | wc -l") if pid else "?",
        "fd_zh": sh(f"ls /proc/{pid_zh}/fd 2>/dev/null | wc -l") if pid_zh else "?",
        "rss_en": sh(f"grep VmRSS /proc/{pid}/status 2>/dev/null | awk '{{print $2}}'"),
        "load": open("/proc/loadavg").read().split()[0],
    }


async def one_visitor(stats):
    """一位混沌訪客。"""
    zh = random.random() < 0.5
    port = 8001 if zh else 8002
    pool = (ZH_OK if zh else EN_OK)
    n = random.randint(1, 6)
    leave = random.choice(["clean", "rst", "midway"])
    ws = None
    try:
        ws = await websockets.connect(f"wss://localhost:{port}/ws?fast=1",
                                      ssl=ctx, max_size=None, open_timeout=30)
        for i in range(n):
            kind = random.choices(
                ["ok", "junk", "half", "write"], weights=[5, 3, 2, 1])[0]
            text = random.choice(
                {"ok": pool, "junk": JUNK, "half": HALF, "write": WRITE}[kind])
            await ws.send(json.dumps({"type": "chat", "text": text},
                                     ensure_ascii=False))
            # midway：問到一半直接斷（最容易留下 pending 卡 / step 機狀態）
            if leave == "midway" and i == n - 1:
                ws.transport.abort()
                stats["midway"] += 1
                return
            t0 = time.perf_counter()
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
                if m.get("type") == "done":
                    stats["lat"].append(time.perf_counter() - t0)
                    stats["ok"] += 1
                    break
                if m.get("type") == "error":
                    stats["err"].append(f"{text[:20]}: {m.get('text','')[:40]}")
                    break
            await asyncio.sleep(random.uniform(0.2, 1.5))
        if leave == "rst":
            ws.transport.abort()
            stats["rst"] += 1
        else:
            await ws.close()
            stats["clean"] += 1
    except Exception as e:
        stats["exc"].append(f"{type(e).__name__}: {str(e)[:60]}")
        if ws:
            try:
                ws.transport.abort()
            except Exception:
                pass


async def main():
    base = snapshot()
    print(f"混沌測試：{ROUNDS} 輪 × 每輪 {PER} 位訪客"
          f"（隨機語言/句數/亂打/離開方式）")
    print(f"起始：連線 {base['conn']} · fd_en {base['fd_en']} · "
          f"fd_zh {base['fd_zh']} · RSS {base['rss_en']}KB\n")

    stats = {"ok": 0, "clean": 0, "rst": 0, "midway": 0,
             "err": [], "exc": [], "lat": []}
    for r in range(1, ROUNDS + 1):
        await asyncio.gather(*[one_visitor(stats) for _ in range(PER)])
        await asyncio.sleep(2)
        s = snapshot()
        print(f"[{r:2}/{ROUNDS}] 連線 {s['conn']:2} · fd_en {s['fd_en']:>3} · "
              f"fd_zh {s['fd_zh']:>3} · RSS {s['rss_en']:>7}KB · "
              f"load {s['load']} · 完成 {stats['ok']}")

    await asyncio.sleep(10)
    fin = snapshot()
    lat = sorted(stats["lat"])
    print(f"\n{'='*62}")
    print(f"完成請求 {stats['ok']} · 離開方式："
          f"好好關 {stats['clean']} / 硬斷 {stats['rst']} / 中途斷 {stats['midway']}")
    if lat:
        print(f"回應 中位 {lat[len(lat)//2]:.2f}s · "
              f"p90 {lat[int(len(lat)*0.9)]:.2f}s · max {lat[-1]:.2f}s")
    print(f"\n資源變化（起始 → 結束）")
    print(f"  連線  {base['conn']} → {fin['conn']}")
    print(f"  fd_en {base['fd_en']} → {fin['fd_en']}")
    print(f"  fd_zh {base['fd_zh']} → {fin['fd_zh']}")
    print(f"  RSS   {base['rss_en']}KB → {fin['rss_en']}KB")
    if stats["err"]:
        print(f"\n⚠️ error view {len(stats['err'])}:")
        for e in stats["err"][:6]:
            print(f"   {e}")
    if stats["exc"]:
        print(f"\n❌ 例外 {len(stats['exc'])}:")
        for e in stats["exc"][:6]:
            print(f"   {e}")
    if not stats["err"] and not stats["exc"]:
        print("\n✅ 零錯誤零例外")


asyncio.run(main())
