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
    # 動作
    "庫存", "存量", "還有", "剩", "幾件", "多少", "幾個", "查詢",
    "怎麼用", "教我", "功能", "使用說明",
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
    # conv100-r6：缺貨/滯銷/連帶/RCA/明細 口語
    "斷炊", "吃緊", "急診", "快空", "墊底", "購物車", "黃金組合", "防蚊",
    "兜不上", "少掉", "流向", "吞吐", "業績", "存貨", "不能賣",
    # conv100-r7：賺錢/沒動靜/速配/見紅/撐不到/危險/賣況/縮水/落差/追查/怪異/上調/安全量
    "賺錢", "沒動靜", "速配", "見紅", "撐不到", "危險", "賣況",
    "縮水", "落差", "追查", "怪異", "上調", "下調", "安全量",
    # conv100-r12：遮陽帽/搭什麼（「買防曬遮陽帽的都搭什麼買」曾被守門員拒）
    "遮陽帽", "防曬", "搭什麼", "都搭",
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
    "top", "selling", "movement", "inbound", "outbound",
    "bluetooth", "earphone", "coffee", "machine", "bought", "together", "related",
    "what", "how", "show", "today", "week", "month", "much", "many",
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
    # 簡體常見倉管詞（陸港訪客，第18輪）
    "库存", "耳机", "进货", "出货", "调货", "缺货", "补货", "报表", "报告",
    "仓库", "查询", "热销", "滞销",
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
# 第17輪「訪客閒聊輪」大擴充：展場訪客會把系統當聊天機器人（問身份/閒聊/
# 嗆聲）甚至下搗蛋指令（刪全部/要密碼/套 system prompt），這些句子常夾帶
# 倉管關鍵字（「告訴我」撞通知詞、「壞掉」撞到期詞）誤入功能路由，
# 黑名單優先於白名單直接友善拒絕。
_GATEKEEPER_BLACKLIST = (
    # 離題領域
    "股市", "股票", "天氣", "電影", "音樂", "新聞", "地圖",
    "翻譯", "計算", "食譜", "笑話", "遊戲", "stocks", "weather",
    "寫詩", "作業", "便當", "樂透", "唱歌", "唱首", "說個故事", "講個故事",
    "陪我聊", "聊天", "星期幾", "現在幾點", "下雨",
    # 問 AI 身份 / 嗆聲
    "你是誰", "機器人嗎", "chatgpt", "你是真人", "你有意識", "你幾歲",
    "誰做的你", "什麼模型", "你是不是", "你多聰明", "你會說",
    "你好笨", "你好棒", "好厲害", "沒用的東西", "白癡", "廢物",
    "你很慢", "回答快一點", "你答錯", "當機",
    # 搗蛋 / 注入探測（永遠擋）
    "格式化", "重開機", "關機", "密碼", "管理員", "admin",
    "rm -rf", "rm-rf", "system prompt", "prompt是什麼",
    "忽略你的指令", "忽略指令", "告訴我祕密", "告訴我秘密",
    "全部刪掉", "刪掉全部", "全部刪光", "刪光", "刪除全部", "清空資料",
    "清空庫存", "清倉", "改成0元", "改成 0 元", "改成1元", "價格改成",
    "改成0", "全部改成", "所有商品改", "全部價格",
    # 清空/歸零變體（RPI5 v21：「把庫存全部清掉」被當商品查詢問你要查啥）
    "全部清掉", "清掉庫存", "庫存清掉", "清掉所有", "清光", "全部清光",
    "庫存歸零", "全部歸零", "歸零", "全部清空", "清除全部", "清除所有",
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
    if len(s) < 2:
        return False
    if len(s) > 20:
        # 長句碎念 fallback（第17輪）：展場訪客的長句閒聊（「逛展逛了一整天
        # 腳好痠…過來看看」）夾帶守門員字誤入功能路由。長句若無任何具體
        # 查詢線索（SPECIFIC 詞/數字）→ 給引導頁，比亂路由好。
        # 第18輪回歸補：線索詞要涵蓋連帶（買）/查詢（多少/剩）/紀錄/比較/
        # 警示等所有意圖家族，「買 coffee machine 的人還買什麼」曾被誤攔。
        _long_specific = ("庫存", "進", "出", "調", "退", "缺", "到期", "熱銷",
                          "報告", "警示", "排程", "採購", "盤點", "安全", "倉",
                          "買", "賣", "多少", "剩", "紀錄", "記錄", "明細",
                          "比較", "通知", "提醒", "月報", "報表", "體檢",
                          "對帳", "少了", "怪", "coffee", "stock", "buy")
        if (not any(w in s for w in _long_specific)
                and not re.search(r"\d", s)):
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
        "庫存", "缺貨", "斷貨", "補貨", "警示", "熱銷", "滯銷", "進貨", "出貨",
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
        "連帶", "也買", "一起買", "搭配", "帶動", "好夥伴",
        "到期", "過期", "保存期限", "效期", "保鮮", "賞味", "即期",
        "壞掉", "快壞", "快爛", "快過期",
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
    "low stock", "restock", "running low", "alert",
)

# 熱銷意圖詞（C4 用）
_HOT_INTENT_WORDS_HOT = (
    "賣最好", "最熱門", "熱銷", "暢銷", "賣最多",
    "銷量第一", "銷量冠軍", "搶手", "熱賣榜", "熱賣", "賣得最兇", "賣最兇",
    "排行榜", "銷售排行", "銷售冠軍", "人氣王", "賣翻", "銷路最好", "最好賣",
    # conv100-r6：「業績最好的商品」被 LLM 亂填 rank_type
    "業績最好", "業績冠軍",
    # conv100-r7：賺錢/賣得怎樣（「賣況」不能放這——「賣況最差」是滯銷）
    "賺錢", "賣得怎樣", "賣得如何",
    "top selling", "best seller", "hot",
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
    # conv100-r13：賣最不好
    "賣最不好", "最不好賣",
    "worst selling", "slow", "slow mover",
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
    # conv100-r14：都會多帶什麼
    "多帶", "會多帶",
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
)

# C8 search_log（RCA）：追原因/對不上/異常 —— 跟 query_movement（純進出統計）區隔
_RCA_INTENT_WORDS = (
    "對不上", "對不起來", "兜不攏", "帳不對", "短少", "短收", "少貨", "少了",
    "怎麼少", "為什麼少", "異常", "誰改的", "誰動的", "查原因", "追原因",
    "差異", "不對", "對帳", "怪怪", "莫名其妙", "有問題", "有鬼",
    # 「帳面」移除：「露營帳篷帳面上有幾頂」純存量問句被誤轉 RCA（conv100-r7；
    # corpus「純棉素T帳面跟實際差好多」有「差好多」罩住不受影響）
    "有出入", "差好多", "詭異", "蒸發",
    # conv100-r5：跳來跳去/被偷/變少/不太對勁 全退成純庫存查詢
    "跳來跳去", "被偷", "偷了", "變少", "對勁",
    # conv100-r6：兜不上/少掉/怎麼回事
    "兜不上", "少掉", "怎麼回事",
    # conv100-r7：對不太起來/縮水/怪異/落差/追查
    "對不太起來", "縮水", "怪異", "落差", "追查",
    # conv100-r11：帳對嗎
    "帳對嗎", "的帳對",
    "discrepancy", "why", "who changed", "trace",
)

# 寫入/複雜工具的「意圖詞閘門」——LLM 對閒聊句常自由發揮輸出 set_alert /
# generate_po / query_related 這類「不需要商品名就能開卡」的功能（WS 端沒有
# intent_clf 兜底時尤其嚴重）。execute 之前檢查：這些工具若句中完全沒有對應
# 意圖詞 → 判定為 LLM 幻覺，降級 rejected（第18輪訪客閒聊II抓到大量此類）。
_TOOL_INTENT_GUARD = {
    "set_alert":        ("通知", "提醒", "警示", "告訴我", "就通知", "缺貨就", "低於", "盯"),
    "generate_po":      ("採購", "補貨", "叫貨", "進貨單", "po", "下單", "開單", "該補"),
    "generate_report":  ("報告", "報表", "體檢", "健檢", "月報", "週報", "彙整", "摘要", "總結"),
    # 「一起/順便/還會」裸字太寬（「一起吃飯」誤命中 → related_empty，RPI5/WIN
    #  硬體分歧：本機 intent_clf route 判 related 繞過 C6-skip）。收緊成購物詞組。
    "query_related_items": ("買", "連帶", "搭配", "加購", "夥伴", "帶動", "連帶備貨",
                            "一起買", "一起賣", "一起結帳", "還會買", "還會帶", "也買",
                            "還配", "還扛", "順手帶", "順手拿", "順手抓", "購物車", "黃金組合",
                            "速配"),
    "search_log":       _RCA_INTENT_WORDS,
    "list_files":       ("檔", "資料夾", "目錄", "紀錄檔", "有哪些資料"),
    # run_script：需含腳本動作詞，否則閒聊句「一起吃飯」被 LLM 幻覺成
    # run_script{一起吃飯} → 執行時回「不在白名單，可用：月底盤點…」把內部
    # 腳本清單暴露給訪客（RPI5 v21 抓到）。沒動作詞 → 閘門擋成 rejected 婉拒。
    "run_script":       ("跑", "執行", "盤點", "匯出", "產出", "重產", "重新產生",
                         "重生", "重建", "做一次", "做個", "run", "export", "regenerate"),
    # query_movement：需進出貨/紀錄/期間意圖詞。閒聊句「今天過得如何」的
    # 「今天」曾讓 LLM 幻覺 movement（第19輪）。含商品名的進出貨已走 C13b
    # create_movement，這裡只擋純幻覺的空 movement。
    # 注意：閘門要涵蓋所有合法 movement 語彙——「今天入庫了什麼」「今天
    # inbound 多少」曾被誤擋（入庫/inbound 漏收）。時間詞+動作字都算合理。
    "query_movement":   ("進", "出", "貨", "紀錄", "記錄", "明細", "異動", "流水",
                         "統計", "進出", "調", "退", "入庫", "出庫", "入倉",
                         "什麼", "多少", "哪些", "賣", "銷", "補",
                         "動了", "動過", "幾次", "流向", "吞吐", "進倉",
                         "movement", "inbound", "outbound", "in", "out"),
}


def _tool_intent_ok(func_name: str, user_text: str) -> bool:
    """該工具需要意圖詞才合理時，檢查句中有沒有。沒列在 guard 裡的工具一律放行。"""
    words = _TOOL_INTENT_GUARD.get(func_name)
    if not words:
        return True
    return any(w in user_text for w in words)


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

    # Bug1: search_log 缺 RCA 意圖詞、但帶到有效商品名 → 其實是查庫存，降級 query_inventory
    if func_name == "search_log":
        cand = kw if _match_solid(kw) else _extract_sku_keyword(user_text)
        if _match_solid(cand):
            log.info(f"[gate-rescue] search_log 缺RCA詞但有商品名 → query_inventory kw={cand!r}")
            return "query_inventory", {"keyword": cand}

    # run_script 缺腳本意圖詞、但有商品名 → 其實是查庫存（RPI5 conv100-r5：
    # 「想確認一下咖啡濾紙100入的量」LLM 誤投 run_script → 閘門擋 rejected）
    if func_name == "run_script":
        cand = _extract_sku_keyword(user_text)
        if _match_solid(cand):
            log.info(f"[gate-rescue] run_script 缺腳本詞但有商品名 → query_inventory kw={cand!r}")
            return "query_inventory", {"keyword": cand}
        # 沒商品名但有進出貨語彙 → 是進出統計（「昨天有出貨嗎」LLM 誤投
        # run_script{出貨} 被閘門拒，conv100-r8）
        if any(w in user_text for w in ("出貨", "進貨", "進出", "入庫", "出庫")):
            _rs_period = ("this_month" if any(w in user_text for w in ("這個月", "本月", "月")) else
                          "yesterday" if any(w in user_text for w in ("昨天", "昨晚")) else
                          "last_week" if any(w in user_text for w in ("上週", "上禮拜")) else
                          "today" if any(w in user_text for w in ("今天", "今日")) else
                          "this_week")
            log.info(f"[gate-rescue] run_script 實為進出查詢 → query_movement period={_rs_period}")
            return "query_movement", {"period": _rs_period, "direction": "both"}

    # query_related_items 缺連帶詞、但有商品名 → 其實是查庫存（RPI5 conv100-r3：
    # 「北中南倉的滑鼠各有幾個」LLM 誤投 related → 閘門擋 rejected，該查庫存）
    if func_name == "query_related_items":
        cand = kw if _match_solid(kw) else _extract_sku_keyword(user_text)
        if _match_solid(cand):
            log.info(f"[gate-rescue] query_related 缺連帶詞但有商品名 → query_inventory kw={cand!r}")
            return "query_inventory", {"keyword": cand}

    # query_movement 缺進出詞、但有商品名 → 查該商品分倉庫存
    # （「藍牙喇叭中倉南倉哪邊多」LLM 誤投 movement 被閘門拒，conv100-r9）
    if func_name == "query_movement":
        cand = kw if _match_solid(kw) else _extract_sku_keyword(user_text)
        if _match_solid(cand):
            log.info(f"[gate-rescue] query_movement 缺進出詞但有商品名 → query_inventory kw={cand!r}")
            return "query_inventory", {"keyword": cand}

    # Bug2: set_alert 缺意圖詞、但句含「安全庫存」+設定動作詞 → 是改設定，轉 manage_config
    # （C9 校正故意跳過 set_alert，導致這種句子一路走到閘門被 reject。這裡用
    #  跟 C9 相同的 args 組法救回：action/key/warehouse/value 結構化參數。）
    if func_name == "set_alert":
        if any(k in user_text for k in _CONFIG_KEY_WORDS) and \
           any(a in user_text for a in _CONFIG_SET_WORDS):
            _action = ("set" if not (any(w in user_text for w in _CONFIG_READ_CUES)
                       and _extract_config_value(user_text) is None) else "read")
            _key = next((w for w in _CONFIG_KEY_WORDS if w in user_text), "安全庫存")
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
_CONFIG_KEY_WORDS = ("安全庫存", "安全存量", "安全水位", "前置天數", "補貨前置",
                     "安全水位倍數", "補貨目標天數", "警戒值", "補貨天數", "安全量",
                     "lead", "safety stock")
_CONFIG_SET_WORDS = ("改成", "設成", "設為", "調成", "調到", "改為", "設定為",
                     "調高", "調低", "提高", "提升", "降低", "降", "加", "減", "+", "改", "設",
                     "調升", "調降", "上修", "下修", "升到", "降到",
                     "訂在", "訂為", "定在", "定為", "縮短成", "縮短到", "縮成",
                     "歸", "拉長", "拉長到", "延長到", "加長到",
                     "上調", "下調", "壓到", "改回")
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
_DA_TAIL = r"(?:用)?的(?:機器|那台|那個|東西)?"
_DESCRIPTOR_ALIASES = (
    # ── 家電廚具 ──
    (_re.compile(r"(?<!手)(?:[煮泡沖磨]咖啡)" + _DA_TAIL), "咖啡機"),
    (_re.compile(r"(?:刷牙|潔牙)" + _DA_TAIL), "電動牙刷"),
    (_re.compile(r"(?:[燙熨]衣(?:服)?|除皺)" + _DA_TAIL), "電熨斗"),
    (_re.compile(r"(?:[打榨](?:果)?汁|打果昔|打冰沙)" + _DA_TAIL), "果汁機"),
    (_re.compile(r"(?:拖地|除塵|擦地)" + _DA_TAIL), "拖把"),
    (_re.compile(r"(?:炒菜|煎[東蛋]西?|煎煮)" + _DA_TAIL), "不沾鍋"),
    (_re.compile(r"(?:[悶燜][湯粥]|保溫湯|[裝帶]湯)" + _DA_TAIL), "悶燒罐"),
    (_re.compile(r"(?:裝剩菜|保鮮|裝便當)" + _DA_TAIL), "保鮮盒"),
    (_re.compile(r"(?:野炊|露營煮飯?)" + _DA_TAIL), "野炊鍋具"),
    # ── 電子產品 ──
    (_re.compile(r"[塞掛戴]耳朵" + _DA_TAIL), "無線藍牙耳機"),
    (_re.compile(r"(?:出門|隨身|行動)充電" + _DA_TAIL), "行動電源"),
    (_re.compile(r"(?:充電|傳輸)(?:用)?的線"), "快充線"),
    (_re.compile(r"(?:放音樂|外放)" + _DA_TAIL), "藍牙喇叭"),
    (_re.compile(r"(?:計步|量心跳|測心率|戴手[上腕]量?)" + _DA_TAIL), "智慧手環"),
    (_re.compile(r"包手機" + _DA_TAIL), "防摔殼"),
    (_re.compile(r"保護手機" + _DA_TAIL), "防摔殼"),
    (_re.compile(r"裝(?:筆電|電腦)" + _DA_TAIL), "筆電包"),
    (_re.compile(r"打字" + _DA_TAIL), "鍵盤"),
    (_re.compile(r"(?:吹風|吹涼|消暑)" + _DA_TAIL), "風扇"),
    # ── 食品飲料 ──
    (_re.compile(r"有氣的水"), "氣泡水"),
    (_re.compile(r"(?:會醉的|有酒精的)"), "精釀啤酒"),
    (_re.compile(r"(?:健身喝|練完喝)" + _DA_TAIL), "乳清"),
    (_re.compile(r"(?:運動喝|流汗喝)" + _DA_TAIL), "運動飲"),
    (_re.compile(r"巧克力(?:粉|飲|牛奶)?"), "熱可可粉"),
    (_re.compile(r"掛耳(?:咖啡|包)"), "濾掛咖啡"),
    (_re.compile(r"(?:蘇打)?餅乾"), "蘇打餅"),
    # ── 日用品 ──
    (_re.compile(r"洗衣(?:服)?" + _DA_TAIL), "洗衣精"),
    (_re.compile(r"(?:洗澡|洗身體)" + _DA_TAIL), "沐浴乳"),
    (_re.compile(r"(?:防蚊|驅蚊|防蚊蟲)" + _DA_TAIL), "防蚊液"),
    (_re.compile(r"(?:插電的?蚊香|電蚊香)(?:液)?"), "蚊香液"),
    (_re.compile(r"擦屁股" + _DA_TAIL), "衛生紙"),
    (_re.compile(r"包屁股" + _DA_TAIL), "紙尿布"),
    (_re.compile(r"裝垃圾" + _DA_TAIL), "垃圾袋"),
    # 「清潔手套」的「清潔」會被 RPI5 LLM 當類別詞跑去 clarify → 用全名
    (_re.compile(r"(?:洗碗|做家事)戴?" + _DA_TAIL), "橡膠清潔手套"),
    # ── 服飾 ──
    (_re.compile(r"(?:遮太陽|遮陽|防曬)" + _DA_TAIL), "遮陽帽"),
    (_re.compile(r"冬天戴" + _DA_TAIL), "毛帽"),
    (_re.compile(r"冬天穿" + _DA_TAIL), "羽絨外套"),
    (_re.compile(r"(?:跑步|慢跑)[穿用]" + _DA_TAIL), "慢跑鞋"),
    # ── 運動用品 ──
    (_re.compile(r"(?:[做練]瑜[珈伽]|拉筋)" + _DA_TAIL), "瑜珈墊"),
    (_re.compile(r"(?:裝水|喝水)" + _DA_TAIL), "水壺"),
    (_re.compile(r"(?:舉重|重訓|練肌肉|練二頭肌?)" + _DA_TAIL), "啞鈴"),
    (_re.compile(r"拉力環"), "健身環"),
    (_re.compile(r"擦汗" + _DA_TAIL), "運動毛巾"),
    (_re.compile(r"露營[睡搭]" + _DA_TAIL), "帳篷"),
    (_re.compile(r"露營坐" + _DA_TAIL), "露營椅"),
    (_re.compile(r"照明" + _DA_TAIL), "露營燈"),
)


def _descriptor_hit(user_text: str) -> str | None:
    """描述句偵測（rewrite 之前呼叫——rewrite 會把描述換掉）。命中回傳商品關鍵字。"""
    t = user_text.strip().translate(_S2T)
    for _dh_pat, _dh_name in _DESCRIPTOR_ALIASES:
        if _dh_pat.search(t):
            return _dh_name
    return None


def _rewrite_query(user_text: str) -> str:
    """將口語/模糊輸入改寫成 LLM 訓練時的標準句型。"""
    t = user_text.strip().translate(_S2T)
    # 亂敲重複詞收斂：「庫存庫存庫存庫存庫存」→「庫存」（conv100-r7b 亂打組）
    _rep_m = _re.fullmatch(r"(.{1,4})\1{2,}", t)
    if _rep_m:
        t = _rep_m.group(1)
        log.info(f"[Rewrite] 重複詞收斂 → 「{t}」")
    # 功能描述改寫（表定義在函式上方）
    for _da_pat, _da_name in _DESCRIPTOR_ALIASES:
        _da_new = _da_pat.sub(_da_name, t)
        if _da_new != t:
            log.info(f"[Rewrite] 功能描述 →「{_da_name}」: 「{t}」→「{_da_new}」")
            t = _da_new
            break
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
    # 熱銷 rewrite 同病：「這個月熱銷排行」被改成固定句「熱銷商品排行」→ 時間詞
    # 銷毀，C4b 拿改寫後句子校 period 校不回 this_month（conv100-r8）
    _hot_keep = any(w in t for w in ("本月", "這個月", "上個月", "月", "今天", "本週", "這週"))
    _GENERIC_RCA_HEADS = ("庫存", "數量", "進貨", "帳", "對不上", "差異")
    for pattern, replacement in _REWRITE_RULES:
        if _cmp_keep and replacement == "比較各倉庫庫存":
            continue
        if _hot_keep and replacement == "熱銷商品排行":
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
    if any(w in t for w in _RCA_INTENT_WORDS):
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
    # match_items 只做整句子字串比對，句子含雜訊（動詞/數量詞）時比對不到
    # （「咖啡機剛進100包到北倉」match_items 抓不到「咖啡機」）。
    # 用 _extract_sku_keyword 的多層 fuzzy 邏輯再試一次，比較不會漏判。
    if not _has_product:
        _has_product = bool(_extract_sku_keyword(t))
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
                  "缺貨清單轉採購", "缺貨的產", "幫我補貨", "產po",
                  # 「出一張採購單」「列成採購單」這類明確開單動詞（第9輪測試補）
                  "出一張", "開一張", "生一張", "出採購", "列成採購", "列採購", "該補的",
                  # 「幫我開單採購」（第10輪測試補）
                  "開單採購", "開單補貨", "開單",
                  # 「開進採購單」「擬一張採購草稿」「轉採購單」（第11輪測試補）
                  "開進採購", "擬一張", "擬張", "擬採購", "轉採購", "開張採購")
    has_po_intent = any(w in user_text for w in _po_kw)
    has_po_direct = any(w in user_text for w in _po_direct)
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
    _vague = {"查", "查詢", "看", "確認", "了解", "瞭解", "問一下", "查一下", "看一下", "看看", "那個", "這個", "欸", "誒", "喂", "嗨", "查個東西", "有個問題", "有問題", "問題",
              "然後", "然後呢", "接下來", "接下來呢", "接著呢", "再來", "再來呢",
              "有人在嗎", "有人嗎", "在嗎", "哈囉", "你好", "喂喂"}
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
    for ch in s:
        if ch == "千":
            section += (digit or 1) * 1000
            total += section; section = 0; digit = 0
        elif ch == "百":
            section += (digit or 1) * 100
            digit = 0
        elif ch == "十":
            section += (digit or 1) * 10
            digit = 0
        elif ch in _CN_NUM:
            digit = _CN_NUM[ch]
        else:
            return None
    result = total + section + digit
    return result if result > 0 else None


# 數字部分：阿拉伯 or 中文，用於 manage_config 的 value 抽取
_NUM_PART = r'([0-9]+|[零一二兩三四五六七八九十百千]+)'


def _extract_config_value(user_text: str):
    """從句子抽 manage_config 的設定值，回傳字串（"+30"/"-15"/"100"）或 None。
    同時支援阿拉伯與中文數字（2026-07-02：「改成五天」「調到一百」原本中文
    數字整段漏抽）。相對值（加/提高/降低）帶正負號，絕對值（改成/調到）純數字。"""
    import re as _re
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
        n = _cn_to_int(_abs_to.group(1))
        return str(n) if n is not None else None
    if _rel_pos:
        n = _cn_to_int(_rel_pos.group(1))
        return f"+{n}" if n is not None else None
    if _rel_neg:
        n = _cn_to_int(_rel_neg.group(1))
        return f"-{n}" if n is not None else None
    if _abs:
        n = _cn_to_int(_abs.group(1))
        return str(n) if n is not None else None
    return None


def _config_item_kw(user_text: str) -> str:
    """從 config 句抽商品名（「瑜珈墊的安全庫存加20」→「瑜珈墊」）。
    LLM 幾乎不會把商品塞進 manage_config 參數，導致影響範圍變全部商品 183 項
    （conv100-r5）。剝掉設定詞/動詞/倉名/數字後 fuzzy 比對，比不到真商品回空字串。"""
    import re as _re_ci
    import warehouse as _W_ci
    t = user_text
    for w in _CONFIG_KEY_WORDS + _CONFIG_SET_WORDS + (
            "安全", "水位", "庫存", "天數", "前置", "設定", "警戒",
            "北倉", "中倉", "南倉", "北區", "中區", "南區", "全部", "所有", "三倉",
            "幫我", "麻煩", "請", "把", "的", "商品", "全部商品"):
        t = t.replace(w, " ")
    t = _re_ci.sub(r'[0-9]+|[零一二兩三四五六七八九十百千]+|[天件個]', ' ', t)
    kw = _extract_sku_keyword(t.strip())
    # 注意：_extract_sku_keyword 命中商品時回的是「全名」（可能含空格，如
    # 「瑜珈墊 6mm」），不能拿空格當雜訊判準——雜訊靠 match 低分濾掉即可
    if not kw:
        return ""
    m = _W_ci.match_items(kw)
    if m and m[0].get("score", 0) >= 3:
        return kw
    return ""


_CAT_GROUND_WORDS = {
    "electronics": ("電子", "3c"), "appliance_kitchen": ("家電", "廚具", "廚房"),
    "food_beverage": ("食品", "飲料"), "daily_goods": ("日用", "生活用品"),
    "apparel": ("服飾", "衣服", "服裝"), "sports": ("運動", "露營", "戶外", "健身"),
}


def _drop_ungrounded_category(func_args: dict, user_text: str) -> dict:
    """LLM 常幻覺 category（「彈力健身環庫存」給 apparel 把 sports 商品濾光
    變成找不到，conv100-r13）→ 句中沒對應類別詞就丟棄。"""
    _cat = func_args.get("category")
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
    _qty13a_m = _re13a.search(
        r'([0-9]+|[零一二兩三四五六七八九十百千]+)\s*'
        # 「個」排除「三個倉」（曾把倉數吃成 qty=3，conv100-r6）；單位補「盞」
        r'(?:件|個(?!月|星期|禮拜|小時|鐘頭|倉)|條|支|台|箱|包|瓶|罐|組|雙|套|盒|對|頂|張|把|副|顆|粒|袋|桶|杯|塊|片|卷|捲|盞)', user_text)
    _qty13a_int = _cn_to_int(_qty13a_m.group(1)) if _qty13a_m else None
    # 動詞跟介系詞被商品隔開的句型：「北倉送20個藍牙耳機到南倉」的「送…到」
    # 子字串比對不到（第11輪抓到）。兩倉名+數量的前提下跨距比對安全。
    _sep_verb_m = _re13a.search(r'[送運搬移調撥挪轉勻分抓撤].{0,18}?[到去給回]', user_text)
    # 「北倉給南倉12瓶X」句型：倉名+給+倉名，沒有其他調貨動詞也算（conv100-r5）
    _wh_give_wh_m = _re13a.search(r'[北中南](?:區倉|區|倉)?\s*給\s*[北中南]', user_text)
    _has_transfer_verb = (any(w in user_text for w in _transfer_verbs)
                          or _sep_verb_m is not None or _wh_give_wh_m is not None)
    # 句中有外部對象（供應商/客戶）→ 是進出貨不是調貨，讓給 C13b
    # （conv100-r5：「供應商剛送到一批瑜珈墊 25張放南倉」的「送到」被搶成調貨 clarify）
    if any(w in user_text for w in ("供應商", "廠商", "客戶", "客人", "顧客")):
        _has_transfer_verb = False
    # 模糊量詞（「調一批悶燒罐到南倉」無精確數字）：有調貨動詞+兩倉時也算調貨，
    # qty 留 None 讓 create_transfer clarify 問數量（RPI5 conv100-r2：原本落 config）
    _vague_qty13a = any(w in user_text for w in ("一批", "一些", "些", "若干", "幾個", "幾件",
                        "一點", "一部分", "部分", "一半", "半數", "分點", "勻一點", "勻些",
                        "平均", "平分"))
    # 兩個不同倉名（北/中/南去重後 >= 2）才算調貨
    _wh_mentions13a = [w for w in ("北倉", "北區倉", "北區", "中倉", "中區倉", "中區",
                                    "南倉", "南區倉", "南區") if w in user_text]
    _wh_keys13a = {w[0] for w in _wh_mentions13a}
    if (func_name != "create_transfer" and _has_transfer_verb
            and (_qty13a_int is not None or _vague_qty13a)
            and (len(_wh_keys13a) >= 2
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
        _to_m = _re13a.search(r'(?:到|去|過去|給|往|支援|回)\s*([北中南])', user_text)
        if _to_m:
            _to_key = _to_m.group(1)
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
            _oov_ok = (_kw13a and " " not in _kw13a and 3 <= len(_kw13a) <= 8
                       and not any(g in _kw13a for g in ("庫存", "東西", "商品", "的貨", "一些")))
            log.info(f"[校正 C13a] 調貨但商品名抽壞/低分 kw={_kw13a!r} → "
                     f"{'OOV clarify' if _oov_ok else '查詢概覽'}")
            return "query_inventory", {"keyword": _kw13a if _oov_ok else ""}, True
        log.info(f"[校正 C13a] 調貨意圖 → create_transfer kw={_kw13a!r} from={_from_zh!r} to={_to_zh!r} qty={_qty13a_int}")
        return "create_transfer", {"keyword": _kw13a, "from_wh": _from_zh,
                                    "to_wh": _to_zh, "qty": str(_qty13a_int)}, True

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
                           "取貨")
    _is_return13b = any(w in user_text for w in _movement_return_words)
    _has_movement_word = any(w in user_text for w in _movement_in_words + _movement_out_words)
    # 單獨「進」「出」風險較高（「進去看看」也含「進」），只在句子裡緊接著數字+量詞
    # 時才承認為進出貨動詞（「南區進登山杖100盒」的「進」緊挨著商品名跟數量）。
    import re as _re13b_single
    _single_dir_m = _re13b_single.search(r'[進出](?=[一-鿿]{0,8}(?:[0-9]+|[零一二兩三四五六七八九十百千]+)\s*(?:件|個(?!月|星期|禮拜|小時|鐘頭|倉)|條|支|台|箱|包|瓶|罐|組|雙|套|盒|對|頂|張|把|副|顆|粒|袋|桶|杯|塊|片|卷|捲|盞))', user_text)
    if _single_dir_m and not _has_movement_word:
        _has_movement_word = True
        if _single_dir_m.group(0) == "進":
            _movement_in_words = _movement_in_words + ("進",)
        else:
            _movement_out_words = _movement_out_words + ("出",)
    # 量詞放寬：件/個/條/支/台/箱/包/瓶/罐/組/雙/套/盒；數字可能是阿拉伯或中文
    #   （「三箱」「十個」這種口語，2026-07-02 實測「剛剛入庫三箱衛生紙」抓到：
    #    原本正則只認阿拉伯數字，中文數字全漏，整句 C13b 不觸發跌回誤判）。
    import re as _re13b_pre
    _qunit = r'(?:件|個(?!月|星期|禮拜|小時|鐘頭|倉)|條|支|台|箱|包|瓶|罐|組|雙|套|盒|對|頂|張|把|副|顆|粒|袋|桶|杯|塊|片|卷|捲|盞)'
    _qty_re = r'([0-9]+|[零一二兩三四五六七八九十百千]+)\s*' + _qunit
    _qty13b_m = _re13b_pre.search(_qty_re, user_text)
    # 「數量35」這種無量詞寫法（「南倉補進來一批防蚊液 數量35」，conv100-r7）
    if not _qty13b_m:
        _qty13b_m = _re13b_pre.search(r'數量\s*([0-9]+)', user_text)
    # 中文數字要能真的轉成整數才算數（避免「幾個」的「幾」等非數字被誤收）
    _qty13b_int = _cn_to_int(_qty13b_m.group(1)) if _qty13b_m else None
    _has_explicit_qty = _qty13b_int is not None

    if func_name != "create_movement" and _has_movement_word and _has_explicit_qty:
        # 句子同時提到兩個以上倉別時（如「北倉跟南倉的藍牙耳機各出貨了10個跟15個」），
        # 單一 create_movement 呼叫無法表達「哪個倉對應哪個數量」，硬猜容易猜錯真的
        # 異動錯倉庫。刻意不解析出 warehouse（留空），讓 tools_v2.create_movement
        # 既有的「倉別不明 → clarify」分支接手，請使用者拆成一句一倉分別描述。
        _wh_mentions13b = [w for w in ("北倉", "北區倉", "北區", "中倉", "中區倉", "中區",
                                        "南倉", "南區倉", "南區") if w in user_text]
        _wh_keys13b = {w[0] for w in _wh_mentions13b}  # 北/中/南 去重
        _dir13b = "in" if any(w in user_text for w in _movement_in_words) else "out"
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
        _kw13b = _extract_sku_keyword(_pre_clean) or _extract_sku_keyword(user_text) or ""
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
        log.info(f"[校正 C13b] 進出貨意圖 → create_movement（原 {func_name}）kw={_kw13b!r} wh={_wh13b!r} dir={_dir13b} qty={_qty13b!r} return={_is_return13b}")
        _args13b = {"keyword": _kw13b, "warehouse": _wh13b,
                    "direction": _dir13b, "qty": _qty13b}
        if _is_return13b:
            _args13b["is_return"] = True
        return "create_movement", _args13b, True

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
                         and any(w in user_text for w in _RCA_INTENT_WORDS))
        if not _cmp_rca_leak:
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
    # 「系統壞掉了啦」的「壞掉」是抱怨系統不是問效期（conv100-r9）→ 排除機器語境
    _c7_sys_ctx = any(w in user_text for w in ("系統", "當機", "網站", "機器", "程式", "app"))
    if (any(kw in user_text for kw in _EXPIRING_INTENT_WORDS) or
        any(kw in text_low for kw in _EXPIRING_INTENT_WORDS)) and not _has_report and not _has_alert \
            and not _c7_sys_ctx:
        # category 幻覺防呆：句中沒類別詞就丟棄（「到期壓力最大的是哪批貨」被 LLM
        # 塞 apparel 回「服飾類沒有快到期」漏報全局，conv100-r6）
        _c7_cat_words = {"electronics": ("電子", "3c"), "appliance_kitchen": ("家電", "廚具", "廚房"),
                         "food_beverage": ("食品", "飲料"), "daily_goods": ("日用", "生活用品"),
                         "apparel": ("服飾", "衣服", "服裝"), "sports": ("運動", "露營", "戶外")}
        _c7_cat = func_args.get("category")
        _c7_cat_ok = _c7_cat in VALID_CATEGORIES and any(
            w in user_text for w in _c7_cat_words.get(_c7_cat, ()))
        if func_name != "list_expiring_items":
            log.info(f"[校正 C7] {func_name} → list_expiring_items (到期意圖)")
            new_args = {}
            if func_args.get("warehouse") in VALID_WAREHOUSES:
                new_args["warehouse"] = func_args["warehouse"]
            if _c7_cat_ok:
                new_args["category"] = _c7_cat
            return "list_expiring_items", new_args, True
        elif _c7_cat and not _c7_cat_ok:
            func_args = {k: v for k, v in func_args.items() if k != "category"}
            log.info(f"[校正 C7] 丟棄幻覺 category={_c7_cat}")

    # ── C3: 缺貨意圖詞 → list_low_stock（最高優先、bypass 其他校正）──
    #   排除：句中含設定項詞（安全庫存/前置天數）時讓給 C9；含報表/報告詞時讓給 C12。
    # 「天數」入列：「中倉補貨天數縮短成3天」的「補貨」曾把 config 句搶成缺貨清單（conv100-r5）
    _cfg_key_in_text = any(w in user_text for w in
                           ("安全庫存", "安全存量", "安全水位", "前置天數", "補貨前置", "前置時間",
                            "天數", "警戒值"))
    _report_in_text = any(w in user_text for w in ("報表", "報告", "體檢", "健檢"))
    # 「告訴我」收斂成「就告訴我」：「見底的貨順便告訴我要補幾個」不是警示設定，
    # 曾被這裡排除掉落到 C6 亂轉 related（conv100-r5）
    _alert_in_text = any(w in user_text for w in ("通知", "提醒", "警示我", "就通知", "就提醒", "就告訴我"))
    # 「叫貨」從 PO 排除詞移除：叫貨=缺貨要補的查詢語意，讓 C3 轉 low_stock
    # （開採購單是「採購單/下單/產採購/補貨單」等明確 PO 詞，RPI5 conv100-r2）
    _po_in_text = any(w in user_text for w in ("採購單", "下單", "產採購", "補貨單"))
    # 「XX最近有補貨嗎」是問進貨紀錄不是缺貨清單 → 讓給 C7b movement（conv100-r13）
    _mv_q_in_text = any(w in user_text for w in ("有補貨", "有進貨", "補過貨", "進過貨"))
    if (any(kw in user_text for kw in _LOW_STOCK_INTENT_WORDS) or
        any(kw in text_low for kw in _LOW_STOCK_INTENT_WORDS)) \
       and not _cfg_key_in_text and not _report_in_text \
       and not _alert_in_text and not _po_in_text and not _mv_q_in_text:
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
            if _c3_cat_ok:
                new_args["category"] = _c3_cat
            return "list_low_stock", new_args, True
        else:
            # LLM 已正確輸出 list_low_stock，但後續 C14 看到「警示」會誤覆蓋成 set_alert
            # → hard-return 防止被後面規則（C14 等）推翻
            if _c3_cat and not _c3_cat_ok:
                func_args = {k: v for k, v in func_args.items() if k != "category"}
                log.info(f"[校正 C3] 丟棄幻覺 category={_c3_cat}")
            return func_name, func_args, True

    # ── C4: 熱銷 / 滯銷意圖詞 → list_hot_items ──
    is_hot = any(kw in user_text for kw in _HOT_INTENT_WORDS_HOT) or \
             any(kw in text_low for kw in _HOT_INTENT_WORDS_HOT)
    is_slow = any(kw in user_text for kw in _HOT_INTENT_WORDS_SLOW) or \
              any(kw in text_low for kw in _HOT_INTENT_WORDS_SLOW)
    # 連帶意圖詞在場時熱銷不搶——「帳篷跟什麼一起賣最多」的「賣最多」是
    # 連帶語境，不是排行榜（第14輪抓到）
    _c4_related_block = any(w in user_text for w in _RELATED_INTENT_WORDS)
    # 帶具體商品名的熱銷問句（「輕量羽絨外套最近賣得如何」）是問該商品銷況，
    # 回全類別排行答非所問 → 轉該商品 movement（conv100-r13）
    if (is_hot or is_slow) and not _c4_related_block \
            and not any(w in user_text for w in ("類", "用品")):
        # （「露營用品類賣最好」是類別排行，fuzzy 會誤中帳篷 → 類/用品 句不轉）
        import warehouse as _W_c4p
        _c4_prod = _extract_sku_keyword(user_text)
        _c4_pm = _W_c4p.match_items(_c4_prod) if _c4_prod else []
        if _c4_pm and _c4_pm[0].get("score", 0) >= 3:
            _c4_period = ("this_month" if any(w in user_text for w in ("本月", "這個月", "月")) else "this_month")
            log.info(f"[校正 C4-prod] 帶商品名的銷況問句 → query_movement kw={_c4_prod!r}")
            return "query_movement", {"keyword": _c4_prod, "period": _c4_period,
                                      "direction": "both"}, True
    if (is_hot or is_slow) and not _c4_related_block and func_name != "list_hot_items":
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
        if func_args.get("rank_type") not in ("hot", "slow"):
            func_args = {**func_args, "rank_type": "slow" if is_slow else "hot"}
            log.info(f"[校正 C4] rank_type 校準 → {func_args['rank_type']}")
        # period 也要一起校——hard-return 會跳過 C4b（「這個月熱銷排行」曾顯示本週，conv100-r8）
        _c4p = ("this_month" if any(w in user_text for w in ("本月", "這個月", "月度")) else "this_week")
        if func_args.get("period") != _c4p:
            func_args = {**func_args, "period": _c4p}
            log.info(f"[校正 C4] period 校準 → {_c4p}")
        # category 也是（「電子產品賣得如何」曾回全類別，conv100-r8）
        if func_args.get("category") not in VALID_CATEGORIES:
            for _zh4, _cat4 in {"電子": "electronics", "3c": "electronics",
                                "家電": "appliance_kitchen", "廚具": "appliance_kitchen",
                                "食品": "food_beverage", "飲料": "food_beverage",
                                "日用": "daily_goods", "服飾": "apparel", "衣服": "apparel",
                                "運動": "sports", "露營": "sports", "戶外": "sports"}.items():
                if _zh4 in user_text:
                    func_args = {**func_args, "category": _cat4}
                    log.info(f"[校正 C4] 補 category={_cat4}")
                    break
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
        if _c2d_inv_cue and not _c2d_mv_cue:
            _kw2d = _extract_sku_keyword(user_text)
            # kw 要真的比對得到商品才帶——「塞 貨」這種殘字 hard-return 後
            # 沒人清得掉，會 clarify 找不到（conv100-r8）
            import warehouse as _W2d
            _m2d = _W2d.match_items(_kw2d) if _kw2d else []
            if not _m2d or _m2d[0].get("score", 0) < 3:
                _kw2d = ""
            log.info(f"[校正 C2d] 存量問句誤投 movement → query_inventory kw={_kw2d!r}")
            _a2d = {"keyword": _kw2d} if _kw2d else {}
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
            if kw and (not func_args.get("keyword")
                       or not _WC6.match_items(func_args.get("keyword", ""))):
                func_args = {**func_args, "keyword": kw}
            func_args = _drop_ungrounded_category(func_args, user_text)
            return func_name, func_args, True

    # ── C2e: 原句明講昨天/上週 → 覆寫 period（LLM 常給「合法但錯」的
    #   this_week，容錯 map 只救非法值管不到，2026-07-06 加 yesterday/last_week
    #   支援時抓到）──
    if func_name == "query_movement":
        _c2e = ("yesterday" if any(w in user_text for w in ("昨天", "昨晚", "昨日")) else
                "last_week" if any(w in user_text for w in ("上週", "上周", "上禮拜")) else None)
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

    # ── C1: query_inventory 沒抽到 keyword 但 user_text 含商品意圖詞 → 補 keyword ──
    if func_name == "query_inventory":
        kw = func_args.get("keyword")
        cat = func_args.get("category")
        if not kw and not cat:
            # 若 user_text 含意圖詞 → 把去掉意圖詞跟時間詞的剩餘字當 keyword
            if any(w in user_text for w in _INVENTORY_INTENT_WORDS):
                cleaned = _extract_sku_keyword(user_text)
                if cleaned and len(cleaned) >= 2 and _kw_grounded(cleaned, user_text):
                    log.info(f"[校正 C1] query_inventory 補 keyword: {cleaned!r}")
                    func_args = dict(func_args)
                    func_args["keyword"] = cleaned

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
            "yesterday": "yesterday", "昨天": "yesterday", "昨日": "yesterday",
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
            "最滿", "分布", "佔比", "哪個最", "誰最"))
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

    # C13：明確查庫存意圖 + SKU → hard-return query_inventory（防止 C18 誤覆蓋）
    # RCA 意圖詞（對帳/異常/少了）優先於 C13，不搶。
    # 含設定項詞（安全庫存/前置天數）時也不搶——「現在安全庫存是多少」的
    # 「庫存」曾讓 C13 在 C9 之前 hard-return 搶走 config 查詢（第15輪抓到）
    _c13_has_rca = any(w in user_text for w in _RCA_INTENT_WORDS)
    _c13_has_cfg = any(w in user_text for w in _CONFIG_KEY_WORDS)
    _inv_intent = ("庫存", "剩多少", "還有多少", "有多少", "幾個", "數量", "查庫存",
                   "還剩", "幾件", "存貨",
                   "inventory", "stock", "查一下庫存", "看庫存", "查看庫存")
    # 「賣了幾件」是銷售統計不是存量——C13 不可搶（conv100-r7）
    _c13_has_sale = any(w in user_text for w in ("賣了", "售出", "賣出", "賣掉"))
    if (not _c13_has_rca and not _c13_has_cfg and not _c13_has_sale
            and any(w in user_text for w in _inv_intent) and func_name == "query_inventory"):
        kw = _extract_sku_keyword(user_text) or func_args.get("keyword", "")
        if kw and not _kw_grounded(kw, user_text):
            kw = ""  # fuzzy 亂中的全名不可信（conv100-r8）
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
            # hard-return 會跳過 C17a 的 warehouse 補全 → 單倉句在這裡補
            # （「耳機在南倉有幾個」曾少了南倉 filter，conv100-r12）
            _c13_args = _drop_ungrounded_category({**func_args, "keyword": kw}, user_text)
            _c13_whs = {z[0] for z in ("北倉", "北區", "中倉", "中區", "南倉", "南區") if z in user_text}
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
    if _c7b_hit:
        kw = func_args.get("keyword", "") or _extract_sku_keyword(user_text)
        # keyword 髒掉（帶時間/疑問詞，如「最近 進什麼貨」）比對不到商品就丟掉
        # → 全品項進出統計；否則後面 OOV 檢查會誤判成「找不到商品」clarify
        if kw:
            import warehouse as _WC7
            if not _WC7.match_items(kw):
                kw = ""
        # period 從原句推斷（hard-return 會跳過後面的 C2 時間詞規則，
        # 「最近一個月進貨多少」曾顯示成今天的數字，第14輪抓到）
        _c7b_period = ("this_month" if any(w in user_text for w in ("這個月", "本月", "一個月", "上個月", "月")) else
                       "yesterday" if any(w in user_text for w in ("昨天", "昨晚", "昨日")) else
                       "last_week" if any(w in user_text for w in ("上週", "上周", "上禮拜")) else
                       "this_week" if any(w in user_text for w in ("這週", "本週", "這禮拜", "週", "禮拜", "前天")) else
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

    has_rca    = any(w in user_text for w in _RCA_INTENT_WORDS)
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
    if has_cfgkey and (func_name not in ("manage_config", "set_alert") or _c9_needs_fix):
        # 「多少」是問句語氣（「設定多少」「是多少」），有它一律當 read——
        # 曾經只擋「是多少/設多少」，「補貨前置天數設定多少」被「設」搶成 set 而報錯
        action = "set" if has_cfgset and not (any(w in user_text for w in _CONFIG_READ_CUES) and _extract_config_value(user_text) is None) else "read"
        # 抽 key
        key = next((w for w in _CONFIG_KEY_WORDS if w in user_text), "安全庫存")
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
                     "掃一遍", "整理一份", "彙整", "report", "做份報告", "產生報告",
                     "月報", "週報", "年報", "日報", "營運摘要", "匯總")
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

    # C14：警示設定意圖 → set_alert（自動化工具）
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
            _key_hit = next((w for w in _CONFIG_KEY_WORDS if w in user_text), None)
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
    import re as _re
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

    if func_name == "query_inventory" and func_args.get("warehouse") in ("north", "central", "south"):
        _wh_zh_names = {"north": ("北倉", "北區", "北邊", "北部"),
                        "central": ("中倉", "中區"),
                        "south": ("南倉", "南區", "南邊", "南部")}
        _c17ap_whs = {z[0] for z in ("北倉", "北區", "中倉", "中區", "南倉", "南區") if z in user_text}
        if not any(z in user_text for z in _wh_zh_names[func_args["warehouse"]]):
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
        elif _kw_wh and any(w in _kw_wh for w in ("全店", "總庫存", "全部商品", "所有商品")):
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

    # 通用 category 接地檢查（inventory/related 直達路徑，conv100-r13）
    if func_name in ("query_inventory", "query_related_items"):
        func_args = _drop_ungrounded_category(func_args, user_text)

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
_item_create_state: dict = {}
_item_delete_state: dict = {}  # 刪除模式的 session state
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
_CTX_FUNC_HINT = {
    "進出": "query_movement", "異動": "query_movement", "紀錄": "query_movement",
    "搭配": "query_related_items", "推薦": "query_related_items",
    "到期": "list_expiring_items", "保存": "list_expiring_items",
}

def _update_ctx(vid, func_name: str, func_args: dict):
    """每輪成功執行後更新該訪客的 context。"""
    _ctx = _ctx_for(vid)
    kw = func_args.get("keyword") or func_args.get("target") or func_args.get("script_name")
    wh = func_args.get("warehouse")
    if kw:
        _ctx["last_sku"] = kw
    if wh and wh not in ("all", None):
        _ctx["last_wh"] = wh
    _ctx["last_func"] = func_name

def _resolve_followup(vid, user_text: str, func_name: str, func_args: dict):
    """
    若 user_text 是追問句（含代詞/倉庫切換）且 func_args 沒有 keyword，
    嘗試從該訪客的 _ctx 補上 last_sku / last_wh。
    回傳 (new_func_name, new_func_args) 或原值。
    """
    _ctx = _ctx_for(vid)
    if not _ctx.get("last_sku"):
        return func_name, func_args
    is_followup = any(w in user_text for w in _CTX_FOLLOWUP_WORDS)
    raw_kw = (func_args.get("keyword") or func_args.get("target") or "").strip()
    # keyword 本身含代詞或功能詞視為無效（LLM 把「它 進出紀錄」「進出紀錄」當 keyword）
    _bad_kw_words = list(_CTX_FOLLOWUP_WORDS) + list(_CTX_FUNC_HINT.keys())
    kw_is_proxy = any(w in raw_kw for w in _bad_kw_words)
    has_kw = bool(raw_kw) and not kw_is_proxy
    # 偵測功能切換（「它的進出紀錄呢？」「進出紀錄呢」「這個快到期嗎？」）
    # 「紀錄檔」是問檔案列表（list_files），不是問進出紀錄，排除掉避免誤判成功能切換。
    _ctx_func_hint_text = user_text.replace("紀錄檔", "").replace("記錄檔", "")
    new_func = next((v for k, v in _CTX_FUNC_HINT.items() if k in _ctx_func_hint_text), None)

    # 有功能切換詞 or 追問代詞，且沒有有效 keyword → 介入
    if not (is_followup or new_func) or has_kw:
        return func_name, func_args

    new_args = dict(func_args)
    new_args["keyword"] = _ctx["last_sku"]
    log.info(f"[ctx] 補 keyword={_ctx['last_sku']!r} 從上一輪 context")

    if new_func:
        log.info(f"[ctx] 切換 func {func_name!r} → {new_func!r}")
        func_name = new_func

    return func_name, new_args

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
        finance.init(WH_DATA_DIR)
        intent_clf.load()
        SYSTEM_PROMPT = load_system_prompt()
        LLM, MODEL_FILE = load_model()
        # intent_clf 暖機：首次 predict 要載 jieba 分詞詞典（~900ms），先跑一句
        # 讓第一個真訪客的路由就快（2026-07-04）
        try:
            intent_clf.predict("藍牙耳機庫存")
        except Exception:
            pass
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

    # ── 取消（rewrite 之前先攔截，避免被改掉）──
    if user_text == "取消":
        _item_create_state.clear()
        _item_delete_state.clear()
        return JSONResponse({"ok": True, "summary": "已取消。", "view": "item_cancelled", "data": {}})

    user_text = _rewrite_query(user_text)

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
            return JSONResponse({"ok": False, "view": "error", "summary": "系統忙碌中，請稍後再試"})
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
    _has_rca_kw = any(w in user_text for w in _RCA_INTENT_WORDS)
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
    _delete_item_kws = ("刪除", "下架", "砍掉", "移除商品", "刪掉")
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


@app.post("/api/reset_demo")
async def reset_demo_data_api(req: Request):
    """展示資料一鍵重置（獨立按鈕觸發，需密碼）。換回 warehouse_data_baseline/ 並清 session state。"""
    import tools_v2
    body = await req.json()
    password = body.get("password", "")
    res = tools_v2.commit_reset_demo_data(password=password, actor="user_confirmed")
    if res.get("ok"):
        _item_create_state.clear()
        _item_delete_state.clear()
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

            # ── 取消（rewrite 之前先攔截）──
            if user_text == "取消":
                _item_create_state.clear()
                _item_delete_state.clear()
                await send({"type": "done", "result": {"ok": True, "view": "item_cancelled", "data": {}}})
                continue

            _desc_kw_ws = _descriptor_hit(user_text)   # rewrite 前偵測（rewrite 會換掉描述）
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

            # ── 黑名單閘門（最高優先，在刪除/商品清單等任何功能攔截之前）──
            # HTTP 端守門員本來就在功能攔截之前，WS 端順序相反導致「把庫存
            # 全部刪掉」等搗蛋句先被刪除攔截接走，黑名單沒機會擋（第17輪）。
            _bl_hit_ws = next((b for b in _GATEKEEPER_BLACKLIST if b in user_text.lower()), None)
            if _bl_hit_ws:
                log.info(f"[gate] 黑名單命中 {_bl_hit_ws!r} → rejected")
                await push_display({"type": "trace", "stage": "rejected",
                                    "reason": f"blacklist:{_bl_hit_ws}"})
                for ch in GATEKEEPER_REJECT_MSG:
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(0.008)
                await send({"type": "done", "result": {"ok": False, "view": "rejected",
                                                        "summary": GATEKEEPER_REJECT_MSG}})
                continue

            # ── 刪除/下架（優先於 clarify）──
            _delete_kws_ws = ("刪除", "下架", "砍掉", "移除", "刪掉")
            if any(w in user_text for w in _delete_kws_ws):
                # 搗蛋守衛：要刪的是訂單/資料/別人的東西 → 不是刪商品功能，直接拒絕
                # （conv100-r5：「幫我把別人的訂單刪掉」曾開出刪除商品流程）
                if any(w in user_text for w in ("訂單", "資料", "紀錄", "記錄", "帳號",
                                                 "別人", "全部", "所有", "資料庫", "系統")):
                    log.info(f"[gate] 刪除句含敏感對象 → rejected: {user_text!r}")
                    for ch in GATEKEEPER_REJECT_MSG:
                        await send({"type": "token", "text": ch})
                        await asyncio.sleep(0.008)
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
                        names = "、".join(it["name"] for it in user_items[:10])
                        result = {"ok": True, "summary": f"可刪除的商品：{names}\n請輸入要刪除的名稱", "view": "item_list",
                                   "data": {"items": [{"name": it["name"], "sku": it["sku_id"]} for it in user_items]}}
                        # 刪除 pending 狀態以 vid 為 key——全域旗標會讓下一個訪客的
                        # 任意輸入被當成要刪的商品名（跨訪客污染，conv100-r5）
                        _item_delete_state[vid] = True
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
            if _item_delete_state.get(vid):
                import tools_v2 as _tv2_del_mode
                _item_delete_state.pop(vid, None)
                result = _tv2_del_mode.delete_item_start(keyword=user_text.strip())
                for ch in result.get("summary", ""):
                    await send({"type": "token", "text": ch})
                    await asyncio.sleep(0.012)
                await send({"type": "done", "result": result})
                continue

            # ── 列出所有商品（優先於引導）──
            # 含設定關鍵字時不攔（「中倉全部商品安全庫存改成六十」是 config 句）
            if (any(w in user_text for w in ("所有商品", "商品列表", "商品清單", "全部商品", "列出商品", "商品名稱"))
                    and not any(w in user_text for w in _CONFIG_KEY_WORDS)
                    # 搗蛋語境不觸發列表（「所有商品免費送我」「全部商品算零元」曾吐 61 項全清單）
                    and not any(w in user_text for w in ("免費", "送我", "送給", "白拿", "改成", "刪",
                                                          "零元", "0元", "算我的", "打包"))):
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
            if _item_delete_state.get(vid):
                import tools_v2 as _tv2_del_mode2
                _item_delete_state.pop(vid, None)
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
                await send({"type": "done", "result": {"ok": False, "view": "rejected",
                                                        "summary": GATEKEEPER_REJECT_MSG}})
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

            # ── 功能描述直達：描述句 + 查詢語氣 → 不進 LLM 直接查庫存 ──
            # 描述改寫後交給 LLM 在 RPI5 有平台分歧（「橡膠清潔手套還有嗎」
            # 被抽成 category=清潔 跑去 clarify）。這是展示主打功能，不能賭
            # LLM 抽取——確定性直達。寫入/排程/報表/銷售語境不攔，走既有流程。
            _DESC_Q_CUES = ("還有", "還剩", "剩", "庫存", "多少", "幾",
                            "有沒有", "有嗎", "夠", "存量", "現貨")
            _DESC_BLOCK = ("進貨", "出貨", "進了", "出了", "調", "補", "退",
                           "改", "設", "刪", "新增", "賣", "銷", "熱", "滯",
                           "比較", "警示", "排程", "報表", "採購", "對帳",
                           "到期", "過期", "缺貨", "買", "多少錢", "價格")
            if (_desc_kw_ws
                    and any(w in user_text for w in _DESC_Q_CUES)
                    and not any(w in user_text for w in _DESC_BLOCK)):
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
                        await asyncio.sleep(0.008)
                    await send({"type": "done", "result": result})
                    continue
                # 查詢失敗 → 不攔，交給 LLM 流程

            # ── 複合句攔截：「賣最好/賣最差的還剩多少」= 排行 Top1 + 它的庫存 ──
            # C4 會把「賣最好/滯銷」強轉 list_hot_items 回排行榜，但這句訪客
            # 要的是那個商品的庫存數字（RPI5 實測 2026-07-06），進 LLM 前先攔。
            _bs_hot_words = ("賣最好", "賣得最好", "最好賣", "賣最快", "賣得最快",
                             "最熱銷", "最暢銷", "熱銷第一", "銷量第一", "賣第一")
            _bs_slow_words = ("賣最差", "賣得最差", "賣最爛", "賣最慢", "最難賣",
                              "最不好賣", "最滯銷", "滯銷", "賣不動", "賣不掉")
            _bs_stock_words = ("剩多少", "還剩", "剩幾", "庫存", "還有多少", "還有幾", "存量")
            _bs_rank_type = ("slow" if any(w in user_text for w in _bs_slow_words)
                             else "hot" if any(w in user_text for w in _bs_hot_words)
                             else None)
            if _bs_rank_type and any(w in user_text for w in _bs_stock_words):
                _bs_period = "this_month" if "月" in user_text else "this_week"
                _bs_hot = finance.execute("list_hot_items",
                                          {"rank_type": _bs_rank_type, "period": _bs_period})
                _bs_rank = (_bs_hot.get("data") or {}).get("rankings") or []
                _bs_done = False
                if _bs_rank:
                    _bs_name = _bs_rank[0]["name"]
                    _bs_rlabel = "賣最好" if _bs_rank_type == "hot" else "賣最差"
                    log.info(f"[dispatch-ws] 複合句攔截: {user_text!r} → "
                             f"{_bs_rlabel}Top1「{_bs_name}」庫存")
                    result = finance.execute("query_inventory", {"keyword": _bs_name})
                    if result.get("ok") and result.get("summary"):
                        _bs_plabel = "本月" if _bs_period == "this_month" else "本週"
                        _bs_qty_label = ("出" if _bs_rank_type == "hot" else "只出")
                        result["summary"] = (f"{_bs_plabel}{_bs_rlabel}的是「{_bs_name}」"
                                             f"（{_bs_qty_label} {_bs_rank[0]['out_qty']:,} 件）。"
                                             + result["summary"])
                        for ch in result["summary"]:
                            await send({"type": "token", "text": ch})
                            await asyncio.sleep(0.008)
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
            if _clf_func_ws and _clf_func_ws not in ("unknown", "unclear") and _clf_conf_ws >= 0.8:
                log.info(f"[intent_clf primary] vid={vid} {user_text!r} → {_clf_func_ws} (conf={_clf_conf_ws:.2f})")
                func_name = _clf_func_ws
                _needs_llm_ws = func_name in ("manage_config", "run_script", "set_alert",
                                               "set_schedule", "generate_po", "generate_report",
                                               "query_movement", "compare_warehouses")
                if not _needs_llm_ws:
                    func_args = {}
                    if func_name in ("query_inventory", "search_log", "query_related_items"):
                        if _pre_kw_ws and len(_pre_kw_ws) >= 2:
                            func_args["keyword"] = _pre_kw_ws
                    elif func_name == "query_movement":
                        func_args["period"] = "this_month"; func_args["direction"] = "both"
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

            if not _clf_skip_llm_ws:
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
                        "text": "系統有點忙、請稍候再試（試試更簡短的講法、例如「藍牙耳機庫存」）",
                    })
                    continue
                except Exception as e:
                    log.error(f"[llm-error] vid={vid} {type(e).__name__}: {e}", exc_info=True)
                    await send({"type": "error", "text": "推理失敗、請重試"})
                    continue

                output = r["choices"][0]["text"].strip()
                log.info(f"[trace] vid={vid} model={output[:120]}")
                # 效能徽章：推論完成即送 tok/s 給前端
                await send({"type": "perf", "mode": "llm", **_last_perf})
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

            # ── intent_clf 命中時也走到這裡（跟 LLM 分支匯流，同一縮排層繼續
            #   下面共用的 Pre-C 規則 / 校正流程，維持跟 HTTP 版一致的行為）──
            if True:
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
                elif ("排程" in user_text and any(w in user_text for w in ("取消", "刪除", "停掉", "關閉", "移除"))):
                    # 「取消所有排程」→ 先列排程讓訪客指名（不做批量刪除，conv100-r7）
                    func_name = "list_schedules"
                    func_args = {}
                    log.info("[Pre-C-Sched] 取消排程意圖 → list_schedules（列出讓訪客選）")
                else:
                    # 「每個月/每星期」漏收：「幫我排每個月十五號盤點」曾被 Pre-C10
                    # 搶成立即執行腳本（conv100-r5）
                    _sched_time_kws = ("每天", "每日", "天天", "每週", "每周", "每月", "每個月",
                                       "每星期", "每禮拜", "定時", "自動", "排程",
                                       "每天早上", "每天晚上", "每天中午", "固定")
                    # 「缺貨警示/警示」入列：「每天晚上七點自動出缺貨警示」是排程不是立即查（conv100-r5）
                    # 「報表」入列：「每週三下午三點出貨報表」曾立即產報告（conv100-r9）
                    _sched_act_kws  = ("盤點", "匯出", "報告", "報表", "體檢", "腳本", "跑", "月報", "週報",
                                       "缺貨警示", "警示", "缺貨")
                    _has_sched_time = any(w in user_text for w in _sched_time_kws)
                    _has_sched_act  = any(w in user_text for w in _sched_act_kws)
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
                _pre_script_kws = ("盤點", "匯出進出", "匯出記錄", "進出記錄", "體檢報告", "月底盤點")
                _pre_script_hit = next((w for w in _pre_script_kws if w in user_text), None)
                # 排程語氣（每個月十五號盤點）讓給 Pre-C-Sched，不搶成立即執行（conv100-r5）
                _prec10_sched = any(w in user_text for w in (
                    "每天", "每日", "天天", "每週", "每周", "每月", "每個月", "每星期", "每禮拜", "排程"))
                if _pre_script_hit and func_name not in _prec10_skip and not _prec10_sched:
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
                                    "倉庫比較", "倉庫對比", "比較倉庫",
                                    "三個倉", "三倉", "各倉", "每個倉", "哪個倉", "哪一倉", "哪邊",
                                    "誰大", "誰多", "誰高")
                _alert_set_kws_ws = ("新增庫存警示規則", "設定缺貨警示", "設定警示", "新增警示",
                                     "庫存不足時提醒", "低於安全庫存通知")
                _skip_override = ("run_script", "set_schedule", "list_schedules",
                                  "list_alerts", "delete_alert", "delete_schedule")
                _has_rca_kw_ws = any(w in user_text for w in _RCA_INTENT_WORDS)
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
                if func_name == "compare_warehouses" and _cmp_has_prod_ws:
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
                    _cw_seq = [e for _, e in sorted(_cw_pos)]
                    _cw_rank3 = any(w in user_text for w in (
                        "哪個倉", "哪倉", "各倉", "三倉", "三個倉", "每個倉",
                        "最多", "最空", "最滿", "分布", "佔比", "哪個最", "誰最"))
                    func_args = dict(func_args)
                    if len(_cw_seq) == 2:
                        func_args["warehouse_a"], func_args["warehouse_b"] = _cw_seq
                    elif len(_cw_seq) < 2 and _cw_rank3:
                        func_args["warehouse_a"] = func_args["warehouse_b"] = "all"
                    if "週轉" in user_text:
                        func_args["metric"] = "turnover"
                    elif any(w in user_text for w in ("價值", "總值", "值多少", "金額")):
                        func_args["metric"] = "stock_value"
                    elif any(w in user_text for w in ("幾件", "幾項", "品項數", "商品數")):
                        func_args["metric"] = "item_count"
                    log.info(f"[Pre-C-Cmp2] compare args 依原句校準 → {func_args}")

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

                # ── 校正（一定要在 task_plan / OOV 判斷之前：這兩者都是根據 func_name
                #   決定顯示內容跟商品比對邏輯，若 C13b 等規則會把 func_name 校正成
                #   create_movement，校正前的 keyword 可能還帶著數字量詞雜訊，直接
                #   餵給 OOV 找不到商品會誤判成查詢失敗，task_plan 也會顯示錯的步驟，
                #   see HTTP 版 api_query 的同一個順序，2026-07-02 修 WS/HTTP 不同步）──
                func_name, func_args, _hard = _correct_function_call(user_text, func_name, func_args)

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
                        # （fixed_keyword 為空時不加提示——「昨天進了什麼貨」的
                        # 雜訊 kw 被修成空字串曾顯示「已自動對應至「」」）
                        _oov_hint = (f"（已自動對應至「{oov['fixed_keyword']}」）"
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
                func_name, func_args = _resolve_followup(vid, user_text, func_name, func_args)
                corrected_call = f"{func_name}({func_args})"

                # ── C18：clf mismatch 檢查（hard_corrected 時不蓋過）──
                mismatch, clf_intent, clf_conf = intent_clf.check_mismatch(user_text, func_name)
                if mismatch and not _hard and clf_intent != "unknown":
                    log.info(f"[C18] clf={clf_intent}({clf_conf:.2f}) vs model={func_name} → 校正")
                    func_name = intent_clf.LABEL_TO_FUNC.get(clf_intent, clf_intent)
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
                        _c18_key = next((w for w in _CONFIG_KEY_WORDS if w in user_text), "安全庫存")
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
                            for _zh18, _en18 in (("北倉", "north"), ("北區", "north"),
                                                 ("中倉", "central"), ("中區", "central"),
                                                 ("南倉", "south"), ("南區", "south")):
                                _p18 = user_text.find(_zh18)
                                if _p18 >= 0 and _en18 not in [e for _, e in _pos18]:
                                    _pos18.append((_p18, _en18))
                            _seq18 = [e for _, e in sorted(_pos18)]
                            if len(_seq18) == 2:
                                func_args = {"warehouse_a": _seq18[0], "warehouse_b": _seq18[1]}
                            else:
                                func_args = {"warehouse_a": "all", "warehouse_b": "all",
                                             "metric": "item_count"}
                            if "週轉" in user_text:
                                func_args["metric"] = "turnover"
                            elif any(w in user_text for w in ("價值", "總值", "金額")):
                                func_args["metric"] = "stock_value"
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
                # 「哪個」單字太寬，會誤傷「北倉跟南倉哪個庫存多」這類倉庫比較句（compare_warehouses）。
                # 判別特徵：兩倉比較句一定會提到「倉」，單一商品排行榜問句不會。
                _stock_rank_kws_ws = ("哪個", "哪個東西", "庫存最多", "數量最多", "哪個最多", "存貨最多", "東西最多")
                if (any(w in user_text for w in _stock_rank_kws_ws)
                        and not any(w in user_text for w in ("熱銷", "賣", "排行", "hot", "滯銷",
                                                              "業績", "冠軍", "銷", "墊底"))
                        # 「北區跟南區哪個庫存比較多」沒有「倉」字也是倉庫比較（conv100-r8）
                        and not any(w in user_text for w in ("倉", "北區", "中區", "南區"))):
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
                if not _tool_intent_ok(func_name, user_text):
                    # reject 前先試降級救援（口語前綴害 LLM 輸出錯 function，RPI5 v21）
                    _rescue = _intent_guard_rescue(func_name, func_args, user_text)
                    if _rescue:
                        func_name, func_args = _rescue
                    else:
                        log.info(f"[gate] {func_name} 缺意圖詞 → rejected: {user_text!r}")
                        await push_display({"type": "trace", "stage": "rejected",
                                            "reason": f"no_intent:{func_name}"})
                        for ch in GATEKEEPER_REJECT_MSG:
                            await send({"type": "token", "text": ch})
                            await asyncio.sleep(0.008)
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
                        await asyncio.sleep(0.012)
                    await send({"type": "done", "result": result})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error(f"WS error: {e}", exc_info=True)
    finally:
        all_sockets.discard(ws)
        _ctx_by_vid.pop(vid, None)   # 斷線清掉該訪客 context，避免殘留
        log.info(f"訪客斷線（剩 {len(all_sockets)}）")


if __name__ == "__main__":
    print(f"Starting at {get_url()}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
