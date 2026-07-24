# -*- coding: utf-8 -*-
"""多輪短句全枚舉產生器（r36，2026-07-15）

【為什麼要這支】
r32-r35 四輪多輪掃蕩，真 bug 軌跡 6→7→6→8 未收斂。但那不是工程品質問題，
是測試方法沒對上空間的形狀：那些 bug 幾乎全是「短句追問」（南／呢／北倉多少／
進出／那個近出紀錄呢），而多輪短句空間 ≈ 單句空間 × 追問形，比單句大一個
數量級——四輪只是隨機採樣不同角落，撈不完是必然的。

r31 已經證明過怎麼把一個空間變成「可證明的保證」：機械全枚舉。這支對多輪
做同一件事。

【空間定義】
    多輪短句空間 = 首句（建立 context）× 追問形（省略掉商品名的各種樣子）

- 首句：r31 已認證單句 100%，所以每個商品只需一個代表首句（60 個）。
- 追問形：從 r32-r35 挖到的 27 個 bug 反推出 6 族（見 FOLLOWUPS）。
        這才是真正要窮舉的維度。

【判分】
句量 1000+ 無法人工審 → 用 ws_convo 的第三欄（回答必須含此關鍵字）自動判：
追問句的正確性 = 「回答還在講同一個商品」。這正是接地的定義。

用法：
    python gen_convo_sweep.py                                   # 產出 _convo_sweep.txt
    python ws_convo.py --file _convo_sweep.txt --reset --quiet  # 本機
    python3 ws_convo.py --file _convo_sweep.txt --rpi5 --reset --quiet   # RPI5
"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# 60 商品：訪客會打的短稱 → 回答裡必定出現的驗證關鍵字（全名的獨特片段）
SHORT = {
    "藍牙耳機": "耳機", "行動電源": "行動電源", "快充線": "快充線",
    "藍牙喇叭": "喇叭", "智慧手環": "手環", "防摔殼": "防摔殼", "無線滑鼠": "滑鼠",
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

# 追問形：(句子, 期望 view 集合, 是否驗商品名)
#   期望寫寬鬆（多種 view 都算對），真正的判準是「回答還在講同一個商品」——
#   view 對但商品跑掉（回全店統計/60 項概覽）才是這個空間的典型 bug。
FOLLOWUPS = [
    # ① 代詞（r32 的主幹）
    ("那個呢",         "not:error,not:rejected", True),
    ("它還剩幾個",     "not:error,not:rejected", True),
    ("那個進出紀錄呢", "not:error,not:rejected", True),
    ("這個快到期嗎",   "not:error,not:rejected", True),
    ("那個多少錢",     "not:error,not:rejected", True),
    ("它安全庫存多少", "not:error,not:rejected", True),

    # ② 純功能詞（省略商品名，只講要什麼）
    ("進出",       "not:error,not:rejected", True),
    ("進出紀錄",   "not:error,not:rejected", True),
    ("到期",       "not:error,not:rejected", True),
    ("快到期嗎",   "not:error,not:rejected", True),
    ("安全庫存",   "not:error,not:rejected", True),
    ("安全庫存多少", "not:error,not:rejected", True),
    ("搭配什麼賣", "not:error,not:rejected", True),
    ("多少錢",     "not:error,not:rejected", True),
    ("還剩幾個",   "not:error,not:rejected", True),
    ("現在剩幾個", "not:error,not:rejected", True),
    ("快缺貨了嗎", "low_stock", True),
    ("夠不夠",     "low_stock", True),

    # ③ 倉別追問（r35：「北倉多少」曾回全店 60 項概覽）
    ("南倉呢",     "not:error,not:rejected", True),
    ("北倉呢",     "not:error,not:rejected", True),
    ("中倉呢",     "not:error,not:rejected", True),
    ("北倉多少",   "not:error,not:rejected", True),
    ("南倉幾個",   "not:error,not:rejected", True),
    ("南",         "not:error,not:rejected", True),
    ("北",         "not:error,not:rejected", True),
    ("哪一倉最多", "not:error,not:rejected", True),

    # ④ 語助詞（極限省略）
    ("呢", "not:error,not:rejected", True),
    ("咧", "not:error,not:rejected", True),

    # ⑤ 寫入追問（r34：「北倉進20個」曾回「找不到商品『進20個』」）
    ("北倉進20個", "movement_confirm", True),
    ("南倉出5個",  "movement_confirm,error", True),

    # ⑥ 追問句錯字/注音殘字（r35：功能詞一壞，追問直接失效）
    ("那個近出紀錄呢", "not:error,not:rejected", True),
    ("安全ㄎ存多少",   "not:error,not:rejected", True),
    ("那ㄍ快到期嗎",   "not:error,not:rejected", True),
]


def main():
    lines = [
        "# 多輪短句全枚舉（gen_convo_sweep.py 產出，勿手改）",
        "# 每個情境 = 一位訪客：首句建立 context（商品名）+ 一句追問（省略商品名）",
        "# 判準：追問的回答必須還在講同一個商品 —— 這就是「接地」的定義。",
        "",
    ]
    n = 0
    for prod, kw in SHORT.items():
        for i, (fu, expect, check) in enumerate(FOLLOWUPS):
            n += 1
            lines.append(f"### {prod}-{i:02d} {fu}")
            # 首句：最單純的建立 context 方式（r31 已認證這種句 100%）
            lines.append(f"> {prod}還剩幾個 | not:error,not:rejected | {kw}")
            tail = f" | {kw}" if check else ""
            lines.append(f"> {fu} | {expect}{tail}")
            lines.append("")

    out = "\n".join(lines)
    with open("_convo_sweep.txt", "w", encoding="utf-8") as f:
        f.write(out)
    print(f"已產出 _convo_sweep.txt：{len(SHORT)} 商品 × {len(FOLLOWUPS)} 追問形 "
          f"= {n} 情境 / {n * 2} 輪")


if __name__ == "__main__":
    main()

# r51：手寫守衛景自動併入（序數選單/倉別咧/寫入驗證等多輪修復的守衛）
try:
    _hand = open(__file__.replace('gen_convo_sweep.py', '_convo_hand.txt'), encoding='utf-8').read()
    with open(__file__.replace('gen_convo_sweep.py', '_convo_sweep.txt'), 'a', encoding='utf-8') as _f:
        _f.write('\n' + _hand)
    print('手寫守衛景已併入')
except FileNotFoundError:
    pass
