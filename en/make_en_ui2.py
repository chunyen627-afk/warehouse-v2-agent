# -*- coding: utf-8 -*-
"""
make_en_ui2.py — templates/index.html 第二輪英文化（補 make_en_ui.py 的漏網）。

背景：第一輪 make_en_ui.py 處理了快捷選單與主要標籤，但 **view card 的表頭、
HITL 卡片欄位、警示/排程/檔案面板、狀態文字** 還是中文（209 行含中文）。
訪客一操作就看得到。

⚠️ 兩個必守規則（第一輪踩過）：
  1. **長字串排前面**：短字串（中/北/南/倉/件/天）先替換會咬進已英文化的字，
     產生「處理Central」「分析Central」「連線Central斷」這種混血。
     本腳本用 (long → short) 排序，且短詞一律加上下文（如「'件'」只在
     量詞位置換）。
  2. **JS 字串裡的撇號**：英文常有 don't / didn't / what's，若目標字串要放進
     單引號 JS 字面量，一律改用雙引號包（第一輪的 'Didn't catch that' 讓整個
     <script> 語法錯誤、全站 JS 不執行 → 能力地圖不出現、卡 Loading）。
     本腳本產生的替換值若含撇號，會自動檢查所在行的引號型別。

可重複執行：先備份 index.html.zh.bak2（存在則不覆蓋）。
用法：cd warehouse_v2/en && <Python311> make_en_ui2.py
"""
import io
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
TPL = HERE / "templates" / "index.html"
BAK = HERE / "templates" / "index.html.zh.bak2"

# ── ① 先修「混血傷害」：第一輪把「中」誤換成 Central 造成的 ──────────────
FIX_MIXED = [
    ("⏳ 處理Central，請稍候…", "⏳ Processing, please wait…"),
    ("底部「處理Central」文字讓位給右下角效能徽章", "底部處理中文字讓位給右下角效能徽章"),
    ("「處理Central」動畫放在對話裡", "處理中動畫放在對話裡"),
    ("分析Central…", "Analyzing…"),
    ("⚠️ 連線Central斷，", "⚠️ Connection lost — "),
]

# ── ② 一般替換（**長字串在前**，避免短詞先咬）───────────────────────────
REPL = [
    # 標題 / 狀態
    ("<title>管助理 Demo</title>", "<title>Warehouse Assistant Demo</title>"),
    ("重置展示資料需要密碼（此動作將清除all added items, movements、警示、排程，無法復原）：",
     "Password required to reset demo data (this clears all added items, movements, "
     "alerts and schedules — cannot be undone):"),
    ("上次執行：", "Last run: "),
    ("⚠ 主動偵測到 ", "⚠ Detected "),
    (" 項需注意異常", " issues needing attention"),
    ("展開 ▼", "Expand ▼"),
    ("收合 ▲", "Collapse ▲"),
    # 卡片標題 / 說明
    ("🔔 警示規則（", "🔔 Alert rules ("),
    (" 條）", ")"),
    ("⏰ 排程（", "⏰ Schedules ("),
    (" 個）", ")"),
    ("找到 ", "Found "),
    (" 筆相關商品", " matching items"),
    ("缺貨警示 (", "Low stock alerts ("),
    (" 項) — 按「撐幾天」排序", ") — sorted by days left"),
    ("⚠️ 發現 ", "⚠️ Found "),
    (" 筆短收異常", " short-received issues"),
    ("⚙️ 安全庫存設定（", "⚙️ Safety stock settings ("),
    (" 項）", ")"),
    ("⚙️ 設定查詢", "⚙️ Settings"),
    ("📦 add item — 請Confirm", "📦 Add item — please confirm"),
    ("（自動產生）", " (auto-generated)"),
    ("買 <b>", "People who buy <b>"),
    ("</b> 的人,通常也會備這些貨:", "</b> usually also stock:"),
    (" 近期出貨量高,連帶品「", " has high recent outbound; related item \""),
    ("」庫存吃緊,", "\" is running low,"),
    ("別等缺了才補。", " — don't wait until it runs out."),
    ("📊 點同捆率看「這數字怎麼算出來的」· 隨機商品同單僅約 ",
     "📊 Tap the bundle rate to see how it is calculated · random items co-occur only about "),
    ("⏰ 紅(≤7天) / 橙(≤14天) / 黃(≤30天) — 主動下架 / 促銷 / 注意",
     "⏰ Red (≤7d) / Orange (≤14d) / Yellow (≤30d) — pull / promote / watch"),
    ("最近 ", "next "),
    (" 批 · 約 NT$ ", " batches · approx NT$ "),
    ("比較", " comparison"),
    # 表頭（逐一列，避免短詞誤傷）
    ("<th>條件</th>", "<th>Condition</th>"),
    ("<th>範圍</th>", "<th>Scope</th>"),
    ("<th>狀態</th>", "<th>Status</th>"),
    ("<th>內容</th>", "<th>Details</th>"),
    ("<th>頻率</th>", "<th>Frequency</th>"),
    ("<th>商品</th>", "<th>Item</th>"),
    ("<th>類別</th>", "<th>Category</th>"),
    ("<th>庫存</th>", "<th>Stock</th>"),
    ("<th>現量</th>", "<th>On hand</th>"),
    ("<th>撐天</th>", "<th>Days left</th>"),
    ("<th>建議補</th>", "<th>Suggest</th>"),
    ("<th>剩餘</th>", "<th>Remaining</th>"),
    ("<th>數量</th>", "<th>Qty</th>"),
    ("<th>採購單</th>", "<th>PO</th>"),
    ("<th>應收</th>", "<th>Ordered</th>"),
    ("<th>實收</th>", "<th>Received</th>"),
    ("<th>短收</th>", "<th>Short</th>"),
    ("<th>基準</th>", "<th>Base</th>"),
    ("<th>欄位</th>", "<th>Field</th>"),
    ("<th>區域</th>", "<th>Area</th>"),
    ("<th>說明</th>", "<th>Description</th>"),
    ("<th>檔數</th>", "<th>Files</th>"),
    ("<th>補貨量</th>", "<th>Reorder qty</th>"),
    ("<th>金額</th>", "<th>Amount</th>"),
    ("<th>供應商</th>", "<th>Supplier</th>"),
    ("<th>上月</th>", "<th>Last month</th>"),
    ("<th>本月</th>", "<th>This month</th>"),
    ("<th>變化</th>", "<th>Change</th>"),
    ("<th>庫</th>", "<th>WH</th>"),
    ("<th>分</th>", "<th>Per-WH</th>"),
    # 欄位名（HITL 卡片內）
    ("<td>名稱</td>", "<td>Name</td>"),
    ("<td>類別</td>", "<td>Category</td>"),
    ("<td>單價</td>", "<td>Unit price</td>"),
    ("<td>安全庫存</td>", "<td>Safety stock</td>"),
    ("<td>初始庫存</td>", "<td>Initial stock</td>"),
    ("<td>倉別</td>", "<td>Warehouse</td>"),
    ("<td>數量</td>", "<td>Quantity</td>"),
    ("<td>方向</td>", "<td>Direction</td>"),
    ("<td>商品</td>", "<td>Item</td>"),
    # 圖例 / 小字（短詞放最後）
    (">進貨<", ">Inbound<"),
    (">出貨<", ">Outbound<"),
    ("/ 安全 ", "/ safety "),
]


def main() -> None:
    if not TPL.exists():
        print(f"[error] {TPL} 不存在")
        return
    if not BAK.exists():
        shutil.copy2(TPL, BAK)
        print(f"備份 → {BAK.name}")
    s = io.open(TPL, encoding="utf-8").read()

    n = 0
    for a, b in FIX_MIXED + REPL:
        c = s.count(a)
        if c:
            s = s.replace(a, b)
            n += c

    io.open(TPL, "w", encoding="utf-8").write(s)

    # 驗證：剩餘中文 + JS 引號安全
    lines = s.split("\n")
    left = [
        (i, l.strip()) for i, l in enumerate(lines, 1)
        if re.search(r"[一-鿿]", l)
        and not l.strip().startswith(("//", "/*", "*"))
    ]
    m = re.search(r"<script[^>]*>", s)
    base = s[:m.start()].count("\n") + 1 if m else 0
    sc = re.findall(r"<script[^>]*>(.*?)</script>", s, re.S)
    risky = []
    if sc:
        for ln, line in enumerate(sc[0].split("\n"), 1):
            st = line.strip()
            if st.startswith(("//", "/*", "*")):
                continue
            if re.search(r"'[^']*[A-Za-z]'[A-Za-z]", line):
                risky.append(base + ln)

    print(f"替換 {n} 處；剩餘含中文的非註解行 {len(left)}；"
          f"JS 未跳脫引號風險行 {len(risky)}")
    if risky:
        print(f"  ⚠️ 風險行號：{risky}")
    io.open(HERE / "_ui2_left.txt", "w", encoding="utf-8").write(
        "\n".join(f"{i}: {t[:140]}" for i, t in left))


if __name__ == "__main__":
    main()
