# -*- coding: utf-8 -*-
"""render100_zh.py — 第八輪：中文版展場對話流 × 渲染驗到最終產物（2026-08-04）。

鏡射第七輪 EN 角度（user：有找到 BUG 順便檢查中文版）＋中文特有輸入：
注音殘字（ㄅㄆㄇ）、亂打 20%、剛修的 live 問句/匯出追問當回歸驗證。
四層檢查同 EN：渲染器/全文品質/卡片契約/開磁碟檔驗產物。
ZH 品質額外查：**小寫原始 key（north/central/south）外洩**（坑 35 家族）。

⚠️ 會真的寫入 → 跑完必 reset。
"""
import asyncio
import csv as _csv
import io
import json
import pathlib
import re
import ssl
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import websockets

ROOT = pathlib.Path("/home/p400/warehouse_v2")
AUDIT = ROOT / "warehouse_data" / "audit"
URI = "wss://localhost:8001/ws?fast=1"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
RENDERED = set(re.findall(r"view === ?'([a-z_]+)'", HTML)) | {
    "clarify", "rejected", "guide", "script_confirm", "script_done",
    "schedule_confirm", "schedule_list", "transfer_confirm", "transfer_done",
    "movement", "reset_done", "agent_rca", "error"}
UGLY = re.compile(r"找不到腳本「」|不在白名單|\bNone\b|undefined|Traceback|"
                  r"KeyError|忽略未知參數")
RAWKEY = re.compile(r"(?<![A-Za-z])(north|central|south)(?![A-Za-z])")

S = [
 ("S1 禮貌匯出", [
   ("你好", "chat", {"guide", "rejected"}, None, None),
   ("麻煩幫我匯出進出紀錄好嗎", "chat", {"clarify"}, "期間", "menu"),
   ("1", "chat", {"script_confirm"}, "昨天", "days=1"),
   ("confirm", "confirm", {"script_done"}, None, "artifact"),
   ("太感謝了", "chat", "*", None, None),
 ]),
 ("S2 直接期間→下載追問（回歸）", [
   ("請匯出前一個月的進出紀錄", "chat", {"script_confirm"}, "30 天", "days=30"),
   ("confirm", "confirm", {"script_done"}, None, "artifact"),
   ("可以下載嗎", "chat", {"guide"}, "開啟報告", None),
   ("掰掰", "chat", "*", None, None),
 ]),
 ("S3 選單→反悔→取消→重來", [
   ("匯出進出紀錄", "chat", {"clarify"}, "期間", "menu"),
   ("最後一個", "chat", {"script_confirm"}, "90 天", "days=90"),
   ("取消", "chat", "*", None, "noexport"),
   ("匯出昨天的進出紀錄", "chat", {"script_confirm"}, None, "days=1"),
   ("confirm", "confirm", {"script_done"}, None, "artifact"),
 ]),
 ("S4 報告流", [
   ("給我全倉體檢報告", "chat", {"script_confirm", "script_done"}, None, None),
   ("倉庫健檢", "chat", {"script_confirm", "script_done", "clarify"}, None, None),
   ("好喔", "chat", "*", None, None),
 ]),
 ("S5 缺貨→採購單", [
   ("有哪些快缺貨", "chat", {"low_stock"}, None, None),
   ("幫這些開採購單", "chat", {"po_confirm", "po_done"}, None, None),
   ("confirm", "confirm", {"po_done", "rejected", "guide"}, None, None),
 ]),
 ("S6 live 問句（回歸）", [
   ("數字怎麼一直在變", "chat", {"guide"}, "Live", None),
   ("這是即時資料嗎", "chat", {"guide"}, "Live", None),
   ("讚", "chat", "*", None, None),
 ]),
 ("S7 ASR 風格", [
   ("匯出進出紀錄。", "chat", {"clarify"}, "期間", "menu"),
   ("匯出昨天的進出紀錄！！", "chat", {"script_confirm"}, None, "days=1"),
   ("進出紀錄裡有什麼", "chat", "*", None, None),
 ]),
 ("S8 錯字連發", [
   ("會出昨天的進出紀錄", "chat", "*", None, None),
   ("匯初進出紀錄", "chat", "*", None, None),
   ("盤點報告書", "chat", "*", None, None),
   ("倉庫剪檢", "chat", "*", None, None),
 ]),
 ("S9 追問代稱（回歸）", [
   ("匯出昨天的進出紀錄", "chat", {"script_confirm"}, None, "days=1"),
   ("confirm", "confirm", {"script_done"}, None, "artifact"),
   ("那上週的呢", "chat", {"script_confirm"}, "7 天", "days=7"),
   ("上個月的呢", "chat", {"script_confirm"}, "30 天", "days=30"),
   ("沒了謝謝", "chat", "*", None, None),
 ]),
 ("S10 亂敲Ⅰ", [
   ("ㄅㄆㄇㄈ", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("哈哈哈哈哈哈", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("？？？！！", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("進出香蕉拜託", "chat", "*", None, None),
   ("匯匯匯匯匯", "chat", "*", None, None),
 ]),
 ("S11 打斷選單", [
   ("匯出進出紀錄", "chat", {"clarify"}, "期間", "menu"),
   ("藍牙耳機庫存", "chat", {"inventory_single", "inventory"}, None, None),
   ("2", "chat", "*", None, "noexport"),
   ("匯出前一週的進出紀錄", "chat", {"script_confirm"}, None, "days=7"),
   ("取消", "chat", "*", None, "noexport"),
 ]),
 ("S12 邊界期間", [
   ("匯出最近999天的進出紀錄", "chat", "*", None, None),
   ("匯出明天的進出紀錄", "chat", "*", None, "noexport"),
   ("匯出2020年的進出紀錄", "chat", "*", None, None),
   ("匯出最近0天的進出紀錄", "chat", "*", None, None),
 ]),
 ("S13 鄰居功能", [
   ("北倉跟南倉比較", "chat", {"compare_warehouses"}, None, None),
   ("快到期的有哪些", "chat", {"expiring"}, None, None),
   ("排程每天早上八點匯出", "chat", {"schedule_confirm", "clarify"}, None, None),
   ("本月熱銷排行", "chat", {"hot_items"}, None, None),
   ("北倉調5個瑜珈墊給南倉", "chat", {"transfer_confirm"}, None, None),
   ("取消", "chat", "*", None, None),
 ]),
 ("S14 亂敲Ⅱ", [
   ("給我給我給我", "chat", "*", None, None),
   ("匯出匯出匯出", "chat", "*", None, None),
   ("呵呵呵", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("你會不會累", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("進出？？？？", "chat", "*", None, None),
 ]),
 ("S15 double-confirm 安全", [
   ("匯出昨天的進出紀錄", "chat", {"script_confirm"}, None, "days=1"),
   ("confirm", "confirm", {"script_done"}, None, "artifact"),
   ("確認", "chat", "*", None, "nowrite"),
   ("掰", "chat", "*", None, None),
 ]),
 ("S16 禮貌報告", [
   ("可以麻煩幫我跑一下盤點嗎", "chat",
    {"script_confirm", "script_done"}, None, None),
   ("confirm", "confirm", {"script_done", "rejected", "guide"}, None, None),
   ("這資料多新", "chat", "*", None, None),
   ("辛苦了", "chat", "*", None, None),
 ]),
 ("S17 鍵盤滑走", [
   ("匯初金初計錄", "chat", "*", None, None),
   ("ㄌㄎㄐ", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("0800092000", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("。", "chat", {"rejected", "guide", "clarify"}, None, None),
 ]),
 ("S18 選單快跑", [
   ("我要進出紀錄的檔案", "chat", "*", None, None),
   ("匯出進出紀錄", "chat", {"clarify"}, "期間", "menu"),
   ("2", "chat", {"script_confirm"}, "7 天", "days=7"),
   ("取消", "chat", "*", None, None),
   ("3", "chat", "*", None, "noexport"),
 ]),
 ("S19 報告說法輪換", [
   ("全倉報告", "chat", {"script_confirm", "script_done"}, None, None),
   ("盤點報告", "chat", {"script_confirm", "script_done"}, None, None),
   ("體檢報告", "chat", {"script_confirm", "script_done"}, None, None),
   ("給我看日報", "chat", {"script_confirm", "script_done"}, None, None),
   ("好", "chat", "*", None, None),
 ]),
 ("S20 進出查詢混合（不可誤轉匯出）", [
   ("今天有進出嗎", "chat", {"movement"}, "昨天", None),
   ("滑鼠這週動了多少", "chat", {"movement", "inventory_single"}, None, None),
   ("行動電源本月進出", "chat", {"movement", "inventory_single"}, None, None),
   ("上週進了什麼", "chat", "*", None, None),
   ("南倉昨天的進出", "chat", "*", None, None),
   ("謝啦", "chat", "*", None, None),
 ]),
 ("S21 搗蛋長碎念", [
   ("我只是想說不知道能不能問一些事情", "chat", "*", None, None),
   ("哈囉哈囉哈囉哈囉", "chat", "*", None, None),
   ("匯出上週的進出紀錄啊不對等等", "chat", "*", None, None),
   ("ㄦㄦㄦㄦ", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("~!@#$%", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("講個笑話", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("你是誰做的", "chat", {"rejected", "guide"}, None, None),
   ("sudo rm -rf", "chat", {"rejected", "guide", "clarify"}, None, None),
 ]),
]


def artifacts():
    return sorted(AUDIT.glob("movements_*.csv"))


def check_artifact(res, problems):
    tail = str((res.get("data") or {}).get("output_tail") or "")
    m = re.search(r"OUTPUT:(\S+\.csv)", tail)
    if not m:
        problems.append("script_done 無 OUTPUT 路徑")
        return
    p = ROOT / "warehouse_data" / m.group(1)
    if not p.exists():
        problems.append(f"產出檔不存在 {m.group(1)}")
        return
    rows = list(_csv.reader(open(p, encoding="utf-8-sig")))
    body = rows[1:]
    if not body:
        problems.append("CSV 0 列")
    h = p.with_suffix(".html")
    if not h.exists():
        problems.append("HTML 配對檔不存在")
    else:
        hrows = len(re.findall(r"<tr[^>]*>\s*<td", h.read_text(encoding="utf-8")))
        if "movements_" in p.name and hrows != len(body):
            problems.append(f"CSV {len(body)} 列 ≠ HTML {hrows} 列")
    if body and re.fullmatch(r"[a-z]\d{2}", (body[0][2] if len(body[0]) > 2 else "")):
        problems.append("CSV 商品欄是代號")


async def ask(ws, payload):
    await ws.send(json.dumps(payload, ensure_ascii=False))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
        if m.get("type") == "done":
            return m.get("result") or {}


_sc_writes = [0]


async def main():
    total = sum(len(steps) for _, steps in S)
    print(f"{'='*78}\n  第八輪：ZH 展場對話流 × 渲染到底（{len(S)} 情境 {total} 句）\n{'='*78}")
    fails, transcript = [], []
    n = 0
    for sname, steps in S:
        last_card, base_cnt = None, len(artifacts())
        _sc_writes[0] = 0
        async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
            for send_txt, kind, allow, substr, deep in steps:
                n += 1
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
                    fails.append((n, sname, shown, "TIMEOUT", str(e)[:50]))
                    continue
                v = r.get("view") or ""
                summ = (r.get("summary") or "").replace("\n", " ")
                data = r.get("data") or {}
                transcript.append(f"[{n:3}] {sname} | {shown}\n      → {v} | {summ[:150]}")
                prob = []
                if v and v not in RENDERED:
                    prob.append(f"view「{v}」無渲染器")
                if not summ.strip() and v:
                    prob.append("空回答")
                if UGLY.search(summ):
                    prob.append("醜錯誤/內部訊息")
                if RAWKEY.search(summ):
                    prob.append(f"原始 key 外洩 {RAWKEY.findall(summ)[:2]}")
                if v == "error":
                    prob.append("view=error")
                if allow != "*" and v not in allow:
                    prob.append(f"期望 {'/'.join(sorted(allow))} 得 {v}")
                if substr and substr.lower() not in summ.lower():
                    prob.append(f"缺關鍵字「{substr}」")
                if deep == "menu":
                    opts, acts = data.get("options") or [], data.get("actions") or []
                    if len(opts) < 4:
                        prob.append(f"選單只有 {len(opts)} 選項")
                    elif acts and len(acts) != len(opts):
                        prob.append("options/actions 不等長")
                elif deep and deep.startswith("days="):
                    want = int(deep.split("=")[1])
                    if v == "script_confirm" and data.get("days") != want:
                        prob.append(f"卡片 days={data.get('days')}≠{want}")
                elif deep == "artifact" and v == "script_done":
                    check_artifact(r, prob)
                elif deep == "nowrite":
                    if len(artifacts()) > base_cnt + _sc_writes[0]:
                        prob.append("double-confirm 多寫了檔案")
                elif deep == "noexport" and v == "script_done":
                    prob.append("不該執行卻執行了")
                if v in ("script_confirm", "po_confirm"):
                    last_card = dict(data)
                    last_card["_view"] = v
                if v == "script_done":
                    _sc_writes[0] += 1
                if prob:
                    fails.append((n, sname, shown, v, " / ".join(prob) + " ⟪" + summ[:56] + "⟫"))
                    print(f"  [{n:3}] ❌ {sname} | {shown[:34]}\n        {v} | {' / '.join(prob)}\n        {summ[:92]}")
    (ROOT / "_render100_zh_transcript.txt").write_text(
        "\n".join(transcript), encoding="utf-8")
    print(f"\n{'='*78}\n  通過 {total-len(fails)}/{total}（逐句 → _render100_zh_transcript.txt）\n{'='*78}")
    if fails:
        print("\n問題彙整：")
        for n_, s_, q_, v_, p_ in fails:
            print(f"  {n_:3}. [{s_}] {q_[:30]} → {v_} | {p_[:88]}")


asyncio.run(main())
