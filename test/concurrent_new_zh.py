# -*- coding: utf-8 -*-
"""concurrent_new_zh.py — ZH 並發 × 新功能（鏡射 EN 26/26 那輪，2026-08-04）。

EN 的 per-vid 隔離已實證,ZH 的新狀態（選單 actions/確認卡/_export_done_by_vid/
live-qa）只有推定 —— 這輪補實證。6 訪客同時,V2 專職竊取。
結尾對帳：movements +3（V1×2+V6×1）、orders/PO_draft +1。
"""
import asyncio
import io
import json
import pathlib
import ssl
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import websockets

ROOT = pathlib.Path("/home/p400/warehouse_v2")
AUDIT = ROOT / "warehouse_data" / "audit"
PO_DIR = ROOT / "warehouse_data" / "orders" / "PO_draft"
URI = "wss://localhost:8001/ws?fast=1"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

V = {
 "V1 選單流": [
   ("匯出進出紀錄", "chat", {"clarify"}, "期間", None),
   ("2", "chat", {"script_confirm"}, "7 天", None),
   ("confirm", "confirm", {"script_done"}, None, None),
   ("上個月的呢", "chat", {"script_confirm"}, "30 天", None),
   ("confirm", "confirm", {"script_done"}, None, None),
 ],
 "V2 竊取者": [
   ("2", "chat", "*", None, "STEAL:不可 script_confirm"),
   ("確認", "chat", {"rejected", "guide"}, None, "STEAL:無卡要擋"),
   ("那上週的呢", "chat", "*", None, "STEAL:不可匯出"),
   ("ㄅㄆㄇㄈ", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("匯出進出紀錄", "chat", {"clarify"}, "期間", None),
 ],
 "V3 PO 流": [
   ("有哪些快缺貨", "chat", {"low_stock"}, None, None),
   ("幫這些開採購單", "chat", {"po_confirm", "po_done"}, None, None),
   ("confirm", "confirm", {"po_done", "guide", "rejected"}, None, None),
   ("讚", "chat", "*", None, None),
 ],
 "V4 報告流": [
   ("倉庫健檢", "chat", {"script_confirm", "script_done"}, None, None),
   ("confirm", "confirm", {"script_done", "guide", "rejected"}, None, None),
   ("可以下載嗎", "chat", {"guide"}, "開啟報告", None),
   ("辛苦了", "chat", "*", None, None),
 ],
 "V5 查詢流": [
   ("藍牙耳機庫存", "chat", {"inventory_single"}, None, None),
   ("北倉跟南倉比較", "chat", {"compare_warehouses"}, None, None),
   ("本月熱銷排行", "chat", {"hot_items"}, None, None),
   ("快到期的有哪些", "chat", {"expiring"}, None, None),
 ],
 "V6 直接匯出": [
   ("匯出昨天的進出紀錄", "chat", {"script_confirm"}, "昨天", None),
   ("confirm", "confirm", {"script_done"}, None, None),
   ("可以下載嗎", "chat", {"guide"}, "開啟報告", None),
   ("掰掰", "chat", "*", None, None),
 ],
}


def n_mv():
    return len(list(AUDIT.glob("movements_*.csv")))


def n_po():
    try:
        return len(list(PO_DIR.glob("*")))
    except Exception:
        return 0


async def ask(ws, payload):
    await ws.send(json.dumps(payload, ensure_ascii=False))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=240))
        if m.get("type") == "done":
            return m.get("result") or {}


async def visitor(name, steps, results):
    last_card = None
    try:
        async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
            for send_txt, kind, allow, substr, note in steps:
                if kind == "confirm" and last_card:
                    d = last_card
                    if d.get("_view") == "po_confirm":
                        payload = {"type": "confirm", "action": "generate_po",
                                   "pending": d}
                    else:
                        payload = {"type": "confirm", "action": "run_script",
                                   "script_id": d.get("script_id", ""),
                                   "days": d.get("days")}
                    shown = f"[confirm {d.get('script_id', 'po')}]"
                else:
                    payload = {"type": "chat", "text": send_txt}
                    shown = send_txt
                try:
                    r = await ask(ws, payload)
                except Exception as e:
                    results.append((name, shown, "TIMEOUT", str(e)[:40], True))
                    continue
                v = r.get("view") or ""
                summ = (r.get("summary") or "").replace("\n", " ")
                data = r.get("data") or {}
                bad, why = False, []
                if allow != "*" and v not in allow:
                    bad = True
                    why.append(f"期望 {'/'.join(sorted(allow))}")
                if substr and substr not in summ:
                    bad = True
                    why.append(f"缺「{substr}」")
                if note and note.startswith("STEAL"):
                    if "不可 script_confirm" in note and v == "script_confirm":
                        bad = True
                        why.append("偷到選單!")
                    if "不可匯出" in note and v in ("script_confirm", "script_done") \
                            and "匯出" in summ:
                        bad = True
                        why.append("偷到匯出 context!")
                if v == "error":
                    bad = True
                    why.append("view=error")
                if v in ("script_confirm", "po_confirm"):
                    last_card = dict(data)
                    last_card["_view"] = v
                results.append((name, shown, v,
                                (" / ".join(why) + " ⟪" + summ[:44] + "⟫")
                                if bad else summ[:56], bad))
    except Exception as e:
        results.append((name, "(連線)", "CONN_FAIL", str(e)[:60], True))


async def main():
    mv0, po0 = n_mv(), n_po()
    print(f"{'='*74}\n  ZH 並發 × 新功能（6 訪客同時）  起始 mv={mv0} po={po0}\n{'='*74}")
    results = []
    t0 = time.time()
    await asyncio.gather(*[visitor(n, s, results) for n, s in V.items()])
    dt = time.time() - t0
    bad = [r for r in results if r[4]]
    for name, shown, v, info, isbad in results:
        print(f"  {'❌' if isbad else '✅'} {name:9} | {shown[:26]:28} → {v:16} | {info[:52]}")
    mv1, po1 = n_mv(), n_po()
    print(f"\n{'='*74}\n  斷言 {len(results)-len(bad)}/{len(results)}  耗時 {dt:.1f}s")
    print(f"  movements {mv0}→{mv1}（應 +3）  PO_draft {po0}→{po1}（應 +1）")
    if mv1 - mv0 > 3:
        print("  ❌ 匯出多寫")
    if po1 - po0 > 1:
        print("  ❌ PO 多寫")
    print(f"{'='*74}")


asyncio.run(main())
