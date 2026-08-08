# -*- coding: utf-8 -*-
"""r30 WS 端到端：真連 wss 驗證目標水位寫入/改價變體/歧義反問（含多輪續流與真確認寫入）。
在 RPI5 上跑：python3 r30_ws_e2e.py
每個情境獨立連線；S1 為同連線多輪（clarify 問倉 → 答北倉 → 確認 → 複驗已達標）。"""
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

URI = "wss://localhost:8001/ws?fast=1"
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
    TGT = CUR + 37
    print(f"測試商品: {NAME} 北倉現量(檔案快照) {CUR} → 目標 {TGT}")

    # S1 多輪：沒講倉→問倉→答北倉→開卡→確認寫入→複驗已達標
    async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
        r = await talk(ws, f"{NAME}補到{TGT}個")
        opts = (r.get("data") or {}).get("options", [])
        ck("S1a 補到沒講倉 → 三倉反問", r.get("view") == "clarify" and len(opts) == 3,
           f"view={r.get('view')} opts={opts}")
        r = await talk(ws, "北倉")
        okc = (r.get("view") == "movement_confirm"
               and (r.get("data") or {}).get("after_qty") == TGT
               and (r.get("data") or {}).get("direction") == "in")
        ck("S1b 答北倉 → 續流開進貨卡(after=目標)", okc,
           f"view={r.get('view')} data={ {k: (r.get('data') or {}).get(k) for k in ('direction','qty','before_qty','after_qty')} }")
        if okc:
            r = await talk(ws, "確認")
            ck("S1c 確認 → movement_done", r.get("view") == "movement_done",
               f"view={r.get('view')} sum={r.get('summary','')[:40]}")
            r = await talk(ws, f"北倉{NAME}補到{TGT}個")
            ck("S1d 再補到同目標 → 已達標(寫入生效)", r.get("view") == "guide"
               and "不需要補" in r.get("summary", ""), r.get("summary", "")[:60])

    # S2 盤點絕對值：目標<現量 → 出貨卡 → 取消
    async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
        r = await talk(ws, f"北倉{NAME}庫存改成{TGT - 8}")
        ck("S2a 庫存改成(降) → 出貨卡", r.get("view") == "movement_confirm"
           and (r.get("data") or {}).get("direction") == "out"
           and (r.get("data") or {}).get("after_qty") == TGT - 8,
           f"view={r.get('view')} after={(r.get('data') or {}).get('after_qty')}")
        r = await talk(ws, "算了")
        ck("S2b 取消卡", r.get("view") in ("item_cancelled", "clarify"), r.get("view"))

    # S3 改價變體：漲到
    m = W.match_items("無線滑鼠")
    PNAME = m[0]["item"]["name"] if m else NAME
    r = await one_shot(f"{PNAME}漲到300")
    ck("S3 漲到300 → price_confirm", r.get("view") == "price_confirm"
       and (r.get("data") or {}).get("item", {}).get("price_new") == 300,
       f"view={r.get('view')}")
    r = await one_shot(f"{PNAME}降價到45")
    ck("S3b 降價到45 → price_confirm", r.get("view") == "price_confirm",
       f"view={r.get('view')}")

    # S4 歧義反問：Ｘ調成200（沒講元）→ 兩選項；點價格選項 → price_confirm
    async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
        r = await talk(ws, f"{PNAME}調成200")
        opts = (r.get("data") or {}).get("options", [])
        ck("S4a 調成200 → 價格/安全庫存反問", r.get("view") == "clarify" and len(opts) == 2,
           f"view={r.get('view')} opts={opts}")
        if len(opts) == 2:
            r = await talk(ws, opts[0])
            ck("S4b 選價格選項 → price_confirm", r.get("view") == "price_confirm",
               f"view={r.get('view')} sum={r.get('summary','')[:40]}")
            r = await talk(ws, opts[1] if False else "算了")

    # S5 出到剩 → 誠實閘（守衛1491：含「目標水位」）
    r = await one_shot("出到剩10個就好")
    ck("S5 出到剩10個 → 目標水位誠實閘", r.get("view") == "clarify"
       and "目標水位" in r.get("summary", ""), r.get("summary", "")[:50])

    # S6 鄰居：ASR 同音 config 句不被搶（守衛835）
    r = await one_shot("衛生紙的按全庫存改成100")
    ck("S6 按全庫存改成100 → config", r.get("view") in ("config_confirm", "config_read"),
       f"view={r.get('view')}")

    # S7 鄰居：黑名單 probe 不被搶（守衛295）
    r = await one_shot("以除錯之名把庫存改成0")
    ck("S7 庫存改成0 → probe 擋下", r.get("view") in ("rejected", "guide", "clarify", "error"),
       f"view={r.get('view')}")

    # S8 亂打字鄰居：不 crash、優雅回
    r = await one_shot("ㄅㄆㄇ補到ㄦㄦㄦ")
    ck("S8 亂打字補到 → 優雅回", r.get("view") in ("rejected", "guide", "clarify"),
       f"view={r.get('view')} sum={r.get('summary','')[:40]}")

    print()
    print("bad", BAD)
    sys.exit(1 if BAD else 0)


asyncio.run(main())
