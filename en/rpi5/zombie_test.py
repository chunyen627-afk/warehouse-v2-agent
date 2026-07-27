# -*- coding: utf-8 -*-
"""殭屍連線實測：訪客「直接消失」時 server 會不會留下殘留。

展場真實情境：訪客不會好好關頁面，而是
  ① 關螢幕/鎖手機 → WS 可能還在
  ② 走出 WiFi 範圍 → **TCP 半開**，server 收不到 FIN
  ③ 直接關機

②③ 最危險：server 卡在 receive 永遠等不到訊息，
連線與 session state（_ctx_by_vid / _pending_by_vid…）都清不掉。

⚠️ scp 上去執行，不要用 SSH heredoc（跳脫字元會被層層轉譯搞壞檔案）。
"""
import asyncio
import json
import socket
import ssl
import subprocess
import time

import websockets

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
PORT = 8002
LINGER0 = struct_linger = bytes([1, 0, 0, 0, 0, 0, 0, 0])   # onoff=1, linger=0


def conn_count():
    out = subprocess.run(
        ["ss", "-tn", "state", "established", f"( sport = :{PORT} )"],
        capture_output=True, text=True).stdout
    return max(0, len(out.strip().splitlines()) - 1)


def last_log():
    out = subprocess.run(
        ["sudo", "journalctl", "-u", "warehouse-v2-en", "--since", "3 min ago",
         "--no-pager"], capture_output=True, text=True).stdout
    hits = [l for l in out.splitlines() if "訪客斷線（剩" in l]
    return hits[-1].split("]")[-1].strip() if hits else "(無斷線紀錄)"


async def query(ws, text):
    await ws.send(json.dumps({"type": "chat", "text": text},
                             ensure_ascii=False))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
        if m.get("type") == "done":
            return m.get("result", {}).get("view")


async def normal_visitor():
    """正常訪客：問一句然後好好關閉。"""
    async with websockets.connect(f"wss://localhost:{PORT}/ws?fast=1",
                                  ssl=ctx, max_size=None) as ws:
        return await query(ws, "power bank stock")


async def vanishing_visitor():
    """消失的訪客：問一句後**不告而別**（RST，模擬當機/斷電/走出範圍）。"""
    ws = await websockets.connect(f"wss://localhost:{PORT}/ws?fast=1",
                                  ssl=ctx, max_size=None)
    await query(ws, "whats running low")
    try:
        sk = ws.transport.get_extra_info("socket")
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, LINGER0)
    except Exception:
        pass
    ws.transport.abort()      # 不送 close frame，直接斷
    return ws                 # 持有參照，避免 GC 順手做清理


async def main():
    base = conn_count()
    print(f"起始連線數 {base}（kiosk 兩個分頁本來就佔著）\n")

    print("① 正常訪客（好好關閉）")
    await normal_visitor()
    await asyncio.sleep(2)
    print(f"   → 連線數 {conn_count()} | {last_log()}\n")

    print("② 3 位『不告而別』的訪客（RST）")
    keep = [await vanishing_visitor() for _ in range(3)]
    await asyncio.sleep(3)
    c1 = conn_count()
    print(f"   → 連線數 {c1}（起始 {base}）| {last_log()}")

    print("\n   等 45 秒，看 server 會不會自己清掉…")
    await asyncio.sleep(45)
    c2 = conn_count()
    print(f"   → 連線數 {c2} | {last_log()}")
    leaked = c2 - base
    print(f"\n   殘留 {leaked} 條"
          f"{'  ⚠️ 每位走掉的訪客都會累積' if leaked > 0 else '  ✅ 沒有殘留'}")

    print("\n③ 殘留期間系統照樣可用？")
    t0 = time.perf_counter()
    v = await normal_visitor()
    print(f"   → 新訪客查詢正常（view={v}），{time.perf_counter()-t0:.2f}s")
    print(f"\n最終連線數 {conn_count()}")


asyncio.run(main())
