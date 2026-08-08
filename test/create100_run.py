# -*- coding: utf-8 -*-
"""建檔 100 句批次測試（CDP 版）——像訪客一樣打字送出，讀畫面上的回覆。

r23c（user 定調「要真的建立起來、然後真的可以查詢」）：
  * `--confirm`：出確認卡就**真的按 ✅ 確認新增** → 等 item_created →
    再發「Ｘ庫存」查詢驗證真的查得到（端到端含寫入路）。
  * 整輪跑完用 POST /api/reset_demo（密碼 0000）一鍵還原 baseline →
    同一份語料每輪可重跑、機器不留測試垃圾（守衛也靠這個基準隔離）。
  * 不帶 --confirm = 舊行為（零寫入，只判解析/分類）。

其他要點：
  * 開跑前 Network.clearBrowserCache + Page.reload(ignoreCache)
    —— kiosk 頁面是舊快取，普通 reload 無效（交接雷點）
  * 句間偵測到分步流程 → 自動送「取消」重置（防連環吞，首輪實測 40 句陪葬）

用法（在 RPI5 上）：
    python3 create100_run.py --port 8001 --confirm create100_zh.txt /tmp/out.jsonl
"""
import json
import sys
import time
import urllib.request

import websockets.sync.client as wsc


def cdp_pages():
    with urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5) as r:
        return [t for t in json.load(r) if t["type"] == "page"]


def _browser_call(method, **params):
    with urllib.request.urlopen("http://127.0.0.1:9222/json/version",
                                timeout=5) as r:
        burl = json.load(r)["webSocketDebuggerUrl"]
    bws = wsc.connect(burl, max_size=None)
    bws.send(json.dumps({"id": 1, "method": method, "params": params}))
    while True:
        m = json.loads(bws.recv())
        if m.get("id") == 1:
            bws.close()
            return m.get("result", {})


def page_ws(port, fast=False):
    """找目標分頁；fast=True 用/開一個 ?fast=1 分頁（打字動畫 0，
    同 code path 只省等待——en r5 實測 8ms/字 × 大卡片讓每互動 30-60s，
    批次測試不開 fast 撐不完）。kiosk 本頁不動，訪客畫面不受影響。"""
    want = "fast=1" if fast else None
    for t in cdp_pages():
        u = t.get("url", "")
        if f":{port}" in u and ((want in u) if want else ("fast=1" not in u)):
            if fast:
                # ⚠️ 背景分頁會被 Chromium 節流/凍結：timers 停擺 → WS 斷了
                #   reconnect 永不觸發，批次打進黑洞（r7/r8 實抓：5 分鐘後
                #   伺服器再沒收到任何 User）。帶到前景才能長跑。
                _browser_call("Target.activateTarget", targetId=t["id"])
            return t["webSocketDebuggerUrl"]
    if not fast:
        raise SystemExit(f"port {port} 的頁面不在 CDP 裡")
    # 開新分頁（Target.createTarget；不可從 SSH 起 chromium——缺 X11）
    with urllib.request.urlopen("http://127.0.0.1:9222/json/version",
                                timeout=5) as r:
        burl = json.load(r)["webSocketDebuggerUrl"]
    bws = wsc.connect(burl, max_size=None)
    bws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                         "params": {"url": f"https://localhost:{port}/?fast=1"}}))
    while True:
        m = json.loads(bws.recv())
        if m.get("id") == 1:
            break
    bws.close()
    for _ in range(30):
        time.sleep(2)
        for t in cdp_pages():
            if f":{port}" in t.get("url", "") and "fast=1" in t.get("url", ""):
                _browser_call("Target.activateTarget", targetId=t["id"])
                return t["webSocketDebuggerUrl"]
    raise SystemExit(f"開 {port} fast 分頁逾時")


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


def load_corpus(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            exp, _, sent = ln.partition("|")
            rows.append((exp.strip(), sent.strip()))
    return rows


def msg_count(c):
    return c.js("document.querySelectorAll('#messages > *').length") or 0


def page_sig(c):
    """畫面簽章 = 訊息數×1e7 + 全文字長。⚠️ 只看訊息數會踩 race：
    token 串流塞在同一顆泡泡裡、訊息數不動，推論靜默期就被誤判「講完了」
    （r6 實抓：擷取到半句 + 卡片還沒渲染）。字長每個 token 都會動。"""
    return c.js("(document.querySelectorAll('#messages > *').length * 10000000)"
                " + ((document.getElementById('messages')||{innerText:''})"
                ".innerText||'').length") or 0


def send_text(c, text):
    """填字＋送出；送不出去（輸入框沒清空）最多重按 3 次。
    en r3 實抓：頁面忙碌時 click 無效 → 整句 EMPTY。
    en r4b 實抓：**送出鈕卡 disabled**（done frame 沒到就永久鎖）→ 先等
    解鎖最多 60s，仍鎖住就強制解鎖（並回報 stuck，這是前端看門狗缺口）。"""
    for _ in range(90):                     # 等前端 busy 解除（最多 45s）
        if not c.js("document.getElementById('send-btn').disabled"):
            break
        time.sleep(0.5)
    if c.js("document.getElementById('send-btn').disabled"):
        print("    ⚠️ 送出鈕卡 disabled 45s → setSending(false) 強制復位",
              flush=True)
        # ⚠️ 只解 disabled 不夠——onclick 還會擋 isSending 旗標（r4c 實抓）
        c.js("try { setSending(false); } catch(e) { "
             "document.getElementById('send-btn').disabled = false; }")
    esc = json.dumps(text)
    for _try in range(3):
        c.js(f"""(() => {{
            const box = document.getElementById('input-text');
            box.value = {esc};
            box.dispatchEvent(new Event('input', {{bubbles:true}}));
            document.getElementById('send-btn').click();
        }})()""")
        for _ in range(12):
            time.sleep(0.5)
            if not (c.js("document.getElementById('input-text').value") or ""):
                return True
    return False


def wait_reply(c, before_cnt, max_stream=80):
    """三段等待（drive_kiosk 教訓）：①開始回覆 ②串流穩定。
    en r3 實抓：LLM prompt eval 有 >5s 的**靜默段**（沒有任何訊息），
    穩定門檻太短會提早收工、殘訊溢到下一句 → 靜默要 5.6s 才算完。"""
    for _ in range(80):
        time.sleep(0.5)
        if msg_count(c) > before_cnt:
            break
    prev, stable = -1, 0
    for _ in range(max_stream):
        time.sleep(0.7)
        sig = page_sig(c)
        if sig == prev:
            stable += 1
            if stable >= 8:
                break
        else:
            prev, stable = sig, 0
    time.sleep(0.5)


def new_msgs_text(c, before_cnt, limit=2000):
    return c.js(f"""(() => {{
        const m = [...document.querySelectorAll('#messages > *')].slice({before_cnt});
        return m.map(e => e.innerText || '').join('\\n⸻\\n').slice(0, {limit});
    }})()""") or ""


def main():
    args = sys.argv[1:]
    port, confirm_mode, fast = 8001, False, False
    while args and args[0].startswith("--"):
        if args[0] == "--port":
            port = int(args[1]); args = args[2:]
        elif args[0] == "--confirm":
            confirm_mode = True; args = args[1:]
        elif args[0] == "--fast":
            fast = True; args = args[1:]
        else:
            raise SystemExit(f"unknown flag {args[0]}")
    corpus_path, out_path = args[0], args[1]
    rows = load_corpus(corpus_path)
    lang = "zh" if port == 8001 else "en"
    print(f"語料 {len(rows)} 句，port {port}，confirm={confirm_mode}，fast={fast}",
          flush=True)

    # 關動態模擬要在**載頁之前**——模擬吃滿事件圈時 index 頁根本載不進
    # （r8 實抓：關模擬步驟原在載頁後，雞生蛋、三次重試全逾時）
    try:
        import ssl as _ssl
        _ctx0 = _ssl.create_default_context()
        _ctx0.check_hostname = False
        _ctx0.verify_mode = _ssl.CERT_NONE
        _rq0 = urllib.request.Request(
            f"https://localhost:{port}/api/live_mode",
            data=json.dumps({"action": "stop"}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(_rq0, context=_ctx0, timeout=30)
        print("live 模擬已關（載頁前）", flush=True)
    except Exception as _e0:
        print(f"live 模擬預關失敗（{_e0!r}），照跑", flush=True)

    c = CDP(page_ws(port, fast=fast))
    c.send("Page.enable")
    c.send("Runtime.enable")
    c.send("Network.enable")
    c.send("Network.clearBrowserCache")
    # ⚠️ 硬重載可能撞上伺服器忙碌 → Chrome 錯誤頁（ERR_TIMED_OUT），
    #   en r4 實抓：整批 100 句打在錯誤頁上全空。重載後**必須驗到輸入框**，
    #   驗不到就重新導航重試；三次都失敗直接停，不能帶病開跑。
    _page_ok = False
    for _attempt in range(3):
        if _attempt == 0:
            c.send("Page.reload", ignoreCache=True)
        else:
            print(f"頁面載入失敗，第 {_attempt+1} 次重新導航…", flush=True)
            c.send("Page.navigate",
                   url=f"https://localhost:{port}/" + ("?fast=1" if fast else ""))
        for _ in range(60):
            time.sleep(1)
            try:
                if c.js("!!document.getElementById('input-text') && "
                        "!!document.getElementById('send-btn')"):
                    _page_ok = True
                    break
            except Exception:
                pass
        if _page_ok:
            break
    if not _page_ok:
        raise SystemExit("頁面三次都載不出輸入框，中止（檢查 server/health）")
    time.sleep(8)
    # 關動態模擬（live_mode docstring：跑測試前務必關）——它持續灌 movements
    # 會讓 en 的 len(movements) 快取鍵永遠失效 → 每查全掃 30s（r7 實抓）
    try:
        import ssl as _ssl
        _ctx = _ssl.create_default_context()
        _ctx.check_hostname = False
        _ctx.verify_mode = _ssl.CERT_NONE
        _rq = urllib.request.Request(
            f"https://localhost:{port}/api/live_mode",
            data=json.dumps({"action": "stop"}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(_rq, context=_ctx, timeout=10)
        print("live 模擬已關（測試模式）", flush=True)
    except Exception as _e_lv:
        print(f"live 模擬關閉失敗（{_e_lv!r}），照跑", flush=True)
    print("頁面已硬重載，開跑", flush=True)

    out = open(out_path, "w", encoding="utf-8")
    import re as _re
    for n, (exp, s) in enumerate(rows, 1):
        # 送句前先等前一句**完全**沉澱（en r1 實測：LLM 慢句串流溢到下一句
        # 的擷取窗，回覆整批錯位 → 判定出幻影 ❌）
        prev_sig, quiet = page_sig(c), 0
        for _ in range(120):
            time.sleep(0.5)
            cur = page_sig(c)
            if cur == prev_sig:
                quiet += 1
                if quiet >= 10:         # 5s 靜默才算前一句真的結束
                    break
            else:
                prev_sig, quiet = cur, 0
        # 逐句 WS 健康檢查：斷線的送出會**靜默蒸發**（r8b 實抓：WS 死了
        # 兩小時、73 句全打黑洞）→ 斷了就重載頁面重連再送
        if not c.js("typeof ws !== 'undefined' && !!ws && ws.readyState === 1"):
            print(f"    ⚠️ [{n}] 前端 WS 斷線 → 重載頁面重連", flush=True)
            c.send("Page.navigate",
                   url=f"https://localhost:{port}/" + ("?fast=1" if fast else ""))
            for _ in range(60):
                time.sleep(1)
                try:
                    if c.js("!!document.getElementById('input-text') && "
                            "typeof ws !== 'undefined' && !!ws && ws.readyState === 1"):
                        break
                except Exception:
                    pass
            time.sleep(5)
        before_cnt = msg_count(c)
        if not send_text(c, s):
            print(f"    ⚠️ [{n}] 送出失敗（重試 3 次仍未清空）", flush=True)
        wait_reply(c, before_cnt)
        reply = new_msgs_text(c, before_cnt)
        # r10：slice(before_cnt) 有邊界 race（卡片文字在、按鈕判不在）→
        #   改掃**最後 8 則**訊息找按鈕（上一句的卡按過就消失，不會誤按舊卡）
        has_card = bool(c.js("""(() => {
            const m = [...document.querySelectorAll('#messages > *')].slice(-8);
            return m.some(e => e.querySelector &&
                e.querySelector('button.hitl-approve[data-action="item_create"]'));
        })()"""))
        created, query_ok, create_reply, query_reply, item_name = False, None, "", "", ""
        # 名稱從確認卡抽（查詢驗證要用）
        if lang == "zh":
            nm = _re.search(r'名稱\s*\t?\s*([^\n\t]+)', reply)
        else:
            nm = _re.search(r'Name\s*\t?\s*([^\n\t]+)', reply, _re.I)
        if nm:
            item_name = nm.group(1).strip()
        if confirm_mode and has_card:
            b2 = msg_count(c)
            clicked = c.js("""(() => {
                const m = [...document.querySelectorAll('#messages > *')].slice(-8);
                for (let i = m.length - 1; i >= 0; i--) {
                    const b = m[i].querySelector &&
                        m[i].querySelector('button.hitl-approve[data-action="item_create"]');
                    if (b) { b.click(); return true; }
                }
                return false;
            })()""")
            if clicked:
                wait_reply(c, b2, max_stream=40)
                create_reply = new_msgs_text(c, b2, 600)
                created = ("已新增" in create_reply or "已建立" in create_reply
                           or "added" in create_reply.lower()
                           or "created" in create_reply.lower()
                           or "item_created" in create_reply)
                # 真查詢驗證：問庫存，**輪詢到回覆裡出現商品名**才收工
                #   （en r3 實抓：只等「訊息數穩定」會在 LLM 靜默段提早收工，
                #     查詢結果晚到溢進下一句的窗，整批錯位一格）
                if created and item_name:
                    q = (f"{item_name}庫存" if lang == "zh"
                         else f"{item_name} stock")
                    b3 = msg_count(c)
                    send_text(c, q)
                    for _ in range(120):        # 最多 60s
                        time.sleep(0.5)
                        query_reply = new_msgs_text(c, b3, 600)
                        if item_name in query_reply:
                            break
                    query_ok = item_name in query_reply
                    wait_reply(c, b3, max_stream=30)   # 等它渲染完再進下一句
                    query_reply = new_msgs_text(c, b3, 600)
        rec = {"n": n, "expected": exp, "sent": s, "has_card": has_card,
               "item_name": item_name, "created": created,
               "query_ok": query_ok, "reply": reply,
               "create_reply": create_reply, "query_reply": query_reply}
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()
        print(f"[{n}/{len(rows)}] {exp} | {s[:24]} | card={has_card}"
              f" created={created} q={query_ok}", flush=True)
        # 分步流程殘留 → 送取消重置
        flow_markers = ("步驟 1/4", "步驟 2/4", "步驟 3/4", "步驟 4/4",
                        "Step 1/4", "Step 2/4", "Step 3/4", "Step 4/4")
        if any(m in reply for m in flow_markers) and not has_card:
            cancel_word = "取消" if lang == "zh" else "cancel"
            b4 = msg_count(c)
            send_text(c, cancel_word)
            for _ in range(40):
                time.sleep(0.5)
                if msg_count(c) > b4:
                    break
            time.sleep(1.5)
            print(f"    ↳ 流程中，已送{cancel_word}重置", flush=True)
    out.close()
    print("DONE", flush=True)


main()
