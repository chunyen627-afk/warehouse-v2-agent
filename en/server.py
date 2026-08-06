"""
RPI5 Exhibition Demo — 倉管版 (v3.8)
═════════════════════════════════════════════════════
模型: functiongemma-270m-it-fine-tune (q8_0 GGUF) — 倉管專屬微調
推論: llama-cpp-python raw completion

特性:
  - 5 個倉管查詢 function
  - SKU 走 keyword + server fuzzy match（業界 retrieval 做法）
  - 透明面板：每次推論的 LLM 原文、parsed function、結果都廣播到 /display
  - dummy data：seed_data.json 由 generate_seed_data.py 一次性生成
  - 校正層 5 條規則（C1-C5）
  - chip bypass LLM 機制（type=direct_call、給「庫存警示」零容錯設計用）
  - 離線優先：模型本地、無 CDN

支援的 Function Call (5 個)：
  query_inventory, query_movement, list_low_stock,
  compare_warehouses, list_hot_items
"""

import asyncio
import io
import json
import logging
import os
import re
import socket
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import warehouse as finance  # 保留 finance 別名讓既有基礎設施段不用全改
import intent_clf

# ─── Logging ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("demo")

# r47：打字機動畫延遲（秒/字）。測試連線 ?fast=1 設 0——全量回歸曾為動畫純等 ~45 分鐘。
import contextvars as _ctxvars
_TK_DELAY = _ctxvars.ContextVar("tk_delay", default=0.008)

# ─── Config ───────────────────────────────────────────────
BASE_DIR           = Path(__file__).parent
MODELS_DIR         = BASE_DIR / "models"
TEMPLATES_DIR      = BASE_DIR / "templates"
STATIC_DIR         = BASE_DIR / "static"
SYSTEM_PROMPT_FILE = BASE_DIR / "system_prompt.txt"
WH_DATA_DIR        = BASE_DIR / "warehouse_data"

PORT          = int(os.getenv("PORT",         "8000"))

# ─── CPU thread 自動偵測 ──────────────────────────────────
def _detect_physical_cores() -> int:
    try:
        import psutil  # type: ignore
        n = psutil.cpu_count(logical=False)
        if n:
            return int(n)
    except Exception:
        pass
    logical = os.cpu_count() or 4
    return max(1, logical // 2) if logical >= 4 else logical

_PHYS_CORES = _detect_physical_cores()
N_THREADS       = int(os.getenv("N_THREADS",       str(_PHYS_CORES)))
N_THREADS_BATCH = int(os.getenv("N_THREADS_BATCH", str(_PHYS_CORES)))
N_BATCH         = int(os.getenv("N_BATCH",         "1024"))
N_CTX           = int(os.getenv("N_CTX",           "1280"))
MAX_TOKENS      = int(os.getenv("MAX_TOKENS",      "120"))
TEMPERATURE     = float(os.getenv("TEMPERATURE",   "0.0"))

EXTERNAL_URL  = os.getenv("EXTERNAL_URL", "")

GEMMA_STOP = ["<end_of_turn>", "<eos>", "<end_function_call>"]


# ════════════════════════════════════════════════════════════════════
# 守門員 — 倉管關鍵字白名單
# ════════════════════════════════════════════════════════════════════
GATEKEEPER_KEYWORDS = {
    # 類別
    "電子", "電子產品", "3c", "家電", "廚具", "家電廚具", "廚房",
    "食品", "飲料", "食品飲料", "日用", "日用品", "生活用品",
    "服飾", "衣服", "服裝",
    "運動", "運動用品", "運動類",
    "electronics", "appliance", "kitchen", "food", "beverage",
    "daily", "apparel", "clothing", "sports",
    # 商品俗稱（高頻常見）
    "耳機", "藍牙", "藍芽", "喇叭", "充電", "充電線", "行動電源", "電源", "手環",
    "悶燒罐", "熨斗", "電熨斗", "鍋", "不沾鍋", "牙刷", "果汁機",
    "氣泡水", "咖啡", "咖啡豆", "茶", "檸檬茶", "堅果", "餅", "蘇打餅",
    "啤酒", "可可", "乳清", "運動飲",
    "洗衣精", "洗劑", "衛生紙", "紙巾", "沐浴乳", "蚊香", "垃圾袋",
    "t 恤", "素t", "襪", "羊毛襪", "外套", "羽絨", "牛仔", "牛仔褲", "內衣",
    "瑜珈墊", "瑜珈", "水壺", "健身環", "慢跑鞋", "毛巾", "帽子", "毛帽",
    "拖鞋", "手套", "睡袋", "拖把", "背包", "太陽眼鏡", "野餐墊", "地墊", "毛毯",
    # 倉庫
    "北倉", "北區倉", "北區", "北部",
    "中倉", "中區倉", "中區", "中部",
    "南倉", "南區倉", "南區", "南部",
    "倉", "倉庫", "warehouse",
    # EN build：裸倉名（劇情批 r1）——中文「北倉呢」自帶倉名命中白名單，
    #   英文訪客的追問是裸 'north' / 'and central'，原本只有 'warehouse'
    #   在白名單 → 這類追問被當搗蛋拒絕。
    "north", "central", "south",
    # 動作
    "庫存", "存量", "還有", "剩", "幾件", "多少", "幾個", "查詢",
    "怎麼用", "教我", "功能", "使用說明", "怎麼教", "系統",
    # 資料管理
    "新增", "建立", "加入", "增加", "新建", "添加",
    "刪除", "下架", "砍掉", "移除", "刪掉",
    "取消", "退出", "停止",
    "進貨", "出貨", "入庫", "出庫", "進倉", "出倉", "進出",
    "庫存量", "庫存價值", "週轉", "週轉率",
    "前置", "天數", "前置天數", "延長", "縮短", "安全水位",
    "缺貨", "補貨", "警示", "警報", "告急", "快沒", "不足", "低庫存", "庫存警示",
    # 補貨口語核心字（RPI5 v21：「什麼得補了」「該補些什麼」的裸「補」沒命中
    # 守門員關鍵字被判無意義擋掉，沒機會走 C3 low_stock）
    "補", "要補", "該補", "得補", "缺", "缺的",
    # 缺貨/滯銷口語變體（RPI5 conv100-r2）
    "拉警報", "警報", "水位", "撐不住", "叫貨", "斷貨", "見底", "賣不動", "賣不出",
    # conv100-r5：警戒值訂在N / 沒人買 / 亮紅燈 / 動了幾次 / 啞鈴
    "警戒", "訂在", "沒人買", "亮紅燈", "開天窗", "快斷", "動了", "啞鈴",
    # r74：「退步最多的呢」曾被守門員擋（進步/退步→跨期比較）
    "進步", "退步",
    # r75：「價格改成299」要能進改價誠實閘，不能死在守門員
    "價格", "單價", "售價", "改價",
    # r76：「改成每週五」（改排程時間）與「算了 維持原樣」（放棄句）要能進
    # 對應 gate，不能死在守門員
    "每週", "每天", "每月", "改成", "維持", "原樣",
    # r78：改回原本/差最多/第N急 追問
    "改回", "恢復", "原本", "差最多", "第二急", "第三急", "最急",
    # r80：掉最多前三/就總值/大家辛苦
    "掉最多", "總值", "辛苦",
    # r84：匯出後問存放位置「存哪了」「檔案咧」
    "存哪", "檔案", "匯出",
    # r28：最沒人氣（曾被守門員拒）
    "沒人氣",
    # conv100-r6：缺貨/滯銷/連帶/RCA/明細 口語
    "斷炊", "吃緊", "急診", "快空", "墊底", "購物車", "黃金組合", "防蚊",
    "兜不上", "少掉", "流向", "吞吐", "業績", "存貨", "不能賣",
    # conv100-r7：賺錢/沒動靜/速配/見紅/撐不到/危險/賣況/縮水/落差/追查/怪異/上調/安全量
    "賺錢", "沒動靜", "速配", "見紅", "撐不到", "危險", "賣況",
    "縮水", "落差", "追查", "怪異", "上調", "下調", "安全量",
    # conv100-r12：遮陽帽/搭什麼（「買防曬遮陽帽的都搭什麼買」曾被守門員拒）
    "遮陽帽", "防曬", "搭什麼", "都搭",
    # ── EN build：英文**功能**查詢詞（不含商品名但完全合法的句子）。
    #   守門員的最後一關 _text_has_item_name 改成詞級比對後，這些句子
    #   （'best sellers this week' / 'compare north and south' /
    #   'item list'）因為沒有商品名而被擋掉 → 白名單必須先接住它們。
    "best seller", "best sellers", "top seller", "top sellers",
    "hot items", "hot item", "bestseller", "selling", "sells",
    "slow moving", "slow-moving", "dead stock", "not selling",
    "compare", "comparison", "versus", "item list", "product list",
    "full list", "all items", "everything we", "what do we have",
    "running low", "low stock", "restock", "reorder", "out of stock",
    # r14+1：#26 'which SKUs need replenishment'/#31 'stockout risk' 曾被
    #   守門擋成搗蛋；days of cover 是庫存卡本來就有的欄位、訪客會直接問
    "replenish", "stockout", "skus", "zero stock", "days of cover",
    "stock cover",
    # r15 #11：'anything sitting at zero units' 曾被守門擋
    "zero units", "at zero",
    # r16：#23 'cheapest thing we have' 曾被守門擋（直達層修好了守門沒放行）
    #   ＋#38 bulk＋#15 collecting dust（SLOW 接了守門沒放）
    "cheapest", "priciest", "expensive", "bulk of", "collecting dust",
    "expiring", "expire", "expiry", "shelf life", "movements",
    "moved", "inbound", "outbound", "transfers", "help",
    "what can you", "how does this", "safety stock", "purchase order",
    "report", "stocktake", "audit", "reconcile", "discrepanc",
    # r3：抱怨式 RCA 講法（'the numbers dont look right' / 'this is wrong'）
    "numbers", "look right", "not right", "doesnt look", "doesn't look",
    "add up", "off by", "went missing", "make sense",
    # Agent Tools 的功能句（RCA/檔案/排程/警示/報表/期間比較）——
    #   這些不含商品名，守門員收嚴後會被擋，但都是合法功能查詢
    "purchase record", "transaction log", "transaction record",
    "movement log", "movement record", "audit trail", "change log",
    "what files", "which files", "read files", "data files",
    "schedule", "schedules", "scheduled", "recurring", "every day",
    "every week", "every month", "alert rule", "alert rules",
    "my alerts", "set alert", "notify me", "remind me",
    "last two months", "past two months", "month over month",
    "period compare", "trend", "growth", "decline",
    # （管理動詞改用詞界比對，見下方 _GK_ACTION_RE——放在 set 裡會 substring
    #   誤爆：'set ' ∈ sun**set** time、'order ' ∈ b**order** control）
    # ── 守衛第 8 輪：上一輪把守門員收嚴後，這些「沒有商品名但完全合法」
    #    的功能句被擋成 rejected（low/hot/rca/mv 共 11 句）。守門員的
    #    最後一關要求句中有商品名，功能句得靠白名單接住。
    "almost out", "running out", "about to run out", "nearly out",
    "shortage", "shortages", "short list", "restock list", "reorder list",
    "below safety", "under safety", "need to order", "should i order",
    "what to order", "order anything", "getting low", "runs out",
    "which products", "which items", "what items", "what products",
    "sales ranking", "ranking", "rank", "popular", "least popular",
    "top selling", "worst selling", "moving fast", "moving slow",
    "expiry", "expiry alert", "expiry alerts", "expiring stock",
    "going bad", "past date", "use by", "best before",
    "reconciliation", "reconciliation issues", "anomalies", "anomaly",
    "doesnt add up", "does not add up", "add up",
    "in and out", "ins and outs", "recommend", "recommendation",
    "goes well with", "pairs with", "bundle with",
    "warehouse doing", "how are we doing", "stock value", "total value",
    "inventory value", "stock overview", "inventory overview",
    # 新增/刪除商品的英文觸發詞——守門員排在流程攔截**之前**，
    #   白名單沒收的話 'add item' 會先被擋成 rejected，根本進不了分步流程
    "add item", "add a item", "add an item", "add new item", "add a new item",
    "create item", "create a item", "create an item", "create a new item",
    "new item", "new product", "add product", "add a product",
    "register item", "delete item", "remove item", "delete product",
    "remove product", "take down item", "discontinue",
    # ⚠️ 不放 status/summary/dashboard/overview 這種泛詞——'is this offline'
    #    'how big is the warehouse' 這類搗蛋句會跟著放行（guidey 類回歸）
    "powerbank", "lunch box", "lunchbox",
    "賣最好", "賣最差", "熱銷", "暢銷", "滯銷", "排行", "排名", "top",
    "冠軍", "最熱門", "最冷門", "銷量", "搶手", "熱賣", "賣得最兇", "最夯",
    "比較", "比", "跟", "和", "vs", "對比",
    "查", "看", "顯示", "列", "看一下", "查一下",
    # 連帶分析 (v3.8 連鎖網)
    "連帶", "也買", "也會買", "還會買", "一起買", "一起賣", "順便買",
    "搭配", "帶動", "好夥伴", "帶貨", "連帶備貨", "連鎖",
    "還扛了", "還配了", "還帶了", "順手帶了", "扛了",
    "帳篷", "露營", "咖啡機", "尿布",
    "bought together", "also buy", "related",
    # 時間
    "今天", "今日", "本日", "本週", "這週", "這禮拜", "本月", "這個月", "這月",
    "月度", "週度", "本日", "目前", "現在", "當下",
    "最近", "這幾天", "這陣子", "近", "month", "week", "today",
    # 動作補強 (v3.8 round 2)
    "記錄", "紀錄", "明細", "清單", "排行", "所有", "全部", "查", "看", "顯示",
    "最差", "最好", "賣", "進", "出", "貨", "交易", "報表", "自動", "沒了", "快要沒",
    "撐多久", "撐幾天", "撐得", "撐到", "夠撐", "日銷",   # r71/r73
    # 保存期限(v3.9 連動)
    "到期", "過期", "快到期", "即將到期", "保存期限", "效期", "保鮮",
    "賞味", "新鮮度", "快爛", "即期", "expire", "expiring", "shelf life",
    # RCA / 採購對帳
    "對帳", "採購對帳", "短收", "差異", "帳對不上", "帳不對", "盤點",
    "異常", "入庫異常", "進貨異常", "採購異常", "庫存異常",
    "少貨", "少了", "怎麼少", "為什麼少", "誰改", "誰動",
    "短少", "PO", "訂單", "採購單", "採購",
    "帳目", "怪怪的", "怪怪", "帳目怪",
    "哪些", "有哪些", "列出",
    "short", "discrepancy", "mismatch",
    # English actions
    "stock", "inventory", "low", "alert", "restock", "compare", "hot", "slow",
    # r3：'yogamat count' 的 count 是查詢詞（黏字商品名靠模糊層還原）
    "count", "counts", "qty", "quantity", "amount",
    "top", "selling", "movement", "inbound", "outbound",
    "bluetooth", "earphone", "coffee", "machine", "bought", "together", "related",
    "what", "how", "show", "today", "week", "month",
    # ⚠️ 'much'/'many' 拿掉裸詞（守衛 chat 類回歸）：'thank you very **much**'
    #   命中白名單 → 寒暄句被當倉管查詢回全店概覽。改用 _GK_QTY_RE 要求
    #   「how much/many」連用才算查詢意圖。
    #   （中文版第 18 輪也移除過純虛詞，理由相同：守門形同虛設——見上方註解）
    # 錯字容錯關鍵字（避免 OOV 被守門員擋掉）
    "芽", "汽", "灌", "精", "只", "基", "郭", "伽", "店", "員", "文", "窄", "胡", "湖",
    "容", "鬥", "挖", "帶", "素", "協", "一", "運", "允", "燙",
    # 口語關鍵字（避免「刷牙的那個」被 reject）——只留「指向具體商品用途」的
    # 實詞。第18輪移除純虛詞/單字助詞（我/這/哪/嗎/呢/啊/吧/喔/怎/問題/東西/
    # 機器/壞掉…）：它們讓幾乎任何中文閒聊句都矇混過門（「你好嗎朋友」命中
    # 「嗎」、「你有感情嗎」命中「嗎」），守門形同虛設。錯字容錯字元另有
    # 上面那組（芽/汽/灌…）針對特定商品錯字，不受影響。
    "刷牙", "洗衣服", "擦身體", "裝水", "煮咖啡", "運動用的", "充電的",
    "墊子", "衣服", "手機殼",
    "洗澡", "牙刷", "牙膏", "毛巾", "肥皂", "洗髮", "戶外", "快壞",
    # 回歸驗證後補回的實詞（第18輪瘦身誤傷）：這幾個有真實倉管用途——
    # 「那個」是 carry-over 代詞、「有個問題」是模糊求助、「快要壞掉」是
    # 到期口語、「我想知道」是查詢開頭。閒聊濫用風險由黑名單先擋
    # （「你是不是壞掉」有 你是不是 黑名單）。
    "那個", "問題", "壞掉", "想知道", "想查", "想問",
    # 品項/報表口語（新增商品/報告類）
    "品項", "新品", "月報", "週報", "日報", "統計", "報告", "整理", "產出", "產生",
    "體檢", "健檢", "報表",
    # r19：警示設定/價格排序口語（「低於30就提醒我」「最貴的商品」曾被拒）
    "提醒", "通知", "低於", "最貴", "最便宜", "單價",
    # r20：滯銷/動態口語（乏人問津/動靜/嚇嚇叫 曾被守門員拒）
    "問津", "動靜", "嚇嚇叫", "歸零",
    # r21：開單/後設取消（曾被守門員拒）
    "開單", "開一張", "不算",
    # r26：「明天有什麼排程」曾被守門員拒
    "排程",
    # r27：「中午前的異動」「asdfgh鍵盤」曾被守門員拒
    "異動", "鍵盤", "總覽",
    # r28：「qwerty滑鼠」曾被守門員拒
    "滑鼠",
    # 簡體常見倉管詞（陸港訪客，第18輪）
    "库存", "耳机", "进货", "出货", "调货", "缺货", "补货", "报表", "报告",
    "仓库", "查询", "热销", "滞销",
}


# 守門員的描述放行需同時帶查詢語氣（與 WS 直達的 _DESC_Q_CUES 一致），
# 避免「放音樂給我聽」這種描述命中但無查詢意圖的閒聊句被誤放。
_DESC_GATE_CUES = ("還有", "還剩", "剩", "庫存", "多少", "幾",
                   "有沒有", "有嗎", "夠", "存量", "現貨",
                   # 與直達 _DESC_Q_CUES 同步（「有賣煮咖啡的嗎」，2026-07-09）
                   "有賣", "賣不賣", "有沒有賣", "賣嗎", "有沒有這", "有這個")


def is_meaningful_input(text: str) -> bool:
    """守門員：判斷輸入是否值得送 LLM。"""
    s = text.strip().lower()
    if len(s) < 2:
        return False
    if re.fullmatch(r"\d+", s):
        return False
    # r3 S9：純標點（'?' / '....' / '!!!!'）不是查詢——原本落到全店概覽，
    #   該走 guide/rejected 給訪客方向
    if re.fullmatch(r"[^\w一-鿿]+", s):
        return False
    # EN build（純英文模型）：含中文字一律當搗蛋擋掉（純中文、中英夾雜都不受理）。
    #   user 定調 2026-07-26：英文版模型不留中文，遇中文/中英混雜 → reject。
    #   商品名/資料已全英文，正常英文查詢不含中文；此規則零誤傷正常英文輸入。
    if any("一" <= c <= "鿿" for c in s):
        return False
    # 功能描述句（「裝便當的還有嗎」「放音樂的還剩幾台」）不含傳統倉管詞、
    # 甚至含黑名單詞（便當/音樂 是防閒聊用的），但描述 regex 命中 + 帶查詢
    # 語氣 = 明確查商品意圖，放行讓後續功能描述直達接手。必須在黑名單之前
    # 判——否則「便當」「音樂」會先被黑名單擋掉。加查詢語氣條件避免誤放
    # 閒聊（「放音樂給我聽」描述命中但無查詢語氣 → 不放行、續走黑名單擋下）。
    if _descriptor_hit(text) and any(c in s for c in _DESC_GATE_CUES):
        return True
    # r12（探針批）：**引導類問句**在守門員就該放行——它們是合理提問不是搗蛋。
    #   實測 `how does this work` 被擋成 rejected（12ms，連 guide 判定都沒到），
    #   訪客第一句問「這怎麼用」卻收到婉拒。`what can you do` 能過是碰巧
    #   命中別的詞，不是有意為之 → 這裡明確化。
    #   ⚠️ 全部用片語（坑 1）：裸 work/this/demo 會撞合法查詢。
    if any(w in s for w in (
            "how does this work", "how do i use this", "how does it work",
            "what can you do", "what can i ask", "what do you do",
            "what is this demo", "what's this demo", "whats this demo",
            "what is this system", "what's this system",
            "is this real data", "how does this demo",
            # r18 #28：超禮貌開頭（would it be possible to see the movement
            #   log 曾被守門拒）——後面接什麼都是合理提問
            "would it be possible", "is it possible to")):
        return True
    # ── 產出類意圖放行（2026-08-04，意圖測試抓到）───────────────────
    #   訪客要的正是報告/採購單/進出紀錄,卻在守門員就被擋掉：
    #     `generate a PO`（PO 太短）/ `export everything from last quarter`
    #     （everything 不是受詞）→ rejected
    #     `warehouse health check` / `which items grew the most`
    #     → 被當商品名查 →「查無 health/grew 這個商品」
    #   ⚠️ 一律用**詞界正則**不用 substring（坑 1：`po` ∈ re**po**rt/ex**po**rt）。
    if re.search(r"\b(?:generate|create|make|run|produce|give\s+me|i\s+need|"
                 r"i\s+want|show\s+me|export|download)\b[^.?!]{0,30}"
                 r"\b(?:po|purchase\s+order|report|stocktake|stock\s+take|"
                 r"audit|health\s+check|movements?|transactions?|records?|"
                 r"logs?|csv|everything)\b", s, re.I):
        return True
    #   排行/趨勢問法（grew/dropped/best/worst + items/sellers）
    if re.search(r"\b(?:which|what)\b[^.?!]{0,20}\b(?:items?|products?|skus?|"
                 r"sellers?)\b[^.?!]{0,20}"
                 r"\b(?:grew|grow|dropped|drop|rose|fell|best|worst|most|least)\b",
                 s, re.I):
        return True
    #   r14+1：數量條件查詢（#12 'is anything under 20 units' 曾 rejected）
    #   ——比較詞後必須跟數字，閒聊句（'anything under the sun'）不會中
    if re.search(r"\b(?:anything|any\s+items?|what|which|items?|skus?|stock)\b"
                 r"[^.?!]{0,24}\b(?:under|below|over|above|less\s+than|"
                 r"fewer\s+than|more\s+than)\b\s*\d+", s, re.I):
        return True
    #   健檢/盤點類名詞片語（health check / stock audit / stocktake report）
    if re.search(r"\b(?:warehouse|inventory|stock|full)\s+"
                 r"(?:health\s+check|report|audit|summary|stocktake)\b|"
                 r"\b(?:health\s+check|stocktake|stock\s+audit)\b", s, re.I):
        return True

    # 身份問句（2026-08-04 第七輪：'who made you' 曾掉「查無商品 who made」,
    #   ZH 版黑名單有對應詞,EN 漏）
    if re.fullmatch(r"(?:so\s+)?who\s+(?:made|built|created|designed)\s+"
                    r"(?:you|this|it)[?.! ]*|who\s+are\s+you[?.! ]*|"
                    r"are\s+you\s+(?:an?\s+)?(?:ai|robot|bot|chatgpt|gpt|human)"
                    r"[?.! ]*", s):
        return False
    # r81 寫入契約：破壞動詞 × 全稱詞的組合一律擋（不枚舉個案）。
    # 「全部商品歸零」曾漏黑名單→吐 60 項清單。破壞語意 + 全稱範圍 = 搗蛋。
    # 查詢語（「快歸零的有哪些」）不含全稱詞、或含查詢語尾，不會誤中。
    _DESTROY_VERB = ("歸零", "清空", "清掉", "清光", "刪光", "刪掉", "刪除",
                     "清除", "全砍", "砍掉", "格式化", "全部改成", "都改成")
    _SCOPE_ALL = ("全部", "所有", "全店", "全倉", "整批", "整個倉", "商品", "庫存")
    if (any(v in s for v in _DESTROY_VERB) and any(a in s for a in _SCOPE_ALL)
            and not any(q in s for q in ("哪些", "有哪", "快", "剩", "查", "看",
                                          "幾", "多少", "清單", "警示"))):
        return False
    # r62：「退貨3個耳機 北倉」是倉管退貨入庫（is_return 一直支援），不是購物
    # 退貨搗蛋——退貨+數量+（倉別或真商品）豁免黑名單
    #   r101：`\d` 只認阿拉伯數字 → 「退貨二十個滑鼠」（中文數字）沒被豁免、
    #   掉進黑名單被 rejected（真人語音實測 #31：ASR 聽對了卻被拒）。
    #   數量改認阿拉伯＋中文數字。
    if re.search(r"退[貨回]?\s*(?:\d|[零一二兩三四五六七八九十百千])", s) and \
            (re.search(r"[北中南][倉區]", s) or _text_has_item_name(s)):
        return True
    # r77：期間退貨統計查詢（「上週退貨總共退了幾件」）也是倉管句
    if re.search(r"(上週|本週|今天|昨天|上個?月|本月).{0,4}退貨"
                 r"|退貨.{0,8}(幾件|多少件|統計|記錄|記在哪)", s):
        return True
    # ⚠️ EN build：破壞短語 × **合法管理受詞**豁免（邊界測試抓到）。
    #   黑名單有 'delete the'（防 'delete the database'），但
    #   'delete the schedule' / 'cancel the alert rule' 是**合法的管理操作**
    #   （中文版對應的「取消排程/刪除警示」走 Pre-C-Sched 列清單讓訪客選）。
    #   → 受詞是排程/警示/規則時放行，交給 Pre-C-Sched 處理（它只會**列清單**
    #   讓訪客指名，不做批量刪除，所以放行是安全的）。
    #   不含 database/table/everything/all 這類全域破壞受詞。
    if (re.search(r"\b(?:delete|remove|cancel|clear|drop|turn off|disable|stop)\b"
                  r".{0,12}\b(?:schedules?|alerts?|alert rules?|reminders?|"
                  r"rules?|jobs?)\b", s)
            and not re.search(r"\b(?:database|table|everything|all data|"
                              r"all items|all stock|system)\b", s)):
        return True
    # 黑名單：明顯非倉管領域 → 直接擋
    for kw in _GATEKEEPER_BLACKLIST:
        if kw in s:
            return False
    for kw in GATEKEEPER_KEYWORDS:
        if kw in s:
            return True
    # 管理動詞 / 數量問句（詞界比對，見 _GK_ACTION_RE 註解）
    if _GK_ACTION_RE.search(s) or _GK_QTY_RE.search(s):
        return True
    # r3：黏字查詢句——要有黏字虛詞**且**句子不只那一個詞（避免裸 'stok' 過關）
    if _GK_GLUED_RE.search(s) and len(s.split()) >= 2:
        return True
    # r25：句含真商品名（3 字滑窗）→ 放行（「機械式鍵盤的帳兜得攏嗎」的商品
    # 不在手列俗稱名單曾被拒——與 guide 判定同款結構性判準）
    if _text_has_item_name(s):
        return True
    return False


# ── 劇情批 r1：寫入/設定/管理句「沒有商品名但語意完全明確」，原本被守門員
#    擋成搗蛋（'change central to 80' / 'move 20 from the fullest to the
#    emptiest'）。⚠️ 坑 1：這些詞**必須用詞界**，放進 GATEKEEPER_KEYWORDS
#    做 substring 會誤爆（'set ' ∈ sun**set** time、'order ' ∈ b**order**
#    control——實測過，兩句都被誤放行）。
_GK_ACTION_RE = re.compile(
    r"\b(?:change|set|adjust|update|increase|decrease|raise|lower|"
    r"move|transfer|shift|ship|received|receive|restock|reorder|"
    r"fullest|emptiest)\b", re.I)
# 'how much/many' 連用才是查詢意圖（裸 much/many 會讓 'thank you very much'
#   這種寒暄句矇混過守門員 → 回全店概覽）
_GK_QTY_RE = re.compile(r"\bhow\s+(?:much|many)\b", re.I)
# r3：語音/快打黏字的查詢句（'howmany …' / 'wat about themouse' /
#   'powerbank stok'）——守門員原本擋掉，但它們是正常查詢只是打字黏了/錯了。
#   ⚠️ 要求**同時**有黏字虛詞與其他內容（單獨一個 'stok' 不放行）。
_GK_GLUED_RE = re.compile(
    r"\b(?:howmany|howmuch|whatabout|isthere|arethere|doyou|dowe|"
    r"themouse|theearphones|thestock|instock|stok|stcok|invetory|inventry)\b",
    re.I)


def _GATEKEEPER_BLACKLIST_HIT(text: str) -> bool:
    """句中是否命中黑名單（閒聊/破壞/注入）。
    供「context 追問放行」用——放行短追問時，黑名單仍要照擋，
    否則 'can you speak chinese' / 'will you crash' 這種閒聊會跟著溜進來。"""
    _s = text.strip().lower()
    return any(_k in _s for _k in _GATEKEEPER_BLACKLIST)


GATEKEEPER_REJECT_MSG = (
    "This demo is a warehouse assistant — I can check stock, movements and low-stock alerts.\n"
    "Try asking:\n"
    "\"bluetooth earphones stock\"  \"what's running low\"  \"best sellers this month\"  "
    "\"compare north and south\"\n"
    "Or type \"help\" to see everything I can do!"
)

# ── r16/r17：**形容詞式修飾語**（不是商品名，也不是「庫裡沒有的商品」）──
#   `is the earphone stock healthy` / `whats the situation` 這類，訪客問的是
#   狀態評估不是商品。它們同時被兩層誤判：
#     ① C1g-oov：把**已抽對**的 keyword 清掉 → 回全店 60 項概覽
#     ② oov:noex：宣告「查無 healthy 這個商品」
#   ⇒ 兩處共用這張表（改一邊要改兩邊——提成模組常數就是為了避免走味）。
#   ⚠️ **不逐詞列舉**是刻意的：`_NOEX_STOP` 已累積 60+ 個、每輪都在加，
#     那是結構性問題的徵兆。這裡靠「已知抽象詞 + 形容詞字尾規則」涵蓋，
#     具體名詞（office/microwave/bicycle）不在其中，照常誠實查無。
def _period_from_en(text: str) -> str | None:
    """r20：從英文句抽期間。抽不到回 None（讓呼叫端決定預設值）。

    ⚠️ 為什麼需要這支：`query_movement` 的 period 只認 6 個值，
      **不在清單裡就靜默 fallback 成 today**（warehouse.py:869）。
      加上 clf 快路徑寫死 `this_month`、rescue 路徑的判定**全是中文**
      （昨天/上週/本月）→ 英文訪客問什麼期間都被吃掉：
        `what came in yesterday`      → 回 **Today** 816 units
        `movements in the last 7 days` → 回 **Today**
        `what came in on may 20`       → 回 **Today**
      數字看起來很正常，但答的是錯的期間——**誤導級**，訪客不會發現。
    ⚠️ 順序有意義：先比長片語（last week 要贏過 week）。
    """
    t = text.lower()
    if _re.search(r"\bday before yesterday\b", t):
        return "day_before_yesterday"
    # r15 #23/#34：'this morning' 資料只有日粒度——today 是最誠實近似
    #   （原本掉到 this_week 回 1,399 件＝誤導級）
    if _re.search(r"\bthis morning\b|\bthis afternoon\b", t):
        return "today"
    if _re.search(r"\byesterday(?:'s)?\b", t):
        return "yesterday"
    if _re.search(r"\blast\s+week\b|\bprevious\s+week\b|\bpast\s+week\b", t):
        return "last_week"
    if _re.search(r"\blast\s+month\b|\bprevious\s+month\b|\bpast\s+month\b", t):
        # tool 沒有 last_month（只到 this_month）——回 None 讓上游別硬塞，
        #   免得靜默 fallback 成 today 給出錯的數字
        return None
    if _re.search(r"\bthis\s+week\b|\bcurrent\s+week\b", t):
        return "this_week"
    if _re.search(r"\bthis\s+month\b|\bcurrent\s+month\b|\bmonth to date\b", t):
        return "this_month"
    if _re.search(r"\btoday(?:'s)?\b|\bso far today\b", t):
        return "today"
    return None


_ADJ_LIKE_OOV = {
    "healthy", "accurate", "reliable", "normal", "situation", "status",
    "condition", "level", "expensive", "cheap", "balanced", "overstocked",
    "understocked", "stocked", "space", "capacity", "recommend",
    "recommended", "suggestion",
    # r14+2（#32/#69）：庫存狀態名詞——'stock position of camping tent' 的
    #   position 曾被當陌生修飾詞**清掉已抽對的 Camping Tent 4-person**
    #   （坑 3 同型），cat-fill 再拿 camping 填成 Sports 類概覽
    "position", "positions", "levels", "standing",
    # r14+2（#69）：禮貌片語的名詞——'could I trouble you' 的 trouble 曾被
    #   當陌生修飾詞清掉已抽對的 Camping Tent（r12 禮貌詞批漏了這幾個）
    "trouble", "bother", "pardon",
    # r23：**疑問詞**——`where is the mouse stock` 的 where 被當商品名 →
    #   「查無 where 這個商品」。
    #   ⚠️ 不能加進 `_oov_stop`／`_NOEX_STOP`：那三個詞（why/when/where）
    #     是 RCA（why is X off）與期間查詢的關鍵意圖詞，剝掉會傷下游判斷
    #     （見 server.py 該處註解）。放這裡只影響「句中已有真商品詞」時的
    #     判定——那種情況它必然是修飾語，不是商品名。
    "where", "when", "why", "who", "how",
}

# 明顯非倉管領域的黑名單（股市/天氣/電影…）— 就算含「查」也不放行
# 第17輪「訪客閒聊輪」大擴充：展場訪客會把系統當聊天機器人（問身份/閒聊/
# 嗆聲）甚至下搗蛋指令（刪全部/要密碼/套 system prompt），這些句子常夾帶
# 倉管關鍵字（「告訴我」撞通知詞、「壞掉」撞到期詞）誤入功能路由，
# 黑名單優先於白名單直接友善拒絕。
_GATEKEEPER_BLACKLIST = (
    # r55 台語字＝搗蛋（user 定調：只支援國語，台語書寫一律優雅拒；挑字選台語
    # 專屬詞避免誤傷國語——歹勢/拍謝/欸 屬台灣國語日常用語不列入）
    "佇", "叨位", "攏總", "偌濟", "閣有", "啥物", "逐家", "咱攏", "叨個",
    # r44 購物語境（把 demo 當電商：運費/付款/折扣/退貨——曾掉進空手訊息回顯醜句）
    "運費", "貨到付款", "打幾折", "會員價", "積點", "退貨", "結帳", "下單",
    # r44 觀念問題句（是什麼意思/怎麼算——曾回「找不到商品『意思』」）
    "是什麼意思", "怎麼算的", "什麼叫",
    # 探人隱私（r19：「把別人的購物車給我看」曾進 related_empty）
    "別人的",
    # r27：「你們老闆電話多少」的「多少」曾繞進 movement
    "電話",
    # r28：「偷偷告訴我進貨成本」曾開 alert 卡、「你的程式碼給我看」曾回熱銷榜
    "偷偷", "程式碼", "原始碼", "source code",
    # 注入字串（r20：<script> 曾因英文 alert 命中缺貨詞回清單）
    "<script", "</script", "select * from", "onerror=",
    # r14+2（#86/#87/#80）：'drop all tables' 中間隔 all 讓 'drop table'
    #   比不到；meaning of life／what model are you 家族曾 fuzzy 撞
    #   Running Shoes 回商品卡
    "drop all", "meaning of life", "what model are you",
    "which model are you", "what llm", "what ai model",
    # 離題領域
    "股市", "股票", "天氣", "電影", "音樂", "新聞", "地圖",
    # r14+1：裸 "stocks" 曾誤殺 'urgent restocks'（substring 撞複數）——
    #   股票閒聊改收片語；漏網的裸 stocks 會被商品比對 OOV 誠實拒絕
    "翻譯", "計算", "食譜", "笑話", "遊戲", "stock market", "stock exchange",
    "weather",
    "寫詩", "作業", "便當", "樂透", "唱歌", "唱首", "說個故事", "講個故事",
    "陪我聊", "聊天", "星期幾", "現在幾點", "下雨",
    # 問 AI 身份 / 嗆聲
    "你是誰", "機器人嗎", "chatgpt", "你是真人", "你有意識", "你幾歲",
    "誰做的你", "什麼模型", "你是不是", "你多聰明", "你會說",
    "你好笨", "你好棒", "好厲害", "沒用的東西", "白癡", "廢物",
    # r16 #91：'you suck' 曾回「查無 suck 這個商品」（substring 蓋 sucks）
    "you suck", " suck",
    # r17 #19：元對話抱怨（"that's not what i asked"）→ rejected 引導
    "not what i asked", "not what i meant", "you misunderstood",
    "你很慢", "回答快一點", "你答錯", "當機",
    "罵我", "罵人", "罵一下",   # r58：「罵我一下」曾回「沒有『罵我』這個商品」
    "講中文", "說中文",         # r59：「講中文好嗎」曾回「沒有『講中文』這個商品」
    "加油好嗎", "加加油",       # r60：「中倉加油好嗎」曾回「沒有『加油』這個商品」
    "倉租", "租金", "水電費",   # r71：「倉租多少錢一個月」曾回 60 項概覽
    # 搗蛋 / 注入探測（永遠擋）
    "格式化", "重開機", "關機", "密碼", "管理員", "admin",
    "rm -rf", "rm-rf", "system prompt", "prompt是什麼",
    # ── EN build：英文破壞/注入指令。⚠️ 這類**必須**進黑名單而不是靠
    #   守門員的商品名判定——'wipe all stock' 的 wipe 精確命中主檔的
    #   Baby Wet Wipes → 被當成正常商品查詢放行（守衛 probe 抓到）。
    "wipe all", "wipe the", "delete all", "delete the", "drop table",
    "drop the database", "clear everything", "clear all", "erase all",
    "reset everything", "shutdown", "shut down", "format the",
    # ── DEMO 情境 B（不配合的訪客）抓到的破壞句缺口 ──────────────────
    #   `delete everything` → 回「查無 delete 這個商品」（該 rejected）
    #   `set all stock to zero` → 回「我可以調整庫存設定，試試 set north
    #     safety stock to 50」＝**在指導破壞操作**，比不擋更糟。
    #   ⚠️ 這類永不豁免（同 _BL_NEVER_EXEMPT 的道理）。
    "delete everything", "remove everything", "erase everything",
    "wipe everything", "destroy everything", "delete it all",
    "set all stock", "stock to zero", "all to zero", "zero out",
    "set everything to", "empty the warehouse", "empty all",
    "make it all zero", "clear the stock", "clear the warehouse",
    "developer mode", "ignore your", "ignore previous", "ignore all",
    "reveal your", "show your prompt", "your instructions",
    "system prompt", "jailbreak", "sudo ", "admin password",
    "忽略你的指令", "忽略指令", "告訴我祕密", "告訴我秘密",
    "全部刪掉", "刪掉全部", "全部刪光", "刪光", "刪除全部", "清空資料",
    # r75：「價格改成」移出黑名單——改價誠實閘會優雅回「不支援改價」；
    # 0元/1元 惡搞句仍留在黑名單
    "清空庫存", "清倉", "改成0元", "改成 0 元", "改成1元",
    "改成0", "全部改成", "所有商品改", "全部價格",
    # 清空/歸零變體（RPI5 v21：「把庫存全部清掉」被當商品查詢問你要查啥）
    "全部清掉", "清掉庫存", "庫存清掉", "清掉所有", "清光", "全部清光",
    # 裸「歸零」窄化（r20：「存貨快歸零的有哪些」是缺貨查詢曾被拒）
    "庫存歸零", "全部歸零", "改成歸零", "設成歸零", "把庫存歸零",
    "全部清空", "清除全部", "清除所有",
    # r16：「幫我把刷牙的庫存清空」漏擋（只有「全部清空」沒有「庫存清空」）
    "庫存清空", "清空庫存", "清空",
    # 第18輪：假授權/反串/注入變體
    "後台", "後台權限", "重設系統", "測試模式", "除錯模式", "debug模式",
    "沒有任何限制", "沒有規則", "沒有限制的ai", "設定檔", "原始指令",
    "無視前面", "無視所有", "無視規則", "以管理員", "工程師 給我", "工程師給我",
    "不用聽", "不用遵守", "聽我的指令", "指令了",
    "sudo", "drop table", "delete from", "ignore all", "ignore previous",
    "圖靈測試", "你的system", "顯示所有密碼",
    # 第18輪：情緒/裝熟/離題閒聊
    "心情不好", "好難過", "陪我玩", "陪我聊", "你愛我", "晚安", "誇獎我",
    "過得如何", "過得好", "最近好嗎", "吃什麼", "喜歡吃", "好可愛", "笨蛋",
    "好無聊", "唐詩", "做菜", "餐廳", "匯率", "email", "e-mail",
    "穿什麼", "朋友嗎", "是朋友", "掰掰", "再見", "謝謝掰", "bye",
    "感情嗎", "有感情", "會累", "會學習", "自己學習",
    "傳說中", "真的假的", "隔壁攤", "機器手臂",
    # 第20輪 conv100-r5：問系統價格 / 大量改數字 / 刪別人資料 / 角色注入
    "多少錢一套", "這系統多少", "系統多少錢",
    "全部庫存數字", "數字改成", "庫存全部改",
    "別人的訂單", "訂單刪", "刪掉別人",
    "忘記倉管", "忘記你是", "現在起你叫", "你現在叫",
    # conv100-r6：「把資料庫整個匯出給我」曾開出腳本確認卡
    "資料庫",
    # conv100-r7：改價搗蛋
    "價格全部", "打對折", "全部打折",
    # conv100-r8：破壞語 + 改庫存數字變體（「調成」漏擋）
    "炸掉", "炸了", "燒掉", "數字調成", "庫存數字調",
    # conv100-r9：抱怨系統（「壞掉」會撞到期詞）
    "系統壞",
    # conv100-r13：抱怨語變體/嗆聲
    "壞掉了吧", "壞了吧", "看什麼看",
    # conv100-r10：資料外傳
    "傳到我", "資料傳",
    # conv100-r11：白拿/問展示機價格
    "免費送我", "這台機器",
    # conv100-r15：白拿變體
    "算零元", "算我的",
    # ── EN build：英文閒聊/離題詞（原黑名單全中文 → 'order me a pizza'
    #    這類英文閒聊沒被擋、掉進 clarify 而不是婉拒）──
    # ⚠️ "lunch" 移除——英文版 'lunch box' 是 Glass Food Containers 的合法
    #   別名（'south shipped 100 lunch box' 被黑名單擋成 rejected）。
    #   防閒聊改用更明確的片語（見下方 lunch break / for lunch）。
    "pizza", "dinner", "breakfast", "coffee for me", "order me",
    "lunch break", "for lunch", "eat lunch", "having lunch", "lunch time",
    "weather", "joke", "song", "music", "movie", "game", "translate",
    "who are you", "your name", "how are you", "stock market", "bitcoin",
    "recipe", "restaurant", "taxi", "flight", "hotel",
    # ── r5-voice：問系統在幹嘛/是什麼（user 現場抓到 `what are you doing`
    #   回了全店 60 項概覽）。這族的共同結構是**問句的詞被停用詞剝光 →
    #   空 keyword → 落到「全店概覽」fallback**，而不是被守門員婉拒。
    #   `who are you` / `are you a robot` 早就在黑名單所以正確擋下，
    #   `what are you …` 這側漏了一整族。
    #   ⚠️ 用完整片語不用裸前綴（坑 8）——實測撞到四句**合法倉管查詢**：
    #     'what are you'    ⊂ 'what are you selling most of'（熱銷）
    #                       ⊂ 'what are you missing'（缺貨）
    #     'whats happening' ⊂ 'whats happening with the toothbrush count'（RCA）
    #     'what do you do'  ⊂ 'what do you do with expired items'（到期處理）
    #   → 只收「後面不會再接倉管語」的完整閒聊句。
    "what are you doing", "what are you up to", "what're you doing",
    "whats going on", "what's going on", "how do you work",
    "how does this work", "what is this thing", "what r u doing",
    # ── EN build（劇情批 r5）：展場「場館問題」——訪客把 demo 機當服務台。
    #   原本掉進 oov:noex 回「No item matching "wifi password"」（把它當成
    #   一個查不到的商品），該婉拒並導回倉管能力。
    #   ⚠️ 用片語不用裸詞（坑 8：業務詞會撞功能詞）——實測 'bathroom' 撞
    #   'bathroom cleaner stock'、'parking' 撞 'parking lot inventory'，
    #   兩者都是合理的倉管查詢 → 只收「明確在問場館」的片語。
    "wifi password", "wi-fi password", "wifi code", "whats the wifi",
    "where is the restroom", "where is the toilet", "where is the bathroom",
    "where can i park", "where is the parking", "how do i get to",
    "opening hours", "what time do you close", "what time do you open",
)


# ════════════════════════════════════════════════════════════════════
# 客服引導關鍵字
# ════════════════════════════════════════════════════════════════════
GUIDE_KEYWORDS = {
    "查倉管", "查倉", "查庫存系統", "看倉管", "看倉庫",
    "有什麼", "有什么", "可以查", "能查",
    "菜單", "菜单", "功能", "選項", "选项", "幫助", "帮助",
    "列表", "清單", "清单", "全部", "所有", "都有",
    "能做什麼", "能做什么", "可以做什麼", "會做什麼", "幫我做什麼", "看看", "導航", "導覽",
    "怎麼用", "怎麼操作", "教我", "使用說明", "怎麼玩",
    # r76：「新來的同事要用這系統 怎麼教」曾 rejected
    "怎麼教", "怎麼上手", "怎麼入門",
    "menu", "help", "list", "options", "what can", "guide",
    # ── r5-voice：英文只有上面六個詞，中文的「怎麼用/怎麼操作/教我/使用說明」
    #   一個對應都沒有 → `how does the transfer work` 這類「問功能怎麼用」
    #   掉進商品比對回全店概覽（訪客想學怎麼操作，收到 60 項清單）。
    #   ⚠️ 用片語不用裸詞（坑 8）——實測撞到三句**合法商品查詢**，已移除：
    #     'how to use'   ⊂ 'how to use the yoga mat'
    #     'show me how'  ⊂ 'show me how many earphones are left'
    #     'instructions' ⊂ 'instructions for the coffee machine'
    #   （`_is_guide_request` 開頭雖有「含具體商品→當查詢」的排除，
    #    但不賭它涵蓋所有情況——詞表本身就該乾淨。）
    "how do i use this", "how does it work", "how does this work",
    "how does the transfer work", "how does transfer work",
    "teach me", "tutorial", "what else can you", "anything else you can",
    "capabilities", "getting started", "how do i start", "where do i start",
    # r12（探針批）：**問展示本身**——展場訪客站到機器前，第一句常常是
    #   「這是什麼」而不是查商品。實測 `what's this demo about` 回**熱銷榜**
    #   （demo 撞到什麼詞就路由過去），訪客第一印象就是答非所問。
    #   ⚠️ 一律用片語：裸 this/about/demo 會撞到合法查詢。
    "this demo", "the demo", "about this demo", "what is this demo",
    "what's this demo", "whats this demo", "what is this system",
    "what's this system", "whats this system", "what is this thing",
    "real data", "is this real", "fake data", "demo data",
    # ── r14（展場開場白批）：訪客站到機器前的第一句。實測整類被**黑名單
    #   婉拒**或路由到全店概覽——第一印象就是答非所問／被打槍。
    #   ⚠️ 一律片語：裸 what/who/explain 會撞掉大量合法查詢。
    "what is this", "what's this", "whats this",
    "what is this thing", "what are you", "who are you",
    "what do you do", "what does this do", "what can this do",
    "tell me about yourself", "tell me about this",
    "explain this", "explain the system", "explain this system",
    "what should i ask", "what can i ask", "what questions",
    "give me some examples", "give me an example", "show me an example",
    "how do i use", "help me get started", "what do i do",
    "introduce yourself", "what is this demo about",
}

GUIDE_MSG = (
    "Here is what I can look up in the warehouse system:\n\n"
    "📦 Stock lookup\n"
    "  • bluetooth earphones stock\n"
    "  • food and drinks category stock\n"
    "  • how much sparkling water is left in north warehouse\n\n"
    "🚨 Low stock alerts\n"
    "  • stock alerts\n"
    "  • north warehouse low stock list\n"
    "  • which items are running low\n\n"
    "🔥 Best sellers\n"
    "  • best sellers this week\n"
    "  • top selling sports gear this month\n"
    "  • which items are slow moving\n\n"
    "🔗 Related stocking analysis\n"
    "  • what else do bluetooth earphone buyers get\n"
    "  • related items for the coffee machine\n"
    "  • what else do diaper buyers grab\n\n"
    "⏰ Expiry alerts\n"
    "  • what is expiring soon\n"
    "  • north warehouse expiry list\n"
    "  • shelf life for the food category\n\n"
    "📊 Inbound and outbound records\n"
    "  • what came in today\n"
    "  • how many earphones shipped this week\n\n"
    "🏭 Warehouse comparison\n"
    "  • which has more stock, north or south\n"
    "  • compare turnover of central and south warehouse\n\n"
    "Tap a shortcut button below, or just type your question!"
)


# ── r10：英文詞典（守衛最後一句 scks 用）────────────────────────────
#   用途只有一個：區分「英文真詞但庫裡沒有」（hair/shampoo/bicycle → 誠實
#   查無）與「錯字」（scks/traash → 該修）。兩者在字元相似度上完全相同，
#   詞典是唯一可用的訊號。
#   ⚠️ 系統字典（Debian 套件 wamerican，RPI5 已內建 /usr/share/dict/）不是
#     本專案資產 → **檔案不存在時必須優雅降級**（回 None＝這道容錯不啟用，
#     其餘行為完全不變），否則違反雷 7「test/ 目錄必須自足」。
_EN_DICT_CACHE: set | None = None
_EN_DICT_LOADED = False


def _load_en_dict() -> set | None:
    """載入英文詞典（lazy + 快取；985KB 只讀一次）。沒有就回 None。"""
    global _EN_DICT_CACHE, _EN_DICT_LOADED
    if _EN_DICT_LOADED:
        return _EN_DICT_CACHE
    _EN_DICT_LOADED = True
    for _p in ("/usr/share/dict/american-english",
               "/usr/share/dict/british-english",
               "/usr/share/dict/words"):
        try:
            with open(_p, encoding="utf-8", errors="ignore") as _f:
                _words = {w.strip().lower() for w in _f
                          if w.strip() and "'" not in w}
            if len(_words) > 10000:
                _EN_DICT_CACHE = _words
                log.info(f"[en-dict] 載入 {_p}：{len(_words)} 詞")
                return _EN_DICT_CACHE
        except Exception:
            continue
    log.info("[en-dict] 找不到系統英文詞典 → 單 token 錯字容錯停用（其餘不受影響）")
    return None


def _en_typo_hits_item(tok: str) -> str:
    """孤立 token 是某商品名的錯字 → 回**商品名**；否則回 ""。

    （呼叫前必須先確認它不是字典真詞——那是這道容錯的前提，見 _load_en_dict）
    門檻 0.85 是**實測**切出來的，不是拍腦袋：
      scks→socks 0.889（真錯字）／ stok 最高才 0.667（該留給既有路徑）
    另要求與第二名有差距，避免平手時亂猜（違反不猜原則）。
    ⚠️ 回商品名而非布林——守門員只需要「有沒有」，但下游 _extract_sku_keyword
       需要**拿到名字**才能填 keyword，否則守門員放行了卻回全店概覽（實測過）。
    """
    try:
        import difflib as _dl_t
        import warehouse as _W_t
        _cand: dict[str, str] = {}
        for _it in _W_t.state().items:
            for _w in _re.split(r"[^a-z0-9]+", _it["name"].lower()):
                if len(_w) >= 4 and _w.isalpha():
                    _cand.setdefault(_w, _it["name"])
        if not _cand:
            return ""
        # ②商品自身詞彙豁免——cookware / beanie / onesie 不在字典裡卻是真商品
        #   詞，不能被當成錯字（它們走上面的精確比對路徑）
        _t = tok.lower()
        if _t in _cand or _t.rstrip("s") in _cand:
            return ""
        _scored = sorted(
            ((_dl_t.SequenceMatcher(None, _t, _w).ratio(), _w)
             for _w in _cand), reverse=True)
        if not _scored or _scored[0][0] < 0.85:
            return ""
        # 與第二名要有差距（≥0.06），平手代表訊號不明確 → 不猜
        if len(_scored) > 1 and (_scored[0][0] - _scored[1][0]) < 0.06:
            return ""
        log.info(f"[en-typo-gate] {tok!r} → {_cand[_scored[0][1]]!r} "
                 f"(ratio={_scored[0][0]:.3f})")
        return _cand[_scored[0][1]]
    except Exception:
        return ""


def _en_typo_keyword(text: str) -> str:
    """整句掃一遍，找出「非字典真詞的孤立錯字」對應的商品名（找不到回 ""）。

    r10：`do we have scks` 的收尾——守門員放行後，下游還要**真的抽到**
    Socks，否則回全店概覽（守門員只回布林，不傳遞找到的名字）。
    """
    _d = _load_en_dict()
    if not _d:
        return ""
    for _t in _re.split(r"[\s\-/]+", text.lower()):
        _t = _t.strip(" ?.!,'\"")
        if len(_t) < 4 or not _t.isalpha():
            continue
        if _t in _d or _t.rstrip("s") in _d:      # 真詞 → 誠實查無，不修
            continue
        _hit = _en_typo_hits_item(_t)
        if _hit:
            return _hit
    return ""


# ── 功能詞錯字容錯（2026-08-02）────────────────
#   既有 `_en_typo_hits_item` 的候選集**只掃商品名主檔** ⇒ 拼錯商品名
#   救得到、拼錯**功能詞**完全裸奔。variant_probe.py 實測 typo 類 6/6 全破：
#     invetory / inventry → rejected（訪客拼錯一個字就被當閒聊趕走）
#     recieved → 判成查詢而非寫入（**語意翻轉**，最嚴重）
#     wearhouse / warehose / stok → clarify
#   ⇒ 容錯原本只保護了「查什麼」，沒保護「做什麼」。
#   ⚠️ 候選集已撞商品主檔：51 詞中僅 `hot` 衝突 → 已排除。
_EN_FUNC_WORDS = (
    "inventory", "warehouse", "warehouses", "received", "receive",
    "shipped", "stock", "stocks", "report", "reports",
    "script", "scripts", "file", "files", "alert", "alerts",
    "schedule", "schedules", "movement", "movements", "transfer",
    "expiring", "restock", "restocking", "safety", "items",
    "compare", "history", "record", "records", "purchase",
    "order", "orders", "supplier", "category", "seller", "sellers",
    "confirm", "cancel", "delete", "between", "below", "above",
    # 匯出/報告類（2026-08-04,坑 24：候選集原本只涵蓋查詢詞,
    #   'exprot' 根本沒有修復目標可配）
    "export", "exports", "download", "audit", "stocktake",
    "generate", "quarter", "yesterday",
)


def _en_funcword_fix(text: str) -> str:
    """拼錯的**功能詞**還原（掛入口，整條鍵都吃到）。

    判準與 `_en_typo_hits_item` 一致：非字典真詞才修、門檻 0.85、
    與第二名要有 0.06 差距（平手不猜）。
    候選以**詞幹**聚合，避免 warehouse/warehouses 互相稀釋分數。
    """
    if not _is_mostly_english(text):
        return text
    try:
        import difflib as _dl_f
        import warehouse as _W_f
        _d = _load_en_dict()
        _pw = set()
        for _it_f in _W_f.state().items:
            for _w_f in _re.split(r"[^a-z0-9]+", _it_f["name"].lower()):
                if len(_w_f) >= 4 and _w_f.isalpha():
                    _pw.add(_w_f)
        out = []
        changed = False
        for _tok in _re.split(r"(\s+)", text):
            _bare = _tok.strip(" ?.!,'\"").lower()
            if len(_bare) < 4 or not _bare.isalpha():
                out.append(_tok)
                continue
            if _bare in _EN_FUNC_WORDS:
                out.append(_tok)
                continue
            if _d and (_bare in _d or _bare.rstrip("s") in _d):
                out.append(_tok)
                continue
            # ⚠️ **商品名優先**：像商品名就別碰（門檻放寬到 0.75）。
            #   `filtes`→filter 只有 0.833，商品名層自己救不到，
            #   但「do we have coffue filtes」原本靠別條路徑 PASS；
            #   功能詞層若把它改成 files(0.909) 就把那條路堵死了
            #   （守衛 891/892 的成因）。
            if _pw and max(_dl_f.SequenceMatcher(None, _bare, _w_f).ratio()
                           for _w_f in _pw) >= 0.75:
                out.append(_tok)
                continue
            _best = {}
            for _w_f in _EN_FUNC_WORDS:
                _stem = _w_f.rstrip("s")
                _r = _dl_f.SequenceMatcher(None, _bare, _w_f).ratio()
                if _r > _best.get(_stem, (0, ""))[0]:
                    _best[_stem] = (_r, _w_f)
            _sc = sorted(_best.values(), reverse=True)
            # 換位錯字（2026-08-04）：'exprot'→'export' ratio 0.833 過不了
            #   0.85,但**同字母異序＋同首字母**是換位打字的精準訊號 ——
            #   定向放行,不放寬全域門檻（記憶方法論：絕不放寬全域門檻）。
            _anag = bool(_sc and _sc[0][0] >= 0.80
                         and sorted(_bare) == sorted(_sc[0][1])
                         and _bare[0] == _sc[0][1][0])
            if not _sc or (_sc[0][0] < 0.85 and not _anag):
                out.append(_tok)
                continue
            if not _anag and len(_sc) > 1 and (_sc[0][0] - _sc[1][0]) < 0.06:
                out.append(_tok)
                continue
            out.append(_tok.lower().replace(_bare, _sc[0][1]))
            changed = True
            log.info(f"[en-funcword] {_bare!r} → {_sc[0][1]!r} "
                     f"(ratio={_sc[0][0]:.3f})")
        return "".join(out) if changed else text
    except Exception:
        return text

def _text_has_item_name(text: str) -> bool:
    """句中含任一真商品名的 3 字滑窗片段 → 視為具體查詢，不進 guide 導覽。
    r24：「查一下橡膠清潔手套全部加起來有幾雙」「幫我看看看看濕紙巾」的商品
    不在 SPECIFIC 手列名單，被「全部/看看」搶成 guide——手工枚舉必有盲區，
    改用商品主檔滑窗做結構性判準（60 商品 × ~5 窗，成本可忽略）。"""
    try:
        import warehouse as _W_gd
        s = text.lower().replace(" ", "")

        # ── EN build：英文句走**詞級**比對，不能用 3 字元滑窗 ──────────────
        #   中文 3 個字是有意義的詞，英文 3 個字母不是。去空白後滑窗會撞爆：
        #     'hi there'        → 'the' (Wireless Bluetooth EarpHOnes…)
        #     'delete all items'→ 'ele' / 'tea'
        #     'good morning'    → 'ing' / 'ood'
        #     'are you a robot' → 'are' (CookwARE) / 'bot' (BOTtle)
        #     'qwertyuiop'      → 'wer' (PoWER Bank)
        #   → 守門員對**所有**英文搗蛋句放行（守衛 chat/probe/noex/guidey
        #   共 26 句 FAIL 全是這個成因）。英文改判「整個單詞相符」。
        if _is_mostly_english(text):
            _toks_gd = {w.strip(" ?.!,'\"") for w in _re.split(r"[\s\-/]+", text.lower())}
            _toks_gd = {w for w in _toks_gd if len(w) >= 3}
            if not _toks_gd:
                return False
            for _it_en in _W_gd.state().items:
                for _w_en in _re.split(r"[\s\-/]+", _it_en["name"].lower()):
                    _w_en = _w_en.strip()
                    # 純數字/規格詞（2m、28cm、10000mah、5pcs、200g）不算商品指涉
                    if len(_w_en) < 3 or any(c.isdigit() for c in _w_en):
                        continue
                    if _w_en in _toks_gd:
                        return True
                    # 單複數（earphones vs earphone、crackers vs cracker）
                    if _w_en.rstrip("s") in {t.rstrip("s") for t in _toks_gd} \
                            and len(_w_en.rstrip("s")) >= 4:
                        return True
            # 英文俗稱（earbuds / power bank / toilet paper…）也算具體指涉
            try:
                from alias_en import ALIAS_EN as _AL_gd
                _tl_gd = text.lower()
                for _k_gd in _AL_gd:
                    if _re.search(r"(?<![a-z])" + _re.escape(_k_gd.lower())
                                  + r"(?![a-z])", _tl_gd):
                        return True
            except Exception:
                pass
            # ⚠️ 最後讓**錯字模糊層**表態——否則守門員與錯字容錯互相打架：
            #   'keyyboard on hand' / 'cordles mose' / 'sprkling wateer'
            #   的詞當然不會精確等於商品名單詞 → 被守門員擋在門外 rejected，
            #   上一輪剛修好的錯字句全部回歸（守衛 inv 41→52 就是這樣來的）。
            #   ⚠️ 要傳**剝過虛詞的核心詞**：直接傳整句的話 'on hand' 這種
            #   虛詞會觸發 _en_fuzzy_keyword 的陌生修飾詞防線而回空
            #   （anti-hallu 那條踩過同一個坑）。
            #   OOV 句（microwave/bicycle）剝完仍是陌生詞，防線照樣擋住。
            try:
                _gd_core = _re.sub(
                    r"\b(?:how|many|much|whats|what|is|are|the|a|an|of|do|does|"
                    r"we|i|you|got|have|has|any|some|there|show|me|tell|give|"
                    r"list|check|look|looking|see|find|get|left|remain|remaining|"
                    r"stock|stocks|inventory|count|counts|on|hand|in|at|for|to|"
                    r"from|with|now|currently|available|availability|status|"
                    r"please|pls|quantity|qty|units?|level|levels|number|"
                    r"warehouse|wh|north|central|south|total|still|right|"
                    r"hows|its|it)\b", " ", text, flags=_re.I)
                _gd_core = _re.sub(r"\s+", " ", _gd_core).strip(" ?.!,")
                if _gd_core and _en_fuzzy_keyword(_gd_core):
                    return True
            except Exception:
                pass
            # 功能描述句也算「指到了商品」——'something to clean teeth'
            #   沒有任何商品名單詞，但它明確在問電動牙刷，不該被守門員擋
            try:
                from descriptor_en import descriptor_hit_en as _dsc_gd
                if _dsc_gd(text):
                    return True
            except Exception:
                pass
            # ── r10：**詞典把關的單 token 錯字容錯**（守衛最後一句 scks）─────
            #   `do we have scks` 被守門員擋在門外（19ms，連 keyword 抽取都沒
            #   進到）。上面的 _en_fuzzy_keyword 對**孤立單 token** 對不到
            #   （'socks' 單獨餵可以，'scks' 不行），所以這裡補最後一道。
            #   ⚠️ 為什麼需要英文詞典：`scks→socks` 與 `hair→chair` 在字元層面
            #     **完全相同**（都是 0.889、都是插入型）。差別只在 hair 是真詞、
            #     scks 不是。守衛的 noex 反例（hair dryers / shampoo / bicycle /
            #     microwave / chairs for the office）**全是英文真詞**，錯字
            #     （scks/traash/powr/coffe/stok）**全不是** → 這就是可用的訊號。
            #   三重條件缺一不可（同坑 8：放寬英文分支必製造誤配）：
            #     ①不在字典（排除 hair/chairs/shampoo…真詞查無）
            #     ②不是商品自身詞彙（cookware/beanie/onesie 非字典卻是真商品詞）
            #     ③ratio ≥ 0.85 且與第二名有差距（scks→socks 0.889 vs 次名
            #       0.667；stok 最高才 0.667 → 不修，維持既有路徑處理）
            try:
                if _en_typo_keyword(text):
                    return True
            except Exception:
                pass
            return False

        # r43：單字通稱（帽子/鍋子…）也算具體商品指涉——「帽子有哪些」曾被 guide
        # 拒絕，但通稱表能導到毛帽/遮陽帽清單，該讓句子進查詢流程
        if any(_gt in s for _gt in getattr(_W_gd, "_GENERIC_QUERY_FALLBACK", {})):
            return True
        for _it_gd in _W_gd.state().items:
            nm = _it_gd["name"].lower().replace(" ", "")
            # r75 縱深防禦：名稱不足 2 字（歷史髒資料的空名商品）不可比對——
            # 「"" in s」恆真曾讓守門員對亂打字全放行
            if len(nm) < 2:
                continue
            if len(nm) <= 3:
                if nm in s:
                    return True
                continue
            for i in range(len(nm) - 2):
                if nm[i:i + 3] in s:
                    return True
            # r44：核心名尾 2 字也算（「毛帽」是「保暖毛帽」尾 2 字，3 字滑窗掃不到
            # →「不知道能不能查一下毛帽」曾被 guide 搶走）。僅取純中文尾避免 ml/kg。
            _core_gd = _it_gd["name"].split()[0].lower()
            _tail_gd = _core_gd[-2:]
            if (len(_core_gd) >= 3 and len(_tail_gd) == 2
                    and all("一" <= c <= "鿿" for c in _tail_gd) and _tail_gd in s):
                return True
    except Exception:
        pass
    return False


def _is_guide_request(text: str) -> bool:
    """判斷訪客是否想看倉管工具總覽。
    優先排除：句中已含具體商品 / 類別 / 倉庫關鍵字 → 當查詢、交給 LLM
    """
    s = text.strip().lower()
    if len(s) < 2:
        return False
    # r78：「那全部倉都改150好了」——帶數字的設定句不是引導請求
    # （「全部」是 GUIDE_WORDS 曾把 config 意圖吃掉）
    if re.search(r"\d", s) and any(w in s for w in ("改", "設", "調")):
        return False
    # ⚠️ EN build：門檻「字元數 > 20」是為中文調的（中文 20 字很長）。英文
    #   字元數是中文 2-3 倍——'best sellers this week' 才 4 詞卻 22 字元、
    #   'compare north and south' 4 詞 23 字元 → 兩句都掉進長句 fallback，
    #   而 _long_specific 又幾乎全中文 → 合法功能查詢被判成「碎念」回導覽頁。
    #   英文改用**單詞數 > 12**（≈中文 20 字的資訊量），與其他長度閘門同款處理。
    _too_long = (len(s.split()) > 12) if _is_mostly_english(s) else (len(s) > 20)
    if _too_long:
        # 長句碎念 fallback（第17輪）：展場訪客的長句閒聊（「逛展逛了一整天
        # 腳好痠…過來看看」）夾帶守門員字誤入功能路由。長句若無任何具體
        # 查詢線索（SPECIFIC 詞/數字）→ 給引導頁，比亂路由好。
        # 第18輪回歸補：線索詞要涵蓋連帶（買）/查詢（多少/剩）/紀錄/比較/
        # 警示等所有意圖家族，「買 coffee machine 的人還買什麼」曾被誤攔。
        _long_specific = ("庫存", "進", "出", "調", "退", "缺", "到期", "熱銷",
                          "報告", "警示", "排程", "採購", "盤點", "安全", "倉",
                          "買", "賣", "多少", "剩", "紀錄", "記錄", "明細",
                          "比較", "通知", "提醒", "月報", "報表", "體檢",
                          "對帳", "少了", "怪", "coffee", "stock", "buy",
                          # EN build：英文具體查詢線索
                          "sell", "seller", "compare", "warehouse", "north",
                          "central", "south", "inventory", "left", "how many",
                          "expiring", "expire", "low", "restock", "reorder",
                          "moved", "movement", "transfer", "received",
                          "shipped", "alert", "notify", "safety", "report",
                          "list", "count", "why", "who", "match", "record")
        if (not any(w in s for w in _long_specific)
                and not re.search(r"\d", s)
                and not _text_has_item_name(s)):
            return True
        return False
    if re.fullmatch(r"\d+", s):
        return False
    # 含明確類別 / 倉庫 / 商品關鍵字 → 不視為引導
    SPECIFIC = (
        "電子", "家電", "廚具", "食品", "飲料", "日用", "服飾", "運動",
        "北倉", "中倉", "南倉", "北區", "中區", "南區",
        "耳機", "悶燒", "氣泡", "咖啡", "洗衣", "衛生", "瑜珈", "水壺",
        "藍牙", "充電", "蚊香", "牛仔", "筆電",
        "庫存", "缺貨", "斷貨", "補貨", "警示", "熱銷", "熱賣", "滯銷", "進貨", "出貨",
        "週轉", "進出",
        # 補貨口語（RPI5 v21：「幫我看看哪些要補」被 guide 攔，沒進 C3 low_stock）
        "要補", "該補", "得補", "快補", "趕快補", "需要補", "缺的",
        # 缺貨/滯銷口語（conv100-r5：「有什麼商品快見底了」「給我看看哪些貨快斷了」被 guide 攔）
        "見底", "快斷", "斷了", "亮紅燈", "沒人買", "賣不動", "開天窗", "警戒",
        # conv100-r6：斷炊/吃緊/快空/急診/不能賣/進倉/墊底
        "斷炊", "吃緊", "快空", "急診", "不能賣", "進倉", "出倉", "墊底",
        # conv100-r7：「取消所有排程」的「所有」曾被 GUIDE_KEYWORDS 搶走
        "排程", "見紅", "速配", "賺錢", "沒動靜",
        # 「剩多少 / 還剩 / 幾個 / 夠不夠」是具體查詢語氣，不是要看功能總覽
        # （「看看14吋筆電包剩多少」「今天有什麼進出嗎」曾被 guide 誤攔）
        "剩", "多少", "幾個", "還有", "夠不夠", "夠賣", "堅果",
        # r27：「本月全部異動總覽」的「全部」曾搶成 guide
        "異動", "總覽",
        # r29：「全部倉一共幾項商品」曾搶成 guide
        "幾項", "幾種",
        # r30：「全部商品裡最貴的前五名」曾搶成 guide（讓給價格直答）
        "最貴", "最便宜",
        # r59：「全部的啞鈴都出光」曾搶成 guide（讓給比例出貨攔截）
        "出光", "出掉", "清光",
        "連帶", "也買", "一起買", "搭配", "帶動", "好夥伴",
        "到期", "過期", "保存期限", "效期", "保鮮", "賞味", "即期",
        "壞掉", "快壞", "快爛", "快過期",
        # ── EN build：英文具體查詢線索。GUIDE_KEYWORDS 含 "list"，
        #    'shortage list' / 'restock list please' / 'expiring stock list'
        #    因此被搶成導覽頁 → 這些功能詞在場就不是要看總覽。
        "stock", "inventory", "shortage", "restock", "reorder", "low",
        "expiring", "expiry", "expire", "movement", "movements", "moved",
        "best seller", "bestseller", "selling", "ranking", "popular",
        "hot", "slow", "compare", "transfer", "received", "shipped",
        "safety", "alert", "schedule", "report", "audit", "reconcil",
        "discrepanc", "anomal", "short", "supplier", "purchase", "order",
        "value", "worth", "count", "left", "remaining", "how many",
    )
    for h in SPECIFIC:
        if h in s:
            return False
    # 句含真商品名（3 字滑窗）→ 是具體查詢不是要導覽（r24）
    if _text_has_item_name(s):
        return False
    for kw in GUIDE_KEYWORDS:
        if kw in s:
            return True
    return False


# ════════════════════════════════════════════════════════════════════
# 校正層：倉管版 5 條規則 (C1-C5)
#
# 設計依據：raw Q8 GGUF 預期 ~85% → +校正 = E2E ≥ 95%
#
# C1: query_inventory 沒抽到 keyword 但 user_text 含商品意圖詞 → 補 keyword
# C2: 「最近 / 這幾天 / 這陣子」LLM 隨機選 today/this_week → 強轉 this_week
# C3: 「快沒了 / 缺貨 / 補貨 / 庫存警示」LLM 走 query_inventory → 強轉 list_low_stock
# C4: 「賣最好 / 最熱門 / 滯銷 / 賣最差」LLM 走 query_movement/inventory → 強轉 list_hot_items
# C5: compare_warehouses 漏 slot（只給 1 個 warehouse）→ fallback help
# ════════════════════════════════════════════════════════════════════

VALID_CATEGORIES = set(finance.CATEGORY_LABEL.keys())
VALID_WAREHOUSES = {"north", "central", "south", "all"}
VALID_PERIODS    = {"today", "yesterday", "this_week", "last_week", "this_month"}

# 商品意圖詞（C1 用）
_INVENTORY_INTENT_WORDS = (
    "庫存", "存量", "還有", "剩", "幾件", "多少", "幾個", "查詢", "查", "看",
    "stock", "inventory",
)

# 缺貨意圖詞（C3 用）
_LOW_STOCK_INTENT_WORDS = (
    "快沒", "缺貨", "補貨", "庫存警示", "庫存告急", "存量不足",
    "庫存不足", "低庫存", "存量警報", "警示", "告急", "補不上", "見底",
    "斷貨", "斷貨危機", "需要進貨", "該進貨", "警戒線", "緊急補", "該補",
    # 補貨口語（RPI5 v21 抓到：「有哪些是要趕快補的」誤走 hot、
    # 「缺的東西大概要補多少」誤走 manage_config → 缺貨清單已含 days_left/
    # suggest_qty，正好回答「要補什麼、補多少」）
    # 注意：這清單要跟 _is_guide_request 的 SPECIFIC 補貨詞對齊，否則「得補」
    # 躲過 guide 卻沒被 C3 接住 → 落 rejected（RPI5 v21 二輪抓到）
    "要補", "趕快補", "該補的", "要補的", "得補", "需要補", "快點補",
    "缺的東西", "缺什麼", "缺哪些", "補多少", "要補多少", "補幾個",
    "補一補", "補一下", "該補", "得補了", "缺的補", "要補了",
    # 缺貨口語變體（RPI5 conv100-r2：拉警報/水位過低/撐不住/叫貨 誤走 run_script/movement/hot）
    "拉警報", "警報", "水位過低", "水位太低", "水位低", "撐不住", "撐不下去",
    "叫貨", "趕緊叫貨", "要叫貨", "該叫貨", "快斷", "見底", "快見底", "存量太低",
    # conv100-r5：亮紅燈/開天窗 誤走 hot/rejected
    "亮紅燈", "開天窗",
    # conv100-r6：斷炊/吃緊/掛急診/快空/裸「缺的」
    "斷炊", "吃緊", "掛急診", "急診", "快空", "缺的",
    # conv100-r7：見紅/撐不到/安全線以下/危險名單
    "見紅", "撐不到", "安全線以下", "危險名單", "庫存危險",
    # conv100-r13：庫存快不夠的（裸「不夠」會誤傷「夠不夠賣」查詢句，只收精確詞）
    "快不夠",
    # r19：「缺最兇的前三名」曾回熱銷榜（完全相反的誤導）
    "缺最兇", "缺得最兇", "最缺", "缺最多",
    # r20：「存貨快歸零的有哪些」（裸歸零已從黑名單窄化）
    "快歸零", "歸零的",
    # r22：「再一週就沒貨的有哪些」曾回熱銷榜（相反誤導）
    "就沒貨", "快不行",
    # r23：要進貨的/彈盡糧絕（曾回無關單品/進出統計）
    "要進貨", "彈盡糧絕", "撐不了",
    # r25：「再不補就斷的有哪些」（RPI5 曾整句 rejected）
    "再不補", "就斷的", "就要斷",
    # r30：「有啥要趕快進貨的」插字變體、「庫存最危險的」
    "趕快進貨", "要趕快進", "最危險",
    "low stock", "restock", "running low", "alert",
    # ── EN build：英文缺貨詞（原表幾乎全中文 → 'whats about to run out'
    #    'which items need reordering' 不命中 _c3e_low → C3e 把 clf conf=1.00
    #    的 list_low_stock 降級成 query_inventory 全店概覽）──
    "run out", "running out", "about to run out", "run low", "runs low",
    "reorder", "reordering", "need reorder", "needs restocking",
    "need restocking", "restocking", "low on stock", "low inventory",
    "almost out", "nearly out", "out of stock", "short on", "shortage",
    "below safety", "safety stock", "need to order", "needs ordering",
    "what's low", "whats low", "replenish",
    # 守衛第 10 輪：這些常見講法沒收 → 落到商品比對/RCA
    "getting low", "gets low", "getting short", "running short",
    "should i order", "should we order", "what to order",
    "order anything", "need anything", "anything to order",
    "are short", "is short", "short of stock", "low stock list",
    "shortage list", "restock list", "reorder list", "need topping up",
    "top up", "topping up", "needs more", "need more stock",
    # r9：「minimum / par / threshold」是安全庫存的常見英文同義說法，
    #   `anything below the minimum` 原本回全店 60 項概覽（既有長尾）。
    #   ⚠️ **只收帶比較詞的片語**，不收裸 "minimum"——裸詞會把設定句
    #     `set X minimum stock to 80` 搶成缺貨清單（同坑 8 補充：
    #     功能詞撞業務詞）。設定句走 _cfg_key_in_text 讓路。
    "below the minimum", "below minimum", "under the minimum",
    "under minimum", "below the min", "under the min",
    "below par", "under par", "below the threshold", "below threshold",
    "under the threshold", "beneath the minimum",
    # r14+1（backlog 類1）：#31 'stockout risk items'/#52 'zero stock' 曾
    #   「查無此商品」。⚠️ 'stock out'（帶空白）**不可收**——substring 會
    #   撞守衛句 'low stock outdoor gear'（撞詞掃描實證），只收連寫形。
    "stockout", "stocked out", "zero stock",
    # r15：#11 'anything sitting at zero units' 曾誤擋、#10 'top anything
    #   up'（top…up 被 anything 隔開，'top up' substring 比不到）、
    #   #78 'healthy on stock' 反向問法兜 low_stock（79 模式：清單即答案）
    #   ⚠️ 'stock healthy' 不可收——撞 r17 保護句 'is the earphone stock
    #     healthy'（單品卡），只收 #78 的語序
    "zero units", "at zero", "top anything up", "top something up",
    "top it up", "top us up", "healthy on stock",
    # r16 #21：'anything close to running dry'
    "running dry", "close to running",
)

# 熱銷意圖詞（C4 用）
_HOT_INTENT_WORDS_HOT = (
    # 「賣得最好」隔了「得」比不到「賣最好」——r20 RPI5 平台分歧：LLM 幻覺
    # related{飲料} 被閘門拒，C4 沒詞可攔
    "賣最好", "賣得最好", "賣得最快", "最熱門", "熱銷", "暢銷", "賣最多",
    "銷量第一", "銷量冠軍", "搶手", "熱賣榜", "熱賣", "賣得最兇", "賣最兇",
    "排行榜", "銷售排行", "銷售冠軍", "人氣王", "賣翻", "銷路最好", "最好賣",
    # conv100-r6：「業績最好的商品」被 LLM 亂填 rank_type
    "業績最好", "業績冠軍",
    # conv100-r7：賺錢/賣得怎樣（「賣況」不能放這——「賣況最差」是滯銷）
    "賺錢", "賣得怎樣", "賣得如何",
    # r18：「這個月營收多少」曾回進出件數（答非所問）——熱銷榜每名帶營收數字
    "營收",
    # r19：「藍牙喇叭上週跟這週哪週賣得多」帶商品名 → C4-prod 轉該商品 movement
    "賣得多", "賣得少", "哪週賣",
    # r20：賣得嚇嚇叫
    "嚇嚇叫",
    # r21：「打果汁的賣得好嗎」descriptor 曾直達回庫存
    "賣得好嗎", "賣得好不好",
    # r26：「露營用品最近夯什麼」（夯 曾 fuzzy 亂配單品）
    "夯", "最夯",
    # r30：賣況/買氣（帶商品名時 C4-prod 轉該商品銷況）
    "賣況怎樣", "賣況如何", "買氣",
    "top selling", "best seller", "hot",
    # r14+2（#42）：'what sold over the weekend' 是熱銷清單問法——曾被
    #   LLM 幻覺 keyword 再被 carry-over 補成前句商品卡。weekend 含
    #   week substring → C4 period 自動 this_week。
    "what sold", "what was sold",
    # r15 #82：'skip the slow movers show me winners'——winners 靠 C4
    #   「後講的贏」（rfind）勝過前面的 slow movers
    "winners", "winner", "top performers", "best performers",
)
_HOT_INTENT_WORDS_SLOW = (
    "賣最差", "滯銷", "賣不掉", "最冷門", "賣最少", "銷量最差",
    # 滯銷口語（RPI5 conv100-r2：「哪些貨賣不動」誤走 movement）
    "賣不動", "賣不出去", "動不了", "乏人問津", "沒人買", "賣不太動",
    # conv100-r6：「銷售墊底的三名」誤走 low_stock
    "墊底", "銷售墊底",
    # conv100-r7：賣況最差/沒動靜
    "賣況最差", "沒動靜",
    # conv100-r10：賣不好
    "賣不好",
    # r28：最沒人氣（曾 rejected）
    "沒人氣", "最沒人氣",
    # conv100-r13：賣最不好
    "賣最不好", "最不好賣",
    # r17：「哪些商品從來沒動過」曾回今天進出總覽（答非所問）
    "沒動過", "沒有動過", "從來沒動", "都沒動", "沒在動", "沒賣過",
    "worst selling", "slow", "slow mover",
    # r14+1（網頁百句 backlog 類1）：倉管營運行話的滯銷家族——
    #   #30 'any dead stock this month' 曾回熱銷 TOP10（**反義誤導**，
    #   clf 判對 hot_items 但 rank 預設 hot）；#48 never sells/#51 least
    #   popular/#54 not selling 曾「查無此商品」。撞詞掃描：60 商品名
    #   零碰撞；守衛既有 'dead stock'/'least popular items'/'whats not
    #   moving' 三句期望 view=hot_items 不變、rank 修正為 slow=真正解。
    "dead stock", "never sell", "never sold", "least popular", "unpopular",
    "not selling", "not moving", "no movement", "hasnt moved", "hasn't moved",
    "isnt moving", "isn't moving", "zero sales", "no sales",
    # r15 #4：'which products have gone stale' 曾回熱銷（反義）
    "gone stale", "stale",
    # r16：#99 'and the worst'（帶 the 較安全，裸 worst 撞 low_stock 語境）
    #   ＋#15 collecting dust/#33 underperforming/#16 slowest sku
    "the worst", "collecting dust", "underperforming", "underperform",
    "slowest sku", "slowest item", "slowest items",
    # r18 #30：'worst seller'（單數——worst selling 有收、seller 沒）
    "worst seller", "worst sellers",
)

# 模糊時間詞（C2 用）
_VAGUE_TIME_WORDS = ("最近", "這幾天", "這陣子", "前陣子")

# 連帶意圖詞（C6 用）— 出現這些 → query_related_items
_RELATED_INTENT_WORDS = (
    "連帶", "也買", "也會買", "還會買", "還買了", "一起買", "一起賣",
    "順便買", "搭配", "帶動", "好夥伴", "通常還買", "也買了",
    "一起出貨", "連帶備貨", "帶貨", "買的人還", "買的人也",
    # 能力地圖範例用的情境化口語動詞（買帳篷「還扛了」裝備、買咖啡機「還配」了什麼）
    "還扛了", "還配了", "還帶了", "順手帶了", "還買什麼", "還扛什麼", "還配什麼",
    "還會拿", "會拿什麼", "還會帶", "加購", "一起結帳", "搭配銷售",
    # conv100-r5：「買瑜珈墊的人還會順手拿什麼」的「順手拿」漏收
    "順手拿", "還會順手",
    # conv100-r6：購物車還有什麼/黃金組合/順手抓
    "購物車", "黃金組合", "順手抓",
    # conv100-r7：速配
    "速配", "最速配",
    # conv100-r11：通常還拿什麼
    "還拿什麼", "通常還拿",
    # conv100-r12：都搭什麼買
    "搭什麼買", "都搭",
    # r27：「買防曬遮陽帽的還買啥」（還買啥 不在 還買了/還會買 覆蓋內）
    "還買啥", "還會買啥",
    # r18：「買了咖啡機還需要買什麼」
    "還需要買", "還要買什麼",
    # r20：「跟瑜珈墊類似的商品有哪些」
    "類似", "同類",
    # r23：最佳拍檔/對味
    "拍檔", "對味", "最麻吉", "麻吉",
    # r26：最佳搭檔（搭檔≠拍檔，曾被直達劫走）
    "搭檔", "最佳搭檔",
    # conv100-r14：都會多帶什麼
    "多帶", "會多帶",
    # r24：「運動壓縮臂套跟啥最搭」（gate 三表同步：NONQUERY/_TOOL_INTENT_GUARD 同補）
    "最搭", "跟啥搭", "跟什麼搭",
    # 「順便帶啥/順便買啥」的「順便」（RPI5 壓測抓到：只有「順便買」時
    # 「順便帶啥」落到 LLM 自由判斷，WIN11 判 related、RPI5 判 hot_items
    # ——硬體敏感的分歧。加規則 hard-return 消除不確定性）
    "順便", "還順便", "順便帶", "順便還",
    "bought together", "also buy", "frequently bought", "related item",
)

# 到期警示意圖詞(C7 用)
_EXPIRING_INTENT_WORDS = (
    "到期", "過期", "快到期", "即將到期", "保存期限", "效期", "保鮮期",
    "賞味期", "新鮮度", "快爛", "快壞", "壞掉", "快壞掉", "要爛了", "即期品", "即期",
    # conv100-r6：「快要不能賣的」
    "不能賣",
    "expire", "expiring", "expired", "shelf life", "best before",
)

# r44 C4-mv：進出量問句判準（進/出+量疑問緊鄰）——「上週出了幾件」「這個月進多少」
# 「出貨了沒」是 movement 不是庫存；描述直達/C3e/C13 各 hard-return 出口都要讓路。
_C4MV_RE = re.compile(
    r'([進出])貨?了?(幾|多少|了沒|沒有)'
    # EN build：英文進出量問句。'how much X moved this month' 的 clf 判
    #   query_movement conf=1.00，但 LLM 吐 search_log 且兩者都在候選內
    #   → C18 不仲裁 → 回單品庫存（守衛 mvt 類）。
    r'|\b(?:how\s+(?:much|many)|whats?|what)\b[^.?]{0,40}?'
    r'\b(?:moved|move|movement|movements|shipped|received|went\s+out|came\s+in|'
    r'in\s+and\s+out|ins?\s+and\s+outs?|inbound|outbound)\b'
    r'|\b(?:moved|shipped|received|went\s+out|came\s+in)\b[^.?]{0,20}?'
    r'\b(?:this|last|past)\s+(?:week|month|day)\b'
    # 無疑問詞的省略句：'this months in and out' / 'todays movements'
    r'|\b(?:in\s+and\s+out|ins?\s+and\s+outs?)\b'
    r'|\b(?:this|last|past|todays?|yesterdays?)\s*(?:week|month|day)?s?\s+'
    r'(?:movements?|inbound|outbound)\b',
    re.IGNORECASE)

# 功能描述直達的「非查庫存意圖」守衛詞（2026-07-09）：進貨/出貨/調貨/連帶/
# 銷況句常「描述命中+無查詢語氣」，錯字放寬會誤劫成查庫存。這些意圖詞出現時
# 即使描述命中也不走直達，交回原本的 movement/transfer/related/銷況路徑。
# 涵蓋 15 輪收斂累積的進出貨動詞（含 RPI5 回歸抓到的 新到/走了/掃走/訂了/抓/支援）。
_DESC_NONQUERY_INTENT = (
    # 進貨
    # r44：進出量問句（進多少/出了幾/出貨了沒——描述直達曾劫「南倉啤酒這個月進多少」）
    "進多少", "出多少", "進了幾", "出了幾", "出貨了沒", "進貨了沒", "出了沒", "進了沒",
    # r45：差額比較（「衛生紙比濕紙巾多多少」曾被描述直達劫成單品查詢）
    "多多少", "少多少", "多幾件", "少幾件",
    "進了", "進貨", "到貨", "收貨", "入庫", "補了", "補貨", "來貨", "收了",
    "送來", "送到", "卸了", "卸貨", "入了", "囤了", "囤貨", "補上", "補進",
    "補齊", "收到", "收一批", "入倉", "上架", "新到", "收進", "剛進", "叫",
    # r92：「加」是展場自然講法（「北倉加五十個滑鼠」）。這裡是描述直達的
    #   排除表——不加的話句子會在 C13b 之前就被判成庫存查詢。
    #   歧義由 C13b 的 _add_ok 把關（安全庫存/加起來等語境不算進貨）。
    "加",
    # 出貨
    "出貨", "出庫", "賣掉", "賣了", "銷貨", "售出", "出了", "買走", "拿走",
    "提走", "取走", "載走", "銷了", "賣出", "發貨", "發出", "送走", "訂走",
    "帶走", "出清", "出給", "領走", "領出", "走了", "掃走", "訂了", "取貨",
    "客退", "退回", "退貨", "退了",
    # 調貨（複合詞：動詞+方向/量詞，比單字「調/送/撥」安全，涵蓋 _transfer_verbs）
    "調撥", "調貨", "調到", "調去", "調過去", "調給", "調一", "調了",
    "移到", "移去", "移過去", "撥到", "撥去", "撥一", "勻給", "勻",
    "轉到", "轉去", "轉過去", "撤", "抓", "支援", "搬去", "搬到", "搬過去",
    "送到", "送去", "運到", "運去", "分到", "分給", "分過去", "過去南",
    "過去北", "過去中", "往南倉", "往北倉", "往中倉", "往南區", "往北區", "往中區",
    "到南倉", "到北倉", "到中倉", "到南區", "到北區", "到中區", "去南倉", "去北倉", "去中倉",
    # 連帶（r16 補：「買」從 _DESC_BLOCK 移除後，related 句靠這裡的精準詞擋——
    # 「買X的人/的都」「還會拿」是連帶分析語境，不是查該商品庫存）
    # 「還買」拆成「還買了/還會買」——「瑜珈墊還買得到嗎」的「還買」是可得性
    # 詢問不是連帶（r16）
    "黃金組合", "速配", "連帶", "搭配", "好夥伴", "一起買", "一起賣", "也買",
    "還買了", "還會買", "帶動", "組合", "會一起", "的人", "的都", "還會拿",
    "還會帶", "通常還",
    # r27：還買啥
    "還買啥",
    # r18：「買了咖啡機還需要買什麼」曾被 descriptor 直達搶成查庫存
    "還需要買", "還要買什麼", "需要搭",
    # r19 smoke：「北倉報廢5個保鮮盒」報廢不以 進/出 開頭，mv_qty 結構抓不到
    "報廢", "耗損", "丟棄", "損毀",
    # r22：「中倉今天到了一批牛仔褲 35件」批次進貨句（查詢句不會講一批/一票）
    "一批", "一票", "到了",
    # r20：「跟瑜珈墊類似的商品」是連帶/相關查詢、「X跟Y各剩多少」是多品查詢，
    # 都不可被單品描述直達搶走
    "類似", "同類", "相關商品", "的相關", "各剩", "各多少", "各有多少", "各還",
    # r21：「露營馬克杯跟露營燈哪個庫存多」兩商品比較
    "哪個庫存", "誰的庫存", "哪個比較多",
    # r23：「濕紙巾的最佳拍檔」「跟啞鈴最對味的」連帶句
    "拍檔", "對味", "麻吉",
    # r24：「跟啥最搭」（與 _RELATED_INTENT_WORDS / gate 三表同步）
    "最搭", "跟啥搭", "跟什麼搭",
    # r26：最佳搭檔
    "搭檔",
    # r25：流水=進出紀錄、提醒/通知=警示設定、比一下=兩商品比較——都曾被直達劫走
    "流水", "提醒", "通知", "就通知", "比一下", "比一比", "比較一下",
    # r26：「洗衣精最近進出如何」曾被直達劫走
    "進出",
    # r30：「啞鈴和健身環哪個多」兩商品句曾被直達劫走單品
    "哪個多",
    # 銷況
    "賣得如何", "賣得怎樣", "賣況", "賣得動", "最近賣", "銷量", "賣最",
)

# ── v2 Agent 進階工具校正詞（C8-C11）──────────────────────────────────
# C7b query_movement 保護詞：含這些詞 → 強制 movement，不被 RCA 攔截
_MOVEMENT_PROTECT_WORDS = (
    "進出紀錄", "進出狀況", "動了多少", "異動紀錄", "流水紀錄",
    "進出了多少", "這個月動", "上個月動",
    "出了多少貨", "進了多少貨", "出多少貨", "進多少貨",
    "出了多少", "進了多少",
    "進什麼貨", "出什麼貨", "有進什麼", "有出什麼",
    "進出明細", "出貨明細", "進貨明細", "異動明細", "進出流水",
    "最近的進出", "的進出", "進出統計", "進出量",
    # RPI5 全量回歸抓到的平台分歧（2026-07-06）：「最近一個月進貨多少」本機
    # LLM 判對、RPI5 判 manage_config → guide。詞表補齊讓兩平台走同一條路。
    "進貨多少", "出貨多少",
    # conv100-r6：「精釀啤酒最近的流向」「中倉這個月吞吐量」「這禮拜進倉的貨物清單」
    "流向", "吞吐", "進倉的",
    # conv100-r7：「純棉素T這週賣了幾件」「玻璃保鮮盒的異動歷史」
    "賣了幾", "賣了多少", "異動歷史",
    # conv100-r8：「慢跑鞋這季賣得動嗎」「昨天有出貨嗎」
    "賣得動", "有出貨", "有進貨",
    # conv100-r10：「慢跑鞋最近有人買嗎」
    "有人買",
    # conv100-r11：「這個月出貨幾台」
    "出貨幾", "進貨幾",
    # conv100-r12：「野炊鍋具組有進過貨嗎」
    "進過貨", "出過貨",
    # conv100-r13：「玻璃保鮮盒最近有補貨嗎」（問進貨紀錄不是缺貨清單）
    "有補貨", "補過貨",
    # r24：「登山水壺這禮拜出了幾支」（RPI5 平台分歧退成庫存 → 確定性層接手）
    "出了幾", "進了幾",
    # r25：「高蛋白乳清飲最近的流水」（裸「流水」曾被描述直達劫走）
    "流水", "的流水",
    # r26：出貨統計/有動嗎/進出如何（曾掉 OOV clarify、inventory、直達劫）
    "出貨統計", "進貨統計", "有動嗎", "有動靜", "進出如何", "最近進出",
    # r29：上一筆/最新一筆/這個月的紀錄（曾回熱銷榜/inventory）
    "上一筆", "最新的一筆", "最後一筆", "最近一筆", "的紀錄", "的記錄",
)

# C8 search_log（RCA）：追原因/對不上/異常 —— 跟 query_movement（純進出統計）區隔
_RCA_INTENT_WORDS = (
    # r34：「兜不起來」「湊不起來」是「兜不攏」的常見講法，過去漏收 → 「電動牙刷的帳
    #   怎麼兜不起來」被當成庫存查詢，回一般庫存數字（沒回答「為什麼對不上」）
    "兜不起來", "湊不起來", "兜不上",
    "對不上", "對不起來", "兜不攏", "帳不對", "短少", "短收", "少貨", "少了",
    "怎麼少", "為什麼少", "異常", "誰改的", "誰動的", "查原因", "追原因",
    # 裸「不對」移除（r18：「中倉...不對...南倉的滑鼠」自我修正句被誤判 RCA）
    # → 收窄成帳/數字語境
    "差異", "數字不對", "數量不對", "帳目不對", "對帳", "怪怪", "莫名其妙", "有問題", "有鬼",
    # 「帳面」移除：「露營帳篷帳面上有幾頂」純存量問句被誤轉 RCA（conv100-r7；
    # corpus「純棉素T帳面跟實際差好多」有「差好多」罩住不受影響）
    "有出入", "差好多", "詭異", "蒸發",
    # conv100-r5：跳來跳去/被偷/變少/不太對勁 全退成純庫存查詢
    "跳來跳去", "被偷", "偷了", "變少", "對勁",
    # r92（user 定調）：「多了/少了」是**盤點差異**語意＝帳對不上，不是進貨指令。
    #   原本只收「少了」→ 短少查得到、溢出查不到（可能是重複入帳/退貨沒沖銷，
    #   一樣要追）。⚠️ 不能用裸「多了」——「差不多了」是告別語（corpus 有兩條
    #   chat|差不多了 謝謝你 / ok瞭解 差不多了）→ 用「怎麼多」「變多」等明確形，
    #   「多了」則靠下方 _RCA_DIFF_RE 要求前後有數量/帳務語境才算。
    "怎麼多", "為什麼多", "變多", "多出來", "溢出", "多了",
    # conv100-r6：兜不上/少掉/怎麼回事
    "兜不上", "少掉", "怎麼回事",
    # conv100-r7：對不太起來/縮水/怪異/落差/追查
    "對不太起來", "縮水", "怪異", "落差", "追查",
    # conv100-r11：帳對嗎
    "帳對嗎", "的帳對",
    # r24：「帳目對不對得上」「庫存數字有點怪」都曾退成純庫存查詢
    "對不對得上", "帳目對不對", "對得上嗎", "有點怪", "數字有點", "數字怪",
    # r25：「帳兜得攏嗎」（兜不攏的正問形）、「數量對嗎」
    "兜得攏", "兜攏嗎", "數量對嗎", "數字對嗎",
    "discrepancy", "why", "who changed", "trace",
    # ── EN build：英文 RCA 詞（原表只有 4 個英文詞，'who moved the mouse stock'
    #    'the earphone numbers dont match' 都不命中 → gate-rescue 降級成庫存查詢）──
    "who moved", "who changed", "who took", "doesn't match", "dont match",
    "don't match", "doesnt match", "mismatch", "not match", "count off",
    "numbers off", "looks off", "seems off", "is off", "wrong", "missing",
    # r3：訪客的抱怨式講法（'the numbers dont look right' / 'this is wrong'）
    #   ⚠️ 複驗回歸：**不可放通用詞**——'back' 曾讓 'ok back to the earphones'
    #   被 clf 判成 search_log(0.94) → 回 RCA 報告（訪客只是想切回那個商品）。
    #   RCA 詞要**帳務語境專屬**，泛用動詞/介副詞一律不收。
    "look right", "looks right", "look correct", "seem right", "seems right",
    "not right", "isnt right", "isn't right", "doesnt look", "doesn't look",
    "off by", "out by", "no sense", "make sense",
    "shortfall", "short", "reconcil", "audit", "investigate", "look into",
    "went missing", "disappear", "strange", "weird", "odd", "unusual",
    "doesn't add up", "dont add up", "don't add up", "add up",
    # ── EN build 第二批（守衛 rca 14 句全 FAIL，逐句追 log 補）──
    #   複數形沒收（discrepancy 有、discrepancies 沒有）、「發生什麼事」
    #   句型、採購對帳問法全缺 → 這些句子連 RCA 判定的門都進不去
    "discrepancies", "anomaly", "anomalies", "irregular", "irregularity",
    "what happened to", "what happened with", "whats up with",
    "what's up with", "whats going on with", "something wrong",
    "purchase record", "purchase records", "po record", "po records",
    "receiving record", "receipt record", "check the purchase",
    "under-received", "under received", "short-received", "short received",
    "over-received", "over received", "count is strange", "count is off",
    "figures look wrong", "numbers look wrong", "numbers dont match",
    "stock doesnt add up", "doesnt add up",
)


def _has_rca_word(t: str) -> bool:
    """RCA 詞比對前先剝「多少」——「電子產品類還有多少貨」的「少貨」、
    「剩多少了」的「少了」是數量問句不是對帳異常（r17，第 1~8 輪
    「進出多少貨」同根 bug 的通用修法）。
    r92：同理剝「差不多了」——那是告別語（corpus 兩條 chat|差不多了 謝謝你），
    剝掉後「多了」才能安全當 RCA 裸詞（盤點溢出＝帳對不上，要能追）。"""
    import re as _re_rca
    _t = t.replace("差不多了", "").replace("差不多", "")
    _t = _re_rca.sub(r"多少", "", _t)
    # EN build：英文詞表全小寫，原句可能有大寫（'Sparkling Water 500ml stock
    #   doesnt add up'）→ 一併比對小寫版。中文無大小寫不受影響。
    return any(w in _t for w in _RCA_INTENT_WORDS) \
        or any(w in _t.lower() for w in _RCA_INTENT_WORDS)

# 寫入/複雜工具的「意圖詞閘門」——LLM 對閒聊句常自由發揮輸出 set_alert /
# generate_po / query_related 這類「不需要商品名就能開卡」的功能（WS 端沒有
# intent_clf 兜底時尤其嚴重）。execute 之前檢查：這些工具若句中完全沒有對應
# 意圖詞 → 判定為 LLM 幻覺，降級 rejected（第18輪訪客閒聊II抓到大量此類）。
# ── 英文匯出意圖共用判準（2026-08-04）──────────────────────────────
#   訪客不會只講 export：`give me a csv` / `can i get the records` 也是要匯出。
#   一個判準接整條鏈（_exp_intent / Pre-C-Cmp2 / C18 / _tool_intent_ok / C16）,
#   避免「修一層、下一層再擋」（坑 3——上次 bypass 就是被 run_script 意圖
#   閘門 reject 的）。動詞必須與受詞連用,`give me the stock` 不會誤中（坑 8）。
_EN_EXP_VERB_RE = re.compile(
    r"\b(?:export|download|dump|save|extract)\b|"
    r"\b(?:give|get|send|fetch)\s+me\b|"
    r"\bcan\s+i\s+(?:get|have|see)\b|"
    r"\bi(?:'d|\s+would)?\s+(?:need|want|like)\b", re.I)
_EN_EXP_OBJ_RE = re.compile(
    r"\b(?:movements?|transactions?|records?|logs?|history|"
    r"in\s*/?\s*out|csv)\b", re.I)


def _en_export_intent(text: str) -> bool:
    """句面有「匯出/索取動詞 + 進出受詞」＝明確要一份進出紀錄檔。"""
    return bool(_EN_EXP_VERB_RE.search(text) and _EN_EXP_OBJ_RE.search(text))


_TOOL_INTENT_GUARD = {
    # ⚠️ EN build：每個工具的詞表都補了英文（原表幾乎全中文 → 英文句一律不命中，
    #    被 gate-rescue 降級成 query_inventory，clf conf=1.00 的正確判斷全被打掉）
    "set_alert":        ("通知", "提醒", "警示", "告訴我", "就通知", "缺貨就", "低於", "盯",
                         "alert", "notify", "warn", "remind", "let me know",
                         "drops below", "drop below", "goes under", "falls below",
                         "less than", "threshold"),
    "generate_po":      ("採購", "補貨", "叫貨", "進貨單", "po", "下單", "開單", "該補",
                         "purchase order", "raise a po", "create a po", "reorder",
                         "restock order", "order the"),
    "generate_report":  ("報告", "報表", "體檢", "健檢", "月報", "週報", "日報", "彙整", "摘要", "總結",
                         "report", "summary", "overview report", "export report"),
    # 「一起/順便/還會」裸字太寬（「一起吃飯」誤命中 → related_empty，RPI5/WIN
    #  硬體分歧：本機 intent_clf route 判 related 繞過 C6-skip）。收緊成購物詞組。
    "query_related_items": ("買", "連帶", "搭配", "加購", "夥伴", "帶動", "連帶備貨",
                            "一起買", "一起賣", "一起結帳", "還會買", "還會帶", "也買",
                            "還配", "還扛", "順手帶", "順手拿", "順手抓", "購物車", "黃金組合",
                            "速配",
                            # r20：「跟瑜珈墊類似的商品」閘門缺詞 → rescue 轉回庫存
                            # r23：「最佳拍檔/對味/麻吉」同款（詞表/NONQUERY/gate 三處要同步）
                            # r24：「跟啥最搭」同款；r26：搭檔；r27：還買啥
                            "類似", "同類", "相關", "拍檔", "對味", "麻吉", "最搭", "跟啥搭", "跟什麼搭",
                            "搭檔", "還買啥",
                            # EN build：英文連帶詞
                            "bought with", "buy with", "sells with", "sold with",
                            "goes with", "go with", "pairs with", "pair with",
                            "related", "bundle", "cross-sell", "cross sell",
                            "also buy", "also bought", "also get", "along with",
                            "combo", "together with", "what else",
                            # r16 #50/#51：'who buys it with what'/'show me
                            #   similar items' 曾 not found
                            "similar", "buys with", "buy it with",
                            # 守衛第 10 輪：'recommend items for X' 沒收 →
                            #   gate-rescue 降級成庫存查詢
                            "recommend", "recommendation", "suggest items",
                            "what goes", "frequently bought", "often bought",
                            "customers also", "people also", "similar to",
                            "similar items", "matching items", "add-on",
                            "upsell", "complement"),
    "search_log":       _RCA_INTENT_WORDS,
    "list_files":       ("檔", "資料夾", "目錄", "紀錄檔", "有哪些資料",
                         "file", "files", "folder", "directory", "what data",
                         # r11：C13 已把「你能跑哪些腳本」轉成 list_files，
                         #   但這道意圖閘門沒有對應詞 → 又被擋成 rejected
                         #   （坑 3：修一層、下一層再擋一次，兩處要同步）
                         "script", "scripts", "report", "reports"),
    # run_script：需含腳本動作詞，否則閒聊句「一起吃飯」被 LLM 幻覺成
    # run_script{一起吃飯} → 執行時回「不在白名單，可用：月底盤點…」把內部
    # 腳本清單暴露給訪客（RPI5 v21 抓到）。沒動作詞 → 閘門擋成 rejected 婉拒。
    "run_script":       ("跑", "執行", "盤點", "匯出", "產出", "重產", "重新產生",
                         "重生", "重建", "做一次", "做個", "run", "export", "regenerate",
                         "stocktake", "stock count", "stock audit", "rebuild", "perform",
                         # 2026-08-04：health check 直達要過閘門（坑 3 一次接齊）
                         "health check", "audit"),
    # query_movement：需進出貨/紀錄/期間意圖詞。閒聊句「今天過得如何」的
    # 「今天」曾讓 LLM 幻覺 movement（第19輪）。含商品名的進出貨已走 C13b
    # create_movement，這裡只擋純幻覺的空 movement。
    # 注意：閘門要涵蓋所有合法 movement 語彙——「今天入庫了什麼」「今天
    # inbound 多少」曾被誤擋（入庫/inbound 漏收）。時間詞+動作字都算合理。
    "query_movement":   ("進", "出", "貨", "紀錄", "記錄", "明細", "異動", "流水",
                         "統計", "進出", "調", "退", "入庫", "出庫", "入倉",
                         "什麼", "多少", "哪些", "賣", "銷", "補",
                         "動了", "動過", "幾次", "流向", "吞吐", "進倉",
                         # r26：「昨天有動嗎」——三表同步（PROTECT 加了閘門沒加，
                         # C7b 轉過去被 gate-rescue 轉回 inventory）
                         "有動", "動靜",
                         "movement", "inbound", "outbound", "in", "out",
                         # EN build：英文進出貨詞
                         # r14+2（#21）：transfers 裸句經 en-admin 直達 movement
                         #   卻被本閘門「缺意圖詞」拒掉——調撥紀錄本就是 movement
                         "transfer", "transfers",
                         "movements", "shipment", "shipments", "shipped", "ship",
                         "received", "receive", "came in", "come in", "went out",
                         "moved", "transactions", "activity", "goods",
                         # 'show me the transaction logs' 的單數形（原本只有
                         #   複數 transactions）＋紀錄類同義詞
                         "transaction", "log", "logs", "record", "records",
                         "history", "ledger", "audit trail", "traffic"),
}


def _tool_intent_ok(func_name: str, user_text: str) -> bool:
    """該工具需要意圖詞才合理時，檢查句中有沒有。沒列在 guard 裡的工具一律放行。

    ⚠️ EN build（語音）：**要同時比對小寫**——guard 的英文詞全是小寫，而 ASR
    （whisper）一律輸出首字大寫（`What else do coffee beans buyers get?`）→
    連帶詞 'what else do' 不命中 → `gate-rescue` 把正確的 query_related_items
    降級成 query_inventory（訪客問搭售卻收到庫存數字）。
    這道閘門管七個工具，大小寫敏感等於**所有 ASR 句都少一層保護**。
    """
    words = _TOOL_INTENT_GUARD.get(func_name)
    if not words:
        return True
    # ⚠️ run_script × 匯出意圖（2026-08-04）：`give me a csv of the movements`
    #   沒有 run/export 字面,曾被這道閘門當幻覺 reject（上次 bypass 失敗的兇手）。
    #   句面有「索取動詞+進出受詞」＝合法的匯出請求,放行。
    if func_name == "run_script" and _en_export_intent(user_text):
        return True
    _ut_low = user_text.lower()
    return any(w in user_text or w in _ut_low for w in words)


def _intent_guard_rescue(func_name: str, func_args: dict, user_text: str):
    """意圖閘門攔下前的降級救援（RPI5 壓測 v21）：
    口語前綴會讓 LLM 對正經句輸出錯 function（查庫存→search_log、
    設安全庫存→set_alert），再被意圖閘門當幻覺 reject。這裡在 reject 前
    先看句子的真實意圖，能救則轉正確 function，救不了才回 None 讓它 reject。
    回傳 (new_func, new_args) 或 None。"""
    import warehouse as _WR
    kw = func_args.get("keyword") or func_args.get("target") or ""

    def _match_solid(cand: str) -> bool:
        """商品比對要有足夠分數才算真商品——「把 炸掉」靠單字「把」中拖把
        score=1，rescue 拿雜訊開庫存卡回無關商品（conv100-r8 幻覺三連發）。"""
        if not cand:
            return False
        m = _WR.match_items(cand)
        return bool(m) and m[0].get("score", 0) >= 3

    # ── r13（探針批 #2）：**不帶商品名的口語泛問**整片被婉拒 ──────────
    #   `what happened yesterday` / `anything come in recently` /
    #   `what moves fastest` / `what's the most popular item` 都是合理提問，
    #   卻因 LLM 判錯 tool（多半判成 search_log）＋現有救援都要求「有商品名」
    #   → 全部落到 rejected。**婉拒是最糟的回應**：訪客問了正常問題卻被打槍。
    #   ⇒ 看句子本身的意圖詞導向正解，不看有沒有商品名。
    #   ⚠️ 只在**閘門已經要 reject** 時才跑（這裡是 reject 前的最後一站），
    #     不會搶走正常路由；且要求句中有明確意圖詞，不是無條件放行。
    if _is_mostly_english(user_text):
        _rt = user_text.lower().replace("'", "").replace("’", "")
        _has_period = bool(_re.search(
            r"\b(?:yesterday|today|this week|last week|this month|last month|"
            r"recently|lately|past week|past month|so far)\b", _rt))
        # ①期間 + 泛問（沒商品名）→ 進出貨紀錄
        if _has_period and _re.search(
                r"\b(?:what|anything|any|whats)\b.*\b(?:happen|happened|"
                r"come in|came in|coming in|went out|go out|move|moved|"
                r"moving|arrive|arrived|new)\b", _rt):
            log.info(f"[gate-rescue] 期間泛問 → query_movement: {user_text!r}")
            return "query_movement", {}
        # ②最高級 + 銷售語 → 熱銷榜（`what moves fastest` / `biggest seller`）
        if _re.search(r"\b(?:fastest|quickest|most popular|best selling|"
                      r"biggest seller|top seller|hottest|sells best|"
                      r"sells better|sold best|moves fastest|sells the most)\b", _rt):
            log.info(f"[gate-rescue] 最高級銷售語 → list_hot_items: {user_text!r}")
            return "list_hot_items", {}

    # Bug1: search_log 缺 RCA 意圖詞、但帶到有效商品名 → 其實是查庫存，降級 query_inventory
    if func_name == "search_log":
        # 幻覺 kw 要接地（r23：「哪些貨在苟延殘喘」LLM 幻覺 藍牙耳機 被
        # rescue 救成無關單品）——kw 與原句無重疊就改用 extractor 重抽
        cand = kw if (_match_solid(kw) and _kw_grounded(kw, user_text)) else _extract_sku_keyword(user_text)
        # r25：extractor 重抽的 cand 也要接地——「剛剛那個再查一次」weak-match
        # 出「啞鈴 5kg 一對」回無關商品（rescue 每個分支的 cand 都要接地）
        if _match_solid(cand) and _kw_grounded(cand, user_text):
            log.info(f"[gate-rescue] search_log 缺RCA詞但有商品名 → query_inventory kw={cand!r}")
            return "query_inventory", {"keyword": cand}

    # run_script 缺腳本意圖詞、但有商品名 → 其實是查庫存（RPI5 conv100-r5：
    # 「想確認一下咖啡濾紙100入的量」LLM 誤投 run_script → 閘門擋 rejected）
    if func_name == "run_script":
        cand = _extract_sku_keyword(user_text)
        # r25：cand 接地（同 search_log 分支，「剛剛那個再查一次」曾 weak-match 啞鈴）
        if _match_solid(cand) and _kw_grounded(cand, user_text):
            # r92：救成庫存查詢前先看是不是**盤點差異**（帳對不上）。
            #   「北倉多了五十個滑鼠」LLM 誤投 run_script{盤點}，救援直接回庫存
            #   數字＝答非所問（user 問的是「為什麼多出來」不是「現在有幾個」）。
            if _has_rca_word(user_text):
                log.info(f"[gate-rescue] run_script 實為盤點差異 → search_log kw={cand!r}")
                return "search_log", {"keyword": cand}
            log.info(f"[gate-rescue] run_script 缺腳本詞但有商品名 → query_inventory kw={cand!r}")
            return "query_inventory", {"keyword": cand}
        # 沒商品名但有進出貨語彙 → 是進出統計（「昨天有出貨嗎」LLM 誤投
        # run_script{出貨} 被閘門拒，conv100-r8）
        if any(w in user_text for w in ("出貨", "進貨", "進出", "入庫", "出庫")):
            _rs_period = ("day_before_yesterday" if ("前天" in user_text and "大前天" not in user_text) else
                          "this_month" if any(w in user_text for w in ("這個月", "本月", "月")) else
                          "yesterday" if any(w in user_text for w in ("昨天", "昨晚")) else
                          "last_week" if any(w in user_text for w in ("上週", "上禮拜")) else
                          "today" if any(w in user_text for w in ("今天", "今日")) else
                          "this_week")
            log.info(f"[gate-rescue] run_script 實為進出查詢 → query_movement period={_rs_period}")
            return "query_movement", {"period": _rs_period, "direction": "both"}

    # query_related_items 缺連帶詞、但有商品名 → 其實是查庫存（RPI5 conv100-r3：
    # 「北中南倉的滑鼠各有幾個」LLM 誤投 related → 閘門擋 rejected，該查庫存）
    if func_name == "query_related_items":
        cand = kw if (_match_solid(kw) and _kw_grounded(kw, user_text)) else _extract_sku_keyword(user_text)
        if _match_solid(cand):
            log.info(f"[gate-rescue] query_related 缺連帶詞但有商品名 → query_inventory kw={cand!r}")
            return "query_inventory", {"keyword": cand}
        # 沒商品名但有進出貨語彙 → 進出統計（r17：「上週北倉進了哪些貨」
        # rewrite 保留原句後 intent_clf 誤判 related(0.95) 被閘門拒）。
        # 期間/倉別從原句抽，跟 run_script 同款 rescue。
        # 「哪些貨」移除（r23：「哪些貨在苟延殘喘」誤入進出統計；
        # 「上週北倉進了哪些貨」由「進了」罩住不受影響）
        if any(w in user_text for w in ("出貨", "進貨", "進出", "入庫", "出庫",
                                         "進了", "出了", "進什麼")):
            _qr_period = ("day_before_yesterday" if ("前天" in user_text and "大前天" not in user_text) else
                          "this_month" if any(w in user_text for w in ("這個月", "本月", "月")) else
                          "yesterday" if any(w in user_text for w in ("昨天", "昨晚")) else
                          "last_week" if any(w in user_text for w in ("上週", "上周", "上禮拜")) else
                          "today" if any(w in user_text for w in ("今天", "今日")) else
                          "this_week")
            _qr_args = {"period": _qr_period,
                        "direction": "in" if any(w in user_text for w in ("進了", "進貨", "進什麼")) else
                                     "out" if any(w in user_text for w in ("出了", "出貨")) else "both"}
            for zh, en in _WH_ZH_MAP.items():
                if zh in user_text and en != "all":
                    _qr_args["warehouse"] = en
                    break
            log.info(f"[gate-rescue] query_related 實為進出查詢 → query_movement {_qr_args}")
            return "query_movement", _qr_args

    # query_movement 缺進出詞、但有商品名 → 查該商品分倉庫存
    # （「藍牙喇叭中倉南倉哪邊多」LLM 誤投 movement 被閘門拒，conv100-r9）
    if func_name == "query_movement":
        cand = kw if (_match_solid(kw) and _kw_grounded(kw, user_text)) else _extract_sku_keyword(user_text)
        if _match_solid(cand):
            log.info(f"[gate-rescue] query_movement 缺進出詞但有商品名 → query_inventory kw={cand!r}")
            return "query_inventory", {"keyword": cand}

    # list_files 缺檔案詞、但有商品名 → 其實是查庫存（r18：「嬰兒用品有哪些」
    # intent_clf 誤判 list_files(0.99) 被閘門拒，該列嬰兒系列商品）
    if func_name == "list_files":
        cand = _extract_sku_keyword(user_text)
        if _match_solid(cand):
            log.info(f"[gate-rescue] list_files 缺檔案詞但有商品名 → query_inventory kw={cand!r}")
            return "query_inventory", {"keyword": cand}

    # Bug2: set_alert 缺意圖詞、但句含「安全庫存」+設定動作詞 → 是改設定，轉 manage_config
    # （C9 校正故意跳過 set_alert，導致這種句子一路走到閘門被 reject。這裡用
    #  跟 C9 相同的 args 組法救回：action/key/warehouse/value 結構化參數。）
    if func_name == "set_alert":
        if any(k in user_text for k in _CONFIG_KEY_WORDS) and \
           any(a in user_text for a in _CONFIG_SET_WORDS):
            _action = ("set" if not (any(w in user_text for w in _CONFIG_READ_CUES)
                       and _extract_config_value(user_text) is None) else "read")
            _key = max((w for w in _CONFIG_KEY_WORDS if w in user_text), key=len, default="安全庫存")
            new_args = {"action": _action, "key": _key}
            for zh, en in _WH_ZH_MAP.items():
                if zh in user_text:
                    new_args["warehouse"] = en
                    break
            if _action == "set":
                _cv = _extract_config_value(user_text)
                if _cv is not None:
                    new_args["value"] = _cv
            _ri_item = _config_item_kw(user_text)
            if _ri_item:
                new_args["item"] = _ri_item
            log.info(f"[gate-rescue] set_alert 實為改安全庫存 → manage_config{{{_action}}}: {user_text!r}")
            return "manage_config", new_args
    return None


# C9 manage_config：改設定（設定項詞 + 動作詞）
_CONFIG_KEY_WORDS = ("安全庫存", "安全存量", "安全水位", "安全線", "前置天數", "補貨前置",
                     "安全水位倍數", "補貨目標天數", "警戒值", "補貨天數", "安全量",
                     # r24：「把三層抽取衛生紙的庫存底線拉高到400」曾退成純庫存查詢
                     "庫存底線", "存量底線",
                     # EN build：⚠️ 這裡的詞會被 C9-key 拿去「以最長者覆寫 key」，
                     #   所以必須是 _resolve_key() 認得的**完整**別名。原本放的
                     #   碎片 "lead" 會把 'lead time' 覆寫成 'lead' → resolve
                     #   不到 → 回 guide 教學文（`set reorder lead time to 7
                     #   days` 就是這樣壞的）。
                     "safety stock", "safety level", "reorder point",
                     "reorder level", "minimum stock", "min stock",
                     "safety threshold", "lead time", "lead days",
                     "reorder lead", "restock lead", "buffer ratio",
                     "safety multiplier", "restock target", "days of cover")

# EN build：中文 config key → 英文標籤。歧義選單的 question/options 要給訪客看，
#   也要能被後端重新解析（選項＝點了會送回來的查詢字串），所以用 _resolve_key()
#   認得的完整英文別名，不能自創詞。
_CFG_KEY_LABEL_EN = {
    "安全庫存": "safety stock", "安全存量": "safety stock",
    "安全水位": "safety level", "安全線": "safety level",
    "安全量": "safety stock", "警戒值": "safety threshold",
    "庫存底線": "minimum stock", "存量底線": "minimum stock",
    "前置天數": "lead time", "補貨前置": "reorder lead",
    "補貨天數": "lead days", "補貨目標天數": "restock target",
    "安全水位倍數": "safety multiplier",
}
_CONFIG_SET_WORDS = ("改成", "設成", "設為", "調成", "調到", "改為", "設定為",
                     "調高", "調低", "提高", "提升", "降低", "降", "加", "減", "+", "改", "設",
                     "調升", "調降", "上修", "下修", "升到", "降到",
                     "訂在", "訂為", "定在", "定為", "縮短成", "縮短到", "縮成",
                     "歸", "拉長", "拉長到", "延長到", "加長到",
                     "上調", "下調", "壓到", "改回",
                     # r24：庫存底線「拉高到400」
                     "拉高", "拉高到", "拉到",
                     # EN build：英文設定動詞（原全中文 → 英文設定句判不出
                     #   action=set，LLM 給 read 就照 read 走，`reduce mouse
                     #   safety stock by 10` 變成查詢）
                     "set ", "change ", "update ", "increase", "decrease",
                     "raise ", "lower ", "reduce ", "bump ", "adjust ",
                     "make it ", "put it at", "bring it")
_CONFIG_READ_WORDS = ("是多少", "設多少", "多少", "現在設", "目前", "查一下", "看一下", "設定值")
# 問句/讀取語氣詞：出現這些且句中抽不到數值 → manage_config 一律當 read。
# C9 / C9b / C18 三處共用（曾經三處各自維護，「設定給我看」只修了 C9b 又被
# 之後執行的 C9 蓋回 set，第12輪抓到）
_CONFIG_READ_CUES = ("多少", "查", "給我看", "看一下", "看看", "是什麼", "是啥",
                     "目前", "現在的", "列給我", "秀給我",
                     # conv100-r11：「幫我看智慧手環的安全庫存設定」曾被當 set 追問值
                     # （單字「看」受「句中抽不到數值才轉 read」保護，不會誤傷 set 句）
                     "幫我看", "看", "的設定")
# C10 run_script：執行白名單腳本
_SCRIPT_INTENT_WORDS = ("跑一次", "執行", "跑個", "跑一下", "幫我跑", "做一次", "做個",
                        "盤點", "匯出", "重產", "重新產生", "重生", "重建", "run", "export", "regenerate")

_WH_ZH_MAP = {"北倉": "north", "北區": "north", "北邊": "north", "北部": "north",
              "中倉": "central", "中區": "central",
              "南倉": "south", "南區": "south", "南邊": "south", "南部": "south",
              "全部": "all", "所有": "all", "三倉": "all"}


# ── Clarification 偵測 ──────────────────────────────────────────────────────
# 所有已知意圖詞（用於判斷「有沒有動作詞」）
_ALL_INTENT_WORDS = (
    "查", "看", "庫存", "查庫存", "還有多少", "有多少", "數量", "剩多少", "剩幾個",
    "存貨", "狀態",   # conv100-r6：「橡膠清潔手套存貨狀態」被類別 clarify 攔
    "賣得", "熱銷",   # conv100-r8：「電子產品賣得如何」被類別 clarify 攔
    "冠軍", "人氣王", "銷售冠軍",   # r24：「電子產品的銷售冠軍是誰」被類別 clarify 攔
    "賣最好", "賣最差", "滯銷", "最好的",   # r27：「食品類賣最好的」被類別 clarify 攔

    "多少", "幾個", "幾件", "多少個", "多少件",   # ← 補「多少」系列
    "進出", "進貨", "出貨", "異動", "移動", "移轉", "紀錄", "流向",
    "進了", "出了", "到貨", "收貨", "入庫", "出庫", "賣掉", "銷貨",
    "補了", "來貨了", "來貨", "賣了", "出貨了", "進了貨",
    "缺貨", "低庫存", "不夠", "快沒", "即將缺貨", "需要補", "補貨",
    "見底", "快斷", "亮紅燈", "開天窗", "警戒", "訂在",
    "熱銷", "賣得好", "最多", "暢銷", "滯銷", "賣不掉", "冷門", "沒人買", "賣不動",
    "動了",
    "到期", "過期", "快過期", "保存期", "效期",
    "比較", "對比", "哪個倉", "哪倉", "哪個好",
    "帳對不上", "對不上", "短收", "差異", "少貨", "為什麼少", "怎麼少",
    "通知", "提醒", "警示", "設定", "設為", "調整",
    "採購單", "下單", "補貨單", "產採購",
    "報告", "報表", "健檢", "體檢",
    "關聯", "連帶", "推薦", "一起買",
    # r18：「買 coffee machine 的人還買什麼」typo-norm 後 kw 可解析，
    # 「還買」缺席讓 _detect_clarify 誤攔成「你想查X的什麼」
    "還買", "還會買", "還需要買",
    "這個月", "上個月", "本月", "跨期", "變化",
    # RCA 意圖詞同步加入，避免被 clarify 攔截
    "對帳", "異常", "帳不對", "誰改", "誰動", "查原因", "追原因",
    "採購對帳", "扣帳", "盤點", "不對", "兜不攏",

    # ── EN build：英文意圖詞（原表全中文 → 英文句 has_intent 恆 False，
    #    被判成「只有商品名沒動作」全部掉進 clarify，即使 clf 已 conf=1.00 判對意圖）──
    # 查庫存
    "stock", "inventory", "how many", "how much", "left", "in stock", "available",
    "availability", "quantity", "count", "on hand", "remaining", "got any", "do we have",
    "check", "show", "list", "look up", "find", "situation", "overview",
    # r12（TTS 基準批抓到）：「倉庫裡有什麼」型的查詢——句中沒有 stock/
    #   inventory 這些詞，靠 `what's in ... warehouse` 表達意圖。
    #   實測 `what's in central warehouse for wireless mouse`（ASR **完全聽對**）
    #   → has_intent=False → 判成「只有商品名沒動作」→ 反問「你想知道滑鼠的什麼」，
    #   但商品和倉庫訪客都給了。用**片語**避免裸 what/in 誤爆。
    "what's in", "whats in", "what is in", "what do we have in",
    "what's at", "whats at", "anything in",
    # 進出貨
    "movement", "movements", "came in", "come in", "shipped", "shipment", "shipments",
    "inbound", "outbound", "received", "goods in", "goods out", "went out", "moved",
    "transactions", "activity",
    # 缺貨
    "running low", "run out", "low stock", "restock", "reorder", "reordering",
    "shortage", "short on", "almost out", "need", "below safety",
    # 熱銷
    "best seller", "best sellers", "top selling", "selling", "sold", "hot items",
    "ranking", "top 10", "slow mover", "slow movers", "dead stock", "not selling",
    # 到期
    "expiry", "expiring", "expire", "expires", "shelf life", "about to expire",
    # 比較
    "compare", "versus", " vs ", "which warehouse", "higher", "more stock",
    # RCA
    "why is", "why did", "who moved", "doesn't match", "dont match", "don't match",
    "discrepancy", "mismatch", "reconciliation", "audit", "investigate", "trace",
    "missing", "wrong", "off", "strange", "shortfall", "numbers",
    # 連帶
    "bought with", "goes with", "sells with", "related", "bundle", "cross-sell",
    "pairs with", "also buy", "also bought",
    # 警示/設定
    "alert", "notify", "warn", "remind", "set ", "change ", "update ", "threshold",
    "safety stock", "reorder point",
    # 報表/採購單/腳本
    "report", "export", "purchase order", " po ", "audit", "stocktake", "stock count",
    # 寫入（進出貨/調撥/退貨）——沒收的話 _detect_clarify 會判成「只有商品名
    #   沒動作」把寫入句攔成 clarify，根本進不到 C13b 開卡（守衛 mv 抓到）
    "add ", "added", "put ", "received", "receive", "came in", "arrived",
    "restock ", "restocked", "delivered", "supplier sent", "got ",
    "shipped", "ship ", "sent out", "sold", "sell ", "dispatched",
    "picked up", "took out", "take ", "removed", "scrapped", "damaged",
    "returned", "return of", "sent back",
    "transfer", "move ", "moved to", "send ",
)

# ── Query Rewriting ───────────────────────────────────────────────────────────
# 將使用者的口語/模糊輸入改寫成 LLM 訓練時見過的標準句型。
# 只做字串正規化，不改變語義；改寫後的句子才送進 LLM。
import re as _re

_REWRITE_RULES: list[tuple] = [
    # ⚠️ 排程「設定」不做 rewrite——曾有兩條規則把「每週一匯出進出報表」
    # 「每天早上八點盤點」全改寫成同一句死字串「每天定時執行盤點」，時間/頻率/
    # 腳本資訊全毀（每週匯出變每天盤點、八點變預設 09:00 還誤判重複排程）。
    # 路由安全交給 Pre-C-Sched 攔截（每天/每週 + 盤點/匯出 → set_schedule
    # raw_text=原句），時間頻率由 tools_v2._parse_schedule_intent 解析原句。
    # 教訓同 set_alert：rewrite 成固定句 = 資訊銷毀，只適合「查詢類」意圖。

    # ── 排程查詢 ──
    (_re.compile(r"(看|查|查看|顯示|列出|有哪些|目前|現在).*(排程|定時任務)"),
                                                                    "查看排程"),
    (_re.compile(r"排程(列表|清單|狀態|有哪些)"),                   "查看排程"),
    (_re.compile(r"^(查排程|看排程|目前排程|查看排程)$"),           "查看排程"),

    # ── 警示設定：不再改寫成固定句「新增庫存警示規則」──
    # 之前這 4 條會把「藍牙耳機缺貨就通知我」這類句子改寫成固定字串，結果
    # intent_clf 對這句改寫後的固定句反而誤判成 list_low_stock（信心 0.9997），
    # 蓋過了 intent_clf 對「原句」本身的正確判斷（set_alert，信心 1.0）。
    # 原句讓 intent_clf 直接判斷，準確率明顯更好，不需要標準化改寫。

    # ── 警示查詢 ──
    (_re.compile(r"(看|查|查看|顯示|有哪些).*(警示|告警|alert)規則"),
                                                                    "查看警示規則"),
    (_re.compile(r"(目前|現在).*(警示|告警)"),                      "查看警示規則"),

    # ── r79：最操倉/待辦/處理完 口語 ──
    (_re.compile(r"(哪個?倉|北中南|三個?倉)[^。]{0,6}最[操忙累]"),   "各倉週轉率比較"),
    (_re.compile(r"(還有什麼|有什麼)要處理|要處理的事?"),            "庫存警示"),
    (_re.compile(r"^都?(處理|弄|搞定|補)[完好]了?嗎[?？]?$"),        "哪些商品缺貨警示"),

    # ── r78：對帳/盤點排程/改回 口語 ──
    (_re.compile(r"^(對帳|帳目)(有沒有|有無)(問題|異常|對不上)?[?？]*$"), "採購對帳異常"),
    (_re.compile(r"(盤點|匯出|報告)[^。]{0,4}(什麼時候|何時|幾點)(跑|執行|會跑)?"),
                                                                    "查看排程"),

    # ── r77：進出報表/昨天出貨追問 口語 ──
    (_re.compile(r"(進出|出入).{0,4}(出個|產個|拉個|做個|給個|出張)(報表|報告)"),
                                                                    "匯出進出記錄"),
    # r84/r85：「匯出給我」「順便匯出今天的」——demo 只有匯出進出記錄一個匯出腳本
    (_re.compile(r"^(?:順便|幫我|請|那就|然後)?\s*匯出"
                 r"(給我|一下|來|吧|資料|檔案|報表|今天的?|一份)?$"),
                                                                    "匯出進出記錄"),
    # 「昨天那批出貨有成功嗎」改走 WS 直答（rewrite 固定句在 RPI5 期間解析
    # 走鐘＝平台分歧，r77w 抓到）

    # ── r76：生意如何/異常巡查/被改過 口語（曾 rejected/guide/醜 clarify）──
    (_re.compile(r"^(這週|本週|最近|今天)?的?生意(如何|怎樣|怎麼樣|好嗎|好不好)[?？]?$"),
                                                                    "本週熱銷排行"),
    (_re.compile(r"(有什麼|有沒有|哪裡有|最近.{0,3})異常"),          "採購對帳異常"),
    # 帶 \1 保留倉別片段——固定句版本會被 r20 實體守衛（中倉）跳過
    (_re.compile(r"^(.{0,6}?)(有東西|有什麼|東西)?被人?[改動]過(嗎)?$"), "\\1 誰改的"),

    # ── RCA 異常追查（優先於庫存查詢，避免被抓成 inventory）──
    # 帶商品名的句型：保留商品名 → "XXX 帳對不上"，讓 C17 能抽出 keyword
    (_re.compile(r"^(.+?)(的)?(帳不對|帳對不上|對不上帳|對不起來|兜不攏)$"),
                                                                    "\\1 帳對不上"),
    (_re.compile(r"^(.+?)(庫存|數量|進貨)?(少了|短少|短收).*(查|追|找|原因|為什麼)"),
                                                                    "\\1 帳對不上"),
    # 無商品名的通用句型
    (_re.compile(r"^(庫存差異|數量差異|進貨差異)(追查|調查|原因)?$"), "庫存帳對不上"),
    (_re.compile(r"^(帳不對|異常|對不上|兜不攏).*(查|追|找原因|原因)$"),
                                                                    "庫存帳對不上"),
    (_re.compile(r"^(誰改|誰動|是誰).*(庫存|帳|數量)"),            "庫存帳對不上"),

    # ── 執行腳本（明確動詞 + 品名）──
    (_re.compile(r"(幫我跑|幫我執行|請執行|請跑|執行|跑).*(盤點|月底盤點)"),
                                                                    "執行腳本 月底盤點"),
    (_re.compile(r"(幫我跑|幫我執行|請執行|請跑|執行|跑|匯出|產出).*(進出|移動).*(記錄|匯出|CSV|表)"),
                                                                    "執行腳本 進出記錄匯出"),
    (_re.compile(r"(幫我跑|幫我執行|請執行|請跑|執行|跑|產|產出|生成|匯出).*(體檢|健診)"),
                                                                    "執行腳本 庫存體檢報告"),
    (_re.compile(r"(幫我跑|幫我執行|請執行|請跑|執行|跑).*(腳本)"),
                                                                    "執行腳本"),
    (_re.compile(r"^跑盤點$"),                                      "執行腳本 月底盤點"),
    (_re.compile(r"^月底盤點$"),                                    "執行腳本 月底盤點"),
    (_re.compile(r"^(產|生成).*(體檢|健診)報告"),                  "執行腳本 庫存體檢報告"),
    # 「匯出進出記錄」不帶動詞前綴，獨立規則（注意不能被進出記錄查詢搶走）
    (_re.compile(r"^(匯出|產出)(進出|移動)記錄"),                   "執行腳本 進出記錄匯出"),

    # ── r55 收官批：低庫存追問/盤點結果/月底結算/分倉設定 ──
    # 「最緊急的是哪個」（剛看完缺貨警示的追問，也可獨立問）→ 警示列表開頭就是答案
    # r56 收窄成全句比對：「最急的那批放哪個倉」是到期追問，曾被這條搶走
    # r66 加同義：最慘/最糟/最嚴重
    (_re.compile(r"^最(緊?急|慘|糟|嚴重)的?(是)?(哪個|哪一個|哪項|誰)[?？。!！]*$"),
                                                                    "哪些商品缺貨警示"),
    # r72：「都達標了嗎」＝安全庫存達標總檢（警示清單就是答案）
    (_re.compile(r"^(都|全部)?達標(了)?嗎?[?？]*$"),                "哪些商品缺貨警示"),
    # r73：「出貨最多的是哪個」曾回庫存排行；「週轉最快的倉」曾回庫存排行
    (_re.compile(r"^(哪個商品|什麼|誰)?(出貨|賣出|出)最多的?(是哪個|是誰)?[?？]*$"),
                                                                    "熱銷商品排行"),
    (_re.compile(r"週轉(率)?最(快|高|慢|低)的?(倉|倉庫)?(是哪個|是誰)?"),
                                                                    "各倉週轉率比較"),
    # r66：「篩選一下30天內到期的」——剝掉篩選前綴讓到期查詢自然接
    (_re.compile(r"^(?:幫我)?篩選?一?下?(.{4,})$"),                 "\\1"),
    # r67：「倒數第一名呢」＝滯銷名次
    (_re.compile(r"^倒數第([一二三四五六七八九十\d]+)名?(呢|剩多少|是什麼)?[?？]*$"),
                                                                    "滯銷第\\1名剩多少"),
    # r70：冠亞季軍口語（整句開頭才改，避免傷長句守衛「…銷售冠軍是誰」）
    (_re.compile(r"^(?:熱銷)?冠軍(.{0,6})$"),                       "熱銷第一名\\1"),
    (_re.compile(r"^亞軍(.{0,6})$"),                                "第二名\\1"),
    (_re.compile(r"^季軍(.{0,6})$"),                                "第三名\\1"),
    # r56：「最少人買的是什麼」曾被守門員拒——滯銷排行的口語講法
    (_re.compile(r"(最少人買|沒什麼人買|沒人買|買最少|賣最少)"),    "滯銷品有哪些"),
    # 「月底結算」＝展場訪客對月底盤點的另一種講法
    (_re.compile(r"^(月底)?結算$"),                                 "執行腳本 月底盤點"),
    # 「上次盤點結果在哪」曾答非所問回熱銷榜 → 列紀錄檔讓訪客看到檔案在哪
    (_re.compile(r"盤點.*(結果|報告|檔|紀錄|記錄).*(在哪|哪裡|哪邊|去哪|找)"),
                                                                    "有哪些紀錄檔"),
    # 「北倉的設定」（config_read 後的分倉追問）曾被守門員拒；r58 放寬尾綴（給我看）
    (_re.compile(r"^([北中南])(區)?倉的?(設定|安全庫存)(給我看|看一下|是多少|多少)?$"),
                                                                    "\\1倉安全庫存是多少"),

    # ── 倉庫比較（優先於庫存查詢，避免「北中南倉差多少」被吃成 inventory）──
    (_re.compile(r"(北|中|南|東|西).*(倉|倉庫).*(比|差|對比|PK|差多少)"),
                                                                    "比較各倉庫庫存"),
    (_re.compile(r"(各倉|各個倉庫|三個倉|多個倉|多倉).*(比|差|差異|比較)"),
                                                                    "比較各倉庫庫存"),
    (_re.compile(r"(倉庫|倉).*(比較|對比|差多少)"),                 "比較各倉庫庫存"),
    (_re.compile(r"比較.*(倉庫|倉|北|中|南)"),                     "比較各倉庫庫存"),
    # r57：「三個倉哪個最滿」曾回庫存排行 TOP10（答非所問）
    # r60 補：「北中南倉哪個最強」「誰墊底」倉別最上級也走比較
    (_re.compile(r"(哪個|哪一個|誰)(倉|倉庫)?最(滿|空)|倉.*(最滿|最空|貨最多|東西最多"
                 r"|最強|最弱|墊底)"),                              "比較各倉庫庫存"),
    # r60：「家電廚具類有哪些」曾被拒——類別清單口語
    (_re.compile(r"^(電子|家電|家電廚具|食品|食品飲料|日用|日用品|服飾|運動|運動用品)"
                 r"(產品|用品|廚具|飲料)?類?(有哪些|有什麼|清單)$"), "\\1類庫存"),
    # r62：「這幾天的進出」曾被 ctx 黏上舊商品（近似本週、全店）
    (_re.compile(r"^(這幾天|最近幾天|近幾天)的?(進出|異動)(紀錄|記錄|統計)?$"),
                                                                    "本週進出統計"),
    # r63：「今天進貨的東西有哪些」曾回熱銷榜（答非所問）
    (_re.compile(r"^(今天|昨天|前天)進貨?的?(東西|商品|貨)?(有哪些|有什麼|列一下|幫我列).*$"),
                                                                    "\\1進了什麼貨"),
    # r63：「南倉的濕紙巾昨天有異動嗎」曾被庫存 fast-path 搶（保留商品/倉/時間）
    (_re.compile(r"^(.{2,14}?)(今天|昨天|前天)有?異動嗎?$"),        "\\1\\2進出紀錄"),
    # r64：「最會賣的飲料是哪個」曾 clarify 你想查什麼——類別排行口語
    (_re.compile(r"^最會?賣(?:得最好)?的(飲料|食品|電子|家電|廚具|日用品?|服飾|運動用品)"
                 r"類?(?:是哪個|是什麼)?$"),                        "\\1類熱銷排行"),
    # r64：審計/設定異動紀錄 → 紀錄檔清單（曾回商品庫存/熱銷榜）
    (_re.compile(r"(審計|audit).{0,3}(紀錄|記錄|log)|(今天)?(改過什麼|誰改過)(設定)?"),
                                                                    "有哪些紀錄檔"),
    # r65：行內糾錯句（「出10個 打錯 是出20個」）——取更正後的量（商品/倉由 ctx 補）
    (_re.compile(r"^(?:[進出調]\s*\d+\s*[個件]?)\s*(?:打錯|說錯|講錯|不對)\s*是?"
                 r"([進出調])\s*(\d+)\s*[個件]?$"),                 "\\1\\2個"),
    (_re.compile(r"^([進出調])\s*\d+\s*[個件]?\s*(?:打錯|說錯|講錯|不對)\s*是?"
                 r"(\d+)\s*[個件]?$"),                              "\\1\\2個"),
    # r62：「它上週賣幾個」——賣幾個是銷量(出貨)不是庫存（保留商品與期間）
    (_re.compile(r"^(.{2,10}?)(上週|本週|這週|昨天|今天|本月)賣了?幾[個件]?$"),
                                                                    "\\1\\2出貨多少"),

    # ── 缺貨 / 低庫存 ──
    (_re.compile(r"(快沒貨|快沒了|即將缺貨|快缺貨)"),              "哪些商品缺貨警示"),
    (_re.compile(r"(庫存告急|告急)"),                               "哪些商品缺貨警示"),
    (_re.compile(r"(安全庫存).*(不足|告急|低)"),                    "哪些商品缺貨警示"),
    (_re.compile(r"(缺貨警示|哪些缺貨|哪些不足)"),                  "哪些商品缺貨警示"),
    (_re.compile(r"(哪些|什麼).*(快沒|不夠|庫存低)"),              "哪些商品缺貨警示"),

    # ── 到期 ──
    (_re.compile(r"(快到期|快過期|即將到期|即將過期)"),              "哪些商品即將到期"),
    (_re.compile(r"(本月|這個月|近期).*(到期|過期)"),                "哪些商品即將到期"),
    (_re.compile(r"(到期|過期).*(商品|有哪些|有什麼)"),             "哪些商品即將到期"),

    # ── 熱銷 ──
    (_re.compile(r"(最近|近期|這週|本月).*(賣最好|最熱銷|熱賣)"),   "熱銷商品排行"),
    (_re.compile(r"(熱銷|暢銷|賣得好|賣得最好)"),                   "熱銷商品排行"),
    (_re.compile(r"(什麼|哪些).*(賣最好|最受歡迎|最多人買)"),       "熱銷商品排行"),
    (_re.compile(r"熱銷排行"),                                       "熱銷商品排行"),

    # ── 相關商品 ──
    (_re.compile(r".+相關.*(商品|產品|品項)"),                      "相關商品查詢"),
    (_re.compile(r"(有什麼|有哪些).*(相關|類似).*(商品|產品)"),     "相關商品查詢"),
    (_re.compile(r"(跟|和|與).+?(類似|相關|同類).*(有哪些|有什麼|商品|產品)?"),
                                                                    "相關商品查詢"),
    (_re.compile(r"相關商品|類似商品|同類商品"),                     "相關商品查詢"),

    # ── 進出記錄 / 移動（只匹配無商品名的純動作查詢）──
    (_re.compile(r"^(進貨|入庫|出貨|出庫)(記錄|多少|幾|狀況)?$"),   "查詢進出記錄"),
    (_re.compile(r"^(最近|近期|上週|本週|這週|本月|最近\d+天)(進出|出貨|進貨|移動)(記錄|狀況|多少)?$"),
                                                                    "\\1\\2\\3"),
    (_re.compile(r"(進出記錄|移動記錄|庫存移動)"),                   "查詢進出記錄"),
    (_re.compile(r"上週.*(進了|入了|來了)"),                        "查詢進出記錄"),

    # ── 庫存查詢（只改寫真正的裸句，帶商品/倉庫名的原樣送 LLM）──
    (_re.compile(r"^(查一下|看一下|幫我查|幫我看)庫存$"),           "查詢庫存"),
    (_re.compile(r"^幫.查$"), "查詢"),  # 「幫偶查」「幫我查」→ clarify
    (_re.compile(r"^查庫存$"),                                      "查詢所有庫存"),
        # ── 英文常用句型 ──
    (_re.compile(r"(show|list|get)\s+(today|this week|this month)\s+(inbound|outbound)", _re.IGNORECASE),
                                                                    "查詢進出記錄"),
    (_re.compile(r"low stock alert", _re.IGNORECASE),                 "庫存警示"),
    (_re.compile(r"what(?:'?s?| is) bought with (.+)", _re.IGNORECASE),
                                                                    "買\\1的人還會買什麼"),
    (_re.compile(r"(what|show|list|get).*(bought|related).*", _re.IGNORECASE),
                                                                    "相關商品查詢"),
    (_re.compile(r"(?:how much |how many |show |list |get )(.+)", _re.IGNORECASE),
                                                                    "\\1 庫存"),

# 含「有多少/剩多少」但沒有名詞（< 7 字）才改寫；長句含商品名讓 LLM 抽
    (_re.compile(r"^現在還有多少貨$"),                              "查詢庫存"),
]


def _has_product_or_wh_keyword(text: str) -> bool:
    """判斷文字是否含商品名或倉庫名，如果有就不該被通用 inventory rewrite 改寫。"""
    wh_words = ("北區", "中區", "南區", "北倉", "中倉", "南倉", "北區倉", "中區倉", "南區倉")
    return len(text) > 6 or any(w in text for w in wh_words)


# 倉管高頻簡體字→繁體（陸港訪客，RPI5 conv100-r4：「调货30个耳机到南仓」
# 的簡體讓 C13a 調貨/倉名偵測全失效 → 誤判 config）。只轉語境明確的字避免誤傷。
_S2T = str.maketrans({
    "调": "調", "货": "貨", "仓": "倉", "机": "機", "库": "庫", "存": "存",
    "进": "進", "个": "個", "补": "補", "销": "銷", "转": "轉", "价": "價",
    "报": "報", "总": "總", "查": "查", "过": "過", "还": "還", "东": "東",
    "买": "買", "卖": "賣", "会": "會", "内": "內", "两": "兩", "几": "幾",
})


# 功能描述 → 商品名（RPI5 實測：「那個煮咖啡的庫存多少」fuzzy 配到咖啡豆）。
# 「手沖咖啡壺組」含「沖咖啡」→ lookbehind 排除「手沖」、且要求「的」收尾，
# 避免誤改真商品名。之後有同型抱怨（描述功能不講品名）就往這張表加。
# 原則：只收無歧義映射（「充電的」=行動電源/快充線兩可、「聽音樂的」=
# 耳機/喇叭兩可 → 不收，留給 fuzzy/clarify）；替換字串取全名的子字串，
# 保證下游 substring/fuzzy 一定配得到正確全名。
# _DA_TAIL 放寬（RPI5 實測「煮個咖啡的機器」「用來拖地板的」漏出直達）：
# 動詞和「的」之間允許 0-3 個受詞字（地→地板、湯→熱湯、衣→衣物），
# 動詞前允許「用來/拿來/幫忙」等前綴（_DA_HEAD）。「的」可省（「煮咖啡機」）。
_DA_HEAD = r"(?:用來|拿來|幫忙)?"
_DA_TAIL = r"[一-鿿]{0,3}(?:的)?(?:機器|那台|那個|東西|用品|器具)?"
_DESCRIPTOR_ALIASES = (
    # ── OOV-100 優先條目（2026-07-11，長描述先比對防被短 pattern 劫走）──
    # 「照亮帳篷的燈」曾被「帳篷」規則搶（該露營燈）
    (_re.compile(r"照亮[一-鿿]{0,6}的?燈|營地的?燈"), "露營燈"),
    # 「那個充手機的寶還有嗎」曾被「手機→防摔殼」搶（該行動電源）
    (_re.compile(r"充(?:手機)?的?寶(?!特)|充電的寶"), "行動電源"),
    # 「防止蚊子咬的」曾 rejected
    (_re.compile(r"防止?蚊子?[咬叮]?"), "防蚊液"),
    # 「喝了會補充電解質的」曾 rejected
    (_re.compile(r"電解質"), "運動飲"),
    # 「睡外面用的帳」「烤肉煮飯的鍋具」「野餐坐的椅子」「裝咖啡渣的紙」
    (_re.compile(r"睡外面[一-鿿]{0,3}的?帳|睡野外"), "帳篷"),
    (_re.compile(r"(?:烤肉|野餐)[一-鿿]{0,3}的?鍋"), "野炊鍋具"),
    (_re.compile(r"(?:野餐|營地)坐的"), "露營椅"),
    (_re.compile(r"裝?咖啡渣的?紙?"), "濾紙"),
    # ── 家電廚具 ──
    (_re.compile(_DA_HEAD + r"(?<!手)(?:[煮泡沖磨](?:個|杯|壺)?咖啡|咖啡機|拿鐵|美式咖啡)" + _DA_TAIL), "咖啡機"),
    (_re.compile(_DA_HEAD + r"(?:刷牙|潔牙|清牙|牙齒|電動的?牙刷|音波牙刷)" + _DA_TAIL), "電動牙刷"),
    (_re.compile(_DA_HEAD + r"(?:[燙熨]衣|除皺|燙平|熨平|燙襯衫|去皺)" + _DA_TAIL), "電熨斗"),
    (_re.compile(_DA_HEAD + r"(?:[打榨](?:果|蔬果)?汁|打果昔|打冰沙|榨柳丁|打奶昔|攪拌)" + _DA_TAIL), "果汁機"),
    (_re.compile(_DA_HEAD + r"(?:拖地|除塵|擦地|掃地|清地板|拖把)" + _DA_TAIL), "拖把"),
    # 「煮飯」移除（r16：「煮飯的電鍋」曾配到不沾鍋——煮飯聯想是電鍋不是煎鍋）
    (_re.compile(_DA_HEAD + r"(?:炒菜|煎[東蛋]西?|煎煮|下廚|炒飯|不沾鍋|平底鍋|做菜)" + _DA_TAIL), "不沾鍋"),
    (_re.compile(_DA_HEAD + r"(?:[悶燜](?:熱)?[湯粥]|保溫湯|[裝帶煮](?:熱)?湯|保溫罐|燜燒杯|保溫便當|悶燒罐)" + _DA_TAIL), "悶燒罐"),
    (_re.compile(_DA_HEAD + r"(?:裝剩菜|保鮮|裝便當|收納食物|裝菜|冰箱收納|保鮮盒|密封盒)" + _DA_TAIL), "保鮮盒"),
    (_re.compile(_DA_HEAD + r"(?:野炊|露營煮飯?|戶外煮|露營炊具|野餐鍋|露營鍋)" + _DA_TAIL), "野炊鍋具"),
    (_re.compile(_DA_HEAD + r"(?:手沖咖啡|手沖壺|沖泡咖啡|濾泡咖啡|手沖組|咖啡壺)" + _DA_TAIL), "手沖咖啡壺組"),
    # ── 電子產品 ──
    (_re.compile(_DA_HEAD + r"[塞掛戴]耳朵" + _DA_TAIL), "無線藍牙耳機"),
    (_re.compile(_DA_HEAD + r"(?:聽音樂戴|運動戴|通勤戴|無線|藍牙|入耳|聽歌)耳機" + _DA_TAIL), "無線藍牙耳機"),
    (_re.compile(_DA_HEAD + r"(?:出門|隨身|行動|外出|旅行|沒電|緊急)充電" + _DA_TAIL), "行動電源"),
    (_re.compile(_DA_HEAD + r"(?:行動電源|尿袋|補電|充電寶|行動電|電源)" + _DA_TAIL), "行動電源"),
    (_re.compile(_DA_HEAD + r"(?:充電|傳輸|接手機|接電腦)(?:用)?的線"), "快充線"),
    (_re.compile(_DA_HEAD + r"(?:充電線|傳輸線|快充線|typec線|type-c線|c\s?to\s?c)" + _DA_TAIL), "快充線"),
    (_re.compile(_DA_HEAD + r"(?:放音樂|外放|喇叭|音響|播歌|放歌|音箱)" + _DA_TAIL), "藍牙喇叭"),
    (_re.compile(_DA_HEAD + r"(?:計步|量心跳|測心率|戴手[上腕]量?|運動手錶|智慧手錶|手錶|運動手環|健康手環|智能手環)" + _DA_TAIL), "智慧手環"),
    (_re.compile(_DA_HEAD + r"(?:包手機|保護手機|手機殼|手機套|防摔|保護殼|手機保護)" + _DA_TAIL), "防摔殼"),
    (_re.compile(_DA_HEAD + r"裝(?:筆電|電腦|平板|notebook)" + _DA_TAIL), "筆電包"),
    (_re.compile(_DA_HEAD + r"(?:筆電包|電腦包|筆電袋|裝電腦的包)" + _DA_TAIL), "筆電包"),
    (_re.compile(_DA_HEAD + r"(?:打字|打電腦|敲鍵盤|鍵盤|機械鍵盤|青軸)" + _DA_TAIL), "鍵盤"),
    (_re.compile(_DA_HEAD + r"(?:滑鼠|點滑鼠|移游標|無線滑鼠|游標|點來點去)" + _DA_TAIL), "無線滑鼠"),
    # r60：吹風(?!機)——「有賣吹風機嗎」曾被吹風描述搶成 USB 風扇（吹風機是別的商品，
    # 店裡沒有，該走查無）
    (_re.compile(_DA_HEAD + r"(?:吹風(?!機)|吹涼|消暑|散熱|電風扇|小風扇|桌扇|usb風扇|風扇|吹電風)" + _DA_TAIL), "風扇"),
    # ── 食品飲料 ──
    (_re.compile(r"(?:有氣的水|氣泡的水|帶氣的水|碳酸水|汽水|蘇打水|氣泡水|開特力那種)"), "氣泡水"),
    (_re.compile(r"(?:會醉的|有酒精的|喝的酒|啤酒|精釀|生啤|麥酒|喝的beer)"), "精釀啤酒"),
    (_re.compile(_DA_HEAD + r"(?:健身喝|練完喝|補蛋白|高蛋白|乳清|蛋白飲|增肌喝|練肌肉喝)" + _DA_TAIL), "乳清"),
    (_re.compile(_DA_HEAD + r"(?:運動完?喝|流汗喝|練完喝|補電解質|運動飲料|運動飲|補水|寶礦力那種|舒跑那種)" + _DA_TAIL), "運動飲"),
    (_re.compile(r"(?:巧克力(?:粉|飲|牛奶)?|可可|熱可可|沖泡可可|巧克力沖泡)"), "熱可可粉"),
    (_re.compile(r"(?:掛耳(?:咖啡|包)|濾掛|即溶咖啡|沖泡咖啡包|隨身咖啡)"), "濾掛咖啡"),
    (_re.compile(r"(?:蘇打餅乾|全麥餅|蘇打餅|餅乾|鹹餅乾|全麥蘇打)"), "蘇打餅"),
    # 咖啡豆：與咖啡機分流——「豆」「磨豆」明確指豆子，不撞煮咖啡的機器
    # 「研磨咖啡」移除——含「咖啡」會被咖啡機 pattern 先吃；用明確的豆相關詞
    (_re.compile(r"(?:咖啡豆|磨豆|黑咖啡豆|烘豆|豆子|義式豆|研磨的豆)"), "經典黑咖啡豆"),
    (_re.compile(r"(?:堅果|核果|綜合果仁|下酒果|杏仁腰果|果仁|綜合堅果)"), "綜合堅果罐"),
    (_re.compile(r"(?:檸檬茶|蜂蜜茶|蜂蜜檸檬|檸檬水|蜂蜜飲|甜茶)"), "蜂蜜檸檬茶"),
    (_re.compile(r"(?:咖啡濾紙|濾紙|濾杯紙|手沖濾紙|扇形濾紙)"), "咖啡濾紙"),
    # ── 日用品 ──
    (_re.compile(_DA_HEAD + r"(?:洗衣(?:服)?|洗衣精|洗衣粉|洗衣液|洗衣服的洗劑)" + _DA_TAIL), "洗衣精"),
    (_re.compile(_DA_HEAD + r"(?:洗澡|洗身體|沐浴|沐浴乳|洗澡乳|身體乳)" + _DA_TAIL), "沐浴乳"),
    (_re.compile(_DA_HEAD + r"(?:防蚊|驅蚊|防蚊蟲|防蚊液|擦的防蚊|噴的防蚊)" + _DA_TAIL), "防蚊液"),
    # 「防蚊插座」移除——「防蚊」會被防蚊液 pattern 先吃；蚊香/電蚊香已明確
    (_re.compile(r"(?:插電的?蚊香|電蚊香|蚊香液|液體蚊香|補充瓶蚊香)(?:液)?"), "蚊香液"),
    # r24：「給寶寶擦屁股的」是濕紙巾，長描述在前防被下一條「擦屁股→衛生紙」劫走
    (_re.compile(r"(?:給)?(?:寶寶|嬰兒|小孩|小朋友|北鼻)[一-鿿]{0,3}擦(?:屁股|屁屁)?"), "嬰兒濕紙巾"),
    (_re.compile(_DA_HEAD + r"(?:擦屁股|衛生紙|抽取衛生紙|廁所紙|捲筒紙|面紙)" + _DA_TAIL), "衛生紙"),
    (_re.compile(_DA_HEAD + r"(?:包屁股|包寶寶|寶寶包|給寶寶包|尿布|紙尿褲|尿褲|嬰兒尿布)" + _DA_TAIL), "紙尿布"),
    # 濕紙巾：與紙尿布分流（都是嬰兒用品，但「濕紙巾/擦手擦嘴」明確）
    (_re.compile(_DA_HEAD + r"(?:濕紙巾|擦手擦嘴|擦寶寶|擦屁屁|嬰兒濕巾|濕巾)" + _DA_TAIL), "嬰兒濕紙巾"),
    (_re.compile(_DA_HEAD + r"(?:裝垃圾|垃圾袋|丟垃圾|裝垃圾的袋)" + _DA_TAIL), "垃圾袋"),
    # 「清潔手套」的「清潔」會被 RPI5 LLM 當類別詞跑去 clarify → 用全名
    # 「洗碗精/洗碗機」不是手套（r16：「有賣洗碗精嗎」曾回手套庫存）
    (_re.compile(_DA_HEAD + r"(?:洗碗(?!精|機)|做家事|家事|清潔手套|橡膠手套|洗碗手套)戴?" + _DA_TAIL), "橡膠清潔手套"),
    # ── 服飾 ──
    # 「防曬」後不接袖套/臂套（那是壓縮臂套）；遮陽/帽類明確
    (_re.compile(_DA_HEAD + r"(?:遮太陽|遮陽|防曬(?!袖套|臂套)|防太陽|遮陽帽|太陽帽|漁夫帽|防曬帽|擋太陽)" + _DA_TAIL), "遮陽帽"),
    (_re.compile(_DA_HEAD + r"(?:冬天戴|保暖帽|毛帽|針織帽|保暖的帽|冬天的帽)" + _DA_TAIL), "毛帽"),
    # 「外套」明講時優先羽絨外套（防「冬天保暖穿的外套」被 fuzzy 配到毛帽）
    (_re.compile(r"(?:冬天|保暖).{0,4}外套|外套|羽絨|禦寒穿|保暖穿的|冬天的衣服"), "羽絨外套"),
    (_re.compile(_DA_HEAD + r"冬天穿" + _DA_TAIL), "羽絨外套"),
    (_re.compile(_DA_HEAD + r"(?:跑步|慢跑)[穿用]" + _DA_TAIL), "慢跑鞋"),
    (_re.compile(_DA_HEAD + r"(?:慢跑鞋|跑鞋|運動鞋|球鞋)" + _DA_TAIL), "慢跑鞋"),
    # 服飾補齊（明確講法，各品項不衝突）
    (_re.compile(r"(?:素T|棉T|短袖|T恤|素色t|純棉上衣|白t)"), "純棉素T"),
    (_re.compile(r"(?:牛仔褲|長褲|牛仔長褲|丹寧褲|褲子)"), "牛仔長褲"),
    (_re.compile(r"(?:排汗衣|排汗衫|機能衣|速乾衣|運動上衣|吸濕排汗)"), "機能排汗衣"),
    (_re.compile(r"(?:運動內衣|運動胸衣|bra|運動bra|健身內衣)"), "彈性運動內衣"),
    (_re.compile(r"(?:壓縮臂套|臂套|袖套|手臂套|防曬袖套|防曬臂套)"), "運動壓縮臂套"),
    (_re.compile(r"(?:嬰兒連身衣|寶寶衣|包屁衣|連身衣|寶寶連身|嬰兒服)"), "嬰兒連身衣"),
    # ── 運動用品 ──
    (_re.compile(_DA_HEAD + r"(?:[做練]瑜[珈伽]|拉筋|瑜[珈伽]墊|運動墊|地墊|伸展)" + _DA_TAIL), "瑜珈墊"),
    (_re.compile(_DA_HEAD + r"(?:裝水|喝水|水壺|水瓶|登山水壺|保溫水壺|運動水壺)" + _DA_TAIL), "水壺"),
    (_re.compile(_DA_HEAD + r"(?:舉重|重訓|練肌肉|練二頭肌?|啞鈴|舉的|練手臂|健身舉)" + _DA_TAIL), "啞鈴"),
    (_re.compile(_DA_HEAD + r"(?:拉力環|健身環|彈力環|阻力環|運動環)" + _DA_TAIL), "健身環"),
    (_re.compile(_DA_HEAD + r"(?:擦汗|運動毛巾|運動巾|健身毛巾|長毛巾|吸汗巾)" + _DA_TAIL), "運動毛巾"),
    (_re.compile(_DA_HEAD + r"(?:露營[睡搭]|帳篷|露營帳|野營帳|搭帳|過夜帳)" + _DA_TAIL), "帳篷"),
    (_re.compile(_DA_HEAD + r"(?:露營坐|露營椅|折疊椅|摺疊椅|野營椅|戶外椅)" + _DA_TAIL), "露營椅"),
    # 露營馬克杯：與悶燒罐/水壺分流（「馬克杯/露營杯/喝咖啡的杯」明確）
    (_re.compile(_DA_HEAD + r"(?:馬克杯|露營杯|鋁杯|喝咖啡的?杯|不鏽鋼杯|露營馬克)" + _DA_TAIL), "露營馬克杯"),
    (_re.compile(_DA_HEAD + r"(?:照明|露營燈|營燈|led燈|手電筒|夜燈|戶外燈)" + _DA_TAIL), "露營燈"),
    (_re.compile(_DA_HEAD + r"(?:保暖襪|羊毛襪|厚襪|毛襪|長襪|冬天襪)" + _DA_TAIL), "羊毛保暖襪"),
)


# 同音/形近錯字正規化（r17）：fuzzy 對「咖啡雞」在咖啡豆/咖啡機各拿同分靠
# 排序選錯邊、「啞玲」被 2 字頭名詞規則擋掉後退成概覽、「按全庫存」打壞
# config 偵測讓「100」亂配到運動毛巾 100x30cm。只收「確定無歧義」的 pair，
# 在 WS 入口最早套用（rewrite/descriptor/黑名單之前），兩平台同一份。
_TYPO_NORM = (
    # r35：追問句的功能詞錯字/注音殘字——這些字一壞，_ctx_expand 的功能詞就認不出
    # 來，追問直接失效（「那個近出紀錄呢」→ 回庫存、「安全ㄎ存多少」→ 回全店泛答）
    ("近出", "進出"), ("進處", "進出"), ("盡出", "進出"),
    ("ㄎ存", "庫存"), ("ㄍㄨ存", "庫存"), ("褲存", "庫存"), ("酷存", "庫存"),
    ("安全ㄎ", "安全庫"), ("ㄐ錄", "紀錄"), ("記錄呢", "紀錄呢"),
    ("咖啡雞", "咖啡機"), ("珈啡", "咖啡"),
    # r43 注音殘字補（衛生ㄓˇ/ㄆㄧˊ酒/ㄩˊㄐㄧㄚ墊 曾空手；露營ㄉㄥ曾誤配露營椅）
    ("生ㄓˇ", "生紙"), ("ㄆㄧˊ酒", "啤酒"), ("ㄩˊㄐㄧㄚ", "瑜珈"),
    ("營ㄉㄥ", "營燈"), ("ㄋㄞˇ粉", "奶粉"),
    # r44 英文俗稱補（check一下tissue曾空手、運動towel曾掉類別概覽）。
    # paper towel 要排在 towel 前（長詞先換，否則被拆成 paper 毛巾 誤配運動毛巾）
    ("paper towel", "衛生紙"), ("tissue", "衛生紙"), ("towel", "毛巾"),
    ("啞玲", "啞鈴"), ("啞零", "啞鈴"),
    # r97 真人聲/異體字：ASR 與 OpenCC 常出「溼」（教育部標準字），但商品名
    #   用通俗的「濕」→ 「嬰兒溼紙巾」配不到「嬰兒濕紙巾」。「溼」在中文只有
    #   「濕」一義，無條件正規化零風險（守衛/sweep 含「溼」皆 0 次）。
    #   打字用「溼」的訪客同樣受惠。
    ("溼", "濕"),
    # r98 真人聲：「電子類總值」被聽/講成「總價」——倉管語境「總價」＝「總值」
    #   （庫存價值），系統只認「總值」。demo 無其他「總價」語意，安全。
    ("總價", "總值"),
    # r101 真人聲(#73)：「對帳」被 ASR 出成「對賬」——「賬」是「帳」異體字
    #   （賬目=帳目），系統一律用「帳」。守衛/語料含「賬」0 次，無條件換安全。
    ("賬", "帳"),
    ("按全庫存", "安全庫存"), ("按全水位", "安全水位"), ("案全庫存", "安全庫存"),
    # r78：「安全庫存線」正規化（裸「安全線」另在函式尾條件處理——
    # 「補到安全線」是補貨語不能硬換，r78v 曾把 generate_po 句改壞）
    ("安全庫存線", "安全庫存"),
    ("庫純", "庫存"), ("庫崇", "庫存"),
    ("熱削", "熱銷"), ("熱效", "熱銷"),
    ("帳蓬", "帳篷"), ("藍芽", "藍牙"),
    ("智慧手還", "智慧手環"), ("啤灑", "啤酒"),
    # r81：瑜珈常見錯字（瑜迦/瑜伽）
    ("瑜迦", "瑜珈"), ("瑜伽", "瑜珈"),
    ("尿部", "尿布"), ("衛生只", "衛生紙"),
    # 英文別名（r18：related 的 kw 接地變嚴後，「coffee machine」比不到降級成
    # related_help——常見英文講法直接映射）
    ("coffee machine", "咖啡機"), ("Coffee Machine", "咖啡機"),
    ("yoga", "瑜珈"), ("Yoga", "瑜珈"), ("YOGA", "瑜珈"),
    # OOV-100 招牌大補帖（2026-07-11）：同音/形近錯字（RPI5 實測 clarify 的全補）
    ("籃芽", "藍牙"), ("滑鼡", "滑鼠"), ("瑜咖", "瑜珈"), ("慢泡鞋", "慢跑鞋"),
    # r24：律紙/化鼠（同音錯字，RPI5 clarify 而本機直達=平台分歧 → 確定性層接手）
    ("律紙", "濾紙"), ("化鼠", "滑鼠"),
    # r26：注音整詞/中英混（ㄎㄚㄈㄟ機曾錯配耳機、露營deng曾配露營椅）
    ("ㄎㄚㄈㄟ", "咖啡"), ("露營deng", "露營燈"), ("啤就", "啤酒"),
    # r27：褲存=庫存（多錯字疊加曾配去喇叭）、快沖線、摺/折異體、英文詞
    ("褲存", "庫存"), ("快沖線", "快充線"), ("摺疊", "折疊"),
    ("earphones", "耳機"), ("inventory", "庫存"), ("Inventory", "庫存"),
    # r30：建身環/耳幾/社定
    ("建身環", "健身環"), ("耳幾", "耳機"), ("社定", "設定"),
    ("水湖", "水壺"), ("寧檬", "檸檬"), ("悶稍", "悶燒"), ("咖啡綠紙", "咖啡濾紙"),
    ("筆店包", "筆電包"), ("電動壓刷", "電動牙刷"), ("垃圾帶", "垃圾袋"),
    ("揚聲器", "喇叭"), ("冒子", "帽子"), ("拉圾", "垃圾"),
    # r55 收官批：露營裝備口語短稱（「椅子呢」「燈勒」曾被守門員拒——店裡唯一的椅/燈）
    ("椅子", "露營椅"), ("燈勒", "露營燈"), ("燈呢", "露營燈"), ("燈咧", "露營燈"),
    # r56：英拼殘字（yog墊/shuei壺 展場快打）。yog 要排在 yoga 對之後（那組在前面已換完）
    ("yog", "瑜珈"), ("shuei", "水"),
    # r58：訛變補（悶少罐/電風扇——USB 風扇的口語稱呼）
    ("悶少罐", "悶燒罐"), ("電風扇", "風扇"),
    # r64：注音告別
    ("ㄅㄞˋㄅㄞˋ", "掰掰"), ("ㄅㄞㄅㄞ", "掰掰"),
    # r66：排汗衣口語短稱（「排汗的呢」——排汗的 不會撞完整名「機能排汗衣」）
    ("排汗的", "排汗衣的"),
    # r67：橡膠手套口語（「橡膠的那種」）
    ("橡膠的", "橡膠清潔手套的"),
    # r68：網路語尾（先醬=先這樣）
    ("先醬", "先這樣"),
    # （r70 冠亞季軍 pair 撤回：曾改壞守衛句「最近電子產品的銷售冠軍是誰」——
    #   改用 rewrite 表的整句規則）
    # 俗稱正名
    ("健身墊", "瑜珈墊"), ("吸汗衣", "排汗衣"), ("T恤", "素T"),
    ("餅乾", "蘇打餅"),   # r28：config item「餅乾」曾誠實找不到（唯一對應）
    # 英文/拼音俗稱（展場常見）
    ("earphone", "耳機"), ("Earphone", "耳機"), ("earbuds", "耳機"),
    ("speaker", "喇叭"), ("Speaker", "喇叭"),
    ("powerbank", "行動電源"), ("power bank", "行動電源"), ("Powerbank", "行動電源"),
    ("keyboard", "鍵盤"), ("Keyboard", "鍵盤"),
    ("tshirt", "素T"), ("Tshirt", "素T"), ("t-shirt", "素T"), ("T-shirt", "素T"),
    ("beer", "啤酒"), ("Beer", "啤酒"),
    ("paper towel", "衛生紙"), ("mouse", "滑鼠"), ("Mouse", "滑鼠"),
    ("kafei", "咖啡"), ("shui壺", "水壺"),
    ("smart watch", "智慧手環"), ("Smart Watch", "智慧手環"), ("smartwatch", "智慧手環"),
)

# 全形→半形（r19：「南倉出貨３０個智慧手環」全形數字讓 qty 抽不到）
_FW2HW = str.maketrans("０１２３４５６７８９ＡＢＣＤＥＦＧａｂｃｄｅｆｇ．：",
                        "0123456789ABCDEFGabcdefg.:")


# ── 英文版（EN build）：跳過中文導向處理 ──────────────────────────
#   商品名/資料已英文化。原 _TYPO_NORM/_REWRITE_RULES 是為「中文商品名」磨的，
#   含大量「英文詞→中文商品名」映射（power bank→行動電源、towel→毛巾…）。商品名
#   英文化後這些映射變污染源：把英文詞導向已不存在的中文名 → 誤配。
#   對「以英文為主」的輸入直接跳過中文映射/改寫，讓英文詞 substring 直接對英文商品名，
#   容錯改靠補訓模型泛化。規則本體保留（不刪）以利與中文版對照、日後參考。
def _is_mostly_english(s: str) -> bool:
    cjk = sum(1 for c in s if "一" <= c <= "鿿")
    ascii_letters = sum(1 for c in s if c.isascii() and c.isalpha())
    # 有英文字母、且中文字極少（≤1，容忍偶發混一個中文語氣詞）
    return ascii_letters >= 2 and cjk <= 1


# ── `daily` 的兩種詞性：頻率副詞 vs 形容詞（2026-08-03）────────────────
#   `daily report` / `daily summary` 的 daily 是**形容詞**（日報這份文件），
#   不是「每天執行」。但排程判準把它當頻率詞 ⇒
#     `show me the daily report` → **開排程確認卡**（訪客只想看報表，
#     按下去卻建立每天 09:00 的持久化排程）。
#   坑 8 補充記過同型（`daily goods` 類別名被當頻率詞），當時只補了
#   `goods|necessities` 兩個負向環視詞 ⇒ **換一個名詞就再犯**。
#   ⇒ 不再往負向環視塞詞（塞詞已證明會漏），改用**語境**分辨：
#     真排程句必然帶「排程動詞或時刻」（schedule/set up/every X/at 9am），
#     索取句是 give me / show me / i want / can i see / … please。
#   ⚠️ 只在「沒有任何排程訊號」時才認定為索取 ——
#     `schedule a daily report at 9am` 兩者都有，排程訊號優先，照常開排程卡。
_EN_SCHED_SIGNAL = _re.compile(
    r"\b(?:schedule|scheduled|scheduling|recurring|set\s+up|"
    r"every\s+(?:day|morning|night|evening|week|month|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday|\d+\s*days?)|"
    r"each\s+(?:day|week|month)|automatically|"
    r"at\s+\d{1,2}\s*(?::\d{2})?\s*(?:am|pm|o'clock)|"
    r"at\s+\d{1,2}:\d{2})\b", _re.I)
_EN_FETCH_NOW = _re.compile(
    r"\b(?:show|give|send|get|fetch|bring|display|"
    r"i\s+(?:want|need|would\s+like)|can\s+i\s+(?:see|get|have)|"
    r"let\s+me\s+see|pull\s+up|open)\b|\bplease\s*[.?!]*$|\bnow\b", _re.I)


def _en_daily_is_adjective(user_text: str) -> bool:
    """英文句的 daily/weekly/monthly 是形容詞（索取一份日報）而非排程頻率。

    True ⇒ 排程判準應該讓路，讓句子走原本的報告/查詢路由。
    保守設計：只要句中有任何排程訊號就回 False（寧可維持現狀也不要吃掉真排程句）。
    """
    if not _is_mostly_english(user_text):
        return False
    if _EN_SCHED_SIGNAL.search(user_text):
        return False
    return bool(_EN_FETCH_NOW.search(user_text))


def _normalize_typos(user_text: str) -> str:
    # EN build：以英文為主的輸入跳過中文錯字/注音/中英映射（只保留全半形正規化）
    if _is_mostly_english(user_text):
        return user_text.translate(_FW2HW)
    t = user_text.translate(_FW2HW)
    # 自我修正句取後半（OOV-100：「奶瓶刷…不對 電動的牙刷還有嗎」曾抓前半
    # 的奶瓶刷 clarify）——「X…不對/不是 Y」訪客要的是 Y
    import re as _re_fix
    # r28：「啞鈴啊不對是健身環」的「不對是」黏字變體（原 regex 要求空格）
    _m_fix = _re_fix.match(r"^[^，,。]{1,10}?[…\.]*\s*(?:不對|不是啦|不是)[ ，,是]+(.{3,})$", t)
    if _m_fix:
        t = _m_fix.group(1)
    # r82：反向口誤更正「Y啦不是X」——正確商品在前（「咖啡豆啦不是咖啡機」要咖啡豆）。
    # 前段 2-8 字＋「啦/才對」＋「不是/不對」＋後段 → 取前段 Y
    _m_fix2 = _re_fix.match(r"^(?:呃|欸|喔|唉|啊)?\s*(.{2,8}?)(?:啦|才對|才對啦|對啦)?"
                            r"\s*(?:不是|不對)\s*.{2,10}$", t)
    if _m_fix2 and not _m_fix:
        import warehouse as _W_fix2
        _fix2_kw = _m_fix2.group(1).strip()
        if _fix2_kw and _W_fix2.match_items(_fix2_kw) and \
                _W_fix2.match_items(_fix2_kw)[0].get("score", 0) >= 3:
            t = _fix2_kw
    for _bad, _good in _TYPO_NORM:
        if _bad in t:
            t = t.replace(_bad, _good)
    # r78：安全線→安全庫存 正規化——「補到安全線」（補貨語）不換
    if "安全線" in t and "到安全線" not in t:
        t = t.replace("安全線", "安全庫存")
    # r98 異體字「周→週」，只在時間語境（這/上/本/下/每/前/近+數字 周）——
    #   ASR 常出「這周/上周」，系統時間詞認「週」。⚠️ 限語境避免碰
    #   周圍/周到/周轉/周邊 等正常詞（守衛「周」出現 0 次，但防未來新增）。
    t = _re.sub(r"(這|上|本|下|每|前|哪|[0-9一二兩三四五六七八九十幾])周", r"\1週", t)
    if t != user_text:
        log.info(f"[typo-norm] 「{user_text}」→「{t}」")
    return t


def _descriptor_hit(user_text: str) -> str | None:
    """描述句偵測（rewrite 之前呼叫——rewrite 會把描述換掉）。命中回傳商品關鍵字。"""
    # ── EN build：英文句一律不走中文描述表 ──────────────────────────────
    #   _DESCRIPTOR_ALIASES 是為中文商品名磨的，回傳值全是**中文商品名**。
    #   表裡夾雜的英文碎片（bra / t恤的 t / 素色t）會讓英文句命中並拿到
    #   中文名：'athletic bra stock' → 「彈性運動內衣」→ OOV clarify 印出
    #   「We don't carry 「彈性運動內衣」」中英混血（守衛第 9 輪抓到）。
    #   英文的描述句容錯由 alias_en + _en_fuzzy_keyword 負責。
    if _is_mostly_english(user_text):
        return None
    t = user_text.strip().translate(_S2T)
    # r75：描述 pattern 命中、但句中已含完整商品名（≥4 字，含新建商品）→
    # 用句內的名字取代別名。「鑄鐵平底鍋庫存」曾被「平底鍋→不沾鍋」別名搶走
    # 查不到新商品；非描述句不受影響（避免所有含商品名的句子都變描述命中）
    _dh_exact = None
    try:
        import warehouse as _W_dh
        _s_dh = t.replace(" ", "").lower()
        for _it_dh in _W_dh.state().items:
            _nm_dh = _it_dh["name"].split()[0].lower()
            if len(_nm_dh) >= 4 and _nm_dh in _s_dh:
                _dh_exact = _it_dh["name"]
                break
    except Exception:
        pass
    for _dh_pat, _dh_name in _DESCRIPTOR_ALIASES:
        if _dh_pat.search(t):
            return _dh_exact or _dh_name
    return None


def _rewrite_query(user_text: str) -> str:
    """將口語/模糊輸入改寫成 LLM 訓練時的標準句型。"""
    # EN build：以英文為主的輸入跳過中文口語改寫（_REWRITE_RULES 全是中文 pattern）
    if _is_mostly_english(user_text):
        return user_text.strip()
    t = user_text.strip().translate(_S2T)
    # 亂敲重複詞收斂：「庫存庫存庫存庫存庫存」→「庫存」（conv100-r7b 亂打組）
    _rep_m = _re.fullmatch(r"(.{1,4})\1{2,}", t)
    if _rep_m:
        t = _rep_m.group(1)
        log.info(f"[Rewrite] 重複詞收斂 → 「{t}」")
    # 功能描述句：不在此改寫（用 sub 會把「還有嗎」殘留成「嗎」、語氣詞被吞，
    # 害 WS 直達的 _DESC_Q_CUES 守衛判斷失敗掉進 LLM → clarify）。描述判斷
    # 全權交給 WS 端的 _descriptor_hit + 功能描述直達 fast-path（在 rewrite
    # 之前跑、且不改動原句）。2026-07-07 放寬 _DA_TAIL 後暴露此雙軌衝突。
    # 排程句一律不 rewrite——「每天晚上七點自動出缺貨警示」曾被缺貨規則改寫成
    # 「哪些商品缺貨警示」，時間/頻率資訊全毀（conv100-r5，教訓同 585 行註解）。
    # Pre-C-Sched 會用原句攔截 set_schedule。
    if _re.search(r"每天|每日|天天|每週|每周|每星期|每禮拜|每個月|每月|每逢", t):
        return t
    # compare rewrite 資訊銷毀防護（conv100-r5）：「北倉和中倉哪邊的貨比較齊」被改寫
    # 成固定句「比較各倉庫庫存」→ 倉名/指標全毀，LLM 預設回 central vs south 答非所問。
    # 句中已點名 ≥2 倉、或含指標詞（週轉/價值/缺貨）→ 保留原句給 LLM + Pre-C-Cmp。
    _cmp_wh_cnt = len({z[0] for z in ("北倉", "北區", "中倉", "中區", "南倉", "南區") if z in t})
    _cmp_keep = _cmp_wh_cnt >= 2 or any(w in t for w in ("週轉", "價值", "總值", "缺貨"))
    # r60：倉別最上級（「北中南倉哪個最強」）例外——比較固定句會列三倉排名，
    # 點名倉不損資訊；保留原句反而掉 LLM 回庫存排行（答非所問）
    if any(w in t for w in ("最強", "最弱", "最滿", "最空", "墊底")):
        _cmp_keep = False
    # 熱銷 rewrite 同病：「這個月熱銷排行」被改成固定句「熱銷商品排行」→ 時間詞
    # 銷毀，C4b 拿改寫後句子校 period 校不回 this_month（conv100-r8）
    # r16 補：倉名/庫存語氣也要保留原句——「熱銷第一名在南倉有幾個」曾被改寫成
    # 「熱銷商品排行」，南倉+有幾個 全銷毀，複合攔截（排行Top1+庫存）就接不到
    # （rewrite 固定句資訊銷毀第四例）
    _hot_keep = any(w in t for w in ("本月", "這個月", "上個月", "月", "今天", "本週", "這週",
                                     "北倉", "中倉", "南倉", "北區", "中區", "南區",
                                     "有幾", "還剩", "剩多少", "庫存", "還有多少",
                                     # r18：「防曬帽跟毛帽哪個賣得好」被改寫成固定句
                                     # → 兩商品資訊全毀（兩商品銷量比較攔截接不到）
                                     "哪個賣", "誰賣", "哪一個賣",
                                     # r25：「不要熱銷榜 我要滯銷的」被改寫成「熱銷商品
                                     # 排行」＝資訊銷毀+語意反轉（第十例）——否定詞/滯銷
                                     # 詞在場一律保留原句給 C4 後講的贏判準
                                     "不要", "不是", "滯銷", "賣不動", "賣最差", "冷門",
                                     # r28：「運動類熱銷排行」類別被固定句銷毀（第十一例）
                                     "類", "用品"))
    # r67：「熱銷第十名是什麼」名次被固定句銷毀（第十二例）——帶第N名一律保留原句
    if _re.search(r"第[一二三四五六七八九十\d]+名", t):
        _hot_keep = True
    # expiring rewrite 同病（r18，固定句資訊銷毀第六例）：「香皂快過期了嗎」被
    # 改寫成「哪些商品即將到期」→ 商品名銷毀，C7 的「指名未知商品誠實回找不到」
    # 接不到。剝掉到期語後仍有具體名詞殘餘 → 保留原句。
    _exp_resid_rw = _re.sub(r"(?:快要|快|已經|要)?(?:過期|到期|壞掉|不能賣|即期品|即期)(?:了)?(?:嗎|沒)?"
                            r"|[的有呢啊喔嗎？?]", "", t).strip()
    _exp_keep = (len(_exp_resid_rw) >= 2 and bool(_re.fullmatch(r"[一-鿿]+", _exp_resid_rw))
                 and not any(g in _exp_resid_rw for g in ("什麼", "哪些", "東西", "商品",
                                                           "期限", "保存", "批", "飲料", "食品")))
    # movement rewrite 同病（r17，固定句資訊銷毀第五例）：「上週北倉進了哪些貨」
    # 被改寫成「查詢進出記錄」→ 期間+倉別全毀，回「本週全部商品」。句中帶
    # 倉名/明確期間 → 保留原句，讓 C2e/C17a 校正接手。
    # ⚠️ 期間詞抽成共用常數（2026-08-03，第 10 例資訊銷毀的教訓）：
    #   `_parse_days` 加了「前一週/前一個月/前一季」，但**這裡的保護清單沒跟著加**
    #   ⇒ 「給我前一週的進出記錄」被 1265 行的固定句 rewrite 吃掉期間，
    #     只回今天一天的資料。加期間詞時**兩處必須同步**，故集中在此。
    _PERIOD_KEEP_WORDS = (
        "上週", "上周", "上禮拜", "昨天", "昨日", "前天",
        "上個月", "今天", "本月", "這個月", "本週", "這週", "這禮拜",
        # 2026-08-03 user 需求：往前推一週/一個月/一季
        "前一週", "前一周", "前一個月", "前一月", "前一季", "上一季", "上季",
        "近一季", "本季", "這一季", "前三個月", "近三個月", "過去三個月",
        "前七天", "前7天", "最近", "過去",
    )
    _mv_keep = any(w in t for w in ("北倉", "中倉", "南倉", "北區", "中區", "南區")) \
        or any(w in t for w in _PERIOD_KEEP_WORDS)
    _GENERIC_RCA_HEADS = ("庫存", "數量", "進貨", "帳", "對不上", "差異")
    # ── r20 通用實體守衛：句中帶商品名/倉名/類別詞 → 跳過所有「固定句」改寫 ──
    # 固定句 rewrite 已累計 9 例資訊銷毀（compare/hot×3/movement/expiring×2/
    # alert/low×4/related×2），逐 rule 補 keep 條件補不完。帶實體的句子保留
    # 原句給 LLM+校正層，一定不比資訊全毀的固定句差。帶 group（）的改寫
    # 不受影響（它們保留原句片段）。
    _ent_hit = (any(w in t for w in ("北倉", "中倉", "南倉", "北區", "中區", "南區"))
                or any(w in t for w in ("電子", "家電", "廚具", "食品", "飲料", "日用",
                                         "服飾", "衣服", "運動用品", "露營用品")))
    if not _ent_hit:
        _kw_rw = _extract_sku_keyword(t)
        if _kw_rw:
            import warehouse as _W_rw
            try:
                _m_rw = _W_rw.match_items(_kw_rw)
                _ent_hit = bool(_m_rw) and _m_rw[0].get("score", 0) >= 3
            except Exception:
                pass
    # r60：倉別最上級（「北中南倉哪個最強」）例外——固定句比較會列三倉排名，
    # 倉名不算會被銷毀的資訊；不例外會掉 LLM 回庫存排行（答非所問）
    if "倉" in t and any(w in t for w in ("最強", "最弱", "最滿", "最空", "墊底")):
        _ent_hit = False
    for pattern, replacement in _REWRITE_RULES:
        if _ent_hit and "\\1" not in replacement:
            continue
        if _cmp_keep and replacement == "比較各倉庫庫存":
            continue
        if _hot_keep and replacement == "熱銷商品排行":
            continue
        if _mv_keep and replacement == "查詢進出記錄":
            continue
        if _exp_keep and replacement == "哪些商品即將到期":
            continue
        # 警示設定句不可被缺貨固定句改寫（r19，資訊銷毀第七例：「幫瑜珈墊設
        # 缺貨警示」→「哪些商品缺貨警示」商品名+設定意圖全毀 → low_stock）。
        # 條件用「警示設定詞組」——裸「幫/設」太寬，「拜託幫我看看什麼快沒了」
        # 曾被誤留原句掉 guide（r19 smoke 抓到）
        if (replacement == "哪些商品缺貨警示"
                and any(w in t for w in ("設缺貨警示", "設警示", "設庫存警示",
                                          "加警示", "建警示", "設個警示", "加個警示",
                                          "訂警示"))):
            continue
        m = pattern.search(t)
        if m:
            if "\\1" in replacement:
                rewritten = pattern.sub(replacement, t)
                # group 1 是純通用詞（非商品名）→ 改回固定字串
                try:
                    g1 = m.group(1).strip()
                    if g1 in _GENERIC_RCA_HEADS or g1 == "":
                        rewritten = "庫存帳對不上"
                except IndexError:
                    pass
            else:
                rewritten = replacement
            if rewritten != t:
                log.info(f"[Rewrite] 「{t}」→「{rewritten}」")
                return rewritten
    return t


def _detect_clarify(user_text: str) -> dict | None:
    """
    偵測模糊輸入，回傳 clarify payload 或 None。
    payload = {"question": "...", "options": ["...", ...], "hint": "..."}
    """
    import warehouse as W
    t = user_text.strip()
    if not t or len(t) > 60:   # 太長的句子不攔（通常很具體）
        return None

    # RCA intent → 直接放行，交給校正層處理
    if _has_rca_word(t):
        return None

    # ── 匯出進出紀錄：沒指定期間 → 反問（user 定調 2026-08-03）──
    #   訪客講「匯出進出紀錄」時，直接給預設 7 天他不知道可以改；
    #   反問並給可點選項，跟現有 clarify 選單同一套機制。
    #   ⚠️ 已經講了期間的（昨天/本週/最近 N 天）不攔，直接放行執行。
    _exp_t = t.lower()
    # ⚠️ 索取式也算匯出意圖（2026-08-04）：`give me a csv of the movements`
    #   沒講期間 → 該出期間選單,而不是掉到 movement 統計卡。
    _exp_intent = _re.search(
        r"\bexport\b.*\b(?:movement|transaction|record|log|history|in\s*/?\s*out)|"
        r"\b(?:movement|transaction)\s*(?:log|record|history)\b.*\bexport\b|"
        r"\bdownload\b.*\bmovement", _exp_t)
    if not _exp_intent and _en_export_intent(_exp_t):
        # ⚠️ 索取式擴充要**商品讓路**（user 定調：統計卡留給單一商品）：
        #   `give me the movement records for wireless mouse`（13 分）走統計卡,
        #   `give me a csv of the movements`（雜訊 4 分）走期間選單。
        #   門檻 6 來自實測分布（雜訊 3-5 / 真商品 7-13）。
        try:
            import warehouse as _W_expi
            _m_expi = _W_expi.match_items(_exp_t)
            if not (_m_expi and _m_expi[0].get("score", 0) >= 6):
                _exp_intent = True
        except Exception:
            _exp_intent = True
    # ⚠️ 必須與選單措辭對齊（2026-08-03）：選項改成 Last week/month/quarter 後,
    #   訪客點了送出的句子若這裡認不得,會**再反問一次**（選單承諾跳票）。
    _exp_has_period = _re.search(
        r"\b(?:today|yesterday|this\s+week|last\s+week|this\s+month|last\s+month|"
        # ⚠️ previous/past 變體（2026-08-04）：原本只收 previous quarter，
        #   漏了 previous week / past month ⇒ 路由已定案成 run_script，
        #   卻在這裡被判「沒講期間」→ 又反問一次（選單承諾跳票同型）。
        r"(?:last|past|previous)\s+(?:week|month|quarter)|"
        r"this\s+quarter|last\s+3\s+months|past\s+3\s+months|"
        r"past\s+\d+|last\s+\d+|recent\s+\d+|\d+\s*days?)\b", _exp_t)
    # ⚠️ 排程句讓路（2026-08-04，鏡射中文版 _exp_sched）：
    #   `schedule the movement export every monday` 是要**設排程**,
    #   「every monday」是頻率不是期間 → 不該問期間,交給 Pre-C-Sched。
    _exp_sched_en = _re.search(
        r"\b(?:every\s+\w+|daily(?!\s+(?:goods|necessities))|weekly|monthly|"
        r"nightly|each\s+(?:day|week|month)|schedule[ds]?|scheduling|"
        r"recurring|automatically)\b", _exp_t, _re.I)
    if _exp_intent and not _exp_has_period and not _exp_sched_en:
        return {"question": "Which period do you want to export?",
                "options": ["Yesterday", "Last week", "Last month", "Last quarter (3 months)"],
                "actions": ["export movements yesterday",
                            "export movements last week",
                            "export movements last month",
                            "export movements last quarter"],
                "hint": "Movement log export"}


    # 進出貨結構（含空格斷開，r19：「北倉進 50 個 耳機」曾被這裡誤攔成
    # 「你想查什麼」）→ 放行給 C13b 開卡
    import re as _re_dc
    if _re_dc.search(r'[進出][一-鿿\s]{0,8}(?:[0-9]+|[零一二兩三四五六七八九十百千萬億半]+)\s*'
                     r'(?:件|個(?!月|星期|禮拜)|條|支|台|箱|包|瓶|罐|組|雙|套|盒|對|頂|張|把|打)', t):
        return None

    # 「庫存加/減N件」語意本身模糊——可能是改安全庫存設定值，也可能是進出貨事件，
    # 沒有明確進出貨動詞（進了/出貨等）時不要硬猜，讓訪客自己選（user 2026-07-01 要求）。
    import re as _re_c13c
    _movement_verbs_c13c = ("進了", "進貨", "到貨", "收貨", "入庫", "補了", "補貨",
                            "來貨了", "來貨", "出貨了", "出貨", "出庫", "賣掉了",
                            "賣掉", "賣了", "銷貨", "出了")
    _qty_m_c13c = _re_c13c.search(r'(\d+)\s*(?:件|個(?!月|星期|禮拜|小時|鐘頭)|條|支|台|箱|包|瓶|罐|組|雙|套|盒|對|頂|張|把|副|顆|粒|袋|桶|杯|塊|片|卷|捲)', t)
    if (_qty_m_c13c and "庫存" in t and any(w in t for w in ("加", "減"))
            and not any(w in t for w in _movement_verbs_c13c)):
        kw = _extract_sku_keyword(t) or ""
        qty = _qty_m_c13c.group(1)
        return {
            "question": f"「{kw or t}」要修改安全庫存設定，還是記一筆進出貨？",
            "options": [f"修改「{kw}」的安全庫存設定", f"記一筆「{kw}」進貨 {qty} 件",
                        f"記一筆「{kw}」出貨 {qty} 件"],
            "hint": "輸入數字選擇，或直接輸入完整描述",
        }

    # 「XX多了/少了N件」語意本身模糊——可能是盤點發現的庫存差異（該走 RCA 查原因），
    # 也可能是進出貨事件的口語講法，沒有明確進出貨動詞時不要硬猜（同上一條原則）。
    if (_qty_m_c13c and any(w in t for w in ("多了", "少了"))
            and not any(w in t for w in _movement_verbs_c13c)):
        kw = _extract_sku_keyword(t) or ""
        qty = _qty_m_c13c.group(1)
        _dir_word = "進貨" if "多了" in t else "出貨"
        return {
            "question": f"「{kw or t}」是盤點發現的庫存差異，還是要記一筆{_dir_word}？",
            "options": [f"查「{kw}」的庫存差異原因", f"記一筆「{kw}」{_dir_word} {qty} 件"],
            "hint": "輸入數字選擇，或直接輸入完整描述",
        }

    # ⚠️ EN build（語音）：**要用小寫比對**——`_ALL_INTENT_WORDS` 的英文詞
    #   全是小寫，而 ASR（whisper）一律輸出首字大寫與專有名詞大寫
    #   （`Transfer 20 Bluetooth earphones from North to Central.`）→
    #   `"transfer" in t` 不成立 → has_intent=False → 整句被判成「只有商品名
    #   沒動作」轉 clarify，**校正層（C13a）根本執行不到**。
    #   實測：同一句小寫開卡成功、大寫回「你想知道耳機的什麼？」。
    #   打字訪客不會打大寫，所以文字端 r1-r5 五輪收斂都沒暴露這條。
    _t_low_intent = t.lower()
    # r12（TTS 基準批）：**撇號也要攤平**——同大小寫那條的道理。
    #   ASR 輸出 `what's in central warehouse…`（帶撇號），詞表寫的是
    #   `whats in` → 比不到 → has_intent=False → 又掉進 clarify。
    #   ⚠️ 彎引號 U+2019 也要收：whisper 兩種都會產，肉眼看不出差別。
    _t_noapos = _t_low_intent.replace("'", "").replace("’", "")
    has_intent = any(w in t or w in _t_low_intent or w in _t_noapos
                     for w in _ALL_INTENT_WORDS)

    # 剝通用填充詞，避免「幫我查」的「幫我」誤觸商品 match
    _FILLER = ("幫我", "幫忙", "請問", "麻煩", "請", "幫", "給我", "看一下",
               "查一下", "查查", "看看", "了解", "確認", "問一下", "一下", "呢", "嗎", "啊",
               "我想要", "我想", "想要", "想看", "想知道", "想查", "我要", "要查", "要看")
    t_clean = t
    for f in _FILLER:
        t_clean = t_clean.replace(f, "")
    t_clean = t_clean.strip()
    # EN build：英文句也要剝虛詞/意圖動名詞，否則下面 ⑤ 的 match_items 會
    #   靠虛詞亂中（'whats running out at north' → Running Shoes Men's →
    #   反問「你想知道 Running Shoes 的什麼」，守衛第 10 輪抓到）
    if _is_mostly_english(t):
        t_clean = _re.sub(
            r"\b(?:running|getting|going|runs|gets)\s+(?:out|low|short|down)\b",
            " ", t_clean, flags=_re.I)
        t_clean = _re.sub(
            r"\b(?:whats|what|hows|how|is|are|the|a|an|of|do|does|did|we|i|you|"
            r"got|have|has|any|some|there|show|me|tell|give|list|check|look|"
            r"see|find|get|left|remain|remaining|on|hand|in|at|for|to|from|"
            r"with|now|currently|available|please|pls|still|right|it|its|"
            r"should|need|want|know|about|anything|something)\b",
            " ", t_clean, flags=_re.I)
        t_clean = _re.sub(r"\s+", " ", t_clean).strip(" ?.!,")

    # ⓪ 剝完後 t_clean 為空 → 純意圖動詞，直接給通用選單
    if not t_clean and not has_intent:
        if _is_mostly_english(t):
            return {
                "question": "What would you like to check?",
                # options 送回後端 → 用後端聽得懂的英文句
                "options": ["whats running low", "whats expiring soon",
                            "best sellers this month", "any stock discrepancies"],
                "hint": "Tap an option or type an item name",
            }
        return {
            "question": "你想查什麼？",
            "options": ["哪些商品快缺貨", "哪些商品快到期", "本月熱銷商品", "採購對帳異常"],
            "hint": "輸入數字選擇，或直接輸入商品名稱",
        }

    # ① 只有倉庫名、沒有動作詞 → 問查什麼
    #   例外：多個倉庫名同時出現 → 是比較意圖，直接放行
    _wh_names = ["北倉", "北區倉", "南倉", "南區倉", "中倉", "中區倉"]
    matched_whs = [zh for zh in _wh_names if zh in t]
    matched_wh = matched_whs[0] if matched_whs else None
    # 如果也含類別或商品關鍵字 → 不是純倉庫查詢，不攔
    _cat_hint = next((zh for zh in ("電子", "家電", "廚具", "食品", "飲料", "日用", "服飾", "運動") if zh in t), None)
    _has_product = bool(W.match_items(t_clean)) if t_clean else False
    # match_items 只做整句子字串比對，句子含雜訊（動詞/數量詞）時比對不到
    # （「咖啡機剛進100包到北倉」match_items 抓不到「咖啡機」）。
    # 用 _extract_sku_keyword 的多層 fuzzy 邏輯再試一次，比較不會漏判。
    if not _has_product:
        _has_product = bool(_extract_sku_keyword(t))
    if matched_wh and len(matched_whs) < 2 and not has_intent and not _cat_hint and not _has_product:
        return {
            "question": f'What do you want to check for "{matched_wh}"?',
            # EN build：options 是送回後端的查詢字串 → 必須是後端聽得懂的英文句
            "options": [
                f"{matched_wh} low stock",
                f"{matched_wh} recent movements",
                f"{matched_wh} expiring items",
                f"{matched_wh} stock value",
            ],
            "hint": "Tap one of the options, or type a more complete question"
        }

    # ② 採購/短少/PO 意圖 + 無 SKU keyword → 推工具選項
    _po_kw = {"短少", "短收", "PO", "po", "訂單", "採購單", "採購", "對帳", "帳對不上"}
    # 明確產採購單意圖 → 直接放行，不攔
    _po_direct = ("產採購", "下採購", "補貨單下單", "幫我叫貨", "開採購", "幫我把缺貨",
                  "缺貨清單轉採購", "缺貨的產", "幫我補貨", "產po",
                  # 「出一張採購單」「列成採購單」這類明確開單動詞（第9輪測試補）
                  "出一張", "開一張", "生一張", "出採購", "列成採購", "列採購", "該補的",
                  # 「幫我開單採購」（第10輪測試補）
                  "開單採購", "開單補貨", "開單",
                  # 「開進採購單」「擬一張採購草稿」「轉採購單」（第11輪測試補）
                  "開進採購", "擬一張", "擬張", "擬採購", "轉採購", "開張採購")
    # ⚠️ EN build：「po」是 substring 比對 → 英文句裡到處都中
    #   （re**po**rt / ex**po**rt / **po**rtable / trans**po**rt），
    #   'generate a full report' 因此被判成採購意圖、回 PO clarify。
    #   英文的 po/order 要求**詞界**，中文詞不受影響。
    _po_zh = {w for w in _po_kw if any("一" <= c <= "鿿" for c in w)}
    has_po_intent = (any(w in user_text for w in _po_zh)
                     or bool(_re.search(r"\b(?:po|pos|purchase orders?|"
                                        r"orders?)\b", t.lower())))
    has_po_direct = any(w in user_text for w in _po_direct)
    # EN build：英文的**明確開單動詞**（_po_direct 全中文 → 英文開單句
    #   命中 has_po_intent 卻沒被放行，全被 PO clarify 攔住）
    if _re.search(r"\b(?:create|generate|make|draft|raise|issue|open|"
                  r"give me|prepare|build|"
                  # 2026-08-04：'i need a purchase order …' 曾被 PO 歧義
                  #   選單攔住——需要句也是明確開單意圖
                  r"i\s+need|i\s+want|we\s+need|need\s+an?|want\s+an?)"
                  r"\b[^.]{0,30}?"
                  r"\b(?:po|purchase orders?|orders?)\b", t.lower()):
        has_po_direct = True
    # 系統性防漏：跟 C-PO 同一組判斷——「採購單/採購草稿/補貨單 + 開單動詞」
    # 一律當明確開單意圖直接放行，不再逐字追 _po_direct 同義詞
    # （出一張/開單採購/擬一張/給我一張…已經漏了三輪，第13輪定案）
    if (any(w in user_text for w in ("採購單", "採購草稿", "補貨單", "補貨草稿", "補貨採購", "開單採購", "開單補貨"))
            and any(v in user_text for v in ("出", "開", "產", "生", "列", "建", "做", "給我", "擬", "轉", "來一", "拉"))):
        has_po_direct = True
    # 兩個倉名同時出現 → 比較意圖，放行（不攔）
    has_two_whs = len(matched_whs) >= 2
    if has_po_intent and not has_po_direct and not has_two_whs:
        sku_kw = _extract_sku_keyword(user_text)
        has_sku = bool(sku_kw and len(sku_kw) >= 2 and any(
            sku_kw in nm for nm in [it["name"] for it in W.state().items]
        ))
        if not has_sku:
            # EN build：options/actions 是**會送回後端的查詢字串**，
            #   中文的話英文版後端直接 reject（訪客一點就壞）
            return {
                "question": "Which purchasing / shortfall question do you mean?",
                "options": [
                    "Check all short-received POs (scan all warehouses)",
                    "Check which items are low on stock",
                    "Generate a purchase order to restock",
                    "Check purchasing issues for a specific item",
                ],
                "actions": [
                    "check all purchase shortfalls",
                    "whats running low",
                    "create a purchase order for low stock",
                    "any stock discrepancies",
                ],
                "hint": "Tap one of the options, or just say the item name"
            }

    # ④ 類別詞 + 無動作 → 問查什麼（優先於商品名 match，避免把類別詞誤當商品名）
    _cat_kw = {
        "電子": "electronics", "3c": "electronics", "食品": "food", "飲料": "beverage",
        "清潔": "cleaning", "清潔用品": "cleaning", "嬰幼": "baby", "醫療": "medical",
        "戶外": "outdoor", "家居": "home",
    }
    matched_cat = next((zh for zh in _cat_kw if zh in t.lower()), None)
    # r36：類別詞是別的商品名的前綴時不可搶（「清潔手套」含「清潔」、但它是完整
    #   商品名，不是要問清潔『類』）。句中抽得到真商品 → 讓給商品名。
    if matched_cat:
        import warehouse as _W_cat
        _cat_kw2 = _extract_sku_keyword(t)
        _cat_m = _W_cat.match_items(_cat_kw2) if _cat_kw2 else []
        if _cat_m and _cat_m[0].get("score", 0) >= 5:
            matched_cat = None
    if matched_cat and not has_intent:
        return {
            "question": f'What do you want to check for the "{matched_cat}" category?',
            "options": [
                f"{matched_cat} low stock",
                f"{matched_cat} best sellers",
                f"{matched_cat} expiring items",
                f"{matched_cat} movements",
            ],
            "hint": "Tap one of the options, or type a more complete question"
        }

    # ⑤ 只有商品名、沒有任何動作詞 → 問要做什麼（用 t_clean 剝掉填充詞再 match）
    matched = W.match_items(t_clean) if t_clean else []
    # EN build：功能描述句不可在這裡被 match_items 亂中後反問——
    #   'what do i drink after running' 撈到 Running Shoes Men's 反問
    #   「你想知道跑鞋的什麼」。它有明確目標（運動飲料），放行讓
    #   _extract_sku_keyword 的描述層處理。
    if matched and _is_mostly_english(t) and _en_descriptor_hit(t):
        matched = []
    # 進出量問句同理不可在這裡被商品名亂中後反問
    #   （'this months in and out' 撈到 Smart Fitness Band 反問）
    if matched and _is_mostly_english(t) and _C4MV_RE.search(t):
        matched = []
    # ⚠️ EN build（劇情批 r1）：**低分噪音**不可拿來反問。實測招呼語
    #   'hi there busy today' 的 'hi' 撈到 Automatic Coffee **M**achine
    #   （score=2）→ 訪客只是打招呼，系統卻反問「你想知道咖啡機的什麼？」。
    #   同類前科：'alert me' 的 'no item' 比到 Ceramic **No**n-stick Pan。
    #   反問是要幫訪客聚焦，拿一個他沒提過的商品反問只會更混亂
    #   → 分數不足就不反問，讓下游走正常路徑（guide/概覽）。
    if (matched and _is_mostly_english(t)
            and matched[0].get("score", 0) < 4):
        log.info(f"[clarify] 商品名分數不足({matched[0].get('score', 0)})不反問: {t!r}")
        matched = []
    if matched and not has_intent:
        item = matched[0]
        name = item["item"]["name"] if isinstance(item, dict) and "item" in item else item.get("name", t)
        return {
            "question": f'What do you want to know about "{name}"?',
            "options": [
                f"how many {name} left",
                f"{name} movements",
                f"{name} stock doesnt add up",
                f"is {name} expiring soon",
            ],
            "hint": "Tap one of the options, or type a more complete question"
        }

    # ⑥ 純模糊短句（查/看/確認等）— 用 t_clean 或 t 都檢查，剝掉填充詞後剩「查」也算
    #    也涵蓋「幫偶查」→ strip「幫」→「偶查」太短且無具體目標 → clarify
    _vague = {"查", "查詢", "看", "確認", "了解", "瞭解", "問一下", "查一下", "看一下", "看看", "那個", "這個", "欸", "誒", "喂", "嗨", "查個東西", "有個問題", "有問題", "問題",
              "然後", "然後呢", "接下來", "接下來呢", "接著呢", "再來", "再來呢",
              "有人在嗎", "有人嗎", "在嗎", "哈囉", "你好", "喂喂"}
    # 剝完填充詞只剩 1-3 字且有動作意圖 → clarify（但含類別關鍵字則放行，如「查食品」）
    _has_cat = any(zh in t for zh in ("電子", "家電", "廚具", "食品", "飲料", "日用", "服飾", "運動"))
    # r31：裸意圖詞（缺貨/到期…）放行讓 clf 直出對應清單——短輸入=產品本體，
    # 反問「你想查什麼」是砸招牌（953 句掃蕩抓到）
    if t_clean in ("缺貨", "到期", "低庫存", "補貨", "過期", "報表", "比較", "缺貨清單"):
        return None
    # r31：「查耳機」剝完 3 字但含真商品 → 放行（曾被 _too_short 吞掉反問）
    _too_short = len(t_clean) <= 3 and has_intent and not _has_cat and not _has_product
    if t in _vague or t_clean in _vague or (not t_clean and not has_intent) or _too_short:
        return {
            "question": "What would you like to check?",
            "options": [
                "which items are running low",
                "what is expiring soon",
                "best sellers this week",
                "purchase reconciliation issues",
            ],
            "hint": "Tap one, or type an item name or warehouse name"
        }

    return None


def _detect_oov(func_name: str, func_args: dict) -> dict | None:
    """
    OOV 偵測：keyword 不在已知 SKU 清單時，用 fuzzy match 推測候選商品。

    score ≥ 85 → 靜默修復（auto_fix=True，直接換 keyword，回應加提示）
    score 60-84 → 給選單讓使用者確認
    score < 60  → 回傳 None（查無，交給工具正常處理）

    只攔 query_inventory / query_movement / search_log 三個帶 keyword 的工具。
    """
    if func_name not in ("query_inventory", "query_movement", "search_log",
                         "set_alert", "list_low_stock", "compare_warehouses"):
        return None
    # set_alert 用 target，其餘用 keyword
    keyword = (func_args.get("keyword") or func_args.get("target") or "").strip()
    if not keyword or len(keyword) < 2:
        return None

    # 清理 LLM 常帶的語氣前綴/後綴，例如：
    # 前綴：「有洗衣精」→「洗衣精」
    # 後綴：「洗衣精有」→「洗衣精」、「洗衣精剩」→「洗衣精」、「詢」→ 丟棄
    _kw_prefixes = ("幫我查", "幫我看", "幫我找", "查看", "查詢", "查一下",
                    "看看", "有沒有", "有", "是", "了", "也", "還", "的")
    _kw_suffixes = ("有多少", "剩多少", "有幾個", "剩幾個", "有幾", "剩幾",
                    "有", "剩", "還", "的", "嗎", "啊", "呢", "吧", "了", "喔")
    _kw_clean = keyword
    for pfx in sorted(_kw_prefixes, key=len, reverse=True):
        if _kw_clean.startswith(pfx) and len(_kw_clean) > len(pfx) + 1:
            _kw_clean = _kw_clean[len(pfx):]
            break
    for sfx in sorted(_kw_suffixes, key=len, reverse=True):
        if _kw_clean.endswith(sfx) and len(_kw_clean) >= len(sfx) + 1:
            _kw_clean = _kw_clean[:-len(sfx)]
            break
    # 清理後太短（< 2字）→ 清空，讓後續邏輯查全部，而非用單字亂比對
    if len(_kw_clean) < 2:
        _kw_clean = ""
    if _kw_clean != keyword:
        log.info(f"[OOV] keyword 清理: 「{keyword}」→「{_kw_clean}」")
        keyword = _kw_clean

    import warehouse as W

    snap = W.state()
    all_names = [it["name"] for it in snap.items]

    # 完全命中 → 若清理過就更新 keyword，否則不需 OOV 處理
    # r37：substring 判定對「空格差異」太脆弱——「USB風扇」不是「桌上型 USB 風扇」的
    #   substring（中間有空格），RPI5 LLM 抽「USB風扇」→ 誤判沒命中 → 進 fuzzy → 撈
    #   不到 → 空選單 clarify（訪客看到「你是指？」卻沒選項＝死路）。改用 match_items
    #   （score≥5 就算命中，不受空格影響，兩平台一致），命中就靜默修成真商品名。
    _oov_m = W.match_items(keyword)
    # 只在「唯一明確命中」時 auto_fix：最高分要明顯領先第二名。否則有歧義（如 LLM 抽殘
    # 的「嬰兒」同時 match 到「嬰兒連身衣」與「嬰兒紙尿布」，盲取第一名會靜默修成錯的
    # 商品——RPI5 平台分歧抓到）。歧義時不 auto_fix，讓下游 carry-over / 反問處理。
    if _oov_m and _oov_m[0].get("score", 0) >= 5:
        _top_s = _oov_m[0]["score"]
        _second_s = _oov_m[1]["score"] if len(_oov_m) > 1 else 0
        if _top_s - _second_s >= 3:   # 領先夠多 = 唯一明確
            _oov_name = _oov_m[0]["item"]["name"]
            return {"auto_fix": True, "original_keyword": func_args.get("keyword", ""),
                    "fixed_keyword": _oov_name, "score": 100}
        # 同分歧義且 keyword 不是任何商品名的片段（LLM 抽殘如「咖啡還」）→ 不准
        # 掉進下面 fuzzy ≥85 對單一候選靜默猜（user 原則 2026-07-16 不猜）。
        # 直接給同分候選選單；是片段的（咖啡/嬰兒）留給 query_inventory 列
        # 更豐富的清單（含庫存概況），走既有 substring → None 路。
        if not any(keyword in name for name in all_names):
            _tied = [r["item"]["name"] for r in _oov_m
                     if r["score"] * 2 >= _top_s][:8]
            return {"auto_fix": False,
                    "question": f"\"{keyword}\" matches {len(_tied)} items. Which one do you mean?",
                    "options": _tied,
                    "hint": "Tap one, or type the full item name",
                    "oov": True, "original_keyword": keyword}
    if any(keyword in name or name in keyword for name in all_names):
        if keyword != (func_args.get("keyword") or func_args.get("target") or "").strip():
            # 清理前後不同 → 靜默修復，讓 caller 更新 func_args
            return {"auto_fix": True, "original_keyword": func_args.get("keyword", ""),
                    "fixed_keyword": keyword, "score": 100}
        return None

    # 用 _fuzzy_score（剝規格 + 雙向滑窗 + 字元重疊），比純 SequenceMatcher 更抗規格詞稀釋
    scored = sorted(
        [(s, n) for n in all_names if (s := _fuzzy_score(keyword, n)) >= 60],
        reverse=True,
    )

    if not scored:
        return None

    best_score, best_name = scored[0]

    if best_score >= 85:
        # 靜默修復：直接換 keyword，回應加一行提示
        return {
            "auto_fix": True,
            "original_keyword": keyword,
            "fixed_keyword": best_name,
            "score": best_score,
        }
    else:
        # 給選單：列出前 3 名候選
        options = [n for _, n in scored[:3]]
        return {
            "auto_fix": False,
            "question": f"No exact match for \"{keyword}\". Did you mean?",
            "options": options,
            "hint": "Tap one, or type the full item name",
            "oov": True,
            "original_keyword": keyword,
        }


# ── 模糊匹配：中文錯字 / 不完整名稱的容錯比對 ────────────────────────
# 問題：SequenceMatcher 把全名一起比，DB 裡「氣泡水 500ml」跟 user 的「汽泡水」
#       被規格詞稀釋到 <60%。修法：剝規格 → 取核心名 → 雙向滑窗 + 字元重疊。
import re as _re_fuzzy

_SPEC_RE = _re_fuzzy.compile(
    r'\d+(\.\d+)?\s*(ml|kg|g|mm|cm|L|oz|入|抽|包|件|組|片|張|條|雙|瓶|罐|盒|袋|箱'
    r'|公升|公斤|公克|公分|毫升|男款|女款|兒童|成人|加大|標準|輕量|厚底|短袖|長袖)'
)
_VARIANT_SFX = (' 男款', ' 女款', ' 兒童', ' 成人', ' 加大', ' 標準', ' 輕量',
                ' 厚底', ' 短袖', ' 長袖', ' 窄版', ' 寬版')


def _fuzzy_score(keyword: str, name: str) -> float:
    """中文模糊相似度 0-100。
    把 DB 商品名的規格詞剝掉後，用雙向滑窗 + 字元重疊計算。
    設計為對 2-4 字 keyword 含 1-2 個錯字仍有 ≥55 分。"""
    from difflib import SequenceMatcher

    # 剝規格詞，留下核心商品名稱
    core = _SPEC_RE.sub('', name).strip()
    for sfx in _VARIANT_SFX:
        if core.endswith(sfx):
            core = core[:-len(sfx)].strip()
            break
    if not core or len(core) < 2:
        core = name

    # ① substring 命中 → 高分（70-100，依長度比）
    if keyword in core or core in keyword:
        ratio = min(len(keyword), len(core)) / max(len(keyword), len(core))
        return 70.0 + 30.0 * ratio

    # ② 全字串 SequenceMatcher（base）
    best = SequenceMatcher(None, keyword, core).ratio() * 100

    # ③ 雙向滑窗：keyword 在 core 上滑，core 在 keyword 上滑
    kw_len, core_len = len(keyword), len(core)
    if core_len >= kw_len:
        for i in range(core_len - kw_len + 1):
            w = core[i:i + kw_len]
            best = max(best, SequenceMatcher(None, keyword, w).ratio() * 100)
    if kw_len >= core_len and core_len >= 2:
        for i in range(kw_len - core_len + 1):
            w = keyword[i:i + core_len]
            best = max(best, SequenceMatcher(None, w, core).ratio() * 100)

    # ④ 字元重疊（Dice）— 對短 keyword 的錯字額外加分
    kw_set = set(keyword)
    core_set = set(core)
    if kw_set and core_set:
        char_score = 2 * len(kw_set & core_set) / (len(kw_set) + len(core_set)) * 100
        if len(keyword) <= 3:
            best = max(best, char_score * 0.85)

    # ⑤ 2字 keyword 只共用 1 個字時，該字必須是「頭名詞」才算數——
    # 中文 2 字詞多為「修飾+頭」（電鍋的頭=鍋），X子/X們 例外頭在前（帽子的頭=帽）。
    # 共用修飾字會亂配：「電鍋」滑窗跟「電解質運動飲」的「電解」拿 50 分超過
    # Layer-4 門檻 40，自信地回錯商品（第12輪抓到）。共用頭名詞（電鍋 vs
    # 陶瓷不沾鍋的「鍋」）仍放行，clarify 建議近似品是好體驗。
    # 第16輪補強：共用字還必須位於商品核心名的「字尾」——中文複合詞頭名詞
    # 在末位，「冷氣」的頭「氣」出現在「蒸氣電熨斗」中段只是修飾字，曾誤配。
    if len(keyword) == 2:
        _ov = kw_set & core_set
        if len(_ov) == 1:
            _head = keyword[0] if keyword.endswith(("子", "們")) else keyword[-1]
            if _ov != {_head} or not core.endswith(_head):
                best = min(best, 39.0)

    return best


_EXTRA_NOISE = [
    "好像有", "好像", "感覺", "應該", "可能", "似乎", "有點",
    "怎麼", "是不是", "有沒有", "一下", "好嗎", "對吧",
    "呀", "啊", "耶", "喔", "吧", "欸", "嗎", "呢",
    # 鬆散口語填充詞（第19輪）：「就北倉進了大概50個耳機這樣」
    "大概", "這樣", "差不多", "左右", "那個", "然後", "就是",
    "幫我登記", "登記一下", "幫我記", "記一下", "差點忘了", "對了",
    "話說", "順便", "拜託", "麻煩", "唉", "嗯",
]

_WH_NOISE = ("北倉", "南倉", "中倉", "東倉", "西倉", "北區倉", "南區倉", "中區倉",
             "北區", "南區", "中區", "全倉", "所有倉")
_QTY_NOISE = ("多少", "幾個", "幾件", "多少個", "多少件", "還有", "剩多少", "剩幾個",
              "庫存", "數量", "查", "看看", "告訴我", "幫我查", "多少了", "多少啊")

# 完整的雜詞清單：把所有會汙染 keyword 的詞統一在此
_ALL_KEYWORD_NOISE = (
    # 倉庫名 + 前綴
    "北區倉的", "中區倉的", "南區倉的", "北倉的", "中倉的", "南倉的",
    "北區的", "中區的", "南區的", "北倉", "南倉", "中倉", "北區倉", "南區倉", "中區倉",
    "北區", "南區", "中區", "全倉", "所有倉", "全部的",
    # 數量/動作詞
    "還有多少件", "還有多少", "剩多少", "有多少", "有幾個", "剩幾個", "多少個", "多少件",
    "多少", "幾個", "幾件", "還有", "庫存量", "庫存查詢", "庫存", "數量", "剩餘",
    # 量詞尾巴（「咖啡機還有幾台」剝掉「還有」後殘留「幾台」害 fuzzy 歪掉）
    "幾台", "幾支", "幾瓶", "幾包", "幾盒", "幾罐", "幾組", "幾雙", "幾條", "幾箱",
    "幾張", "幾頂", "幾對", "幾套", "幾把", "幾袋", "幾捲", "幾杯", "幾顆", "幾粒",
    # 動作/查詢詞
    "查一下", "看一下", "幫我查", "告訴我", "查詢", "查", "看", "詢",
    # 填充/語氣詞
    "好像有", "好像", "感覺", "應該", "可能", "是不是", "有沒有", "有",
    "怎麼", "一下", "好嗎", "對吧", "呀", "啊", "耶", "喔", "吧", "欸", "嗎", "呢",
    "那個", "這個", "的", "了", "還", "剩", "有幾", "剩幾", "多少了", "多少啊",
    "啥",  # 「買耳機的通常還買啥」
    # 口語填充（「洗衣服用的那個」「裝水壺」）
    "用的那個", "用的", "那個", "還有沒有", "有沒有貨", "有現貨嗎", "有貨嗎",
    "現貨嗎", "夠不夠", "有庫存嗎", "有嗎", "多少錢", "怎麼樣", "如何",
    "狀況", "總共有", "目前", "現在", "幫我看", "幫我看一下", "幫偶",
    "目前為止", "到現在", "目前有", "看一下", "現在有",
    # RCA 雜訊
    "帳對不上", "對不上", "對不起來", "兜不攏", "帳不對", "怎麼少這麼多",
    "怎麼少", "為什麼少", "為什麼短少", "短少", "少貨", "是誰動的", "誰改的",
    "庫存差異", "差異", "扣帳異常", "異常", "短收", "誰動的",
    # 鬆散口語填充詞（第19輪：「就北倉進了大概50個耳機這樣」）
    # 注意：不放「個/進了/出了」等會傷商品名的字——C13b 的 _pre_clean 已先
    # 剝掉量詞+數字，這裡只清純填充詞。
    "大概", "這樣", "差不多", "左右", "然後", "就是", "話說", "順便",
    "幫我登記", "登記一下", "幫我記", "記一下", "差點忘了", "對了",
    "拜託", "麻煩", "唉", "嗯",
    # r17：「耳機的水位還健康嗎」抽成「耳機水位健康」比不到商品 → clarify
    "水位", "還健康", "健康",
    # r18：「嬰兒用品有哪些」的「用品/有哪些」、「給我看看s01的庫存」的「給我」
    "用品", "有哪些", "給我",
    # r21：「不是南倉 我要看北倉的耳機」kw 曾殘「不是我要耳機」找不到
    "不是", "我要看", "我要",
    # r18：「給我全部庫存的總表」「有多少種商品」——概覽詞不可殘留當商品名
    # （C13 曾重建出「全部 總表」kw → clarify 找不到；殘「全部」同病）
    "總表", "種商品", "全部庫存", "總庫存", "全店", "全部",
    "想知道", "想看看", "我想",
)

def _en_descriptor_hit(text: str) -> str:
    """英文功能描述句 → 商品關鍵字（包一層，避免各呼叫點重複 try/import）。"""
    try:
        from descriptor_en import descriptor_hit_en as _d
        return _d(text) or ""
    except Exception:
        return ""


_EN_Q_STOP_RE = _re.compile(
    r"\b(?:how|many|much|whats|what|is|are|the|a|an|of|do|does|"
    r"we|i|you|got|have|has|any|some|there|show|me|tell|give|"
    r"list|check|look|looking|see|find|get|left|remain|remaining|"
    r"stock|stocks|inventory|count|counts|on|hand|in|at|for|to|"
    r"from|with|now|currently|available|availability|status|"
    r"please|pls|quantity|qty|units?|level|levels|number|"
    r"warehouse|wh|north|central|south|total|still|right|"
    # r5-voice：疑問詞（`which items are running well` 剝完只剩 which →
    #   回「No item matching "which"」）。
    # ⚠️ **不收 why/when/where**——那三個是 RCA（why is X off）與期間查詢的
    #   關鍵意圖詞，剝掉會傷到 _RCA_INTENT_WORDS 之類的下游判斷。
    r"which|whose|whom|"
    # r15 #56/#38：連接詞/裸 top 沒剝——'and in south' 剝完剩 'and'
    #   → substring 命中 B**and** → Smart Fitness Band（全新連線也中）；
    #   'top -5 sellers' 的 top → lap**top**。
    r"and|or|but|nor|top|"
    r"hows|how's|what's|whats|its|it)\b", _re.I)
_EN_Q_STOP_INTENT_RE = _re.compile(
    # ⚠️ 這裡收的是「動詞+狀態」的**意圖片語**，剝掉才不會讓 running 之類的
    #   詞被當商品名。`running` 單獨留下會撈到 **Running** Shoes Men's
    #   （score 12）——語音實測 `which items are running well`（ASR 把
    #   'low' 聽成 'well'）整句被改成「跑鞋庫存」，clf 明明正確判了
    #   list_low_stock。同坑 1：英文短詞必然撞商品名。
    #   r5-voice：補「賣得好/賣得快」那側的狀態詞（原本只收缺貨側）。
    r"\b(?:running|getting|going|runs|gets|selling|sells|moving|moves)\s+"
    r"(?:out|low|short|down|empty|well|fine|good|great|fast|slow|"
    r"strong|steady|badly|poorly)\b",
    _re.I)


# ── EN build：英文「查詢語境」詞（C13b/C13c 的寫入意圖排除用）──────────
#   中文版有一長串排除詞（嗎/什麼/哪些/多少/上週/紀錄…），英文版沒有 →
#   查詢句被寫入校正劫走。⚠️ 只擋**沒有明確數量**的句子（C13c 的前提已含
#   `not _has_explicit_qty`），所以 'north received 50 mouse' 這種真寫入
#   句不受影響。
_EN_QUERY_CTX_RE = _re.compile(
    r"\b(?:which|what|whats|how many|how much|hows|any|anything|"
    r"records?|history|logs?|list|stats|statistics|summary|report|"
    r"trends?|volume|movements?|flows?|patterns?|rates?|breakdown|activity|"
    r"compare|comparison|versus|vs|more|less|most|least|total|"
    r"last week|this week|last month|this month|today|yesterday|"
    r"recently|lately|did we|do we|have we|was there|were there|"
    r"why|when|where|who)\b", _re.I)

_EN_WRITE_STOP_RE = _re.compile(
    r"\b(?:put|into|onto|came|come|coming|arrived|arrive|customer|returned|"
    r"return|returns|took|take|taken|got|goes|went|sent|send|out of|"
    r"add|added|adding|plus|minus)\b", _re.I)


# ── EN build（劇情批 r3 S1）：語音/快打的**黏字虛詞**還原 ────────────────
#   ASR 與快打常把虛詞黏成一塊：'howmany' / 'themouse' / 'howmuch' /
#   'whatabout' / 'isthere'。這些不是商品名，卻會被 OOV 判定當陌生商品
#   → 整句回「No item matching "howmany"」或 rejected。
#   ⚠️ 只還原**虛詞開頭**的黏字（how/what/the/is/are/do…），不動商品合成詞
#   （powerbank/yogamat 要留給 _en_fuzzy_keyword 的拆解層處理）。
_EN_GLUED_STOP_RE = _re.compile(
    r"\b(?:how(?=many\b|much\b|long\b)|what(?=about\b|is\b|are\b)|"
    r"the(?=mouse\b|earphones?\b|coffee\b|stock\b)|"
    r"is(?=there\b|it\b)|are(?=there\b)|do(?=we\b|you\b)|"
    # ⚠️ 'in(?=stock)' 只拆 'instock'；**不可**加 'ventory' ——
    #   `in(?=ventory)` 會在正常單字 **in**ventory 內部命中，把它咬成
    #   'in ventory' → 商品比對全毀（守衛 887，4 句錯字句掛掉）。
    #   所有 lookahead 前面都要有 \b 保護，且不可對應到真實單字的內部。
    r"can(?=you\b|i\b)|whats(?=the\b)|in(?=stock\b)|"
    # r17 #21/#24/#26/#30/#27：ASR 黏字第二批——whatsthe/lowstock/
    #   runninglow/whatcamein/earphonesstock（全部 not found 或概覽）。
    #   lookahead 規則同上：\b 起點、目標是完整詞。
    r"low(?=stock\b)|running(?=low\b)|what(?=camein\b|sthe\b)|"
    r"came(?=in\b)|show(?=me\b)|earphones(?=stock\b)|transfer(?=ten\b)|"
    r"compare(?=north\b|south\b)|daysof(?=cover\b)|days(?=ofcover\b))",
    _re.I)


def _en_unglue(text: str) -> str:
    """把黏在一起的虛詞拆開（'howmany' → 'how many'）。"""
    return _EN_GLUED_STOP_RE.sub(lambda m: m.group(0) + " ", text)


# ── EN build（劇情批 r4 S9）：英文數字詞 → 阿拉伯數字 ────────────────────
#   'five hundred yoga mat in north' / 'a dozen wireless mouse' 的數量抽不到
#   → 寫入句被當成查詢（實測回了庫存數字而非開卡）。
#   ⚠️ 語音接上後更重要：ASR 常吐 'fifty' 而非 '50'。
_EN_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
    "dozen": 12, "couple": 2, "pair": 2,
}
# ⚠️ 誤傷防護（實測踩到）：
#   ①'the one i mentioned' 的 one 是**指代詞**不是數字 → 前面接 the/that/this 不轉
#   ②'a pair of socks' 的 pair 是**商品單位** → 後面接 of 不轉
#   ③'couple'/'pair'/'dozen' 只在**明確數量語境**（前有 a/the 且後接名詞）才轉
_EN_NUM_RE = _re.compile(
    r"(?<!\bthe\s)(?<!\bthat\s)(?<!\bthis\s)(?<!\bwhich\s)"
    r"\b(?:a\s+)?((?:" + "|".join(_EN_NUM_WORDS) + r")"
    r"(?:[\s-]+(?:" + "|".join(_EN_NUM_WORDS) + r"))*)\b(?!\s+of\b)", _re.I)


def _en_words_to_num(text: str) -> str:
    """把英文數字詞換成阿拉伯數字（'five hundred'→500、'a dozen'→12）。"""
    def _conv(m):
        # ④ 裸 'one' 當**指代**時不轉（'the first one' / 'the most urgent one'
        #   / 句尾的 one）——數量語境的 one 後面一定接商品名。
        _g = m.group(1).lower()
        if _g == "one":
            _after = text[m.end():].strip()
            if not _after or not _re.match(r"[a-z]", _after, _re.I):
                return m.group(0)      # 句尾 → 指代
            _before = text[:m.start()].strip().lower()
            if _re.search(r"\b(?:first|second|third|last|other|next|same|"
                          r"worst|best|biggest|smallest|most|least|urgent|"
                          r"cheapest|newest|oldest)$", _before):
                return m.group(0)      # 形容詞 + one → 指代
        parts = _re.split(r"[\s-]+", m.group(1).lower())
        total = cur = 0
        for w in parts:
            v = _EN_NUM_WORDS.get(w)
            if v is None:
                return m.group(0)
            if v == 100:
                cur = (cur or 1) * 100
            elif v >= 20 and cur and cur < 20:
                return m.group(0)          # 'two twenty' 這種不合理組合不動
            else:
                cur += v
        total += cur
        return str(total) if total else m.group(0)
    return _EN_NUM_RE.sub(_conv, text)


def _en_query_core(text: str) -> str:
    """剝掉英文查詢虛詞，只留商品詞。

    ⚠️ 為什麼要抽成共用函式：`_en_fuzzy_keyword` 對**整句**會失效——
    虛詞（whats/the/count/hand）會進 _core_toks，被 OOV 防線判成陌生詞
    一票否決整句。實測：
      fuzzy('whats the lapptop case count') = ''      ← 整句
      fuzzy('lapptop case')                 = 'Laptop Bag'  ← 剝完
    原本這段剝詞是內嵌在 _extract_sku_keyword 快路徑裡，別處要用只能
    複製一份 → 必然不同步。抽出來讓所有呼叫端共用同一套。"""
    _c = _en_unglue(text)          # 黏字虛詞先拆（howmany → how many）
    _c = _EN_Q_STOP_INTENT_RE.sub(" ", _c)
    _c = _EN_Q_STOP_RE.sub(" ", _c)
    # 寫入句動詞 + 純數字（守衛 mv 回歸）：'put 100 mop into south' 的
    #   '100' 會撈到 Power Bank **10000**mAh / Coffee Filter **100**pcs /
    #   Sports Towel **100**x30cm → 四個同分 3 → 判歧義回空，商品抽不到。
    #   數量與寫入動詞都不是商品詞，比對前先剝掉。
    _c = _EN_WRITE_STOP_RE.sub(" ", _c)
    _c = _re.sub(r"\b\d+\b", " ", _c)
    return _re.sub(r"\s+", " ", _c).strip(" ?.!,")


def _en_fuzzy_keyword(core: str) -> str:
    """EN build：英文錯字 → 商品名（編輯距離模糊比對）。抓不準就回 ""。

    為什麼需要：match_items 是 substring 打分，英文**一個字母打錯就整個
    對不到**（pwerbank / keyyboard / crackees），回全店概覽＝答非所問。
    中文版對應的是 _phonetic_match（同音字），英文對應的是編輯距離。

    比對對象＝商品名的每個單詞 + alias_en 的別名鍵（俗稱也會被打錯，
    如 biscuits/crackees）。門檻從嚴，避免把陌生詞硬掰成商品：
      - 只比長度 ≥4 的詞（短詞編輯距離 1 就換一個意思：mop/map/mob）
      - 允許的距離隨長度放寬：4-5 字母容 1、6-8 容 2、9+ 容 2
      - 最佳與次佳必須指向**同一商品**，否則＝歧義不猜
    """
    import difflib
    import warehouse as _W
    if not core:
        return ""
    try:
        items = _W.state().items
    except Exception:
        return ""
    # 候選：商品名單詞 → 商品名；alias 鍵（含多詞片語的整串與單詞）→ 目標商品
    # ⚠️ 一個詞可能出現在多個商品名（bags → Drip Coffee Bags / Trash Bags /
    #   Laptop Bag），這裡只留**第一個**——下游的歧義判斷依賴一對一映射。
    cand: dict[str, str] = {}
    # 主檔名集合——用來辨識 cand 的值是「主檔名」還是「alias 值」（見下方
    #   假歧義修復：兩者可能指同一商品但字串不同）
    _ITEM_NAME_SET = {it["name"] for it in items}
    # 3 字母的商品詞（mat / bra / pan / fan / cup…）另存一份——主 cand 用
    #   len>=4 是為了避免短詞亂中，但**合成詞拆解**需要它們
    #   （'yogamat' 要拆成 yoga|mat，mat 不在 cand 就永遠拆不開，r3 實測）。
    _cand3: dict[str, str] = {}
    for it in items:
        for w in _re.split(r"[\s\-/]+", it["name"]):
            w = w.strip().lower()
            # 純數字/規格詞（2m、28cm、10000mah、5pcs）不當比對錨點
            if len(w) >= 4 and not any(c.isdigit() for c in w):
                cand.setdefault(w, it["name"])
            elif len(w) == 3 and not any(c.isdigit() for c in w):
                _cand3.setdefault(w, it["name"])
    _ALIAS_KEYS_EN: list[str] = []
    try:
        from alias_en import ALIAS_EN as _AL
        for _k, _v in _AL.items():
            _kl = _k.lower()
            if len(_kl) >= 4:
                cand.setdefault(_kl, _v)
                if len(_kl) >= 5:
                    _ALIAS_KEYS_EN.append(_kl)
            # ⚠️ 多詞別名**不拆單詞**：修飾詞單獨拆出來必然歧義
            #   （cordless 同時屬於 cordless mouse / cordless mop，
            #    workout 屬於 workout bra / mat / shirt / towel），
            #   先建立者贏 → 'cordless mop' 誤配 Wireless Mouse。
            #   單詞別名（earbuds/nappies）在上面那行已經收了。
    except Exception:
        pass
    if not cand:
        return ""
    keys = list(cand)

    # ── 多詞 alias **整串**優先（守衛第 11 輪的誤配根因）────────────────
    #   上面把多詞別名拆成單詞放進 cand，先建立者贏：
    #     cordless → Wireless Mouse（來自 "cordless mouse"）
    #     workout  → Sports Bra    （來自 "workout bra"）
    #   於是 'cordless mop' 被逐詞比對成 Wireless Mouse、'workout mat'
    #   成 Sports Bra——**連正確拼字都誤配**，比漏抓嚴重得多。
    #   → 先拿整串（含錯字）跟多詞別名比對，中了就直接用。
    try:
        from alias_en import ALIAS_EN as _AL2
        _multi = {k.lower(): v for k, v in _AL2.items() if " " in k}
        if _multi:
            _core_l = " ".join(core.lower().split())
            if _core_l in _multi:
                return _multi[_core_l]
            _near_m = difflib.get_close_matches(_core_l, list(_multi), n=1, cutoff=0.85)
            if _near_m:
                return _multi[_near_m[0]]
    except Exception:
        pass

    def _max_dist(n: int) -> int:
        return 1 if n <= 7 else 2

    # 誤傷防線：這些是**閒聊/搗蛋/非商品**常見詞，跟商品名差一兩個字母
    #   （weather↔water、chairs↔chair(Camping Chair)、robot、joke…）。
    #   模糊層把它們救成商品＝搗蛋句回商品卡，比漏抓更糟 → 明確擋掉。
    #   ⚠️ 不可放商品名裡真的有的詞（water/coffee/chair 是主檔用字，擋掉會
    #      傷到正常查詢）——只擋「非商品語境」的詞；weather/chairs 這類跟
    #      商品名近似的，靠上面 cutoff 0.85 + 長度差把關。
    _FUZZY_BLOCK = {
        "weather", "whether", "robot", "robots", "joke", "jokes",
        "feeling", "feelings", "hello", "hallo", "there",
        "pizza", "burger", "database", "data", "system", "server",
        "admin", "password", "instruction", "instructions", "developer",
        "mode", "everything", "thing", "things", "stuff", "item", "items",
        "product", "products", "order", "orders", "morning", "afternoon",
        "evening", "night", "today", "tomorrow", "thanks", "thank",
        "please", "sorry", "help", "hours", "open", "human", "person",
        "people", "name", "names", "price", "prices",
        # r15 #56：超短虛詞被 0.85 門檻放過——'and in south' 的 **and 配到
        #   band(0.857)** → 全新連線也回 Smart Fitness Band（追了一輪
        #   carry-over 才發現是純函式層誤配）。連接詞/介系詞永不是商品。
        "and", "the", "for", "with", "from", "into", "onto", "but",
        "our", "your", "their", "his", "her", "its", "was", "were",
        "are", "has", "had", "have", "any", "all", "per", "via",
        # r15 #27/#38 同款：'hows central **holding** up'→**Folding** Camping
        #   Chair(0.857)、'top -5 sellers' 的 top→laptop。口語動詞/副詞
        #   永不是商品名。
        "holding", "happened", "looking", "sitting", "going", "doing",
        "coming", "getting", "top", "hows", "whats", "wheres",
    }

    hits: list[str] = []
    _split_ok: set[str] = set()   # 靠合成詞拆解命中的 token（powerbank）
    for tok in _re.split(r"[\s\-/]+", core.lower()):
        tok = tok.strip(" ?.!,'\"")
        if len(tok) < 4 or any(c.isdigit() for c in tok):
            continue
        if tok in _FUZZY_BLOCK:
            continue
        if tok in cand:                      # 精確詞本來就中，不需模糊
            hits.append(cand[tok])
            continue
        # 合成詞（powerbank / powerbanks / usbcable）：訪客把兩個字黏在一起
        #   打，整串跟商品名任一單詞都不夠像 → 拆成兩段各自比對。
        #   兩段都要命中**同一個**商品才算，避免亂拆亂配。
        if len(tok) >= 7:
            _sp_hit = None
            for _cut in range(3, len(tok) - 2):
                _a, _b = tok[:_cut], tok[_cut:]
                # 3 字母商品詞（mat/bra/pan…）也要查 —— 'yogamat' 拆 yoga|mat
                _ah = (cand.get(_a) or cand.get(_a.rstrip("s"))
                       or _cand3.get(_a) or _cand3.get(_a.rstrip("s")))
                _bh = (cand.get(_b) or cand.get(_b.rstrip("s"))
                       or _cand3.get(_b) or _cand3.get(_b.rstrip("s")))
                # ⚠️ 合成詞**又打錯字**（pwerbank = powerbank 漏 o）：
                #   拆成 pwer|bank 時 bank 精確命中、pwer 要靠模糊。
                #   原本兩段都要求精確 → 這類永遠救不回（守衛 inv 長尾）。
                #   放寬成允許模糊，但**兩段必須指向同一商品**這個安全條件
                #   不變——實測 pwer|bank 兩段模糊都指向 Power Bank，
                #   一致性本身就是很強的證據（亂拆不可能兩段都中同一個）。
                if not (_ah and _bh):
                    if len(_a) >= 4 and not _ah:
                        _fa = difflib.get_close_matches(_a, keys, n=1, cutoff=0.80)
                        _ah = cand[_fa[0]] if _fa else None
                    if len(_b) >= 4 and not _bh:
                        _fb = difflib.get_close_matches(_b, keys, n=1, cutoff=0.80)
                        _bh = cand[_fb[0]] if _fb else None
                if _ah and _bh and _ah == _bh:
                    _sp_hit = _ah
                    break
            if _sp_hit:
                hits.append(_sp_hit)
                _split_ok.add(tok)   # 下面的 OOV 防線要認得它已經命中
                continue
        # difflib 先粗篩再用長度比例把關。cutoff 0.85 擋掉 weather→water、
        #   chairs→chair 這種「差一兩字母但語意完全不同」的詞；但實測常見
        #   錯字落在 0.83（coffue→coffee、filtes→filter、tushirt→shirt），
        #   剛好卡在門檻外 → 這些句子全退回全店概覽。
        #   → 主門檻維持 0.85；0.82-0.85 之間的降級候選另存，只有在
        #     「句中還有別的 token 指向同一商品」時才採用（同商品佐證），
        #     單獨一個模稜兩可的詞不放行。
        near = difflib.get_close_matches(tok, keys, n=3, cutoff=0.85)
        # ── alias 鍵（人工維護的俗稱表）放寬到 0.80 ────────────────────────
        #   'rimper'→'romper'(0.833) 卡在 0.85 外 → 整句回空。
        #   alias 鍵只有 186 個且是人工挑的俗稱，範圍遠小於主檔全詞，
        #   放寬風險低——**實測驗證**：守衛 noex 的 13 個 OOV 詞
        #   （hair/dryers/microwave/toothpaste/umbrellas/laptops/printer/
        #   shampoo/bicycle/chairs/pads/office/gaming）對 alias 鍵在 0.80
        #   門檻下**零誤中**，只有 rimper→romper 命中。
        #   ⚠️ 限 ≥5 字母：4 字母的編輯距離 1 就換一個意思。
        if not near and len(tok) >= 5:
            # r14+2（#21）：**正確拼寫的系統功能詞不做商品 fuzzy**——
            #   'transfers?' 曾被放寬層配到 'trainers'(0.80+) → 回 Running
            #   Shoes 庫存卡。功能詞的錯字由 _en_funcword_fix 負責，
            #   商品模糊層不該碰它們。
            if tok in ("transfer", "transfers", "movement", "movements",
                       "schedule", "schedules", "report", "reports",
                       "export", "exports", "record", "records",
                       "return", "returns", "receipt", "receipts"):
                continue
            near = [k for k in difflib.get_close_matches(
                tok, _ALIAS_KEYS_EN, n=3, cutoff=0.80) if k in cand]
            if near:
                log.info(f"[EN fuzzy] alias 鍵放寬 0.80: {tok!r} → {near[0]!r}")
        if not near:
            continue
        best = near[0]
        if abs(len(best) - len(tok)) > _max_dist(len(tok)):
            continue
        # 次佳若指向**不同商品**且分數同樣接近 → 歧義，不猜
        if len(near) > 1 and cand[near[1]] != cand[best]:
            r0 = difflib.SequenceMatcher(None, tok, best).ratio()
            r1 = difflib.SequenceMatcher(None, tok, near[1]).ratio()
            if r1 >= r0:
                continue
        # ⚠️ alias 值 vs 主檔名的**假歧義**：cand 的值有兩種來源——主檔名
        #   （"Cotton Plain T-shirt Men's"）與 alias 值（'Plain T-shirt'），
        #   兩者其實是**同一個商品**，但字串不同 → 投票時被當成兩個候選 →
        #   'plaiin tushirt'（plaiin→主檔名、tushirt→alias 值）判成歧義回空。
        #   → 統一正規化成主檔名再投票。
        _hit_name = cand[best]
        if _hit_name not in _ITEM_NAME_SET:
            try:
                _hn_m = _W.match_items(_hit_name)
                if _hn_m and _hn_m[0].get("score", 0) >= 4:
                    _hit_name = _hn_m[0]["item"]["name"]
            except Exception:
                pass
        hits.append(_hit_name)
    # ⚠️ 曾試過「0.82-0.85 降級候選 + 同商品佐證」想救『兩個 token 都打錯』
    #   的長尾（traash bags / plaiin tushirt）——只多修 1-2 句，卻因為撞名詞
    #   （sports/bags/coffee 出現在多個商品名）產生新誤配
    #   （sports bra → Electrolyte Sports Drink）。誤配比漏抓難看，已回退。
    #   這類長尾留給模型（補訓語料裡加雙錯字樣本）比規則層硬湊乾淨。
    if not hits:
        return ""
    # OOV 防線：句中有**主檔沒有的修飾詞**時不要硬救。
    #   'office chairs' 的 chairs 精確命中 Folding Camping Chair 的 chair，
    #   但 office 不是任何商品名的字 → 這是庫裡沒有的「辦公椅」，該誠實說
    #   沒有，不能回露營椅（守衛 noex 類期望的正是這個）。
    _core_toks = [t.strip(" ?.!,'\"").lower()
                  for t in _re.split(r"[\s\-/]+", core.lower())]
    _core_toks = [t for t in _core_toks if len(t) >= 4 and not any(c.isdigit() for c in t)]
    #   判準：**每個** ≥4 字母的實詞都要能對應到主檔（精確或模糊），
    #   有任何一個是全然陌生的詞 → 這是庫裡沒有的商品，不猜。
    #     office chairs / hair dryer / gaming chair：office/hair/gaming
    #       對不到任何主檔字 → 誠實回空（守衛 noex 期望）
    #     phonne coaer / usbc cablle：兩個 token 都模糊對得到 → 救回
    #   ⚠️ 這條同時解掉「單一 token 靠一個字母之差亂中」（hair→chair）：
    #      hair 雖模糊比得到 chair，但它是**唯一**實詞且是 OOV 商品的核心，
    #      所以另外要求：模糊救回的商品，其名稱必須有 ≥2 個字元級證據
    #      （token 長度 ≥5，或句中有第二個 token 也指向同一商品）。
    # ── 已達成共識的商品名，其**自己的詞**可當陌生詞的接地證據 ──────────
    #   'Mosquuito Rpellent Soray'：mosquuito/rpellent 兩個 token 都指向
    #   Mosquito Repellent（共識明確），只有 soray 對全主檔比不到 0.85
    #   → 被這條防線一票否決回空。但 soray→**spray** 是 0.80，而 spray
    #   正是該商品名裡的詞＝這是錯字不是 OOV 修飾詞。
    #   安全前提（三個條件同時成立才放行，缺一不可）：
    #     ①已有 **≥2 個** token 指向同一商品（共識，不是單一 token 硬猜）
    #     ②陌生詞對**該商品名自己的詞**達 0.80（範圍極小才敢放寬）
    #   OOV 案例不受影響：'office chairs' 只有 chairs 一個 token 命中
    #   （不滿足①）；'hair dryer' 兩個 token 都對不到主檔（同樣不滿足①）。
    _consensus_name = ""
    if hits:
        from collections import Counter as _Cn
        _hc = _Cn(hits).most_common(1)
        if _hc and _hc[0][1] >= 2:
            _consensus_name = _hc[0][0]
    _consensus_words = set()
    if _consensus_name:
        for _cw in _re.split(r"[\s\-/]+", _consensus_name.lower()):
            _cw = _cw.strip(" ?.!,'\"")
            if len(_cw) >= 3 and not any(c.isdigit() for c in _cw):
                _consensus_words.add(_cw)
    _unknown_tok = False
    for t in _core_toks:
        if t in _FUZZY_BLOCK or t in cand or t in _split_ok:
            continue
        if not difflib.get_close_matches(t, keys, n=1, cutoff=0.85):
            # alias 鍵放寬 0.80（同上方 near 的放寬，兩處必須同步——
            #   'rimper' 在 near 已靠 alias 對到 romper，但這道防線用
            #   0.85 對全主檔比不到 → 又把它擋掉）
            if len(t) >= 5 and difflib.get_close_matches(
                    t, _ALIAS_KEYS_EN, n=1, cutoff=0.80):
                continue
            if _consensus_words and difflib.get_close_matches(
                    t, list(_consensus_words), n=1, cutoff=0.80):
                log.info(f"[EN fuzzy] 陌生詞 {t!r} 對共識商品 "
                         f"{_consensus_name!r} 接地成立 → 視為錯字")
                continue
            # 共識已成立時，陌生詞對**任一主檔詞**達 0.80 也算錯字：
            #   'Mosquuito Rpellent Soray' 的共識是 Mosquito Repellent
            #   **Refill**（cand 只留第一個商品），soray 對 Refill 的詞
            #   接地不了，但它對主檔的 **spray**（同家族另一款）是 0.80。
            #   有 ≥2 token 共識當前提，這種放寬不會讓 OOV 案例過關
            #   （office/dryer 對全主檔最高只有 0.31/0.44）。
            if _consensus_name and difflib.get_close_matches(
                    t, keys, n=1, cutoff=0.80):
                log.info(f"[EN fuzzy] 陌生詞 {t!r} 在共識 "
                         f"{_consensus_name!r} 下對主檔接地(0.80) → 視為錯字")
                continue
            _unknown_tok = True
            break
    if _unknown_tok:
        return ""
    # 單一短詞（≤4 字母）靠模糊硬中 → 證據不足（hair→chair、mose→mouse
    #   前者是 OOV 後者是錯字，長度是唯一可靠的區分訊號）
    _fuzzy_only = [t for t in _core_toks if t not in cand and t not in _split_ok]
    if len(_core_toks) == 1 and _fuzzy_only and len(_core_toks[0]) <= 4:
        return ""
    # 多個 token 指向同一商品（Wireeless Bluetouth Earpones）→ 該商品；
    # 指向不同商品 → 取出現最多次者，仍平手就不猜
    from collections import Counter
    _c = Counter(hits).most_common(2)
    if len(_c) > 1 and _c[1][1] == _c[0][1]:
        # ── 平手決勝：看哪個候選被**句中最多 token** 支持 ────────────────
        #   投票是「每個 token 只投 top-1」，太粗：
        #     'traash bags' → traash 投 Trash Bags、bags 投 **Coffee** Bags
        #     （bags 對兩者同樣合理，卻只投給其中一個）→ 1:1 平手 → 回空。
        #   但 Trash Bags 這個候選其實**兩個 token 都支持**（traash≈trash、
        #   bags=bags）＝證據明顯較強。改用「候選名的詞被句中幾個 token
        #   命中（精確或模糊≥0.8）」當決勝局，仍平手才真的不猜。
        #   ⚠️ 只在平手時啟用，不改變原本的單一贏家行為（零回歸面）。
        def _support(_name: str) -> int:
            _nw = [w.strip(" ?.!,'\"").lower()
                   for w in _re.split(r"[\s\-/]+", _name.lower())]
            _nw = [w for w in _nw if len(w) >= 3 and not any(c.isdigit() for c in w)]
            _n = 0
            for _t in _core_toks:
                if _t in _nw or _t.rstrip("s") in _nw or any(
                        _t.rstrip("s") == w.rstrip("s") for w in _nw):
                    _n += 1
                elif difflib.get_close_matches(_t, _nw, n=1, cutoff=0.80):
                    _n += 1
            return _n
        _s0, _s1 = _support(_c[0][0]), _support(_c[1][0])
        if _s0 != _s1:
            _win = _c[0][0] if _s0 > _s1 else _c[1][0]
            log.info(f"[EN fuzzy 平手決勝] {_c[0][0]!r}({_s0}) vs "
                     f"{_c[1][0]!r}({_s1}) → {_win!r}")
            return _win
        return ""
    return _c[0][0]


def _extract_sku_keyword(text: str) -> str:
    """從任意句子抽出最可能的 SKU keyword。
    分層清理 → 精準匹配 → fuzzy 滑窗 → 字元重疊。"""
    import warehouse as _W

    try:
        all_names = [it["name"] for it in _W.state().items]
    except Exception:
        all_names = []

    if not all_names:
        return text.strip()

    # ── EN build 英文快路徑（這支被呼叫 96 次，是全系統中樞）──────────────
    #   下面各層是中文導向（剝中文雜詞、中文滑窗），對英文句會回傳整句或虛詞
    #   （實測 'whats about to run out' → 'to'、'this weeks shipments' → 整句），
    #   校正層/C18 再拿它去 match_items 誤配 → 把正確的 low_stock/compare 改成
    #   庫存查詢。英文改走 match_items（已驗證對英文含錯字都準），並遵守
    #   「不確定不猜」：分數不足或同分並列 → 回空字串，讓上層走無 keyword 路徑。
    if _is_mostly_english(text):
        # 先把英文俗稱正規化成能命中主檔的字串（alias_en：英文版對應中文
        #   _TYPO_NORM 的角色）。守衛庫抓到 61 個俗稱對不到/誤配：
        #   battery pack→Craft Beer、toilet paper→Coffee Filter、sneakers→無。
        try:
            from alias_en import normalize_alias_en as _norm_en
            text = _norm_en(text)
        except Exception:
            pass
        # ⚠️ 用**整句**比對會被查詢虛詞稀釋：match_items 是逐 token 加分，
        #   'mop on hand' 的 on/hand 會低分撈到一堆不相干商品，讓 Electric Mop
        #   從單看 'mop' 的 8 分掉到 3 分（< 4 門檻）→ 回空 → 上層沒 keyword →
        #   全店概覽。守衛 inv 類大量 FAIL 都是這個成因（mop/biscuits/錯字句）。
        #   → 先剝掉查詢虛詞，只留商品詞再比對。
        # 剝詞已抽成共用函式 _en_query_core（原本內嵌在這裡，別處要用只能
        #   複製一份 → 必然不同步；見該函式註解）
        _en_core = _en_query_core(text)
        # ── 功能描述句**優先**（要在 match_items 之前）────────────────────
        #   'something to clean teeth' 的 clean 會讓 match_items 撈到
        #   Rubber Cleaning Gloves（分數還過 4 分門檻）→ 描述層永遠輪不到，
        #   而且誤配成清潔手套。描述表命中＝訪客講的是**功能**不是商品名，
        #   應該直接用描述的目標。
        #   ⚠️ 但**句中已有明確商品名**時不可搶（'yoga mat stock' 會中
        #   yoga+mat 規則，雖然目標一樣，但 'coffee beans stock' 會被
        #   'makes coffee' 類規則導向咖啡機）→ 先確認沒有扎實的商品名比對。
        try:
            _m_pre = _W.match_items(_en_core) if _en_core else []
            _solid_pre = bool(_m_pre and _m_pre[0].get("score", 0) >= 8)
        except Exception:
            _solid_pre = False
        if not _solid_pre:
            try:
                from descriptor_en import descriptor_hit_en as _dsc_pre
                _d_pre = _dsc_pre(text)
                if _d_pre:
                    log.info(f"[EN descriptor] {text!r} → {_d_pre!r}")
                    return _d_pre
            except Exception:
                pass
        try:
            # 先用剝乾淨的核心詞比對；剝到空（整句都是虛詞）才退回整句
            _m_en = _W.match_items(_en_core) if _en_core else _W.match_items(text)
        except Exception:
            return ""
        # ── 英文錯字模糊層（守衛 inv 類最大宗）──────────────────────────
        #   match_items 是 substring 打分，**一個字母打錯就完全對不到**：
        #   pwerbank / keyyboard / crackees / Wireeless Bluetouth Earpones
        #   全部 match 到空 → 回全店概覽。中文版靠 _phonetic_match 救同音字，
        #   英文對應的是**編輯距離**（英文版當初關掉發音層是對的——中文拼音
        #   對英文亂救；但不能什麼都不補）。
        #   只在精確比對失敗時啟用，且門檻從嚴（見下），避免亂救。
        if not _m_en or _m_en[0].get("score", 0) < 4:
            _fz = _en_fuzzy_keyword(_en_core or text)
            if _fz:
                log.info(f"[EN fuzzy] {(_en_core or text)!r} → {_fz!r}")
                return _fz
            # 英文**功能描述句**（訪客講不出商品名時的招牌能力）：
            #   'something to clean teeth' / 'the machine that makes coffee'。
            #   中文版靠 _DESCRIPTOR_ALIASES，但那張表回傳中文名、英文版已
            #   關掉（會湊出中英混血）→ descriptor_en 是英文版的對應物。
            try:
                from descriptor_en import descriptor_hit_en as _dsc_en
                _d = _dsc_en(text)
                if _d:
                    log.info(f"[EN descriptor] {text!r} → {_d!r}")
                    return _d
            except Exception:
                pass
            # ── r10：詞典把關的**孤立單 token** 錯字（守衛最後一句 scks）──
            #   _en_fuzzy_keyword 處理的是「詞組」錯字（traash bags / powr
            #   bank），對**孤立單詞**對不到（實測 'socks' 可以、'scks' 不行）。
            #   這道只在它失敗後才問，且要求 token 不是英文真詞——
            #   `hair`/`shampoo`/`bicycle`/`chairs` 是真詞 → 維持誠實查無。
            _tk = _en_typo_keyword(text)
            if _tk:
                log.info(f"[EN typo-token] {text!r} → {_tk!r}")
                return _tk
            return ""
        if len(_m_en) > 1 and _m_en[1].get("score", 0) >= _m_en[0].get("score", 0):
            # 同分並列＝歧義，不猜。但**不能回空字串**——回空的話下游沒 keyword，
            #   query_inventory 會給「全店 60 商品概覽」，訪客問 'stock of coffee'
            #   卻收到一份不相干清單（實測破口）。中文版 Layer 2.5 的作法是
            #   **回共同片段**（「咖啡」），讓 query_inventory 走既有的「疑似清單
            #   ＋各候選庫存概況」路請訪客選 → 英文對齊同一行為。
            #   條件：剝乾淨的核心詞必須是**所有並列候選的共同 substring**，
            #   才確定它是通稱（coffee×5 / mosquito repellent×2）而非碰巧撞分。
            _tied = [r for r in _m_en
                     if r.get("score", 0) >= _m_en[0].get("score", 0)]
            _stem = (_en_core or "").strip().lower()
            # ⚠️ 守衛回歸（869 vs 873）：光驗「是共同 substring」不夠——虛詞殘片
            #   也會滿足（'put 100 mop into south' 剝完剩 'op'、'increase safety
            #   stock by 30' 剩 'by'）→ 回出 `"op" matches 3 items` 這種醜回答。
            #   詞幹必須是**真的通稱詞**：長度夠 + 不是虛詞 + 在原句以完整詞出現。
            _STEM_STOP = {
                "by", "op", "in", "on", "at", "to", "of", "up", "an", "as",
                "is", "it", "or", "so", "we", "do", "no", "my", "me", "the",
                "and", "for", "all", "any", "new", "old", "put", "get", "set",
                "add", "how", "why", "who", "was", "are", "has", "one", "two",
            }
            # ── 並列時先問模糊層（錯字句的正解常在這裡）─────────────────
            #   'traash bags' 的 match_items 是 Laptop Bag / Coffee Bags /
            #   Trash Bags **三個同分 4**（都只靠 bags 撈到）→ 判歧義回空，
            #   但模糊層明確知道 traash≈trash → Heavy-duty Trash Bags。
            #   原本 fuzzy 只在「分數 <4」時才被呼叫，同分並列這條路徑
            #   **完全跳過模糊層**＝錯字句永遠救不回來（守衛 inv 長尾主因）。
            #   安全條件：fuzzy 的答案必須**就在並列候選裡**（等於用 fuzzy
            #   在平手候選中做決勝，不是另起爐灶猜一個新商品）。
            #   ⚠️ 但**只在原句真的有錯字時**才用 fuzzy 決勝——否則會把
            #   「真歧義」也一併猜掉：'stock of coffee' 有 5 個咖啡商品、
            #   'mosquito repellent' 有 Spray/Refill 兩款，訪客講的就是
            #   通稱，正確行為是**列候選反問**（user 定調的不確定不猜）。
            #   實測回歸：這段沒設條件時，那些句子變成靜默猜第一個。
            #   判準：core 的每個實詞都精確命中主檔 → 沒有錯字 → 是真歧義。
            _core_toks_tie = [w.strip(" ?.!,'\"").lower()
                              for w in _re.split(r"[\s\-/]+", (_en_core or text))]
            _core_toks_tie = [w for w in _core_toks_tie if len(w) >= 3]
            _item_words_tie = set()
            for _r_tie in _tied:
                for _w_tie in _re.split(r"[\s\-/]+", _r_tie["item"]["name"].lower()):
                    _w_tie = _w_tie.strip(" ?.!,'\"")
                    if len(_w_tie) >= 3:
                        _item_words_tie.add(_w_tie)
            _has_typo = any(w not in _item_words_tie and w.rstrip("s") not in _item_words_tie
                            for w in _core_toks_tie)
            try:
                _fz_tie = _en_fuzzy_keyword(_en_core or text) if _has_typo else ""
            except Exception:
                _fz_tie = ""
            if _fz_tie and any(r["item"]["name"] == _fz_tie for r in _tied):
                log.info(f"[EN ambiguous→fuzzy] {len(_tied)} 並列 → 模糊層決勝 {_fz_tie!r}")
                return _fz_tie
            if (_stem and len(_stem) >= 4 and _stem not in _STEM_STOP
                    and _re.search(rf"\b{_re.escape(_stem)}\b", text, _re.I)
                    and all(_stem in r["item"]["name"].lower() for r in _tied)):
                log.info(f"[EN ambiguous] {_stem!r} → {len(_tied)} 並列候選，回詞幹讓下游列清單")
                return _stem
            return ""
        return _m_en[0]["item"]["name"]

    # ── Layer 1: 完整雜詞剝除，取乾淨片段 ──
    cleaned = text
    # 按長度倒序剝（先剝長詞，避免「北區倉的」被「北區」先吃掉）
    noise_sorted = sorted(_ALL_KEYWORD_NOISE, key=len, reverse=True)
    for w in noise_sorted:
        cleaned = cleaned.replace(w, " ")
    cleaned = " ".join(cleaned.split()).strip()

    # ── Layer 2: 精準 substring match ──
    for src in (cleaned, text):
        if not src:
            continue
        hits = [n for n in all_names if n in src]
        if hits:
            return max(hits, key=len)

    # ── Layer 2.5: 歧義短稱不猜（user 原則 2026-07-16「不喜歡用猜的」）──
    #   cleaned 是 ≥2 個商品名的共同片段（咖啡×5/運動×4/露營×4/電動×2/嬰兒×3）
    #   → 這是「模糊短稱」不是錯字，Layer 3/4 硬取最高分＝亂猜（「咖啡還剩多少」
    #   曾靜默對應到濾掛咖啡）。回原片段讓下游 query_inventory 列疑似清單
    #   （含各候選庫存概況）請訪客選。唯一命中（瑜珈→瑜珈墊）不受影響照樣直達。
    if cleaned and len(cleaned) >= 2:
        _contain_hits = [n for n in all_names if cleaned in n]
        if len(_contain_hits) == 1:
            return _contain_hits[0]
        if len(_contain_hits) >= 2:
            return cleaned
        # r43：整串比不到時逐 token 比（「咖啡 賣得怎樣」的「賣得怎樣」不在噪音表，
        # 整串 containment 落空 → 曾掉到 Layer4 fuzzy 硬猜濾掛咖啡）。token 唯一命中
        # →取全名；token 歧義（咖啡×5/露營×4）→回該 token 不猜，讓下游聚合或列清單。
        for _tk in cleaned.split():
            if len(_tk) < 2:
                continue
            _tk_hits = [n for n in all_names if _tk in n]
            if len(_tk_hits) == 1:
                return _tk_hits[0]
            if len(_tk_hits) >= 2:
                return _tk

    # ── Layer 3: 商品名 part 在 text 中 ──
    for src in (cleaned, text):
        if not src:
            continue
        part_hits = []
        for n in all_names:
            parts = [p for p in n.split() if len(p) >= 2]
            match_len = max((len(p) for p in parts if p in src), default=0)
            if match_len >= 2:
                part_hits.append((match_len, n))
        if part_hits:
            return max(part_hits)[1]

    # ── Layer 4: _fuzzy_score（剝規格 + 雙向滑窗 + 字元重疊）──
    # 弱配對防呆（2026-07-08，user「隨便一句就中 BUG」系統掃描：90 假商品 36
    # 被硬配）：核心判準＝「最長連續共同子串（LCS）」。真配對的 keyword 一定
    # 含商品的核心名詞連續 ≥2 字（藍牙耳機/帳篷/悶燒罐）；假配對只靠單一共通
    # 字（牙線↔耳機的「線」→耳、盤子↔鍵盤的「盤」）LCS=1。門檻維持 40（保留
    # 雜訊容錯，守衛庫「藍牙耳機月進出貨」這類帶雜訊 keyword 仍命中），但要求
    # LCS≥2 且該連續子串不是純開頭材質修飾詞（玻璃/不鏽鋼…擋「玻璃清潔劑↔
    # 玻璃保鮮盒」）。配不到→回原詞→_detect_oov clarify 誠實引導。
    _MOD_HEADS = ("玻璃", "不鏽鋼", "陶瓷", "電動", "無線", "全自動", "蒸氣",
                  "抗菌", "天然", "強力", "彈力", "彈性", "輕量", "折疊", "機能",
                  "純棉", "羊毛", "牛仔", "防曬", "高蛋白", "電解質", "迷你",
                  "桌上型", "嬰兒", "經典", "綜合", "蜂蜜", "精釀", "三層抽取",
                  "露營", "登山", "野炊")

    def _lcs_len(a: str, b: str) -> tuple[int, str]:
        m = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
        best, end = 0, 0
        for i in range(len(a)):
            for j in range(len(b)):
                if a[i] == b[j]:
                    m[i + 1][j + 1] = m[i][j] + 1
                    if m[i + 1][j + 1] > best:
                        best, end = m[i + 1][j + 1], j + 1
        return best, (b[end - best:end] if best else "")

    def _fuzzy_grounded(src_t: str, name: str) -> bool:
        """fuzzy 命中是否接地：連續共同子串 ≥2 且非純開頭修飾詞。
        （user 2026-07-08 定案「嚴格優先，寧錯殺少數真詞絕不亂答」：只靠單一
        共通字的弱配對一律擋，回原詞讓 _detect_oov 走 clarify 誠實引導。
        代價＝「帽子」這種單字通稱會 clarify「你是指遮陽帽還是毛帽」，可接受。）"""
        n_len, sub = _lcs_len(src_t, name)
        if n_len < 2:
            return False
        # r43：連續子串必須含中文字——「北倉吃得下100箱」的「100」跟規格「100x30cm」
        # LCS=3 曾接地成運動毛巾（純數字/字母沾邊不算真配對）
        if not any("一" <= c <= "鿿" for c in sub):
            return False
        core = name.split()[0]
        # 連續子串恰好是「開頭材質修飾詞」→ 只靠修飾詞沾邊，不接地
        if any(core.startswith(m) and sub == m for m in _MOD_HEADS):
            return False
        return True

    for src in (cleaned, text):
        if not src or len(src) < 2:
            continue
        scored = sorted(
            [(s, n) for n in all_names
             if (s := _fuzzy_score(src, n)) >= 40 and _fuzzy_grounded(src, n)],
            reverse=True,
        )
        if scored:
            return scored[0][1]

    return cleaned if len(cleaned) >= 2 else ""


_CN_NUM = {"零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(s: str):
    """把中文數字字串轉阿拉伯整數，支援 1-9999 的常見口語（三、十、十五、
    二十、一百二十、一千、兩千五百）。無法解析回 None。展場訪客講「三箱」
    「五十個」「一千件」很自然，量詞抽取原本只認阿拉伯數字會全漏。"""
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total = 0      # 已結算的部分（千/百段）
    section = 0    # 當前累積的「十位以下」段
    digit = 0      # 剛讀到的個位數字
    last_unit = 0  # 最後出現的單位值（口語尾數用：「一百五」=150 不是 105，r20）
    for ch in s:
        if ch == "億":
            # 「一億個耳機」搗蛋數字（r19）——解析出真值讓上限防呆接手
            sec = total + section + digit
            total = (sec or 1) * 100000000
            section = 0; digit = 0; last_unit = 100000000
        elif ch == "萬":
            # 「十萬」曾被抽成 10（萬不認得、regex 只吃到「十」）→ 設定值差 4 個
            # 數量級還開出 183 項確認卡（r17）
            sec = total + section + digit
            total = (sec or 1) * 10000
            section = 0; digit = 0; last_unit = 10000
        elif ch == "千":
            section += (digit or 1) * 1000
            total += section; section = 0; digit = 0; last_unit = 1000
        elif ch == "百":
            section += (digit or 1) * 100
            digit = 0; last_unit = 100
        elif ch == "十":
            section += (digit or 1) * 10
            digit = 0; last_unit = 10
        elif ch == "零":
            # 「一百零五」＝105：零之後的尾數是字面個位
            digit = 0
            last_unit = 0
        elif ch in _CN_NUM:
            digit = _CN_NUM[ch]
        else:
            return None
    # 口語尾數（r20：「一百五」曾算成 105 寫進設定）：結尾殘留個位數且前面
    # 有單位 → 乘上該單位的 1/10（一百五=150、兩千五=2500、三萬五=35000）
    if digit and last_unit >= 10:
        result = total + section + digit * (last_unit // 10)
    else:
        result = total + section + digit
    return result if result > 0 else None


# 數字部分：阿拉伯 or 中文，用於 manage_config 的 value 抽取
_NUM_PART = r'([0-9]+(?:\.[0-9]+)?|[零一二兩三四五六七八九十百千萬億]+)'  # r26：支援小數（倍數1.5）


def _extract_config_value(user_text: str):
    """從句子抽 manage_config 的設定值，回傳字串（"+30"/"-15"/"100"）或 None。
    同時支援阿拉伯與中文數字（2026-07-02：「改成五天」「調到一百」原本中文
    數字整段漏抽）。相對值（加/提高/降低）帶正負號，絕對值（改成/調到）純數字。"""
    import re as _re
    # 「一天半/7天半」帶半的口語（r21：「前置天數加一天半」曾抽成 +1=數值不精確）
    # → 回 None 讓上層 clarify 追問
    if _re.search(_NUM_PART + r"(?:天|件|個)?半", user_text):
        return None
    # 「到」結尾的動詞是絕對值語氣（調升到40=設成40，不是+40），要在相對值之前檢查
    # （第11輪抓到：「調升到40」的 rel/abs 都比對不到，value 整個漏抽）
    # 「動詞+到」是絕對值語氣（調高到100=設成100，不是+100）。RPI5 v21 抓到
    # 「調高到100」「拉高到」漏抽——把所有「(調|提|拉|升)+高?+到」都收進絕對值。
    _abs_to = _re.search(r"(?:調升到|升到|調降到|降到|調到|改到|拉到|調整到|"
                         r"調高到|提高到|拉高到|升高到|調低到|降低到|設到|設定到|"
                         # 「訂在25/定在25」「縮短到3天」也是絕對值語氣（conv100-r5）
                         # 「拉長到14天/延長到N」「歸一百」（conv100-r6）
                         r"訂在|訂為|定在|定為|縮短到|縮到|拉長到|延長到|加長到|壓到|歸)\s*"
                         + _NUM_PART, user_text)
    _rel_pos = _re.search(r"(?:[加+]|提高|提升|調高|調升|上修|上調|高)\s*" + _NUM_PART, user_text)
    _rel_neg = _re.search(r"(?:[降減]|調低|調降|下修|下調|低)\s*" + _NUM_PART, user_text)
    # 「設定成/設定到」補進絕對值（原本只有「設成/設為/設定為」，RPI5 v21
    # 「前置天數設定成7天」漏抽）
    _abs = _re.search(r"(?:改成|設成|設為|改為|設定為|設定成|調成|調整成|調到|改到|調整為|縮短成|縮成|改回|改|設)\s*"
                      + _NUM_PART, user_text)
    if _abs_to:
        g = _abs_to.group(1)
        n = g if "." in g else _cn_to_int(g)   # r26：小數直傳（倍數1.5）
        return str(n) if n is not None else None
    if _rel_pos:
        g = _rel_pos.group(1)
        n = g if "." in g else _cn_to_int(g)
        return f"+{n}" if n is not None else None
    if _rel_neg:
        g = _rel_neg.group(1)
        n = g if "." in g else _cn_to_int(g)
        return f"-{n}" if n is not None else None
    if _abs:
        g = _abs.group(1)
        n = g if "." in g else _cn_to_int(g)
        return str(n) if n is not None else None
    # ── EN build：英文設定值（原全中文動詞 → 英文設定句 value 整個漏抽，
    #    LLM 幻覺出的值沒有東西可以覆蓋。`set Power Bank 10000mAh safety
    #    stock to 100` LLM 抽成 '10000mAh'（商品名裡的規格），真值 100 在
    #    句尾）。用「**最後一個** to/at/= + 數字」——設定值總在句尾，
    #    商品名裡的規格數字（10000mAh/2M/28cm）在前面，且要求數字後面
    #    不接單位字母才算（排除 10000mAh）──
    _en_abs = _re.findall(
        r"(?:set(?:\s+\w+)*?\s+to|change(?:\s+\w+)*?\s+to|to|at|=|make\s+it)\s+"
        r"([0-9]+(?:\.[0-9]+)?)\s*(?:days?|units?)?\b(?![A-Za-z])",
        user_text, flags=_re.I)
    # 相對值：動詞跟數字常被商品名隔開（`increase yoga mat safety stock by 20`）
    #   → 用 by/to 錨定，動詞與 by 之間允許跨距。「by」是英文相對值的固定標記。
    _en_rel_pos = _re.search(
        r"(?:increase|raise|bump|add|up)\b[^0-9]{0,40}?\bby\s+"
        r"([0-9]+(?:\.[0-9]+)?)\b(?![A-Za-z])", user_text, flags=_re.I) \
        or _re.search(r"(?:increase|raise|bump)\s+(?:it\s+)?"
                      r"([0-9]+(?:\.[0-9]+)?)\b(?![A-Za-z])",
                      user_text, flags=_re.I)
    _en_rel_neg = _re.search(
        r"(?:decrease|reduce|lower|drop|cut|down)\b[^0-9]{0,40}?\bby\s+"
        r"([0-9]+(?:\.[0-9]+)?)\b(?![A-Za-z])", user_text, flags=_re.I) \
        or _re.search(r"(?:decrease|reduce|lower|cut)\s+(?:it\s+)?"
                      r"([0-9]+(?:\.[0-9]+)?)\b(?![A-Za-z])",
                      user_text, flags=_re.I)
    if _en_rel_pos:
        return f"+{_en_rel_pos.group(1)}"
    if _en_rel_neg:
        return f"-{_en_rel_neg.group(1)}"
    if _en_abs:
        return _en_abs[-1]
    return None


def _config_item_kw(user_text: str) -> str:
    """從 config 句抽商品名（「瑜珈墊的安全庫存加20」→「瑜珈墊」）。
    LLM 幾乎不會把商品塞進 manage_config 參數，導致影響範圍變全部商品 183 項
    （conv100-r5）。剝掉設定詞/動詞/倉名/數字後 fuzzy 比對，比不到真商品回空字串。"""
    import re as _re_ci
    import warehouse as _W_ci
    t = user_text
    # 長詞優先剝（r17 回歸抓到：單字「設」先剝掉害「設定給我看」殘「定給」
    # 誤判成未知商品）——排序一次解決所有「短詞破壞長詞」的順序問題
    for w in sorted(_CONFIG_KEY_WORDS + _CONFIG_SET_WORDS + (
            "安全", "水位", "庫存", "天數", "前置", "設定", "警戒",
            "北倉", "中倉", "南倉", "北區", "中區", "南區", "全部", "所有", "三倉",
            "幫我", "麻煩", "請", "把", "的", "商品", "全部商品",
            # 副詞/填充（r17：殘句判斷「有指名商品但不存在」時要先排除這些，
            # 「中倉安全水位全面調升10」的「全面」不是商品名）
            "全面", "整體", "一律", "通通", "統一", "全店", "整個", "順便",
            "現在", "目前", "大概", "左右", "一下", "確認", "看看", "可以", "嗎",
            "幫", "我", "你", "請問", "那個", "這個", "喔", "啊", "呢", "欸", "就",
            "是多少", "設多少", "多少", "是", "哪個", "哪些", "什麼", "怎麼",
            "如何", "要", "給我看", "秀給我", "列給我", "告訴我", "給我", "看", "給",
            # 剝詞順序殘留（「前置天數」先剝掉害「補貨前置」比不到 →「補貨」
            # 殘留誤判成未知商品；「設定成」剝掉「設定」殘「定成」）
            "補貨", "設定成", "定成", "成", "為", "到"), key=len, reverse=True):
        t = t.replace(w, " ")
    t = _re_ci.sub(r'[0-9]+|[零一二兩三四五六七八九十百千萬億]+|[天件個]', ' ', t)
    kw = _extract_sku_keyword(t.strip())
    # 注意：_extract_sku_keyword 命中商品時回的是「全名」（可能含空格，如
    # 「瑜珈墊 6mm」），不能拿空格當雜訊判準——雜訊靠 match 低分濾掉即可
    _resid = max((seg for seg in t.split()), key=len, default="")
    if not kw:
        # 剝完設定詞後仍殘留 ≥2 字的連續中文塊 = 有指名商品但比不到（「吹風機
        # 安全庫存設成30」曾 fallback 成全部商品 183 項確認卡，r17）→ 回
        # sentinel 讓 tools_v2.manage_config 誠實說找不到，不可默默改全庫
        if len(_resid) >= 2 and _re_ci.fullmatch(r"[一-鿿]+", _resid):
            return f"__unknown__:{_resid}"
        return ""
    m = _W_ci.match_items(kw)
    if m and m[0].get("score", 0) >= 3:
        return kw
    if len(_resid) >= 2 and _re_ci.fullmatch(r"[一-鿿]+", _resid):
        return f"__unknown__:{_resid}"
    return ""


_CAT_GROUND_WORDS = {
    "electronics": ("電子", "3c"), "appliance_kitchen": ("家電", "廚具", "廚房"),
    "food_beverage": ("食品", "飲料"), "daily_goods": ("日用", "生活用品"),
    "apparel": ("服飾", "衣服", "服裝"), "sports": ("運動", "露營", "戶外", "健身"),
}

# ── EN build：英文類別詞表 ────────────────────────────────────────────────
#   全系統 20+ 處 cat_zh_map 的**鍵全是中文**（值才是 slug）→ 英文類別句
#   （'Electronics stock' / 'all Daily Goods stock'）一處也命中不了，被當成
#   商品名 keyword 抽出去 → OOV「庫中無此商品」誠實拒絕＝整條類別查詢功能
#   在英文版是壞的（實測 6 類別 5 個掛，GUIDE_MSG 還教訪客這樣問）。
#   ⚠️ 不走「英文→中文改寫」：_rewrite_query 的註解記錄了多次資訊銷毀事故
#   （時間詞被吞、倉名全毀）→ 用**加法**，只在需要 category 的點多問一句。
#   ⚠️ 坑 1：英文一律詞界比對。'sports' 會出現在 Electrolyte Sports Drink，
#   但那是商品名不是類別語境，故類別解析只在「無扎實商品名命中」時才採用。
_CAT_WORDS_EN = {
    "electronics":       (r"electronics?", r"3c", r"consumer electronics"),
    "appliance_kitchen": (r"appliances?", r"kitchen(?:ware)?", r"home appliances?",
                          r"appliance\s*&?\s*kitchen"),
    "food_beverage":     (r"food", r"foods", r"beverages?", r"drinks?",
                          r"food\s*&?\s*beverage", r"food and drinks?",
                          r"groceries", r"grocery"),
    "daily_goods":       (r"daily goods", r"daily necessities", r"household",
                          r"household goods", r"consumables", r"toiletries"),
    "apparel":           (r"apparel", r"clothing", r"clothes", r"garments?",
                          r"wear"),
    "sports":            (r"sports?", r"sporting goods", r"fitness", r"outdoor",
                          r"camping", r"gym"),
}


def _category_from_en(user_text: str) -> str | None:
    """英文句 → category slug。找不到回 None。
    最長匹配優先（'food & beverage' 要贏 'food'），避免部分命中選錯類。"""
    if not _is_mostly_english(user_text):
        return None
    _best, _best_len = None, 0
    for _cat, _pats in _CAT_WORDS_EN.items():
        for _p in _pats:
            _m = _re.search(rf"\b{_p}\b", user_text, _re.I)
            if _m and len(_m.group(0)) > _best_len:
                _best, _best_len = _cat, len(_m.group(0))
    return _best


def _drop_ungrounded_category(func_args: dict, user_text: str) -> dict:
    """LLM 常幻覺 category（「彈力健身環庫存」給 apparel 把 sports 商品濾光
    變成找不到，conv100-r13）→ 句中沒對應類別詞就丟棄。"""
    _cat = func_args.get("category")
    # EN build：接地詞表全中文 → 英文句 LLM 給的**正解 category 會被丟掉**
    #   （坑 3「防幻覺閘門吃掉正解」）→ 英文另用英文詞表接地。
    if _cat in VALID_CATEGORIES and _is_mostly_english(user_text):
        if _category_from_en(user_text) == _cat:
            return func_args          # 英文接地成功，保留
        return {k: v for k, v in func_args.items() if k != "category"}
    if _cat in VALID_CATEGORIES and not any(
            w in user_text for w in _CAT_GROUND_WORDS.get(_cat, ())):
        func_args = {k: v for k, v in func_args.items() if k != "category"}
        log.info(f"[校正 C-cat] 丟棄幻覺 category={_cat}")
    return func_args


def _kw_grounded(kw: str, user_text: str) -> bool:
    """extractor 的 fuzzy 結果要跟原句「接地」才可信：全名的任一連續兩字、
    或商品核心尾字（非把/的等虛字）出現在原句。「把北倉的傘」被 fuzzy 成
    「除塵電動拖把」——兩者毫無重疊、只靠介詞「把」亂中（conv100-r8）。
    正向案例要保住：「帽子」→防曬遮陽帽（尾字帽 ✓）、「電鍋」→陶瓷不沾鍋（尾字鍋 ✓）。"""
    # ── EN build：英文句要走英文接地判準 ────────────────────────────────
    #   原判準是**中文字元級**（任兩字出現在原句）。英文錯字天生不接地——
    #   'keyyboard on hand' 裡找不到 'Mechanical Keyboard' 的任何兩字元 →
    #   判未接地 → C1g 把模糊層剛救回的正解清掉，又回全店概覽（守衛 inv
    #   類大量 FAIL 的最後一哩）。英文改判「詞級接地」：商品名的任一實詞
    #   出現在原句、或原句某個詞跟它足夠相似（＝模糊層的證據本身）。
    if _is_mostly_english(user_text):
        _kl = (kw or "").lower()
        _ul = user_text.lower()
        if not _kl:
            return False
        # 門檻 3 不是 4——mop / pan / fan / bag 都是合法商品核心詞，
        #   排掉它們會讓 'mop on hand' 判成未接地（Electric Mop 只剩
        #   electric 可比，原句沒有）→ 清 kw 回全店概覽
        _kw_words = [w for w in _re.split(r"[\s\-/]+", _kl)
                     if len(w) >= 3 and not any(c.isdigit() for c in w)]
        if not _kw_words:
            return _kl in _ul
        _u_words = [w.strip(" ?.!,'\"") for w in _re.split(r"[\s\-/]+", _ul)]
        _u_words = [w for w in _u_words if len(w) >= 3]
        # 別名正規化後的字面接地最可信（biscuits→Crackers：字面不重疊，
        #   但 alias 是確定性映射，不是幻覺）→ 先看這條
        try:
            from alias_en import normalize_alias_en as _na
            _norm = _na(user_text).lower()
            if any(_kwd in _norm for _kwd in _kw_words):
                return True
        except Exception:
            pass
        if any(_kwd in _ul for _kwd in _kw_words):
            return True
        # 模糊接地：只要**商品名的任一實詞**在原句有夠像的對應詞就算接地
        #   （'keyyboard' ≈ Keyboard → Mechanical Keyboard 接地成立；訪客
        #   只講部分名稱是英文常態，不能要求商品名的詞全部出現）。
        #   ⚠️ 這一層只負責「kw 是不是憑空冒出來的」；「chairs for the
        #   office 該不該回露營椅」是 OOV 判斷，由 _en_fuzzy_keyword 的
        #   陌生修飾詞防線處理，不在這裡兼管（職責分開才不會互相拉扯）。
        #   ⚠️ 3-4 字母的詞不做模糊（mop↔map、pan↔can 只差一個字母卻是
        #      完全不同的東西）——短詞只認上面的精確 substring 比對。
        import difflib as _dl
        if any(_dl.SequenceMatcher(None, _uw, _kwd).ratio() >= 0.84
               for _kwd in _kw_words if len(_kwd) >= 5
               for _uw in _u_words):
            return True
        # ⚠️ 最後讓模糊層表態：**別名的錯字**（nappues→Baby Diapers、
        #   traash bags→Heavy-duty Trash Bags）跟商品名字面完全無關，
        #   上面每一條都比不到 → C1g 判未接地把正解清掉，退回全店概覽。
        #   _en_fuzzy_keyword 自帶陌生修飾詞防線，OOV 句仍不會誤放行。
        try:
            # 用 _extract_sku_keyword（英文快路徑已含剝虛詞 + 別名 + 模糊 +
            #   描述表 + 不確定不猜）的結果比對；直接傳整句給
            #   _en_fuzzy_keyword 會被 'on hand' 這種虛詞觸發 OOV 防線
            if _extract_sku_keyword(user_text) == kw:
                return True
        except Exception:
            pass
        # 功能描述句：keyword 字面必然不在原句（那正是描述句的定義）
        _d_g = _en_descriptor_hit(user_text)
        if _d_g and _d_g.lower() in (kw or "").lower():
            return True
        return False

    k = (kw or "").replace(" ", "")
    if len(k) < 2:
        return bool(k) and k in user_text
    if any(k[i:i + 2] in user_text for i in range(len(k) - 1)):
        return True
    _tail = k[-1]
    return _tail in user_text and _tail not in "把的了個包入款"


def _correct_function_call(user_text: str, func_name: str, func_args: dict) -> tuple[str, dict, bool]:
    """校正規則。回 (corrected_name, corrected_args, hard_corrected)。
    hard_corrected=True 表示有確定性規則命中，C18 不應再覆蓋。"""
    text_low = user_text.lower()

    # C-PO：明確「開採購單」意圖 → generate_po。一定要在 C13b 之前、且不管
    # func_name 是什麼都要 hard-return——「出一張缺貨採購單」的「出一張」會被
    # C13b 的單字「出」+數字量詞規則搶成出貨 1 張，即使 intent_clf 已正確判成
    # generate_po 也會被劫走（第9輪測試抓到）。
    if (any(w in user_text for w in ("採購單", "採購草稿", "補貨單", "補貨草稿", "補貨採購", "開單採購", "開單補貨",
                                     "一張單", "開單"))
            and any(v in user_text for v in ("出", "開", "產", "生", "列", "建", "做", "給我", "擬", "轉", "拉"))
            and not any(w in user_text for w in ("查", "看", "哪些", "紀錄", "記錄", "歷史", "對帳", "短收"))):
        log.info("[校正 C-PO] 開採購單意圖 → generate_po")
        return "generate_po", {"source": "low_stock"}, True

    # C-INV：「存貨水位/庫存水位」是查庫存口語，不是安全水位設定/RCA
    # （「看下不鏽鋼悶燒罐的存貨水位」曾被判 search_log，第11輪抓到）
    if (("存貨水位" in user_text or "庫存水位" in user_text)
            and "安全" not in user_text):
        _kw_inv = _extract_sku_keyword(user_text)
        log.info(f"[校正 C-INV] 存貨水位 → query_inventory kw={_kw_inv!r}")
        return "query_inventory", ({"keyword": _kw_inv} if _kw_inv else {}), True

    # C13a：跨倉調貨意圖 → create_transfer（2026-07-02 新增）。放在 C13b（進出貨）
    #   之前，因為調貨句同時含「調」動詞+數量+兩個倉名，元素跟進出貨重疊，要先
    #   攔截才不會被 C13b 誤判成單倉進出貨。判別特徵：含明確調貨動詞（調/調撥/
    #   調貨/移/搬）+ 具體數字量詞 + 兩個不同倉名（一來源一目標）。
    import re as _re13a
    # 單字「調/搬/移/撥」搭配「兩個倉名 + 數字量詞」已經夠精準（純進出貨句不會
    #   同時提到兩個倉），可以放心收單字動詞（「搬30個到南倉」的「搬」）。
    # 兩倉名條件下可放心收單字動詞；「送到/送去/轉到/運到」只在這裡（兩倉）收，
    # 單倉的「廠商送到南倉35瓶」仍走 C13b 進貨（第11輪窮舉補齊）
    _transfer_verbs = ("調貨", "調撥", "調到", "調去", "調過去", "調給", "調", "搬到", "搬去",
                       "搬", "移到", "移去", "移過去", "移撥", "移", "撥到", "撥去", "撥出", "撥",
                       "挪到", "挪去", "挪過去", "挪", "轉到", "轉去", "轉過去", "轉倉", "轉",
                       "勻給", "勻", "送到", "送去", "運到", "運去", "運過去", "拉到", "拉去",
                       # RPI5 conv100-r2：「分5台熨斗到北倉」的「分…到」漏收
                       "分到", "分去", "分給", "分過去",
                       # conv100-r5：「北倉的折疊露營椅過去南倉10張」的裸「過去」
                       # （兩倉名+數量前提下安全）
                       "過去",
                       # conv100-r6：「抓5盞…去南倉」「出20捲垃圾袋支援南倉」
                       "抓去", "抓到", "支援",
                       # conv100-r7：「從中倉撤20包…回北倉」
                       "撤到", "撤回", "撤去", "撤")
    # ── EN build：英文調貨動詞（原動詞庫全中文 → 英文調貨句一句都判不到，
    #    守衛 tf 類 14 句全 FAIL：`transfer 30 earbuds from north to south`
    #    掉到 C13b/查庫存。英文用小寫比對；動詞帶尾空白降低誤傷
    #    （"移"對應 move，但 "movement/moved to" 是查進出貨紀錄，不能收裸 move）──
    _ut13a = user_text.lower()
    _EN_TRANSFER_VERBS = ("transfer ", "transfers ", "transferring ", "transfer ",
                          "move ", "moving ", "shift ", "shifting ",
                          "send ", "sending ", "ship ", "shipping ",
                          "relocate", "reallocate", "redistribute",
                          "rebalance", "reroute", "divert",
                          "bring ", "take ", "pull ", "push ")
    _has_en_transfer_verb = any(w in _ut13a for w in _EN_TRANSFER_VERBS)
    _qty13a_m = _re13a.search(
        r'([0-9]+|[零一二兩三四五六七八九十百千萬億]+)\s*'
        # 「個」排除「三個倉」（曾把倉數吃成 qty=3，conv100-r6）；單位補「盞」
        r'(?:件|個(?!月|星期|禮拜|小時|鐘頭|倉)|條|支|台|箱|包|瓶|罐|組|雙|套|盒|對|頂|張|把|副|顆|粒|袋|桶|杯|塊|片|卷|捲|盞)', user_text)
    # r80：調貨裸數字（「中倉調100過來南倉」沒帶量詞）——同 r79 進出貨裸數字形
    if not _qty13a_m:
        _qty13a_m = _re13a.search(
            r'[調撥挪搬移轉勻]\s*([0-9]{1,6})(?![0-9]*[月日號點樓年%．\.])', user_text)
    # ── EN build：英文數量（同 C13b 的英文式數量，`transfer 30 earbuds`）。
    #    排除日期時間百分比，另排除「14-inch」這種商品名內的數字
    #    （`ship 5 14-inch Laptop Bag south to central` 要抓 5 不是 14）──
    if not _qty13a_m and _has_en_transfer_verb:
        # 數量＝**調貨動詞後緊跟的第一個數字**（`transfer 30 earbuds`）。
        # 不能只用「數字＋空白＋字母」——`ship 5 14-inch Laptop Bag` 的 5 後面
        # 接的是數字、14 後面接的是連字號，兩個都不中，qty 會抽成 None 而掉出
        # C13a（守衛 tf 抓到）。綁動詞後最近的數字最穩，也不會誤收商品名內的
        # 尺寸數字（14-inch / 28cm / 10000mAh）。
        _qty13a_m = _re13a.search(
            r'(?:transfer|transfers|transferring|move|moves|moving|shift|shifting|'
            r'send|sends|sending|ship|ships|shipping|relocate|reallocate|'
            r'redistribute|rebalance|reroute|divert|bring|take|pull|push)\s+'
            r'(?:me\s+|us\s+|about\s+|around\s+|roughly\s+)?'
            r'([0-9]{1,6})\b(?!\s*(?:%|percent|am\b|pm\b|days?\b|weeks?\b|'
            r'months?\b|years?\b|hours?\b|minutes?\b|oclock\b))',
            user_text, flags=_re13a.I)
    _qty13a_int = _cn_to_int(_qty13a_m.group(1)) if _qty13a_m else None
    # ── 小數保護-en-13a（2026-08-02）：調撥走本分支、有自己的數量抽取，
    #   `transfer 3.5 wireless mouse …` 會被截成 3（訪客講的與要寫的不符）。
    #   同 C13b 的處理：小數 → 當模糊量，讓下游追問實際件數。
    if _qty13a_int is not None and _re13a.search(
            r'\b[0-9]+\.[0-9]+\s+[A-Za-z]', user_text):
        _qty13a_int = None
        log.info(f'[qty-decimal-en-13a] 調撥小數數量 → 追問: {user_text!r}')
    # 動詞跟介系詞被商品隔開的句型：「北倉送20個藍牙耳機到南倉」的「送…到」
    # 子字串比對不到（第11輪抓到）。兩倉名+數量的前提下跨距比對安全。
    _sep_verb_m = _re13a.search(r'[送運搬移調撥挪轉勻分抓撤].{0,18}?[到去給回]', user_text)
    # 「北倉給南倉12瓶X」句型：倉名+給+倉名，沒有其他調貨動詞也算（conv100-r5）
    _wh_give_wh_m = _re13a.search(r'[北中南](?:區倉|區|倉)?\s*給\s*[北中南]', user_text)
    _has_transfer_verb = (any(w in user_text for w in _transfer_verbs)
                          or _sep_verb_m is not None or _wh_give_wh_m is not None
                          or _has_en_transfer_verb)
    # 句中有外部對象（供應商/客戶）→ 是進出貨不是調貨，讓給 C13b
    # （conv100-r5：「供應商剛送到一批瑜珈墊 25張放南倉」的「送到」被搶成調貨 clarify）
    # EN build：英文外部對象同理（`supplier sent 50 X to north` 是進貨不是調貨；
    #   `shipped 20 X to the customer` 是出貨）
    if any(w in user_text for w in ("供應商", "廠商", "客戶", "客人", "顧客")) \
            or any(w in _ut13a for w in ("supplier", "vendor", "customer", "client",
                                         "buyer")):
        _has_transfer_verb = False
    # 模糊量詞（「調一批悶燒罐到南倉」無精確數字）：有調貨動詞+兩倉時也算調貨，
    # qty 留 None 讓 create_transfer clarify 問數量（RPI5 conv100-r2：原本落 config）
    _vague_qty13a = any(w in user_text for w in ("一批", "一些", "些", "若干", "幾個", "幾件",
                        "一點", "一部分", "部分", "一半", "半數", "分點", "勻一點", "勻些",
                        "平均", "平分",
                        # r18：「把瑜珈墊全部從北倉調到南倉」的「全部」也是模糊量
                        # （曾退成查庫存）→ clarify 問確切數量
                        "全部", "全數", "整批", "通通", "所有",
                        # r20：「調3打啤酒」的「打」（＝12 但不硬算，問清楚）
                        "打"))
    # 兩個不同倉名（北/中/南去重後 >= 2）才算調貨
    _wh_mentions13a = [w for w in ("北倉", "北區倉", "北區", "中倉", "中區倉", "中區",
                                    "南倉", "南區倉", "南區") if w in user_text]
    # ── EN build：英文倉名（north/central/south [warehouse]）。**依出現順序**
    #    收集，下面「來源倉＝第一個非目標倉」的邏輯才會對
    #    （`from north to south`：north 先出現＝來源）──
    if not _wh_mentions13a:
        _EN_WH2ZH = {"north": "北倉", "central": "中倉", "south": "南倉"}
        _wh_hits13a = []
        for _en_w, _zh_w in _EN_WH2ZH.items():
            for _mm in _re13a.finditer(r'\b' + _en_w + r'\b', _ut13a):
                _wh_hits13a.append((_mm.start(), _zh_w))
        _wh_mentions13a = [_z for _p, _z in sorted(_wh_hits13a)]
    _wh_keys13a = {w[0] for w in _wh_mentions13a}
    # 零倉名但「調貨動詞緊鄰數量量詞」（r20：「毛帽調10頂過去」曾退成查庫存）
    # → 開 transfer 讓 tools clarify 問從哪到哪。「安全庫存調成100」的「調成」
    # 後面不是數字量詞，不會誤中。
    _tf0_m = _re13a.search(r'[調挪移撥轉]\s*(?:[0-9]+|[零一二兩三四五六七八九十百千萬億]+)\s*'
                           r'(?:件|個|條|支|台|箱|包|瓶|罐|組|雙|套|盒|對|頂|張|把|打)', user_text)
    # ── EN build：英文**強**調貨動詞（語意唯一、不會是查詢句）。
    #    零倉名/單倉名分支只收這些——`move`/`send`/`ship` 太弱，零倉名時
    #    `what moved today` 這類查詢會被誤搶；`transfer 20 earphones`
    #    （沒說從哪到哪）則該開卡讓 clarify 問路線──
    _EN_STRONG_TF = ("transfer", "transfers", "transferring", "relocate",
                     "reallocate", "redistribute", "rebalance", "transferred")
    _has_en_strong_tf = any(w in _ut13a for w in _EN_STRONG_TF)
    # 單倉名 + 目標倉介系詞（`move 30 yoga mats to south`）也算調貨意圖：
    #   有「動詞＋數量＋to 倉名」就夠明確，來源留空讓 clarify 問
    _en_to_wh_m = _re13a.search(
        r'\b(?:to|into|over to)\s+(?:the\s+)?(?:north|central|south)\b', _ut13a)
    if (func_name != "create_transfer" and _has_transfer_verb
            and (_qty13a_int is not None or _vague_qty13a)
            and (len(_wh_keys13a) >= 2
                 or (len(_wh_keys13a) == 0 and _tf0_m is not None
                     and not any(w in user_text for w in _CONFIG_KEY_WORDS))
                 # EN build：零倉名 + 強調貨動詞（`transfer 20 earphones`）
                 or (len(_wh_keys13a) == 0 and _has_en_strong_tf
                     and _qty13a_int is not None
                     and not any(w in text_low for w in ("safety stock",
                                                        "reorder point",
                                                        "lead time")))
                 # EN build：單倉名 + 目標倉介系詞（`move 30 yoga mats to south`）
                 #   排除明確的進貨動詞——`add/put/received 100 X to south`
                 #   是單倉進貨（C13b），不是調貨
                 or (len(_wh_keys13a) == 1 and _has_en_transfer_verb
                     and _en_to_wh_m is not None and _qty13a_int is not None
                     and not any(w in _ut13a for w in
                                 ("add ", "added", "adding", "put ", "puts ",
                                  "received", "receive", "restock", "arrived",
                                  "came in", "delivered", "returned")))
                 # 單倉 + 強調貨動詞（兩字以上，不含單字「調/搬/移/撥」）也算調貨
                 # 意圖：「調撥25個藍牙耳機給南倉」只提到目標倉，來源留空讓
                 # create_transfer 的 clarify 問「從哪個倉調」（守衛庫抓到的回歸：
                 # 這種句曾跑去查庫存）
                 or (len(_wh_keys13a) == 1 and (_sep_verb_m is not None or any(
                     w in user_text for w in (
                     "調撥", "調貨", "轉倉", "調到", "調去", "調過去", "調給",
                     "搬到", "搬去", "移到", "移去", "移過去", "移撥",
                     "撥到", "撥去", "挪到", "挪去", "挪過去",
                     "轉到", "轉去", "轉過去", "勻給")))))):
        # 解析來源倉 / 目標倉：目標倉通常緊跟在「到/去/過去/調到」之後。
        _WH_ZH2KEY = {"北": "北倉", "中": "中倉", "南": "南倉"}
        _to_key = ""
        # 「往南倉調」「支援南倉」「撤回北倉」的目標倉介系詞（conv100-r6/r7）
        # r80：「調100過來南倉」的「過來…南倉」目標介系詞——過來/來也要收
        _to_m = _re13a.search(r'(?:到|去|過去|過來|來|給|往|支援|回)\s*([北中南])',
                              user_text)
        if _to_m:
            _to_key = _to_m.group(1)
        # ── EN build：英文目標倉介系詞（to / into / over to / across to）。
        #    `from north to south` 抓 south；`ship 10 X central to north` 抓 north。
        #    用最後一個 to-片語（來源常在前、目標在後）──
        if not _to_key:
            _EN_TO_HITS = _re13a.findall(
                r'(?:\bto|\binto|\bover to|\bacross to|\bthru to)\s+(?:the\s+)?'
                r'(north|central|south)\b', _ut13a)
            if _EN_TO_HITS:
                _to_key = {"north": "北", "central": "中",
                           "south": "南"}[_EN_TO_HITS[-1]]
        # 來源倉：第一個出現、且不是目標倉的倉名
        _from_key = ""
        for _w in _wh_mentions13a:
            if _w[0] != _to_key:
                _from_key = _w[0]
                break
        # 目標倉沒抓到（例如「北倉南倉調20個」沒有明確到/去）→ 留空讓 clarify 問
        _from_zh = _WH_ZH2KEY.get(_from_key, "")
        _to_zh = _WH_ZH2KEY.get(_to_key, "")
        # 商品名：剝掉動詞/時間詞/數量量詞/倉名後交給 _extract_sku_keyword
        # （_qty13a_m 可能為 None——模糊量詞「一批」路徑無精確數字，見 _vague_qty13a）
        _pre13a = user_text.replace(_qty13a_m.group(0), "") if _qty13a_m else user_text
        # 模糊量詞也要從商品名剝掉，避免「一批悶燒罐」把「一批」當商品名一部分
        # （RPI5 conv100-r3：漏剝「一半/一部分/分點」→ kw 抽成「把 耳機一半」雜訊）
        for _vq in ("一部分", "一批", "一些", "半數", "一半", "部分", "若干", "一點",
                    "分點", "勻一點", "勻些", "些"):
            _pre13a = _pre13a.replace(_vq, "")
        for _w in (_transfer_verbs + tuple(_wh_mentions13a) +
                   ("今天", "今日", "剛剛", "剛才", "幫我", "麻煩", "請", "從", "到", "去",
                    "過去", "給", "把", "庫存", "的貨", "的")):
            _pre13a = _pre13a.replace(_w, "")
        # ── EN build：英文句要剝掉調貨動詞/倉名/介系詞，才抽得到商品名
        #    （`transfer 20 wireless mouse from north to south` 不剝的話
        #    _extract_sku_keyword 會被 from/to/north 干擾）。用 \b 詞界替換，
        #    不能用裸 replace——`ship ` 會把商品名裡的字切壞──
        if _has_en_transfer_verb:
            _pre13a = _re13a.sub(
                r'\b(?:transfer|transfers|transferring|move|moves|moving|shift|'
                r'shifting|send|sends|sending|ship|ships|shipping|relocate|'
                r'reallocate|redistribute|rebalance|reroute|divert|bring|take|'
                r'pull|push|please|pls|can you|could you|i want to|i need to|'
                r'from|to|into|over|across|the|units?|pcs|pieces?|boxes?|box|'
                r'north|central|south|warehouse|wh|stock|inventory)\b',
                ' ', _pre13a, flags=_re13a.I)
            _pre13a = _re13a.sub(r'\s+', ' ', _pre13a).strip()
        _kw13a = _extract_sku_keyword(_pre13a) or _extract_sku_keyword(user_text) or ""
        # 商品名防呆（RPI5 conv100-r3：「把中倉庫存移一些去南倉」抽成雜訊
        # 「把 移一些去」）。kw 比對不到真商品 → 不硬轉 transfer 帶雜訊，退回
        # clarify 問要調哪個商品，比 create_transfer 拿爛 kw 報錯好。
        import warehouse as _W13a
        _m13a = _W13a.match_items(_kw13a) if _kw13a else []
        # 最高分 <3 = 雜訊靠單字亂中（「把 移一些去」靠「移」中行動電源 score 1），
        # 不算真商品名 → 退回查詢概覽（RPI5 conv100-r3）
        if not _kw13a or not _m13a or _m13a[0].get("score", 0) < 3:
            # kw 像真商品名（乾淨中文、無雜訊空格、非泛詞）但庫裡沒有 → 走 OOV
            # clarify「找不到 XX」，比回全倉概覽答非所問好（conv100-r5：掃地機器人）
            # r18：2 字乾淨名詞也算（「幫我調5箱可樂去北倉」的「可樂」曾退成
            # 概覽答非所問）——全中文 + 無空格 + 非泛詞
            import re as _re_oov
            _oov_ok = (_kw13a and " " not in _kw13a and 2 <= len(_kw13a) <= 8
                       and bool(_re_oov.fullmatch(r"[一-鿿]+", _kw13a))
                       and not any(g in _kw13a for g in ("庫存", "東西", "商品", "的貨", "一些")))
            # r80：純調貨句缺商品名（「中倉調100過來南倉」沒說調什麼）——退回
            # 概覽答非所問。改標記讓 WS 層用 ctx 商品補回或 clarify 問商品
            log.info(f"[校正 C13a] 調貨但商品名抽壞/低分 kw={_kw13a!r} → "
                     f"{'OOV clarify' if _oov_ok else '調貨缺商品'}")
            if not _oov_ok and (_qty13a_int is not None) and len(_wh_keys13a) >= 2:
                return "create_transfer", {"keyword": "", "from_wh": _from_zh,
                                           "to_wh": _to_zh,
                                           "qty": str(_qty13a_int)}, True
            return "query_inventory", {"keyword": _kw13a if _oov_ok else ""}, True
        log.info(f"[校正 C13a] 調貨意圖 → create_transfer kw={_kw13a!r} from={_from_zh!r} to={_to_zh!r} qty={_qty13a_int}")
        return "create_transfer", {"keyword": _kw13a, "from_wh": _from_zh,
                                    "to_wh": _to_zh,
                                    # 模糊量詞路徑 qty=None → 傳空字串讓 tools clarify
                                    # 問數量（str(None)='None' 曾進 int() 靠例外兜住，r18 整潔化）
                                    "qty": str(_qty13a_int) if _qty13a_int is not None else ""}, True

    # C13-hypo（r25）：「假設明天要出100個悶燒罐 南倉夠嗎」是庫存試算不是真出貨
    # ——曾被 C13b 當真執行出貨（碰巧庫存不足回 error 才沒開卡）。
    if (any(w in user_text for w in ("假設", "如果", "要是", "萬一"))
            and any(q in user_text for q in ("夠嗎", "夠不夠", "撐得住", "怎麼辦",
                                              "會不會", "行嗎", "可以嗎", "夠出嗎"))):
        _kw_hy = _extract_sku_keyword(user_text)
        if _kw_hy and _kw_grounded(_kw_hy, user_text):
            _args_hy = {"keyword": _kw_hy}
            for _zh_hy, _en_hy in _WH_ZH_MAP.items():
                if _zh_hy in user_text and _en_hy != "all":
                    _args_hy["warehouse"] = _en_hy
                    break
            log.info(f"[校正 C13-hypo] 假設句 → query_inventory({_kw_hy!r})")
            return "query_inventory", _args_hy, True

    # C-alert-exp（r47）：到期提醒訴求（「快過期前三天提醒」）曾開出「低於安全庫存」
    # 條件卡＝卡片內容與訴求不符。警示引擎只支援庫存條件 → 誠實說明＋指路。
    # 要有「主動通知」意圖詞才算設警示——「南倉的到期警示」是查詢，讓給 C7 轉到期清單
    if (func_name == "set_alert"
            and (any(w in user_text for w in ("過期", "到期", "效期"))
                 or _re.search(r"\bexpir(?:y|ing|es?|ation)\b|\bshelf\s*life\b", text_low))
            and (any(w in user_text for w in ("提醒", "通知", "叫我", "跟我說", "告訴我"))
                 or _re.search(r"\b(?:remind|notify|alert|tell)\s+me\b|\blet me know\b",
                               text_low))):
        if _is_mostly_english(user_text):
            return "clarify", {
                "question": 'Alerts currently support "below safety stock" and '
                            '"below a set quantity". For expiry info, just ask '
                            '"whats expiring soon" and I will list it right away.',
                # options 送回後端 → 英文句
                "options": ["whats expiring soon", "whats running low"],
                "hint": ""}, True
        return "clarify", {
            "question": "警示目前支援「低於安全庫存／低於指定數量」；到期資訊可以直接問"
                        "「哪些快過期」，我馬上列給你。",
            "options": ["哪些快過期", "庫存警示"], "hint": ""}, True

    # C-excl（r48）：排除式否定（衛生紙不要 其他都查/除了啤酒 什麼都好）——clf 高信心
    # route 曾帶著被排除的商品直查＝語意反轉。排除式總覽不支援 → 誠實說明。
    # EN build（r4 S8）：排除式否定的英文講法——原正則全中文 → 英文句
    #   'i dont want the earphones show me something else' 直接**回了耳機**
    #   ＝語意反轉（訪客明說不要那個）。
    _cexcl = _re.search(r'(?:不要|除了|排除)(?!北|中|南).{0,6}?(?:其他|以外|什麼都|都查|都好|都要|通通|全列|列出來)', user_text) \
        or (_is_mostly_english(user_text) and _re.search(
            r"\b(?:dont|don't|do not|not)\s+(?:want|need|like)\b.{0,40}?"
            r"\b(?:something else|anything else|other|others|else)\b"
            r"|\b(?:except|other than|apart from|besides|excluding)\b"
            r"|\bnot\s+the\b.{0,30}?\b(?:the other|another|something else)\b",
            user_text, _re.I))
    if _cexcl and func_name in ("query_inventory", "list_low_stock", "list_hot_items"):
        import warehouse as _W_cex
        _cex_kw = _extract_sku_keyword(user_text)
        _cex_m = _W_cex.match_items(_cex_kw) if _cex_kw else []
        if _cex_m and _cex_m[0].get("score", 0) >= 5:
            return "clarify", {
                "question": f"「排除{_cex_m[0]['item']['name']}看其他全部」這種總覽還不支援喔——"
                            "可以看「全部商品庫存」或直接指定想查的商品。",
                "options": ["全部商品庫存", "商品清單"], "hint": ""}, True

    # C13-hypo2（r47）：假設/轉述語氣＋寫入近似句（「考慮進50個滑鼠」「供應商說要
    # 送100個滑鼠來」「如果我要出10箱啤酒的話」）曾進追問倉別的寫入流——非命令
    # 語氣不開寫入，查詢化回該商品庫存（訪客要的是「能不能/夠不夠」的判斷素材）。
    _HYPO2 = ("考慮", "在想", "想說", "說要", "聽說", "朋友想", "有人要", "客人問",
              "供應商說", "假設", "會怎樣", "的話")
    if (any(w in user_text for w in _HYPO2)
            and _re.search(r'[進出補調送][貨]?\s*\d', user_text)):
        _kw_h2 = _extract_sku_keyword(user_text)
        if _kw_h2 and _kw_grounded(_kw_h2, user_text):
            log.info(f"[校正 C13-hypo2] 假設/轉述寫入近似句 → query_inventory({_kw_h2!r})")
            return "query_inventory", {"keyword": _kw_h2}, True

    # C13-defer（r25）：寫入喊卡改查詢——「幫我把X出貨——喔不用了 先查庫存就好」
    # 「出貨的事等等 先看啞鈴還剩幾對」曾照走出貨流程 clarify 要異動哪個倉。
    if (any(w in user_text for w in ("不用了", "等等", "先不", "暫緩", "晚點再"))
            and any(w in user_text for w in ("先查", "先看", "查庫存", "看庫存", "還剩", "剩幾"))):
        _kw_df = _extract_sku_keyword(user_text)
        import warehouse as _W_df
        _m_df = _W_df.match_items(_kw_df) if _kw_df else []
        # r30：kw 要是真商品才走查詢——junk kw（進貨 事晚點再說…）曾直接 OOV clarify
        if (_kw_df and _m_df and _m_df[0].get("score", 0) >= 3
                and _kw_grounded(_kw_df, user_text)):
            log.info(f"[校正 C13-defer] 寫入喊卡改查詢 → query_inventory({_kw_df!r})")
            return "query_inventory", {"keyword": _kw_df}, True
        # r30：後半不是商品而是功能（先看缺貨清單/到期清單）→ 轉對應功能
        if any(w in user_text for w in _LOW_STOCK_INTENT_WORDS) or "缺貨" in user_text:
            log.info("[校正 C13-defer] 喊卡改缺貨清單 → list_low_stock")
            return "list_low_stock", {}, True
        if any(w in user_text for w in ("到期", "過期", "效期")):
            log.info("[校正 C13-defer] 喊卡改到期清單 → list_expiring_items")
            return "list_expiring_items", {}, True

    # C13-sellout（r24）：「X是不是快出清完了」是存量/可得性詢問——「出清」在
    # 出貨動詞表裡，但疑問句型+無數量=沒人要異動庫存 → 直接查該商品庫存。
    # r28：台語「賣了了/賣光了」同族（曾開異動流程 clarify 要哪個倉）
    if (any(w in user_text for w in ("出清", "賣了了", "賣光了", "賣光"))
            and any(w in user_text for w in ("是不是", "了嗎", "完了", "了沒", "得差不多", "喔", "吧", "了了"))
            and not re.search(r"\d", user_text)):
        _kw_so = _extract_sku_keyword(user_text)
        if _kw_so:
            log.info(f"[校正 C13-sellout] 出清疑問句 → query_inventory({_kw_so!r})")
            return "query_inventory", {"keyword": _kw_so}, True

    # C13b：即時進出貨意圖 → create_movement（輕量版，不依賴模型認得這個新 function，
    #   270M 沒訓練過 create_movement，靠關鍵字 + 正則抽參數，跟 list_files/set_alert 同模式）
    #   放在所有規則最前面（優先權最高）：「藍牙喇叭庫存加50組」這種句子含「庫存」+
    #   「加」，原本會被後面的 C9（→ manage_config）搶先攔走，導致 C13b 永遠跑不到。
    #   進出貨意圖的判定條件（明確商品名+方向詞+具體數字+量詞）已經夠具體，不會誤傷
    #   真正的 manage_config/query_movement 意圖，可以放心搶最高優先權。
    #   商品名抽取沿用既有的 _extract_sku_keyword()（多層 fuzzy 比對，比自己 replace
    #   噪音詞可靠很多，同一套邏輯 search_log/query_inventory 都在用）。
    # 退貨（客人退回來）= 庫存增加，當進貨的特例（2026-07-03 新增）。退貨詞放
    #   進 in words，並另外標記 is_return 讓顯示/紀錄標「退貨」而不是「進貨」。
    # 動詞庫第11輪改成主動窮舉（之前被動一輪補一輪，每輪都漏 5-8 個口語變體）
    _movement_return_words = ("退貨", "退回", "退還", "退了", "客人退", "顧客退",
                              "被退", "退回來", "退貨回來", "客退", "退進來",
                              "退貨進來", "收到退貨", "收到退回")
    _movement_in_words = ("進了", "進貨", "到貨", "收貨", "入庫", "補了", "補貨",
                          "來貨了", "來貨", "進了貨", "來了",
                          "收了", "送來", "送到", "送了",
                          "卸了", "卸貨", "卸下", "入了",
                          "囤了", "囤貨", "囤", "補上", "補進", "補齊", "進帳",
                          "到了", "收到", "收一批", "入倉", "上架",
                          # conv100-r6：「北倉新到一批LED露營燈」
                          "新到", "收進",
                          # r24：「剛到一批機能排汗衣60件放中倉」的「剛到」漏收
                          # → LLM 幻覺成 manage_config read 回設定導覽
                          "剛到",
                          # conv100-r7：「北倉剛進一批玻璃保鮮盒」
                          "剛進",
                          # conv100-r9：「幫我叫30包嬰兒濕紙巾的貨」（qty 前提下單字安全）
                          "叫") + _movement_return_words
    _movement_out_words = ("出貨了", "出貨", "出庫", "賣掉了", "賣掉", "賣了",
                           "銷貨", "銷出", "銷售", "售出", "出了", "買走了", "買走",
                           "拿走", "提走", "取走", "載走", "銷了", "賣出",
                           "發貨", "發出", "發走", "送走", "訂走", "帶走",
                           "出清", "出給", "出掉", "賣走", "出倉",
                           # conv100-r5：「領走5組啞鈴」漏收
                           "領走", "領出", "領了", "提領",
                           # conv100-r6：「走了8組」「掃走15包」「有人訂了25條…從南倉出」
                           "走了", "掃走", "訂了",
                           # conv100-r7：「客戶取貨9台迷你果汁機」
                           "取貨",
                           # r19：「北倉報廢5個保鮮盒」報廢/耗損=庫存減少
                           "報廢", "耗損", "損毀", "丟棄")
    # ── EN build：英文進出貨動詞（原動詞庫全中文 → 英文寫入句一句都判不到，
    #    守衛 mv 類 72 句全 FAIL。英文用小寫比對，且多為片語以降低誤傷）──
    _EN_RETURN_W = ("returned", "customer returned", "return of", "sent back",
                    "gave back", "brought back")
    _EN_IN_W = ("received", "receive", "came in", "come in", "arrived",
                "add ", "added", "adding", "put ", "restocked", "restock ",
                "stocked", "delivered", "delivery of", "got ", "inbound",
                "supplier sent", "brought in", "topped up", "top up") + _EN_RETURN_W
    _EN_OUT_W = ("shipped", "ship ", "shipping", "sent out", "sent ", "send ",
                 "sold", "sell ", "dispatched", "picked up", "took out",
                 "take ", "taken", "removed", "remove ", "issued", "outbound",
                 "delivered to customer", "gone out", "went out", "scrapped",
                 "damaged", "discarded", "wrote off", "write off")
    _ut13b = user_text.lower()
    _is_return13b = any(w in user_text for w in _movement_return_words) \
        or any(w in _ut13b for w in _EN_RETURN_W)
    _has_movement_word = any(w in user_text for w in _movement_in_words + _movement_out_words) \
        or any(w in _ut13b for w in _EN_IN_W + _EN_OUT_W)
    # 單獨「進」「出」風險較高（「進去看看」也含「進」），只在句子裡緊接著數字+量詞
    # 時才承認為進出貨動詞（「南區進登山杖100盒」的「進」緊挨著商品名跟數量）。
    import re as _re13b_single
    # r92：「加」是展場自然講法（「幫我在北倉加五十個滑鼠」），但歧義大——
    #   守衛有三條會被誤傷：「安全庫存加20」「北倉加15」是改設定（cfg）、
    #   「三個倉加起來」是查詢（inv）。→ 句中出現這些語境時「加」不算進貨動詞。
    #   r95 回歸：「加一條 電子產品低於20通知我」是**建立警示規則**，卻被
    #   「加+數字+量詞（條）」判成進貨、20 被當進貨量抽走 → intent_clf 本來
    #   正確判 set_alert(conf=1.00) 被 C13b 劫走。排除警示語境（不只擋「加一條」
    #   字面，要擋語意——「加個提醒」「設一條…通知我」等講法同樣不該算進貨）。
    #   ⚠️ 不排除「一條」——「條」是正當量詞（守衛「訂走20條運動毛巾」
    #   「25條USB-C快充線」），「北倉加20條毛巾」是合理進貨句。只排警示語意。
    _add_ok = not _re13b_single.search(
        r'安全庫存|安全線|加起來|水位|通知我|提醒我|警示|低於|高於', user_text)
    _dir_chars = '進出送補加' if _add_ok else '進出送補'
    _single_dir_m = _re13b_single.search(r'[' + _dir_chars + r'](?=[一-鿿\s0-9.]{0,10}(?:[0-9]+|[零一二兩三四五六七八九十百千萬億半幾來]+)\s*(?:件|個(?!月|星期|禮拜|小時|鐘頭|倉)|條|支|台|箱|包|瓶|罐|組|雙|套|盒|對|頂|張|把|副|顆|粒|袋|桶|杯|塊|片|卷|捲|盞|打))', user_text)
    if _single_dir_m and not _has_movement_word:
        _has_movement_word = True
        # r26：裸「補」+數量=進貨（「供應商補30罐氣泡水到北倉」曾被 config guide 劫走）
        if _single_dir_m.group(0) in ("進", "送", "補", "加"):
            # 「送40個防摔殼來南倉」的裸「送」+數量=進貨（送我/送給無數量不會中）
            # r92：「加」同理（「北倉加五十個滑鼠」）——已由 _add_ok 濾掉
            # 安全庫存/加起來等非進貨語境
            _movement_in_words = _movement_in_words + (_single_dir_m.group(0),)
        else:
            _movement_out_words = _movement_out_words + ("出",)
    # r79 危險邊緣：動詞+裸數字（「北倉出400衛生紙」沒帶量詞）也是進出貨——
    # 曾被查詢直答/庫存卡吞掉。排除日期形（出12月報表），且要求句帶倉名保精度
    # r99：擴充雙字動詞（入庫/賣掉/銷掉/退貨）——訪客快打常漏量詞，
    #   「南倉入庫30氣泡水」「賣掉15慢跑鞋」「退貨10行動電源」原被判查詢。
    #   入庫=進、賣掉/銷掉=出、退貨=return（退貨也是庫存增加，走 in + is_return）。
    if not _has_movement_word:
        _bare_dir_m79 = _re13b_single.search(
            r'(入庫|賣掉|銷掉|退貨|[進出補])(?=\s*[0-9]{1,6}(?![0-9]*[月日號點樓年%．\.]))',
            user_text)
        if _bare_dir_m79 and _re13b_single.search(r'[北中南][區倉]', user_text):
            _has_movement_word = True
            _bd = _bare_dir_m79.group(1)
            if _bd in ("進", "補", "入庫"):
                _movement_in_words = _movement_in_words + (_bd,)
            elif _bd == "退貨":
                # 退貨＝庫存增加，走進貨方向 + return 標記
                _movement_in_words = _movement_in_words + ("退貨",)
                _is_return13b = True
            else:  # 出 / 賣掉 / 銷掉
                _movement_out_words = _movement_out_words + (_bd,)
    # 數字開頭的反向詞序（r18：「30個耳機進北倉」——方向詞在商品後、倉名前，
    # 上面的 lookahead 只往後看抓不到）：數量+量詞+商品+進/出+倉名 一樣是進出貨
    if not _has_movement_word:
        _rev_dir_m = _re13b_single.search(
            r'(?:[0-9]+|[零一二兩三四五六七八九十百千萬億]+)\s*'
            r'(?:件|個(?!月|星期|禮拜|小時|鐘頭|倉)|條|支|台|箱|包|瓶|罐|組|雙|套|盒|對|頂|張|把|副|顆|粒|袋|桶|杯|塊|片|卷|捲|盞)'
            r'[一-鿿]{1,8}?([進出])(?=[北中南]?[區倉])', user_text)
        if _rev_dir_m:
            _has_movement_word = True
            if _rev_dir_m.group(1) == "進":
                _movement_in_words = _movement_in_words + ("進",)
            else:
                _movement_out_words = _movement_out_words + ("出",)
    # r40：無量詞的進出貨（「北倉進50藍牙耳機」——數量緊貼商品名、漏打「個」，展場
    #   訪客打字快很常見）。上面的正則都要求「數字+量詞」，缺量詞就漏 → 掉進庫存查詢
    #   （答錯：訪客要進貨卻查庫存）。這裡放寬：方向詞+數字+一段中文，且那段中文 match
    #   得到真商品 → 算進出貨。用真商品驗證避免誤傷（「進50樓」match 不到商品就不算）。
    if not _has_movement_word:
        _nomu_m = _re13b_single.search(
            r'([進出送補])\s*(?:[0-9]+|[零一二兩三四五六七八九十百千萬]+)\s*([一-鿿]{2,8})',
            user_text)
        if _nomu_m and _nomu_m.group(2) not in ("樓", "層", "號", "點", "度"):
            import warehouse as _W_nomu
            _nomu_kw = _extract_sku_keyword(_nomu_m.group(2))
            _nomu_hit = _W_nomu.match_items(_nomu_kw) if _nomu_kw else []
            if _nomu_hit and _nomu_hit[0].get("score", 0) >= 3:
                _has_movement_word = True
                _d = _nomu_m.group(1)
                if _d in ("進", "送", "補"):
                    _movement_in_words = _movement_in_words + (_d,)
                else:
                    _movement_out_words = _movement_out_words + (_d,)
                log.info(f"[C13b-nomu] 無量詞進出貨: 方向{_d} 商品{_nomu_hit[0]['item']['name']}")

    # r26：白拿/搗蛋語境不開進出貨流程（「送我兩箱啤酒當試用品」曾追問異動哪個倉）
    # ——放在所有動詞/結構判定之後，才不會被 _single_dir/_rev_dir 重新點亮
    if any(w in user_text for w in ("送我", "送給我", "請我喝", "請我吃", "招待",
                                     "試用品", "試吃", "免費", "白送", "送你")):
        _has_movement_word = False
    # ── EN build：RCA 語境不開進出貨流程。'charger cable stock doesnt add up'
    #   的 'stock' 被當成進貨動詞 → C13c 追問「要異動哪個倉」（守衛 rca 類
    #   4 句 FAIL 都是這個成因）。對帳句在問「為什麼對不上」，不是要寫入。
    #   ⚠️ 只在**沒有明確數量**時讓路——'north received 50 X, numbers look
    #   wrong' 這種帶數量的仍是寫入句。
    if (_is_mostly_english(user_text) and _has_rca_word(user_text)
            and not _re13b_single.search(r'\b[0-9]{1,6}\b', user_text)):
        _has_movement_word = False
    # 量詞放寬：件/個/條/支/台/箱/包/瓶/罐/組/雙/套/盒；數字可能是阿拉伯或中文
    #   （「三箱」「十個」這種口語，2026-07-02 實測「剛剛入庫三箱衛生紙」抓到：
    #    原本正則只認阿拉伯數字，中文數字全漏，整句 C13b 不觸發跌回誤判）。
    import re as _re13b_pre
    _qunit = r'(?:件|個(?!月|星期|禮拜|小時|鐘頭|倉)|條|支|台|箱|包|瓶|罐|組|雙|套|盒|對|頂|張|把|副|顆|粒|袋|桶|杯|塊|片|卷|捲|盞)'
    _qty_re = r'([0-9]+|[零一二兩三四五六七八九十百千萬億]+)\s*' + _qunit
    _qty13b_m = _re13b_pre.search(_qty_re, user_text)
    # 「數量35」這種無量詞寫法（「南倉補進來一批防蚊液 數量35」，conv100-r7）
    if not _qty13b_m:
        _qty13b_m = _re13b_pre.search(r'數量\s*([0-9]+)', user_text)
    # r79：動詞緊跟裸數字（「北倉出400衛生紙」）——量詞省略形，排除日期
    # r99：雙字動詞同步（「南倉入庫30氣泡水」的 30 緊跟「庫」後）——與上面
    #   _bare_dir_m79 的動詞集對齊，否則方向認出來但數量抽不到卡在問「幾件」。
    if not _qty13b_m:
        _qty13b_m = _re13b_pre.search(
            r'(?:入庫|賣掉|銷掉|退貨|[進出補調])\s*([0-9]{1,6})(?![0-9]*[月日號點樓年%．\.])',
            user_text)
    # ── EN build：英文數量（原正則要求「數字＋中文量詞」，英文 'received 50 power
    #    banks' 抽不到 → 掉進 C13c「數量不明」永遠追問幾件，mv 守衛全 FAIL）──
    #    形式：數字後接英文字（50 power banks / 50 units / 50 boxes of）。
    #    排除日期時間百分比（3 days / 9am / 50%）避免誤收。
    if not _qty13b_m:
        _qty13b_m = _re13b_pre.search(
            r'\b([0-9]{1,6})\s+(?!%|percent\b|am\b|pm\b|days?\b|weeks?\b|months?\b|'
            r'years?\b|hours?\b|minutes?\b|oclock\b)[A-Za-z]',
            user_text)
    # 中文數字要能真的轉成整數才算數（避免「幾個」的「幾」等非數字被誤收）
    _qty13b_int = _cn_to_int(_qty13b_m.group(1)) if _qty13b_m else None
    # r26：0 件是無效異動量 → 當數量不明追問（「出貨0個耳機」曾先問倉別）
    if _qty13b_int == 0:
        _qty13b_int = None
    # r25：小數量詞（進3.5箱）不可硬取整 → 當模糊量追問實際件數
    if _re13b_pre.search(r'[0-9]+\.[0-9]+\s*' + _qunit, user_text):
        _qty13b_int = None
        if not _qty13b_m:
            _qty13b_m = _re13b_pre.search(r'[0-9]+\.[0-9]+\s*' + _qunit, user_text)
    # ── 小數保護-en（2026-08-02）：上面那道要求接**中文量詞**，英文句沒有
    #   → 保護失效，而英文裸數字分支 `\b([0-9]{1,6})\s+[A-Za-z]` 會匹配到
    #   `.5` 的 5 → **`1.5`/`10.5`/`0.5` 一律抽成 5 並開確認卡**（數值錯誤級，
    #   訪客不細看按下去就寫錯資料；超大數量有 9,999 上限、小數卻沒有）。
    #   ⚠️ 只認「數字.數字 + 空白 + 英文字」＝數量位置，不碰型號/版本號。
    if _re13b_pre.search(r'\b[0-9]+\.[0-9]+\s+[A-Za-z]', user_text):
        _qty13b_int = None
        log.info(f"[qty-decimal-en] 小數數量 → 當模糊量追問: {user_text!r}")
    # r25：中文模糊範圍量（一兩百包/兩三箱/三五十個）→ 不可硬猜單一數字開卡，追問
    if _re13b_pre.search(r'[一二兩三四五六七八九十][二兩三四五六七八九](?=[十百千])', user_text):
        _qty13b_int = None
    # 「一箱半」量詞後綴「半」（r21：曾抽成 1 開出 -1 件卡=數值錯）→ 當模糊量追問
    if _qty13b_m and _qty13b_int is not None and user_text[_qty13b_m.end():_qty13b_m.end() + 1] == "半":
        _qty13b_int = None
    # 負數進貨（r17：「北倉進貨-20個耳機」負號在 regex 之外被吞、開出 +20 卡
    # 語意整個反轉）→ 保留負號讓 tools_v2.create_movement 擋下追問
    if _qty13b_m and _qty13b_int is not None:
        _qs = _qty13b_m.start()
        if _qs > 0 and user_text[_qs - 1] in "-−負":
            _qty13b_int = -_qty13b_int
    _has_explicit_qty = _qty13b_int is not None

    if func_name != "create_movement" and _has_movement_word and _has_explicit_qty:
        # 句子同時提到兩個以上倉別時（如「北倉跟南倉的藍牙耳機各出貨了10個跟15個」），
        # 單一 create_movement 呼叫無法表達「哪個倉對應哪個數量」，硬猜容易猜錯真的
        # 異動錯倉庫。刻意不解析出 warehouse（留空），讓 tools_v2.create_movement
        # 既有的「倉別不明 → clarify」分支接手，請使用者拆成一句一倉分別描述。
        _wh_mentions13b = [w for w in ("北倉", "北區倉", "北區", "中倉", "中區倉", "中區",
                                        "南倉", "南區倉", "南區") if w in user_text]
        # EN build：倉別也認英文（原只認中文 → 英文寫入句抽不到倉別，
        #   會落到「倉別不明 → clarify」永遠開不了卡）
        if not _wh_mentions13b:
            for _en_w, _zh_w in (("north", "北倉"), ("central", "中倉"), ("south", "南倉")):
                if _en_w in _ut13b:
                    _wh_mentions13b.append(_zh_w)
        _wh_keys13b = {w[0] for w in _wh_mentions13b}  # 北/中/南 去重
        # EN build：方向判定也要認英文動詞（原只認中文 → 英文寫入句一律被判成
        #   "out"，'add 100 earphones' 會變成出貨扣庫存＝寫錯資料方向）
        _dir13b = ("in" if (any(w in user_text for w in _movement_in_words)
                            or any(w in _ut13b for w in _EN_IN_W))
                   else "out")
        _wh13b = _wh_mentions13b[0] if len(_wh_keys13b) <= 1 and _wh_mentions13b else ""
        _qty13b = str(_qty13b_int)  # 統一成阿拉伯數字（中文數字已轉整數）
        # 退貨用 in 的算法，但下面 return 帶 is_return 讓工具函式標「退貨」
        # 先剝掉進出貨專屬的動詞/時間詞/數量+量詞（這些不在共用的
        # _ALL_KEYWORD_NOISE 清單裡，因為那份清單是給查詢句設計的），
        # 剩下的再交給 _extract_sku_keyword 做既有的多層 fuzzy 商品名比對。
        _pre_clean = user_text.replace(_qty13b_m.group(0), "")
        for _w in (_movement_in_words + _movement_out_words +
                   ("今天", "今日", "本週", "這週", "本月", "這個月", "上午", "早上",
                    "剛剛", "剛才", "登記", "記一下", "麻煩", "幫我", "請", "沒登記",
                    # 鬆散口語開頭/填充（第19輪）
                    "大概", "這樣", "差不多", "就是", "然後", "話說", "對了",
                    "差點忘了", "順便", "喔對", "唉", "嗯", "就")):
            _pre_clean = _pre_clean.replace(_w, "")
        # 批次量詞（r22：「到了一批牛仔褲 35件」kw 曾抽成「一批牛仔褲」找不到）
        for _vq13b in ("一批", "一票", "一些", "一組貨", "一堆"):
            _pre_clean = _pre_clean.replace(_vq13b, " ")
        # 範圍句殘留數字（r20：「進10到20個耳機」剝掉 qty「20個」後殘「10到」
        # 黏進 kw 變「10到耳機」→ 找不到）
        _pre_clean = _re13b_pre.sub(r'[0-9]+[到~－-]?', ' ', _pre_clean)
        # ── EN build：英文剝詞（原剝詞表全中文 → 英文寫入句的動詞/倉名/外部
        #    對象留在字串裡干擾比對：`shipped 20 mouse to the customer` 曾抽成
        #    Wireless Bluetooth Earphones（靠 customer 的字亂中）、
        #    `customer returned 5 keyboards` 抽成空 → 兩句都回「請說明要異動
        #    哪個商品」。用 \b 詞界替換，不能用裸 replace 切壞商品名──
        if any(w in _ut13b for w in _EN_IN_W + _EN_OUT_W):
            _pre_clean = _re13b_pre.sub(
                r'\b(?:shipped|ship|shipping|sent|send|sending|sold|sell|sells|'
                r'dispatched|issued|received|receive|receives|got|add|added|'
                r'adding|put|puts|restocked|restock|stocked|delivered|delivery|'
                r'arrived|arrive|came|come|returned|return|took|take|taken|'
                r'picked|pick|removed|remove|scrapped|damaged|discarded|'
                r'wrote|write|off|out|in|into|to|from|the|a|an|of|and|'
                r'customer|customers|client|clients|supplier|suppliers|vendor|'
                r'buyer|warehouse|wh|north|central|south|today|yesterday|'
                r'this|week|month|morning|please|pls|units?|pcs|pieces?)\b',
                ' ', _pre_clean, flags=_re13b_pre.I)
            _pre_clean = _re13b_pre.sub(r'\s+', ' ', _pre_clean).strip()
        _kw13b = _extract_sku_keyword(_pre_clean) or _extract_sku_keyword(user_text) or ""
        # EN build：剝乾淨後只剩**短商品詞**（mop / pan / fan / bag）時，
        #   _extract_sku_keyword 的英文快路徑有 score>=4 與短詞不模糊的門檻，
        #   會回空 → 'put 100 mop into south' 開不了卡（回「請說明要異動
        #   哪個商品」）。這裡直接拿剝乾淨的殘詞比對，扎實就用。
        if not _kw13b and _is_mostly_english(user_text):
            # 從**原句**剝掉數字與寫入/方位虛詞，剩下的就是商品詞
            #   （_pre_clean 已被前面的中文剝詞迴圈動過，不能直接用）
            _short13b = _re13b_pre.sub(
                r"\b(?:put|puts|add|added|adding|received|receive|got|take|took|"
                r"taken|remove|removed|ship|shipped|send|sent|sold|sell|"
                r"returned|return|into|to|from|in|at|out|of|the|a|an|and|"
                r"north|central|south|warehouse|wh|today|yesterday|"
                r"units?|pcs|pieces?|record|inbound|outbound)\b|\b[0-9]+\b",
                " ", user_text, flags=_re13b_pre.I)
            _short13b = _re13b_pre.sub(r"\s+", " ", _short13b).strip(" ?.!,")
            if _short13b and len(_short13b.split()) <= 2:
                import warehouse as _W13b_s
                _m13b_s = _W13b_s.match_items(_short13b)
                if _m13b_s and _m13b_s[0].get("score", 0) >= 3 and (
                        len(_m13b_s) == 1
                        or _m13b_s[1].get("score", 0) < _m13b_s[0].get("score", 0)):
                    _kw13b = _m13b_s[0]["item"]["name"]
                    log.info(f"[校正 C13b-short] 短商品詞 {_short13b!r} → {_kw13b!r}")
        # 尾巴殘留介系詞（「空氣清淨機到」，conv100-r6）
        _kw13b = _kw13b.rstrip("到去往")
        # fuzzy 錯商品開卡防呆（conv100-r7：「衛生棉」被 extractor 換成
        # 「三層抽取衛生紙」直接開出貨卡）。原句殘字的尾字若是商品核心名詞
        # （棉/紙/機/線…）、卻沒出現在比對到的全名裡 → 改用原詞讓工具報找不到。
        _CORE_TAILS13b = ("棉紙機線墊巾衣褲鞋襪帽壺罐盒袋刷杯鍋燈扇環鈴粉茶豆餅乾"
                          "乳液皂傘椅桌床貼膜殼繩網竿板車鏡錶筆包")
        _frag13b = _pre_clean.replace(" ", "")
        for _w in ("北倉", "中倉", "南倉", "北區倉", "中區倉", "南區倉", "北區", "中區", "南區",
                   "客戶", "客人", "供應商", "顧客", "有筆", "有人", "從", "放", "回", "給", "的"):
            _frag13b = _frag13b.replace(_w, "")
        if (_kw13b and len(_frag13b) >= 3
                and _frag13b[-1] in _CORE_TAILS13b
                and _frag13b[-1] not in _kw13b
                and _frag13b not in _kw13b):
            log.info(f"[校正 C13b] 殘字「{_frag13b}」尾字與「{_kw13b}」不符 → 改用原詞當 OOV")
            _kw13b = _frag13b
        # ── EN build（守衛 mv 回歸）：C13b 自己重抽商品，會被撞名詞帶偏——
        #   '10 laptop sleeve came in at south' 被抽成 Sports Compression
        #   Arm **Sleeve**（laptop sleeve 對 Laptop Bag / Arm Sleeve 是
        #   6:6 同分），接著 anti-hallu 把這個錯的丟掉 → 商品變空 → 開不了卡
        #   （回「Please tell me which item to update」）。
        #   而 **clf/LLM 上游已經抽對**（keyword='14-inch Laptop Bag'）。
        #   → C13b 的結果不接地時，優先沿用上游 keyword。
        if _is_mostly_english(user_text):
            _up_kw13b = str(func_args.get("keyword") or "").strip()
            if _up_kw13b:
                import warehouse as _W13b_up
                _m_up = _W13b_up.match_items(_up_kw13b)
                _up_ok = bool(_m_up and _m_up[0].get("score", 0) >= 6)
                _cur_ok = False
                if _kw13b:
                    _m_cur = _W13b_up.match_items(_kw13b)
                    # 現有 kw 要「接地」＝它的核心詞真的出現在原句。
                    #   ⚠️ 單一詞命中不算——'Sports Compression Arm Sleeve'
                    #   只靠 'sleeve' 一個詞就通過接地（laptop **sleeve**），
                    #   而 sleeve 在原句其實是修飾 laptop 的。多詞商品名要求
                    #   **至少兩個詞**命中，才不會被單一撞名詞蒙混。
                    _cur_words = [w.lower() for w in _kw13b.split() if len(w) >= 4]
                    _cur_hits = sum(1 for w in _cur_words
                                    if w in user_text.lower())
                    _cur_ok = bool(_m_cur and _m_cur[0].get("score", 0) >= 6
                                   and _cur_hits >= (2 if len(_cur_words) >= 2 else 1))
                # ⚠️ 上游 kw **也要接地**才可採用——否則會把 C13b 抽對的
                #   結果換成 LLM 的幻覺。實測回歸：'take 20 espresso machine
                #   out of south' 的 C13b 抽對 Automatic Coffee Machine
                #   （espresso machine 是別名），我卻換成 LLM 的 'coffee
                #   maker'（原句沒有）→ 被 anti-hallu 丟掉 → 開不了卡。
                #   接地判準同下：多詞名要 ≥2 詞命中原句；別名對應則看
                #   _extract_sku_keyword 是否也指向同一商品。
                _up_words = [w.lower() for w in _up_kw13b.split() if len(w) >= 4]
                _up_hits = sum(1 for w in _up_words if w in user_text.lower())
                _up_grounded = _up_hits >= (2 if len(_up_words) >= 2 else 1)
                if not _up_grounded:
                    # 別名路：extractor 對原句的結果若與上游 kw 同一商品，也算接地
                    try:
                        _ex13b = _extract_sku_keyword(user_text)
                        if _ex13b:
                            _m_ex = _W13b_up.match_items(_ex13b)
                            _up_grounded = bool(
                                _m_ex and _m_up
                                and _m_ex[0]["item"]["name"] == _m_up[0]["item"]["name"])
                    except Exception:
                        pass
                # 兩者指向**同一商品**時保留現有 kw：C13b 給的是主檔全名
                #   （Automatic Coffee Machine），上游可能是別名/俗稱
                #   （'coffee maker'）——換過去反而會被 anti-hallu 丟掉。
                _same_item = False
                if _kw13b and _m_up:
                    try:
                        _m_cur2 = _W13b_up.match_items(_kw13b)
                        _same_item = bool(
                            _m_cur2
                            and _m_cur2[0]["item"]["name"] == _m_up[0]["item"]["name"])
                    except Exception:
                        _same_item = False
                if _up_ok and _up_grounded and not _cur_ok and not _same_item:
                    log.info(f"[校正 C13b-en] kw {_kw13b!r} 不接地 → 沿用上游 {_up_kw13b!r}")
                    _kw13b = _up_kw13b
        log.info(f"[校正 C13b] 進出貨意圖 → create_movement（原 {func_name}）kw={_kw13b!r} wh={_wh13b!r} dir={_dir13b} qty={_qty13b!r} return={_is_return13b}")
        _args13b = {"keyword": _kw13b, "warehouse": _wh13b,
                    "direction": _dir13b, "qty": _qty13b}
        if _is_return13b:
            _args13b["is_return"] = True
        return "create_movement", _args13b, True

    # ── C13c（r17）：進出貨動作句「沒有具體數量」→ 追問數量，不可退成查詢 ──
    # 「進貨半箱衛生紙」曾回 movement 查詢、「中倉進貨耳機」曾回單品庫存——
    # 動作意圖被答非所問。條件收緊：有進出動詞 + 真商品名 + 無疑問/期間詞
    # （「最近有進什麼貨嗎」「玻璃保鮮盒最近有補貨嗎」是查紀錄，不受影響）。
    # r81 寫入契約：「北倉進滑鼠」有進/出動詞+商品+倉、只缺數量——動詞判定
    # 依賴數字量詞會整句漏成查詢。補：句首倉別+裸單字進出動詞+緊接非數字內容，
    # 也算進出貨意圖（缺量交給 C13c 追問）。「進去看看」無倉不會中。
    _c13c_bare_mv = bool(_re13b_single.search(
        r'^[^。]{0,4}[北中南][區倉][^。]{0,2}?[進出補][^0-9]', user_text)) \
        and not _has_explicit_qty
    if (func_name != "create_movement" and (_has_movement_word or _c13c_bare_mv)
            and not _has_explicit_qty
            and not any(w in user_text for w in (
                "嗎", "什麼", "哪些", "多少", "幾次", "紀錄", "記錄", "明細",
                "清單", "統計", "流水", "歷史", "沒有", "有沒有", "最近",
                "今天", "昨天", "上週", "本週", "這週", "本月", "這個月",
                "上個月", "這禮拜", "怎", "如何", "狀況", "誰", "為何",
                # r24：「智慧手環是不是快出清完了」是存量詢問不是出貨動作，
                # 曾被「出清」開 create_movement 追問「要異動哪個倉」
                "是不是", "完了", "了沒",
                # r26：「到底衛生紙在哪個倉」的「找不到貨」誤中「到貨」——
                # 位置/疑問詞在場=查詢不是異動
                "哪個", "在哪", "去哪", "到底",
                # r28：「我同事叫我問濕紙巾庫存」的「叫」誤觸（問=查詢語境）
                "問", "庫存"))
            # ── EN build（劇情批 r1）：上面整串「查詢語境排除詞」**全是中文**
            #    → 英文查詢句一個都命中不了，被 C13c 當成寫入意圖劫走。
            #    實測 'which week shipped more last week or this week'
            #    （clf 已正確判 compare_periods）被改成 create_movement，
            #    還反問「Which warehouse for "Elastic Sports Bra"?」＝
            #    問跨期比較卻跳出不相干商品，後續追問全崩。
            and not _EN_QUERY_CTX_RE.search(user_text)):
        import warehouse as _W13c
        # 先剝進出貨動詞/數字量詞再抽（r19：「北倉進貨零個耳機」kw 抽成
        # 「進貨零個耳機」比不到商品 → C13c 沒接到）
        _c13c_src = user_text
        for _w13c in _movement_in_words + _movement_out_words:
            _c13c_src = _c13c_src.replace(_w13c, "")
        _c13c_src = _re13b_pre.sub(_qty_re, "", _c13c_src)
        _kw13c = _extract_sku_keyword(_c13c_src) or _extract_sku_keyword(user_text)
        _m13c = _W13c.match_items(_kw13c) if _kw13c else []
        if _m13c and _m13c[0].get("score", 0) >= 3:
            # EN build：方向/倉別同樣要認英文（同 C13b）
            _dir13c = ("in" if (any(w in user_text for w in _movement_in_words)
                                or any(w in _ut13b for w in _EN_IN_W))
                       else "out")
            _wh13c_l = [w for w in ("北倉", "北區倉", "北區", "中倉", "中區倉", "中區",
                                     "南倉", "南區倉", "南區") if w in user_text]
            if not _wh13c_l:
                for _e13c, _z13c in (("north", "北倉"), ("central", "中倉"), ("south", "南倉")):
                    if _e13c in _ut13b:
                        _wh13c_l.append(_z13c)
            log.info(f"[校正 C13c] 進出貨意圖但數量不明 → 追問 kw={_kw13c!r}")
            return "create_movement", {"keyword": _kw13c,
                                       "warehouse": _wh13c_l[0] if _wh13c_l else "",
                                       "direction": _dir13c, "qty": ""}, True

    # 排程/警示管理工具：Pre-C 已確定，不再校正
    # set_alert / schedule / compare 已被 Pre-C 校正過，不需再過 C1-C18
    # query_movement 不加在此，因為 C8 RCA 校正需要能 override 它
    # set_alert 不 early-return —「庫存警示」可能被模型誤判 set_alert，需經 C3 校正
    if func_name in ("set_schedule", "list_schedules", "delete_schedule",
                     "list_alerts", "delete_alert",
                     "compare_warehouses"):
        # 例外：compare 但句中沒有兩個倉名、且含強 RCA 意圖詞（「純棉素T帳面
        # 跟實際差好多」的「跟…差」讓 intent_clf 誤判 compare）→ 不保護，
        # 放行給 C8 轉 search_log（第10輪測試抓到）
        _cmp_rca_leak = (func_name == "compare_warehouses"
                         and len(_wh_keys13a) < 2
                         and _has_rca_word(user_text))
        # 例外2（r20）：「今天進的貨跟出的貨各多少」無兩倉名+含進出語 →
        # 放行給 C-CmpMv 轉進出統計（曾被這裡保護住回倉庫比較=答非所問）
        _cmp_mv_leak = (func_name == "compare_warehouses"
                        and len(_wh_keys13a) < 2
                        and any(w in user_text for w in ("進的貨", "出的貨", "進貨",
                                                          "出貨", "進了", "出了", "進出")))
        if not _cmp_rca_leak and not _cmp_mv_leak:
            return func_name, func_args, True

    # C9c：LLM 偶爾輸出非標準 action（increase/decrease/add…）→ 正規化成 set/read。
    # 「南倉安全庫存提高40」曾吐 action=increase 直接報「不支援的 config 動作」
    # （第10輪測試抓到）。相對意圖的 value 常沒帶正負號，一律重抽原句。
    if func_name == "manage_config":
        _act_raw = str(func_args.get("action") or "").lower()
        if _act_raw and _act_raw not in ("set", "read"):
            _norm = "set" if _act_raw in ("increase", "decrease", "add", "raise",
                                           "reduce", "update", "change", "increment",
                                           "decrement", "adjust", "modify", "write") else "read"
            func_args = {**func_args, "action": _norm}
            if _norm == "set":
                _cv9c = _extract_config_value(user_text)
                if _cv9c is not None:
                    func_args["value"] = _cv9c
            log.info(f"[校正 C9c] 非標準 action {_act_raw!r} → {_norm}")

    # C9b：manage_config 問句防護——「…設定多少」「…給我看」是查詢不是設值。
    # intent_clf 直判 manage_config 時 LLM 常自己標 action=set 且沒 value，
    # C9/C18 都沒機會翻正（第9輪測試補；第12輪補讀取語氣詞）。
    if (func_name == "manage_config" and func_args.get("action") == "set"
            and not str(func_args.get("value") or "").strip()
            and any(w in user_text for w in _CONFIG_READ_CUES)
            and _extract_config_value(user_text) is None):
        log.info("[校正 C9b] 問句語氣 → manage_config action=read")
        func_args = {**func_args, "action": "read"}

    # ── C7: 到期意圖詞 → list_expiring_items(最高優先)──
    # C0：未知函式名 → 從 user_text 推斷最接近的已知函式
    _KNOWN = {"query_inventory","query_movement","list_low_stock","list_hot_items",
               "list_expiring_items","compare_warehouses","query_related_items",
               "search_log","manage_config","run_script","generate_report","list_files",
               "set_alert","generate_po","compare_periods",
               "set_schedule","list_schedules","delete_schedule",
               "list_alerts","delete_alert","create_movement","create_transfer"}
    if func_name not in _KNOWN:
        log.info(f"[校正 C0] 未知函式 {func_name!r}，嘗試從 user_text 推斷")
        # 用 C8-C16 的 intent 詞來推斷
        if _has_rca_word(user_text):
            func_name, func_args = "search_log", {"keyword": _extract_sku_keyword(user_text) or ""}
        elif any(w in user_text for w in ("安全庫存","前置天數","補貨天數","補貨頻率","庫存上限","庫存下限")):
            _c0_action = "set" if any(v in user_text for v in ("改","設","調","成")) else "read"
            func_name, func_args = "manage_config", {"action": _c0_action, "key": "", "value": ""}
        elif any(w in user_text for w in ("採購","補貨單","叫貨")):
            func_name, func_args = "generate_po", {"source": "low_stock"}
        elif any(w in user_text for w in ("報告","報表","體檢")):
            func_name, func_args = "generate_report", {"report_type": "full"}
        elif any(w in user_text for w in ("通知","提醒","警示")):
            func_name, func_args = "set_alert", {"condition": "below_safety", "target": ""}
        else:
            func_name, func_args = "query_inventory", {"keyword": _extract_sku_keyword(user_text) or ""}

    #   排除：含「報告/報表」時讓給 C12 generate_report（出報告 ≠ 查清單）。
    _has_report = any(w in user_text for w in ("報告", "報表", "彙整"))
    _has_alert = any(w in user_text for w in ("通知", "提醒", "就通知", "就提醒", "警示我"))
    # 「系統壞掉了啦」的「壞掉」是抱怨系統不是問效期（conv100-r9）→ 排除機器語境
    _c7_sys_ctx = any(w in user_text for w in ("系統", "當機", "網站", "機器", "程式", "app"))
    # EN build：'expiry alerts' / 'expiry alert list' 的 alert 讓 _has_alert
    #   成立而被排除 → 落回全店概覽。到期詞緊鄰 alert 時＝要看到期清單，
    #   不是要**設定**警示（設定句會有 me/when/below 等）。
    _c7_expiry_alert = bool(_re.search(
        r"\bexpir(?:y|ing|ation)\s+(?:alerts?|warnings?|list)\b", text_low)
        and not _re.search(r"\b(?:alert|notify|warn|remind)\s+me\b|\bwhen\b|"
                           r"\bbelow\b|\bunder\b|\bdrops?\b", text_low))
    if (any(kw in user_text for kw in _EXPIRING_INTENT_WORDS) or
        any(kw in text_low for kw in _EXPIRING_INTENT_WORDS)) and not _has_report \
            and (not _has_alert or _c7_expiry_alert) \
            and not _c7_sys_ctx:
        # category 幻覺防呆：句中沒類別詞就丟棄（「到期壓力最大的是哪批貨」被 LLM
        # 塞 apparel 回「服飾類沒有快到期」漏報全局，conv100-r6）
        _c7_cat_words = {"electronics": ("電子", "3c"), "appliance_kitchen": ("家電", "廚具", "廚房"),
                         "food_beverage": ("食品", "飲料"), "daily_goods": ("日用", "生活用品"),
                         "apparel": ("服飾", "衣服", "服裝"), "sports": ("運動", "露營", "戶外")}
        _c7_cat = func_args.get("category")
        _c7_cat_ok = _c7_cat in VALID_CATEGORIES and any(
            w in user_text for w in _c7_cat_words.get(_c7_cat, ()))
        # r17：帶商品名/類別詞的到期問句要真的過濾——「這批咖啡豆什麼時候到期」
        # 「快到保存期限的飲料有哪些」曾一律回全倉總覽（答非所問）
        import warehouse as _W_c7
        _c7_kw = _extract_sku_keyword(user_text)
        _c7_km = _W_c7.match_items(_c7_kw) if _c7_kw else []
        _c7_kw_name = _c7_km[0]["item"]["name"] if (_c7_km and _c7_km[0].get("score", 0) >= 3) else ""
        # kw 要接地於「剝掉到期語」後的原句（r22：「保鮮期倒數的有哪些」的
        # 「保鮮」錨到玻璃保鮮盒 → 回單品到期=答非所問，應回全倉總覽）
        if _c7_kw_name:
            import re as _re_c7g
            _c7_gtxt = _re_c7g.sub(r"保鮮期|效期|到期|過期|保存期限|賞味", "", user_text)
            if not _kw_grounded(_c7_kw_name, _c7_gtxt):
                _c7_kw_name = ""
        # 指名了商品但庫裡沒有（r18：「香皂快過期了嗎」曾回全倉到期總覽，
        # 讓人以為有賣香皂）→ 轉庫存查詢讓它誠實回「找不到香皂」。
        # kw 常黏著到期語（「香皂快過期」）→ 先剝掉再判斷殘餘名詞。
        import re as _re_c7
        _c7_resid = _re_c7.sub(r"(?:快要|快|已經|要)?(?:過期|到期|壞掉|不能賣|即期品|即期)(?:了)?(?:嗎|沒)?"
                               # 時間殘字（r20：「南倉這個月會過期的」kw 殘「月會」誤當商品）
                               r"|這個月|上個月|下個月|本月|月底|月初|近期|會"
                               r"|這週|本週|下週|上週|這禮拜|週"
                               r"|[0-9零一二兩三四五六七八九十百千]+天內?|幾天內?|天內", "",
                               (_c7_kw or "")).strip()
        if (_c7_resid and not _c7_kw_name and " " not in _c7_resid
                and 2 <= len(_c7_resid) <= 6 and _re_c7.fullmatch(r"[一-鿿]+", _c7_resid)
                and not any(g in _c7_resid for g in ("期限", "保存", "東西", "商品",
                                                      "什麼", "哪些", "批", "飲料", "食品",
                                                      # r27：清單/先列十個/一內 等殘渣曾被當
                                                      # 商品名回「找不到清單」（26/27/94 三連破）
                                                      "清單", "列", "個", "內", "先", "以", "倉",
                                                      # r28：「南倉的到期警示」殘「警示」
                                                      "警示", "警報", "提醒"))
                and not _W_c7.match_items(_c7_resid)):
            log.info(f"[校正 C7] 到期問句指名未知商品「{_c7_resid}」→ query_inventory 誠實回找不到")
            return "query_inventory", {"keyword": _c7_resid}, True
        _c7_cat_from_text = next((c for z, c in (
            ("電子", "electronics"), ("3c", "electronics"),
            ("家電", "appliance_kitchen"), ("廚具", "appliance_kitchen"),
            ("食品", "food_beverage"), ("飲料", "food_beverage"), ("飲品", "food_beverage"),
            ("日用", "daily_goods"), ("服飾", "apparel"), ("衣服", "apparel"),
            ("運動", "sports"), ("露營", "sports"), ("戶外", "sports")) if z in user_text), None)
        if func_name != "list_expiring_items":
            log.info(f"[校正 C7] {func_name} → list_expiring_items (到期意圖)")
            new_args = {}
            if func_args.get("warehouse") in VALID_WAREHOUSES:
                new_args["warehouse"] = func_args["warehouse"]
            else:
                # 倉名從原句抽（r20：「北倉有什麼快到期」曾回全倉總覽）
                for _zh7, _en7 in _WH_ZH_MAP.items():
                    if _zh7 in user_text and _en7 != "all":
                        new_args["warehouse"] = _en7
                        break
            if _c7_cat_ok:
                new_args["category"] = _c7_cat
            elif _c7_cat_from_text:
                new_args["category"] = _c7_cat_from_text
            if _c7_kw_name:
                new_args["keyword"] = _c7_kw_name
            return "list_expiring_items", new_args, True
        else:
            if _c7_cat and not _c7_cat_ok:
                func_args = {k: v for k, v in func_args.items() if k != "category"}
                log.info(f"[校正 C7] 丟棄幻覺 category={_c7_cat}")
            if _c7_cat_from_text and not func_args.get("category"):
                func_args = {**func_args, "category": _c7_cat_from_text}
                log.info(f"[校正 C7] 補 category={_c7_cat_from_text}")
            if _c7_kw_name and not func_args.get("keyword"):
                func_args = {**func_args, "keyword": _c7_kw_name}
                log.info(f"[校正 C7] 補 keyword={_c7_kw_name!r}")

    # ── C3: 缺貨意圖詞 → list_low_stock（最高優先、bypass 其他校正）──
    #   排除：句中含設定項詞（安全庫存/前置天數）時讓給 C9；含報表/報告詞時讓給 C12。
    # 「天數」入列：「中倉補貨天數縮短成3天」的「補貨」曾把 config 句搶成缺貨清單（conv100-r5）
    # EN build：設定關鍵詞補英文。「safety stock」同時在 _LOW_STOCK_INTENT_WORDS
    #   裡（"whats below safety stock" 是查缺貨），所以**設定句必須靠這裡讓路**，
    #   否則 'set X safety stock to 80' / 'whats the X safety stock' 會被 C3
    #   搶成缺貨清單（守衛 cfg 類 35 句全 FAIL，2026-07-25）。
    _cfg_key_in_text = any(w in user_text for w in
                           ("安全庫存", "安全存量", "安全水位", "前置天數", "補貨前置", "前置時間",
                            "天數", "警戒值", "庫存底線", "存量底線")) \
        or (any(w in text_low for w in
                ("safety stock", "safety level", "reorder point", "restock target",
                 "lead time", "safety threshold",
                 # r9：minimum/par 是安全庫存的同義說法 → 設定句
                 #   （`set X minimum stock to 80`）要讓給 C9。查詢句
                 #   （`anything below the minimum`）由下面的比較詞排除擋住。
                 "minimum stock", "min stock", "minimum level", "par level"))
            # ⚠️ 「低於安全庫存的有哪些」是**查缺貨**不是查/改設定——
            #   'items below safety stock' 原本讓路給 C9 回設定表（答非所問）。
            #   有比較詞（below/under/less than）或清單詞＝查缺貨，不讓路。
            and not _re.search(r"\b(?:below|under|less than|lower than|beneath|"
                               r"short of|not enough|running low|almost out)\b",
                               text_low))
    _report_in_text = any(w in user_text for w in ("報表", "報告", "體檢", "健檢")) \
        or any(w in text_low for w in ("report", "summary"))
    # 「告訴我」收斂成「就告訴我」：「見底的貨順便告訴我要補幾個」不是警示設定，
    # 曾被這裡排除掉落到 C6 亂轉 related（conv100-r5）
    _alert_in_text = any(w in user_text for w in ("通知", "提醒", "警示我", "就通知", "就提醒", "就告訴我",
                                                   # r19：「幫瑜珈墊設缺貨警示」是設定警示不是查缺貨清單
                                                   "設缺貨警示", "設警示", "設庫存警示", "加警示",
                                                   "建警示", "設個警示", "加個警示")) \
                     or any(w in text_low for w in
                            # EN build：英文「設定警示」語（原排除詞全中文 →
                            #   'alert me when earphones drop below 30' 因含 'alert'
                            #   命中 _LOW_STOCK_INTENT_WORDS 被 C3 搶成缺貨清單，
                            #   而 set_alert 才是正解）
                            ("alert me", "notify me", "warn me", "remind me", "tell me when",
                             "let me know when", "set an alert", "set alert", "create an alert",
                             "drops below", "drop below", "goes under", "falls below",
                             "when it drops", "if it drops"))
    # 「叫貨」從 PO 排除詞移除：叫貨=缺貨要補的查詢語意，讓 C3 轉 low_stock
    # （開採購單是「採購單/下單/產採購/補貨單」等明確 PO 詞，RPI5 conv100-r2）
    # EN build：英文開單詞也要讓路——'create a purchase order for low stock
    #   items' 的 "low stock" 命中缺貨詞表，被 C3 搶成缺貨清單
    #   （clf 判 generate_po conf=1.00 卻沒生效）。這是能力地圖直接列出的
    #   範例句，訪客一點就得到非預期結果。
    _po_in_text = (any(w in user_text for w in ("採購單", "下單", "產採購", "補貨單"))
                   or bool(_re.search(
                       r"\b(?:create|generate|make|draft|raise|issue|open|prepare|"
                       # 2026-08-04：'i need a purchase order …' 的需要句——
                       #   D3 修了 _detect_clarify 那份,這裡是同款正則的
                       #   另一份（坑 28）,沒補會被 C3 搶成 list_low_stock
                       r"give me|build|i\s+need|i\s+want|we\s+need|"
                       r"need\s+an?|want\s+an?)\b[^.]{0,30}?"
                       r"\b(?:po|purchase orders?)\b",
                       text_low)))
    # 「XX最近有補貨嗎」是問進貨紀錄不是缺貨清單 → 讓給 C7b movement（conv100-r13）
    _mv_q_in_text = any(w in user_text for w in ("有補貨", "有進貨", "補過貨", "進過貨"))
    # EN build：到期詞在場讓給 list_expiring_items——'expiry alerts' 的
    #   'alert' 命中缺貨詞表被 C3 搶成缺貨清單（到期跟缺貨是兩件事）
    _expiry_in_text = bool(_re.search(
        r"\bexpir(?:y|ing|es?|ation)\b|\bshelf\s*life\b|\bbest\s*before\b|"
        r"\buse\s*by\b|\bgoing\s+bad\b|\bpast\s+(?:its\s+)?date\b", text_low))
    # ── EN build（劇情批 r5）：**單品**是否低於安全庫存 → 讓給 query_inventory ──
    #   'electric toothbrush stock' → 'is it below safety stock' 時，
    #   carry-over 正確補上 keyword='Electric Toothbrush'，但 C3 轉去的
    #   `list_low_stock` **不吃 keyword**（warehouse.py 直接「忽略未知參數」）
    #   → 退化成全店 43 筆清單＝答非所問，而系統其實知道問的是哪個商品。
    #   單品庫存卡本就會標 ⚠️ Below safety stock，正是訪客要的答案。
    #   ⚠️ 只在「指代單品」時讓路：句中要有 it/this/that 之類指代或明確商品名，
    #   且**不可**有複數/全店語（which items / anything / what's running low），
    #   否則會把正常的缺貨清單查詢也搶走。
    #   ⚠️ 2026-08-03（資料邊界批）：原本只認「指代詞」那一半，
    #   `is wireless mouse below safety stock`（句中直接講商品名、無指代）
    #   落不進來 → 又退化成全店 43 筆清單（同一個坑的另一個入口）。
    #   註解本來就寫「指代**或明確商品名**」，實作漏了後者 → 補上。
    #   商品名分支要**接地**（match 分數 ≥4）才算，避免把 `is anything below
    #   safety stock` 這種全店問法搶走。
    _c3_ref_word = bool(_re.search(
        r"\b(?:is|are|was)\s+(?:it|this|that|the\s+\w+)\b|\bit'?s\b", text_low))
    _c3_named_item = False
    _c3_named_kw = ""          # 接地成功的商品名 → 下面直接複用，不要再抽一次
    if _re.search(r"\b(?:is|are|was|has|does)\b", text_low):
        try:
            import warehouse as _W_c3n
            _kw_c3n = _extract_sku_keyword(user_text) or ""
            if _kw_c3n:
                _m_c3n = _W_c3n.match_items(_kw_c3n)
                if _m_c3n and _m_c3n[0].get("score", 0) >= 4:
                    _c3_named_item = True
                    _c3_named_kw = _kw_c3n
        except Exception:
            _c3_named_item = False
    _c3_single_ref = (_c3_ref_word or _c3_named_item) \
        and not _re.search(
            r"\b(?:which|what|anything|everything|all|any)\s+(?:items?|ones?|"
            r"products?|things?)\b|\bwhats?\s+(?:running|getting|low)\b|"
            r"\bitems?\s+(?:are|is)\b", text_low)
    _c3_single_low = (_is_mostly_english(user_text) and _c3_single_ref
                      and _re.search(r"\bbelow\s+(?:the\s+)?safety|\bunder\s+(?:the\s+)?safety|"
                                     r"\bbelow\s+(?:the\s+)?(?:minimum|threshold)", text_low))
    if _c3_single_low:
        # ⚠️ 只「讓路」不夠——實測讓給 C3 之後，下游 C9（設定意圖）又把它轉成
        #   `manage_config{read}`，而 manage_config **也不吃 keyword**
        #   （同一句 log 出現第二次「忽略未知參數」）→ 從全店缺貨清單變成
        #   全店安全庫存設定表，一樣答非所問。必須 hard-return 定案。
        #   keyword 留空：交給下游 carry-over 從 context 補上（它本來就補得對，
        #   問題從頭到尾都是「補上的 keyword 被丟給不收 keyword 的 tool」）。
        log.info(f"[C3-single-en] {user_text!r} 單品安全庫存詢問 → query_inventory")
        _c3_sl_args = {}
        # ⚠️ LLM 給的 keyword 常是幻覺（本句實測是 'bottleneck'，句中根本沒有）
        #   → 必須接地：字面要在原句出現，且能對到商品。接不到就留空，
        #   讓 carry-over 從 context 補（那條路徑本來就對）。
        _c3_sl_kw = (func_args.get("keyword") or "").strip()
        if _c3_sl_kw and _c3_sl_kw.lower() in text_low:
            try:
                import warehouse as _W_c3sl
                _m_c3sl = _W_c3sl.match_items(_c3_sl_kw)
                if _m_c3sl and _m_c3sl[0].get("score", 0) >= 4:
                    _c3_sl_args["keyword"] = _c3_sl_kw
            except Exception:
                pass
        # ⚠️ 2026-08-03：LLM 不一定給 keyword（實測 `is bluetooth speaker under
        #   safety stock` 給的是 category=electronics）→ 空 keyword 就回全店概覽，
        #   等於白讓路。進入條件已經接地抽出商品名了，直接複用（坑 16 的另一面：
        #   上游抽對了、下游沒接）。
        if not _c3_sl_args.get("keyword") and _c3_named_kw:
            _c3_sl_args["keyword"] = _c3_named_kw
        return "query_inventory", _c3_sl_args, True

    if (any(kw in user_text for kw in _LOW_STOCK_INTENT_WORDS) or
        any(kw in text_low for kw in _LOW_STOCK_INTENT_WORDS)) \
       and not _cfg_key_in_text and not _report_in_text \
       and not _alert_in_text and not _po_in_text and not _mv_q_in_text \
       and not _expiry_in_text and not _c3_single_low:
        # category 幻覺防呆：LLM 常憑空抽 category（「哪些品項低於警戒線」給
        # food_beverage 只回 4 項，conv100-r5）→ 句中沒類別詞就丟棄
        _c3_cat_words = {"electronics": ("電子", "3c"), "appliance_kitchen": ("家電", "廚具", "廚房"),
                         "food_beverage": ("食品", "飲料"), "daily_goods": ("日用", "生活用品"),
                         "apparel": ("服飾", "衣服", "服裝"), "sports": ("運動", "健身")}
        _c3_cat = func_args.get("category")
        _c3_cat_ok = _c3_cat in VALID_CATEGORIES and any(
            w in user_text for w in _c3_cat_words.get(_c3_cat, ()))
        if func_name != "list_low_stock":
            log.info(f"[校正 C3] {func_name} → list_low_stock (缺貨意圖)")
            new_args = {}
            # 保留 warehouse / category（若 LLM 有抽且句中真有講）
            if func_args.get("warehouse") in VALID_WAREHOUSES:
                new_args["warehouse"] = func_args["warehouse"]
            else:
                # 倉名從原句抽（r20：「北倉有什麼快沒了」曾回全部倉清單）
                _c3_whs = {z[0] for z in ("北倉", "北區", "中倉", "中區", "南倉", "南區")
                           if z in user_text}
                if len(_c3_whs) == 1:
                    for _zh3, _en3 in _WH_ZH_MAP.items():
                        if _zh3 in user_text and _en3 != "all":
                            new_args["warehouse"] = _en3
                            break
            if _c3_cat_ok:
                new_args["category"] = _c3_cat
            elif not new_args.get("category"):
                # 類別從原句補（r21：「電子產品類缺貨的」曾回全部類別清單）
                for _zh3c, _cat3c in {"電子": "electronics", "3c": "electronics",
                                       "家電": "appliance_kitchen", "廚具": "appliance_kitchen",
                                       "食品": "food_beverage", "飲料": "food_beverage",
                                       "日用": "daily_goods", "清潔": "daily_goods", "服飾": "apparel", "衣服": "apparel",
                                       "運動": "sports", "露營": "sports"}.items():
                    if _zh3c in user_text:
                        new_args["category"] = _cat3c
                        break
            return "list_low_stock", new_args, True
        else:
            # LLM 已正確輸出 list_low_stock，但後續 C14 看到「警示」會誤覆蓋成 set_alert
            # → hard-return 防止被後面規則（C14 等）推翻
            if not func_args.get("warehouse"):
                _c3w = {z[0] for z in ("北倉", "北區", "中倉", "中區", "南倉", "南區")
                        if z in user_text}
                if len(_c3w) == 1:
                    for _zh3b, _en3b in _WH_ZH_MAP.items():
                        if _zh3b in user_text and _en3b != "all":
                            func_args = {**func_args, "warehouse": _en3b}
                            break
            if func_args.get("category") not in VALID_CATEGORIES:
                # 類別從原句補（r21：「電子產品類缺貨的」曾回全部類別，else 分支同款漏）
                for _zh3d, _cat3d in {"電子": "electronics", "3c": "electronics",
                                       "家電": "appliance_kitchen", "廚具": "appliance_kitchen",
                                       "食品": "food_beverage", "飲料": "food_beverage",
                                       "日用": "daily_goods", "清潔": "daily_goods", "服飾": "apparel", "衣服": "apparel",
                                       "運動": "sports", "露營": "sports"}.items():
                    if _zh3d in user_text:
                        func_args = {**func_args, "category": _cat3d}
                        break
            if _c3_cat and not _c3_cat_ok:
                func_args = {k: v for k, v in func_args.items() if k != "category"}
                log.info(f"[校正 C3] 丟棄幻覺 category={_c3_cat}")
            return func_name, func_args, True

    # ── C-CmpMv（r20）：「今天進的貨跟出的貨各多少」LLM 誤投 compare（答非
    # 所問回倉庫比較）→ 進出統計。兩倉名的真比較句不受影響。──
    if (func_name == "compare_warehouses"
            and any(w in user_text for w in ("進的貨", "出的貨", "進貨", "出貨",
                                              "進了", "出了", "進出"))
            and len({z[0] for z in ("北倉", "北區", "中倉", "中區", "南倉", "南區")
                     if z in user_text}) < 2):
        _cmv_p = ("today" if any(w in user_text for w in ("今天", "今日")) else
                  "yesterday" if "昨天" in user_text else
                  "this_month" if any(w in user_text for w in ("本月", "這個月")) else
                  "this_week")
        log.info("[校正 C-CmpMv] compare 誤投進出統計 → query_movement")
        return "query_movement", {"period": _cmv_p, "direction": "both"}, True

    # ── C9-key（r25）：manage_config 的 key 以原句「最長」設定項詞覆寫——
    # 「安全水位倍數現在是多少」LLM 只抽到「安全水位」→ 錯回 safety_stock 表 ──
    if func_name == "manage_config":
        _k9 = max((w for w in _CONFIG_KEY_WORDS if w in user_text), key=len, default=None)
        if _k9 and _k9 != func_args.get("key"):
            func_args = {**func_args, "key": _k9}
            log.info(f"[校正 C9-key] key 以原句最長設定項覆寫 → {_k9!r}")
        # ── EN build C9-act：LLM 把英文設定句判成 read 時要改回 set。
        #    `reduce mouse safety stock by 10` LLM 給 action=read → 照 read
        #    走變成查詢，設定完全沒生效（比報錯更糟：訪客以為改好了）。
        #    條件從嚴：**同時**有設定動詞與抽得到的設定值才覆寫，
        #    純查詢句（`whats the safety stock`）抽不到值不會誤中。
        if func_args.get("action") != "set":
            _sv9 = _extract_config_value(user_text)
            if _sv9 is not None and any(w in text_low for w in _CONFIG_SET_WORDS):
                func_args = {**func_args, "action": "set", "value": _sv9}
                log.info(f"[校正 C9-act] 設定動詞+數值 → action=set value={_sv9!r}")
        # r26：value 也以原句抽取覆寫——「改成兩萬」LLM 幻覺 value=2000 開出
        # 錯值卡×183（原句抽取=20000 會被極端值防呆擋下）
        if func_args.get("action") == "set":
            _cv9 = _extract_config_value(user_text)
            if _cv9 is not None and str(func_args.get("value")) != _cv9:
                func_args = {**func_args, "value": _cv9}
                log.info(f"[校正 C9-val] value 以原句抽取覆寫 → {_cv9!r}")

    # ── C9d（r17）：安全庫存「排名」問句 → config read ──
    # 「安全庫存最高的是哪個商品」曾回庫存排行 TOP10（庫存最多≠安全庫存最高，
    # 資料完全不同=誤導）。無 set 數值 + 排名詞 → 回安全庫存設定表（誠實）。
    if (any(w in user_text for w in ("安全庫存", "安全水位", "警戒值", "安全線"))
            and any(w in user_text for w in ("最高", "最低", "排行", "排名"))
            and _extract_config_value(user_text) is None):
        log.info("[校正 C9d] 安全庫存排名問句 → manage_config read")
        return "manage_config", {"action": "read", "key": "安全庫存"}, True

    # ── C3e（r22）：LLM 幻覺 list_low_stock 防閘——句中完全沒有缺貨語彙
    # （「幫我看那個...忘記叫什麼」「上次那個藍牙的東西還有嗎」曾回缺貨清單）
    # → 有扎實商品線索轉查庫存，否則回概覽。真缺貨句必含 C3 詞表詞不受影響。
    if func_name == "list_low_stock":
        # r30：cat 幻覺通殺補 low_stock 未經 C3 的路徑（「庫存最危險的三個」
        # LLM 自帶 category=food 曾把全倉清單濾成 4 項）
        _c3f_cat = func_args.get("category")
        if _c3f_cat:
            _c3f_words = {"electronics": ("電子", "3c"), "appliance_kitchen": ("家電", "廚具", "廚房"),
                          "food_beverage": ("食品", "飲料"), "daily_goods": ("日用", "生活用品", "清潔"),
                          "apparel": ("服飾", "衣服", "服裝"), "sports": ("運動", "健身", "露營")}
            if not any(w in user_text for w in _c3f_words.get(_c3f_cat, ())):
                func_args = {k: v for k, v in func_args.items() if k != "category"}
                log.info(f"[校正 C3f] low_stock 丟棄幻覺 category={_c3f_cat}")
        _c3e_low = (any(w in user_text for w in _LOW_STOCK_INTENT_WORDS)
                    or any(w in text_low for w in _LOW_STOCK_INTENT_WORDS)
                    or any(w in user_text for w in ("警示", "缺", "補", "低於", "安全",
                                                     "斷", "沒了", "沒貨", "不行",
                                                     "告急", "危", "急"))
                    # r27：「不夠」保留但豁免「夠不夠/還夠」可得性問句——「電熨斗還
                    # 夠不夠」RPI5 LLM 直投 low_stock 曾被這裡擋掉救援
                    or ("不夠" in user_text and "夠不夠" not in user_text and "還夠" not in user_text))
        # 熱銷/滯銷詞在場讓給 C4 轉排行（r22 smoke：「賣不好的有哪些」曾被
        # 這裡搶成概覽，該滯銷榜）
        _c3e_hotslow = (any(w in user_text for w in _HOT_INTENT_WORDS_HOT)
                        or any(w in user_text for w in _HOT_INTENT_WORDS_SLOW)
                        # 趨勢句讓路（2026-08-04）：'which items grew the most'
                        #   clf 誤判 list_low_stock,C3e 概覽 hard-return 搶在
                        #   C16 趨勢轉換之前 → 'grew' 被當商品名查。
                        #   讓路後 C16 的 grew/dropped 正則轉 compare_periods。
                        or bool(_re.search(
                            r"\bgrew\b|\bgrowth\b|\bdropped\b|\brose\b|\bfell\b|"
                            r"\btrend(?:ing)?\b|\bmonth[- ]over[- ]month\b",
                            text_low)))
        # RCA 詞也放行讓 C8 轉 search_log（r22 RPI5：「庫存少得莫名其妙」
        # 曾被這裡搶成單品庫存）
        if not _c3e_low and not _c3e_hotslow and not _has_rca_word(user_text) \
                and not _C4MV_RE.search(user_text):
            # （r44：進出量問句整塊讓給 C4-mv 轉 movement——商品線索與概覽兩個
            #   hard-return 出口都曾把「咖啡機今天出貨了沒」定死）
            import warehouse as _W3e
            _c3e_kw = _extract_sku_keyword(user_text)
            _c3e_m = _W3e.match_items(_c3e_kw) if _c3e_kw else []
            # ⚠️ EN build（語音）：英文要求**更高分數**才算「商品線索」——
            #   score>=2 在英文太鬆，一個同時是功能詞的單詞就滿足：
            #   `which items are running well` 的 **running** 撈到
            #   Running Shoes Men's → clf 正確判的 list_low_stock 被改成
            #   「跑鞋庫存」（訪客問的是哪些商品狀況好）。
            #   同坑 1（短字串 substring 在英文必然誤爆）＋ 記憶裡「keyword
            #   分數 ≥8 才算扎實」的既有標準。中文維持 >=2（中文詞不會這樣撞）。
            _c3e_need = 8 if _is_mostly_english(user_text) else 2
            if _c3e_m and _c3e_m[0].get("score", 0) >= _c3e_need:
                log.info(f"[校正 C3e] low_stock 幻覺+商品線索 → query_inventory kw={_c3e_kw!r}")
                return "query_inventory", {"keyword": _c3e_kw}, True
            # r30：概覽 fallback 前先試類別詞（「北倉的家電類庫存總覽」RPI5 LLM
            # 投 low_stock，這裡曾直接回 60 項概覽——hard-return 出口要自帶推導）
            _c3e_cat_map = {"電子": "electronics", "家電": "appliance_kitchen",
                            "廚具": "appliance_kitchen", "食品": "food_beverage",
                            "飲料": "food_beverage", "日用": "daily_goods",
                            "清潔": "daily_goods", "服飾": "apparel", "衣服": "apparel",
                            "運動": "sports"}
            for _zh3e, _en3e in sorted(_c3e_cat_map.items(), key=lambda x: -len(x[0])):
                if (_zh3e + "類") in user_text or (_zh3e + "用品") in user_text or (_zh3e + "產品") in user_text:
                    _c3e_args = {"category": _en3e}
                    for _zhw3e, _enw3e in _WH_ZH_MAP.items():
                        if _zhw3e in user_text and _enw3e != "all":
                            _c3e_args["warehouse"] = _enw3e
                            break
                    log.info(f"[校正 C3e] 概覽前撿到類別詞 → category={_en3e}")
                    return "query_inventory", _c3e_args, True
            log.info("[校正 C3e] low_stock 幻覺無缺貨語 → 概覽")
            return "query_inventory", {}, True
    # ── C4: 熱銷 / 滯銷意圖詞 → list_hot_items ──
    is_hot = any(kw in user_text for kw in _HOT_INTENT_WORDS_HOT) or \
             any(kw in text_low for kw in _HOT_INTENT_WORDS_HOT)
    # ⚠️ EN build（守衛回歸 891→887）：**意圖詞被商品名吸收**時不算意圖。
    #   'hot cocoa availability' 的 hot 是 Hot Cocoa Powder 的一部分，
    #   卻讓 is_hot=True → C4-prod 把庫存查詢轉成該商品的進出貨。
    #   在源頭排除，下游（C4/C4b/C4-prod）全部受益。
    #   ⚠️ 不能只用字面比對（'whats the hot cooa count' 錯字就漏掉）→
    #   改判「抽出的商品名本身含該意圖詞」＝意圖詞被商品名吸收。
    if is_hot and _is_mostly_english(user_text):
        try:
            import warehouse as _W_hn
            _hn_kw = _extract_sku_keyword(user_text)
            _hn_m = _W_hn.match_items(_hn_kw) if _hn_kw else []
            if _hn_m and _hn_m[0].get("score", 0) >= 6:
                _hn_name = _hn_m[0]["item"]["name"].lower()
                if any(w.lower() in _hn_name for w in _HOT_INTENT_WORDS_HOT
                       if w.isascii() and len(w) >= 3):
                    log.info(f"[C4] 意圖詞被商品名吸收（{_hn_m[0]['item']['name']}）"
                             f"→ 不算熱銷意圖: {user_text!r}")
                    is_hot = False
        except Exception:
            pass
    is_slow = any(kw in user_text for kw in _HOT_INTENT_WORDS_SLOW) or \
              any(kw in text_low for kw in _HOT_INTENT_WORDS_SLOW)
    # 連帶意圖詞在場時熱銷不搶——「帳篷跟什麼一起賣最多」的「賣最多」是
    # 連帶語境，不是排行榜（第14輪抓到）
    _c4_related_block = any(w in user_text for w in _RELATED_INTENT_WORDS)
    # 帶具體商品名的熱銷問句（「輕量羽絨外套最近賣得如何」）是問該商品銷況，
    # 回全類別排行答非所問 → 轉該商品 movement（conv100-r13）
    _c4mv = _C4MV_RE.search(user_text)
    # EN build：英文進出量問句的 LLM 常吐 search_log（clf 判 query_movement
    #   conf=1.00 但兩者都在候選內 → C18 不仲裁）→ 也要收 search_log
    if func_name in ("query_inventory", "list_low_stock",
                     *(("search_log",) if _is_mostly_english(user_text) else ())) \
            and _c4mv \
            and not any(w in user_text for w in ("還有多少", "還剩多少", "剩多少")):
        # 英文分支沒有 group(1)（中文的進/出）→ 從英文詞判方向，
        #   'in and out' 這種雙向句用 both
        if _c4mv.group(1) == "進":
            _mv_dir = "in"
        elif _c4mv.group(1) == "出":
            _mv_dir = "out"
        else:
            _t4l = user_text.lower()
            if _re.search(r"\bin\s+and\s+out\b|\bins?\s+and\s+outs?\b|"
                          r"\bmovements?\b|\bmoved\b", _t4l):
                _mv_dir = "both"
            elif _re.search(r"\b(?:shipped|went\s+out|outbound|sold)\b", _t4l):
                _mv_dir = "out"
            else:
                _mv_dir = "in"
        _mv_period = ("today" if "今天" in user_text else
                      "yesterday" if "昨天" in user_text else
                      "last_week" if any(w in user_text for w in ("上週", "上周", "上禮拜")) else
                      "this_week" if any(w in user_text for w in ("這週", "本週", "這周", "本周", "這禮拜")) else
                      "this_month")
        if _is_mostly_english(user_text):
            _t4l = user_text.lower()
            _mv_period = ("today" if "today" in _t4l else
                          "yesterday" if "yesterday" in _t4l else
                          "last_week" if _re.search(r"\blast\s+week\b", _t4l) else
                          "this_week" if _re.search(r"\bthis\s+week\b", _t4l) else
                          "this_month")
        _mv_src = _re.sub(r'[進出]貨?了?(幾|多少|了沒|沒有).*', '', user_text)
        _mv_kw = _extract_sku_keyword(_mv_src)
        # 英文：kw 要接地於原句，否則 'this months in and out'（沒指名商品）
        #   會抽到雜訊商品 → 變成單品進出貨（該回全店進出總覽）
        if _mv_kw and _is_mostly_english(user_text) and not _kw_grounded(_mv_kw, user_text):
            _mv_kw = ""
        _mv_args = {"period": _mv_period, "direction": _mv_dir}
        if _mv_kw:
            _mv_args["keyword"] = _mv_kw
        for _zh4, _en4 in _WH_ZH_MAP.items():
            if _zh4 in user_text:
                _mv_args["warehouse"] = _en4
                break
        log.info(f"[校正 C4-mv] 進出量問句 → query_movement {_mv_args}")
        return "query_movement", _mv_args, True

    # ── C4-mvg（r44）：movement keyword 幻覺接地——「昨天出的貨是哪些」（無商品）
    #   LLM 曾自帶 keyword=無線藍牙耳機 錨定單品。kw 對不上原句 → 丟棄改全商品統計。──
    if func_name == "query_movement" and func_args.get("keyword") \
            and not _kw_grounded(func_args["keyword"], user_text):
        log.info(f"[校正 C4-mvg] movement kw 不接地丟棄: {func_args['keyword']!r}")
        func_args = {k: v for k, v in func_args.items() if k != "keyword"}

    # ── C4-mvp（r20）：**期間也要接地**（同 C4-mvg 的道理）────────────
    #   `what came in yesterday` LLM 吐 period='today' → 回「Today 816 units」。
    #   數字看起來很正常，但**答的是錯的期間**——訪客不會發現（誤導級）。
    #   成因：①clf 快路徑寫死 this_month ②rescue 的期間判定全中文
    #   ③tool 對不認得的 period **靜默 fallback 成 today**（warehouse.py:869）
    #   ⇒ 句子明講了期間就以句子為準，覆蓋 LLM 的值。
    if func_name == "query_movement" and _is_mostly_english(user_text):
        _p_en = _period_from_en(user_text)
        if _p_en and func_args.get("period") != _p_en:
            log.info(f"[校正 C4-mvp] 期間接地 {func_args.get('period')!r} → {_p_en!r}")
            func_args = {**func_args, "period": _p_en}

    if (is_hot or is_slow) and not _c4_related_block \
            and not any(w in user_text for w in ("類", "用品")):
        # （「露營用品類賣最好」是類別排行，fuzzy 會誤中帳篷 → 類/用品 句不轉）
        import warehouse as _W_c4p
        _c4_prod = _extract_sku_keyword(user_text)
        _c4_pm = _W_c4p.match_items(_c4_prod) if _c4_prod else []
        # r27：kw 要接地——「熱銷榜 快」的「快」曾 fuzzy 成快充線回銷況（答非所問）
        # ⚠️ EN build（r3 S9）：**裸意圖詞**不可轉商品——訪客只打 'hot'
        #   （clf 已正確判 list_hot_items conf=0.98），卻因為 hot 撞到
        #   Hot Cocoa Powder 被轉成該商品的銷況＝答非所問。
        #   整句只有那個意圖詞時，clf 的判斷才是對的。
        _c4_solo_intent = (_is_mostly_english(user_text)
                           and len(user_text.strip().strip("?.!,").split()) <= 1)
        # （'hot cocoa' 這類「意圖詞被商品名吸收」已在 is_hot 源頭排除）
        # r14+2（#42）：字面接地會被介系詞騙——'what sold over the weekend'
        #   靠 Pour-**over** 的 over 過關 → 幻覺商品銷況卡。英文句要求
        #   商品名**實詞**（≥3 字母、非介系詞）真的出現在原句。
        _c4_solid = True
        if _is_mostly_english(user_text) and _c4_prod:
            _c4_func = {"over", "under", "with", "for", "and", "per",
                        "off", "out", "the"}
            _c4_core = {w.strip(" ?.!,'\"").lower()
                        for w in _re.split(r"[\s\-/]+", user_text.lower())}
            _c4_core_s = {t.rstrip("s") for t in _c4_core}
            _c4_solid = any(
                w.lower() in _c4_core or w.lower().rstrip("s") in _c4_core_s
                for w in _re.split(r"[\s\-/]+", _c4_prod)
                if len(w) >= 3 and w.lower() not in _c4_func)
        if (_c4_pm and _c4_pm[0].get("score", 0) >= 3
                and not _c4_solo_intent and _c4_solid
                and _kw_grounded(_c4_prod, user_text)):
            _c4_period = ("this_month" if any(w in user_text for w in ("本月", "這個月", "月")) else "this_month")
            log.info(f"[校正 C4-prod] 帶商品名的銷況問句 → query_movement kw={_c4_prod!r}")
            return "query_movement", {"keyword": _c4_prod, "period": _c4_period,
                                      "direction": "both"}, True
    if (is_hot or is_slow) and not _c4_related_block and func_name != "list_hot_items":
        log.info(f"[校正 C4] {func_name} → list_hot_items ({'hot' if is_hot else 'slow'})")
        # 從 user_text 抽 period / category
        period = "this_week"
        if any(w in user_text for w in ("這季", "本季", "這一季")):
            period = "this_month"   # r25：季無資料粒度，取最接近的本月（標籤誠實顯示本月）
        if "本月" in user_text or "這個月" in user_text or "month" in text_low:
            period = "this_month"
        elif "本週" in user_text or "這週" in user_text or "這禮拜" in user_text or "week" in text_low:
            period = "this_week"
        # r25：兩類詞同句（「不要熱銷榜 我要滯銷的」）→ 後講的贏（訪客最後講的才是要的）
        if is_hot and is_slow:
            _hp = max((user_text.rfind(w) for w in _HOT_INTENT_WORDS_HOT if w in user_text), default=-1)
            _sp = max((user_text.rfind(w) for w in _HOT_INTENT_WORDS_SLOW if w in user_text), default=-1)
            _rank_c4 = "slow" if _sp > _hp else "hot"
        else:
            _rank_c4 = "slow" if is_slow else "hot"
        # r16 #34：'everything except slow movers'——排除語境反轉（只中 slow
        #   詞但訪客要的是 slow 以外＝hot）
        if (_rank_c4 == "slow" and not is_hot and _is_mostly_english(user_text)
                and _re.search(r"\b(?:except|excluding|without|besides|skip)\b"
                               r"[^.?!]{0,16}\b(?:slow|stale|dead)\b",
                               user_text.lower())):
            log.info("[校正 C4] 排除滯銷語境 → 反轉 rank=hot")
            _rank_c4 = "hot"
        new_args = {
            "rank_type": _rank_c4,
            "period":    period,
        }
        # 抽 category（若 user_text 含類別關鍵字）
        cat_zh_map = {
            "電子": "electronics", "3c": "electronics",
            "家電": "appliance_kitchen", "廚具": "appliance_kitchen",
            "食品": "food_beverage", "飲料": "food_beverage",
            "日用": "daily_goods", "清潔": "daily_goods",
            "服飾": "apparel", "衣服": "apparel",
            "運動": "sports", "露營": "sports", "戶外": "sports",
        }
        for zh, cat in cat_zh_map.items():
            if zh in user_text:
                new_args["category"] = cat
                break
        return "list_hot_items", new_args, True
    elif is_hot or is_slow:
        # LLM 已正確輸出 list_hot_items → hard-return 防後面規則推翻。
        # 但 rank_type 可能亂填（「業績最好」給 inventory 排行，conv100-r6）→ 校準
        if is_hot and is_slow:
            # r25：兩類詞同句 → 後講的贏（clf route 路徑帶進來的 rank_type 也要覆寫）
            _hp2 = max((user_text.rfind(w) for w in _HOT_INTENT_WORDS_HOT if w in user_text), default=-1)
            _sp2 = max((user_text.rfind(w) for w in _HOT_INTENT_WORDS_SLOW if w in user_text), default=-1)
            func_args = {**func_args, "rank_type": "slow" if _sp2 > _hp2 else "hot"}
            log.info(f"[校正 C4] 雙類詞後講的贏 → {func_args['rank_type']}")
        elif is_slow and func_args.get("rank_type") != "slow":
            # r27：「電子類滯銷有哪些」LLM 給 rank_type=hot（合法但方向反）→ 以原句為準
            # r16 #34：排除語境反轉——'everything except slow movers' 走這段
            #   （clf 已判 hot_items），要的是 slow 以外＝hot
            if (_is_mostly_english(user_text)
                    and _re.search(r"\b(?:except|excluding|without|besides|skip)\b"
                                   r"[^.?!]{0,16}\b(?:slow|stale|dead)\b",
                                   user_text.lower())):
                func_args = {**func_args, "rank_type": "hot"}
                log.info("[校正 C4] 排除滯銷語境 → rank_type=hot")
            else:
                func_args = {**func_args, "rank_type": "slow"}
                log.info("[校正 C4] 滯銷詞在場 → rank_type=slow")
        elif is_hot and func_args.get("rank_type") not in ("hot", "stock"):
            func_args = {**func_args, "rank_type": "hot"}
            log.info("[校正 C4] 熱銷詞在場 → rank_type=hot")
        elif func_args.get("rank_type") not in ("hot", "slow"):
            func_args = {**func_args, "rank_type": "slow" if is_slow else "hot"}
            log.info(f"[校正 C4] rank_type 校準 → {func_args['rank_type']}")
        # period 也要一起校——hard-return 會跳過 C4b（「這個月熱銷排行」曾顯示本週，conv100-r8）
        # r25：這季/本季 取最接近的 this_month（回答標示「本月」誠實呈現實際範圍）
        # EN build：詞表全中文 → 英文 'best sellers this month' 一律掉回
        #   this_week（實測回「This week…」＝答非所問）。C4b 有補 'month'，
        #   但 C4 是 hard-return 搶先，永遠輪不到 C4b（本行上方註解已預告）。
        _c4p = ("this_month"
                if (any(w in user_text for w in ("本月", "這個月", "月度", "這季", "本季", "這一季"))
                    or _re.search(r"\b(?:this|current|the)\s+(?:month|quarter)\b"
                                  r"|\bmonthly\b|\bthis quarter\b", user_text, _re.I))
                else "this_week")
        # r30：elif 分支的 category 幻覺也要接地（「這個月各類別賣最好的」LLM
        # 自帶 apparel 曾只回服飾類）
        _c4cat = func_args.get("category")
        if _c4cat:
            _c4cat_words = {"electronics": ("電子", "3c"), "appliance_kitchen": ("家電", "廚具", "廚房"),
                            "food_beverage": ("食品", "飲料"), "daily_goods": ("日用", "生活用品", "清潔"),
                            "apparel": ("服飾", "衣服", "服裝"), "sports": ("運動", "健身", "露營", "戶外")}
            # EN build：接地詞表全中文 → 英文句的正解 category 會被當幻覺丟掉（坑 3）
            _c4_grounded = (any(w in user_text for w in _c4cat_words.get(_c4cat, ()))
                            or _category_from_en(user_text) == _c4cat)
            if not _c4_grounded:
                func_args = {k: v for k, v in func_args.items() if k != "category"}
                log.info(f"[校正 C4] elif 丟棄幻覺 category={_c4cat}")
        if func_args.get("period") != _c4p:
            func_args = {**func_args, "period": _c4p}
            log.info(f"[校正 C4] period 校準 → {_c4p}")
        # category 也是（「電子產品賣得如何」曾回全類別，conv100-r8）
        if func_args.get("category") not in VALID_CATEGORIES:
            for _zh4, _cat4 in {"電子": "electronics", "3c": "electronics",
                                "家電": "appliance_kitchen", "廚具": "appliance_kitchen",
                                "食品": "food_beverage", "飲料": "food_beverage",
                                "日用": "daily_goods", "清潔": "daily_goods", "服飾": "apparel", "衣服": "apparel",
                                "運動": "sports", "露營": "sports", "戶外": "sports"}.items():
                if _zh4 in user_text:
                    func_args = {**func_args, "category": _cat4}
                    log.info(f"[校正 C4] 補 category={_cat4}")
                    break
            # EN build：中文表沒中 → 用英文類別表補（'top selling sports gear
            #   this month' 是 GUIDE_MSG 教訪客的句型）
            else:
                _cat4_en = _category_from_en(user_text)
                # r15 #69：**排除語境不補**——'best sellers excluding food'
                #   曾被補成 category=food_beverage → 回「食品類 TOP」＝
                #   與訪客要的完全相反。excluding 後的類別＝訪客不要的。
                if _cat4_en and _re.search(
                        r"\b(?:excluding|except|other than|without|besides|"
                        r"apart from|not counting)\b", user_text.lower()):
                    log.info(f"[校正 C4] 排除語境 → 不補 category={_cat4_en}")
                    _cat4_en = None
                if _cat4_en:
                    func_args = {**func_args, "category": _cat4_en}
                    log.info(f"[校正 C4] 補 category={_cat4_en}（EN）")
        return func_name, func_args, True

    # ── C4e: 存量問句被 LLM 誤投 hot_items → 攔回 inventory ──
    # 「素T還剩幾件啊順便看一下」給滯銷 TOP10 答非所問（conv100-r6）：
    # 句中無熱銷/滯銷詞、有真商品名 + 存量語氣 → 查庫存。
    if func_name == "list_hot_items":
        _c4e_kw = _extract_sku_keyword(user_text)
        import warehouse as _W_c4e
        if (_c4e_kw and _W_c4e.match_items(_c4e_kw)
                and any(w in user_text for w in ("還剩", "剩幾", "剩多少", "庫存", "還有多少", "幾件", "存量"))):
            log.info(f"[校正 C4e] 存量問句誤投 hot → query_inventory kw={_c4e_kw!r}")
            return "query_inventory", {"keyword": _c4e_kw}, True
        # ── EN 分支（2026-08-03 資料邊界批）：同 C4e 的意圖，但上面判準全中文
        #   （坑 7）→ 英文句一個都不中。實測 5/5 穩定壞掉：
        #   `which warehouse has the most wireless mouse` → LLM 給
        #   list_hot_items{rank_type:slow} → 回「本週滯銷 TOP10」，
        #   訪客問的是滑鼠在哪個倉最多。
        #   ⚠️ `list_hot_items` **不在 `_TOOL_INTENT_GUARD`**，所以沒有任何一層
        #     問過「句中有講熱銷/排行嗎」——這裡補上。
        #   判準：有接地商品名 + **句中沒有任何銷售排行語** → 不是排行問句。
        #   （`best sellers` / `what sells fastest` 等正常入口全部保留。）
        if _is_mostly_english(user_text) and _c4e_kw:
            _c4e_low = user_text.lower().replace("'", "").replace("’", "")
            _c4e_sales = _re.search(
                r"\b(?:hot|hottest|best[\s-]?sell\w*|top[\s-]?sell\w*|"
                r"popular|fastest|quickest|slow[\s-]?moving|slowest|"
                r"sells?\s+(?:best|most|fastest)|sold\s+(?:best|most)|"
                r"biggest\s+seller|rank\w*|top\s*\d+|bestseller\w*)\b", _c4e_low)
            if not _c4e_sales:
                _m_c4e_en = _W_c4e.match_items(_c4e_kw)
                if _m_c4e_en and _m_c4e_en[0].get("score", 0) >= 4:
                    log.info(f"[校正 C4e-en] 無排行語卻投 hot → query_inventory "
                             f"kw={_c4e_kw!r}")
                    return "query_inventory", {"keyword": _c4e_kw}, True

    # ── C4b: list_hot_items period + category 依 user_text 校準 ──
    # (模型對沒明講期間的 query period 不穩定、且常漏抽 category slot)
    if func_name == "list_hot_items":
        # 幻覺 category 接地（r20：「倉庫裡最多的商品」LLM 給 food_beverage
        # 「合法但沒接地」曾保留 → 回食品類排行答非所問）
        func_args = _drop_ungrounded_category(dict(func_args), user_text)
        func_args = dict(func_args)
        # r15 #69：**排除語境**——'best sellers excluding food' 的 food 被
        #   接地成 category=food_beverage → 回「食品類 TOP」＝與訪客要的
        #   **完全相反**。excluding/except 後面的類別要丟棄（回全類別排行；
        #   「真正排除該類」tool 不支援，全類別是最接近且不誤導的答案）。
        if (func_args.get("category")
                and _is_mostly_english(user_text)
                and _re.search(r"\b(?:excluding|except|other than|without|"
                               r"besides|apart from|not counting)\b",
                               user_text.lower())):
            log.info(f"[校正 C4b] 排除語境 → 丟棄 category "
                     f"{func_args.get('category')!r}（回全類別）")
            func_args.pop("category", None)
        # period:user_text 明講「本月/月」→ this_month;否則 → this_week
        if "本月" in user_text or "這個月" in user_text or "月度" in user_text or "month" in text_low:
            want_period = "this_month"
        else:
            want_period = "this_week"
        if func_args.get("period") != want_period:
            log.info(f"[校正 C4b] list_hot_items period {func_args.get('period')} → {want_period}")
            func_args["period"] = want_period
        # category:user_text 含類別詞但 args 漏抽 → 補上
        if func_args.get("category") not in VALID_CATEGORIES:
            _cat_kw = {
                "電子": "electronics", "3c": "electronics",
                "家電": "appliance_kitchen", "廚具": "appliance_kitchen", "廚房": "appliance_kitchen",
                "食品": "food_beverage", "飲料": "food_beverage",
                "日用": "daily_goods", "清潔": "daily_goods", "生活用品": "daily_goods",
                "服飾": "apparel", "衣服": "apparel", "服裝": "apparel",
                "運動": "sports", "露營": "sports", "戶外": "sports",
            }
            for zh, cat in _cat_kw.items():
                if zh in user_text:
                    log.info(f"[校正 C4b] list_hot_items 補 category={cat}")
                    func_args["category"] = cat
                    break

    # ── C4c: list_low_stock 漏抽 category → 從 user_text 補 ──
    if func_name == "list_low_stock" and func_args.get("category") not in VALID_CATEGORIES:
        _cat_kw2 = {
            "電子": "electronics", "3c": "electronics",
            "家電": "appliance_kitchen", "廚具": "appliance_kitchen",
            "食品": "food_beverage", "飲料": "food_beverage",
            "日用": "daily_goods", "清潔": "daily_goods", "服飾": "apparel", "衣服": "apparel",
            "運動": "sports",
        }
        for zh, cat in _cat_kw2.items():
            if zh in user_text:
                func_args = dict(func_args)
                func_args["category"] = cat
                log.info(f"[校正 C4c] list_low_stock 補 category={cat}")
                break

    # ── C2b: query_movement 漏抽 direction → 從 user_text 補（純「進貨」→in、「出貨」→out）──
    if func_name == "query_movement" and func_args.get("direction") not in ("in", "out", "both"):
        has_in  = ("進貨" in user_text or "入庫" in user_text or "進倉" in user_text or "inbound" in text_low)
        has_out = ("出貨" in user_text or "出庫" in user_text or "出倉" in user_text or "賣出" in user_text or "outbound" in text_low)
        if has_in and not has_out:
            func_args = dict(func_args); func_args["direction"] = "in"
            log.info("[校正 C2b] query_movement 補 direction=in")
        elif has_out and not has_in:
            func_args = dict(func_args); func_args["direction"] = "out"
            log.info("[校正 C2b] query_movement 補 direction=out")

    # C5-diff（r26）：「北倉南倉庫存差多少」兩倉相鄰無連接詞 → compare 兩倉。
    # 必須排在 C2d 之前——RPI5 的 LLM 曾投 movement{北倉南} 被 C2d hard-return
    # 成 60 項概覽，C5-diff 根本輪不到（本機 LLM 投 compare 才會過=平台分歧）。
    _diff_whs = [z for z in ("北倉", "北區", "中倉", "中區", "南倉", "南區") if z in user_text]
    _diff_keys = {z[0] for z in _diff_whs}
    if (len(_diff_keys) >= 2
            and any(w in user_text for w in ("差多少", "差幾", "差距", "誰多", "哪個多", "哪邊多", "差很多嗎"))
            and not _extract_sku_keyword(user_text)):
        _d_map = {"北": "north", "中": "central", "南": "south"}
        _d_ab = [_d_map[k] for k in ("北", "中", "南") if k in _diff_keys][:2]
        log.info(f"[校正 C5-diff] 兩倉差距問句 → compare({_d_ab[0]},{_d_ab[1]})")
        return "compare_warehouses", {"warehouse_a": _d_ab[0], "warehouse_b": _d_ab[1],
                                      "metric": "item_count"}, True

    # C-cat-short（r31）：「X類有什麼/X用品有啥」明講類別 → 類別清單
    # （「運動類有什麼」曾被 fuzzy 劫成運動毛巾單品；不能靠 C13 因其需 _inv_intent 詞）
    _ccs_m = re.search(r'(電子|家電|廚具|食品|飲料|日用|清潔|服飾|衣服|運動)(?=類|用品|產品)', user_text)
    if (_ccs_m and any(w in user_text for w in ("有什麼", "有啥", "有哪些東西", "總覽", "庫存"))
            and not any(w in user_text for w in ("缺", "熱銷", "滯銷", "到期", "賣", "進", "出"))):
        _ccs_cat = {"電子": "electronics", "家電": "appliance_kitchen", "廚具": "appliance_kitchen",
                    "食品": "food_beverage", "飲料": "food_beverage", "日用": "daily_goods",
                    "清潔": "daily_goods", "服飾": "apparel", "衣服": "apparel",
                    "運動": "sports"}[_ccs_m.group(1)]
        _ccs_args = {"category": _ccs_cat}
        for _zh_cc, _en_cc in _WH_ZH_MAP.items():
            if _zh_cc in user_text and _en_cc != "all":
                _ccs_args["warehouse"] = _en_cc
                break
        log.info(f"[校正 C-cat-short] 類別短句 → query_inventory(category={_ccs_cat})")
        return "query_inventory", _ccs_args, True

    # C-shorty-v（r31）：「查/看+短稱」極短句（查耳機）——RPI5 clf 曾丟 keyword
    # 回 60 項概覽（短輸入=產品本體，確定性直達）
    if len(user_text.replace(" ", "")) <= 6 and user_text[:1] in ("查", "看", "找"):
        _shv_txt = user_text[1:].replace("一下", "").replace("的", "").strip()
        _kw_shv = _extract_sku_keyword(_shv_txt) if _shv_txt else ""
        import warehouse as _W_shv
        _m_shv = _W_shv.match_items(_kw_shv) if _kw_shv else []
        if (_kw_shv and _m_shv and _m_shv[0].get("score", 0) >= 3
                and _kw_grounded(_kw_shv, user_text)):
            log.info(f"[校正 C-shorty-v] 查+短稱 → query_inventory({_kw_shv!r})")
            return "query_inventory", {"keyword": _kw_shv}, True

    # C-shorty（r31）：極短「倉名+商品」句（北倉耳機）——RPI5 LLM 曾回 60 項概覽
    # （短輸入=產品本體，確定性直達）
    _sh_whs = [z for z in ("北倉", "北區", "中倉", "中區", "南倉", "南區") if z in user_text]
    if _sh_whs and len(user_text.replace(" ", "")) <= 8:
        _sh_txt = user_text
        for _z_sh in _sh_whs:
            _sh_txt = _sh_txt.replace(_z_sh, "")
        _sh_txt = _sh_txt.replace("的", "").strip()
        _kw_sh = _extract_sku_keyword(_sh_txt) if _sh_txt else ""
        import warehouse as _W_sh
        _m_sh = _W_sh.match_items(_kw_sh) if _kw_sh else []
        # kw 必須是真商品（score≥3）——「上週南倉出了多少」曾被抽成「上週出」
        # 直接 OOV clarify（junk kw 老病，每個新規則出口都要驗）
        if (_kw_sh and _m_sh and _m_sh[0].get("score", 0) >= 3
                and _kw_grounded(_kw_sh, user_text)):
            _args_sh = {"keyword": _kw_sh}
            for _zh_sh, _en_sh in _WH_ZH_MAP.items():
                if _zh_sh in user_text and _en_sh != "all":
                    _args_sh["warehouse"] = _en_sh
                    break
            log.info(f"[校正 C-shorty] 倉+品極短句 → query_inventory({_kw_sh!r})")
            return "query_inventory", _args_sh, True

    # C5-rank3c（r29）：點名三倉+誰最大 → compare(all)。跟 C5-diff 同理由：
    # C5-rank3 只接 compare 分支，RPI5 的 LLM 投 hot 就繞過（func 無關規則要放前面）
    if (sum(1 for z in ("北倉", "中倉", "南倉") if z in user_text) >= 3
            and any(w in user_text for w in ("誰最大", "誰最多", "哪個最大", "誰最滿", "誰大", "最大", "最多", "最滿"))
            and not any(w in user_text for w in ("賣", "銷", "熱", "滯"))):
        log.info("[校正 C5-rank3c] 點名三倉排名 → compare(all)")
        return "compare_warehouses", {"warehouse_a": "all", "warehouse_b": "all",
                                      "metric": "item_count"}, True

    # ── C2d: 純庫存問句被 LLM 誤投 movement → 攔回 inventory ──
    # 「羽絨外套冬天快到了 倉庫有多少件」的「到了」讓 LLM 幻覺 movement，
    # 回「本週進貨 0 件」答非所問（conv100-r5）。句中有存量語氣、且完全沒有
    # 進出貨/紀錄語彙 → 查庫存。
    if func_name == "query_movement":
        _c2d_inv_cue = any(w in user_text for w in (
            "庫存", "還剩", "還有多少", "有多少", "剩多少", "存量", "多少件",
            "多少錢", "值多少", "剩幾", "還有幾"))
        # 「賣」收斂成賣了/賣出/賣掉——「這個帳篷賣多少錢」是問價不是進出（conv100-r11）
        _c2d_mv_cue = any(w in user_text for w in (
            "進", "出", "入庫", "退", "紀錄", "記錄", "明細", "異動", "流水",
            "動了", "賣了", "賣出", "賣掉", "銷", "補", "inbound", "outbound", "movement"))
        # r27：設定項詞在場（慢跑鞋安全庫存多少）→ 讓給 C9 config read，
        # C2d 搶成純庫存會丟設定意圖
        if _c2d_inv_cue and not _c2d_mv_cue and not any(w in user_text for w in _CONFIG_KEY_WORDS):
            _kw2d = _extract_sku_keyword(user_text)
            # kw 要真的比對得到商品才帶——「塞 貨」這種殘字 hard-return 後
            # 沒人清得掉，會 clarify 找不到（conv100-r8）
            import warehouse as _W2d
            _m2d = _W2d.match_items(_kw2d) if _kw2d else []
            if not _m2d or _m2d[0].get("score", 0) < 3:
                _kw2d = ""
            log.info(f"[校正 C2d] 存量問句誤投 movement → query_inventory kw={_kw2d!r}")
            _a2d = {"keyword": _kw2d} if _kw2d else {}
            # 兩倉以上 → 不塞單倉 filter，回三倉分佈（r19 RPI5 抓到：「北倉跟
            # 中倉的濾掛咖啡各剩多少」C2d 塞 north 只回北倉）
            _c2d_whs = {z[0] for z in ("北倉", "北區", "中倉", "中區", "南倉", "南區")
                        if z in user_text}
            if len(_c2d_whs) == 1:
                for _zh2d, _en2d in _WH_ZH_MAP.items():
                    if _zh2d in user_text:
                        _a2d["warehouse"] = _en2d
                        break
            return "query_inventory", _a2d, True

    # ── C6: 連帶意圖詞 → query_related_items ──
    _has_related = any(kw in user_text for kw in _RELATED_INTENT_WORDS) or \
                   any(kw in text_low for kw in _RELATED_INTENT_WORDS)
    # 「順便問/順便看/順便查」是語氣詞（順便＝附帶問一句），不是「順便買」的
    # 連帶購物意圖。若唯一命中的連帶詞是「順便」且後面接問句動詞，且沒有其他
    # 明確連帶詞（買/一起/搭配/連帶/夥伴/加購/還會買），則不觸發 C6。
    # （RPI5 壓測 v21 抓到：「順便問一下USB風扇還有嗎」被誤判 related）
    if _has_related:
        _strong_related = ("連帶", "也買", "還會買", "一起買", "順便買", "搭配",
                           "帶動", "好夥伴", "加購", "一起結帳", "還會拿", "還買")
        _has_strong = any(w in user_text for w in _strong_related)
        _shunbian_ask = any(p in user_text for p in
                            ("順便問", "順便看", "順便查", "順便瞧", "順便瞄", "順便了解"))
        if _shunbian_ask and not _has_strong:
            _has_related = False
            log.info(f"[校正 C6-skip] 「順便問/看/查」為語氣詞非連帶意圖: {user_text!r}")
    if _has_related:
        # keyword:LLM 已抽的要先驗證比對得到商品才用（「帳篷跟什麼一起賣最多」
        # LLM 曾把整句當 keyword → related_empty，第14輪抓到），否則從
        # user_text 去掉意圖詞+雜詞重抽
        import warehouse as _WC6
        kw = func_args.get("keyword")
        if kw and not _WC6.match_items(kw):
            kw = None
        if not kw:
            cleaned = user_text
            for w in _RELATED_INTENT_WORDS + (
                "買", "的人", "什麼", "啥", "哪些", "通常", "跟", "和",
                "查", "看", "會", "還", "了", "嗎", "呢", "?", "？",
                "的有", "有哪", "的", "有", "商品", "產品",
                "一起", "賣最多", "最常", "拿",
            ):
                cleaned = cleaned.replace(w, " ")
            cleaned = " ".join(cleaned.split())
            kw = cleaned if len(cleaned) >= 2 else ""
            # 清完還是比對不到 → 用多層 fuzzy 抽取器兜底
            if not kw or not _WC6.match_items(kw):
                _kw6 = _extract_sku_keyword(user_text)
                if _kw6 and _WC6.match_items(_kw6):
                    kw = _kw6
        if func_name != "query_related_items":
            log.info(f"[校正 C6] {func_name} → query_related_items (連帶意圖)")
            return "query_related_items", {"keyword": kw}, True
        else:
            # LLM 已正確輸出 query_related_items，但 keyword 可能漏/髒、category
            # 可能幻覺（「智慧手環的連帶商品」曾被塞 apparel 濾成找不到，conv100-r13）
            # r18：塞回去的 kw 也要比對扎實——「買吹風機的人還會買什麼」的髒 kw
            # 「買吹風機 人 會買什麼」靠單字「人」錨到露營帳篷 4人（score=1）。
            # 比不扎實就整個清掉，讓 tool 回 related_help 請訪客講商品名。
            _kw6_m = _WC6.match_items(kw) if kw else []
            if kw and _kw6_m and _kw6_m[0].get("score", 0) >= 3 \
                    and (not func_args.get("keyword")
                         or not _WC6.match_items(func_args.get("keyword", ""))):
                func_args = {**func_args, "keyword": kw}
            elif func_args.get("keyword"):
                _kwf_m = _WC6.match_items(func_args["keyword"])
                if not _kwf_m or _kwf_m[0].get("score", 0) < 3:
                    func_args = {k: v for k, v in func_args.items() if k != "keyword"}
                    log.info(f"[校正 C6] related keyword 不扎實 → 清空請訪客指名")
            func_args = _drop_ungrounded_category(func_args, user_text)
            return func_name, func_args, True

    # ── C2e: 原句明講昨天/上週 → 覆寫 period（LLM 常給「合法但錯」的
    #   this_week，容錯 map 只救非法值管不到，2026-07-06 加 yesterday/last_week
    #   支援時抓到）──
    if func_name == "query_movement":
        # r25：前天真日期支援；上上週在上週之前判（substring 撞字防呆）
        _c2e = ("day_before_yesterday" if (any(w in user_text for w in ("前天", "前日"))
                                           and "大前天" not in user_text) else
                "yesterday" if any(w in user_text for w in ("昨天", "昨晚", "昨日")) else
                "last_week" if (any(w in user_text for w in ("上週", "上周", "上禮拜"))
                                and not any(w in user_text for w in ("上上週", "上上周", "上上禮拜"))) else None)
        if _c2e and func_args.get("period") != _c2e:
            func_args = {**func_args, "period": _c2e}
            log.info(f"[校正 C2e] 原句時間詞 → period={_c2e}")

    # ── C2: 模糊時間詞 → period rewrite ──
    if func_name == "query_movement":
        if any(kw in user_text for kw in _VAGUE_TIME_WORDS):
            old_period = func_args.get("period")
            if old_period != "this_week":
                log.info(f"[校正 C2] period {old_period} → this_week (模糊時間詞)")
                func_args = dict(func_args)
                func_args["period"] = "this_week"

    # ── C1g（r23）：LLM 自帶 kw 全域接地——「中倉有什麼要進貨的」「哪些貨在
    # 苟延殘喘」LLM 曾幻覺 keyword=耳機 回無關單品。kw 與原句無 bigram/尾字/
    # SKU 代號重疊 → 清除退概覽（寧錯殺不亂答）──
    _en_oov_cleared = False   # C1g-oov 清過 kw → 下游 C1/C1s 不要補回來
    if func_name == "query_inventory" and func_args.get("keyword"):
        _kwg = str(func_args["keyword"])
        if not _kw_grounded(_kwg, user_text):
            import warehouse as _Wkg
            _kwg_m = _Wkg.match_items(_kwg)
            _kwg_sku = _kwg_m[0]["item"]["sku_id"] if _kwg_m else ""
            if not _kwg_sku or _kwg_sku.lower() not in user_text.lower():
                log.info(f"[校正 C1g] LLM kw 未接地 → 清除 {_kwg!r}")
                func_args = {k: v for k, v in func_args.items() if k != "keyword"}
        # ── EN build C1g-oov：英文句帶**陌生修飾詞**時不要硬回近似商品。
        #   'how many chairs for the office' 的 chairs 接地成立（主檔真有
        #   chair），但 office 不是任何商品名的字＝訪客要的是「辦公椅」，
        #   庫裡沒有 → 該誠實回概覽/OOV，不能回露營椅（守衛 noex 期望）。
        elif (_is_mostly_english(user_text) and func_args.get("keyword")
              # 功能描述句由 descriptor_en 負責，不可當 OOV 清掉
              #   （'something to clean teeth' 的 clean/teeth 都不是主檔字，
              #    會被判陌生修飾詞 → 清 kw 回全店概覽）
              and not _en_descriptor_hit(user_text)):
            _oov_stop = {
                         # 寒暄/時間/語氣詞（同 _NOEX_STOP，兩處要同步——
                         #   劇情批 r1：'hi there busy today' 被當商品查詢）
                         # r1：'whats worth watching in stock' 的 watching /
                        #   worth 被當商品名（訪客問的是「有什麼要注意的」）
                        "watching", "watch", "worth", "noting", "note",
                        # r22：Agent 功能詞（不是商品名）——`show me the scripts`
                        #   回「查無 scripts 這個商品」；errors/export 同款
                                                # r24：到期/搭售的功能詞（不是商品名）——
                        #   `check expiry dates` → 查無 dates、
                        #   `show me pairings` → 查無 pairings、
                        #   `whats going off soon` → 查無 "off soon"
                        "date", "dates", "expiry", "expiration", "pairing",
                        "pairings", "bundle", "bundles", "combo", "combos",
                        "off", "soon", "bad", "spoiled",
                        # r14 網頁百句：比較/雜訊詞被抽成商品名 →「more/sku
                        #   not found」（與 _NOEX_STOP 同步）
                        "sku", "skus", "more", "less", "than", "fewer",
                        "exceed", "exceeds", "number", "carry", "carrying",
                        # r14+1：營運行話（backlog 類1）——stockout risk/
                        #   replenishment/volume/popular 曾被抽成商品名。
                        #   ⚠️ 'cover' 不可收（撞 phone cover 別名，掃描實證）
                        "stockout", "risk", "risks", "replenishment",
                        "urgent", "popular", "unpopular", "volume",
                        "volumes", "dead", "zero", "restocks", "restock",
                        # r14+2：weekend（#42 曾幻覺配商品）＋功能名詞
                        "weekend", "weekends", "restocking", "replenishing",
                        "transfer", "transfers", "movement", "movements",
                        # r15：口語縮寫/狀態詞（與 _NOEX_STOP 同步）——#20
                        #   gimme numbers/#26 wheres sitting/#67 totals/#73
                        #   categories 曾被抽成商品名
                        "numbers", "gimme", "lemme", "wheres", "sitting",
                        "totals", "categories", "category", "stale",
                        # r18（與 _NOEX_STOP 同步）
                        "sanity", "customer", "customers", "asking",
                        "time", "times", "usual",
                        "script", "scripts", "error", "errors", "export",
                        "exports", "backup", "backups", "audit", "audits",
                        # r15：確認/操作詞永遠不是商品名（同 _NOEX_STOP，兩處同步）
                        "ahead", "proceed", "submit", "confirm", "confirmed",
                        "approve", "approved", "accept", "agreed", "sure",
                        "okay", "yep", "yeah", "yup", "fine", "alright",
                        # r12（探針批）：**禮貌用語**——展場訪客很常客氣地問，
                        #   而 r1-r11 的造句全是命令式（'earphone stock'），
                        #   整類漏掉。實測 'could you tell me the earphone
                        #   stock' → No item matching "could"；
                        #   'i'd like to know the tent stock' → matching "like"。
                        #   這些是純功能詞、不可能是商品名，收進來很安全。
                        "could", "would", "should", "shall", "might", "may",
                        "like", "ask", "asking", "know", "knowing", "want",
                        "wanted", "wish", "hoping", "hope", "kindly", "mind",
                        "possible", "possibly", "maybe", "perhaps", "just",
                        "quick", "quickly", "question",
                        # 2026-08-03（資料邊界批）：**分布/範圍介系詞**——
                        #   `show me wireless mouse across warehouses` 的 across
                        #   被當陌生修飾詞 → C1g-oov **清掉已抽對的
                        #   'Wireless Mouse'** → 回「查無 across warehouses」。
                        #   clf conf=0.99、keyword 抽對，純粹被閘門吃掉正解
                        #   （坑 3 同型）。這些是純方位/範圍詞，不可能是商品名。
                        #   ⚠️ 不收 "in"（in-ear/built-in 等商品名含它）。
                        "across", "among", "amongst", "between", "throughout",
                        "per", "each", "every", "versus", "vs",
                        # r1：確認語（did it take effect / put it back）不是商品名
                        "effect", "effective", "applied", "apply", "back",
                        "take", "takes", "took", "put", "puts", "get", "gets",
                        # r2：序數/最高級指代（the most urgent one）不是商品名
                        "most", "least", "urgent", "one", "ones", "cheapest",
                        # r3：語音/快打的虛詞黏字與常見錯字（不是商品名）
                        "howmany", "howmuch", "whatabout", "isthere", "stok",
                        "stcok", "invetory", "inventry", "wat", "wht", "hw",
                        "biggest", "largest", "smallest", "newest", "oldest",
                        "done", "changed", "change", "updated", "saved",
                        "interesting", "important", "urgent", "attention",
                        "busy", "today", "tomorrow", "yesterday", "morning",
                         "afternoon", "evening", "night", "hello", "hey",
                         "thanks", "thank", "please", "sorry", "welcome",
                         "good", "great", "fine", "okay", "sure", "yeah",
                         "well", "just", "really", "very", "quite", "maybe",
                         "guys", "everyone", "team", "here", "hows", "doing",
                         "many", "much", "have", "there", "some", "any",
                         "show", "tell", "give", "list", "check", "look",
                         "left", "stock", "stocks", "inventory", "count",
                         "hand", "with", "from", "that", "this", "them",
                         "they", "your", "what", "whats", "hows", "does",
                         "still", "right", "available", "availability",
                         "status", "please", "quantity", "units", "unit",
                         "level", "levels", "number", "warehouse", "north",
                         "central", "south", "total", "currently", "remaining",
                         "remain", "looking", "about", "need", "want", "know",
                         # 功能詞（同 oov:noex 的 _NOEX_STOP，兩處要同步）
                         "schedule", "schedules", "scheduled", "alert", "alerts",
                         "rule", "rules", "report", "reports", "log", "logs",
                         "record", "records", "file", "files", "compare",
                         "comparison", "last", "past", "two", "months", "month",
                         "week", "weeks", "day", "days", "trend", "growth",
                         "decline", "audit", "trail", "history", "purchase",
                         "order", "orders", "movement", "movements", "transfer",
                         "transfers", "setting", "settings", "config", "safety",
                         "everything", "anything", "something", "help",
                         # 劇情批 r5：追問副詞／最高級補齊（兩處同步）——
                         #   'whats the total again' → 回「No item matching
                         #   "again"」、'whats the lowest one' → 「"lowest"」。
                         #   ⚠️ 這些詞 `_CTX_FOLLOWUP_RE_EN` 早就認得（carry-over
                         #   偵測沒問題），但句子在更早的 OOV 層就被攔掉，
                         #   carry-over 分支根本執行不到——補停用詞才進得去。
                         "again", "lowest", "highest", "priciest", "worst",
                         "best", "cheaper", "pricier", "lower", "higher",
                         "then", "also", "too", "each", "same", "other",
                         "next", "first", "second", "third", "last", "previous",
                         "earlier", "before", "after", "instead", "actually",
                         # r5：比較/門檻介系詞——'is it below safety stock'
                         #   回「No item matching "below"」
                         "below", "under", "above", "over", "than", "minimum",
                         "maximum", "threshold", "limit", "target",
                         # r5-voice：疑問詞（不收 why/when/where/who——那是
                         #   RCA 與期間查詢的意圖詞）
                         "which", "whose", "whom",
                         # r5-voice：動名詞/泛詞被當商品名——
                         #   'whats happening with the toothbrush count'
                         #   → 回「No item matching "happening"」；
                         #   'how does the transfer work' → 「"work"」
                         "happening", "happens", "happened", "work", "works",
                         "working", "mean", "means", "thing", "stuff"}
            try:
                import warehouse as _Woov
                _oov_words = set()
                for _it in _Woov.state().items:
                    for _w in _re.split(r"[\s\-/]+", _it["name"].lower()):
                        if len(_w) >= 4:
                            _oov_words.add(_w)
                from alias_en import ALIAS_EN as _ALoov
                for _k in _ALoov:
                    for _w in _k.lower().split():
                        if len(_w) >= 4:
                            _oov_words.add(_w)
                import difflib as _dloov
                _oov_keys = list(_oov_words)
                # ── 已抽出的 keyword 當接地證據（坑 3：閘門吃掉正解）──
                #   'drip coffoe bags' 的 kw 已經是 'Drip Coffee Bags 20pcs'
                #   （drip/bags 精確命中），但 coffoe 對全主檔只有 0.833
                #   卡在 0.85 外 → 這裡把**已抽對的 kw 清掉** → 全店概覽。
                #   ⚠️ 與 oov:noex 的同名修復**兩處必須同步**（坑 3 的教訓：
                #   修一層下一層又擋掉——實測就是修完 oov:noex 才發現還有這道）。
                #   門檻同樣是 0.80（實測：真錯字 0.833 / 誤配 gaming→camping 0.769）。
                _oov_tgt_words = set()
                try:
                    _oov_m = _Woov.match_items(func_args.get("keyword", ""))
                    if _oov_m and _oov_m[0].get("score", 0) >= 4:
                        for _wt in _re.split(r"[\s\-/]+",
                                             _oov_m[0]["item"]["name"].lower()):
                            _wt = _wt.strip(" ?.!,'\"")
                            if len(_wt) >= 3 and not any(c.isdigit() for c in _wt):
                                _oov_tgt_words.add(_wt)
                except Exception:
                    pass
                for _t in _re.split(r"[\s\-/]+", user_text.lower()):
                    _t = _t.strip(" ?.!,'\"")
                    if len(_t) < 4 or _t in _oov_stop or any(c.isdigit() for c in _t):
                        continue
                    # ── DEMO 演練抓到：**打錯的功能詞**也要當停用詞 ─────────
                    #   `powr bank invntory`（訪客同時打錯兩個詞）→ keyword
                    #   已**抽對** Power Bank，卻因 `invntory`（inventory 的
                    #   錯字）不在停用詞表 → 被當陌生修飾詞清掉正解 →
                    #   「查無 invntory 這個商品」。
                    #   **這剛好落在招牌賣點（容錯）的展示上**，最尷尬。
                    #   ⚠️ 停用詞表列的是正確拼法，錯字版列不完 → 用模糊比對：
                    #     很像某個停用詞（≥0.85）就一樣跳過。
                    #     門檻取 0.85 而非更低：invntory→inventory 是 0.94，
                    #     而真商品詞不會這麼像功能詞。
                    try:
                        if _dloov.get_close_matches(_t, list(_oov_stop),
                                                    n=1, cutoff=0.85):
                            continue
                    except Exception:
                        pass
                    # r12（TTS 基準批）：**句中帶撇號的是英文縮寫，不是商品修飾詞**。
                    #   `what's in central warehouse for wireless mouse`——
                    #   LLM 判**全對**（keyword=mouse, warehouse=central），
                    #   卻因 "what's" 被當陌生修飾詞 → 這道閘門清掉 keyword
                    #   → C17a-pre 再丟掉 warehouse → 回全店概覽（坑 3 典型）。
                    #   ⚠️ 上面 strip 只剝**頭尾**標點，撇號在字中間剝不掉。
                    #   ⚠️ 商品名 `Men's` 系列不受影響：那是 keyword 本身，
                    #     不會走到這個「修飾詞」迴圈（實測 men's jeans 正常）。
                    if "'" in _t or "’" in _t:
                        continue
                    # r13（多腔調批）：**商品詞 + 功能詞黏在一起**也不是陌生詞。
                    #   ASR 穩定地把 `steam iron stock` 聽成 `steam ironstock`
                    #   ——GB/AU/IN **三個腔調都一樣**，是固定行為不是隨機。
                    #   結果：keyword 早就抽對（Steam Iron），卻被這道閘門
                    #   當「ironstock 是陌生商品」清掉 → 誠實回覆「查無」。
                    #   ⚠️ 既有的黏字拆解只處理「商品詞+商品詞」（yogamat /
                    #     powerbank），「商品詞+功能詞」沒人管。
                    #   ⚠️ **剝完要再驗**：剝掉尾綴後必須是認得的商品詞才放行，
                    #     不是無條件跳過（否則 'xyzstock' 也會被放過）。
                    _GLUE_SFX = ("stock", "stocks", "inventory", "count",
                                 "counts", "qty", "quantity", "level", "levels")
                    _glued = None
                    for _sfx in _GLUE_SFX:
                        if len(_t) > len(_sfx) + 2 and _t.endswith(_sfx):
                            _glued = _t[:-len(_sfx)]
                            break
                    if _glued:
                        try:
                            if (_glued in _oov_words
                                    or _glued.rstrip("s") in _oov_words
                                    or _en_fuzzy_keyword(_glued)):
                                log.info(f"[C1g-oov] 黏字還原 {_t!r} → {_glued!r}"
                                         f"（商品詞+功能詞）")
                                continue
                        except Exception:
                            pass
                    if _t in _oov_words or _t.rstrip("s") in _oov_words:
                        continue
                    # 模糊層（合成詞/錯字）認得的不算陌生修飾詞
                    try:
                        if _en_fuzzy_keyword(_t):
                            continue
                    except Exception:
                        pass
                    if _oov_tgt_words and _dloov.get_close_matches(
                            _t, list(_oov_tgt_words), n=1, cutoff=0.80):
                        continue
                    # kw 已抽出時，陌生詞對**主檔任一詞**達 0.80 也算錯字
                    #   （同 oov:noex 的同名放寬，三道閘門必須同步——
                    #   'Mosquuito Rpellent Soray' 的 soray 對 spray 是 0.80，
                    #   但對已抽出的 Refill 接地不了）
                    if _oov_tgt_words and _dloov.get_close_matches(
                            _t, _oov_keys, n=1, cutoff=0.80):
                        continue
                    if not _dloov.get_close_matches(_t, _oov_keys, n=1, cutoff=0.85):
                        # ── r17：**形容詞式修飾語不該清掉正解**（同 oov:noex 判準）──
                        #   `is the earphone stock healthy` 的 keyword 已經
                        #   **抽對**（Wireless Bluetooth Earphones、clf conf=0.99），
                        #   卻因 healthy 不在主檔 → 這裡清掉 → 回全店 60 項概覽。
                        #   r16 只修了下游「不宣告查無」，沒擋住上游清 keyword，
                        #   結果是「不說查無了，但答案還是錯的」（坑 3：修一層
                        #   不夠，同一個判準要用在所有相關層）。
                        #   ⚠️ 判準與 oov:noex 那處一致，改一邊要改兩邊。
                        if (_t in _ADJ_LIKE_OOV
                                or _re.search(r"(?:ic|ed|able|ible|ful|ous)$", _t)):
                            log.info(f"[校正 C1g-oov] {_t!r} 是形容詞式修飾語 "
                                     f"→ 保留 kw {func_args['keyword']!r}")
                            continue
                        log.info(f"[校正 C1g-oov] 陌生修飾詞 {_t!r} → 清除 kw "
                                 f"{func_args['keyword']!r}")
                        func_args = {k: v for k, v in func_args.items()
                                     if k != "keyword"}
                        # 標記：下游補 kw 的規則（C1/C1s）要尊重這個判斷，
                        #   否則清掉後 C1s 立刻用 match_items 補回同一個商品
                        _en_oov_cleared = True
                        break
            except Exception:
                pass

    # ── C1: query_inventory 沒抽到 keyword 但 user_text 含商品意圖詞 → 補 keyword ──
    if func_name == "query_inventory":
        kw = func_args.get("keyword")
        cat = func_args.get("category")
        # 非法 category（baby_goods 這類幻覺值）後面才會被清掉，這裡先視為空，
        # 否則 C1/C1s 補 kw 的機會被幻覺值堵死（r18 RPI5 平台分歧）
        if cat not in VALID_CATEGORIES:
            cat = None
        if not kw and not cat:
            # 若 user_text 含意圖詞 → 把去掉意圖詞跟時間詞的剩餘字當 keyword
            if any(w in user_text for w in _INVENTORY_INTENT_WORDS):
                cleaned = _extract_sku_keyword(user_text)
                if cleaned and len(cleaned) >= 2 and _kw_grounded(cleaned, user_text) \
                        and not _en_oov_cleared:
                    log.info(f"[校正 C1] query_inventory 補 keyword: {cleaned!r}")
                    func_args = dict(func_args)
                    func_args["keyword"] = cleaned
            else:
                # r18 平台分歧修：「嬰兒用品有哪些」無庫存語氣詞，RPI5 LLM 幻覺
                # category=baby_goods 被 C-cat 丟棄後 args 全空 → 概覽（本地走
                # clf rescue 有 kw）。句中有「扎實」商品名（score≥3）就補——
                # 比對扎實本身就是強證據，不依賴語氣詞，兩平台同路。
                import warehouse as _W_c1s
                _c1s_kw = _extract_sku_keyword(user_text)
                _c1s_m = _W_c1s.match_items(_c1s_kw) if _c1s_kw else []
                if _c1s_m and _c1s_m[0].get("score", 0) >= 3 \
                        and _kw_grounded(_c1s_kw, user_text) and not _en_oov_cleared:
                    log.info(f"[校正 C1s] 無語氣詞但商品名扎實 → 補 keyword: {_c1s_kw!r}")
                    func_args = dict(func_args)
                    func_args["keyword"] = _c1s_kw

    # ── C1b: query_inventory keyword 結尾有功能詞 → 去掉（如「藍牙耳機庫存」→「藍牙耳機」）──
    if func_name == "query_inventory" and func_args.get("keyword"):
        _inv_suffixes = ("庫存", "數量", "現貨", "存貨", "庫量", "剩餘", "剩多少", "還有多少", "有多少")
        kw = func_args["keyword"]
        for _sfx in _inv_suffixes:
            if kw.endswith(_sfx) and len(kw) > len(_sfx):
                kw = kw[:-len(_sfx)].strip()
                log.info(f"[校正 C1b] query_inventory keyword 去尾詞: {func_args['keyword']!r} → {kw!r}")
                func_args = dict(func_args)
                func_args["keyword"] = kw
                break

    # 註：新增商品名撞既有商品前綴（露營燈罩 vs LED露營燈）→ 刻意不猜，
    #   讓 query_inventory 回「疑似清單」請訪客選（2026-07-15 定調：太模糊不硬猜，
    #   反問訪客也是互動；避免猜錯才是展場底線）。

    # ── C2c: query_movement 沒抽到 keyword，或 keyword 黏了功能詞尾巴 → 補 ──
    if func_name == "query_movement":
        _c2c_kw = func_args.get("keyword") or ""
        # r36：「牛仔褲進出紀錄」黏著（無「的」）時，低分商品的 keyword 會整串
        #   殘留功能詞尾巴 → match 不到 → 回全部商品。先剝尾巴再抽。
        #   （USB風扇這類高分商品剛好能穿透，牛仔褲 score=3 就掉——全枚舉抓到）
        _c2c_src = _re.sub(r"(的)?(這個?月|本月|上個?月|這週|上週|今天|昨天)?"
                           r"(的)?(進出貨?紀錄?|進出貨?狀況|進出貨?|異動紀錄?|流水紀錄?|異動|流水)$",
                           "", user_text).strip() or user_text
        _c2c_bad = bool(_re.search(r"進出|異動|流水|紀錄", _c2c_kw))
        if not _c2c_kw or _c2c_bad:
            cleaned = _extract_sku_keyword(_c2c_src)
            # r11（真人語音批乾跑抓到）：英文純追問句要驗接地才可補。
            #   `and south` 剝尾巴的 regex 全中文、對英文剝不掉東西 → 整句拿去
            #   match → 亂配 **Smart Fitness Band**（"s" 開頭硬湊），訪客問的是
            #   上一個商品的南倉庫存，卻收到不相干商品的進出紀錄。
            #   同坑 4：上游（C4-mvg）才剛把幻覺 keyword 'shoes' 丟棄，
            #   這裡又補一個回來——補 keyword 的規則都要尊重接地。
            _c2c_ok = True
            if cleaned and _is_mostly_english(user_text):
                _core = {w.strip(" ?.!,'\"").lower()
                         for w in _re.split(r"[\s\-/]+", user_text.lower())}
                # 商品名任一實詞要真的出現在原句（別名/錯字路已在上游處理過）
                # r14+2（#42）：接地實詞要排除介系詞——'what sold over the
                #   weekend' 的 over 恰好是 Pour-**over** Coffee Set 的一段，
                #   讓幻覺商品騙過接地檢查
                _C2C_FUNC = {"over", "under", "with", "for", "and", "per",
                             "off", "out", "the"}
                _c2c_ok = any(
                    w.lower() in _core or w.lower().rstrip("s") in
                    {t.rstrip("s") for t in _core}
                    for w in _re.split(r"[\s\-/]+", cleaned)
                    if len(w) >= 3 and w.lower() not in _C2C_FUNC)
                if not _c2c_ok:
                    log.info(f"[校正 C2c] 英文補 keyword {cleaned!r} 不接地 → 不補")
            if (cleaned and len(cleaned) >= 2 and _c2c_ok
                    and not _re.search(r"進出|異動|流水|紀錄", cleaned)):
                log.info(f"[校正 C2c] query_movement 補 keyword: {cleaned!r}（剝尾巴後）")
                func_args = dict(func_args)
                func_args["keyword"] = cleaned
        # r39：keyword 歧義（LLM 抽殘「嬰兒」match 到多個嬰兒類商品）但 user_text 含
        #   更完整的商品名（carry-over 補的「嬰兒連身衣」）→ 用完整名。RPI5 LLM 抽詞弱、
        #   常只抽共用前綴，本機 route 抽得準才沒露出來（全枚舉雙平台抓到）。
        elif _c2c_kw:
            import warehouse as _W_c2d
            _c2d_m = _W_c2d.match_items(_c2c_kw)
            _amb = (len(_c2d_m) > 1 and _c2d_m[0]["score"] - _c2d_m[1]["score"] < 3)
            if _amb:
                _c2d_full = None
                for _it in _W_c2d.state().items:
                    _nm = _it["name"]
                    if _nm in _c2c_src and len(_nm) > len(_c2c_kw) and _c2c_kw in _nm:
                        if _c2d_full is None or len(_nm) > len(_c2d_full):
                            _c2d_full = _nm
                if _c2d_full:
                    log.info(f"[校正 C2d] movement keyword 歧義 → 用原句完整名: "
                             f"{_c2c_kw!r} → {_c2d_full!r}")
                    func_args = dict(func_args)
                    func_args["keyword"] = _c2d_full

    # ── 通用：warehouse / category / period enum 容錯 ──
    if "warehouse" in func_args and func_args["warehouse"] not in VALID_WAREHOUSES:
        # 簡單 mapping
        wh_map = {
            "北倉": "north", "北區": "north", "north warehouse": "north",
            "中倉": "central", "中區": "central", "central warehouse": "central",
            "南倉": "south", "南區": "south", "south warehouse": "south",
            "全部": "all", "全部倉": "all", "三個": "all", "三倉": "all",
        }
        v = func_args["warehouse"]
        func_args = dict(func_args)
        func_args["warehouse"] = wh_map.get(v, wh_map.get(v.lower(), "all"))

    if "category" in func_args and func_args["category"] not in VALID_CATEGORIES:
        cat_map = {
            "電子": "electronics", "電子產品": "electronics", "3c": "electronics", "3c 產品": "electronics",
            "家電": "appliance_kitchen", "廚具": "appliance_kitchen", "家電廚具": "appliance_kitchen",
            "食品": "food_beverage", "飲料": "food_beverage", "食品飲料": "food_beverage",
            "日用": "daily_goods", "清潔": "daily_goods", "日用品": "daily_goods", "生活用品": "daily_goods",
            "服飾": "apparel", "衣服": "apparel", "服裝": "apparel",
            "運動": "sports", "運動用品": "sports", "運動類": "sports",
        }
        v = func_args["category"]
        func_args = dict(func_args)
        new_cat = cat_map.get(v, cat_map.get(v.lower()))
        if new_cat:
            func_args["category"] = new_cat
        else:
            del func_args["category"]

    if "period" in func_args and func_args["period"] not in VALID_PERIODS:
        period_map = {
            "today": "today", "今天": "today", "今日": "today", "本日": "today",
            "yesterday": "yesterday", "昨天": "yesterday", "昨日": "yesterday",
            # r30：漏了這行會把 C2e 修好的前天蓋回 today（多層不同步）
            "day_before_yesterday": "day_before_yesterday", "前天": "day_before_yesterday",
            "this_week": "this_week", "本週": "this_week", "這週": "this_week", "this week": "this_week",
            "last_week": "last_week", "上週": "last_week", "上周": "last_week", "上禮拜": "last_week",
            "this_month": "this_month", "本月": "this_month", "這個月": "this_month", "this month": "this_month",
        }
        v = func_args["period"]
        func_args = dict(func_args)
        func_args["period"] = period_map.get(v, period_map.get(v.lower(), "today"))

    # ── C5: compare_warehouses 漏 slot → 預設 north vs central；全空才 fallback ──
    if func_name == "compare_warehouses":
        valid_wh_pair = {"north", "central", "south"}
        wa = func_args.get("warehouse_a")
        wb = func_args.get("warehouse_b")
        # 三倉排名意圖（「哪個倉最多/最空」「各倉分布」「三個倉比一比」）：句中
        # 沒明確點名 2 個倉、卻問排名/分布 → warehouse_a=all 觸發三倉排名
        # （RPI5 conv100-r4：這類原本只比 2 倉、答非所問）
        _named_whs = sum(1 for z in ("北倉", "北區", "中倉", "中區", "南倉", "南區") if z in user_text)
        _rank3_cue = any(w in user_text for w in (
            "哪個倉", "哪倉", "各倉", "三倉", "三個倉", "每個倉", "最多", "最空",
            "最滿", "分布", "佔比", "哪個最", "誰最",
            # r29：「北倉南倉中倉誰最大」曾回熱銷榜
            "誰最大", "哪個最大", "誰大"))
        if _rank3_cue and _named_whs < 2:
            _mt = func_args.get("metric") if func_args.get("metric") in ("stock_value", "item_count", "turnover") else "item_count"
            log.info(f"[校正 C5-rank3] 三倉排名意圖 → compare(all) metric={_mt}")
            return "compare_warehouses", {"warehouse_a": "all", "warehouse_b": "all", "metric": _mt}, True
        if wa not in valid_wh_pair and wb not in valid_wh_pair:
            # 兩個都沒給 → 給預設值（北倉 vs 中倉）
            func_args = dict(func_args)
            func_args["warehouse_a"] = "north"
            func_args["warehouse_b"] = "central"
            log.info("[校正 C5] compare 漏 slot → 預設 north vs central")
        elif wa not in valid_wh_pair:
            func_args = dict(func_args)
            func_args["warehouse_a"] = "north"
        elif wb not in valid_wh_pair:
            func_args = dict(func_args)
            func_args["warehouse_b"] = "central"

    # C3c：「低於安全庫存/跌破安全水位」是查缺貨清單，不是查改設定
    # （第15輪抓到：「低於安全庫存的品項」被 config key 詞搶成 config_read）
    if (any(w in user_text for w in ("低於", "跌破", "以下"))
            and any(w in user_text for w in ("安全庫存", "安全水位", "安全線", "警戒"))):
        log.info("[校正 C3c] 低於安全庫存 → list_low_stock")
        return "list_low_stock", {}, True

    # C5-rank3b（r24）：「三個倉的庫存量排一下名」LLM 誤投 query_inventory{三倉}
    # → C5-rank3 只接 compare 分支接不到，C13 還會把幻覺 kw 硬轉單品庫存。
    # 排名 cue + 倉 cue + 未點名 2 倉 + 非銷售/設定語境 → compare(all) 三倉排名。
    if (any(w in user_text for w in ("排名", "排行", "排一下", "比一比", "排個名", "名次"))
            and any(w in user_text for w in ("三個倉", "三倉", "各倉", "每個倉", "哪個倉", "倉的庫存"))
            and sum(1 for z in ("北倉", "北區", "中倉", "中區", "南倉", "南區") if z in user_text) < 2
            and not any(w in user_text for w in ("賣", "銷", "熱", "滯", "安全庫存", "警戒"))):
        log.info("[校正 C5-rank3b] 倉庫排名意圖 → compare(all)")
        return "compare_warehouses", {"warehouse_a": "all", "warehouse_b": "all", "metric": "item_count"}, True

    # C13：明確查庫存意圖 + SKU → hard-return query_inventory（防止 C18 誤覆蓋）
    # RCA 意圖詞（對帳/異常/少了）優先於 C13，不搶。
    # 含設定項詞（安全庫存/前置天數）時也不搶——「現在安全庫存是多少」的
    # 「庫存」曾讓 C13 在 C9 之前 hard-return 搶走 config 查詢（第15輪抓到）
    _c13_has_rca = _has_rca_word(user_text)
    _c13_has_cfg = any(w in user_text for w in _CONFIG_KEY_WORDS)
    _inv_intent = ("庫存", "剩多少", "還有多少", "有多少", "幾個", "數量", "查庫存",
                   "還剩", "幾件", "存貨",
                   # r27：「電熨斗還夠不夠」RPI5 曾漂去 low_stock（確定性接手）
                   "夠不夠", "還夠",
                   "inventory", "stock", "查一下庫存", "看庫存", "查看庫存")
    # 「賣了幾件」是銷售統計不是存量——C13 不可搶（conv100-r7）
    _c13_has_sale = any(w in user_text for w in ("賣了", "售出", "賣出", "賣掉"))
    if (not _c13_has_rca and not _c13_has_cfg and not _c13_has_sale
            and any(w in user_text for w in _inv_intent) and func_name == "query_inventory"):
        kw = _extract_sku_keyword(user_text) or func_args.get("keyword", "")
        if kw and not _kw_grounded(kw, user_text):
            kw = ""  # fuzzy 亂中的全名不可信（conv100-r8）
        # EN build：C1g-oov 已判定「句中有陌生修飾詞＝庫裡沒有的商品」
        #   （printer paper / office chairs），這裡不可再用 match_items
        #   把近似商品撿回來（C1s 踩過同一個坑）
        if _en_oov_cleared:
            kw = ""
        # 概覽詞不是商品名（r18：「給我全部庫存的總表」fallback 撿回 LLM 的
        # 「全部庫存」kw → clarify 找不到）→ 清掉查概覽
        if kw and any(w in kw for w in ("總表", "種商品", "全部庫存", "總庫存",
                                         "全店", "全部", "所有商品",
                                         # r27：「全部倉的總庫存值多少」kw 殘「倉 值」
                                         # 曾 clarify 找不到（無商品含「值」，安全）
                                         "值")):
            kw = ""
        if kw:
            # 檢查 keyword 是否其實是類別名（如「電子產品庫存」→ category=electronics）
            _CAT_ZH_MAP = {
                "電子產品": "electronics", "家電廚具": "appliance_kitchen",
                "食品飲料": "food_beverage", "日用品": "daily_goods",
                "服飾": "apparel", "運動用品": "sports",
                "電子": "electronics", "家電": "appliance_kitchen", "廚具": "appliance_kitchen",
                "食品": "food_beverage", "飲料": "food_beverage",
                "日用": "daily_goods", "清潔": "daily_goods", "衣服": "apparel", "服裝": "apparel",
                "運動": "sports",
            }
            cat_en = None
            for zh, en in sorted(_CAT_ZH_MAP.items(), key=lambda x: -len(x[0])):
                if zh in kw:
                    cat_en = en
                    break
            # 只有純類別詞才轉 category（商品名含類別詞如「運動毛巾」不該被轉）
            import warehouse as _W13
            _c13_names = [it["name"] for it in _W13.state().items]
            _kw_matches_product = any(n for n in _c13_names if kw in n)
            # r26：明講「X用品/X類/X產品」= 類別查詢——fuzzy 擴出的單品不可搶
            # （「南倉所有清潔用品庫存」曾被擴成「橡膠清潔手套」只回單品）
            # 注意用頂層 re——函式內後段有 import re as _re 會讓 _re 變區域變數
            if re.search(r'(?:電子|家電|廚具|食品|飲料|日用|服飾|運動|清潔)(?:用品|產品|類)', user_text):
                _kw_matches_product = False
            if cat_en and func_args.get("category", "") not in VALID_CATEGORIES and not _kw_matches_product:
                log.info(f"[校正 C13] 類別庫存查詢 kw={kw!r} → category={cat_en}")
                _c13cat_args = {**{k: v for k, v in func_args.items() if k != 'keyword'}, "category": cat_en}
                # r26：倉別從原句補（「南倉所有清潔用品」曾回全部倉）
                if not _c13cat_args.get("warehouse"):
                    for _zh13c, _en13c in _WH_ZH_MAP.items():
                        if _zh13c in user_text and _en13c != "all":
                            _c13cat_args["warehouse"] = _en13c
                            break
                return "query_inventory", _c13cat_args, True
            # hard-return 會跳過 C17a 的 warehouse 補全 → 單倉句在這裡補
            # （「耳機在南倉有幾個」曾少了南倉 filter，conv100-r12）
            _c13_args = _drop_ungrounded_category({**func_args, "keyword": kw}, user_text)
            # r24：LLM 幻覺 warehouse（含中文值「中倉」）要在 hard-return 前接地——
            # C17a-pre 排在 C13 之後跑不到（「防曬遮陽帽三個倉各剩多少」RPI5 只回中倉）
            _whv13 = _c13_args.get("warehouse")
            if _whv13 and _whv13 not in ("north", "central", "south"):
                _whn13 = next((en for zh, en in _WH_ZH_MAP.items()
                               if zh in str(_whv13) and en != "all"), None)
                if _whn13:
                    _c13_args["warehouse"] = _whn13
                else:
                    _c13_args.pop("warehouse", None)
                _whv13 = _c13_args.get("warehouse")
            if _whv13:
                # ⚠️ r19（坑 7）：同 C17a-pre——原本只認中文倉名，
                #   英文句的 warehouse 全被當幻覺丟掉。兩處要一起改。
                _zh13 = {"north": ("北倉", "北區", "北邊", "北部", "north"),
                         "central": ("中倉", "中區", "central"),
                         "south": ("南倉", "南區", "南邊", "南部", "south")}[_whv13]
                _ut13 = user_text.lower()
                if not any((z in user_text)
                           or (z.isascii() and _re.search(rf"\b{z}\b", _ut13))
                           for z in _zh13):
                    _c13_args.pop("warehouse", None)
                    log.info("[校正 C13] 丟棄幻覺 warehouse")
            _c13_whs = {z[0] for z in ("北倉", "北區", "中倉", "中區", "南倉", "南區") if z in user_text}
            _c13_whs |= {_w for _w in ("north", "central", "south")
                         if _re.search(rf"\b{_w}\b", user_text.lower())}
            if len(_c13_whs) >= 2:
                # 「智慧手環中倉北倉哪邊存量多」「北中南倉的滑鼠各有幾個」是
                # 多倉比較語意 → 丟單倉 filter 回三倉分佈（conv100-r12）
                _c13_args.pop("warehouse", None)
            elif not _c13_args.get("warehouse") and len(_c13_whs) == 1:
                for zh, en in _WH_ZH_MAP.items():
                    if zh in user_text:
                        _c13_args["warehouse"] = en
                        break
            log.info(f"[校正 C13] 明確庫存查詢 → query_inventory({kw!r})")
            return "query_inventory", _c13_args, True
        # r30：kw 空但句含明確類別詞 → 類別庫存查詢（「北倉的家電類庫存總覽」
        # 曾因 kw 空整段跳過 cat 推導、回 60 項概覽）
        if not kw:
            _CAT_ZH_MAP2 = {"電子": "electronics", "家電": "appliance_kitchen",
                            "廚具": "appliance_kitchen", "食品": "food_beverage",
                            "飲料": "food_beverage", "日用": "daily_goods",
                            "清潔": "daily_goods", "服飾": "apparel", "衣服": "apparel",
                            "運動": "sports"}
            for _zh30, _en30 in sorted(_CAT_ZH_MAP2.items(), key=lambda x: -len(x[0])):
                if (_zh30 + "類") in user_text or (_zh30 + "用品") in user_text or (_zh30 + "產品") in user_text:
                    _c13c_args2 = {"category": _en30}
                    for _zhw30, _enw30 in _WH_ZH_MAP.items():
                        if _zhw30 in user_text and _enw30 != "all":
                            _c13c_args2["warehouse"] = _enw30
                            break
                    log.info(f"[校正 C13] kw空+類別詞 → category={_en30}")
                    return "query_inventory", _c13c_args2, True

    # ══════════════ v2 Agent 進階工具校正（C8-C11）══════════════

    # ── C8-pre: 「還有嗎/夠不夠/有沒有貨」被 LLM 誤判 RCA → 攔回 inventory ──
    _is_stock_question = any(w in user_text for w in (
        "還有嗎", "還有貨嗎", "有沒有貨", "夠不夠", "還夠嗎", "有貨嗎",
        "有沒有", "還有沒有", "會缺貨嗎", "快沒了嗎",
    ))
    if _is_stock_question and func_name == "search_log":
        kw = func_args.get("keyword", "") or _extract_sku_keyword(user_text)
        if kw:
            log.info(f"[校正 C8-pre] 庫存詢問攔回 inventory: {user_text!r} kw={kw!r}")
            return "query_inventory", {"keyword": kw}, True

    # ── C7b: 含 movement 保護詞 → 強制 query_movement，不被 RCA 攔截 ──
    # ⚠️ 一定要 hard-return——C7b 之前只改 func_name 不返回，「最近有進什麼貨嗎」
    # 被 C7b 正確轉成 movement 後，C18 又拿 intent_clf 的 query_inventory 蓋回去
    # （第10輪測試抓到）。hard=True 讓 C18 不碰。
    _c7b_hit = any(w in user_text for w in _MOVEMENT_PROTECT_WORDS)
    # ⚠️ **匯出/下載讓路**（2026-08-03）：「匯出昨天的進出紀錄」含保護詞
    #   「進出紀錄」，會被 C7b 搶去 query_movement（查詢），但訪客要的是
    #   **跑匯出腳本產檔**。LLM 其實判對了（run_script{script_name:匯出}），
    #   是這道保護把它蓋掉。⇒ 句中有明確匯出動詞時不攔。
    if _c7b_hit and _re.search(r"匯出|匯到|輸出|下載|導出|存成|存檔|"
                               r"\bexport\b|\bdownload\b", user_text, _re.I):
        _c7b_hit = False
    if _c7b_hit:
        kw = func_args.get("keyword", "") or _extract_sku_keyword(user_text)
        # keyword 髒掉（帶時間/疑問詞，如「最近 進什麼貨」）比對不到商品就丟掉
        # → 全品項進出統計；否則後面 OOV 檢查會誤判成「找不到商品」clarify
        if kw:
            import warehouse as _WC7
            if not _WC7.match_items(kw):
                # r36：kw 黏了功能詞尾巴（「牛仔褲進出紀錄」LLM 沒斷開）→ 剝尾巴再抽一次。
                #   低分商品（牛仔褲 match「牛仔長褲」score=3）黏著就 match 不到，被清空
                #   → 回全部商品（全枚舉 + 守衛抓到）。高分商品剛好能穿透才沒露出來。
                _c7b_stripped = _re.sub(
                    r"(的)?(這個?月|本月|上個?月|這週|上週|今天|昨天|前天)?(的)?"
                    r"(進出貨?紀錄?|進出貨?狀況|進出貨?|異動紀錄?|流水紀錄?|異動|流水)$",
                    "", kw).strip()
                _c7b_re = _extract_sku_keyword(_c7b_stripped) if _c7b_stripped else ""
                if _c7b_re and _WC7.match_items(_c7b_re):
                    kw = _c7b_re
                else:
                    kw = ""
        # period 從原句推斷（hard-return 會跳過後面的 C2 時間詞規則，
        # 「最近一個月進貨多少」曾顯示成今天的數字，第14輪抓到）
        # r25：前天要排最前（曾被下面的「週」家族吃掉回本週）；大前天走 time-gate 誠實 clarify
        # r27：時段詞（早上/下午/中午/晚上/傍晚）=今天的近似（「下午有出貨嗎」曾回本月）
        _c7b_period = ("today" if any(w in user_text for w in ("早上", "下午", "中午", "晚上",
                                                                "傍晚", "今早", "今晚", "上午"))
                                   and not any(w in user_text for w in ("每天", "每日", "昨天", "明天")) else
                       "day_before_yesterday" if (any(w in user_text for w in ("前天", "前日"))
                                                  and "大前天" not in user_text) else
                       "this_month" if any(w in user_text for w in ("這個月", "本月", "一個月", "上個月", "月")) else
                       "yesterday" if any(w in user_text for w in ("昨天", "昨晚", "昨日")) else
                       "last_week" if any(w in user_text for w in ("上週", "上周", "上禮拜")) else
                       "this_week" if any(w in user_text for w in ("這週", "本週", "這禮拜", "週", "禮拜")) else
                       "today" if any(w in user_text for w in ("今天", "今日")) else
                       "this_month")
        _c7b_args = {"period": _c7b_period, "direction": "both"}
        if kw:
            _c7b_args["keyword"] = kw
        for _zh7b, _en7b in _WH_ZH_MAP.items():
            if _zh7b in user_text:
                _c7b_args["warehouse"] = _en7b
                break
        log.info(f"[校正 C7b] movement 保護詞 → query_movement kw={kw!r} period={_c7b_period}（hard）")
        return "query_movement", _c7b_args, True

    has_rca    = _has_rca_word(user_text)
    has_cfgkey = any(w in user_text for w in _CONFIG_KEY_WORDS)
    has_cfgset = any(w in user_text for w in _CONFIG_SET_WORDS)
    has_script = any(w in user_text for w in _SCRIPT_INTENT_WORDS)

    # C8：含 RCA 意圖詞 → 強轉 search_log（排除已正確的 search_log 和寫入類）
    _rca_exclude = {"search_log", "manage_config", "set_alert", "generate_po",
                    "commit_po", "run_script", "generate_report"}
    # 兩倉比較意圖（如「北倉和南倉庫存差異比一下」）→ RCA 不搶
    _two_whs_in_text = sum(1 for zh in _WH_ZH_MAP if zh in user_text) >= 2
    # C7b 剛判斷過這句話含明確的 movement 保護詞（不是 RCA 意圖）→ RCA 不搶。
    # 沒有這道防線的話，「南倉這禮拜出了多少貨」的「多少貨」子字串會誤命中
    # _RCA_INTENT_WORDS 的「少貨」，把 C7b 剛修正好的 query_movement 蓋回
    # search_log（2026-07-02 實測抓到：字元級子字串誤判，跟商品/RCA語意無關）。
    if has_rca and func_name not in _rca_exclude and not _two_whs_in_text and not _c7b_hit:
        kw = func_args.get("keyword", "")
        # C8 轉換時就補好 keyword，否則 C17 沒機會跑
        if not kw:
            kw = _extract_sku_keyword(user_text) or ""
        new_args = {"keyword": kw}
        if func_args.get("period"):
            new_args["time_range"] = func_args["period"]
        log.info(f"[校正 C8] RCA 意圖 → search_log（原 {func_name}）keyword={kw!r}")
        return "search_log", new_args, True

    # C9-gen（r55·分支遍歷抓到）：單字通稱＋設定句（「鍋子警戒值設成60」）——C9
    # hard-return 的 sentinel 曾讓它「找不到商品」。通稱展開成帶完整語的選單，
    # 選完直接續 config 流。要在 C9 之前（鐵律：hard-return 前自帶防線）。
    _c9g_key = max((w for w in ("安全庫存", "安全水位", "警戒值", "前置天數",
                                 "補貨天數", "庫存上限", "庫存下限") if w in user_text),
                   key=len, default="")
    if _c9g_key and any(v in user_text for v in ("改", "設", "調", "變", "提高", "降")):
        import warehouse as _W_c9g
        for _gt, _gf in getattr(_W_c9g, "_GENERIC_QUERY_FALLBACK", {}).items():
            if _gt in user_text:
                _gv = _extract_config_value(user_text)
                _gnames = [it["name"] for it in _W_c9g.state().items
                           if any(f in it["name"] for f in _gf)]
                if len(_gnames) >= 2:
                    # EN build：同 C11d，選項要是後端認得的英文設定句
                    _c9g_key_en = _CFG_KEY_LABEL_EN.get(_c9g_key, _c9g_key)
                    _gopts = ([f"set {_c9g_key_en} for {n} to {_gv}" for n in _gnames]
                              if _gv is not None else _gnames)
                    log.info(f"[校正 C9-gen] 通稱設定句 {_gt!r} → clarify {len(_gnames)} 候選")
                    return "clarify", {
                        "question": (f"\"{_gt}\" matches {len(_gnames)} items. "
                                     f"Which one's {_c9g_key_en} do you want to change?"),
                        "options": _gopts,
                        "hint": "Tap one, or type the full item name"}, True
                break

    # C9-pct（r50·危險修復）：百分比/「N成」值＋設定意圖 → 誠實追問。C9 hard-return
    # 曾把「警戒值設成八成」的值抽成 8 開全店 180 項卡——依鐵律在 hard-return 前自帶防線。
    _c9p_t = _re.sub(r"[改設調變換]成", "", user_text)
    if (any(k in user_text for k in ("安全庫存", "安全水位", "警戒值", "前置天數",
                                      "補貨天數", "庫存上限", "庫存下限"))
            and any(v in user_text for v in ("改", "設", "調", "變"))
            and (_re.search(r"\d\s*[%％]", user_text)
                 or _re.search(r"[一二兩三四五六七八九十半\d]\s*成", _c9p_t))):
        log.info(f"[校正 C9-pct] 百分比/N成 設定值 → clarify: {user_text!r}")
        return "clarify", {
            "question": "設定值請用實際數量（百分比／幾成還不支援喔），你想設成多少？",
            "options": [], "hint": "例如「安全庫存改成 80」"}, True

    # C9c（r45）：設定總覽句（「北倉的安全庫存總覽」曾回「找不到商品『總覽』」）——
    # 要在 C9 抽 item 之前攔，否則 item='__unknown__:總覽' 進誠實拒絕路
    if any(k in user_text for k in ("安全庫存", "安全水位", "警戒值")) \
            and any(w in user_text for w in ("總覽", "全表", "一覽", "所有設定", "全部設定")):
        log.info(f"[校正 C9c] 設定總覽 → manage_config read: {user_text!r}")
        return "manage_config", {"action": "read", "key": "安全庫存"}, True

    # C9：含設定項詞 + 動作詞 → 強轉 manage_config（set_alert 已有自己的路由不干涉）
    # 也涵蓋 LLM 已經正確輸出 manage_config、但 key/value 自己抽壞的情況——原本
    # 只在 func_name 不是 manage_config 時才校正，等於預設 LLM 判對功能就一定
    # 也抽對參數，2026-07-02 實測連續兩句戳破這個假設：
    #   「北倉安全水位提高20」key 抽成空字串
    #   「把安全庫存提升一下」key 抽成「提升」（把動詞誤當設定項名稱）
    # 判斷條件除了 key 是空字串，也要涵蓋 key 不在已知設定項清單裡的情況
    # （代表抽到的不是真正的設定項名稱，是雜訊詞）。
    _c9_raw_key = (func_args.get("key") or "").strip()
    _c9_needs_fix = func_name == "manage_config" and (
        not _c9_raw_key or not any(_c9_raw_key in w or w in _c9_raw_key for w in _CONFIG_KEY_WORDS)
    )
    # r16 #44/#90：'days of cover for X' 是**庫存卡欄位查詢**不是設定——
    #   en-admin 已直達 query_inventory，C9 不可搶（曾回「restock target
    #   days=14」答非所問）。set 句（change cover days to 20）照走 C9。
    if (has_cfgkey and not has_cfgset
            and _re.search(r"\bdays? of (?:cover|stock|supply)\b|\bstock cover\b",
                           user_text.lower())):
        has_cfgkey = False
    if has_cfgkey and (func_name not in ("manage_config", "set_alert") or _c9_needs_fix):
        # 「多少」是問句語氣（「設定多少」「是多少」），有它一律當 read——
        # 曾經只擋「是多少/設多少」，「補貨前置天數設定多少」被「設」搶成 set 而報錯
        action = "set" if has_cfgset and not (any(w in user_text for w in _CONFIG_READ_CUES) and _extract_config_value(user_text) is None) else "read"
        # 抽 key
        key = max((w for w in _CONFIG_KEY_WORDS if w in user_text), key=len, default="安全庫存")
        new_args = {"action": action, "key": key}
        # 抽 warehouse
        for zh, en in _WH_ZH_MAP.items():
            if zh in user_text:
                new_args["warehouse"] = en
                break
        # 抽 value（+N/-N / 數字，阿拉伯或中文，見 _extract_config_value）
        if action == "set":
            _cv = _extract_config_value(user_text)
            if _cv is not None:
                new_args["value"] = _cv
        # 抽商品名縮小影響範圍（「啞鈴的警戒值訂在25」只改啞鈴，conv100-r5）
        _c9_item = _config_item_kw(user_text)
        if _c9_item:
            new_args["item"] = _c9_item
        log.info(f"[校正 C9] 設定意圖 → manage_config{{{action}}}（原 {func_name}）")
        return "manage_config", new_args, True

    # C9b: run_script script_name 去掉前綴動詞（「執行腳本 月底盤點」→「月底盤點」）
    if func_name == "run_script" and func_args.get("script_name"):
        # ⚠️ **期間要從原句帶進去**（2026-08-03）：LLM 抽的 script_name 常常
        #   只有「匯出」兩個字，訪客講的「昨天/最近 7 天」在裡面找不到
        #   ⇒ 匯出腳本永遠用預設天數。
        #   🩸 第一版把原句**附加到 script_name** ⇒ `_match_script` 比對不到
        #   白名單。⇒ 改用**獨立欄位** `_period_text`，不動 script_name。
        if _re.search(r"匯出|輸出|下載|導出|\bexport\b|\bdownload\b", user_text, _re.I):
            func_args = dict(func_args)
            func_args["_period_text"] = user_text
            # 🩸 英文版另一個坑：LLM 常把**期間詞本身**當成 script_name
            #   （`export movements yesterday` → script_name='yesterday'）
            #   ⇒ 白名單比對不到、走「不在白名單」的反問。
            #   ⇒ script_name 只剩期間詞時，用原句的匯出意圖補回腳本名。
            _sn_raw = str(func_args.get("script_name", "")).strip().lower()
            if _re.fullmatch(r"(?:today|yesterday|this\s+week|last\s+week|"
                             r"this\s+month|last\s+month|\d+\s*days?|"
                             r"今天|昨天|前天|本週|上週|本月|上個月)", _sn_raw):
                func_args["script_name"] = ("export movements"
                                            if _re.search(r"[a-z]", user_text, _re.I)
                                            else "匯出進出記錄")
                log.info(f"[校正 C9c] script_name 是期間詞 {_sn_raw!r} → "
                         f"{func_args['script_name']!r}")
        _script_prefixes = ("執行腳本", "幫我執行", "請執行", "麻煩跑", "執行一次",
                            "幫我跑", "跑一次", "執行", "跑", "啟動", "run ")
        sn = func_args["script_name"].strip()
        for _pfx in _script_prefixes:
            if sn.startswith(_pfx):
                sn = sn[len(_pfx):].strip()
                log.info(f"[校正 C9b] run_script 去前綴: {func_args['script_name']!r} → {sn!r}")
                func_args = dict(func_args)
                func_args["script_name"] = sn
                break

    # C10：含明確腳本動作詞 → 強轉 run_script
    #   明確腳本詞（盤點/匯出/重產）即使模型誤判成 manage_config 也要救回；
    #   但若同時含設定項詞（前置天數/安全庫存）則讓給 C9（避免誤傷設定查詢）。
    _script_strong = ("盤點", "匯出", "重產", "重新產生", "重生種子", "重建資料", "重新產生種子",
                      "跑盤點", "跑個盤", "跑一個盤", "體檢報告", "進出記錄")
    _sched_time_kws_c10 = ("每天", "每日", "每週", "每周", "每月", "每個月", "每星期", "每禮拜",
                           "定時", "排程", "固定時間",
                           "每天早上", "每天晚上", "自動執行", "自動跑")
    # EN build：排程時間詞原全中文 → 英文排程句（every morning / daily / at 9am）
    #   在 C10 也不被視為排程意圖
    #   ⚠️ 索取語氣讓路（2026-08-03，同 Pre-C-Sched）：`show me the daily report`
    #     的 daily 是形容詞，不該被視為排程意圖。
    _is_sched_intent = (not _en_daily_is_adjective(user_text)) and (
                       any(w in user_text for w in _sched_time_kws_c10)
                        or bool(_re.search(
                            r"\b(?:schedule|scheduled|scheduling|recurring|"
                            r"every\s+(?:day|morning|night|week|month|monday|tuesday|"
                            r"wednesday|thursday|friday|saturday|sunday)|"
                            r"daily(?!\s+(?:goods|necessities))|weekly|monthly|nightly|"
                            r"each\s+(?:day|week|month)|"
                            r"automatically|auto)\b", user_text, _re.I)))
    # r27：查詢語境豁免——「剛剛盤點的時候發現…庫存數字是多少」是查庫存不是要
    # 跑盤點腳本（曾開出 script_confirm 卡）
    _c10_query_ctx = any(w in user_text for w in ("是多少", "多少", "還剩", "剩幾",
                                                   "數字", "對不對", "的時候", "發現",
                                                   # r29：「盤點表在哪」是找檔案不是要跑腳本
                                                   "在哪", "哪裡", "哪邊")) \
        or bool(_re.search(
            # ── r23（中文詞表掃描）：這道**查詢語境豁免原本全中文** →
            #   `where is the stocktake file`（盤點表在哪）被當成「要跑腳本」
            #   攔下 → rejected；`where are the audit files` → agent_rca。
            #   中文「盤點表在哪」有豁免、英文沒有＝同一個問題兩種答案。
            #   ⚠️ 只認**查詢語境**（在哪/多少/找得到嗎），不含 run/execute
            #     ——`run the stocktake` 仍要正常開腳本卡。
            r"\b(?:where(?:'s| is| are)?|which file|what file|find|locate|"
            r"how many|how much|what(?:'s| is)? the (?:number|count|result)|"
            r"saved|stored|located|results? of)\b",
            user_text, _re.I))
    if not _is_sched_intent and not _c10_query_ctx and \
            (func_name not in ("run_script", "set_schedule") or not func_args.get("script_name")) \
            and not has_cfgkey and any(w in user_text for w in _script_strong):
        sname = next((w for w in ("月底盤點", "盤點", "匯出進出", "匯出", "體檢報告", "重產", "重新產生", "重生") if w in user_text), "")
        if sname:
            log.info(f"[校正 C10] 腳本意圖 → run_script（原 {func_name}）")
            return "run_script", {"script_name": sname}, True

    # C12：報告意圖 → generate_report（A 波：寫報告）
    #   「報告/報表」是強訊號 → 蓋過 list_expiring/list_low 等查詢路由。
    _report_words = ("報告", "報表", "體檢", "健檢", "出個報告", "全倉掃描",
                     "掃一遍", "整理一份", "彙整", "report", "做份報告", "產生報告",
                     "月報", "週報", "年報", "日報", "營運摘要", "匯總")
    # ⚠️ 排程讓路：「**每天**出缺貨報表」是 set_schedule 不是馬上出一份報表。
    #   C12 原本沒有這道讓路（`_is_sched_intent` 只用在 C10）→ 'schedule a daily
    #   low stock report at 9am' 被 C12 搶成 generate_report＝**排程功能對英文
    #   整條進不去**（實測直接產了一份報表）。中文詞表也一併掛上（共用邏輯）。
    #   ⚠️ 索取語氣不讓路（2026-08-03）：`show me the daily report` 要**產報告**，
    #     讓路會把它推給排程路徑 → 開出每天 09:00 的確認卡。
    _sched_let_pass = (not _en_daily_is_adjective(user_text)) and (
                      _is_sched_intent
                       or bool(_re.search(
                           r"\b(?:schedule|scheduled|scheduling|recurring|every\s+"
                           r"(?:day|morning|night|week|month|monday|tuesday|wednesday|"
                           r"thursday|friday|saturday|sunday)|"
                           r"daily(?!\s+(?:goods|necessities))|weekly|monthly|"
                           r"nightly|each\s+(?:day|week|month)|automatically|auto)\b",
                           user_text, _re.I)))
    # ⚠️ r22：**檢視讓路**——`show me the reports` / `list the report files`
    #   訪客想「看已經有哪些報表」，C12 卻**真的產生一份新報表**
    #   （裸詞 `report` 命中 _report_words）。展場訪客只是想看看，
    #   系統卻寫了檔案＝**做了訪客沒要求的事**，比答錯更糟。
    #   ⚠️ 判準要收窄：只有「檢視動詞 + 複數/檔案詞」才讓路，
    #     `generate a report` / `make me a report` 仍照常產生。
    _view_let_pass = bool(_re.search(
        r"\b(?:show|list|see|view|open|find|where(?:'s| is| are)?|"
        r"what)\b[^.?!]{0,20}\b(?:reports|report files?|files|scripts)\b",
        user_text, _re.I))
    if func_name != "generate_report" and not has_cfgkey and not _sched_let_pass \
            and not _view_let_pass \
            and any(w in user_text for w in _report_words):
        rt = ("low_stock" if (any(w in user_text for w in ("缺貨", "補貨", "低庫存"))
                              or _re.search(r"\b(?:low stock|running low|restock|"
                                            r"reorder|shortage)\b", user_text, _re.I)) else
              "expiring" if (any(w in user_text for w in ("到期", "效期", "過期"))
                             or _re.search(r"\b(?:expir\w*|shelf life)\b",
                                           user_text, _re.I)) else
              "rca" if (any(w in user_text for w in ("異常", "對不上", "短收"))
                        or _re.search(r"\b(?:anomal\w*|discrepanc\w*|count off|"
                                      r"shortfall|mismatch)\b", user_text, _re.I)) else "full")
        log.info(f"[校正 C12] 報告意圖 → generate_report{{{rt}}}（原 {func_name}）")
        return "generate_report", {"report_type": rt}, True

    # C13：檔案列表意圖 → list_files（B 波：動態找檔）
    _listfile_words = ("有哪些檔", "有什麼檔", "有哪些資料", "列出檔案", "看一下檔案",
                       "有哪些紀錄檔", "資料夾", "有哪些目錄", "列檔", "list files", "有什麼資料可以查",
                       # r11（真人語音批乾跑）：「你能跑哪些腳本」是合理提問，
                       #   原本 LLM 判成 query_related_items → 意圖閘門發現沒搭售詞
                       #   → **整句 rejected 婉拒**（訪客看到「這是倉管助理」教學文）。
                       #   ⚠️ 用**片語**不用裸 "script"（坑 1：短字串在英文必誤爆——
                       #     'run the script' 是執行、不是列清單）。
                       "what scripts", "which scripts", "list scripts",
                       "available scripts", "scripts can you", "scripts are there",
                       "what reports", "which reports", "list reports",
                       "what files", "which files",
                       # r22：`show me the reports` / `show me the scripts`
                       #   ——最自然的講法反而漏了（讓路 C12 之後掉到全店概覽）。
                       #   ⚠️ 仍用片語：裸 show/see 會撞掉大量商品查詢。
                       "show me the reports", "show me the scripts",
                       "show me the files", "show the reports", "show the scripts",
                       "see the reports", "see the scripts", "view the reports",
                       "report files", "script files", "list the reports",
                       "list the scripts", "list the files",
                       # r23（中文詞表掃描）：「檔案在哪」型——中文「盤點表在哪」
                       #   有豁免走 list_files，英文 `where is the stocktake file`
                       #   卻被 clf 判 search_log → 意圖閘門擋成 **rejected**。
                       #   `where are the audit files` 則回 agent_rca（答非所問）。
                       #   ⚠️ **不能只寫 `where is the`**——會撞掉
                       #     `where is the mouse stock` 這種正常查詢。
                       #     必須是「位置詞 + 檔案類名詞」的完整片語。
                       "audit files", "audit file", "stocktake file",
                       "log files", "log file", "saved reports",
                       "report file", "where are the files",
                       "where are the reports", "where is the report",
                       "where are the logs", "where is the file")
    if func_name != "list_files" and any(w in user_text for w in _listfile_words):
        # r11：英文要認單複數（'what scripts' 的 area 是 scripts；原本只比對
        #   單數 'script' 之類的鍵，複數句反而抽不到 area → 列成預設區域）
        _lf_low = user_text.lower()
        area = next((k for k in ("transactions", "orders", "master", "audit", "reports", "scripts",
                                 "交易", "採購", "主檔", "異動", "報告", "腳本") if k in _lf_low), "")
        if not area:
            for _sg, _pl in (("script", "scripts"), ("report", "reports"),
                             ("order", "orders"), ("transaction", "transactions")):
                if _re.search(rf"\b{_sg}s?\b", _lf_low):
                    area = _pl
                    break
        log.info(f"[校正 C13] 檔案列表意圖 → list_files（原 {func_name}）")
        return "list_files", ({"area": area} if area else {}), True

    # C14：警示設定意圖 → set_alert（自動化工具）
    #   「就通知我 / 設個提醒 / 警示我 / 低於X就告訴我」
    _alert_words = ("通知我", "提醒我", "警示", "告訴我", "就通知", "設個提醒",
                    "設定警示", "低於就", "缺貨就", "到期就", "alert", "提醒",
                    # EN build：第一關原本只有裸 "alert" → 'notify me when X
                    #   drops below 50' 過不了第一關，C14 救不回 LLM 的誤判
                    #   （clf 判 set_alert conf=1.00，LLM 吐 search_log → 純查詢）
                    "notify", "remind", "warn me", "ping me", "heads up")
    # ── EN build：第二個 and 條件原本全中文（通知/提醒/警示/告訴）→ 英文句
    #    命中 _alert_words 的 'alert' 卻過不了第二關，C14 對英文完全失效
    #    （實測 'set alert' 掉到 query_inventory 回全店概覽）。
    #    ⚠️ 坑 1：英文用詞界，避免 'alert' ∈ 'alerted' 之外的意外 substring。
    _en_alert_hit = bool(_re.search(
        r"\b(?:alert|alerts|notify|notification|notifications|remind|reminder|"
        r"warn|warning|ping|heads[- ]?up|let me know|tell me when|"
        r"drops? below|falls? below|goes? below|runs? out)\b",
        user_text, _re.I))
    # ⚠️ 守衛回歸：'expiry alerts' / 'stock alerts' / 'shelf life warnings' 是
    #   **查清單**（exp / low_stock），不是設警示規則。裸名詞 alerts/warnings
    #   跟著查詢主題詞出現時要讓路，否則 C14 會把整批查詢句搶成 alert_confirm。
    #   （鏡像於 C3「警示設定讓路」——那邊是設定讓查詢路，這邊是查詢讓設定路。）
    if _en_alert_hit and not _re.search(
            r"\b(?:set|create|add|make|configure|enable|turn on|schedule|"
            r"notify me|remind me|let me know|tell me when|alert me|when(?:ever)?|"
            r"if|once|below|under|drops?|falls?|goes?)\b", user_text, _re.I):
        # 沒有任何「設定/條件」語 → 是查詢句（'expiry alerts'、'stock alerts'）
        _en_alert_hit = False
    if func_name != "set_alert" and any(w in user_text for w in _alert_words) \
            and (any(w in user_text for w in ("通知", "提醒", "警示", "告訴"))
                 or _en_alert_hit):
        # EN build：condition 判定原只認中文 → 英文一律落到 below_safety
        #   （'alert me when X runs out' 應是 out_of_stock、'expiring' 應是 expiring）
        cond = ("out_of_stock" if (any(w in user_text for w in ("缺貨", "斷貨", "沒貨"))
                                   or _re.search(r"\b(?:runs? out|run out|out of stock|"
                                                 r"sold out|stockout)\b", user_text, _re.I)) else
                "expiring" if (any(w in user_text for w in ("到期", "過期", "效期"))
                               or _re.search(r"\b(?:expir\w*|shelf life|use[- ]?by|"
                                             r"best before)\b", user_text, _re.I)) else
                "below_safety")
        log.info(f"[校正 C14] 警示意圖 → set_alert{{{cond}}}（原 {func_name}）")
        # 直接在 C14 內做 C17b 的工作，因為 return 後 C17b 跑不到
        import re as _re14
        # EN build：門檻正則原只認中文（低於/少於）→ 'drops below 30' 抓不到數字
        _thr14 = (_re14.search(r'(?:低於|少於|小於|不足)\s*(\d+)', user_text)
                  or _re14.search(r'\b(?:below|under|less than|fewer than|drops? to|'
                                  r'falls? to)\s*(\d+)', user_text, _re14.I))
        _tgt14 = _extract_sku_keyword(user_text) or ""
        _c14_args = {"condition": ("below_threshold" if _thr14 else cond), "target": _tgt14}
        if _thr14:
            _c14_args["threshold"] = int(_thr14.group(1))
        return "set_alert", _c14_args, True

    # C15：產採購單意圖 → generate_po（閉環）
    _po_words = ("採購單", "下單", "補貨單", "進貨單", "幫我叫貨", "產採購", "開採購",
                 "產po", "purchase order", "下採購", "補貨清單下單", "幫我補貨", "要補的貨")
    # r76：「進貨單價多少」的「進貨單」撞 _po_words 曾誤開採購草稿卡——
    # 價格問句不是開單意圖
    if (func_name != "generate_po" and any(w in user_text for w in _po_words)
            and not any(w in user_text for w in ("單價", "價格", "多少錢", "什麼價"))):
        src = "shortfall" if any(w in user_text for w in ("短收", "對不上", "補單")) else "low_stock"
        log.info(f"[校正 C15] 採購意圖 → generate_po{{{src}}}（原 {func_name}）")
        return "generate_po", {"source": src}, True

    # C16：跨期比較意圖 → compare_periods
    _cmp_period_words = ("這個月跟上個月", "本月對比上月", "跟上月比", "跨期", "兩個月比",
                         "月對比", "上月相比", "變化最大", "哪些變化大", "成長最多", "衰退最多",
                         "這月和上月", "本月vs上月", "月增減",
                         # r74：「誰進步最多」「退步最多的呢」曾掉熱銷榜/rejected
                         "進步最多", "退步最多", "進步得最多", "誰進步", "誰退步",
                         # r78：週對週比較後「差最多的是哪個」曾 rejected
                         "差最多", "掉最多", "差距最大", "落差最大")
    # ── EN build：英文跨期比較（原詞表全中文 → 英文句掉到
    #    compare_warehouses/庫存查詢。⚠️ 要**排除倉庫比較**：
    #    'compare north and south' 是比倉不是比期間）──
    _cmp_period_en = bool(
        _re.search(r"\b(?:last|past|previous)\s+(?:two|2|few)?\s*"
                   r"(?:months?|weeks?|periods?)\b", text_low)
        or _re.search(r"\b(?:this|current)\s+month\b.{0,20}\b(?:last|previous)\s+month\b",
                      text_low)
        or _re.search(r"\bmonth[- ]over[- ]month\b|\bperiod\s+compar|"
                      r"\btrend(?:ing)?\b|\bbiggest\s+(?:growth|drop|decline|change)\b|"
                      r"\bgrew\s+the\s+most\b|\bdropped\s+the\s+most\b", text_low))
    if _cmp_period_en and any(w in text_low for w in ("north", "central", "south")):
        _cmp_period_en = False   # 提到倉名＝比倉，讓給 compare_warehouses
    # ⚠️ 匯出句讓路（2026-08-04）：`export movements last week` 的「last week」
    #   是**匯出期間**不是跨期比較,但 _cmp_period_en 正則直接命中
    #   ⇒ 把 Pre-C10 已定案的 run_script{匯出} 蓋掉。
    #   今天第四次同型（C12/C7b/C18/C16 各蓋過一次上游定案）
    #   ⇒ 上游定案後,每個下游覆蓋層都要加交集防護。
    #   收窄：匯出動詞 + 進出受詞**兩者齊備**才讓路,
    #   單純 `compare last two months` 照常走比較。
    # ⚠️ 讓路動詞與 Pre-C10 同一組（2026-08-04）：訪客不會只講 export，
    #   `i'd like to see last week's movements` 也是要匯出。
    #   兩處用同判準，避免各自演化成不同行為。
    _c16_is_export = (
        bool(_re.search(r"\b(?:export|download|dump|save|extract)\b|"
                        r"\b(?:give|get|send|fetch)\s+me\b|"
                        r"\bcan\s+i\s+(?:get|have|see)\b|"
                        r"\bi(?:'d|\s+would)?\s+(?:need|want|like)\b", user_text, _re.I))
        and bool(_re.search(r"\b(?:movements?|transactions?|records?|logs?|"
                            r"history|in\s*/?\s*out|csv)\b", user_text, _re.I))
    ) or (func_name == "run_script"
          and str(func_args.get("script_name", "")) in ("匯出", "export movements"))
    # ⚠️ 單一商品句不轉跨期比較（2026-08-04）：
    #   `how many bluetooth earphones moved last week` 是問**那個商品**的進出，
    #   不是全店跨期成長比較 ⇒ 讓給 movement/inventory。
    #   接地用 match_items 分數（坑 10：別用字面比對）。
    _c16_has_item = False
    try:
        # ⚠️ 用 match_items(整句) 不用 _extract_sku_keyword（2026-08-04）：
        #   抽取器 fallback 會回傳雜詞再被 fuzzy 誤配（'download movements
        #   last quarter' 曾 item=True 擋掉匯出）。整句比對無商品 token=0 分。
        import warehouse as _W_c16
        _c16_m = _W_c16.match_items(user_text)
        # 門檻 6：實測雜訊 3-5（'last'→Elastic 滑窗）/ 真商品 7-13
        _c16_has_item = bool(_c16_m and _c16_m[0].get("score", 0) >= 6)
    except Exception:
        _c16_has_item = False
    if _c16_is_export or _c16_has_item:
        log.info(f"[C16] 讓路 → 不轉 compare_periods"
                 f"（export={_c16_is_export} item={_c16_has_item}）")
    if func_name != "compare_periods" and not _c16_is_export \
            and not _c16_has_item and (
            any(w in user_text for w in _cmp_period_words) or _cmp_period_en):
        log.info(f"[校正 C16] 跨期比較 → compare_periods（原 {func_name}）")
        return "compare_periods", {"metric": "out"}, True

    # ── C16b（r21）：**反向修正**——clf 把單純的期間查詢誤判成跨期比較 ──
    #   `movements last week` / `what happened last week` / `last week
    #   movements` 全被 clf 判成 compare_periods（conf 0.97）→ 回「近兩個月
    #   出貨變化榜」，訪客問的是上週進出貨。
    #   成因：clf 把 `last week` 當成 compare 的強訊號（語料裡跨期比較句
    #   多半帶 last/previous），但「上週的進出」跟「這月比上月」是兩回事。
    #   ⚠️ C16 是 `func_name != "compare_periods"` 才跑 → clf 直接判成
    #     compare_periods 時**根本進不到 C16**，得在這裡反向攔。
    #   判準：有明確**進出貨詞**且**沒有比較詞** → 退回 query_movement。
    if (func_name == "compare_periods" and _is_mostly_english(user_text)
            and _re.search(r"\b(?:movements?|came in|come in|went out|go out|"
                           r"shipped|received|inbound|outbound|deliveries|"
                           r"happened|activity)\b", text_low)
            and not _re.search(r"\b(?:compare|versus|\bvs\b|change[ds]?|growth|"
                               r"decline|trend|most|biggest|month[- ]over)\b",
                               text_low)):
        _p16b = _period_from_en(user_text) or "this_week"
        _d16b = ("in" if _re.search(r"\b(?:came in|come in|received|inbound)\b", text_low)
                 else "out" if _re.search(r"\b(?:went out|go out|shipped|outbound)\b", text_low)
                 else "both")
        log.info(f"[校正 C16b] clf 誤判跨期比較 → query_movement "
                 f"period={_p16b} direction={_d16b}")
        return "query_movement", {"period": _p16b, "direction": _d16b}, True

    # C11-pre0：manage_config action 修正 — 含「改成/設成/調成/改為/設為」→ set
    # 也涵蓋「降低/提升/提高/調低/調高」這類不帶「成/為/到」的直接動詞（2026-07-02
    # 實測「北倉安全水位降低15」抓到：LLM 把 action 判成 read，這類詞沒被
    # _set_verbs 涵蓋，C11-pre0 沒機會校正，value 也就跟著沒被抽）。
    _set_verbs = ("改成", "設成", "調成", "改為", "設為", "調整為", "改為", "修改成",
                  "調到", "改到", "設定成", "更改為", "更改成",
                  "降低", "提升", "提高", "調低", "調高")
    # 「加N/減N」（後面緊跟數字）也是設定動作，但單獨「加/減」太寬不能直接收
    # （「加起來」的「加」）；用 _extract_config_value 抓得到值就代表是設定意圖。
    _c11pre0_is_set = any(v in user_text for v in _set_verbs) or (
        _extract_config_value(user_text) is not None)
    if func_name == "manage_config" and func_args.get("action") == "read" \
            and _c11pre0_is_set:
        func_args = {**func_args, "action": "set"}
        log.info("[校正 C11-pre0] manage_config action read→set（含改/設/調動詞）")

    # C11-inv：非設定句誤投 manage_config → 攔回 inventory——「瑜珈墊庫存還夠嗎」
    # LLM 給 manage_config{read, key:庫存}，回設定引導答非所問（conv100-r5）。
    # 句中完全沒有設定項詞、卻有真商品名 + 存量語氣 → 查庫存。
    if func_name == "manage_config" \
            and not any(w in user_text for w in _CONFIG_KEY_WORDS) \
            and "設定" not in user_text:
        _c11i_kw = _extract_sku_keyword(user_text)
        import warehouse as _W_c11i
        if (_c11i_kw and _W_c11i.match_items(_c11i_kw)
                and any(w in user_text for w in ("庫存", "還有", "剩", "夠", "多少", "存量"))):
            log.info(f"[校正 C11-inv] 非設定句誤投 config → query_inventory kw={_c11i_kw!r}")
            return "query_inventory", {"keyword": _c11i_kw}, True

    # C11-pre0b：讀取語氣（現在是設多少/目前多少）且句中抽不到數值 → 一律 read
    # （「北倉的安全水位現在是設多少」LLM 給 set value='已設' → 誤開 clarify 問值，conv100-r5）
    if func_name == "manage_config" and func_args.get("action") == "set" \
            and any(w in user_text for w in _CONFIG_READ_CUES) \
            and _extract_config_value(user_text) is None:
        func_args = {k: v for k, v in func_args.items() if k != "value"}
        func_args["action"] = "read"
        log.info("[校正 C11-pre0b] manage_config 讀取語氣 → action=read")

    # C11-pre：manage_config key 補全 — 模型可能把「補貨前置天數」截成「補貨」或空字串
    if func_name == "manage_config":
        raw_key = str(func_args.get("key", "")).strip()
        # 「補貨」單字 or 空字串 + user_text 含「前置/天數/days」→ 補全為「前置天數」
        # 「補貨天數」也算（「中倉補貨天數縮短成3天」，conv100-r5）
        if (raw_key in ("補貨", "") or ("天數" in raw_key and "前置" not in raw_key)) \
                and any(w in user_text for w in ("前置", "前置天數", "天數", "days")):
            func_args = {**func_args, "key": "前置天數"}
            log.info(f"[校正 C11-pre] manage_config key {raw_key!r} → '前置天數'")
        elif raw_key == "" and any(w in user_text for w in ("安全庫存",)):
            func_args = {**func_args, "key": "安全庫存"}
            log.info(f"[校正 C11-pre] manage_config key '' → '安全庫存'")
        else:
            # key 被 LLM 截半（「安全」）或塞雜訊、_resolve_key 會解不開 → 用原句
            # 完整設定項詞覆蓋（「中倉安全水位全面調升10」key='安全' 回引導頁，conv100-r6）
            _key_hit = max((w for w in _CONFIG_KEY_WORDS if w in user_text), key=len, default=None)
            if _key_hit and not any(w in raw_key for w in _CONFIG_KEY_WORDS):
                func_args = {**func_args, "key": _key_hit}
                log.info(f"[校正 C11-pre] manage_config key {raw_key!r} → {_key_hit!r}（原句重抽）")

    # C11c：manage_config 補商品名——LLM 只給 key=安全庫存 沒帶商品，影響範圍
    # 會變成全部商品 183 項（「瑜珈墊的安全庫存加20」，conv100-r5）
    if func_name == "manage_config" and not func_args.get("item"):
        _c11c_item = _config_item_kw(user_text)
        if _c11c_item:
            func_args = {**func_args, "item": _c11c_item}
            log.info(f"[校正 C11c] manage_config 補 item={_c11c_item!r}")

    # C11d（r43，不猜原則）：item 是歧義短稱（咖啡×5/露營×4…）→ 寫入卡曾一口氣
    # 改整個家族 15 項。歧義寫入不猜、追問是哪一個。
    if func_name == "manage_config" and func_args.get("action") == "set" \
            and func_args.get("item"):
        import warehouse as _W_c11d
        # r55（分支遍歷抓到）：單字通稱（鍋子/帽子…）在 config 路曾「找不到商品」——
        # 通稱表展開成候選，跟查詢路同一套不猜邏輯
        _c11d_item_s = str(func_args["item"]).strip()
        if _c11d_item_s.startswith("__unknown__:"):
            _c11d_item_s = _c11d_item_s.split(":", 1)[1]
        _c11d_gen = getattr(_W_c11d, "_GENERIC_QUERY_FALLBACK", {}).get(_c11d_item_s)
        if _c11d_gen:
            _c11d_m = [{"item": it, "score": 7} for it in _W_c11d.state().items
                       if any(f in it["name"] for f in _c11d_gen)]
        else:
            _c11d_m = _W_c11d.match_items(func_args["item"])
        if len(_c11d_m) >= 2:
            _c11d_top = _c11d_m[0]["score"]
            # 嚴格同分才算歧義（咖啡→5個全 7 分）；指名句的精確命中會領先，不誤攔
            _c11d_tied = [r["item"]["name"] for r in _c11d_m if r["score"] == _c11d_top]
            if len(_c11d_tied) >= 2:
                log.info(f"[校正 C11d] config item 歧義 {func_args['item']!r} → clarify {len(_c11d_tied)} 候選")
                # r55（分支遍歷抓到）：選項要帶完整語（商品+設定+值），點選/序數選完
                # 直接續 config 流——裸商品名會丟失「改成50」的原意圖變成查詢追問
                _c11d_key = func_args.get("key", "安全庫存")
                _c11d_val = str(func_args.get("value", "")).strip()
                # EN build：選項是「點了會送回後端的查詢字串」，中文會被英文版
                #   守門員 reject＝點了沒反應。用已驗證可用的英文設定句型。
                _c11d_key_en = _CFG_KEY_LABEL_EN.get(_c11d_key, _c11d_key)
                _c11d_opts = ([f"set {_c11d_key_en} for {n} to {_c11d_val}"
                               for n in _c11d_tied[:8]]
                              if _c11d_val else _c11d_tied[:8])
                return "clarify", {
                    "question": (f"\"{func_args['item']}\" matches {len(_c11d_tied)} items. "
                                 f"Which one's {_c11d_key_en} do you want to change?"),
                    "options": _c11d_opts,
                    "hint": "Tap one, or type the full item name"}, True

    # （r43 曾加 C11e「無 item 追問範圍」→ 守衛 11 句誤攔即撤：倉別/全域 config
    #   不指名商品是既有合法行為，確認卡本身就是保險。危險防線收斂為 C11f 百分比。）

    # C11f（r43）：百分比數值不支援——「調成200%」曾被當絕對值 200 寫入。誠實追問。
    # r50：「設成八成」的「N成」也是百分比語（曾被抽成 value=8 開全店卡=危險）；
    # 先剝「改成/設成/調成」的動詞成再驗，避免誤判。
    _c11f_t = _re.sub(r"[改設調變換]成", "", user_text)
    if func_name == "manage_config" and func_args.get("action") == "set" \
            and (_re.search(r"\d\s*[%％]", user_text)
                 or _re.search(r"[一二兩三四五六七八九十半\d]\s*成", _c11f_t)):
        log.info(f"[校正 C11f] config 百分比值 → clarify: {user_text!r}")
        return "clarify", {
            "question": "設定值請用實際數量（不支援百分比喔），你想設成多少？",
            "options": [], "hint": "例如「安全庫存改成 80」"}, True

    # C11：manage_config set 缺 warehouse → 預設 all（不擋，給預設）
    if func_name == "manage_config" and func_args.get("action") == "set" \
            and not func_args.get("warehouse"):
        for zh, en in _WH_ZH_MAP.items():
            if zh in user_text:
                func_args["warehouse"] = en
                break
        func_args.setdefault("warehouse", "all")
        log.info(f"[校正 C11] manage_config set 補 warehouse={func_args['warehouse']}")

    # C11b：manage_config value 補全（加減N → ±N；改成N → N；中文數字也支援）
    # （用頂層 _re；此處不可再 import re as _re，否則整個函式的 _re 變區域變數，
    #   C2c 等更前面用 _re 的地方會 UnboundLocalError——r36 踩過，卡死全枚舉近 100 分）
    if func_name == "manage_config" and func_args.get("action") == "set":
        raw_v = str(func_args.get("value", "")).strip()
        # 已是合法阿拉伯數字（含 ±）→ 不動；否則從 user_text 用統一函式重抽
        if not _re.match(r'^[+\-]?\d+$', raw_v):
            _cv = _extract_config_value(user_text)
            if _cv is not None:
                func_args["value"] = _cv
                log.info(f"[校正 C11b] manage_config value {raw_v!r} → {func_args['value']!r}")
        else:
            # LLM 給純數字但原句是相對語氣（「安全庫存加20」LLM 給 value=20 →
            # 被當設為 20 絕對值，conv100-r5）→ 依原句補回正負號
            _cv2 = _extract_config_value(user_text)
            if (_cv2 is not None and _cv2.startswith(("+", "-"))
                    and _cv2.lstrip("+-") == raw_v.lstrip("+-") and _cv2 != raw_v):
                func_args["value"] = _cv2
                log.info(f"[校正 C11b2] manage_config value {raw_v!r} → {_cv2!r}（相對語氣）")

    # C17：search_log 參數清理 + keyword 抽取（_extract_sku_keyword）
    if func_name == "search_log":
        model_kw = func_args.get("keyword", func_args.get("script_name", "")).strip()
        # rewrite 後的 user_text 可能是「XXX 帳對不上」，把 RCA 後綴去掉只留商品名
        _rca_suffixes = (" 帳對不上", " 帳不對", " 對不上帳", " 對不起來", " 兜不攏", "帳對不上", "庫存帳對不上")
        _clean_user = user_text
        for _sfx in _rca_suffixes:
            if _clean_user.endswith(_sfx):
                _clean_user = _clean_user[: -len(_sfx)].strip()
                break
        # EN build：英文 RCA 語尾同樣要剝，否則 'charger cable stock doesnt
        #   add up' 整句拿去抽 keyword 會被 doesnt/add/up 干擾
        if _is_mostly_english(user_text):
            _clean_user = _re.sub(
                r"\b(?:stock|inventory|count|numbers?|figures?)?\s*"
                r"(?:doesn'?t|does not|dont|do not)\s+(?:add up|match|tally)\b"
                r"|\bis\s+off\b|\blooks?\s+(?:off|wrong|strange)\b"
                r"|\bcount\s+is\s+(?:off|strange|wrong)\b"
                r"|\bshortfall\b|\bdiscrepanc(?:y|ies)\b|\bmismatch\b"
                r"|\bwhat\s+happened\s+to\b|\bwho\s+(?:moved|took|changed)\b"
                r"|\btrace\s+the\b|\binvestigate\s+the\b",
                " ", _clean_user, flags=_re.I)
            _clean_user = _re.sub(r"\s+", " ", _clean_user).strip(" ?.!,")
        # 先用模型抽到的 keyword 跑 SKU match；沒結果再用去後綴的 user_text
        # ⚠️ EN build：LLM 的 keyword 常是**幻覺**（'charger cable stock
        #   doesnt add up' → LLM 吐 'power cord'，句中根本沒有）。拿它去
        #   match 會比到 Power Bank，再被下游接地層清空 → 整句退成全域對帳
        #   （守衛 rca：回 6 筆全域短收而非指名商品）。英文先驗證接地，
        #   不接地就改用原句抽。
        if model_kw and _is_mostly_english(user_text) \
                and not _kw_grounded(str(model_kw), user_text):
            log.info(f"[校正 C17] LLM kw {model_kw!r} 未接地 → 改用原句抽")
            model_kw = ""
        final_kw = _extract_sku_keyword(model_kw) if model_kw else ""
        if not final_kw:
            final_kw = _extract_sku_keyword(_clean_user)
        # EN build：英文句抽不到商品名時**留空**，不要把整句當 keyword。
        #   'check the purchase records' 沒指名商品＝要看全域對帳，整句當
        #   keyword 會讓 tool 去 fuzzy 撈出兩個不相干商品開 clarify
        #   （守衛 rca：'"check the purchase records" matches 2 items'）。
        #   留空 → search_log 走全域掃描，正是這句要的。
        _fallback_kw = ("" if _is_mostly_english(user_text)
                        else (_clean_user or user_text))
        func_args = {
            "keyword":    final_kw or model_kw or _fallback_kw,
            "time_range": func_args.get("time_range", func_args.get("period")),
        }
        if func_args["time_range"] is None:
            del func_args["time_range"]
        log.info(f"[校正 C17] search_log keyword → {repr(func_args['keyword'])}")
        return func_name, func_args, True  # hard：C18 不得再覆蓋 search_log

    # C17a-pre：query_inventory 的 warehouse 幻覺防呆——LLM 給了單倉、句中卻沒
    # 提那個倉（「氣泡水三個倉加起來有幾瓶」被塞 central 只回中倉，conv100-r5）
    # → 丟掉讓它查三倉總量。
    # LLM 自帶的 keyword 也要接地——「給我來一下」曾幻覺 kw=耳機回無關商品
    # （conv100-r15；C1 只驗「補」的 kw，LLM 直接給的沒人驗）
    if func_name == "query_inventory" and func_args.get("keyword"):
        _kw_llm = func_args["keyword"]
        import warehouse as _W_g
        if _W_g.match_items(_kw_llm) and not _kw_grounded(_kw_llm, user_text):
            func_args = {k: v for k, v in func_args.items() if k != "keyword"}
            log.info(f"[校正 C1c] LLM kw「{_kw_llm}」與原句不接地 → 清 keyword")

    # 倉別值正規化（r24）：LLM 偶爾輸出中文倉名（warehouse=中倉），C17a-pre 的
    # 英文值前提比不到 → 幻覺單倉逃過丟棄（「防曬遮陽帽三個倉各剩多少」RPI5
    # 只回中倉）。先轉英文值讓後面防呆吃得到。
    _whv24 = func_args.get("warehouse")
    if (func_name == "query_inventory" and isinstance(_whv24, str) and _whv24
            and _whv24 not in ("north", "central", "south")):
        _whn24 = next((en for zh, en in _WH_ZH_MAP.items() if zh in _whv24 and en != "all"), None)
        func_args = {k: v for k, v in func_args.items() if k != "warehouse"}
        if _whn24:
            func_args["warehouse"] = _whn24
    # 多倉分佈意圖（三個倉/各剩/每倉…）→ 單倉 filter 一律丟棄回三倉分佈（r24）
    if (func_name == "query_inventory" and func_args.get("warehouse")
            and (any(w in user_text for w in ("三個倉", "三倉", "每個倉", "每倉",
                                              "各剩", "各多少", "各有多少", "各還", "各倉"))
                 # r19：英文的多倉分佈講法（原本只有中文 → 英文訪客問
                 #   'across all warehouses' 會被硬塞單倉 filter）
                 or _re.search(r"\b(?:all (?:three )?warehouses|each warehouse|"
                               r"every warehouse|per warehouse|across warehouses|"
                               r"by warehouse|in each|breakdown)\b",
                               user_text, _re.I))):
        func_args = {k: v for k, v in func_args.items() if k != "warehouse"}
        log.info("[校正 C17a-pre] 多倉分佈意圖 → 丟 warehouse")

    if func_name == "query_inventory" and func_args.get("warehouse") in ("north", "central", "south"):
        # ⚠️ r19（坑 7）：這張接地表**原本只有中文倉名** → 英文句
        #   `earphones in central` 的 warehouse 一律被判幻覺丟掉 →
        #   回全三倉概況（數字沒錯，但訪客問「中倉多少」得自己挑）。
        #   query_inventory 本來就收 warehouse（warehouse.py:650），
        #   帶了就回「in Central: 42 units」精準回答。
        _wh_zh_names = {"north": ("北倉", "北區", "北邊", "北部", "north"),
                        "central": ("中倉", "中區", "central"),
                        "south": ("南倉", "南區", "南邊", "南部", "south")}
        _ut_c17 = user_text.lower()
        _c17ap_whs = {z[0] for z in ("北倉", "北區", "中倉", "中區", "南倉", "南區") if z in user_text}
        # 英文倉名也要納入「提了幾個倉」的計算（多倉＝比較語意，丟單倉 filter）
        _c17ap_whs |= {_w for _w in ("north", "central", "south")
                       if _re.search(rf"\b{_w}\b", _ut_c17)}
        if not any((z in user_text) or (z.isascii() and _re.search(rf"\b{z}\b", _ut_c17))
                   for z in _wh_zh_names[func_args["warehouse"]]):
            func_args = {k: v for k, v in func_args.items() if k != "warehouse"}
            log.info("[校正 C17a-pre] query_inventory 丟棄幻覺 warehouse")
        elif len(_c17ap_whs) >= 2:
            # 「智慧手環中倉北倉哪邊存量多」提兩倉是比較語意 → 丟單倉 filter
            # 回三倉分佈（conv100-r12）
            func_args = {k: v for k, v in func_args.items() if k != "warehouse"}
            log.info("[校正 C17a-pre] 句提多倉 → 丟 warehouse 回三倉分佈")

    # C17a：query_inventory / query_movement 從 user_text 補 warehouse（「南倉洗衣精」→ warehouse=south）
    # 句中提到 ≥2 個不同倉（「智慧手環中倉北倉哪邊存量多」）→ 不補，讓三倉
    # 分佈回答比較問題（conv100-r12：曾硬補第一個倉只回單倉）
    _c17a_whs = {z[0] for z in ("北倉", "北區", "中倉", "中區", "南倉", "南區") if z in user_text}
    if (func_name in ("query_inventory", "query_movement") and not func_args.get("warehouse")
            and len(_c17a_whs) < 2):
        for zh, en in _WH_ZH_MAP.items():
            if zh in user_text:
                func_args = {**func_args, "warehouse": en}
                log.info(f"[校正 C17a] {func_name} 補 warehouse={en}")
                break

    # C17a2：query_inventory 的 keyword 其實是純倉名（「北倉總共值多少」LLM 把
    #   「北倉」當商品 → clarify「找不到北倉」）。清掉 keyword、補 warehouse，
    #   讓它查該倉概覽（RPI5 conv100-r3）。
    if func_name == "query_inventory":
        _kw_wh = (func_args.get("keyword") or "").strip()
        _WH_NAMES = ("北倉", "中倉", "南倉", "北區倉", "中區倉", "南區倉",
                     "北區", "中區", "南區", "全倉", "所有倉", "各倉")
        if _kw_wh in _WH_NAMES:
            _en = _WH_ZH_MAP.get(_kw_wh, "all")
            func_args = {k: v for k, v in func_args.items() if k != "keyword"}
            func_args["warehouse"] = _en
            log.info(f"[校正 C17a2] keyword 是倉名「{_kw_wh}」→ 清 keyword、warehouse={_en}")
        # 「全店/總庫存」是全倉概覽不是商品名（「想知道全店總庫存值多少錢」
        # 曾 clarify 找不到「想知道全店總 值」，conv100-r7）
        elif _kw_wh and any(w in _kw_wh for w in ("全店", "總庫存", "全部商品", "所有商品",
                                                   # r18：「給我全部庫存的總表」「有多少種商品」
                                                   # 曾 clarify 找不到「總表/種商品」
                                                   "總表", "種商品", "全部庫存", "全部")):
            func_args = {k: v for k, v in func_args.items() if k != "keyword"}
            log.info(f"[校正 C17a2b] keyword 含全店/總庫存 → 清 keyword 查概覽")
        # 問價值的句子 kw 抽成雜訊（「北倉塞了多少錢的貨」→ kw「塞 貨」）
        # → 清 kw 查該倉概覽（conv100-r8）
        elif _kw_wh and any(w in user_text for w in ("多少錢", "值多少", "總值")):
            import warehouse as _W_17c
            _m17c = _W_17c.match_items(_kw_wh)
            if not _m17c or _m17c[0].get("score", 0) < 3:
                func_args = {k: v for k, v in func_args.items() if k != "keyword"}
                log.info(f"[校正 C17a2c] 價值問句 kw 雜訊「{_kw_wh}」→ 清 keyword 查概覽")
        # C17a2d：kw 只靠單字雜訊低分亂中 → 剝雜訊字留純殘字，讓工具誠實回
        # 「找不到」而不是回無關商品（「把 傘都調去」score=1 中拖把，conv100-r8）
        elif _kw_wh:
            import warehouse as _W_17d
            _m17d = _W_17d.match_items(_kw_wh)
            if _m17d and _m17d[0].get("score", 0) < 3:
                _kw17d = _kw_wh
                for _nz in ("把", "都", "調去", "調到", "移去", "搬去", "的", "了",
                            "去", "到", "給", "從", "幫我", " "):
                    _kw17d = _kw17d.replace(_nz, "")
                if _kw17d and _kw17d != _kw_wh:
                    func_args = {**func_args, "keyword": _kw17d}
                    log.info(f"[校正 C17a2d] kw 低分雜訊「{_kw_wh}」→「{_kw17d}」")

    # ── EN build：clf/LLM 給的 keyword 被「撞名詞」帶偏 → 讓模糊層糾正 ──
    #   'whats the lapptop case count' → clf 直接給 keyword='Phone
    #   Protective Case'（靠 **case** 撞名），但句中 lapptop→**laptop** 是
    #   更強的訊號（0.923），正解是 14-inch Laptop Bag。
    #   條件從嚴，只在「模糊層有答案 + 該答案的字面證據明顯較強」時才覆寫：
    #     ①模糊層對整句有明確答案 ②與現有 kw 不同
    #     ③模糊答案在原句的字面支持 > 現有 kw 的字面支持
    #   （字面支持＝商品名的詞有幾個能在句中找到近似 token）
    if (func_name == "query_inventory" and _is_mostly_english(user_text)
            and func_args.get("keyword")):
        try:
            # 餵剝過虛詞的 core（見 _en_query_core 註解）
            _fz_fix = _en_fuzzy_keyword(_en_query_core(user_text))
        except Exception:
            _fz_fix = ""
        if _fz_fix and _fz_fix != func_args.get("keyword"):
            import difflib as _dlfx

            def _lit_support(_nm: str) -> int:
                _nw = [w.strip(" ?.!,'\"").lower()
                       for w in _re.split(r"[\s\-/]+", _nm.lower())]
                _nw = [w for w in _nw if len(w) >= 4 and not any(c.isdigit() for c in w)]
                _tk = [t.strip(" ?.!,'\"").lower()
                       for t in _re.split(r"[\s\-/]+", user_text.lower())]
                _tk = [t for t in _tk if len(t) >= 4]
                return sum(1 for w in _nw
                           if w in _tk or _dlfx.get_close_matches(w, _tk, n=1, cutoff=0.85))
            _s_new, _s_old = _lit_support(_fz_fix), _lit_support(func_args["keyword"])
            # 平手也採用模糊層：'lapptop case' 兩者都只中一個詞
            #   （Laptop Bag 靠 lapptop≈laptop、Phone Case 靠 case），
            #   但模糊層是拿**整個 core** 判的、證據更完整，而 clf 的 kw
            #   只是被通用詞 case 撞名帶偏。舊 kw 明顯較強時（>）才保留。
            if _s_new >= _s_old:
                # ⚠️ 正規化成**主檔名**再寫回：模糊層可能回 alias 值
                #   （'Laptop Bag' 而非主檔的 '14-inch Laptop Bag'），
                #   下游閘門拿 alias 值比對會對不到 → 清掉 kw 回全店概覽
                #   （實測 lapptop case 就是這樣從誤配變成概覽）。
                _fz_norm = _fz_fix
                try:
                    import warehouse as _W_fz
                    _mfz = _W_fz.match_items(_fz_fix)
                    if _mfz and _mfz[0].get("score", 0) >= 4:
                        _fz_norm = _mfz[0]["item"]["name"]
                except Exception:
                    pass
                log.info(f"[EN kw 糾正] clf kw {func_args['keyword']!r}(支持{_s_old}) "
                         f"→ 模糊層 {_fz_norm!r}(支持{_s_new})")
                func_args = {**func_args, "keyword": _fz_norm}

    # 通用 category 接地檢查（inventory/related 直達路徑，conv100-r13）
    if func_name in ("query_inventory", "query_related_items"):
        func_args = _drop_ungrounded_category(func_args, user_text)

    # ── EN build：英文類別句補 category（'all Sports stock'）────────────────
    #   全系統 cat_zh_map 鍵全中文 → 英文類別詞填不進 category。走 OOV 那條的
    #   已在 oov:noex→cat 攔下，但**類別詞撞到商品名**時（sports ∈ Electrolyte
    #   Sports Drink）不會進 OOV，而是 keyword 抽空 → 全店 60 商品概覽。
    #   條件從嚴：只在 query_inventory、**沒有 category 也沒抽到扎實商品名**時補，
    #   避免把 'sports drink stock'（真商品）誤轉成整個 sports 類。
    if (func_name == "query_inventory" and not func_args.get("category")
            and _is_mostly_english(user_text)):
        _cat_en2 = _category_from_en(user_text)
        if _cat_en2:
            _kw_en2 = str(func_args.get("keyword", "")).strip()
            _solid_kw = False
            if _kw_en2:
                try:
                    import warehouse as _W_ce
                    _m_ce = _W_ce.match_items(_kw_en2)
                    _solid_kw = bool(_m_ce and _m_ce[0].get("score", 0) >= 8)
                except Exception:
                    _solid_kw = False
            # ⚠️ 轉類別前先問模糊層：'food conntainers on hand' 的 food 命中
            #   類別詞，但 conntainers→Glass Food **Containers** 是明確的
            #   錯字商品查詢，轉成整個 food_beverage 類＝答非所問。
            #   模糊層對整句有明確答案時，那才是訪客要的。
            _fz_cat = ""
            try:
                # 餵**剝過虛詞的 core**，不能餵整句（見 _en_query_core 註解）
                _fz_cat = _en_fuzzy_keyword(_en_query_core(user_text))
            except Exception:
                _fz_cat = ""
            if _fz_cat:
                func_args = {**func_args, "keyword": _fz_cat}
                func_args.pop("category", None)
                log.info(f"[EN cat-fill] 讓路模糊層：{user_text!r} → kw={_fz_cat!r}"
                         f"（不轉 {_cat_en2} 類）")
            elif not _solid_kw and not _kw_en2:
                # r14+2（#61）：kw 全空時先拿句子 core 試商品比對——
                #   'camping rent stock'（rent=tent 錯字）曾直接轉 Sports 類
                #   概覽，其實 core 對 Camping Tent 4-person 分數已達門檻。
                #   比到具體商品就用商品，比不到才轉類別。
                _cf_kw2 = ""
                try:
                    _cf_core2 = _en_query_core(user_text)
                    # ⚠️ 只在「類別詞之外還有別的實詞」時才試商品——
                    #   純類別句（'sports category stock'）的 sports 會
                    #   誤配 Sports Compression Arm Sleeve
                    _cf_rest = [w for w in _re.split(r"[\s\-/]+", _cf_core2.lower())
                                if len(w) >= 3 and _category_from_en(w) is None]
                    if _cf_rest:
                        import warehouse as _W_cf2
                        _cf_m2 = _W_cf2.match_items(_cf_core2)
                        if _cf_m2 and _cf_m2[0].get("score", 0) >= 4:
                            _cf_kw2 = _cf_m2[0]["name"]
                except Exception:
                    _cf_kw2 = ""
                if _cf_kw2:
                    func_args = {**func_args, "keyword": _cf_kw2}
                    func_args.pop("category", None)
                    log.info(f"[EN cat-fill] core 比到具體商品 → kw={_cf_kw2!r}"
                             f"（不轉 {_cat_en2} 類）")
                else:
                    func_args = {k: v for k, v in func_args.items() if k != "keyword"}
                    func_args["category"] = _cat_en2
                    log.info(f"[EN cat-fill] {user_text!r} → query_inventory"
                             f"{{category:{_cat_en2}}}（原 kw={_kw_en2!r} 不扎實）")
            elif not _solid_kw:
                func_args = {k: v for k, v in func_args.items() if k != "keyword"}
                func_args["category"] = _cat_en2
                log.info(f"[EN cat-fill] {user_text!r} → query_inventory"
                         f"{{category:{_cat_en2}}}（原 kw={_kw_en2!r} 不扎實）")

    # related 直達且 kw 是雜訊（「買精釀啤酒的都會多帶什麼」LLM kw='都會'
    # → related_empty，conv100-r14）→ 從原句重抽
    if func_name == "query_related_items":
        import warehouse as _W_rel
        _rel_kw = func_args.get("keyword", "")
        if not _rel_kw or not _W_rel.match_items(_rel_kw):
            _rel_kw2 = _extract_sku_keyword(user_text)
            if _rel_kw2 and _W_rel.match_items(_rel_kw2) and _kw_grounded(_rel_kw2, user_text):
                func_args = {**func_args, "keyword": _rel_kw2}
                log.info(f"[校正 C6b] related kw 雜訊 → 重抽 {_rel_kw2!r}")

    # C17b：set_alert 參數清理 — 只保留 condition / target，清掉 keyword 等非法參數
    if func_name == "set_alert":
        # 用頂層 _re，不可 import re as _re（見 C11b 註解：會遮蔽整個函式的 _re）
        cond = str(func_args.get("condition", func_args.get("keyword", ""))).strip()
        tgt  = str(func_args.get("target", func_args.get("item", ""))).strip()
        # 若 condition 不是合法 enum，從 user_text 推斷
        _valid_conds = {"below_safety", "below_threshold", "expiring_soon", "overstock"}
        if cond not in _valid_conds:
            # 整句帶數字「低於N/少於N/小於N」→ below_threshold
            # EN build：門檻正則原只認中文 → 'drop below 30' 抓不到數字＝
            #   訪客指定的門檻被吞掉，一律變成 below_safety
            _thr = (_re.search(r'(?:低於|少於|小於|不足)\s*(\d+)', user_text)
                    or _re.search(r'\b(?:below|under|less than|fewer than|'
                                  r'drops? (?:to|below)|falls? (?:to|below)|'
                                  r'goes? (?:to|below)|hits?)\s*(\d+)',
                                  user_text, _re.I))
            if _thr:
                cond = "below_threshold"
                func_args["threshold"] = int(_thr.group(1))
            else:
                cond = "below_safety"
        # EN build：LLM 常把商品名放在 **keyword** 而非 target（實測
        #   'alert me when earphones drop below 30' → keyword:'earphones',
        #   target:'below safety zone'＝模型亂填的條件描述）。原本只看
        #   target/item → 商品名被丟掉、target 變成那句廢話 → 全店警示。
        #   → target 不像商品名時，改用 keyword。
        _kw_alert = str(func_args.get("keyword", "")).strip()
        if _kw_alert and _kw_alert not in _valid_conds:
            try:
                import warehouse as _W_al
                _tgt_ok = bool(tgt and _W_al.match_items(tgt)
                               and _W_al.match_items(tgt)[0].get("score", 0) >= 4)
                _kw_ok = bool(_W_al.match_items(_kw_alert)
                              and _W_al.match_items(_kw_alert)[0].get("score", 0) >= 4)
                if _kw_ok and not _tgt_ok:
                    log.info(f"[校正 C17b] target {tgt!r} 非商品 → 改用 keyword {_kw_alert!r}")
                    tgt = _kw_alert
            except Exception:
                pass
        # 若 target 是整句話，改用 _extract_sku_keyword
        # EN build：`len > 6` 是**中文字元**門檻（坑 2）——英文正常商品名
        #   'earphones'(9) / 'yoga mat'(8) 都 >6 會被無謂重抽。英文改用詞數。
        _tgt_too_long = (len(tgt.split()) > 3 if _is_mostly_english(tgt)
                         else len(tgt) > 6)
        if tgt and _tgt_too_long:
            tgt = _extract_sku_keyword(tgt) or tgt
        # 若 target 為空，嘗試從 user_text 抽 SKU
        if not tgt:
            tgt = _extract_sku_keyword(user_text) or ""
        # ⚠️ target 接地驗證（邊界測試抓到的**危險破口**）：裸句 'alert me'
        #   LLM 吐 target:'no item'（它在說「沒有商品」），下游卻拿去 match →
        #   'no item' 低分(2)比到 Ceramic **No**n-stick Pan → 確認卡上寫著
        #   訪客從沒提過的商品，而這是**寫入操作**（按確認就真的建規則）。
        #   → 佔位詞 / 低分噪音一律清空，退回「全店警示」比亂指一個商品安全。
        #   （符合 user 定調的「不確定不猜」）
        if tgt:
            _tgt_placeholder = _re.fullmatch(
                r"\s*(?:no item|none|n/?a|null|unknown|any|all|all items|"
                r"item|items|-{1,2}|\?+)\s*", tgt, _re.I)
            _tgt_low_score = False
            if not _tgt_placeholder:
                try:
                    import warehouse as _W_tg
                    _m_tg = _W_tg.match_items(tgt)
                    _tgt_low_score = not (_m_tg and _m_tg[0].get("score", 0) >= 4)
                except Exception:
                    _tgt_low_score = False
            if _tgt_placeholder or _tgt_low_score:
                log.info(f"[校正 C17b] target {tgt!r} 未接地（佔位詞/低分）→ 清空改全店警示")
                tgt = ""
        func_args = {"condition": cond, "target": tgt,
                     **({} if "threshold" not in func_args else {"threshold": func_args["threshold"]})}
        log.info(f"[校正 C17b] set_alert args → {func_args}")

    # C17c：generate_po / commit_po 參數清理
    if func_name in ("generate_po", "commit_po"):
        legal = {"source", "items", "confirm", "po_id"}
        func_args = {k: v for k, v in func_args.items() if k in legal}
        log.info(f"[校正 C17c] {func_name} args → {func_args}")

    return func_name, func_args, False


# ─── HEALTH ───────────────────────────────────────────────
HEALTH = {
    "stage":   "starting",
    "message": "Server 啟動中...",
    "error":   None,
    "clf":     "unknown",   # intent_clf 主路由自檢狀態（ok / DEAD: 原因 / unknown）
}


def _set_health(stage: str, message: str, error: str | None = None):
    HEALTH["stage"] = stage
    HEALTH["message"] = message
    HEALTH["error"] = error
    log.info(f"[health] {stage}: {message}" + (f" | error: {error}" if error else ""))


# ─── Global state ─────────────────────────────────────────
LLM: object        = None
MODEL_FILE: str    = ""
SYSTEM_PROMPT: str = ""
LLM = None          # set by _background_init (via load_model)
llm_lock = asyncio.Lock()

# ── 效能量測：最近一次 LLM 推論的 token 速度（給前端效能徽章）──
_last_perf = {"tok": 0, "ms": 0, "tps": 0.0}


def _record_perf(r, t0):
    """LLM 呼叫後記錄 completion tokens / wall time / tok每秒。"""
    import time as _t
    ms = (_t.perf_counter() - t0) * 1000.0
    ct = (r.get("usage") or {}).get("completion_tokens", 0) if isinstance(r, dict) else 0
    _last_perf["tok"] = ct
    _last_perf["ms"] = round(ms)
    _last_perf["tps"] = round(ct / (ms / 1000.0), 1) if ms > 0 and ct else 0.0
display_sockets: set[WebSocket] = set()
all_sockets:     set[WebSocket] = set()
_visitor_closed = False
_item_create_state: dict = {}          # HTTP 端用（單請求無並發）
# WS 端改 per-vid（2026-07-08 抓到全域污染重大 bug：一個訪客觸發新增商品流程
# 後，所有後續訪客/句子被吸進流程當步驟，展場多人玩必爆；同 pending 的 per-vid 修法）
_item_create_state_ws: dict = {}       # {vid: {active, step, name, ...}}
_item_delete_state: dict = {}  # 刪除模式的 session state
# r74：排程/警示「刪掉它」多筆 clarify 後的一次性選擇模式（{vid: {kind, ids}}）
_del_select_by_vid: dict = {}
_vid_counter: int = 0          # WS 連線唯一序號（遞增，絕不碰撞/重用）
# Context carry-over：記住上一輪的 sku/warehouse/func。
# ⚠️ 一定要按訪客(vid)隔離——曾經是單一全域 dict，展場多裝置時 A 訪客問的
# 商品會污染 B 訪客的「那個呢」追問（2026-07-03 第9輪測試抓到：不同 WS 連線
# 之間 context 互相滲透）。
_ctx_by_vid: dict = {}         # vid → {"last_sku":…, "last_wh":…, "last_func":…}
_CTX_MAX_VISITORS = 500        # 防止長期展示無上限成長


def _ctx_for(vid) -> dict:
    if vid not in _ctx_by_vid and len(_ctx_by_vid) >= _CTX_MAX_VISITORS:
        _ctx_by_vid.clear()    # 展場簡單粗暴：滿了整個重置（context 丟了頂多多問一句）
    return _ctx_by_vid.setdefault(vid, {})


# ─── Context carry-over ────────────────────────────────────
_CTX_FOLLOWUP_WORDS = ("那", "它", "這個", "該", "同樣", "這支", "這件", "剛才", "上次")
# ── EN build（劇情批 r1）：追問詞表原本全中文 → 英文 carry-over **整條失效**。
#   實測：'bluetooth earphones stock' 之後打 'north' / 'and central' /
#   'whats in south' / 'how many in each now' 全部回**全店 60 商品概覽**，
#   context 完全沒接上（其中 'north' 的 view=inventory 還被守衛判成通過＝
#   畫面級破口，正是「只看 view 會漏」的實例）。
#   同坑 7：中文鍵的對照表對英文一處也命中不了。
_CTX_FOLLOWUP_RE_EN = _re.compile(
    r"\b(?:it|its|it's|that|this|those|these|them|the same|same one|"
    r"the one|that one|this one|the item|"
    # 裸倉別追問：'north' / 'and central' / 'whats in south' / 'in north'
    r"and\s+(?:north|central|south)|^(?:north|central|south)$|"
    r"(?:in|at|for)\s+(?:north|central|south)|"
    # r11（真人語音批乾跑抓到）：'what about north' / 'how about central'
    #   ——英文最自然的追問講法，卻是漏的。`about` 沒在上面的介系詞清單裡，
    #   整句對不到任何追問樣式 → carry-over 沒觸發 → 回全店 60 商品概覽。
    #   ⚠️ 這兩個詞在**守門員**的放行判斷裡早就有（~10338 行），
    #     所以句子進得了門、卻在 carry-over 這層落空——兩處詞表不同步。
    r"(?:what|how)\s+about\b|"
    # 承接副詞：'then' / 'also' / 'too' / 'again' / 'now' / 'each'
    # r18 #37：'one more time'/'once more'——same again 家族的同義講法
    r"then|also|too|again|now|each|one more time|once more|"
    # 序數指代：'the first one' / 'the second'
    # ⚠️ 'the \w+ ones?' 要排在 'the (first|…)' **前面**（交替分支左優先不回溯，
    #   否則 'the most urgent one' 不會命中——r2 S5 實測）
    # ⚠️ 最高級（the worst）必須排在 'the \w+ ones?' **之前**——交替分支
    #   左優先不回溯：'the worst' 先被 'the \w+\s+ones?' 嘗試（那需要
    #   「詞+空白」），worst 後沒空白 → 失敗後整個 the- 起點就放棄。
    #   （r2 S5 同款坑，r4 加在後面又踩一次。）
    r"the\s+(?:worst|best|biggest|largest|smallest|highest|lowest|cheapest|"
    r"priciest|newest|oldest|most|least)\b|"
    r"which\s+(?:item|one)\b|"
    r"the\s+(?:\w+\s+){1,3}ones?|"
    r"the\s+(?:first|second|third|fourth|fifth|last|other|next)|"
    r"earlier|just now|last time|previous)\b", _re.I)
_CTX_FUNC_HINT = {
    "進出": "query_movement", "異動": "query_movement", "紀錄": "query_movement",
    "搭配": "query_related_items", "推薦": "query_related_items",
    "到期": "list_expiring_items", "保存": "list_expiring_items",
}
# EN build：功能切換詞的英文版（'its movements?' / 'what goes with it' /
#   'does it expire soon'）
_CTX_FUNC_HINT_RE_EN = (
    (r"\b(?:movements?|in\s*/?\s*out|inbound|outbound|shipments?|"
     r"received|shipped)\b", "query_movement"),
    (r"\b(?:goes? with|pairs? with|related|bought together|also (?:buy|get))\b",
     "query_related_items"),
    (r"\b(?:expir\w*|shelf life|use[- ]?by|best before)\b",
     "list_expiring_items"),
)

def _update_ctx(vid, func_name: str, func_args: dict):
    """每輪成功執行後更新該訪客的 context。"""
    _ctx = _ctx_for(vid)
    kw = func_args.get("keyword") or func_args.get("target") or func_args.get("script_name")
    wh = func_args.get("warehouse")
    # r55 收官批：寫 context 前必須驗證是真商品（跟 _ctx_absorb 同標準）。
    # 「連帶第一名的庫存」查無後 keyword=「第一名 庫存」曾被存進 last_sku，
    # 下一句「快過期的有哪些」被污染成查不存在商品 → 回「✅ 沒有快到期」假全綠。
    if kw:
        import warehouse as _W_uc
        _m_uc = _W_uc.match_items(str(kw).strip())
        if not (_m_uc and _m_uc[0].get("score", 0) >= 3):
            kw = None
        # ⚠️ EN build（劇情批 r1）：存進 context 的必須是**命中的商品全名**，
        #   不能是 LLM/clf 給的原始 keyword 片段。實測 'bluetooth earphones
        #   stock' 的 kw 是 'Wireless'（片段）→ 下一句追問 'north' 被注入
        #   'Wireless' → `"Wireless" matches 2 items` 歧義反問，carry-over
        #   等於白接。用 match_items 已算好的結果正規化即可。
        elif _m_uc and _m_uc[0].get("score", 0) >= 3:
            _full_uc = _m_uc[0]["item"]["name"]
            if _full_uc != kw:
                log.info(f"[ctx] last_sku 正規化 {kw!r} → {_full_uc!r}")
            kw = _full_uc
    if kw:
        _ctx["last_sku"] = kw
    if wh and wh not in ("all", None):
        _ctx["last_wh"] = wh
    _ctx["last_func"] = func_name
    # r15 #56 串品案加的常駐觀測點：追 carry-over 污染必看這行
    log.info(f"[ctx-upd] vid={vid} func={func_name} "
             f"last_sku={_ctx.get('last_sku')!r} last_wh={_ctx.get('last_wh')!r}")

def _resolve_followup(vid, user_text: str, func_name: str, func_args: dict):
    """
    若 user_text 是追問句（含代詞/倉庫切換）且 func_args 沒有 keyword，
    嘗試從該訪客的 _ctx 補上 last_sku / last_wh。
    回傳 (new_func_name, new_func_args) 或原值。
    """
    _ctx = _ctx_for(vid)
    # ── EN build（劇情批 r1 S6）：**設定復原**（'put it back' / 'revert'）──
    #   上一輪剛改過設定，訪客要改回原值。舊值由 commit_config 存進
    #   `last_cfg_undo`（沒有舊值就不接，讓它走原路徑誠實反問）。
    if (_is_mostly_english(user_text)
            and _ctx.get("last_cfg_undo")
            and len(user_text.split()) <= 5
            and _re.search(r"\b(?:back|revert|undo|restore|原|previous|before)\b",
                           user_text, _re.I)):
        _u = _ctx["last_cfg_undo"]
        _undo_args = {"action": "set", "key": _u.get("canon") or "safety stock",
                      "value": str(_u.get("value"))}
        if _u.get("item"):
            _undo_args["item"] = _u["item"]
        if _u.get("warehouse"):
            _undo_args["warehouse"] = _u["warehouse"]
        log.info(f"[ctx] 設定復原 → manage_config{{set {_undo_args.get('item')}="
                 f"{_u.get('value')}}}: {user_text!r}")
        return "manage_config", _undo_args
    # ── EN build（劇情批 r1）：**設定生效確認**（'did it take effect' /
    #   'put it back'）——上一輪剛改過設定，訪客問的是**設定值**不是庫存量。
    #   沒這條會回 inventory_single（Yoga Mat 335 units），答非所問。
    if (_is_mostly_english(user_text)
            and _ctx.get("last_func") == "manage_config"
            and len(user_text.split()) <= 5
            # ⚠️ 'back' / 'revert' 不列入——那是**復原意圖**（要改回原值，
            #   走 config_confirm），不是查詢確認。混在一起會讓
            #   'put it back' 只回設定值、什麼都沒改。
            and _re.search(r"\b(?:effect|effective|applied|apply|done|"
                           r"changed|updated|saved|work|worked)\b",
                           user_text, _re.I)):
        _cfg_item = _ctx.get("last_sku") or ""
        log.info(f"[ctx] 設定生效確認 → manage_config{{read}} item={_cfg_item!r}: "
                 f"{user_text!r}")
        return "manage_config", ({"action": "read", "key": "safety stock",
                                  "item": _cfg_item} if _cfg_item
                                 else {"action": "read", "key": "safety stock"})
    # ── EN build（劇情批 r1）：**全域功能追問**（沒有商品，是追問上一輪的
    #   那份結果）。'which week shipped more…' → period_compare 之後，
    #   'by how many units' 是在問同一份比較的細節，但 carry-over 只處理
    #   「商品追問」→ 這句掉到全店概覽（答非所問）。
    #   → 上一輪是全域功能且本句是純追問（無商品、無新意圖）→ 重跑該功能。
    if (_is_mostly_english(user_text)
            and _ctx.get("last_func") in ("compare_periods", "list_hot_items",
                                          "list_low_stock", "compare_warehouses",
                                          "list_expiring_items",
                                          # r4 S3：RCA 清單也要能追問
                                          #   （'any stock discrepancies' →
                                          #   'which item is the worst'）
                                          # ⚠️ 複驗回歸：RCA 承接**只限最高級
                                          #   追問**（which/worst/最嚴重），
                                          #   否則 RCA 之後任何追問都被吸過去
                                          #   （'ok back to the earphones' /
                                          #   'did the yoga mat one go through'
                                          #   全變成 agent_rca）。
                                          "search_log")
            and (_ctx.get("last_func") != "search_log"
                 or _re.search(r"\b(?:which|worst|biggest|largest|most|"
                               r"top|first)\b", user_text, _re.I))
            and not func_args.get("keyword")
            # 純追問＝**極短且無動作詞**。'by how many units' / 'and then'
            #   算；'restock the first one how many should i order' 不算
            #   （有 restock/order 動作詞，訪客要的是別的功能）。
            and len(user_text.split()) <= 5
            and not _GK_ACTION_RE.search(user_text)
            and not _re.search(r"\b(?:order|buy|purchase|po|create|make|"
                               r"generate|run|schedule|alert|notify)\b",
                               user_text, _re.I)
            and (_CTX_FOLLOWUP_RE_EN.search(user_text.strip())
                 or _re.search(r"^\s*(?:by |and |so |then )?how (?:many|much)\b",
                               user_text, _re.I))
            # ⚠️ r4 S1：**本句自帶明確功能詞就不搶**。'best sellers this month'
            #   因為含 'this month'（我列的追問詞）被搶去沿用上一輪的
            #   low_stock，但 clf 已正確判 list_hot_items(conf=1.00)。
            #   下面 _own_intent 只認 movement/related/expiring 三類，
            #   漏了熱銷/缺貨/比較 → 在進入前先擋掉。
            and not _re.search(
                r"\b(?:best sellers?|top sellers?|hot items?|bestsell\w*|"
                r"selling|slow (?:movers?|moving)|dead stock|"
                r"running low|low stock|out of stock|restock|reorder|"
                r"expir\w*|shelf life|compare|comparison|versus|vs|"
                # r4 複驗回歸：**明確功能詞**也不可被承接——
                #   'does it still have stock' 是問庫存，卻被承接成熱銷榜；
                #   'move 20 from the fullest…' 是調貨，卻被承接成 RCA。
                #   carry-over 只該接**沒有自己意圖**的純追問。
                r"stock|stocks|inventory|count|left|remaining|"
                r"move|transfer|ship|receive|received|add|remove|"
                r"discrepanc\w*|anomal\w*|audit|reconcil\w*)\b",
                user_text, _re.I)):
        # 本句自己有明確功能意圖時不搶（'whats expiring' 該走自己的路）
        _own_intent = any(_re.search(_p, user_text, _re.I)
                          for _p, _v in _CTX_FUNC_HINT_RE_EN)
        if not _own_intent and func_name != _ctx["last_func"]:
            # 裸倉別追問（'north'）要把**倉別帶進去**，否則回的是全站榜
            #   ＝沒回答訪客問的「北倉的」（r3 S9 實測）
            _gf_args = {}
            _gf_wh = _re.search(r"\b(north|central|south)\b", user_text, _re.I)
            if _gf_wh:
                # ⚠️ 不是每個功能都吃 warehouse——list_hot_items 是**全站榜**，
                #   硬承接會回全站資料＝沒回答「北倉的」（r3 S9 實測）。
                #   該功能不支援倉別時，改回該倉的庫存概況（訪客問裸倉名，
                #   最可能就是想看那個倉的狀況）。
                if _ctx["last_func"] in ("list_low_stock", "list_expiring_items",
                                         "query_movement"):
                    _gf_args["warehouse"] = _gf_wh.group(1).lower()
                else:
                    log.info(f"[ctx] {_ctx['last_func']!r} 不吃倉別 → 裸倉名回該倉庫存: "
                             f"{user_text!r}")
                    return "query_inventory", {"warehouse": _gf_wh.group(1).lower()}
            log.info(f"[ctx] 全域功能追問 {func_name!r} → 沿用上一輪 "
                     f"{_ctx['last_func']!r}{_gf_args or ''}: {user_text!r}")
            return _ctx["last_func"], _gf_args
    if not _ctx.get("last_sku"):
        return func_name, func_args
    # r76：排程/腳本/警示/採購管理 func 絕不被追問機制改寫——「排程 每週一早上
    # 八點匯出進出記錄」的「進出記錄」曾把 set_schedule 踩成 query_movement 還
    # 注入 ctx 商品（排程意圖整句銷毀）。
    # ⚠️ create_movement/create_transfer/manage_config 不可入列——改量句
    # 「那出200件就好」本來就靠 ctx 注入補商品（r76x 打破 r60 舊卡作廢守衛）
    if func_name in ("set_schedule", "run_script", "set_alert", "generate_po",
                     "delete_schedule", "delete_alert", "list_schedules",
                     "list_alerts"):
        return func_name, func_args
    is_followup = any(w in user_text for w in _CTX_FOLLOWUP_WORDS)
    # EN build：英文追問詞（見 _CTX_FOLLOWUP_RE_EN 註解）
    if not is_followup and _is_mostly_english(user_text):
        is_followup = bool(_CTX_FOLLOWUP_RE_EN.search(user_text.strip()))
    raw_kw = (func_args.get("keyword") or func_args.get("target") or "").strip()
    # keyword 本身含代詞或功能詞視為無效（LLM 把「它 進出紀錄」「進出紀錄」當 keyword）
    _bad_kw_words = list(_CTX_FOLLOWUP_WORDS) + list(_CTX_FUNC_HINT.keys())
    kw_is_proxy = any(w in raw_kw for w in _bad_kw_words)
    has_kw = bool(raw_kw) and not kw_is_proxy
    # r77 危險級：kw 帶代詞雜訊但也帶真實「未知」名詞（「那先補中倉的 保溫瓶」
    # 的 kw='那先...保溫瓶' 因含「那」被當純代詞）→ 剝掉代詞後剩 2 字以上詞幹
    # 且比對不到商品 = 查無商品，絕不可拿 ctx 舊商品頂替——曾把保溫瓶開成
    # 耳機進貨卡（錯誤寫入落地）
    if raw_kw and kw_is_proxy:
        _rk77 = raw_kw
        for _w77 in _bad_kw_words:
            _rk77 = _rk77.replace(_w77, "")
        # r77v：語氣殘渣（「不夠 那好」）不是未知商品——濾掉語氣/方位字後
        # 還剩 2 字以上實詞才算「句中有真實未知名詞」（r60 改量流程曾被誤擋）
        _rk77 = "".join(ch for ch in _rk77 if ch not in
                        "不夠好就那這先別喔啦嗯哦哈欸咦件個些了呢吧的 "
                        "進出調補中北南倉區。，,?？!！")
        if len(_rk77) >= 2:
            import warehouse as _W_rf77
            _rm77 = _W_rf77.match_items(_rk77)
            if not (_rm77 and _rm77[0].get("score", 0) >= 3):
                log.info(f"[ctx] kw 殘詞「{_rk77}」查無商品 → 不注入 ctx（誠實查無）")
                return func_name, func_args
    # 偵測功能切換（「它的進出紀錄呢？」「進出紀錄呢」「這個快到期嗎？」）
    # 「紀錄檔」是問檔案列表（list_files），不是問進出紀錄，排除掉避免誤判成功能切換。
    _ctx_func_hint_text = user_text.replace("紀錄檔", "").replace("記錄檔", "")
    new_func = next((v for k, v in _CTX_FUNC_HINT.items() if k in _ctx_func_hint_text), None)
    # EN build：英文功能切換詞
    if new_func is None and _is_mostly_english(user_text):
        for _p_fh, _v_fh in _CTX_FUNC_HINT_RE_EN:
            if _re.search(_p_fh, _ctx_func_hint_text, _re.I):
                new_func = _v_fh
                break

    # 有功能切換詞 or 追問代詞，且沒有有效 keyword → 介入
    if not (is_followup or new_func) or has_kw:
        return func_name, func_args
    # r55 收官批：全域查詢句（「快過期的有哪些」）絕不注入 last_sku——
    # 拿舊商品過濾全域問題會漏報（到期警示曾被濾成單一商品 → 假「沒有快到期」）。
    if any(w in user_text for w in _CTX_GLOBAL) or (
            _is_mostly_english(user_text)
            and _CTX_GLOBAL_RE_EN.search(user_text)):
        return func_name, func_args

    new_args = dict(func_args)
    new_args["keyword"] = _ctx["last_sku"]
    log.info(f"[ctx] 補 keyword={_ctx['last_sku']!r} 從上一輪 context")
    # ⚠️ EN build（劇情批 r1）：注入 keyword 時要清掉 LLM 幻覺的 category。
    #   'and central' 的 LLM 輸出是 query_inventory{category:electronics,
    #   warehouse:central}——追問句本身沒有任何類別詞，category 是幻覺。
    #   keyword + category 並存 → 商品被類別過濾 → 掉進歧義反問
    #   （實測回「What do you want to know about "Smart Fitness Band"?」
    #   ＝跳到完全不相干的商品）。追問句指的是**上一輪那個商品**，
    #   類別條件必然是多餘的。
    if new_args.get("category") and _is_mostly_english(user_text) \
            and _category_from_en(user_text) is None:
        log.info(f"[ctx] 追問句清掉幻覺 category={new_args['category']!r}")
        new_args.pop("category", None)
    # EN build：裸倉別追問（'north' / 'and central' / 'whats in south'）
    #   除了補商品，還要把**倉別**帶進去，否則回的是三倉合計＝沒回答問題。
    if _is_mostly_english(user_text) and not new_args.get("warehouse"):
        _wh_fu = _re.search(r"\b(north|central|south)\b", user_text, _re.I)
        if _wh_fu:
            new_args["warehouse"] = _wh_fu.group(1).lower()
            log.info(f"[ctx] 補 warehouse={new_args['warehouse']!r}（英文倉別追問）")

    if new_func:
        log.info(f"[ctx] 切換 func {func_name!r} → {new_func!r}")
        func_name = new_func

    return func_name, new_args


# ─── r32 多輪：context 統一切面 + pending 卡片記憶 ─────────────
# 【r32 根因】_update_ctx / _resolve_followup 只掛在 LLM 路徑，但 r24-r31 把大量
# 查詢改成確定性 dispatch 直答（hard-return continue，5ms 那批）→ 直答完全不寫
# context，carry-over 整個被架空：「無線滑鼠還剩幾個」→「那個進出紀錄呢」會回
# 全部商品的進出統計。鐵律「hard-return 出口要自帶接地」的新變體：出口也要**寫回**
# context。出口有數十處，逐一補必漏 → 改在 send(done) 這個唯一咽喉統一攔截。
_pending_by_vid: dict = {}     # vid → {"view": …}：server 端記住畫面上那張確認卡

# 需要訪客按按鈕才寫入的確認卡（對照 templates/index.html 的 doConfirm）
# r54：view → confirm action（口語確認代按用；對照前端 doConfirm data-action）
_VIEW2ACTION_WS = {
    "movement_confirm":  "create_movement",
    "transfer_confirm":  "create_transfer",
    "config_confirm":    "config_set",
    "po_confirm":        "generate_po",
    "alert_confirm":     "set_alert",
    "schedule_confirm":  "set_schedule",
    "script_confirm":    "run_script",
    "item_confirm":      "item_create",
    # r74：排程/警示刪除卡口語確認（schedule_list 後「刪掉它」→ 卡片→「確認刪除」）
    "schedule_delete_confirm": "delete_schedule",
    "alert_delete_confirm":    "delete_alert",
    # r75：商品刪除卡的「確認刪除」曾重新進刪除閘再列一次清單（老缺口）
    "item_delete_confirm":     "item_delete",
}

_PENDING_VIEWS = {"movement_confirm", "transfer_confirm", "config_confirm",
                  "item_confirm", "item_delete_confirm", "po_confirm",
                  "alert_confirm", "schedule_confirm", "script_confirm",
                  "schedule_delete_confirm", "alert_delete_confirm"}
_VIEW2FUNC = {"inventory": "query_inventory", "inventory_single": "query_inventory",
              "movement": "query_movement", "movement_confirm": "query_inventory",
              "low_stock": "list_low_stock", "expiring": "list_expiring_items",
              "hot_items": "list_hot_items", "related": "query_related_items",
              "config_read": "manage_config", "agent_rca": "search_log"}


_clarify_opts_by_vid: dict = {}   # r51：vid → 上一輪 clarify 的選單（序數選擇用）
_export_done_by_vid: dict = {}    # 2026-08-04：vid → (kind, ts)，產出完成後的追問接續用
_write_flow_by_vid: dict = {}     # r56：vid → 寫入續流（進出貨/調貨 clarify 問倉別/數量後，
                                  #        短答「北倉」「30件」要接回寫入而不是變庫存查詢）


def _ctx_absorb(vid, result: dict):
    """每個 done 出口的統一切面：把答案裡的商品/倉別寫回 context、記住確認卡。"""
    if not isinstance(result, dict):
        return
    view = result.get("view", "")
    data = result.get("data") if isinstance(result.get("data"), dict) else {}

    # r74：記住上一畫面——schedule_list 後說「刪掉它」是要刪排程不是刪商品
    # （rejected/guide 不覆蓋，亂聊一句不該洗掉畫面記憶）
    # r76：clarify/error 也不覆蓋——「排程列表→改成每週五(clarify)→刪掉它」
    # 曾因 last_view 被 clarify 蓋掉而誤入商品刪除
    if view and view not in ("rejected", "guide", "clarify", "error"):
        _ctx_for(vid)["last_view"] = view
    # 設定改動的**舊值**（劇情批 r1 S6：'put it back' 復原用）
    if view == "config_done" and (data.get("undo") or {}).get("value") is not None:
        _ctx_for(vid)["last_cfg_undo"] = data["undo"]
    # r74：排程/警示清單記憶——「刪掉它」只有一筆時可以直指
    if view == "schedule_list":
        _ctx_for(vid)["last_sched_jobs"] = [
            j.get("id") for j in (data.get("jobs") or [])
            if isinstance(j, dict) and j.get("id")]
    # r78：對帳異常最大筆商品接地——「它短收幾件」要能指到蚊香液
    if view == "agent_rca":
        _disc78 = data.get("discrepancies") or []
        if _disc78 and isinstance(_disc78[0], dict) and _disc78[0].get("name"):
            # ⚠️ EN build：`.split()[0]` 是**中文導向**——中文商品名沒空格，
            #   取第一段等於取全名；英文卻會截成 'Wireless'（Wireless
            #   Bluetooth Earphones）→ 下一句追問拿它去比對變成歧義反問。
            _n78 = str(_disc78[0]["name"])
            _ctx_for(vid)["last_sku"] = (
                _n78 if _is_mostly_english(_n78) else _n78.split()[0])
    # r77：連帶清單記憶——「第一個連帶的庫存」要能直指
    if view == "related":
        _ctx_for(vid)["last_related"] = [
            r.get("name") for r in (data.get("related") or [])
            if isinstance(r, dict) and r.get("name")]
    if view == "alert_list":
        _ctx_for(vid)["last_alert_rules"] = [
            r.get("id") for r in (data.get("rules") or [])
            if isinstance(r, dict) and r.get("id")]
    # r75：剛建立完排程/警示，「剛加的那條刪掉」也要能直指
    if view == "alert_done" and data.get("rule_id"):
        _ctx_for(vid)["last_alert_rules"] = [data["rule_id"]]
    if view == "schedule_done" and (data.get("job") or {}).get("id"):
        _ctx_for(vid)["last_sched_jobs"] = [data["job"]["id"]]

    # 確認卡記憶：卡片一出現就記（r54 起含完整 data——口語確認代按要用它組
    # confirm payload）。rejected/guide 不清卡（亂聊一句卡片還在畫面上，
    # 「你晚餐吃什麼」後的「好啦確認」曾因此失效）。
    if view in _PENDING_VIEWS:
        _pending_by_vid[vid] = {"view": view, "data": data}
    elif view not in ("clarify", "rejected", "guide"):
        _pending_by_vid.pop(vid, None)
    # r77 危險級：寫入句查無商品的 clarify → 作廢舊寫入卡。「中倉進15個保溫瓶」
    # 查無後訪客說「確認」，曾執行前一張耳機 +20 舊卡＝錯誤寫入落地
    if (view == "clarify" and "找不到商品" in (result.get("summary") or "")
            and _re.search(r"[進出調補]\s*\d", _ctx_for(vid).get("_cur_text") or "")):
        _pending_by_vid.pop(vid, None)

    # r51：clarify 選單記憶——「咖啡對應到5個商品」後訪客說「第一個」要能選
    # （語音輸入時代點不了按鈕，序數是最自然的選法）。非 clarify 回答即清。
    # 產出完成記憶（2026-08-04）：script_done 後的「and last week too /
    #   can i download it」要接得住（第七輪抓到兩類都答非所問）
    if view == "script_done":
        _sd_tail = str((data or {}).get("output_tail") or "")
        _export_done_by_vid[vid] = (
            "export" if "movements_" in _sd_tail else "report",
            __import__("time").time())
    if view == "clarify":
        _opts51 = data.get("options") or []
        # ⚠️ 有 actions 就存 actions（2026-08-04,第七輪抓到——ZH 同款修法
        #   沒鏡射過來）：options 是顯示標籤（'Last week'）,actions 才是完整
        #   指令（'export movements last week'）。序數代換標籤 → 雜訊路由
        #   （'2' 變 period_compare、'the last one' 掉進商品 clarify）。
        #   前端點按鈕本來就送 actions ⇒ 序數路與點擊路才一致。
        _acts51 = data.get("actions") or []
        if _acts51 and len(_acts51) == len(_opts51):
            _clarify_opts_by_vid[vid] = list(_acts51)
        elif _opts51:
            _clarify_opts_by_vid[vid] = list(_opts51)
        else:
            _clarify_opts_by_vid.pop(vid, None)
    else:
        _clarify_opts_by_vid.pop(vid, None)

    # r56：寫入續流記憶——進出貨/調貨 clarify 帶 flow（tools_v2 標注待補槽位）就記住，
    # 下一句短答（「北倉」「30件」）由 WS 層接回寫入。
    # r61：rejected/guide 不清 flow（亂打一句 ㄎㄎ 曾把「進30個咖啡豆→問倉」流程
    # 殺掉，接著答「北倉」變庫存查詢）——比照確認卡的存活規則；其他成功回答即清。
    if view == "clarify" and isinstance(data.get("flow"), dict):
        _write_flow_by_vid[vid] = dict(data["flow"])
        # r60 危險邊緣：新寫入意圖的 clarify 出現＝舊確認卡作廢——「出300卡在場說
        # 『那出200件就好』」曾開新 clarify 但舊卡沒清，接著「確認」執行了舊的 300
        _pending_by_vid.pop(vid, None)
    elif view not in ("rejected", "guide"):
        _write_flow_by_vid.pop(vid, None)

    # 商品/倉別接地（只認單一字串，多商品列表不寫，避免 context 指到錯的那個）
    # r33：一定要驗證是真商品才寫 —— clarify「找不到商品『進30個』」的 data 也帶
    #   keyword，曾被吸成 last_sku → 下一句「快到期嗎」展開成「進30個快到期嗎」。
    #   （鐵律：任何出口的參數都要接地，寫 context 也不例外。）
    # r34：config/alert 卡用 item / target 存商品名（不是 name）→ 設定完智慧手環後
    #   說「那個還剩幾個」曾回到更早的無線滑鼠。
    kw = (data.get("name") or data.get("keyword") or data.get("item_name")
          or data.get("item") or data.get("target"))
    # r34：商品全名帶規格尾巴（「瑜珈墊 6mm」「運動毛巾 100x30cm」），拿去組追問句
    #   會打壞下游的 keyword 抽取（「瑜珈墊 6mm搭配什麼賣」→ 反問「你想查什麼」，
    #   但「瑜珈墊搭配什麼賣」是好的）。context 只留可辨識的主幹。
    if isinstance(kw, str):
        # 🚨 EN build（劇情批 r4 S7）：這段「取第一個空白前的詞」是**中文導向**——
        #   中文商品名沒有空白（「瑜珈墊 6mm」只有規格尾巴前有），取第一段
        #   等於取全名；**英文商品名全部用空白分隔**，'Wireless Mouse' 會被
        #   截成 'Wireless' → 下一句追問 match 到 Wireless Bluetooth
        #   Earphones ＝ **context 從一開始就是錯的**。
        #   實測後果（危險）：'wireless mouse stock' → 'set its safety stock
        #   to 100' 改到了**耳機**的安全庫存。
        #   英文改成只剝**規格尾巴**（純數字/單位詞），保留完整商品名。
        if _is_mostly_english(kw):
            _kw_core = _re.sub(
                r"\s+(?:\d+(?:\.\d+)?\s*(?:mm|cm|m|kg|g|ml|l|pcs|pair|"
                r"inch|in|oz|mah|w|x\d+)?|\d+x\d+\w*)\s*$", "", kw.strip(),
                flags=_re.I).strip()
            if len(_kw_core) >= 3:
                kw = _kw_core
        else:
            _kw_core = _re.split(r"[ 　]", kw.strip())[0]
            if _kw_core and len(_kw_core) >= 2:
                kw = _kw_core
    # r34：清單類（缺貨/熱銷）的 data 是列表 → 取榜首，讓「最急的那個還剩幾個」
    #   「第一名還有多少」接得住（過去回「找不到商品『最急』」）。
    # r59：generic config_read（10 項無排序）的 rows[0] 是任意商品，不可當榜首吸——
    #   曾把耳機存進 last_sku，下一句「快到期的東西」被污染成查耳機到期（假 ✅ 家族）
    # r71：60 項總覽（view=inventory 無 keyword/category）同病——「倉租多少錢」掉
    #   概覽後，「輸的那個要促銷嗎」曾回從沒查過的耳機（rows[0]=e01）
    if not isinstance(kw, str):
        _generic_inv = (view == "inventory"
                        and not data.get("category") and not data.get("keyword"))
        for _lk in ("warnings", "rankings", "preview", "items", "rows"):
            _lv = data.get(_lk)
            if isinstance(_lv, list) and _lv and isinstance(_lv[0], dict):
                if (view == "config_read" or _generic_inv) and len(_lv) > 1:
                    break
                kw = _lv[0].get("name") or _lv[0].get("item")
                break
    if isinstance(kw, str) and kw.strip():
        import warehouse as _W_ab
        _m_ab = _W_ab.match_items(kw.strip())
        if _m_ab and _m_ab[0].get("score", 0) >= 3:
            # 存**命中的商品全名**而非原始 keyword（可能是 'Wireless' 這種
            #   片段）——同 _update_ctx 的正規化，兩處要一致，否則
            #   carry-over 拿片段去比對會變成歧義反問（劇情批 r1 實測）。
            _ctx_for(vid)["last_sku"] = _m_ab[0]["item"]["name"]
    wh = data.get("warehouse")
    if isinstance(wh, str) and wh.strip() and wh not in ("all", "全部倉"):
        _ctx_for(vid)["last_wh"] = wh.strip()
    if view in _VIEW2FUNC:
        _ctx_for(vid)["last_func"] = _VIEW2FUNC[view]
    # r55 收官批：記住排行榜期間——「本月熱銷前五」→「第三名剩多少」要沿用本月，
    # 不能默默換回本週（榜單不同名次會指到錯的商品）。
    if view == "hot_items" and data.get("period"):
        _ctx_for(vid)["last_hot_period"] = data["period"]
    # r56 fuzz：記住進出查詢的期間標籤——全店進出後「只看南倉的」要沿用同期間
    if view == "movement" and data.get("period_label"):
        _ctx_for(vid)["last_mv_plabel"] = data["period_label"]
    # r64：記住排行榜的類別範圍——「廚具類熱銷」→「第二名多少錢」要在同榜內解析
    if view == "hot_items":
        _ctx_for(vid)["last_hot_cat"] = data.get("category") or None


# 追問展開：代詞句/純功能句/純倉別句 → 在 rewrite 前補回上一輪的商品名，
# 讓下游每一層（dispatch 直答 + LLM）都自然接地，不必各自去讀 context。
_CTX_PRON = ("那個", "那支", "那件", "那款", "它", "牠", "這個", "這支", "這件",
             "該商品", "剛才", "剛剛", "上面那", "同一個", "同樣")
_CTX_BARE = ("進出紀錄", "進出", "異動", "流水", "安全庫存", "快到期", "到期",
             "保存期限", "效期", "搭配", "推薦", "還剩", "剩幾", "剩多少",
             "現在剩", "有多少", "庫存", "缺貨", "夠不夠", "多少錢", "單價",
             "哪一倉", "哪個倉", "哪倉")      # r35：追問分倉極值
# r35：裸功能詞的期間語意要跟完整講法一致——追問「進出」曾回**今天**的進出，
#   但「那個進出紀錄呢」回本月，同一個意思兩種答案。
_CTX_BARE_CANON = {"進出": "進出紀錄", "異動": "進出紀錄", "流水": "進出紀錄"}
# r35：追問的倉別句遠不只「南倉呢」——「北倉多少」「中倉幾個」「南」（單字）都是。
#   過去「北倉多少」被當成獨立查詢 → 回全店 60 項概覽；單字「南」直接被守門員拒。
# r55 收官批：「只看南倉的」（到期/警示清單後的倉別過濾追問）也要吃——加可選前綴
# r61：加「現在/目前」（「北倉現在幾個」曾回 60 項概覽）
_CTX_WH_ONLY = _re.compile(
    r"^(只看|只要|先看|看)?(那|這)?([北中南])(區)?(倉)?(的)?(現在|目前)?"
    r"(呢|咧|勒|嗎|多少|有多少|剩?多少|剩?幾[個件台雙張頂罐瓶盒條包組箱層支])?[?？。!！]*$")
# 純語助詞追問（「呢」「咧」「勒」）——訪客用最短的方式問「那另一個呢」
_CTX_PARTICLE = ("呢", "咧", "勒", "喔", "哦")
# r40：時段追問——看完「本月進出」後問「上週呢」＝同商品換時段。過去被守門員
#   當無意義輸入 rejected（時段詞不在任何追問詞表）。純時段詞+上一輪是進出查詢
#   → 展開成「商品+時段+進出紀錄」。value=標準句用的時段詞。
_CTX_PERIOD = {"上週": "上週", "上禮拜": "上週", "上星期": "上週",
               "這週": "這週", "本週": "本週", "這禮拜": "這週", "這星期": "這週",
               "本月": "本月", "這個月": "本月", "這月": "本月",
               "上個月": "上個月", "上月": "上個月",
               "今天": "今天", "昨天": "昨天", "前天": "前天"}
_CTX_WRITE = ("進", "出", "調", "補")
# 自帶完整意圖的全域查詢句 → 絕不接地（「哪些商品快缺貨了」曾被展開成
# 「無線滑鼠哪些商品快缺貨了」→ 回無線滑鼠庫存；泛用詞「庫存/缺貨」是誘因）
# r33：不可放單字「全」「都」——「安全庫存多少」的「全」曾誤中，導致追問不接地
#   → config_read 回全店泛答清單。一律用完整詞。
_CTX_GLOBAL = ("哪些", "所有", "全部", "每個", "各倉", "各個", "排行", "熱銷", "賣最",
               "警示", "清單", "列表", "比較", "前十", "前三", "前五", "總共", "整體",
               "有什麼", "缺什麼", "全店", "全倉", "都有",
               # r74：「那看庫存總值」被 ctx 注入變成單品查詢——總值類詞是全店視角
               "總值", "總價值", "庫存價值", "總市值")
# ── EN build：全域視角詞的英文版。⚠️ 必須跟 _CTX_FOLLOWUP_RE_EN 同時補，
#   否則「補了追問詞、沒補全域詞」會變成：'whats running low' 被注入上一輪的
#   商品 → 缺貨清單被濾成單一商品 → **假的「沒有缺貨」**（中文版 r55 踩過，
#   註解就在上面）。這是「修一半更危險」的典型。
_CTX_GLOBAL_RE_EN = _re.compile(
    r"\b(?:which items?|what items?|all|every|everything|each (?:one|item)|"
    r"list|lists|ranking|rankings|top \d+|top (?:three|five|ten)|"
    r"best sellers?|hot items?|slow (?:movers?|moving)|"
    r"running low|low stock|out of stock|restock|reorder|"
    r"alerts?|clearance|expiring|expire|expiry|"
    r"compare|comparison|versus|vs|overview|summary|"
    r"total value|stock value|inventory value|overall|"
    # r14+2（#42）：what sold/came in/moved 是**全店** movement 問句——
    #   'what sold over the weekend' 曾被 C2c carry-over 補成前句商品
    r"what (?:sold|was sold|came in|moved|shipped|arrived|happened)|"
    # r16 #13/#28/#29/#40/#60：裸方向詞/紀錄總稱/全店價值——排行卡吸榜首
    #   （r34 設計）後這些全店句被 followup 補品（'this afternoon inbound'
    #   曾回 Smart Fitness Band）。
    r"\binbound\b|\boutbound\b|movement history|transfer (?:log|history)|"
    r"in ?/? ?out balance|full history|worth|todays? summary|"
    r"anything|whats there|what do we have|item list)\b", _re.I)


# 「這個月」「那個星期」的代詞是時間片語，不是指商品
_CTX_TIME_WORDS = ("月", "週", "星期", "禮拜", "季", "年", "今天", "昨天", "上週",
                   "本週", "營收", "業績")


def _has_real_item(text: str) -> bool:
    """句中抽得到真商品（score≥3）→ 訪客已經講明了要查什麼，不是純追問。"""
    kw = _extract_sku_keyword(text)
    if not kw:
        return False
    import warehouse as _W_hr
    m = _W_hr.match_items(kw)
    return bool(m and m[0].get("score", 0) >= 3)


def _ctx_expand(vid, text: str) -> str:
    """把追問句補成完整句。沒有上一輪商品、或句中已有可辨識商品 → 原樣不動。"""
    ctx = _ctx_for(vid)
    last = ctx.get("last_sku")
    if len(text) > 16:                  # 長句訪客自己講清楚了，不介入
        return text
    # r56 fuzz：全店進出查詢後的期間/倉別追問——沒有商品 context 也要接
    # （「今天進了什麼貨」→「昨天呢」曾被守門員拒、「只看南倉的」曾回 60 項概覽）
    if not last:
        if ctx.get("last_func") == "query_movement":
            _pstem0 = text.strip().strip("的呢嗎吧了?？。!！，, ")
            if _pstem0 in _CTX_PERIOD:
                return f"{_CTX_PERIOD[_pstem0]}進出紀錄"
            _wh0 = _CTX_WH_ONLY.match(text)
            if _wh0:
                return f"{_wh0.group(3)}倉{ctx.get('last_mv_plabel', '今天')}進出紀錄"
        elif ctx.get("last_func") == "manage_config":
            # r59：generic config 總覽（不吸 last_sku）後「只看南倉的」＝南倉設定
            _wh0c = _CTX_WH_ONLY.match(text)
            if _wh0c:
                return f"{_wh0c.group(3)}倉安全庫存是多少"
        else:
            # r69 fuzz：冷 context 的「只看南倉的」（卡片/選單/清單後）→ 南倉庫存概覽
            # （曾回全店 60 項概覽、看不出南倉視角）
            _wh0g = _CTX_WH_ONLY.match(text)
            if _wh0g and any(w in text for w in ("只看", "只要", "先看", "的")):
                return f"{_wh0g.group(3)}倉的庫存"
        return text

    # 鐵律：句中已有可辨識實體 → 絕不覆蓋（資訊銷毀已 11 例）
    if _has_real_item(text):
        return text

    if any(w in text for w in _CTX_GLOBAL):   # 自帶完整意圖 → 不接地
        return text

    wh_only = _CTX_WH_ONLY.match(text)
    has_pron = any(p in text for p in _CTX_PRON)
    # 光靠功能詞（「快到期嗎」「現在剩幾個」）認定追問 → 只認很短的句子，
    # 長一點就可能是自帶意圖的獨立問句
    has_bare = any(w in text for w in _CTX_BARE) and len(text) <= 10
    # r37：「鋼琴烤漆保養油庫存」含「庫存」又 ≤10 字 → 曾被當追問、carry-over 到
    #   前句商品（回 LED 露營燈 / 熱銷榜）。但它自帶商品名（只是不存在的商品）→
    #   不是追問，該讓它往下走回「找不到鋼琴烤漆保養油」。判別：剝掉功能詞後還剩
    #   ≥3 字實質描述（非純代詞/倉別/語助）→ 訪客自己講了商品名，不接地。
    if has_bare:
        _stem = text
        for _fw in sorted(_CTX_BARE, key=len, reverse=True):
            _stem = _stem.replace(_fw, "")
        for _fp62 in ("來著", "是說", "啊這"):   # r62b：語尾填充詞非商品（「剩多少來著」）
            _stem = _stem.replace(_fp62, "")
        _stem = _stem.strip("的呢嗎吧了還剩現在有多少什麼賣怎麼哪個要幫我？?。!！， 　")
        # 剩餘要「像商品名」：≥3 字、非代詞/倉別、不含疑問殘字（「什麼賣」→ 剝完剩空
        #   或殘字，不算；「鋼琴烤漆保養油」→ 實質商品名，算）
        _qwords = ("什麼", "怎麼", "哪", "如何", "為何", "多少", "幾")
        if (len(_stem) >= 3 and not any(p in _stem for p in _CTX_PRON)
                and not any(q in _stem for q in _qwords)
                and not _CTX_WH_ONLY.match(_stem)):
            has_bare = False   # 自帶商品名，交給下游回「找不到」
        elif (len(_stem) == 2 and not any(p in _stem for p in _CTX_PRON)
                and not any(q in _stem for q in _qwords)
                # 代詞殘字（「那ㄍ」「這ㄍ」注音殘）不是商品名——曾誤殺 60 條
                # 「那ㄍ快到期嗎」多輪守衛（r62 修正的修正）
                and not _re.search(r"[那這它牠]", _stem)):
            # r62：2 字未知商品（「奶瓶還有多少庫存」）——查無比黏上 context 舊商品
            # 錯答好（曾回玻璃保鮮盒）。2 字真商品（耳機）已被 _has_real_item 擋在前面。
            try:
                import warehouse as _W_st62
                if not _W_st62.match_items(_stem):
                    has_bare = False
            except Exception:
                pass
    # r34：寫入追問（「北倉進20個」——查完商品接著進貨，展場高頻）。r32 寫了
    #   組句邏輯卻沒把它列進觸發條件 → 這條路徑從來沒被走到，訪客拿到
    #   「找不到商品『進20個』」。（r32 的守衛斷言只寫 not:error，clarify 也算過 → 假綠）
    has_write = any(w in text for w in _CTX_WRITE) and _re.search(r"\d", text)
    has_part = text.strip() in _CTX_PARTICLE     # r35：「呢」「咧」單字追問
    # r40：時段追問（「上週呢」「這個月呢」）——上一輪是進出查詢時成立
    # r56 fuzz：庫存查詢後的「昨天呢」也是問那個商品的進出（曾被守門員拒）
    # r68：語尾補「勒/咧」（「上週勒」曾被拒）
    _period_stem = text.strip().strip("的呢咧勒嗎吧了?？。!！，, ")
    has_period = (_period_stem in _CTX_PERIOD
                  and ctx.get("last_func") in ("query_movement", "query_inventory"))
    if not (wh_only or has_pron or has_bare or has_write or has_part or has_period):
        return text

    stripped = text
    for p in _CTX_PRON:
        stripped = stripped.replace(p, "")
    stripped = stripped.strip("的呢嗎吧?？。!！，, ")

    fw = "進出紀錄" if ctx.get("last_func") == "query_movement" else "庫存"
    if has_period:
        # 時段追問（「上週呢」→「藍牙耳機上週進出紀錄」）
        new = f"{last}{_CTX_PERIOD[_period_stem]}進出紀錄"
    elif wh_only:
        # 純倉別追問：「南倉呢」「北倉多少」「南」→ 只取倉別字，其餘（「多少」「幾個」）
        #   丟掉，否則組出「北倉多少無線滑鼠庫存」這種怪句
        _whc = wh_only.group(3)
        if ctx.get("last_func") == "list_expiring_items":
            # r55 收官批：「快過期的有哪些」→「只看南倉的」＝到期清單換倉別過濾，
            # 不是問單一商品（曾回 60 項概覽）
            new = f"{_whc}倉快到期的有哪些"
        elif ctx.get("last_func") == "list_low_stock":
            new = f"{_whc}倉庫存警示"
        elif ctx.get("last_func") == "manage_config":
            # r57：「瑜珈墊安全庫存多少」→「北倉的呢」＝問北倉的設定值（曾回北倉庫存）
            new = f"{_whc}倉{last}安全庫存是多少"
        else:
            new = f"{_whc}倉{last}{fw}"
    elif any(w in stripped for w in _CTX_WRITE) and _re.search(r"\d", stripped):
        # 寫入追問（「北倉進20個」）→ 商品名補在句尾，維持「動作+數量+商品」語序
        # r56 危險邊緣：「奶茶進30杯」——句中自帶（查無的）商品名，曾被黏上 context
        # 舊商品變「奶茶進30杯無線滑鼠」→ 進錯貨。剝掉動作/數字/單位/倉名後還有
        # ≥2 字殘留＝訪客自己講了商品，不展開，讓下游誠實回「找不到」。
        _wr_res = stripped
        for _ww in ("進貨", "出貨", "調貨", "補貨", "進", "出", "調", "補", "到", "去", "從",
                    # r57：「馬上出10個」的副詞不是商品名（曾回「找不到商品『馬上』」）
                    "馬上", "立刻", "趕快", "順便", "然後", "再", "先", "幫我", "請", "麻煩",
                    "直接", "快點",
                    # r71：「保險起見北倉進30頂」的狀語（曾回找不到商品「保險起見」）
                    "保險起見", "以防萬一", "乾脆", "還是"):
            _wr_res = _wr_res.replace(_ww, "")
        _wr_res = _re.sub(r"[\d０-９]+", "", _wr_res)
        for _wu in ("個", "件", "箱", "杯", "打", "包", "盒", "罐", "瓶", "組", "雙",
                    "入", "台", "支", "條", "頂", "卡車", "車", "北倉", "中倉", "南倉",
                    "北區倉", "中區倉", "南區倉"):
            _wr_res = _wr_res.replace(_wu, "")
        for _wq in ("一點", "一些", "一批", "幾件", "幾個", "就好", "好了"):
            _wr_res = _wr_res.replace(_wq, "")   # r60：「調一點…調20個」的量詞副詞非商品名
        _wr_res = _wr_res.strip("的呢嗎吧了好就喔啦那 ")
        if len(_wr_res) >= 2:
            return text
        # r57：「馬上出10個」句中沒倉別 → 沿用上一輪倉別（確認卡會顯示、HITL 把關）
        _pref_wh57 = ""
        if not _re.search(r"[北中南]", stripped):
            _pref_wh57 = {"north": "北倉", "central": "中倉",
                          "south": "南倉"}.get(ctx.get("last_wh"), "")
        new = f"{_pref_wh57}{stripped}{last}"
    elif not stripped or text.strip() in _CTX_PARTICLE:
        # 純代詞句（「那個呢」）／純語助詞（「呢」「咧」）→ 用上一輪的功能補完
        new = f"{last}{fw}"
    else:
        new = f"{last}{_CTX_BARE_CANON.get(stripped, stripped)}"

    log.info(f"[ctx-expand] {text!r} → {new!r}（last_sku={last!r}）")
    return new


# pending 卡片在畫面上時，訪客對著卡片講的話（過去 server 毫無所悉 → 「好」被守門員
# 拒、「不對是100個」把 100 match 成「運動毛巾 100x30cm」幻覺回庫存）。
# 產品決策：一律引導按按鈕，寫入授權只認按鈕（打字不寫入）。
_PEND_OK = ("好", "好的", "好啊", "可以", "確認", "確定", "對", "是", "沒錯", "嗯",
            "嗯嗯", "行", "沒問題", "就這樣", "送出", "執行", "ok", "okay", "yes", "y",
            # ⚠️ EN build（r5-voice）：**裸 `confirm` 原本沒收**——中文的「確認」
            #   在這裡，英文卻只有 `press confirm`/`confirm it` 那類片語進了
            #   `_PEND_OK_SUB`。實測 alert 卡上打 `confirm` 回
            #   「No item matching "confirm"」＝訪客照著卡片上的字打卻沒反應。
            "confirm", "confirmed", "yep", "yeah", "yup", "sure", "affirmative",
            "proceed", "go", "save", "save it", "done")
# r33：整句比對漏掉「按確認」「幫我按」「就這樣送出」這類講法 → 落到守門員回教學文。
#   卡片在時，含這些詞的短句一律視為「想按確認」。
_PEND_OK_SUB = ("按確認", "幫我按", "幫我確認", "按下去", "按鈕", "點確認", "就這樣",
                "送出", "執行吧", "確認吧", "可以了", "沒問題",
                # r78：「現在先跑一次」＝執行腳本卡
                "跑一次", "跑吧", "執行",
                # EN build：英文版對應講法（原全中文 → 英文訪客講什麼都不命中，
                #   落到守門員回教學文）
                "press confirm", "click confirm", "hit confirm", "confirm it",
                "go ahead", "do it", "submit", "sounds good", "looks good",
                "thats right", "that's right", "correct",
                # r5-voice：訪客常見的「肯定＋客套」組合。原本只收單詞或
                #   動賓片語，`yes please`(10 字元) 連長度門檻都過不了
                #   （已同步改成英文用詞數，見 _vc_len_ok）
                # ⚠️ 移除 "all good" / "perfect"（坑 8 撞出來的）：
                #   'is that all good' 是**疑問**該引導不該代按（代按＝直接
                #   寫入，判錯代價高）；'perfect fit socks stock' 是商品查詢。
                "yes please", "ok do it", "please do", "go for it",
                "thats fine", "that's fine")
# 疑問/求助（「這樣對嗎」「要按哪裡」）→ 也該引導，不可回教學文
_PEND_ASK = ("對嗎", "可以嗎", "是這樣嗎", "要按哪", "怎麼按", "然後呢", "接下來", "要幹嘛",
             # r55 收官批：「算了照原本的」＝維持卡片內容 → 引導按確認（不代按、不取消）
             "照原本", "照舊", "維持原本",
             # r57：暫停詞（「先等一下」「還在嗎」）＝卡片留著等訪客想，引導不取消
             "等一下", "等等", "先等", "考慮一下", "讓我想", "還在嗎",
             # r64：維持語新形
             "維持原樣", "保持原樣", "維持現狀",
             # EN build
             "is this right", "is that right", "what now", "whats next",
             "what's next", "which button", "where do i", "how do i",
             "then what", "keep it", "leave it", "as is", "wait", "hold on",
             "let me think",
             # r5-voice：確認前再問一次（`is that all good` 曾回全店概覽）。
             #   ⚠️ 放這裡（引導）**不是**放 _PEND_OK_SUB（代按）——疑問句
             #   判成代按就是直接寫入，判錯代價高。
             "all good", "is that ok", "is that okay", "does that look right",
             "looks ok", "sure about", "you sure")
_PEND_FIX = ("不對", "不是", "改成", "改為", "錯了", "應該是", "換成", "改一下", "多一點",
             "少一點", "太多", "太少", "數量錯", "倉庫錯", "商品錯",
             # r35 反悔鏈：訪客改主意的實際講法（「還是80好了」曾落到守門員教學文、
             #   「我是說南倉」曾回「找不到『我是說』相關商品」）
             "還是", "我是說", "我要說", "剛剛講錯", "說成", "打成", "應該改",
             # r55 收官批：「等等 改15個」——短句帶「改」就是想改卡片內容
             "改",
             # EN build
             "wrong", "not right", "change it", "change to", "make it",
             "should be", "i meant", "i mean", "instead", "too many",
             "too few", "no its", "no it's")
_PEND_LABEL = {"movement_confirm": "Confirm Inbound/Outbound",
               "transfer_confirm": "Confirm Transfer",
               "config_confirm": "Confirm Setting", "item_confirm": "Confirm Add",
               "item_delete_confirm": "Confirm Delete",
               "po_confirm": "Authorize Purchase Order",
               "alert_confirm": "Authorize Alert", "schedule_confirm": "Confirm Schedule",
               "script_confirm": "Authorize Run"}
# 修正引導的例句要貼合卡片類型（設定卡舉進貨的例子會讓訪客更迷惘）
_PEND_EXAMPLE = {"movement_confirm": "north received 100 wireless mouse",
                 "transfer_confirm": "transfer 20 wireless mouse from north to south",
                 "config_confirm": "set fitness band safety stock to 50",
                 "alert_confirm": "alert me when yoga mats drop below 30",
                 "schedule_confirm": "run the stock report at 8am every day"}


# ── r33 統一放棄層 ──
# 「取消」二字有攔、其他講法全漏：流程中說「我不要了」「先不要」「退出」→ 被吞成
# 商品名稱；卡片在時說「算了不用」「不要了」→ 守門員（排在 meta-gate 之前）直接
# 回教學文。放棄是**跨情境**的意圖，必須在守門員之前用同一個閘門處理。
# 「跳過」不可列入——那是新增商品第四步的合法值。
_ABORT_WORDS = ("取消", "算了", "不用了", "不要了", "我不要", "先不要", "不想要",
                "不用查", "不查了", "不用了啦", "退出", "離開", "結束", "停止",
                "放棄", "當我沒說", "沒事了", "不管了", "先不用", "不繼續", "不做了",
                "不用補", "不補了",   # r71：「那不用補囉」曾被 ctx 當查詢回庫存
                "不管它", "不理它", "隨它",   # r73：「還很多啊 那不管它」曾回庫存
                "先不補",   # r79：「好 那先不補」曾回庫存卡
                # EN build：英文放棄講法（原全中文 → 英文訪客說 cancel/never
                #   mind 沒有任何一條命中，卡片取消不掉）
                "cancel", "never mind", "nevermind", "forget it", "drop it",
                "no thanks", "no thank you", "not now", "leave it", "stop",
                "quit", "exit", "abort", "discard", "undo", "scrap it")


# 「取消所有排程」「取消瑜珈墊的警示」是**管理指令**不是放棄——有明確對象詞就豁免
# （r23 已為 meta-gate 建過同一組豁免，這裡必須同步，否則守衛庫的排程/警示句全掛）
# r55 收官批：「算了照原本的」＝維持原卡片內容，不是放棄——「照原本/照舊/維持」豁免，
# 讓 pending 卡片口語層引導按確認（卡片曾被「算了」二字整張誤取消）。
_ABORT_EXEMPT = ("排程", "警示", "提醒", "訂閱", "規則", "訂單", "採購",
                 "照原本", "照舊", "維持")

# EN build：豁免詞的英文版。中文版早有「排程/警示/規則」豁免——
#   「取消排程」是**管理排程**不是放棄當前卡片。英文沒補 → 'cancel my
#   schedules' 3 個詞命中 cancel → 判成放棄，回「沒有進行中的操作」，
#   排程取消功能整條進不去。
_ABORT_EXEMPT_RE_EN = (r"\b(?:schedules?|alerts?|reminders?|subscriptions?|"
                       r"rules?|orders?|purchase orders?|pos?)\b")


def _abort_intent(text: str) -> bool:
    """短句 + 命中放棄詞 → 視為放棄意圖。長句可能是正常需求（「不要缺貨的商品」）。"""
    t = text.strip().strip("!！。.~ ")
    if any(w in t for w in _ABORT_EXEMPT):
        return False
    if _is_mostly_english(t) and _re.search(_ABORT_EXEMPT_RE_EN, t, _re.I):
        return False
    # r73：結尾語放棄（「還很多啊 那不管它」）——結尾錨定放寬到 12 字
    if len(t) <= 12 and _re.search(r"(不管它|不理它|隨它|算了吧)$", t):
        return True
    # EN build：英文用單詞數門檻（原「字元數 > 8」是中文制，英文
    #   "never mind" 就 10 字元直接被擋掉＝放棄層對英文全失效）；
    #   比對也要小寫化（"Cancel" / "Never mind" 大寫開頭很常見）。
    if _is_mostly_english(t):
        if len(t.split()) > 4:
            return False
        _tl = t.lower()
        # ⚠️ 英文放棄詞必須要求**詞界**，不能用 substring：
        #   'mos**quit**o spray inventory' 會命中 quit、
        #   'clear the **stop**page' 會命中 stop → 正常查詢被當成放棄句，
        #   回「目前沒有進行中的操作」（而且是中文的，展場直接露餡）。
        #   同一類問題已在 _po_kw 的裸 "po" 上踩過（report/export 誤中）。
        _abort_en = [w for w in _ABORT_WORDS if w.isascii()]
        if any(_re.search(r"(?<![a-z])" + _re.escape(w) + r"(?![a-z])", _tl)
               for w in _abort_en):
            return True
        # 中文放棄詞在英文句裡不會出現，但保留比對以防中英混雜殘句
        return any(w in _tl for w in _ABORT_WORDS if not w.isascii())
    if len(t) > 8:
        return False
    return any(w in t for w in _ABORT_WORDS)


def _pending_reply(vid, text: str) -> str:
    """有 pending 卡且訪客在對卡片講話 → 回引導語；否則回空字串。"""
    pend = _pending_by_vid.get(vid)
    if not pend:
        return ""
    # 長度門檻：原本「字元數 > 12」是為中文調的，英文字元數是中文 2-3 倍
    #   （"is this right?" 才 3 詞卻 14 字元）→ 英文引導語幾乎全被擋掉。
    #   英文改用單詞數 > 6（同 long-gate 的處理方式）。
    _is_en_pend = _is_mostly_english(text)
    if (len(text.split()) > 6) if _is_en_pend else (len(text) > 12):
        return ""
    view = pend.get("view", "")
    btn = _PEND_LABEL.get(view, "Confirm")
    t = text.strip().strip("!！。.~ ").lower()
    # r57：FIX 先於 ASK——「等等 改15個」同時含暫停詞(等等)與修改詞(改)，
    # 訪客要的是改內容，修改引導比按鈕引導對
    if any(w in text for w in _PEND_FIX) or any(w in t for w in _PEND_FIX):
        # ⚠️ r15：**這句本身可能已經是完整查詢**——`show me the earphone
        #   stock instead` 剛好 6 詞（沒超過上面的門檻）、又含 'instead'
        #   命中 _PEND_FIX → 回「請說完整的請求」，但訪客**已經說了**，
        #   於是卡在那張卡片出不來（展場會卡死的體感）。
        #   ⇒ 有具體商品名就放行走正常路由，別回引導語。
        #   （只認**英文**：中文的「還是80好了」本來就該走引導，那是改數量）
        if _is_en_pend and _text_has_item_name(text):
            return ""
        eg = _PEND_EXAMPLE.get(view, "north received 100 wireless mouse")
        return (f'To change it, please say the full request again '
                f'(e.g. "{eg}"), or just tap the "✅ {btn}" button on the '
                f'card above to accept it as is.')
    if t in _PEND_OK or any(w in text for w in _PEND_OK_SUB) \
            or any(w in t for w in _PEND_OK_SUB) \
            or any(w in text for w in _PEND_ASK) or any(w in t for w in _PEND_ASK):
        return (f'Please tap the "✅ {btn}" button on the card above to '
                f'actually save it, or say "cancel" to discard.')
    return ""

# ─── Util ─────────────────────────────────────────────────
def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def get_url() -> str:
    return EXTERNAL_URL or f"http://{get_local_ip()}:{PORT}"


def find_gguf() -> str:
    """只看 test/models/ — test/ 必須自足。"""
    explicit = os.getenv("MODEL_PATH", "")
    if explicit and Path(explicit).exists():
        return explicit
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(MODELS_DIR.glob("*.gguf"))
    if not files:
        raise FileNotFoundError(
            f"\n找不到 GGUF 模型！請放到：{MODELS_DIR}/\n"
            f"提示：每次重新量化後、把 .q8_0.gguf 複製到 test/models/"
        )
    return str(files[0])


def load_system_prompt() -> str:
    """只讀 test/system_prompt.txt — test/ 必須自足。"""
    if SYSTEM_PROMPT_FILE.exists():
        return SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(
        f"找不到 {SYSTEM_PROMPT_FILE}\n"
        f"提示：每次重新微調後、把根目錄 system_prompt.txt 複製到 test/"
    )


def load_model():
    from llama_cpp import Llama
    path = find_gguf()
    # EN build：_set_health 的 message 會透過 /health 顯示在**訪客載入畫面**上
    #   （log/print 是維運訊息、保留中文）
    _set_health("loading_model", f"Loading model... ({Path(path).name})")
    log.info(f"載入模型：{path}")
    log.info(f"CPU 設定: n_threads={N_THREADS} n_threads_batch={N_THREADS_BATCH} "
             f"n_batch={N_BATCH} n_ctx={N_CTX}")
    llm = Llama(
        model_path=path,
        n_ctx=N_CTX,
        n_threads=N_THREADS,
        n_threads_batch=N_THREADS_BATCH,
        n_batch=N_BATCH,
        n_gpu_layers=0,
        use_mmap=True,
        use_mlock=False,
        flash_attn=False,
        verbose=False,
    )
    _set_health("self_check", "Model loaded, running inference self-check (up to 10s)...")
    log.info("模型載入完成、正在自我檢測推論...")

    import threading
    result_holder = {"text": None, "error": None}

    def _self_check():
        try:
            # 自我檢測 + KV cache 暖機二合一：用真實 build_prompt（完整
            # system_prompt ~1236 tok）跑一次，讓 KV cache 熱的是正確的
            # system_prompt 內容。第一個真訪客的 prompt 前綴命中快取，
            # 首句延遲從冷啟 ~2.9s 降到 ~1.1s（RPI5 實測，2026-07-04）。
            # 舊版用 "Hi"（1 tok）自檢，快取熱錯內容，第一句仍全量重算。
            warm_prompt = build_prompt("藍牙耳機庫存")
            r = llm(warm_prompt, max_tokens=8, echo=False, temperature=0.0,
                    stop=GEMMA_STOP)
            result_holder["text"] = r["choices"][0]["text"]
        except Exception as e:
            result_holder["error"] = e

    t = threading.Thread(target=_self_check, daemon=True)
    t.start()
    t.join(timeout=20.0)   # 完整 prompt 首次 eval 較久（冷啟 ~3s），放寬 timeout

    if t.is_alive():
        err_msg = (
            "推論自我檢測超時（10 秒）— 模型載入成功但推論卡住。\n"
            "可能原因：\n"
            "  1. CPU 指令集不相容（llama-cpp DLL 在這台 CPU 上 deadlock）\n"
            "  2. 防毒軟體攔截了 native code 執行\n"
            "  3. CPU 太舊（早於 2008 年）\n"
            "請回報此問題並附上 CPU 型號（執行 wmic cpu get name 取得）"
        )
        _set_health("failed", "Inference self-check failed (no response in 10s)", error=err_msg)
        print("\n" + "=" * 70, flush=True)
        print(" X 推論自我檢測失敗：超過 10 秒沒回應", flush=True)
        print("=" * 70, flush=True)
        for line in err_msg.split("\n"):
            print(" " + line, flush=True)
        print("=" * 70 + "\n", flush=True)
        raise RuntimeError("推論自我檢測 timeout（10 秒）")

    if result_holder["error"] is not None:
        e = result_holder["error"]
        err_msg = f"{type(e).__name__}: {e}"
        _set_health("failed", "Inference self-check failed (exception)", error=err_msg)
        raise RuntimeError(f"推論自我檢測失敗: {err_msg}") from e

    log.info(f"模型就緒 OK 自我檢測輸出: {result_holder['text']!r}")
    return llm, path


# ─── Prompt Builder ───────────────────────────────────────
def sanitize_input(user_text: str) -> str:
    """過濾 Gemma 控制 token、防止 prompt injection。"""
    for t in ("<start_of_turn>", "<end_of_turn>", "<start_function_call>",
              "<end_function_call>", "<escape>", "<eos>"):
        user_text = user_text.replace(t, "[X]")
    return user_text


def build_prompt(user_text: str) -> str:
    user_text = sanitize_input(user_text)
    p = SYSTEM_PROMPT
    if not p.endswith("\n"):
        p += "\n"
    p += f"<start_of_turn>user\n{user_text}\n<end_of_turn>\n"
    p += "<start_of_turn>model\n"
    return p


# ─── Function Call Parser ─────────────────────────────────
TOOL_RE = re.compile(r"<start_function_call>call:(\w+)\{")
ARG_RE  = re.compile(r"(\w+):<escape>([^<]*)<escape>")


def parse_function_call(text: str) -> tuple[str, dict] | None:
    m = TOOL_RE.search(text)
    if not m:
        return None
    name = m.group(1)
    args: dict = {}
    for k, v in ARG_RE.findall(text):
        args[k] = v
    return name, args


# ─── 定時排程執行器 ───────────────────────────────────────
async def _schedule_runner_loop():
    """每分鐘掃一次 schedule_jobs.json，時間到就跑對應腳本並推 WS。"""
    import asyncio as _aio
    await _aio.sleep(15)  # 等 server ready
    while True:
        try:
            if HEALTH.get("stage") == "ready":
                await _run_due_schedules()
        except Exception as e:
            log.error(f"[scheduler] 掃描失敗: {e}")
        await _aio.sleep(60)  # 每分鐘檢查一次

async def _run_due_schedules():
    """檢查哪些排程到時間，到了就跑腳本。"""
    from tools_v2 import _data_dir, commit_run_script
    import json as _json
    now = datetime.now()
    try:
        dd = _data_dir()
        jobs_path = dd / "schedule_jobs.json"
        if not jobs_path.exists():
            return
        jobs = _json.loads(jobs_path.read_text("utf-8")).get("jobs", [])
        for job in jobs:
            if not job.get("enabled", True):
                continue
            h, m = map(int, job["time_str"].split(":"))
            if now.hour != h or now.minute != m:
                continue
            # 防重複：同一分鐘不重跑（last_run 記錄）
            last_run = job.get("last_run", "")
            now_min = now.strftime("%Y-%m-%dT%H:%M")
            if last_run.startswith(now_min):
                continue
            # 執行腳本
            log.info(f"[scheduler] 排程 {job['id']} 觸發：{job['script_label']}")
            await push_display({"type": "schedule_triggered", "job_id": job["id"],
                                "script_label": job["script_label"], "ts": now.strftime("%H:%M")})
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda jid=job["script_id"]: __import__("tools_v2").commit_run_script(
                                                                   jid, actor="scheduler"))
            # 更新 last_run
            job["last_run"] = now.isoformat(timespec="seconds")
            jobs_path.write_text(_json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2), encoding="utf-8")
            # 2026-08-06 user 要求（ZH 同款）：排程產出給可點開分頁的報告連結
            _sd_tail = str(result.get("data", {}).get("output_tail", ""))
            _sd_view = _re.search(r"VIEW:\S*?(audit/[^\s/\\]+\.html)", _sd_tail)
            _sd_link = (f'<br><a href="/{_sd_view.group(1)}" target="_blank" '
                        f'style="color:#2b6cb0;font-weight:600">📊 Open report</a>'
                        if _sd_view else "")
            await push_display({"type": "schedule_done", "job_id": job["id"],
                                "script_label": job["script_label"],
                                "ok": result.get("ok", False),
                                "summary": result.get("summary", ""),
                                "output_tail": result.get("summary", "") + _sd_link,
                                "ts": now.strftime("%H:%M")})
    except Exception as e:
        log.error(f"[_run_due_schedules] {e}", exc_info=True)


# ─── 警示規則背景排程 ──────────────────────────────────────
_ALERT_CHECK_INTERVAL = 3600  # 每小時掃一次


# ── 展場體驗：設定完馬上觸發一次 ────────────────────────────
#   背景排程的節奏是給「真實營運」用的（警示每小時、排程每天固定時刻），
#   但**展場訪客只停留幾分鐘** ⇒ 設定完只看到一張確認卡，看不到「它真的會動」，
#   而觸發那一刻（畫面跳出通知橫幅）才是這個功能的賣點。
#   ⇒ 確認後立刻跑一次**真實檢查/真實腳本**（非假動畫），讓因果在同一個畫面完成。
#   ⚠️ 用 create_task 背景跑：不擋確認卡的回應（訪客先看到「已建立」，
#     1-2 秒後通知橫幅才跳出來，順序才對）。
def _demo_kick(coro, tag: str):
    """把展場即時觸發丟到背景跑，失敗只記 log 不影響主流程。"""
    async def _run():
        try:
            await asyncio.sleep(1.2)      # 讓「已建立」的卡片先落地
            await coro
        except Exception as e:
            log.warning(f"[demo-kick:{tag}] 即時觸發失敗（不影響設定本身）: {e}")
    try:
        asyncio.create_task(_run())
    except Exception as e:
        log.warning(f"[demo-kick:{tag}] 無法排程: {e}")


async def _demo_run_schedule(job: dict):
    """立刻執行剛建立的排程（走 `_run_due_schedules` 同一套推播與執行）。"""
    if not job or not job.get("script_id"):
        return
    now = datetime.now()
    await push_display({"type": "schedule_triggered", "job_id": job.get("id", ""),
                        "script_label": job.get("script_label", ""),
                        "ts": now.strftime("%H:%M")})
    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: __import__("tools_v2").commit_run_script(
                                      job["script_id"], actor="scheduler"))
    ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
    # 2026-08-06（ZH 同款）：立跑是展場設計但橫幅與正式觸發同型＝像亂執行。
    #   summary 前綴示範說明。
    _kick_note = (f"🎬 Schedule created — running once now as a demo; "
                  f"it will then run automatically "
                  f"{job.get('freq_label', 'daily')} at "
                  f"{job.get('time_str', '')}.\n")
    await push_display({"type": "schedule_done", "job_id": job.get("id", ""),
                        "script_label": job.get("script_label", ""),
                        "ok": ok,
                        "summary": _kick_note + (result or {}).get("summary", "")})

async def _alert_scheduler_loop():
    """背景每小時掃 alert_rules.json，觸發時推 WebSocket 通知。"""
    import asyncio as _aio
    await _aio.sleep(10)  # 等 server ready
    while True:
        try:
            if HEALTH.get("stage") == "ready":
                await _check_alert_rules()
        except Exception as e:
            log.error(f"[alert_scheduler] 掃描失敗: {e}")
        await _aio.sleep(_ALERT_CHECK_INTERVAL)

async def _check_alert_rules(only_rule_id: str = ""):
    """掃一次 alert_rules.json，有觸發就推 WebSocket。

    `only_rule_id`：**展場即時觸發**用——只檢查訪客剛建立的那一條。
    不限定的話會把 baseline 既有的全域規則（AL001/AL003）一起觸發，
    畫面跳出三條橫幅、前兩條跟訪客無關 ⇒ 展場會很混亂（實測到）。
    背景排程仍是不帶參數＝掃全部（那才是真實營運要的行為）。
    """
    from tools_v2 import _data_dir, _match_script
    import json as _json
    try:
        dd = _data_dir()
        rules_path = dd / "alert_rules.json"
        if not rules_path.exists():
            return
        rules = _json.loads(rules_path.read_text("utf-8")).get("rules", [])
        active = [r for r in rules if r.get("enabled", True)]
        if only_rule_id:
            active = [r for r in active if r.get("id") == only_rule_id]
        if not active:
            return
        # 用 list_low_stock 取缺貨資料
        result = finance.execute("list_low_stock", {})
        warns = result.get("data", {}).get("warnings", []) if isinstance(result.get("data"), dict) else []
        sku_warn_ids = {w["sku_id"] for w in warns}

        for rule in active:
            cond = rule.get("condition", "")
            scope = rule.get("scope", [])  # [] = 全部
            cond_label = rule.get("condition_label", cond)
            scope_names = rule.get("scope_names", [])
            scope_txt = "all items" if not scope_names else ", ".join(scope_names[:3])

            triggered = False
            detail = ""
            if cond in ("below_safety", "below_threshold", "out_of_stock"):
                if scope:
                    hits = [w for w in warns if w["sku_id"] in scope]
                else:
                    hits = warns
                if hits:
                    triggered = True
                    names = ", ".join(w["name"] for w in hits[:3])
                    detail = f"{names} and {len(hits)} items below safety stock"
            elif cond == "expiring":
                exp_result = finance.execute("list_expiring_items", {"days": 14})
                exp_items = exp_result.get("data", {}).get("items", []) if isinstance(exp_result.get("data"), dict) else []
                if scope:
                    exp_items = [e for e in exp_items if e.get("sku_id") in scope]
                if exp_items:
                    triggered = True
                    names = "、".join(e["name"] for e in exp_items[:3])
                    detail = f"{names} 等 {len(exp_items)} 項即將到期"

            if triggered:
                log.info(f"[alert] 規則 {rule['id']} 觸發：{detail}")
                await push_display({
                    "type": "alert_triggered",
                    "rule_id": rule["id"],
                    "condition_label": cond_label,
                    "scope_txt": scope_txt,
                    "detail": detail,
                    "ts": datetime.now().strftime("%H:%M"),
                })
            elif only_rule_id:
                # 🎯 展場體驗：**沒觸發也要回報一次**。
                #   60 個商品裡只有 31 個當下低於安全庫存 ⇒ 訪客隨手挑有
                #   約一半機率什麼都不會跳，而「系統壞了」與「這商品剛好沒缺」
                #   在畫面上**完全一樣**（都只有一張「已建立」卡片）⇒ 訪客
                #   無法確定警示到底有沒有生效。
                #   回報「已檢查、目前不符合條件」反而更能證明規則真的跑了。
                #   ⚠️ 只在展場即時觸發（only_rule_id）時送——背景每小時掃描
                #     不能送，否則沒事也一直洗版。
                log.info(f"[alert] 規則 {rule['id']} 已檢查、未達條件")
                await push_display({
                    "type": "alert_checked_ok",
                    "rule_id": rule["id"],
                    "condition_label": cond_label,
                    "scope_txt": scope_txt,
                    "ts": datetime.now().strftime("%H:%M"),
                })
    except Exception as e:
        log.error(f"[_check_alert_rules] {e}", exc_info=True)


# ─── Display 廣播 ─────────────────────────────────────────
async def push_display(payload: dict):
    """推播給看板 + **訪客本人**。

    🚨 2026-08-03：原本只送 `display_sockets`（`/ws/display`），但訪客頁面
    **只連 `/ws`**（index.html:978）、且 `/ws/display` 實測**零連線**
    ⇒ `alert_triggered` / `schedule_triggered` 這些通知橫幅
    **從來沒有任何訪客收得到**（前端 handleMessage 的處理分支一直是對的，
    訊息根本沒送到那條連線）。今天 SCH001 早上 9 點真的跑了，畫面上也沒東西。
    ⇒ 改成兩個 set 都送。訪客沒開的推播型別前端會自然忽略（handleMessage
    是 if-type 分派），不會有副作用。
    """
    msg  = json.dumps(payload, ensure_ascii=False)
    dead = set()
    for ws in list(display_sockets) + list(all_sockets):
        try:
            # 🚨 2026-08-06（ZH 排程盤點連兩天卡死案，兩版同修）：隔夜半死
            #   連線的 send_text 永久 pending 不 raise → 排程/警示推播的
            #   coroutine 掛死。per-send 3 秒上限。
            await asyncio.wait_for(ws.send_text(msg), timeout=3.0)
        except Exception:
            dead.add(ws)
    display_sockets.difference_update(dead)
    all_sockets.difference_update(dead)


# ─── FastAPI ──────────────────────────────────────────────
app = FastAPI()


async def _clf_watchdog_loop():
    """intent_clf 週期金絲雀自檢（每 10 分鐘）。

    開機自檢只擋「生下來就死」；這裡抓「跑到一半死」（套件熱更新/OOM/記憶體
    損壞）。狀態轉變才吼（ok→DEAD 一次 CRITICAL），不洗版；/health 的 clf
    欄位隨時反映最新狀態給外部監控。
    """
    while True:
        await asyncio.sleep(600)
        if HEALTH.get("stage") != "ready":
            continue
        try:
            ok, msg = intent_clf.self_check()
        except Exception as e:
            ok, msg = False, f"self_check crashed: {e}"
        if not ok:
            # 自癒：先試 reload 一次（抓記憶體損壞/暫時性錯誤），reload 救不回
            # 的（如 numpy2 這種確定性 bug）就留在 fallback 模式並大聲。
            try:
                await asyncio.to_thread(intent_clf.reload)
                ok, msg = intent_clf.self_check()
                if ok:
                    log.warning("[clf-check] 自檢失敗但 reload 後恢復——曾短暫死亡")
            except Exception as e:
                msg = f"reload failed: {e}"
        prev = HEALTH.get("clf")
        HEALTH["clf"] = "ok" if ok else f"DEAD: {msg}"
        if not ok and prev == "ok":
            log.critical(f"[clf-check] intent_clf 週期自檢由 ok 轉失敗（reload 無效）: {msg}")
        elif ok and prev != "ok":
            log.info("[clf-check] intent_clf 自檢恢復 ok")


def _background_init():
    """背景載入模型。"""
    global LLM, MODEL_FILE, SYSTEM_PROMPT
    try:
        _set_health("starting", "Initializing seed data...")
        finance.init(WH_DATA_DIR)
        intent_clf.load()
        SYSTEM_PROMPT = load_system_prompt()
        LLM, MODEL_FILE = load_model()
        # intent_clf 暖機＋金絲雀自檢（首次 predict 載 jieba 詞典 ~900ms，順便驗真）。
        # numpy2 事件（2026-07-16）：predict 內部崩潰被吞成 unknown、主路由在 RPI5
        # 靜默死亡多輪沒被發現——舊版這裡只是 try/except pass 的暖機，bug 就從這
        # 滑過去。自檢失敗不擋啟動（fail-soft：LLM+校正層扛得住、展場不能不開機），
        # 但要大聲：log CRITICAL + /health 曝光給外部監控。
        try:
            _clf_ok, _clf_msg = intent_clf.self_check()
        except Exception as _e:
            _clf_ok, _clf_msg = False, f"self_check crashed: {_e}"
        HEALTH["clf"] = "ok" if _clf_ok else f"DEAD: {_clf_msg}"
        if not _clf_ok:
            log.critical(f"[clf-check] intent_clf 主路由自檢失敗，每句將 fallback LLM"
                         f"（效能降級、C18 失效）: {_clf_msg}")
        snap = finance.state()
        log.info(f"快照日期：{snap.snapshot_date}")
        log.info(f"SKU 數：{len(snap.items)} / 倉庫：{len(snap.warehouses)} / 類別：{len(snap.categories)}")
        log.info(f"URL: {get_url()}")
        _set_health("ready",
                    f"Ready — snapshot {snap.snapshot_date}, {len(snap.items)} SKUs, "
                    f"{len(snap.warehouses)} warehouses")
    except Exception as e:
        log.error(f"[startup] 初始化失敗: {e}", exc_info=True)
        if HEALTH["stage"] != "failed":
            _set_health("failed", "Initialization failed", error=f"{type(e).__name__}: {e}")


@app.on_event("startup")
async def startup():
    import threading, asyncio
    threading.Thread(target=_background_init, daemon=True).start()
    # ── 主動異常偵測：背景線程定時掃描 + WS 推播 ──
    try:
        import anomaly
        anomaly.set_ws_push(push_display)              # 注入 WS 推播
        loop = asyncio.get_event_loop()
        anomaly.run_scheduler(loop)                    # 起背景排程（內建等 server ready）
        log.info(f"[anomaly] 背景異常掃描已啟動，間隔 {anomaly.AnomalyConfig.scan_interval_s}s")
    except Exception as e:
        log.error(f"[anomaly] 啟動失敗: {e}", exc_info=True)
    # ── intent_clf 週期自檢（抓「跑到一半死掉」，numpy2 事件後加）──
    asyncio.create_task(_clf_watchdog_loop())
    # ── 警示規則背景排程 ──
    asyncio.create_task(_alert_scheduler_loop())
    # ── 定時腳本排程 ──
    asyncio.create_task(_schedule_runner_loop())
    # ── 動態倉庫模擬：開機自動啟動（user 定調 2026-08-03）──
    #   展場開機就要看到數據在跳，不必手動按。預設 200× + 60 商品全動。
    #   ⚠️ 跑守衛/寫入測試前務必先關（run_guard_en.sh 已內建 stop）。
    asyncio.create_task(_live_autostart())


@app.get("/reports/{fname}")
async def get_report_file(fname: str):
    """報告圖表 PNG / markdown（沙盒：只允許 reports/ 下、擋路徑穿越）。"""
    from pathlib import Path as _P
    if "/" in fname or "\\" in fname or ".." in fname:
        return Response(status_code=400)
    rp = _P(finance.state().v2_data_dir) / "reports" / fname
    if not rp.exists():
        return Response(status_code=404)
    is_png = fname.endswith(".png")
    media = "image/png" if is_png else "text/markdown; charset=utf-8"
    headers = dict(NO_CACHE)
    if not is_png:
        # .md 沒有 Content-Disposition 時瀏覽器會直接顯示原始文字（不排版）；
        # 加 attachment 讓體驗跟 .csv 下載一致。PNG 本來就會正常顯示，不強制下載。
        headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return Response(content=rp.read_bytes(), media_type=media, headers=headers)


@app.get("/audit/{fname}")
async def get_audit_file(fname: str):
    """下載 audit/ 下的 CSV（盤點/匯出結果）。"""
    from pathlib import Path as _P
    if "/" in fname or "\\" in fname or ".." in fname:
        return Response(status_code=400)
    ap = _P(finance.state().v2_data_dir) / "audit" / fname
    if not ap.exists():
        return Response(status_code=404)
    # ⚠️ 2026-08-03：**HTML 直接在瀏覽器看，不強制下載**。
    #   RPI5 上用 LibreOffice 開 60 列的 CSV 要 3 分鐘還卡住（實測），
    #   展場根本不能用 ⇒ 盤點同時產一份 HTML，訪客點了直接看。
    #   CSV 保留給「要帶走用 Excel 分析」的情境。
    if fname.endswith(".html"):
        return Response(content=ap.read_bytes(),
                        media_type="text/html; charset=utf-8", headers=dict(NO_CACHE))
    media = "text/csv; charset=utf-8-sig"
    headers = {**NO_CACHE, "Content-Disposition": f'attachment; filename="{fname}"'}
    return Response(content=ap.read_bytes(), media_type=media, headers=headers)


def _live_grid() -> list:
    """60 商品 × 3 倉的當下數量（給前端網格用）。"""
    s = finance.state()
    out = []
    for it in s.items:
        sku = it["sku_id"]
        per = {w["key"]: s.stock.get(w["key"], {}).get(sku, 0) for w in s.warehouses}
        out.append({"sku": sku, "name": it["name"],
                    "total": sum(per.values()), "per": per,
                    "safety": it.get("safety_stock") or 0})
    return out


async def _live_push(mvs):
    """模擬跑完一批 → 推整批（前端只刷一次畫面，避免 60 筆刷 60 次）。"""
    if isinstance(mvs, dict):
        mvs = [mvs]
    await push_display({"type": "snapshot", "snapshot": finance.dashboard_snapshot()})
    # ⚠️ 2026-08-03：**不再送 moves 明細**——前端已不把例行進出貨塞進對話區
    #   （user 定調：版面只留缺貨警示這類該被看到的東西）。
    #   sweep 模式一輪 60 筆，送了也沒人渲染，純浪費頻寬。
    #   `n` 只是給前端/除錯知道這輪動了幾筆。
    await push_display({
        "type": "live_batch",
        "ts": datetime.now().strftime("%H:%M:%S"),
        "n": len(mvs),
        "grid": _live_grid(),
    })


@app.get("/api/live_grid")
async def live_grid():
    """60 商品當下數量（開啟網格時先填一次基準）。"""
    return JSONResponse({"grid": _live_grid()}, headers=NO_CACHE)


async def _live_autostart():
    """開機後等 server ready 再啟動動態模擬。

    ⚠️ 等 ready 是必要的：模擬會呼叫 `commit_movement`，那需要資料已載入。
       失敗只記 log，絕不影響服務啟動（模擬是展示功能，不是核心）。
    """
    import asyncio as _aio
    try:
        for _ in range(120):                 # 最多等 2 分鐘
            if HEALTH.get("stage") == "ready":
                break
            await _aio.sleep(1)
        else:
            log.warning("[live] 等 ready 逾時，自動啟動略過")
            return
        import live_sim
        live_sim.start_in_loop(push=_live_push)
        st = live_sim.status()
        log.info(f"[live] 開機自動啟動（{st['speedup']}× 速、"
                 f"{'全部商品' if st['sweep_all'] else st['batch']} 筆/輪、"
                 f"間隔 {st['interval_s']}s）")
    except Exception as e:
        log.error(f"[live] 自動啟動失敗（不影響服務）: {e}")


@app.post("/api/live_mode")
async def live_mode(req: Request):
    """動態倉庫模擬開關 + 現場調速（展場用；**預設關閉**）。

    body: {"action": "start"|"stop"|"tune",
           "speedup": 1-400, "batch": 1-60, "sweep_all": bool}

    ⚠️ 開著跑會持續改資料 ⇒ 守衛 892 句與所有寫入測試會隨機 FAIL，
       跑測試前務必關掉（或別開）。
    """
    import live_sim
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    act = (body.get("action") or "").lower()
    # 調速可以跟 start 同時送，也可以單獨 tune（跑的時候即時生效）
    if any(k in body for k in ("speedup", "batch", "sweep_all")):
        live_sim.tune(speedup=body.get("speedup"), batch=body.get("batch"),
                      sweep_all=body.get("sweep_all"))
    if act == "start":
        live_sim.start_in_loop(push=_live_push)
        st = live_sim.status()
        log.info(f"[live] 動態倉庫模擬 **開啟**（{st['speedup']}× 速、"
                 f"每輪 {'全部商品' if st['sweep_all'] else st['batch']} 筆、"
                 f"間隔 {st['interval_s']}s）")
    elif act == "stop":
        live_sim.stop()
        log.info("[live] 動態倉庫模擬 **關閉**")
    return JSONResponse(live_sim.status(), headers=NO_CACHE)


_ANOM_CACHE: dict = {"ts": 0.0, "data": None}
_ANOM_TTL = 30.0   # 秒；告警真正的即時通道是 WS 推播，這裡是輪詢的兜底


@app.get("/anomalies")
async def anomalies(only_new: bool = False):
    """主動異常偵測 — 也可被使用者主動查詢（雙軌：背景推 + 手動拉）。
    ⚠️ 2026-08-05 改 TTL 快取＋executor：scan_once 設計時 ~190ms，但它每次
    全掃 s.movements，模擬一天灌數十萬筆後膨脹成秒級，而且原本**同步跑在
    event loop 上**——kiosk 每 8 秒 poll 一次 → 兩版 server 100% CPU、
    所有 API 被餓死、UI 卡頓（機二實案，py-spy 抓到 MainThread 全在
    _daily_out_series）。"""
    import anomaly
    import time as _t
    loop = asyncio.get_event_loop()
    if only_new:
        data = await loop.run_in_executor(
            None, lambda: anomaly.scan_once(only_new=True))
        return JSONResponse(data, headers=NO_CACHE)
    if _ANOM_CACHE["data"] is None or _t.time() - _ANOM_CACHE["ts"] > _ANOM_TTL:
        data = await loop.run_in_executor(
            None, lambda: anomaly.scan_once(only_new=False))
        _ANOM_CACHE["ts"], _ANOM_CACHE["data"] = _t.time(), data
    return JSONResponse(_ANOM_CACHE["data"], headers=NO_CACHE)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}


@app.get("/health")
async def health():
    return JSONResponse(HEALTH, headers=NO_CACHE)


@app.get("/")
async def index():
    return HTMLResponse(
        (TEMPLATES_DIR / "index.html").read_text("utf-8"),
        headers=NO_CACHE,
    )


@app.get("/display")
async def display():
    return HTMLResponse(
        (TEMPLATES_DIR / "display.html").read_text("utf-8"),
        headers=NO_CACHE,
    )


@app.get("/snapshot")
async def snapshot():
    return JSONResponse(finance.dashboard_snapshot(), headers=NO_CACHE)


@app.get("/info")
async def info():
    return JSONResponse({
        "url":  get_url(),
        "host": get_local_ip(),
        "port": PORT,
        "https": get_url().startswith("https://"),
    })


@app.get("/qr.png")
async def qr_png():
    import qrcode
    url = get_url()
    qr  = qrcode.QRCode(version=None, box_size=10, border=2,
                        error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png", headers=NO_CACHE)


@app.post("/api/query")
async def api_query(req: Request):
    """HTTP query endpoint — same logic as ws_handler but returns JSON directly.
    Useful for automated tests that can't tolerate WebSocket session eviction."""
    body = await req.json()
    user_text = body.get("text", "").strip()
    if not user_text:
        return JSONResponse({"ok": False, "view": "error", "summary": "empty query"})

    # ── 取消（rewrite 之前先攔截，避免被改掉）──
    if user_text == "取消":
        _item_create_state.clear()
        _item_delete_state.clear()
        return JSONResponse({"ok": True, "summary": "已取消。", "view": "item_cancelled", "data": {}})

    user_text = _rewrite_query(user_text)
    user_text = _en_funcword_fix(user_text)   # EN 功能詞拼錯還原（同 WS 入口）

    # ── 刪除模式中 → 直接處理，跳過守門員 ──
    if _item_delete_state.get("active"):
        import tools_v2 as _tv2_del_http_mode
        _item_delete_state.clear()
        result = _tv2_del_http_mode.delete_item_start(keyword=user_text.strip())
        return JSONResponse(result)

    # ── 分步建立商品流程中 → 直接處理，跳過守門員 + clarify ──
    if _item_create_state.get("active"):
        if user_text.strip() == "取消":
            _item_create_state.clear()
            return JSONResponse({"ok": True, "summary": "已取消新增商品。", "view": "item_cancelled", "data": {}})
        import tools_v2 as _tv2_item
        st = _item_create_state
        kwargs = {**{k: v for k, v in st.items() if k in ("step", "name", "category", "price", "safety", "stock_north", "stock_central", "stock_south")}, "raw_text": ""}
        if st["step"] == 1: kwargs["name"] = user_text
        elif st["step"] == 2: kwargs["category"] = user_text
        elif st["step"] == 3:
            raw_ps = user_text.replace("元", " ").replace("件", " ").replace("，", ",")
            parts = [p.strip() for p in raw_ps.replace(" ", ",").split(",") if p.strip().lstrip("-").isdigit()]
            if len(parts) >= 2: kwargs["price"] = parts[0]; kwargs["safety"] = parts[1]
            elif len(parts) == 1: kwargs["price"] = parts[0]
            else: kwargs["price"] = user_text
        elif st["step"] == 4:
            if "跳過" in user_text or any(_sk in user_text.lower() for _sk in ("skip", "none", "zero", "no stock", "leave it")):
                kwargs["stock_north"] = kwargs["stock_central"] = kwargs["stock_south"] = "0"
            elif not any(kw in user_text for kw in ("北", "中", "南")):
                parts = user_text.replace(",", " ").split()
                nums = [p for p in parts if p.strip().lstrip("-").isdigit()]
                if len(nums) == 3:
                    kwargs["stock_north"], kwargs["stock_central"], kwargs["stock_south"] = nums[0], nums[1], nums[2]
            else:
                for part in user_text.replace("，", ",").split(","):
                    p = part.strip()
                    if "北" in p: kwargs["stock_north"] = p.replace("北", "").strip()
                    elif "中" in p: kwargs["stock_central"] = p.replace("中", "").strip()
                    elif "南" in p: kwargs["stock_south"] = p.replace("南", "").strip()
        result = _tv2_item.create_item_collect(**kwargs)
        if result.get("view") == "item_confirm":
            _item_create_state.clear()
        else:
            d = result.get("data", {})
            _item_create_state.update({k: v for k, v in d.items() if k in ("step", "name", "category", "price", "safety", "stock_north", "stock_central", "stock_south")})
            _item_create_state["active"] = True
        return JSONResponse(result)

    # ── 守門員（HTTP 版）──
    if not is_meaningful_input(user_text):
        return JSONResponse({"ok": False, "view": "rejected",
                             "summary": GATEKEEPER_REJECT_MSG})

    # ── 刪除/下架 → 優先處理（避免被 clarify 攔截）──
    _delete_kws_http = ("刪除", "下架", "砍掉", "移除", "刪掉")
    if any(w in user_text for w in _delete_kws_http):
        # 搗蛋守衛：要刪的是訂單/資料/別人的東西 → 拒絕（同 WS 端，conv100-r5）
        if any(w in user_text for w in ("訂單", "資料", "紀錄", "記錄", "帳號",
                                         "別人", "全部", "所有", "資料庫", "系統")):
            return JSONResponse({"ok": False, "view": "rejected",
                                 "summary": GATEKEEPER_REJECT_MSG})
        import tools_v2 as _tv2_del_http
        kw = _extract_sku_keyword(user_text)
        if not kw:
            for w in _delete_kws_http: kw = user_text.replace(w, "").strip()
        # 檢查 keyword 是否真的有對應商品
        import warehouse as _W_del_http
        _has_match = bool(_W_del_http.match_items(kw)) if kw else False
        if not kw or len(kw) < 2 or not _has_match:
            PROTECTED = {f"{p}{i:02d}" for p in "eafdcs" for i in range(1,11)}
            user_items = [it for it in _W_del_http.state().items if it["sku_id"] not in PROTECTED]
            if user_items:
                names = ", ".join(it["name"] for it in user_items[:10])
                result = {"ok": True, "summary": f"Deletable items: {names}\nPlease type the name of the item to delete", "view": "item_list",
                           "data": {"items": [{"name": it["name"], "sku": it["sku_id"]} for it in user_items]}}
                _item_delete_state["active"] = True
            else:
                result = {"ok": True, "summary": "No deletable items yet. Use \"add item\" to create one first.", "view": "item_list", "data": {}}
        else:
            result = _tv2_del_http.delete_item_start(keyword=kw)
        return JSONResponse(result)

    clarify = _detect_clarify(user_text)
    if clarify:
        return JSONResponse({"ok": True, "view": "clarify", **clarify})

    vid = body.get("vid", "api")

    # ── intent_clf 主要路由：分類器先決定 function，LLM 只抽 keyword ──
    _clf_func = None
    _clf_conf = 0.0
    try:
        _clf_func, _clf_conf = intent_clf.predict(user_text)
    except Exception:
        pass

    # 需要 keyword 的 function → 先抽 keyword
    _pre_kw = _extract_sku_keyword(user_text)

    _clf_skip_llm = False
    if _clf_func and _clf_func not in ("unknown", "unclear") and _clf_conf >= 0.8:
        log.info(f"[intent_clf primary] {user_text!r} → {_clf_func} (conf={_clf_conf:.2f})")
        func_name = _clf_func
        _needs_llm = func_name in ("manage_config", "run_script", "set_alert",
                                    "set_schedule", "generate_po", "generate_report",
                                    "query_movement", "compare_warehouses")  # 需要 LLM 抽參數
        if not _needs_llm:
            func_args = {}
            if func_name in ("query_inventory", "search_log", "query_related_items"):
                if _pre_kw and len(_pre_kw) >= 2:
                    func_args["keyword"] = _pre_kw
            elif func_name == "query_movement":
                func_args["period"] = "this_month"; func_args["direction"] = "both"
                # movement 不從 user_text 抽 keyword（容易誤抽時間/動作詞）
                # 讓 LLM 專門處理參數提取，或用 dispatch 補
            _clf_skip_llm = True
            log.info(f"[intent_clf primary] skip LLM, func={func_name} args={func_args}")

    if not _clf_skip_llm:
        try:
            prompt = build_prompt(user_text)
            # llm_lock 序列化所有對 LLM 物件的存取，避免 HTTP/WS 兩路徑並發呼叫
            # 同一個 llama_cpp 實例造成 KV cache 競爭、底層 GGML_ASSERT 崩潰。
            # 等待取得鎖本身也要有 timeout，否則若鎖被異常長時間佔用（例如另一
            # 請求卡住），這裡會無限期排隊、前端永遠停在 loading 狀態沒有提示。
            async with asyncio.timeout(40.0):
                async with llm_lock:
                    # 不 reset：llama-cpp 前綴快取讓相同 system_prompt（~1236 tok）
                    # 跳過重算，RPI5 首結果延遲 3.3s→~1.1s。temperature=0 貪婪
                    # 解碼輸出只由 prompt 決定，不 reset 無狀態污染（2026-07-04）。
                    r = await asyncio.wait_for(
                        asyncio.to_thread(
                            LLM, prompt,
                            max_tokens=160, temperature=0.0,
                            stop=["</s>", "<end_of_turn>", "<start_of_turn>"],
                            echo=False, stream=False,
                        ),
                        timeout=25.0,
                    )
        except (asyncio.TimeoutError, TimeoutError):
            return JSONResponse({"ok": False, "view": "error",
                                 "summary": "System is busy, please try again in a moment"})
        except Exception as e:
            return JSONResponse({"ok": False, "view": "error", "summary": str(e)})

        output = r["choices"][0]["text"].strip()
        parsed = parse_function_call(output)
        if not parsed:
            return JSONResponse({"ok": False, "view": "error", "summary": "parse_failed", "raw": output})

        func_name, func_args = parsed

    # search_log keyword pre-clean (both paths)
    if func_name == "search_log" and func_args.get("keyword"):
        pre_kw = _extract_sku_keyword(func_args["keyword"])
        if pre_kw:
            func_args = {**func_args, "keyword": pre_kw}

    # ── Pre-C-Schedule（HTTP 版）──
    _list_alert_kws_h = ("查看警示", "查警示", "有哪些警示", "目前警示", "現在警示",
                         # EN build：英文查警示規則清單
                         "my alerts", "show alerts", "list alerts", "view alerts",
                         "what alerts", "which alerts", "existing alerts",
                         "current alerts", "alert rules", "active alerts")
    _list_alert_rule_kw = "警示規則"  # 單獨處理，避免「新增警示規則」誤走 list
    _list_sched_kws_h = ("查看排程", "查排程", "看排程", "有哪些排程", "排程列表", "目前排程",
                         # EN build：英文查排程清單
                         "my schedules", "show schedules", "list schedules",
                         "view schedules", "what schedules", "which schedules",
                         "existing schedules", "current schedules",
                         "scheduled jobs", "scheduled tasks", "my schedule")
    # ⚠️ 英文比對要小寫化（詞表英文全小寫，訪客可能大寫開頭）
    user_text_l_h = user_text.lower()
    _is_alert_set = any(w in user_text for w in ("新增", "設定", "加入", "建立", "通知我", "提醒我")) \
        or any(w in user_text_l_h for w in ("add ", "set ", "create ", "notify me",
                                            "remind me", "alert me"))
    if (not _is_alert_set and
            (any(w in user_text for w in _list_alert_kws_h) or
             any(w in user_text_l_h for w in _list_alert_kws_h) or
             (_list_alert_rule_kw in user_text and not _is_alert_set))):
        func_name = "list_alerts"
        func_args = {}
    elif (any(w in user_text for w in _list_sched_kws_h)
          or any(w in user_text_l_h for w in _list_sched_kws_h)):
        func_name = "list_schedules"
        func_args = {}
    else:
        _sched_time_kws = ("每天", "每日", "天天", "每週", "每周", "每月", "定時", "排程", "固定時間",
                           "每天早上", "每天晚上", "每天中午", "自動執行", "自動跑")
        _sched_act_kws  = ("盤點", "匯出", "報告", "體檢", "腳本", "跑", "月報", "週報")
        if (any(w in user_text for w in _sched_time_kws) and
                any(w in user_text for w in _sched_act_kws)):
            if func_name != "set_schedule":
                func_name = "set_schedule"
                func_args = {"raw_text": user_text}
            else:
                # LLM 已判 set_schedule 但自己亂填 script_name/freq 時，
                # 一定要把原句帶給 tools 重新解析（freq/時間以原句明講的為準）
                func_args["raw_text"] = user_text

    # ── Pre-C10（HTTP 版）──
    _prec_skip = ("run_script", "set_schedule", "list_schedules", "delete_schedule",
                  "list_alerts", "delete_alert", "query_movement", "compare_warehouses")
    if func_name not in _prec_skip:
        _pre_script_kws = ("盤點", "匯出進出", "匯出記錄", "進出記錄", "體檢報告", "月底盤點")
        _pre_hit = next((w for w in _pre_script_kws if w in user_text), None)
        if _pre_hit:
            smap = {"盤點": "盤點", "月底盤點": "月底盤點", "匯出進出": "匯出",
                    "匯出記錄": "匯出", "進出記錄": "匯出", "體檢報告": "體檢報告"}
            func_name = "run_script"
            func_args = {"script_name": smap.get(_pre_hit, _pre_hit)}

    # ── Pre-C-Movement（HTTP 版）── rewrite 後的標準句 → query_movement（RCA 意圖優先）
    _movement_kws = ("查詢進出記錄", "進出記錄", "出貨了多少", "上週進了多少", "最近30天出貨",
                     "進貨記錄", "出貨記錄", "入庫記錄", "移動記錄")
    _has_rca_kw = _has_rca_word(user_text)
    if (not _has_rca_kw and
            func_name != "query_movement" and
            func_name not in ("run_script", "set_schedule", "list_schedules") and
            any(w in user_text for w in _movement_kws)):
        func_name = "query_movement"
        func_args = {"period": "this_month", "direction": "both"}

    # ── Pre-C-Compare（HTTP 版）── rewrite 後的標準句 → compare_warehouses
    _compare_kws = ("比較各倉庫庫存", "各倉庫比較", "三個倉庫比較", "北中南倉",
                    "倉庫比較", "倉庫對比", "比較倉庫",
                    "三個倉", "三倉", "各倉", "每個倉", "哪個倉", "哪一倉")
    # 句中有具體商品名時不攔——「牛仔長褲各倉還有幾條」是查該商品的分倉庫存，
    # 不是倉庫比較（第13輪抓到：加「各倉」後誤劫帶商品名的查詢）
    _cmp_kw_prod = _extract_sku_keyword(user_text)
    import warehouse as _W_cmp
    _cmp_has_prod = bool(_cmp_kw_prod and _W_cmp.match_items(_cmp_kw_prod))
    if (func_name != "compare_warehouses" and
            func_name not in ("run_script", "set_schedule", "list_schedules") and
            not _cmp_has_prod and
            any(w in user_text for w in _compare_kws)):
        func_name = "compare_warehouses"
        func_args = {}

    # ── Pre-C-Alert-Set（HTTP 版）── rewrite 後的標準句 → set_alert
    _alert_set_kws = ("新增庫存警示規則", "設定缺貨警示", "設定警示", "新增警示",
                      "庫存不足時提醒", "低於安全庫存通知")
    if (func_name not in ("list_alerts", "delete_alert", "set_alert") and
            any(w in user_text for w in _alert_set_kws)):
        func_name = "set_alert"
        func_args = {"raw_text": user_text}

    # correct（先校正，OOV 才能對正確的 func_name/keyword 做判斷）
    func_name, func_args, _hard = _correct_function_call(user_text, func_name, func_args)

    # Context carry-over：追問句補 keyword/warehouse（按訪客 vid 隔離）
    func_name, func_args = _resolve_followup(vid, user_text, func_name, func_args)

    # C18
    mismatch, clf_intent, clf_conf = intent_clf.check_mismatch(user_text, func_name)
    if mismatch and not _hard and clf_intent != "unknown":
        func_name = clf_intent

    # OOV（在校正後才跑，避免誤攔 RCA keyword）
    oov_hint = ""
    oov = _detect_oov(func_name, func_args)
    if oov:
        if oov.get("auto_fix"):
            func_args = {**func_args, "keyword": oov["fixed_keyword"]}
            oov_hint = f"（已自動修正：{oov['original_keyword']} → {oov['fixed_keyword']}）"
        else:
            return JSONResponse({"ok": True, "view": "clarify", **oov})

    # ── dispatch 前最後防線：keyword 其實是類別名 → 轉 category ──
    # 同時處理 category 已被 enum 容錯轉換但 keyword 殘留的情況
    _CAT_FALLBACK = {
        "電子產品": "electronics", "家電廚具": "appliance_kitchen",
        "食品飲料": "food_beverage", "日用品": "daily_goods",
        "服飾": "apparel", "運動用品": "sports",
    }
    if func_name == "query_inventory":
        _dkw = (func_args.get("keyword") or "").strip()
        _dcat = func_args.get("category", "")
        # 先剝掉常見前後綴雜訊，取純類別名
        _dkw_clean = _dkw
        # 剝前綴（倉庫名 + 動作詞）
        for _pfx in ("北區倉的", "中區倉的", "南區倉的", "北倉的", "中倉的", "南倉的",
                     "北區的", "中區的", "南區的", "北部的", "中部的", "南部的",
                     "查", "看一下", "看", "查一下"):
            if _dkw_clean.startswith(_pfx):
                _dkw_clean = _dkw_clean[len(_pfx):].strip()
                break
        # 剝常見後綴
        for _sfx in ("類別", "庫存查詢", "庫存", "查詢", "類", "詢"):
            if _dkw_clean.endswith(_sfx) and len(_dkw_clean) > len(_sfx) + 1:
                _dkw_clean = _dkw_clean[:-len(_sfx)].strip()
                break
        # case A: keyword 是類別名且 category 未設 → 轉成 category 查詢
        #   避免誤轉商品名（如「運動毛巾」含「運動」但不該變類別）
        if _dkw and _dcat not in VALID_CATEGORIES:
            import warehouse as _W_dispatch
            _dispatch_names = [it["name"] for it in _W_dispatch.state().items]
            _dispatch_kw_is_product = any(_dkw_clean in n or n in _dkw_clean for n in _dispatch_names)
            if not _dispatch_kw_is_product:
                for _zh, _en in sorted(_CAT_FALLBACK.items(), key=lambda x: -len(x[0])):
                    if _zh in _dkw_clean or _dkw_clean in _zh:
                        log.info(f"[dispatch] 類別轉換: kw={_dkw!r} → category={_en}")
                        func_args = {k: v for k, v in func_args.items() if k != "keyword"}
                        func_args["category"] = _en
                        break
        # case B: category 已設但 keyword 是純類別名（enum 容錯修完 category 但 keyword 殘留）
        elif _dkw and _dcat in VALID_CATEGORIES:
            for _zh in _CAT_FALLBACK:
                if _zh in _dkw_clean or _dkw_clean in _zh:
                    log.info(f"[dispatch] 關鍵字是類別名，清掉 kw={_dkw!r} 保留 cat={_dcat}")
                    func_args = {k: v for k, v in func_args.items() if k != "keyword"}
                    break

    # ── dispatch 前最後攔截：LLM 常見誤判 pattern → 強制修正 ──
    _stock_question_kws = ("還有嗎", "還有貨嗎", "有沒有貨", "夠不夠", "還夠嗎", "有貨嗎",
                           "有沒有", "還有沒有", "會缺貨嗎", "快沒了嗎", "有嗎", "還有嗎",
                           "有貨嗎", "現貨嗎", "有現貨嗎", "有庫存嗎")
    _movement_kws  = ("出了多少", "進了哪些", "進了什麼", "進了多少", "出貨狀況", "進貨狀況",
                      "進出狀況", "出多少貨", "進多少貨", "出貨多少", "進貨多少")
    _hot_kws       = ("賣最好", "賣最差", "熱賣", "暢銷", "滯銷", "賣得", "銷量")
    _low_kws       = ("快沒了", "快斷貨", "快缺貨", "不夠了", "要補貨", "需要補", "缺貨了")

    if func_name in ("search_log",) and any(w in user_text for w in _stock_question_kws):
        _sq_kw = _extract_sku_keyword(user_text) or func_args.get("keyword", "")
        if _sq_kw and len(_sq_kw) >= 2:
            log.info(f"[dispatch] 庫存問句攔回: {user_text!r} → query_inventory(kw={_sq_kw!r})")
            func_name = "query_inventory"
            func_args = {"keyword": _sq_kw}

    if func_name in ("search_log", "query_inventory") and any(w in user_text for w in _movement_kws):
        _mv_kw = _extract_sku_keyword(user_text) or ""
        _mv_period = "this_week" if any(w in user_text for w in ("這禮拜","這週","本週")) else \
                     "this_month" if any(w in user_text for w in ("本月","這個月")) else \
                     "today" if any(w in user_text for w in ("今天","今日")) else None
        log.info(f"[dispatch] 進出記錄攔回: {user_text!r} → query_movement")
        func_name = "query_movement"
        func_args = {"period": _mv_period or func_args.get("period", "this_month"), "direction": "both"}
        if _mv_kw:
            func_args["keyword"] = _mv_kw

    if func_name not in ("list_hot_items",) and any(w in user_text for w in _hot_kws):
        log.info(f"[dispatch] 熱銷攔回: {user_text!r} → list_hot_items")
        func_name = "list_hot_items"
        func_args = {"rank_type": "hot", "period": "this_week"}

    if func_name not in ("list_low_stock",) and any(w in user_text for w in _low_kws):
        log.info(f"[dispatch] 低庫存攔回: {user_text!r} → list_low_stock")
        func_name = "list_low_stock"
        func_args = {}

    # ── dispatch 攔截：「刪除/下架商品」→ delete_item 流程 ──
    _delete_item_kws = ("刪除", "下架", "砍掉", "移除商品", "刪掉",
                        # EN build：英文刪除商品觸發詞
                        "delete item", "delete the item", "remove item",
                        "remove the item", "delete product", "remove product",
                        "take down item", "discontinue")
    if any(w in user_text for w in _delete_item_kws):
        import tools_v2 as _tv2_del
        kw = _extract_sku_keyword(user_text)
        if not kw:
            for w in _delete_item_kws: kw = user_text.replace(w, "").strip()
        result = _tv2_del.delete_item_start(keyword=kw)
        return JSONResponse(result)

    # ── dispatch 攔截：「列出所有商品/商品清單」→ 全商品列表 ──
    # 「中倉全部商品安全庫存改成六十」這種 config 句含「全部商品」會被劫走，
    # 含設定關鍵字時不攔（第11輪抓到）
    if (any(w in user_text for w in ("所有商品", "商品列表", "商品清單", "全部商品", "列出商品", "商品名稱"))
            and not any(w in user_text for w in _CONFIG_KEY_WORDS)):
        import warehouse as _W_list
        snap = _W_list.state()
        rows = [{"sku": it["sku_id"], "name": it["name"],
                 "category": _W_list.CATEGORY_LABEL.get(it["category"], it["category"]),
                 "price": it["unit_price"], "safety": it["safety_stock"]}
                for it in snap.items]
        summary = f"共 {len(rows)} 項商品：\n" + "\n".join(f"  {r['sku']} {r['name']} ({r['category']}) NT${r['price']}" for r in rows)
        return JSONResponse({"ok": True, "view": "item_list", "summary": summary,
                             "data": {"total": len(rows), "items": rows}})

    # ── dispatch 攔截：「新增商品」→ 分步引導流程 ──
    _create_item_kws = ("新增商品", "建立商品", "加一個商品", "新增一個", "加入商品", "增加商品", "新建商品",
                          # EN build：英文新增商品觸發詞（原表全中文 → 英文訪客
                          #   打 "add item" 完全進不了流程，還被守門員擋成 rejected）
                          "add item", "add a item", "add an item", "add new item",
                          "add a new item", "create item", "create a item",
                          "create an item", "create a new item", "new item",
                          "new product", "add product", "add a product",
                          "register item", "register a new item")
    if any(w in user_text for w in _create_item_kws):
        import tools_v2 as _tv2
        log.info(f"[dispatch] 新增商品攔截: {user_text!r}")
        raw = user_text
        for kw in _create_item_kws: raw = raw.replace(kw, "").strip()
        result = _tv2.create_item_collect(step=1, raw_text=raw) if raw else _tv2.create_item_start()
        if result.get("view") != "item_confirm":
            d = result.get("data", {})
            _item_create_state.update({k: v for k, v in d.items()
                if k in ("step", "name", "category", "price", "safety", "stock_north", "stock_central", "stock_south")})
            _item_create_state["active"] = True
        return JSONResponse(result)
    if _item_create_state.get("active"):
        st = _item_create_state
        log.info(f"[dispatch] item_create step {st['step']}: {user_text!r}")
        import tools_v2 as _tv2
        kwargs = {**st, "raw_text": ""}
        if st["step"] == 1:
            kwargs["name"] = user_text
        elif st["step"] == 2:
            kwargs["category"] = user_text
        elif st["step"] == 3:
            parts = user_text.replace("，", ",").split(",")
            if len(parts) >= 2:
                kwargs["price"] = parts[0].strip()
                kwargs["safety"] = parts[1].strip()
            else:
                kwargs["price"] = user_text
        elif st["step"] == 4:
            if "跳過" in user_text or any(_sk in user_text.lower() for _sk in ("skip", "none", "zero", "no stock", "leave it")):
                kwargs["stock_north"] = kwargs["stock_central"] = kwargs["stock_south"] = "0"
            else:
                for part in user_text.replace("，", ",").split(","):
                    p = part.strip()
                    if "北" in p: kwargs["stock_north"] = p.replace("北", "").strip()
                    elif "中" in p: kwargs["stock_central"] = p.replace("中", "").strip()
                    elif "南" in p: kwargs["stock_south"] = p.replace("南", "").strip()
        result = _tv2.create_item_collect(**kwargs)
        if result.get("view") == "item_confirm":
            _item_create_state.clear()
        else:
            d = result.get("data", {})
            _item_create_state.update({k: v for k, v in d.items() if k in ("step", "name", "category", "price", "safety", "stock_north", "stock_central", "stock_south")})
            _item_create_state["active"] = True
        return JSONResponse(result)

    # ── dispatch 攔截：「哪個最多/庫存排行」→ list_hot_items stock ──
    # 「哪個」單字太寬，會誤傷「北倉跟南倉哪個庫存多」這類倉庫比較句（compare_warehouses）。
    # 判別特徵：兩倉比較句一定會提到「倉」（北倉/南倉/幾個倉），單一商品排行榜問句不會提「倉」。
    _stock_rank_kws = ("哪個", "哪個東西", "庫存最多", "數量最多", "哪個最多", "存貨最多", "東西最多")
    if (any(w in user_text for w in _stock_rank_kws)
            and not any(w in user_text for w in ("熱銷", "賣", "排行", "hot", "滯銷",
                                                  "業績", "冠軍", "銷", "墊底"))
            and not any(w in user_text for w in ("倉", "北區", "中區", "南區"))):
        log.info(f"[dispatch] 庫存排行攔截: {user_text!r} → list_hot_items(stock)")
        func_name = "list_hot_items"
        func_args = {"rank_type": "stock"}

    # ── dispatch 攔截：「那個XX」被 intent_clf 誤判 query_related_items / search_log ──
    _descriptive_kws = ("的那個", "用的那個", "的那台", "的那個", "用的", "刷牙", "擦身體", "洗衣服")
    if func_name in ("query_related_items", "search_log") and any(w in user_text for w in _descriptive_kws):
        _dk = _extract_sku_keyword(user_text)
        if _dk and len(_dk) >= 2:
            log.info(f"[dispatch] 描述性查詢攔回 inventory: {user_text!r} kw={_dk!r}")
            func_name = "query_inventory"
            func_args = {"keyword": _dk}

    # ── dispatch 攔截：「幫我查一下XX的庫存好嗎」被誤判 search_log ──
    if func_name == "search_log" and any(w in user_text for w in ("庫存好嗎", "的庫存", "庫存量", "幫我查一下", "查一下")):
        _dk = _extract_sku_keyword(user_text)
        if _dk and len(_dk) >= 2:
            log.info(f"[dispatch] 庫存查詢句攔回: {user_text!r} kw={_dk!r}")
            func_name = "query_inventory"
            func_args = {"keyword": _dk}

    # ── dispatch 攔截：「XX墊子/補貨了」被誤判 query_related_items ──
    if func_name == "query_related_items" and not any(w in user_text for w in ("買", "連帶", "一起買", "還會買", "搭配")):
        _dk = _extract_sku_keyword(user_text)
        if _dk and len(_dk) >= 2:
            import warehouse as _WR
            # 確認沒有明顯的 related 意圖 → 攔回 inventory
            if not any(w in user_text for w in ("買", "連帶", "一起", "搭配", "相關", "帶動", "順便")):
                log.info(f"[dispatch] 無連帶意圖攔回: {user_text!r} kw={_dk!r}")
                func_name = "query_inventory"
                func_args = {"keyword": _dk}

    # ── dispatch：compare_warehouses 清理非法參數 + 補預設倉庫 ──
    if func_name == "compare_warehouses":
        func_args = {k: v for k, v in func_args.items()
                     if k in ("warehouse_a", "warehouse_b", "metric")}
        if "warehouse_a" not in func_args:
            func_args["warehouse_a"] = "north"
        if "warehouse_b" not in func_args:
            func_args["warehouse_b"] = "south"

    # ── dispatch 攔截：movement 關鍵字清理 + 自動提取 ──
    if func_name == "query_movement":
        import warehouse as _WM2
        _mv_kw = func_args.get("keyword", "")
        # 清理髒 keyword
        if _mv_kw and not _WM2.match_items(_mv_kw):
            func_args = {k: v for k, v in func_args.items() if k != "keyword"}
            _mv_kw = ""
        # 沒有 keyword → 從 user_text 提取
        if not _mv_kw or not func_args.get("keyword"):
            _extracted = _extract_sku_keyword(user_text)
            if _extracted and len(_extracted) >= 2 and _WM2.match_items(_extracted):
                func_args["keyword"] = _extracted

    # dispatch — same as ws_handler
    # 執行前清理 keyword 前後綴雜訊（LLM 常把「有/的/剩/幾個」黏在 keyword 上）
    _kw_field = "keyword" if "keyword" in func_args else ("target" if "target" in func_args else None)
    if _kw_field and func_args.get(_kw_field):
        _raw_kw = func_args[_kw_field]
        _pfx_list = ("幫我查","幫我看","幫我找","查看","查詢","查一下","看看","有沒有","有","是","了","也","還","的")
        _sfx_list = ("有多少","剩多少","有幾個","剩幾個","有幾","剩幾","有","剩","的","嗎","啊","呢","吧","了","喔")
        _ck = _raw_kw
        for p in sorted(_pfx_list, key=len, reverse=True):
            if _ck.startswith(p) and len(_ck) > len(p) + 1:
                _ck = _ck[len(p):]; break
        for s in sorted(_sfx_list, key=len, reverse=True):
            if _ck.endswith(s) and len(_ck) > len(s) + 1:
                _ck = _ck[:-len(s)]; break
        if len(_ck) < 2:
            _ck = ""    # 清掉單字雜訊，讓 warehouse 走全倉概覽
        if _ck != _raw_kw:
            log.info(f"[dispatch] keyword 清理: 「{_raw_kw}」→「{_ck}」")
            func_args = {**func_args, _kw_field: _ck}
    # ── 意圖閘門（HTTP 版，同 WS）──
    if not _tool_intent_ok(func_name, user_text):
        # reject 前先試降級救援（口語前綴害 LLM 輸出錯 function，RPI5 v21）
        _rescue = _intent_guard_rescue(func_name, func_args, user_text)
        if _rescue:
            func_name, func_args = _rescue
        else:
            log.info(f"[gate] {func_name} 缺意圖詞 → rejected: {user_text!r}")
            return JSONResponse({"ok": False, "view": "rejected", "summary": GATEKEEPER_REJECT_MSG})
    result = finance.execute(func_name, func_args)
    if isinstance(result, dict) and result.get("ok"):
        _update_ctx(vid, func_name, func_args)
    # ── 參數錯誤時，從 user_text 推測正確意圖 → clarify ──
    if isinstance(result, dict) and not result.get("ok") and "unexpected keyword" in str(result.get("summary", "")):
        log.info(f"[dispatch] 參數錯誤 {func_name}: {result['summary']!r} → clarify")
        # EN build：這是**語言無關**的錯誤路徑（參數對不上就會走到），英文句
        #   到得了 → 訊息與選項全給英文（選項送回後端，中文會被守門員 reject）。
        if _is_mostly_english(user_text):
            _hint_q = "What would you like to check?"
            _hint_opts = ["whats running low", "whats expiring soon",
                          "best sellers this week", "any stock discrepancies"]
            if _re.search(r"\b(?:which|compare|more|less|most|least|versus|vs)\b",
                          user_text, _re.I):
                _hint_q = "Do you want to compare warehouses, or see a stock ranking?"
                _hint_opts = ["compare north and south warehouse",
                              "best sellers this month", "all items stock"]
            result = {"ok": True, "view": "clarify", "question": _hint_q,
                      "options": _hint_opts,
                      "hint": "Tap an option, or type a more complete question",
                      "data": {}}
        else:
            _hint_q = "你是想查什麼？"
            _hint_opts = ["哪些商品快缺貨", "哪些商品快到期", "本週熱銷商品", "採購對帳異常"]
            # 從 user_text 推測
            if any(w in user_text for w in ("哪個", "哪", "比較", "比", "多", "少")):
                _hint_q = "你是想比較倉庫、還是查庫存排行？"
                _hint_opts = ["北倉跟南倉庫存比較", "本月熱銷排行", "查全部庫存"]
            result = {"ok": True, "view": "clarify", "question": _hint_q, "options": _hint_opts,
                      "hint": "輸入數字選擇，或直接輸入更完整的問題", "data": {}}

    if isinstance(result, dict):
        result["_function"] = func_name
        _res_kw = func_args.get("keyword", "") or func_args.get("target", "")
        if _res_kw and isinstance(result.get("data"), dict) and "keyword" not in result["data"]:
            result["data"]["keyword"] = _res_kw
    if oov_hint and isinstance(result, dict) and result.get("summary"):
        result["summary"] = oov_hint + result["summary"]

    return JSONResponse(result)


# ─── 警示規則 REST API ────────────────────────────────────
@app.get("/api/alerts")
async def get_alerts():
    """列出所有警示規則。"""
    from tools_v2 import _data_dir
    import json as _json
    try:
        dd = _data_dir()
        rules_path = dd / "alert_rules.json"
        if not rules_path.exists():
            return JSONResponse({"rules": []})
        rules = _json.loads(rules_path.read_text("utf-8")).get("rules", [])
        _cond_labels = {"below_safety": "below safety stock", "out_of_stock": "out of stock",
                        "expiring": "expiring soon", "below_threshold": "below a set quantity"}
        for r in rules:
            r["condition_label"] = _cond_labels.get(r["condition"], r["condition"])
            r["scope_txt"] = "all items" if not r.get("scope_names") else ", ".join(r["scope_names"][:3])
        return JSONResponse({"rules": rules}, headers=NO_CACHE)
    except Exception as e:
        return JSONResponse({"rules": [], "error": str(e)})


@app.delete("/api/alerts/{rule_id}")
async def delete_alert_api(rule_id: str):
    """刪除指定警示規則並推 WebSocket 更新。"""
    from tools_v2 import _data_dir
    import json as _json
    try:
        dd = _data_dir()
        rules_path = dd / "alert_rules.json"
        if not rules_path.exists():
            return JSONResponse({"ok": False, "error": "找不到規則檔"}, status_code=404)
        data = _json.loads(rules_path.read_text("utf-8"))
        rules = data.get("rules", [])
        new_rules = [r for r in rules if r["id"] != rule_id]
        if len(new_rules) == len(rules):
            return JSONResponse({"ok": False, "error": f"找不到 {rule_id}"}, status_code=404)
        rules_path.write_text(_json.dumps({"rules": new_rules}, ensure_ascii=False, indent=2), encoding="utf-8")
        await push_display({"type": "alert_deleted", "rule_id": rule_id})
        log.info(f"[alert] 規則 {rule_id} 已刪除")
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ─── 排程 REST API ────────────────────────────────────────
@app.get("/api/schedules")
async def get_schedules():
    from tools_v2 import _data_dir
    import json as _json
    try:
        dd = _data_dir()
        p = dd / "schedule_jobs.json"
        if not p.exists():
            return JSONResponse({"jobs": []})
        jobs = _json.loads(p.read_text("utf-8")).get("jobs", [])
        return JSONResponse({"jobs": jobs}, headers=NO_CACHE)
    except Exception as e:
        return JSONResponse({"jobs": [], "error": str(e)})


@app.delete("/api/schedules/{job_id}")
async def delete_schedule_api(job_id: str):
    from tools_v2 import _data_dir
    import json as _json
    try:
        dd = _data_dir()
        p = dd / "schedule_jobs.json"
        if not p.exists():
            return JSONResponse({"ok": False, "error": "找不到排程檔"}, status_code=404)
        data = _json.loads(p.read_text("utf-8"))
        jobs = data.get("jobs", [])
        new_jobs = [j for j in jobs if j["id"] != job_id]
        if len(new_jobs) == len(jobs):
            return JSONResponse({"ok": False, "error": f"找不到 {job_id}"}, status_code=404)
        p.write_text(_json.dumps({"jobs": new_jobs}, ensure_ascii=False, indent=2), encoding="utf-8")
        await push_display({"type": "schedule_deleted", "job_id": job_id})
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/alerts/check")
async def trigger_alert_check():
    """立即觸發一次警示掃描（不等排程）。"""
    await _check_alert_rules()
    return JSONResponse({"ok": True})


@app.post("/reset")
async def reset():
    finance.reset()
    snap = finance.dashboard_snapshot()
    await push_display({"type": "reset", "snapshot": snap})
    log.info("已重置快照")
    return JSONResponse({"ok": True, "snapshot": snap}, headers=NO_CACHE)


# ── 語音辨識（whisper.cpp tiny.en 本地跑，展場離線可用）────────────
#   為何不用瀏覽器 Web Speech API：那會連 Google 雲端，展場沒網路就死。
#   前端錄音 → POST webm/wav → ffmpeg 轉 16k mono → whisper-cli → 回文字。
#   缺任一組件（binary/模型/ffmpeg）→ 回 ok:false + reason，
#   前端據此回退到瀏覽器內建辨識，不讓展場整組壞掉。
#
#   ── EN build：為何從 Fun-ASR-Nano 換成 whisper ──────────────────
#   ① **來源約束**（user 定調）：Fun-ASR 是阿里（FunAudioLLM）的，
#      英文版只用歐美模型。whisper 是 OpenAI，whisper.cpp 生態成熟。
#   ② **原本英文根本不會成功**：舊取字邏輯是「取最後一行**含中文**的輸出」，
#      英文辨識結果不含中文字 → text 恆空 → 一律回「聽不出內容」。
#   ③ **實測 tiny.en 勝過 base.en**（RPI5 選型：0.94s/WER 9.3% vs
#      2.33s/WER 10.2%）——倉管查詢句短、句型固定，tiny 容量已夠，
#      模型變大的收益顯現不出來。比舊的 Fun-ASR（2.45s）快 2.6 倍。
#   ④ **2026-07-31：非母語腔實測後改用 small-q5_0 + audio-ctx**
#      user 錄 15 句真人英文（台灣口音）實測，同一批句子：
#        | 模型                  | 通過 | ASR 延遲 |
#        | tiny.en（原）         | 27% | 0.95s   |
#        | base 多語             | 33% | 2.07s   |
#        | small.en              | 40% | 6.69s   |
#        | small 多語            | 53% | 6.66s   |
#        | **small-q5_0 + ac640**| **60%** | **3.45s** |
#      **兩個反直覺發現**：
#      ⓐ **多語版打敗英文專用版**（small 53% vs small.en 40%）——多語模型
#        訓練時看過大量非母語者講的英文，對台灣口音反而更寬容。
#      ⓑ **量化單獨用會變慢**（6.66→8.30s，ARM 無對應加速指令、即時解量化
#        費時；官方說法「量化只加速 decoder、encoder 反而更慢」），但
#        **搭配 -ac 就變成加分**——ac 削掉 encoder 成本後，decoder 的優勢
#        才顯現出來。
#   ⑤ **`-ac`（audio-ctx）是速度關鍵**：whisper 預設處理 **30 秒**上下文
#      （1500 token），但倉管查詢句只有 1.3-2.9 秒，其餘全是浪費的 encoder
#      運算。ac=640 → 6.66s 降到 2.86s（快 2.3 倍）且準確度零損失。
#      ⚠️ **ac 不能太小**：ac=256 雖只要 1.64s，但上下文太短會讓模型
#        **循環重複輸出**（'Wireless mouse counter' 重複 9 次）。640 是安全下限。
#      ⚠️ **超過容量會靜默截斷**（不報錯）：容量 = ac × 30/1500 秒，
#        ac=640 → **12.8 秒**。前端 MAXLEN 已配合改為 12 秒（見 index.html）。
#   ⚠️ 切回舊模型：`WAREHOUSE_ASR_MODEL=tiny.en systemctl restart ...`
#      或直接改下面的預設值。ac 用 `WAREHOUSE_ASR_AC`（0 = 不限，走完整 30 秒）。
_VOICE_DIR = Path.home() / "whisper.cpp"
_VOICE_CLI = _VOICE_DIR / "build/bin/whisper-cli"
_ASR_MODEL_NAME = os.getenv("WAREHOUSE_ASR_MODEL", "small-q5_0")
_VOICE_MODEL = _VOICE_DIR / f"models/ggml-{_ASR_MODEL_NAME}.bin"
#   audio-ctx：0 或空 = 不加 -ac（完整 30 秒上下文，最慢）
_ASR_AUDIO_CTX = os.getenv("WAREHOUSE_ASR_AC", "640").strip()

# EN build：不再需要 OpenCC（s2twp 是簡轉繁+台灣用語，對英文無意義）。
#   舊版把它列進 _voice_ready 的必要條件 → 沒裝 opencc 會讓英文語音
#   整組回報不可用，那是中文版遺留的相依。


# ── 語音專用：寫入動詞/倉別同音正規化 ────────────────────────────
#   為何需要：ASR 把「進」聽成「近」、「倉」聽成「昌/蒼/槍」，整句就掉出
#   寫入路徑 → 訪客以為進貨了其實只是查詢（展場最尷尬的失敗）。實測 12 句
#   ASR 錯字寫入句只有 1 句能正確開卡。
#
#   ⚠️ 只掛在 /api/asr 出口，不碰 warehouse.py —— 打字訪客完全不受影響、
#   守衛零風險。商品名錯字交給既有發音容錯層（華數→滑鼠已能救）。
#
#   ⚠️ 規則從嚴、限定上下文（守衛語料實測邊界）：
#     ①「近」→「進」只在後接【數字+量詞】時（守衛的「最近一個月/最近有進
#        什麼貨」是時間詞，不接量詞 → 不誤傷；14+ 條含「最近」的句子安全）
#     ②「掉」→「調」只在後接【數量 … 給/到 X倉】時（守衛「刪掉排程」「庫存
#        掉一半」無調撥目標 → 不誤傷）
#     ③ 昌/蒼/槍 → 倉 只在【北/南/中/東/西 + 該字】時（三字在守衛出現 0 次）
_ASR_NUM = r"[0-9０-９一二三四五六七八九十百千兩]"
_ASR_UNIT = r"[個件台臺箱包盒支瓶罐組雙頂條捲張片袋]"

_ASR_FIX = [
    # ── EN build：whisper 一律輸出**首字大寫＋專有名詞大寫＋句末標點**
    #   （`Transfer 20 Bluetooth earphones from North to Central.`），而
    #   訪客打字是小寫 → **系統裡 21 個詞表的英文詞全是小寫**，大小寫敏感
    #   的比對一律落空。實測後果（都是真的踩到）：
    #     `Transfer …` → `_ALL_INTENT_WORDS` 不中 → has_intent=False
    #                    → 判成「只有商品名沒動作」轉 clarify，
    #                    **校正層 C13a 根本執行不到**（小寫同句正常開卡）
    #     `What else do …` → `_TOOL_INTENT_GUARD` 的連帶詞不中
    #                    → gate-rescue 把 query_related 降級成庫存查詢
    #   兩個關鍵閘門（`_detect_clarify` / `_tool_intent_ok`）已各自改成
    #   同時比對小寫（治本，打字訪客打大寫也受惠）；這裡再做一次出口正規化
    #   當**全面保險**，涵蓋其餘 19 個詞表。
    #   ⚠️ 全小寫安全：`match_items` 早已 `.lower()` 比對（英文化時修的
    #   大小寫 bug），商品名 USB-C / LED / 1kg 全小寫仍對得到。
    #   ⚠️ 只掛 /api/asr 出口，打字路徑完全不受影響（同中文版設計）。
    #   實作在下面的 `_asr_normalize`（開頭就處理，不放進這張中文規則表）。
    # ③ 倉別同音（最高頻錯誤：實測 20 句錯 5 次）——限定方位詞後
    #   「藏」是真人聲實測補的（合成音從沒產生過；user 唸「北倉」→「北藏」）。
    #   守衛/sweep 語料含「藏」皆 0 次 → 安全。
    (_re.compile(r"([北南中東西])[昌蒼槍倉艙藏](?=[^庫]|$)"), r"\1倉"),
    # r97 真人聲實測：「中」zhong 特別不穩，「中倉」被聽成「總」zong 倉（捲舌
    #   zh/z 混淆）。→「總倉」還原成「中倉」。限定不碰「總倉庫/總倉儲」
    #   （那是「全部倉庫」的合理語意，不是倉別）。
    (_re.compile(r"總倉(?![庫儲])"), "中倉"),
    # ① 進貨動詞——限定「近 + 數字 + 量詞」
    #   ⚠️「最近一個月/近三天」是時間詞不是數量：排除「最近」前綴，且量詞後
    #   不可接時間單位（守衛「最近一個月進貨多少」曾被誤改成「最進一個月」）
    (_re.compile(rf"(?<!最)近(?={_ASR_NUM}+{_ASR_UNIT}(?![月週周日天年季]))"), "進"),
    # ①b「近了」形（北倉近了五十個）
    (_re.compile(rf"(?<!最)近(?=了{_ASR_NUM})"), "進"),
    # ①c「補」在 heavy 噪音下被聽成「谷」（實測「中倉補一百個衛生紙」→「中倉谷」）。
    #   非同音（bu vs gu）而是吵雜環境的辨識劣化 → 同樣限定「+數字+量詞」才換。
    #   守衛/sweep 含「谷」皆 0 次、商品名無「谷」→ 安全。
    (_re.compile(rf"谷(?={_ASR_NUM}+{_ASR_UNIT})"), "補"),
    # ② 調撥動詞——限定後面有「給/到 X倉」
    (_re.compile(rf"掉(?={_ASR_NUM}+{_ASR_UNIT}?[^，。]{{0,10}}[給到][北南中東西]倉)"), "調"),
    # ④ 量詞異體字「臺」→「台」——OpenCC s2twp 會把「台」轉成「臺」，但
    #   server 各處量詞字元類只收「台」→ 語音路徑必踩（實測 w04「二十臺藍牙
    #   喇叭」開不出卡、「二十台」正常）。只在數字後才換，不動「臺灣/舞臺」。
    (_re.compile(rf"(?<={_ASR_NUM})臺"), "台"),
    # ⑤「到齊」→「到期」——全語音驗收抓到：「它快到期嗎」被聽成「快到齊嗎」
    #   → rejected（訪客問到期卻被當搗蛋）。「到齊」在守衛出現 0 次、「到期」
    #   17 次 → 安全。不用更寬的「齊→期」，避免碰到「備齊/湊齊/到齊了嗎」等
    #   正常語意；也不動「快到了」（守衛「冬天快到了」是合法句）。
    (_re.compile(r"到齊"), "到期"),
]


# ── 英文 ASR 出口修正表（真人錄音批建立）──────────────────────────
#   user 錄 38 句真人英文（台灣腔）實測歸納出的**穩定錯法**。
#   ⚠️ 只收「有規律 + 不會誤傷」的三類，音近詞（south→sauce、
#     mop→monk's）**刻意不收**——那要靠上下文判斷，硬修會誤配。
#   ⚠️ 一律用詞界 \b：坑 1 的教訓（短字串在英文必誤爆）。
_ASR_FIX_EN = [
    # ① 寫入動詞的**詞尾被吃掉**（最高頻、最有規律）
    #    shipped→shed / received→receive / send→sen
    (_re.compile(r"\bshed\b", _re.I), "shipped"),
    (_re.compile(r"\bsen\b", _re.I), "send"),
    (_re.compile(r"\breceive\b(?=\s+\d)", _re.I), "received"),
    (_re.compile(r"\bship\b(?=\s+out\b)", _re.I), "ship"),   # 保留（本來就對）
    # ② 商品名的固定聽錯（**已驗證主檔有對應商品**才收）
    (_re.compile(r"\bpower bands\b", _re.I), "power banks"),
    (_re.compile(r"\byoga mess\b", _re.I), "yoga mats"),
    (_re.compile(r"\belectric mass\b", _re.I), "electric mops"),
    # ③ 倉別聽錯（三個倉名是封閉集合，可安全修）
    (_re.compile(r"\bsauce\b(?=\s+got|\s+received|\s+shipped)", _re.I), "south"),
    # ④ alert 的**詞首**被聽錯（真人錄音 2026-08-02，第 46 句連唸 5 次全錯）
    #    5 次分別聽成：allow(2) / lock / other / and-the-microphone，
    #    但句尾 `earphones drop below 30` **每次都對** ⇒ 只有第一個字錯。
    #    ① 限定「後接 me when」——該句型在倉管語境只有 alert 一種合理解釋
    #    ② 誤傷檢查：守衛 931 句 allow=0 / lock=0、商品主檔全 0 命中
    #    ⚠ other **不收**——守衛第 851 行有 `vague|and the other one`
    (_re.compile(r"\b(?:allow|lock)\b(?=\s+me\s+when\b)", _re.I), "alert"),
    # ── asr-fix-en batch2：102 條真實錯法重放歸納（2026-08-02）──────
    #   只收「**功能詞**被聽成近音詞」——商品名被聽成完全不同的詞無解。
    #   每條都撞過守衛 931 句 + 商品主檔（全部 0 命中，sales 唯一 1 句
    #   是 `hot|sales ranking`，與本規則方向一致）。
    # ① stock 的近音：low stack list / safety stark
    (_re.compile(r"\b(?:stack|stark)\b(?=\s+list\b)", _re.I), "stock"),
    (_re.compile(r"(?<=\bsafety\s)(?:stack|stark)\b", _re.I), "stock"),
    # ② 排行意圖：top sales → top sellers
    (_re.compile(r"(?<=\btop\s)sales\b", _re.I), "sellers"),
    # ③ inbound 被拆成兩個字（whisper 對複合詞常見）
    (_re.compile(r"\bin\s+bond\b", _re.I), "inbound"),
    # ④ 寫入動詞 shipped 的變形（ship's 已被撇號處理，這裡收 ships）
    (_re.compile(r"\bships\b(?=\s+\d)", _re.I), "shipped"),
    # ⑤ purchase order：a purchase → approaches（連音黏成一個詞）
    (_re.compile(r"\bapproaches\b(?=\s+order\b)", _re.I), "a purchase"),
    # ⑥ alert 被聽成 error（限定「set an X for」句型，避免碰真正的錯誤訊息）
    (_re.compile(r"(?<=\bset\san\s)error\b(?=\s+for\b)", _re.I), "alert"),
    # ── asr-fix-en batch3：撇號變形（2026-08-02）────────────────────
    #   batch2 只收了 `ships`，漏掉 whisper 的另一種寫法 `ship's`
    #   （撇號讓 \bships\b 對不到）⇒ **同一個音有兩種寫法時要一起收**。
    #   ⚠️ 限定「後接數字」——`today's inbound` 這種所有格是正常英文，不能碰。
    (_re.compile(r"\bship'?s\b(?=\s+\d)", _re.I), "shipped"),
    (_re.compile(r"\breceive'?s\b(?=\s+\d)", _re.I), "received"),
]


def _asr_normalize(text: str) -> str:
    """語音專用正規化。回修正後文字（沒命中則原樣回傳）。

    EN build：英文句只做**大小寫＋句末標點**正規化（理由見 _ASR_FIX 開頭
    的長註解：系統 21 個詞表的英文詞全是小寫，whisper 一律大寫開頭）。
    中文同音規則（_ASR_FIX）對英文無意義，且可能誤傷 → 英文句不跑。
    """
    if _is_mostly_english(text):
        out = text.strip().lower()
        # ── 真人錄音批抓到：**句中逗號要拿掉**（user 實測回報）────────
        #   whisper 對「唸得慢／中間停頓」會插逗號，把一句話拆成好幾段：
        #     `send 30 yoga mats from south to central`
        #     → `sen, 30, yoga mess from south to central`
        #   兩個問題：①逗號本身是雜訊（**語音查詢不會有逗號**）
        #             ②它還會**切斷詞彙**——`send` 被切成 `sen`
        #   ⚠️ 只拿掉逗號/分號，**連字號要留**（`14-inch`、`usb-c` 是商品名
        #     的一部分，剝掉會對不到主檔）。
        out = _re.sub(r"\s*[,;]\s*", " ", out)
        out = _re.sub(r"\s{2,}", " ", out).strip()
        # 句末標點：whisper 一定會加 . ? !，訪客打字通常沒有。
        out = out.rstrip(" .?!,;:")
        # 出口修正（真人錄音批歸納的穩定錯法）——**逗號拿掉之後**才跑，
        #   否則 `sen, 30,` 的 sen 帶著逗號比對不到 \bsen\b。
        for _pat, _rep in _ASR_FIX_EN:
            _new = _pat.sub(_rep, out)
            if _new != out:
                log.info(f"[asr-fix-en] {out!r} → {_new!r}")
                out = _new
        return out
    out = text
    for pat, rep in _ASR_FIX:
        out = pat.sub(rep, out)
    return out


def _voice_ready() -> tuple[bool, str]:
    # EN build：訊息改英文（訪客會在載入畫面看到），並移除 OpenCC 檢查
    #   ——舊版把它列為必要條件，沒裝 opencc 會讓英文語音整組回報不可用，
    #   那是中文版的遺留相依（英文根本用不到簡轉繁）。
    if not _VOICE_CLI.exists():
        return False, "Speech recognition is not installed"
    if not _VOICE_MODEL.exists():
        return False, "Speech recognition model is missing"
    return True, ""


@app.get("/api/voice_status")
async def voice_status():
    """前端啟動時問一次：本地 ASR 能不能用（決定要不要回退瀏覽器辨識）。"""
    ok, reason = _voice_ready()
    return JSONResponse({"ok": ok, "reason": reason}, headers=NO_CACHE)


@app.post("/api/asr")
async def asr_api(req: Request):
    """收前端錄音 → 本地辨識 → 回繁體文字。不直接送 WS（讓前端沿用既有送出路徑）。"""
    import subprocess
    import tempfile
    import time as _time

    ok, reason = _voice_ready()
    if not ok:
        return JSONResponse({"ok": False, "reason": reason}, headers=NO_CACHE)

    audio = await req.body()
    if not audio:
        return JSONResponse({"ok": False, "reason": "No audio received"}, headers=NO_CACHE)

    t0 = _time.time()
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "in.bin"
        wav = Path(td) / "out.wav"
        raw.write_bytes(audio)
        # 瀏覽器給 webm/opus，ASR 只吃 16k mono PCM16 → 一律轉一次
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(raw), "-ar", "16000", "-ac", "1",
                 "-c:a", "pcm_s16le", str(wav)],
                capture_output=True, timeout=30,
            )
        except Exception as e:
            return JSONResponse({"ok": False, "reason": f"Audio conversion failed: {e}"},
                                headers=NO_CACHE)
        if not wav.exists() or wav.stat().st_size < 1000:
            return JSONResponse({"ok": False, "reason": "Audio too short or unsupported format"},
                                headers=NO_CACHE)

        try:
            p = subprocess.run(
                # `-nt` = no timestamps：不加的話每行都是
                #   `[00:00:00.000 --> 00:00:02.080]   How many …`，取字要剝
                #   時間戳。加了之後 **stdout 只剩辨識文字**（載入訊息與計時
                #   都走 stderr）→ 取字邏輯可以極簡、不必猜格式。
                # `-l en` 明確指定英文：tiny.en 本就是英文專用模型，
                #   指定後不會浪費時間做語言偵測。
                # `-ac N` audio-ctx：只處理 N 個上下文 token 而非預設 1500
                #   （=30 秒）。倉管句只有 1-3 秒，削掉的全是浪費的 encoder
                #   運算 → 快 2.3 倍且準確度不掉。詳見 _ASR_AUDIO_CTX 上方註解。
                [str(_VOICE_CLI), "-m", str(_VOICE_MODEL),
                 "-f", str(wav), "-nt", "-l", "en"]
                + (["-ac", _ASR_AUDIO_CTX]
                   if _ASR_AUDIO_CTX and _ASR_AUDIO_CTX != "0" else []),
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            return JSONResponse({"ok": False, "reason": "Speech recognition timed out"}, headers=NO_CACHE)

    # ── EN build：取字。舊版是「取最後一行**含中文**的輸出」——英文辨識結果
    #   不含中文字 → text 恆空 → 英文語音**一句都不會成功**，一律回
    #   「Could not make out any speech」。這是英文版語音先前結構性不可用的
    #   主因之一。改成：whisper -nt 的 stdout 全是文字，接起來即可。
    text = " ".join(l.strip() for l in p.stdout.splitlines() if l.strip()).strip()
    # whisper 對無語音/純噪音會輸出空字串或 [BLANK_AUDIO] / (silence) 之類標記
    if not text or _re.fullmatch(r"[\[\(][^\]\)]*[\]\)]", text):
        return JSONResponse({"ok": False, "reason": "Could not make out any speech"}, headers=NO_CACHE)

    # EN build：不再經 OpenCC（s2twp 是簡轉繁，對英文無意義）
    _raw = text
    text = _asr_normalize(text)
    dt = round(_time.time() - t0, 2)
    if text != _raw:
        log.info(f"[asr] {dt}s → 「{_raw}」→ 同音修正「{text}」")
    else:
        log.info(f"[asr] {dt}s → 「{text}」")
    return JSONResponse({"ok": True, "text": text, "sec": dt}, headers=NO_CACHE)


@app.post("/api/reset_demo")
async def reset_demo_data_api(req: Request):
    """展示資料一鍵重置（獨立按鈕觸發，需密碼）。換回 warehouse_data_baseline/ 並清 session state。"""
    import tools_v2
    body = await req.json()
    password = body.get("password", "")
    res = tools_v2.commit_reset_demo_data(password=password, actor="user_confirmed")
    if res.get("ok"):
        _item_create_state.clear()
        _item_create_state_ws.clear()
        _item_delete_state.clear()
        _pending_by_vid.clear()   # r32：舊卡片記憶（資料都換掉了，卡片內容已失效）
        _ctx_by_vid.clear()       # r32：舊 context（last_sku 可能指向已刪除的商品）
        await push_display({"type": "snapshot", "snapshot": finance.dashboard_snapshot()})
        log.info("[reset_demo] 展示資料已重置")
    return JSONResponse(res, headers=NO_CACHE)


@app.websocket("/ws/display")
async def ws_display(ws: WebSocket):
    await ws.accept()
    display_sockets.add(ws)
    log.info(f"Display 連線（共 {len(display_sockets)}）")
    try:
        await ws.send_text(json.dumps(
            {"type": "snapshot", "snapshot": finance.dashboard_snapshot()},
            ensure_ascii=False,
        ))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning(f"Display ws 錯誤: {e}")
    finally:
        display_sockets.discard(ws)


@app.websocket("/ws")
async def ws_handler(ws: WebSocket):
    # r47：測試連線帶 ?fast=1 → 該連線打字機動畫 sleep=0（同 code path、token frame
    # 照送，只省純等待）。訪客不帶參數維持 8ms。contextvars 每 task（=每連線）隔離。
    try:
        if ws.query_params.get("fast") == "1":
            _TK_DELAY.set(0.0)
    except Exception:
        pass
    global _visitor_closed

    # 多裝置展示模式：允許多個同時連線（桌面+手機），不踢舊連線
    await ws.accept()
    all_sockets.add(ws)
    log.info(f"訪客連線（共 {len(all_sockets)}）")

    async def send(o: dict):
        # r32：done 是所有回答路徑的唯一咽喉（dispatch 直答有數十個 continue 出口，
        # 逐一補 context 必漏）→ 在這裡統一吸收商品/倉別 + 記住確認卡。
        if o.get("type") == "done":
            try:
                _ctx_absorb(vid, o.get("result") or {})
            except Exception as e:
                log.warning(f"[ctx-absorb] vid={vid} 失敗（不影響回答）: {e}")
            # 展場除錯用：把系統回答也記進 journal，與上面的「User vid=X: 輸入」
            #   配成對（展後 grep 'User vid=\|Answer vid=' 就能看完整問答）。
            #   只多印一行 log、用現有 journal，不建檔不動前端、零負擔。
            try:
                _r = o.get("result") or {}
                _sm = (_r.get("summary") or "").replace("\n", " ")[:80]
                log.info(f"Answer vid={vid}: [{_r.get('view') or '-'}] {_sm}")
            except Exception:
                pass
        await ws.send_text(json.dumps(o, ensure_ascii=False))

    # vid 用全域遞增序號，絕不碰撞（2026-07-09：原 id(ws)%10000 兩個連線會算出
    # 同 vid、且 id 斷線後會被回收重用 → 不同訪客共用 session state 污染）
    global _vid_counter
    _vid_counter += 1
    vid = _vid_counter

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                continue

            msg_type = data.get("type")

            # ── r54：口語確認代按——卡片在場＋純確認短句（好/確認/ok/就這樣送出）→
            #   轉成 confirm 訊息走同一條 HITL 路（等同按下前端按鈕）。全語音展示的
            #   最後一塊：以前只回「請點卡片按鈕」的引導，訪客得伸手摸螢幕。
            #   範圍鎖：①有 pending 卡（per-vid）②句子=確認詞全匹配或含「按確認」類
            #   片語且 ≤8 字——長句/其他意圖不會誤觸發。──
            if msg_type == "chat":
                _vc_txt = (data.get("text") or "").strip().rstrip("!！。~～喔啦唷呦")
                _vc_pend = _pending_by_vid.get(vid) or {}
                if _vc_pend.get("data") is not None and _vc_pend.get("view") in _VIEW2ACTION_WS:
                    # r78：「現在先跑一次」的「先」是加急不是猶豫——負向詞改用
                    # 複合形，裸「先」不再擋代按
                    _vc_neg = (any(n in _vc_txt for n in ("不", "別", "取消", "算了",
                                                          "先不", "先別", "先等", "先緩",
                                                          "等等", "等一下"))
                               # r16：英文負向原本沒擋——加詞界 confirm 家族後
                               #   'dont do it' 若不擋會被誤代按（寫入級）
                               or bool(_re.search(
                                   r"\b(?:dont|don't|do not|not|no|never|nope|"
                                   r"cancel|hold|wait|stop|forget)\b",
                                   _vc_txt.lower())))
                    # r76：「就出3件 確認」——帶內容重申+結尾確認。句中數字必須跟
                    # 卡片數量一致才代按（不一致是改量意圖，讓給改卡提示）
                    _vc_num = _re.search(r"(\d+)", _vc_txt)
                    _vc_qty = (_vc_pend.get("data") or {}).get("qty")
                    _vc_qty_ok = (not _vc_num or not isinstance(_vc_qty, int)
                                  or abs(_vc_qty) == int(_vc_num.group(1)))
                    # ⚠️ EN build：`_PEND_OK_SUB` 的英文詞全是小寫，這裡原本用
                    #   **原句**比對（只有上面 _PEND_OK 那行有 .lower()）→
                    #   `Go Ahead` / `GO AHEAD` 不命中代按，掉到 pending-gate
                    #   只回「請按按鈕」。**這是寫入路徑**：訪客以為確認了、
                    #   其實沒寫入（小寫 `go ahead` 同一張卡是真的寫入成功的）。
                    #   語音更會踩到——whisper 一律首字大寫。
                    _vc_low = _vc_txt.lower()
                    # ⚠️ 長度門檻同樣要分語言（坑 2）：`<= 8 字元`是為中文調的，
                    #   英文 `yes please`(10) / `go ahead please`(15) 都超標
                    #   → 確認詞明明命中卻被長度擋掉（實測 `yes please` 大小寫
                    #   都失敗，那是既有破口不是大小寫問題）。英文改**詞數 ≤ 3**。
                    _vc_len_ok = ((len(_vc_txt.split()) <= 3)
                                  if _is_mostly_english(_vc_txt)
                                  else (len(_vc_txt) <= 8))
                    _vc_ok = (not _vc_neg
                              and (_vc_low in _PEND_OK
                                   or (_vc_len_ok
                                       and any(w in _vc_txt or w in _vc_low
                                               for w in _PEND_OK_SUB))
                                   # r16 #55/#75：'yes confirm'/'confirm the
                                   #   alert' 不是 exact 也不含 SUB 片語 →
                                   #   掉過代按被 C3 搶成缺貨清單（訪客以為
                                   #   警示建了）。≤4 詞＋詞界確認詞＝代按；
                                   #   負向詞（dont…）已在上面擋。
                                   or (_is_mostly_english(_vc_txt)
                                       and len(_vc_txt.split()) <= 4
                                       and _re.search(
                                           r"\b(?:confirm|confirmed|yes|yeah|"
                                           r"yep|proceed|go ahead|do it)\b",
                                           _vc_low))
                                   or (len(_vc_txt) <= 6 and ("確認" in _vc_txt or "送出" in _vc_txt))
                                   or (len(_vc_txt) <= 10 and _vc_qty_ok
                                       and _re.search(r"(確認|送出)$", _vc_txt))))
                    if _vc_ok:
                        log.info(f"[voice-confirm] vid={vid} 「{_vc_txt}」→ 代按 {_vc_pend['view']}")
                        msg_type = "confirm"
                        data = {"type": "confirm",
                                "action": _VIEW2ACTION_WS[_vc_pend["view"]],
                                "pending": _vc_pend.get("data") or {},
                                "script_id": (_vc_pend.get("data") or {}).get("script_id", ""),
                                # r74：排程/警示刪除卡的 commit 走 id 不走 pending
                                "job_id": (_vc_pend.get("data") or {}).get("job_id", ""),
                                "rule_id": (_vc_pend.get("data") or {}).get("rule_id", "")}

            # ── confirm：Agent 進階工具寫入/執行的二次確認（HITL gate）──
            #   前端在收到 view=config_confirm / script_confirm 後，訪客按「確認」才送這個。
            if msg_type == "confirm":
                import tools_v2
                act = data.get("action", "")
                trace_id = f"vid{vid}-{int(__import__('time').time())}"
                try:
                    if act == "config_set":
                        res = tools_v2.commit_config_set(
                            data.get("pending", {}), actor="user_confirmed", trace_id=trace_id)
                    elif act == "run_script":
                        # days：訪客在開卡時講的期間（匯出進出紀錄用）
                        res = tools_v2.commit_run_script(
                            data.get("script_id", ""), actor="user_confirmed",
                            trace_id=trace_id, days=data.get("days"))
                    elif act == "generate_po":
                        res = tools_v2.commit_po(
                            data.get("pending", {}), actor="user_confirmed", trace_id=trace_id)
                    elif act == "set_alert":
                        res = tools_v2.commit_alert_set(
                            data.get("pending", {}), actor="user_confirmed", trace_id=trace_id)
                        # 展場體驗：設定完**馬上**跑一次真實檢查（背景排程是每小時，
                        #   訪客只停留幾分鐘、等不到）。走的是同一支
                        #   `_check_alert_rules()`＝真的讀規則、真的算缺貨，
                        #   不是假動畫。
                        #   ⚠️ 只檢查訪客**剛建立的那條**——不限定的話 baseline
                        #     既有的兩條全域規則會一起跳，訪客看到三條橫幅、
                        #     前兩條跟他無關（實測到）。
                        _demo_kick(
                            _check_alert_rules(
                                only_rule_id=(res.get("data") or {}).get("rule_id", "")),
                            "alert")
                    elif act == "set_schedule":
                        res = tools_v2.commit_schedule_set(
                            data.get("pending", {}), actor="user_confirmed", trace_id=trace_id)
                        await push_display({"type": "schedule_created",
                                           "job": res.get("data", {}).get("job", {})})
                        # 2026-08-06 user 定調（ZH 同款）：排程建立後不再立跑
                        #   示範；警示立跑檢查保留。
                    elif act == "item_create":
                        res = tools_v2.commit_create_item(
                            data.get("pending", {}), actor="user_confirmed", trace_id=trace_id)
                    elif act == "item_delete":
                        res = tools_v2.commit_delete_item(
                            data.get("pending", {}), actor="user_confirmed", trace_id=trace_id)
                    elif act == "delete_schedule":
                        res = tools_v2.commit_delete_schedule(
                            data.get("job_id", ""), actor="user_confirmed", trace_id=trace_id)
                    elif act == "delete_alert":
                        res = tools_v2.commit_delete_alert(
                            data.get("rule_id", ""), actor="user_confirmed", trace_id=trace_id)
                    elif act == "create_movement":
                        res = tools_v2.commit_movement(
                            data.get("pending", {}), actor="user_confirmed", trace_id=trace_id)
                        await push_display({"type": "snapshot", "snapshot": finance.dashboard_snapshot()})
                    elif act == "create_transfer":
                        res = tools_v2.commit_transfer(
                            data.get("pending", {}), actor="user_confirmed", trace_id=trace_id)
                        await push_display({"type": "snapshot", "snapshot": finance.dashboard_snapshot()})
                    else:
                        res = {"ok": False, "summary": "Unknown confirmation action",
                               "view": "error", "data": {}}
                except Exception as e:
                    log.error(f"[confirm] vid={vid} {act} 失敗: {e}", exc_info=True)
                    res = {"ok": False, "summary": f"Action failed: {e}",
                           "view": "error", "data": {}}
                log.info(f"[confirm] vid={vid} {act} → {res.get('summary','')[:60]}")
                await push_display({"type": "trace", "stage": "committed",
                                    "action": act, "result": res,
                                    "snapshot": finance.dashboard_snapshot()})
                for ch in res.get("summary", ""):
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get() * 1.5)
                await send({"type": "done", "result": res})
                continue

            # ── direct_call：chip bypass LLM ──
            # 給「庫存警示」等零容錯 chip 用。前端送 {type:"direct_call", function:"list_low_stock", args:{}}
            if msg_type == "direct_call":
                func_name = data.get("function", "")
                func_args = data.get("args", {}) or {}
                log.info(f"User vid={vid} [direct_call] {func_name}({func_args})")
                await push_display({
                    "type":     "trace",
                    "stage":    "direct_call",
                    "function": func_name,
                    "args":     func_args,
                })
                result = finance.execute(func_name, func_args)
                await push_display({
                    "type":     "trace",
                    "stage":    "result",
                    "function": func_name,
                    "args":     func_args,
                    "result":   result,
                    "snapshot": finance.dashboard_snapshot(),
                })
                summary = result.get("summary", "")
                for ch in summary:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get() * 1.5)
                await send({"type": "done", "result": result})
                continue

            if msg_type != "chat":
                continue

            user_text = (data.get("text") or "").strip()
            if not user_text:
                continue
            _raw_text = user_text
            user_text = _normalize_typos(user_text)   # 同音錯字正規化（r17，最早套用）
            # EN：功能詞拼錯還原。**必須在入口做**——
            #   v1 只餵給 intent_clf，下游 C18／keyword 抽取／gate
            #   仍拿原文，log 顯示有修但結果照樣 rejected。
            user_text = _en_funcword_fix(user_text)
            # EN：句中逗號/分號 → 空白。**whisper 會自己插逗號**
            #   （真人錄音第 31 句 `sen, 30, yoga mess`），而
            #   `north received 50, wireless mouse` 的逗號會讓 LLM 抽不到
            #   qty（實測 create_movement qty=''）→ 反問「幾個？」。
            #   `/api/asr` 出口本來就有剝，但**打字路徑沒有** → 補在這。
            #   ⚠️ 只碰逗號/分號，**連字號要留**（14-inch／usb-c 是商品名）。
            if _is_mostly_english(user_text):
                _cm = _re.sub(r"\s*[,;]\s*", " ", user_text)
                _cm = _re.sub(r"\s{2,}", " ", _cm).strip()
                if _cm != user_text:
                    log.info(f"[en-comma] {user_text!r} → {_cm!r}")
                    user_text = _cm
            # EN build（r4 S9）：英文數字詞 → 阿拉伯數字，**必須在寫入意圖
            #   判定（C13b/C13c 抽數量）之前**，否則 'five hundred yoga mat'
            #   抽不到數量 → 被當查詢回庫存數字（實測）。
            #   語音接上後更關鍵：ASR 常吐 'fifty' 而非 '50'。
            if _is_mostly_english(user_text):
                _num_norm = _en_words_to_num(user_text)
                if _num_norm != user_text:
                    log.info(f"[EN num] {user_text!r} → {_num_norm!r}")
                    user_text = _num_norm
            # 錯字被修好 → 告訴前端，讓對話氣泡顯示「修復後」的文字。
            #   展場價值：訪客打錯／ASR 聽錯時，畫面若顯示原始錯字會讓人以為
            #   系統沒聽懂；顯示修好的版本才看得出「錯字容錯」真的在運作。
            if user_text != _raw_text:
                try:
                    await send({"type": "user_fixed", "text": user_text})
                except Exception:
                    pass

            # ── 放棄閘門（rewrite / 守門員 之前，r33 統一）──
            #   涵蓋三種情境：流程中放棄、卡片在時放棄、閒置時說放棄。
            #   過去只認「取消」二字 → 其他講法在流程中被吞成商品名、在卡片時被
            #   守門員回教學文。
            # r64：卡片在場的拒絕開頭句（「不要好了 我自己叫貨」len>8 過不了
            # _abort_intent）——不要/不用開頭且非換看句 → 視為放棄卡片
            _abort64 = (vid in _pending_by_vid and len(user_text) <= 14
                        and _re.match(r"^(不要|不用|先不|不然算了)", user_text.strip())
                        and not _re.search(r"我要|查|看|換", user_text))
            if _abort_intent(user_text) or _abort64:
                _in_flow = (_item_create_state_ws.get(vid, {}).get("active")
                            or _item_delete_state.get(vid)
                            or _write_flow_by_vid.get(vid))
                _had_card = vid in _pending_by_vid
                _item_create_state_ws.pop(vid, None)   # 只清自己這位訪客的流程
                _item_delete_state.pop(vid, None)
                _write_flow_by_vid.pop(vid, None)      # r56：寫入續流也一併清
                _pending_by_vid.pop(vid, None)
                log.info(f"[abort] {user_text!r} → 清流程/卡片（flow={bool(_in_flow)} card={_had_card}）")
                if _in_flow or _had_card:
                    # r55 收官批：取消不能空回答（畫面曾只剩一行「已取消新增商品」，
                    # 取消的是出貨/調貨卡時文字還是錯的）。給通用取消文，前端優先顯示。
                    _ab_ok = "OK, that operation is cancelled — nothing was saved."
                    for ch in _ab_ok:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "item_cancelled", "summary": _ab_ok, "data": {}}})
                else:
                    _ab_msg = ("No problem — there is no operation in progress. "
                               "Just tell me what you need, e.g. "
                               '"south bluetooth earphones stock" or '
                               '"north received 50 wireless mouse".')
                    for ch in _ab_msg:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "clarify", "summary": _ab_msg,
                        "data": {"question": _ab_msg, "options": [], "hint": ""}}})
                continue

            # ── r56：寫入續流——進出貨/調貨 clarify（問倉別/數量/路線）後的短答接回 ──
            #   「進30個毛帽」→「要異動哪個倉？」→ 訪客答「北倉」曾被當獨立查詢回庫存、
            #   流程斷裂。tools_v2 在 clarify data 帶 flow 槽位，這裡比對短答並重呼叫。
            _wf56 = _write_flow_by_vid.get(vid)
            if _wf56 and not _item_create_state_ws.get(vid, {}).get("active"):
                _wf_args = None
                _wf_t = user_text.strip().strip("!！。.~～ ")
                if _wf56.get("await") == "warehouse":
                    _m_wh56 = _re.fullmatch(
                        r"(不對[ ，,]*)?(是|放|到|去)?\s*([北中南])(區)?(倉)?(好了|吧|喔)?", _wf_t)
                    if _m_wh56:
                        _wf_args = {"keyword": _wf56.get("keyword", ""),
                                    "direction": _wf56.get("direction", ""),
                                    "qty": _wf56.get("qty", ""),
                                    "is_return": bool(_wf56.get("is_return")),
                                    "warehouse": _m_wh56.group(3) + "倉"}
                elif _wf56.get("await") == "qty":
                    _m_q56 = _re.fullmatch(
                        r"(進貨?|出貨?|調貨?)?\s*([0-9]{1,7})\s*(個|件|箱|台|支|包|盒|罐|瓶|組|雙)?(就好|好了|吧|喔)?",
                        _wf_t)
                    if _m_q56:
                        if _wf56.get("tool") == "create_transfer":
                            _wf_args = {"keyword": _wf56.get("keyword", ""),
                                        "from_wh": _wf56.get("from_wh", ""),
                                        "to_wh": _wf56.get("to_wh", ""),
                                        "qty": _m_q56.group(2)}
                        else:
                            _wf_args = {"keyword": _wf56.get("keyword", ""),
                                        "warehouse": _wf56.get("warehouse", ""),
                                        "direction": _wf56.get("direction", ""),
                                        "is_return": bool(_wf56.get("is_return")),
                                        "qty": _m_q56.group(2)}
                elif _wf56.get("await") == "route":
                    _m_r56 = _re.search(
                        r"(從)?([北中南])(區)?倉?\s*(調|到|去|往|搬|撥|挪)+\s*([北中南])(區)?倉?", _wf_t)
                    if _m_r56 and len(_wf_t) <= 14:
                        _wf_args = {"keyword": _wf56.get("keyword", ""),
                                    "qty": _wf56.get("qty", ""),
                                    "from_wh": _m_r56.group(2) + "倉",
                                    "to_wh": _m_r56.group(5) + "倉"}
                    else:
                        # r61：單邊倉回答（「從北倉調」「去南倉」）——補進缺的那一側
                        _m_r1 = _re.fullmatch(
                            r"(從|由|去|到|往)?\s*([北中南])(區)?倉?(調|出|走|撥|挪|搬|吧|好了)?", _wf_t)
                        if _m_r1:
                            _wf_from = _wf56.get("from_wh", "")
                            _wf_to = _wf56.get("to_wh", "")
                            _wf_side_to = _m_r1.group(1) in ("去", "到", "往")
                            if _wf_side_to or _wf_from:
                                _wf_to = _m_r1.group(2) + "倉"
                            else:
                                _wf_from = _m_r1.group(2) + "倉"
                            _wf_args = {"keyword": _wf56.get("keyword", ""),
                                        "qty": _wf56.get("qty", ""),
                                        "from_wh": _wf_from, "to_wh": _wf_to}
                # r61：只在命中時消耗——沒命中的訊息讓它正常處理，flow 的清理交給
                # _ctx_absorb（成功回答即清、rejected/guide 存活），亂打不再殺流程
                if _wf_args is not None:
                    _write_flow_by_vid.pop(vid, None)
                    import tools_v2 as _tv2_wf
                    _wf_res = getattr(_tv2_wf, _wf56["tool"])(**_wf_args)
                    log.info(f"[write-flow] vid={vid} {_wf56['tool']}"
                             f" {_wf56.get('await')}←{_wf_t!r}")
                    for ch in (_wf_res.get("summary") or ""):
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": _wf_res})
                    continue

            # ── r32 pending 卡片口語層（rewrite 之前）──
            #   卡片在畫面上時訪客打「好」→ 過去被守門員 rejected；打「不對是100個」
            #   → 100 被 match 成「運動毛巾 100x30cm」幻覺回庫存。寫入授權只認按鈕，
            #   這裡一律引導，不寫入、不猜商品。新增商品流程中不套用（那是 step 值）。
            # ── EN build（劇情批 r2 S2）：**卡片改量**（'make it 20 instead'）──
            #   原本回「請重講整句」＝訪客要把商品/倉別再打一次，體驗差。
            #   卡片本身已有商品/倉別/方向，缺的只是新數量 → 抽到數字就
            #   **重開一張確認卡**（仍需按鈕授權，符合「寫入只認按鈕」原則，
            #   不是直接寫入）。
            _pend_cur = _pending_by_vid.get(vid)
            if (_pend_cur and _is_mostly_english(user_text)
                    and not _item_create_state_ws.get(vid, {}).get("active")
                    and _pend_cur.get("view") in ("movement_confirm",
                                                  "transfer_confirm")):
                # r3 S5：多輪反悔的各種講法——'make it 20' / 'actually 30' /
                #   'no lets do 10' / 'change to 5' / 'just 15'
                _mq = _re.search(
                    r"\b(?:make it|change it to|change to|set it to|lets do|"
                    r"let's do|do|just|only|instead|actually|rather|no)\b"
                    r".{0,12}?\b(\d+)\b"
                    r"|\b(\d+)\b\s*(?:instead|rather|then)\b", user_text, _re.I)
                if _mq:
                    _newq = _mq.group(1) or _mq.group(2)
                    # data 結構（實測）：{pending: bool, sku, name, warehouse,
                    #   warehouse_label, direction, direction_label, qty, …}
                    _pd = _pend_cur.get("data") or {}
                    _pend_item = _pd.get("name")
                    _pend_wh = _pd.get("warehouse")
                    _pend_dir = _pd.get("direction")
                    if _pend_item and _newq:
                        _verb = "received" if _pend_dir == "in" else "shipped"
                        _wh_txt = {"north": "north", "central": "central",
                                   "south": "south"}.get(str(_pend_wh).lower(), "")
                        _restated = f"{_wh_txt} {_verb} {_newq} {_pend_item}".strip()
                        log.info(f"[pending-fix] 改量 {user_text!r} → 重開卡 "
                                 f"{_restated!r}")
                        user_text = _restated
                        _pending_by_vid.pop(vid, None)   # 舊卡作廢，走正常流程開新卡
                # ── r18：**改倉別 / 改商品**（原本只有改數量）─────────────
                #   `no i meant 30`（改量）能 work，但 `sorry i meant south`
                #   （改倉別）/ `actually i meant the tent`（改商品）只回引導語
                #   ——系統認得這是修改意圖（命中 _PEND_FIX），卻沒去解析新值。
                #   展場口誤講錯倉別/商品比講錯數量更常見，而且**改不掉就得
                #   整句重講**，體感很差。
                #   ⚠️ 沿用改量同一套「重述整句 → 重開卡」機制，不另闢路徑：
                #     重開卡會走完整的既有驗證（庫存夠不夠、倉別合法…），
                #     比直接改 pending 的欄位安全。
                elif _re.search(r"\b(?:i meant|imeant|meant|actually|"
                                r"sorry|no|rather|instead)\b", user_text, _re.I):
                    _pd = _pend_cur.get("data") or {}
                    _pend_item = _pd.get("name")
                    _pend_wh = _pd.get("warehouse")
                    _pend_dir = _pd.get("direction")
                    _pend_qty = _pd.get("qty")
                    if _pend_item and _pend_qty:
                        _verb = "received" if _pend_dir == "in" else "shipped"
                        # ①改倉別：句中出現另一個倉名
                        _mw = _re.search(r"\b(north|central|south)\b",
                                         user_text, _re.I)
                        _new_wh = _mw.group(1).lower() if _mw else None
                        # ②改商品：句中出現另一個真商品
                        _new_item = None
                        try:
                            _cand_item = _extract_sku_keyword(user_text)
                            if (_cand_item
                                    and _cand_item.lower() != str(_pend_item).lower()):
                                _new_item = _cand_item
                        except Exception:
                            pass
                        if _new_wh or _new_item:
                            _wh_txt = _new_wh or str(_pend_wh).lower()
                            _it_txt = _new_item or _pend_item
                            _restated = (f"{_wh_txt} {_verb} {_pend_qty} "
                                         f"{_it_txt}").strip()
                            log.info(
                                f"[pending-fix] 改"
                                f"{'倉別' if _new_wh else ''}"
                                f"{'商品' if _new_item else ''} "
                                f"{user_text!r} → 重開卡 {_restated!r}")
                            user_text = _restated
                            _pending_by_vid.pop(vid, None)
            if not _item_create_state_ws.get(vid, {}).get("active"):
                _pend_msg = _pending_reply(vid, user_text)
                if _pend_msg:
                    log.info(f"[pending-gate] 對卡片講話 → 引導: {user_text!r}")
                    for ch in _pend_msg:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "clarify", "summary": _pend_msg,
                        "data": {"question": _pend_msg, "options": [], "hint": ""}}})
                    continue

                # r61：「剛剛那個進貨還在嗎」——沒卡片時老實說結案了（曾被 ctx
                # 幻覺成新寫入 clarify「無線滑鼠要異動哪個倉」；有卡片由 _PEND_ASK 接）
                if (vid not in _pending_by_vid and len(user_text) <= 14
                        and _re.search(r"還在|還有效|還算數", user_text)
                        and _re.search(r"剛剛|剛才|那個|那筆|進貨|出貨|調貨|操作|卡片", user_text)):
                    _ps_msg = ("目前沒有進行中的操作（確認卡按過或取消後就結案囉）。"
                               "要再來一筆直接說完整需求，例如「中倉進25個啤酒」。")
                    log.info(f"[pending-status] 無卡的還在嗎 → 結案說明: {user_text!r}")
                    for ch in _ps_msg:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "clarify", "summary": _ps_msg,
                        "data": {"question": _ps_msg, "options": [], "hint": ""}}})
                    continue

                # r80：純調貨句缺商品名（「太少了 中倉調100過來南倉」）——訪客
                # 指的是剛查過的商品，用 ctx last_sku 補回，不要退成概覽。
                # 條件嚴：調貨動詞+數字+兩倉+句中抽不到真商品+有 ctx
                _tf80_bare = _re.search(r"[調撥挪搬移]\s*[0-9]{1,4}", user_text)
                _tf80_whs = {z for z in "北中南" if z + "倉" in user_text
                             or z + "區" in user_text}
                # ── EN build：上面兩個判準**都是中文**（調撥挪搬移／北中南倉）
                #   → 英文調貨句整段進不來，掉到 C13a 反問「要調哪個商品」，
                #   代稱式調撥（上一句才鎖定商品）接不上脈絡。
                #   長對話測 2026-08-02 抓到；⚠️ 第一次只改區塊內部的剝詞邏輯，
                #   但**進入條件沒改** → 那段從沒執行（同坑：要追執行路徑）。
                if not _tf80_bare and _is_mostly_english(user_text):
                    _tf80_bare_en = _re.search(
                        r"\b(?:transfer|move|send|ship|shift|relocate)\b"
                        r"[^0-9]{0,20}[0-9]{1,4}", user_text, _re.I)
                    _tf80_whs_en = {w for w in ("north", "central", "south")
                                    if _re.search(r"\b" + w + r"\b",
                                                  user_text, _re.I)}
                    if _tf80_bare_en and len(_tf80_whs_en) >= 2:
                        _tf80_bare = _tf80_bare_en
                        _tf80_whs = _tf80_whs_en
                if (_tf80_bare and len(_tf80_whs) >= 2
                        and _ctx_for(vid).get("last_sku")
                        and vid not in _pending_by_vid):
                    import warehouse as _W_tf80
                    _tf80_kw = _extract_sku_keyword(user_text) or ""
                    _tf80_m = _W_tf80.match_items(_tf80_kw) if _tf80_kw else []
                    # r81 寫入契約鐵律：只有句中「完全沒講商品」才補 ctx。
                    # 「把北倉的傘都調去南倉」的「傘」是明確講出但查無的商品
                    # → 誠實查無，絕不補拖把（ctx 頂替只給真的省略商品名的句）。
                    # 判別：剝掉調貨動詞/數量/倉別/助詞後，剩餘實詞 ≥2 字 = 有講商品
                    _tf80_stem = user_text
                    # 前綴語氣/評論廢話（「太少了」「不夠」）先剝——不是商品
                    for _pfx80 in ("太少了", "太少", "不夠了", "不夠", "太多了", "太多",
                                   "快沒了", "快沒", "剩太少", "有點少", "幫我"):
                        _tf80_stem = _tf80_stem.replace(_pfx80, "")
                    # 長詞先剝（過來/過去 在 來/去 之前，否則剩孤字「過」）
                    for _z80 in ("過來", "過去", "把", "將", "調", "撥", "挪", "搬", "移",
                                 "來", "去", "到", "都", "的", "北倉", "中倉", "南倉",
                                 "北區", "中區", "南區", "倉", "區", "過", "喔", "啦", "吧"):
                        _tf80_stem = _tf80_stem.replace(_z80, "")
                    _tf80_stem = _re.sub(r"[0-9]+[個件]?", "", _tf80_stem).strip("　 ")
                    # 契約鐵律：剝掉前綴廢話+調貨結構後，殘詞非空 = 訪客有講要調的
                    # 東西（「傘」查無也要誠實回查無，不補拖把）；殘詞空 = 真的省略
                    # 商品名（「調100過來南倉」）→ 才補 ctx。代詞殘留視為省略。
                    _tf80_named = (len(_tf80_stem) >= 1
                                   and _tf80_stem not in ("那", "這", "它", "他", "個",
                                                          "些", "它的", "那個", "這個"))
                    # ── EN build：上面整段剝詞表**全是中文**（調/撥/北倉/個件…），
                    #   英文句一個都剝不掉 → 殘詞恆非空 → 永遠判定「有講商品」
                    #   → 代稱式調撥（`transfer 10 from north to south`）接不上
                    #   脈絡，害訪客要再講一次商品名（長對話測 2026-08-02 抓到）。
                    #   契約鐵律不變：**剝掉調撥結構後殘詞為空**才補 ctx；
                    #   `transfer 10 umbrellas from…` 的 umbrellas 是明確講出
                    #   但查無 → 誠實查無，絕不頂替。
                    if _is_mostly_english(user_text):
                        _tfen = user_text.lower()
                        _tfen = _re.sub(
                            r"\b(?:transfer|move|send|ship|shift|relocate|"
                            r"from|to|into|out of|the|a|an|of|please|"
                            r"north|central|south|warehouse|units?|pcs|pieces?|"
                            r"item|items|some|all)\b", " ", _tfen)
                        _tfen = _re.sub(r"[0-9]+", " ", _tfen)
                        _tfen = _re.sub(r"[^a-z]+", " ", _tfen).strip()
                        _tf80_named = bool(_tfen)
                        if not _tf80_named:
                            log.info(f"[ctx-tf-en] 剝完殘詞為空＝省略商品名: {user_text!r}")

                    if (not (_tf80_m and _tf80_m[0].get("score", 0) >= 3)
                            and not _tf80_named):
                        # 句中確實沒講商品 → 注入 ctx 商品名到句首
                        _sku_ctx = _ctx_for(vid)['last_sku']
                        # EN：英文要用空格接，不能像中文直接黏（黏了對不到主檔）
                        user_text = (f"{_sku_ctx} {user_text}"
                                     if _is_mostly_english(user_text)
                                     else f"{_sku_ctx}{user_text}")
                        log.info(f"[ctx-tf] 純調貨補商品 → {user_text!r}")

                # ── r82 危險級：代詞寫入句（「北倉那個補一下 進20」）——「那個」
                #   指剛查的商品，用 ctx last_sku 替換代詞，避免 C13b 抽不到真商品
                #   後 fuzzy 亂中低分商品開錯卡（蚊香被誤補）。寫入代詞補全安全
                #   （訪客明確指上一個）；無 ctx 則不動、讓後續誠實查無 ──
                _pw82 = _re.search(r"(那個|這個|那支|這支|那款|這款|那批)\s*"
                                   r"[^。]{0,4}?[進出補調撥挪]\s*[0-9]", user_text)
                if _pw82 and _ctx_for(vid).get("last_sku"):
                    import warehouse as _W_pw82
                    # 句中若已有真商品名就不替（避免「北倉那個藍牙耳機補20」誤動）
                    _pw82_kw = _extract_sku_keyword(user_text) or ""
                    _pw82_has = (_pw82_kw and _W_pw82.match_items(_pw82_kw)
                                 and _W_pw82.match_items(_pw82_kw)[0].get("score", 0) >= 3)
                    if not _pw82_has:
                        user_text = user_text.replace(_pw82.group(1),
                                                      _ctx_for(vid)["last_sku"], 1)
                        log.info(f"[ctx-pron-write] 代詞寫入補商品 → {user_text!r}")

                # r65：行內糾錯（「出10個 打錯 是出20個」）→ 先化簡成更正後的量，
                # 再交給 ctx 展開補商品/倉（rewrite 表跑在展開之後，來不及）
                _corr65 = _re.fullmatch(
                    r"(?:[進出調]\s*\d+\s*[個件]?)\s*(?:打錯|說錯|講錯|不對)\s*是?"
                    r"([進出調])?\s*(\d+)\s*[個件]?", user_text.strip())
                if _corr65:
                    _cv65 = _corr65.group(1) or user_text.strip()[0]
                    user_text = f"{_cv65}{_corr65.group(2)}個"
                    log.info(f"[inline-fix] 糾錯句 → {user_text!r}")

                # ── r57/r62/r59（r70 上移到展開之前）：config 裸改值/設定追問/最急批
                #   ctx 改寫——「北倉的調成400」的「調」曾先觸發寫入展開把句子吃掉 ──
                # r78：「那全部倉都改150好了」——全倉形也接（group1 空＝全倉）
                _cfg_bare57 = _re.fullmatch(
                    r"(?:那|嗯|就)?(?:([北中南])(?:區)?倉的?|((?:全部|三個?|各)倉)都?的?)?"
                    r"(改回|改成?|設成?|調成?|調回)\s*([0-9]{1,6})\s*(好了|吧|喔)?",
                    user_text.strip())
                if (_cfg_bare57 and _ctx_for(vid).get("last_sku")
                        and vid not in _pending_by_vid):
                    _cfg_wh61 = f"{_cfg_bare57.group(1)}倉" if _cfg_bare57.group(1) else ""
                    # r78v：全倉形要把「全部」寫進改寫句——丟掉會讓下游平台各自
                    # 亂猜倉別（RPI5 曾猜成中區倉 1 項）
                    _cfg_all78 = "全部" if _cfg_bare57.group(2) else ""
                    user_text = (f"{_cfg_wh61}{_ctx_for(vid)['last_sku']}"
                                 f"安全庫存{_cfg_all78}改成{_cfg_bare57.group(4)}")
                    log.info(f"[ctx-cfg] 裸改值 → {user_text!r}")
                _cfgq70 = _re.fullmatch(
                    r"(現在|目前)?(?:([北中南])(?:區)?倉)?的?設定(是)?(多少)?[?？。!！]*",
                    user_text.strip())
                if (_cfgq70 and _ctx_for(vid).get("last_func") == "manage_config"
                        and _ctx_for(vid).get("last_sku")):
                    _cfgq70_wh = f"{_cfgq70.group(2)}倉" if _cfgq70.group(2) else ""
                    user_text = f"{_cfgq70_wh}{_ctx_for(vid)['last_sku']}安全庫存是多少"
                    log.info(f"[ctx-cfg] 設定追問 → {user_text!r}")
                # r78：「改完了嗎 現在北倉多少」——config 寫入後的生效驗證追問，
                # 曾掉「沒有『改完』這個商品」醜 clarify
                _cfgv78 = _re.search(r"(改完|改好|生效|改了|設好)(了?嗎|沒|了沒)",
                                     user_text)
                if (_cfgv78 and _ctx_for(vid).get("last_func") == "manage_config"):
                    if _ctx_for(vid).get("last_sku"):
                        _cfgv78_wh = next((f"{z}倉" for z in "北中南"
                                           if z in user_text), "")
                        user_text = (f"{_cfgv78_wh}{_ctx_for(vid)['last_sku']}"
                                     "安全庫存是多少")
                    else:
                        # r85：全倉全商品改後「改完沒」→ 回安全庫存總表
                        user_text = "安全庫存設定"
                    log.info(f"[ctx-cfg] 生效驗證追問 → {user_text!r}")
                if (_ctx_for(vid).get("last_func") == "list_expiring_items"
                        and len(user_text) <= 12
                        and _re.search(r"最緊?急", user_text)
                        and _re.search(r"批|放哪|在哪", user_text)):
                    log.info(f"[ctx-exp] 最急批追問 → 快過期的有哪些（原 {user_text!r}）")
                    user_text = "快過期的有哪些"

                # ── r32 追問展開：「那個進出紀錄呢」→「無線滑鼠進出紀錄」──
                # r74：schedule_list 後的「刪掉它」，「它」指的是排程不是 ctx 商品——
                # 展開會變「刪掉行動電源」誤入商品刪除流程，跳過展開讓刪除閘接手
                _sd74_skip = (_ctx_for(vid).get("last_view") in
                              ("schedule_list", "alert_list", "schedule_done", "alert_done")
                              and _re.fullmatch(
                                  r"(把|幫我)?(剛加的?|剛剛的?|最新的?|剛排的?|剛設的?)?"
                                  r"(它|這個|那個|那條|那筆)?"
                                  r"(刪掉|刪除|移除|取消|停掉|刪了)(它|這個|那個)?(吧|喔|囉|好了)?",
                                  user_text.strip().strip("!！?？。 ")))
                # r82：招呼句（「安安 剛剛做到哪」）不可被 ctx_expand 把「做到哪」
                # 當追問展開成「電動牙刷做到哪」——招呼開頭即跳過展開，讓下方
                # 招呼 gate 接手
                _greet_skip82 = bool(_re.match(
                    r"^(哈囉|嗨+|hi|hello|安安|你好|大家好|早+)[啊呀!！~～\s]",
                    user_text.strip(), _re.IGNORECASE))
                if not _sd74_skip and not _greet_skip82:
                    user_text = _ctx_expand(vid, user_text)

                # r33：一進門就用代詞追問（沒有上一輪可指）→ 過去回全店統計/60 項概覽，
                #   訪客看得一頭霧水。沒有 context 就老實問他要查哪個商品。
                #   ⚠️ 代詞≠追問：「這個帳篷賣多少錢」「那個煮咖啡的庫存」是**描述句**
                #   （代詞後面接的就是商品），「這個月營收多少」的「這個」是時間片語。
                #   → 一定要先接地：句中抽得到真商品、或含時間/全域詞 → 不是純追問。
                if (not _ctx_for(vid).get("last_sku")
                        and len(user_text) <= 10
                        and any(p in user_text for p in _CTX_PRON)
                        and not any(w in user_text for w in _CTX_TIME_WORDS)
                        and not any(w in user_text for w in _CTX_GLOBAL)
                        # r74：schedule_list/alert_list（r75 +done 態）後的「刪掉它/
                        # 剛排的那個刪了」指清單項目，讓給刪除閘的排程/警示分支
                        and not (_ctx_for(vid).get("last_view") in
                                 ("schedule_list", "alert_list", "schedule_done", "alert_done")
                                 and any(w in user_text for w in ("刪", "移除", "取消", "停掉")))
                        and not _has_real_item(user_text)):
                    _np_msg = ("請問你想查哪個商品呢？直接說商品名就可以，"
                               "例如「藍牙耳機庫存」「無線滑鼠進出紀錄」。")
                    log.info(f"[ctx-empty] 無 context 的代詞句 → clarify: {user_text!r}")
                    for ch in _np_msg:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "clarify", "summary": _np_msg,
                        "data": {"question": _np_msg, "options": [], "hint": ""}}})
                    continue

            _desc_kw_ws = _descriptor_hit(user_text)   # rewrite 前偵測（rewrite 會換掉描述）
            user_text = _rewrite_query(user_text)
            # r77：存本句最終文字給 _ctx_absorb 判斷（寫入句查無 → 作廢舊卡）
            _ctx_for(vid)["_cur_text"] = user_text
            if LLM is None:
                msg = HEALTH.get("message") or "系統還在啟動中"
                if HEALTH.get("stage") == "failed":
                    msg = f"系統啟動失敗：{HEALTH.get('error') or '未知錯誤'}"
                await send({"type": "error", "text": msg})
                continue

            log.info(f"User vid={vid}: {user_text}")
            await push_display({
                "type":      "trace",
                "stage":     "user_input",
                "user_text": user_text,
            })

            # ── r51：clarify 選單序數選擇——「咖啡對應到5個商品」後說「第一個」
            #   曾回「沒有『第一個』這個商品」。有選單在場且句子是純序數 → 代入選項。──
            # 講法收斂：第一個/選項1/第一項/項目一/我要第一個/1號/最上面那個/最後一個 全吃
            _ord_txt = user_text.strip()
            _ord51 = _re.fullmatch(
                r"(?:我?要|選|給我|就)?\s*(?:選項|項目|第)?\s*([一二兩三四五六七八12345678])\s*(?:個|項|號)?\s*(?:好了|吧|喔)?[?？。!！]*",
                _ord_txt)
            _ord_last = _re.fullmatch(r"(?:我?要|選)?\s*最後(?:一個|一項|那個)?\s*(?:好了|吧)?[?？。!！]*", _ord_txt)
            _ord_first = _re.fullmatch(r"(?:我?要|選)?\s*(?:最上面|頭一個|頭一項)(?:那個)?\s*(?:好了|吧)?[?？。!！]*", _ord_txt)
            # ── EN build（branch_walk r1）：序數 regex 原本全中文 → 英文訪客
            #   說 'the first one' 被當商品名查詢 → rejected /「No item
            #   matching "first one"」。語音輸入時代點不了按鈕，序數是最自然
            #   的選法（中文版註解已寫明），英文必須對等支援。
            if not (_ord51 or _ord_last or _ord_first):
                _ord_en_num = _re.fullmatch(
                    r"(?:i(?:'?d)?\s*(?:want|like|pick|choose|take)\s*)?"
                    r"(?:the\s*)?(?:option\s*|item\s*|number\s*|no\.?\s*|#)?"
                    r"(?:(first|second|third|fourth|fifth|sixth|seventh|eighth)|([1-8]))"
                    r"\s*(?:one|option|item)?\s*(?:please|thanks)?[?.!]*",
                    _ord_txt, _re.I)
                _ord_en_last = _re.fullmatch(
                    r"(?:i(?:'?d)?\s*(?:want|like|pick|choose|take)\s*)?"
                    r"(?:the\s*)?last\s*(?:one|option|item)?\s*(?:please|thanks)?[?.!]*",
                    _ord_txt, _re.I)
                if _ord_en_last and _clarify_opts_by_vid.get(vid):
                    _ord_last = _ord_en_last
                elif _ord_en_num and _clarify_opts_by_vid.get(vid):
                    _EN_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4,
                               "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8}
                    _oi_en = (_EN_ORD.get((_ord_en_num.group(1) or "").lower())
                              or int(_ord_en_num.group(2) or 0))
                    _opts_en = _clarify_opts_by_vid[vid]
                    if 1 <= _oi_en <= len(_opts_en):
                        log.info(f"[ordinal-select EN] vid={vid} {user_text!r} → "
                                 f"選項{_oi_en}「{_opts_en[_oi_en-1]}」")
                        user_text = str(_opts_en[_oi_en - 1])
            if (_ord51 or _ord_last or _ord_first) and _clarify_opts_by_vid.get(vid):
                _ORD_MAP = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5,
                            "六": 6, "七": 7, "八": 8}
                if _ord_last:
                    _oi = len(_clarify_opts_by_vid[vid])
                elif _ord_first:
                    _oi = 1
                else:
                    _oi = _ORD_MAP.get(_ord51.group(1)) or int(_ord51.group(1))
                _opts51g = _clarify_opts_by_vid[vid]
                if 1 <= _oi <= len(_opts51g):
                    log.info(f"[ordinal-select] vid={vid} 「{user_text}」→ 選項{_oi}「{_opts51g[_oi-1]}」")
                    user_text = str(_opts51g[_oi - 1])
            elif _clarify_opts_by_vid.get(vid) and (_ord_attr := _re.fullmatch(
                    r"(?:第)?([一二兩三四五六七八12345678])(?:個|項|名)?"
                    r"(多少錢|單價|剩多少|還剩幾個|庫存|快到期嗎)[?？]*", _ord_txt)):
                # r68：選單序數+屬性後綴（「第二個多少錢」曾回 60 項概覽）——
                # 取該選項的商品主幹，接上屬性問句
                _ORD_MAP68 = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
                              "五": 5, "六": 6, "七": 7, "八": 8}
                _oi68 = _ORD_MAP68.get(_ord_attr.group(1)) or int(_ord_attr.group(1))
                _opts68 = _clarify_opts_by_vid[vid]
                if 1 <= _oi68 <= len(_opts68):
                    _core68 = _re.sub(r"\s*(庫存|多少錢|進了?\d+件?|安全庫存.*)$", "",
                                      str(_opts68[_oi68 - 1])).strip()
                    # r70：選項主幹必須是真商品才改寫——功能型選單（「你想查食品類
                    # 的什麼」的選項是庫存/進出）曾被拼成「進出紀錄剩多少」亂配
                    if _has_real_item(_core68):
                        user_text = f"{_core68}{_ord_attr.group(2)}"
                        log.info(f"[ordinal-attr] 選項{_oi68}+屬性 → {user_text!r}")
            elif _ord51 and _ctx_for(vid).get("last_func") == "list_hot_items":
                # r56 fuzz：排行榜後裸序數（「第二個」）＝問那一名——改寫成
                # 「第N名剩多少」讓排行追問路（r43/r55）接手，期間自動沿用
                _ORD_MAP56 = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5,
                              "六": 6, "七": 7, "八": 8}
                _oi56 = _ORD_MAP56.get(_ord51.group(1)) or int(_ord51.group(1))
                log.info(f"[ordinal-rank] vid={vid} 「{user_text}」→ 排行第{_oi56}名")
                user_text = f"第{_oi56}名剩多少"
            elif ((_ctx_for(vid).get("last_func") == "list_hot_items"
                   or _ctx_for(vid).get("last_hot_period"))
                  and _re.fullmatch(r"第[一二兩三四五六七八九十\d]+名的?(是)?(什麼|哪個|啥|誰)?"
                                    r"[呢勒咧?？。!！]*", _ord_txt)):
                # r57：「第五名是什麼」——排行身分追問光句面過不了守門員（無功能詞），
                # 前綴補「熱銷排行」讓它自然走排行追問路
                user_text = f"熱銷排行{_ord_txt}" + ("是什麼" if "是" not in _ord_txt else "")
                log.info(f"[ordinal-rank] vid={vid} 身分追問 → {user_text!r}")

            # （r70：裸改值/設定追問/最急批 三組 ctx 改寫已上移到 ctx_expand 之前——
            #   「北倉的調成400」的「調」曾先觸發寫入展開把句子吃掉）

            # ── r55 收官批：告別/道謝句友善回應——「掰掰」「謝謝辛苦了」曾回
            #   守門員教學文（展場冷場）。純客套短句 → 溫暖收尾；混雜其他內容不攔。──
            # ── r57：接續詞（「換一個」「下一個」「繼續」）——訪客要看別的但沒說
            #   是什麼（曾回「沒有『換一個』這個商品」醜 clarify）──
            if _re.fullmatch(r"(那)?(換|下|再來)一?個(呢|吧|好了)?|繼續|再來|然後呢",
                             user_text.strip().strip("!！?？。~～ ")):
                _nx_msg = ("想看哪個商品呢？直接說名稱就可以——例如「藍牙喇叭庫存」；"
                           "或說「商品清單」看全部 60 項。")
                log.info(f"[continue-word] {user_text!r} → 請指名商品")
                for ch in _nx_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "clarify", "summary": _nx_msg,
                    "data": {"question": _nx_msg, "options": [], "hint": ""}}})
                continue

            # ── EN build（r5）：英文招呼句 ──────────────────────────────────
            #   原中文正則只含 hi|hello 兩個英文詞 → hey/morning/howdy 等全 miss
            #   （掉進商品比對變醜 clarify），而命中的 hi/hello **回中文**
            #   ——展場英文訪客的第一句話就看到中文。
            #   依坑 7「加法」原則：獨立英文分支，不動中文正則。
            #   ⚠️ 一律 fullmatch + 短句：'morning shift report' 這種不可攔
            #   （morning/hey 都可能出現在正常句子裡）。
            _gr_en = None
            if _is_mostly_english(user_text):
                _gr_en = _re.fullmatch(
                    r"(?:hi+|hey+|hello+|yo|hiya|howdy|greetings|"
                    r"good\s*(?:morning|afternoon|evening)|morning|afternoon)"
                    r"[\s,!~]*"
                    r"(?:there|folks|guys|everyone|all)?[\s,!~]*"
                    # 招呼+閒聊尾巴（'hi there busy today'）——曾撈到 Coffee
                    # Machine 變醜 clarify，正解是招呼回覆
                    r"(?:busy\s*today|whats\s*up|what'?s\s*up|hows?\s*it\s*going|"
                    r"how\s*are\s*(?:you|u)|you\s*there|anyone\s*(?:there|home))?",
                    user_text.strip().strip("!！?？。. "), _re.IGNORECASE)
            if _gr_en:
                _gr_msg = ("Hi! I'm your warehouse assistant. Want to start with "
                           "\"what's running low\" or \"best sellers this week\"? "
                           "You can also just ask directly — for example "
                           "\"bluetooth earphones stock\".")
                log.info(f"[greet-en] {user_text!r} → 英文招呼回覆")
                for ch in _gr_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "guide", "summary": _gr_msg, "data": {}}})
                continue

            # ── 直達層（2026-08-04 第七輪配套）────────────────────────────
            #   ① live 問句：訪客盯著跳動數字必問「why do the numbers keep
            #     changing」,曾掉 RCA 對帳報告（答非所問）。
            _lq_en = _re.search(
                r"(?:why|how\s+come)\b.{0,30}\b(?:numbers?|figures?|stock|data)\b"
                r".{0,24}\b(?:chang|mov|jump|updat|different)|"
                r"\bis\s+th(?:is|at|ese)\s+(?:real[- ]?time|live|real)\b|"
                r"\breal[- ]?time\s+data\b|\blive\s+(?:data|mode)\b|"
                r"\bnumbers?\s+(?:keep\s+)?(?:changing|moving|jumping)\b",
                user_text, _re.I)
            _lq_has_item = False
            if _lq_en:
                try:
                    import warehouse as _W_lq
                    _lq_m = _W_lq.match_items(user_text)
                    _lq_has_item = bool(_lq_m and _lq_m[0].get("score", 0) >= 6)
                except Exception:
                    _lq_has_item = False
            if _lq_en and _is_mostly_english(user_text) and not _lq_has_item:
                _lq_msg = ("Yes — Live mode is on! The warehouse simulates real "
                           "operations (PDA scans, WMS sync, e-commerce orders), "
                           "so stock numbers keep updating just like a real "
                           "warehouse. Ask me anything about the moving stock — "
                           "try \"what's running low\" or "
                           "\"export movements yesterday\".")
                log.info(f"[live-qa] {user_text!r} → live 模式說明")
                for ch in _lq_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "guide", "summary": _lq_msg, "data": {}}})
                continue
            #   ② 產出完成後的追問（180 秒窗口防陳舊 context）
            _pex = _export_done_by_vid.get(vid)
            if _pex and __import__("time").time() - _pex[1] < 180:
                _dl_en = _re.fullmatch(
                    r"(?:can|could|may)?\s*i?\s*(?:download|open|get|save|view)\s*"
                    r"(?:it|that|this|the\s+(?:file|report|csv))?\s*"
                    r"(?:please)?[?.! ]*", user_text.strip(), _re.I)
                if _dl_en:
                    _dl_msg = ("Sure — tap 📊 [Open report] to view it in a new "
                               "tab, or ⬇ [Download CSV] on the card above to "
                               "save the file.")
                    log.info("[post-export] 下載指路")
                    for ch in _dl_msg:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "guide", "summary": _dl_msg,
                        "data": {}}})
                    continue
                #   期間追問改寫成 canonical 句 → 走完整已驗證鏈（選單/確認/days）
                _pp_en = _re.fullmatch(
                    r"(?:and|what\s+about|how\s+about|also|now|then)?\s*(?:the)?\s*"
                    r"(last\s+(?:week|month|quarter)|previous\s+(?:week|month)|"
                    r"past\s+(?:week|month)|yesterday)\s*"
                    r"(?:too|as\s+well|please|now)?\s*[?.! ]*",
                    user_text.strip(), _re.I)
                if _pex[0] == "export" and _pp_en:
                    user_text = "export movements " + _pp_en.group(1).lower()
                    log.info(f"[post-export] 期間追問改寫 → {user_text!r}")
            #   ③ 無卡 confirm（第七輪 S15：曾回 60 項概覽,ZH 版是正確引導）
            if (_re.fullmatch(r"(?:ok\s*)?(?:confirm(?:\s*it)?|go\s+ahead|"
                              r"yes\s+confirm|approve)[?.! ]*",
                              user_text.strip(), _re.I)
                    and vid not in _pending_by_vid):
                _nc_msg = ("There's nothing waiting for confirmation right now. "
                           "Ask me to export movements or run a report first, "
                           "then confirm the card that appears.")
                log.info("[no-pending-confirm] 無卡確認 → 引導")
                for ch in _nc_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "guide", "summary": _nc_msg, "data": {}}})
                continue

            # r76：招呼開場（「哈囉開店啦」）曾掉「沒有這個商品」醜 clarify
            _gr_m82 = _re.fullmatch(
                r"(哈囉|嗨+|hi|hello|安安|你好|大家好|早+)[啊呀!！~～\s]*"
                r"(開店|開工|上班|來了|開始)?[啦囉了喔吧]?"
                # r82：招呼+問進度（「安安 剛剛做到哪」）——demo 無 session 記憶
                r"(\s*剛剛?做到哪|\s*做到哪了?|\s*剛剛?做啥|\s*上次做到哪)?",
                user_text.strip().strip("!！?？。 "), _re.IGNORECASE)
            if _gr_m82:
                _gr_prog = bool(_gr_m82.group(3))
                _gr_msg = (("嗨！這個 demo 每次都是全新開始（不保留上次操作）。"
                            "要看「庫存警示」還是「本週熱銷排行」？") if _gr_prog else
                           "早安！我是倉管助理。要不要先看「庫存警示」或「本週熱銷排行」？"
                           "也可以直接問，例如「藍牙耳機庫存」。")
                log.info(f"[greet] {user_text!r} → 招呼回覆")
                for ch in _gr_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "guide", "summary": _gr_msg, "data": {}}})
                continue

            # ── r83：倉別更正句（「欸不是南倉 是北倉啦」）——接在寫入 done 後，
            #   卡已執行來不及改，誠實說明已記錄+怎麼補救，不誤判成倉庫比較 ──
            _wc_fix83 = _re.search(r"不是\s*([北中南])(?:區)?倉?\s*[，,]?\s*是?\s*"
                                   r"([北中南])(?:區)?倉", user_text)
            if (_wc_fix83 and len(user_text) <= 16
                    and _ctx_for(vid).get("last_view") in
                    ("movement_done", "transfer_done")):
                _wf83_wrong = f"{_wc_fix83.group(1)}區倉"
                _wf83_right = f"{_wc_fix83.group(2)}區倉"
                _wf83_sku = _ctx_for(vid).get("last_sku") or "該商品"
                _wf83_msg = (f"剛剛那筆已經記錄到{_wf83_wrong}了（來不及改）。"
                             f"要移到{_wf83_right}的話，直接說"
                             f"「{_wf83_wrong}調到{_wf83_right} {_wf83_sku}」就可以。")
                log.info(f"[wh-fix-r83] 寫入後倉別更正 → 說明: {user_text!r}")
                for ch in _wf83_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "clarify", "summary": _wf83_msg,
                    "data": {"question": _wf83_msg, "options": [], "hint": ""}}})
                continue

            # r57：告別+道謝混雜（「辛苦了88」）也要接——token 聯集全句比對，
            # 有告別詞就走告別回覆，否則道謝回覆
            _FW_BYE_TOK = (r"掰掰|掰|拜拜|再見|bye+|88+|886|明天見|下次見|下次再來|"
                           r"先走了|我走了|走囉|閃人|告辭|沒事|沒了|下班了?|回家了?|各位|收工|"
                           r"明天再[處說弄用]理?|明天再說|881|[先去]去?忙|有事叫我|"
                           # EN build（r5）：英文告別。原本只有 bye+ 一個詞，
                           #   see you / goodbye / later 全 miss → 掉商品比對變醜回答。
                           r"good\s*bye|see\s*(?:you|ya|yah)(?:\s*(?:later|around|soon))?|"
                           r"later|catch\s*you\s*later|cya|take\s*care|"
                           r"im\s*off|i'?m\s*off|gotta\s*(?:go|run)|heading\s*(?:off|out)|"
                           r"have\s*a\s*(?:good|nice|great)\s*(?:one|day|night|evening)|"
                           r"good\s*night")
            _FW_THX_TOK = (r"謝謝你?們?|謝了|謝啦|多謝|感謝|感恩|3q|thx|thanks?|thank\s*you|"
                           r"辛苦了|辛苦囉|辛苦你了|好棒|太強了|厲害|完美|讚讚?|就這樣|沒問題了|"
                           r"差不多了|就到這|先這樣|都?沒問題|[就先]?巡到這|看到這裡?|"
                           r"ok瞭解|okay|ok|瞭解|我?知道了|就[醬降]|大家辛苦了?|懂了|"
                           # EN build（r5）：英文道謝＋讚美收尾
                           r"thank\s*(?:you|u)\s*(?:so|very)\s*much|thanks\s*(?:a\s*lot|"
                           r"a\s*bunch|so\s*much|very\s*much|again|for\s*(?:your\s*)?help)|"
                           r"cheers|appreciate\s*(?:it|that)|much\s*appreciated|"
                           r"(?:thats|that'?s)\s*(?:all|it|great|perfect|helpful)|"
                           r"nice\s*one|awesome|perfect|great\s*stuff|good\s*stuff|"
                           # 2026-08-04 第七輪：裸 'cool' 曾掉「查無商品 cool」
                           r"cool|sweet|neat|nice|love\s*it|"
                           r"got\s*it|understood|makes\s*sense|"
                           r"(?:youre|you'?re)\s*(?:the\s*best|great|awesome)")
            # r63：允許開頭客套填充（「好啦下班了 掰」的「好啦」）
            # r67：+今天/那我/我先（「今天就到這 感謝」）
            # EN build（r5）：英文開頭填充（'ok thanks' / 'alright bye' / 'well thanks'）
            _fw_all = _re.fullmatch(
                rf"(?:今天|那我|我先|那就|ok|okay|alright|all\s*right|well|"
                rf"anyway|right)?[\s好啦嗯哦喔,]*(?:(?:{_FW_BYE_TOK}|{_FW_THX_TOK})"
                rf"[\s啦嘍囉喔哦耶呀呦唷了~～!！?？。.，,]*)+",
                user_text.strip(), _re.IGNORECASE)
            _fw_bye = _fw_all and _re.search(rf"(?:{_FW_BYE_TOK})", user_text, _re.IGNORECASE)
            _fw_thx = _fw_all and not _fw_bye
            if ((_fw_bye or _fw_thx)
                    and not _item_create_state_ws.get(vid, {}).get("active")
                    and not _item_delete_state.get(vid)):   # 流程中「88」是欄位值不是告別
                # EN build（r5）：訊息依輸入語言分流——英文訪客收到中文收尾
                #   是展場最後一眼看到的東西（r5 抓到）
                if _is_mostly_english(user_text):
                    _fw_msg = ("👋 Bye! Thanks for stopping by — come back anytime "
                               "to check stock or movements."
                               if _fw_bye else
                               "😊 You're welcome! Glad it helped. Ask me anything else — "
                               "stock, movements, low-stock alerts, all fair game.")
                else:
                    _fw_msg = ("👋 掰掰，謝謝光臨！下次想查庫存、進出貨隨時再來。"
                               if _fw_bye else
                               "😊 不客氣，有幫上忙就好！還想查什麼隨時說——庫存、進出貨、缺貨警示都可以。")
                log.info(f"[farewell] {user_text!r} → 客套收尾")
                for ch in _fw_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "guide", "summary": _fw_msg, "data": {}}})
                continue

            # ── r74：排程/警示刪除選擇模式——「刪掉它」多筆 clarify 承諾「請說
            #   ID」，訪客回 AL001/SCH001 或「第一個」就要接得住（一次性狀態，
            #   答非 ID 即清空放行走正常管線）──
            _ds74 = _del_select_by_vid.pop(vid, None)
            if _ds74:
                _ds_txt = user_text.strip().strip("。.!！?？，, ")
                _ds_ids = _ds74.get("ids") or []
                _ds_id = None
                # EN build：前綴/後綴詞補英文（原本只認中文的「刪掉 AL001 吧」）
                _m_id74 = _re.fullmatch(
                    r"(?:刪|刪掉|刪除|移除|delete|remove|drop|kill)?\s*"
                    r"((?:al|sch)\s*\d{1,3})\s*(?:吧|好了|囉|please|thanks?)?",
                    _ds_txt, _re.IGNORECASE)
                if _m_id74:
                    _c74 = _m_id74.group(1).replace(" ", "").upper()
                    _p74 = _re.match(r"[A-Z]+", _c74).group(0)
                    _n74 = int(_c74[len(_p74):])
                    _cand74 = f"{_p74}{_n74:03d}"
                    if _cand74 in _ds_ids:
                        _ds_id = _cand74
                else:
                    # EN build：序數路原本只認中文（第一個/一二三）→ 英文訪客答
                    #   'the first one' / '#2' 接不住，clarify 承諾跳票
                    _m_ord74 = _re.fullmatch(
                        r"(?:第)?([一二三四五12345])(?:個|條|筆)?"
                        r"|(?:the\s+)?(first|second|third|fourth|fifth|last)(?:\s+one)?"
                        r"|#?([12345])(?:st|nd|rd|th)?",
                        _ds_txt, _re.IGNORECASE)
                    if _m_ord74:
                        _g_zh, _g_en, _g_num = _m_ord74.groups()
                        _i74 = None
                        if _g_zh:
                            _i74 = ({"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
                                    .get(_g_zh) or int(_g_zh))
                        elif _g_en:
                            _e74 = _g_en.lower()
                            _i74 = (len(_ds_ids) if _e74 == "last" else
                                    {"first": 1, "second": 2, "third": 3,
                                     "fourth": 4, "fifth": 5}[_e74])
                        elif _g_num:
                            _i74 = int(_g_num)
                        if _i74 and 1 <= _i74 <= len(_ds_ids):
                            _ds_id = _ds_ids[_i74 - 1]
                if _ds_id:
                    import tools_v2 as _tv2_ds74
                    result = (_tv2_ds74.delete_schedule(job_id=_ds_id)
                              if _ds74.get("kind") == "sched"
                              else _tv2_ds74.delete_alert(rule_id=_ds_id))
                    log.info(f"[del-select-r74] {_ds74.get('kind')} 選擇 {_ds_id}")
                    for ch in result.get("summary", ""):
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": result})
                    continue

            # ── EN build：「delete AL003 / remove SCH001」明確 ID 直達 ──
            #   r8 渲染批抓到：alert_list 卡片右下明明寫著
            #   `To remove, say "delete AL001"`，訪客照打卻回
            #   `No item matching "delete"`（clf 判 query_inventory → oov:noex）。
            #   ⚠️ 必須放在黑名單閘門**之前**：`delete the` 在黑名單裡，
            #     `delete the alert AL003` 會被當破壞句擋掉。
            #   安全前提（同坑 8 補充「攔進 tool 前先確認參數抽得到」）：
            #     只有 ID **真的存在於現有規則/排程**才攔，否則放行走原路由——
            #     所以 `delete all` / `delete AL999` 不會被這條吃掉。
            _m_delid = _re.search(
                r"\b(?:delete|remove|drop|cancel|kill|get\s+rid\s+of)\b[^A-Za-z0-9]*"
                r"(?:the\s+)?(?:alert|rule|schedule|job|reminder)?[^A-Za-z0-9]*"
                r"\b(al|sch)\s*(\d{1,3})\b",
                user_text, _re.IGNORECASE)
            if _m_delid:
                import tools_v2 as _tv2_delid
                _pfx_di = _m_delid.group(1).upper()
                _id_di = f"{_pfx_di}{int(_m_delid.group(2)):03d}"
                _sched_di = _pfx_di == "SCH"
                # 接地：ID 必須存在，否則不攔（讓原管線去回它該回的話）
                _exist_di = _tv2_delid.list_schedules() if _sched_di \
                    else _tv2_delid.list_alerts()
                _ids_di = [x["id"] for x in (_exist_di.get("data", {}).get(
                    "jobs" if _sched_di else "rules") or [])]
                if _id_di in _ids_di:
                    result = (_tv2_delid.delete_schedule(job_id=_id_di) if _sched_di
                              else _tv2_delid.delete_alert(rule_id=_id_di))
                    log.info(f"[del-id-en] vid={vid} {_id_di} → {result.get('view')}")
                    for ch in result.get("summary", ""):
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": result})
                    continue

            # ── 黑名單閘門（最高優先，在刪除/商品清單等任何功能攔截之前）──
            # HTTP 端守門員本來就在功能攔截之前，WS 端順序相反導致「把庫存
            # 全部刪掉」等搗蛋句先被刪除攔截接走，黑名單沒機會擋（第17輪）。
            # 描述命中+查詢語氣的句子豁免黑名單（「裝便當的還有嗎」的「便當」、
            # 「放音樂的還剩幾台」的「音樂」是防閒聊黑名單詞，但這裡語境明確
            # 是查商品 → 讓功能描述直達接手，2026-07-07）。
            # ⚠️ 安全修補（r16）：豁免不可涵蓋「破壞類」黑名單詞——「刷牙的庫存
            # 清空」曾靠 描述命中+「庫存」cue 繞過黑名單。豁免只給閒聊類誤傷
            # （便當/音樂），破壞/注入類（刪/清空/歸零/0元/密碼/權限…）永不豁免。
            _BL_NEVER_EXEMPT = ("刪", "清空", "清掉", "清光", "歸零", "格式化",
                                "0元", "1元", "零元", "密碼", "權限", "重開", "關機",
                                "rm -rf", "drop table", "無視", "忽略", "system prompt")
            _desc_exempt_ws = bool(_descriptor_hit(user_text)
                                   and any(c in user_text.lower() for c in _DESC_GATE_CUES)
                                   and not any(w in user_text.lower() for w in _BL_NEVER_EXEMPT))
            _bl_hit_ws = None if _desc_exempt_ws else next(
                (b for b in _GATEKEEPER_BLACKLIST if b in user_text.lower()), None)
            _gk_admin_pass = False   # 黑名單豁免（排程/警示管理句）→ 守門員也要放行
            # r62：倉管退貨句豁免（同 is_meaningful_input 的豁免，兩處要同步）
            #   r101：`\d` 只認阿拉伯數字 → 「退貨二十個滑鼠」（中文數字）漏豁免
            #   被 rejected（真人語音 #31）。改認阿拉伯＋中文數字，與 256 行同步。
            if (_bl_hit_ws in ("退貨", "退換貨")
                    and _re.search(r"退[貨回]?\s*(?:\d|[零一二兩三四五六七八九十百千])", user_text)
                    and (_re.search(r"[北中南][倉區]", user_text)
                         or _has_real_item(user_text))):
                _bl_hit_ws = None
            # r77：期間退貨統計查詢豁免（同 is_meaningful_input，兩處同步）
            if (_bl_hit_ws in ("退貨", "退換貨")
                    and _re.search(r"(上週|本週|今天|昨天|上個?月|本月).{0,4}退貨"
                                   r"|退貨.{0,8}(幾件|多少件|統計|記錄|記在哪)", user_text)):
                _bl_hit_ws = None
            # ⚠️ EN build：破壞短語 × **合法管理受詞**豁免（與 is_meaningful_input
            #   的同名豁免兩處同步——WS 端有自己一份黑名單檢查，只改那邊沒用）。
            #   'delete the schedule' / 'cancel the alert rule' 是合法管理操作，
            #   但黑名單有 'delete the'（防 'delete the database'）→ 被擋成搗蛋。
            #   放行後由 Pre-C-Sched 接手，它只**列清單**讓訪客指名、不做批量刪除。
            if (_bl_hit_ws
                    and (_re.search(r"\b(?:delete|remove|cancel|clear|drop|turn off|"
                                    r"disable|stop)\b.{0,12}\b(?:schedules?|alerts?|"
                                    r"alert rules?|reminders?|rules?|jobs?)\b",
                                    user_text, _re.I)
                         # 劇情批 r1：'delete the one i just made' —— 指代前一輪
                         #   建立的東西（排程/警示/商品），是合法管理操作。
                         #   限**有 context** 時，否則裸句仍照擋。
                         or (_re.search(r"\b(?:delete|remove|cancel)\s+(?:the\s+)?"
                                        r"(?:one|that|it|this)\b", user_text, _re.I)
                             and bool(_ctx_by_vid.get(vid) or {})))
                    and not _re.search(r"\b(?:database|table|everything|all data|"
                                       r"all items|all stock|system)\b",
                                       user_text, _re.I)):
                log.info(f"[gate] 黑名單 {_bl_hit_ws!r} 豁免：排程/警示管理句 → 放行")
                _bl_hit_ws = None
                # ⚠️ 兩道關卡：黑名單豁免後**還有守門員**，只修前者無效
                #   （實測 'delete the one i just made' 豁免通過後仍被
                #   守門員拒絕）。既然已判定是合法管理句，直接標記放行。
                _gk_admin_pass = True
            # ── r14：**導覽句豁免**（展場開場白，最高頻的第一句）─────────
            #   `what is this thing` / `who are you` **同時在黑名單和
            #   GUIDE_KEYWORDS 裡**，兩張表互相打架、黑名單先執行就贏了
            #   → 訪客站到機器前問「這是什麼」直接被婉拒（第一印象就是打槍）。
            #   ⚠️ 用 `_is_guide_request()` 當判準、不另建詞表——它本身已含
            #     「含具體商品名 → 當查詢不當導覽」的排除，不會誤放行
            #     `delete all items` 這種（那句沒有導覽詞、也不會命中）。
            #   ⚠️ 破壞類黑名單詞永不豁免（同 _BL_NEVER_EXEMPT 的道理）：
            #     `what is this database` 這種要照擋。
            if (_bl_hit_ws
                    and _is_guide_request(user_text)
                    and not _re.search(r"\b(?:database|table|everything|all data|"
                                       r"password|credential|token|api key|"
                                       r"system prompt|ignore)\b",
                                       user_text, _re.I)):
                log.info(f"[gate] 黑名單 {_bl_hit_ws!r} 豁免：導覽句 → 放行")
                _bl_hit_ws = None
                _gk_admin_pass = True   # 同排程豁免：後面還有守門員，要一起放行

            if _bl_hit_ws:
                log.info(f"[gate] 黑名單命中 {_bl_hit_ws!r} → rejected")
                await push_display({"type": "trace", "stage": "rejected",
                                    "reason": f"blacklist:{_bl_hit_ws}"})
                for ch in GATEKEEPER_REJECT_MSG:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {"ok": False, "view": "rejected",
                                                        "summary": GATEKEEPER_REJECT_MSG}})
                continue

            # ── 刪除/下架（優先於 clarify）──
            _delete_kws_ws = ("刪除", "下架", "砍掉", "移除", "刪掉", "刪了",
                              # EN build：英文刪除商品觸發詞（原表全中文 →
                              #   "delete item" 進不了流程，被當成查詢 "delete"
                              #   這個商品 → oov:noex 回「查無此商品」）
                              "delete item", "delete the item", "remove item",
                              "remove the item", "delete product", "remove product",
                              "take down", "discontinue", "delist")
            # r19：「刪掉早上九點的排程」是排程管理不是刪商品——排程/警示對象
            # 讓給 Pre-C-Sched 的取消排程規則（列排程讓訪客選）
            if (any(w in user_text for w in _delete_kws_ws)
                    and not any(w in user_text for w in ("排程", "警示", "提醒", "鬧鐘"))
                    # ⚠️ 這裡是 WS handler 作用域，沒有 text_low（那是
                    #   _correct_function_call 的區域變數）→ 自己 lower()
                    and not any(w in user_text.lower()
                                for w in ("schedule", "alert", "reminder", "job"))):
                # r74：schedule_list/alert_list 畫面後的短刪除句（「刪掉它」）是刪
                # 排程/警示不是刪商品——曾誤入商品刪除流程回「電動牙刷無法刪除」
                _lv74 = _ctx_for(vid).get("last_view")
                _sj74 = (_ctx_for(vid).get("last_sched_jobs")
                         if _lv74 in ("schedule_list", "schedule_done")
                         else _ctx_for(vid).get("last_alert_rules")) or []
                if (_lv74 in ("schedule_list", "alert_list",
                              "schedule_done", "alert_done")
                        and _sj74 and len(user_text) <= 10):
                    import tools_v2 as _tv2_sd74
                    _sched74 = _lv74 in ("schedule_list", "schedule_done")
                    _obj74 = "排程" if _sched74 else "警示規則"
                    # r75：「剛加的那條刪掉」→ 直指最後一筆，不再反問 ID
                    if (len(_sj74) > 1 and any(w in user_text for w in
                                               ("剛加", "剛剛", "最新", "最後",
                                                "剛排", "剛設"))):
                        _sj74 = [_sj74[-1]]
                    if len(_sj74) == 1:
                        result = (_tv2_sd74.delete_schedule(job_id=_sj74[0])
                                  if _sched74
                                  else _tv2_sd74.delete_alert(rule_id=_sj74[0]))
                    else:
                        _q74 = (f"要刪哪一個{_obj74}？目前有 {len(_sj74)} 個，"
                                f"請說 ID（例如 {_sj74[0]}）。")
                        result = {"ok": True, "view": "clarify", "summary": _q74,
                                  "data": {"question": _q74,
                                           "options": list(_sj74), "hint": ""}}
                        _del_select_by_vid[vid] = {
                            "kind": "sched" if _sched74 else "alert",
                            "ids": list(_sj74)}
                    log.info(f"[gate-r74] {_lv74} 後刪除句 → {_obj74}刪除 {_sj74}")
                    for ch in result.get("summary", ""):
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": result})
                    continue
                # 搗蛋守衛：要刪的是訂單/資料/別人的東西 → 不是刪商品功能，直接拒絕
                # （conv100-r5：「幫我把別人的訂單刪掉」曾開出刪除商品流程）
                if any(w in user_text for w in ("訂單", "資料", "紀錄", "記錄", "帳號",
                                                 "別人", "全部", "所有", "資料庫", "系統")):
                    log.info(f"[gate] 刪除句含敏感對象 → rejected: {user_text!r}")
                    for ch in GATEKEEPER_REJECT_MSG:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {"ok": False, "view": "rejected",
                                                        "summary": GATEKEEPER_REJECT_MSG}})
                    continue
                import tools_v2 as _tv2_del_ws
                kw = _extract_sku_keyword(user_text)
                if not kw:
                    for w in _delete_kws_ws: kw = user_text.replace(w, "").strip()
                # 沒有具體商品名 → 列出可刪除的商品供選擇
                import warehouse as _W_del_list
                _has_match_ws = bool(_W_del_list.match_items(kw)) if kw else False
                if not kw or len(kw) < 2 or not _has_match_ws:
                    PROTECTED = {f"{p}{i:02d}" for p in "eafdcs" for i in range(1,11)}
                    user_items = [it for it in _W_del_list.state().items if it["sku_id"] not in PROTECTED]
                    if user_items:
                        names = ", ".join(it["name"] for it in user_items[:10])
                        result = {"ok": True, "summary": f"Deletable items: {names}\nPlease type the name of the item to delete", "view": "item_list",
                                   "data": {"items": [{"name": it["name"], "sku": it["sku_id"]} for it in user_items]}}
                        # 刪除 pending 狀態以 vid 為 key——全域旗標會讓下一個訪客的
                        # 任意輸入被當成要刪的商品名（跨訪客污染，conv100-r5）
                        _item_delete_state[vid] = True
                    else:
                        result = {"ok": True, "summary": "No deletable items yet. Use \"add item\" to create one first.", "view": "item_list", "data": {}}
                else:
                    result = _tv2_del_ws.delete_item_start(keyword=kw)
                for ch in result.get("summary", ""):
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get() * 1.5)
                await send({"type": "done", "result": result})
                continue

            # ── 刪除模式中：訪客輸入商品名 → 執行刪除 ──
            if _item_delete_state.get(vid):
                # r33：刪除流程也要有查詢句防呆（跟新增流程同一組詞表）——
                #   「無線滑鼠還剩幾個」曾被整句當成要刪的商品名 → error 醜回答。
                if any(w in user_text for w in ("還剩", "剩多少", "剩幾", "有多少",
                                                "多少件", "庫存", "哪些", "缺貨",
                                                "熱銷", "排行", "快到期", "進出紀錄",
                                                "幾個", "幾件")):
                    _dq_msg = ("你正在刪除商品的流程中，這句不會被當成要刪的商品。"
                               "要查別的請先說「取消」退出流程。")
                    log.info(f"[delete-gate] 流程中的查詢句 → 提示退出: {user_text!r}")
                    for ch in _dq_msg:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "clarify", "summary": _dq_msg,
                        "data": {"question": _dq_msg, "options": [], "hint": ""}}})
                    continue
                import tools_v2 as _tv2_del_mode
                _item_delete_state.pop(vid, None)
                result = _tv2_del_mode.delete_item_start(keyword=user_text.strip())
                # r75：流程中亂打（「ㄟ奇怪」）曾吐 error frame——換成友善收口
                if not result.get("ok") or result.get("view") == "error":
                    _dm_msg = (f"「{user_text.strip()}」不像商品名，先幫你退出刪除流程。"
                               "要刪商品再說「刪除商品」就可以。")
                    result = {"ok": True, "view": "clarify", "summary": _dm_msg,
                              "data": {"question": _dm_msg, "options": [], "hint": ""}}
                for ch in result.get("summary", ""):
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get() * 1.5)
                await send({"type": "done", "result": result})
                continue

            # ── 列出所有商品（優先於引導）──
            # 含設定關鍵字時不攔（「中倉全部商品安全庫存改成六十」是 config 句）
            # EN build：英文觸發詞。⚠️「item list」是**英文版回答自己教訪客
            #   打的字**（概覽卡寫 say "item list" for the full list），不收
            #   的話訪客照著打會落到 GUIDE_KEYWORDS 的 "list" 回導覽頁。
            if ((any(w in user_text for w in ("所有商品", "商品列表", "商品清單", "全部商品", "列出商品", "商品名稱",
                                              # r29：「全部倉一共幾項商品」
                                              "幾項商品", "幾種商品"))
                 or any(w in user_text.lower() for w in
                        ("item list", "items list", "product list", "full list",
                         "list of items", "list all items", "list the items",
                         "list everything", "all item names", "show all items",
                         # 2026-08-02：單數形（ASR 最易吞字尾 s；打字也會漏）
                         "item lists", "list all item", "list the item",
                         "all item name", "show all item", "list of item")))
                    # r30：「全部商品裡最貴的前五名」讓給價格直答
                    and not any(w in user_text for w in ("最貴", "最便宜", "前三", "前五", "前十"))
                    and not any(w in user_text for w in _CONFIG_KEY_WORDS)
                    # 搗蛋語境不觸發列表（「所有商品免費送我」「全部商品算零元」曾吐 61 項全清單）
                    and not any(w in user_text for w in ("免費", "送我", "送給", "白拿", "改成", "刪",
                                                          "零元", "0元", "算我的", "打包",
                                                          # r81 寫入契約：破壞動詞（歸零/清空/清掉…）
                                                          # 出現在全稱句 → 搗蛋，不吐清單
                                                          "歸零", "清空", "清掉", "清光", "清除",
                                                          "砍掉", "全砍", "格式化",
                                                          # r19：「幫全部商品都設警示」是警示設定
                                                          # 不是要商品清單
                                                          "警示", "提醒", "通知"))):
                import warehouse as _W_list_ws
                snap = _W_list_ws.state()
                # r28：「全部商品總共幾件」是問總量不是要 60 項全清單
                if any(w in user_text for w in ("幾件", "多少件", "總件", "總數", "總共幾",
                                                 "幾項", "幾種", "一共幾")) \
                        or any(w in user_text.lower() for w in
                               ("how many items", "how many products", "total units",
                                "total stock", "how many in total")):
                    _tot_qty = sum(q for wh in snap.stock.values() for q in wh.values())
                    _tot_sum = (f"{len(snap.items)} items in total, "
                                f"{_tot_qty:,} units across all 3 warehouses.")
                    for ch in _tot_sum:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {"ok": True, "view": "inventory",
                                "summary": _tot_sum, "data": {"total_qty": _tot_qty}}})
                    continue
                rows = [f"{it['sku_id']} {it['name']} ({_W_list_ws.CATEGORY_LABEL.get(it['category'], it['category'])}) NT${it['unit_price']}" for it in snap.items]
                summary = f"{len(rows)} items in total:\n" + "\n".join(f"  {r}" for r in rows)
                for ch in summary:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(0.003)
                await send({"type": "done", "result": {"ok": True, "view": "item_list", "summary": summary, "data": {"total": len(rows)}}})
                continue

            # ── 客服引導 ──
            if _is_guide_request(user_text):
                log.info(f"[引導] 訪客想看倉管工具總覽: {user_text!r}")
                await push_display({"type": "trace", "stage": "guided", "user_text": user_text})
                for ch in GUIDE_MSG:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(0.006)
                # ⚠️ r14：這裡原本**沒帶 summary**（同檔另外五處 view=guide 都有）
                #   → done 事件的 summary 是空字串。畫面靠串流 token 顯示看似
                #   正常，但任何讀 summary 的地方（歷史、複製、ws_inspect、
                #   測試工具判定）全部拿到空值，而且串流一中斷內容就沒了
                #   （實測畫面最後一行被截成 'what else do diaper'）。
                await send({"type": "done", "result": {
                    "ok": True, "view": "guide", "summary": GUIDE_MSG, "data": {}}})
                continue

            # ── 刪除模式中 → 優先處理，不進守門員 ──
            if _item_delete_state.get(vid):
                import tools_v2 as _tv2_del_mode2
                _item_delete_state.pop(vid, None)
                result = _tv2_del_mode2.delete_item_start(keyword=user_text.strip())
                for ch in result.get("summary", ""):
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get() * 1.5)
                await send({"type": "done", "result": result})
                continue

            # ── 守門員（per-vid：只有『自己這位訪客』在新增流程中才豁免）──
            _ic_st = _item_create_state_ws.get(vid, {})
            # ── EN build：**有 context 時放行短追問**（劇情批 r1 抓到）──────
            #   守門員是「白名單命中才放行」，但真實訪客的追問句不含任何
            #   商品/倉管詞：'north' / 'and central' / 'the first one' /
            #   'put it back' / 'did it take effect' → 全被當搗蛋拒絕。
            #   中文版沒這問題是因為「北倉呢」自帶倉名命中白名單，
            #   英文的裸 'north' 卻不在白名單（只有 'warehouse'）。
            #   這類句子**靠 context 才有意義**，正解不是硬列白名單，
            #   而是「上一輪有結果 → 這輪的短句是追問」。
            #   條件從嚴：①上一輪留下了 context ②本句夠短（≤6 詞）
            #   ③不是純亂敲（要有母音、不是隨機字母串）
            #   ⇒ 亂敲（gjfkdls/asdkjhaskjdh）與閒聊搗蛋仍照擋。
            _gk_ctx_pass = False
            # ⚠️ 純標點/純數字不是追問（r3 S9：'?' 被 context 放行 → 全店概覽）
            if not is_meaningful_input(user_text) and _re.search(r"[a-z]", user_text, _re.I):
                _gk_ctx = _ctx_by_vid.get(vid) or {}
                if _gk_ctx.get("last_sku") or _gk_ctx.get("last_func"):
                    _w = user_text.strip().split()
                    # ⚠️ 只放行**看起來像追問**的句子，不能因為「有 context」
                    #   就全放——實測放太寬會讓 'will you crash' /
                    #   'can you speak chinese' / 'do you sell rolex' 這些
                    #   搗蛋句溜進來（它們原本正是靠白名單不命中被擋的）。
                    #   追問的判準：帶指代詞/序數/承接詞，或極短的裸名詞片語。
                    # ⚠️ 'the \w+ ones?' 必須排在 'the (first|second|…)' **前面**：
                    #   交替分支是**左優先且不回溯**，'the most urgent one' 會先
                    #   被 'the (?:first|…|one)' 嘗試、失敗後整個 the-分支就放棄，
                    #   不會再試後面的 'the \w+ one'（實測 followup=False）。
                    _is_followup = bool(_re.search(
                        r"\b(?:it|its|that|this|those|these|them|"
                        # ⚠️ 最高級排在 ones? 之前（交替分支左優先不回溯，
                        #   同 _CTX_FOLLOWUP_RE_EN 的修正）
                        r"the (?:worst|best|biggest|largest|smallest|highest|"
                        r"lowest|cheapest|priciest|newest|oldest|most|least)\b|"
                        r"which (?:item|one)\b|"
                        r"the (?:\w+ ){1,3}ones?|"
                        r"the (?:first|second|third|last|other|same|one)|"
                        r"first|second|third|last one|"
                        r"and|what about|how about|then|also|too|again|"
                        r"back|effect|now|instead)\b", user_text, _re.I))
                    # 純數量/倉別追問（'20 more' / 'in central'）也算
                    if not _is_followup and len(_w) <= 3:
                        _is_followup = True
                    if (1 <= len(_w) <= 6 and _is_followup
                            and not _GATEKEEPER_BLACKLIST_HIT(user_text)
                            # 疑問式閒聊（will you… / can you… / do you sell…）
                            # 不是追問，照擋
                            and not _re.search(
                                r"^\s*(?:will|can|could|would|are|do)\s+you\b",
                                user_text, _re.I)):
                        # 亂敲判準：每個詞都要有母音（gjfkdls/asdkjhaskjdh 沒有）
                        # r15 #94：**中文照擋**——'買 iphone' 曾靠 ≤3 詞追問
                        #   放行繞過 is_meaningful_input 的中文防線（EN 版
                        #   policy：含中文一律 reject）
                        if (all(_re.search(r"[aeiou]", _t, _re.I)
                                for _t in _w if len(_t) >= 3)
                                and not any("一" <= c <= "鿿" for c in user_text)):
                            _gk_ctx_pass = True
                            log.info(f"[守門員] context 追問放行: {user_text!r}")
            if not _ic_st.get("active") and not _gk_ctx_pass \
                    and not _gk_admin_pass \
                    and not is_meaningful_input(user_text):
                log.info(f"[守門員] 拒絕無意義輸入: {user_text!r}")
                await push_display({"type": "trace", "stage": "rejected",
                                    "reason": "input matched no warehouse keywords"})
                for ch in GATEKEEPER_REJECT_MSG:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {"ok": False, "view": "rejected",
                                                        "summary": GATEKEEPER_REJECT_MSG}})
                continue

            # ── 後設/取消句攔截（r17）：「我說錯了 是南倉不是北倉」曾幻覺出
            #   倉庫比較、「不用查了」曾幻覺出缺貨清單。句中沒有新的具體需求
            #   （無商品名）→ 友善請訪客重講，不可亂猜功能。「取消所有排程」
            #   這類管理句有明確對象詞，豁免。──
            _META_FIX_WORDS = ("說錯", "講錯", "打錯了", "重講一次", "重新講")
            _META_DROP_WORDS = ("不用查", "不查了", "不用了", "先不用", "算了",
                                "當我沒說", "取消", "不算", "作廢", "撤回")
            _meta_hit = (any(w in user_text for w in _META_FIX_WORDS)
                         or (any(w in user_text for w in _META_DROP_WORDS)
                             and not any(w in user_text for w in
                                         ("排程", "警示", "提醒", "訂閱", "規則", "訂單"))))
            # r77：「算了 看熱銷第二名」——放棄詞**之後**帶明確新需求才不收口
            # （「庫存...算了當我沒問」的需求詞在放棄詞前面＝真放棄，照收）
            _meta_pos77 = max((user_text.rfind(w) + len(w)
                               for w in _META_DROP_WORDS if w in user_text),
                              default=0)
            _meta_demand77 = _re.search(
                r"(熱銷|排行|警示|庫存|到期|過期|清單|比較|進出|報表|排程|"
                r"改回|恢復|第[一二三四五12345]名)", user_text[_meta_pos77:])
            if _meta_hit and not _meta_demand77:
                import warehouse as _W_meta
                _meta_kw = _extract_sku_keyword(user_text)
                if not (_meta_kw and _W_meta.match_items(_meta_kw)):
                    # r32：放棄句（「算了」「不用了」）必須跟「取消」一樣清掉流程狀態。
                    #   過去只回 clarify 不清 state → 新增商品流程還活著，下一句
                    #   「哪些商品快缺貨了」被 step 機吞成商品名，後面每一句都被流程
                    #   吃掉（流程劫持，展場必爆）。
                    _item_create_state_ws.pop(vid, None)
                    _item_delete_state.pop(vid, None)
                    _meta_msg = ("No problem — there is no operation in progress. "
                                 "Just tell me what you need, e.g. "
                                 '"south bluetooth earphones stock" or '
                                 '"north received 50 wireless mouse".')
                    log.info(f"[meta-gate] 後設/取消句 → clarify（已清流程狀態）: {user_text!r}")
                    for ch in _meta_msg:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "clarify", "summary": _meta_msg,
                        "data": {"question": _meta_msg, "options": [], "hint": ""}}})
                    continue

            # ── 雙寫入複合句攔截（r18）：「進貨50個毛帽到北倉 然後調20個去南倉」
            #   曾被 C13a 搶成一張方向相反的調貨卡（危險級）。一句含兩個寫入
            #   動作（進出貨+調貨）且有連接詞 → 請訪客分開講，一次一個動作。──
            _dw_connector = any(w in user_text for w in ("然後", "接著", "之後再", "順便", "完再", "再調", "再進", "再出"))
            _dw_mv = any(w in user_text for w in ("進貨", "進了", "出貨", "出了", "入庫", "出庫", "補貨", "退貨"))
            _dw_tf = any(w in user_text for w in ("調", "撥", "挪", "移到", "移去", "轉倉"))
            if (_dw_connector and _dw_mv and _dw_tf
                    and not any(w in user_text for w in ("供應商", "廠商", "客戶", "客人", "顧客"))):
                _dw_msg = ("一次幫你處理一個動作喔。請分開說，例如先講"
                           "「北倉進50個毛帽」，完成後再講「北倉調20個毛帽到南倉」。")
                log.info(f"[dw-gate] 雙寫入複合句 → clarify: {user_text!r}")
                for ch in _dw_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "clarify", "summary": _dw_msg,
                    "data": {"question": _dw_msg, "options": [], "hint": ""}}})
                continue

            # ── 同方向多商品寫入攔截（r40）：「北倉進50個藍牙耳機和30個滑鼠」曾只開
            #   第一個商品的卡、第二個(滑鼠30個)完全漏掉——訪客誤以為兩個都進了(比查錯
            #   庫存嚴重，因為他以為完成了)。確認卡是單商品設計，一次多商品請分開講。──
            _mpw_has_mv = any(w in user_text for w in ("進", "出", "補", "送", "退")) \
                and _re.search(r'\d', user_text)
            # 數兩個以上「數字+量詞」段
            _mpw_qty_n = len(_re.findall(
                r'(?:[0-9]+|[零一二兩三四五六七八九十百千]+)\s*'
                r'(?:件|個|條|支|台|箱|包|瓶|罐|組|雙|套|盒|對|頂|張|把|副|顆|粒|袋|桶|杯|塊|片)',
                user_text))
            _mpw_conn = any(w in user_text for w in ("和", "跟", "還有", "與", "、", "，", ",", "以及", "同時", "外加"))
            # ── EN build（mpw-gate-en，2026-08-02）：上面三個判準全中文
            #   （量詞/連接詞）→ 英文句一個都不中，
            #   `north received 50 wireless mouse and 30 yoga mats`
            #   **只開第一個商品的卡、第二個默默消失**，訪客以為兩筆都記了。
            #   （中文版 r40 原註解：比查錯庫存嚴重，因為他以為完成了。）
            if _is_mostly_english(user_text):
                _ul_mpw = user_text.lower()
                _mpw_has_mv_en = bool(_re.search(
                    r"\b(?:received|receive|got|shipped|ship|sent|send|"
                    r"add|added|returned|restock)\b", _ul_mpw)) and bool(
                        _re.search(r"\d", _ul_mpw))
                # 「數字 + 英文字」出現兩組以上＝兩個商品（英文無量詞）
                #   排除數字後接單位/時間詞的假計數
                _mpw_qty_n_en = len(_re.findall(
                    r"\b\d+\s+(?!units?\b|pcs\b|pieces?\b|days?\b|weeks?\b|"
                    r"months?\b|years?\b|hours?\b|am\b|pm\b|percent\b)[a-z]",
                    _ul_mpw))
                _mpw_conn_en = bool(_re.search(
                    r"\b(?:and|plus|also|as well as)\b|[,&]", _ul_mpw))
                _mpw_is_tf_en = bool(_re.search(
                    r"\b(?:transfer|move|shift|relocate|reallocate)\b", _ul_mpw))
                if (_mpw_has_mv_en and _mpw_qty_n_en >= 2 and _mpw_conn_en
                        and not _mpw_is_tf_en):
                    _mpw_msg_en = ("I can only take one item per inbound/outbound. "
                                   "Please say them one at a time, e.g. "
                                   "\"north received 50 bluetooth earphones\" first, "
                                   "then \"north received 30 wireless mouse\".")
                    log.info(f"[mpw-gate-en] 英文多商品寫入 → clarify: {user_text!r}")
                    for ch in _mpw_msg_en:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "clarify", "summary": _mpw_msg_en,
                        "data": {"question": _mpw_msg_en, "options": [],
                                 "hint": ""}}})
                    continue

            if (_mpw_has_mv and _mpw_qty_n >= 2 and _mpw_conn
                    and not any(w in user_text for w in ("調", "撥", "挪", "移到", "轉倉"))):
                _mpw_msg = ("一次幫你進/出一種商品喔。請分開說，例如先講"
                            "「北倉進50個藍牙耳機」，完成後再講「北倉進30個滑鼠」。")
                log.info(f"[mpw-gate] 同方向多商品寫入 → clarify: {user_text!r}")
                for ch in _mpw_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "clarify", "summary": _mpw_msg,
                    "data": {"question": _mpw_msg, "options": [], "hint": ""}}})
                continue

            # ── 多商品並列查詢攔截（r40，user 定調）：「衛生紙跟濕紙巾的庫存」「A、B、C
            #   各剩多少」曾只回第一個商品（其餘漏）。user 決定：同時問兩種以上庫存 →
            #   請分開問（一次一個）。**比較題例外**（「A跟B哪個多」答案只有一個結論，
            #   保留）。判別：多商品名 + 無比較詞 + 無進出詞（寫入歸 mpw-gate 管）。──
            _plq_cmp = any(w in user_text for w in ("哪個", "哪一個", "誰", "比較", "比一比",
                            "比一下", "比比看", "誰比較", "較多", "較少", "賣得", "賣最",
                            "多還是", "大還是", "對比", "差多少", "哪邊", "誰多", "誰少", "哪種多"))
            _plq_mv = any(w in user_text for w in ("進", "出", "補", "調", "退")) and _re.search(r'\d', user_text)
            # r45：裸並列（「北倉衛生紙、南倉啤酒」「衛生紙+濕紙巾」無庫存 cue）也進 gate
            _plq_bare = (any(c in user_text for c in "、+；？") and len(user_text) <= 16
                         and not _re.search(r'\d', user_text))
            # r46：related/連帶句豁免（「衛生紙跟濕紙巾一起賣嗎」是連帶不是並列查詢）
            _plq_rel = any(w in user_text for w in _RELATED_INTENT_WORDS)
            if not _plq_mv and not _plq_rel and (_plq_cmp or _plq_bare
                    or any(w in user_text for w in ("庫存", "還剩", "剩多少", "各剩", "有多少",
                                                     "還有多少", "剩幾",
                                                     # r46：「運動毛巾跟毛帽都查」「露營燈和露營椅和
                                                     # 露營帳篷」（裸和三連）曾只回一個
                                                     "都查", "都要", "都給我", "全都", "一起查"))
                    or ("和" in user_text and len(user_text) <= 16 and not _re.search(r'\d', user_text))):
                import warehouse as _W_plq
                # 只靠分隔符切（跟/和/、等）。無分隔黏寫（「衛生紙濕紙巾尿布」）不處理——
                #   曾試「掃 2 字短稱」但誤傷嚴重（「無線藍牙耳機」被拆成藍牙耳機+藍牙喇叭、
                #   「嬰兒濕紙巾」被拆成尿布+濕紙巾），穩定優先。訪客通常會加「跟/和」。
                _plq_src = _re.sub(r"的?庫存|各剩多少|各剩幾|各有多少|還有多少|剩多少|剩幾個?|還剩", "", user_text)
                # r43：比較尾巴一併剝（「尿布哪個賣最好」的尾巴害第三個商品抽不到 → hits=2
                # 漏攔三商品比較）
                _plq_src = _re.sub(r"(哪個|哪一個|誰|比一比|比比看|比較一下).*$", "", _plq_src)
                _plq_parts = [p.strip() for p in _re.split(r"[跟和與、,，及+；;？?]|還有|以及", _plq_src) if p.strip()]
                _plq_hits = []
                for p in _plq_parts:
                    _m = _W_plq.match_items(_extract_sku_keyword(p) or p)
                    if _m and _m[0].get("score", 0) >= 5 and _m[0]["item"]["name"] not in _plq_hits:
                        _plq_hits.append(_m[0]["item"]["name"])
                # r43：三個以上商品的比較題（「衛生紙跟濕紙巾和尿布哪個賣最好」）曾被
                # LLM 猜一個答 movement——比較一次只支援兩個，超過就請兩兩比。
                if _plq_cmp and len(_plq_hits) >= 3:
                    _plq_msg = (f"一次幫你比兩個喔——{'、'.join(_plq_hits[:4])}，"
                                f"請兩兩比，例如「{_plq_hits[0]} 跟 {_plq_hits[1]} 哪個多」。")
                    log.info(f"[plq-gate] ≥3 商品比較 → clarify: {user_text!r} hits={_plq_hits}")
                    for ch in _plq_msg:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "clarify", "summary": _plq_msg,
                        "data": {"question": _plq_msg, "options": [], "hint": ""}}})
                    continue
                if not _plq_cmp and len(_plq_hits) >= 2:
                    _plq_msg = ("一次幫你查一種商品的庫存喔。請分開問，例如先問"
                                f"「{_plq_hits[0]}庫存」，再問「{_plq_hits[1]}庫存」。"
                                "（想比較多寡可以問「A 跟 B 哪個多」）")
                    log.info(f"[plq-gate] 多商品並列查詢 → clarify: {user_text!r} hits={_plq_hits}")
                    for ch in _plq_msg:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "clarify", "summary": _plq_msg,
                        "data": {"question": _plq_msg, "options": [], "hint": ""}}})
                    continue

            # ── 不支援時間粒度誠實 clarify（r25）：「上上週的出貨量」曾回上週數字
            #   （錯期間誤導）、「週末有進貨嗎」曾回本週。誠實列出支援範圍。──
            # r56：上個月改誠實 clarify——資料快照只涵蓋近期，過去「近似成本月」會讓
            # 「上個月呢」拿到本月數字（答非所問且訪客無從發現）
            # r62：時段粒度（早上/下午）也誠實——「下午出了幾件」曾默默回整天數字。
            # 「每天晚上七點」排程句要讓路（Pre-C-Sched 在後面接）
            _UNSUPPORTED_TIME = ("上上週", "上上周", "上上禮拜", "大前天", "週末", "周末",
                                 "上季", "上一季", "去年", "前年", "年初", "年底",
                                 "上個月", "上月",
                                 # r14+2（#42）：本表原全中文（坑 7）——
                                 #   'what sold over the weekend' 進不了
                                 #   gate、掉到 fuzzy 幻覺出商品卡
                                 "weekend", "last year", "a year ago",
                                 # r15 #32/#35/#36：星期名/夜間/季度/區間都是
                                 #   資料粒度沒有的（日粒度快照）——誠實反問
                                 "friday", "monday", "tuesday", "wednesday",
                                 "thursday", "saturday", "sunday", "tonight",
                                 "last night", " q1", " q2", " q3", " q4",
                                 "between monday", "between tuesday")
            # （r62 撤回時段粒度 gate：「中午前的異動」「下午有出貨嗎」是既有守衛
            #   接受的整天近似行為——出手前要先查 corpus，守衛既定行為優先）
            # r26：「上個月跟這個月哪個賣得多」雙期間比較不支援（上個月單獨出現時
            # 既有規則近似成本月，僅在兩期間同句要求比較時誠實 clarify）
            _dual_period = (("上個月" in user_text
                             and any(w in user_text for w in ("這個月", "本月"))
                             and any(w in user_text for w in ("哪個", "誰", "比", "多還是", "差")))
                            # r27：「上週跟這週哪週出貨多」曾只回上週（無從比較）。
                            # 帶商品名的（藍牙喇叭上週跟這週哪週賣得多）讓給 C4-prod，不攔
                            or (any(w in user_text for w in ("上週", "上周", "上禮拜"))
                                and any(w in user_text for w in ("這週", "本週", "這禮拜"))
                                and any(w in user_text for w in ("哪週", "哪個", "誰", "比", "差"))
                                and not _extract_sku_keyword(user_text)))
            _ut_low = user_text.lower()
            if ((any(w in _ut_low for w in _UNSUPPORTED_TIME) or _dual_period)
                    and any(w in _ut_low for w in ("進", "出", "貨", "賣", "異動", "紀錄", "記錄",
                                                   # r14+2：觸發詞也要有英文（坑 7）
                                                   "sold", "sell", "sales", "moved",
                                                   "movement", "shipped", "received",
                                                   "bought", "came in", "went out",
                                                   # r15 #32/#35：happened/裸方向詞
                                                   "happened", "happen", "inbound",
                                                   "outbound"))
                    # r15：排程句（every friday…）不是查歷史，不可被本 gate 攔
                    and not _re.search(r"\b(?:every|schedule|daily|weekly|remind)\b",
                                       _ut_low)):
                _ut_msg = ("Movement stats currently support: today / yesterday / "
                           "this week / last week / this month. "
                           "Which range would you like?")
                log.info(f"[time-gate] 不支援時間粒度 → clarify: {user_text!r}")
                for ch in _ut_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "clarify", "summary": _ut_msg,
                    "data": {"question": _ut_msg,
                             "options": ["what came in today", "yesterday's outbound",
                                         "last week movements", "this month in/out stats"],
                             "hint": ""}}})
                continue

            # ── 最貴/最便宜直答（r19）：資料就有單價，曾被守門員拒答 ──
            #   r15 #71：判準原全中文（坑 7）——'whats the most expensive
            #   item we carry' 回「查無 expensive」。補英文詞。
            _pr_low15 = user_text.lower()
            if ((any(w in user_text for w in ("最貴", "最便宜", "價格最高", "價格最低",
                                              "單價最高", "單價最低"))
                 or _re.search(r"\b(?:most|least)\s+expensive\b|\bcheapest\b|"
                               r"\bpriciest\b|\bhighest\s+price\b|\blowest\s+price\b",
                               _pr_low15))
                    and not any(w in user_text for w in ("改", "設", "調", "元", "折"))
                    and not _re.search(r"\bset\b|\bchange\b|\bnt\$", _pr_low15)):
                import warehouse as _W_pr
                _pr_items = _W_pr.state().items
                _pr_hi = (any(w in user_text for w in ("最貴", "價格最高", "單價最高"))
                          or bool(_re.search(r"\bmost\s+expensive\b|\bpriciest\b|"
                                             r"\bhighest\s+price\b", _pr_low15)))
                # r28：「最貴的前三名」曾只回第一名（英文 top N 同步收）
                _pr_n_m = (_re.search(r"前\s*([0-9一二三四五六七八九十]+)", user_text)
                           or _re.search(r"\btop\s*([0-9]+)\b", _pr_low15))
                _pr_n = min(int(_cn_to_int(_pr_n_m.group(1)) or 1), 10) if _pr_n_m else 1
                # r15 #71 英化：本段原全中文（EN 檔內死中文——守門一直擋著沒人
                #   走到）。⚠️ view 原是 inventory_single 但 data 缺 total_qty/
                #   per_warehouse → 渲染器 toLocaleString 會炸進 try/catch；
                #   改用無渲染器的 view 名，前端只顯示 summary 文字。
                if _pr_n > 1:
                    _pr_top = sorted(_pr_items, key=lambda x: x["unit_price"], reverse=_pr_hi)[:_pr_n]
                    _pr_it = _pr_top[0]
                    _pr_sum = ((f"Top {_pr_n} most expensive: " if _pr_hi
                                else f"Top {_pr_n} cheapest: ")
                               + ", ".join(f"{i['name']} NT$ {i['unit_price']:,}"
                                           for i in _pr_top))
                else:
                    _pr_it = (max if _pr_hi else min)(_pr_items, key=lambda x: x["unit_price"])
                    _pr_sum = ((f"The most expensive item is " if _pr_hi
                                else f"The cheapest item is ")
                               + f"{_pr_it['name']} at NT$ {_pr_it['unit_price']:,} per unit.")
                log.info(f"[dispatch-ws] 價格排序直答: {_pr_it['name']}")
                for ch in _pr_sum:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "price_answer", "summary": _pr_sum,
                    "data": {"name": _pr_it["name"], "unit_price": _pr_it["unit_price"]}}})
                continue

            # ── 單品分倉極值（r35）：「瑜珈墊哪一倉最多」過去回三倉「總」排名
            #   （全店 600 萬 vs 300 萬），完全不是訪客問的那個商品。──
            if (any(w in user_text for w in ("哪一倉", "哪個倉", "哪倉"))
                    and any(w in user_text for w in ("最多", "最少", "比較多", "比較少"))):
                import warehouse as _W_wx
                _wx_kw = _extract_sku_keyword(user_text)
                _wx_m = _W_wx.match_items(_wx_kw) if _wx_kw else []
                if _wx_m and _wx_m[0].get("score", 0) >= 3 and _kw_grounded(_wx_kw, user_text):
                    _wx_it = _wx_m[0]["item"] if "item" in _wx_m[0] else _wx_m[0]
                    _wx_st = _W_wx.state()
                    _wx_q = {w: int(_wx_st.stock.get(w, {}).get(_wx_it["sku_id"], 0))
                             for w in ("north", "central", "south")}
                    _wx_hi = not any(w in user_text for w in ("最少", "比較少"))
                    _wx_pick = (max if _wx_hi else min)(_wx_q, key=_wx_q.get)
                    _wx_all = "、".join(f"{_W_wx.WAREHOUSE_LABEL.get(w, w)} {q} 件"
                                        for w, q in _wx_q.items())
                    _wx_sum = (f"「{_wx_it['name']}」{'最多' if _wx_hi else '最少'}的是"
                               f"{_W_wx.WAREHOUSE_LABEL.get(_wx_pick, _wx_pick)}"
                               f"（{_wx_q[_wx_pick]} 件）。三倉：{_wx_all}。")
                    log.info(f"[dispatch-ws] 單品分倉極值: {_wx_it['name']} → {_wx_pick}")
                    for ch in _wx_sum:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "inventory_single", "summary": _wx_sum,
                        "data": {"name": _wx_it["name"], "warehouse": _wx_pick}}})
                    continue

            # ── 單品缺貨判定（r34）：「瑜珈墊快缺貨了嗎」「藍牙耳機夠不夠」過去回
            #   一般庫存數字，等於沒回答問題（把安全庫存設成 9999 後仍說「共 455 件」）。
            #   → 拿該商品各倉的 qty 跟 safety_stock 比，直接講缺不缺。──
            if any(w in user_text for w in ("快缺貨", "缺貨了嗎", "缺貨嗎", "夠不夠",
                                            "夠嗎", "不夠嗎", "要補貨嗎", "要補嗎",
                                            "需要補", "低於安全庫存",
                                            "不夠賣", "會不會缺",     # r71
                                            "還缺嗎", "缺嗎",          # r72
                                            "快沒了嗎", "快沒嗎", "快空了")):  # r73
                import warehouse as _W_ls
                _ls_kw = _extract_sku_keyword(user_text)
                _ls_m = _W_ls.match_items(_ls_kw) if _ls_kw else []
                # r72：「還缺嗎」補貨後追問——extract 失敗退回 ctx（同 r70 撐天修法）
                _ls_grounded = bool(_ls_m and _ls_m[0].get("score", 0) >= 3
                                    and _kw_grounded(_ls_kw, user_text))
                # r77 危險級家族：「保溫瓶還缺嗎」——句中有明確但查無的商品名時
                # 不可退 ctx 頂替（曾拿耳機的缺貨判定回答保溫瓶＝資料誤導）
                _ls_stem77 = user_text
                for _t77 in ("還缺嗎", "缺嗎", "快沒了嗎", "快沒嗎", "快空了", "夠嗎",
                             "夠不夠", "要補嗎", "需要補", "會不會缺", "不夠賣",
                             "快缺貨", "缺貨了嗎", "缺貨嗎", "的", "呢", "現在", "那"):
                    _ls_stem77 = _ls_stem77.replace(_t77, "")
                _ls_stem77 = _ls_stem77.strip("?？。!！， ")
                if (not _ls_grounded and len(_ls_stem77) >= 2
                        and not _W_ls.match_items(_ls_stem77)
                        and not any(p in _ls_stem77 for p in ("它", "這", "那", "都"))
                        # r77v：全域/疑問/名次句不可搶（「低於安全庫存的品項」
                        # 「哪個倉最需要補貨」「第一名北倉夠嗎」曾被誠實反問劫走）
                        and not any(p in _ls_stem77 for p in
                                    ("哪", "什麼", "啥", "品項", "項目", "東西", "貨",
                                     "清單", "全部", "所有", "第", "名", "倉", "區",
                                     "低於", "安全", "庫存", "有沒有", "多少"))):
                    _ls_msg77 = (f"找不到「{_ls_stem77}」這個商品喔，"
                                 "請確認名稱後再問一次（或說「商品清單」看全部）。")
                    log.info(f"[dispatch-ws] 缺貨判定查無「{_ls_stem77}」→ 誠實反問")
                    for ch in _ls_msg77:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "clarify", "summary": _ls_msg77,
                        "data": {"question": _ls_msg77, "options": [], "hint": ""}}})
                    continue
                if not _ls_grounded and len(user_text) <= 8 and _ctx_for(vid).get("last_sku"):
                    _ls_kw = _ctx_for(vid)["last_sku"]
                    _ls_m = _W_ls.match_items(_ls_kw)
                    _ls_grounded = bool(_ls_m and _ls_m[0].get("score", 0) >= 3)
                if _ls_grounded:
                    _ls_it = _ls_m[0]["item"] if "item" in _ls_m[0] else _ls_m[0]
                    _ls_st = _W_ls.state()          # 庫存在 state().stock[倉][sku]，不在 item 上
                    _ls_sku = _ls_it["sku_id"]
                    # 安全庫存有「分倉覆寫」層：item 上的只是基準值，訪客改過的值寫在
                    # v2_config["safety_stock_override"][倉][sku]（不讀覆寫 → 改完設定
                    # 再問「快缺貨了嗎」會拿舊值回答，等於設定沒生效）
                    _ls_base = int(_ls_it.get("safety_stock", 0) or 0)
                    _ls_ov = ((_ls_st.v2_config or {}).get("safety_stock_override") or {})
                    _ls_safe = {w: int(_ls_ov.get(w, {}).get(_ls_sku, _ls_base))
                                for w in ("north", "central", "south")}
                    _ls_qty = {w: int(_ls_st.stock.get(w, {}).get(_ls_sku, 0))
                               for w in ("north", "central", "south")}
                    _ls_low = [(_W_ls.WAREHOUSE_LABEL.get(w, w), q, _ls_safe[w])
                               for w, q in _ls_qty.items() if q < _ls_safe[w]]
                    _ls_tot = sum(_ls_qty.values())
                    _ls_shown = (f"{min(_ls_safe.values())}~{max(_ls_safe.values())}"
                                 if len(set(_ls_safe.values())) > 1
                                 else str(next(iter(_ls_safe.values()))))
                    if _ls_low:
                        _ls_txt = "、".join(f"{w} {q} 件（安全線 {s}）" for w, q, s in _ls_low)
                        _ls_sum = (f"⚠️「{_ls_it['name']}」低於安全庫存：{_ls_txt}。"
                                   f"三倉共 {_ls_tot} 件，建議補貨。")
                    else:
                        _ls_sum = (f"✅「{_ls_it['name']}」庫存充足，三倉共 {_ls_tot} 件，"
                                   f"都在安全庫存（{_ls_shown} 件）之上。")
                    log.info(f"[dispatch-ws] 單品缺貨判定: {_ls_it['name']}")
                    for ch in _ls_sum:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "low_stock", "summary": _ls_sum,
                        "data": {"name": _ls_it["name"], "safety_stock": _ls_shown,
                                 "total": _ls_tot}}})
                    continue

            # ── r57：上個月的排行——資料不含上個月，過去默默回本週榜（拿錯榜單
            #   不自知），誠實說支援範圍 ──
            if (any(w in user_text for w in ("上個月", "上月", "上週", "上禮拜", "上星期",
                                              "昨天", "前天"))
                    and any(w in user_text for w in ("熱銷", "排行", "賣最", "暢銷", "滯銷"))):
                # r59：「上週的排行呢」也曾默默回本週榜——排行只有本週/本月資料
                # r71：「昨天賣最好的是什麼」同病（日粒度排行不支援）
                _rk_msg = "排行目前支援本週／本月。想看哪個範圍呢？"
                log.info(f"[time-gate] 上個月排行 → clarify: {user_text!r}")
                for ch in _rk_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "clarify", "summary": _rk_msg,
                    "data": {"question": _rk_msg,
                             "options": ["本週熱銷排行", "本月熱銷排行"], "hint": ""}}})
                continue

            # ── r58：排除式換看（「不要衛生紙 我要看濕紙巾」「衛生紙就算了 看一下
            #   尿布好了」）——曾回被排除的 A＝語意反轉（r16 家族）。取「要/看」後的
            #   B 直查。要放在所有庫存 dispatch 之前，否則 A 先被接走。──
            _sw58 = _re.search(
                r"(?:不要|不用|先不要|跳過|就算了|算了)[^，,]{0,5}[，,、 ]*"
                r"(?:我要看|我要|改看|換看|換|看一下|看看|查一下|要看|看)\s*"
                r"(.{2,10}?)(?:好了|吧|的庫存|庫存)?$", user_text)
            if _sw58:
                import warehouse as _W_sw
                _sw_kw = _extract_sku_keyword(_sw58.group(1)) or _sw58.group(1).strip()
                _sw_hit = _W_sw.match_items(_sw_kw) if _sw_kw else []
                if _sw_hit and _sw_hit[0].get("score", 0) >= 5:
                    result = finance.execute("query_inventory",
                                             {"keyword": _sw_hit[0]["item"]["name"]})
                    if result.get("ok") and result.get("summary"):
                        log.info(f"[dispatch-ws] 排除式換看 → {_sw_hit[0]['item']['name']}")
                        for ch in result["summary"]:
                            await send({"type": "token", "text": ch})
                            await asyncio.sleep(_TK_DELAY.get())
                        await send({"type": "done", "result": result})
                        continue

            # ── r64：類別身分追問（「它是廚具還是食品」）——曾重回庫存卡＝半答 ──
            if (len(user_text) <= 24 and _ctx_for(vid).get("last_sku")
                    and (any(w in user_text for w in ("是什麼類", "哪一類", "屬於什麼"))
                         or _re.search(r"是[一-鿿]{2,4}還是[一-鿿]{2,4}類?", user_text))
                    # r74：「哪一類最值錢」是類別排行問句不是身分追問，曾誤答「屬於日用品類」
                    and not any(w in user_text for w in ("賣", "熱銷", "排行", "庫存", "比較",
                                                          "值錢", "總值", "最貴", "最多", "最少"))):
                import warehouse as _W_cat
                _cat_m = _W_cat.match_items(_ctx_for(vid)["last_sku"])
                if _cat_m:
                    _cat_it = _cat_m[0]["item"]
                    _cat_lbl = _W_cat.CATEGORY_LABEL.get(_cat_it["category"], _cat_it["category"])
                    _cat_sum = f"「{_cat_it['name']}」屬於{_cat_lbl}類。"
                    log.info(f"[dispatch-ws] 類別身分: {_cat_it['name']} → {_cat_lbl}")
                    for ch in _cat_sum:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "inventory_single", "summary": _cat_sum,
                        "data": {"name": _cat_it["name"]}}})
                    continue

            # ── r77：連帶序數追問（「第一個連帶的庫存」「連帶第二名剩多少」）——
            #   曾回 related_empty 醜訊息（連帶第N家族第三次出現）──
            _rel77_m = _re.search(r"第\s*([一二三123])", user_text)
            if ("連帶" in user_text and _rel77_m
                    and _ctx_for(vid).get("last_related")
                    and any(w in user_text for w in ("庫存", "剩", "還有", "幾個",
                                                      "幾件", "多少", "存量"))):
                _rl77 = _ctx_for(vid)["last_related"]
                _ri77 = ({"一": 1, "二": 2, "三": 3}.get(_rel77_m.group(1))
                         or int(_rel77_m.group(1))) - 1
                if 0 <= _ri77 < len(_rl77):
                    result = finance.execute("query_inventory", {"keyword": _rl77[_ri77]})
                    if result.get("ok") and result.get("summary"):
                        result["summary"] = (f"連帶第{_ri77 + 1}名是「{_rl77[_ri77]}」。"
                                             + result["summary"])
                        log.info(f"[dispatch-ws] 連帶序數: {_rl77[_ri77]}")
                        for ch in result["summary"]:
                            await send({"type": "token", "text": ch})
                            await asyncio.sleep(_TK_DELAY.get())
                        await send({"type": "done", "result": result})
                        continue

            # ── r77：期間退貨統計（「上週退貨總共退了幾件」）——曾被黑名單擋，
            #   退貨記在進貨方向、誠實註明 ──
            if (_re.search(r"(上週|本週|今天|昨天|上個?月|本月)[^。]{0,4}退貨"
                           r"|退貨[^。]{0,8}(幾件|多少件|統計|記錄)", user_text)
                    and not _re.search(r"退[貨回]?\s*\d", user_text)):
                _rt_p77 = ("last_week" if any(w in user_text for w in ("上週", "上禮拜"))
                           else "this_month" if "月" in user_text
                           else "yesterday" if "昨天" in user_text
                           else "this_week" if any(w in user_text for w in ("本週", "這週"))
                           else "today")
                result = finance.execute("query_movement",
                                         {"period": _rt_p77, "direction": "in"})
                if result.get("ok") and result.get("summary"):
                    result["summary"] = ("退貨在這個 demo 會記成進貨方向（庫存加回）——"
                                         + result["summary"])
                    log.info(f"[dispatch-ws] 退貨統計 → movement in {_rt_p77}")
                    for ch in result["summary"]:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": result})
                    continue

            # ── r77：「昨天那批出貨有成功嗎」——確定性直答昨天出貨統計
            #   （rewrite 固定句曾在 RPI5 被解析成今天＝平台分歧）──
            if _re.fullmatch(r"昨天.{0,6}出貨(有)?(成功|順利)了?嗎[?？]?",
                             user_text.strip()):
                result = finance.execute("query_movement",
                                         {"period": "yesterday", "direction": "out"})
                if result.get("ok") and result.get("summary"):
                    log.info("[dispatch-ws] 昨天出貨成功追問 → 直答")
                    for ch in result["summary"]:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": result})
                    continue

            # ── r78：短收追問（「它短收幾件」→ ctx 商品的對帳查詢）——曾被
            #   LLM 亂路由成寫入 clarify ──
            if (_re.search(r"短收.{0,3}(幾|多少)", user_text)
                    and not _re.search(r"[進出調補]\s*\d", user_text)):
                import warehouse as _W_sc78
                _sc78_kw = _extract_sku_keyword(user_text) or ""
                if not (_sc78_kw and _W_sc78.match_items(_sc78_kw)):
                    _sc78_kw = _ctx_for(vid).get("last_sku") or ""
                if _sc78_kw:
                    result = finance.execute("search_log", {"keyword": _sc78_kw})
                    if result.get("ok") and result.get("summary"):
                        log.info(f"[dispatch-ws] 短收追問 → search_log {_sc78_kw!r}")
                        for ch in result["summary"]:
                            await send({"type": "token", "text": ch})
                            await asyncio.sleep(_TK_DELAY.get())
                        await send({"type": "done", "result": result})
                        continue

            # ── r77：退貨記在哪（「這批退貨要記在哪」）——已自動入帳，明說 ──
            if _re.search(r"退貨[^。]{0,4}(記在哪|怎麼記|記到哪|要記)", user_text):
                _rr_msg = ("退貨我已經自動記進進出記錄了（方向：進貨、庫存加回）。"
                           "說「今天進出」就看得到那筆。")
                log.info("[dispatch-ws] 退貨記在哪 → 說明")
                for ch in _rr_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "guide", "summary": _rr_msg, "data": {}}})
                continue

            # ── r77：評論式追問（「進的比出的多喔 正常嗎」）——曾掉週轉率比較
            #   ＝答非所問，簡短回應不亂路由 ──
            if (len(user_text) <= 16
                    and _re.search(r"(正常嗎|合理嗎|還好嗎|對嗎)[?？]?$", user_text)
                    and _ctx_for(vid).get("last_func") == "query_movement"):
                _nm_msg = ("進貨大於出貨通常是補貨日的正常節奏，不用緊張。"
                           "想看明細可以說「今天進了什麼貨」或「庫存警示」。")
                log.info("[dispatch-ws] 評論式追問 → 簡答")
                for ch in _nm_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "guide", "summary": _nm_msg, "data": {}}})
                continue

            # ── r77：剛設的警示看一下——曾回 ctx 商品庫存 ──
            if (_ctx_for(vid).get("last_view") in ("alert_done", "alert_list")
                    and any(w in user_text for w in ("剛設", "剛加", "那條"))
                    and any(w in user_text for w in ("看", "顯示", "內容", "檢查"))):
                import tools_v2 as _tv2_al77
                result = _tv2_al77.list_alerts()
                log.info("[dispatch-ws] 剛設警示查看 → list_alerts")
                for ch in result.get("summary", ""):
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": result})
                continue

            # ── r82：營收/毛利/賺多少 誠實閘——demo 只有出貨量與熱銷榜營收，
            #   沒有成本/毛利資料 ──
            if (any(w in user_text for w in ("賺多少", "賺了多少", "毛利", "淨利",
                                              "利潤", "獲利", "賺錢嗎"))
                    and not any(w in user_text for w in ("熱銷", "排行", "賣最"))):
                _rev_msg = ("這個 demo 沒有成本/毛利資料，能看的是出貨量與營收——"
                            "例如「本週熱銷排行」每名都帶營收，或「庫存總值」。")
                log.info(f"[revenue-gate] {user_text!r} → 無毛利資料")
                for ch in _rev_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "guide", "summary": _rev_msg, "data": {}}})
                continue

            # ── r82：倉別總值佔比（「北倉佔比咧」「中南倉勒」）——接在總值後 ──
            _ratio82 = _re.search(r"([北中南]{1,3})(?:區)?倉?[^。]{0,2}(佔比|占比|比重|佔幾成)",
                                  user_text)
            if (_ratio82 or (_re.fullmatch(r"[北中南]{1,3}(?:區)?倉?[的]?[咧勒呢]?",
                                           user_text.strip())
                             and (_ctx_for(vid).get("_last_ratio")
                                  or (_ctx_for(vid).get("last_view") == "inventory_single"
                                      and "總值" in (_ctx_for(vid).get("_cur_text") or ""))))):
                import warehouse as _W_r82
                _r82_s = _W_r82.state()
                _r82_vals, _r82_tot = {}, 0
                for _wh in _r82_s.warehouses:
                    _v = sum(_r82_s._items_by_sku[sk]["unit_price"] * q
                             for sk, q in _r82_s.stock.get(_wh["key"], {}).items()
                             if sk in _r82_s._items_by_sku)
                    _r82_vals[_wh["key"]] = (_wh["label"], _v)
                    _r82_tot += _v
                _ctx_for(vid).pop("_last_ratio", None)   # 用完即清，不殘留污染
                _r82_zh = {"北": "north", "中": "central", "南": "south"}
                _r82_ask = [_r82_zh[z] for z in "北中南"
                            if z in (user_text)] or list(_r82_vals.keys())
                _r82_parts = [f"{_r82_vals[k][0]} {_r82_vals[k][1] * 100 // _r82_tot}%"
                              f"（NT$ {_r82_vals[k][1]:,}）"
                              for k in _r82_ask if k in _r82_vals]
                _r82_msg = "各倉庫存總值佔比：" + "、".join(_r82_parts) + "。"
                _ctx_for(vid)["_last_ratio"] = True   # r82：讓「中南倉勒」接續佔比
                log.info(f"[dispatch-ws] 倉別佔比直答: {_r82_ask}")
                for ch in _r82_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "inventory_single", "summary": _r82_msg,
                    "data": {}}})
                continue

            # ── r80：單一類別總值直答（「電子類總值多少」）——曾掉今天進出統計 ──
            _catv80 = _re.search(r"(電子|家電|廚具|食品|飲料|日用|服飾|運動)"
                                 r"(產品|用品|品)?類?[^。]{0,4}(總值|總價值|值多少)",
                                 user_text)
            if _catv80:
                import warehouse as _W_cv80
                _cv80_map = {"電子": "electronics", "家電": "appliance_kitchen",
                             "廚具": "appliance_kitchen", "食品": "food_beverage",
                             "飲料": "food_beverage", "日用": "daily_goods",
                             "服飾": "apparel", "運動": "sports"}
                _cv80_key = _cv80_map.get(_catv80.group(1))
                _cv80_s = _W_cv80.state()
                _cv80_val = sum(
                    _it["unit_price"] * _cv80_s.stock.get(w["key"], {}).get(_it["sku_id"], 0)
                    for _it in _cv80_s.items if _it["category"] == _cv80_key
                    for w in _cv80_s.warehouses)
                _cv80_lbl = _W_cv80.CATEGORY_LABEL.get(_cv80_key, _cv80_key)
                _cv80_msg = f"「{_cv80_lbl}」類庫存總值約 NT$ {_cv80_val:,}。"
                log.info(f"[dispatch-ws] 類別總值直答: {_cv80_key} {_cv80_val:,}")
                for ch in _cv80_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "inventory_single", "summary": _cv80_msg,
                    "data": {"category": _cv80_key, "total_value": _cv80_val}}})
                continue

            # ── r80：「照建議補」——行動意圖，直接開最急項的進貨卡（HITL）──
            if (_re.fullmatch(r"(好|嗯|那)?就?照(這個)?建議[補進來辦](吧|好了)?",
                              user_text.strip().strip("!！?？。 "))
                    and _ctx_for(vid).get("last_view") == "low_stock"):
                import warehouse as _W_sg80
                _sg80_ws = ((_W_sg80.list_low_stock().get("data") or {})
                            .get("warnings") or [])
                if _sg80_ws:
                    _w80 = _sg80_ws[0]
                    result = finance.execute("create_movement", {
                        "keyword": _w80.get("name"),
                        "warehouse": _w80.get("warehouse_label", ""),
                        "direction": "in", "qty": str(_w80.get("suggest_qty", ""))})
                    if result.get("ok") and result.get("summary"):
                        log.info(f"[dispatch-ws] 照建議補 → 開卡 {_w80.get('name')}")
                        for ch in result["summary"]:
                            await send({"type": "token", "text": ch})
                            await asyncio.sleep(_TK_DELAY.get())
                        await send({"type": "done", "result": result})
                        continue

            # ── r77：全店平均單價（「全店東西平均一件多少錢」）──
            if (_re.search(r"平均[^。]{0,6}(一件|單價|多少錢|幾錢)", user_text)
                    and (any(w in user_text for w in ("全店", "全部", "商品", "東西",
                                                       "庫存", "整體"))
                         # r80：「懂了 平均一件多少」——「平均一件」本身就夠明確
                         or "平均一件" in user_text)):
                import warehouse as _W_avg
                _av_s = _W_avg.state()
                _av_val = _av_qty = 0
                for _wh in _av_s.warehouses:
                    for _sk, _q in _av_s.stock.get(_wh["key"], {}).items():
                        if _sk in _av_s._items_by_sku:
                            _av_val += _av_s._items_by_sku[_sk]["unit_price"] * _q
                            _av_qty += _q
                _av_avg = (_av_val // _av_qty) if _av_qty else 0
                _av_msg = (f"全店庫存 {_av_qty:,} 件、總值約 NT$ {_av_val:,}，"
                           f"平均一件約 NT$ {_av_avg:,}。")
                log.info(f"[dispatch-ws] 平均單價直答: {_av_avg}")
                for ch in _av_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "inventory_single", "summary": _av_msg,
                    "data": {"avg_price": _av_avg, "total_qty": _av_qty}}})
                continue

            # ── r76：缺貨清單序數建議補直答（「第一個缺的補到安全線 要進幾個」）
            #   ——曾掉「找不到商品」醜 clarify ──
            _ls76_ord = _re.search(r"(第\s*([一二三四五12345])\s*[個急]|最急|最緊急)",
                                   user_text)
            if (_ls76_ord and _ctx_for(vid).get("last_view") == "low_stock"
                    and (_re.search(r"[補進].{0,4}幾|補到安全|要[補進]多少|建議補多少",
                                    user_text)
                         # r78：「第二急的是什麼」身分形也接
                         # r82：「第一個是啥」（缺貨清單後追問榜首）——啥/哪一項
                         or _re.search(r"是什麼|是誰|是哪個|是哪一個|是啥|哪一項|是哪項",
                                       user_text))
                    and not _re.search(r"[進出調補]\s*\d", user_text)):
                import warehouse as _W_ls76
                _ls76_res = _W_ls76.list_low_stock()
                _ls76_ws = (_ls76_res.get("data") or {}).get("warnings") or []
                if _ls76_ws:
                    _ls76_n = ({"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
                               .get((_ls76_ord.group(2) or ""), None)
                               or (int(_ls76_ord.group(2))
                                   if (_ls76_ord.group(2) or "").isdigit() else 1))
                    _ls76_i = min(max(_ls76_n - 1, 0), len(_ls76_ws) - 1)
                    _w76 = _ls76_ws[_ls76_i]
                    _ls76_sum = (f"第{_ls76_i + 1}急的是「{_w76.get('name')}」"
                                 f"（{_w76.get('warehouse_label', '')}剩 {_w76.get('qty')} 件、"
                                 f"約再 {_w76.get('days_left')} 天斷貨）——"
                                 f"補到安全線建議補 {_w76.get('suggest_qty')} 件。"
                                 f"要記進貨的話直接說「{_w76.get('warehouse_label', '北區倉')}"
                                 f"進{_w76.get('suggest_qty')}個{_w76.get('name')}」。")
                    log.info(f"[dispatch-ws] 缺貨序數建議補: {_w76.get('name')}")
                    for ch in _ls76_sum:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "low_stock", "summary": _ls76_sum,
                        "data": {"name": _w76.get("name"), "warnings": [_w76],
                                 "warehouse": "all", "count": 1}}})
                    continue

            # ── r74：類別總值排行直答（「哪一類最值錢」）——曾誤觸類別身分答
            #   「濕紙巾屬於日用品類」＝答非所問 ──
            _cv_m75 = _re.search(r"(哪一?類|什麼類|哪個?類別).{0,3}"
                                 r"(最值錢|最貴|價值最高|總值最高|最不值錢|最沒價值|"
                                 r"價值最低|總值最低|最便宜)", user_text)
            if _cv_m75:
                import warehouse as _W_cv
                _cv_s = _W_cv.state()
                _cv_low = _cv_m75.group(2) in ("最不值錢", "最沒價值", "價值最低",
                                               "總值最低", "最便宜")
                _cv_by: dict = {}
                for _it in _cv_s.items:
                    _q = sum(_cv_s.stock.get(w["key"], {}).get(_it["sku_id"], 0)
                             for w in _cv_s.warehouses)
                    _cv_by[_it["category"]] = (_cv_by.get(_it["category"], 0)
                                               + _it["unit_price"] * _q)
                _cv_rank = sorted(_cv_by.items(),
                                  key=lambda kv: kv[1] if _cv_low else -kv[1])[:3]
                _cv_sum = (("Lowest stock value: " if _cv_low else "Highest stock value: ")
                           + _W_cv.CATEGORY_LABEL.get(_cv_rank[0][0], _cv_rank[0][0])
                           + f" at approx NT$ {_cv_rank[0][1]:,}; followed by "
                           + ", ".join(f"{_W_cv.CATEGORY_LABEL.get(c, c)} NT$ {v:,}"
                                       for c, v in _cv_rank[1:]) + ".")
                log.info(f"[dispatch-ws] 類別總值排行直答: {_cv_rank[0][0]}")
                for ch in _cv_sum:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "inventory_single", "summary": _cv_sum,
                    "data": {"rank": [[c, v] for c, v in _cv_rank]}}})
                continue

            # ── r58：單品撐幾天直答（「它撐幾天」「衛生紙還能撐多久」）——days_left
            #   資料一直都有（v3.9.1 起），過去只回庫存數字＝半答 ──
            if any(w in user_text for w in ("撐幾天", "撐多久", "能撐", "幾天斷貨",
                                             "還能賣幾天", "夠賣多久", "賣多久",
                                             "日銷", "每天賣幾", "一天賣幾",
                                             # r68：撐得了/建議補（答案本來就含建議補 N 件）
                                             "撐得了", "建議補", "該補幾", "要補幾",
                                             "撐得過", "撐到", "夠撐",   # r70/r73
                                             # r74：「照這樣多久賣完」曾掉熱銷榜＝答非所問
                                             "多久賣完", "幾天賣完", "多久賣光", "賣得完")) \
                    and not _re.search(r"[進出調補]\s*\d", user_text):
                # r76 危險邊緣：「那就照建議補 北倉進13個電動牙刷」是寫入句，
                # 曾被「建議補」token 吞成查詢直答——卡沒開、訪客以為補了。
                # 帶寫入動詞+數字一律讓給開卡流程
                import warehouse as _W_dl
                # r70：extract 抽到垃圾詞（「第一項建議補多少」的「第一項」）曾蓋掉
                # ctx——比不到就退回 last_sku 再試
                _dl_kw = _extract_sku_keyword(user_text) or ""
                _dl_m = _W_dl.match_items(_dl_kw) if _dl_kw else []
                if not (_dl_m and _dl_m[0].get("score", 0) >= 3):
                    _dl_kw = _ctx_for(vid).get("last_sku") or ""
                    _dl_m = _W_dl.match_items(_dl_kw) if _dl_kw else []
                if not (_dl_m and _dl_m[0].get("score", 0) >= 3):
                    # r71：冷 context 純撐天短句 → 友善反問（曾掉守門員教學文）。
                    # 全域句（「見底的貨…要補幾個」）放行給既有缺貨清單路。
                    if (len(user_text) <= 12
                            and not any(w in user_text for w in ("見底", "哪些", "全部",
                                                                  "清單", "的貨", "什麼"))):
                        _dl_msg = "想看哪個商品能撐幾天？直接說商品名，例如「衛生紙撐幾天」。"
                        log.info(f"[dispatch-ws] 撐天無指涉 → 反問: {user_text!r}")
                        for ch in _dl_msg:
                            await send({"type": "token", "text": ch})
                            await asyncio.sleep(_TK_DELAY.get())
                        await send({"type": "done", "result": {
                            "ok": True, "view": "clarify", "summary": _dl_msg,
                            "data": {"question": _dl_msg, "options": [], "hint": ""}}})
                        continue
                if _dl_m and _dl_m[0].get("score", 0) >= 3:
                    _dl_name = _dl_m[0]["item"]["name"]
                    _dl_res = _W_dl.list_low_stock()
                    _dl_ws = [w for w in (_dl_res.get("data") or {}).get("warnings", [])
                              if w.get("name") == _dl_name]
                    if _dl_ws:
                        _dl_top = min(_dl_ws, key=lambda w: w.get("days_left", 999))
                        _dl_sum = (f"「{_dl_name}」最吃緊的是{_dl_top['warehouse_label']}："
                                   f"剩 {_dl_top['qty']} 件、日銷約 {_dl_top['daily_burn']}，"
                                   f"約再 {_dl_top['days_left']} 天斷貨"
                                   f"（建議補 {_dl_top['suggest_qty']} 件）。")
                    else:
                        _dl_sum = f"「{_dl_name}」目前各倉都在安全庫存之上，短期沒有斷貨風險 ✅"
                    log.info(f"[dispatch-ws] 撐天直答: {_dl_name}")
                    for ch in _dl_sum:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "low_stock", "summary": _dl_sum,
                        "data": {"name": _dl_name, "warnings": _dl_ws,
                                 "warehouse": "all", "count": len(_dl_ws)}}})
                    continue

            # ── r56：寫入邊緣攔截 ──
            # 負數量寫入（「北倉進-5個衛生紙」曾被當庫存查詢＝答非所問）
            if (_re.search(r"[進出調補]\s*[-−]\s*\d", user_text)
                    or _re.search(r"[-−]\d+\s*[個件箱包]", user_text)):
                _neg_msg = ("數量不能是負數喔。要減少庫存請用「出貨」，"
                            "例如「北倉出5個衛生紙」。")
                log.info(f"[write-edge] 負數量 → clarify: {user_text!r}")
                for ch in _neg_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "clarify", "summary": _neg_msg,
                    "data": {"question": _neg_msg, "options": [], "hint": ""}}})
                continue
            # 目標水位式寫入（「出到剩10個」「補到100個」）——語意是「補/出到剩 N」，
            # 若照數字開卡會異動錯量（危險邊緣），誠實說不支援
            if _re.search(r"[出進補]到剩?\s*\d+", user_text):
                _tl_msg = ("「出到剩幾件／補到幾件」這種目標水位操作還不支援，"
                           "請直接說要異動的數量，例如「北倉出20個衛生紙」。")
                log.info(f"[write-edge] 目標水位 → clarify: {user_text!r}")
                for ch in _tl_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "clarify", "summary": _tl_msg,
                    "data": {"question": _tl_msg, "options": [], "hint": ""}}})
                continue
            # 清空/比例式出貨（「全部的衛生紙都出掉」「衛生紙出一半」）→ 不猜數量
            if (any(w in user_text for w in ("出掉", "出光", "清光", "全出", "全部出")) or
                    (_re.search(r"出", user_text)
                     and any(w in user_text for w in ("全部", "全都", "通通", "整批", "一半", "一部分")))) \
                    and not any(w in user_text for w in ("庫存", "警示", "排行", "統計",
                                                          "紀錄", "記錄", "多少", "幾件", "比較",
                                                          # 「排程全部列出來」「通通列出來」的
                                                          # 「出」是列出不是出貨（r56 誤傷修正）
                                                          "列出", "出來", "排程", "清單")) \
                    and not _re.search(r"\d+\s*[件個箱瓶罐組雙]", user_text):
                    # r57：「出掉20件 北倉」帶確切數字＝正常出貨，不攔（r56 誤傷修正）
                _pc_msg = ("「全部／一半」這類比例我不會自己換算成件數（怕出錯量）。"
                           "請說確切數量，例如「北倉出50個衛生紙」。")
                log.info(f"[write-edge] 比例出貨 → clarify: {user_text!r}")
                for ch in _pc_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "clarify", "summary": _pc_msg,
                    "data": {"question": _pc_msg, "options": [], "hint": ""}}})
                continue

            # ── r62：促銷/檔期語（打折/主打/買一送一）——沒有價格促銷資料，優雅明說
            #   （曾回商品庫存＝答非所問、或 related_help 亂接）──
            # r62b：帶真商品的促銷句（「衛生紙有優惠嗎」）是既有守衛接受的
            # 「顯示該商品庫存」半答——只攔沒點名商品的檔期句
            if (any(w in user_text for w in ("打折", "促銷", "特價", "主打", "主推",
                                              "買一送一", "優惠", "檔期", "折扣"))
                    and not _has_real_item(user_text)):
                _pm_msg = ("這個 demo 沒有建價格促銷／檔期資料，我能查的是庫存、"
                           "進出貨、缺貨與到期警示——例如「啤酒庫存」「本月熱銷排行」。")
                log.info(f"[promo-gate] {user_text!r} → 促銷資料未建檔")
                for ch in _pm_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "guide", "summary": _pm_msg, "data": {}}})
                continue

            # ── r78：「差最多的是哪個」——週對週比較後追問，確定性走跨期比較
            if _re.fullmatch(r"(那)?(差|掉|落差)最多的?(是)?(哪個|誰|啥|什麼)?",
                             user_text.strip().strip("!！?？。 ")):
                result = finance.execute("compare_periods", {"metric": "out"})
                if result.get("ok") and result.get("summary"):
                    log.info("[dispatch-ws] 差最多追問 → compare_periods")
                    for ch in result["summary"]:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": result})
                    continue

            # ── r78：「現在先跑一次」——剛看完排程，指的是立刻跑那個腳本
            if (_re.fullmatch(r"(那)?(現在|馬上|立刻)?先?(跑|執行)一[次遍](好了|吧|看看)?",
                              user_text.strip().strip("!！?？。 "))
                    and _ctx_for(vid).get("last_view") in ("schedule_list",
                                                            "schedule_done")):
                import tools_v2 as _tv2_rn78
                _sl78 = _tv2_rn78.list_schedules()
                _jobs78 = (_sl78.get("data") or {}).get("jobs") or []
                if _jobs78:
                    result = _tv2_rn78.run_script(
                        script_name=(_jobs78[0].get("script_label")
                                     or _jobs78[0].get("script_id", "")))
                    log.info("[dispatch-ws] 排程後跑一次 → run_script")
                    for ch in result.get("summary", ""):
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": result})
                    continue

            # ── r78：改回原設定句（「算了算了 改回原本的」）——不記舊值，
            #   引導直接講數值 ──
            if _re.search(r"(改回|恢復|退回)[^。]{0,3}(原本|原樣|原值|原設定|預設)",
                          user_text):
                _cr78_msg = ("我沒有記住舊值，直接說要改回的數字就可以，"
                             "例如「北倉衛生紙安全庫存改成100」。"
                             "（歷次改動都記在 audit log 裡）")
                log.info(f"[cfg-restore-gate] {user_text!r}")
                for ch in _cr78_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "clarify", "summary": _cr78_msg,
                    "data": {"question": _cr78_msg, "options": [], "hint": ""}}})
                continue

            # ── r76：改排程時間句（schedule_list 後「改成每週五」）——不支援改期，
            #   優雅明說刪掉重設的路 ──
            if (_ctx_for(vid).get("last_view") in ("schedule_list", "schedule_done")
                    and len(user_text) <= 14
                    and _re.search(r"改(成|到|去|為)?\s*(每週[一二三四五六日天]?|每天|每日|"
                                   r"每月|週[一二三四五六日]|[0-9一二三四五六七八九十]+點|時間)",
                                   user_text)):
                _sc_msg = ("排程目前不支援直接改時間喔。先說「刪掉它」取消現有排程，"
                           "再重新設定（例如「每週五早上八點匯出進出記錄」）就可以。")
                log.info(f"[sched-edit-gate] {user_text!r} → 不支援改期")
                for ch in _sc_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "clarify", "summary": _sc_msg,
                    "data": {"question": _sc_msg, "options": [], "hint": ""}}})
                continue

            # ── r76：英文介面詢問——介面只有中文，優雅明說 ──
            if _re.search(r"英文(介面|版|模式)|有沒有英文|english", user_text, _re.IGNORECASE) \
                    and "庫存" not in user_text:
                _en_msg = ("介面目前只有中文喔。不過商品查詢聽得懂簡單英文，"
                           "例如「show me bluetooth earphone stock」。")
                log.info(f"[en-gate] {user_text!r} → 介面只有中文")
                for ch in _en_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "guide", "summary": _en_msg, "data": {}}})
                continue

            # ── r75：排程查詢短句（「之前設的排程還在嗎」）——clf 常判 no_function
            #   掉 rejected，直達 list_schedules ──
            if any(w in user_text for w in ("排程還在", "排程還有", "還有排程",
                                             "排程狀態", "我的排程")):
                import tools_v2 as _tv2_sl75
                result = _tv2_sl75.list_schedules()
                log.info("[dispatch-ws] 排程查詢直達 → list_schedules")
                for ch in result.get("summary", ""):
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": result})
                continue

            # ── r75：報告/檔案在哪看——曾重跑一次報告（答非所問），直接回最新
            #   檔案路徑 ──
            _rw78_done = (_ctx_for(vid).get("last_view")
                          in ("script_done", "report_done", "po_done"))
            if ((any(w in user_text for w in ("在哪", "放哪", "去哪", "哪裡", "哪邊"))
                    and any(w in user_text.lower() for w in ("報告", "報表", "檔案",
                                                              "csv", "採購單", "po"))
                    and not any(w in user_text for w in ("產出", "產生", "跑一")))
                    # r78：「結果咧」「跑完了嗎」——剛跑完腳本/報告/採購單的成果追問
                    or (_rw78_done and _re.fullmatch(
                        r"(結果|成果)(咧|呢|如何|怎樣)?|跑完了嗎|好了嗎|出來了嗎"
                        # r84：「存哪了」「檔案咧」——剛匯出後問存放位置
                        r"|存哪了?|存到哪|放哪了?|檔案咧|檔案呢|檔案在哪",
                        user_text.strip().strip("!！?？。 ")))):
                import tools_v2 as _tv2_rp75
                _dd75 = _tv2_rp75._data_dir()
                _cands75 = []
                for _sub75 in ("reports", "audit", "orders/PO_draft"):
                    _p75 = _dd75 / _sub75
                    if _p75.exists():
                        # 只認產出物（md/csv/png/json）——*.log 是稽核流水不是報告
                        _cands75 += [(f.stat().st_mtime, f"{_sub75}/{f.name}")
                                     for f in _p75.iterdir()
                                     if f.is_file()
                                     and f.suffix.lower() in (".md", ".csv", ".png",
                                                              ".json")]
                # r78：問採購單就只看 PO_draft（曾回不相干的圖表 PNG）
                if any(w in user_text.lower() for w in ("採購單", "po")):
                    _cands75 = [c for c in _cands75 if "PO_draft" in c[1]]
                if _cands75:
                    _rp_msg = (f"最新產出的檔案在 {max(_cands75)[1]}"
                               "（伺服器 warehouse_data 資料夾底下）。")
                else:
                    _rp_msg = "目前還沒有產出過報告，可以說「產出體檢報告」或「月底盤點」。"
                log.info(f"[report-where] {user_text!r} → {_rp_msg[:40]}")
                for ch in _rp_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "guide", "summary": _rp_msg, "data": {}}})
                continue

            # ── r75：改價句誠實閘（「價格改成299」）——demo 單價是固定資料，
            #   不支援改價，曾掉守門員教學文 ──
            if (any(w in user_text for w in ("價格", "單價", "售價", "定價"))
                    and any(w in user_text for w in ("改", "調成", "調高", "調低",
                                                      "設成", "變更", "漲", "降"))
                    and not any(w in user_text for w in ("安全", "庫存", "警戒", "水位"))):
                _pp_msg = ("這個 demo 的商品單價是固定資料，不支援改價喔。"
                           "可以查單價，例如「藍牙耳機一個賣多少」。")
                log.info(f"[price-gate] {user_text!r} → 不支援改價")
                for ch in _pp_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "guide", "summary": _pp_msg, "data": {}}})
                continue

            # ── r74：訂單/預約出貨句（「先看今天要出的單」「有預定出貨嗎」）——
            #   沒有訂單系統，曾回熱銷榜＝答非所問，優雅明說 ──
            if (any(w in user_text for w in ("要出的單", "預定出貨", "預約出貨",
                                              "待出貨", "出貨單", "接單"))
                    and not any(w in user_text for w in ("記錄", "紀錄", "統計", "報表"))
                    and not _re.search(r"[進出調補]\s*\d", user_text)):
                _od_msg = ("這個 demo 沒有訂單／預約出貨資料，出貨都是當下記錄。"
                           "可以看「今天出了什麼貨」或「本週出貨統計」。")
                log.info(f"[order-gate] {user_text!r} → 訂單資料未建檔")
                for ch in _od_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "guide", "summary": _od_msg, "data": {}}})
                continue

            # ── r56：空間方位句（倉位/樓層/地址沒建檔）→ 優雅明說，不再回
            #   「沒有『在哪裡』這個商品」這種醜 clarify ──
            # 只收「位置疑問」詞——貨架/架子這類純名詞不收（「貨架上還有衛生紙嗎」
            # 是存量查詢，r56 誤傷修正）；「第三排架子上有什麼」由第N排 regex 接
            _SPATIAL56 = ("在哪裡", "在哪邊", "地址", "幾坪", "坪數", "放得下", "放不下",
                          "哪一排", "第幾排", "幾樓", "樓上", "樓下", "哪一區", "位置在哪")
            if (any(w in user_text for w in _SPATIAL56)
                    or _re.search(r"第[一二三四五六七八九十\d]+排", user_text)):
                _sp_msg = ("這個 demo 沒有建倉位／樓層／地址這類實體位置資料，"
                           "我能查的是數量與警示——例如「衛生紙庫存」「北倉缺貨清單」。")
                log.info(f"[spatial-gate] {user_text!r} → 位置資訊未建檔")
                for ch in _sp_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "guide", "summary": _sp_msg, "data": {}}})
                continue

            # ── r56：到期清單後問「最急的那批放哪個倉」——曾被缺貨警示搶走，
            #   依 context 分流回到期清單（開頭就是最急批+倉別）──
            if (any(w in user_text for w in ("最急", "最緊急"))
                    and any(w in user_text for w in ("批", "哪個倉", "哪一倉", "在哪倉"))
                    and _ctx_for(vid).get("last_func") == "list_expiring_items"
                    # r68：複合寫入句（「最急那批處理掉 出586件北倉氣泡水」）不可
                    # 被到期重秀吃掉——帶寫入動詞+數字就讓路給寫入流程
                    and not _re.search(r"[進出調補]\s*\d", user_text)):
                import warehouse as _W_xq
                _xq_res = _W_xq.list_expiring_items()
                log.info(f"[dispatch-ws] 到期最急批分流: {user_text!r}")
                for ch in (_xq_res.get("summary") or ""):
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": _xq_res})
                continue

            # ── r55 收官批：本週 vs 上週進出比較——「這禮拜跟上週比」曾答非所問
            #   回倉庫庫存價值比較（訪客問的是時段進出量）──
            _wkc = _re.search(r"(這|本)(週|禮拜|星期).{0,6}(上|前)(週|禮拜|星期).{0,4}(比|差)"
                              r"|(上|前)(週|禮拜|星期).{0,6}(這|本)(週|禮拜|星期).{0,4}(比|差)",
                              user_text)
            # r80：週對週比較後「差幾件」追問——重算差值直答
            _wkc_f80 = (_re.fullmatch(r"(那)?差了?(幾|多少)件?",
                                      user_text.strip().strip("!！?？。 "))
                        and _ctx_for(vid).get("last_func") == "query_movement")
            if _wkc or _wkc_f80:
                import warehouse as _W_wk
                _wk_a = _W_wk.query_movement(period="this_week")
                _wk_b = _W_wk.query_movement(period="last_week")
                _wk_da, _wk_db = _wk_a.get("data", {}), _wk_b.get("data", {})
                _wk_din = _wk_da.get("in_qty", 0) - _wk_db.get("in_qty", 0)
                _wk_dout = _wk_da.get("out_qty", 0) - _wk_db.get("out_qty", 0)
                _wk_sum = (f"{_wk_a.get('summary', '')}\n{_wk_b.get('summary', '')}\n"
                           f"→ 本週比上週：進貨 {_wk_din:+,} 件、出貨 {_wk_dout:+,} 件")
                log.info(f"[dispatch-ws] 週對週進出比較: {user_text!r}")
                for ch in _wk_sum:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "movement", "summary": _wk_sum,
                    "data": {"period": "this_week", "in_qty": _wk_da.get("in_qty", 0),
                             "out_qty": _wk_da.get("out_qty", 0),
                             "delta": _wk_da.get("delta", 0),
                             "compare_last_week": {"in_qty": _wk_db.get("in_qty", 0),
                                                    "out_qty": _wk_db.get("out_qty", 0)}}}})
                continue

            # ── r55 收官批：補貨成本直答——「補起來要多少錢」曾回 60 項概覽 ──
            if (any(w in user_text for w in ("補起來", "補齊", "補滿", "全補", "都補", "補貨", "補一補"))
                    and any(w in user_text for w in ("多少錢", "要花", "成本", "預算"))):
                import warehouse as _W_rc
                _rc_res = _W_rc.list_low_stock()
                _rc_warn = (_rc_res.get("data") or {}).get("warnings") or []
                _rc_items = _W_rc.state()._items_by_sku
                _rc_total = sum(w["suggest_qty"] * _rc_items.get(w["sku_id"], {}).get("unit_price", 0)
                                for w in _rc_warn)
                _rc_qty = sum(w["suggest_qty"] for w in _rc_warn)
                _rc_sum = (f"目前 {len(_rc_warn)} 筆低庫存警示，照建議補量全補齊約需 "
                           f"{_rc_qty:,} 件、金額約 NT$ {_rc_total:,}。明細如下表。")
                _rc_out = dict(_rc_res)
                _rc_out["summary"] = _rc_sum
                log.info(f"[dispatch-ws] 補貨成本直答: {_rc_qty} 件 / NT$ {_rc_total:,}")
                for ch in _rc_sum:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": _rc_out})
                continue

            # ── r74：庫存總值直答（「那看庫存總值」「全店值多少錢」）——過去被
            #   ctx 注入成單品查詢或掉進 60 項概覽，總值問句直接加總回答 ──
            _tv74 = (any(w in user_text for w in ("總值", "總價值", "總市值"))
                     # r75：「南倉整體值多少錢」也算總值問句
                     or _re.search(r"(庫存|全店|全倉|倉庫|整體|[北中南](?:區)?倉)"
                                   r"[^。]{0,4}值多少", user_text)
                     # r77：「北中南各值多少」曾掉 60 項概覽
                     or _re.search(r"([北中南]{2,3}|三倉|各倉)各?值多少", user_text))
            if _tv74 and not any(c in user_text for c in
                                 ("電子", "家電", "廚具", "食品", "飲料", "日用",
                                  "服飾", "運動", "清潔", "嬰兒", "類")):
                import warehouse as _W_tv
                _tv_stem = user_text
                for _bw in ("庫存總值", "總價值", "總市值", "總值", "值多少錢", "值多少",
                            "那看", "看一下", "給我", "幫我", "查", "看", "的", "呢", "現在"):
                    _tv_stem = _tv_stem.replace(_bw, "")
                _tv_stem = _tv_stem.strip("?？。!！，, ")
                _tv_wh = next((k for z, k in (("北", "north"), ("中", "central"),
                                              ("南", "south")) if z + "倉" in user_text
                               or z + "區倉" in user_text), None)
                _tv_prod = (_W_tv.match_items(_tv_stem)
                            if len(_tv_stem) >= 2 and _tv_stem not in
                            ("全店", "全倉", "倉庫", "全部") else [])
                if not _tv_prod:
                    _tv_s = _W_tv.state()
                    _tv_parts, _tv_total = [], 0
                    for _wh in _tv_s.warehouses:
                        if _tv_wh and _wh["key"] != _tv_wh:
                            continue
                        _v = sum(_tv_s._items_by_sku[sk]["unit_price"] * q
                                 for sk, q in _tv_s.stock.get(_wh["key"], {}).items()
                                 if sk in _tv_s._items_by_sku)
                        _tv_total += _v
                        _tv_parts.append(f"{_wh['label']} NT$ {_v:,}")
                    _tv_scope = ("全店庫存總值" if not _tv_wh
                                 else f"{_tv_parts[0].split(' ')[0]}庫存總值")
                    _tv_sum = (f"{_tv_scope}約 NT$ {_tv_total:,}"
                               + ("" if _tv_wh else f"（{'、'.join(_tv_parts)}）") + "。")
                    log.info(f"[dispatch-ws] 庫存總值直答: {_tv_total:,}")
                    for ch in _tv_sum:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "inventory_single", "summary": _tv_sum,
                        "data": {"total_value": _tv_total}}})
                    continue

            # ── r72：數量×單價試算（「買10組總共多少錢」）——ctx 商品 × N ──
            _qp72 = _re.search(r"[買進拿]?\s*([0-9]{1,4})\s*[組個件盒箱].{0,4}(總共|一共|共)?多少錢",
                               user_text)
            if _qp72 and _ctx_for(vid).get("last_sku"):
                import warehouse as _W_qp
                _qp_m = _W_qp.match_items(_ctx_for(vid)["last_sku"])
                if _qp_m:
                    _qp_it = _qp_m[0]["item"]
                    _qp_n = int(_qp72.group(1))
                    _qp_sum = (f"「{_qp_it['name']}」單價 NT$ {_qp_it['unit_price']:,}，"
                               f"{_qp_n} 件約 NT$ {_qp_it['unit_price'] * _qp_n:,}。")
                    log.info(f"[dispatch-ws] 數量試算: {_qp_it['name']} × {_qp_n}")
                    for ch in _qp_sum:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "inventory_single", "summary": _qp_sum,
                        "data": {"name": _qp_it["name"], "unit_price": _qp_it["unit_price"]}}})
                    continue

            # ── 單品價格直答（r25）：「機能排汗衣一件賣多少錢」曾回庫存無單價 ──
            # r58 補「價格/售價」裸詞（「露營全套價格」曾繞過組合詞選單回單品庫存）
            if (any(w in user_text for w in ("多少錢", "什麼價", "單價", "一件賣", "一個賣",
                                              "價格", "售價"))
                    and not any(w in user_text for w in ("改", "設", "調", "元", "折",
                                                          "最貴", "最便宜", "最高", "最低"))):
                import warehouse as _W_pq
                # r55 收官批：「露營全套多少錢」——「全套/整套」是多件組合，extractor 會
                # 靜默挑一件（曾只報折疊露營椅）。剝掉組合詞+價格詞取詞幹，詞幹對到
                # 多個商品就列選單讓訪客選。
                if any(w in user_text for w in ("全套", "整套", "一整組", "一套", "組合價",
                                                 "全部", "全都")):   # r57：「帽子全部多少錢」
                    _pq_stem = user_text
                    for _bw in ("全套", "整套", "一整組", "一套", "組合價", "全部", "全都",
                                "多少錢", "什麼價", "單價", "價格多少", "價格", "要", "的", "買"):
                        _pq_stem = _pq_stem.replace(_bw, "")
                    _pq_stem = _pq_stem.strip("?？。!！， ")
                    _pq_sm = _W_pq.match_items(_pq_stem) if len(_pq_stem) >= 2 else []
                    # r57：通稱詞幹（帽子/鍋子）比不到 → 用通稱表展開候選
                    if len(_pq_sm) <= 1 and len(_pq_stem) >= 2:
                        _pq_gen = getattr(_W_pq, "_GENERIC_QUERY_FALLBACK", {}).get(_pq_stem)
                        if _pq_gen:
                            _pq_sm = []
                            for _gfrag in _pq_gen:
                                _gm = _W_pq.match_items(_gfrag)
                                if _gm:
                                    _pq_sm.append(_gm[0])
                    if len(_pq_sm) > 1:
                        _pq_snames = [r["item"]["name"] for r in _pq_sm[:8]]
                        _pq_sq = (f"\"{_pq_stem}\" covers {len(_pq_snames)} items. "
                                  f"Which one's price do you want? (or tap them one by one)")
                        log.info(f"[dispatch-ws] 組合詞價格選單: {_pq_stem!r} × {len(_pq_snames)}")
                        for ch in _pq_sq:
                            await send({"type": "token", "text": ch})
                            await asyncio.sleep(_TK_DELAY.get())
                        await send({"type": "done", "result": {
                            "ok": True, "view": "clarify", "summary": _pq_sq,
                            "data": {"question": _pq_sq,
                                     "options": [f"how much is {n}" for n in _pq_snames],
                                     "hint": "Tap one, or say the full item name"}}})
                        continue
                _pq_kw = _extract_sku_keyword(user_text)
                _pq_m = _W_pq.match_items(_pq_kw) if _pq_kw else []
                # r59：「鍋子價格」extractor 抽不到 → 剝價格詞取詞幹，用通稱表展開選單
                if not _pq_m:
                    _pq_st59 = user_text
                    for _bw59 in ("多少錢", "什麼價", "單價", "價格多少", "價格", "售價",
                                  "的", "要", "買", "一個", "一件"):
                        _pq_st59 = _pq_st59.replace(_bw59, "")
                    _pq_st59 = _pq_st59.strip("?？。!！， ")
                    _pq_g59 = (getattr(_W_pq, "_GENERIC_QUERY_FALLBACK", {}).get(_pq_st59)
                               if len(_pq_st59) >= 2 else None)
                    if _pq_g59:
                        _pq_n59 = []
                        for _gf59 in _pq_g59:
                            _gm59 = _W_pq.match_items(_gf59)
                            if _gm59:
                                _pq_n59.append(_gm59[0]["item"]["name"])
                        if len(_pq_n59) > 1:
                            _pq_q59 = (f"\"{_pq_st59}\" matches {len(_pq_n59)} items. "
                                       f"Which one's price do you want?")
                            log.info(f"[dispatch-ws] 通稱價格選單: {_pq_st59!r}")
                            for ch in _pq_q59:
                                await send({"type": "token", "text": ch})
                                await asyncio.sleep(_TK_DELAY.get())
                            await send({"type": "done", "result": {
                                "ok": True, "view": "clarify", "summary": _pq_q59,
                                "data": {"question": _pq_q59,
                                         "options": [f"how much is {n}" for n in _pq_n59],
                                         "hint": "Tap one, or say the full item name"}}})
                            continue
                        elif len(_pq_n59) == 1:
                            _pq_m = _W_pq.match_items(_pq_n59[0])
                # r55 收官批：多商品同分歧義（「露營全套多少錢」曾靜默挑折疊露營椅報價）
                # → 跟庫存查詢同一套不猜邏輯，列選單讓訪客選
                if (_pq_m and len(_pq_m) > 1 and _pq_m[0].get("score", 0) >= 3
                        and _pq_m[0]["score"] - _pq_m[1]["score"] < 3):
                    _pq_tied = [r["item"]["name"] for r in _pq_m
                                if r.get("score", 0) * 2 >= _pq_m[0]["score"]][:8]
                    if len(_pq_tied) > 1:
                        _pq_q = (f"\"{_pq_kw}\" matches {len(_pq_tied)} items. "
                                 f"Which one's price do you want?")
                        log.info(f"[dispatch-ws] 價格歧義選單: {_pq_kw!r} × {len(_pq_tied)}")
                        for ch in _pq_q:
                            await send({"type": "token", "text": ch})
                            await asyncio.sleep(_TK_DELAY.get())
                        await send({"type": "done", "result": {
                            "ok": True, "view": "clarify", "summary": _pq_q,
                            "data": {"question": _pq_q,
                                     "options": [f"how much is {n}" for n in _pq_tied],
                                     "hint": "Tap one, or say the full item name"}}})
                        continue
                if _pq_m and _pq_m[0].get("score", 0) >= 3 and _kw_grounded(_pq_kw, user_text):
                    _pq_it = _pq_m[0]["item"] if "item" in _pq_m[0] else _pq_m[0]
                    _pq_sum = f"「{_pq_it['name']}」單價 NT$ {_pq_it['unit_price']:,}。"
                    log.info(f"[dispatch-ws] 單品價格直答: {_pq_it['name']}")
                    for ch in _pq_sum:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "inventory_single", "summary": _pq_sum,
                        "data": {"name": _pq_it["name"], "unit_price": _pq_it["unit_price"]}}})
                    continue

            # ── item_create 流程中 → 攔截處理，不進 LLM（per-vid）──
            if _ic_st.get("active"):
                # r32：流程中訪客常常改問別的（「無線滑鼠還剩幾個」），過去整句被
                #   吞成欄位值 → 商品類別變成「無線滑鼠還剩幾個」。明顯的查詢句不
                #   寫進欄位，提示他先退出流程。
                # r33：詞表太窄 → 「藍牙耳機庫存」仍被吞成商品類別。改為「查詢動詞/
                #   問句特徵」為主（合法類別值是「戶外用品」「日用品」這種名詞，不含
                #   這些詞），並加上「庫存」「排行」等明確功能詞。
                _CREATE_QUERY_WORDS = ("還剩", "剩多少", "剩幾", "有多少", "多少件",
                                       "庫存", "存量", "哪些", "哪個", "缺貨", "快缺",
                                       "熱銷", "賣最", "排行", "快到期", "進出紀錄",
                                       "異動", "查一下", "查詢", "比較", "警示", "報表",
                                       "幾個", "幾件")
                # ── EN build（劇情批 r2 S3）：上面詞表**全是中文** → 英文查詢句
                #   一個都不中，'add item' → step1 後打 'whats running low'
                #   直接變成「Name recorded: "whats running low"」＝訪客會建出
                #   一個叫那句話的商品（展場最容易踩的一種）。
                _CREATE_QUERY_RE_EN = _re.compile(
                    r"\b(?:whats|what|which|how many|how much|show me|show|list|"
                    r"tell me|do we|does it|is there|are there|any\b|"
                    r"stock|stocks|inventory|running low|low stock|restock|"
                    r"expiring|expire|expiry|best sellers?|hot items?|"
                    r"compare|comparison|movements?|alerts?|report|reports|"
                    r"count|counts|left|remaining|total)\b", _re.I)
                if (any(w in user_text for w in _CREATE_QUERY_WORDS)
                        or (_is_mostly_english(user_text)
                            and _CREATE_QUERY_RE_EN.search(user_text))):
                    _cq_step = _ic_st.get("step", 1)
                    if _is_mostly_english(user_text):
                        _cq_msg = (f"You're in the middle of adding an item "
                                   f"(step {_cq_step}) — this won't be saved as "
                                   f'item data. Say "cancel" first if you want to '
                                   f"look something up.")
                    else:
                        _cq_msg = (f"你正在新增商品的流程中（第 {_cq_step} 步），"
                                   "這句不會被存成商品資料。要查別的請先說「取消」退出流程。")
                    log.info(f"[create-gate] 流程中的查詢句 → 提示退出: {user_text!r}")
                    for ch in _cq_msg:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "clarify", "summary": _cq_msg,
                        "data": {"question": _cq_msg, "options": [], "hint": ""}}})
                    continue
                import tools_v2 as _tv2_item_ws
                st2 = _ic_st
                kwargs2 = {**{k: v for k, v in st2.items() if k in ("step", "name", "category", "price", "safety", "stock_north", "stock_central", "stock_south")}, "raw_text": ""}
                if st2["step"] == 1: kwargs2["name"] = user_text
                elif st2["step"] == 2: kwargs2["category"] = user_text
                elif st2["step"] == 3:
                    raw_ps = user_text.replace("元", " ").replace("件", " ").replace("，", ",")
                    parts = [p.strip() for p in raw_ps.replace(" ", ",").split(",") if p.strip().lstrip("-").isdigit()]
                    if len(parts) >= 2: kwargs2["price"] = parts[0]; kwargs2["safety"] = parts[1]
                    elif len(parts) == 1: kwargs2["price"] = parts[0]
                    else: kwargs2["price"] = user_text
                elif st2["step"] == 4:
                    if "跳過" in user_text or any(_sk in user_text.lower() for _sk in ("skip", "none", "zero", "no stock", "leave it")):
                        kwargs2["stock_north"] = kwargs2["stock_central"] = kwargs2["stock_south"] = "0"
                    elif not any(kw in user_text for kw in ("北", "中", "南")):
                        parts = user_text.replace(",", " ").split()
                        nums = [p for p in parts if p.strip().lstrip("-").isdigit()]
                        if len(nums) == 3:
                            kwargs2["stock_north"], kwargs2["stock_central"], kwargs2["stock_south"] = nums[0], nums[1], nums[2]
                    else:
                        for part in user_text.replace("，", ",").split(","):
                            p = part.strip()
                            if "北" in p: kwargs2["stock_north"] = p.replace("北", "").strip()
                            elif "中" in p: kwargs2["stock_central"] = p.replace("中", "").strip()
                            elif "南" in p: kwargs2["stock_south"] = p.replace("南", "").strip()
                result = _tv2_item_ws.create_item_collect(**kwargs2)
                if result.get("view") == "item_confirm":
                    _item_create_state_ws.pop(vid, None)
                else:
                    d = result.get("data", {})
                    _new_st = {k: v for k, v in d.items() if k in ("step", "name", "category", "price", "safety", "stock_north", "stock_central", "stock_south")}
                    _new_st["active"] = True
                    _item_create_state_ws[vid] = _new_st
                for ch in result.get("summary", ""):
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get() * 1.5)
                await send({"type": "done", "result": result})
                continue

            # ── 新增商品 keyword 攔截（首次進入流程，per-vid）──
            _create_item_kws_ws2 = ("新增商品", "建立商品", "加一個商品", "新增一個", "加入商品", "增加商品", "新建商品",
                          # EN build：英文新增商品觸發詞（原表全中文 → 英文訪客
                          #   打 "add item" 完全進不了流程，還被守門員擋成 rejected）
                          "add item", "add a item", "add an item", "add new item",
                          "add a new item", "create item", "create a item",
                          "create an item", "create a new item", "new item",
                          "new product", "add product", "add a product",
                          "register item", "register a new item")
            if any(w in user_text for w in _create_item_kws_ws2):
                import tools_v2 as _tv2_ci2
                raw = user_text
                for kw in _create_item_kws_ws2: raw = raw.replace(kw, "").strip()
                # r75 危險級：「幫我新增商品」剝掉關鍵字剩「幫我」，曾被當 raw_text
                # 解析→靜默落到 step1 空名前進→建出商品「」。填充詞剝乾淨，
                # 剩空字串就老實從第一步開始問
                for _fw75 in ("幫我", "幫忙", "麻煩", "請", "我要", "我想", "想要",
                              "一下", "喔", "啊", "吧", "了"):
                    raw = raw.replace(_fw75, "").strip()
                result = _tv2_ci2.create_item_collect(step=1, raw_text=raw) if raw else _tv2_ci2.create_item_start()
                if result.get("view") != "item_confirm":
                    d = result.get("data", {})
                    _new_st = {k: v for k, v in d.items() if k in ("step", "name", "category", "price", "safety", "stock_north", "stock_central", "stock_south")}
                    _new_st["active"] = True
                    _item_create_state_ws[vid] = _new_st
                for ch in result.get("summary", ""):
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get() * 1.5)
                await send({"type": "done", "result": result})
                continue

            # ── 多商品列舉庫存（r28）：「衛生紙跟濕紙巾跟尿布的庫存」「啤酒和氣泡水
            #   和檸檬茶哪個多」曾只回第一/最後一個 ──
            if any(w in user_text for w in ("庫存", "各剩", "剩多少", "還有多少", "哪個多")):
                _mp_src = _re.sub(r"的?庫存|各剩多少|各剩幾|還有多少|剩多少|哪個多", "", user_text)
                _mp_parts = [p.strip() for p in _re.split(r"[跟和與、,，]", _mp_src) if p.strip()]
                if len(_mp_parts) >= 3:
                    import warehouse as _W_mp
                    _mp_names = []
                    for _p in _mp_parts[:4]:
                        _k_mp = _extract_sku_keyword(_p)
                        _m_mp = _W_mp.match_items(_k_mp) if _k_mp else []
                        if _m_mp and _m_mp[0].get("score", 0) >= 3:
                            _nm_mp = _m_mp[0]["item"]["name"]
                            if _nm_mp not in _mp_names:
                                _mp_names.append(_nm_mp)
                    if len(_mp_names) >= 3:
                        _mp_lines = []
                        for _nm_mp in _mp_names:
                            _r_mp = finance.execute("query_inventory", {"keyword": _nm_mp})
                            if _r_mp.get("ok") and _r_mp.get("summary"):
                                _mp_lines.append(_r_mp["summary"].split("\n")[0])
                        if len(_mp_lines) >= 3:
                            _mp_sum = "\n".join(_mp_lines)
                            log.info(f"[dispatch-ws] 多商品列舉庫存: {_mp_names}")
                            for ch in _mp_sum:
                                await send({"type": "token", "text": ch})
                                await asyncio.sleep(0.005)
                            await send({"type": "done", "result": {
                                "ok": True, "view": "inventory_single", "summary": _mp_sum,
                                "data": {"names": _mp_names}}})
                            continue

            # ── 功能描述直達：描述句 + 查詢語氣 → 不進 LLM 直接查庫存 ──
            # 描述改寫後交給 LLM 在 RPI5 有平台分歧（「橡膠清潔手套還有嗎」
            # 被抽成 category=清潔 跑去 clarify）。這是展示主打功能，不能賭
            # LLM 抽取——確定性直達。寫入/排程/報表/銷售語境不攔，走既有流程。
            _DESC_Q_CUES = ("還有", "還剩", "剩", "庫存", "多少", "幾",
                            "有沒有", "有嗎", "夠", "存量", "現貨",
                            # 「有賣…嗎」詢問是否有此商品（2026-07-09：「有賣煮咖啡
                            # 的嗎」缺 cue 掉進 LLM→咖啡豆）。用精準「賣」相關詞，不用
                            # 裸「嗎/呢」（會誤放「放音樂給我聽嗎」「今天喝咖啡嗎」閒聊）。
                            "有賣", "賣不賣", "有沒有賣", "賣嗎", "有沒有這", "有這個")
            _DESC_BLOCK = ("進貨", "出貨", "進了", "出了", "調撥", "調貨", "調到",
                           # 「補」不可單字擋（「補水的」=運動飲誤傷）→ 補貨語境詞
                           "補貨", "該補", "要補", "得補", "去補", "快補", "補一補",
                           "退貨", "退回", "改成", "設成", "設定", "刪", "新增",
                           # 「熱」不可單字擋（「裝熱湯」誤傷）→ 用熱銷語境詞
                           "熱銷", "熱賣", "最熱", "滯銷", "賣不動",
                           # r18：「防曬帽跟毛帽哪個賣得好」是兩商品銷量比較，
                           # 不可被描述直達搶成單品庫存
                           "哪個賣", "誰賣", "哪一個賣",
                           "動得快", "動最快", "誰動",   # r73：「它跟毛巾比誰動得快」
                           # r19：「藍牙喇叭上週跟這週哪週賣得多」——期間詞在場
                           # 是動態（銷量/進出）問句不是存量，不可直達回庫存
                           "上週", "上周", "這週", "本週", "哪週", "上個月", "昨天",
                           "這禮拜", "上禮拜",
                           # r21：「打果汁的賣得好嗎」銷況問句
                           "賣得好",
                           "比較", "警示", "排程", "報表", "採購", "對帳",
                           # 「買」「缺貨」移除（r16：擋掉正當查詢——「啤酒還有得買嗎」
                           # 「氣泡水缺貨了嗎」「上次買的那個防蚊的」都是問該商品庫存，
                           # 直達回庫存正是好答案；related 句改靠 _DESC_NONQUERY_INTENT
                           # 的「的人/的都/還會拿」等精準詞擋）
                           "到期", "過期", "保鮮期", "效期", "倒數", "多少錢", "價格",
                           # config/設定語境（「防蚊液安全庫存下修15」曾被劫走）——
                           # 安全庫存/水位一出現就絕非查存量，是設定操作或設定查詢
                           "安全庫存", "安全水位", "水位", "前置", "補貨天數",
                           "下修", "上修", "調高", "調低", "調成", "設成", "訂在",
                           "警戒", "提高", "降低", "拉高")
            # RCA/movement 意圖詞複用系統既有集合（比手列穩健、自動跟著演進）：
            # 「藍牙喇叭庫存少得莫名其妙」是 RCA、「濕紙巾這個月動了幾次」是
            # movement——描述命中+查詢語氣會誤劫，這裡尊重更明確的意圖（回歸抓到）。
            _desc_intent_block = (_has_rca_word(user_text)
                                  or any(w in user_text for w in _MOVEMENT_PROTECT_WORDS))
            # 查詢語氣放寬（2026-07-09，user 打錯字「查煮咖啡的庫純」查詢語氣詞被
            # 打壞掉出直達→亂配）：描述命中已是強意圖，只要句子不含「非查詢語境」
            # 的動作/閒聊詞（給我/聽/吃/唱/陪…），即使語氣詞有錯字也放行。這樣
            # 「煮咖啡的庫純」「洗衣服的有ㄇ」走直達，但「放音樂給我聽」仍被擋。
            _DESC_CHITCHAT = ("給我", "幫我聽", "聽歌", "來聽", "唱", "吃", "喝一杯",
                              "陪", "聊", "玩", "睡覺", "洗澡好", "覺得", "喜歡", "討厭",
                              "好不好", "要不要", "可不可以", "謝謝", "掰掰", "你好嗎")
            # 放寬的副作用守衛（RPI5 回歸抓到）：進貨/調貨/連帶/銷況句也「描述命中+
            # 無閒聊詞」，放寬會誤劫成查庫存。複用 module 級 _DESC_NONQUERY_INTENT
            # （涵蓋 15 輪收斂的進出貨動詞），放寬時一併排除，走原本正確路徑。
            # 另：句子含「兩個倉名」= 強調貨信號（查庫存句頂多一倉、調貨句必兩倉），
            # 一律不放寬——比枚舉調貨動詞（調/送/撥/過去…單字危險）穩健（2026-07-09）。
            _desc_two_wh = len({z for z in ("北", "中", "南")
                                if any(z + s in user_text for s in ("倉", "區"))}) >= 2
            # 「進/出/退 + 數量 + 量詞」= 進出貨句（結構判準，複用 C13b 模式，比枚舉
            # 單字動詞穩健）：「中倉進三箱衛生紙」的「進三箱」= 進+三+箱（2026-07-09）。
            _desc_mv_qty = _re.search(
                r"[進出退補來調撥挪移轉送到][一-鿿\s]{0,8}(?:[0-9]+\.[0-9]+|[0-9]+|[零一二兩三四五六七八九十百千萬億半]+)\s*"
                r"(?:件|個|條|支|台|箱|包|瓶|罐|組|雙|套|盒|對|頂|張|把|副|顆|粒|袋|桶|杯|塊|片|卷|捲|盞|打|手)",
                user_text) or _re.search(
                # r79 危險邊緣：「北倉出400衛生紙」沒帶量詞曾漏判——寫入動詞
                # 緊跟裸數字也是寫入句，不可被描述直達吞
                r"[進出退補調撥挪]\s*[0-9]{1,6}", user_text) or _re.search(
                r"[進出退補來調撥挪移轉送到]\s*(?:個)?\s*(?:十幾|十來|幾)\s*"
                r"(?:件|個|條|支|台|箱|包|瓶|罐|組|雙|套|盒|對|頂|張|把|打)", user_text)
            # ↑ r22b：「幾」從主結構移出後（「出門充電的還有幾個」曾被誤殺），
            #   「出十幾個滑鼠」「進幾箱」這類動詞緊鄰模糊量的寫入句用第二條抓——
            #   查詢句的「還有幾個」動詞不緊鄰不會中
            # NONQUERY 提升為「無條件排除」（r16 回歸抓到：「買露營燈的人購物車
            # 還有什麼」的「還有」命中 QCUE 直接走直達、繞過放寬分支的 NONQUERY
            # 檢查——related/進出貨/調貨意圖不管有沒有查詢語氣都不該直達）
            _desc_nonquery = any(w in user_text for w in _DESC_NONQUERY_INTENT)
            # _desc_mv_qty 升級為無條件排除（r17）：「北倉進50個滑鼠 然後跟我說
            # 現在總共幾個」的「幾」命中 QCUE 繞過結構守衛 → 進貨意圖被直達
            # 銷毀只回查詢。進/出/退+數量+量詞 = 寫入句，不管有沒有查詢語氣
            # 都不可直達，交給 C13b 開卡。
            # _desc_two_wh 也升級無條件排除（r19，同 r17 mv_qty 的構造漏洞）：
            # 「北倉跟中倉的濾掛咖啡各剩多少」的「多少」命中 QCUE 繞過兩倉檢查
            # → 直達只回北倉單倉。兩倉句交給 C13 丟單倉 filter 回三倉分佈。
            # r81 寫入契約：句首倉別+進出調動詞（「北倉進滑鼠」缺量）是寫入意圖，
            # 描述直達不可吃掉——讓路交 C13c 追問數量。查詢語尾（庫存/剩/幾件）
            # 不會中，「進去看看」無倉不會中
            _desc_bare_write81 = bool(_re.search(
                r"^[^。]{0,3}[北中南][區倉].{0,3}?[進出調撥挪][^0-9]", user_text)) \
                and not any(w in user_text for w in
                            ("庫存", "剩", "還有", "幾件", "多少", "價值", "嗎"))
            _desc_q_ok = (not _desc_nonquery
                          and not _desc_mv_qty
                          and not _desc_two_wh
                          and not _desc_bare_write81
                          and (any(w in user_text for w in _DESC_Q_CUES)
                               or not any(w in user_text for w in _DESC_CHITCHAT)))
            if (_desc_kw_ws
                    and _desc_q_ok
                    and not any(w in user_text for w in _DESC_BLOCK)
                    and not _desc_intent_block
                    # r48：排除式否定（衛生紙不要 其他都查/除了啤酒 什麼都好）讓給下方
                    # 專屬 gate——曾在這被直達搶走、偏偏回被排除的商品（倉名除外句不讓，
                    # 「除了北倉以外哪裡有貨」走上面 wh=all 既有路）
                    and not _re.search(r'(?:不要|除了|排除)(?!北|中|南).{0,6}?(?:其他|以外|什麼都|都查|都好|都要|通通|全列|列出來)', user_text)):
                # r25：「除了北倉以外哪裡有貨」曾被填 wh=north 只回被排除的倉——
                # 除外句一律回三倉分佈讓訪客自己看其他倉
                if any(w in user_text for w in ("除了", "以外", "之外")):
                    _desc_wh = "all"
                else:
                    _desc_wh = ("north" if any(w in user_text for w in ("北倉", "北區")) else
                                "central" if any(w in user_text for w in ("中倉", "中區")) else
                                "south" if any(w in user_text for w in ("南倉", "南區")) else "all")
                log.info(f"[dispatch-ws] 功能描述直達: {user_text!r} → "
                         f"query_inventory({_desc_kw_ws!r}, wh={_desc_wh})")
                result = finance.execute("query_inventory",
                                         {"keyword": _desc_kw_ws, "warehouse": _desc_wh})
                if result.get("ok") and result.get("summary"):
                    await push_display({"type": "trace", "stage": "llm_output",
                                         "raw": f"[descriptor] query_inventory({_desc_kw_ws!r})"})
                    for ch in result["summary"]:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": result})
                    continue
                # 查詢失敗 → 不攔，交給 LLM 流程

            # ── r48：排除式否定（「衛生紙不要 其他都查」「除了啤酒 什麼都好」）曾
            #   偏偏回被排除的那個商品＝語意反轉（r16 家族）。排除式總覽不支援 →
            #   誠實說明＋給路。──
            _excl_m = _re.search(r'(?:不要|除了|排除)\s*(.{2,6}?)\s*(?:，|,| )?(?:其他|以外|什麼都|都查|都好|都要|通通|全列|列出來)', user_text)
            if _excl_m:
                import warehouse as _W_ex
                _ex_hit = _W_ex.match_items(_extract_sku_keyword(_excl_m.group(1)) or _excl_m.group(1))
                if _ex_hit and _ex_hit[0].get("score", 0) >= 5:
                    _ex_msg = (f"「排除{_ex_hit[0]['item']['name']}看其他全部」這種總覽還不支援喔——"
                               "可以看「全部商品庫存」或直接指定想查的商品。")
                    log.info(f"[dispatch-ws] 排除式否定 → clarify: {user_text!r}")
                    for ch in _ex_msg:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "clarify", "summary": _ex_msg,
                        "data": {"question": _ex_msg, "options": ["全部商品庫存", "商品清單"], "hint": ""}}})
                    continue

            # ── 否定排行（r16：「我不要排行榜我要庫存」曾偏偏回排行榜——最挑釁
            # 的答案）：否定詞+排行 → 尊重否定，回庫存概覽 ──
            if (any(w in user_text for w in ("不要排行", "不是排行", "不要熱銷", "不要榜"))
                    and any(w in user_text for w in ("庫存", "存量", "要庫存"))):
                log.info(f"[dispatch-ws] 否定排行 → 庫存概覽: {user_text!r}")
                result = finance.execute("query_inventory", {})
                if result.get("ok") and result.get("summary"):
                    for ch in result["summary"]:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": result})
                    continue

            # ── r43：庫存最少/墊底排行 → 缺貨清單（「庫存最少的前三個」曾回 60 項概覽）──
            if (("庫存最少" in user_text or "存量最少" in user_text)
                    or ("最少" in user_text and _re.search(r"前[一二三四五12345]", user_text))):
                log.info(f"[dispatch-ws] 庫存最少 → list_low_stock: {user_text!r}")
                result = finance.execute("list_low_stock", {})
                if result.get("ok") and result.get("summary"):
                    for ch in result["summary"]:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": result})
                    continue

            # ── r45 比較家族補洞 ──────────────────────────────
            # A. 期間比較（今天比昨天/這週比上週）：compare_periods 只支援月級 → 誠實說明
            # 帶真商品名的讓路（「藍牙喇叭上週跟這週哪週賣得多」C4-prod 有現成處理，守衛句）
            if (_re.search(r'(今天|昨天|前天|大前天|這週|本週|上週|這周|上周|早上|上午|中午|下午|晚上|傍晚)(比|跟|和)(今天|昨天|前天|大前天|這週|本週|上週|這周|上周|早上|上午|中午|下午|晚上|傍晚)', user_text)
                    or "這週比上週" in user_text or "今天比昨天" in user_text) \
                    and not _text_has_item_name(user_text):
                _pc_msg = ("期間對比目前支援「這個月跟上個月」的變化（可以問「這個月跟上個月"
                           "差多少」）；單日／單週的直接對比還不支援喔。")
                log.info(f"[dispatch-ws] 期間比較誠實說明: {user_text!r}")
                for ch in _pc_msg:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(_TK_DELAY.get())
                await send({"type": "done", "result": {
                    "ok": True, "view": "clarify", "summary": _pc_msg,
                    "data": {"question": _pc_msg, "options": [], "hint": ""}}})
                continue
            # B. 兩倉單品比較（「北倉比南倉多幾件衛生紙」「北倉南倉誰的衛生紙多」曾回全倉單品）
            _w2m = _re.search(r'(北|中|南)[區]?倉?(?:比|跟|和)?(北|中|南)[區]?倉?(?:誰的|哪邊的?|比較|多幾件|少幾件|誰比較多|誰比較少|的)?(.{2,8}?)(?:多|少|比較多|比較少|哪邊多|誰多)?$', user_text) \
                if _re.search(r'(北|中|南)[區]?倉.{0,4}(北|中|南)[區]?倉', user_text) else None
            if _w2m and any(w in user_text for w in ("多", "少", "誰", "哪邊", "比")):
                import warehouse as _W_w2
                _w2_kw = _extract_sku_keyword(user_text)
                _w2_m = _W_w2.match_items(_w2_kw) if _w2_kw else []
                if _w2_m and _w2_m[0].get("score", 0) >= 5:
                    _w2_name = _w2_m[0]["item"]["name"]
                    _WH_E = {"北": "north", "中": "central", "南": "south"}
                    _WH_L = {"北": "北區倉", "中": "中區倉", "南": "南區倉"}
                    _wa, _wb = _w2m.group(1), _w2m.group(2)
                    _ra2 = finance.execute("query_inventory", {"keyword": _w2_name, "warehouse": _WH_E[_wa]})
                    _rb2 = finance.execute("query_inventory", {"keyword": _w2_name, "warehouse": _WH_E[_wb]})
                    _qa2 = (_ra2.get("data") or {}).get("total_qty", 0)
                    _qb2 = (_rb2.get("data") or {}).get("total_qty", 0)
                    _diff2 = abs(_qa2 - _qb2)
                    _winwh = _WH_L[_wa] if _qa2 >= _qb2 else _WH_L[_wb]
                    _w2_sum = (f"「{_w2_name}」{_WH_L[_wa]} {_qa2:,} 件、{_WH_L[_wb]} {_qb2:,} 件"
                               f"——{_winwh}多 {_diff2:,} 件。")
                    log.info(f"[dispatch-ws] 兩倉單品比較: {_w2_name} {_wa}{_qa2}/{_wb}{_qb2}")
                    for ch in _w2_sum:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "inventory_single", "summary": _w2_sum, "data": {}}})
                    continue
            # C. 兩商品差額比較（「衛生紙比濕紙巾多多少」「啤酒比氣泡水少幾件」曾回單品）
            _d2m = _re.search(r'^(.{2,10}?)比(.{2,10}?)(?:多|少)(?:多少|幾件|幾個|幾)', user_text)
            if _d2m:
                import warehouse as _W_d2
                _da = _W_d2.match_items(_extract_sku_keyword(_d2m.group(1)) or _d2m.group(1))
                _db = _W_d2.match_items(_extract_sku_keyword(_d2m.group(2)) or _d2m.group(2))
                if (_da and _da[0].get("score", 0) >= 5 and _db and _db[0].get("score", 0) >= 5
                        and _da[0]["item"]["sku_id"] != _db[0]["item"]["sku_id"]):
                    _na2, _nb2 = _da[0]["item"]["name"], _db[0]["item"]["name"]
                    _ia = finance.execute("query_inventory", {"keyword": _na2})
                    _ib = finance.execute("query_inventory", {"keyword": _nb2})
                    _ta = (_ia.get("data") or {}).get("total_qty") or sum(r.get("qty", 0) for r in (_ia.get("data") or {}).get("rows", []))
                    _tb = (_ib.get("data") or {}).get("total_qty") or sum(r.get("qty", 0) for r in (_ib.get("data") or {}).get("rows", []))
                    _dw = _na2 if _ta >= _tb else _nb2
                    _d2_sum = (f"「{_na2}」三倉共 {_ta:,} 件、「{_nb2}」三倉共 {_tb:,} 件"
                               f"——「{_dw}」多 {abs(_ta - _tb):,} 件。")
                    log.info(f"[dispatch-ws] 兩商品差額比較: {_na2}{_ta}/{_nb2}{_tb}")
                    for ch in _d2_sum:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "inventory_single", "summary": _d2_sum, "data": {}}})
                    continue

            # ── 兩商品銷量比較（r18）：「防曬帽跟毛帽哪個賣得好」「e01跟e02哪個
            #   賣得好」曾回全類別熱銷 TOP10（兩商品都不在榜=答非所問）──
            _pvs = _re.search(r'^(.{1,12}?)(?:比)?[跟和與](.{1,12}?)(?:比)?(?:哪個|哪一個|誰)(?:比較)?'
                              r'(?:好賣|賣得比較好|賣得好|賣得快|賣得動|賣比較好|賣最好|賣最差|熱銷|暢銷'
                              r'|動得快|動最快|走得快)', user_text)   # r73：「它跟毛巾比誰動得快」
            if _pvs:
                import warehouse as _W_pvs
                _pa_kw = _extract_sku_keyword(_pvs.group(1)) or _pvs.group(1).strip()
                _pb_kw = _extract_sku_keyword(_pvs.group(2)) or _pvs.group(2).strip()
                _pa = _W_pvs.match_items(_pa_kw) if _pa_kw else []
                _pb = _W_pvs.match_items(_pb_kw) if _pb_kw else []
                # r57：一側是代詞（「它跟不沾鍋哪個賣得好」）→ 用 context 商品接地
                if ((not _pa or _pa[0].get("score", 0) < 3)
                        and any(p in _pvs.group(1) for p in ("它", "牠", "這個", "那個"))
                        and _ctx_for(vid).get("last_sku")):
                    _pa = _W_pvs.match_items(_ctx_for(vid)["last_sku"])
                if (_pa and _pa[0].get("score", 0) >= 3 and _pb and _pb[0].get("score", 0) >= 3
                        and _pa[0]["item"]["sku_id"] != _pb[0]["item"]["sku_id"]):
                    _na, _nb = _pa[0]["item"]["name"], _pb[0]["item"]["name"]
                    _ra = finance.execute("query_movement", {"keyword": _na, "period": "this_month", "direction": "out"})
                    _rb = finance.execute("query_movement", {"keyword": _nb, "period": "this_month", "direction": "out"})
                    _qa = (_ra.get("data") or {}).get("out_qty", 0)
                    _qb = (_rb.get("data") or {}).get("out_qty", 0)
                    _win = _na if _qa >= _qb else _nb
                    _pvs_sum = (f"本月「{_na}」出貨 {_qa:,} 件、「{_nb}」出貨 {_qb:,} 件，"
                                f"「{_win}」賣得比較好。")
                    log.info(f"[dispatch-ws] 兩商品銷量比較: {_na} vs {_nb} → {_qa}/{_qb}")
                    for ch in _pvs_sum:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "movement", "summary": _pvs_sum,
                        "data": {"a": {"name": _na, "out_qty": _qa},
                                 "b": {"name": _nb, "out_qty": _qb}, "period": "this_month"}}})
                    continue

            # ── 兩商品庫存比較（r20）：「運動毛巾跟登山水壺各剩多少」曾只回
            #   單品。倉名開頭句（北倉跟中倉…）比不到商品自然跳過。──
            _pvi = _re.search(r'^(.{1,12}?)(?:[跟和與]|還有)(.{1,12}?)(?:各剩多少|各多少|各有多少'
                              r'|各還[有剩]|庫存各|各庫存|各剩幾'
                              # r21：「露營馬克杯跟露營燈哪個庫存多」曾只回單品
                              r'|哪個庫存多|哪個庫存比較多|誰的?庫存多|哪個比較多|哪個多'
                              # r25：「瑜珈墊跟啞鈴的庫存比一下」曾被 Pre-C-Cmp2 只抽第一個商品
                              r'|的?庫存比一下|的?庫存比一比|的?庫存比較一下|庫存比看看'
                              # r43：「防蚊液和蚊香液比較一下」曾只回單品；「A還有B誰比較少」
                              # 分隔詞「還有」＋少方向 也漏
                              r'|誰比較少|哪個比較少|誰的?庫存少|誰少|誰多'
                              r'|的?比較一下|比一比$|比比看)', user_text)
            if _pvi:
                import warehouse as _W_pvi

                def _pv_resolve67(_kw):
                    """r67：比較句側邊解析——直接比不到就走通稱表（襪子→保暖襪）。"""
                    _m = _W_pvi.match_items(_kw) if _kw else []
                    if not (_m and _m[0].get("score", 0) >= 3):
                        _g = getattr(_W_pvi, "_GENERIC_QUERY_FALLBACK", {}).get(_kw)
                        if _g and len(_g) == 1:
                            _m = _W_pvi.match_items(_g[0])
                    return _m

                _pia_kw = _extract_sku_keyword(_pvi.group(1)) or _pvi.group(1).strip()
                _pib_kw = _extract_sku_keyword(_pvi.group(2)) or _pvi.group(2).strip()
                _pia = _pv_resolve67(_pia_kw)
                _pib = _pv_resolve67(_pib_kw)
                # r78：一邊查無（「帳篷跟睡袋各剩多少」的睡袋不存在）——答有的那個
                # ＋誠實說找不到另一個（曾整句掉 LLM 只回單品、隻字不提睡袋）
                _pia_ok78 = bool(_pia and _pia[0].get("score", 0) >= 3)
                _pib_ok78 = bool(_pib and _pib[0].get("score", 0) >= 3)
                if _pia_ok78 != _pib_ok78:
                    _hit78 = _pia[0] if _pia_ok78 else _pib[0]
                    _miss78 = (_pib_kw if _pia_ok78 else _pia_kw).strip(" 的現在還")
                    _rh78 = finance.execute("query_inventory",
                                            {"keyword": _hit78["item"]["name"]})
                    if _rh78.get("ok") and _rh78.get("summary"):
                        _pv78_sum = (_rh78["summary"]
                                     + f"\n（另外「{_miss78}」找不到這個商品喔，"
                                       "可以說「商品清單」看全部）")
                        log.info(f"[dispatch-ws] 兩商品比較一邊查無: {_miss78!r}")
                        for ch in _pv78_sum:
                            await send({"type": "token", "text": ch})
                            await asyncio.sleep(_TK_DELAY.get())
                        await send({"type": "done", "result": {
                            "ok": True, "view": "inventory_single",
                            "summary": _pv78_sum, "data": {}}})
                        continue
                if (_pia and _pia[0].get("score", 0) >= 3 and _pib and _pib[0].get("score", 0) >= 3
                        and _pia[0]["item"]["sku_id"] != _pib[0]["item"]["sku_id"]):
                    _ria = finance.execute("query_inventory", {"keyword": _pia[0]["item"]["name"]})
                    _rib = finance.execute("query_inventory", {"keyword": _pib[0]["item"]["name"]})
                    _pvi_sum = (_ria.get("summary", "") + chr(10) + _rib.get("summary", ""))
                    log.info(f"[dispatch-ws] 兩商品庫存比較: {_pia[0]['item']['name']} / {_pib[0]['item']['name']}")
                    for ch in _pvi_sum:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "inventory_single", "summary": _pvi_sum,
                        "data": {}}})
                    continue

            # ── r72：「前三名各剩多少」——逐名列庫存（曾回「沒有『前三名各』」）──
            # r78：「前三名出貨加起來多少件」——名次加總形
            _tns78 = _re.search(r"前\s*([一二三四五12345])\s*名[^。]{0,4}"
                                r"(加起來|總共|合計)[^。]{0,4}(多少|幾件)", user_text)
            if _tns78 and (_ctx_for(vid).get("last_hot_period")
                           or _ctx_for(vid).get("last_func") == "list_hot_items"
                           or any(w in user_text for w in ("熱銷", "排行", "出貨"))):
                _tns_n = ({"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
                          .get(_tns78.group(1)) or int(_tns78.group(1)))
                _tns_p = _ctx_for(vid).get("last_hot_period") or "this_week"
                _tns_hot = finance.execute("list_hot_items",
                                           {"rank_type": "hot", "period": _tns_p})
                _tns_rank = (_tns_hot.get("data") or {}).get("rankings") or []
                if _tns_rank:
                    _tns_rows = _tns_rank[:_tns_n]
                    _tns_sum_qty = sum(r.get("out_qty", 0) for r in _tns_rows)
                    _tns_msg = ((f"{'本月' if _tns_p == 'this_month' else '本週'}"
                                 f"前{_tns_n}名（"
                                 + "、".join(r["name"] for r in _tns_rows)
                                 + f"）出貨合計 {_tns_sum_qty:,} 件。"))
                    log.info(f"[dispatch-ws] 前{_tns_n}名出貨加總: {_tns_sum_qty}")
                    for ch in _tns_msg:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "inventory_single", "summary": _tns_msg,
                        "data": {"total_out": _tns_sum_qty}}})
                    _ctx_for(vid)["last_func"] = "list_hot_items"
                    continue

            _topn72 = _re.search(r"前\s*([一二三四五12345])\s*名各?(剩多少|庫存|各剩|還剩)",
                                 user_text)
            if _topn72 and (_ctx_for(vid).get("last_hot_period")
                            or any(w in user_text for w in ("熱銷", "排行"))):
                _tn = ({"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}.get(_topn72.group(1))
                       or int(_topn72.group(1)))
                _tn_args = {"rank_type": "hot",
                            "period": _ctx_for(vid).get("last_hot_period") or "this_week"}
                if _ctx_for(vid).get("last_hot_cat"):
                    _tn_args["category"] = _ctx_for(vid)["last_hot_cat"]
                _tn_hot = finance.execute("list_hot_items", _tn_args)
                _tn_rank = (_tn_hot.get("data") or {}).get("rankings") or []
                if _tn_rank:
                    _tn_lines = []
                    for _ti, _tr in enumerate(_tn_rank[:_tn], 1):
                        _tri = finance.execute("query_inventory", {"keyword": _tr["name"]})
                        _tn_lines.append(f"第{_ti}名 {(_tri.get('summary') or '').strip()}")
                    _tn_sum = "\n".join(_tn_lines)
                    log.info(f"[dispatch-ws] 前{_tn}名各庫存")
                    for ch in _tn_sum:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "inventory_single", "summary": _tn_sum,
                        "data": {}}})
                    _ctx_for(vid)["last_func"] = "list_hot_items"
                    continue

            # ── 複合句攔截：「賣最好/賣最差的還剩多少」= 排行 Top1 + 它的庫存 ──
            # C4 會把「賣最好/滯銷」強轉 list_hot_items 回排行榜，但這句訪客
            # 要的是那個商品的庫存數字（RPI5 實測 2026-07-06），進 LLM 前先攔。
            _bs_hot_words = ("賣最好", "賣得最好", "最好賣", "賣最快", "賣得最快",
                             "最熱銷", "最暢銷", "熱銷第一", "銷量第一", "賣第一")
            _bs_slow_words = ("賣最差", "賣得最差", "賣最爛", "賣最慢", "最難賣",
                              "最不好賣", "最滯銷", "滯銷", "賣不動", "賣不掉")
            # r16 補「有幾個/有多少」（「熱銷第一名在南倉有幾個」曾漏攔回排行榜）
            _bs_stock_words = ("剩多少", "還剩", "剩幾", "庫存", "還有多少", "還有幾",
                               "存量", "有幾個", "有多少", "有幾件",
                               "夠嗎", "夠不夠")   # r72：「第一名北倉夠嗎」
            _bs_rank_type = ("slow" if any(w in user_text for w in _bs_slow_words)
                             else "hot" if any(w in user_text for w in _bs_hot_words)
                             else None)
            # r43：「排行榜第二名剩多少」——第N名+庫存語沒有賣最好字眼，曾只回排行榜
            # r55 收官批：剛看完排行榜再問「第三名剩多少」（連排行字眼都沒有）也要接
            _bs_rankn_m = _re.search(r'第\s*([一二三四五六七八九十0-9]+)\s*名', user_text)
            if (_bs_rankn_m and not _bs_rank_type
                    and (any(w in user_text for w in ("排行", "榜", "熱銷", "暢銷"))
                         or _ctx_for(vid).get("last_func") == "list_hot_items"
                         # r59：排行後又追問了商品（last_func 被蓋），只要這段對話
                         # 看過排行榜（last_hot_period 有值）「第七名剩幾個」就接
                         or _ctx_for(vid).get("last_hot_period"))):
                _bs_rank_type = "hot"
            # r57：「第五名是什麼」——排行後問名次身分（沒有庫存語）也要接
            _bs_idwords = ("是什麼", "是哪個", "是啥", "叫什麼", "是誰")
            _bs_want_id = bool(_bs_rankn_m) and any(w in user_text for w in _bs_idwords)
            # r77：「算了 看熱銷第二名」——純觀看形（看/查+第N名）也是身分追問
            if _bs_rankn_m and not _bs_want_id and _re.fullmatch(
                    r"[算了那就 ]{0,5}(看|查|給我看?)?(熱銷|排行榜?)?第\s*[一二三四五12345]\s*名的?",
                    user_text.strip().strip("!！?？。 ")):
                _bs_want_id = True
            # r64：「第二名多少錢」——排行後問名次價格
            _bs_want_pr = bool(_bs_rankn_m) and any(
                w in user_text for w in ("多少錢", "單價", "什麼價", "售價"))
            # r76：「第三名上週賣幾件」——排行後問名次×期間出量，曾掉全店統計
            _bs_want_mv = bool(_bs_rankn_m) and any(
                w in user_text for w in ("賣幾件", "賣了幾", "賣多少件", "出幾件",
                                          "出了幾", "出多少", "賣幾個",
                                          # rewrite 會把「賣幾個」改成「出貨多少」
                                          "出貨多少", "出貨幾件"))
            if _bs_rank_type and (any(w in user_text for w in _bs_stock_words)
                                  or _bs_want_id or _bs_want_pr or _bs_want_mv):
                # 期間：句內有講就照句子，沒講沿用上一輪排行榜的期間（r55）
                if "月" in user_text:
                    _bs_period = "this_month"
                elif any(w in user_text for w in ("週", "禮拜", "星期")):
                    _bs_period = "this_week"
                else:
                    _bs_period = _ctx_for(vid).get("last_hot_period") or "this_week"
                # r64：沿用上一輪排行榜的類別範圍（「廚具類熱銷」→「第二名多少錢」
                # 曾用全類別榜解析到錯的商品）
                _bs_args = {"rank_type": _bs_rank_type, "period": _bs_period}
                if _ctx_for(vid).get("last_hot_cat"):
                    _bs_args["category"] = _ctx_for(vid)["last_hot_cat"]
                _bs_hot = finance.execute("list_hot_items", _bs_args)
                _bs_rank = (_bs_hot.get("data") or {}).get("rankings") or []
                _bs_done = False
                if _bs_rank:
                    _bs_idx = 0
                    if _bs_rankn_m:
                        _ZHN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                                "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                        _bs_n_raw = _bs_rankn_m.group(1)
                        _bs_n = _ZHN.get(_bs_n_raw) or (int(_bs_n_raw) if _bs_n_raw.isdigit() else 1)
                        _bs_idx = min(max(_bs_n - 1, 0), len(_bs_rank) - 1)
                    _bs_name = _bs_rank[_bs_idx]["name"]
                    _bs_rlabel = (f"第{_bs_idx + 1}名" if _bs_idx else
                                  ("賣最好" if _bs_rank_type == "hot" else "賣最差"))
                    # 帶倉別（「在南倉有幾個」要回南倉的數字，r16）
                    _bs_wh = ("north" if any(w in user_text for w in ("北倉", "北區")) else
                              "central" if any(w in user_text for w in ("中倉", "中區")) else
                              "south" if any(w in user_text for w in ("南倉", "南區")) else "all")
                    # r57：純身分追問（「第五名是什麼」）→ 直接報名字+出量，不帶庫存
                    if _bs_want_id and not any(w in user_text for w in _bs_stock_words):
                        _bs_plabel_id = "本月" if _bs_period == "this_month" else "本週"
                        _bs_id_sum = (f"{_bs_plabel_id}{_bs_rlabel}的是"
                                      f"「{_bs_name}」（出 {_bs_rank[_bs_idx]['out_qty']:,} 件）。")
                        log.info(f"[dispatch-ws] 排行身分追問: 第{_bs_idx + 1}名 → {_bs_name}")
                        for ch in _bs_id_sum:
                            await send({"type": "token", "text": ch})
                            await asyncio.sleep(_TK_DELAY.get())
                        await send({"type": "done", "result": {
                            "ok": True, "view": "inventory_single", "summary": _bs_id_sum,
                            "data": {"name": _bs_name}}})
                        # r58：身分追問後排行 context 要保留——「第四名是啥」→
                        # 「第四名剩多少」曾因 last_func 被蓋成 query_inventory 而斷鏈
                        _ctx_for(vid)["last_func"] = "list_hot_items"
                        _bs_done = True
                        continue
                    # r76：名次×期間出量追問（「第三名上週賣幾件」）→ 查該品進出
                    if _bs_want_mv:
                        _mv_p76 = ("last_week" if "上週" in user_text or "上禮拜" in user_text
                                   else "this_month" if "月" in user_text
                                   else "yesterday" if "昨天" in user_text
                                   else "today" if "今天" in user_text
                                   else "this_week")
                        result = finance.execute("query_movement",
                                                 {"keyword": _bs_name, "period": _mv_p76,
                                                  "direction": "out"})
                        if result.get("ok") and result.get("summary"):
                            result["summary"] = (f"{_bs_rlabel}是「{_bs_name}」。"
                                                 + result["summary"])
                            log.info(f"[dispatch-ws] 名次出量追問: {_bs_name} {_mv_p76}")
                            for ch in result["summary"]:
                                await send({"type": "token", "text": ch})
                                await asyncio.sleep(_TK_DELAY.get())
                            await send({"type": "done", "result": result})
                            _ctx_for(vid)["last_func"] = "list_hot_items"
                            _bs_done = True
                            continue
                    # r64：名次價格追問（「第二名多少錢」）→ 直接報單價
                    if _bs_want_pr and not any(w in user_text for w in _bs_stock_words):
                        import warehouse as _W_bp
                        _bp_m = _W_bp.match_items(_bs_name)
                        _bp_price = (_bp_m[0]["item"]["unit_price"] if _bp_m else 0)
                        _bs_pr_sum = (f"{'本月' if _bs_period == 'this_month' else '本週'}"
                                      f"{_bs_rlabel}是「{_bs_name}」，單價 NT$ {_bp_price:,}。")
                        log.info(f"[dispatch-ws] 排行價格追問: {_bs_name} → {_bp_price}")
                        for ch in _bs_pr_sum:
                            await send({"type": "token", "text": ch})
                            await asyncio.sleep(_TK_DELAY.get())
                        await send({"type": "done", "result": {
                            "ok": True, "view": "inventory_single", "summary": _bs_pr_sum,
                            "data": {"name": _bs_name, "unit_price": _bp_price}}})
                        _ctx_for(vid)["last_func"] = "list_hot_items"
                        _bs_done = True
                        continue
                    log.info(f"[dispatch-ws] 複合句攔截: {user_text!r} → "
                             f"{_bs_rlabel}Top1「{_bs_name}」庫存 wh={_bs_wh}")
                    result = finance.execute("query_inventory",
                                             {"keyword": _bs_name, "warehouse": _bs_wh})
                    if result.get("ok") and result.get("summary"):
                        _bs_plabel = "本月" if _bs_period == "this_month" else "本週"
                        _bs_qty_label = ("出" if _bs_rank_type == "hot" else "只出")
                        result["summary"] = (f"{_bs_plabel}{_bs_rlabel}的是「{_bs_name}」"
                                             f"（{_bs_qty_label} {_bs_rank[_bs_idx]['out_qty']:,} 件）。"
                                             + result["summary"])
                        for ch in result["summary"]:
                            await send({"type": "token", "text": ch})
                            await asyncio.sleep(_TK_DELAY.get())
                        await send({"type": "done", "result": result})
                        _bs_done = True
                if _bs_done:
                    continue
                # 沒出貨記錄/查詢失敗 → 不攔，交給既有流程

            # ── intent_clf 主要路由（跟 HTTP 版 api_query 同一套邏輯，2026-07-02
            #   補齊：WS 端原本完全沒有這層，導致 query_movement 這類「LLM 容易
            #   誤抽時間/動作詞當 keyword」的句子路由準確率明顯偏低，見
            #   memory warehouse_v2_project 的「WS 缺 intent_clf」記錄）──
            _clf_func_ws = None
            _clf_conf_ws = 0.0
            _clf_t0 = __import__("time").perf_counter()
            try:
                _clf_func_ws, _clf_conf_ws = intent_clf.predict(user_text)
            except Exception:
                pass
            _clf_ms = round((__import__("time").perf_counter() - _clf_t0) * 1000)
            _pre_kw_ws = _extract_sku_keyword(user_text)
            _clf_skip_llm_ws = False
            # ── r81 寫入契約閘門：寫入意圖句絕不被 clf 降級成查詢 ──
            # 「北倉進滑鼠」（缺量）曾被 clf 判 query_inventory 直答庫存＝寫入意圖
            # 被吞。特徵：句首倉別 + 進/出/調動詞 + 非查詢語尾 → 強制走校正層
            # （C13b/C13c 判進出貨、缺量追問），不讓 clf skip。
            # 只攔「動詞緊接商品/數量」的真寫入意圖：句首倉別+進出調動詞後
            # 緊跟商品名或數字。「把北倉的傘都調去南倉」的傘查無、且動詞後不是
            # 商品/數字 → 不攔，讓它回 r80 的既有路徑（避免 LLM 亂聯想成拖把）
            # r83：動詞加「補」（「北倉補50 電動牙刷」曾漏判成查詢 clarify）
            _wc_m81 = _re.search(r"^(?:把|將|幫我)?[北中南][區倉]的?"
                                 r"([進出調撥挪補][^0-9]{0,6}?)(?=[0-9]|$)", user_text)
            _wc_write81 = False
            if (_wc_m81 and not any(w in user_text for w in
                                    ("庫存", "剩", "還有", "幾件", "多少", "價值",
                                     "嗎", "哪", "什麼", "比較", "紀錄", "記錄",
                                     "統計", "警示", "到期"))):
                # 動詞後的殘詞若比得到真商品、或句帶明確數字 = 真寫入意圖
                import warehouse as _W_wc81
                _wc_tail81 = _re.sub(r"^[進出調撥挪補]", "", _wc_m81.group(1)).strip("去到過來的都")
                _wc_has_prod = bool(_wc_tail81 and _W_wc81.match_items(_wc_tail81)
                                    and _W_wc81.match_items(_wc_tail81)[0].get("score", 0) >= 3)
                _wc_has_num = bool(_re.search(r"[進出調撥挪補]\s*[0-9]", user_text))
                _wc_write81 = _wc_has_prod or _wc_has_num
            if (_wc_write81 and _clf_func_ws in
                    ("query_inventory", "query_movement", "list_hot_items", None)):
                log.info(f"[write-gate-r81] 寫入意圖不降級 → 交校正層: {user_text!r}")
                _clf_func_ws = None   # 清掉 clf 判斷，強制走 LLM+校正

            # ── EN build：英文 Agent 管理句直達（排程/警示清單、可讀檔案）。
            #   FastText 語料裡沒有這些英文句 → clf 一律吐 query_inventory
            #   conf=1.00 並 skip LLM，全店概覽答非所問（'show my schedules'）。
            #   詞組夠獨特（schedules/alert rules/what files），直接指定工具。
            _en_admin = None
            _en_admin_hard = False
            if _is_mostly_english(user_text):
                _ul_adm = user_text.lower()
                if _re.search(r"\b(?:my|the|any|current|existing|active|which|what)\b"
                              r"[^.]{0,20}\bschedules?\b|\bscheduled\s+(?:jobs?|tasks?)\b",
                              _ul_adm) and not _re.search(
                                  r"\b(?:create|set|add|new|make|every)\b", _ul_adm):
                    _en_admin = "list_schedules"
                elif _re.search(r"\b(?:my|the|any|current|existing|active|which|what)\b"
                                r"[^.]{0,20}\balert\s+rules?\b|\b(?:my|show|list|view)\s+alerts\b",
                                _ul_adm) and not _re.search(
                                    r"\b(?:create|set|add|new|notify me|remind me|"
                                    r"alert me|below|under|drops?)\b", _ul_adm):
                    _en_admin = "list_alerts"
                elif _re.search(r"\bwhat\s+files?\b|\bwhich\s+files?\b|"
                                r"\b(?:list|show)\s+(?:the\s+)?files?\b|"
                                r"\bwhat\s+(?:data\s+)?can\s+you\s+read\b", _ul_adm):
                    # ⚠️ files? 的 `?` 是 2026-08-02 補的：真人錄音第 74 句
                    #   whisper 把 files 聽成 file → 漏出去被 clf 判 query_inventory
                    #   conf=1.00 skip LLM（clf 語料 files 6 句、file **0 句**）。
                    #   英文字尾 s 是 ASR 最容易吞的音，單複數一律要一起收。
                    _en_admin = "list_files"
                elif _re.search(r"\b(?:top|best)\s+(?:seller|sellers|selling)\b|"
                                r"\bbest[\s-]?sellers?\b|\bhot\s+items?\b",
                                _ul_adm):
                    # 2026-08-02：`show me the top seller`（**單數**）被 clf
                    #   判 query_inventory(0.83) + skip LLM → 把 "top seller"
                    #   當商品名查 → clarify「查無此商品」。
                    #   複數形正常（hot_items）。單複數一起收。
                    _en_admin = "list_hot_items"
                elif _re.search(r"\bwhat\s+scripts?\b|\bwhich\s+scripts?\b|"
                                r"\b(?:list|show)\s+(?:the\s+)?scripts?\b|"
                                r"\bwhat\s+scripts?\s+can\s+you\s+run\b", _ul_adm) \
                        and not _re.search(r"\b(?:run|execute|start)\s+(?:the\s+)?"
                                           r"(?:month|stocktake|export|report)", _ul_adm):
                    # 2026-08-02 新增：scripts **原本完全沒有規則**（第 75 句）
                    #   `what script can you run` 被判成要**執行**腳本
                    #   → run_script({'script_name': 'run_script_one'}) 白名單錯誤。
                    #   「問有哪些」是列表意圖，排除明確的執行句（run the month end…）。
                    _en_admin = "list_files"
                # 'expiry alerts' / 'expiry warnings'：clf 判 query_inventory
                #   conf=1.00 skip LLM → 根本進不到 C7 的到期規則
                elif _re.search(r"\bexpir(?:y|ing|ation)\s+(?:alerts?|warnings?|list)\b",
                                _ul_adm) and not _re.search(
                                    r"\b(?:alert|notify|warn|remind)\s+me\b|\bwhen\b|"
                                    r"\bbelow\b|\bunder\b|\bdrops?\b", _ul_adm):
                    _en_admin = "list_expiring_items"
                # r14+2（#21）：裸 'transfers?' / 'movements?' 功能詞單獨句
                #   ——曾被 alias fuzzy 配成 trainers→Running Shoes。
                #   單獨一個功能名詞＝想看進出/調撥紀錄 → movement 總覽。
                elif (_re.fullmatch(r"\s*(?:transfers?|movements?)"
                                    r"(?:\s+(?:log|history|records?))?"
                                    r"(?:\s+please)?\s*[?.!]*\s*", _ul_adm)
                      # r18 #28：'would it be possible to see the movement
                      #   log' clf 誤判 run_script(0.97) 被 guard 拒——句含
                      #   紀錄片語且無寫入數字＝查 movement
                      or (_re.search(r"\b(?:movement|transfer)s?\s+"
                                     r"(?:log|history|records?)\b", _ul_adm)
                          and not _re.search(r"\d", _ul_adm))):
                    # r16 #27/#28：'transfer log'/'movement history' 片語也收
                    _en_admin = "query_movement"
                # r16 #16：'slowest sku this month' 曾被 clf 判 compare 直達
                #   （early-return 保護讓 C4 slow 詞攔不到）→ 倉值比較答非所問
                elif _re.search(r"\bslowest\s+(?:skus?|items?|products?|"
                                r"movers?|sellers?)\b", _ul_adm):
                    _en_admin = "list_hot_items"
                # r16 #83：'back to the yoga mat' 曾被 clf 判 search_log(0.90)
                #   → RCA 對帳（坑 14 原案句型）。back to + 商品＝切品查詢。
                elif _re.search(r"\bback to (?:the )?[a-z]", _ul_adm) \
                        and _text_has_item_name(user_text):
                    _en_admin = "query_inventory"
                # r16 #44/#90：'days of cover for it'/'yoga mat days of cover'
                #   曾被 C9 搶成 manage_config(read, key='days of cover')。
                #   撐天就在庫存卡上——直達庫存查詢（kw 由句面/followup 補）。
                elif _re.search(r"\bdays? of (?:cover|stock|supply)\b|"
                                r"\bstock cover\b", _ul_adm):
                    _en_admin = "query_inventory"
                # r14+1（#35）：'inbound volume yesterday' 這類**進出量裸句**
                #   clf 語料沒見過 → query_inventory 把 'inbound volume' 當
                #   商品名查（「查無此商品」）。方向詞＋量詞的組合夠獨特，
                #   直達 movement；period/direction 交 LLM 抽、C4-mvp 期間
                #   接地兜底。排除真寫入句（received/shipped + 數字）。
                elif _re.search(r"\b(?:inbound|outbound|incoming|outgoing)\s+"
                                r"(?:volumes?|flows?|totals?|activity)\b|"
                                r"\b(?:movement|shipping)\s+volumes?\b",
                                _ul_adm) and not _re.search(
                                    r"\b(?:received|shipped|arrived)\s+\d", _ul_adm):
                    _en_admin = "query_movement"
            # ── 2026-08-03（資料邊界批）：「哪個倉最多/最空」三倉排名 ──
            #   compare_warehouses(warehouse_a='all') **本來就支援**三倉排名
            #   （warehouse.py:1080 註解自述「哪個倉最多/最空/各倉分布」），
            #   但英文句一個都進不去（坑 7 同型：功能在、英文入口缺）。
            #   實測 5/5 穩定壞掉（非 LLM 浮動）：
            #     'which warehouse has the most stock' → clf query_inventory(1.00) 全店概覽
            #     'which warehouse is the emptiest'    → 「查無 emptiest 這個商品」
            #   ⚠️ 帶商品名的（'which warehouse has the most wireless mouse'）
            #     **不走這裡**——那要的是單品在各倉的分布，query_inventory
            #     單品卡本來就列三倉數量，是正解。
            _en_whrank_args = None
            if _en_admin is None and _is_mostly_english(user_text):
                _ul_wr = user_text.lower()
                if _re.search(r"\bwhich\s+(?:warehouse|site|location)\b|"
                              r"\bwhat\s+warehouse\b|\brank\s+the\s+warehouses?\b|"
                              # r15 #26：'wheres most of our inventory sitting'
                              #   ＝倉庫排名句
                              r"\bwhere'?s?\s+most\s+of\b", _ul_wr):
                    # 句中有具體商品名 → 不是整倉排名，讓原路由處理（單品卡）
                    _wr_kw = ""
                    try:
                        import warehouse as _W_wr
                        _wr_kw = _extract_sku_keyword(user_text) or ""
                        if _wr_kw:
                            _m_wr = _W_wr.match_items(_wr_kw)
                            if not (_m_wr and _m_wr[0].get("score", 0) >= 4):
                                _wr_kw = ""
                    except Exception:
                        _wr_kw = ""
                    if not _wr_kw:
                        # metric：講「值/金額」用 stock_value，其餘用 item_count
                        #   （「最多東西」「最空」問的是數量，不是金額）
                        _wr_metric = ("stock_value"
                                      if _re.search(r"\bvalue|\bworth|\bmoney|\bnt\$|\bcost",
                                                    _ul_wr)
                                      else "turnover"
                                      # r15 #74：'moves inventory fastest' 中間隔
                                      #   受詞讓 moves? fastest 比不到——容許 1 詞
                                      if _re.search(r"\bturnover|\bmoves?\s+(?:\w+\s+)?fastest|"
                                                    r"\bfastest\s+moving", _ul_wr)
                                      else "item_count")
                        _en_whrank_args = {"warehouse_a": "all", "warehouse_b": "all",
                                           "metric": _wr_metric}
            if _en_whrank_args:
                log.info(f"[en-whrank] {user_text!r} → compare_warehouses{_en_whrank_args}")
                _clf_func_ws = "compare_warehouses"
                _clf_conf_ws = 1.0
                _en_admin_hard = True

            if _en_admin:
                log.info(f"[en-admin] {user_text!r} → {_en_admin}")
                _clf_func_ws = _en_admin
                _clf_conf_ws = 1.0
                # ⚠️ 要標 hard，否則 C18（clf vs model 仲裁）會拿 clf 的原判斷
                #   把它蓋回去——'expiry alerts' 的 clf 是 query_inventory
                #   conf=1.00，en-admin 改成 list_expiring_items 後又被 C18
                #   改回全店概覽（守衛第 10 輪抓到）
                _en_admin_hard = True

            if _clf_func_ws and _clf_func_ws not in ("unknown", "unclear") and _clf_conf_ws >= 0.8:
                log.info(f"[intent_clf primary] vid={vid} {user_text!r} → {_clf_func_ws} (conf={_clf_conf_ws:.2f})")
                func_name = _clf_func_ws
                _needs_llm_ws = func_name in ("manage_config", "run_script", "set_alert",
                                               "set_schedule", "generate_po", "generate_report",
                                               "query_movement", "compare_warehouses")
                # 2026-08-03：en-whrank 已經把參數算好了（warehouse_a/b='all' +
                #   metric），不需要 LLM 再抽一次——丟給 LLM 反而抽不穩。
                if _en_whrank_args:
                    _needs_llm_ws = False
                # r16 'transfer log'：en-admin 的 movement 直達曾被 LLM 覆蓋
                #   func（幻覺 search_log(coffee maker)）——這類裸功能句
                #   period/direction 由 14014 段自算即可，不需要 LLM。
                if _en_admin == "query_movement":
                    _needs_llm_ws = False
                if not _needs_llm_ws:
                    func_args = {}
                    if _en_whrank_args:
                        func_args = dict(_en_whrank_args)   # 三倉排名：參數已算好
                    if func_name in ("query_inventory", "search_log", "query_related_items"):
                        if _pre_kw_ws and len(_pre_kw_ws) >= 2:
                            func_args["keyword"] = _pre_kw_ws
                        # related/search_log 拿髒 kw 會亂錨定（r18：「買吹風機的人
                        # 還會買什麼」kw='買吹風機 人 會買什麼' 靠單字「人」錨到
                        # 露營帳篷 4人）→ 比對不扎實就不塞，讓 tool 回 related_help
                        if func_name in ("search_log", "query_related_items") and func_args.get("keyword"):
                            import warehouse as _W_clf
                            _clf_m = _W_clf.match_items(func_args["keyword"])
                            if not _clf_m or _clf_m[0].get("score", 0) < 3:
                                func_args.pop("keyword", None)
                        # ── r19：**倉別參數**（clf 快路徑原本只填 keyword）──
                        #   `north bluetooth earphones stock` 問的是北倉，
                        #   卻回全三倉概況——數字沒錯（North 141 有列出來），
                        #   但訪客得自己從三個數字裡挑，等於沒回答問題。
                        #   query_inventory **本來就收 warehouse 參數**
                        #   （warehouse.py:650），只是這條快路徑沒填。
                        #   ⚠️ 只在**明確講了倉名**時填；沒講就維持 all，
                        #     不要自作主張挑一個倉。
                        if func_name == "query_inventory":
                            _mw_clf = _re.search(r"\b(north|central|south)\b",
                                                 user_text, _re.I)
                            if _mw_clf:
                                func_args["warehouse"] = _mw_clf.group(1).lower()
                            # r14+2（#64）：'coffee beand stock' 錯字句在這條
                            #   快路徑抽不到 kw → 掉全店概覽。錯字修復層本就
                            #   能救（beand→beans 0.889 且 coffee 同商品佐證），
                            #   接上；修復層對正常句/OOV 反例回空、不影響。
                            if not func_args.get("keyword") and _is_mostly_english(user_text):
                                try:
                                    _fz_clf = _en_fuzzy_keyword(_en_query_core(user_text))
                                    if _fz_clf:
                                        func_args["keyword"] = _fz_clf
                                        log.info(f"[clf-fastpath] 錯字修復補 kw: {_fz_clf!r}")
                                except Exception:
                                    pass
                    elif func_name == "query_movement":
                        # r20：期間原本**寫死 this_month** → 問昨天/上週都被
                        #   吃掉（而且 tool 對不認得的值靜默 fallback 成 today，
                        #   回答看起來很正常但期間是錯的＝誤導級）。
                        func_args["period"] = _period_from_en(user_text) or "this_month"
                        # 方向也從句子抽（原本一律 both）
                        _dir_clf = ("in" if _re.search(
                                        r"\b(?:came in|come in|coming in|received|"
                                        r"receive|inbound|arrived|delivered)\b",
                                        user_text, _re.I)
                                    else "out" if _re.search(
                                        r"\b(?:went out|go out|going out|shipped|"
                                        r"ship|outbound|sold|dispatched)\b",
                                        user_text, _re.I)
                                    else "both")
                        func_args["direction"] = _dir_clf
                        # movement 不從 user_text 抽 keyword（容易誤抽時間/動作詞）
                    _clf_skip_llm_ws = True
                    raw_call = f"{func_name}({func_args})"
                    await push_display({"type": "trace", "stage": "llm_output",
                                         "raw": f"[intent_clf] {func_name} (conf={_clf_conf_ws:.2f})"})
                    # 效能徽章：純分類路由（沒跑 LLM），送路由延遲，tok/s 維持上次
                    await send({"type": "perf", "mode": "route", "ms": _clf_ms,
                                "conf": round(_clf_conf_ws, 2),
                                "tok": _last_perf["tok"], "tps": _last_perf["tps"]})
                    log.info(f"[intent_clf primary] vid={vid} skip LLM, func={func_name} args={func_args}")

            # ── 長度閘門（r30，user 2026-07-14 定調）：小腦不適合長句——超過
            #   30 有效字元就不進 LLM，只讓確定性層（C13b/C7b/直達/RCA…）接手；
            #   接不住就優雅引導。長句 LLM 自由發揮=亂猜主因，這裡直接歸零。──
            _long_det_only = False
            if not _clf_skip_llm_ws:
                # EN build：原門檻「30 有效字元」是為中文調的（中文 30 字很長）。
                #   英文字元數天生是中文的 2-3 倍——'alert me when earphones drop
                #   below 30' 才 7 個單詞卻有 31 字元 → 會攔截幾乎所有正常英文句。
                #   英文改用**單詞數**判斷：> 18 詞才算長句（≈中文 30 字的資訊量）。
                _eff_words = len(re.sub(r"[，。,.!！?？~～、…]", " ", user_text).split())
                if _eff_words > 18:
                    _long_det_only = True
                    func_name, func_args = "query_inventory", {}
                    raw_call = "long-input(det-only)"
                    log.info(f"[long-gate] {_eff_words} 詞 > 18，跳過 LLM 走確定性層")
                    await push_display({"type": "trace", "stage": "llm_output",
                                         "raw": "[long-gate] 長句只走確定性層"})

            if not _clf_skip_llm_ws and not _long_det_only:
                prompt = build_prompt(user_text)

                # 等待取得 llm_lock 本身也要有 timeout（見 api_query 同樣的修法），
                # 否則鎖被異常長時間佔用時，這裡會無限期排隊、前端永遠停在 loading。
                try:
                    async with asyncio.timeout(45.0):
                        async with llm_lock:
                            # 不 reset：保留 KV 前綴快取（見上方說明），RPI5 首結果 3.3s→~1.1s
                            _perf_t0 = __import__("time").perf_counter()
                            r = await asyncio.wait_for(
                                asyncio.to_thread(
                                    LLM, prompt,
                                    max_tokens=MAX_TOKENS,
                                    temperature=TEMPERATURE,
                                    stop=GEMMA_STOP,
                                    echo=False,
                                ),
                                timeout=30.0,
                            )
                            _record_perf(r, _perf_t0)
                except (asyncio.TimeoutError, TimeoutError):
                    log.warning(f"[timeout] vid={vid} 推理超時: {user_text!r}")
                    await send({
                        "type": "error",
                        "text": ("System is a bit busy, please try again in a moment "
                                 "(a shorter phrasing helps, e.g. "
                                 "\"bluetooth earphones stock\")"),
                    })
                    continue
                except Exception as e:
                    log.error(f"[llm-error] vid={vid} {type(e).__name__}: {e}", exc_info=True)
                    await send({"type": "error", "text": "Inference failed, please try again"})
                    continue

                output = r["choices"][0]["text"].strip()
                log.info(f"[trace] vid={vid} model={output[:120]}")
                # 效能徽章：推論完成即送 tok/s 給前端
                await send({"type": "perf", "mode": "llm", **_last_perf})
                await push_display({"type": "trace", "stage": "llm_output", "raw": output})

                parsed = parse_function_call(output)
                if not parsed and _clf_func_ws and _clf_func_ws not in ("unknown", "unclear") \
                        and _clf_conf_ws >= 0.8:
                    # r56 fuzz：clf 高信心路由在場時不放棄——RPI5 模型偶發幻覺函式名
                    # （calculate_safety_stock）讓「安全庫存是多少 全部的」掉到「我看
                    # 不懂」。fallback 用 clf 的 func + 空 args，交給下游校正層補參數。
                    log.info(f"[trace] vid={vid} no_function → clf fallback {_clf_func_ws}"
                             f" (conf={_clf_conf_ws:.2f})")
                    parsed = (_clf_func_ws, {})
                if not parsed:
                    log.info(f"[trace] vid={vid} no_function")
                    await send({"type": "error",
                                "text": ("I didn't get that. Try: \"bluetooth earphones "
                                         "stock\", \"stock alerts\", \"best sellers "
                                         "this month\"")})
                    await push_display({"type": "trace", "stage": "no_function"})
                    continue

                func_name, func_args = parsed
                raw_call = f"{func_name}({func_args})"

            # ── intent_clf 命中時也走到這裡（跟 LLM 分支匯流，同一縮排層繼續
            #   下面共用的 Pre-C 規則 / 校正流程，維持跟 HTTP 版一致的行為）──
            if True:
                # ── Pre-C-Schedule：定時排程意圖攔截 ──
                _list_alert_kws = ("查看警示", "查警示", "有哪些警示", "目前警示", "現在警示")
                _list_sched_kws = ("查看排程", "查排程", "看排程", "有哪些排程", "排程列表", "目前排程",
                                   # r19：「把排程都列出來」曾 clarify 找不到
                                   "排程都列", "列出排程", "排程列出來", "排程清單", "列排程",
                                   # r26：「排程全部列出來」（插字）/「明天有什麼排程」
                                   "排程全部", "全部排程", "有什麼排程", "排程有哪些", "排程有什麼",
                                   # r75：「之前設的排程還在嗎」曾被 rejected
                                   "排程還在", "排程還有", "還有排程", "排程狀態", "我的排程")
                # ── EN build：本區塊 8 個詞表**全中文** → 排程功能（設定/查詢/
                #    取消）對英文整條進不去（實測 'schedule a daily low stock
                #    report at 9am' 掉 guide、'every morning send me the low stock
                #    list' 掉 low_stock 立即查）。LLM 對英文排程句也抽不出
                #    set_schedule（抽成 manage_config）＝**中文版本來就靠這支
                #    正則攔截、不靠 LLM**，所以補這裡才是正解。
                _en_sched = _is_mostly_english(user_text)
                _en_list_alert = bool(_re.search(
                    r"\b(?:(?:show|list|view|see|check|what|which|any)\b.{0,20}\b"
                    r"alerts?\b|alerts?\b.{0,15}\b(?:list|set up|configured|active)\b|"
                    r"my alerts?|current alerts?|existing alerts?|alert rules?)\b",
                    user_text, _re.I))
                _en_list_sched = bool(_re.search(
                    r"\b(?:(?:show|list|view|see|check|what|which|any)\b.{0,20}\b"
                    r"schedules?\b|schedules?\b.{0,15}\b(?:list|set up|configured|active)\b|"
                    r"my schedules?|current schedules?|existing schedules?|"
                    r"scheduled (?:jobs?|tasks?|reports?))\b", user_text, _re.I))
                # ⚠️ 坑 1 變體：`drop` 在庫存語境是**數量下降**不是刪除
                #   （'alert me when earphones drop below 30' 曾被判成取消警示
                #   → list_alerts）。drop 只在後面不接 below/to/under 時才算刪除動詞。
                _en_cancel_verb = bool(_re.search(
                    r"\b(?:cancel|delete|remove|disable|turn off|clear out)\b",
                    user_text, _re.I))
                # 設定語在場 → 一律不是取消（'set/notify me/alert me when…'）
                if _en_cancel_verb and _re.search(
                        r"\b(?:set|create|add|new|notify me|remind me|alert me|"
                        r"let me know|when(?:ever)?\b.{0,30}\b(?:below|under|"
                        r"runs? out|expires?))\b", user_text, _re.I):
                    _en_cancel_verb = False
                _is_alert_set_ws = (any(w in user_text for w in ("新增", "設定", "加入", "建立", "通知我", "提醒我"))
                                    or (_en_sched and bool(_re.search(
                                        r"\b(?:set|create|add|new|notify me|remind me|"
                                        r"alert me|let me know)\b", user_text, _re.I))))
                if (not _is_alert_set_ws and
                        (any(w in user_text for w in _list_alert_kws) or
                         ("警示規則" in user_text and not _is_alert_set_ws) or
                         (_en_sched and _en_list_alert))):
                    func_name = "list_alerts"
                    func_args = {}
                    log.info("[Pre-C-Sched] 查警示攔截 → list_alerts")
                elif any(w in user_text for w in _list_sched_kws) or (_en_sched and _en_list_sched):
                    func_name = "list_schedules"
                    func_args = {}
                    log.info("[Pre-C-Sched] 查排程攔截 → list_schedules")
                elif ((any(w in user_text for w in ("警示", "提醒規則")) and
                        any(w in user_text for w in ("取消", "刪除", "刪掉", "停掉",
                                                      "關閉", "移除", "停用", "解除")))
                       or (_en_sched and _en_cancel_verb
                           and _re.search(r"\balerts?\b", user_text, _re.I))):
                    # r23：「取消瑜珈墊的警示」「停用所有警示」曾回缺貨清單
                    func_name = "list_alerts"
                    func_args = {}
                    log.info("[Pre-C-Sched] 取消警示意圖 → list_alerts（列出讓訪客選）")
                elif (("排程" in user_text and any(w in user_text for w in ("取消", "刪除", "刪掉", "停掉", "關閉", "移除", "砍掉")))
                      or (_en_sched and _en_cancel_verb
                          and _re.search(r"\bschedules?\b", user_text, _re.I))
                      # 代詞式取消（'delete the one i just made'）——上一輪
                      #   剛列過排程/剛建過排程時，指的就是排程。
                      #   列清單讓訪客指名（不做批量刪除，同中文版 conv100-r7）。
                      or (_en_sched and _en_cancel_verb
                          and _re.search(r"\b(?:the\s+)?(?:one|that|it|this)\b",
                                         user_text, _re.I)
                          and (_ctx_by_vid.get(vid) or {}).get("last_view")
                              in ("schedule_list", "schedule_confirm", "schedule_done"))):
                    # 「取消所有排程」→ 先列排程讓訪客指名（不做批量刪除，conv100-r7）
                    func_name = "list_schedules"
                    func_args = {}
                    log.info("[Pre-C-Sched] 取消排程意圖 → list_schedules（列出讓訪客選）")
                else:
                    # 「每個月/每星期」漏收：「幫我排每個月十五號盤點」曾被 Pre-C10
                    # 搶成立即執行腳本（conv100-r5）
                    # 裸「自動」移除（r18：「幫我把缺貨的自動補到安全線」是開採購單
                    # 不是排程，曾被搶走還撞名報 error）——排程句必有「每X」頻率詞
                    _sched_time_kws = ("每天", "每日", "天天", "每週", "每周", "每月", "每個月",
                                       "每星期", "每禮拜", "定時", "排程",
                                       "每天早上", "每天晚上", "每天中午", "固定",
                                       # r74：「明天自動幫我看警示可以嗎」曾直接掉警示清單
                                       "明天自動", "自動幫我")
                    # 「缺貨警示/警示」入列：「每天晚上七點自動出缺貨警示」是排程不是立即查（conv100-r5）
                    # 「報表」入列：「每週三下午三點出貨報表」曾立即產報告（conv100-r9）
                    _sched_act_kws  = ("盤點", "匯出", "報告", "報表", "體檢", "腳本", "跑", "月報", "週報",
                                       "缺貨警示", "警示", "缺貨")
                    # EN build：頻率詞 + 動作詞都要英文版，否則英文排程句
                    #   （'schedule a daily low stock report at 9am'）永遠
                    #   進不到 set_schedule。同樣要求「頻率 + 動作」兩者齊備，
                    #   避免 'daily sales' 這種純形容詞句被誤攔。
                    # ⚠️ 索取語氣讓路（2026-08-03）：`show me the daily report`
                    #   的 daily 是形容詞不是頻率 → 不可攔進 set_schedule
                    #   （訪客只想看報表，卻收到「每天 09:00 自動執行」確認卡）。
                    _en_sched_time = (not _en_daily_is_adjective(user_text)) and bool(_re.search(
                        r"\b(?:schedule|scheduled|recurring|"
                        r"every\s+(?:day|morning|night|evening|week|month|monday|"
                        r"tuesday|wednesday|thursday|friday|saturday|sunday|\d+\s*days?)|"
                        r"daily(?!\s+(?:goods|necessities))|weekly|monthly|nightly|"
                        r"each\s+(?:day|week|month)|"
                        r"automatically|auto)\b", user_text, _re.I))
                    _en_sched_act = bool(_re.search(
                        r"\b(?:report|reports|stocktake|stock take|audit|export|"
                        r"alert|alerts|low stock|expiry|expiring|health check|"
                        # 2026-08-02：UI 排程頁教訪客打 "run a stock count
                        #   every day at 9am"。`run …` 靠 run 這個動作詞
                        #   才進得來，但 `schedule a stock count …` 不含
                        #   任何動作詞 → Pre-C-Sched 整段跳過 → clf 的
                        #   run_script(1.00) 一路到底，C18 把 LLM 亂填的
                        #   period='today' 當 script_name → 回「today 不在白名單」。
                        r"stock count|inventory count|cycle count|"
                        r"script|send me|email me|run)\b", user_text, _re.I))
                    _has_sched_time = (any(w in user_text for w in _sched_time_kws)
                                       or (_en_sched and _en_sched_time))
                    _has_sched_act  = (any(w in user_text for w in _sched_act_kws)
                                       or (_en_sched and _en_sched_act))
                    # ⚠️ 保險（守衛 low 類回歸教訓）：攔進 set_schedule 前先確認
                    #   **抽得到腳本**，否則 tools 會回 `Script "" not found` 的
                    #   醜錯誤（view=error）。抽不到就不攔，讓原路由正常走。
                    #   只對英文句加這道（中文詞表磨了三個月、誤攔率已低）。
                    if _has_sched_time and _has_sched_act and _en_sched:
                        try:
                            import tools_v2 as _tv2_sc
                            if not _tv2_sc._parse_schedule_intent(user_text).get("script_id"):
                                _has_sched_act = False
                                log.info(f"[Pre-C-Sched] 英文排程句抽不到腳本 → 不攔截: {user_text!r}")
                        except Exception:
                            pass
                    if _has_sched_time and _has_sched_act:
                        if func_name != "set_schedule":
                            func_name = "set_schedule"
                            func_args = {"raw_text": user_text}
                            log.info(f"[Pre-C-Sched] 排程意圖攔截 → set_schedule raw_text={user_text!r}")
                        else:
                            # LLM 已判 set_schedule 但自己亂填參數時，原句一定要帶給 tools 重解析
                            func_args["raw_text"] = user_text

                # ── Pre-C10：腳本意圖強攔截（在 clarify / LLM 校正之前）──
                _prec10_skip = ("run_script", "set_schedule", "query_movement", "compare_warehouses")
                # ⚠️ 英文腳本意圖（2026-08-04,坑 7 典型：詞表全中文,英文一處
                #   也命中不了 → Pre-C10 整段跳過,英文匯出句落到 clf/LLM 自由判斷。
                #   實測 `export movements last week` 被判 compare_periods、
                #   `last month` 被判 compare_warehouses,只有 yesterday/quarter
                #   剛好判對 ⇒ **純靠運氣**）。
                #   要求動詞+受詞齊備,避免裸 export/report 誤攔（坑 8：
                #   英文功能詞常同時是業務詞）。
                _pre_en_script = None
                # ⚠️ 動詞含**索取式**（2026-08-04）：訪客不會只講 export，
                #   更常講 `give me the movement log` / `can i get the records`。
                #   必須與受詞連用才算，否則 `give me the stock` 被誤攔（坑 8）。
                if _en_export_intent(user_text):
                    # ⚠️ 單一商品讓路（2026-08-04）：`give me the movement records
                    #   for wireless mouse`（match 13 分）該留統計卡,不轉全倉匯出。
                    #   門檻 6 同其他層（實測雜訊 3-5 / 真商品 7-13）。
                    #   商品讓路要**每個開火點都接**——先前只接 Cmp2/C16,
                    #   漏了這裡,Cmp2 判對也來不及（Pre-C10 先轉完）。
                    try:
                        import warehouse as _W_p10e
                        _p10_m = _W_p10e.match_items(user_text)
                        _p10_item = bool(_p10_m and _p10_m[0].get("score", 0) >= 6)
                    except Exception:
                        _p10_item = False
                    if not _p10_item:
                        _pre_en_script = "export movements"   # ⚠️ 英文版白名單別名,不可用中文「匯出」
                elif _re.search(r"\b(?:run(?:ning)?|do(?:ing)?|perform(?:ing)?|"
                                r"start(?:ing)?)\b", user_text, _re.I) and \
                        _re.search(r"\b(?:stocktake|stock\s*take|stock\s*count|"
                                   r"inventory\s*count|audit)\b", user_text, _re.I):
                    # 2026-08-04：'would you mind **running** a quick stock audit'
                    #   動名詞不匹配 \brun\b 曾整段 miss → RCA/設定 guide
                    _pre_en_script = "stocktake"
                elif _re.search(r"\bhealth\s+check(?:up)?\b", user_text, _re.I):
                    # 2026-08-04：'warehouse health check' 被 clf 判
                    #   query_inventory → keyword 'health' → 查無商品。
                    #   health check＝體檢報告（manifest 別名精確命中）。
                    _pre_en_script = "health report"
                _pre_script_kws = ("盤點", "匯出進出", "匯出記錄", "進出記錄", "體檢報告", "月底盤點",
                                   # r19：「匯出這週的進出成Excel」動詞跟受詞被隔開
                                   "匯出")
                _pre_script_hit = next((w for w in _pre_script_kws if w in user_text),
                                       None) or _pre_en_script
                # 排程語氣（每個月十五號盤點）讓給 Pre-C-Sched，不搶成立即執行（conv100-r5）
                _prec10_sched = any(w in user_text for w in (
                    "每天", "每日", "天天", "每週", "每周", "每月", "每個月", "每星期", "每禮拜", "排程"))
                # r27：查詢語境豁免（「剛剛盤點的時候發現…數字是多少」是查庫存）
                _prec10_query = any(w in user_text for w in ("是多少", "多少", "還剩", "剩幾",
                                                              "數字", "對不對", "的時候", "發現",
                                                              "在哪", "哪裡", "哪邊"))
                if _pre_script_hit and func_name not in _prec10_skip and not _prec10_sched and not _prec10_query:
                    smap = {"盤點": "盤點", "月底盤點": "月底盤點",
                            "匯出進出": "匯出", "匯出記錄": "匯出", "進出記錄": "匯出",
                            "匯出": "匯出", "體檢報告": "體檢報告"}
                    func_name = "run_script"
                    func_args = {"script_name": smap.get(_pre_script_hit, _pre_script_hit)}
                    # ⚠️ 英文匯出路帶原句（2026-08-04）：C9b 只認 export|download
                    #   字面,索取式句子不帶 _period_text 會永遠用預設 7 天。
                    if _pre_en_script:
                        func_args["_period_text"] = user_text
                    log.info(f"[Pre-C10] 腳本意圖強攔截 → run_script script_name={func_args['script_name']!r}")

                # ── Pre-C-Movement（ws 版）──
                _movement_kws_ws = ("查詢進出記錄", "進出記錄", "出貨了多少", "上週進了多少",
                                    "最近30天出貨", "進貨記錄", "出貨記錄", "入庫記錄", "移動記錄")
                _compare_kws_ws  = ("比較各倉庫庫存", "各倉庫比較", "三個倉庫比較", "北中南倉",
                                    "倉庫比較", "倉庫對比", "比較倉庫",
                                    "三個倉", "三倉", "各倉", "每個倉", "哪個倉", "哪一倉", "哪邊",
                                    "誰大", "誰多", "誰高")
                _alert_set_kws_ws = ("新增庫存警示規則", "設定缺貨警示", "設定警示", "新增警示",
                                     "庫存不足時提醒", "低於安全庫存通知")
                _skip_override = ("run_script", "set_schedule", "list_schedules",
                                  "list_alerts", "delete_alert", "delete_schedule")
                _has_rca_kw_ws = _has_rca_word(user_text)
                # 帶具體商品名 → 查該商品分倉庫存，不是倉庫比較
                # （第13輪抓到：「牛仔長褲各倉還有幾條」被「各倉」誤劫）
                import warehouse as _W_cmp_ws
                _cmp_prod_kw_ws = _extract_sku_keyword(user_text)
                _cmp_has_prod_ws = bool(_cmp_prod_kw_ws and _W_cmp_ws.match_items(_cmp_prod_kw_ws))
                if func_name not in _skip_override:
                    if (not _has_rca_kw_ws and
                            func_name != "query_movement" and
                            any(w in user_text for w in _movement_kws_ws)):
                        func_name = "query_movement"
                        func_args = {"period": "this_month", "direction": "both"}
                        log.info("[Pre-C-Mov] → query_movement")
                    elif (func_name != "compare_warehouses"
                          and not _cmp_has_prod_ws
                          # 缺貨/到期句讓給 C3/C7（「三個倉的缺貨數量比一比」該回缺貨清單，conv100-r5）
                          and not any(w in user_text for w in ("缺貨", "低庫存", "到期", "過期"))
                          # 進出句讓給 movement（「今天各倉的進出總覽」曾被「各倉」劫走，conv100-r7）
                          and not any(w in user_text for w in ("進出", "進貨", "出貨", "異動"))
                          and any(w in user_text for w in _compare_kws_ws)):
                        func_name = "compare_warehouses"
                        # 三倉排名 cue（哪個最多/最空/各倉/分布，且沒點名 2 倉）→ warehouse_a=all
                        # 觸發三倉排名，而非只比 2 倉（RPI5 conv100-r4）
                        _named2 = sum(1 for z in ("北倉","北區","中倉","中區","南倉","南區") if z in user_text)
                        _rank3 = any(w in user_text for w in (
                            "哪個倉","哪倉","各倉","三倉","三個倉","每個倉","最多","最空",
                            "最滿","分布","佔比","哪個最","誰最"))
                        _mt3 = "item_count" if any(w in user_text for w in ("東西","商品","幾件","幾項","數量","最多","最空","最滿","塞")) else "stock_value"
                        func_args = {"warehouse_a": "all", "warehouse_b": "all", "metric": _mt3} if (_rank3 and _named2 < 2) else {}
                        log.info(f"[Pre-C-Cmp] → compare_warehouses args={func_args}")
                    elif func_name not in ("set_alert", "list_alerts") and any(w in user_text for w in _alert_set_kws_ws):
                        func_name = "set_alert"
                        func_args = {"raw_text": user_text}
                        log.info("[Pre-C-Alert] → set_alert")

                # ── Pre-C-Cmp2：compare 倉名/指標一律以原句為準 ──
                # LLM 對 compare 的 warehouse_a/b 常自由發揮（「北倉和中倉哪邊的貨
                # 比較齊」回 central vs south，conv100-r5）。句中點名 2 倉 → 依出現
                # 順序覆寫；點名 <2 倉且有排名語氣 → 三倉排名；指標詞覆寫 metric。
                # ⚠️ 匯出句讓路（2026-08-04，今天第五次同型）：
                #   `export movements last month` LLM 幻覺出 compare_warehouses，
                #   Cmp2 又抽出**句中根本沒有的**商品名 Elastic Sports Bra
                #   → query_inventory。Cmp2 跑在 Pre-C10 之前 ⇒ 路由被定死。
                # 改用共用判準（含索取式動詞,2026-08-04）
                _cmp2_is_export = _en_export_intent(user_text)
                # ⚠️ 單一商品讓路（user 定調：統計卡留給只問單一商品）：
                #   句中有接地商品就不轉全倉匯出。接地看**句子**不看 LLM args
                #   （LLM keyword 常是幻覺,trace 實見 'food item'/'coffee maker'）。
                _cmp2_has_item = False
                if _cmp2_is_export:
                    try:
                        # ⚠️ 同 C16：match_items(整句)，不經抽取器 fallback
                        import warehouse as _W_cmp2e
                        _cmp2_m = _W_cmp2e.match_items(user_text)
                        # 門檻 6：同 C16（實測雜訊 3-5 / 真商品 7-13）
                        _cmp2_has_item = bool(_cmp2_m and _cmp2_m[0].get("score", 0) >= 6)
                    except Exception:
                        _cmp2_has_item = False
                    if _cmp2_is_export:
                        log.info(f"[Cmp2-exp] export=True item={_cmp2_has_item}")
                # ⚠️ 讓路不夠,要 **hard-return 定案**（坑 16）：只做「不搶」會讓
                #   句子停在 LLM 幻覺的 compare_warehouses 上（實測回倉庫週轉率）。
                #   匯出意圖明確 ⇒ 直接定案 run_script,腳本名用 manifest 英文別名。
                if _cmp2_is_export and not _cmp2_has_item and func_name in (
                        "compare_warehouses", "compare_periods", "query_inventory",
                        "query_movement"):
                    func_name = "run_script"
                    func_args = {"script_name": "export movements",
                                 "_period_text": user_text}
                    log.info(f"[Pre-C-Cmp2] 匯出句定案 → run_script: {user_text!r}")
                if func_name == "compare_warehouses" and _cmp_has_prod_ws \
                        and not _cmp2_is_export:
                    # 帶真商品名的「XX北倉中倉哪邊多」是查該商品分倉庫存，
                    # 不是倉庫總量比較（conv100-r10；LLM 直接輸出 compare 時
                    # elif 的商品守衛擋不到）
                    import warehouse as _W_cmp2
                    _m_cmp2 = _W_cmp2.match_items(_cmp_prod_kw_ws)
                    if _m_cmp2 and _m_cmp2[0].get("score", 0) >= 3:
                        func_name = "query_inventory"
                        func_args = {"keyword": _cmp_prod_kw_ws}
                        log.info(f"[Pre-C-Cmp2] compare 帶商品名 → query_inventory kw={_cmp_prod_kw_ws!r}")
                if func_name == "compare_warehouses" and any(
                        w in user_text for w in ("缺貨", "低庫存")):
                    # 「三個倉的缺貨數量比一比」問的是缺貨，排名商品總數答非所問
                    # → 轉缺貨清單（C3 會 hard-return list_low_stock）
                    func_name = "list_low_stock"
                    func_args = {}
                    log.info("[Pre-C-Cmp2] compare 含缺貨語意 → list_low_stock")
                if func_name == "compare_warehouses" and any(
                        w in user_text for w in ("進出", "進貨", "出貨", "異動")):
                    # 「今天各倉的進出總覽」是進出統計不是倉庫排名（conv100-r7）
                    func_name = "query_movement"
                    func_args = {"direction": "both",
                                 "period": ("today" if "今天" in user_text else
                                            "this_week" if any(w in user_text for w in ("本週", "這週", "這禮拜")) else
                                            "this_month")}
                    log.info("[Pre-C-Cmp2] compare 含進出語意 → query_movement")
                if func_name == "compare_warehouses":
                    _cw_pos = []
                    for _zh, _en in (("北倉", "north"), ("北區", "north"), ("中倉", "central"),
                                     ("中區", "central"), ("南倉", "south"), ("南區", "south")):
                        _p = user_text.find(_zh)
                        if _p >= 0 and _en not in [e for _, e in _cw_pos]:
                            _cw_pos.append((_p, _en))
                    # ── EN build（語音）：英文倉名也要依原句校準（同坑 7：
                    #   上面那張表的鍵全是中文，英文句一處也命中不了 → 沿用
                    #   LLM 抽的值）。ASR 把倉名聽壞時（`compare north and
                    #   self-way house`）LLM 給 warehouse_b='self-way'，
                    #   原樣送進 tool → 訪客看到醜的 `view=error
                    #   「Warehouse must be north / central / south」`。
                    if not _cw_pos:
                        _ut_cmp = user_text.lower()
                        for _en_w in ("north", "central", "south"):
                            _p = _ut_cmp.find(_en_w)
                            if _p >= 0 and _en_w not in [e for _, e in _cw_pos]:
                                _cw_pos.append((_p, _en_w))
                    _cw_seq = [e for _, e in sorted(_cw_pos)]
                    _cw_rank3 = any(w in user_text for w in (
                        "哪個倉", "哪倉", "各倉", "三倉", "三個倉", "每個倉",
                        "最多", "最空", "最滿", "分布", "佔比", "哪個最", "誰最"))
                    func_args = dict(func_args)
                    if len(_cw_seq) >= 3:
                        # r30：「中倉北倉南倉庫存量誰最多」點名三倉 → 三倉排名（曾只比前兩倉）
                        func_args["warehouse_a"] = func_args["warehouse_b"] = "all"
                    elif len(_cw_seq) == 2:
                        func_args["warehouse_a"], func_args["warehouse_b"] = _cw_seq
                    elif len(_cw_seq) < 2 and _cw_rank3:
                        func_args["warehouse_a"] = func_args["warehouse_b"] = "all"
                    if "週轉" in user_text or any(w in user_text for w in ("賣得快", "賣最快", "賣比較快")):
                        # r30：「南倉跟中倉哪個賣得快」是週轉語意（曾比商品數量）
                        func_args["metric"] = "turnover"
                    elif any(w in user_text for w in ("價值", "總值", "值多少", "金額")):
                        func_args["metric"] = "stock_value"
                    elif any(w in user_text for w in ("幾件", "幾項", "品項數", "商品數")):
                        func_args["metric"] = "item_count"
                    # ── EN build（語音）：**無效倉名接地**——上面校準完仍可能
                    #   留著 LLM 抽的雜訊（ASR 聽壞倉名時只找得到一個真倉名，
                    #   長度不足 2 走不到覆寫分支）。無效值直接送進 tool 會回
                    #   `view=error`＝訪客看到醜錯誤。改成：只留有效倉名，
                    #   湊不滿兩個就退回全倉比較（tool 的正常路徑）。
                    #   ⚠️ 同「LLM 佔位字串會被下游當真實值」那類坑：凡是拿
                    #   LLM 輸出當 enum 參數的地方都要驗，不能假設它合法。
                    _VALID_WH_CMP = ("north", "central", "south", "all")
                    _bad_wh = [_k for _k in ("warehouse_a", "warehouse_b")
                               if str(func_args.get(_k, "")).lower() not in _VALID_WH_CMP]
                    if _bad_wh:
                        _keep = [str(func_args.get(_k, "")).lower()
                                 for _k in ("warehouse_a", "warehouse_b")
                                 if str(func_args.get(_k, "")).lower() in
                                 ("north", "central", "south")]
                        log.info(f"[Pre-C-Cmp2] 無效倉名 {[func_args.get(_k) for _k in _bad_wh]}"
                                 f" → 退回全倉比較（保留 {_keep}）")
                        func_args["warehouse_a"] = func_args["warehouse_b"] = "all"
                    log.info(f"[Pre-C-Cmp2] compare args 依原句校準 → {func_args}")

                # ── Clarification：模糊意圖攔截（在校正前）──
                # EN build：clf 已高信心（≥0.8）判出意圖並 skip LLM 時，不該再被
                #   _detect_clarify 推翻成「你想查X的什麼？」——那個判斷靠中文意圖詞，
                #   英文錯字（stok≠stock）就失效，會把 clf conf=1.00 的正確路由打掉。
                #   clf 說得準就執行，別再問。
                # ⚠️ **匯出期間反問是例外**（2026-08-03）：上面的 skip 是為了
                #   「別把 clf 的高信心路由推翻成『你想查 X 的什麼？』」，
                #   但匯出期間反問**不是猜商品**，是已知意圖下問參數
                #   （訪客講「匯出進出紀錄」沒說期間）⇒ 不該被 skip 掉。
                #   ⇒ skip 時仍跑一次，但**只採用匯出那一種**（hint 標記）。
                if _clf_skip_llm_ws:
                    _c_try = _detect_clarify(user_text)
                    clarify = _c_try if (_c_try or {}).get("hint") in (
                        "Movement log export", "進出紀錄匯出") else None
                else:
                    clarify = _detect_clarify(user_text)
                if clarify:
                    log.info(f"[clarify] vid={vid} q={clarify['question']!r}")
                    await send({"type": "done", "result": {
                        "ok": True,
                        "summary": clarify["question"],
                        "view": "clarify",
                        "data": clarify,
                    }})
                    continue

                # ── 校正（一定要在 task_plan / OOV 判斷之前：這兩者都是根據 func_name
                #   決定顯示內容跟商品比對邏輯，若 C13b 等規則會把 func_name 校正成
                #   create_movement，校正前的 keyword 可能還帶著數字量詞雜訊，直接
                #   餵給 OOV 找不到商品會誤判成查詢失敗，task_plan 也會顯示錯的步驟，
                #   see HTTP 版 api_query 的同一個順序，2026-07-02 修 WS/HTTP 不同步）──
                func_name, func_args, _hard = _correct_function_call(user_text, func_name, func_args)

                # ── 長度閘門收尾（r30）：長句只走確定性層，若整條校正鏈都沒接手
                #   （佔位 query_inventory 原樣出來）→ 優雅引導，不硬答 ──
                if (_long_det_only and not _hard and func_name == "query_inventory"
                        and not func_args.get("keyword") and not func_args.get("category")):
                    _lg_msg = ("這句有點長，我怕理解錯你的意思——試試短一點的問法，"
                               "例如「藍牙耳機庫存」「北倉進50個滑鼠」「哪些快缺貨」。")
                    log.info(f"[long-gate] 確定性層無接手 → 優雅引導: {user_text!r}")
                    for ch in _lg_msg:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get())
                    await send({"type": "done", "result": {
                        "ok": True, "view": "clarify", "summary": _lg_msg,
                        "data": {"question": _lg_msg, "options": [], "hint": ""}}})
                    continue

                # ── 任務拆解進度樹：根據工具送出計劃步驟 ──
                _TASK_PLANS = {
                    "query_inventory":    ["解析查詢關鍵字", "查詢各倉庫庫存", "彙整結果"],
                    "query_movement":     ["解析時間範圍", "讀取進出記錄", "統計進出量"],
                    "search_log":         ["解析異常關鍵字", "搜尋操作日誌", "比對進出差異", "分析異常原因"],
                    "manage_config":      ["解析設定項目", "讀取/寫入設定值", "確認變更"],
                    "run_script":         ["確認腳本路徑", "執行腳本", "取得執行結果"],
                    "list_low_stock":     ["掃描各倉庫庫存", "比對安全庫存水位", "產生低庫存清單"],
                    "list_hot_items":     ["讀取出貨記錄", "計算銷售排名", "產生排行清單"],
                    "compare_warehouses": ["讀取倉庫資料", "比較指定指標", "產生對比報告"],
                    "generate_po":        ["計算補貨需求", "匹配供應商報價", "產生採購草稿"],
                    "set_alert":          ["解析警示條件", "確認商品範圍", "建立警示規則"],
                    "query_related_items":["解析商品關鍵字", "搜尋關聯品項", "產生推薦清單"],
                    "list_expiring_items":["掃描保存期限", "找出即將到期品項", "產生警示清單"],
                    "generate_report":    ["收集報表資料", "產生報表內容", "輸出檔案"],
                    "create_movement":    ["解析商品與數量", "比對倉庫與庫存", "產生確認卡"],
                }
                plan_steps = _TASK_PLANS.get(func_name, ["分析請求", "執行查詢", "回傳結果"])
                # **不送 task_plan**（user 2026-07-25 決定移除，中英同步）。
                #   原意是給 Agent 一個 checklist 視覺效果，但下面的 Agent
                #   trace 已經在做同一件事、而且說的是**真的做了什麼**：
                #     task_plan：解析商品與數量 / 比對倉庫與庫存 / 產生確認卡
                #                （寫死，每次都一樣）
                #     trace    ：scanned transactions/ → 180/180 log files
                #                matched "Electric Toothbrush" → 86 records
                #                scanned POs → 22 contain it（帶真實數字）
                #   trace 同樣逐步展開、task_tick 打勾也還在送，所以視覺效果
                #   沒少，反而少了一塊寫死的內容弱化 demo 說服力。
                #   plan_steps 保留——下面 task_tick 的節奏用它的長度算，
                #   前端沒有 task_plan 元素時 tick 會自己 return。
                #   （要恢復就把下面這段 send 解除註解）
                # if func_name != "search_log":
                #     try:
                #         await send({"type": "task_plan", "steps": plan_steps})
                #         await asyncio.sleep(0.1)
                #     except RuntimeError:
                #         pass

                # search_log keyword 在 OOV 前先用 _extract_sku_keyword 預清理，
                # 避免模型帶入雜詞（例如「抗菌洗衣精帳」）降低 fuzzy 分
                if func_name == "search_log" and func_args.get("keyword"):
                    pre_kw = _extract_sku_keyword(func_args["keyword"])
                    if pre_kw:
                        func_args = {**func_args, "keyword": pre_kw}

                # ── EN build 防幻覺閘門：LLM 抽的 keyword 必須真的出現在原句裡 ──
                #   實測 'what came in today' → LLM 吐 keyword='beverage'（句中根本沒有），
                #   害後面 OOV 找不到 → clarify。原句沒出現的 keyword 一律丟掉，
                #   讓 tool 走「無 keyword」的正常路徑（全店查詢）。
                #   比對用小寫且允許逐詞命中（keyword 可能是複合詞的一部分）。
                if func_args.get("keyword"):
                    _kw_h = str(func_args["keyword"]).lower().strip()
                    # ⚠️ 比對基準要用「原句 + 別名正規化後的原句」——否則
                    #   'earbuds stock' 經 alias_en 正規化出的正解 keyword
                    #   'Wireless Bluetooth Earphones' 會被自己的防幻覺閘門丟掉
                    #   （別名修復與防幻覺互相打架，2026-07-25 守衛抓到）。
                    _txt_h = user_text.lower()
                    try:
                        from alias_en import normalize_alias_en as _norm_h
                        _txt_h += " " + _norm_h(user_text).lower()
                    except Exception:
                        pass
                    # ⚠️ 第三個接地依據＝**英文模糊層的結果**。訪客打錯字時
                    #   （keyyboard/pwerbank/crackees）正解 keyword 的字面
                    #   本來就不會出現在原句——那正是要模糊救的原因。少了這條，
                    #   模糊層剛救回的正解會被這道閘門丟掉，又退回全店概覽
                    #   （別名修復曾踩過同一個坑，見上方註解）。
                    #   用 _extract_sku_keyword（英文快路徑：剝虛詞 → 精確
                    #   比對 → 模糊層，且自帶不確定不猜/OOV 防線）的結果當
                    #   基準——直接呼叫 _en_fuzzy_keyword 要自己傳剝乾淨的
                    #   核心詞，傳整句會被 'on hand' 這種虛詞觸發 OOV 防線。
                    _fz_ok = False
                    if _is_mostly_english(user_text):
                        try:
                            _fz_h = _extract_sku_keyword(user_text)
                            _fz_ok = bool(_fz_h) and _fz_h.lower() == _kw_h
                        except Exception:
                            _fz_ok = False
                        # 功能描述句的 keyword 字面本來就不在原句
                        #   （'something to clean teeth' → Electric Toothbrush）
                        if not _fz_ok:
                            _d_h = _en_descriptor_hit(user_text)
                            _fz_ok = bool(_d_h) and _d_h.lower() in _kw_h
                        # 第四個接地依據＝**直接問模糊層**（餵剝過虛詞的 core）。
                        #   上面用 _extract_sku_keyword 當基準，但它可能被
                        #   撞名詞帶偏（'lapptop case' → Phone Protective
                        #   **Case**），而 [EN kw 糾正] 已用模糊層把 kw 改成
                        #   正解 14-inch Laptop Bag → 基準不匹配 → 正解被這道
                        #   閘門丟掉（坑 3 第五次）。模糊層自己認得的就算接地。
                        if not _fz_ok:
                            try:
                                _fz_h2 = _en_fuzzy_keyword(_en_query_core(user_text))
                                if _fz_h2:
                                    _fz_ok = _fz_h2.lower() == _kw_h
                                    if not _fz_ok:
                                        import warehouse as _W_h2
                                        _m_h2 = _W_h2.match_items(_fz_h2)
                                        _fz_ok = bool(_m_h2) and \
                                            _m_h2[0]["item"]["name"].lower() == _kw_h
                            except Exception:
                                pass
                    # ⚠️ EN build（branch_walk r1）：逐詞命中原本是 `any`，
                    #   **任一詞中就放行** → LLM 對 'Drip Coffee Bags 20pcs'
                    #   幻覺出 keyword='coffee filter'，靠 'coffee' 一個詞
                    #   矇混過關 → 訪客點「咖啡袋」卻收到「咖啡濾紙」
                    #   （選單分支誤配，畫面級破口）。
                    #   英文改成：多詞 keyword 要求**每個實詞**都在原句，
                    #   單詞 keyword 維持原邏輯。
                    _kw_words = [w for w in _kw_h.split() if len(w) >= 3]
                    if _is_mostly_english(user_text) and len(_kw_words) >= 2:
                        _kw_partial = all(w in _txt_h for w in _kw_words)
                    else:
                        _kw_partial = any(w in _txt_h for w in _kw_words)
                    if _kw_h and not _fz_ok and not any(c in _txt_h for c in (_kw_h,)) \
                            and not _kw_partial:
                        log.info(f"[anti-hallu] keyword={_kw_h!r} 不在原句 → 丟棄")
                        func_args.pop("keyword", None)

                # ── EN build：LLM 抽的 keyword 也套英文俗稱正規化 ──
                #   （_extract_sku_keyword 那條路已套，這裡補 LLM 直出的 keyword：
                #     LLM 常原樣抽 'earbuds'/'sneakers'，主檔沒這些字會對不到）
                if func_args.get("keyword"):
                    try:
                        from alias_en import normalize_alias_en as _norm_kw
                        _kw_o = str(func_args["keyword"])
                        _kw_n = _norm_kw(_kw_o)
                        if _kw_n != _kw_o:
                            func_args["keyword"] = _kw_n
                            log.info(f"[alias-en] keyword {_kw_o!r} → {_kw_n!r}")
                    except Exception:
                        pass

                # ── EN build OOV-noname：英文查詢句「指名了某個東西，但那個
                #   東西不在庫」→ 誠實說沒有，不要回全店概覽。
                #   'microwave stock' / 'toothpaste inventory' / 'bicycle
                #   inventory' 含 stock/inventory 白名單詞（確實是倉管句、
                #   守門員該放行），但抽不到 keyword → 退概覽＝答非所問
                #   （守衛 noex 類期望 clarify/rejected）。
                #   判準：句中有「非虛詞的實詞」，且該實詞既不是主檔用字、
                #   也模糊比不到 → 那是庫裡沒有的商品名。
                if (func_name == "query_inventory" and not func_args.get("keyword")
                        and not func_args.get("category")
                        and _is_mostly_english(user_text)):
                    _NOEX_STOP = {
                        # r1：'whats worth watching in stock' 的 watching/worth
                        #   被當商品名（訪客問的是「有什麼要注意的」）
                        "watching", "watch", "worth", "noting", "note",
                        "interesting", "important", "urgent", "attention",
                        # r22：Agent 功能詞（不是商品名）——`show me the scripts`
                        #   回「查無 scripts 這個商品」；errors/export 同款
                                                # r24：到期/搭售的功能詞（不是商品名）——
                        #   `check expiry dates` → 查無 dates、
                        #   `show me pairings` → 查無 pairings、
                        #   `whats going off soon` → 查無 "off soon"
                        "date", "dates", "expiry", "expiration", "pairing",
                        "pairings", "bundle", "bundles", "combo", "combos",
                        "off", "soon", "bad", "spoiled",
                        # r14 網頁百句：比較/雜訊詞（與 _oov_stop 同步）——
                        #   'how many SKUs do we carry' 曾走 [oov:noex]
                        #   回「查無 skus carry 這個商品」
                        "sku", "skus", "more", "less", "than", "fewer",
                        "exceed", "exceeds", "number", "carry", "carrying",
                        # r14+1：營運行話（與 _oov_stop 同步；cover 不收）
                        "stockout", "risk", "risks", "replenishment",
                        "urgent", "popular", "unpopular", "volume",
                        "volumes", "dead", "zero", "restocks", "restock",
                        # r14+2：weekend＋功能名詞（與 _oov_stop 同步）
                        "weekend", "weekends", "restocking", "replenishing",
                        "transfer", "transfers", "movement", "movements",
                        # r15：口語縮寫/狀態詞（與 _oov_stop 同步）
                        "numbers", "gimme", "lemme", "wheres", "sitting",
                        "totals", "categories", "category", "stale",
                        # r18（與 _oov_stop 同步）
                        "sanity", "customer", "customers", "asking",
                        "time", "times", "usual",
                        "script", "scripts", "error", "errors", "export",
                        "exports", "backup", "backups", "audit", "audits",
                        # r15：**確認/操作詞永遠不是商品名**。卡片被前一句插話
                        #   清掉後，訪客才說 'ok go ahead' → 'ahead' 被當商品
                        #   查 → 回「查無 ahead 這個商品」（訪客一頭霧水）。
                        #   正解是回「目前沒有進行中的操作」那類引導。
                        "ahead", "proceed", "submit", "confirm", "confirmed",
                        "approve", "approved", "accept", "agreed", "sure",
                        "okay", "yep", "yeah", "yup", "fine", "alright",
                        # r12（探針批）：禮貌用語——與上面 _oov_stop 同步
                        #   （兩處必須一致，否則修一層下一層再擋一次）
                        "could", "would", "should", "shall", "might", "may",
                        "like", "ask", "asking", "know", "knowing", "want",
                        "wanted", "wish", "hoping", "hope", "kindly", "mind",
                        "possible", "possibly", "maybe", "perhaps", "just",
                        "quick", "quickly", "question",
                        # 2026-08-03（資料邊界批）：**分布/範圍介系詞**——
                        #   `show me wireless mouse across warehouses` 的 across
                        #   被當陌生修飾詞 → C1g-oov **清掉已抽對的
                        #   'Wireless Mouse'** → 回「查無 across warehouses」。
                        #   clf conf=0.99、keyword 抽對，純粹被閘門吃掉正解
                        #   （坑 3 同型）。這些是純方位/範圍詞，不可能是商品名。
                        #   ⚠️ 不收 "in"（in-ear/built-in 等商品名含它）。
                        "across", "among", "amongst", "between", "throughout",
                        "per", "each", "every", "versus", "vs",
                        # r1：確認語（did it take effect / put it back）不是商品名
                        "effect", "effective", "applied", "apply", "back",
                        "done", "changed", "change", "updated", "saved",
                        "take", "takes", "took", "put", "puts", "get", "gets",
                        # r2：序數/最高級指代（the most urgent one）不是商品名
                        "most", "least", "urgent", "one", "ones", "cheapest",
                        "biggest", "largest", "smallest", "newest", "oldest",
                        # r3：語音/快打的虛詞黏字與常見錯字（不是商品名）
                        "howmany", "howmuch", "whatabout", "isthere", "stok",
                        "stcok", "invetory", "inventry", "wat", "wht", "hw",
                        # 劇情批 r1：寒暄/時間/語氣詞不是商品名——'hi there
                        #   busy today' 曾回「No item matching "busy today"」
                        #   （訪客只是打招呼，卻被當成在查一個叫 busy today
                        #   的商品）。這類詞要跟功能詞一樣列入停用。
                        "busy", "today", "tomorrow", "yesterday", "morning",
                        "afternoon", "evening", "night", "hello", "hey",
                        "thanks", "thank", "please", "sorry", "welcome",
                        "good", "great", "fine", "okay", "sure", "yeah",
                        "well", "just", "really", "very", "quite", "maybe",
                        "guys", "everyone", "team", "here", "hows", "doing",
                        "how", "many", "much", "whats", "what", "have", "has",
                        "there", "some", "any", "show", "tell", "give", "list",
                        "check", "look", "looking", "left", "stock", "stocks",
                        "inventory", "count", "counts", "hand", "with", "from",
                        "that", "this", "them", "they", "your", "does", "did",
                        "still", "right", "available", "availability", "status",
                        "please", "quantity", "units", "unit", "level", "levels",
                        "number", "warehouse", "north", "central", "south",
                        "total", "currently", "remaining", "remain", "about",
                        "need", "want", "know", "the", "and", "for", "are",
                        "you", "got", "all", "our", "get", "see", "now", "item",
                        "items", "product", "products", "thing", "things",
                        # 功能詞（不是商品名）——沒收的話 'show my schedules'
                        #   會把 schedules 當成「庫裡沒有的商品」誠實回沒有
                        "schedule", "schedules", "scheduled", "alert", "alerts",
                        "rule", "rules", "report", "reports", "log", "logs",
                        "record", "records", "file", "files", "compare",
                        "comparison", "last", "past", "two", "months", "month",
                        "week", "weeks", "day", "days", "trend", "growth",
                        "decline", "audit", "trail", "history", "purchase",
                        "order", "orders", "movement", "movements", "transfer",
                        "transfers", "setting", "settings", "config", "safety",
                        "everything", "anything", "something", "help",
                        # 守衛第 10 輪：這些形容詞/動名詞被當成「庫裡沒有的
                        #   商品」誠實回沒有（'whats getting low' → 說沒有
                        #   getting low 這個商品）
                        "getting", "going", "running", "doing", "coming",
                        "expiry", "expire", "expires", "expiring", "value",
                        "values", "worth", "amount", "price", "prices",
                        "cost", "costs", "low", "high", "short", "out",
                        "empty", "full", "fine", "okay", "good", "bad",
                        # 劇情批 r5：追問副詞／最高級（同 _oov_stop，兩處要同步）
                        #   carry-over 正則認得這些詞，但句子在這層先被判 OOV
                        #   → 追問鏈斷在「查無此商品」。
                        "again", "lowest", "highest", "priciest", "worst",
                        "best", "cheaper", "pricier", "lower", "higher",
                        "then", "also", "too", "each", "same", "other",
                        "next", "first", "second", "third", "previous",
                        "earlier", "before", "after", "instead", "actually",
                        # r5：比較/門檻介系詞（同 _oov_stop，兩處要同步）
                        "below", "under", "above", "over", "than", "minimum",
                        "maximum", "threshold", "limit", "target",
                        # r5-voice：疑問詞（不收 why/when/where/who）
                        "which", "whose", "whom",
                        # r5-voice：動名詞/泛詞被當商品名（同 _oov_stop）
                        "happening", "happens", "happened", "work", "works",
                        "working", "mean", "means",
                    }
                    # 功能描述句不是「查不存在的商品」——它有明確目標，
                    #   由 descriptor_en 處理，這裡不可攔成 OOV
                    _is_desc_nx = False
                    try:
                        from descriptor_en import descriptor_hit_en as _dsc_nx
                        _is_desc_nx = bool(_dsc_nx(user_text))
                    except Exception:
                        pass
                    try:
                        if _is_desc_nx:
                            raise ValueError("descriptor句，跳過 OOV 判定")
                        import warehouse as _Wnx
                        import difflib as _dlnx
                        _nx_words = set()
                        for _itx in _Wnx.state().items:
                            for _wx in _re.split(r"[\s\-/]+", _itx["name"].lower()):
                                if len(_wx) >= 3 and not any(c.isdigit() for c in _wx):
                                    _nx_words.add(_wx)
                        try:
                            from alias_en import ALIAS_EN as _ALnx
                            for _kx in _ALnx:
                                for _wx in _kx.lower().split():
                                    if len(_wx) >= 3:
                                        _nx_words.add(_wx)
                        except Exception:
                            pass
                        _nx_keys = list(_nx_words)
                        # ── EN build：**已抽出的商品名**當額外接地證據（坑 3）──
                        #   `drip coffoe bags` 的 extractor 已經抽對
                        #   'Drip Coffee Bags 20pcs'（drip/bags 精確命中、
                        #   coffoe 模糊 0.83），但這裡逐 token 對**全主檔**用
                        #   cutoff 0.85 → coffoe/tiosue/persin/coaer 都落在
                        #   0.83 卡在門檻外 → 判成 OOV「查無此商品」，
                        #   **把已經抽對的正解擋掉**（守衛 inv 19 句大半是這樣掛的）。
                        #   ⚠️ 不放寬全域門檻（上次那樣做撞名詞誤配已回退）：
                        #   改成**只對已抽出的那個商品名**放寬 0.78——比對範圍從
                        #   60 商品的所有詞縮到單一商品名的幾個詞，撞名機率極低。
                        _nx_target_words = set()
                        try:
                            _nx_kw = _extract_sku_keyword(user_text)
                            if _nx_kw:
                                _nx_m = _Wnx.match_items(_nx_kw)
                                if _nx_m and _nx_m[0].get("score", 0) >= 4:
                                    for _wt in _re.split(r"[\s\-/]+",
                                                         _nx_m[0]["item"]["name"].lower()):
                                        _wt = _wt.strip(" ?.!,'\"")
                                        if len(_wt) >= 3 and not any(c.isdigit() for c in _wt):
                                            _nx_target_words.add(_wt)
                        except Exception:
                            pass
                        _unknown = []
                        # r17 #21/#24/#26/#30：黏字句（whatsthe/lowstock/
                        #   runninglow/whatcamein）——keyword 層有 unglue、
                        #   這裡掃**原句** token → 黏字被當「庫裡沒有的商品」。
                        #   先 unglue 再切。
                        for _tx in _re.split(r"[\s\-/]+", _en_unglue(user_text).lower()):
                            _tx = _tx.strip(" ?.!,'\"")
                            if len(_tx) < 3 or _tx in _NOEX_STOP or any(c.isdigit() for c in _tx):
                                continue
                            if _tx in _nx_words or _tx.rstrip("s") in _nx_words:
                                continue
                            if _dlnx.get_close_matches(_tx, _nx_keys, n=1, cutoff=0.85):
                                continue
                            # keyword 已抽出（= 上游已對這句有把握）時，陌生詞
                            #   對**主檔任一詞**達 0.80 也算錯字：
                            #   'Mosquuito Rpellent Soray' 抽出 Mosquito
                            #   Repellent Refill，soray 對 Refill 的詞接地不了，
                            #   但對主檔的 spray（同家族另一款）是 0.80。
                            #   前提是上游已抽出商品名，OOV 句（office chairs /
                            #   hair dryer）抽不出來，不受影響。
                            if _nx_target_words and _dlnx.get_close_matches(
                                    _tx, _nx_keys, n=1, cutoff=0.80):
                                continue
                            # 對「已抽出商品名」的詞放寬到 0.80（範圍極小才敢放寬）。
                            #   ⚠️ 門檻是**實測**切出來的，不是拍腦袋：
                            #     真錯字 coffoe→coffee / tiosue→tissue /
                            #       persin→person / soorts→sports 全落在 0.833
                            #     誤配 gaming→camping 是 0.769（0.78 會放它過關，
                            #       實測 'gaming chair' 誤配成 Folding Camping Chair）
                            #   → 0.80 剛好切開兩者。動這個數字前先跑守衛 noex 類。
                            if _nx_target_words and _dlnx.get_close_matches(
                                    _tx, list(_nx_target_words), n=1, cutoff=0.80):
                                continue
                            # 模糊層（含合成詞拆解 powerbank→Power Bank、
                            #   錯字 keyyboard→Keyboard）認得的就不是陌生詞
                            try:
                                if _en_fuzzy_keyword(_tx):
                                    continue
                            except Exception:
                                pass
                            # r12（TTS 基準批）：**帶撇號的縮寫一律不是商品名**。
                            #   ASR 產出 `what's in central warehouse…` →
                            #   `what's` 被當成「庫裡沒有的商品」→ 誠實回覆
                            #   「No item matching "what's"」。
                            #   這是結構性判準不是逐詞列舉：**主檔零商品名含撇號**，
                            #   所以帶撇號的 token 必然是縮寫（what's/where's/
                            #   there's/it's/don't…），逐個列進停用詞表列不完。
                            if "'" in _tx or "’" in _tx:
                                continue
                            # r13：黏字（商品詞+功能詞）——與上面 C1g-oov 同步
                            #   （坑 3：只修一層，下一層還是會擋掉）
                            _gl = None
                            for _sf in ("stock", "stocks", "inventory", "count",
                                        "counts", "qty", "quantity", "level",
                                        "levels"):
                                if len(_tx) > len(_sf) + 2 and _tx.endswith(_sf):
                                    _gl = _tx[:-len(_sf)]
                                    break
                            if _gl:
                                try:
                                    if (_gl in _nx_words or _gl.rstrip("s") in _nx_words
                                            or _en_fuzzy_keyword(_gl)):
                                        continue
                                except Exception:
                                    pass
                            _unknown.append(_tx)
                        # ── r16：**句中另有真商品詞 → 不是查無，是抽錯 keyword** ──
                        #   `is the earphone stock healthy` 句中明明有
                        #   earphone，卻抽出 healthy 當 keyword → 回
                        #   「查無 healthy 這個商品」（訪客問的根本不是商品）。
                        #   同批還有 situation / accurate / space / expensive /
                        #   overstocked / balanced / data source… 16 個抽象詞。
                        #   ⚠️ 逐詞加停用詞表**列不完**（這張表已累積 60+ 個、
                        #     每輪都在加）→ 用結構性判準取代：
                        #     句中若有**真商品詞**，那陌生詞就是形容詞/修飾語，
                        #     不該拿它去宣告「查無此商品」。
                        #   ⚠️ 不影響真 OOV：`do you have hair dryers` 句中
                        #     沒有任何真商品詞 → 照常誠實回覆查無。
                        if _unknown:
                            #   ⚠️ 要做**單複數正規化**：主檔是 `Earphones`
                            #     （複數），訪客常打 `earphone`（單數）→ 直接
                            #     用集合交集比不到（實測第一版就漏了這句）。
                            _nx_sing = {_w.rstrip("s") for _w in _nx_words}
                            _real_prod = set()
                            for _w in _re.split(r"[\s\-/]+", user_text):
                                _w = _w.strip(" ?.!,'\"").lower()
                                if len(_w) < 3:
                                    continue
                                if _w in _nx_words or _w.rstrip("s") in _nx_sing:
                                    _real_prod.add(_w)
                            #   ⚠️ **只在陌生詞是「形容詞式修飾語」時才放行**。
                            #     第一版寫成「句中有真商品詞就放行」→ 立刻回歸：
                            #     `how many chairs for the office` 的 chairs 在
                            #     主檔（Folding Camping Chair）→ office 被當修飾語
                            #     放行 → 回全店概覽，但守衛要求這句誠實查無。
                            #     （記憶教訓：放寬條件必製造誤配，要收窄範圍。）
                            #   判準：陌生詞若是**形容詞/狀態詞**（-y/-ed/-ic 結尾
                            #     或已知抽象詞），才視為修飾語；具體名詞
                            #     （office/microwave/bicycle）仍照常宣告查無。
                            _mod_only = _unknown and all(
                                _u in _ADJ_LIKE_OOV
                                or _re.search(r"(?:ic|ed|able|ible|ful|ous)$", _u)
                                for _u in _unknown)
                            if _real_prod and _mod_only:
                                log.info(f"[oov:noex] vid={vid} 句中有真商品詞 "
                                         f"{sorted(_real_prod)}、陌生詞 {_unknown} "
                                         f"是修飾語 → 不宣告查無")
                                _unknown = []
                        # ── EN build：陌生詞其實是「類別詞」→ 類別查詢，不是查無 ──
                        #   全系統 cat_zh_map 的鍵都是中文，英文類別句
                        #   （'all Electronics stock' / 'Daily Goods stock'）
                        #   一處也命中不了 → 類別詞被當商品名 → 這裡誠實拒絕，
                        #   整條類別查詢功能在英文版是壞的（6 類 5 個掛，
                        #   GUIDE_MSG 還教訪客這樣問）。改成先問類別表。
                        if _unknown:
                            _cat_nx = _category_from_en(user_text)
                            if _cat_nx:
                                # 確認陌生詞**全部**來自類別詞本身，不是「類別詞 +
                                #   真的不存在的商品」（'Electronics unicorn'
                                #   仍該誠實說沒有 unicorn）。
                                #   ⚠️ 類別詞可能是**多詞**（daily goods /
                                #   sporting goods）→ 不能逐詞 fullmatch（配不到），
                                #   改成把類別 pattern 從原句挖掉後，看還剩哪些
                                #   陌生詞。
                                _cat_strip = user_text
                                for _p in _CAT_WORDS_EN[_cat_nx]:
                                    _cat_strip = _re.sub(rf"\b{_p}\b", " ",
                                                         _cat_strip, flags=_re.I)
                                _cat_left = {
                                    _w.strip(" ?.!,'\"").lower()
                                    for _w in _re.split(r"[\s\-/]+", _cat_strip)
                                    if len(_w.strip(" ?.!,'\"")) >= 3
                                }
                                if not (set(_unknown) & _cat_left):
                                    log.info(f"[oov:noex→cat] vid={vid} 英文類別詞 "
                                             f"{_unknown} → query_inventory"
                                             f"{{category:{_cat_nx}}}")
                                    func_name = "query_inventory"
                                    func_args = {"category": _cat_nx}
                                    _unknown = []
                        # ── EN build：陌生詞其實是「倉庫比較」的虛詞 → compare_help ──
                        #   r8 抓到：'which warehouse is better' 的 clf 判**對**
                        #   （compare_warehouses conf=0.80），但 LLM 幻覺成
                        #   list_low_stock → C3e 轉概覽 → 這裡把虛詞 'better'
                        #   當商品名，回「No item matching "better"」。
                        #   比較意圖缺 slot 的正解是 compare_help（13182 已有
                        #   同一份文案，但只有 LLM 主動吐 __help__ 才走得到）。
                        #   接地條件從嚴（同坑 8：放寬英文分支必製造誤配）：
                        #     ①句中要有 warehouse/wh 這種**比較對象詞**
                        #     ②要有比較語（compare / which / better / more…）
                        #     ③陌生詞**全部**是虛詞，不含真的查無的商品名
                        #       （'which warehouse has more unicorns' 仍要誠實說沒有）
                        if _unknown and _is_mostly_english(user_text):
                            _ut_cmp = user_text.lower()
                            _CMP_FILLER = {"better", "best", "worse", "worst", "bigger",
                                           "biggest", "larger", "largest", "smaller",
                                           "more", "most", "less", "fewer", "higher",
                                           "lower", "stronger", "good", "bad"}
                            if (_re.search(r"\b(?:warehouses?|wh|sites?|locations?)\b", _ut_cmp)
                                    and _re.search(r"\b(?:compare|comparison|versus|vs|"
                                                   r"which|who|whose|better|best|more|most)\b",
                                                   _ut_cmp)
                                    and set(_unknown) <= _CMP_FILLER):
                                _cmp_msg = ("What do you want to compare between two "
                                            "warehouses?\n"
                                            'Try: "which has more stock north or south" '
                                            'or "compare central and south by turnover"')
                                log.info(f"[oov:noex→cmp] vid={vid} 比較意圖缺 slot "
                                         f"{_unknown} → compare_help")
                                for ch in _cmp_msg:
                                    await send({"type": "token", "text": ch})
                                    await asyncio.sleep(_TK_DELAY.get() * 1.5)
                                await send({"type": "done", "result": {
                                    "ok": True, "summary": _cmp_msg,
                                    "view": "compare_help", "data": {}}})
                                continue
                        if _unknown:
                            _nx_name = " ".join(_unknown)
                            log.info(f"[oov:noex] vid={vid} 庫中無此商品 {_nx_name!r} → 誠實回覆")
                            await send({"type": "done", "result": {
                                "ok": True, "view": "clarify",
                                "summary": (f'No item matching "{_nx_name}" is in '
                                            'the warehouse. Please check the name, '
                                            'or say "item list" to see everything '
                                            'we carry.'),
                                "data": {"question": f'"{_nx_name}" not found',
                                         "options": [], "hint": ""},
                            }})
                            continue
                    except Exception:
                        pass

                # ── r24：**keyword 本身就是類別名** → 類別查詢，別當商品歧義 ──
                #   `sports stock` 六個類別裡**只有它掛**：sports 同時出現在
                #   4 個商品名裡（Sports Drink / Sports Bra / Sports Towel /
                #   Sports Compression Sleeve）→ 先當商品名比對命中 4 個 →
                #   反問「你要哪一個」。但訪客講 `sports stock` 明顯是問類別。
                #   ⚠️ 只在 keyword **整個就是類別詞**時才轉（`sports towel`
                #     仍要當商品查）——用 `_category_from_en` 對 keyword 本身
                #     比對，不是對整句。
                if (func_name == "query_inventory" and func_args.get("keyword")
                        and not func_args.get("category")
                        and _is_mostly_english(user_text)):
                    #   ⚠️ 第一版用 `_category_from_en(keyword)` 太寬——
                    #     keyword='sports towel' 也會命中 sports → 毛巾查詢
                    #     變成類別總覽（實測回歸）。改成**整個 keyword 必須
                    #     just 是類別詞**：先確認它對不到任何商品，才轉類別。
                    _kw_raw = str(func_args["keyword"]).strip()
                    _kw_cat = _category_from_en(_kw_raw)
                    if _kw_cat:
                        try:
                            import warehouse as _W_ck
                            _ck_m = _W_ck.match_items(_kw_raw)
                            # ⚠️ 判準是**分數分布**不是絕對分數（實測數據）：
                            #     sports        → 11, 11, 11（平手＝類別詞，
                            #                     只是撞到多個商品名的共同詞）
                            #     sports towel  → 16, 6（差 10＝有明確贏家，
                            #                     是商品名）
                            #     sports drink  → 16, 6（同上）
                            #   用絕對門檻會兩邊都錯（8 會擋掉 sports、
                            #   12 會放行 sports towel）。改看「第一名是否
                            #   明顯勝出」。
                            if len(_ck_m) >= 1:
                                _s1 = _ck_m[0].get("score", 0)
                                _s2 = _ck_m[1].get("score", 0) if len(_ck_m) > 1 else 0
                                if _s1 >= 8 and _s1 - _s2 >= 4:
                                    _kw_cat = None   # 有明確贏家＝商品名
                        except Exception:
                            pass
                    if _kw_cat:
                        log.info(f"[cat-kw] vid={vid} keyword "
                                 f"{func_args['keyword']!r} 本身是類別詞 → "
                                 f"category={_kw_cat}")
                        func_args = {k: v for k, v in func_args.items()
                                     if k != "keyword"}
                        func_args["category"] = _kw_cat

                # ── OOV 偵測：keyword 不在 SKU 清單時推測候選 ──
                oov = _detect_oov(func_name, func_args)
                if oov:
                    if oov["auto_fix"]:
                        # 靜默修復：直接換 keyword，繼續執行，回應加提示
                        log.info(f"[oov:auto_fix] vid={vid} {oov['original_keyword']!r} → {oov['fixed_keyword']!r} (score={oov['score']:.0f})")
                        func_args["keyword"] = oov["fixed_keyword"]
                        # 把修復提示帶入後續 result，由工具回傳後前端顯示
                        # （fixed_keyword 為空時不加提示——「昨天進了什麼貨」的
                        # 雜訊 kw 被修成空字串曾顯示「已自動對應至「」」）
                        _oov_hint = (f"(auto-matched to \"{oov['fixed_keyword']}\") "
                                     if oov.get("fixed_keyword") else "")
                    else:
                        # 給選單：回傳 clarify，等使用者選
                        log.info(f"[oov:clarify] vid={vid} keyword={oov['original_keyword']!r} candidates={oov['options']}")
                        await send({"type": "done", "result": {
                            "ok": True,
                            "summary": oov["question"],
                            "view": "clarify",
                            "data": oov,
                        }})
                        continue
                else:
                    _oov_hint = None

                # Context carry-over：追問句補 keyword/warehouse（按訪客 vid 隔離）
                _pre_followup_func = func_name
                func_name, func_args = _resolve_followup(vid, user_text, func_name, func_args)

                # ── 🚨 EN build（劇情批 r4 S7）：**config set 的指代必須接對商品**
                #   實測危險破口：'wireless mouse stock' 之後說
                #   'set its safety stock to 100' → 系統改了 **[all items] 180 筆**
                #   （its 沒接到 context，一路掉到「全庫套用」）。
                #   寫入類誤傷全庫比答錯嚴重得多 → 有指代詞卻沒有明確商品時：
                #     ①用 context 的 last_sku ②接不到就**反問**，絕不套用全庫。
                if (func_name == "manage_config"
                        and func_args.get("action") == "set"
                        and _is_mostly_english(user_text)
                        and not str(func_args.get("item") or "").strip()):
                    _pron = _re.search(r"\b(?:its|it|that|this|the same)\b",
                                       user_text, _re.I)
                    if _pron:
                        _cfg_sku = (_ctx_by_vid.get(vid) or {}).get("last_sku")
                        if _cfg_sku:
                            func_args["item"] = _cfg_sku
                            log.info(f"[cfg-pron] 指代 {_pron.group(0)!r} → "
                                     f"item={_cfg_sku!r}（避免誤套全庫）")
                        else:
                            log.info(f"[cfg-pron] 指代但無 context → 反問: {user_text!r}")
                            _cp_msg = ('Which item should I change? Say the item '
                                       'name, e.g. "set safety stock for yoga mat '
                                       'to 100".')
                            for ch in _cp_msg:
                                await send({"type": "token", "text": ch})
                                await asyncio.sleep(_TK_DELAY.get())
                            await send({"type": "done", "result": {
                                "ok": True, "view": "clarify", "summary": _cp_msg,
                                "data": {"question": _cp_msg,
                                         "options": ["item list"], "hint": ""}}})
                            continue
                # ⚠️ 坑 4：carry-over 改了 func 就要標 hard，否則下游 C18
                #   （clf 仲裁）會拿 clf 的原判斷蓋回去。實測
                #   'by how many units' 被 carry-over 正確導向 compare_periods，
                #   卻被 clf(query_inventory conf=1.00) 覆蓋回全店概覽——
                #   clf 看的是**單句**，看不到上一輪的 context，這種句子
                #   本來就只有 carry-over 判得準。
                _ctx_hard_followup = (func_name != _pre_followup_func)
                if _ctx_hard_followup:
                    _hard = True
                corrected_call = f"{func_name}({func_args})"

                # ── C18：clf mismatch 檢查（hard_corrected 時不蓋過）──
                mismatch, clf_intent, clf_conf = intent_clf.check_mismatch(user_text, func_name)
                # ⚠️ 匯出定案保護（2026-08-04）：Cmp2/Pre-C10 已把索取式匯出句
                #   定案成 run_script，clf 對這種句子只會判 query_movement
                #   （它看不出「要檔案」）→ 不讓 C18 拿 clf 蓋回去。
                #   專屬條件不借用 _hard（坑 14：借大旗標會關掉整道閘門）。
                _c18_exp_keep = (func_name == "run_script"
                                 and (_en_export_intent(user_text)
                                      # health check 直達同樣保護（2026-08-04：
                                      #   clf=query_inventory(1.00) 曾蓋回,還把
                                      #   script_name 帶進 query_inventory → 坑 16
                                      #   「忽略未知參數」信號燈）
                                      or bool(_re.search(
                                          r"\bhealth\s+check(?:up)?\b",
                                          user_text, _re.I))
                                      # 跑盤點（含動名詞禮貌形）同樣保護
                                      or bool(_re.search(
                                          r"\b(?:run(?:ning)?|do(?:ing)?|"
                                          r"perform(?:ing)?)\b.{0,24}"
                                          r"\b(?:stock\s*audit|stocktake|"
                                          r"stock\s*(?:take|count))\b",
                                          user_text, _re.I))))
                # ⚠️ movement 定案保護（2026-08-04）：'south warehouse movements
                #   yesterday' 模型判對 query_movement（ctx 也補好了），clf 卻
                #   高信心 compare_warehouses(0.91) 蓋回去 → 答倉庫排名答非所問。
                #   句含 movement 名詞（查詢形，不收 received/shipped 寫入動詞）
                #   且無比較記號時，clf 的 compare 不得覆蓋。只擋 compare 這一種
                #   collision——clf → run_script（匯出）等其他校正照常有效。
                _c18_mv_keep = (func_name == "query_movement"
                                and clf_intent == "compare_warehouses"
                                and bool(_re.search(
                                    r"\b(?:movements?|inbound|outbound|"
                                    r"shipments?|in\s*(?:and|/)\s*out)\b",
                                    user_text, _re.I))
                                and not _re.search(
                                    r"\b(?:compare|comparison|versus|vs\.?|"
                                    r"difference|which\s+warehouse|"
                                    r"rank(?:ing)?s?|most|least|busiest)\b",
                                    user_text, _re.I))
                if _c18_mv_keep and mismatch:
                    log.info(f"[C18-mvkeep] clf={clf_intent}({clf_conf:.2f}) "
                             f"不蓋 query_movement（句含 movement 名詞、無比較記號）")
                    # 順手補倉別：這類句常見 'south warehouse movements …'，
                    #   fresh 連線下 LLM 沒抽 warehouse → 回全倉答非所「倉」。
                    #   只在句中恰好提到一個倉、且 args 未設時才填（兩倉並列不猜）。
                    if func_args.get("warehouse") in (None, "", "all"):
                        _mvk_whs = set(_re.findall(r"\b(north|central|south)\b",
                                                   user_text, _re.I))
                        if len(_mvk_whs) == 1:
                            func_args["warehouse"] = _mvk_whs.pop().lower()
                            log.info(f"[C18-mvkeep] 補 warehouse="
                                     f"{func_args['warehouse']!r}（句面倉別）")
                if mismatch and not _hard and not _en_admin_hard \
                        and not _c18_exp_keep and not _c18_mv_keep \
                        and clf_intent != "unknown":
                    log.info(f"[C18] clf={clf_intent}({clf_conf:.2f}) vs model={func_name} → 校正")
                    func_name = intent_clf.LABEL_TO_FUNC.get(clf_intent, clf_intent)
                    # ⚠️ 轉成 run_script 時**必須補 script_name**（2026-08-03）：
                    #   `export movements yesterday` → clf 判 run_script(1.00) 正確，
                    #   但舊 func_args 是 query_movement 的（period/direction），
                    #   沒有 script_name ⇒ 下游找不到腳本、走「不在白名單」反問。
                    if func_name == "run_script" and not func_args.get("script_name"):
                        _c18_exp = _re.search(r"匯出|輸出|下載|導出|\bexport\b|\bdownload\b",
                                              user_text, _re.I)
                        # ⚠️ 匯出但**沒講期間** → 直接回期間反問（2026-08-03）。
                        #   不能只是「不補腳本名」——那會落到「不在白名單」的
                        #   通用反問（選項是三支腳本），不是我們要的期間選單。
                        _c18_has_period = _re.search(
                            r"\b(?:today|yesterday|this\s+week|last\s+week|this\s+month|"
                            # ⚠️ 與 _exp_has_period 同步（2026-08-04）
                            r"last\s+month|(?:last|past|previous)\s+(?:week|month|quarter)|"
                            r"this\s+quarter|last\s+3\s+months|past\s+3\s+months|"
                            r"past\s+\d+|last\s+\d+|recent\s+\d+|\d+\s*days?)\b|"
                            r"今天|今日|昨天|昨日|前天|本週|這週|上週|本月|這個月|上個月|"
                            r"最近\s*\d+\s*天|過去\s*\d+\s*天|前\s*[0-9零一二三四五六七八九十兩]+\s*天", user_text, _re.I)
                        if _c18_exp and not _c18_has_period:
                            _c18_zh = not _re.search(r"[a-z]", user_text, _re.I)
                            _c18_clar = ({
                                "question": "要匯出哪個期間的進出紀錄？",
                                "options": ["昨天", "最近 7 天", "最近 30 天", "本月"],
                                "actions": ["匯出昨天的進出紀錄", "匯出最近 7 天的進出紀錄",
                                            "匯出最近 30 天的進出紀錄", "匯出本月的進出紀錄"],
                                "hint": "進出紀錄匯出"} if _c18_zh else {
                                "question": "Which period do you want to export?",
                                "options": ["Yesterday", "Last week", "Last month", "Last quarter (3 months)"],
                                "actions": ["export movements yesterday",
                                            "export movements last week",
                                            "export movements last month",
                                            "export movements last quarter"],
                                "hint": "Movement log export"})
                            log.info(f"[C18] 匯出無期間 → 期間反問")
                            await send({"type": "done", "result": {
                                "ok": True, "summary": _c18_clar["question"],
                                "view": "clarify", "data": _c18_clar}})
                            continue
                        func_args = {"script_name": (
                            ("export movements" if _re.search(r"[a-z]", user_text, _re.I)
                             else "匯出進出記錄") if _c18_exp else user_text),
                            "_period_text": user_text}
                        log.info(f"[C18] run_script 補 script_name="
                                 f"{func_args['script_name']!r}")
                    # C18 改了 func_name 後，舊 func_args 是照舊 func_name 的參數格式
                    # （例如 set_alert 的 condition/target），直接沿用到新功能會
                    # 完全對不上、甚至讓工具函式收到空必填參數而報錯。轉成
                    # search_log / manage_config 都要照各自參數格式重新組。
                    if func_name == "search_log":
                        from tools_v2 import _RCA_NOISE, _RCA_GENERIC
                        _raw = user_text
                        for _nz in _RCA_NOISE: _raw = _raw.replace(_nz, "")
                        for _gz in _RCA_GENERIC: _raw = _raw.replace(_gz, "")
                        _raw = _raw.strip()
                        func_args = {"keyword": _raw if _raw else func_args.get("keyword", "")}
                    elif func_name == "manage_config":
                        # C18 把 set_alert（或其他功能）改判成 manage_config 時，
                        # 舊參數（如 set_alert 的 condition/target）沒有 key/value
                        # 可用，靠 _CONFIG_KEY_WORDS 從 user_text 重新抽（同 C9 邏輯，
                        # 2026-07-02 實測「北倉安全水位提高20」抓到：clf 判斷正確
                        # 但 func_args 沒跟著轉換，manage_config 收到空 key 直接報錯）。
                        _c18_action = "set" if any(w in user_text for w in _CONFIG_SET_WORDS) and not (
                            any(w in user_text for w in _CONFIG_READ_CUES)
                            and _extract_config_value(user_text) is None) else "read"
                        _c18_key = max((w for w in _CONFIG_KEY_WORDS if w in user_text), key=len, default="安全庫存")
                        func_args = {"action": _c18_action, "key": _c18_key}
                        for _zh, _en in _WH_ZH_MAP.items():
                            if _zh in user_text:
                                func_args["warehouse"] = _en
                                break
                        if _c18_action == "set":
                            _c18v = _extract_config_value(user_text)
                            if _c18v is not None:
                                func_args["value"] = _c18v
                        # 商品名縮小影響範圍（同 C11c，conv100-r5：C18 重組漏 item
                        # 讓「瑜珈墊安全庫存加20」變全部商品）
                        _c18_item = _config_item_kw(user_text)
                        if _c18_item:
                            func_args["item"] = _c18_item
                    elif func_name == "compare_warehouses":
                        # C18 在所有 compare 守衛之後執行，轉 compare 要自帶守衛：
                        # 帶真商品名 → 查分倉庫存；否則從原句重建倉名/指標
                        # （「無線滑鼠北倉中倉哪邊多」曾被 clf 0.98 蓋成北/南總量比較，conv100-r10）
                        import warehouse as _W_c18cmp
                        _c18_kw2 = _extract_sku_keyword(user_text)
                        _c18_m2 = _W_c18cmp.match_items(_c18_kw2) if _c18_kw2 else []
                        if _c18_m2 and _c18_m2[0].get("score", 0) >= 3:
                            func_name = "query_inventory"
                            func_args = {"keyword": _c18_kw2}
                        else:
                            _pos18 = []
                            # EN build：倉名同時認中文與英文（原只認中文 → 英文句
                            #   'compare central and south by value' 永遠抓不到倉名、
                            #   退化成 all/all 全店比較，倉名資訊全毀）
                            for _zh18, _en18 in (("北倉", "north"), ("北區", "north"),
                                                 ("中倉", "central"), ("中區", "central"),
                                                 ("南倉", "south"), ("南區", "south"),
                                                 ("north", "north"), ("central", "central"),
                                                 ("south", "south")):
                                _p18 = user_text.lower().find(_zh18.lower())
                                if _p18 >= 0 and _en18 not in [e for _, e in _pos18]:
                                    _pos18.append((_p18, _en18))
                            _seq18 = [e for _, e in sorted(_pos18)]
                            if len(_seq18) == 2:
                                func_args = {"warehouse_a": _seq18[0], "warehouse_b": _seq18[1]}
                            else:
                                func_args = {"warehouse_a": "all", "warehouse_b": "all",
                                             "metric": "item_count"}
                            _ut18 = user_text.lower()
                            if "週轉" in user_text or "turnover" in _ut18:
                                func_args["metric"] = "turnover"
                            elif any(w in user_text for w in ("價值", "總值", "金額")) \
                                    or any(w in _ut18 for w in ("value", "worth")):
                                func_args["metric"] = "stock_value"
                    corrected_call = f"[C18]{func_name}({func_args})"
                if corrected_call != raw_call:
                    log.info(f"[trace] vid={vid} corrected: {raw_call} → {corrected_call}")

                # ── C5: __help__ → 引導訪客補 slot ──
                if func_name == "__help__":
                    reason = func_args.get("reason", "")
                    if reason == "compare_missing_slot":
                        msg = ("What do you want to compare between two warehouses?\n"
                               'Try: "which has more stock north or south" or '
                               '"compare central and south by turnover"')
                    else:
                        msg = "Please add a bit more detail and try again"
                    for ch in msg:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get() * 1.5)
                    await send({"type": "done", "result": {
                        "ok": True, "summary": msg, "view": "compare_help", "data": {},
                    }})
                    continue

                log.info(f"[trace] vid={vid} call={corrected_call}")
                await push_display({"type": "trace", "stage": "parsed",
                                    "function": func_name, "args": func_args})

                # ── dispatch 前最後防線：keyword 是類別名 → 轉 category（WS 版）──
                _CAT_FB = {
                    "電子產品": "electronics", "家電廚具": "appliance_kitchen",
                    "食品飲料": "food_beverage", "日用品": "daily_goods",
                    "服飾": "apparel", "運動用品": "sports",
                }
                if func_name == "query_inventory":
                    _dkw = (func_args.get("keyword") or "").strip()
                    _dcat = func_args.get("category", "")
                    _dkw_clean = _dkw
                    for _pfx in ("北區倉的", "中區倉的", "南區倉的", "北倉的", "中倉的", "南倉的",
                                 "北區的", "中區的", "南區的", "北部的", "中部的", "南部的"):
                        if _dkw_clean.startswith(_pfx):
                            _dkw_clean = _dkw_clean[len(_pfx):].strip()
                            break
                    for _sfx in ("類別", "庫存查詢", "庫存", "查詢", "類", "詢"):
                        if _dkw_clean.endswith(_sfx) and len(_dkw_clean) > len(_sfx) + 1:
                            _dkw_clean = _dkw_clean[:-len(_sfx)].strip()
                            break
                    if _dkw and _dcat not in VALID_CATEGORIES:
                        for _zh, _en in sorted(_CAT_FB.items(), key=lambda x: -len(x[0])):
                            if _zh in _dkw_clean or _dkw_clean in _zh:
                                log.info(f"[dispatch-ws] 類別轉換: kw={_dkw!r} → category={_en}")
                                func_args = {k: v for k, v in func_args.items() if k != "keyword"}
                                func_args["category"] = _en
                                break
                    elif _dkw and _dcat in VALID_CATEGORIES:
                        for _zh in _CAT_FB:
                            if _zh in _dkw_clean or _dkw_clean in _zh:
                                log.info(f"[dispatch-ws] 關鍵字是類別名，清掉 kw={_dkw!r}")
                                func_args = {k: v for k, v in func_args.items() if k != "keyword"}
                                break

                # ── dispatch-ws：item_create 分步流程（per-vid）──
                if _item_create_state_ws.get(vid, {}).get("active"):
                    # EN build：原本寫死 == "取消"，英文訪客講 cancel / never mind
                    #   出不來＝卡在新增流程裡（_ABORT_WORDS 已含英文，改用共用表）
                    _ic_t = user_text.strip().lower().rstrip(" .!?")
                    if (user_text.strip() == "取消"
                            or _ic_t in {w.lower() for w in _ABORT_WORDS}):
                        _item_create_state_ws.pop(vid, None)
                        await send({"type": "token", "text": "Item creation cancelled."})
                        # r14：同 guide 那處——done 要帶 summary，否則讀 summary
                        #   的地方（歷史/複製/測試判定）拿到空字串
                        await send({"type": "done", "result": {
                            "ok": True, "view": "item_cancelled",
                            "summary": "Item creation cancelled.", "data": {}}})
                        continue
                    import tools_v2 as _tv2_item_ws
                    st2 = _item_create_state_ws.get(vid, {})
                    kwargs2 = {**{k: v for k, v in st2.items() if k in ("step", "name", "category", "price", "safety", "stock_north", "stock_central", "stock_south")}, "raw_text": ""}
                    if st2["step"] == 1: kwargs2["name"] = user_text
                    elif st2["step"] == 2: kwargs2["category"] = user_text
                    elif st2["step"] == 3:
                        parts = user_text.replace("，", ",").split(",")
                        if len(parts) >= 2: kwargs2["price"] = parts[0].strip(); kwargs2["safety"] = parts[1].strip()
                        else: kwargs2["price"] = user_text
                    elif st2["step"] == 4:
                        if "跳過" in user_text or any(_sk in user_text.lower() for _sk in ("skip", "none", "zero", "no stock", "leave it")): kwargs2["stock_north"] = kwargs2["stock_central"] = kwargs2["stock_south"] = "0"
                        else:
                            for part in user_text.replace("，", ",").split(","):
                                p = part.strip()
                                if "北" in p: kwargs2["stock_north"] = p.replace("北", "").strip()
                                elif "中" in p: kwargs2["stock_central"] = p.replace("中", "").strip()
                                elif "南" in p: kwargs2["stock_south"] = p.replace("南", "").strip()
                    result = _tv2_item_ws.create_item_collect(**kwargs2)
                    if result.get("view") == "item_confirm":
                        _item_create_state_ws.pop(vid, None)
                    else:
                        d = result.get("data", {})
                        _new_st = {k: v for k, v in d.items() if k in ("step", "name", "category", "price", "safety", "stock_north", "stock_central", "stock_south")}
                        _new_st["active"] = True
                        _item_create_state_ws[vid] = _new_st
                    for ch in result.get("summary", ""):
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get() * 1.5)
                    await send({"type": "done", "result": result})
                    continue

                # ── dispatch-ws：新增商品 keyword 攔截（per-vid）──
                _create_item_kws_ws = ("新增商品", "建立商品", "加一個商品", "新增一個", "加入商品", "增加商品", "新建商品",
                          # EN build：英文新增商品觸發詞（原表全中文 → 英文訪客
                          #   打 "add item" 完全進不了流程，還被守門員擋成 rejected）
                          "add item", "add a item", "add an item", "add new item",
                          "add a new item", "create item", "create a item",
                          "create an item", "create a new item", "new item",
                          "new product", "add product", "add a product",
                          "register item", "register a new item")
                if any(w in user_text for w in _create_item_kws_ws):
                    import tools_v2 as _tv2_ci
                    log.info(f"[dispatch-ws] 新增商品攔截: {user_text!r}")
                    raw = user_text
                    for kw in _create_item_kws_ws: raw = raw.replace(kw, "").strip()
                    result = _tv2_ci.create_item_collect(step=1, raw_text=raw) if raw else _tv2_ci.create_item_start()
                    for ch in result.get("summary", ""):
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get() * 1.5)
                    await send({"type": "done", "result": result})
                    _new_st = {k: v for k, v in result.get("data", {}).items()
                               if k in ("step", "name", "category", "price", "safety", "stock_north", "stock_central", "stock_south")}
                    if result.get("view") != "item_confirm":
                        _new_st["active"] = True
                        _item_create_state_ws[vid] = _new_st
                    else:
                        _item_create_state_ws.pop(vid, None)
                    continue

                # ── dispatch-ws：庫存排行 / 口語 pattern 攔截 ──
                # 「哪個」單字太寬，會誤傷「北倉跟南倉哪個庫存多」這類倉庫比較句（compare_warehouses）。
                # 判別特徵：兩倉比較句一定會提到「倉」，單一商品排行榜問句不會。
                _stock_rank_kws_ws = ("哪個", "哪個東西", "庫存最多", "數量最多", "哪個最多", "存貨最多", "東西最多",
                                      # r20：「倉庫裡最多的商品是什麼」
                                      "最多的商品", "最多的東西", "最多的貨")
                if (any(w in user_text for w in _stock_rank_kws_ws)
                        # r25：「哪個商品乏人問津」曾被這裡蓋掉 C4 的 slow 修正（r6 老雷）
                        # → 排除表複用完整熱銷/滯銷集合，不再手列
                        and not any(w in user_text for w in
                                    _HOT_INTENT_WORDS_HOT + _HOT_INTENT_WORDS_SLOW)
                        # r27：「缺最兇的是哪個」曾被搶成庫存排行（相反誤導）
                        # → 缺貨集合也複用
                        and not any(w in user_text for w in _LOW_STOCK_INTENT_WORDS)
                        and not any(w in user_text for w in ("熱銷", "賣", "排行", "hot", "滯銷",
                                                              "業績", "冠軍", "銷", "墊底",
                                                              # r16：「哪個商品最快斷貨」是缺貨
                                                              # 警示不是庫存排行（曾回庫存最多
                                                              # TOP=完全相反的誤導）
                                                              "斷貨", "缺貨", "快斷", "沒貨",
                                                              # r17：「安全庫存最高的是哪個商品」
                                                              # 是設定值排名不是庫存排行（C9d 已轉
                                                              # config read，這裡不可蓋回）
                                                              "安全庫存", "安全水位", "警戒"))
                        # 「北區跟南區哪個庫存比較多」是倉庫比較；但裸「倉」排除太寬——
                        # 「倉庫裡最多的商品是什麼」曾被放走回熱銷榜（r20）→
                        # 收斂成「哪個倉/哪邊」或兩倉名才算倉庫比較
                        and not any(w in user_text for w in ("哪個倉", "哪一倉", "哪邊",
                                                              "北區", "中區", "南區"))
                        and len({z for z in ("北", "中", "南")
                                 if any(z + s in user_text for s in ("倉", "區"))}) < 2):
                    log.info(f"[dispatch-ws] 庫存排行攔截: {user_text!r} → list_hot_items(stock)")
                    func_name = "list_hot_items"
                    func_args = {"rank_type": "stock"}

                # ── dispatch-ws：compare_warehouses 清理非法參數 ──
                if func_name == "compare_warehouses":
                    func_args = {k: v for k, v in func_args.items()
                                 if k in ("warehouse_a", "warehouse_b", "metric")}
                    if "warehouse_a" not in func_args: func_args["warehouse_a"] = "north"
                    if "warehouse_b" not in func_args: func_args["warehouse_b"] = "south"

                # ── 執行前清理 keyword 前後綴雜訊 ──
                _kw_f2 = "keyword" if "keyword" in func_args else ("target" if "target" in func_args else None)
                if _kw_f2 and func_args.get(_kw_f2):
                    _raw2 = func_args[_kw_f2]
                    _pfx2 = ("幫我查","幫我看","幫我找","查看","查詢","查一下","看看","有沒有","有","是","了","也","還","的")
                    _sfx2 = ("有多少","剩多少","有幾個","剩幾個","有幾","剩幾","有","剩","的","嗎","啊","呢","吧","了","喔")
                    _ck2 = _raw2
                    for p in sorted(_pfx2, key=len, reverse=True):
                        if _ck2.startswith(p) and len(_ck2) > len(p) + 1:
                            _ck2 = _ck2[len(p):]; break
                    for s in sorted(_sfx2, key=len, reverse=True):
                        if _ck2.endswith(s) and len(_ck2) > len(s) + 1:
                            _ck2 = _ck2[:-len(s)]; break
                    if len(_ck2) < 2:
                        _ck2 = ""
                    if _ck2 != _raw2:
                        log.info(f"[dispatch-ws] keyword 清理: 「{_raw2}」→「{_ck2}」")
                        func_args = {**func_args, _kw_f2: _ck2}
                # ── 意圖閘門：LLM 對閒聊句幻覺出寫入/複雜工具時擋下（第18輪）──
                # ⚠️ EN build（r4 S3）：**carry-over 的硬決定不受意圖閘門管**。
                #   'any stock discrepancies' → 'which item is the worst' 是
                #   追問同一份 RCA 清單，carry-over 已正確導向 search_log，
                #   但該句沒有 RCA 意圖詞（意圖在**上一輪**）→ 被閘門 rejected。
                #   閘門是為了擋「LLM 對閒聊句亂輸出工具」，而 carry-over 是
                #   看得到 context 的判斷，比單句閘門更可信。
                #   ⚠️ 收緊（複驗回歸）：`_hard` 涵蓋太廣（校正層各處都會設），
                #   用它豁免等於**把整道意圖閘門關掉** → clf 誤判
                #   'ok back to the earphones'→search_log(0.94) 直接放行回 RCA
                #   （舊版是被這道閘門擋下的）。改成只認
                #   **carry-over 剛改過 func** 這一種情況。
                if not _tool_intent_ok(func_name, user_text) \
                        and not _ctx_hard_followup:
                    # reject 前先試降級救援（口語前綴害 LLM 輸出錯 function，RPI5 v21）
                    _rescue = _intent_guard_rescue(func_name, func_args, user_text)
                    if not _rescue and _re.search(r'[進出]的?貨', user_text):
                        # r44：「昨天出的貨是哪些」LLM 曾投 related(幻覺kw) 被 gate 拒
                        # → 降級成當期進出統計（方向/期間從原句抽）
                        _g44_args = {"direction": "out" if "出" in user_text else "in",
                                     "period": ("yesterday" if "昨天" in user_text else
                                                "today" if "今天" in user_text else "this_week")}
                        log.info(f"[gate-rescue r44] 進出貨句 → query_movement {_g44_args}")
                        _rescue = ("query_movement", _g44_args)
                    if not _rescue and _text_has_item_name(user_text):
                        # r43：句帶真商品/通稱（「帽子有哪些」clf 誤判 list_files 曾被拒）
                        # → 降級成該商品庫存查詢，不冤枉正經查詢句
                        _g43_kw = _extract_sku_keyword(user_text)
                        if _g43_kw:
                            log.info(f"[gate-rescue r43] {func_name} 缺意圖詞但帶商品 → query_inventory kw={_g43_kw!r}")
                            _rescue = ("query_inventory", {"keyword": _g43_kw})
                    # r92（user 定調「引導」）：講的是**帳對不上**但沒指名商品
                    #   （「實際比帳面多了五十個」）→ 這是正經的盤點問題，不該
                    #   當搗蛋拒絕。問清楚是哪個商品，訪客補完就能進 RCA。
                    if not _rescue and _has_rca_word(user_text):
                        log.info(f"[gate-rescue r92] 帳務差異缺商品名 → clarify: {user_text!r}")
                        await push_display({"type": "trace", "stage": "clarify",
                                            "reason": "rca_no_item"})
                        # 選項用「動作型」而非寫死商品名——展場資料會變動，
                        # 且訪客要查的商品未必在任何固定清單裡（同 6150 行風格）。
                        # EN build：_has_rca_word 已補英文（who moved / count off /
                        #   dont match…）→ 英文句到得了這裡，訊息與選項要英文。
                        if _is_mostly_english(user_text):
                            _rca_ask = {
                                "ok": True, "view": "clarify",
                                "summary": ("Which item's count is off? Tell me the item "
                                            "name and I will trace its movements."),
                                "question": "Which item's records don't match?",
                                "options": ["which items have anomalies",
                                            "purchase reconciliation issues",
                                            "which items are running low"],
                                "hint": 'You can just say the item name, e.g. "the mouse count is off"',
                                "data": {},
                            }
                        else:
                            _rca_ask = {
                                "ok": True, "view": "clarify",
                                "summary": "帳對不上要查哪個商品呢？說商品名我幫你追進出紀錄。",
                                "question": "是哪個商品的帳對不上？",
                                "options": ["哪些商品有異常", "採購對帳異常", "哪些商品快缺貨"],
                                "hint": "直接說商品名也可以，例如「滑鼠的帳對不上」",
                                "data": {},
                            }
                        await send({"type": "done", "result": _rca_ask})
                        continue
                    # ── EN build（劇情批 r1）：有**管理動詞**但工具判錯的句子
                    #   不是搗蛋，該問清楚而不是拒絕。實測被誤拒的：
                    #     'change central to 80'（LLM 判成 query_movement）
                    #     'move 20 from the fullest to the emptiest'（判成 search_log）
                    #   訪客講的是真實操作，只是少了商品名/設定項 → clarify
                    #   問缺的那一塊，符合「不確定不猜」而非當搗蛋趕走。
                    # ── 先試 context 補齊（劇情批 r1 S6）：
                    #   'whats the safety stock for cling film' → config_read
                    #   之後，'change central to 80' 指的就是**那個商品**。
                    #   直接 clarify 問「which item」等於忘了訪客剛講過，
                    #   設定流程整條走不完。
                    if not _rescue and _is_mostly_english(user_text) \
                            and _GK_ACTION_RE.search(user_text):
                        _cfg_ctx = _ctx_by_vid.get(vid) or {}
                        _cfg_val = _re.search(r"\b(?:to|=)\s*(\d+)\b", user_text, _re.I)
                        _cfg_wh = _re.search(r"\b(north|central|south)\b",
                                             user_text, _re.I)
                        if (_cfg_ctx.get("last_sku") and _cfg_val
                                and _cfg_ctx.get("last_func") in
                                ("manage_config", "query_inventory")):
                            _rescue = ("manage_config", {
                                "action": "set",
                                "key": "safety stock",
                                "item": _cfg_ctx["last_sku"],
                                "value": _cfg_val.group(1),
                                **({"warehouse": _cfg_wh.group(1).lower()}
                                   if _cfg_wh else {}),
                            })
                            log.info(f"[gate] 管理句用 context 補齊 → manage_config"
                                     f"{{{_cfg_ctx['last_sku']}={_cfg_val.group(1)}}}: "
                                     f"{user_text!r}")
                    if not _rescue and _is_mostly_english(user_text) \
                            and _GK_ACTION_RE.search(user_text):
                        log.info(f"[gate] 管理動詞句缺參數 → clarify（原 {func_name}）: "
                                 f"{user_text!r}")
                        _act_ask = {
                            "ok": True, "view": "clarify",
                            "summary": ("I can do that — which item, and which "
                                        "warehouse? e.g. \"set safety stock for "
                                        "yoga mat to 80\" or \"move 20 yoga mat "
                                        "from north to central\"."),
                            "data": {
                                "question": "Which item should I apply this to?",
                                "options": ["item list", "whats running low"],
                                "hint": "Say the item name, or tap an option",
                            },
                        }
                        for ch in _act_ask["summary"]:
                            await send({"type": "token", "text": ch})
                            await asyncio.sleep(_TK_DELAY.get())
                        await send({"type": "done", "result": _act_ask})
                        continue
                    if _rescue:
                        func_name, func_args = _rescue
                    else:
                        log.info(f"[gate] {func_name} 缺意圖詞 → rejected: {user_text!r}")
                        await push_display({"type": "trace", "stage": "rejected",
                                            "reason": f"no_intent:{func_name}"})
                        for ch in GATEKEEPER_REJECT_MSG:
                            await send({"type": "token", "text": ch})
                            await asyncio.sleep(_TK_DELAY.get())
                        await send({"type": "done", "result": {"ok": False, "view": "rejected",
                                                        "summary": GATEKEEPER_REJECT_MSG}})
                        continue

                # ── 執行（先通知前端 tool call）──
                _arg_preview = ", ".join(f"{k}={v!r}" for k, v in list(func_args.items())[:2])
                await send({"type": "tool_call", "func": func_name, "args_preview": _arg_preview})
                result = finance.execute(func_name, func_args)
                if isinstance(result, dict) and result.get("ok"):
                    _update_ctx(vid, func_name, func_args)
                log.info(f"[trace] vid={vid} result={result.get('summary', '')[:80]!r}")

                # ── 逐步送出 trace steps（讓前端看到內部執行過程）──
                trace_steps = (result.get("data") or {}).get("trace", [])
                task_tick_idx = 0
                try:
                    for i, step in enumerate(trace_steps):
                        await send({"type": "trace_step", "step": step})
                        if trace_steps and i % max(1, len(trace_steps) // len(plan_steps)) == 0:
                            await send({"type": "task_tick", "index": task_tick_idx})
                            task_tick_idx = min(task_tick_idx + 1, len(plan_steps) - 1)
                        await asyncio.sleep(0.18)
                    for idx in range(task_tick_idx, len(plan_steps)):
                        await send({"type": "task_tick", "index": idx})
                        await asyncio.sleep(0.08)
                except RuntimeError:
                    pass  # 連線已關閉（新連線取代），靜默忽略

                await push_display({
                    "type":     "trace",
                    "stage":    "result",
                    "function": func_name,
                    "args":     func_args,
                    "result":   result,
                    "snapshot": finance.dashboard_snapshot(),
                })

                summary = result["summary"]
                # ⚠️ 路由已被改掉就不貼提示（2026-08-03）：
                #   `export movements last 7 days` 曾顯示
                #   `(auto-matched to "Elastic Sports Bra")` —— Pre-C-Cmp2 先從
                #   LLM 的幻覺 compare 抽出商品名、oov:auto_fix 設了提示字，
                #   之後 C18 把路由修正回 run_script（匯出腳本，**不吃 keyword**），
                #   提示字卻沒跟著撤 ⇒ 訪客看到匯出 CSV 卻標著不相干的商品名。
                #   ⇒ 只有「吃 keyword 的查詢類」才保留提示。
                if _oov_hint and func_name not in (
                        "query_inventory", "query_movement", "list_low_stock",
                        "query_related_items", "search_log", "list_expiring_items",
                        "compare_warehouses", "list_hot_items"):
                    _oov_hint = None
                if _oov_hint:
                    summary = _oov_hint + " " + summary
                    result = {**result, "summary": summary}
                # agent_rca：先送第一輪結果，再做第二輪 LLM 推理
                if result.get("view") == "agent_rca":
                    await send({"type": "done", "result": result})   # 先顯示 trace + 表格

                    rca_ctx = result.get("data", {}).get("rca_context", {})
                    if rca_ctx and rca_ctx.get("disc_count", 0) > 0 and LLM:
                        # ── Step 2: judge_cause_found（規則判斷，不靠模型）──
                        await send({"type": "rca_round2_start"})
                        await send({"type": "tool_call", "func": "judge_cause_found", "args_preview": f"disc_count={rca_ctx['disc_count']}"})
                        await asyncio.sleep(0.6)
                        cause_found = rca_ctx["disc_count"] > 0
                        verdict = f"✅ 已確認根因：短收 {rca_ctx['total_gap']} 件，供應商 {rca_ctx.get('main_supplier','?')}" if cause_found else "✅ 未發現短收異常"
                        await send({"type": "trace_step", "step": {"kind": "verify", "detail": verdict}})
                        await asyncio.sleep(0.3)
                        await send({"type": "trace_step", "step": {
                            "kind": "reason",
                            "detail": f"發現 {rca_ctx['disc_count']} 筆短收，商品 {rca_ctx['sku_name']} 現存 {rca_ctx['total_stock']} 件／安全 {rca_ctx['safety_stock']} 件"
                        }})
                        await asyncio.sleep(0.4)

                        # ── Step 3: suggest_action（LLM 推理建議）──
                        await send({"type": "tool_call", "func": "suggest_action", "args_preview": "action=?"})
                        await asyncio.sleep(0.6)

                        ctx = rca_ctx
                        stock_status = (
                            "庫存嚴重不足（低於安全庫存）" if ctx["total_stock"] < ctx["safety_stock"]
                            else "庫存尚可（高於安全庫存）" if ctx["total_stock"] >= ctx["safety_stock"] * 1.5
                            else "庫存偏低（接近安全庫存）"
                        )
                        round2_prompt = (
                            f"<|system|>\n你是倉管助理，根據 RCA 結果選擇建議行動。"
                            f"只輸出一個 function call，不要解釋。\n"
                            f"可用 function：\n"
                            f'suggest_action(action="contact_supplier") # 聯絡供應商追差額\n'
                            f'suggest_action(action="create_po") # 立即補採購單\n'
                            f'suggest_action(action="monitor") # 庫存充足，僅監控\n'
                            f"<|user|>\n"
                            f"商品：{ctx['sku_name']}，短收 {ctx['total_gap']} 件，"
                            f"供應商：{ctx['main_supplier']}，"
                            f"現存量：{ctx['total_stock']} 件，安全庫存：{ctx['safety_stock']} 件，"
                            f"狀態：{stock_status}。建議？\n<|assistant|>\n"
                        )
                        try:
                            # 這裡是 RCA 第二輪推理（Agent 建議行動），之前完全沒有
                            # timeout 保護：若 llm_lock 被別的請求長時間佔用，或推論
                            # 本身卡住，前端會永遠停在「Agent 推理建議行動 ●●●」
                            # 沒有任何錯誤提示或恢復機制。加 40 秒總時限。
                            async with asyncio.timeout(40.0):
                                async with llm_lock:
                                    # 不 reset：保留 KV 前綴快取（見主推論路徑說明）
                                    r2_raw = await asyncio.to_thread(
                                        LLM, round2_prompt,
                                        max_tokens=80, temperature=0.0, stop=["<|user|>", "\n\n"]
                                    )
                            r2_text = r2_raw["choices"][0]["text"].strip()
                            action = "contact_supplier"
                            if "create_po" in r2_text:
                                action = "create_po"
                            elif "monitor" in r2_text:
                                action = "monitor"
                            _ACTION_TEXT = {
                                "contact_supplier": f"📧 建議聯絡供應商 {ctx['main_supplier']} 追討短收 {ctx['total_gap']} 件差額",
                                "create_po":        f"📋 建議立即補開採購單 {ctx['total_gap']} 件（現存低於安全庫存）",
                                "monitor":          f"👁 現存量充足，建議持續監控，暫不補單",
                            }
                            suggestion = _ACTION_TEXT[action]
                            log.info(f"[RCA round2] action={action!r}")
                            await send({"type": "rca_round2_done",
                                        "suggestion": suggestion,
                                        "suggestion_action": action})
                        except Exception as e2:
                            log.warning(f"[RCA round2] 失敗: {e2}")
                            await send({"type": "rca_round2_done", "suggestion": "", "suggestion_action": ""})
                else:
                    for ch in summary:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(_TK_DELAY.get() * 1.5)
                    await send({"type": "done", "result": result})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        # ── 展場訪客**不會好好關頁面**——直接鎖手機、走出 WiFi 範圍、
        #   關機，連線就這樣斷掉，不送 WS close frame。那是**正常現象**，
        #   不是錯誤。原本一律 log.error(exc_info=True)，混沌測試實測
        #   36 位訪客就洗出 **30 筆 ERROR + 完整 traceback**，
        #   真正的錯誤會被淹沒在雜訊裡（展場一天下來更嚴重）。
        #   ⇒ 斷線類降級成 info 單行，只有真錯誤才印 traceback。
        _msg = str(e)
        _is_disconnect = (
            "no close frame" in _msg
            or "connection closed" in _msg.lower()
            or isinstance(e, (ConnectionResetError, asyncio.IncompleteReadError))
            or type(e).__name__ in ("ConnectionClosedError", "ConnectionClosedOK")
        )
        if _is_disconnect:
            log.info(f"訪客連線中斷（未送 close frame）：{type(e).__name__}")
        else:
            log.error(f"WS error: {e}", exc_info=True)
    finally:
        all_sockets.discard(ws)
        # 斷線清掉該訪客所有 session state，避免殘留（2026-07-09：新增商品流程
        # 開到一半斷線，狀態殘留 + vid 碰撞 → 下一位訪客的查詢被吸進殘留流程）
        _ctx_by_vid.pop(vid, None)
        _item_create_state_ws.pop(vid, None)
        _item_delete_state.pop(vid, None)
        _pending_by_vid.pop(vid, None)   # r32：確認卡記憶（不清會隨展期無上限成長）
        log.info(f"訪客斷線（剩 {len(all_sockets)}）")


if __name__ == "__main__":
    print(f"Starting at {get_url()}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
