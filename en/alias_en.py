# -*- coding: utf-8 -*-
"""
alias_en.py — 英文商品別名 → 主檔商品名 的映射表（EN build）。

為什麼需要（2026-07-25 英文守衛庫一建立就抓到 61 個破口）：
  match_items 是 substring 打分，訪客講的**常見英文俗稱**若不是商品名的子字串，
  就對不到、甚至誤配到不相干商品：
     battery pack  → ❌ Craft Beer 6-pack（"pack" 撞到）
     toilet paper  → ❌ Coffee Filter Papers（"paper" 撞到）
     garbage bags  → ❌ Drip Coffee Bags（"bags" 撞到）
     exercise mat  → ❌ Automatic Coffee Machine
     sneakers / trainers / weights / nappies → 完全對不到
  這是英文版對應中文版 `_TYPO_NORM`（同音錯字表）的角色：把口語俗稱正規化成
  能命中主檔的字串。**在 keyword 進 match_items 之前套用**。

原則：
  - 只收**無歧義**的映射（一個俗稱只對應一個商品）。
  - 值用「主檔商品名的可命中片段」，不用整個主名（避免規格數字干擾）。
  - 長片語排前面（apply 時長的先換，避免被短的先吃掉）。
"""

# 俗稱 → 能命中主檔的字串
ALIAS_EN = {
    # electronics
    "earbuds":            "Bluetooth Earphones",
    "wireless earbuds":   "Bluetooth Earphones",
    "bluetooth headset":  "Bluetooth Earphones",
    "portable charger":   "Power Bank",
    "battery pack":       "Power Bank",
    "phone charger":      "Power Bank",
    "wireless speaker":   "Bluetooth Speaker",
    "portable speaker":   "Bluetooth Speaker",
    "smartwatch":         "Smart Fitness Band",
    "smart watch":        "Smart Fitness Band",
    "fitness tracker":    "Smart Fitness Band",
    "activity band":      "Smart Fitness Band",
    "phone cover":        "Phone Protective Case",
    "shockproof case":    "Phone Protective Case",
    "protective case":    "Phone Protective Case",
    "cordless mouse":     "Wireless Mouse",
    "computer mouse":     "Wireless Mouse",
    "mech keyboard":      "Mechanical Keyboard",
    "gaming keyboard":    "Mechanical Keyboard",
    "laptop sleeve":      "Laptop Bag",
    "notebook bag":       "Laptop Bag",
    "laptop case":        "Laptop Bag",
    "computer bag":       "Laptop Bag",
    "mini fan":           "Desktop USB Fan",
    "desk fan":           "Desktop USB Fan",
    "small fan":          "Desktop USB Fan",
    "type-c cable":       "USB-C Fast Charging Cable",
    "charger cable":      "USB-C Fast Charging Cable",
    # ⚠️ 'usbc'（訪客常把 USB-C 連寫）在模糊層對不到——主檔名拆詞後
    #   'usb' 只有 3 字母，被候選池的 len>=4 濾掉，'usbc' 因此無詞可比。
    #   加成 alias 是最乾淨的解（不必為此放寬全域長度門檻）。
    "usbc cable":         "USB-C Fast Charging Cable",
    "usbc":               "USB-C Fast Charging Cable",
    "usb c":              "USB-C Fast Charging Cable",
    "usb cable":          "USB-C Fast Charging Cable",
    "fast charging cable": "USB-C Fast Charging Cable",
    # appliance / kitchen
    "thermos jar":        "Thermal Food Jar",
    "insulated jar":      "Thermal Food Jar",
    "vacuum jar":         "Thermal Food Jar",
    "soup jar":           "Thermal Food Jar",
    "electric iron":      "Steam Iron",
    "clothes iron":       "Steam Iron",
    "juicer":             "Mini Blender",
    "smoothie blender":   "Mini Blender",
    "juice blender":      "Mini Blender",
    "nonstick pan":       "Non-stick Pan",
    "frying pan":         "Non-stick Pan",
    "ceramic pan":        "Non-stick Pan",
    "cooking pan":        "Non-stick Pan",
    "power toothbrush":   "Electric Toothbrush",
    "rechargeable toothbrush": "Electric Toothbrush",
    "coffee maker":       "Coffee Machine",
    "espresso machine":   "Coffee Machine",
    "automatic coffee maker": "Coffee Machine",
    "drip coffee set":    "Pour-over Coffee Set",
    "hand drip set":      "Pour-over Coffee Set",
    "coffee pour over":   "Pour-over Coffee Set",
    "coffee kettle set":  "Pour-over Coffee Set",
    "camping pots":       "Camping Cookware",
    "outdoor cookware":   "Camping Cookware",
    "cook set":           "Camping Cookware",
    "lunch box":          "Glass Food Containers",
    "storage containers": "Glass Food Containers",
    "food containers":    "Glass Food Containers",
    "glass box":          "Glass Food Containers",
    "spin mop":           "Electric Mop",
    "cordless mop":       "Electric Mop",
    "floor mop":          "Electric Mop",
    # food & beverage
    "soda water":         "Sparkling Water",
    "carbonated water":   "Sparkling Water",
    "fizzy water":        "Sparkling Water",
    "roasted coffee beans": "Coffee Beans",
    "black coffee beans": "Coffee Beans",
    "iced tea":           "Honey Lemon Tea",
    "lemon tea":          "Honey Lemon Tea",
    "honey tea":          "Honey Lemon Tea",
    "nut mix":            "Mixed Nuts",
    "assorted nuts":      "Mixed Nuts",
    "trail mix":          "Mixed Nuts",
    "biscuits":           "Whole Wheat Crackers",
    "soda crackers":      "Whole Wheat Crackers",
    "wheat crackers":     "Whole Wheat Crackers",
    "ale":                "Craft Beer",
    "beer pack":          "Craft Beer",
    "six pack beer":      "Craft Beer",
    "electrolyte drink":  "Electrolyte Sports Drink",
    "energy drink":       "Electrolyte Sports Drink",
    "isotonic drink":     "Electrolyte Sports Drink",
    "protein drink":      "Whey Protein Drink",
    "protein shake":      "Whey Protein Drink",
    "whey drink":         "Whey Protein Drink",
    "hot chocolate":      "Hot Cocoa Powder",
    "chocolate powder":   "Hot Cocoa Powder",
    "hot cocoa":          "Hot Cocoa Powder",
    "pour over coffee bags": "Drip Coffee Bags",
    "instant drip coffee": "Drip Coffee Bags",
    "coffee bags":        "Drip Coffee Bags",
    # daily goods
    "washing liquid":     "Laundry Detergent",
    "laundry soap":       "Laundry Detergent",
    "washing detergent":  "Laundry Detergent",
    "tissues":            "Facial Tissue",
    "paper tissue":       "Facial Tissue",
    "toilet paper":       "Facial Tissue",
    "paper towel":        "Facial Tissue",
    "shower gel":         "Body Wash",
    "shower cream":       "Body Wash",
    "body soap":          "Body Wash",
    "garbage bags":       "Trash Bags",
    "bin bags":           "Trash Bags",
    "rubbish bags":       "Trash Bags",
    "nappies":            "Baby Diapers",
    "baby wipes":         "Baby Wet Wipes",
    "wet wipes":          "Baby Wet Wipes",
    "insect repellent":   "Mosquito Repellent Spray",
    "bug spray":          "Mosquito Repellent Spray",
    "mosquito spray":     "Mosquito Repellent Spray",
    "mosquito refill":    "Mosquito Repellent Refill",
    "repellent refill":   "Mosquito Repellent Refill",
    "rubber gloves":      "Rubber Cleaning Gloves",
    "cleaning gloves":    "Rubber Cleaning Gloves",
    "dish gloves":        "Rubber Cleaning Gloves",
    "coffee filters":     "Coffee Filter Papers",
    "coffee filter paper": "Coffee Filter Papers",
    "filter papers":      "Coffee Filter Papers",
    # apparel
    "tshirt":             "Plain T-shirt",
    "t shirt":            "Plain T-shirt",
    "cotton tee":         "Plain T-shirt",
    "plain tee":          "Plain T-shirt",
    "thermal socks":      "Wool Warm Socks",
    "warm socks":         "Wool Warm Socks",
    "puffer jacket":      "Down Jacket",
    "winter jacket":      "Down Jacket",
    "coat":               "Down Jacket",
    "denim pants":        "Denim Jeans",
    "pants":              "Denim Jeans",
    "trousers":           "Denim Jeans",
    "workout bra":        "Sports Bra",
    "athletic bra":       "Sports Bra",
    # 'sports bra' 是主檔字面（Elastic Sports Bra），但 match_items 逐 token
    #   打分時 'sports' 也命中 Electrolyte Sports Drink / Sports Compression
    #   Arm Sleeve，分數會被稀釋到誤配 → 明確映射到唯一商品
    "sports bra":         "Sports Bra",
    "sport bra":          "Sports Bra",
    "gym bra":            "Sports Bra",
    "quick dry shirt":    "Moisture-wicking Shirt",
    "workout shirt":      "Moisture-wicking Shirt",
    "athletic shirt":     "Moisture-wicking Shirt",
    "sweat wicking shirt": "Moisture-wicking Shirt",
    "arm sleeves":        "Compression Arm Sleeve",
    "sports sleeve":      "Compression Arm Sleeve",
    "compression sleeve": "Compression Arm Sleeve",
    "baby bodysuit":      "Baby Onesie",
    "baby romper":        "Baby Onesie",
    "romper":             "Baby Onesie",
    "warm hat":           "Warm Beanie",
    "winter hat":         "Warm Beanie",
    "knit hat":           "Warm Beanie",
    "wool hat":           "Warm Beanie",
    "sun cap":            "Sun Hat",
    "wide brim hat":      "Sun Hat",
    "beach hat":          "Sun Hat",
    # sports
    "exercise mat":       "Yoga Mat",
    "fitness mat":        "Yoga Mat",
    "workout mat":        "Yoga Mat",
    "gym mat":            "Yoga Mat",
    "sports bottle":      "Hiking Water Bottle",
    "hiking bottle":      "Hiking Water Bottle",
    "drink bottle":       "Hiking Water Bottle",
    "water bottle":       "Hiking Water Bottle",
    "resistance ring":    "Resistance Fitness Ring",
    "pilates ring":       "Resistance Fitness Ring",
    "exercise ring":      "Resistance Fitness Ring",
    "yoga ring":          "Resistance Fitness Ring",
    "fitness ring":       "Resistance Fitness Ring",
    "hand weights":       "Dumbbells",
    "gym dumbbells":      "Dumbbells",
    "weights":            "Dumbbells",
    "gym towel":          "Sports Towel",
    "workout towel":      "Sports Towel",
    "fitness towel":      "Sports Towel",
    "4 person tent":      "Camping Tent",
    "family tent":        "Camping Tent",
    "outdoor tent":       "Camping Tent",
    "folding chair":      "Folding Camping Chair",
    "camp chair":         "Folding Camping Chair",
    "outdoor chair":      "Folding Camping Chair",
    "camping chair":      "Folding Camping Chair",
    "jogging shoes":      "Running Shoes",
    "sneakers":           "Running Shoes",
    "trainers":           "Running Shoes",
    "sports shoes":       "Running Shoes",
    "enamel mug":         "Camping Mug",
    "camp mug":           "Camping Mug",
    "outdoor mug":        "Camping Mug",
    "camping light":      "Camping Lantern",
    "camp light":         "Camping Lantern",
    "camping lamp":       "Camping Lantern",
    "led lantern":        "Camping Lantern",
    "lantern":            "Camping Lantern",
}

# 長片語先換（避免 "coffee bags" 被 "bags" 之類短鍵先吃掉）
_ALIAS_SORTED = sorted(ALIAS_EN.items(), key=lambda kv: -len(kv[0]))


def normalize_alias_en(text: str) -> str:
    """把句中的英文俗稱換成能命中主檔的字串。找不到就原樣回傳。
    大小寫不敏感；只換**完整詞邊界**，避免 'ale' 換掉 'sale' 這種誤傷。
    ⚠️ 換過的區段標記後不再參與後續比對——否則 'camping light' → 'Camping Lantern'
       裡的 'lantern' 會被 'lantern' 鍵二次替換成 'Camping Camping Lantern'。"""
    import re
    if not text:
        return text
    # 用佔位符保護已替換區段
    placeholders = []
    out = text
    for k, v in _ALIAS_SORTED:
        pat = re.compile(r"(?<![A-Za-z])" + re.escape(k) + r"(?![A-Za-z])",
                         re.IGNORECASE)

        def _sub(_m, _v=v):
            placeholders.append(_v)
            return f"\x00{len(placeholders) - 1}\x00"

        out = pat.sub(_sub, out)
    # 還原佔位符
    for i, v in enumerate(placeholders):
        out = out.replace(f"\x00{i}\x00", v)
    return out


if __name__ == "__main__":
    tests = ["earbuds stock", "how many battery pack left", "toilet paper inventory",
             "garbage bags", "exercise mat", "sneakers availability",
             "do we have weights", "camping light stock", "a sale of ale"]
    for t in tests:
        print(f"  {t:32s} -> {normalize_alias_en(t)}")
