"""ws_inspect.py — 「看得到回答」的 WS 巡檢工具。

regression_ws.py 只吐 view 和通過率，看不到實際回答文字，導致「看不懂要查/改
哪個設定項」這種醜回答只有畫面上看得到。這支對每句印出：
    view + 回答全文（summary 或串接 tokens）+ 延遲 + tok/s + mode
並自動把「可疑回答」標 ⚠️（error / rejected / 含看不懂聽不懂 / 空回答），
讓 review 回答品質時一眼抓到破口，不再東漏西漏。

用法：
    # 本機 WIN11 (ws://localhost:8000)
    python ws_inspect.py

    # RPI5 (wss://localhost:8001)，在 RPI5 上跑
    python ws_inspect.py --rpi5

    # 只測自己給的句子（逗號分隔或多個參數）
    python ws_inspect.py "缺的東西大概要補多少" "隨便看看最近哪個賣最兇"

    # 從檔案讀（每行一句，# 註解；支援「類別|句子」格式，類別會被忽略只看回答）
    python ws_inspect.py --file mysents.txt

    # 跨句劇情批：全部句子走同一條 WS 連線（context 會延續，像真訪客連續輸入）
    python ws_inspect.py --file _conv100_r55.txt --session
"""
import asyncio, json, ssl, sys, io, time, argparse
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import websockets

# 預設一批「正經 × 非正經」代表句，沒給句子時就跑這批
DEFAULT_SENTS = [
    "無線滑鼠還剩幾個",
    "欸不好意思想問一下無線滑鼠還剩幾個",
    "把智慧手環安全庫存設成80",
    "順便問一下USB風扇還有嗎",
    "缺的東西大概要補多少",
    "有哪些是要趕快補的",
    "隨便看看最近哪個賣最兇",
    "怪了電動牙刷的帳怎麼兜不起來",
    "哪些商品快缺貨了",
    "本月熱銷前十",
    "哈囉你好嗎",
    "把全部庫存歸零",
]

# 可疑回答的訊號：命中就標 ⚠️ 讓人回頭看
SUSPICIOUS_VIEWS = {"error"}
SUSPICIOUS_TEXT = ("看不懂", "聽不懂", "我不太理解", "無法理解", "不知道你",
                   "請再說一次", "哪個設定項", "我不確定")


def looks_bad(view: str, text: str) -> str:
    """回傳警示原因字串，沒問題則回空字串。"""
    if view in SUSPICIOUS_VIEWS:
        return f"view={view}"
    for s in SUSPICIOUS_TEXT:
        if s in text:
            return f"含「{s}」"
    if not text and view in ("rejected", "guide", "clarify"):
        return ""  # 這些 view 本來可能沒 summary，不算破口
    if not text:
        return "空回答"
    return ""


async def _ask_on(ws, text, timeout=45):
    t0 = time.perf_counter()
    toks, summary, view, mode, tps, tok = [], "", "?", "?", 0.0, 0
    await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        t = m.get("type")
        if t == "token":
            toks.append(m.get("text", ""))
        elif t == "perf":
            mode = m.get("mode", mode)
            if m.get("tps", 0): tps = m["tps"]
            if m.get("tok", 0): tok = m["tok"]
        elif t == "error":
            # error frame 沒有後續 done——別等到 timeout（r56）
            view = "error"
            summary = m.get("text", "") or ""
            break
        elif t == "done":
            r = m.get("result", {}) or {}
            view = r.get("view", "?")
            summary = r.get("summary", "") or ""
            break
    lat = (time.perf_counter() - t0) * 1000.0
    # 畫面顯示的回答文字：summary 優先，沒有就用串接的 tokens
    answer = summary or "".join(toks)
    return {"view": view, "answer": answer.strip(), "lat": lat,
            "mode": mode, "tps": tps, "tok": tok}


async def ask(uri, ctx, text, timeout=45):
    async with websockets.connect(uri, ssl=ctx, max_size=None) as ws:
        return await _ask_on(ws, text, timeout)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sents", nargs="*", help="要測的句子")
    ap.add_argument("--rpi5", action="store_true", help="連 RPI5 wss://localhost:8001")
    ap.add_argument("--file", help="從檔案讀句子（每行一句）")
    ap.add_argument("--session", action="store_true",
                    help="全部句子共用同一條 WS 連線（跨句劇情批用，context 延續）")
    args = ap.parse_args()

    if args.rpi5:
        uri = "wss://localhost:8001/ws?fast=1"
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    else:
        uri = "ws://localhost:8000/ws?fast=1"; ctx = None

    if args.file:
        sents = []
        for line in open(args.file, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            sents.append(line.split("|", 1)[-1].strip())  # 支援「類別|句子」
    elif args.sents:
        sents = []
        for s in args.sents:
            sents.extend(x.strip() for x in s.split(",") if x.strip())
    else:
        sents = DEFAULT_SENTS

    mode_note = "（--session 單一連線、context 延續）" if args.session else ""
    print(f"\n{'='*74}\nws_inspect → {uri}  共 {len(sents)} 句{mode_note}\n{'='*74}")
    bad = []
    sess_ws = None
    if args.session:
        sess_ws = await websockets.connect(uri, ssl=ctx, max_size=None)
    for i, s in enumerate(sents, 1):
        try:
            if args.session:
                r = await _ask_on(sess_ws, s)
            else:
                r = await ask(uri, ctx, s)
        except Exception as e:
            print(f"\n[{i:3}] ✗ WS錯誤: {s}\n       {e}")
            bad.append((s, f"WS錯:{e}"))
            if args.session:  # session 斷線就重連，劇情盡量續走
                try:
                    sess_ws = await websockets.connect(uri, ssl=ctx, max_size=None)
                except Exception:
                    pass
            continue
        why = looks_bad(r["view"], r["answer"])
        flag = "⚠️ " if why else "   "
        head = f"[{i:3}] {flag}Q: {s}"
        meta = f"view={r['view']} · {r['lat']:.0f}ms · {r['mode']}"
        if r["tps"]:
            meta += f" · {r['tps']:.1f}t/s"
        print(f"\n{head}\n       {meta}")
        # 回答全文（多行縮排，讓長回答也看得清楚）
        ans = r["answer"] or "（無文字回答）"
        for ln in ans.splitlines() or [ans]:
            print(f"       │ {ln}")
        if why:
            print(f"       └─⚠️ 可疑：{why}")
            bad.append((s, why))

    if sess_ws is not None:
        try:
            await sess_ws.close()
        except Exception:
            pass
    print(f"\n{'='*74}")
    if bad:
        print(f"⚠️ 可疑回答 {len(bad)} 句（要回頭看）：")
        for s, why in bad:
            print(f"   · {s}  → {why}")
    else:
        print("✅ 沒有可疑回答")
    print(f"{'='*74}")

if __name__ == "__main__":
    asyncio.run(main())
