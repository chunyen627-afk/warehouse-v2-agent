# -*- coding: utf-8 -*-
"""r28c 卡上編輯的 CDP 驗收（RPI5 上跑）：
建檔 → 卡上改 類別下拉/單價/北倉 → 按確認（真寫入）→ 驗 SKU 重發＋查詢對數。
用法：python3 cardedit_cdp_check.py [8001|8002]
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


def settle(sec=20):
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

name = "卡改驗證品" if PORT == 8001 else "card edit probe item"
send(("新增商品" if PORT == 8001 else "add item ") + name)
settle()
# 卡上改值：類別→玩具、單價→777、北倉→66
edited = js("""(() => {
  const cards = [...document.querySelectorAll('.hitl-card')];
  const c = cards[cards.length - 1];
  if (!c) return 'no-card';
  const sel = c.querySelector('.ed-cat');
  const p = c.querySelector('.ed-price');
  const n = c.querySelector('.ed-n');
  if (!sel || !p || !n) return 'no-fields';
  sel.value = 'toys';
  p.value = 777;
  n.value = 66;
  return 'edited';
})()""")
print("edit:", edited)
shot("/tmp/cardedit_1.png")
b = js("document.querySelectorAll('#messages > *').length") or 0
js("""(() => {
  const cards = [...document.querySelectorAll('.hitl-card')];
  const c = cards[cards.length - 1];
  const btn = c && c.querySelector('button.hitl-approve[data-action="item_create"]');
  if (btn) btn.click();
})()""")
settle(30)
print("created:", (js("[...document.querySelectorAll('#messages > *')]"
                      ".slice(-2).map(e=>e.innerText).join('|')") or "")[:120])
send((name + "庫存") if PORT == 8001 else (name + " stock"))
settle(25)
print("query:", (js("[...document.querySelectorAll('#messages > *')]"
                    ".slice(-2).map(e=>e.innerText).join('|')") or "")[:200])
send((name + "多少錢") if PORT == 8001 else (name + " price"))
settle(20)
print("price:", (js("[...document.querySelectorAll('#messages > *')]"
                    ".slice(-1).map(e=>e.innerText).join('|')") or "")[:120])
shot("/tmp/cardedit_2.png")
print("DONE")
