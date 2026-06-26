"""
build_function_declarations.py — 倉管版 SYSTEM_PROMPT (v3.8)

設計（vs 金融 v3.x）：
    - 業務領域：電商雜貨倉
    - 6 大類 (electronics / appliance_kitchen / food_beverage / daily_goods / apparel / sports)
    - 3 個倉 (north / central / south)
    - 3 個 period (today / this_week / this_month)
    - 5 個 function
    - **SKU 不進 prompt**：LLM 抽 keyword 自由字串、server 端 substring match
      （業界 retrieval 做法、Amazon Alexa / 阿里小蜜的標準架構）

執行:
    python build_function_declarations.py

注意:
    declaration 名稱、required 欄位必須與 training_data.jsonl 嚴格一致。
"""

import json
from pathlib import Path

START_TURN = "<start_of_turn>"
END_TURN   = "<end_of_turn>"
START_DECL = "<start_function_declaration>"
END_DECL   = "<end_function_declaration>"
ESCAPE     = "<escape>"


def make_decl(name, desc, props, required):
    """產生單一 function declaration 字串。
    props: list of (key, description, type)
    required: list of keys
    """
    prop_parts = [
        f"{k}:{{description:{ESCAPE}{d}{ESCAPE},type:{ESCAPE}{t}{ESCAPE}}}"
        for k, d, t in props
    ]
    props_str = ",".join(prop_parts)
    req_str   = ",".join(f"{ESCAPE}{k}{ESCAPE}" for k in required)
    return (
        f"{START_DECL}declaration:{name}"
        f"{{description:{ESCAPE}{desc}{ESCAPE},"
        f"parameters:{{properties:{{{props_str}}},"
        f"required:[{req_str}],type:{ESCAPE}OBJECT{ESCAPE}}}}}"
        f"{END_DECL}"
    )


# ============================================================
# 共用 enum
# ============================================================

# 6 大商品分類
CATEGORY_VALUES = (
    "electronics, appliance_kitchen, food_beverage, "
    "daily_goods, apparel, sports"
)

# 3 個倉
WAREHOUSE_VALUES = "north, central, south, all"

# 3 個 period (倉管時間維度比金融簡單很多)
PERIOD_VALUES = "today, this_week, this_month"

# 進出方向
DIRECTION_VALUES = "in, out, both"

# 比較指標
METRIC_VALUES = "stock_value, item_count, turnover"

# 排行類型
RANK_TYPE_VALUES = "hot, slow"

# v2 三金剛：config 動作
CONFIG_ACTION_VALUES = "read, set"

# ============================================================
# 5 個倉管 function
# ============================================================

DECLARATIONS = [
    make_decl(
        "query_inventory",
        "query inventory by keyword or category",
        [
            ("keyword",   "free text item keyword like 藍牙耳機 or 氣泡水", "STRING"),
            ("category",  "category enum",  "STRING"),
            ("warehouse", "warehouse enum", "STRING"),
        ],
        [],   # 全部 optional、由 server 處理缺漏
    ),
    make_decl(
        "query_movement",
        "in/out movement records over a period",
        [
            ("period",    "today|this_week|this_month",                    "STRING"),
            ("keyword",   "free text item keyword (optional, all items if omit)", "STRING"),
            ("direction", "in|out|both",                                    "STRING"),
        ],
        ["period"],
    ),
    make_decl(
        "list_low_stock",
        "items below safety stock (缺貨)",
        [
            ("warehouse", "warehouse enum", "STRING"),
            ("category",  "category enum",  "STRING"),
        ],
        [],   # 全省略 = 全倉全類掃 (一鍵警示)
    ),
    make_decl(
        "compare_warehouses",
        "compare two warehouses by a metric",
        [
            ("warehouse_a", "warehouse enum",                  "STRING"),
            ("warehouse_b", "warehouse enum",                  "STRING"),
            ("metric",      "stock_value|item_count|turnover", "STRING"),
        ],
        ["warehouse_a", "warehouse_b", "metric"],
    ),
    make_decl(
        "list_hot_items",
        "hot/slow selling items (熱銷/滯銷)",
        [
            ("rank_type", "hot|slow",                  "STRING"),
            ("period",    "this_week|this_month",      "STRING"),
            ("category",  "category enum (optional)",  "STRING"),
        ],
        ["rank_type", "period"],
    ),
    make_decl(
        "query_related_items",
        "items often bought together (連帶備貨)",
        [
            ("keyword",  "free text item keyword like 藍牙耳機 or 咖啡機", "STRING"),
            ("category", "category enum (optional)", "STRING"),
        ],
        ["keyword"],
    ),
    # ── v2 三金剛（Agentic 工具）────────────────────────────────
    make_decl(
        "search_log",
        "root-cause of stock discrepancy (對不上/異常)",
        [
            ("keyword",    "item keyword verbatim", "STRING"),
            ("time_range", "today|this_week|this_month", "STRING"),
            ("source",     "log file hint", "STRING"),
        ],
        ["keyword"],
    ),
    make_decl(
        "manage_config",
        "read or set a setting; value allows +N",
        [
            ("action",    "read|set", "STRING"),
            ("key",       "setting keyword verbatim", "STRING"),
            ("value",     "new value e.g. 50 or +30", "STRING"),
            ("warehouse", "warehouse or all", "STRING"),
        ],
        ["action", "key"],
    ),
    make_decl(
        "run_script",
        "run a whitelisted script (盤點/匯出/重產)",
        [
            ("script_name", "script keyword verbatim", "STRING"),
        ],
        ["script_name"],
    ),
    # ── A/B 波（更開放）──────────────────────────────────────
    #   judge_cause_found 是 server 內部決策、訪客不直接叫 → 不進 declaration（省 prompt、守雷4），
    #   但訓練樣本保留讓模型學得會。
    make_decl(
        "generate_report",
        "write a report file (報告/報表/體檢)",
        [
            ("report_type", "full|low_stock|expiring|rca", "STRING"),
        ],
        [],
    ),
    make_decl(
        "list_files",
        "list data files (有哪些檔)",
        [
            ("area", "area name (optional)", "STRING"),
        ],
        [],
    ),
    make_decl(
        "set_alert",
        "set alert rule (缺貨/到期就通知我)",
        [
            ("condition", "below_safety|out_of_stock|expiring", "STRING"),
            ("target", "item keyword (optional)", "STRING"),
        ],
        ["condition"],
    ),
    make_decl(
        "generate_po",
        "draft purchase order (採購單/補貨)",
        [
            ("source", "low_stock|shortfall", "STRING"),
        ],
        [],
    ),
    # compare_periods 不進 prompt（省 token、守雷4）：講法特殊「這月vs上月」校正 C16 抓得準，
    # 訓練樣本保留讓模型學，但不佔 declaration 空間。
]

FUNCTION_DECLARATIONS = "\n".join(DECLARATIONS)

# Preamble: 把長 enum 抽出來一次列出
SYSTEM_PROMPT = (
    f"{START_TURN}developer\n"
    f"You are a warehouse inventory function-calling assistant.\n"
    f"categories: {CATEGORY_VALUES}\n"
    f"warehouses: {WAREHOUSE_VALUES}\n"
    f"periods: {PERIOD_VALUES}\n"
    f"directions: {DIRECTION_VALUES}\n"
    f"metrics: {METRIC_VALUES}\n"
    f"rank_types: {RANK_TYPE_VALUES}\n"
    f"config_actions: {CONFIG_ACTION_VALUES}\n"
    f"For item-level queries, extract the user's keyword verbatim — do NOT map to SKU codes.\n"
    f"{FUNCTION_DECLARATIONS}\n"
    f"{END_TURN}\n"
)


if __name__ == "__main__":
    out = Path(__file__).parent / "system_prompt_preview.txt"
    out.write_text(SYSTEM_PROMPT, encoding="utf-8")
    print(f"已產出: {out}")
    print(f"  總長度: {len(SYSTEM_PROMPT)} chars")
    print(f"  function 數量: {len(DECLARATIONS)}")
    print()
    print("--- 完整預覽 ---")
    print(SYSTEM_PROMPT)
