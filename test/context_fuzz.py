# -*- coding: utf-8 -*-
"""context_fuzz.py — 跨句 context 空間半窮舉掃描（r56 起每輪必跑）

背景：r55 收官批發現跨句連續對話是弱點——「上一輪 view × 這一句追問句型」是
乘法組合，手寫劇本蓋不完。仿 branch_walk 的思路：前置動作 × 追問句型 做
笛卡兒掃描，每對 (setup, followup) 開獨立連線實走兩輪，斷言分三級：
  FAIL  = error / 空回答 / 無卡卻執行寫入 / 有卡卻沒執行 / 全域追問被前置商品污染
  WARN  = 語意可疑（rejected 的追問、答非所問嫌疑）→ 人工回看
  ok    = 通過
用法：python context_fuzz.py [--rpi5] [--only setup_key]
"""
import asyncio, json, ssl, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import websockets

if "--rpi5" in sys.argv:
    URI = "wss://localhost:8001/ws?fast=1"
    CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
else:
    URI, CTX = "ws://localhost:8000/ws?fast=1", None

_ONLY = None
if "--only" in sys.argv:
    _ONLY = sys.argv[sys.argv.index("--only") + 1]

# ── 前置動作：key → (句子, 預期 view, 前置商品名或 None) ──
SETUPS = {
    "single":   ("無線滑鼠還剩幾個",       "inventory_single", "無線滑鼠"),
    "hot":      ("本月熱銷前五",           "hot_items",        None),
    "low":      ("哪些快缺貨",             "low_stock",        None),
    "exp":      ("快過期的有哪些",         "expiring",         None),
    "cfgread":  ("安全庫存是多少 全部的",  "config_read",      None),
    "related":  ("買咖啡機的人還買什麼",   "related",          None),
    "mvcard":   ("北倉進5個毛帽",          "movement_confirm", "保暖毛帽"),
    "movement": ("今天進了什麼貨",         "movement",         None),
}

DONE_VIEWS = {"movement_done", "config_done", "transfer_done", "script_done",
              "item_created", "po_done", "alert_done", "schedule_done"}

# ── 追問句型：(句子, 檢查函式) ──
# 檢查函式簽名 chk(skey, setup_item, view, ans) → None=ok / "WARN:..." / "FAIL:..."


def _base(skey, item, view, ans):
    """所有追問共用的底線：不 error、不空回答。"""
    if view == "error":
        return "FAIL:error view"
    if not ans.strip():
        return "FAIL:空回答"
    return None


def _no_ghost_write(skey, item, view, ans):
    """無卡片的前置後，確認詞絕不可執行寫入；有卡片則必須執行。"""
    if skey == "mvcard":
        return None if view in DONE_VIEWS else "FAIL:有卡片說確認卻沒執行"
    return "FAIL:無卡片卻執行了寫入" if view in DONE_VIEWS else None


def _global_unpolluted(skey, item, view, ans):
    """全域清單追問不可被前置商品污染成單品過濾（r55f 危險級守衛的泛化）。"""
    if view in ("rejected",):
        return "WARN:全域追問被拒"
    if "沒有「" in ans or view == "expiring_empty":
        return "FAIL:全域追問被污染成查無商品"
    if item and item in ans and view in ("inventory_single",):
        return "FAIL:全域追問被前置商品接管"
    return None


def _wh_follow(skey, item, view, ans):
    """倉別追問要換到南倉視角（含南字），不可退回全店概覽/被拒。"""
    if view == "rejected":
        return "WARN:倉別追問被拒"
    if "南" not in ans:
        return "WARN:回答未見南倉"
    return None


def _period_follow(skey, item, view, ans):
    """期間追問：進出類前置 → 應回 movement；其他前置 → 不強制但不可 error。"""
    if skey in ("movement", "single") and view not in ("movement", "clarify"):
        return f"WARN:期間追問回 {view}"
    return None


def _ordinal(skey, item, view, ans):
    """序數：排行/清單前置應接住；單品/卡片前置說第二個 → 合理是 clarify/引導。"""
    if skey in ("hot",) and view in ("rejected",):
        return "WARN:排行後序數被拒"
    return None


def _cancel(skey, item, view, ans):
    """取消：有卡 → item_cancelled；無卡 → 溫和 clarify。都不可寫入。"""
    if view in DONE_VIEWS:
        return "FAIL:取消卻執行了寫入"
    return None


def _pron(skey, item, view, ans):
    """代詞追問（它/那個）：單品前置要接到該商品。"""
    if skey == "single":
        if view == "rejected":
            return "WARN:單品後代詞被拒"
        if item and item not in ans and view in ("inventory_single", "movement"):
            return f"WARN:代詞接到別的商品"
    return None


FOLLOWUPS = [
    ("第二個",           _ordinal),
    ("最後一個",         _ordinal),
    ("南倉咧",           _wh_follow),
    ("只看南倉的",       _wh_follow),
    ("昨天呢",           _period_follow),
    ("上週呢",           _period_follow),
    ("它還剩幾個",       _pron),
    ("那個進出紀錄呢",   _pron),
    ("它快到期嗎",       _pron),
    ("最急的是哪個",     lambda s, i, v, a: "WARN:最急追問被拒" if v == "rejected" else None),
    ("多少錢",           lambda s, i, v, a: None),
    ("哪些快缺貨",       _global_unpolluted),
    ("快過期的有哪些",   _global_unpolluted),
    ("取消",             _cancel),
    ("好",               _no_ghost_write),
    ("確認",             _no_ghost_write),
]


async def ask(ws, text, timeout=45):
    await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))
    toks = []
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if m.get("type") == "token":
            toks.append(m.get("text", ""))
        elif m.get("type") == "error":
            # server 的 error frame（「我看不懂」/推理超時）沒有 done——直接回報，
            # 否則這裡等到 timeout 會誤報成 WS 錯
            return "error", (m.get("text") or "").strip()
        elif m.get("type") == "done":
            r = m.get("result") or {}
            return r.get("view", "?"), (r.get("summary") or "".join(toks)).strip()


async def main():
    fails, warns, total = [], [], 0
    for skey, (setup, want_view, item) in SETUPS.items():
        if _ONLY and skey != _ONLY:
            continue
        print(f"\n▶ 前置[{skey}]「{setup}」")
        for fu, chk in FOLLOWUPS:
            total += 1
            try:
                async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
                    v0, _ = await ask(ws, setup)
                    if v0 != want_view:
                        print(f"   ⚠️ 前置歪掉 view={v0}（預期 {want_view}）→ 跳過本對")
                        warns.append((skey, "(setup)", f"前置 view={v0}"))
                        continue
                    v, a = await ask(ws, fu)
            except Exception as e:
                fails.append((skey, fu, f"WS錯:{e}"))
                print(f"   ❌ {fu} → WS錯: {e}")
                continue
            verdict = _base(skey, item, v, a) or chk(skey, item, v, a)
            if verdict and verdict.startswith("FAIL"):
                fails.append((skey, fu, verdict[5:]))
                mark = "❌"
            elif verdict:
                warns.append((skey, fu, verdict[5:]))
                mark = "⚠️"
            else:
                mark = "✅"
            print(f"   {mark} {fu} → {v} | {a[:42]}"
                  + (f"  ←{verdict.split(':', 1)[1]}" if verdict else ""))

    print(f"\n{'=' * 62}\ncontext_fuzz：{total} 對 · FAIL {len(fails)} · WARN {len(warns)}")
    for s, f, why in fails:
        print(f"  ❌ [{s}] {f} → {why}")
    if warns:
        print("  （WARN 清單人工回看：）")
        for s, f, why in warns:
            print(f"  ⚠️ [{s}] {f} → {why}")
    sys.exit(1 if fails else 0)

asyncio.run(main())
