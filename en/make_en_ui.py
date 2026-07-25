# -*- coding: utf-8 -*-
"""
make_en_ui.py — 前端 templates/index.html 介面文字英文化。
⚠️ 不只是外觀：選單/快捷句是**會被送到後端的查詢字串**，英文版後端已擋中文，
   不翻譯的話訪客一點就被 rejected。
原檔備份 templates/index.html.zh.bak（只備一次）。可重複執行。
用法：python make_en_ui.py
"""
import io, shutil
from pathlib import Path

F = Path(__file__).parent / "templates" / "index.html"

# (中文, 英文) — 長字串排前面，避免短字串先替換造成殘句
PAIRS = [
    # ── 選單/快捷查詢句（會送後端，必須英文）──
    ("北倉進了藍牙耳機50件", "north received 50 bluetooth earphones"),
    ("南倉出貨行動電源20個", "south shipped 20 power banks"),
    ("中倉進三箱衛生紙", "central received 3 boxes of facial tissue"),
    ("北倉調30個藍牙耳機給南倉", "transfer 30 bluetooth earphones from north to south"),
    ("中倉搬20台藍牙喇叭到北倉", "move 20 bluetooth speakers from central to north"),
    ("北倉客人退了3個藍牙耳機", "customer returned 3 bluetooth earphones at north"),
    ("中倉被退5個智慧手環", "5 smart bands returned at central"),
    ("北倉跟中倉商品數量比較", "compare north and central by item count"),
    ("中倉跟南倉週轉率比較", "compare central and south by turnover"),
    ("幫我出個全倉體檢報告", "give me a full warehouse report"),
    ("幫我把缺貨的產採購單", "create a purchase order for low stock items"),
    ("南倉安全庫存全部加30", "increase safety stock by 30 in south"),
    ("智慧手環怎麼少這麼多", "why are smart bands down so much"),
    ("行動電源帳對不上", "power bank numbers dont match"),
    ("買${kw}的人還會買什麼", "what else do ${kw} buyers get"),
    ("${kw} 本週進出", "${kw} movements this week"),
    ("行動電源還有多少", "how many power banks left"),
    ("看一下悶燒罐", "check the thermal food jar"),
    ("這週哪些賣最好", "what sold best this week"),
    ("缺貨的有哪些", "whats running low"),
    ("快到期的有哪些", "whats expiring soon"),
    ("藍牙耳機庫存", "bluetooth earphones stock"),
    ("本週熱銷", "best sellers this week"),
    ("🚨 庫存警示", "🚨 Low stock"),
    ("🔥 本週熱銷", "🔥 Best sellers this week"),
    ("🎲 隨機來一個", "🎲 Surprise me"),
    ("📄 出全倉體檢報告", "📄 Full warehouse report"),
    ("查庫存", "check stock"),
    ("追查「帳對不上」", "trace a mismatch"),
    ("進貨 / 出貨", "Inbound / Outbound"),
    ("調貨（倉間調撥）", "Transfer (between warehouses)"),
    ("退貨（客人退、庫存加回）", "Returns (customer returns, stock added back)"),
    ("缺貨 / 到期 / 熱銷", "Low stock / Expiring / Best sellers"),
    ("產生報告 / 採購單", "Reports / Purchase orders"),
    ("改設定 / 管商品（要你確認）", "Settings / Items (needs your confirmation)"),
    ("新增商品", "add item"),
    ("刪除商品", "delete item"),
    ("比對收貨", "match receipts"),
    # ── Agent 建議泡泡 ──
    ("${a.data.name}怎麼少這麼多", "why is ${a.data.name} down so much"),
    ("我發現「${a.data.name}」帳對不上，要不要點我幫你追原因？",
     "I found a mismatch on \"${a.data.name}\" - shall I trace the cause?"),
    ("${a.data.name}的安全庫存是多少", "whats the safety stock for ${a.data.name}"),
    ("「${a.data.name}」快斷貨了（撐 ${a.data.days_left} 天），想看設定嗎？",
     "\"${a.data.name}\" is running out (${a.data.days_left} days left) - view settings?"),
    ("有商品快到期了，要不要看完整到期清單？",
     "Some items are expiring soon - see the full list?"),
    ("掃到一些異常，要不要我出一份全倉體檢報告？",
     "I spotted some anomalies - shall I produce a full warehouse report?"),
    # ── 對話框 / 系統訊息 ──
    ("重置展示資料需要密碼（此動作將清除所有新增商品、進出貨紀錄、警示、排程，無法復原）：",
     "Password required to reset demo data (this clears all added items, movements, alerts and schedules - cannot be undone):"),
    ("再次確認：真的要重置展示資料嗎？", "Confirm again: really reset the demo data?"),
    ("展示資料已重置，頁面將重新載入", "Demo data reset - reloading the page"),
    ("確定要關閉倉管助理嗎？", "Close the Warehouse Assistant?"),
    ("頁面已關閉，請手動關閉此分頁（Ctrl+W 或 Alt+F4）",
     "Page closed - please close this tab manually (Ctrl+W or Alt+F4)"),
    ("重置失敗：", "Reset failed: "),
    ("刪除失敗：", "Delete failed: "),
    ("刪除失敗", "Delete failed"),
    ("未知錯誤", "Unknown error"),
    ("確定刪除警示規則 ${ruleId}？", "Delete alert rule ${ruleId}?"),
    ("確定刪除排程 ${jobId}？", "Delete schedule ${jobId}?"),
    ("❌ 麥克風權限被拒、請到瀏覽器設定開啟",
     "❌ Microphone permission denied - please enable it in browser settings"),
    ("⚠️ 沒聽到聲音、請再試一次", "⚠️ Didn't catch that - please try again"),
    # ── 警示 / 排程面板 ──
    ("目前沒有警示規則<br><br>試試：「當洗衣精低於安全庫存時通知我」",
     "No alert rules yet<br><br>Try: \"alert me when laundry detergent drops below safety stock\""),
    ("目前沒有定時排程<br><br>輸入「每天早上9點幫我跑盤點」來設定",
     "No schedules yet<br><br>Try: \"run a stock count every day at 9am\""),
    ("輸入「每天早上9點幫我跑盤點」設定排程",
     "Try \"run a stock count every day at 9am\" to add a schedule"),
    ("目前沒有警示規則", "No alert rules"),
    ("目前沒有排程", "No schedules"),
    ("想刪除可以說「刪掉 ", "To remove, say \"delete "),
    ("想取消可以說「刪掉 ", "To cancel, say \"delete "),
    ("尚未執行", "Not run yet"),
    ("⚡ 掃描中...", "⚡ Scanning..."),
    ("✓ 掃描完成", "✓ Scan complete"),
    ("⚡ 立即掃描", "⚡ Scan now"),
    ("建立：", "Created: "),
    ("刪除", "Delete"),
    # ── 卡片 / view 標籤 ──
    ("嚴重</span>", "critical</span>"),
    ("警告</span>", "warning</span>"),
    ("注意</span>", "info</span>"),
    ("▸ 任務規劃", "▸ Task plan"),
    ("掃描目錄", "Scan directory"),
    ("比對關鍵字", "Match keyword"),
    ("讀取檔案", "Read file"),
    ("分析結論", "Analyse"),
    ("執行動作", "Execute"),
    ("確認", "Confirm"),
    ("驗證", "Verify"),
    ("執行中", "Running"),
    ("失敗", "failed"),
    ("[分析] ", "[analysis] "),
    ("💡 Agent 推理建議行動 ", "💡 Agent suggested action "),
    ("🔔 <b>警示觸發</b>", "🔔 <b>Alert triggered</b>"),
    ("⏰ <b>排程自動執行</b>", "⏰ <b>Schedule ran</b>"),
    ("正在執行：", "Running: "),
    (" · ⚠️ 低於安全庫存", " · ⚠️ below safety stock"),
    ("所有商品庫存充足", "All items are well stocked"),
    ("樣本不足、暫無連帶資料", " - not enough samples for association data"),
    ("平手", "tie"),
    ("全類別", "all categories"),
    ("庫存清單（共 ", "stock list ("),
    ("建議補 ", "suggest ordering "),
    ("下批 ", "next batch "),
    ("天到期</span>", " days to expiry</span>"),
    ("建議今天就補", "restock today"),
    ("最近 ${data.within_days} 天內沒有快到期的批次",
     "no batches expiring within ${data.within_days} days"),
    ("⏰ 到期警示 — ", "⏰ Expiry alerts — "),
    ("🔴 7 天內 ", "🔴 within 7 days: "),
    ("🟠 14 天內 ", "🟠 within 14 days: "),
    ("🟡 30 天內 ", "🟡 within 30 days: "),
    ("批</span>", "</span>"),
    ("進貨</div>", "Inbound</div>"),
    ("出貨</div>", "Outbound</div>"),
    ("淨變動</div>", "Net change</div>"),
    ("怎麼算</span>", "how?</span>"),
    ("倉低</span>", " low</span>"),
    ("撐 ", "cover "),
    ("庫存 ", "stock "),
    # ── 類別 / 倉別（放最後，短字串）──
    ("電子產品", "Electronics"),
    ("家電廚具", "Kitchen Appliances"),
    ("食品飲料", "Food & Beverage"),
    ("日用品", "Daily Goods"),
    ("運動用品", "Sports & Outdoors"),
    ("服飾", "Apparel"),
    ("北區倉", "North"),
    ("中區倉", "Central"),
    ("南區倉", "South"),
    ("全部倉", "All warehouses"),
    ("全部商品", "all items"),
    ("藍牙耳機", "bluetooth earphones"),
    ("咖啡機", "coffee machine"),
    ("洗衣精", "laundry detergent"),
    ("羽絨外套", "down jacket"),
    ("尿布", "diapers"),
    ("帳篷", "tent"),
    ("瑜珈墊", "yoga mat"),
    ("慢跑鞋", "running shoes"),
    ("區倉", ""),
]


def main():
    bak = F.with_suffix(".html.zh.bak")
    if not bak.exists():
        shutil.copy(F, bak); print(f"[bak] {bak.name}")
    s = io.open(F, encoding="utf-8").read()
    n = 0
    for a, b in PAIRS:
        if a in s:
            s = s.replace(a, b); n += 1
    io.open(F, "w", encoding="utf-8").write(s)
    lines = s.split("\n")
    left = [l for l in lines if any("一" <= c <= "鿿" for c in l)]
    com = sum(1 for l in left if l.strip().startswith(("//", "/*", "*", "<!--")))
    print(f"[done] 套用 {n}/{len(PAIRS)} 條；剩餘含中文行 {len(left)}（其中註解約 {com}）")


if __name__ == "__main__":
    main()
