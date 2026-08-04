# -*- coding: utf-8 -*-
"""PO 卡 WH 欄 label 驗證（DOM 文字級，兩版）。CDP 新分頁，kiosk 不動。"""
import io
import json
import sys
import time
import urllib.request

import websockets.sync.client as wsc

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
CDP = "http://127.0.0.1:9222"


def http(path, method="GET"):
    req = urllib.request.Request(CDP + path, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
        try:
            return json.loads(raw)
        except Exception:
            return raw.decode("utf-8", "ignore")


def check(url, query, name):
    t = http("/json/new?url=about:blank", "PUT")
    tid = t["id"]
    try:
        ws = wsc.connect(t["webSocketDebuggerUrl"], open_timeout=30, max_size=None)
        mid = [0]

        def cmd(m, p=None):
            mid[0] += 1
            ws.send(json.dumps({"id": mid[0], "method": m, "params": p or {}}))
            while True:
                r = json.loads(ws.recv())
                if r.get("id") == mid[0]:
                    return r.get("result") or {}

        def js(e):
            r = cmd("Runtime.evaluate", {"expression": e, "returnByValue": True})
            return (r.get("result") or {}).get("value")

        cmd("Page.enable")
        cmd("Page.navigate", {"url": url})
        for _ in range(30):
            if js("typeof sendQuery==='function'"):
                break
            time.sleep(1)
        time.sleep(4)
        js(f"sendQuery({json.dumps(query)})")
        for _ in range(60):
            if js("!!document.querySelector('.hitl-card table')"):
                break
            time.sleep(1)
        time.sleep(1)
        cells = js(
            "Array.from(document.querySelectorAll("
            "'.hitl-card table tr')).slice(0,3).map("
            "tr=>Array.from(tr.querySelectorAll('td,th')).map("
            "c=>c.textContent.trim()).join('|'))") or []
        print(f"== {name}")
        for c in cells:
            print("   ", c[:90])
        ws.close()
    finally:
        http(f"/json/close/{tid}")


check("https://localhost:8001/", "幫我把缺貨的產採購單", "ZH PO 卡")
check("https://localhost:8002/", "create a purchase order for low stock items",
      "EN PO 卡")
print("分頁已關閉")
