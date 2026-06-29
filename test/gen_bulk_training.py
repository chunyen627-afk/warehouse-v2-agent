"""大量生成口語訓練資料，涵蓋所有已知 failure pattern"""
import json, sys, io, random
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

random.seed(42)

# 商品名（從 seed_data）
PRODUCTS = [
    "藍牙耳機", "氣泡水", "悶燒罐", "電熨斗", "慢跑鞋", "洗衣精", "衛生紙",
    "手機殼", "不沾鍋", "瑜珈墊", "行動電源", "蚊香", "咖啡豆", "咖啡機",
    "羊毛襪", "垃圾袋", "蘇打餅", "牛仔褲", "水壺", "羽絨外套", "充電線",
    "沐浴乳", "堅果", "檸檬茶", "運動毛巾", "尿布", "帳篷", "果汁機",
    "電動牙刷", "防蚊液", "健身環", "露營椅", "登山水壺",
]
# 錯字版本
TYPO_MAP = {
    "藍牙耳機": ["藍芽耳機", "藍牙耳基", "藍芽耳基"],
    "氣泡水": ["汽泡水", "氣泡水", "氣砲水"],
    "悶燒罐": ["悶燒灌", "悶燒館", "悶燒鍋"],
    "電熨斗": ["電運斗", "電允斗", "電燙斗"],
    "慢跑鞋": ["慢跑協", "慢跑鞋"],
    "洗衣精": ["洗一精", "洗衣經", "洗依精"],
    "衛生紙": ["衛生只", "為生紙", "衛生紙"],
    "手機殼": ["手基殼", "手機咳", "手機殼"],
    "不沾鍋": ["不沾郭", "不沾鍋", "不沾鍋"],
    "瑜珈墊": ["瑜伽墊", "於加墊", "瑜珈墊"],
    "行動電源": ["行動店員", "行動電源"],
    "蚊香": ["文香", "蚊香", "蚊香"],
    "咖啡豆": ["咖啡鬥", "咖非豆", "咖啡豆"],
    "咖啡機": ["咖啡基", "咖非機"],
    "羊毛襪": ["羊毛挖", "羊毛襪"],
    "垃圾袋": ["垃圾帶", "拉圾袋", "垃圾袋"],
    "蘇打餅": ["素打餅", "蘇打餅"],
    "牛仔褲": ["牛窄褲", "牛子褲", "牛仔褲"],
    "水壺": ["水胡", "水湖", "水壺"],
    "羽絨外套": ["羽容外套", "雨絨外套", "羽絨外套"],
    "沐浴乳": ["沐欲乳", "沐浴乳"],
    "運動毛巾": ["運動毛巾", "運動毛斤"],
}

# 庫存問法
STOCK_QUESTIONS = [
    "{kw}庫存", "{kw}有多少", "{kw}剩多少", "{kw}還有嗎",
    "查{kw}庫存", "{kw}還有沒有", "{kw}還有貨嗎", "{kw}夠不夠",
    "看一下{kw}", "{kw}有嗎", "幫我查{kw}", "{kw}的庫存量",
    "請問{kw}還有嗎", "那個{kw}現在還有沒有", "我想知道{kw}剩多少",
    "幫我查一下{kw}的庫存好嗎", "阿那個{kw}還有沒有貨",
    "你幫我看看{kw}剩多少", "我就是想問{kw}還有幾包",
    "{kw}有沒有現貨", "{kw}還有庫存嗎", "現在{kw}剩多少啊",
]

# 缺貨問法
LOW_STOCK_QUESTIONS = [
    "哪些東西快沒了", "什麼快斷貨了", "快要沒了的東西有哪些",
    "有什麼快缺貨的", "哪些要補貨了", "庫存警示",
    "有什麼東西不夠了", "哪些商品快沒貨", "缺貨清單",
    "阿是不是有些東西快沒了", "哪個商品快斷貨了",
]

# 熱銷問法
HOT_QUESTIONS = [
    "最近什麼賣最好", "這週哪些賣最差", "本週熱銷",
    "什麼東西最好賣", "熱銷排行", "哪些滯銷",
    "賣最好的有哪些", "暢銷商品", "這個月什麼最熱賣",
]

# 進出記錄問法
MOVEMENT_QUESTIONS = [
    "今天進了哪些", "這禮拜出了多少貨", "這個月進貨狀況怎樣",
    "最近進出記錄", "本週出貨多少", "今日進貨",
    "{kw}出貨多少", "{kw}進了多少", "這禮拜{kw}出貨狀況",
]

# RCA 問法
RCA_QUESTIONS = [
    "{kw}帳對不上", "{kw}怎麼少這麼多", "{kw}數量不對",
    "{kw}為什麼少了", "查{kw}對帳", "{kw}的帳有問題",
]

# 連帶問法
RELATED_QUESTIONS = [
    "買{kw}的人還會買什麼", "{kw}的連帶商品", "跟{kw}一起買的有哪些",
    "買{kw}順便會買什麼", "{kw}會帶動哪些商品", "通常買{kw}還會買啥",
]

records = set()  # 去重

def add(user_text, tool_name, args={}):
    r = json.dumps({"user_content": user_text, "tool_name": tool_name,
                     "tool_arguments": json.dumps(args, ensure_ascii=False)},
                    ensure_ascii=False)
    records.add(r)

# 1. 正常庫存查詢（每個商品 × 5 問法）
for prod in PRODUCTS:
    for tmpl in random.sample(STOCK_QUESTIONS, min(5, len(STOCK_QUESTIONS))):
        add(tmpl.replace("{kw}", prod), "query_inventory", {"keyword": prod})

# 2. 錯字庫存查詢（每個有 typo 的商品 × 3 變體）
for prod, typos in TYPO_MAP.items():
    for typo in typos[:3]:
        tmpl = random.choice(STOCK_QUESTIONS[:10])
        add(tmpl.replace("{kw}", typo), "query_inventory", {"keyword": prod})

# 3. 缺貨查詢
for q in LOW_STOCK_QUESTIONS:
    add(q, "list_low_stock", {})

# 4. 熱銷查詢
for q in HOT_QUESTIONS:
    add(q, "list_hot_items", {"rank_type": "hot", "period": "this_week"})

# 5. 進出記錄（通用 + 含商品）
for q in MOVEMENT_QUESTIONS:
    if "{kw}" in q:
        for prod in random.sample(PRODUCTS, 10):
            add(q.replace("{kw}", prod), "query_movement",
                {"period": "this_week", "keyword": prod})
    else:
        add(q, "query_movement", {"period": "this_week"})

# 6. RCA 查詢
for prod in random.sample(PRODUCTS, 15):
    for tmpl in random.sample(RCA_QUESTIONS, 2):
        add(tmpl.replace("{kw}", prod), "search_log", {"keyword": prod})

# 7. 連帶查詢
for prod in random.sample(PRODUCTS, 12):
    for tmpl in random.sample(RELATED_QUESTIONS, 2):
        add(tmpl.replace("{kw}", prod), "query_related_items", {"keyword": prod})

# 8. 含倉庫名
WHS = ["北倉", "南倉", "中倉", "北區", "南區", "中區"]
for prod in random.sample(PRODUCTS, 20):
    wh = random.choice(WHS)
    for tmpl in ["{wh}的{kw}還有多少", "{wh}{kw}庫存", "查{wh}{kw}"]:
        add(tmpl.format(wh=wh, kw=prod), "query_inventory",
            {"keyword": prod, "warehouse": {"北":"north","南":"south","中":"central"}.get(wh[0], "all")})

# 輸出
out = []
for r in sorted(records):
    out.append(r)

with open('bulk_training.jsonl', 'w', encoding='utf-8') as f:
    for r in out:
        f.write(r + '\n')

print(f'Generated {len(out)} training records → bulk_training.jsonl')
