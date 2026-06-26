"""
test_cases.py — v3.8 倉管測試案例（共用）

被 test_model.py (BF16) / test_gguf.py (Q8_0) / finetune_local.py 共用。

規模：
  - 5 個 function × 6 cat × 3 wh × 3 period = 50 條 happy path
  - + 15 條 E2E 校正驗證 (校正規則 C1-C5 + 引導排除)
"""

# ────────────────────────────────────────────────
# CASES — 三檔 raw 測試共用 (50 條 happy path)
# ────────────────────────────────────────────────
CASES = [
    # ────── query_inventory (12 條) ──────
    ("藍牙耳機現在有多少",         "query_inventory", {"keyword": "藍牙耳機"}),
    ("氣泡水庫存",                  "query_inventory", {"keyword": "氣泡水"}),
    ("北區倉的悶燒罐還剩多少",     "query_inventory", {"keyword": "悶燒罐", "warehouse": "north"}),
    ("南區倉咖啡豆庫存",           "query_inventory", {"keyword": "咖啡豆", "warehouse": "south"}),
    ("食品飲料類庫存",             "query_inventory", {"category": "food_beverage"}),
    ("電子產品還有多少",           "query_inventory", {"category": "electronics"}),
    ("北倉的家電廚具",             "query_inventory", {"category": "appliance_kitchen", "warehouse": "north"}),
    ("運動用品中區倉",             "query_inventory", {"category": "sports", "warehouse": "central"}),
    ("瑜珈墊有幾件",               "query_inventory", {"keyword": "瑜珈墊"}),
    ("洗衣精庫存量",               "query_inventory", {"keyword": "洗衣精"}),
    ("how much bluetooth earphone stock", "query_inventory", {"keyword": "bluetooth earphone"}),
    ("衛生紙庫存查詢",             "query_inventory", {"keyword": "衛生紙"}),

    # ────── query_movement (10 條) ──────
    ("今天進了什麼貨",             "query_movement", {"period": "today"}),
    ("本週耳機出貨多少",           "query_movement", {"period": "this_week", "keyword": "耳機", "direction": "out"}),
    ("這個月運動用品進出狀況",     "query_movement", {"period": "this_month", "keyword": "運動"}),
    ("本月進貨總量",               "query_movement", {"period": "this_month", "direction": "in"}),
    ("今天出貨幾件",               "query_movement", {"period": "today", "direction": "out"}),
    ("本週氣泡水出貨",             "query_movement", {"period": "this_week", "keyword": "氣泡水", "direction": "out"}),
    ("本月食品類進貨",             "query_movement", {"period": "this_month", "direction": "in"}),
    ("今日所有進出記錄",           "query_movement", {"period": "today"}),
    ("這禮拜咖啡豆動了多少",       "query_movement", {"period": "this_week", "keyword": "咖啡豆"}),
    ("show today inbound",         "query_movement", {"period": "today", "direction": "in"}),

    # ────── list_low_stock (8 條) ──────
    ("庫存警示",                   "list_low_stock", {}),
    ("哪些東西快沒了",             "list_low_stock", {}),
    ("需要補貨的有哪些",           "list_low_stock", {}),
    ("缺貨清單",                   "list_low_stock", {}),
    ("北區倉缺貨清單",             "list_low_stock", {"warehouse": "north"}),
    ("食品類有沒有快缺貨的",       "list_low_stock", {"category": "food_beverage"}),
    ("南倉電子產品庫存警示",       "list_low_stock", {"warehouse": "south", "category": "electronics"}),
    ("low stock alert",            "list_low_stock", {}),

    # ────── compare_warehouses (10 條) ──────
    ("北區跟南區哪個庫存比較多",   "compare_warehouses", {"warehouse_a": "north", "warehouse_b": "south", "metric": "stock_value"}),
    ("中區跟南區週轉率比較",       "compare_warehouses", {"warehouse_a": "central", "warehouse_b": "south", "metric": "turnover"}),
    ("比一下北倉跟中倉的庫存價值", "compare_warehouses", {"warehouse_a": "north", "warehouse_b": "central", "metric": "stock_value"}),
    ("北倉跟中倉商品數差多少",     "compare_warehouses", {"warehouse_a": "north", "warehouse_b": "central", "metric": "item_count"}),
    ("南北兩倉哪個東西多",         "compare_warehouses", {"warehouse_a": "south", "warehouse_b": "north", "metric": "item_count"}),
    ("北倉南倉週轉誰高",           "compare_warehouses", {"warehouse_a": "north", "warehouse_b": "south", "metric": "turnover"}),
    ("中區跟北區庫存價值比",       "compare_warehouses", {"warehouse_a": "central", "warehouse_b": "north", "metric": "stock_value"}),
    ("南區跟中區庫存價值比",       "compare_warehouses", {"warehouse_a": "south", "warehouse_b": "central", "metric": "stock_value"}),
    ("北區中區商品數量比較",       "compare_warehouses", {"warehouse_a": "north", "warehouse_b": "central", "metric": "item_count"}),
    ("compare north and south stock value", "compare_warehouses", {"warehouse_a": "north", "warehouse_b": "south", "metric": "stock_value"}),

    # ────── list_hot_items (10 條) ──────
    ("本月最熱賣的商品",           "list_hot_items", {"rank_type": "hot", "period": "this_month"}),
    ("這禮拜賣最差的",             "list_hot_items", {"rank_type": "slow", "period": "this_week"}),
    ("食品類熱銷排行",             "list_hot_items", {"rank_type": "hot", "period": "this_week", "category": "food_beverage"}),
    ("運動用品本月賣得怎樣",       "list_hot_items", {"rank_type": "hot", "period": "this_month", "category": "sports"}),
    ("滯銷品有哪些",               "list_hot_items", {"rank_type": "slow", "period": "this_month"}),
    ("本週銷量冠軍",               "list_hot_items", {"rank_type": "hot", "period": "this_week"}),
    ("這個月電子產品賣最好",       "list_hot_items", {"rank_type": "hot", "period": "this_month", "category": "electronics"}),
    ("本週日用品熱銷",             "list_hot_items", {"rank_type": "hot", "period": "this_week", "category": "daily_goods"}),
    ("月度最差商品",               "list_hot_items", {"rank_type": "slow", "period": "this_month"}),
    ("top selling this week",      "list_hot_items", {"rank_type": "hot", "period": "this_week"}),

    # ────── query_related_items (8 條) ──────
    ("買藍牙耳機的人還會買什麼",   "query_related_items", {"keyword": "藍牙耳機"}),
    ("咖啡機的連帶商品",           "query_related_items", {"keyword": "咖啡機"}),
    ("尿布通常跟什麼一起買",       "query_related_items", {"keyword": "尿布"}),
    ("瑜珈墊的搭配商品",           "query_related_items", {"keyword": "瑜珈墊"}),
    ("帳篷連帶分析",               "query_related_items", {"keyword": "帳篷"}),
    ("買慢跑鞋順便會買什麼",       "query_related_items", {"keyword": "慢跑鞋"}),
    ("洗衣精的連帶備貨",           "query_related_items", {"keyword": "洗衣精"}),
    ("what's bought with coffee machine", "query_related_items", {"keyword": "coffee machine"}),
]

# ────────────────────────────────────────────────
# E2E_EXTRA_CASES — 只給 test_e2e.py 用（校正規則驗證）
# ────────────────────────────────────────────────
E2E_EXTRA_CASES = [
    # C1: query_inventory 沒抽 keyword、但 user_text 含商品意圖詞
    ("藍牙喇叭剩多少",             "query_inventory", {"keyword": "藍牙喇叭"}),
    ("運動毛巾還有多少件",         "query_inventory", {"keyword": "運動毛巾"}),

    # C2: 「最近 / 這幾天」period rewrite → this_week
    ("最近進貨狀況",               "query_movement", {"period": "this_week", "direction": "in"}),
    ("這幾天出貨多少",             "query_movement", {"period": "this_week", "direction": "out"}),

    # C3: 「快沒了 / 缺貨 / 補貨」意圖詞 → list_low_stock (即使 LLM 走錯也救回來)
    ("有沒有快沒貨的東西",         "list_low_stock", {}),
    ("補貨建議",                   "list_low_stock", {}),

    # C4: 「賣最好 / 最熱門 / 滯銷」意圖詞 → list_hot_items
    ("最熱門的商品",               "list_hot_items", {"rank_type": "hot", "period": "this_week"}),
    ("賣最差的商品有哪些",         "list_hot_items", {"rank_type": "slow", "period": "this_week"}),

    # C5: compare_warehouses 漏 slot → fallback to reject view（這 case 預期 server 引導、不一定能命中 function）
    # 此 case 不放 expected function、放 None 給 e2e 判定容錯
    # ("北倉怎樣", None, None),

    # 守門員 (引導排除測試)
    ("查股市",                     None, None),   # 預期 reject (倉管 demo 沒有股市)
    ("天氣怎樣",                   None, None),   # 預期 reject

    # 容錯：keyword 含類別關鍵字 → 應走 inventory 不走 list_hot_items
    ("家電廚具有什麼",             "query_inventory", {"category": "appliance_kitchen"}),
    ("查食品",                     "query_inventory", {"category": "food_beverage"}),

    # 容錯：「庫存」+ 具體商品 → query_inventory + keyword
    ("看一下牛仔褲庫存",           "query_inventory", {"keyword": "牛仔褲"}),
    ("檢查瑜珈墊存量",             "query_inventory", {"keyword": "瑜珈墊"}),

    # 軟動態：「今日 / 本日」應 → today
    ("今日進貨明細",               "query_movement", {"period": "today", "direction": "in"}),

    # C6: 連帶意圖詞 → query_related_items (即使 LLM 走錯也救回來)
    ("買咖啡的人也買了什麼",       "query_related_items", {"keyword": "咖啡"}),
    ("跟瑜珈墊一起賣的有哪些",     "query_related_items", {"keyword": "瑜珈墊"}),
    ("耳機會帶動哪些商品",         "query_related_items", {"keyword": "耳機"}),
]
