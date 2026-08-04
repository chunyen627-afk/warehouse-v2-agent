# -*- coding: utf-8 -*-
"""concurrent_new_en.py — EN 收斂最終輪：並發 × 新功能（2026-08-04）。

未測角度：今天新加的 per-vid 狀態在**多訪客同時操作**下的隔離——
  `_clarify_opts_by_vid`（匯出選單+序數）/ `_pending_by_vid`（確認卡）/
  `_export_done_by_vid`（匯出後追問,今天新加）——隔離只有推定沒有實證。

6 訪客並發，V2 專職「偷」別人的狀態：
  V1 選單流（選單→2→confirm→and last month too→confirm）
  V2 竊取者（裸 2／裸 confirm／and last week too——**都不該成功**）
  V3 PO 流   V4 報告流+下載指路   V5 一般查詢   V6 直接匯出+指路

結尾對帳：movements 檔增量 == 成功 confirm 的匯出數（V1×2+V6×1=3）、
PO_draft 增量 == 1。任何串線（B 拿到 A 的選單/卡片/context）= 破口。
"""
import asyncio
import io
import json
import pathlib
import re
import ssl
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import websockets

ROOT = pathlib.Path("/home/p400/warehouse_v2_en")
AUDIT = ROOT / "warehouse_data" / "audit"
PO_DIR = ROOT / "warehouse_data" / "PO_draft"
URI = "wss://localhost:8002/ws?fast=1"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# (send, kind, allow(*|set), substr, note)
V = {
 "V1 選單流": [
   ("export the movement log", "chat", {"clarify"}, "period", None),
   ("2", "chat", {"script_confirm"}, "7 days", None),
   ("confirm", "confirm", {"script_done"}, None, "寫檔+1"),
   ("and last month too", "chat", {"script_confirm"}, "30 days", None),
   ("confirm", "confirm", {"script_done"}, None, "寫檔+1"),
 ],
 "V2 竊取者": [
   ("2", "chat", "*", None, "STEAL:不可 script_confirm"),
   ("confirm", "chat", {"guide", "rejected"}, None, "STEAL:無卡要引導"),
   ("and last week too", "chat", "*", None, "STEAL:不可匯出"),
   ("asdfjkl;", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("export movements", "chat", {"clarify"}, "period", None),
 ],
 "V3 PO 流": [
   ("what items are running low", "chat", {"low_stock"}, None, None),
   ("create a purchase order for those", "chat",
    {"po_confirm", "po_done"}, None, None),
   ("confirm", "confirm", {"po_done", "guide", "rejected"}, None, "PO+1"),
   ("cool", "chat", "*", None, None),
 ],
 "V4 報告流": [
   ("warehouse health check", "chat",
    {"script_confirm", "script_done"}, None, None),
   ("confirm", "confirm", {"script_done", "guide", "rejected"}, None, None),
   ("can i download it", "chat", {"guide"}, None, None),
   ("thanks a lot", "chat", "*", None, None),
 ],
 "V5 查詢流": [
   ("bluetooth earphones stock", "chat", {"inventory_single"}, None, None),
   ("compare north and south", "chat", {"compare_warehouses"}, None, None),
   ("best sellers this month", "chat", {"hot_items"}, None, None),
   ("what is expiring soon", "chat", {"expiring"}, None, None),
 ],
 "V6 直接匯出": [
   ("export movements yesterday", "chat", {"script_confirm"}, "yesterday", None),
   ("confirm", "confirm", {"script_done"}, None, "寫檔+1"),
   ("can i download it", "chat", {"guide"}, None, None),
   ("bye", "chat", "*", None, None),
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
                t0 = time.time()
                try:
                    r = await ask(ws, payload)
                except Exception as e:
                    results.append((name, shown, "TIMEOUT", str(e)[:40], True))
                    continue
                v = r.get("view") or ""
                summ = (r.get("summary") or "").replace("\n", " ")
                data = r.get("data") or {}
                bad = False
                why = []
                if allow != "*" and v not in allow:
                    bad = True
                    why.append(f"期望 {'/'.join(sorted(allow))}")
                if substr and substr.lower() not in summ.lower():
                    bad = True
                    why.append(f"缺「{substr}」")
                # 竊取斷言
                if note and note.startswith("STEAL"):
                    if "不可 script_confirm" in note and v == "script_confirm":
                        bad = True
                        why.append("偷到別人的選單!")
                    if "不可匯出" in note and v in ("script_confirm", "script_done") \
                            and "export" in str(data.get("script_id", "")):
                        bad = True
                        why.append("偷到別人的匯出 context!")
                if v == "error":
                    bad = True
                    why.append("view=error")
                if v in ("script_confirm", "po_confirm"):
                    last_card = dict(data)
                    last_card["_view"] = v
                results.append((name, shown, v,
                                (" / ".join(why) + " ⟪" + summ[:48] + "⟫")
                                if bad else summ[:60],
                                bad))
    except Exception as e:
        results.append((name, "(連線)", "CONN_FAIL", str(e)[:60], True))


async def main():
    mv0, po0 = n_mv(), n_po()
    print(f"{'='*74}\n  EN 收斂最終輪：並發 × 新功能（6 訪客同時）\n"
          f"  起始 movements={mv0} PO={po0}\n{'='*74}")
    results = []
    t0 = time.time()
    await asyncio.gather(*[visitor(n, s, results) for n, s in V.items()])
    dt = time.time() - t0
    bad = [r for r in results if r[4]]
    for name, shown, v, info, isbad in results:
        mark = "❌" if isbad else "✅"
        print(f"  {mark} {name:10} | {shown[:34]:36} → {v:16} | {info[:56]}")
    mv1, po1 = n_mv(), n_po()
    print(f"\n{'='*74}")
    print(f"  斷言 {len(results)-len(bad)}/{len(results)}  耗時 {dt:.1f}s")
    exp_mv = mv0 + 3   # V1×2 + V6×1（prune 可能回收舊檔,用 >= 判斷差額訊息）
    print(f"  movements: {mv0} → {mv1}（confirm 匯出 3 次）")
    print(f"  PO_draft : {po0} → {po1}（confirm PO 1 次）")
    if mv1 - mv0 > 3:
        print("  ❌ 匯出檔多寫（可能串線 double-run）")
    if po1 - po0 > 1:
        print("  ❌ PO 多寫")
    print(f"{'='*74}")


asyncio.run(main())
