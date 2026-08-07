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


def page_ws(port):
    for t in cdp_pages():
        if f":{port}" in t.get("url", ""):
            return t["webSocketDebuggerUrl"]
    raise SystemExit(f"port {port} 的頁面不在 CDP 裡")


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


def send_text(c, text):
    """填字＋送出；送不出去（輸入框沒清空）最多重按 3 次。
    en r3 實抓：頁面忙碌時 click 無效 → 整句 EMPTY。"""
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
        cnt = msg_count(c)
        if cnt == prev:
            stable += 1
            if stable >= 8:
                break
        else:
            prev, stable = cnt, 0
    time.sleep(0.5)


def new_msgs_text(c, before_cnt, limit=2000):
    return c.js(f"""(() => {{
        const m = [...document.querySelectorAll('#messages > *')].slice({before_cnt});
        return m.map(e => e.innerText || '').join('\\n⸻\\n').slice(0, {limit});
    }})()""") or ""


def main():
    args = sys.argv[1:]
    port, confirm_mode = 8001, False
    while args and args[0].startswith("--"):
        if args[0] == "--port":
            port = int(args[1]); args = args[2:]
        elif args[0] == "--confirm":
            confirm_mode = True; args = args[1:]
        else:
            raise SystemExit(f"unknown flag {args[0]}")
    corpus_path, out_path = args[0], args[1]
    rows = load_corpus(corpus_path)
    lang = "zh" if port == 8001 else "en"
    print(f"語料 {len(rows)} 句，port {port}，confirm={confirm_mode}", flush=True)

    c = CDP(page_ws(port))
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
            c.send("Page.navigate", url=f"https://localhost:{port}/")
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
    print("頁面已硬重載，開跑", flush=True)

    out = open(out_path, "w", encoding="utf-8")
    import re as _re
    for n, (exp, s) in enumerate(rows, 1):
        # 送句前先等前一句**完全**沉澱（en r1 實測：LLM 慢句串流溢到下一句
        # 的擷取窗，回覆整批錯位 → 判定出幻影 ❌）
        prev_cnt, quiet = msg_count(c), 0
        for _ in range(120):
            time.sleep(0.5)
            cur = msg_count(c)
            if cur == prev_cnt:
                quiet += 1
                if quiet >= 10:         # 5s 靜默才算前一句真的結束
                    break
            else:
                prev_cnt, quiet = cur, 0
        before_cnt = msg_count(c)
        if not send_text(c, s):
            print(f"    ⚠️ [{n}] 送出失敗（重試 3 次仍未清空）", flush=True)
        wait_reply(c, before_cnt)
        reply = new_msgs_text(c, before_cnt)
        has_card = bool(c.js(f"""(() => {{
            const m = [...document.querySelectorAll('#messages > *')].slice({before_cnt});
            return m.some(e => e.querySelector &&
                e.querySelector('button.hitl-approve[data-action="item_create"]'));
        }})()"""))
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
            clicked = c.js(f"""(() => {{
                const m = [...document.querySelectorAll('#messages > *')].slice({before_cnt});
                for (let i = m.length - 1; i >= 0; i--) {{
                    const b = m[i].querySelector &&
                        m[i].querySelector('button.hitl-approve[data-action="item_create"]');
                    if (b) {{ b.click(); return true; }}
                }}
                return false;
            }})()""")
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
