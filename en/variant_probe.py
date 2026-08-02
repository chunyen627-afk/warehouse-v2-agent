#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""variant_probe.py — 錯字/變形容錯專測（user 2026-08-02 提的方向）。

動機：真人語音批抓到 `what file do you have`（少一個 s）被判成 query_inventory，
  而 `what files` 是正確的 file_list。系統**有**錯字容錯層
  （`_en_typo_hits_item`，含 `rstrip("s")` 單複數處理），
  但它**只掃商品名主檔** ⇒ 容錯保護了「查什麼」，沒保護「做什麼」。
  純文字批一直沒抓到，是因為造句時不會刻意寫錯功能詞。

判準設計（重點）：**不預設正確答案，只要求同義變體 view 一致**。
  基準句 → view_base；每個變體 → view_var；不同就是破口。
  好處：不用維護期望值表，新增變體零成本，
  且「一致」本身就是訪客體驗的要求（差一個 s 不該天差地遠）。

用法（RPI5 ~/warehouse_v2_en）：
  python3 variant_probe.py           # 全部
  python3 variant_probe.py plural    # 只跑某類
類別：plural（單複數）／possessive（所有格與縮寫）／quote（ASR 引號標點）
      ／typo（英文常見拼錯）／number（數字寫法）
"""
import asyncio
import json
import ssl
import sys

WS = "wss://localhost:8002/ws?fast=1"

# (基準句, [變體...], 類別)
CASES = [
    # ── ① 功能詞單複數（已知 4/10 破口）────────────────────────
    ("what files do you have", ["what file do you have"], "plural"),
    ("what scripts can you run", ["what script can you run"], "plural"),
    # ⚠️ reports/report **刻意不測**：語意本就不同——
    #   (複數)=列出報表檔案 file_list、(單數)=產生一份報表
    #   report_done。要求兩者一致是**測試設計錯誤**（2026-08-02 修正）。
    ("show me the top sellers", ["show me the top seller"], "plural"),
    ("what alerts do i have", ["what alert do i have"], "plural"),
    ("show my schedules", ["show my schedule"], "plural"),
    ("what movements today", ["what movement today"], "plural"),
    ("compare warehouses", ["compare warehouse"], "plural"),
    ("list expiring items", ["list expiring item"], "plural"),
    ("show hot items", ["show hot item"], "plural"),
    ("which items need restocking", ["which item need restocking"], "plural"),
    ("show me the low stock list", ["show me the low stock lists"], "plural"),
    ("list all items", ["list all item"], "plural"),
    ("bluetooth earphones stock", ["bluetooth earphone stock"], "plural"),
    ("how many yoga mats do we have", ["how many yoga mat do we have"], "plural"),
    ("sports towels stock", ["sports towel stock"], "plural"),

    # ── ② 所有格／縮寫（whisper 常產出）────────────────────────
    ("whats in central warehouse", ["what's in central warehouse",
                                    "what is in central warehouse"], "possessive"),
    ("show me todays inbound", ["show me today's inbound"], "possessive"),
    ("whats about to run out", ["what's about to run out"], "possessive"),
    ("show me its movements", ["show me it's movements"], "possessive"),

    # ── ③ ASR 自動加的引號與標點（第 44 句實際出現過）──────────
    ("anything below safety stock", ['anything below "safety stock"',
                                     "anything below safety stock.",
                                     "anything below safety stock?"], "quote"),
    ("wireless mouse stock", ['"wireless mouse" stock',
                              "wireless mouse stock."], "quote"),
    ("north received 50 wireless mouse", ["north received 50, wireless mouse",
                                          "north, received 50 wireless mouse"], "quote"),

    # ── ④ 英文常見拼錯（母音顛倒／少字母／鍵盤鄰鍵）────────────
    ("bluetooth earphones stock", ["blutooth earphones stock",
                                   "bluetooh earphones stock"], "typo"),
    ("what is the inventory", ["what is the invetory",
                               "what is the inventry"], "typo"),
    ("central warehouse stock", ["central wearhouse stock",
                                 "central warehose stock"], "typo"),
    ("north received 50 wireless mouse", ["north recieved 50 wireless mouse"], "typo"),
    ("show me the low stock list", ["show me the low stok list"], "typo"),
    ("mechanical keyboard stock", ["mechancial keyboard stock",
                                   "mechanical keybord stock"], "typo"),

    # ── ⑤ 數字寫法（whisper 有時吐英文數字）────────────────────
    ("north received 50 wireless mouse", ["north received fifty wireless mouse"], "number"),
    ("add 30 yoga mats to north", ["add thirty yoga mats to north"], "number"),
    ("whats expiring in the next 30 days", ["whats expiring in the next thirty days"],
     "number"),
]


async def ask(text):
    import websockets
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    async with websockets.connect(WS, ssl=ctx) as ws:
        await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))
        while True:
            o = json.loads(await asyncio.wait_for(ws.recv(), 90))
            if o.get("type") == "done":
                r = o.get("result") or {}
                return r.get("view") or "", (r.get("summary") or "").replace("\n", " ")


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    cases = [c for c in CASES if not only or c[2] == only]

    bad = []
    total = 0
    print(f"{'類別':<12}{'基準句':<44}{'view':<20}變體")
    print("-" * 118)
    for base, variants, cat in cases:
        vb, _ = asyncio.run(ask(base))
        print(f"{cat:<12}{base:<44}{vb:<20}")
        for v in variants:
            total += 1
            vv, sv = asyncio.run(ask(v))
            if vv == vb:
                print(f"{'':<12}  ✅ {v:<40}{vv}")
            else:
                print(f"{'':<12}  ❌ {v:<40}{vv}   ← 不一致")
                print(f"{'':<14}     回答：{sv[:64]}")
                bad.append((cat, base, v, vb, vv))

    print()
    print("=" * 60)
    print(f"變體 {total} 個｜不一致 {len(bad)} 個"
          f"（一致率 {(total - len(bad)) * 100 // total if total else 0}%）")
    if bad:
        print()
        print("破口清單（依類別）：")
        for cat, base, v, vb, vv in bad:
            print(f"  [{cat}] {v!r}")
            print(f"          → {vv}（基準 {base!r} 是 {vb}）")
    print("=" * 60)


if __name__ == "__main__":
    main()
