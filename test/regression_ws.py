"""累積式 WS 回歸題庫 — 每輪口語測試的題目測完「不刪」，全部併進來。

背景（2026-07-03）：第 1~8 輪各 100 題口語測試當時測完即刪，後續修正
（rewrite 刪除 / guide 排除詞 / RCA 詞擴充…）理論上可能打壞舊輪已過的題，
卻無從驗證。從第 9 輪起所有題目累積在 regression_corpus.txt，修完任何
dispatch / rewrite / 關鍵字清單，跑這支全量驗證。

用法：
    python regression_ws.py            # 跑全部
    python regression_ws.py mv tf      # 只跑指定類別

題庫格式（regression_corpus.txt）：
    類別|題目               # 井號開頭為註解
    類別|題目|內容關鍵字     # 第三欄（選填）：回答 summary 必須包含該字串
內容欄是「view 對但內容錯」級 bug 的守衛（第8輪起）——例如「瑜珈墊安全庫存
加20」view=config_confirm 永遠對，但影響範圍曾是全部商品 183 項，只有驗
summary 含「瑜珈墊」才擋得住回退。
類別 → 判定規則見 ACCEPT。
"""
import asyncio, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import websockets
from pathlib import Path

WS_URI = 'ws://localhost:8000/ws'
CORPUS = Path(__file__).parent / "regression_corpus.txt"

ACCEPT = {
    "mv":    lambda v: v == "movement_confirm",
    "tf":    lambda v: v == "transfer_confirm",
    "tf_insuff": lambda v: v == "error",   # 來源倉庫存不足 → 擋下是正確行為
    "tf_clarify": lambda v: v in ("transfer_confirm", "clarify"),  # 單倉調貨 → 問來源倉
    "inv":   lambda v: v in ("inventory", "inventory_single", "clarify", "low_stock"),
    "low":   lambda v: v == "low_stock",
    "exp":   lambda v: v == "expiring",
    "hot":   lambda v: v == "hot_items",
    "rca":   lambda v: v == "agent_rca",
    "cfg":   lambda v: v in ("config_confirm", "config", "config_read"),
    # config 諮詢/缺值/搗蛋負數 → clarify 友善追問（不 crash 不亂寫）
    "cfg_clarify": lambda v: v in ("clarify", "config_read", "guide"),
    "rel":   lambda v: v in ("related", "related_help", "related_empty"),
    "mvt":   lambda v: v == "movement",
    "vague": lambda v: v in ("clarify", "guide", "rejected", "inventory"),
    "noex":  lambda v: v in ("clarify", "rejected", "error", "related_empty", "guide"),
    "any":   lambda v: v not in ("error", "clarify", "rejected"),
    # 訪客閒聊/搗蛋防禦（第17輪）：優雅拒絕/引導/追問，不可幻覺商品或開卡
    "chat":   lambda v: v in ("rejected", "guide", "clarify"),
    "guidey": lambda v: v in ("guide", "rejected", "clarify"),
    "probe":  lambda v: v in ("rejected", "guide", "clarify", "error"),
    # 半倉管（第19輪）：問有沒有賣/多少錢，顯示庫存是好回答；只擋寫入確認卡
    "semi":   lambda v: v not in ("movement_confirm", "transfer_confirm",
                                   "config_confirm", "po_confirm", "alert_confirm",
                                   "schedule_confirm", "item_list", "item_delete_denied",
                                   "error"),
}


async def _q(text):
    async with websockets.connect(WS_URI, max_size=None) as ws:
        await ws.send(json.dumps({'type': 'chat', 'text': text}, ensure_ascii=False))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=45)
            msg = json.loads(raw)
            if msg.get('type') == 'done':
                return msg.get('result', {})


def main():
    only = set(sys.argv[1:])
    qa = []
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        cat, text = parts[0], parts[1]
        must = parts[2] if len(parts) >= 3 else ""
        if cat not in ACCEPT or not text:
            continue
        if only and cat not in only:
            continue
        qa.append((cat, text, must))

    total = ok = 0
    fails = []
    for cat, text, must in qa:
        total += 1
        try:
            r = asyncio.run(_q(text))
        except Exception as e:
            fails.append((cat, text, f"WS error: {e}", ""))
            continue
        view = r.get("view", "?")
        summary = r.get("summary") or ""
        if not ACCEPT[cat](view):
            fails.append((cat, text, view, summary[:70]))
        elif must and must not in summary:
            fails.append((cat, text, f"{view}(內容缺「{must}」)", summary[:70]))
        else:
            ok += 1

    print(f"\n{'='*66}\n累積回歸: {ok}/{total} ({ok/total*100:.1f}%)\n")
    for cat, text, view, s in fails:
        print(f"  FAIL [{cat}] {text}\n       -> view={view} | {s}")


if __name__ == "__main__":
    main()
