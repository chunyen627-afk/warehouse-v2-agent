# -*- coding: utf-8 -*-
"""r30b 探針：字尾表收割（521 SKU 歸其他覆盤 → 7 條新 head）。

新增：嬰兒圍欄→baby、寵物提籃→pet、野餐籃/沙灘墊/露營椅墊→sports、
瑜珈服/運動臂套→apparel（主檔先例：運動壓縮臂套=apparel）。
鄰居迴歸：既有 head 不變、歧義單字（墊/套/杯/袋）仍反問、
世代尾標剝除、太短/未知仍歸 None。
放棄項（刻意不收）：垃圾桶（唯一實例車用垃圾桶=5字超尾窗+汽車/日用歧義）、
水桶/打氣筒/保冷袋/掛繩（跨類歧義）。"""
import sys

sys.path.insert(0, ".")
import warehouse as W

W.init("seed_data.json")
import tools_v2 as T

BAD = 0


def ck(label, cond, detail=""):
    global BAD
    if not cond:
        BAD += 1
    print(("OK  " if cond else "NG  ") + label + ("  | " + str(detail) if detail else ""))


CASES = [
    # r30 收割：7 新 head（含尾標/前綴變形）
    ("嬰兒圍欄", "baby"), ("嬰兒圍欄六號", "baby"), ("大型嬰兒圍欄", "baby"),
    ("寵物提籃", "pet"), ("寵物提籃六號", "pet"),
    ("野餐籃", "sports"), ("野餐籃七號", "sports"), ("竹編野餐籃", "sports"),
    ("沙灘墊", "sports"), ("沙灘墊六號", "sports"), ("折疊沙灘墊", "sports"),
    ("露營椅墊", "sports"), ("露營椅墊七號", "sports"),
    ("瑜珈服", "apparel"), ("瑜珈服七號", "apparel"),
    ("運動臂套", "apparel"), ("運動臂套七號", "apparel"),
    # 鄰居：既有 head 不變
    ("瑜珈墊", "sports"), ("瑜珈磚二代", "sports"),
    ("刷毛背心", "apparel"), ("壓縮臂套", None),      # 臂套(2字)不在表→仍反問
    ("衛生紙", "daily_goods"), ("寵物尿墊", "pet"),
    ("嬰兒床", "baby"), ("藍牙耳機", "electronics"),
    # 鄰居：歧義單字仍反問（判錯比反問傷）
    ("咖啡保溫杯", None), ("保冷袋", None), ("桌墊", None),
    ("運動毛巾", None), ("折疊水桶", None), ("電動打氣筒", None),
    ("車用垃圾桶", None),   # 放棄項：維持反問
    ("手機掛繩", None),
]
for name, want in CASES:
    got, reason = T._zh_guess_category(name)
    ck(f"{name} -> {want}", got == want, f"got={got} ({reason})")

# create 流程端：新 head 建檔卡類別自動認
r = T.create_item_collect(step=1, raw_text="嬰兒圍欄旗艦款 250元")
it = r.get("data", {}).get("item", {})
ck("create 嬰兒圍欄旗艦款 -> baby 卡", r.get("view") == "item_confirm"
   and it.get("category") == "baby", f"view={r.get('view')} cat={it.get('category')}")

r = T.create_item_collect(step=1, raw_text="親子沙灘墊 300元")
it = r.get("data", {}).get("item", {})
ck("create 親子沙灘墊 -> sports 卡", r.get("view") == "item_confirm"
   and it.get("category") == "sports", f"view={r.get('view')} cat={it.get('category')}")

print()
print("bad", BAD)
sys.exit(1 if BAD else 0)
