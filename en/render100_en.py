# -*- coding: utf-8 -*-
"""render100_en.py — 第七輪英文 100 句：展場對話流 × 渲染驗到最終產物（2026-08-04）。

角度（坑 20：換產生源,與前六輪全部不同）：
  前六輪＝單句意圖對應/選單實走/HITL天數。這輪＝**多輪同連線對話流**：
  禮貌開場→選單→序數→確認→追問→告別、打斷選單、double-confirm、
  ASR 風格大小寫/撇號、15-20% 亂敲（feedback_demo_mash_input 鐵則）。

「渲染到底」四層（user 兩次抓到我只看半路——確認卡文字≠最終產物）：
  ① view 有前端渲染器（check_views 思路,動態抓 index.html）
  ② 回答全文品質：非空/無中文殘留/無醜錯誤/無內部代號
  ③ 卡片契約：clarify 有 options（有 actions 則等長）;
     匯出確認卡必帶 days;PO 卡有金額或行數
  ④ **最終產物**：script_done 直接開磁碟檔驗列數>0 + CSV/HTML 配對;
     double-confirm 後檔案數不可多長（不重複寫入）

⚠️ 會真的寫入（confirm 落地）→ 跑完必 reset。
用法：python3 render100_en.py --rpi5
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

ROOT = pathlib.Path("/home/p400/warehouse_v2_en")
AUDIT = ROOT / "warehouse_data" / "audit"
URI = "wss://localhost:8002/ws?fast=1"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
RENDERED = set(re.findall(r"view === ?'([a-z_]+)'", HTML)) | {
    "clarify", "rejected", "guide", "script_confirm", "script_done",
    "schedule_confirm", "schedule_list", "transfer_confirm", "transfer_done",
    "movement", "reset_done", "agent_rca", "error"}
CJK = re.compile(r"[一-鿿]")
UGLY = re.compile(r"not on the whitelist|Script \"\" not found|"
                  r"\bNone\b|undefined|Traceback|KeyError|忽略未知參數", re.I)

# ── 劇本：každý 情境開新連線。步驟 = (send, kind, allow, substr, deep)
#   kind: chat | menu:N（點上一個選單第 N 選項的 action）| confirm（按上一張卡）
#   allow: view 集合,"*"=不限（只做品質檢查）; substr: 回答需含(不分大小寫)
#   deep: None | "menu" | "days=N" | "artifact" | "nowrite" | "noexport"
S = [
 ("S1 禮貌匯出全流程", [
   ("hi there", "chat", {"guide", "rejected", "chat"}, None, None),
   ("um, could you possibly export the movement records for me?", "chat",
    {"clarify"}, "period", "menu"),
   ("1", "chat", {"script_confirm"}, "yesterday", "days=1"),
   ("confirm", "confirm", {"script_done"}, None, "artifact"),
   ("thanks so much!", "chat", "*", None, None),
 ]),
 ("S2 直接期間→下載追問", [
   ("please export the movements from the last month", "chat",
    {"script_confirm"}, "30 days", "days=30"),
   ("confirm", "confirm", {"script_done"}, None, "artifact"),
   ("can i download it", "chat", "*", None, None),
   ("bye", "chat", "*", None, None),
 ]),
 ("S3 選單→反悔→取消→重來", [
   ("export the movement log", "chat", {"clarify"}, "period", "menu"),
   ("the last one", "chat", {"script_confirm"}, "90 days", "days=90"),
   ("cancel", "chat", "*", None, "noexport"),
   ("export movements yesterday", "chat", {"script_confirm"}, None, "days=1"),
   ("confirm", "confirm", {"script_done"}, None, "artifact"),
 ]),
 ("S4 報告流＋追問", [
   ("i want a full warehouse report", "chat",
    {"script_confirm", "script_done"}, None, None),
   ("warehouse health check", "chat", {"script_confirm", "script_done"}, None, None),
   ("hmm ok", "chat", "*", None, None),
 ]),
 ("S5 缺貨→開採購單→確認", [
   ("what items are running low?", "chat", {"low_stock"}, None, None),
   ("create a purchase order for those", "chat",
    {"po_confirm", "po_done"}, None, None),
   ("confirm", "confirm", {"po_done", "rejected", "guide"}, None, None),
 ]),
 ("S6 動態模擬提問", [
   ("why do the numbers keep changing?", "chat", "*", None, None),
   ("is this real time data", "chat", "*", None, None),
   ("cool", "chat", "*", None, None),
 ]),
 ("S7 ASR 風格", [
   ("EXPORT MOVEMENTS LAST WEEK", "chat", {"script_confirm"}, "7 days", "days=7"),
   ("Export The Movement Log For Yesterday.", "chat", {"script_confirm"}, None, "days=1"),
   ("what's in the movement log", "chat", "*", None, None),
   ("export movement's last month", "chat", "*", None, None),
 ]),
 ("S8 錯字連發", [
   ("exprot the movment log", "chat", {"clarify", "script_confirm"}, None, None),
   ("gnerate a report", "chat", "*", None, None),
   ("warehose health check", "chat", "*", None, None),
   ("stok audit", "chat", "*", None, None),
 ]),
 ("S9 追問與代稱", [
   ("export movements yesterday", "chat", {"script_confirm"}, None, "days=1"),
   ("confirm", "confirm", {"script_done"}, None, "artifact"),
   ("and last week too", "chat", "*", None, None),
   ("what about last month", "chat", "*", None, None),
   ("ok that's all", "chat", "*", None, None),
 ]),
 ("S10 亂敲Ⅰ", [
   ("asdfjkl;", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("qwerty 123456", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("???!!!...", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("movement banana quarter please", "chat", "*", None, None),
   ("exporttttttt", "chat", "*", None, None),
 ]),
 ("S11 打斷選單→序數失效驗證", [
   ("export the movement log", "chat", {"clarify"}, "period", "menu"),
   ("bluetooth earphones stock", "chat", {"inventory_single", "inventory"}, None, None),
   ("2", "chat", "*", None, "noexport"),
   ("export movements last week", "chat", {"script_confirm"}, None, "days=7"),
   ("cancel", "chat", "*", None, "noexport"),
 ]),
 ("S12 邊界期間", [
   ("export movements last 999 days", "chat", "*", None, None),
   ("export movements tomorrow", "chat", "*", None, "noexport"),
   ("export movements for 2020", "chat", "*", None, None),
   ("export movements last 0 days", "chat", "*", None, None),
   ("export half a year of movements", "chat", "*", None, None),
 ]),
 ("S13 鄰居功能不被匯出污染", [
   ("compare north and south", "chat", {"compare_warehouses"}, None, None),
   ("what is expiring soon", "chat", {"expiring"}, None, None),
   ("schedule a daily export at 8am", "chat",
    {"schedule_confirm", "clarify"}, None, None),
   ("best sellers this month", "chat", {"hot_items"}, None, None),
   ("transfer 5 yoga mats from north to south", "chat",
    {"transfer_confirm"}, None, None),
   ("cancel", "chat", "*", None, None),
 ]),
 ("S14 亂敲Ⅱ＋混英", [
   ("give me give me give me", "chat", "*", None, None),
   ("export export export", "chat", "*", None, None),
   ("aaaaaaaa bbbb", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("do u even lift bro", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("movements?????", "chat", "*", None, None),
 ]),
 ("S15 double-confirm 安全", [
   ("export movements yesterday", "chat", {"script_confirm"}, None, "days=1"),
   ("confirm", "confirm", {"script_done"}, None, "artifact"),
   ("confirm", "chat", "*", None, "nowrite"),
   ("bye bye", "chat", "*", None, None),
 ]),
 ("S16 禮貌報告＋口語", [
   ("hey, would you mind running a quick stock audit for me?", "chat",
    {"script_confirm", "script_done"}, None, None),
   ("confirm", "confirm", {"script_done", "rejected", "guide"}, None, None),
   ("how fresh is this data?", "chat", "*", None, None),
   ("cheers", "chat", "*", None, None),
 ]),
 ("S17 亂敲Ⅲ＋鍵盤滑走", [
   ("ex[prt mpvements", "chat", "*", None, None),
   ("lkjhgfdsa", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("1234567890", "chat", {"rejected", "guide", "clarify"}, None, None),
   (".", "chat", {"rejected", "guide", "clarify"}, None, None),
 ]),
 ("S18 匯出選單全選項快跑", [
   ("i need the movement records", "chat", {"clarify"}, "period", "menu"),
   ("2", "chat", {"script_confirm"}, "7 days", "days=7"),
   ("cancel", "chat", "*", None, None),
   ("give me the movement log", "chat", {"clarify"}, "period", "menu"),
   ("3", "chat", {"script_confirm"}, "30 days", "days=30"),
   ("cancel", "chat", "*", None, None),
 ]),
 ("S19 報告說法輪換", [
   ("full inventory report", "chat", {"script_confirm", "script_done"}, None, None),
   ("stocktake report", "chat", {"script_confirm", "script_done"}, None, None),
   ("audit report", "chat", {"script_confirm", "script_done"}, None, None),
   ("show me the daily report", "chat", {"script_confirm", "script_done"}, None, None),
   ("i want the health report", "chat", {"script_confirm", "script_done"}, None, None),
   ("ok", "chat", "*", None, None),
 ]),
 ("S20 進出查詢混合（不可誤轉匯出）", [
   ("any movements today", "chat", {"movement"}, "yesterday", None),
   ("how many wireless mouse moved this week", "chat",
    {"movement", "inventory_single"}, None, None),
   ("power bank in and out this month", "chat",
    {"movement", "inventory_single"}, None, None),
   ("what came in last week", "chat", "*", None, None),
   ("south warehouse movements yesterday", "chat", "*", None, None),
   ("thanks", "chat", "*", None, None),
 ]),
 ("S21 亂敲Ⅳ＋搗蛋長碎念", [
   ("i was just wondering if maybe you could possibly tell me something about stuff",
    "chat", "*", None, None),
   ("hello hello hello hello hello hello", "chat", "*", None, None),
   ("export movements last week but actually no wait", "chat", "*", None, None),
   ("zzzzzzzz", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("!@#$%^&*()", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("tell me a joke", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("who made you", "chat", {"rejected", "guide", "clarify"}, None, None),
   ("sudo rm -rf everything", "chat", {"rejected", "guide", "clarify"}, None, None),
 ]),
]


def artifacts():
    return sorted(AUDIT.glob("movements_*.csv"))


def check_artifact(res, problems):
    """script_done → 開磁碟檔驗到底。"""
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
    # 欄位要可讀（商品欄非代號）
    if body and re.fullmatch(r"[a-z]\d{2}", (body[0][2] if len(body[0]) > 2 else "")):
        problems.append("CSV 商品欄是代號")


async def ask(ws, payload):
    await ws.send(json.dumps(payload, ensure_ascii=False))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
        if m.get("type") == "done":
            return m.get("result") or {}


async def main():
    total = sum(len(steps) for _, steps in S)
    print(f"{'='*78}\n  第七輪：展場對話流 × 渲染驗到最終產物（{len(S)} 情境 {total} 句）\n{'='*78}")
    fails, transcript = [], []
    n = 0
    for sname, steps in S:
        last_menu, last_card, base_cnt = None, None, len(artifacts())
        async with websockets.connect(URI, ssl=CTX, max_size=None) as ws:
            for send_txt, kind, allow, substr, deep in steps:
                n += 1
                # 組 payload
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
                # ① 渲染器
                if v and v not in RENDERED:
                    prob.append(f"view「{v}」無渲染器")
                # ② 全文品質
                if not summ.strip() and v not in ("", None):
                    prob.append("空回答")
                if CJK.search(summ):
                    prob.append(f"中文殘留 {CJK.findall(summ)[:3]}")
                if UGLY.search(summ):
                    prob.append("醜錯誤/內部訊息")
                if v == "error":
                    prob.append("view=error")
                # 期望 view
                if allow != "*" and v not in allow:
                    prob.append(f"期望 {'/'.join(sorted(allow))} 得 {v}")
                if substr and substr.lower() not in summ.lower():
                    prob.append(f"缺關鍵字「{substr}」")
                # ③④ deep
                if deep == "menu":
                    opts, acts = data.get("options") or [], data.get("actions") or []
                    if len(opts) < 4:
                        prob.append(f"選單只有 {len(opts)} 選項")
                    elif acts and len(acts) != len(opts):
                        prob.append("options/actions 不等長")
                    last_menu = data
                elif deep and deep.startswith("days="):
                    want = int(deep.split("=")[1])
                    if v == "script_confirm" and data.get("days") != want:
                        prob.append(f"卡片 days={data.get('days')}≠{want}")
                elif deep == "artifact" and v == "script_done":
                    check_artifact(r, prob)
                elif deep == "nowrite":
                    if len(artifacts()) > base_cnt + _sc_writes[0]:
                        prob.append("double-confirm 多寫了檔案")
                elif deep == "noexport" and v in ("script_done",):
                    prob.append("不該執行卻執行了")
                # 記住卡片
                if v in ("script_confirm", "po_confirm"):
                    last_card = dict(data)
                    last_card["_view"] = v
                if v == "script_done":
                    _sc_writes[0] += 1
                if prob:
                    fails.append((n, sname, shown, v, " / ".join(prob) + " ⟪" + summ[:60] + "⟫"))
                    print(f"  [{n:3}] ❌ {sname} | {shown[:38]}\n        {v} | {' / '.join(prob)}\n        {summ[:96]}")
        _sc_writes[0] = 0
    (ROOT / "_render100_transcript.txt").write_text(
        "\n".join(transcript), encoding="utf-8")
    print(f"\n{'='*78}\n  通過 {total-len(fails)}/{total}（全文逐句紀錄 → _render100_transcript.txt）")
    print(f"{'='*78}")
    if fails:
        print("\n問題彙整：")
        for n_, s_, q_, v_, p_ in fails:
            print(f"  {n_:3}. [{s_}] {q_[:36]} → {v_} | {p_[:90]}")


_sc_writes = [0]
asyncio.run(main())
