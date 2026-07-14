# -*- coding: utf-8 -*-
"""r31 短句全枚舉掃蕩產生器（2026-07-14）
定位：短句空間（2~12字）= 產品本體，必須可證明地逼近 100%。
產出 regression 格式（類別|句子|內容關鍵字），用 regression_ws.py 的 ACCEPT
機制自動判分——句量 1000+ 無法人工逐句審。

用法：
    python gen_sweep_r31.py            # 產出 _sweep_r31.txt
    python regression_ws.py --file _sweep_r31.txt          （本機）
    python3 regression_ws.py --rpi5 --file _sweep_r31.txt  （RPI5）
"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# 60 商品：短稱（訪客會打的講法）→ 內容驗證關鍵字（全名的獨特片段）
SHORT = {
    "耳機": "耳機", "藍牙耳機": "耳機", "行動電源": "行動電源", "快充線": "快充線",
    "藍牙喇叭": "喇叭", "智慧手環": "手環", "防摔殼": "防摔殼", "滑鼠": "滑鼠",
    "鍵盤": "鍵盤", "筆電包": "筆電包", "USB風扇": "風扇",
    "悶燒罐": "悶燒罐", "電熨斗": "熨斗", "不沾鍋": "不沾鍋", "電動牙刷": "牙刷",
    "果汁機": "果汁機", "咖啡機": "咖啡機", "手沖咖啡壺": "手沖", "野炊鍋具": "野炊",
    "保鮮盒": "保鮮盒", "電動拖把": "拖把",
    "氣泡水": "氣泡水", "咖啡豆": "咖啡豆", "檸檬茶": "檸檬茶", "堅果罐": "堅果",
    "蘇打餅": "蘇打餅", "啤酒": "啤酒", "運動飲": "運動飲", "乳清飲": "乳清",
    "熱可可": "可可", "濾掛咖啡": "濾掛",
    "洗衣精": "洗衣精", "衛生紙": "衛生紙", "沐浴乳": "沐浴乳", "蚊香液": "蚊香",
    "垃圾袋": "垃圾袋", "紙尿布": "尿布", "濕紙巾": "濕紙巾", "防蚊液": "防蚊液",
    "清潔手套": "手套", "咖啡濾紙": "濾紙",
    "素T": "素T", "保暖襪": "襪", "羽絨外套": "羽絨", "牛仔褲": "牛仔",
    "運動內衣": "內衣", "排汗衣": "排汗衣", "壓縮臂套": "臂套", "連身衣": "連身衣",
    "毛帽": "毛帽", "遮陽帽": "遮陽帽",
    "瑜珈墊": "瑜珈墊", "登山水壺": "水壺", "健身環": "健身環", "啞鈴": "啞鈴",
    "運動毛巾": "毛巾", "露營帳篷": "帳篷", "露營椅": "露營椅", "慢跑鞋": "慢跑鞋",
    "露營馬克杯": "馬克杯", "露營燈": "露營燈",
}

# 庫存類短模板（accept=inv：inventory/inventory_single/clarify/low_stock 皆可，
# 內容驗到正確商品即證明沒亂配）
T_INV = ["{n}呢", "{n}多少", "{n}還有嗎", "{n}還剩多少", "查{n}", "{n}庫存",
         "{n}剩幾個", "{n}有貨嗎", "有{n}嗎", "{n}夠嗎", "北倉{n}", "{n}還有沒有"]
# 裸商品名（clarify 問「查什麼」或直接回庫存都算好回答；vague 接受面較寬）
T_BARE = ["{n}"]
# 單品到期/銷況短句（exp/mvt 家族——view 對即可，不驗內容避免資料相依）
T_EXP = ["{n}快到期嗎"]
T_HOT = ["{n}賣得好嗎"]

CATS = {"電子": "電子", "家電": "家電", "食品": "食品", "飲料": "飲料",
        "日用": "日用", "服飾": "服飾", "運動": "運動"}
T_CAT = [("{c}類庫存", "inv", ""), ("{c}類缺貨", "low", ""),
         ("{c}類熱銷", "hot", ""), ("{c}類有什麼", "any", "")]

BARE_FUNC = [("缺貨", "low", ""), ("熱銷", "hot", ""), ("到期", "exp", ""),
             ("進貨", "any", ""), ("出貨", "any", ""), ("報表", "any", ""),
             ("低庫存", "low", ""), ("補貨", "any", ""), ("排行", "hot", ""),
             ("比較", "any", "")]

out = []
seen = set()


def add(cat, sent, must=""):
    if sent in seen:
        return
    seen.add(sent)
    out.append(f"{cat}|{sent}|{must}" if must else f"{cat}|{sent}")


for n, key in SHORT.items():
    for t in T_INV:
        add("inv", t.format(n=n), key)
    for t in T_BARE:
        add("vague", t.format(n=n))          # 裸名：clarify/inventory 皆可
    for t in T_EXP:
        add("any", t.format(n=n))            # 到期短句：不 error/rejected 即可
    for t in T_HOT:
        add("any", t.format(n=n))            # 銷況短句：同上

for c in CATS:
    for t, cat, must in T_CAT:
        add(cat, t.format(c=c), must)

for s, cat, must in BARE_FUNC:
    add(cat, s, must)

with open("_sweep_r31.txt", "w", encoding="utf-8") as f:
    f.write("# r31 短句全枚舉掃蕩（自動產生，勿手編；產生器=gen_sweep_r31.py）\n")
    f.write("\n".join(out) + "\n")

print(f"產出 {len(out)} 句 → _sweep_r31.txt")
