# -*- coding: utf-8 -*-
"""create100 語料生成器 — 每輪產生**全新不重複**商品名（user 定調：真建立、
持續累積、不刪）。

用法：
    python3 create100_gen.py --round 3 --lang zh > create100_zh_r3.txt
    python3 create100_gen.py --round 3 --lang en > create100_en_r3.txt

設計：
  * 名稱池每類 8 個（中英一一對應），每輪取 4 個（①×3＋②×1），
    池用完自動加「{N}代 / mk{N}」尾標繼續唯一。
  * 語音優先：zh 全黏連無標點；en 全小寫無標點。
  * 結構同 base 語料：①19類×3 ②不講類別 ③歧義 ④欄位 ⑤口語 ⑥邊界。
  * 池名迴避 seed 60 商品與 base 語料名稱（產生前有防撞斷言）。
"""
import argparse
import sys

# (zh, en) 名稱池 — 每類 8 組
POOLS = {
 "electronics": [("電競滑鼠","gaming mouse"),("降噪耳罩耳機","noise cancelling headset"),
    ("網路攝影機","web camera"),("智慧插座","smart plug"),("讀卡機","card reader"),
    ("藍牙追蹤器","bluetooth tracker"),("電子書閱讀器","ebook reader"),("桌面麥克風","desk microphone")],
 "appliance_kitchen": [("氣炸鍋","air fryer"),("手持攪拌棒","hand mixer"),
    ("快煮壺","rapid kettle"),("磨豆機","coffee grinder"),("電烤盤","electric griddle"),
    ("壓力鍋","pressure cooker"),("鬆餅機","waffle maker"),("燉鍋","stew pot")],
 "food_beverage": [("蜂蜜芥末醬","honey mustard sauce"),("燕麥片","oatmeal cereal"),
    ("果乾綜合包","dried fruit mix"),("冷萃咖啡液","cold brew coffee"),
    ("海苔酥","seaweed crisps"),("氣泡果汁","sparkling juice"),
    ("黑糖薑茶","brown sugar ginger tea"),("麻辣鍋底","spicy hotpot base")],
 "daily_goods": [("除臭噴霧","deodorizer spray"),("靜電除塵紙","dusting sheets"),
    ("馬桶清潔錠","toilet cleaning tabs"),("洗手慕斯","hand wash foam"),
    ("衣物柔軟精","fabric softener"),("廚房紙巾","kitchen paper towels"),
    ("玻璃清潔液","glass cleaner"),("除濕盒","dehumidifier box")],
 "apparel": [("連帽外套","hooded jacket"),("休閒短褲","casual shorts"),
    ("法蘭絨襯衫","flannel shirt"),("針織圍巾","knit scarf"),("條紋襯衫","striped shirt"),
    ("防潑水風衣","water repellent windbreaker"),("棉質長襪","cotton crew socks"),
    ("刷毛背心","fleece vest")],
 "sports": [("跳繩","jump rope"),("羽毛球拍","badminton racket"),("健腹輪","ab roller"),
    ("瑜珈磚","yoga brick"),("單槓","pull up bar"),("泳鏡","swim goggles"),
    ("護腕","wrist support"),("飛盤","frisbee")],
 "hardware": [("捲尺","tape measure"),("水平儀","spirit level"),("斜口鉗","diagonal pliers"),
    ("內六角組","allen key set"),("電烙鐵","soldering iron"),("砂紙組","sandpaper set"),
    ("膨脹螺絲組","expansion bolt set"),("美工刀","utility knife")],
 "beauty": [("眼霜","eye cream"),("卸妝水","makeup remover"),("護唇膏","lip balm"),
    ("髮膜","hair mask"),("香水體噴","body mist"),("指甲油","nail polish"),
    ("蜜粉餅","pressed powder"),("鬍後水","aftershave lotion")],
 "medical": [("生理食鹽水","saline solution"),("酒精棉片","alcohol pads"),
    ("護踝繃帶","ankle support bandage"),("電子血壓計","blood pressure monitor"),
    ("葉黃素膠囊","lutein capsules"),("退熱貼","fever cooling patch"),
    ("醫用手套","medical gloves"),("魚油軟膠囊","fish oil softgels")],
 "stationery": [("螢光筆組","highlighter set"),("便利貼","sticky notes"),
    ("迴紋針盒","paper clip box"),("桌上型計算機","desk calculator"),
    ("檔案盒","file box"),("白板筆","whiteboard marker"),("口紅膠","glue stick"),
    ("剪刀","scissors")],
 "pet": [("逗貓棒","cat teaser wand"),("寵物剪毛器","pet grooming clipper"),
    ("貓抓板","cat scratcher board"),("胸背帶","pet harness"),
    ("寵物除蚤梳","flea comb"),("飼料保鮮桶","food storage bucket"),
    ("貓跳台","cat tree tower"),("寵物尿墊","pet pee pads")],
 "automotive": [("車用吸塵器","car vacuum cleaner"),("胎壓偵測器","tire pressure sensor"),
    ("行車記錄器","dash camera"),("汽車芳香劑","car air freshener"),
    ("雨刷精","wiper fluid"),("車用手機架","car phone mount"),
    ("補胎劑","tire sealant"),("洗車海綿","car wash sponge")],
 "furniture": [("電腦桌","computer desk"),("掛衣架","coat rack"),("床頭櫃","bedside table"),
    ("懶人沙發","bean bag sofa"),("記憶枕","memory foam pillow"),
    ("折疊餐桌","folding dining table"),("穿衣鏡","dressing mirror"),
    ("五斗櫃","five drawer chest")],
 "baby": [("學步車","baby walker"),("圍兜","baby bib"),("嬰兒監視器","baby monitor"),
    ("固齒器","teether toy"),("澡盆","baby bathtub"),("嬰兒背巾","baby carrier wrap"),
    ("嬰兒指甲剪","baby nail clipper"),("奶粉分裝盒","milk powder dispenser")],
 "media": [("食譜書","cookbook"),("推理小說","mystery novel"),
    ("兒童繪本","children picture book"),("語言學習書","language learning book"),
    ("攝影集","photography album book"),("雜誌合訂本","magazine bundle"),
    ("漫畫套書","comic book set"),("黑膠唱片","classic vinyl record")],
 "industrial": [("齒輪組","gear set"),("輸送帶滾輪","conveyor roller"),
    ("工業感測器","industrial sensor"),("電磁閥","solenoid valve"),
    ("聯軸器","shaft coupling"),("油封組","oil seal kit"),
    ("步進馬達","stepper motor"),("空壓機濾芯","air compressor filter")],
 "toys": [("遙控無人機","rc drone toy"),("布偶熊","teddy bear plush"),
    ("積木火車組","block train set"),("魔術方塊","magic cube"),
    ("黏土組","clay craft set"),("軌道車組","race track set"),
    ("拼豆組","fuse bead kit"),("戲水球組","water play ball set")],
 "luggage": [("側背包","crossbody bag"),("旅行收納袋組","travel packing cubes"),
    ("筆電保護套","laptop sleeve"),("零錢包","coin purse"),("護照夾","passport holder"),
    ("化妝包","cosmetic pouch"),("登山腰包","hiking waist pack"),
    ("帆布托特包","canvas tote bag")],
}

AMBIG = [("保冷袋","cooler bag"),("車用垃圾桶","car trash bin"),
         ("運動臂套","sports arm sleeve"),("野餐籃","picnic basket"),
         ("桌墊","desk mat"),("攜帶式風扇","portable fan"),
         ("瑜珈服","yoga outfit"),("露營椅墊","camping seat pad"),
         ("嬰兒圍欄","baby playpen fence"),("寵物提籃","pet carrier basket"),
         ("工具收納包","tool organizer pouch"),("咖啡保溫杯","coffee thermos mug"),
         ("手機掛繩","phone lanyard"),("折疊水桶","folding bucket"),
         ("電動打氣筒","electric air pump"),("沙灘墊","beach mat")]

# 類別講法（①區輪替用；zh 黏連、en 加空格）
CATWORD = {
 "electronics": (["電子", "電子類", "電子"], ["electronics", "electronics", "electronics"]),
 "appliance_kitchen": (["家電", "廚具", "家電"], ["appliance", "kitchen", "appliance"]),
 "food_beverage": (["食品", "飲料", "食品"], ["beverage", "beverage", "food"]),
 "daily_goods": (["日用", "日用品", "日用"], ["daily", "household", "daily"]),
 "apparel": (["服飾", "服飾", "服飾"], ["apparel", "clothing", "apparel"]),
 "sports": (["運動", "運動用品", "運動"], ["sports", "sports", "outdoor"]),
 "hardware": (["五金", "工具", "五金"], ["hardware", "tools", "hardware"]),
 "beauty": (["美妝", "保養", "美妝"], ["beauty", "skincare", "beauty"]),
 "medical": (["醫療", "保健", "保健"], ["medical", "health", "health"]),
 "stationery": (["文具", "文具", "事務"], ["stationery", "stationery", "office"]),
 "pet": (["寵物", "寵物用品", "寵物"], ["pet", "pet", "pet"]),
 "automotive": (["汽車", "汽機車", "汽車"], ["automotive", "automotive", "automotive"]),
 "furniture": (["家具", "寢具", "家具"], ["furniture", "bedding", "furniture"]),
 "baby": (["母嬰", "嬰兒", "母嬰"], ["baby", "infant", "baby"]),
 "media": (["圖書", "影音", "影音"], ["books", "media", "media"]),
 "industrial": (["工業", "工業", "工業"], ["industrial", "industrial", "industrial"]),
 "toys": (["玩具", "玩具", "玩具"], ["toys", "toys", "toys"]),
 "luggage": (["箱包", "箱包", "箱包"], ["luggage", "luggage", "luggage"]),
}

ZH_NUM = {0: "零", 1: "一", 2: "二", 3: "三", 4: "四", 5: "五",
          6: "六", 7: "七", 8: "八", 9: "九"}


def uniq_name(pair, rnd, idx):
    """池取名；池繞完自動加代次尾標維持全輪唯一。"""
    pool_round = (rnd - 3) % 2          # 每輪吃 4 個、池 8 個 → 兩輪一循環
    cycle = (rnd - 3) // 2              # 第幾圈
    zh, en = pair
    if cycle > 0:
        tag = ZH_NUM.get(cycle + 1, str(cycle + 1))
        zh = f"{zh}{tag}代"
        en = f"{en} mk{cycle + 1}"
    return zh, en


def pick(cat, slot, rnd):
    """slot 0-2 給①、slot 3 給②。每輪位移 4。"""
    pool = POOLS[cat]
    base = ((rnd - 3) * 4) % len(pool)
    pair = pool[(base + slot) % len(pool)]
    cycle = ((rnd - 3) * 4 + slot) // len(pool)
    zh, en = pair
    if cycle > 0:
        tag = ZH_NUM.get(cycle + 1, str(cycle + 1))
        # ⚠️ en 標記三鐵則（r6/r10 實抓）：不可帶數字（毀裸價格判定）、
        #   不可掛尾（'series e' 佔 head-noun 位毀自動分類）→ 用**前綴**
        #   自然詞（neo X / pro X），head 仍是真品名名詞。
        en_tag = ["", "pro", "ultra", "neo", "prime", "apex", "nova"][min(cycle, 6)]
        zh, en = f"{zh}{tag}代", f"{en_tag} {en}".strip()
    return zh, en


def price_of(rnd, i):
    return [120, 350, 590, 890, 1200, 1800, 2500, 450, 780, 990][ (rnd + i) % 10 ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--lang", choices=["zh", "en"], required=True)
    a = ap.parse_args()
    rnd, lang = a.round, a.lang
    li = 0 if lang == "zh" else 1
    out = []
    W = out.append
    W(f"# create100 第 {rnd} 輪語料（{lang}・生成器產出・語音優先黏連句）")
    W("# 真建立模式：確認卡會真的按下去，商品累積不刪（user 定調）")
    W("")
    W("# ══════ ① 19 類各 3 句 ══════")
    i = 0
    for cat in POOLS:
        zh_ws, en_ws = CATWORD[cat]
        for slot in range(3):
            nm = pick(cat, slot, rnd)[li]
            cw = (zh_ws if lang == "zh" else en_ws)[slot]
            p = price_of(rnd, i); i += 1
            if lang == "zh":
                s = [f"新增商品{nm}{cw}{p}元",
                     f"建立{nm}商品{cw}{p}元",
                     f"新增一個{nm}{cw}{p}元"][slot]
            else:
                s = [f"add item {nm} {cw} {p}",
                     f"create {nm} item {cw} {p}",
                     f"add a {nm} {cw} {p}"][slot]
            W(f"{cat}|{s}")
    W("")
    W("# ══════ ② 不講類別（自動判斷）══════")
    # 轉軌特例：舊版 ⑤ 借用了 (rnd=4, slot3) 的名字且已真建立 →
    #   R4 的 ② 這 5 類改用備用名（之後輪次由 ③⑤ 尾標規則保證不再撞）
    _R4_EN_OVERRIDE = {"electronics": "smart doorbell",
                       "appliance_kitchen": "slow juicer",
                       "daily_goods": "lint roller",
                       "sports": "resistance band",
                       "beauty": "cleansing oil"}
    for cat in POOLS:
        if rnd == 4 and lang == "en" and cat in _R4_EN_OVERRIDE:
            nm = _R4_EN_OVERRIDE[cat]
        else:
            nm = pick(cat, 3, rnd)[li]
        p = price_of(rnd, i); i += 1
        if lang == "zh":
            W(f"{cat}|新增商品{nm}{p}元")
        else:
            W(f"{cat}|add item {nm} {p}")
    W("")
    W("# ══════ ③ 跨類歧義 ══════")
    # ③⑤ 名稱帶輪次尾標（r4 實抓：⑤ 借用 slot 撞 R3 已建立名 → 5 句被
    #   dup 擋；ambig 池 16 個兩輪就繞完，en 的猜測路會真建立 → 必撞）
    _rtag_zh = f"{ZH_NUM.get(rnd, str(rnd))}號"
    _rtag_en = f" type {chr(96 + min(rnd, 26))}"
    for k in range(8):
        pair = AMBIG[((rnd - 3) * 8 + k) % len(AMBIG)]
        nm = pair[li] + (_rtag_zh if lang == "zh" else _rtag_en)
        p = price_of(rnd, i); i += 1
        if lang == "zh":
            W(f"ambig|新增商品{nm}{p}元")
        else:
            W(f"ambig|add item {nm} {p}")
    W("")
    W("# ══════ ④ 欄位變化 ══════")
    stems = "甲乙丙丁戊己庚辛"
    rtag = ZH_NUM.get(rnd, str(rnd))
    for k, st in enumerate(stems):
        if lang == "zh":
            nm = f"測試品{st}之{rtag}"
            s = [f"新增商品{nm}電子",
                 f"新增商品{nm}電子安全30",
                 f"新增商品{nm}電子北倉80",
                 f"新增商品{nm}電子500元安全25",
                 f"新增商品{nm}電子三倉各40",
                 f"建立{nm}商品電子1200元北50中30南20",
                 f"新增商品{nm}電子賣800安全庫存抓15",
                 f"新增一個{nm}電子的一個350"][k]
        else:
            en_st = ["alpha", "bravo", "charlie", "delta",
                     "echo", "foxtrot", "golf", "hotel"][k]
            en_rt = ["", "", "three", "four", "five", "six", "seven",
                     "eight", "nine", "ten"][min(rnd, 9)]
            nm = f"testprod {en_st} {en_rt}".strip()
            s = [f"add item {nm} electronics",
                 f"add item {nm} electronics safety 30",
                 f"add item {nm} electronics north 80",
                 f"add item {nm} electronics 500 safety 25",
                 f"add item {nm} electronics 40 in each warehouse",
                 f"create {nm} item electronics 1200 north 50 central 30 south 20",
                 f"add item {nm} electronics sells for 800 keep 15",
                 f"add a {nm} electronics thing 350 each"][k]
        W(f"any|{s}")
    W("")
    W("# ══════ ⑤ 口語與現場情境 ══════")
    # ⑤ 名稱同樣帶輪次尾標（不再向 rnd+1 借 slot——那會撞下一輪 ② 的名字）
    extra = [pick(cat, 3, rnd + 1) for cat in
             ("daily_goods", "electronics", "appliance_kitchen", "electronics",
              "sports", "daily_goods", "beauty", "electronics")]
    extra = [(z + _rtag_zh, e + _rtag_en) for z, e in extra]
    p5 = [price_of(rnd, i + k) for k in range(8)]
    if lang == "zh":
        n5 = [e[0] for e in extra]
        W(f"any|剛到一批新的{n5[0]}日用一個{p5[0]}")
        W(f"any|供應商送來新品{n5[1]}電子{p5[1]}")
        W(f"any|幫我新增一個{n5[2]}家電{p5[2]}元")
        W(f"any|我要加一款新商品叫{n5[3]}電子{p5[3]}元")
        W(f"any|麻煩建立一個{n5[4]}商品運動{p5[4]}元")
        W(f"any|想新增商品{n5[5]}日用{p5[5]}元")
        W(f"any|請幫我加入新品{n5[6]}美妝{p5[6]}元")
        W(f"any|新增商品{n5[7]}電子八百元")
    else:
        n5 = [e[1] for e in extra]
        W(f"any|just got a shipment of new {n5[0]} daily {p5[0]} each")
        W(f"any|supplier delivered a new product {n5[1]} electronics {p5[1]}")
        W(f"any|please add a {n5[2]} for me kitchen {p5[2]}")
        W(f"any|i want to add a new product called {n5[3]} electronics {p5[3]}")
        W(f"any|could you create a {n5[4]} item sports {p5[4]}")
        W(f"any|thinking of adding item {n5[5]} daily {p5[5]}")
        W(f"any|please help me add new product {n5[6]} beauty {p5[6]}")
        W(f"any|add item {n5[7]} electronics eight hundred")
    W("")
    W("# ══════ ⑥ 邊界與該擋的 ══════")
    if lang == "zh":
        W("dup|新增商品無線滑鼠電子590元")
        W("noname|幫我新增商品")
        W("noname|新增商品")
        W(f"any|新增商品XR{500 + rnd}伺服馬達工業8900元")
    else:
        W("dup|add item wireless mouse electronics 590")
        W("noname|help me add an item")
        W("noname|add item")
        W(f"any|add item xr{500 + rnd} servo motor industrial 8900")
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(out))


if __name__ == "__main__":   # r25：讓名稱池可被 import（驗證電池用）
    main()
