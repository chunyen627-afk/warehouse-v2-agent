# -*- coding: utf-8 -*-
"""補拍確認卡與完成卡（前版點擊 selector 沒中——en2/en3 還是選單）。

改廣域 selector（全頁 button + [onclick]），點擊後**驗證狀態變化**再截圖，
點不到就吐最後一則訊息的 DOM 片段供診斷（不再靜默失敗）。
"""
import base64
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

    def shot(name):
        js("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.4)
        r = cmd("Page.captureScreenshot")
        open(f"/tmp/{name}.png", "wb").write(base64.b64decode(r.get("data", "")))
        print(f"  📸 {name}.png")

    def wait(cond, timeout=45):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if js(cond):
                return True
            time.sleep(0.7)
        return False

    cmd("Page.enable")
    cmd("Page.navigate", {"url": "https://localhost:8002/"})
    wait("typeof sendQuery==='function'", 30)
    time.sleep(4)

    # 選單
    js("sendQuery('export the movement log')")
    wait("document.body.innerText.includes('Which period')")
    time.sleep(1)

    # 點 Last week（廣域：button 或帶 onclick 的元素）
    clicked = js(
        "(function(){var els=Array.from(document.querySelectorAll("
        "'button,[onclick],.clarify-opt,.opt'));"
        "var b=els.filter(x=>/last week/i.test(x.textContent||'')"
        "&&x.offsetParent!==null).pop();"
        "if(b){b.click();return b.tagName+'.'+b.className;}return null;})()")
    print("點選項:", clicked)
    if not clicked:
        print("DOM 片段:", (js(
            "(function(){var m=document.querySelectorAll('.msg');"
            "return m.length?m[m.length-1].innerHTML.slice(0,500):'';})()")
            or "")[:400])
    ok = wait("!!document.querySelector('.hitl-approve')", 30)
    time.sleep(1)
    shot("en2_confirm_card")
    if not ok:
        print("⚠️ 確認卡未出現")

    # 按授權執行 → 完成卡
    ap = js("(function(){var b=Array.from(document.querySelectorAll("
            "'.hitl-approve')).filter(x=>x.offsetParent!==null).pop();"
            "if(b){b.click();return true;}return false;})()")
    print("按授權:", ap)
    wait("/completed|Download CSV|Open report/i.test(document.body.innerText)", 60)
    time.sleep(2)
    shot("en3_done_card")
    ws.close()
finally:
    http(f"/json/close/{tid}")
    print("分頁已關閉")
