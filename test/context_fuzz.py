# -*- coding: utf-8 -*-
"""context_fuzz.py — 跨句 context 空間半窮舉掃描（r56 起每輪必跑；r69 擴充到滿）

背景：r55 收官批發現跨句連續對話是弱點——「上一輪狀態 × 這一句追問句型」是
乘法組合，手寫劇本蓋不完。仿 branch_walk：前置狀態 × 追問句型 笛卡兒掃描，
每對 (setup, followup) 開獨立連線實走（前置可多輪），斷言三級：
  FAIL  = error / 空回答 / 無卡卻執行寫入 / 有卡說確認卻沒執行 /
          裸數量、裸改值直接寫入（任何情況打字都不可直接寫）/ 全域追問被污染
  WARN  = 語意可疑（rejected 的追問、答非所問嫌疑）→ 人工回看
  ok    = 通過
r69 滿版：19 前置（含 6 種確認卡、選單、清單、比較、寫入完成態）× 20 追問 = 380 對。
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

# ── 前置狀態：key → (句子列表, 最後一句預期 view, 前置商品名或 None) ──
SETUPS = {
    # 查詢面（r56 原有 8 個）
    "single":    (["無線滑鼠還剩幾個"],        "inventory_single", "無線滑鼠"),
    "hot":       (["本月熱銷前五"],            "hot_items",        None),
    "low":       (["哪些快缺貨"],              "low_stock",        None),
    "exp":       (["快過期的有哪些"],          "expiring",         None),
    "cfgread":   (["安全庫存是多少 全部的"],   "config_read",      None),
    "related":   (["買咖啡機的人還買什麼"],    "related",          None),
    "movement":  (["今天進了什麼貨"],          "movement",         None),
    "mvcard":    (["北倉進5個毛帽"],           "movement_confirm", "保暖毛帽"),
    # r69 擴充：其餘 5 種確認卡
    "tfcard":    (["調15個運動毛巾從北倉到中倉"], "transfer_confirm", "運動毛巾"),
    "cfgcard":   (["瑜珈墊安全庫存改成85"],    "config_confirm",   "瑜珈墊"),
    "scriptcard": (["盤點一下"],               "script_confirm",   None),
    "pocard":    (["幫我開採購單"],            "po_confirm",       None),
    "alertcard": (["瑜珈墊低於30就通知我"],    "alert_confirm",    "瑜珈墊"),
    "schedcard": (["每天晚上七點跑盤點"],      "schedule_confirm", None),
    # r69 擴充：選單/清單/比較/寫入完成態
    "menu":      (["咖啡還剩多少"],            "clarify",          None),
    "itemlist":  (["商品清單"],                "item_list",        None),
    "catlist":   (["服飾類庫存"],              "inventory",        None),
    "compare":   (["北倉跟南倉比"],            "compare_warehouses", None),
    "mvdone":    (["北倉進5個毛帽", "好"],     "movement_done",    "保暖毛帽"),
}

# 有確認卡在畫面上的前置（確認詞必須執行、其他任何輸入絕不可直接寫入）
CARD_SETUPS = {"mvcard", "tfcard", "cfgcard", "scriptcard", "pocard",
               "alertcard", "schedcard"}

DONE_VIEWS = {"movement_done", "config_done", "transfer_done", "script_done",
              "item_created", "po_done", "alert_done", "schedule_done"}

# ── 追問句型：(句子, 檢查函式) ──
# 檢查函式簽名 chk(skey, setup_item, view, ans) → None=ok / "WARN:..." / "FAIL:..."


def _base(skey, item, view, ans):
    if view == "error":
        return "FAIL:error view"
    if not ans.strip():
        return "FAIL:空回答"
    return None


def _confirm_words(skey, item, view, ans):
    """確認詞：有卡必須執行、無卡絕不可執行。"""
    if skey in CARD_SETUPS:
        return None if view in DONE_VIEWS else "FAIL:有卡片說確認卻沒執行"
    return "FAIL:無卡片卻執行了寫入" if view in DONE_VIEWS else None


def _never_write(skey, item, view, ans):
    """裸數量/裸改值/維持語：任何情況打字都不可**直接**寫入（最多開新確認卡）。"""
    if view in DONE_VIEWS:
        return "FAIL:打字內容被直接寫入（未經確認卡）"
    return None


def _global_unpolluted(skey, item, view, ans):
    if view in DONE_VIEWS:
        return "FAIL:全域追問執行了寫入"
    if view == "rejected":
        return "WARN:全域追問被拒"
    if "沒有「" in ans or view == "expiring_empty":
        return "FAIL:全域追問被污染成查無商品"
    if item and item in ans and view in ("inventory_single",):
        return "FAIL:全域追問被前置商品接管"
    return None


def _wh_follow(skey, item, view, ans):
    if view in DONE_VIEWS:
        return "FAIL:倉別追問執行了寫入"
    if view == "rejected":
        return "WARN:倉別追問被拒"
    if "南" not in ans:
        return "WARN:回答未見南倉"
    return None


def _period_follow(skey, item, view, ans):
    if view in DONE_VIEWS:
        return "FAIL:期間追問執行了寫入"
    if skey in ("movement", "single") and view not in ("movement", "clarify"):
        return f"WARN:期間追問回 {view}"
    return None


def _ordinal(skey, item, view, ans):
    if view in DONE_VIEWS:
        return "FAIL:序數執行了寫入"
    if skey in ("hot", "menu") and view == "rejected":
        return "WARN:排行/選單後序數被拒"
    return None


def _cancel(skey, item, view, ans):
    if view in DONE_VIEWS:
        return "FAIL:取消卻執行了寫入"
    if skey in CARD_SETUPS and view != "item_cancelled":
        return "WARN:有卡片取消未回取消確認"
    return None


def _pron(skey, item, view, ans):
    if view in DONE_VIEWS:
        return "FAIL:代詞追問執行了寫入"
    if skey == "single":
        if view == "rejected":
            return "WARN:單品後代詞被拒"
        if item and item not in ans and view in ("inventory_single", "movement"):
            return "WARN:代詞接到別的商品"
    return None


def _soft(skey, item, view, ans):
    return "FAIL:追問執行了寫入" if view in DONE_VIEWS else None


FOLLOWUPS = [
    ("第二個",           _ordinal),
    ("最後一個",         _ordinal),
    ("第一個",           _ordinal),
    ("南倉咧",           _wh_follow),
    ("只看南倉的",       _wh_follow),
    ("昨天呢",           _period_follow),
    ("上週呢",           _period_follow),
    ("它還剩幾個",       _pron),
    ("那個進出紀錄呢",   _pron),
    ("它快到期嗎",       _pron),
    ("最急的是哪個",     lambda s, i, v, a: ("FAIL:追問執行了寫入" if v in DONE_VIEWS
                                             else "WARN:最急追問被拒" if v == "rejected" else None)),
    ("多少錢",           _soft),
    ("哪些快缺貨",       _global_unpolluted),
    ("快過期的有哪些",   _global_unpolluted),
    ("取消",             _cancel),
    ("好",               _confirm_words),
    ("確認",             _confirm_words),
    # r69 新增：裸數量/裸改值/維持語（寫入安全不變量：打字絕不直接寫）
    ("30件",             _never_write),
    ("改成50",           _never_write),
    ("算了照原本的",     _never_write),
]


async def ask(ws, text, timeout=45):
    await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))
    toks = []
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if m.get("type") == "token":
            toks.append(m.get("text", ""))
        elif m.get("type") == "error":
            return "error", (m.get("text") or "").strip()
        elif m.get("type") == "done":
            r = m.get("result") or {}
            return r.get("view", "?"), (r.get("summary") or "".join(toks)).strip()


async def main():
    fails, warns, total = [], [], 0
    for skey, (setup_sents, want_view, item) in SETUPS.items():
        if _ONLY and skey != _ONLY:
            continue
        print(f"\n▶ 前置[{skey}]「{' → '.join(setup_sents)}」")
        for fu, chk in FOLLOWUPS:
            total += 1
            try:
                # r69：前置偶發歪掉（LLM 抖動）→ 重試一次再放棄，降低 setup 噪音
                v = a = None
                for _try in range(2):
                    async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
                        v0 = "?"
                        for _s in setup_sents:
                            v0, _ = await ask(ws, _s)
                        if v0 != want_view:
                            if _try == 0:
                                continue
                            print(f"   ⚠️ 前置歪掉 view={v0}（預期 {want_view}）→ 跳過本對")
                            warns.append((skey, "(setup)", f"前置 view={v0}"))
                            break
                        v, a = await ask(ws, fu)
                        break
                if v is None:
                    continue
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
