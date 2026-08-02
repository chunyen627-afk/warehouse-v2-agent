#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""click_probe.py — 前端互動實測：真的用滑鼠點（2026-08-02）。

今天所有測試都是**送文字進 WS**，但展場訪客最常做的是**點按鈕**：
  12 顆快捷鈕／clarify 選單／確認卡的「確認/取消」
這條路徑今天完全沒碰過，而且今天改了 `_en_admin`（本輪觸發 8 次）、
多商品攔截、逗號處理——**快捷鈕送出的文字會經過這些新邏輯**。

走 CDP（launch_warehouse.sh 已開 127.0.0.1:9222），跟訪客同一條路徑。

⚠️ 已知工具坑（記憶 render_testing_method）：
  · 連續點擊之間要**重整頁面**，否則 clarify 選單佔用輸入框
  · 「等回答」不能只看訊息數不變（送出瞬間就成立）→ 要等 loading dots 消失

用法（RPI5）：python3 click_probe.py
"""
import base64
import json
import time
import urllib.request

import websockets.sync.client as wsc

CDP = "http://127.0.0.1:9222"


def page_ws(port="8002"):
    tabs = json.load(urllib.request.urlopen(f"{CDP}/json", timeout=5))
    for t in tabs:
        if t["type"] == "page" and port in t.get("url", ""):
            return t["webSocketDebuggerUrl"]
    raise RuntimeError(f"找不到 {port} 的頁面")


class Page:
    def __init__(self, port="8002"):
        self.ws = wsc.connect(page_ws(port), max_size=None)
        self.i = 0

    def cmd(self, method, params=None, timeout=30):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method,
                                 "params": params or {}}))
        t0 = time.time()
        while time.time() - t0 < timeout:
            m = json.loads(self.ws.recv())
            if m.get("id") == mid:
                return m.get("result", {})
        raise TimeoutError(method)

    def js(self, expr, timeout=30):
        r = self.cmd("Runtime.evaluate",
                     {"expression": expr, "returnByValue": True,
                      "awaitPromise": True}, timeout)
        return (r.get("result") or {}).get("value")

    def reload(self):
        self.cmd("Page.navigate", {"url": self.js("location.href")})
        time.sleep(4)

    def shot(self, path):
        r = self.cmd("Page.captureScreenshot")
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))

    def wait_answer(self, timeout=45):
        """等回答：loading dots 消失且最後一則是 bot 訊息。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            done = self.js(
                "(function(){var m=document.querySelectorAll('.msg');"
                "if(!m.length) return false;"
                "var last=m[m.length-1];"
                "return last.className.indexOf('bot')>=0 && "
                "!last.querySelector('.loading-dots');})()")
            if done:
                time.sleep(0.6)
                return True
            time.sleep(0.8)
        return False

    def last_bot(self):
        return self.js(
            "(function(){var m=document.querySelectorAll('.msg.bot');"
            "return m.length? m[m.length-1].innerText.slice(0,150):'';})()") or ""

    def msg_count(self):
        return self.js("document.querySelectorAll('.msg').length") or 0


def list_chips(p):
    """取頂部快捷鈕的文字（訪客看得到的那排）。"""
    # ⚠️ 必須排除 SCRIPT/STYLE 且只收**可見**元素——
    #   初版把 <script> 裡的文案字串（隨機推薦句的候選清單）當成按鈕抓進來，
    #   報「點不到」的假破口（實際頁面上沒有那顆按鈕）。
    return p.js(
        "Array.from(document.querySelectorAll('.chip,.quick-btn,[data-q]'))"
        ".filter(e=>e.offsetParent!==null && e.tagName!=='SCRIPT'"
        " && e.tagName!=='STYLE')"
        ".map(e=>(e.innerText||'').trim()).filter(t=>t.length>1&&t.length<44)"
    ) or []


def click_by_text(p, text):
    """依顯示文字點擊按鈕，回傳是否點到。"""
    return p.js(
        "(function(){var t=%s;"
        "var els=Array.from(document.querySelectorAll('button,.chip,.quick-btn,[data-q],a'));"
        "var el=els.find(e=>(e.innerText||'').trim()===t);"
        "if(!el) return false; el.click(); return true;})()"
        % json.dumps(text))


def main():
    p = Page()
    print("=" * 92)
    print("前端互動實測（真的用滑鼠點）")
    print("=" * 92)

    p.reload()
    chips = list_chips(p)
    print(f"偵測到 {len(chips)} 顆可點按鈕：")
    for c in chips[:16]:
        print(f"  · {c}")
    print()

    ok = bad = 0
    for i, label in enumerate(chips[:12], 1):
        p.reload()
        before = p.msg_count()
        if not click_by_text(p, label):
            print(f"  ⚠️ [{i}] {label[:30]:<32} 點不到（可能是裝飾元素）")
            continue
        if not p.wait_answer():
            bad += 1
            print(f"  ❌ [{i}] {label[:30]:<32} 逾時無回應")
            p.shot(f"/tmp/click_{i}.png")
            continue
        after = p.msg_count()
        ans = p.last_bot().replace("\n", " ")
        bad_signal = (after <= before or not ans.strip()
                      or "error" in ans.lower()[:40])
        if bad_signal:
            bad += 1
            print(f"  ❌ [{i}] {label[:30]:<32} {ans[:44]}")
            p.shot(f"/tmp/click_{i}.png")
        else:
            ok += 1
            print(f"  ✅ [{i}] {label[:30]:<32} {ans[:44]}")

    print()
    print("=" * 92)
    print(f"快捷鈕 {ok + bad} 顆：正常 {ok}、異常 {bad}")
    print("=" * 92)


if __name__ == "__main__":
    main()
