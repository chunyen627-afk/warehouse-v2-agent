# -*- coding: utf-8 -*-
"""
item_names_en.py — 60 商品的英文主名 + 變體/俗稱對照。
- name_en:  給 en/seed_data.json 的正式商品名（英文關鍵詞 substring 對得到）
- aliases:  給訓練語料的各種講法（全名/短名/俗稱），對應中文版 KEYWORD_SHORT_FORMS
規則：主名用電商常見寫法；aliases 涵蓋展場訪客會講的短稱、俗稱、通用詞。
順序、sku_id 與 seed_data.json 一致（e01..s10）。
"""

ITEM_EN = [
    # ── electronics (e01-e10) ──
    ("e01", "無線藍牙耳機",       "Wireless Bluetooth Earphones",
        ["bluetooth earphones", "bluetooth earbuds", "wireless earbuds", "earphones", "earbuds", "bluetooth headset"]),
    ("e02", "行動電源 10000mAh",  "Power Bank 10000mAh",
        ["power bank", "powerbank", "portable charger", "battery pack", "phone charger"]),
    ("e03", "USB-C 快充線 2M",    "USB-C Fast Charging Cable 2M",
        ["usb-c cable", "charging cable", "usb c cable", "fast charging cable", "charger cable", "type-c cable"]),
    ("e04", "藍牙喇叭",           "Bluetooth Speaker",
        ["bluetooth speaker", "speaker", "wireless speaker", "portable speaker"]),
    ("e05", "智慧手環",           "Smart Fitness Band",
        ["smart band", "fitness band", "smart watch", "smartwatch", "fitness tracker", "activity band"]),
    ("e06", "手機防摔殼",         "Phone Protective Case",
        ["phone case", "protective case", "phone cover", "shockproof case", "case"]),
    ("e07", "無線滑鼠",           "Wireless Mouse",
        ["wireless mouse", "mouse", "computer mouse", "cordless mouse"]),
    ("e08", "機械式鍵盤",         "Mechanical Keyboard",
        ["mechanical keyboard", "keyboard", "gaming keyboard", "mech keyboard"]),
    ("e09", "14吋筆電包",         "14-inch Laptop Bag",
        ["laptop bag", "laptop sleeve", "notebook bag", "laptop case", "computer bag"]),
    ("e10", "桌上型 USB 風扇",    "Desktop USB Fan",
        ["usb fan", "desk fan", "desktop fan", "mini fan", "small fan"]),

    # ── appliance_kitchen (k01-k10) ──
    ("k01", "不鏽鋼悶燒罐",       "Stainless Steel Thermal Food Jar",
        ["thermal jar", "food jar", "thermos jar", "insulated jar", "soup jar", "vacuum jar"]),
    ("k02", "蒸氣電熨斗",         "Steam Iron",
        ["steam iron", "iron", "clothes iron", "electric iron"]),
    ("k03", "陶瓷不沾鍋 28cm",    "Ceramic Non-stick Pan 28cm",
        ["non-stick pan", "nonstick pan", "frying pan", "ceramic pan", "cooking pan", "pan"]),
    ("k04", "電動牙刷",           "Electric Toothbrush",
        ["electric toothbrush", "toothbrush", "power toothbrush", "rechargeable toothbrush"]),
    ("k05", "迷你果汁機",         "Mini Blender",
        ["blender", "mini blender", "juicer", "smoothie blender", "juice blender"]),
    ("k06", "全自動咖啡機",       "Automatic Coffee Machine",
        ["coffee machine", "coffee maker", "automatic coffee maker", "espresso machine", "coffee machine"]),
    ("k07", "手沖咖啡壺組",       "Pour-over Coffee Set",
        ["pour over coffee set", "coffee pour over", "drip coffee set", "coffee kettle set", "hand drip set"]),
    ("k08", "野炊鍋具組",         "Camping Cookware Set",
        ["camping cookware", "cookware set", "camping pots", "outdoor cookware", "cook set"]),
    ("k09", "玻璃保鮮盒 5入",     "Glass Food Containers 5pcs",
        ["glass containers", "food containers", "lunch box", "storage containers", "glass box"]),
    ("k10", "除塵電動拖把",       "Electric Mop",
        ["electric mop", "mop", "spin mop", "cordless mop", "floor mop"]),

    # ── food_beverage (f01-f10) ──
    ("f01", "氣泡水 500ml",       "Sparkling Water 500ml",
        ["sparkling water", "soda water", "carbonated water", "fizzy water"]),
    ("f02", "經典黑咖啡豆 1kg",   "Classic Black Coffee Beans 1kg",
        ["coffee beans", "black coffee beans", "roasted coffee beans", "coffee bean"]),
    ("f03", "蜂蜜檸檬茶 600ml",   "Honey Lemon Tea 600ml",
        ["honey lemon tea", "lemon tea", "honey tea", "iced tea"]),
    ("f04", "綜合堅果罐 500g",    "Mixed Nuts 500g",
        ["mixed nuts", "nuts", "nut mix", "assorted nuts", "trail mix"]),
    ("f05", "全麥蘇打餅 200g",    "Whole Wheat Crackers 200g",
        ["crackers", "soda crackers", "wheat crackers", "biscuits", "whole wheat crackers"]),
    ("f06", "精釀啤酒 6入",       "Craft Beer 6-pack",
        ["craft beer", "beer", "beer pack", "ale", "six pack beer"]),
    ("f07", "電解質運動飲",       "Electrolyte Sports Drink",
        ["sports drink", "electrolyte drink", "energy drink", "isotonic drink"]),
    ("f08", "高蛋白乳清飲",       "Whey Protein Drink",
        ["protein drink", "whey protein", "protein shake", "whey drink"]),
    ("f09", "熱可可粉 300g",      "Hot Cocoa Powder 300g",
        ["cocoa powder", "hot chocolate", "cocoa", "hot cocoa", "chocolate powder"]),
    ("f10", "濾掛咖啡 20入",      "Drip Coffee Bags 20pcs",
        ["drip coffee", "drip coffee bags", "pour over coffee bags", "coffee bags", "instant drip coffee"]),

    # ── daily_goods (d01-d10) ──
    ("d01", "抗菌洗衣精 4kg",     "Antibacterial Laundry Detergent 4kg",
        ["laundry detergent", "detergent", "washing liquid", "laundry soap", "washing detergent"]),
    ("d02", "三層抽取衛生紙",     "3-ply Facial Tissue",
        ["facial tissue", "tissue", "tissues", "paper tissue", "toilet paper", "paper towel"]),
    ("d03", "天然沐浴乳 1L",      "Natural Body Wash 1L",
        ["body wash", "shower gel", "body soap", "shower cream"]),
    ("d04", "蚊香液補充瓶",       "Mosquito Repellent Refill",
        ["mosquito repellent refill", "mosquito refill", "repellent refill", "bug spray refill"]),
    ("d05", "強力垃圾袋 50入",    "Heavy-duty Trash Bags 50pcs",
        ["trash bags", "garbage bags", "bin bags", "rubbish bags", "trash bag"]),
    ("d06", "嬰兒紙尿布 L",       "Baby Diapers Size L",
        ["diapers", "baby diapers", "nappies", "diaper"]),
    ("d07", "嬰兒濕紙巾",         "Baby Wet Wipes",
        ["wet wipes", "baby wipes", "wipes", "baby wet wipes"]),
    ("d08", "防蚊液",             "Mosquito Repellent Spray",
        ["mosquito repellent", "bug spray", "insect repellent", "mosquito spray"]),
    ("d09", "橡膠清潔手套",       "Rubber Cleaning Gloves",
        ["rubber gloves", "cleaning gloves", "gloves", "dish gloves"]),
    ("d10", "咖啡濾紙 100入",     "Coffee Filter Papers 100pcs",
        ["coffee filters", "coffee filter paper", "filter papers", "coffee filter"]),

    # ── apparel (a01-a10) ──
    ("a01", "純棉素T 男款",       "Cotton Plain T-shirt Men's",
        ["plain t-shirt", "t-shirt", "tshirt", "cotton tee", "plain tee", "t shirt"]),
    ("a02", "羊毛保暖襪 3雙入",   "Wool Warm Socks 3-pair",
        ["wool socks", "warm socks", "socks", "thermal socks"]),
    ("a03", "輕量羽絨外套",       "Lightweight Down Jacket",
        ["down jacket", "puffer jacket", "winter jacket", "jacket", "coat"]),
    ("a04", "牛仔長褲 男款",      "Denim Jeans Men's",
        ["jeans", "denim jeans", "denim pants", "pants", "trousers"]),
    ("a05", "彈性運動內衣",       "Elastic Sports Bra",
        ["sports bra", "workout bra", "athletic bra", "bra"]),
    ("a06", "機能排汗衣",         "Moisture-wicking Shirt",
        ["moisture wicking shirt", "sweat wicking shirt", "quick dry shirt", "workout shirt", "athletic shirt"]),
    ("a07", "運動壓縮臂套",       "Sports Compression Arm Sleeve",
        ["compression arm sleeve", "arm sleeve", "arm sleeves", "sports sleeve", "compression sleeve"]),
    ("a08", "嬰兒連身衣",         "Baby Onesie",
        ["baby onesie", "onesie", "baby bodysuit", "baby romper", "romper"]),
    ("a09", "保暖毛帽",           "Warm Beanie",
        ["beanie", "warm hat", "winter hat", "knit hat", "wool hat"]),
    ("a10", "防曬遮陽帽",         "Sun Hat",
        ["sun hat", "sun cap", "hat", "wide brim hat", "beach hat"]),

    # ── sports (s01-s10) ──
    ("s01", "瑜珈墊 6mm",         "Yoga Mat 6mm",
        ["yoga mat", "exercise mat", "fitness mat", "workout mat", "gym mat"]),
    ("s02", "登山水壺 1L",        "Hiking Water Bottle 1L",
        ["water bottle", "hiking bottle", "sports bottle", "drink bottle"]),
    ("s03", "彈力健身環",         "Resistance Fitness Ring",
        ["fitness ring", "resistance ring", "pilates ring", "exercise ring", "yoga ring"]),
    ("s04", "啞鈴 5kg 一對",      "Dumbbells 5kg Pair",
        ["dumbbells", "dumbbell", "hand weights", "weights", "gym dumbbells"]),
    ("s05", "運動毛巾 100x30cm",  "Sports Towel 100x30cm",
        ["sports towel", "gym towel", "workout towel", "towel", "fitness towel"]),
    ("s06", "露營帳篷 4人",       "Camping Tent 4-person",
        ["camping tent", "tent", "4 person tent", "family tent", "outdoor tent"]),
    ("s07", "折疊露營椅",         "Folding Camping Chair",
        ["camping chair", "folding chair", "camp chair", "outdoor chair", "chair"]),
    ("s08", "慢跑鞋 男款",        "Running Shoes Men's",
        ["running shoes", "jogging shoes", "sneakers", "trainers", "sports shoes"]),
    ("s09", "露營馬克杯",         "Camping Mug",
        ["camping mug", "mug", "enamel mug", "camp mug", "outdoor mug"]),
    ("s10", "LED 露營燈",         "LED Camping Lantern",
        ["camping lantern", "camping light", "led lantern", "lantern", "camp light", "camping lamp"]),
]

# category 英文顯示名（給語料的類別查詢）
CATEGORY_EN = {
    "electronics":       ["electronics", "electronic products", "gadgets"],
    "appliance_kitchen": ["kitchen appliances", "appliances", "kitchenware"],
    "food_beverage":     ["food and beverage", "food & drinks", "food and drinks", "beverages"],
    "daily_goods":       ["daily goods", "household items", "daily necessities", "household goods"],
    "apparel":           ["apparel", "clothing", "clothes"],
    "sports":            ["sports", "sports gear", "fitness gear", "outdoor gear"],
}

# 倉別英文（key 對應 seed warehouses.key；label_en 給 seed 顯示；aliases 給語料）
WAREHOUSE_EN = {
    "north":   ("North Warehouse",   ["north warehouse", "north", "north wh", "northern warehouse"]),
    "central": ("Central Warehouse", ["central warehouse", "central", "central wh", "middle warehouse"]),
    "south":   ("South Warehouse",   ["south warehouse", "south", "south wh", "southern warehouse"]),
}

# 中文倉別 label → key（seed warehouses.label 是中文）
WH_ZH2KEY = {"北區倉": "north", "中區倉": "central", "南區倉": "south"}

# 類別 label 英文（seed categories.label 是中文）
CATEGORY_LABEL_EN = {
    "electronics":       "Electronics",
    "appliance_kitchen": "Kitchen Appliances",
    "food_beverage":     "Food & Beverage",
    "daily_goods":       "Daily Goods",
    "apparel":           "Apparel",
    "sports":            "Sports & Outdoors",
}

# ── 以 seed_data.json 的中文名為準，動態綁真實 sku_id（避免手寫前綴出錯）──
def build_en_map(seed_items):
    """回 {sku_id: {"name_en":..., "aliases":[...]}}，中文名對 seed 查真實 sku_id。"""
    zh2en = {zh: (en, al) for _sku, zh, en, al in ITEM_EN}
    out = {}
    missing = []
    for it in seed_items:
        zh = it["name"]
        if zh in zh2en:
            en, al = zh2en[zh]
            out[it["sku_id"]] = {"name_en": en, "aliases": al}
        else:
            missing.append(zh)
    return out, missing


if __name__ == "__main__":
    import json
    seed = json.load(open("seed_data.json", encoding="utf-8"))["items"]
    m, missing = build_en_map(seed)
    print(f"共 {len(ITEM_EN)} 商品英文對照；對到 seed {len(m)} 個；未對到 {len(missing)}")
    if missing:
        print("⚠️ 未對到:", missing)
    for sku in ["e01", "a01", "c01", "s01"]:
        if sku in m:
            print(f"  {sku}: → {m[sku]['name_en']}  | {len(m[sku]['aliases'])} 變體")
