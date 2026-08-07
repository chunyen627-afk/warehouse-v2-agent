#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""商品類別集中定義（19 類）。

體系參照 **Google Product Taxonomy 21 個頂層**調整而來（砍掉 Mature/
Religious 等倉儲用不到的、把 Cameras 併入電子、Health & Beauty 拆成
美妝與醫療），所以分類邊界有國際標準背書，不是憑空編的。

⚠️ **一處定義、各處引用**——類別原本硬編在 196 處（warehouse/tools_v2/
  server 三檔），加一個類別要掃全部。集中後加類別只要動這個檔。
⚠️ `prefix_legacy` 是**現行料號前綴**（單字母，既有 60 筆用這個）；
  `prefix` 是未來要換的三碼格式（ELE-0001）。**換格式前不可動 legacy**
  ——料號是主鍵，改了歷史進出紀錄全部對不上。
⚠️ keywords 是**手工精選**的品類詞。曾試過用 Google Taxonomy 自動萃取
  （2,622 詞），實測判對率只有 17%（雜訊太多、真品類詞反被字尾規則誤殺），
  手工 321 詞反而 35/36。**詞多不等於準**，別再走自動萃取那條路。
"""

CATEGORIES = {
    "electronics": {
        "prefix_legacy": "e",
        "prefix": "ELE",
        "label_zh": "電子產品",
        "label_en": "Electronics",
        "aliases_zh": ['電子', '3c', '電子產品'],
        "aliases_en": ['electronics', 'electronic', '3c'],
        "keywords": ['band', 'battery', 'cable', 'camera', 'charger', 'console', 'earphone', 'earphones', 'headphone', 'headphones', 'keyboard', 'laptop', 'microphone', 'monitor', 'mouse', 'phone', 'powerbank', 'printer', 'projector', 'router', 'speaker', 'speakers', 'tablet', 'webcam'],
    },
    "appliance_kitchen": {
        "prefix_legacy": "a",
        "prefix": "KIT",
        "label_zh": "家電廚具",
        "label_en": "Appliance & Kitchen",
        "aliases_zh": ['家電', '廚具', '廚房'],
        "aliases_en": ['appliance', 'kitchen', 'cookware'],
        "keywords": ['blender', 'container', 'containers', 'cooker', 'cookware', 'cutlery', 'dispenser', 'fryer', 'grill', 'iron', 'jar', 'kettle', 'knife', 'machine', 'mop', 'oven', 'pan', 'pot', 'spatula', 'steamer', 'tableware', 'toaster', 'vacuum', 'whisk'],
    },
    "food_beverage": {
        "prefix_legacy": "f",
        "prefix": "FNB",
        "label_zh": "食品飲料",
        "label_en": "Food & Beverage",
        "aliases_zh": ['食品', '飲料', '零食'],
        "aliases_en": ['food', 'beverage', 'drink', 'snack'],
        "keywords": ['beans', 'beer', 'biscuit', 'biscuits', 'candy', 'cereal', 'chocolate', 'coffee', 'crackers', 'drink', 'dumplings', 'flour', 'honey', 'juice', 'milk', 'noodle', 'noodles', 'nuts', 'quinoa', 'rice', 'sauce', 'snack', 'soda', 'sugar', 'tea', 'water', 'wine'],
    },
    "daily_goods": {
        "prefix_legacy": "d",
        "prefix": "DLY",
        "label_zh": "日用品",
        "label_en": "Daily Goods",
        "aliases_zh": ['日用', '日用品', '家居', '清潔'],
        "aliases_en": ['daily', 'household', 'cleaning'],
        "keywords": ['broom', 'bucket', 'candle', 'cleaner', 'detergent', 'diaper', 'diapers', 'gloves', 'hanger', 'papers', 'refill', 'shampoo', 'soap', 'sponge', 'spray', 'tissue', 'tissues', 'toothpaste', 'wash', 'wipes'],
    },
    "apparel": {
        "prefix_legacy": "c",
        "prefix": "CLO",
        "label_zh": "服飾",
        "label_en": "Apparel",
        "aliases_zh": ['服飾', '衣服', '衣物'],
        "aliases_en": ['apparel', 'clothing', 'clothes'],
        "keywords": ['beanie', 'bra', 'cap', 'coat', 'dress', 'hat', 'hoodie', 'jacket', 'jeans', 'onesie', 'pajamas', 'pants', 'scarf', 'shirt', 'shorts', 'skirt', 'sleeve', 'socks', 'sweater', 't-shirt', 'tshirt', 'underwear'],
    },
    "sports": {
        "prefix_legacy": "s",
        "prefix": "SPT",
        "label_zh": "運動用品",
        "label_en": "Sports & Outdoors",
        "aliases_zh": ['運動', '戶外', '健身', '露營'],
        "aliases_en": ['sports', 'outdoor', 'fitness', 'camping'],
        "keywords": ['barbell', 'bike', 'dumbbell', 'dumbbells', 'goggles', 'helmet', 'kayak', 'lantern', 'mat', 'paddle', 'racket', 'rope', 'skateboard', 'tent', 'treadmill'],
    },
    "hardware": {
        "prefix_legacy": "h",
        "prefix": "HDW",
        "label_zh": "五金工具",
        "label_en": "Hardware & Tools",
        "aliases_zh": ['五金', '工具', '手工具'],
        "aliases_en": ['hardware', 'tool', 'tools', 'diy'],
        "keywords": ['adhesive', 'bolt', 'bolts', 'chisel', 'clamp', 'drill', 'hammer', 'hinge', 'ladder', 'nail', 'nails', 'nut', 'pliers', 'sander', 'saw', 'screw', 'screwdriver', 'screws', 'sealant', 'socket', 'tape', 'toolbox', 'washer', 'wrench'],
    },
    "beauty": {
        "prefix_legacy": "b",
        "prefix": "BTY",
        "label_zh": "美妝保養",
        "label_en": "Beauty & Personal Care",
        "aliases_zh": ['美妝', '保養', '化妝品'],
        "aliases_en": ['beauty', 'cosmetics', 'skincare', 'makeup'],
        "keywords": ['cleanser', 'cologne', 'conditioner', 'cream', 'eyeliner', 'floss', 'foundation', 'lipstick', 'lotion', 'mascara', 'mask', 'moisturizer', 'nail', 'perfume', 'polish', 'razor', 'serum', 'sunscreen', 'toner', 'toothbrush'],
    },
    "medical": {
        "prefix_legacy": "m",
        "prefix": "MED",
        "label_zh": "醫療保健",
        "label_en": "Health & Medical",
        "aliases_zh": ['醫療', '保健', '藥品', '健康'],
        "aliases_en": ['health', 'medical', 'medicine'],
        "keywords": ['antiseptic', 'bandage', 'bandages', 'crutch', 'gauze', 'inhaler', 'ointment', 'painkiller', 'sanitizer', 'stethoscope', 'supplement', 'supplements', 'syringe', 'thermometer', 'vitamin', 'vitamins', 'wheelchair'],
    },
    "stationery": {
        "prefix_legacy": "t",
        "prefix": "STA",
        "label_zh": "文具事務",
        "label_en": "Office & Stationery",
        "aliases_zh": ['文具', '辦公', '事務'],
        "aliases_en": ['office', 'stationery'],
        "keywords": ['binder', 'calculator', 'clip', 'clips', 'envelope', 'envelopes', 'eraser', 'folder', 'glue', 'highlighter', 'marker', 'notebook', 'notepad', 'pen', 'pencil', 'pencils', 'pens', 'ruler', 'scissors', 'stapler', 'staples'],
    },
    "pet": {
        "prefix_legacy": "p",
        "prefix": "PET",
        "label_zh": "寵物用品",
        "label_en": "Pet Supplies",
        "aliases_zh": ['寵物', '貓', '狗'],
        "aliases_en": ['pet', 'pets', 'animal'],
        "keywords": ['aquarium', 'birdcage', 'catnip', 'chew', 'collar', 'harness', 'kennel', 'leash', 'litter', 'muzzle', 'petfood'],
    },
    "automotive": {
        "prefix_legacy": "v",
        "prefix": "AUT",
        "label_zh": "汽機車",
        "label_en": "Automotive",
        "aliases_zh": ['汽車', '機車', '車用'],
        "aliases_en": ['automotive', 'vehicle', 'car', 'auto'],
        "keywords": ['alternator', 'brake', 'brakes', 'bumper', 'carseat', 'engine', 'jack', 'muffler', 'radiator', 'spark', 'tire', 'tires', 'windshield', 'wiper', 'wipers'],
    },
    "furniture": {
        "prefix_legacy": "n",
        "prefix": "FUR",
        "label_zh": "家具寢具",
        "label_en": "Furniture & Bedding",
        "aliases_zh": ['家具', '寢具', '床'],
        "aliases_en": ['furniture', 'bedding'],
        "keywords": ['bed', 'bench', 'blanket', 'bookcase', 'cabinet', 'chair', 'couch', 'desk', 'dresser', 'duvet', 'mattress', 'nightstand', 'pillow', 'quilt', 'shelf', 'shelves', 'sofa', 'stool', 'table', 'wardrobe'],
    },
    "baby": {
        "prefix_legacy": "y",
        "prefix": "BAB",
        "label_zh": "母嬰用品",
        "label_en": "Baby & Toddler",
        "aliases_zh": ['母嬰', '嬰兒', '幼兒'],
        "aliases_en": ['baby', 'toddler', 'infant'],
        "keywords": ['bassinet', 'bib', 'bottle', 'carrier', 'crib', 'highchair', 'pacifier', 'playpen', 'romper', 'stroller', 'teether'],
    },
    "media": {
        "prefix_legacy": "k",
        "prefix": "BOK",
        "label_zh": "圖書影音",
        "label_en": "Books & Media",
        "aliases_zh": ['圖書', '書', '影音', '音樂'],
        "aliases_en": ['book', 'books', 'media', 'music'],
        "keywords": ['album', 'audiobook', 'bluray', 'book', 'books', 'cdrom', 'comic', 'dvd', 'magazine', 'novel', 'textbook', 'vinyl'],
    },
    "industrial": {
        "prefix_legacy": "i",
        "prefix": "IND",
        "label_zh": "工業商用",
        "label_en": "Industrial & Business",
        "aliases_zh": ['工業', '商用', '零件', '機械'],
        "aliases_en": ['industrial', 'business', 'machinery'],
        "keywords": ['actuator', 'bearing', 'bearings', 'compressor', 'conveyor', 'coupling', 'gasket', 'gear', 'gears', 'motor', 'pulley', 'pump', 'sensor', 'shaft', 'solenoid', 'valve'],
    },
    "toys": {
        "prefix_legacy": "g",
        "prefix": "TOY",
        "label_zh": "玩具遊戲",
        "label_en": "Toys & Games",
        "aliases_zh": ['玩具', '遊戲', '模型'],
        "aliases_en": ['toy', 'toys', 'game', 'games'],
        "keywords": ['blocks', 'boardgame', 'crayon', 'dice', 'doll', 'figurine', 'kite', 'lego', 'paint', 'plush', 'puzzle', 'yoyo'],
    },
    "luggage": {
        "prefix_legacy": "l",
        "prefix": "BAG",
        "label_zh": "箱包行李",
        "label_en": "Luggage & Bags",
        "aliases_zh": ['箱包', '行李', '背包'],
        "aliases_en": ['luggage', 'bag', 'bags', 'backpack'],
        "keywords": ['backpack', 'briefcase', 'duffel', 'handbag', 'purse', 'satchel', 'suitcase', 'tote', 'trolley', 'wallet'],
    },
    "other": {
        "prefix_legacy": "x",
        "prefix": "OTH",
        "label_zh": "其他",
        "label_en": "Other",
        "aliases_zh": ['其他', '雜項'],
        "aliases_en": ['other', 'misc'],
        "keywords": [],
    },
}

# ⚠️ **19 類全部可查**（user 定調）：這是**預留的空倉庫結構**，
#   現有 60 筆商品只是 demo 資料，本來就只填滿其中 6 類。
#   查到沒商品的類別就**老實說「這類目前還沒有商品」**——那是事實，
#   比把類別藏起來誠實，也讓客戶看得出系統預留了哪些分類。
#   （seed 目前有商品的 6 類，其餘 13 類等建檔進來才有東西。）
SEEDED_CATEGORIES = ("electronics", "appliance_kitchen", "food_beverage",
                     "daily_goods", "apparel", "sports")

CATEGORY_LABEL_ZH = {k: v["label_zh"] for k, v in CATEGORIES.items()}
CATEGORY_LABEL_EN = {k: v["label_en"] for k, v in CATEGORIES.items()}
CATEGORY_PREFIX_LEGACY = {k: v["prefix_legacy"] for k, v in CATEGORIES.items()}
CATEGORY_PREFIX = {k: v["prefix"] for k, v in CATEGORIES.items()}


def keyword_to_category(word: str) -> str:
    """單一品類詞 → 類別 key（找不到回 ""）。"""
    w = (word or "").strip().lower()
    for k, v in CATEGORIES.items():
        if w in v["keywords"]:
            return k
    return ""


def alias_to_category(text: str) -> str:
    """句中的類別別名 → 類別 key（中英皆認，找不到回 ""）。"""
    t = (text or "").lower()
    for k, v in CATEGORIES.items():
        for a in v["aliases_zh"]:
            if a in (text or ""):
                return k
        for a in v["aliases_en"]:
            if a in t:
                return k
    return ""
