# -*- coding: utf-8 -*-
"""r28d CDP 驗收：卡上改類別下拉 → SKU 顯示即時更新、快選籤已拆。零寫入。"""
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


call("Page.enable")
call("Network.enable")
call("Network.clearBrowserCache")
call("Page.reload", ignoreCache=True)
time.sleep(14)
send("新增下拉終驗品" if PORT == 8001 else "add item dropdown final probe")
settle()
print("sku-before:", js("(document.querySelector('.ed-sku')||{textContent:''}).textContent"))
js("(() => { const s=document.querySelector('.ed-cat');"
   " if (!s) return; s.value='industrial';"
   " onCardCatChange(s); })()")
time.sleep(1)
print("sku-after :", js("(document.querySelector('.ed-sku')||{textContent:''}).textContent"))
print("chips:", js("document.querySelectorAll('.sug').length"))
d = call("Page.captureScreenshot", format="png")
open("/tmp/r28d_card.png", "wb").write(base64.b64decode(d["data"]))
send("取消" if PORT == 8001 else "cancel")
settle(6)
print("OK")
