"""累積式 WS 回歸題庫 — 每輪口語測試的題目測完「不刪」，全部併進來。

背景（2026-07-03）：第 1~8 輪各 100 題口語測試當時測完即刪，後續修正
（rewrite 刪除 / guide 排除詞 / RCA 詞擴充…）理論上可能打壞舊輪已過的題，
卻無從驗證。從第 9 輪起所有題目累積在 regression_corpus.txt，修完任何
dispatch / rewrite / 關鍵字清單，跑這支全量驗證。

用法：
    python regression_ws.py            # 跑全部
    python regression_ws.py mv tf      # 只跑指定類別

題庫格式（regression_corpus.txt）：
    類別|題目          # 井號開頭為註解
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
    "rel":   lambda v: v in ("related", "related_help", "related_empty"),
    "mvt":   lambda v: v == "movement",
    "vague": lambda v: v in ("clarify", "guide", "rejected", "inventory"),
    "noex":  lambda v: v in ("clarify", "rejected", "error", "related_empty", "guide"),
    "any":   lambda v: v not in ("error", "clarify", "rejected"),
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
        cat, _, text = line.partition("|")
        cat, text = cat.strip(), text.strip()
        if cat not in ACCEPT or not text:
            continue
        if only and cat not in only:
            continue
        qa.append((cat, text))

    total = ok = 0
    fails = []
    for cat, text in qa:
        total += 1
        try:
            r = asyncio.run(_q(text))
        except Exception as e:
            fails.append((cat, text, f"WS error: {e}", ""))
            continue
        view = r.get("view", "?")
        if ACCEPT[cat](view):
            ok += 1
        else:
            fails.append((cat, text, view, (r.get("summary") or "")[:70]))

    print(f"\n{'='*66}\n累積回歸: {ok}/{total} ({ok/total*100:.1f}%)\n")
    for cat, text, view, s in fails:
        print(f"  FAIL [{cat}] {text}\n       -> view={view} | {s}")


if __name__ == "__main__":
    main()
