# -*- coding: utf-8 -*-
"""
make_en_ui3.py — templates/index.html 第三輪：把**所有**中文清乾淨（含註解）。

前兩輪（make_en_ui.py / make_en_ui2.py）處理了快捷選單、主要標籤與表頭，
這輪把剩下的 159 行全部處理掉：HITL 卡片內文、鉤子入口、計算說明彈窗、
語音狀態、效能徽章、以及 CSS/JS 註解。

⚠️ 這輪同時修「前輪批次替換造成的混血」——短字串先替換咬進已英文化的字：
    辨識Central / 聆聽Central / 送出Central / 不can be deleted /
    Confirm新增 / Confirm調貨 / Cancel新增 …
    這類要**先**修（放 FIX_MIXED），否則後面的規則會對不上。

⚠️ JS 引號安全：目標字串若含撇號（don't / didn't），確認它落在反引號模板
    字串或雙引號內。第一輪的 'Didn't catch that' 讓整個 <script> 語法錯誤、
    全站 JS 不執行（能力地圖不出現、卡 Loading）。本腳本一律避免產生撇號，
    改用 do not / cannot 等寫法。

可重複執行：備份 index.html.zh.bak3（存在則不覆蓋）。
用法：cd warehouse_v2/en && <Python311> make_en_ui3.py
"""
import io
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
TPL = HERE / "templates" / "index.html"
BAK = HERE / "templates" / "index.html.zh.bak3"

# ── ① 混血傷害（前輪短字串誤替換）——必須最先修 ─────────────────────────
FIX_MIXED = [
    ("辨識Central…", "Recognizing…"),
    ("聆聽Central… 講完會自動送出", "Listening… will send automatically when you stop"),
    ("聆聽Central... 請說話", "Listening... please speak"),
    ("✓ 辨識done、送出Central...", "✓ Recognized, sending..."),
    ("⚠️ 辨識failed、請再試一次", "⚠️ Recognition failed, please try again"),
    ("不can be deleted。Delete後將從 items.csv 移除並重生種子資料。",
     "cannot be deleted. Deleting removes it from items.csv and regenerates seed data."),
    ("This item is system protected，不can be deleted。",
     "This item is system protected and cannot be deleted."),
    ("⚠ 系統預設的 60 項商品不", "⚠ The 60 built-in demo items "),
    ("✅ Confirm新增", "✅ Confirm"),
    ("✅ Confirm調貨", "✅ Confirm transfer"),
    ("✅ ConfirmDelete (", "✅ Confirm delete ("),
    ("✅ Confirm建立排程", "✅ Confirm schedule"),
    ("Cancel新增", "Cancel"),
    ("🔄 Confirm調貨 — 請Confirm", "🔄 Confirm Transfer"),
    ("✅ 調貨Done", "✅ Transfer complete"),
    ("Confirm${data.direction_label} — 請Confirm", "Confirm ${data.direction_label}"),
    ("Delete排程 — 請Confirm", "Delete schedule"),
    ("Delete警示規則 — 請Confirm", "Delete alert rule"),
    ("delete item — 請Confirm", "Delete item"),
    ("不再auto-run ，此動作無法復原。", "will no longer auto-run. This cannot be undone."),
    ("將定時auto-run ，done後推送通知到此對話。",
     "will run on schedule and push a notification here when done."),
    ("執行${msg.ok ? 'done' : 'failed'}", "${msg.ok ? 'succeeded' : 'failed'}"),
]

# ── ② 訪客可見文字（長字串在前）───────────────────────────────────────
REPL = [
    # HITL 卡片
    ("Confirm後將寫入 items.csv 並自動重生種子資料。",
     "On confirm this is written to items.csv and seed data is regenerated."),
    ("Confirm後將直接寫入庫存資料，重開伺服器或重整頁面不會消失。",
     "On confirm this is written to stock data; it survives server restarts and page reloads."),
    ("Confirm後 ${data.from_label} 扣 ${data.qty} units、${data.to_label} 加 ${data.qty} units，總量不變。重開伺服器或重整頁面不會消失。",
     "On confirm ${data.from_label} loses ${data.qty} units and ${data.to_label} gains ${data.qty}; "
     "total is unchanged. Survives server restarts and page reloads."),
    ("Confirm後將建立採購草稿存入 PO_draft/，並記錄至 audit log。",
     "On confirm a purchase draft is saved to PO_draft/ and recorded in the audit log."),
    ("Confirm後寫入 alert_rules.json 並記錄 audit log。",
     "On confirm this is written to alert_rules.json and recorded in the audit log."),
    ("此操作將寫入正式資料，執行後可透過 audit log 查詢記錄。",
     "This writes to live data; the run is queryable in the audit log afterwards."),
    ("⚠ Delete後此警示不再主動通知，此動作無法復原。",
     "⚠ After deletion this alert stops notifying. This cannot be undone."),
    ("📦 add item — 步驟 ${stepData.step}/4", "📦 Add item — step ${stepData.step}/4"),
    ("✋ 採購授權 — 請Confirm後送出", "✋ Purchase authorization — confirm to submit"),
    ("🔔 設定警示 — 請Confirm", "🔔 Set alert"),
    ("⏰ 設定定時排程 — 請Confirm", "⏰ Set schedule"),
    ("✅ 授權建立採購單", "✅ Authorize purchase order"),
    ("✅ 授權啟用警示", "✅ Authorize alert"),
    ("✅ 授權執行", "✅ Authorize run"),
    ("❌ 退回重擬", "❌ Reject"),
    ("✅ 採購草稿已建立", "✅ Purchase draft created"),
    ("共 <b>${lines.length}</b> 項商品需補貨，預估金額",
     "<b>${lines.length}</b> items need restocking, estimated"),
    ("當【${scopeTxt}】發生「${data.condition_label}」",
     "When [${scopeTxt}] hits \"${data.condition_label}\""),
    ("規則 ID：${data.rule_id} · 啟用後背景掃描自動套用",
     "Rule ID: ${data.rule_id} · applied automatically by background scan once enabled"),
    ("排程 ID：${data.job_id} · 寫入 schedule_jobs.json，重啟不消失",
     "Job ID: ${data.job_id} · saved to schedule_jobs.json, survives restarts"),
    ("當【${(data.scope_names||[]).join('、')||'all items'}】發生「${data.condition_label}」時主動通知。",
     "Notifies when [${(data.scope_names||[]).join(', ')||'all items'}] hits \"${data.condition_label}\"."),
    (" 項，存到 PO_draft/", " lines, saved to PO_draft/"),
    ("🗂️ 資料區概覽（Agent 動態掃描）", "🗂️ Data areas (scanned by the Agent)"),
    ("（${data.total||rows.length} 檔）", " (${data.total||rows.length} files)"),
    ("📈 近兩個月出庫變化（變化最大）", "📈 Outbound change over the last two months (biggest movers)"),
    # 表格欄位（第二輪漏的）
    ("<td>別</td>", "<td>Warehouse</td>"),
    ("<td>庫存變化</td>", "<td>Stock change</td>"),
    ("<td>調貨數量</td>", "<td>Transfer qty</td>"),
    ("<td>作業</td>", "<td>Job</td>"),
    ("<td>頻率</td>", "<td>Frequency</td>"),
    ("<td>條件</td>", "<td>Condition</td>"),
    ("<td>範圍</td>", "<td>Scope</td>"),
    # 清單/單位
    ("${invTotal} 項${invRows.length < invTotal ? `，顯示前 ${invRows.length} 筆` : ''}）",
     "${invTotal} items${invRows.length < invTotal ? `, showing first ${invRows.length}` : ''})"),
    ("${r.unit || '件'}", "${r.unit || 'units'}"),
    ("(days + '天')", "(days + 'd')"),
    ("days內共 ${rows.length} batches", "days: ${rows.length} batches"),
    ("esc(c.unit||'件')", "esc(c.unit||'units')"),
    # clarify
    ("Cancel，我不查了", "Cancel"),
    ("點選項目，或輸入數字 1~${opts.length} / 直接輸入完整問題",
     "Tap an option, type 1-${opts.length}, or type your full question"),
    # 計算說明彈窗
    ("🔍 ${conf}% 是怎麼算出來的?", "🔍 How is ${conf}% calculated?"),
    ("在買「${aname}」的 <b>${anchorOrders}</b> 張訂單裡,<br>",
     "Of the <b>${anchorOrders}</b> orders containing \"${aname}\",<br>"),
    ("有 <b>${co}</b> 張也買了「${bname}」<br>",
     "<b>${co}</b> also contained \"${bname}\"<br>"),
    ("📊 隨便兩個商品「剛好」同單的機率只有 <b>${baseline}%</b>。<br>",
     "📊 Two random items land in the same order only <b>${baseline}%</b> of the time.<br>"),
    ("這組高達 ${conf}%,是隨機的 <span class=\"hi\">約 ${ratio.toFixed(0)} 倍</span> —",
     "This pair hits ${conf}% — about <span class=\"hi\">${ratio.toFixed(0)}x random</span> —"),
    ("代表它們是<span class=\"hi\">真的常一起被買</span>,不是巧合。",
     "so they are <span class=\"hi\">genuinely bought together</span>, not a coincidence."),
    ("這就是「購物籃分析」,跟 Amazon「買了這個的人也買了」同一套作法 —",
     "This is market basket analysis — the same method behind Amazon's \"customers also bought\" —"),
    ("系統真的去數了訂單算出來,不是寫死的。",
     "computed by actually counting orders, not hard-coded."),
    ("懂了！", "Got it!"),
    ("⏳ 「${bname}」還能cover ${days} days?", "⏳ How can \"${bname}\" cover ${days} days?"),
    ("現有stock <b>${stock}</b> units<br>", "Current stock <b>${stock}</b> units<br>"),
    ("÷ 每天約賣 <b>${burn}</b> units<br>", "÷ about <b>${burn}</b> units sold per day<br>"),
    ("📈 每天賣多少 = 看近 30 days平均,再依趨勢加權:<br>",
     "📈 Daily rate = 30-day average, weighted by trend:<br>"),
    ("趨勢 ${trendTxt}<br>", "Trend ${trendTxt}<br>"),
    ("🔔 庫存跌到安全線(${safety} units)或撐不到 7 days → 就提醒補貨。",
     "🔔 Alerts when stock hits the safety line (${safety} units) or drops under 7 days of cover."),
    ("每各自補到能cover ${target} days、含叫貨緩衝(缺最多的排前面)",
     "each restocked to ${target} days of cover, including lead-time buffer (most urgent first)"),
    ("這叫「線性消耗預測」— 依最近賣法往前推,儲補貨常用算法,不是亂猜。",
     "This is linear burn-rate forecasting — projecting from recent sales, a standard restocking method, not guesswork."),
    # follow-up 鈕 / 快捷
    ("🔗 ${r.name} 還連到啥", "🔗 What goes with ${r.name}"),
    ("買${r.name}的人還會買什麼", "what goes with ${r.name}"),
    ("📦 ${data.anchor_name} 庫存", "📦 ${data.anchor_name} stock"),
    ("${data.anchor_name}庫存", "${data.anchor_name} stock"),
    ("📦 ${r.name} 庫存", "📦 ${r.name} stock"),
    ("${r.name}庫存", "${r.name} stock"),
    ("'North到期'", "'North expiring'"),
    ("'Central到期'", "'Central expiring'"),
    ("'South到期'", "'South expiring'"),
    ("'⏰ 到期警示'", "'⏰ Expiring'"),
    ("想查哪一類庫存？", "Which category do you want to check?"),
    ("${CATEGORY_LABEL[c]}庫存", "${CATEGORY_LABEL[c]} stock"),
    ("想看哪個商品「買的人還會買什麼」?點一個試試:",
     "Which item do you want the \"also bought\" list for? Tap one:"),
    ("📥 Inbound / Outbound，一句話就好（會先給你Confirm卡，Confirm後才寫入）。點一個試試:",
     "📥 Inbound / Outbound in one sentence (you get a confirm card first). Tap one:"),
    ("🔄 把貨從一個調到另一個（來源不夠會擋下）。點一個試試:",
     "🔄 Move stock between warehouses (blocked if the source is short). Tap one:"),
    ("↩️ 客人退貨，庫存會加回來（一樣先給Confirm卡）。點一個試試:",
     "↩️ Customer returns add stock back (also via a confirm card). Tap one:"),
    ("🤖 智慧管家能「追原因 / 改設定 / 跑作業 / 管商品」。挑一個看看 Agent 怎麼多步思考:",
     "🤖 The Agent can trace causes, change settings, run jobs and manage items. "
     "Pick one to see multi-step reasoning:"),
    ("📤 North賣掉8支無線滑鼠", "📤 North sold 8 wireless mice"),
    ("'North賣掉8支無線滑鼠'", "'north shipped 8 wireless mouse'"),
    ("🔄 South撥15個行動電源到Central", "🔄 South transfers 15 power banks to Central"),
    ("'South撥15個行動電源到Central'", "'transfer 15 power banks from south to central'"),
    ("↩️ 客人退了3個bluetooth earphones", "↩️ Customer returned 3 bluetooth earphones"),
    ("↩️ 顧客退2台藍牙喇叭", "↩️ Customer returned 2 bluetooth speakers"),
    ("'South顧客退2台藍牙喇叭'", "'customer returned 2 bluetooth speaker at south'"),
    ("🔍 追check stock對不上", "🔍 Trace a stock mismatch"),
    ("📄 產全體檢報告(含圖表)", "📄 Full health report (with charts)"),
    ("📋 缺貨→自動產採購單", "📋 Low stock → purchase order"),
    ("🔔 設警示(缺貨就通知)", "🔔 Set a low-stock alert"),
    ("'bluetooth earphones缺貨就通知我'", "'alert me when bluetooth earphones run low'"),
    ("📈 這月vs上月變化", "📈 This month vs last month"),
    ("'這個月跟上個月哪些變化大'", "'compare the last two months'"),
    ("⚙️ South安全庫存全部+30", "⚙️ South safety stock +30"),
    ("🛠️ 跑月底盤點", "🛠️ Run month-end stocktake"),
    ("'幫我跑一次月底盤點'", "'run the month-end stocktake'"),
    # 鉤子入口
    ("🍺 買diapers的也買了…?", "🍺 What sells with diapers?"),
    ("☕ 買coffee machine還要囤啥?", "☕ What to stock with a coffee machine?"),
    ("🏕️ 買tent的人還扛了?", "🏕️ What else do tent buyers take?"),
    ("🎧 買耳機順手帶了啥?", "🎧 What goes with earphones?"),
    ("💪 買yoga mat的還買?", "💪 What else with a yoga mat?"),
    ("🏃 跑者購物車有啥?", "🏃 What is in a runner's basket?"),
    ("❄️ 買down jacket還配?", "❄️ What pairs with a down jacket?"),
    ("🧹 大掃除要囤哪些?", "🧹 What to stock for a big clean?"),
    ("🎲 隨機驚喜", "🎲 Surprise me"),
    # 開場白
    ("👋 嗨！我是管 Agent。你可以「直接打字問我」，像跟同事講話一樣。\\n\\n",
     "👋 Hi! I am the Warehouse Agent. Just type your question like you would ask a colleague.\\n\\n"),
    ("我會這幾類事 —— 不知道怎麼問的話，下面的範例點一下就會幫你送出 👇",
     "Here is what I can do — not sure how to ask? Tap any example below to send it 👇"),
    # 語音狀態
    ("❌ 此瀏覽器不支援錄音", "❌ This browser does not support recording"),
    ("'沒聽出內容'", "'nothing recognized'"),
    ("'、請再試一次'", "' - please try again'"),
    ("⚠️ 語音辨識需要網路（iOS Safari 離線可用）",
     "⚠️ Speech recognition needs a network connection (iOS Safari works offline)"),
    ("語音錯誤：${ev.error}", "Voice error: ${ev.error}"),
    ("語音啟動failed：", "Voice start failed: "),
    ("此瀏覽器不支援語音輸入（建議用 Chrome / Edge / Safari）",
     "This browser does not support voice input (use Chrome / Edge / Safari)"),
    # 效能徽章
    ("效能 (按 P)", "Performance (press P)"),
    ("自研晶片", "In-house chip"),
    ("生成速度", "Generation"),
    ("本次推論", "This inference"),
    ("CPU 推論 · 即時量測", "CPU inference · live measurement"),
    ("⚡ 智慧路由 ${p.ms}ms", "⚡ Smart routing ${p.ms}ms"),
    ("content: ' ✕ 收合';", "content: ' ✕ collapse';"),
    # 註解（不影響顯示，但一併英文化保持一致）
    ("/* 深藍 */", "/* deep blue */"),
    ("/* Central藍 */", "/* mid blue */"),
    ("/* 出貨橘 */", "/* outbound orange */"),
    ("/* 進貨綠 */", "/* inbound green */"),
    ("/* 缺貨紅 */", "/* low-stock red */"),
    ("/* TOP 3 金 */", "/* TOP 3 gold */"),
]


def main() -> None:
    if not TPL.exists():
        print(f"[error] {TPL} not found")
        return
    if not BAK.exists():
        shutil.copy2(TPL, BAK)
        print(f"backup -> {BAK.name}")
    s = io.open(TPL, encoding="utf-8").read()

    n = 0
    miss = []
    for a, b in FIX_MIXED + REPL:
        c = s.count(a)
        if c:
            s = s.replace(a, b)
            n += c
        else:
            miss.append(a[:55])

    io.open(TPL, "w", encoding="utf-8").write(s)

    lines = s.split("\n")
    left = [(i, l.strip()) for i, l in enumerate(lines, 1)
            if re.search(r"[一-鿿]", l)]
    io.open(HERE / "_left.txt", "w", encoding="utf-8").write(
        "\n".join(f"{i}|{t}" for i, t in left))
    print(f"replaced={n}  not_found={len(miss)}  remaining_zh_lines={len(left)}")
    for m in miss[:12]:
        print(f"  MISS: {m}")


if __name__ == "__main__":
    main()
