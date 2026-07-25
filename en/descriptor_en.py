# -*- coding: utf-8 -*-
"""
descriptor_en.py — 英文「功能描述句 → 商品」對照（EN build）。

為什麼需要（守衛 inv 類長期 FAIL）：
  訪客講不出商品名時會用**功能描述**（招牌能力之一）：
     "something to clean teeth"      → Electric Toothbrush
     "the machine that makes coffee" → Automatic Coffee Machine
     "that stuff for washing clothes"→ Laundry Detergent
  中文版靠 `_DESCRIPTOR_ALIASES`（server.py），但那張表的**回傳值是中文
  商品名**，英文版已明確關掉（英文句走 _descriptor_hit 會拿到中文名，
  湊出「We don't carry「彈性運動內衣」」中英混血）。這裡是英文版的對應物。

設計：
  - 用**關鍵詞組合**比對而非整句精確匹配——訪客的講法無窮
    （"the thing that cleans teeth" / "something for cleaning my teeth"
     / "what do i brush teeth with"），列舉整句必有盲區。
    規則＝(必含詞群, 任一詞群, 目標商品關鍵字)，全部命中才算。
  - 目標值是**能命中主檔的英文片段**（同 alias_en 的原則），交給
    match_items 收斂到唯一商品。
  - 只收無歧義的映射；描述模糊到跨多個商品的（"something to drink"）不收。
"""
import re

# (must_all, must_any, target)
#   must_all: 這些詞全部要出現（用來鎖定「功能」）
#   must_any: 這些詞至少出現一個（用來鎖定「物件」）；空 tuple = 不限
DESCRIPTOR_EN = [
    # ── 電子 ──
    (("charge",), ("phone", "mobile", "portable"), "Power Bank"),
    (("charging",), ("phone", "mobile", "portable"), "Power Bank"),
    (("ear",), ("wireless", "things", "put", "listen", "music"), "Bluetooth Earphones"),
    (("listen",), ("music", "wireless"), "Bluetooth Earphones"),
    (("play",), ("music", "loud", "sound"), "Bluetooth Speaker"),
    (("type",), ("on", "with", "keys"), "Mechanical Keyboard"),
    (("point", "click"), (), "Wireless Mouse"),
    (("click",), ("computer", "thing", "pointer"), "Wireless Mouse"),
    (("track",), ("steps", "fitness", "exercise", "wrist"), "Smart Fitness Band"),
    (("protect",), ("phone", "screen"), "Phone Protective Case"),
    (("carry",), ("laptop", "notebook", "computer"), "Laptop Bag"),
    (("cool",), ("desk", "air", "hot"), "Desktop USB Fan"),
    # ── 家電廚具 ──
    (("clean",), ("teeth", "tooth"), "Electric Toothbrush"),
    (("brush",), ("teeth", "tooth"), "Electric Toothbrush"),
    (("makes", "coffee"), (), "Automatic Coffee Machine"),
    (("make", "coffee"), ("machine", "automatic"), "Automatic Coffee Machine"),
    (("brew", "coffee"), (), "Automatic Coffee Machine"),
    (("blend",), ("fruit", "juice", "smoothie", "small"), "Blender"),
    (("iron",), ("clothes", "shirt", "wrinkle"), "Steam Iron"),
    (("stick",), ("pan", "food", "cook"), "Non-stick Pan"),
    (("cook",), ("pan", "frying"), "Non-stick Pan"),
    (("keep", "hot"), ("food", "soup", "jar"), "Thermal Food Jar"),
    (("mop",), ("floor", "clean", "electric"), "Electric Mop"),
    # ── 食品飲料 ──
    (("fizzy",), (), "Sparkling Water"),
    (("bubbly",), ("water", "drink"), "Sparkling Water"),
    (("drink", "run"), (), "Sports Drink"),      # what do i drink after running
    (("sports",), ("drink", "electrolyte"), "Sports Drink"),
    # ── 日用品 ──
    (("wash", "clothes"), (), "Laundry Detergent"),
    (("washing", "clothes"), (), "Laundry Detergent"),
    (("laundry",), ("stuff", "liquid", "soap"), "Laundry Detergent"),
    (("babies",), ("wipe", "wipes", "clean"), "Baby Wet Wipes"),
    (("baby",), ("wipe", "wipes"), "Baby Wet Wipes"),
    (("mosquito", "spray"), (), "Mosquito Repellent Spray"),
    (("repel",), ("mosquito", "bug", "insect"), "Mosquito Repellent"),
    (("blow", "nose"), (), "Facial Tissue"),
    (("wipe",), ("nose", "face", "tissue"), "Facial Tissue"),
    (("throw", "rubbish"), (), "Trash Bags"),
    (("throw", "trash"), (), "Trash Bags"),
    # ── 服飾 ──
    (("block", "sun"), ("hat", "cap", "head"), "Sun Hat"),
    (("warm",), ("hat", "head", "winter"), "Warm Beanie"),
    (("keep", "feet"), ("warm", "sock"), "Warm Socks"),
    (("run",), ("shoes", "shoe"), "Running Shoes"),
    # ── 運動用品 ──
    (("yoga",), ("mat", "do", "doing"), "Yoga Mat"),
    (("exercise",), ("mat", "on"), "Yoga Mat"),
    (("sleep",), ("camping", "camp", "outdoors", "tent"), "Camping Tent"),
    (("light",), ("camping", "camp", "lantern"), "Camping Lantern"),
    (("fold",), ("chair", "camping", "sit"), "Folding Camping Chair"),
    (("sit",), ("camping", "camp", "outdoor"), "Folding Camping Chair"),
    (("drink", "water"), ("bottle", "hiking", "carry"), "Water Bottle"),
    (("lift",), ("weight", "weights", "dumbbell", "arm"), "Dumbbell"),
]


def descriptor_hit_en(text: str) -> str:
    """英文功能描述 → 能命中主檔的商品關鍵字。抓不到回 ""。

    只在句子**沒有明確商品名**時才該呼叫（呼叫端負責），這裡不做商品名檢查。
    """
    if not text:
        return ""
    t = " " + re.sub(r"[^a-z0-9\s]+", " ", text.lower()) + " "
    toks = set(t.split())

    def _has(w: str) -> bool:
        # 單詞用 token 比對（避免 'run' 中在 'running'? → 反而要中，
        # 所以再加前綴比對），片語用 substring
        if " " in w:
            return w in t
        if w in toks:
            return True
        return any(x.startswith(w) for x in toks if len(x) - len(w) <= 3)

    for must_all, must_any, target in DESCRIPTOR_EN:
        if not all(_has(w) for w in must_all):
            continue
        if must_any and not any(_has(w) for w in must_any):
            continue
        return target
    return ""


if __name__ == "__main__":
    tests = [
        "something to clean teeth", "the thing that cleans your teeth",
        "the machine that makes coffee", "that stuff for washing clothes",
        "the thing that charges my phone", "those wireless ear things",
        "the mat for yoga", "the thing you sleep in when camping",
        "what do i drink after running", "the fizzy water",
        "the pan that food doesn't stick to", "the wipes for babies",
        # 不該命中（有明確商品名或非商品）
        "bluetooth earphones stock", "whats the weather", "hi there",
    ]
    for s in tests:
        print(f"  {s:42s} -> {descriptor_hit_en(s)!r}")
