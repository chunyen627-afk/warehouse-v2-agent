# -*- coding: utf-8 -*-
"""r30c 探針：字尾表大擴充（電商分類樹掃 18 類，~230 條）＋尾窗 4→5。

user 定調（2026-08-09）：「歸其他太容易發生了。像我建立網球就歸其他。」

四道驗證：
A. 表自檢——同字尾不得出現在兩類（multi-hit 會回 None 白加）、不得撞歧義表、長度≤5
B. 案例表——新條目代表句＋5字池名＋歧義保留組＋既有分類迴歸
C. 全主檔掃描——每個現有商品 guess 不得與記錄類別矛盾（None/相同/記錄=other 皆可）
D. create100 名稱池掃描——每個池名 guess 不得與池類別矛盾（未來輪次防假❌）
"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
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


# ═══ A. 表自檢 ═══
_seen = {}
_dup, _amb_clash, _toolong = [], [], []
for cat, sufs in T._ZH_HEAD_TABLE.items():
    for s in sufs:
        if s in _seen and _seen[s] != cat:
            _dup.append((s, _seen[s], cat))
        _seen[s] = cat
        if s in T._ZH_AMBIG_HEADS:
            _amb_clash.append(s)
        if len(s) > 5:
            _toolong.append(s)
ck("A1 無跨類重複字尾", not _dup, _dup[:5])
ck("A2 無撞歧義表", not _amb_clash, _amb_clash[:5])
ck("A3 全部 ≤5 字（尾窗上限）", not _toolong, _toolong[:5])

# ═══ B. 案例表 ═══
CASES = [
    # user 實抓＋球類家族
    ("網球", "sports"), ("網球九號", "sports"), ("棒球", "sports"),
    ("籃球", "sports"), ("高爾夫球", "sports"), ("網球拍", "sports"),
    # 各類代表
    ("冰箱", "appliance_kitchen"), ("洗衣機", "appliance_kitchen"),
    ("吸塵器", "appliance_kitchen"), ("雙門冰箱", "appliance_kitchen"),
    ("電視", "electronics"), ("手機殼", "electronics"), ("鬧鐘", "electronics"),
    ("醬油", "food_beverage"), ("義大利麵", "food_beverage"),
    ("烏龍茶", "food_beverage"), ("果汁", "food_beverage"),
    ("肥皂", "daily_goods"), ("垃圾桶", "daily_goods"), ("雨傘", "daily_goods"),
    ("雨衣", "apparel"), ("棒球帽", "apparel"),
    ("電鋸", "hardware"), ("手電筒", "hardware"), ("燈泡", "hardware"),
    ("精油", "beauty"), ("粉底液", "beauty"), ("髮圈", "beauty"),
    ("血氧機", "medical"), ("眼藥水", "medical"), ("益生菌", "medical"),
    ("鋼筆", "stationery"), ("修正帶", "stationery"), ("貼紙", "stationery"),
    ("項圈", "pet"), ("魚缸", "pet"), ("狗糧", "pet"),
    ("安全帽", "automotive"), ("千斤頂", "automotive"), ("車蠟", "automotive"),
    ("茶几", "furniture"), ("棉被", "furniture"), ("窗簾", "furniture"),
    ("嬰兒車", "baby"), ("溫奶器", "baby"), ("爽身粉", "baby"),
    ("字典", "media"), ("圖鑑", "media"),
    ("護目鏡", "industrial"), ("焊槍", "industrial"),
    ("公仔", "toys"), ("水槍", "toys"), ("氣球", "toys"), ("彈珠", "toys"),
    ("書包", "luggage"), ("皮包", "luggage"),
    # 5 字尾窗（池名對齊＋救活死條目）
    ("車用吸塵器", "automotive"), ("嬰兒監視器", "baby"),
    ("車用手機架", "automotive"), ("遙控無人機", "toys"),
    ("胎壓偵測器", "automotive"), ("行車記錄器", "automotive"),
    ("汽車芳香劑", "automotive"),
    # 複合詞優先於短尾（果汁機≠果汁、寵物零食≠零食）
    ("果汁機", "appliance_kitchen"), ("寵物零食", "pet"), ("零食", "food_beverage"),
    ("泡泡水", "toys"), ("漱口水", "daily_goods"), ("礦泉水", "food_beverage"),
    # 歧義保留組（判錯比反問傷——這些仍反問）
    ("無人機", None), ("手套", None), ("推車", None), ("水壺", None),
    ("背包", None), ("噴霧", None), ("咖啡保溫杯", None), ("鋼絲球", None),
    ("戲水球組", None),
    # 既有分類迴歸
    ("瑜珈墊", "sports"), ("衛生紙", "daily_goods"), ("藍牙耳機", "electronics"),
    ("嬰兒圍欄", "baby"), ("運動臂套", "apparel"),
]
for name, want in CASES:
    got, reason = T._zh_guess_category(name)
    ck(f"B {name} -> {want}", got == want, f"got={got} ({reason})")

# ═══ C. 全主檔矛盾掃描 ═══
_contra = []
for it in W.state().items:
    rec = it.get("category")
    g, _r = T._zh_guess_category(it.get("name", ""))
    if g is not None and rec not in (None, "", "other") and g != rec:
        _contra.append((it["name"], rec, g))
print(f"C 主檔掃描 {len(W.state().items)} 項")
ck("C1 零矛盾（guess 不得推翻既有類別）", not _contra, _contra[:8])

# ═══ D. create100 名稱池矛盾掃描 ═══
try:
    from create100_gen import POOLS, AMBIG
    _pool_contra = []
    for cat, pairs in POOLS.items():
        for zh, _en in pairs:
            g, _r = T._zh_guess_category(zh)
            if g is not None and g != cat:
                _pool_contra.append((zh, cat, g))
    ck("D1 名稱池零矛盾", not _pool_contra, _pool_contra[:8])
    _amb_report = []
    for zh, _en in AMBIG:
        g, _r = T._zh_guess_category(zh)
        _amb_report.append(f"{zh}->{g}")
    print("D2 AMBIG 池現況（資訊）:", "、".join(_amb_report))
except ImportError:
    print("D skip（無 create100_gen）")

# ═══ E. create 流程端 ═══
r = T.create_item_collect(step=1, raw_text="網球九號 50元")
it = r.get("data", {}).get("item", {})
ck("E1 create 網球九號 -> sports 卡", r.get("view") == "item_confirm"
   and it.get("category") == "sports", f"view={r.get('view')} cat={it.get('category')}")
r = T.create_item_collect(step=1, raw_text="安全帽 300元")
it = r.get("data", {}).get("item", {})
ck("E2 create 安全帽 -> automotive 卡（安全字頭不誤觸欄位解析）",
   r.get("view") == "item_confirm" and it.get("category") == "automotive",
   f"view={r.get('view')} cat={it.get('category')} safety={it.get('safety')}")

print()
print("bad", BAD)
sys.exit(1 if BAD else 0)
