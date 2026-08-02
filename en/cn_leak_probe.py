#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cn_leak_probe.py — 掃「英文版對英文訪客講中文」（2026-08-02）。

error_ux 抓到第一個：超長英文句 → 回中文「這句有點長，我怕理解錯你的意思…」。
靜態掃描發現 server.py 有 **50 處**中文訊息可能回給訪客，
但多數所在函式可能對英文 early-return（同坑 23 的教訓：
**判斷雷不能只看「有沒有中文」，要看「英文句會不會執行到」**）。

所以這支用**實際送英文句**去撞，只回報真的漏出來的。
每個情境都對應一段已知會產生中文訊息的程式碼路徑。

判準：view 正常但 summary 含中日韓字元 → 漏（英文訪客看不懂）。

用法（RPI5 ~/warehouse_v2_en）：python3 cn_leak_probe.py
"""
import asyncio
import json
import re
import ssl

WS = "wss://localhost:8002/ws?fast=1"
CJK = re.compile(r"[一-鿿぀-ヿ]")

# (情境, 英文句) — 每句瞄準一段已知含中文訊息的路徑
CASES = [
    ("長句無接手（long-gate）", "please tell me " * 30 + "the stock"),
    ("沒有進行中的操作", "confirm"),
    ("沒有進行中的操作2", "yes"),
    ("問要查哪個商品", "stock"),
    ("重新開始/歡迎", "reset"),
    ("道別", "bye"),
    ("一次比兩個以上", "compare earphones and mouse and yoga mat and tissue"),
    ("一次查多商品", "wireless mouse and yoga mat stock"),
    ("進出統計不支援的期間", "movements last year"),
    ("排行不支援的期間", "best sellers last year"),
    ("設定用百分比", "set safety stock for yoga mat to 20 percent"),
    ("警示不支援的條件", "alert me when yoga mat expires"),
    ("退貨自動記錄", "return 5 wireless mouse to north"),
    ("進貨大於出貨", "why is inbound more than outbound"),
    ("雙寫入複合句", "north received 50 mouse then transfer 20 to south"),
    ("刪除流程中亂講", "delete item"),
    ("排除某商品的總覽", "show all items except yoga mat"),
    ("寫錯倉別來不及改", "undo that"),
    ("空的追問", "what about"),
    ("只說商品名", "yoga mat"),
]


async def main():
    import websockets
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    print("=" * 88)
    print("英文版對英文訪客講中文？（實際送英文句去撞，只回報真的漏出來的）")
    print("=" * 88)

    leaks = []
    ok_n = 0
    for name, sent in CASES:
        # 每句獨立連線，避免前一句的狀態影響
        async with websockets.connect(WS, ssl=ctx) as ws:
            await ws.send(json.dumps({"type": "chat", "text": sent},
                                     ensure_ascii=False))
            view = summ = ""
            try:
                while True:
                    o = json.loads(await asyncio.wait_for(ws.recv(), 120))
                    if o.get("type") == "done":
                        r = o.get("result") or {}
                        view = r.get("view") or ""
                        summ = (r.get("summary") or "").replace("\n", " ")
                        break
            except Exception as e:
                print(f"  ⚠️ {name:24} 例外 {e!r}")
                continue

        m = CJK.search(summ)
        if m:
            leaks.append((name, sent, view, summ))
            print(f"  ❌ {name:24} view={view}")
            print(f"       {summ[:80]}")
        else:
            ok_n += 1
            print(f"  ✅ {name:24} view={view:18} {summ[:44]}")

    print()
    print("=" * 88)
    print(f"{len(CASES)} 個情境：乾淨 {ok_n}、**中文漏出 {len(leaks)}**")
    if leaks:
        print()
        print("漏出清單（需英文化）：")
        for name, sent, view, summ in leaks:
            print(f"  · {name}｜輸入 {sent[:40]!r}")
            print(f"    → {summ[:76]}")
    print("=" * 88)


if __name__ == "__main__":
    asyncio.run(main())
