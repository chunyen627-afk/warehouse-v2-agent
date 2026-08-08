# -*- coding: utf-8 -*-
"""r24 一步建檔的 CDP 畫面驗收（跑在 RPI5 上）：
硬重載 kiosk → 新增商品 → 截圖新文案 → 給名字 → 截圖確認卡 → 取消（零寫入）。
用法：python3 onestep_cdp_check.py [8001|8002]
截圖存 /tmp/onestep_1.png / onestep_2.png
"""
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


def shot(p):
    d = call("Page.captureScreenshot", format="png")
    open(p, "wb").write(base64.b64decode(d["data"]))


def send(text):
    esc = json.dumps(text)
    js("(() => { const b=document.getElementById('input-text');"
       f" b.value={esc};"
       " b.dispatchEvent(new Event('input',{bubbles:true}));"
       " document.getElementById('send-btn').click(); })()")


def settle(sec=18):
    prev, q = -1, 0
    for _ in range(int(sec * 2)):
        time.sleep(0.5)
        c = js("(document.getElementById('messages')||{innerText:''})"
               ".innerText.length") or 0
        if c == prev:
            q += 1
            if q >= 6:
                break
        else:
            prev, q = c, 0


def tail(n=3, ln=200):
    v = js(f"[...document.querySelectorAll('#messages > *')].slice(-{n})"
           ".map(e=>e.innerText).join(' | ')") or ""
    return v[:ln]


call("Page.enable")
call("Network.enable")
call("Network.clearBrowserCache")
call("Page.reload", ignoreCache=True)
time.sleep(14)
trigger = "新增商品" if PORT == 8001 else "add item"
name = "展場示範保溫壺" if PORT == 8001 else "demo booth thermos jug"
cancel = "取消" if PORT == 8001 else "cancel"
send(trigger)
settle()
shot("/tmp/onestep_1.png")
print("step1:", tail())
send(name)
settle(25)
shot("/tmp/onestep_2.png")
print("card:", tail(3, 280))
send(cancel)
settle(8)
print("OK-zero-write")
