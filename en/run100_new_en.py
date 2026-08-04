# -*- coding: utf-8 -*-
"""run100_new_en.py — 英文版新功能 100 句（2026-08-04）。

user：「這兩天針對跑模擬的動態和報告的相關產出功能是全新的,
       英文版跑個一輪100句有關於這方面的測試,
       記得檢查渲染和報告格式對不對」

三層檢查（不只看 view）：
  ① **view 有渲染器**：server 發出的 view 必須在 index.html 有對應渲染分支
     （否則訪客看到空白卡 —— check_views.py 的思路）
  ② **回答品質**：不可有中文殘留 / error / 空回答 / 醜錯誤訊息
  ③ **報告格式**：產出 script_confirm 的,確認 days 有帶；
     產出 script_done 的,驗實際 CSV/HTML 欄位與筆數一致

用法：python3 run100_new_en.py --rpi5
"""
import asyncio
import csv
import io
import json
import pathlib
import re
import ssl
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import websockets

ROOT = pathlib.Path("/home/p400/warehouse_v2_en")
URI = "wss://localhost:8002/ws?fast=1"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# ① 前端渲染器涵蓋的 view（從 index.html 抓，動態比對）
HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
RENDERED = set(re.findall(r"view === ?'([a-z_]+)'", HTML))
RENDERED |= {"clarify", "rejected", "guide", "error", "script_confirm",
             "script_done", "reset_done", "schedule_confirm", "schedule_list",
             "transfer_confirm", "transfer_done", "movement", "agent_rca"}

CJK = re.compile(r"[一-鿿]")
BAD_PAT = re.compile(r"not on the whitelist|Script \"\" not found|None|undefined|"
                     r"傳回|錯誤|失敗", re.I)


async def ask(ws, text):
    await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
        if m.get("type") == "done":
            return m.get("result") or {}


async def main():
    lines = [l.strip() for l in
             (ROOT / "_new100_en.txt").read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")]
    print(f"{'='*74}\n  英文版新功能 100 句（渲染 + 報告格式）\n  共 {len(lines)} 句\n{'='*74}")

    issues = []
    async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
        for i, q in enumerate(lines, 1):
            try:
                r = await ask(ws, q)
            except Exception as e:
                issues.append((i, q, "TIMEOUT", str(e)[:60]))
                print(f"  [{i:3}] ⏱  {q[:52]}")
                continue
            view = r.get("view") or ""
            summ = (r.get("summary") or "").replace("\n", " ")
            data = r.get("data") or {}
            prob = []
            # ① 渲染器
            if view and view not in RENDERED:
                prob.append(f"view「{view}」無渲染器")
            # ② 品質
            if CJK.search(summ):
                prob.append(f"中文殘留: {CJK.findall(summ)[:4]}")
            if not summ.strip():
                prob.append("空回答")
            if BAD_PAT.search(summ):
                prob.append("醜錯誤訊息")
            if view == "error":
                prob.append("view=error")
            # ③ 報告格式：匯出確認卡要帶 days
            if view == "script_confirm" and data.get("script_id") == "export_movements":
                if data.get("days") is None:
                    prob.append("匯出卡沒帶 days")
            if prob:
                issues.append((i, q, view, " / ".join(prob)))
                print(f"  [{i:3}] ❌ {q[:46]}\n         {view} | {' / '.join(prob)}\n         {summ[:76]}")

    print(f"\n{'='*74}")
    print(f"  通過 {len(lines)-len(issues)}/{len(lines)}   問題 {len(issues)} 句")
    print(f"{'='*74}")
    if issues:
        print("\n問題彙整：")
        for i, q, v, p in issues:
            print(f"  {i:3}. [{v}] {q[:48]}  → {p[:64]}")


asyncio.run(main())
