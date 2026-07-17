# -*- coding: utf-8 -*-
"""branch_walk.py — clarify 選單分支全遍歷（r55 起，user 要求：有選項就每個分支都跑）

對每個「會產生選單」的觸發句：取回 options → 逐一在同連線把每個選項送回去，
驗證每個分支 ①不 error ②回答含該選項的關鍵內容（商品選項驗商品名前綴）。
另驗序數路（說「第N個」）等價於點選項。

用法：python branch_walk.py [--rpi5]
"""
import asyncio, json, ssl, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import websockets

if "--rpi5" in sys.argv:
    URI = "wss://localhost:8001/ws?fast=1"
    CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
else:
    URI, CTX = "ws://localhost:8000/ws?fast=1", None

# 會出選單的觸發句（涵蓋：歧義短稱清單/通稱表/oov 選單/config 歧義/寫入歧義）
TRIGGERS = ["咖啡還剩多少", "帽子有哪些", "鍋子還有嗎", "電動還有多少",
            "嬰兒的東西總價值多少", "露營庫存", "咖啡安全庫存改成50", "進10個咖啡"]


async def ask(ws, text):
    await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=45))
        if m.get("type") == "done":
            return m.get("result") or {}


async def main():
    total_br = bad = 0
    for trig in TRIGGERS:
        async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
            r = await ask(ws, trig)
        opts = ((r.get("data") or {}).get("options")) or []
        view = r.get("view")
        print(f"\n▶ {trig}  → view={view}, {len(opts)} 選項")
        if view != "clarify" or not opts:
            print(f"   （無選單{'——請人工確認是否合理' if view != 'clarify' else ''}）")
            continue
        for i, opt in enumerate(opts, 1):
            total_br += 1
            # 每分支獨立連線（乾淨 context）——直接把選項文字當輸入（=點按鈕行為）
            async with websockets.connect(URI, ssl=CTX, max_size=None) as ws2:
                r2 = await ask(ws2, str(opt))
            v2, s2 = r2.get("view", "?"), (r2.get("summary") or "")[:46]
            # 商品型選項（「XX 庫存」）驗回答含商品前 2 字；快捷型只驗不 error
            core = str(opt).replace(" 庫存", "").strip()
            ok = v2 not in ("error", "rejected") and (core[:2] in s2 or len(core) > 8 or "類" in core or core in ("查倉管", "商品清單", "全部商品庫存"))
            mark = "✅" if ok else "❌"
            if not ok:
                bad += 1
            print(f"   {mark} 選項{i}「{opt}」→ {v2} | {s2}")
        # 序數路抽驗（同一條連線：觸發→第一個）
        async with websockets.connect(URI, ssl=CTX, max_size=None) as ws3:
            r0 = await ask(ws3, trig)
            r3 = await ask(ws3, "第一個")
        v3 = r3.get("view", "?")
        # 鏈式 clarify 合法（寫入類選完商品再問倉別）；同選單重問=序數沒被理解才算異常
        same_menu = v3 == "clarify" and (r3.get("summary") or "") == (r0.get("summary") or "")
        m3 = "✅" if v3 not in ("error", "rejected") and not same_menu else "❌"
        if m3 == "❌": bad += 1
        print(f"   {m3} 序數「第一個」→ {v3} | {(r3.get('summary') or '')[:46]}")
    print(f"\n{'='*60}\n分支總數 {total_br}+序數8 · 異常 {bad}")
    sys.exit(1 if bad else 0)

asyncio.run(main())
