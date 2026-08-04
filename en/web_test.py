#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web_test.py — 從網頁實際輸入測試（2026-08-04，第二台驗收用）。

走 CDP 操作 kiosk 裡的 Chromium——**跟訪客同一條路徑**
（打字進輸入框 → 按送出 → 看渲染出來的畫面），
不是送 WS/HTTP 那種繞過前端的測法。

⚠️ 已知工具坑：
  · 連續送句之間要**重整頁面**，否則 clarify 選單佔用輸入框
  · 「等回答」不能只看訊息數（送出瞬間就變）→ 要等 loading dots 消失

用法：python3 web_test.py [port]   預設 8002（英文）
"""
import base64
import json
import sys
import time
import urllib.request

import websockets.sync.client as wsc


class Page:
    def __init__(self, port="8002"):
        tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json",
                                                timeout=5))
        url = None
        for t in tabs:
            if t["type"] == "page" and port in t.get("url", ""):
                url = t["webSocketDebuggerUrl"]
                break
        if not url:   # 分頁被導到 about:blank 時，抓任一分頁再導回去
            for t in tabs:
                if t["type"] == "page":
                    url = t["webSocketDebuggerUrl"]
                    break
        if not url:
            raise RuntimeError("找不到任何分頁")
        self.ws = wsc.connect(url, max_size=None)
        self.url = f"https://localhost:{port}/"
        self.i = 0

    def cmd(self, method, params=None, timeout=40):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method,
                                 "params": params or {}}))
        # ⚠️ Chromium 150 起 CDP 會夾雜大量**事件訊息**（沒有 id），
        #   `ws.recv()` 沒有逾時保護時會在事件流裡永久等下去（踩過：整支腳本靜默卡死）。
        #   → 每次 recv 都設短逾時，並跳過非本次請求的訊息。
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                raw = self.ws.recv(timeout=5)
            except TimeoutError:
                continue
            m = json.loads(raw)
            if m.get("id") == mid:
                return m.get("result", {})
        raise TimeoutError(method)

    def js(self, expr):
        r = self.cmd("Runtime.evaluate",
                     {"expression": expr, "returnByValue": True,
                      "awaitPromise": True})
        return (r.get("result") or {}).get("value")

    def reload(self, url=None):
        # ⚠️ 不要用 self.js("location.href") 當導航目標——
        #   頁面正在導航時取值會拿到 about:blank，之後就再也找不到分頁了（踩過）。
        #   改成**明確傳入目標 URL**。
        self.cmd("Page.navigate", {"url": url or self.url})
        time.sleep(5)

    def shot(self, path):
        r = self.cmd("Page.captureScreenshot")
        open(path, "wb").write(base64.b64decode(r["data"]))

    def send(self, text):
        """把字打進輸入框並送出（跟訪客一樣）。"""
        ok = self.js(
            "(function(){var i=document.getElementById('input-text');"
            "if(!i) return false; i.value=%s;"
            "i.dispatchEvent(new Event('input',{bubbles:true}));"
            "return true;})()" % json.dumps(text))
        if not ok:
            return False
        self.js(
            "(function(){var i=document.getElementById('input-text');"
            "i.dispatchEvent(new KeyboardEvent('keydown',"
            "{key:'Enter',code:'Enter',keyCode:13,bubbles:true}));})()")
        return True

    def wait_answer(self, timeout=60):
        t0 = time.time()
        while time.time() - t0 < timeout:
            done = self.js(
                "(function(){var m=document.querySelectorAll('.msg');"
                "if(!m.length) return false; var l=m[m.length-1];"
                "return l.className.indexOf('bot')>=0 && "
                "!l.querySelector('.loading-dots');})()")
            if done:
                time.sleep(0.8)
                return True
            time.sleep(1)
        return False

    def last_pair(self):
        return self.js(
            "(function(){var u=document.querySelectorAll('.msg.user'),"
            "b=document.querySelectorAll('.msg.bot');"
            "return [(u.length?u[u.length-1].innerText:''),"
            "(b.length?b[b.length-1].innerText.slice(0,150):'')];})()") or ["", ""]


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "8002"
    cases = (["bluetooth earphones stock", "what is running low",
              "north received 50 wireless mouse", "best sellers this week"]
             if port == "8002" else
             ["藍牙耳機庫存", "哪些快缺貨", "北倉進50個無線滑鼠", "本週熱銷"])

    print("=" * 84)
    print(f"從網頁實際輸入測試（port {port}，跟訪客同一條路徑）")
    print("=" * 84)
    ok = bad = 0
    for i, q in enumerate(cases, 1):
        p = Page(port)
        p.reload()
        if not p.send(q):
            print(f"  ❌ [{i}] {q[:30]} → 找不到輸入框")
            bad += 1
            continue
        if not p.wait_answer():
            print(f"  ❌ [{i}] {q[:30]} → 逾時無回應")
            p.shot(f"/tmp/web_{port}_{i}.png")
            bad += 1
            continue
        u, b = p.last_pair()
        b1 = b.replace("\n", " ")[:70]
        if not b1.strip():
            print(f"  ❌ [{i}] {q[:30]} → 空回答")
            bad += 1
        else:
            print(f"  ✅ [{i}] 訪客輸入：{u[:34]}")
            print(f"        系統回答：{b1}")
            ok += 1
        p.shot(f"/tmp/web_{port}_{i}.png")

    print()
    print("=" * 84)
    print(f"網頁實測 {ok + bad} 句：正常 {ok}、異常 {bad}")
    print("=" * 84)


if __name__ == "__main__":
    main()
