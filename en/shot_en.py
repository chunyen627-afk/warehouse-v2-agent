# -*- coding: utf-8 -*-
"""shot_en.py — 英文版新功能像素級截圖（2026-08-04，渲染到底最後一哩）。

⚠️ kiosk 前景是中文 demo,**不可動它** ⇒ CDP 開**新分頁**連 8002,
   截完關閉分頁,展示畫面零影響。
截 4 張：①匯出期間選單 ②匯出確認卡（含 days）③confirm 後的完成卡
（開啟報告/下載按鈕要在）④採購單草稿卡。
手法沿用 click_probe.py（同一台 Chromium,Page.captureScreenshot）。
"""
import base64
import io
import json
import sys
import time
import urllib.request

import websockets.sync.client as wsc  # click_probe 同款依賴

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
CDP = "http://127.0.0.1:9222"


def http(path, method="GET"):
    req = urllib.request.Request(CDP + path, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
        try:
            return json.loads(raw)
        except Exception:
            return raw.decode("utf-8", "ignore")   # /json/close 回純文字


class Tab:
    def __init__(self, ws_url):
        self.ws = wsc.connect(ws_url, open_timeout=30, max_size=None)
        self.mid = 0

    def cmd(self, method, params=None):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method,
                                 "params": params or {}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.mid:
                return m.get("result") or {}

    def js(self, expr):
        r = self.cmd("Runtime.evaluate",
                     {"expression": expr, "returnByValue": True})
        return (r.get("result") or {}).get("value")

    def wait_stable(self, timeout=45):
        """等最後一則 bot 訊息出現且無 loading-dots。"""
        t0, last = time.time(), -1
        while time.time() - t0 < timeout:
            n = self.js("document.querySelectorAll('.msg').length") or 0
            busy = self.js(
                "(function(){var m=document.querySelectorAll('.msg');"
                "if(!m.length)return true;var last=m[m.length-1];"
                "return !!last.querySelector('.loading-dots');})()")
            if n > last and not busy:
                time.sleep(1.2)
                busy2 = self.js(
                    "(function(){var m=document.querySelectorAll('.msg');"
                    "var last=m[m.length-1];"
                    "return !!last.querySelector('.loading-dots');})()")
                if not busy2:
                    return True
            last = max(last, n)
            time.sleep(0.6)
        return False

    def shot(self, name):
        self.js("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.4)
        r = self.cmd("Page.captureScreenshot")
        open(f"/tmp/{name}.png", "wb").write(
            base64.b64decode(r.get("data", "")))
        print(f"  📸 {name}.png")


def main():
    tab_info = http("/json/new?url=https://localhost:8002/", "PUT")
    tid = tab_info["id"]
    try:
        tab = Tab(tab_info["webSocketDebuggerUrl"])
        tab.cmd("Page.enable")
        # ⚠️ 這版 Chromium 的 /json/new?url= 參數被忽略（實測停在 about:blank）
        #   → 用 CDP Page.navigate 導航,並等 sendQuery 就緒
        tab.cmd("Page.navigate", {"url": "https://localhost:8002/"})
        for _ in range(30):
            if tab.js("typeof sendQuery") == "function":
                break
            time.sleep(1)
        else:
            print("⚠️ sendQuery 未就緒:", tab.js("location.href"),
                  tab.js("document.title"))
        time.sleep(4)   # 等頁面連 WS + 能力地圖
        # ① 期間選單
        tab.js("sendQuery('export the movement log')")
        tab.wait_stable()
        tab.shot("en1_export_menu")
        # ② 點選單第 2 選項（Last week）→ 確認卡
        tab.js("(function(){var bs=Array.from(document.querySelectorAll("
               "'.msg.bot button'));var b=bs.reverse().find(x=>/last week/i"
               ".test(x.textContent));if(b)b.click();})()")
        tab.wait_stable()
        tab.shot("en2_confirm_card")
        # ③ 按授權執行 → 完成卡（開啟報告/下載按鈕）
        tab.js("(function(){var b=Array.from(document.querySelectorAll("
               "'.hitl-approve')).pop();if(b)b.click();})()")
        tab.wait_stable(60)
        time.sleep(2)
        tab.shot("en3_done_card")
        # ④ 採購單卡
        tab.js("sendQuery('create a purchase order for low stock items')")
        tab.wait_stable(60)
        tab.shot("en4_po_card")
        tab.ws.close()
    finally:
        http(f"/json/close/{tid}")
        print("分頁已關閉，kiosk 不受影響")


main()
