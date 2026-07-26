"""ws_convo.py — 多輪對話巡檢（r32）。

regression_ws.py / ws_inspect.py 都是「一句一條 WS 連線」，而 server 的
session state（_ctx_by_vid / _item_create_state_ws / _item_delete_state）
是綁 vid、vid 綁連線 → 那兩支工具從來沒測過任何跨輪行為。

本工具在「同一條連線」上連發整個劇本，覆蓋三個沒掃過的空間：
  A. pending 卡片出現後不按確認、直接打字（展場訪客最常見）
  B. carry-over 追問鏈（「那個呢」「B倉呢」連環追問，含跳題/污染）
  C. 寫入流程（新增商品 step 機）中途插查詢／亂打／放棄

用法：
    python ws_convo.py --file convo_r32.txt          # 本機 ws://localhost:8000
    python ws_convo.py --file convo_r32.txt --rpi5   # RPI5 wss://localhost:8001
    python ws_convo.py --file convo_r32.txt --quiet  # 只印 FAIL/⚠️（回歸用）

劇本格式（每個 ### 區塊 = 一個獨立連線 = 一位訪客）：
    ### 情境名稱
    > 使用者句子                      # 不斷言，只看回答
    > 使用者句子 | inventory,clarify   # 斷言 view 必須落在其中之一
    > 使用者句子 | inventory | 無線滑鼠 # 第三欄：summary 必須含此字串
    [confirm]                         # 重播「按確認鍵」：用上一輪 result 自動推
                                      #   action + pending 送出
    ! 說明文字                        # 註解（會印出來，幫 review 看情境意圖）

斷言欄支援 not: 前綴 → view 不可為這些（例：| not:movement_confirm,error）
"""
import asyncio, json, ssl, sys, io, time, argparse
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import websockets

# 確認卡 view → confirm action（對照 templates/index.html 的 doConfirm data-action）
VIEW2ACTION = {
    "item_confirm":      "item_create",
    "movement_confirm":  "create_movement",
    "transfer_confirm":  "create_transfer",
    "item_delete_confirm": "item_delete",
    "config_confirm":    "config_set",
    "po_confirm":        "generate_po",
    "alert_confirm":     "set_alert",
    "schedule_confirm":  "set_schedule",
    "script_confirm":    "run_script",
}
SUSPICIOUS_VIEWS = {"error"}
# ⚠️ EN build（r5）：原本這兩組全是中文 → 英文版跑 r1-r4 時**一句可疑都標不出來**，
#   ⚠️ 標記形同虛設（所以「審到畫面、讀全文」才是唯一防線，見 review_to_the_screen）。
#   這裡補英文對應詞，讓工具至少能標出最明顯的醜回答。
SUSPICIOUS_TEXT = ("看不懂", "聽不懂", "我不太理解", "無法理解", "不知道你",
                   "請再說一次", "哪個設定項", "我不確定",
                   # —— EN ——
                   "I'm not sure", "I am not sure", "not quite sure",
                   "please rephrase", "Please rephrase",
                   "say that again", "Could not make out",
                   "Which setting", "I don't understand", "do not understand")
# 「擋下」型錯誤是正確行為（error view 是刻意的），不算破口
_LEGIT_ERROR = ("庫存不足", "不足", "Not enough stock", "not enough stock")


def looks_bad(view: str, text: str) -> str:
    if view in SUSPICIOUS_VIEWS:
        if any(s in text for s in _LEGIT_ERROR):
            return ""
        return f"view={view}"
    for s in SUSPICIOUS_TEXT:
        if s in text:
            return f"含「{s}」"
    if not text and view not in ("rejected", "guide", "clarify", "item_cancelled"):
        return "空回答"
    return ""


def parse_script(path: Path):
    """→ [(情境名, [step, …])]，step = ("say", text, expect, must) | ("confirm",) | ("note", text)"""
    scenes, cur = [], None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("###"):
            cur = (line.lstrip("#").strip(), [])
            scenes.append(cur)
        elif line.startswith("#"):
            continue
        elif cur is None:
            continue
        elif line.startswith(">"):
            body = line[1:].strip()
            # r34：「>@B 句子」= 這句由訪客 B 說（同情境內開第二條連線，模擬展場
            #   兩台裝置交錯對話 → 驗 vid 隔離）。沒有 @ 前綴 = 預設訪客 A。
            who = "A"
            if body.startswith("@"):
                who, _, body = body[1:].partition(" ")
                who = who.strip() or "A"
            parts = [p.strip() for p in body.split("|")]
            text = parts[0]
            expect = parts[1] if len(parts) >= 2 else ""
            must = parts[2] if len(parts) >= 3 else ""
            cur[1].append(("say", text, expect, must, who))
        elif line.startswith("[para]"):
            # r5：[para] … [/para] 之間的句子**同時**送出（真並發，驗 vid 隔離）
            cur[1].append(("para",))
        elif line.startswith("[/para]"):
            cur[1].append(("endpara",))
        elif line.startswith("[confirm"):
            # [confirm] 或 [confirm@B]
            who = "A"
            if "@" in line:
                who = line.split("@", 1)[1].rstrip("] ").strip() or "A"
            cur[1].append(("confirm", who))
        elif line.startswith("!"):
            cur[1].append(("note", line[1:].strip()))
    return scenes


def check_expect(view: str, expect: str) -> str:
    """回傳失敗說明；通過回空字串。"""
    if not expect:
        return ""
    if expect.startswith("not:"):
        banned = {v.strip() for v in expect[4:].split(",") if v.strip()}
        return f"view={view} 落在禁止集 {sorted(banned)}" if view in banned else ""
    allowed = {v.strip() for v in expect.split(",") if v.strip()}
    return "" if view in allowed else f"view={view}，期望 {sorted(allowed)}"


def check_candidates(result: dict, spec: str) -> str:
    """驗 clarify 反問清單的內容正確性（回失敗說明；通過回空字串）。

    「反問」不是免罪金牌——列漏了訪客要的、列了一堆不相干的、資訊錯的，都跟答錯
    一樣糟。這裡把「反問對不對」變成可測的斷言。

    spec 語法（分號分隔多條）：
        cand:has=露營燈罩       清單必須含名為「露營燈罩」的項（沒漏）
        cand:hasnot=無線滑鼠    清單不可含（沒亂列不相干的）
        cand:count<=8           清單長度上限（沒爆版/沒把全表倒出來）
        cand:count>=2           清單至少幾項
    """
    data = result.get("data") or {}
    cands = data.get("candidates") or []
    names = [c.get("name", "") for c in cands if isinstance(c, dict)]
    # 沒有 candidates 欄位時，退回看 options（相容舊 clarify）
    if not names:
        names = [str(o).replace(" 庫存", "").strip() for o in (data.get("options") or [])]
    for clause in spec.split(";"):
        clause = clause.strip()
        if not clause.startswith("cand:"):
            continue
        body = clause[5:]
        if body.startswith("has="):
            want = body[4:]
            if not any(want in n for n in names):
                return f"清單漏了「{want}」（實際：{names}）"
        elif body.startswith("hasnot="):
            bad = body[7:]
            if any(bad in n for n in names):
                return f"清單不該含「{bad}」（實際：{names}）"
        elif body.startswith("count<="):
            if len(names) > int(body[7:]):
                return f"清單過長 {len(names)}>{body[7:]}（{names}）"
        elif body.startswith("count>="):
            if len(names) < int(body[7:]):
                return f"清單過短 {len(names)}<{body[7:]}（{names}）"
    return ""


async def recv_result(ws, timeout=60):
    """收到 done 為止，回傳 (result, 串接的 tokens, 本輪是否進 LLM)。

    llm_hit 供「RPI5 子集快驗」用（方案2，2026-07-16）：平台分歧只可能發生在
    進 LLM 的輪，全量跑完把 LLM-hit 情境另存 *_llmsub.txt，日常 RPI5 只重驗那份。
    """
    toks, llm_hit = [], False
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        t = m.get("type")
        if t == "token":
            toks.append(m.get("text", ""))
        elif t == "perf" and m.get("mode") == "llm":
            llm_hit = True
        elif t == "error":
            return {"view": "error", "summary": m.get("text", "")}, "".join(toks), llm_hit
        elif t == "done":
            return (m.get("result") or {}), "".join(toks), llm_hit


async def _ensure_conn(conns, who, uri, ctx):
    """取得（必要時建立）訪客 who 的連線。握手逾時會自動重連——server 同時被別的
    測試打時 opening handshake 偶爾逾時，那是連線層抖動，不該算成劇本失敗。"""
    if who not in conns:
        for _try in range(3):
            try:
                conns[who] = [await websockets.connect(uri, ssl=ctx, max_size=None), {}]
                break
            except Exception:
                if _try == 2:
                    raise
                await asyncio.sleep(1.5 * (_try + 1))
    return conns[who]


async def _do_step(st, conns, name, quiet, multi):
    """送出一個 step、收結果、評分。回傳 (fails, sus, llm_hit, report)。

    report = (mark, label, view, ans, lat, why_fail, why_sus)，交由呼叫端印出——
    並發模式下必須等整組跑完才印，否則多位訪客的輸出會交錯難讀。
    """
    fails, sus = [], []
    who = st[-1]
    ws, last = conns[who]
    tag = "" if who == "A" and not multi else f"[{who}] "

    if st[0] == "confirm":
        view = last.get("view", "")
        action = VIEW2ACTION.get(view)
        if not action:
            fails.append((name, f"{tag}[confirm]",
                          f"上一輪 view={view or '?'} 不是確認卡，無法按確認"))
            return fails, sus, False, ("✗", f"{tag}[confirm]", view or "?", "", 0,
                                       f"上一輪 view={view or '?'} 沒有確認卡", "")
        payload = {"type": "confirm", "action": action, "pending": last.get("data", {})}
        if action == "run_script":
            payload["script_id"] = (last.get("data") or {}).get("script_id", "")
        label, t0 = f"{tag}[confirm→{action}]", time.perf_counter()
        await ws.send(json.dumps(payload, ensure_ascii=False))
        expect, must = "", ""
    else:
        _, text, expect, must, _ = st
        label, t0 = f"{tag}Q: {text}", time.perf_counter()
        await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))

    llm_hit = False
    try:
        r, toks, llm_hit = await recv_result(ws)
    except Exception as e:
        fails.append((name, label, f"WS 錯誤/逾時: {e}"))
        return fails, sus, llm_hit, ("✗", label, "?", "", 0, f"WS 錯誤: {e}", "")

    conns[who][1] = r
    view = r.get("view", "?")
    ans = (r.get("summary") or toks).strip()
    lat = (time.perf_counter() - t0) * 1000

    why_fail = check_expect(view, expect)
    if not why_fail and must:
        if "cand:" in must:
            why_fail = check_candidates(r, must)   # 驗反問清單內容
        elif must not in ans:
            why_fail = f"回答缺「{must}」"
    why_sus = looks_bad(view, ans)

    if why_fail:
        fails.append((name, label, f"{why_fail} | {ans[:60]}"))
    if why_sus:
        sus.append((name, label, f"{why_sus} | {ans[:60]}"))

    mark = "✗" if why_fail else ("⚠️" if why_sus else " ")
    return fails, sus, llm_hit, (mark, label, view, ans, lat, why_fail, why_sus)


def _print_report(rep, quiet):
    mark, label, view, ans, lat, why_fail, why_sus = rep
    if quiet and not (why_fail or why_sus):
        return
    print(f"\n   {mark} {label}   [view={view} · {lat:.0f}ms]")
    for ln in (ans or "（無文字回答）").splitlines():
        print(f"     │ {ln}")
    if why_fail:
        print(f"     └─✗ FAIL: {why_fail}")
    elif why_sus:
        print(f"     └─⚠️ 可疑: {why_sus}")


async def run_scene(uri, ctx, name, steps, quiet):
    """一個情境 = 一位（或多位）訪客，每位一條連線（= 一個 vid），連發所有 step。"""
    fails, sus = [], []
    scene_llm = False   # 任一輪進 LLM → 整個情境屬 RPI5 子集
    if not quiet:
        print(f"\n{'─'*74}\n### {name}\n{'─'*74}")

    conns: dict = {}   # who → (ws, last_result)
    try:
        i = 0
        while i < len(steps):
            st = steps[i]
            if st[0] == "note":
                if not quiet:
                    print(f"   ! {st[1]}")
                i += 1
                continue

            # ── [para] 區塊：整組**同時**送出（r5 新增）──────────────────
            # `>@B` 交錯語法測的是「輪流」，一次只有一個請求在 server 裡；
            # 真正的並發（同一瞬間兩位訪客的請求在 server 內交錯執行）從沒測過。
            # vid 隔離若有破口（共用 module-level 狀態、pending 卡互踩），
            # 只有這種同時送才打得出來。
            if st[0] == "para":
                group = []
                i += 1
                while i < len(steps) and steps[i][0] != "endpara":
                    if steps[i][0] != "note":
                        group.append(steps[i])
                    elif not quiet:
                        print(f"   ! {steps[i][1]}")
                    i += 1
                i += 1   # 跳過 endpara
                for g in group:
                    await _ensure_conn(conns, g[-1], uri, ctx)
                if not quiet:
                    print(f"   ⇉ [para] {len(group)} 位訪客同時送出")
                results = await asyncio.gather(
                    *[_do_step(g, conns, name, quiet, True) for g in group],
                    return_exceptions=True)
                for g, res in zip(group, results):
                    if isinstance(res, Exception):
                        fails.append((name, f"[{g[-1]}] para", f"並發例外: {res}"))
                        if not quiet:
                            print(f"   ✗ [{g[-1]}] para 並發例外: {res}")
                        continue
                    f, s, hit, rep = res
                    fails += f
                    sus += s
                    scene_llm = scene_llm or hit
                    _print_report(rep, quiet)
                continue

            await _ensure_conn(conns, st[-1], uri, ctx)
            f, s, hit, rep = await _do_step(st, conns, name, quiet, len(conns) > 1)
            fails += f
            sus += s
            scene_llm = scene_llm or hit
            _print_report(rep, quiet)
            i += 1
    finally:
        for ws, _ in conns.values():
            await ws.close()
    return fails, sus, scene_llm


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="劇本檔")
    ap.add_argument("--rpi5", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="只印 FAIL/可疑")
    ap.add_argument("--only", help="只跑名稱含此字串的情境")
    ap.add_argument("--out", help="把失敗明細另外寫進這個檔（shell redirect 在 Windows "
                                  "背景執行時會漏掉輸出，長跑一定要用這個）")
    ap.add_argument("--reset", action="store_true",
                    help="開跑前把展示資料重置回 baseline（劇本會真的寫入資料，"
                         "連跑兩本劇本會互相污染：前一本新增的商品讓後一本的同名"
                         "新增流程多問一步）")
    args = ap.parse_args()

    if args.reset:
        import ssl as _ssl, urllib.request as _rq
        # ⚠️ EN build：英文版在 **8002**（8001 是中文版）。ws_inspect/regression_ws
        #   都踩過這雷（整批測到中文版、回答全中文），這支也要改。
        base = "https://localhost:8002" if args.rpi5 else "http://localhost:8000"
        _ctx_no = _ssl.create_default_context()
        _ctx_no.check_hostname = False
        _ctx_no.verify_mode = _ssl.CERT_NONE
        req = _rq.Request(f"{base}/api/reset_demo", method="POST",
                          data=json.dumps({"password": "0000"}).encode(),
                          headers={"Content-Type": "application/json"})
        with _rq.urlopen(req, context=_ctx_no if args.rpi5 else None) as r:
            print(f"[reset] {json.loads(r.read()).get('summary', '')}")

    if args.rpi5:
        uri = "wss://localhost:8002/ws?fast=1"   # EN build：8002 不是 8001
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        uri, ctx = "ws://localhost:8000/ws?fast=1", None

    path = Path(args.file)
    if not path.is_absolute():
        path = Path(__file__).parent / args.file
    scenes = parse_script(path)
    if args.only:
        scenes = [s for s in scenes if args.only in s[0]]

    turns = sum(1 for _, sts in scenes for st in sts
                if st[0] in ("say", "confirm"))
    print(f"\n{'='*74}\nws_convo → {uri}\n劇本 {path.name}：{len(scenes)} 情境 / {turns} 輪\n{'='*74}")

    all_fails, all_sus = [], []
    llm_scene_names = set()
    for name, steps in scenes:
        f, s, _hit = await run_scene(uri, ctx, name, steps, args.quiet)
        all_fails += f
        all_sus += s
        if _hit:
            llm_scene_names.add(name)

    # LLM-hit 情境另存 <劇本>_llmsub.txt（方案2：RPI5 日常只重驗這份；
    # 動 LLM 相關層或展前仍跑原全本）。--only 篩過時樣本不全，不寫。
    if llm_scene_names and not args.only:
        _sub_path = path.with_name(path.stem + "_llmsub.txt")
        _blocks, _cur_keep = [], False
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.strip().startswith("###"):
                _cur_keep = raw.lstrip("#").strip() in llm_scene_names
            if _cur_keep:
                _blocks.append(raw)
        _sub_path.write_text(
            f"# LLM-hit 子集（自動產生：{path.name} 全量跑完側錄，"
            f"{len(llm_scene_names)}/{len(scenes)} 情境進過 LLM）\n"
            + "\n".join(_blocks) + "\n", encoding="utf-8")
        print(f"[llm-subset] {len(llm_scene_names)}/{len(scenes)} 情境 → {_sub_path.name}")

    print(f"\n{'='*74}")
    print(f"情境 {len(scenes)} · 斷言失敗 {len(all_fails)} · 可疑回答 {len(all_sus)}")
    if all_fails:
        print("\n✗ FAIL：")
        for n, l, w in all_fails:
            print(f"   [{n}] {l}\n      → {w}")
    if all_sus:
        print("\n⚠️ 可疑（回答醜/error，回頭看）：")
        for n, l, w in all_sus:
            print(f"   [{n}] {l}\n      → {w}")
    if not all_fails and not all_sus:
        print("✅ 全綠")
    print(f"{'='*74}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(f"情境 {len(scenes)} · 斷言失敗 {len(all_fails)} · 可疑回答 {len(all_sus)}\n\n")
            for n, l, w in all_fails:
                f.write(f"FAIL\t{n}\t{l}\t{w}\n")
            for n, l, w in all_sus:
                f.write(f"SUS\t{n}\t{l}\t{w}\n")
        print(f"（失敗明細已寫入 {args.out}）")

    sys.exit(1 if all_fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
