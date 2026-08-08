# -*- coding: utf-8 -*-
"""r30 EN WS 端到端（8002）：restock to / set stock to / price 變體 / set-to 歧義
/ sell-down-to 誠實閘 / 相對量與 config 鄰居迴歸。含多輪續流（答 north）與真確認寫入。"""
import asyncio
import io
import json
import ssl
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import websockets

sys.path.insert(0, ".")
import warehouse as W

W.init("seed_data.json")

URI = "wss://localhost:8002/ws?fast=1"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

BAD = 0


def ck(label, cond, detail=""):
    global BAD
    if not cond:
        BAD += 1
    print(("OK  " if cond else "NG  ") + label + ("  | " + str(detail)[:110] if detail else ""))


async def talk(ws, text):
    await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
        if msg.get("type") == "done":
            return msg.get("result", {})


async def one_shot(text):
    async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
        return await talk(ws, text)


async def main():
    _s = W.state()
    item = next(it for it in _s.items
                if _s.stock.get("north", {}).get(it["sku_id"], 0) >= 10)
    NAME = item["name"]
    CUR = _s.stock["north"][item["sku_id"]]
    TGT = CUR + 33
    print(f"test item: {NAME} north(snapshot)={CUR} target={TGT}")

    # S1 多輪：no warehouse -> clarify -> answer "north"（英文倉名續答）-> confirm -> 已達標
    async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
        r = await talk(ws, f"restock {NAME} to {TGT}")
        opts = (r.get("data") or {}).get("options", [])
        ck("S1a restock no-wh -> 3-option clarify", r.get("view") == "clarify"
           and len(opts) == 3, f"view={r.get('view')} opts={opts}")
        r = await talk(ws, "north")
        okc = (r.get("view") == "movement_confirm"
               and (r.get("data") or {}).get("after_qty") == TGT
               and (r.get("data") or {}).get("direction") == "in")
        ck("S1b answer north -> inbound card (after=target)", okc,
           f"view={r.get('view')} data={ {k: (r.get('data') or {}).get(k) for k in ('direction','qty','before_qty','after_qty')} }")
        if okc:
            r = await talk(ws, "confirm")
            ck("S1c confirm -> movement_done", r.get("view") == "movement_done",
               f"view={r.get('view')} sum={r.get('summary','')[:40]}")
            r = await talk(ws, f"restock north {NAME} to {TGT}")
            ck("S1d re-restock same target -> already met", r.get("view") == "guide"
               and "no restock needed" in r.get("summary", ""), r.get("summary", "")[:60])

    # S2 盤點絕對值（降）-> 出貨卡 -> cancel
    async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
        r = await talk(ws, f"set north {NAME} stock to {TGT - 6}")
        ck("S2a set stock lower -> outbound card", r.get("view") == "movement_confirm"
           and (r.get("data") or {}).get("direction") == "out"
           and (r.get("data") or {}).get("after_qty") == TGT - 6,
           f"view={r.get('view')} after={(r.get('data') or {}).get('after_qty')}")
        r = await talk(ws, "cancel")
        ck("S2b cancel", r.get("view") in ("item_cancelled", "clarify"), r.get("view"))

    # S3 改價變體
    m = W.match_items("wireless mouse")
    PNAME = m[0]["item"]["name"] if m else NAME
    r = await one_shot(f"raise {PNAME} price to 300")
    ck("S3 raise price to 300 -> price_confirm", r.get("view") == "price_confirm",
       f"view={r.get('view')}")
    r = await one_shot(f"lower the price of {PNAME} to 120")
    ck("S3b lower the price of X -> price_confirm", r.get("view") == "price_confirm",
       f"view={r.get('view')} sum={r.get('summary','')[:50]}")

    # S4 set-to 歧義：兩選項，點價格選項 -> price_confirm
    async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
        r = await talk(ws, f"set {PNAME} to 200")
        opts = (r.get("data") or {}).get("options", [])
        ck("S4a set X to 200 -> price/safety clarify", r.get("view") == "clarify"
           and len(opts) == 2, f"view={r.get('view')} opts={opts}")
        if len(opts) == 2:
            r = await talk(ws, opts[0])
            ck("S4b pick price option -> price_confirm", r.get("view") == "price_confirm",
               f"view={r.get('view')}")
            await talk(ws, "cancel")

    # S5 sell-down-to 誠實閘（Down Jacket 誤配防）
    r = await one_shot("sell down to 10 left")
    ck("S5 sell down to 10 left -> honest gate", r.get("view") == "clarify"
       and "not supported" in r.get("summary", ""), r.get("summary", "")[:60])

    # S6 鄰居：相對量 restock（守衛1062 形）不變
    r = await one_shot(f"restock 30 {PNAME} at south")
    d = r.get("data") or {}
    ck("S6 restock 30 X at south -> relative +30", r.get("view") == "movement_confirm"
       and d.get("qty") == 30 and d.get("direction") == "in",
       f"view={r.get('view')} qty={d.get('qty')}")
    if r.get("view") == "movement_confirm":
        pass  # 一次性連線，卡片隨連線關閉作廢，零寫入

    # S7 鄰居：config 句不被搶
    r = await one_shot(f"set {PNAME} safety stock to 50")
    ck("S7 safety stock to 50 -> config", r.get("view") in ("config_confirm", "config_read"),
       f"view={r.get('view')}")

    # S8 鄰居：refill 商品名查詢不誤觸
    r = await one_shot("mosquito repellent refill on hand")
    ck("S8 refill item query unaffected", r.get("view") in ("inventory", "inventory_single"),
       f"view={r.get('view')}")

    # S9 亂打字：qwerty 不是數字 → 不攔，落回既有「restock 關鍵字→缺貨清單」
    #   路徑（守衛627 what needs restocking 同款，非回歸）
    r = await one_shot("restock asdfgh to qwerty")
    ck("S9 mash input graceful", r.get("view") in ("rejected", "guide", "clarify",
                                                    "inventory", "related_help",
                                                    "low_stock"),
       f"view={r.get('view')} sum={r.get('summary','')[:40]}")

    print()
    print("bad", BAD)
    sys.exit(1 if BAD else 0)


asyncio.run(main())
