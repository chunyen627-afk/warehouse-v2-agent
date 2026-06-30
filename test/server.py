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

# ─── Config ───────────────────────────────────────────────
BASE_DIR           = Path(__file__).parent
MODELS_DIR         = BASE_DIR / "models"
TEMPLATES_DIR      = BASE_DIR / "templates"
STATIC_DIR         = BASE_DIR / "static"
SYSTEM_PROMPT_FILE = BASE_DIR / "system_prompt.txt"
SEED_FILE          = BASE_DIR / "seed_data.json"

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
    "洗衣精", "洗劑", "衛生紙", "紙巾", "沐浴乳", "蚊香", "垃圾袋",
    "t 恤", "素t", "襪", "羊毛襪", "外套", "羽絨", "牛仔", "牛仔褲", "內衣",
    "瑜珈墊", "瑜珈", "水壺", "健身環", "慢跑鞋", "毛巾",
    # 倉庫
    "北倉", "北區倉", "北區", "北部",
    "中倉", "中區倉", "中區", "中部",
    "南倉", "南區倉", "南區", "南部",
    "倉", "倉庫", "warehouse",
    # 動作
    "庫存", "存量", "還有", "剩", "幾件", "多少", "幾個", "查詢",
    # 資料管理
    "新增", "建立", "加入", "增加", "新建", "添加",
    "刪除", "下架", "砍掉", "移除", "刪掉",
    "取消", "退出", "停止",
    "進貨", "出貨", "入庫", "出庫", "進倉", "出倉", "進出",
    "庫存量", "庫存價值", "週轉", "週轉率",
    "缺貨", "補貨", "警示", "警報", "告急", "快沒", "不足", "低庫存", "庫存警示",
    "賣最好", "賣最差", "熱銷", "暢銷", "滯銷", "排行", "排名", "top",
    "冠軍", "最熱門", "最冷門", "銷量",
    "比較", "比", "跟", "和", "vs", "對比",
    "查", "看", "顯示", "列", "看一下", "查一下",
    # 連帶分析 (v3.8 連鎖網)
    "連帶", "也買", "也會買", "還會買", "一起買", "一起賣", "順便買",
    "搭配", "帶動", "好夥伴", "帶貨", "連帶備貨", "連鎖",
    "bought together", "also buy", "related",
    # 時間
    "今天", "今日", "本日", "本週", "這週", "這禮拜", "本月", "這個月", "這月",
    "月度", "週度", "本日", "目前", "現在", "當下",
    "最近", "這幾天", "這陣子", "近", "month", "week", "today",
    # 動作補強 (v3.8 round 2)
    "記錄", "明細", "清單", "排行", "所有", "全部", "查", "看", "顯示",
    "最差", "最好", "賣", "進", "出", "貨",
    # 保存期限(v3.9 連動)
    "到期", "過期", "快到期", "即將到期", "保存期限", "效期", "保鮮",
    "賞味", "新鮮度", "快爛", "即期", "expire", "expiring", "shelf life",
    # RCA / 採購對帳
    "對帳", "採購對帳", "短收", "差異", "帳對不上", "帳不對", "盤點",
    "異常", "入庫異常", "進貨異常", "採購異常", "庫存異常",
    "少貨", "少了", "怎麼少", "為什麼少", "誰改", "誰動",
    "短少", "PO", "訂單", "採購單", "採購",
    "哪些", "有哪些", "列出",
    "short", "discrepancy", "mismatch",
    # English actions
    "stock", "inventory", "low", "alert", "restock", "compare", "hot", "slow",
    "top", "selling", "movement", "inbound", "outbound",
    "bluetooth", "earphone", "coffee", "machine", "bought", "together", "related",
    "what", "how", "show", "today", "week", "month", "much", "many",
    # 錯字容錯關鍵字（避免 OOV 被守門員擋掉）
    "芽", "汽", "灌", "精", "只", "基", "郭", "伽", "店", "員", "文", "窄", "胡", "湖",
    "容", "鬥", "挖", "帶", "素", "協", "一", "運", "允", "燙",
    # 口語關鍵字（避免「刷牙的那個」被 reject）
    "刷牙", "洗衣服", "擦身體", "裝水", "煮咖啡", "運動用的", "充電的",
    "那個", "這", "哪", "啥", "怎", "嗎", "呢", "啊", "吧", "喔",
    "壺", "線", "乳", "精", "機器", "墊子", "咖啡", "衣服", "手機",
    "洗澡", "牙刷", "牙膏", "毛巾", "肥皂", "洗髮", "戶外", "壞掉", "問題", "東西",
    # 助理代名
    "我", "my", "i ",
}


def is_meaningful_input(text: str) -> bool:
    """守門員：判斷輸入是否值得送 LLM。"""
    s = text.strip().lower()
    if len(s) < 2:
        return False
    if re.fullmatch(r"\d+", s):
        return False
    # 黑名單：明顯非倉管領域 → 直接擋
    for kw in _GATEKEEPER_BLACKLIST:
        if kw in s:
            return False
    for kw in GATEKEEPER_KEYWORDS:
        if kw in s:
            return True
    return False


GATEKEEPER_REJECT_MSG = (
    "這個 demo 是倉管助理、可以幫你查庫存 / 進出貨 / 缺貨警示。\n"
    "試試這樣問：\n"
    "「藍牙耳機庫存」「庫存警示」「本月熱銷排行」「北倉跟南倉比較」\n"
    "或輸入「查倉管」看完整功能列表！"
)

# 明顯非倉管領域的黑名單（股市/天氣/電影…）— 就算含「查」也不放行
_GATEKEEPER_BLACKLIST = (
    "股市", "股票", "天氣", "電影", "音樂", "新聞", "地圖",
    "翻譯", "計算", "食譜", "笑話", "遊戲", "stocks", "weather",
)


# ════════════════════════════════════════════════════════════════════
# 客服引導關鍵字
# ════════════════════════════════════════════════════════════════════
GUIDE_KEYWORDS = {
    "查倉管", "查倉", "查庫存系統", "看倉管", "看倉庫",
    "有什麼", "有什么", "可以查", "能查",
    "菜單", "菜单", "功能", "選項", "选项", "幫助", "帮助",
    "列表", "清單", "清单", "全部", "所有", "都有",
    "能做什麼", "能做什么", "看看", "導航", "導覽",
    "menu", "help", "list", "options", "what can", "guide",
}

GUIDE_MSG = (
    "我可以幫你查倉管系統：\n\n"
    "📦 庫存查詢\n"
    "  • 藍牙耳機庫存\n"
    "  • 食品飲料類庫存\n"
    "  • 北倉的氣泡水還剩多少\n\n"
    "🚨 缺貨警示\n"
    "  • 庫存警示\n"
    "  • 北區倉缺貨清單\n"
    "  • 哪些東西快沒了\n\n"
    "🔥 熱銷排行\n"
    "  • 本週最熱賣\n"
    "  • 本月運動用品熱銷\n"
    "  • 滯銷品有哪些\n\n"
    "🔗 連帶備貨分析\n"
    "  • 買藍牙耳機的人也買了什麼\n"
    "  • 咖啡機的連帶商品\n"
    "  • 買尿布的還會買啥\n\n"
    "⏰ 到期警示\n"
    "  • 哪些快到期\n"
    "  • 北倉到期清單\n"
    "  • 食品類保存期限\n\n"
    "📊 進出貨記錄\n"
    "  • 今天進了什麼貨\n"
    "  • 本週耳機出貨多少\n\n"
    "🏭 倉庫比較\n"
    "  • 北區跟南區哪個庫存比較多\n"
    "  • 中倉跟南倉週轉率比較\n\n"
    "試試點下方的快捷按鈕、或直接口語輸入！"
)


def _is_guide_request(text: str) -> bool:
    """判斷訪客是否想看倉管工具總覽。
    優先排除：句中已含具體商品 / 類別 / 倉庫關鍵字 → 當查詢、交給 LLM
    """
    s = text.strip().lower()
    if len(s) < 2 or len(s) > 20:
        return False
    if re.fullmatch(r"\d+", s):
        return False
    # 含明確類別 / 倉庫 / 商品關鍵字 → 不視為引導
    SPECIFIC = (
        "電子", "家電", "廚具", "食品", "飲料", "日用", "服飾", "運動",
        "北倉", "中倉", "南倉", "北區", "中區", "南區",
        "耳機", "悶燒", "氣泡", "咖啡", "洗衣", "衛生", "瑜珈", "水壺",
        "藍牙", "充電", "蚊香", "牛仔",
        "庫存", "缺貨", "補貨", "警示", "熱銷", "滯銷", "進貨", "出貨",
        "週轉",
        "連帶", "也買", "一起買", "搭配", "帶動", "好夥伴",
        "到期", "過期", "保存期限", "效期", "保鮮", "賞味", "即期",
    )
    for h in SPECIFIC:
        if h in s:
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
VALID_PERIODS    = {"today", "this_week", "this_month"}

# 商品意圖詞（C1 用）
_INVENTORY_INTENT_WORDS = (
    "庫存", "存量", "還有", "剩", "幾件", "多少", "幾個", "查詢", "查", "看",
    "stock", "inventory",
)

# 缺貨意圖詞（C3 用）
_LOW_STOCK_INTENT_WORDS = (
    "快沒", "缺貨", "補貨", "庫存警示", "庫存告急", "存量不足",
    "庫存不足", "低庫存", "存量警報", "警示", "告急",
    "low stock", "restock", "running low", "alert",
)

# 熱銷意圖詞（C4 用）
_HOT_INTENT_WORDS_HOT = (
    "賣最好", "最熱門", "熱銷", "暢銷", "賣最多",
    "銷量第一", "銷量冠軍", "top selling", "best seller", "hot",
)
_HOT_INTENT_WORDS_SLOW = (
    "賣最差", "滯銷", "賣不掉", "最冷門", "賣最少", "銷量最差",
    "worst selling", "slow", "slow mover",
)

# 模糊時間詞（C2 用）
_VAGUE_TIME_WORDS = ("最近", "這幾天", "這陣子", "前陣子")

# 連帶意圖詞（C6 用）— 出現這些 → query_related_items
_RELATED_INTENT_WORDS = (
    "連帶", "也買", "也會買", "還會買", "還買了", "一起買", "一起賣",
    "順便買", "搭配", "帶動", "好夥伴", "通常還買", "也買了",
    "一起出貨", "連帶備貨", "帶貨", "買的人還", "買的人也",
    "bought together", "also buy", "frequently bought", "related item",
)

# 到期警示意圖詞(C7 用)
_EXPIRING_INTENT_WORDS = (
    "到期", "過期", "快到期", "即將到期", "保存期限", "效期", "保鮮期",
    "賞味期", "新鮮度", "快爛", "快壞", "要爛了", "即期品", "即期",
    "expire", "expiring", "expired", "shelf life", "best before",
)

# ── v2 三金剛校正詞（C8-C11）──────────────────────────────────
# C8 search_log（RCA）：追原因/對不上/異常 —— 跟 query_movement（純進出統計）區隔
_RCA_INTENT_WORDS = (
    "對不上", "對不起來", "兜不攏", "帳不對", "短少", "短收", "少貨", "少了",
    "怎麼少", "為什麼少", "異常", "誰改的", "誰動的", "查原因", "追原因",
    "差異", "不對", "對帳", "discrepancy", "why", "who changed", "trace",
)
# C9 manage_config：改設定（設定項詞 + 動作詞）
_CONFIG_KEY_WORDS = ("安全庫存", "安全存量", "安全水位", "前置天數", "補貨前置",
                     "安全水位倍數", "補貨目標天數", "lead", "safety stock")
_CONFIG_SET_WORDS = ("改成", "設成", "設為", "調成", "調到", "改為", "設定為",
                     "調高", "調低", "加", "減", "+", "改", "設")
_CONFIG_READ_WORDS = ("是多少", "設多少", "多少", "現在設", "目前", "查一下", "看一下", "設定值")
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
    "多少", "幾個", "幾件", "多少個", "多少件",   # ← 補「多少」系列
    "進出", "進貨", "出貨", "異動", "移動", "移轉", "紀錄", "流向",
    "缺貨", "低庫存", "不夠", "快沒", "即將缺貨", "需要補", "補貨",
    "熱銷", "賣得好", "最多", "暢銷", "滯銷", "賣不掉", "冷門",
    "到期", "過期", "快過期", "保存期", "效期",
    "比較", "對比", "哪個倉", "哪倉", "哪個好",
    "帳對不上", "對不上", "短收", "差異", "少貨", "為什麼少", "怎麼少",
    "通知", "提醒", "警示", "設定", "設為", "調整",
    "採購單", "下單", "補貨單", "產採購",
    "報告", "報表", "健檢", "體檢",
    "關聯", "連帶", "推薦", "一起買",
    "這個月", "上個月", "本月", "跨期", "變化",
    # RCA 意圖詞同步加入，避免被 clarify 攔截
    "對帳", "異常", "帳不對", "誰改", "誰動", "查原因", "追原因",
    "採購對帳", "扣帳", "盤點", "不對", "兜不攏",
)

# ── Query Rewriting ───────────────────────────────────────────────────────────
# 將使用者的口語/模糊輸入改寫成 LLM 訓練時見過的標準句型。
# 只做字串正規化，不改變語義；改寫後的句子才送進 LLM。
import re as _re

_REWRITE_RULES: list[tuple] = [
    # ── 排程設定（優先於腳本，避免「每天跑盤點」被誤吃成 run_script）──
    (_re.compile(r"(每天|每日|每週|每周|每月|定時|固定時間|自動執行|自動跑).*(盤點|匯出|報告|體檢|健診|腳本|跑)"),
                                                                    "每天定時執行盤點"),
    (_re.compile(r"(盤點|匯出|報告|體檢).*(每天|每日|每週|每周|每月|定時)"),
                                                                    "每天定時執行盤點"),

    # ── 排程查詢 ──
    (_re.compile(r"(看|查|查看|顯示|列出|有哪些|目前|現在).*(排程|定時任務)"),
                                                                    "查看排程"),
    (_re.compile(r"排程(列表|清單|狀態|有哪些)"),                   "查看排程"),
    (_re.compile(r"^(查排程|看排程|目前排程|查看排程)$"),           "查看排程"),

    # ── 警示設定（優先於缺貨查詢）── 改寫成不含「缺貨警示」以避免被 low_stock 規則再命中
    (_re.compile(r"(設定|新增|加入|建立).*(警示|告警|提醒|通知)"),  "新增庫存警示規則"),
    (_re.compile(r"(庫存不足|缺貨).*(時|就).*(提醒|通知|告訴)"),   "新增庫存警示規則"),
    (_re.compile(r"(提醒|通知).*(庫存不足|缺貨|低於)"),            "新增庫存警示規則"),
    (_re.compile(r"低於.*(安全庫存|數量).*(提醒|通知)"),           "新增庫存警示規則"),

    # ── 警示查詢 ──
    (_re.compile(r"(看|查|查看|顯示|有哪些).*(警示|告警|alert)規則"),
                                                                    "查看警示規則"),
    (_re.compile(r"(目前|現在).*(警示|告警)"),                      "查看警示規則"),

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

    # ── 倉庫比較（優先於庫存查詢，避免「北中南倉差多少」被吃成 inventory）──
    (_re.compile(r"(北|中|南|東|西).*(倉|倉庫).*(比|差|對比|PK|差多少)"),
                                                                    "比較各倉庫庫存"),
    (_re.compile(r"(各倉|各個倉庫|三個倉|多個倉|多倉).*(比|差|差異|比較)"),
                                                                    "比較各倉庫庫存"),
    (_re.compile(r"(倉庫|倉).*(比較|對比|差多少)"),                 "比較各倉庫庫存"),
    (_re.compile(r"比較.*(倉庫|倉|北|中|南)"),                     "比較各倉庫庫存"),

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


def _rewrite_query(user_text: str) -> str:
    """將口語/模糊輸入改寫成 LLM 訓練時的標準句型。"""
    t = user_text.strip()
    _GENERIC_RCA_HEADS = ("庫存", "數量", "進貨", "帳", "對不上", "差異")
    for pattern, replacement in _REWRITE_RULES:
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
    if any(w in t for w in _RCA_INTENT_WORDS):
        return None

    has_intent = any(w in t for w in _ALL_INTENT_WORDS)

    # 剝通用填充詞，避免「幫我查」的「幫我」誤觸商品 match
    _FILLER = ("幫我", "幫忙", "請問", "麻煩", "請", "幫", "給我", "看一下",
               "查一下", "查查", "看看", "了解", "確認", "問一下", "一下", "呢", "嗎", "啊",
               "我想要", "我想", "想要", "想看", "想知道", "想查", "我要", "要查", "要看")
    t_clean = t
    for f in _FILLER:
        t_clean = t_clean.replace(f, "")
    t_clean = t_clean.strip()

    # ⓪ 剝完後 t_clean 為空 → 純意圖動詞，直接給通用選單
    if not t_clean and not has_intent:
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
    if matched_wh and len(matched_whs) < 2 and not has_intent and not _cat_hint and not _has_product:
        return {
            "question": f"你想查「{matched_wh}」的哪個項目？",
            "options": [
                f"{matched_wh} 庫存警示",
                f"{matched_wh} 近期進出貨",
                f"{matched_wh} 快到期商品",
                f"{matched_wh} 庫存總值",
            ],
            "hint": "點選其中一項，或直接輸入更完整的問題"
        }

    # ② 採購/短少/PO 意圖 + 無 SKU keyword → 推工具選項
    _po_kw = {"短少", "短收", "PO", "po", "訂單", "採購單", "採購", "對帳", "帳對不上"}
    # 明確產採購單意圖 → 直接放行，不攔
    _po_direct = ("產採購", "下採購", "補貨單下單", "幫我叫貨", "開採購", "幫我把缺貨",
                  "缺貨清單轉採購", "缺貨的產", "幫我補貨", "產po")
    has_po_intent = any(w in user_text for w in _po_kw)
    has_po_direct = any(w in user_text for w in _po_direct)
    # 兩個倉名同時出現 → 比較意圖，放行（不攔）
    has_two_whs = len(matched_whs) >= 2
    if has_po_intent and not has_po_direct and not has_two_whs:
        sku_kw = _extract_sku_keyword(user_text)
        has_sku = bool(sku_kw and len(sku_kw) >= 2 and any(
            sku_kw in nm for nm in [it["name"] for it in W.state().items]
        ))
        if not has_sku:
            return {
                "question": "你想查的是哪一種短少/採購問題？",
                "options": [
                    "查所有短收的採購單（全倉掃描）",
                    "查哪些商品目前缺貨",
                    "幫我產採購單補貨",
                    "查特定商品採購異常",
                ],
                "actions": [
                    "查全倉所有採購短收異常",
                    "哪些商品缺貨",
                    "幫我把缺貨的產採購單",
                    "查採購對帳異常",
                ],
                "hint": "點選其中一項，或直接說出商品名稱"
            }

    # ④ 類別詞 + 無動作 → 問查什麼（優先於商品名 match，避免把類別詞誤當商品名）
    _cat_kw = {
        "電子": "electronics", "3c": "electronics", "食品": "food", "飲料": "beverage",
        "清潔": "cleaning", "清潔用品": "cleaning", "嬰幼": "baby", "醫療": "medical",
        "戶外": "outdoor", "家居": "home",
    }
    matched_cat = next((zh for zh in _cat_kw if zh in t.lower()), None)
    if matched_cat and not has_intent:
        return {
            "question": f"你想查「{matched_cat}」類的什麼？",
            "options": [
                f"{matched_cat}類 庫存警示",
                f"{matched_cat}類 熱銷商品",
                f"{matched_cat}類 快到期商品",
                f"{matched_cat}類 進出貨紀錄",
            ],
            "hint": "點選其中一項，或直接輸入更完整的問題"
        }

    # ⑤ 只有商品名、沒有任何動作詞 → 問要做什麼（用 t_clean 剝掉填充詞再 match）
    matched = W.match_items(t_clean) if t_clean else []
    if matched and not has_intent:
        item = matched[0]
        name = item["item"]["name"] if isinstance(item, dict) and "item" in item else item.get("name", t)
        return {
            "question": f"你想查「{name}」的什麼？",
            "options": [
                f"{name} 庫存還剩多少",
                f"{name} 進出貨紀錄",
                f"{name} 帳對不上",
                f"{name} 快到期了嗎",
            ],
            "hint": "點選其中一項，或直接輸入更完整的問題"
        }

    # ⑥ 純模糊短句（查/看/確認等）— 用 t_clean 或 t 都檢查，剝掉填充詞後剩「查」也算
    #    也涵蓋「幫偶查」→ strip「幫」→「偶查」太短且無具體目標 → clarify
    _vague = {"查", "查詢", "看", "確認", "了解", "瞭解", "問一下", "查一下", "看一下", "看看", "那個", "這個", "欸", "誒", "喂", "嗨", "查個東西"}
    # 剝完填充詞只剩 1-3 字且有動作意圖 → clarify（但含類別關鍵字則放行，如「查食品」）
    _has_cat = any(zh in t for zh in ("電子", "家電", "廚具", "食品", "飲料", "日用", "服飾", "運動"))
    _too_short = len(t_clean) <= 3 and has_intent and not _has_cat
    if t in _vague or t_clean in _vague or (not t_clean and not has_intent) or _too_short:
        return {
            "question": "你想查什麼？",
            "options": [
                "哪些商品快缺貨",
                "哪些商品快到期",
                "本週熱銷商品",
                "採購對帳異常",
            ],
            "hint": "點選其中一項，或直接輸入商品名稱或倉庫名稱"
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
            "question": f"找不到「{keyword}」，你是指？",
            "options": options,
            "hint": "點選其中一項，或直接輸入完整商品名稱",
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

    return best


_EXTRA_NOISE = [
    "好像有", "好像", "感覺", "應該", "可能", "似乎", "有點",
    "怎麼", "是不是", "有沒有", "一下", "好嗎", "對吧",
    "呀", "啊", "耶", "喔", "吧", "欸", "嗎", "呢",
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
)

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
    for src in (cleaned, text):
        if not src or len(src) < 2:
            continue
        scored = sorted(
            [(s, n) for n in all_names if (s := _fuzzy_score(src, n)) >= 40],
            reverse=True,
        )
        if scored:
            return scored[0][1]

    return cleaned if len(cleaned) >= 2 else ""


def _correct_function_call(user_text: str, func_name: str, func_args: dict) -> tuple[str, dict, bool]:
    """校正規則。回 (corrected_name, corrected_args, hard_corrected)。
    hard_corrected=True 表示有確定性規則命中，C18 不應再覆蓋。"""
    # 排程/警示管理工具：Pre-C 已確定，不再校正
    # set_alert / schedule / compare 已被 Pre-C 校正過，不需再過 C1-C18
    # query_movement 不加在此，因為 C8 RCA 校正需要能 override 它
    # set_alert 不 early-return —「庫存警示」可能被模型誤判 set_alert，需經 C3 校正
    if func_name in ("set_schedule", "list_schedules", "delete_schedule",
                     "list_alerts", "delete_alert",
                     "compare_warehouses"):
        return func_name, func_args, True
    text_low = user_text.lower()

    # ── C7: 到期意圖詞 → list_expiring_items(最高優先)──
    # C0：未知函式名 → 從 user_text 推斷最接近的已知函式
    _KNOWN = {"query_inventory","query_movement","list_low_stock","list_hot_items",
               "list_expiring_items","compare_warehouses","query_related_items",
               "search_log","manage_config","run_script","generate_report","list_files",
               "set_alert","generate_po","compare_periods",
               "set_schedule","list_schedules","delete_schedule",
               "list_alerts","delete_alert"}
    if func_name not in _KNOWN:
        log.info(f"[校正 C0] 未知函式 {func_name!r}，嘗試從 user_text 推斷")
        # 用 C8-C16 的 intent 詞來推斷
        if any(w in user_text for w in _RCA_INTENT_WORDS):
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
    if (any(kw in user_text for kw in _EXPIRING_INTENT_WORDS) or
        any(kw in text_low for kw in _EXPIRING_INTENT_WORDS)) and not _has_report and not _has_alert:
        if func_name != "list_expiring_items":
            log.info(f"[校正 C7] {func_name} → list_expiring_items (到期意圖)")
            new_args = {}
            if func_args.get("warehouse") in VALID_WAREHOUSES:
                new_args["warehouse"] = func_args["warehouse"]
            if func_args.get("category") in VALID_CATEGORIES:
                new_args["category"] = func_args["category"]
            return "list_expiring_items", new_args, True

    # ── C3: 缺貨意圖詞 → list_low_stock（最高優先、bypass 其他校正）──
    #   排除：句中含設定項詞（安全庫存/前置天數）時讓給 C9；含報表/報告詞時讓給 C12。
    _cfg_key_in_text = any(w in user_text for w in
                           ("安全庫存", "安全存量", "安全水位", "前置天數", "補貨前置", "前置時間"))
    _report_in_text = any(w in user_text for w in ("報表", "報告", "體檢", "健檢"))
    _alert_in_text = any(w in user_text for w in ("通知", "提醒", "警示我", "就通知", "就提醒", "告訴我"))
    _po_in_text = any(w in user_text for w in ("採購單", "下單", "產採購", "叫貨", "補貨單"))
    if (any(kw in user_text for kw in _LOW_STOCK_INTENT_WORDS) or
        any(kw in text_low for kw in _LOW_STOCK_INTENT_WORDS)) \
       and not _cfg_key_in_text and not _report_in_text \
       and not _alert_in_text and not _po_in_text:
        if func_name != "list_low_stock":
            log.info(f"[校正 C3] {func_name} → list_low_stock (缺貨意圖)")
            new_args = {}
            # 保留 warehouse / category（若 LLM 有抽）
            if func_args.get("warehouse") in VALID_WAREHOUSES:
                new_args["warehouse"] = func_args["warehouse"]
            if func_args.get("category") in VALID_CATEGORIES:
                new_args["category"] = func_args["category"]
            return "list_low_stock", new_args, True
        else:
            # LLM 已正確輸出 list_low_stock，但後續 C14 看到「警示」會誤覆蓋成 set_alert
            # → hard-return 防止被後面規則（C14 等）推翻
            return func_name, func_args, True

    # ── C4: 熱銷 / 滯銷意圖詞 → list_hot_items ──
    is_hot = any(kw in user_text for kw in _HOT_INTENT_WORDS_HOT) or \
             any(kw in text_low for kw in _HOT_INTENT_WORDS_HOT)
    is_slow = any(kw in user_text for kw in _HOT_INTENT_WORDS_SLOW) or \
              any(kw in text_low for kw in _HOT_INTENT_WORDS_SLOW)
    if (is_hot or is_slow) and func_name != "list_hot_items":
        log.info(f"[校正 C4] {func_name} → list_hot_items ({'hot' if is_hot else 'slow'})")
        # 從 user_text 抽 period / category
        period = "this_week"
        if "本月" in user_text or "這個月" in user_text or "month" in text_low:
            period = "this_month"
        elif "本週" in user_text or "這週" in user_text or "這禮拜" in user_text or "week" in text_low:
            period = "this_week"
        new_args = {
            "rank_type": "slow" if is_slow else "hot",
            "period":    period,
        }
        # 抽 category（若 user_text 含類別關鍵字）
        cat_zh_map = {
            "電子": "electronics", "3c": "electronics",
            "家電": "appliance_kitchen", "廚具": "appliance_kitchen",
            "食品": "food_beverage", "飲料": "food_beverage",
            "日用": "daily_goods",
            "服飾": "apparel", "衣服": "apparel",
            "運動": "sports",
        }
        for zh, cat in cat_zh_map.items():
            if zh in user_text:
                new_args["category"] = cat
                break
        return "list_hot_items", new_args, True
    elif is_hot or is_slow:
        # LLM 已正確輸出 list_hot_items → hard-return 防後面規則推翻
        return func_name, func_args, True

    # ── C4b: list_hot_items period + category 依 user_text 校準 ──
    # (模型對沒明講期間的 query period 不穩定、且常漏抽 category slot)
    if func_name == "list_hot_items":
        func_args = dict(func_args)
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
                "日用": "daily_goods", "生活用品": "daily_goods",
                "服飾": "apparel", "衣服": "apparel", "服裝": "apparel",
                "運動": "sports",
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
            "日用": "daily_goods", "服飾": "apparel", "衣服": "apparel",
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

    # ── C6: 連帶意圖詞 → query_related_items ──
    _has_related = any(kw in user_text for kw in _RELATED_INTENT_WORDS) or \
                   any(kw in text_low for kw in _RELATED_INTENT_WORDS)
    if _has_related:
        # keyword:優先用 LLM 已抽的,否則從 user_text 去掉意圖詞+雜詞當 keyword
        kw = func_args.get("keyword")
        if not kw:
            cleaned = user_text
            for w in _RELATED_INTENT_WORDS + (
                "買", "的人", "什麼", "啥", "哪些", "通常", "跟", "和",
                "查", "看", "會", "還", "了", "嗎", "呢", "?", "？",
                "的有", "有哪", "的", "有", "商品", "產品",
            ):
                cleaned = cleaned.replace(w, " ")
            cleaned = " ".join(cleaned.split())
            kw = cleaned if len(cleaned) >= 2 else (func_args.get("keyword") or "")
        if func_name != "query_related_items":
            log.info(f"[校正 C6] {func_name} → query_related_items (連帶意圖)")
            new_args = {"keyword": kw}
            if func_args.get("category") in VALID_CATEGORIES:
                new_args["category"] = func_args["category"]
            return "query_related_items", new_args, True
        else:
            # LLM 已正確輸出 query_related_items，但可能漏 keyword → 補上並 hard-return
            if not func_args.get("keyword") and kw:
                func_args = {**func_args, "keyword": kw}
            return func_name, func_args, True

    # ── C2: 模糊時間詞 → period rewrite ──
    if func_name == "query_movement":
        if any(kw in user_text for kw in _VAGUE_TIME_WORDS):
            old_period = func_args.get("period")
            if old_period != "this_week":
                log.info(f"[校正 C2] period {old_period} → this_week (模糊時間詞)")
                func_args = dict(func_args)
                func_args["period"] = "this_week"

    # ── C1: query_inventory 沒抽到 keyword 但 user_text 含商品意圖詞 → 補 keyword ──
    if func_name == "query_inventory":
        kw = func_args.get("keyword")
        cat = func_args.get("category")
        if not kw and not cat:
            # 若 user_text 含意圖詞 → 把去掉意圖詞跟時間詞的剩餘字當 keyword
            if any(w in user_text for w in _INVENTORY_INTENT_WORDS):
                cleaned = _extract_sku_keyword(user_text)
                if cleaned and len(cleaned) >= 2:
                    log.info(f"[校正 C1] query_inventory 補 keyword: {cleaned!r}")
                    func_args = dict(func_args)
                    func_args["keyword"] = cleaned

    # ── C2c: query_movement 沒抽到 keyword → 從 user_text 補 ──
    if func_name == "query_movement":
        if not func_args.get("keyword"):
            cleaned = _extract_sku_keyword(user_text)
            if cleaned and len(cleaned) >= 2:
                log.info(f"[校正 C2c] query_movement 補 keyword: {cleaned!r}")
                func_args = dict(func_args)
                func_args["keyword"] = cleaned

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
            "日用": "daily_goods", "日用品": "daily_goods", "生活用品": "daily_goods",
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
            "this_week": "this_week", "本週": "this_week", "這週": "this_week", "this week": "this_week",
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

    # C13：明確查庫存意圖 + SKU → hard-return query_inventory（防止 C18 誤覆蓋）
    # RCA 意圖詞（對帳/異常/少了）優先於 C13，不搶
    _c13_has_rca = any(w in user_text for w in _RCA_INTENT_WORDS)
    _inv_intent = ("庫存", "剩多少", "還有多少", "有多少", "幾個", "數量", "查庫存",
                   "inventory", "stock", "查一下庫存", "看庫存", "查看庫存")
    if not _c13_has_rca and any(w in user_text for w in _inv_intent) and func_name == "query_inventory":
        kw = _extract_sku_keyword(user_text) or func_args.get("keyword", "")
        if kw:
            # 檢查 keyword 是否其實是類別名（如「電子產品庫存」→ category=electronics）
            _CAT_ZH_MAP = {
                "電子產品": "electronics", "家電廚具": "appliance_kitchen",
                "食品飲料": "food_beverage", "日用品": "daily_goods",
                "服飾": "apparel", "運動用品": "sports",
                "電子": "electronics", "家電": "appliance_kitchen", "廚具": "appliance_kitchen",
                "食品": "food_beverage", "飲料": "food_beverage",
                "日用": "daily_goods", "衣服": "apparel", "服裝": "apparel",
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
            if cat_en and func_args.get("category", "") not in VALID_CATEGORIES and not _kw_matches_product:
                log.info(f"[校正 C13] 類別庫存查詢 kw={kw!r} → category={cat_en}")
                return "query_inventory", {**{k:v for k,v in func_args.items() if k!='keyword'}, "category": cat_en}, True
            log.info(f"[校正 C13] 明確庫存查詢 → query_inventory({kw!r})")
            return "query_inventory", {**func_args, "keyword": kw}, True

    # ══════════════ v2 三金剛校正（C8-C11）══════════════

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

    has_rca    = any(w in user_text for w in _RCA_INTENT_WORDS)
    has_cfgkey = any(w in user_text for w in _CONFIG_KEY_WORDS)
    has_cfgset = any(w in user_text for w in _CONFIG_SET_WORDS)
    has_script = any(w in user_text for w in _SCRIPT_INTENT_WORDS)

    # C8：含 RCA 意圖詞 → 強轉 search_log（排除已正確的 search_log 和寫入類）
    _rca_exclude = {"search_log", "manage_config", "set_alert", "generate_po",
                    "commit_po", "run_script", "generate_report"}
    # 兩倉比較意圖（如「北倉和南倉庫存差異比一下」）→ RCA 不搶
    _two_whs_in_text = sum(1 for zh in _WH_ZH_MAP if zh in user_text) >= 2
    if has_rca and func_name not in _rca_exclude and not _two_whs_in_text:
        kw = func_args.get("keyword", "")
        # C8 轉換時就補好 keyword，否則 C17 沒機會跑
        if not kw:
            kw = _extract_sku_keyword(user_text) or ""
        new_args = {"keyword": kw}
        if func_args.get("period"):
            new_args["time_range"] = func_args["period"]
        log.info(f"[校正 C8] RCA 意圖 → search_log（原 {func_name}）keyword={kw!r}")
        return "search_log", new_args, True

    # C9：含設定項詞 + 動作詞 → 強轉 manage_config（set_alert 已有自己的路由不干涉）
    if has_cfgkey and func_name not in ("manage_config", "set_alert"):
        action = "set" if has_cfgset and not any(w in user_text for w in ("是多少", "設多少", "查")) else "read"
        # 抽 key
        key = next((w for w in _CONFIG_KEY_WORDS if w in user_text), "安全庫存")
        new_args = {"action": action, "key": key}
        # 抽 warehouse
        for zh, en in _WH_ZH_MAP.items():
            if zh in user_text:
                new_args["warehouse"] = en
                break
        # 抽 value（+N / 數字）
        import re as _re
        mrel = _re.search(r"[加+]\s*(\d+)", user_text) or _re.search(r"高\s*(\d+)", user_text)
        mabs = _re.search(r"(?:改成|設成|設為|改為|設定為|調到|改|設)\s*(\d+)", user_text)
        if action == "set":
            if mrel:
                new_args["value"] = f"+{mrel.group(1)}"
            elif mabs:
                new_args["value"] = mabs.group(1)
        log.info(f"[校正 C9] 設定意圖 → manage_config{{{action}}}（原 {func_name}）")
        return "manage_config", new_args, True

    # C10：含明確腳本動作詞 → 強轉 run_script
    #   明確腳本詞（盤點/匯出/重產）即使模型誤判成 manage_config 也要救回；
    #   但若同時含設定項詞（前置天數/安全庫存）則讓給 C9（避免誤傷設定查詢）。
    _script_strong = ("盤點", "匯出", "重產", "重新產生", "重生種子", "重建資料", "重新產生種子",
                      "跑盤點", "跑個盤", "跑一個盤", "體檢報告", "進出記錄")
    _sched_time_kws_c10 = ("每天", "每日", "每週", "每周", "每月", "定時", "排程", "固定時間",
                           "每天早上", "每天晚上", "自動執行", "自動跑")
    _is_sched_intent = any(w in user_text for w in _sched_time_kws_c10)
    if not _is_sched_intent and \
            (func_name not in ("run_script", "set_schedule") or not func_args.get("script_name")) \
            and not has_cfgkey and any(w in user_text for w in _script_strong):
        sname = next((w for w in ("月底盤點", "盤點", "匯出進出", "匯出", "體檢報告", "重產", "重新產生", "重生") if w in user_text), "")
        if sname:
            log.info(f"[校正 C10] 腳本意圖 → run_script（原 {func_name}）")
            return "run_script", {"script_name": sname}, True

    # C12：報告意圖 → generate_report（A 波：寫報告）
    #   「報告/報表」是強訊號 → 蓋過 list_expiring/list_low 等查詢路由。
    _report_words = ("報告", "報表", "體檢", "健檢", "出個報告", "全倉掃描",
                     "掃一遍", "整理一份", "彙整", "report", "做份報告", "產生報告")
    if func_name != "generate_report" and not has_cfgkey \
            and any(w in user_text for w in _report_words):
        rt = ("low_stock" if any(w in user_text for w in ("缺貨", "補貨", "低庫存")) else
              "expiring" if any(w in user_text for w in ("到期", "效期", "過期")) else
              "rca" if any(w in user_text for w in ("異常", "對不上", "短收")) else "full")
        log.info(f"[校正 C12] 報告意圖 → generate_report{{{rt}}}（原 {func_name}）")
        return "generate_report", {"report_type": rt}, True

    # C13：檔案列表意圖 → list_files（B 波：動態找檔）
    _listfile_words = ("有哪些檔", "有什麼檔", "有哪些資料", "列出檔案", "看一下檔案",
                       "有哪些紀錄檔", "資料夾", "有哪些目錄", "列檔", "list files", "有什麼資料可以查")
    if func_name != "list_files" and any(w in user_text for w in _listfile_words):
        area = next((k for k in ("transactions", "orders", "master", "audit", "reports", "scripts",
                                 "交易", "採購", "主檔", "異動", "報告", "腳本") if k in user_text), "")
        log.info(f"[校正 C13] 檔案列表意圖 → list_files（原 {func_name}）")
        return "list_files", ({"area": area} if area else {}), True

    # C14：警示設定意圖 → set_alert（第四金剛）
    #   「就通知我 / 設個提醒 / 警示我 / 低於X就告訴我」
    _alert_words = ("通知我", "提醒我", "警示", "告訴我", "就通知", "設個提醒",
                    "設定警示", "低於就", "缺貨就", "到期就", "alert", "提醒")
    if func_name != "set_alert" and any(w in user_text for w in _alert_words) \
            and any(w in user_text for w in ("通知", "提醒", "警示", "告訴")):
        cond = ("out_of_stock" if any(w in user_text for w in ("缺貨", "斷貨", "沒貨")) else
                "expiring" if any(w in user_text for w in ("到期", "過期", "效期")) else
                "below_safety")
        log.info(f"[校正 C14] 警示意圖 → set_alert{{{cond}}}（原 {func_name}）")
        # 直接在 C14 內做 C17b 的工作，因為 return 後 C17b 跑不到
        import re as _re14
        _thr14 = _re14.search(r'(?:低於|少於|小於|不足)\s*(\d+)', user_text)
        _tgt14 = _extract_sku_keyword(user_text) or ""
        _c14_args = {"condition": ("below_threshold" if _thr14 else cond), "target": _tgt14}
        if _thr14:
            _c14_args["threshold"] = int(_thr14.group(1))
        return "set_alert", _c14_args, True

    # C15：產採購單意圖 → generate_po（閉環）
    _po_words = ("採購單", "下單", "補貨單", "進貨單", "幫我叫貨", "產採購", "開採購",
                 "產po", "purchase order", "下採購", "補貨清單下單", "幫我補貨", "要補的貨")
    if func_name != "generate_po" and any(w in user_text for w in _po_words):
        src = "shortfall" if any(w in user_text for w in ("短收", "對不上", "補單")) else "low_stock"
        log.info(f"[校正 C15] 採購意圖 → generate_po{{{src}}}（原 {func_name}）")
        return "generate_po", {"source": src}, True

    # C16：跨期比較意圖 → compare_periods
    _cmp_period_words = ("這個月跟上個月", "本月對比上月", "跟上月比", "跨期", "兩個月比",
                         "月對比", "上月相比", "變化最大", "哪些變化大", "成長最多", "衰退最多",
                         "這月和上月", "本月vs上月", "月增減")
    if func_name != "compare_periods" and any(w in user_text for w in _cmp_period_words):
        log.info(f"[校正 C16] 跨期比較 → compare_periods（原 {func_name}）")
        return "compare_periods", {"metric": "out"}, True

    # C11-pre0：manage_config action 修正 — 含「改成/設成/調成/改為/設為」→ set
    _set_verbs = ("改成", "設成", "調成", "改為", "設為", "調整為", "改為", "修改成",
                  "調到", "改到", "設定成", "更改為", "更改成")
    if func_name == "manage_config" and func_args.get("action") == "read" \
            and any(v in user_text for v in _set_verbs):
        func_args = {**func_args, "action": "set"}
        log.info("[校正 C11-pre0] manage_config action read→set（含改/設/調動詞）")

    # C11-pre：manage_config key 補全 — 模型可能把「補貨前置天數」截成「補貨」或空字串
    if func_name == "manage_config":
        raw_key = str(func_args.get("key", "")).strip()
        # 「補貨」單字 or 空字串 + user_text 含「前置/天數/days」→ 補全為「前置天數」
        if raw_key in ("補貨", "") and any(w in user_text for w in ("前置", "前置天數", "days")):
            func_args = {**func_args, "key": "前置天數"}
            log.info(f"[校正 C11-pre] manage_config key {raw_key!r} → '前置天數'")
        elif raw_key == "" and any(w in user_text for w in ("安全庫存",)):
            func_args = {**func_args, "key": "安全庫存"}
            log.info(f"[校正 C11-pre] manage_config key '' → '安全庫存'")

    # C11：manage_config set 缺 warehouse → 預設 all（不擋，給預設）
    if func_name == "manage_config" and func_args.get("action") == "set" \
            and not func_args.get("warehouse"):
        for zh, en in _WH_ZH_MAP.items():
            if zh in user_text:
                func_args["warehouse"] = en
                break
        func_args.setdefault("warehouse", "all")
        log.info(f"[校正 C11] manage_config set 補 warehouse={func_args['warehouse']}")

    # C11b：manage_config value 補全：加減N → ±N；改成N → N；空白 → 從 user_text 找
    import re as _re
    if func_name == "manage_config" and func_args.get("action") == "set":
        raw_v = str(func_args.get("value", "")).strip()
        # 1. 已是合法數字（含 ±）→ 不動
        if _re.match(r'^[+\-]?\d+$', raw_v):
            pass
        else:
            # 2. 找「加N / 減N / +N / -N」（可含「全部」前綴）
            _adj = _re.search(r'(?:全部)?(加|減|\+|-)(\d+)', raw_v) or \
                   _re.search(r'(?:全部)?(加|減|\+|-)(\d+)', user_text)
            if _adj:
                sign = "+" if _adj.group(1) in ("加", "+") else "-"
                func_args["value"] = f"{sign}{_adj.group(2)}"
                log.info(f"[校正 C11b] manage_config value {raw_v!r} → {func_args['value']!r}")
            else:
                # 3. 找「改成N / 設成N / 調整為N」→ 直接設值
                _set_m = _re.search(r'(?:改成|設成|調整為|設定為|改為|設為|調到|改到)\s*(\d+)', user_text)
                if _set_m:
                    func_args["value"] = _set_m.group(1)
                    log.info(f"[校正 C11b] manage_config value 從 user_text 直接設 {func_args['value']!r}")

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
        # 先用模型抽到的 keyword 跑 SKU match；沒結果再用去後綴的 user_text
        final_kw = _extract_sku_keyword(model_kw) if model_kw else ""
        if not final_kw:
            final_kw = _extract_sku_keyword(_clean_user)
        func_args = {
            "keyword":    final_kw or model_kw or _clean_user or user_text,
            "time_range": func_args.get("time_range", func_args.get("period")),
        }
        if func_args["time_range"] is None:
            del func_args["time_range"]
        log.info(f"[校正 C17] search_log keyword → {repr(func_args['keyword'])}")
        return func_name, func_args, True  # hard：C18 不得再覆蓋 search_log

    # C17a：query_inventory / query_movement 從 user_text 補 warehouse（「南倉洗衣精」→ warehouse=south）
    if func_name in ("query_inventory", "query_movement") and not func_args.get("warehouse"):
        for zh, en in _WH_ZH_MAP.items():
            if zh in user_text:
                func_args = {**func_args, "warehouse": en}
                log.info(f"[校正 C17a] {func_name} 補 warehouse={en}")
                break

    # C17b：set_alert 參數清理 — 只保留 condition / target，清掉 keyword 等非法參數
    if func_name == "set_alert":
        import re as _re
        cond = str(func_args.get("condition", func_args.get("keyword", ""))).strip()
        tgt  = str(func_args.get("target", func_args.get("item", ""))).strip()
        # 若 condition 不是合法 enum，從 user_text 推斷
        _valid_conds = {"below_safety", "below_threshold", "expiring_soon", "overstock"}
        if cond not in _valid_conds:
            # 整句帶數字「低於N/少於N/小於N」→ below_threshold
            _thr = _re.search(r'(?:低於|少於|小於|不足)\s*(\d+)', user_text)
            if _thr:
                cond = "below_threshold"
                func_args["threshold"] = int(_thr.group(1))
            else:
                cond = "below_safety"
        # 若 target 是整句話，改用 _extract_sku_keyword
        if tgt and len(tgt) > 6:
            tgt = _extract_sku_keyword(tgt) or tgt
        # 若 target 為空，嘗試從 user_text 抽 SKU
        if not tgt:
            tgt = _extract_sku_keyword(user_text) or ""
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
display_sockets: set[WebSocket] = set()
all_sockets:     set[WebSocket] = set()
_visitor_closed = False
_item_create_state: dict = {}
_item_delete_state: dict = {}  # 刪除模式的 session state


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
    _set_health("loading_model", f"載入模型中... ({Path(path).name})")
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
    _set_health("self_check", "模型載入完成、正在自我檢測推論（最多等 10 秒）...")
    log.info("模型載入完成、正在自我檢測推論...")

    import threading
    result_holder = {"text": None, "error": None}

    def _self_check():
        try:
            r = llm("Hi", max_tokens=3, echo=False, temperature=0.0)
            result_holder["text"] = r["choices"][0]["text"]
        except Exception as e:
            result_holder["error"] = e

    t = threading.Thread(target=_self_check, daemon=True)
    t.start()
    t.join(timeout=10.0)

    if t.is_alive():
        err_msg = (
            "推論自我檢測超時（10 秒）— 模型載入成功但推論卡住。\n"
            "可能原因：\n"
            "  1. CPU 指令集不相容（llama-cpp DLL 在這台 CPU 上 deadlock）\n"
            "  2. 防毒軟體攔截了 native code 執行\n"
            "  3. CPU 太舊（早於 2008 年）\n"
            "請回報此問題並附上 CPU 型號（執行 wmic cpu get name 取得）"
        )
        _set_health("failed", "推論自我檢測失敗（10 秒無回應）", error=err_msg)
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
        _set_health("failed", "推論自我檢測失敗（例外）", error=err_msg)
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
                None, lambda jid=job["script_id"]: finance.execute("commit_run_script",
                                                                   {"script_id": jid, "confirmed": True}))
            # 更新 last_run
            job["last_run"] = now.isoformat(timespec="seconds")
            jobs_path.write_text(_json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2), encoding="utf-8")
            await push_display({"type": "schedule_done", "job_id": job["id"],
                                "script_label": job["script_label"],
                                "ok": result.get("ok", False),
                                "summary": result.get("summary", ""),
                                "output_tail": result.get("data", {}).get("output_tail", ""),
                                "ts": now.strftime("%H:%M")})
    except Exception as e:
        log.error(f"[_run_due_schedules] {e}", exc_info=True)


# ─── 警示規則背景排程 ──────────────────────────────────────
_ALERT_CHECK_INTERVAL = 3600  # 每小時掃一次

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

async def _check_alert_rules():
    """掃一次 alert_rules.json，有觸發就推 WebSocket。"""
    from tools_v2 import _data_dir, _match_script
    import json as _json
    try:
        dd = _data_dir()
        rules_path = dd / "alert_rules.json"
        if not rules_path.exists():
            return
        rules = _json.loads(rules_path.read_text("utf-8")).get("rules", [])
        active = [r for r in rules if r.get("enabled", True)]
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
            scope_txt = "全部商品" if not scope_names else "、".join(scope_names[:3])

            triggered = False
            detail = ""
            if cond in ("below_safety", "below_threshold", "out_of_stock"):
                if scope:
                    hits = [w for w in warns if w["sku_id"] in scope]
                else:
                    hits = warns
                if hits:
                    triggered = True
                    names = "、".join(w["name"] for w in hits[:3])
                    detail = f"{names} 等 {len(hits)} 項低於安全庫存"
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
    except Exception as e:
        log.error(f"[_check_alert_rules] {e}", exc_info=True)


# ─── Display 廣播 ─────────────────────────────────────────
async def push_display(payload: dict):
    msg  = json.dumps(payload, ensure_ascii=False)
    dead = set()
    for ws in display_sockets:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    display_sockets.difference_update(dead)


# ─── FastAPI ──────────────────────────────────────────────
app = FastAPI()


def _background_init():
    """背景載入模型。"""
    global LLM, MODEL_FILE, SYSTEM_PROMPT
    try:
        _set_health("starting", "初始化 seed 資料...")
        finance.init(SEED_FILE)
        intent_clf.load()
        SYSTEM_PROMPT = load_system_prompt()
        LLM, MODEL_FILE = load_model()
        snap = finance.state()
        log.info(f"快照日期：{snap.snapshot_date}")
        log.info(f"SKU 數：{len(snap.items)} / 倉庫：{len(snap.warehouses)} / 類別：{len(snap.categories)}")
        log.info(f"URL: {get_url()}")
        _set_health("ready",
                    f"就緒 — 快照 {snap.snapshot_date}、{len(snap.items)} SKU、{len(snap.warehouses)} 倉")
    except Exception as e:
        log.error(f"[startup] 初始化失敗: {e}", exc_info=True)
        if HEALTH["stage"] != "failed":
            _set_health("failed", "初始化失敗", error=f"{type(e).__name__}: {e}")


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
    # ── 警示規則背景排程 ──
    asyncio.create_task(_alert_scheduler_loop())
    # ── 定時腳本排程 ──
    asyncio.create_task(_schedule_runner_loop())


@app.get("/reports/{fname}")
async def get_report_file(fname: str):
    """報告圖表 PNG / markdown（沙盒：只允許 reports/ 下、擋路徑穿越）。"""
    from pathlib import Path as _P
    if "/" in fname or "\\" in fname or ".." in fname:
        return Response(status_code=400)
    rp = _P(finance.state().v2_data_dir) / "reports" / fname
    if not rp.exists():
        return Response(status_code=404)
    media = "image/png" if fname.endswith(".png") else "text/markdown; charset=utf-8"
    return Response(content=rp.read_bytes(), media_type=media, headers=NO_CACHE)


@app.get("/audit/{fname}")
async def get_audit_file(fname: str):
    """下載 audit/ 下的 CSV（盤點/匯出結果）。"""
    from pathlib import Path as _P
    if "/" in fname or "\\" in fname or ".." in fname:
        return Response(status_code=400)
    ap = _P(finance.state().v2_data_dir) / "audit" / fname
    if not ap.exists():
        return Response(status_code=404)
    media = "text/csv; charset=utf-8-sig"
    headers = {**NO_CACHE, "Content-Disposition": f'attachment; filename="{fname}"'}
    return Response(content=ap.read_bytes(), media_type=media, headers=headers)


@app.get("/anomalies")
async def anomalies(only_new: bool = False):
    """主動異常偵測 — 也可被使用者主動查詢（雙軌：背景推 + 手動拉）。"""
    import anomaly
    return JSONResponse(anomaly.scan_once(only_new=only_new), headers=NO_CACHE)


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
    user_text = _rewrite_query(user_text)

    # ── 取消（清除所有 session state）──
    if user_text.strip() == "取消":
        _item_create_state.clear()
        _item_delete_state.clear()
        return JSONResponse({"ok": True, "summary": "已取消。", "view": "item_cancelled", "data": {}})

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
            if "跳過" in user_text:
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
                names = "、".join(it["name"] for it in user_items[:10])
                result = {"ok": True, "summary": f"可刪除的商品：{names}\n請輸入要刪除的名稱", "view": "item_list",
                           "data": {"items": [{"name": it["name"], "sku": it["sku_id"]} for it in user_items]}}
                _item_delete_state["active"] = True
            else:
                result = {"ok": True, "summary": "目前沒有可刪除的商品。先用「➕ 新增商品」建立。", "view": "item_list", "data": {}}
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
            r = await asyncio.wait_for(
                asyncio.to_thread(
                    LLM, prompt,
                    max_tokens=160, temperature=0.0,
                    stop=["</s>", "<end_of_turn>", "<start_of_turn>"],
                    echo=False, stream=False,
                ),
                timeout=25.0,
            )
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
    _list_alert_kws_h = ("查看警示", "查警示", "有哪些警示", "目前警示", "現在警示")
    _list_alert_rule_kw = "警示規則"  # 單獨處理，避免「新增警示規則」誤走 list
    _list_sched_kws_h = ("查看排程", "查排程", "看排程", "有哪些排程", "排程列表", "目前排程")
    _is_alert_set = any(w in user_text for w in ("新增", "設定", "加入", "建立", "通知我", "提醒我"))
    if (not _is_alert_set and
            (any(w in user_text for w in _list_alert_kws_h) or
             (_list_alert_rule_kw in user_text and not _is_alert_set))):
        func_name = "list_alerts"
        func_args = {}
    elif any(w in user_text for w in _list_sched_kws_h):
        func_name = "list_schedules"
        func_args = {}
    else:
        _sched_time_kws = ("每天", "每日", "每週", "每周", "每月", "定時", "排程", "固定時間",
                           "每天早上", "每天晚上", "每天中午", "自動執行", "自動跑")
        _sched_act_kws  = ("盤點", "匯出", "報告", "體檢", "腳本", "跑")
        if (any(w in user_text for w in _sched_time_kws) and
                any(w in user_text for w in _sched_act_kws) and
                func_name != "set_schedule"):
            func_name = "set_schedule"
            func_args = {"raw_text": user_text}

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
    _has_rca_kw = any(w in user_text for w in _RCA_INTENT_WORDS)
    if (not _has_rca_kw and
            func_name != "query_movement" and
            func_name not in ("run_script", "set_schedule", "list_schedules") and
            any(w in user_text for w in _movement_kws)):
        func_name = "query_movement"
        func_args = {"period": "this_month", "direction": "both"}

    # ── Pre-C-Compare（HTTP 版）── rewrite 後的標準句 → compare_warehouses
    _compare_kws = ("比較各倉庫庫存", "各倉庫比較", "三個倉庫比較", "北中南倉",
                    "倉庫比較", "倉庫對比", "比較倉庫")
    if (func_name != "compare_warehouses" and
            func_name not in ("run_script", "set_schedule", "list_schedules") and
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
    _delete_item_kws = ("刪除", "下架", "砍掉", "移除商品", "刪掉")
    if any(w in user_text for w in _delete_item_kws):
        import tools_v2 as _tv2_del
        kw = _extract_sku_keyword(user_text)
        if not kw:
            for w in _delete_item_kws: kw = user_text.replace(w, "").strip()
        result = _tv2_del.delete_item_start(keyword=kw)
        return JSONResponse(result)

    # ── dispatch 攔截：「列出所有商品/商品清單」→ 全商品列表 ──
    if any(w in user_text for w in ("所有商品", "商品列表", "商品清單", "全部商品", "列出商品", "商品名稱")):
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
    _create_item_kws = ("新增商品", "建立商品", "加一個商品", "新增一個", "加入商品", "增加商品", "新建商品")
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
            if "跳過" in user_text:
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
    _stock_rank_kws = ("哪個", "哪個東西", "庫存最多", "數量最多", "哪個最多", "存貨最多", "東西最多")
    if any(w in user_text for w in _stock_rank_kws) and not any(w in user_text for w in ("熱銷", "賣", "排行", "hot", "滯銷")):
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
    result = finance.execute(func_name, func_args)
    # ── 參數錯誤時，從 user_text 推測正確意圖 → clarify ──
    if isinstance(result, dict) and not result.get("ok") and "unexpected keyword" in str(result.get("summary", "")):
        log.info(f"[dispatch] 參數錯誤 {func_name}: {result['summary']!r} → clarify")
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
        _cond_labels = {"below_safety": "低於安全庫存", "out_of_stock": "缺貨/斷貨",
                        "expiring": "快到期", "below_threshold": "低於指定數量"}
        for r in rules:
            r["condition_label"] = _cond_labels.get(r["condition"], r["condition"])
            r["scope_txt"] = "全部商品" if not r.get("scope_names") else "、".join(r["scope_names"][:3])
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
    global _visitor_closed

    # 多裝置展示模式：允許多個同時連線（桌面+手機），不踢舊連線
    await ws.accept()
    all_sockets.add(ws)
    log.info(f"訪客連線（共 {len(all_sockets)}）")

    async def send(o: dict):
        await ws.send_text(json.dumps(o, ensure_ascii=False))

    vid = id(ws) % 10000

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                continue

            msg_type = data.get("type")

            # ── confirm：三金剛寫入/執行的二次確認（HITL gate）──
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
                        res = tools_v2.commit_run_script(
                            data.get("script_id", ""), actor="user_confirmed", trace_id=trace_id)
                    elif act == "generate_po":
                        res = tools_v2.commit_po(
                            data.get("pending", {}), actor="user_confirmed", trace_id=trace_id)
                    elif act == "set_alert":
                        res = tools_v2.commit_alert_set(
                            data.get("pending", {}), actor="user_confirmed", trace_id=trace_id)
                    elif act == "set_schedule":
                        res = tools_v2.commit_schedule_set(
                            data.get("pending", {}), actor="user_confirmed", trace_id=trace_id)
                        await push_display({"type": "schedule_created",
                                           "job": res.get("data", {}).get("job", {})})
                    elif act == "item_create":
                        res = tools_v2.commit_create_item(
                            data.get("pending", {}), actor="user_confirmed", trace_id=trace_id)
                    elif act == "item_delete":
                        res = tools_v2.commit_delete_item(
                            data.get("pending", {}), actor="user_confirmed", trace_id=trace_id)
                    else:
                        res = {"ok": False, "summary": "未知的確認動作", "view": "error", "data": {}}
                except Exception as e:
                    log.error(f"[confirm] vid={vid} {act} 失敗: {e}", exc_info=True)
                    res = {"ok": False, "summary": f"執行失敗：{e}", "view": "error", "data": {}}
                log.info(f"[confirm] vid={vid} {act} → {res.get('summary','')[:60]}")
                await push_display({"type": "trace", "stage": "committed",
                                    "action": act, "result": res,
                                    "snapshot": finance.dashboard_snapshot()})
                for ch in res.get("summary", ""):
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(0.012)
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
                    await asyncio.sleep(0.012)
                await send({"type": "done", "result": result})
                continue

            if msg_type != "chat":
                continue

            user_text = (data.get("text") or "").strip()
            if not user_text:
                continue
            user_text = _rewrite_query(user_text)
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

            # ── 刪除/下架（優先於 clarify）──
            _delete_kws_ws = ("刪除", "下架", "砍掉", "移除", "刪掉")
            if any(w in user_text for w in _delete_kws_ws):
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
                        names = "、".join(it["name"] for it in user_items[:10])
                        result = {"ok": True, "summary": f"可刪除的商品：{names}\n請輸入要刪除的名稱", "view": "item_list",
                                   "data": {"items": [{"name": it["name"], "sku": it["sku_id"]} for it in user_items]}}
                        _item_delete_state["active"] = True
                    else:
                        result = {"ok": True, "summary": "目前沒有可刪除的商品。先用「➕ 新增商品」建立。", "view": "item_list", "data": {}}
                else:
                    result = _tv2_del_ws.delete_item_start(keyword=kw)
                for ch in result.get("summary", ""):
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(0.012)
                await send({"type": "done", "result": result})
                continue

            # ── 刪除模式中：訪客輸入商品名 → 執行刪除 ──
            if _item_delete_state.get("active"):
                import tools_v2 as _tv2_del_mode
                _item_delete_state.clear()
                result = _tv2_del_mode.delete_item_start(keyword=user_text.strip())
                for ch in result.get("summary", ""):
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(0.012)
                await send({"type": "done", "result": result})
                continue

            # ── 列出所有商品（優先於引導）──
            if any(w in user_text for w in ("所有商品", "商品列表", "商品清單", "全部商品", "列出商品", "商品名稱")):
                import warehouse as _W_list_ws
                snap = _W_list_ws.state()
                rows = [f"{it['sku_id']} {it['name']} ({_W_list_ws.CATEGORY_LABEL.get(it['category'], it['category'])}) NT${it['unit_price']}" for it in snap.items]
                summary = f"共 {len(rows)} 項商品：\n" + "\n".join(f"  {r}" for r in rows)
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
                await send({"type": "done", "result": {"ok": True, "view": "guide"}})
                continue

            # ── 刪除模式中 → 優先處理，不進守門員 ──
            if _item_delete_state.get("active"):
                import tools_v2 as _tv2_del_mode2
                _item_delete_state.clear()
                result = _tv2_del_mode2.delete_item_start(keyword=user_text.strip())
                for ch in result.get("summary", ""):
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(0.012)
                await send({"type": "done", "result": result})
                continue

            # ── 守門員 ──
            if not _item_create_state.get("active") and not is_meaningful_input(user_text):
                log.info(f"[守門員] 拒絕無意義輸入: {user_text!r}")
                await push_display({"type": "trace", "stage": "rejected",
                                    "reason": "輸入未命中倉管關鍵字"})
                for ch in GATEKEEPER_REJECT_MSG:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(0.008)
                await send({"type": "done", "result": {"ok": False, "view": "rejected"}})
                continue

            # ── item_create 流程中 → 攔截處理，不進 LLM ──
            if _item_create_state.get("active"):
                import tools_v2 as _tv2_item_ws
                st2 = _item_create_state
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
                    if "跳過" in user_text:
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
                    _item_create_state.clear()
                else:
                    d = result.get("data", {})
                    _item_create_state.update({k: v for k, v in d.items() if k in ("step", "name", "category", "price", "safety", "stock_north", "stock_central", "stock_south")})
                    _item_create_state["active"] = True
                for ch in result.get("summary", ""):
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(0.012)
                await send({"type": "done", "result": result})
                continue

            # ── 新增商品 keyword 攔截（首次進入流程）──
            _create_item_kws_ws2 = ("新增商品", "建立商品", "加一個商品", "新增一個", "加入商品", "增加商品", "新建商品")
            if any(w in user_text for w in _create_item_kws_ws2):
                import tools_v2 as _tv2_ci2
                raw = user_text
                for kw in _create_item_kws_ws2: raw = raw.replace(kw, "").strip()
                result = _tv2_ci2.create_item_collect(step=1, raw_text=raw) if raw else _tv2_ci2.create_item_start()
                if result.get("view") != "item_confirm":
                    d = result.get("data", {})
                    _item_create_state.update({k: v for k, v in d.items() if k in ("step", "name", "category", "price", "safety", "stock_north", "stock_central", "stock_south")})
                    _item_create_state["active"] = True
                for ch in result.get("summary", ""):
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(0.012)
                await send({"type": "done", "result": result})
                continue

            prompt = build_prompt(user_text)

            async with llm_lock:
                if hasattr(LLM, "reset"):
                    LLM.reset()
                try:
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
                except asyncio.TimeoutError:
                    log.warning(f"[timeout] vid={vid} 推理超時: {user_text!r}")
                    await send({
                        "type": "error",
                        "text": "系統有點忙、請稍候再試（試試更簡短的講法、例如「藍牙耳機庫存」）",
                    })
                    continue
                except Exception as e:
                    log.error(f"[llm-error] vid={vid} {type(e).__name__}: {e}", exc_info=True)
                    await send({"type": "error", "text": "推理失敗、請重試"})
                    continue

                output = r["choices"][0]["text"].strip()
                log.info(f"[trace] vid={vid} model={output[:120]}")
                await push_display({"type": "trace", "stage": "llm_output", "raw": output})

                parsed = parse_function_call(output)
                if not parsed:
                    log.info(f"[trace] vid={vid} no_function")
                    await send({"type": "error",
                                "text": "我看不懂這句話。試試：「藍牙耳機庫存」「庫存警示」「本月熱銷」"})
                    await push_display({"type": "trace", "stage": "no_function"})
                    continue

                func_name, func_args = parsed
                raw_call = f"{func_name}({func_args})"

                # ── Pre-C-Schedule：定時排程意圖攔截 ──
                _list_alert_kws = ("查看警示", "查警示", "有哪些警示", "目前警示", "現在警示")
                _list_sched_kws = ("查看排程", "查排程", "看排程", "有哪些排程", "排程列表", "目前排程")
                _is_alert_set_ws = any(w in user_text for w in ("新增", "設定", "加入", "建立", "通知我", "提醒我"))
                if (not _is_alert_set_ws and
                        (any(w in user_text for w in _list_alert_kws) or
                         ("警示規則" in user_text and not _is_alert_set_ws))):
                    func_name = "list_alerts"
                    func_args = {}
                    log.info("[Pre-C-Sched] 查警示攔截 → list_alerts")
                elif any(w in user_text for w in _list_sched_kws):
                    func_name = "list_schedules"
                    func_args = {}
                    log.info("[Pre-C-Sched] 查排程攔截 → list_schedules")
                else:
                    _sched_time_kws = ("每天", "每日", "每週", "每周", "每月", "定時", "自動", "排程",
                                       "每天早上", "每天晚上", "每天中午", "固定")
                    _sched_act_kws  = ("盤點", "匯出", "報告", "體檢", "腳本", "跑")
                    _has_sched_time = any(w in user_text for w in _sched_time_kws)
                    _has_sched_act  = any(w in user_text for w in _sched_act_kws)
                    if _has_sched_time and _has_sched_act and func_name != "set_schedule":
                        func_name = "set_schedule"
                        func_args = {"raw_text": user_text}
                        log.info(f"[Pre-C-Sched] 排程意圖攔截 → set_schedule raw_text={user_text!r}")

                # ── Pre-C10：腳本意圖強攔截（在 clarify / LLM 校正之前）──
                _prec10_skip = ("run_script", "set_schedule", "query_movement", "compare_warehouses")
                _pre_script_kws = ("盤點", "匯出進出", "匯出記錄", "進出記錄", "體檢報告", "月底盤點")
                _pre_script_hit = next((w for w in _pre_script_kws if w in user_text), None)
                if _pre_script_hit and func_name not in _prec10_skip:
                    smap = {"盤點": "盤點", "月底盤點": "月底盤點",
                            "匯出進出": "匯出", "匯出記錄": "匯出", "進出記錄": "匯出",
                            "體檢報告": "體檢報告"}
                    func_name = "run_script"
                    func_args = {"script_name": smap.get(_pre_script_hit, _pre_script_hit)}
                    log.info(f"[Pre-C10] 腳本意圖強攔截 → run_script script_name={func_args['script_name']!r}")

                # ── Pre-C-Movement（ws 版）──
                _movement_kws_ws = ("查詢進出記錄", "進出記錄", "出貨了多少", "上週進了多少",
                                    "最近30天出貨", "進貨記錄", "出貨記錄", "入庫記錄", "移動記錄")
                _compare_kws_ws  = ("比較各倉庫庫存", "各倉庫比較", "三個倉庫比較", "北中南倉",
                                    "倉庫比較", "倉庫對比", "比較倉庫")
                _alert_set_kws_ws = ("新增庫存警示規則", "設定缺貨警示", "設定警示", "新增警示",
                                     "庫存不足時提醒", "低於安全庫存通知")
                _skip_override = ("run_script", "set_schedule", "list_schedules",
                                  "list_alerts", "delete_alert", "delete_schedule")
                _has_rca_kw_ws = any(w in user_text for w in _RCA_INTENT_WORDS)
                if func_name not in _skip_override:
                    if (not _has_rca_kw_ws and
                            func_name != "query_movement" and
                            any(w in user_text for w in _movement_kws_ws)):
                        func_name = "query_movement"
                        func_args = {"period": "this_month", "direction": "both"}
                        log.info("[Pre-C-Mov] → query_movement")
                    elif func_name != "compare_warehouses" and any(w in user_text for w in _compare_kws_ws):
                        func_name = "compare_warehouses"
                        func_args = {}
                        log.info("[Pre-C-Cmp] → compare_warehouses")
                    elif func_name not in ("set_alert", "list_alerts") and any(w in user_text for w in _alert_set_kws_ws):
                        func_name = "set_alert"
                        func_args = {"raw_text": user_text}
                        log.info("[Pre-C-Alert] → set_alert")

                # ── Clarification：模糊意圖攔截（在校正前）──
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
                }
                plan_steps = _TASK_PLANS.get(func_name, ["分析請求", "執行查詢", "回傳結果"])
                # search_log 有自己的 trace UI，不需要 task_plan
                if func_name != "search_log":
                    try:
                        await send({"type": "task_plan", "steps": plan_steps})
                        await asyncio.sleep(0.1)
                    except RuntimeError:
                        pass

                # search_log keyword 在 OOV 前先用 _extract_sku_keyword 預清理，
                # 避免模型帶入雜詞（例如「抗菌洗衣精帳」）降低 fuzzy 分
                if func_name == "search_log" and func_args.get("keyword"):
                    pre_kw = _extract_sku_keyword(func_args["keyword"])
                    if pre_kw:
                        func_args = {**func_args, "keyword": pre_kw}

                # ── OOV 偵測：keyword 不在 SKU 清單時推測候選 ──
                oov = _detect_oov(func_name, func_args)
                if oov:
                    if oov["auto_fix"]:
                        # 靜默修復：直接換 keyword，繼續執行，回應加提示
                        log.info(f"[oov:auto_fix] vid={vid} {oov['original_keyword']!r} → {oov['fixed_keyword']!r} (score={oov['score']:.0f})")
                        func_args["keyword"] = oov["fixed_keyword"]
                        # 把修復提示帶入後續 result，由工具回傳後前端顯示
                        _oov_hint = f"（已自動對應至「{oov['fixed_keyword']}」）"
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

                # ── 校正 ──
                func_name, func_args, _hard = _correct_function_call(user_text, func_name, func_args)
                corrected_call = f"{func_name}({func_args})"

                # ── C18：clf mismatch 檢查（hard_corrected 時不蓋過）──
                mismatch, clf_intent, clf_conf = intent_clf.check_mismatch(user_text, func_name)
                if mismatch and not _hard and clf_intent != "unknown":
                    log.info(f"[C18] clf={clf_intent}({clf_conf:.2f}) vs model={func_name} → 校正")
                    func_name = intent_clf.LABEL_TO_FUNC.get(clf_intent, clf_intent)
                    # C18 改了 func_name 後，若轉成 search_log 須重新清 args
                    if func_name == "search_log":
                        from tools_v2 import _RCA_NOISE, _RCA_GENERIC
                        _raw = user_text
                        for _nz in _RCA_NOISE: _raw = _raw.replace(_nz, "")
                        for _gz in _RCA_GENERIC: _raw = _raw.replace(_gz, "")
                        _raw = _raw.strip()
                        func_args = {"keyword": _raw if _raw else func_args.get("keyword", "")}
                    corrected_call = f"[C18]{func_name}({func_args})"
                if corrected_call != raw_call:
                    log.info(f"[trace] vid={vid} corrected: {raw_call} → {corrected_call}")

                # ── C5: __help__ → 引導訪客補 slot ──
                if func_name == "__help__":
                    reason = func_args.get("reason", "")
                    if reason == "compare_missing_slot":
                        msg = ("想比較兩個倉的什麼？\n"
                               "試試這樣問：「北倉跟南倉哪個庫存比較多」「中倉跟南倉週轉率比較」")
                    else:
                        msg = "請補充更明確的訊息再試一次"
                    for ch in msg:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(0.012)
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

                # ── dispatch-ws：item_create 分步流程 ──
                if _item_create_state.get("active"):
                    if user_text.strip() == "取消":
                        _item_create_state.clear()
                        await send({"type": "token", "text": "已取消新增商品。"})
                        await send({"type": "done", "result": {"ok": True, "view": "item_cancelled", "data": {}}})
                        continue
                    import tools_v2 as _tv2_item_ws
                    st2 = _item_create_state
                    kwargs2 = {**{k: v for k, v in st2.items() if k in ("step", "name", "category", "price", "safety", "stock_north", "stock_central", "stock_south")}, "raw_text": ""}
                    if st2["step"] == 1: kwargs2["name"] = user_text
                    elif st2["step"] == 2: kwargs2["category"] = user_text
                    elif st2["step"] == 3:
                        parts = user_text.replace("，", ",").split(",")
                        if len(parts) >= 2: kwargs2["price"] = parts[0].strip(); kwargs2["safety"] = parts[1].strip()
                        else: kwargs2["price"] = user_text
                    elif st2["step"] == 4:
                        if "跳過" in user_text: kwargs2["stock_north"] = kwargs2["stock_central"] = kwargs2["stock_south"] = "0"
                        else:
                            for part in user_text.replace("，", ",").split(","):
                                p = part.strip()
                                if "北" in p: kwargs2["stock_north"] = p.replace("北", "").strip()
                                elif "中" in p: kwargs2["stock_central"] = p.replace("中", "").strip()
                                elif "南" in p: kwargs2["stock_south"] = p.replace("南", "").strip()
                    result = _tv2_item_ws.create_item_collect(**kwargs2)
                    if result.get("view") == "item_confirm":
                        _item_create_state.clear()
                    else:
                        d = result.get("data", {})
                        _item_create_state.update({k: v for k, v in d.items() if k in ("step", "name", "category", "price", "safety", "stock_north", "stock_central", "stock_south")})
                        _item_create_state["active"] = True
                    for ch in result.get("summary", ""):
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(0.012)
                    await send({"type": "done", "result": result})
                    continue

                # ── dispatch-ws：新增商品 keyword 攔截 ──
                _create_item_kws_ws = ("新增商品", "建立商品", "加一個商品", "新增一個", "加入商品", "增加商品", "新建商品")
                if any(w in user_text for w in _create_item_kws_ws):
                    import tools_v2 as _tv2_ci
                    log.info(f"[dispatch-ws] 新增商品攔截: {user_text!r}")
                    raw = user_text
                    for kw in _create_item_kws_ws: raw = raw.replace(kw, "").strip()
                    result = _tv2_ci.create_item_collect(step=1, raw_text=raw) if raw else _tv2_ci.create_item_start()
                    for ch in result.get("summary", ""):
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(0.012)
                    await send({"type": "done", "result": result})
                    _item_create_state.update({k: v for k, v in result.get("data", {}).items()
                                               if k in ("step", "name", "category", "price", "safety", "stock_north", "stock_central", "stock_south")})
                    _item_create_state["active"] = result.get("view") != "item_confirm"
                    continue

                # ── dispatch-ws：庫存排行 / 口語 pattern 攔截 ──
                _stock_rank_kws_ws = ("哪個", "哪個東西", "庫存最多", "數量最多", "哪個最多", "存貨最多", "東西最多")
                if any(w in user_text for w in _stock_rank_kws_ws) and not any(w in user_text for w in ("熱銷", "賣", "排行", "hot", "滯銷")):
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
                # ── 執行（先通知前端 tool call）──
                _arg_preview = ", ".join(f"{k}={v!r}" for k, v in list(func_args.items())[:2])
                await send({"type": "tool_call", "func": func_name, "args_preview": _arg_preview})
                result = finance.execute(func_name, func_args)
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
                        await asyncio.sleep(0.012)
                    await send({"type": "done", "result": result})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error(f"WS error: {e}", exc_info=True)
    finally:
        all_sockets.discard(ws)
        log.info(f"訪客斷線（剩 {len(all_sockets)}）")


if __name__ == "__main__":
    print(f"Starting at {get_url()}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
