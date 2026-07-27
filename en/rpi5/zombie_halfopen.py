# -*- coding: utf-8 -*-
"""真・殭屍測試：TCP **半開**（訪客走出 WiFi 範圍 / 手機沒電關機）。

跟 zombie_test.py 的差別（那支測 RST，結果零殘留）：
  RST  → server 立刻知道對方掛了 → finally 正常清理 ✅
  半開 → **對方靜靜消失、什麼都不送**，server 卡在 receive 永遠等
        → 連線與 session state 都清不掉 ← 展場最常發生的（訪客拿手機走掉）

模擬方法：用 iptables 把該連線的封包全丟掉（DROP，不是 REJECT），
server 端就完全收不到任何東西，等同對方人間蒸發。

⚠️ scp 上去執行，需要 sudo（改 iptables）。
"""
import asyncio
import json
import ssl
import subprocess
import time

import websockets

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
PORT = 8002


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def conn_count():
    out = sh(f"ss -tn state established '( sport = :{PORT} )'")
    return max(0, len(out.strip().splitlines()) - 1)


def ws_procs():
    """server 進程的 fd 數（連線沒清掉的話 fd 會一直長）"""
    pid = sh("pgrep -f warehouse_v2_en/server_https.py").strip().split("\n")[0]
    if not pid:
        return "?"
    return sh(f"ls /proc/{pid}/fd 2>/dev/null | wc -l").strip()


async def query(ws, text):
    await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
        if m.get("type") == "done":
            return m.get("result", {}).get("view")


async def main():
    base_c, base_fd = conn_count(), ws_procs()
    print(f"起始：連線 {base_c} 條、server fd {base_fd} 個\n")

    print("① 建立 3 條連線並各問一句")
    conns = []
    for i in range(3):
        ws = await websockets.connect(f"wss://localhost:{PORT}/ws?fast=1",
                                      ssl=ctx, max_size=None)
        await query(ws, "whats running low")
        sk = ws.transport.get_extra_info("socket")
        conns.append((ws, sk.getsockname()[1]))     # 記本地 port
    print(f"   → 連線 {conn_count()} 條、fd {ws_procs()} 個")
    ports = [p for _, p in conns]
    print(f"   本地 port: {ports}")

    print("\n② 讓這 3 條『人間蒸發』（iptables DROP，模擬走出 WiFi 範圍）")
    for p in ports:
        sh(f"sudo iptables -I INPUT -p tcp --sport {p} -j DROP")
        sh(f"sudo iptables -I OUTPUT -p tcp --dport {p} -j DROP")
    print("   已封鎖，server 從此收不到這 3 條的任何封包")

    try:
        elapsed = 0
        for step in (30, 60, 90):
            await asyncio.sleep(step)
            elapsed += step
            print(f"   [{elapsed}s] 連線 {conn_count()} 條、fd {ws_procs()} 個")

        print("\n③ 殘留期間新訪客照樣可用？")
        t0 = time.perf_counter()
        async with websockets.connect(f"wss://localhost:{PORT}/ws?fast=1",
                                      ssl=ctx, max_size=None) as ws:
            v = await query(ws, "power bank stock")
        print(f"   → 正常（view={v}），{time.perf_counter()-t0:.2f}s")
    finally:
        print("\n④ 解除封鎖，看 server 是否終於察覺")
        for p in ports:
            sh(f"sudo iptables -D INPUT -p tcp --sport {p} -j DROP")
            sh(f"sudo iptables -D OUTPUT -p tcp --dport {p} -j DROP")
        await asyncio.sleep(8)
        print(f"   → 連線 {conn_count()} 條、fd {ws_procs()} 個"
              f"（起始 {base_c} / {base_fd}）")

    print("\n結論：殘留數 = 展場每位『走掉的訪客』會累積的量")


asyncio.run(main())
