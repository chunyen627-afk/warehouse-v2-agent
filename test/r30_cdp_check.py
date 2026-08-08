# -*- coding: utf-8 -*-
"""r30 CDP 畫面驗收：盤點絕對值卡／漲到改價卡／調成歧義選單 真畫面截圖。零寫入。
在機一跑：python3 r30_cdp_check.py（kiosk 9222、port 8001 訪客頁）"""
import base64
import json
import sys
import time
import urllib.request

import websockets.sync.client as wsc

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8001


def pages():
    with urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5) as r:
        return [t for t in json.load(r) if t["type"] == "page"]


t = next(t for t in pages()
         if f":{PORT}" in t["url"] and "fast=1" not in t["url"])
ws = wsc.connect(t["webSocketDebuggerUrl"], max_size=None)
_i = 0


def call(method, **p):
    global _i
    _i += 1
    ws.send(json.dumps({"id": _i, "method": method, "params": p}))
    while True:
        m = json.loads(ws.recv())
        if m.get("id") == _i:
            return m.get("result", {})


def js(e):
    return call("Runtime.evaluate", expression=e,
                returnByValue=True).get("result", {}).get("value")


def send(text):
    esc = json.dumps(text)
    js("(() => { const b=document.getElementById('input-text');"
       f" b.value={esc};"
       " b.dispatchEvent(new Event('input',{bubbles:true}));"
       " document.getElementById('send-btn').click(); })()")


def settle(sec=18):
    prev, q = -1, 0
    for _ in range(sec * 2):
        time.sleep(0.5)
        c = js("(document.getElementById('messages')||{innerText:''})"
               ".innerText.length") or 0
        if c == prev:
            q += 1
            if q >= 6:
                break
        else:
            prev, q = c, 0


def last_text(n=300):
    return (js("(() => { const m=document.getElementById('messages');"
               " return m ? m.innerText.slice(-%d) : ''; })()" % n) or "")


def shot(name):
    d = call("Page.captureScreenshot", format="png")
    open(f"/tmp/{name}.png", "wb").write(base64.b64decode(d["data"]))
    print("shot:", name)


call("Page.enable")

# 1) 盤點絕對值卡（庫存改成 → 出貨方向）
send("北倉無線藍牙耳機庫存改成100")
settle()
txt = last_text()
print("卡1含目標庫存:", "目標庫存" in txt, "| 含盤點調整:", "盤點調整" in txt)
shot("r30_card_stockset")
send("算了")
settle(6)

# 2) 漲到改價卡
send("無線滑鼠漲到300")
settle()
txt = last_text()
print("卡2含單價:", "單價" in txt, "| 含300:", "300" in txt)
shot("r30_card_price_up")
send("算了")
settle(6)

# 3) 調成歧義選單
send("無線滑鼠調成200")
settle()
txt = last_text()
print("卡3含價格:", "價格" in txt, "| 含安全庫存:", "安全庫存" in txt)
shot("r30_card_ambig")
send("算了")
settle(6)
print("DONE")
