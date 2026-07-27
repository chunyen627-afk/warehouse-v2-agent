# -*- coding: utf-8 -*-
"""像訪客一樣操作 kiosk 頁面：在輸入框打字 → 按送出 → 等回答 → 截圖。

用 Chrome DevTools Protocol（launch_warehouse.sh 已開 127.0.0.1:9222）。
與 ws_convo.py 的差別：那支走 WebSocket 看 JSON，這支**看訪客實際看到的
渲染畫面**——「審到畫面」的最後一哩。

用法：python3 drive_kiosk.py "句子1" "句子2" ...
截圖存 /tmp/shot_NN.png
"""
import base64
import json
import sys
import time
import urllib.request

import websockets.sync.client as wsc


def page_ws():
    with urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5) as r:
        for t in json.load(r):
            if t["type"] == "page" and "8002" in t.get("url", ""):
                return t["webSocketDebuggerUrl"]
    raise SystemExit("kiosk page not found on 9222")


class CDP:
    def __init__(self, url):
        self.ws = wsc.connect(url, max_size=None)
        self.i = 0

    def send(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method,
                                 "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.i:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})

    def js(self, expr):
        r = self.send("Runtime.evaluate", expression=expr,
                      returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    def shot(self, path):
        r = self.send("Page.captureScreenshot", format="png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))


def main():
    sents = sys.argv[1:]
    if not sents:
        print("usage: drive_kiosk.py <sentence> ...")
        return
    c = CDP(page_ws())
    c.send("Page.enable")
    c.send("Runtime.enable")

    for n, s in enumerate(sents, 1):
        # 跟訪客一樣：填輸入框 → 觸發 input 事件 → 點送出鈕
        esc = json.dumps(s)
        c.js(f"""(() => {{
            const box = document.getElementById('input-text');
            box.value = {esc};
            box.dispatchEvent(new Event('input', {{bubbles:true}}));
            document.getElementById('send-btn').click();
            return box.value;
        }})()""")
        # 等回答（訊息數不再增加 = 這輪結束）
        prev, stable = -1, 0
        for _ in range(60):
            time.sleep(0.7)
            cnt = c.js("document.querySelectorAll('#messages > *').length") or 0
            if cnt == prev:
                stable += 1
                if stable >= 3:
                    break
            else:
                prev, stable = cnt, 0
        time.sleep(0.6)
        out = f"/tmp/shot_{n:02d}.png"
        c.shot(out)
        # 取最後一則回答的純文字，方便對照畫面
        txt = c.js("""(() => {
            const m = document.querySelectorAll('#messages > *');
            if (!m.length) return '';
            return (m[m.length-1].innerText || '').slice(0, 300);
        })()""")
        print(f"--- [{n}] {s}")
        print((txt or "(empty)").replace("\n", "\n    "))
        print(f"    -> {out}")


main()
