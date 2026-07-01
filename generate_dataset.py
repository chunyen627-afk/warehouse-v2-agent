"""
generate_dataset.py — 倉管版 FunctionGemma 微調資料產生器 (v3.8)

設計：
  - 5 個 function
  - SKU 走 keyword 自由字串、不死記 SKU enum
  - keyword 樣本源：seed_data.items + 部分俗稱（耳機 / 咖啡豆 / 洗衣精）
  - 預期總筆數 ~1940

用法:
    python generate_dataset.py
    → 覆蓋 training_data.jsonl
"""
import csv
import json
import random
from pathlib import Path
from collections import Counter

random.seed(42)
OUT = Path(__file__).parent / "training_data.jsonl"
# seed_data.json 已於 2026-06-30 淘汰（commit 7d44016），資料改讀 warehouse_data/。
# 這裡只需要 items（給 KEYWORD_SAMPLES 用 name），直接讀 items.csv，不需要完整 loader_v2。
_ITEMS_CSV = Path(__file__).parent / "test" / "warehouse_data" / "master" / "items.csv"
with open(_ITEMS_CSV, encoding="utf-8-sig") as _f:
    SEED = {"items": [{"sku_id": r["sku_id"], "name": r["name"], "category": r["category"],
                       "unit_price": int(r["unit_price"]), "safety_stock": int(r["safety_stock"])}
                      for r in csv.DictReader(_f)]}
samples = []


def add(user_content, tool_name, tool_args):
    samples.append({
        "user_content": user_content,
        "tool_name":    tool_name,
        "tool_arguments": json.dumps(tool_args, ensure_ascii=False),
    })


# ════════════════════════════════════════════════════════════
# v2 三金剛訓練資料（search_log / manage_config / run_script）
#   原則：會變動的清單（商品名/設定項/腳本名）一律 keyword verbatim 抽取，
#         模型抽出原文，server 端 match → 不進 enum（守 D5）。
# ════════════════════════════════════════════════════════════
def gen_v2_tools():
    n0 = len(samples)
    item_names = [it["name"] for it in ITEMS]

    # ── ① search_log（RCA）────────────────────────────────────
    # 觸發詞刻意跟 query_movement 拉開：對不上/異常/誰改的/怎麼少/查原因/短收
    RCA_TPL = [
        "{kw}怎麼少這麼多", "{kw}帳對不上", "{kw}庫存異常", "查一下{kw}為什麼短少",
        "{kw}的進貨對不上", "幫我追{kw}的扣帳異常", "{kw}怎麼會對不起來", "{kw}少貨原因",
        "{kw}入庫數量不對", "{kw}是誰動的", "追{kw}的庫存差異", "{kw}短收了嗎",
    ]
    RCA_TPL_TR = [
        "{kw}{tr}怎麼少這麼多", "{kw}{tr}帳對不上", "{tr}{kw}有異常嗎",
        "查{kw}{tr}的扣帳", "{kw}{tr}短收", "{tr}{kw}入庫對不上",
    ]
    EN_RCA = ["why is {kw} short", "{kw} discrepancy", "trace {kw} stock issue", "who changed {kw}"]
    TR_ZH = {"today": "今天", "this_week": "這週", "this_month": "這個月"}

    for nm in item_names:
        for t in random.sample(RCA_TPL, 4):
            add(t.format(kw=nm), "search_log", {"keyword": nm})
        for t in random.sample(RCA_TPL_TR, 2):
            tr = random.choice(list(TR_ZH))
            add(t.format(kw=nm, tr=TR_ZH[tr]), "search_log", {"keyword": nm, "time_range": tr})
    for nm in random.sample(item_names, 20):
        add(random.choice(EN_RCA).format(kw=nm), "search_log", {"keyword": nm})

    # 切界負樣本：純進出統計 → 必須走 query_movement（不是 search_log）
    MOV_NEG = ["{kw}這週出貨多少", "{kw}進貨量", "{kw}本月進出", "{kw}出了幾件", "{kw}的進出記錄"]
    for nm in random.sample(item_names, 40):
        t = random.choice(MOV_NEG)
        add(t.format(kw=nm), "query_movement", {"period": "this_week", "direction": "out"})

    # ── ② manage_config ───────────────────────────────────────
    # read
    CFG_KEYS_READ = [
        ("安全庫存", "安全庫存"), ("補貨前置天數", "前置天數"), ("前置天數", "前置天數"),
        ("安全水位倍數", "安全水位倍數"), ("補貨目標天數", "補貨目標天數"),
    ]
    READ_TPL = ["現在的{k}設多少", "{k}是多少", "查一下{k}", "目前{k}設定", "{k}現在幾",
                "看一下{k}", "{k}設定值", "現在{k}多少", "{k}現在設定", "幫我查{k}",
                "{k}是設多少", "目前{k}是幾"]
    for kdisp, kval in CFG_KEYS_READ:
        for t in READ_TPL:
            add(t.format(k=kdisp), "manage_config", {"action": "read", "key": kval})
    # 安全庫存 read 帶商品（擴量）
    READ_ITEM_TPL = ["{kw}的安全庫存是多少", "{kw}安全庫存設多少", "查{kw}安全庫存",
                     "{kw}的安全水位", "{kw}安全庫存現在幾"]
    for nm in random.sample(item_names, 50):
        add(random.choice(READ_ITEM_TPL).format(kw=nm), "manage_config",
            {"action": "read", "key": "安全庫存"})

    # set（含 +N 相對 / 絕對 / 分倉）
    WH_ZH = {"north": "北倉", "central": "中倉", "south": "南倉", "all": "全部"}
    SET_TPL_REL = ["{wh}安全庫存全部加{n}", "{wh}的安全庫存都+{n}", "把{wh}安全庫存調高{n}", "{wh}安全庫存統一加{n}"]
    SET_TPL_ABS_ITEM = ["把{kw}安全庫存改成{n}", "{kw}安全庫存設{n}", "{kw}的安全庫存改{n}"]
    SET_TPL_LEAD = ["補貨前置天數改成{n}", "前置天數設{n}", "把前置天數調到{n}"]
    for wh in ["north", "central", "south", "all"]:
        for t in SET_TPL_REL:
            n = random.choice([10, 20, 30, 50])
            add(t.format(wh=WH_ZH[wh], n=n), "manage_config",
                {"action": "set", "key": "安全庫存", "value": f"+{n}", "warehouse": wh})
    for nm in random.sample(item_names, 55):
        n = random.choice([20, 50, 80, 100, 30, 60])
        add(random.choice(SET_TPL_ABS_ITEM).format(kw=nm, n=n), "manage_config",
            {"action": "set", "key": "安全庫存", "value": str(n)})
    # 商品 + 分倉 set
    for nm in random.sample(item_names, 24):
        wh = random.choice(["north", "central", "south"])
        n = random.choice([20, 50, 80])
        add(f"{WH_ZH[wh]}的{nm}安全庫存改成{n}", "manage_config",
            {"action": "set", "key": "安全庫存", "value": str(n), "warehouse": wh})
    for t in SET_TPL_LEAD:
        n = random.choice([3, 5, 7, 10, 14])
        add(t.format(n=n), "manage_config", {"action": "set", "key": "前置天數", "value": str(n)})

    # ── ③ run_script ──────────────────────────────────────────
    SCRIPTS = {
        "盤點": ["幫我跑一次月底盤點", "執行盤點", "做個庫存盤點", "跑盤點", "月底盤點一下",
               "現在盤點", "幫我盤點", "來個盤點", "啟動盤點", "我要盤點", "庫存盤點一下",
               "執行月底盤點", "跑一下盤點作業", "盤點全倉", "做盤點報告"],
        "匯出": ["匯出進出記錄", "把異動匯出來", "匯出movements", "幫我匯出交易", "匯出進出明細",
               "匯出庫存異動", "把進出記錄匯出", "匯出交易記錄", "幫我匯出進出", "匯出資料",
               "把交易匯成excel", "匯出這個月異動", "匯出全部進出"],
        "重產": ["重新產生種子資料", "重產資料", "重生seed", "重新產生資料", "重建種子",
               "重跑種子資料", "重新生成資料", "幫我重產seed", "重新整理資料", "重建資料檔",
               "重新產生種子", "重生種子資料"],
    }
    for key, tpls in SCRIPTS.items():
        for t in tpls:
            add(t, "run_script", {"script_name": key})
    # 英文
    for t, key in [("run month-end stock audit", "盤點"), ("do a stock count", "盤點"),
                   ("export movements", "匯出"), ("export transactions", "匯出"),
                   ("regenerate seed data", "重產"), ("regen seed", "重產")]:
        add(t, "run_script", {"script_name": key})

    # ── ④ generate_report（A 波：寫報告）──────────────────────
    RPT_FULL = ["幫我出個全倉體檢報告", "產生全倉報告", "做份倉庫健檢報告", "出個總覽報表",
                "全倉掃描出報告", "幫我整理一份報告", "產生倉儲報表", "出份完整報告",
                "倉庫體檢報告", "彙整一份全倉報告", "做個盤點報告", "生成倉儲體檢",
                "出報告", "產報告", "幫我做報告", "來份報告", "整理報告給我",
                "倉庫狀況報告", "做一份倉儲總覽", "輸出全倉報表", "我要一份報告",
                "幫我產生報告", "出一份體檢", "生成報告", "報表輸出一下"]
    for t in RPT_FULL:
        add(t, "generate_report", {"report_type": "full"})
    RPT_TYPED = [("出個缺貨報表", "low_stock"), ("缺貨報告給我", "low_stock"),
                 ("補貨清單報表", "low_stock"), ("缺貨清單做成報告", "low_stock"),
                 ("低庫存報表", "low_stock"), ("到期商品報告", "expiring"),
                 ("效期警示報表", "expiring"), ("快過期的做份報告", "expiring"),
                 ("保存期限報表", "expiring"), ("異常對帳報告", "rca"),
                 ("短收異常報表", "rca"), ("對不上的彙整報告", "rca"),
                 ("採購異常報告", "rca"), ("對帳差異報表", "rca")]
    for t, rt in RPT_TYPED:
        add(t, "generate_report", {"report_type": rt})
    add("generate a full warehouse report", "generate_report", {"report_type": "full"})
    add("export inventory report", "generate_report", {"report_type": "full"})

    # ── ⑤ list_files（B 波：動態找檔）──────────────────────────
    LF = ["倉庫裡有哪些紀錄檔", "有什麼資料可以查", "列出檔案", "有哪些資料夾",
          "看一下有哪些檔", "有哪些目錄可以看", "資料區有哪些", "列出資料",
          "有哪些資料可以看", "現在有什麼檔案", "有哪些檔可以讀", "資料夾裡有什麼",
          "列一下所有檔", "有哪些資料來源", "看看有什麼資料", "有什麼可以查的"]
    for t in LF:
        add(t, "list_files", {})
    LF_AREA = [("transactions有哪些檔", "transactions"), ("交易紀錄檔有哪些", "transactions"),
               ("進出紀錄檔列一下", "transactions"), ("採購單有哪些", "orders"),
               ("訂單檔有哪些", "orders"), ("主檔有什麼", "master"),
               ("設定檔在哪", "master"), ("看一下報告目錄", "reports"),
               ("有哪些報告", "reports"), ("異動紀錄檔列一下", "audit"),
               ("audit有什麼", "audit"), ("腳本有哪些", "scripts")]
    for t, area in LF_AREA:
        add(t, "list_files", {"area": area})

    # ── ⑥ set_alert（第四金剛：警示規則）──────────────────────
    AL_COND = {
        "below_safety": ["低於安全庫存", "快缺貨", "庫存不足", "低於安全線"],
        "out_of_stock": ["缺貨", "斷貨", "沒貨", "零庫存"],
        "expiring": ["快到期", "到期", "快過期", "效期快到"],
    }
    AL_TPL_NOTGT = ["{c}就通知我", "{c}的時候提醒我", "設個{c}警示", "{c}就告訴我", "幫我設{c}提醒"]
    AL_TPL_TGT = ["{kw}{c}就通知我", "{kw}{c}提醒我", "{kw}{c}的時候叫我", "幫我盯{kw}{c}"]
    import random as _r1
    for cond, words in AL_COND.items():
        for w in words:
            for t in _r1.sample(AL_TPL_NOTGT, 2):
                add(t.format(c=w), "set_alert", {"condition": cond})
    for nm in _r1.sample(item_names, 30):
        cond = _r1.choice(list(AL_COND))
        w = _r1.choice(AL_COND[cond])
        add(_r1.choice(AL_TPL_TGT).format(kw=nm, c=w), "set_alert",
            {"condition": cond, "target": nm})

    # ── ⑦ generate_po（閉環：產採購單）────────────────────────
    PO_LOW = ["幫我把缺貨的產採購單", "缺貨的開張採購單", "產採購單補貨", "幫我補貨下單",
              "把要補的貨產採購單", "缺貨清單轉採購單", "開補貨單", "產張採購單",
              "幫我叫貨", "缺的貨幫我下單", "補貨採購單", "產進貨單"]
    for t in PO_LOW:
        add(t, "generate_po", {"source": "low_stock"})
    PO_SHORT = ["短收的補單", "對不上的開採購單補", "短收商品產採購單", "把短收的補回來下單"]
    for t in PO_SHORT:
        add(t, "generate_po", {"source": "shortfall"})

    # ── ⑧ compare_periods（跨期比較）──────────────────────────
    CP = ["這個月跟上個月哪些變化大", "本月對比上月", "這月和上月比一比", "跨期比較",
          "哪些商品變化最大", "成長最多的是哪些", "衰退最多的商品", "本月vs上月變化",
          "兩個月出貨變化", "跟上月相比哪些差很多", "月增減分析", "這月比上月"]
    for t in CP:
        add(t, "compare_periods", {"metric": "out"})

    # ── ⑨ 口語化句式補強（「我想要/採購對帳異常/我要查」等）──────
    _colloquial_samples(item_names)

    print(f"  gen_v2_tools: +{len(samples)-n0} 條"
          f"（search_log/manage_config/run_script/generate_report/list_files + 切界負樣本 + 口語化）")


# ════════════════════════════════════════════════════════════
# C 波（試 270M 極限）：judge_cause_found 決策點小分類
#   輸入 = 「追查問題 + [一行壓縮的工具結果]」（context 刻意壓短，守雷4）
#   輸出 = judge_cause_found{found: yes|no}
#   ⚠️ 這是試訓性質：270M 做「讀中間結果判斷」是尺寸極限，先小量驗證可行性。
# ════════════════════════════════════════════════════════════
def gen_v2_judge():
    n0 = len(samples)
    item_names = [it["name"] for it in ITEMS]

    # found=yes：context 明確含「短收/對不上/差異」→ 追到了
    YES_CTX = [
        "[結果] 採購單{po}短收{n}件，應收{a}實收{b}",
        "[結果] {nm}在{po}應收{a}、實收{b}，差{n}件",
        "[查到] {po}短收{n}件對不上",
        "[結果] 發現1筆短收：{nm}差{n}件",
    ]
    # found=no：context 明確「無異常/查無」→ 還沒追到
    NO_CTX = [
        "[結果] 進貨{a}出貨{b}，未發現短收異常",
        "[結果] {nm}查無異常紀錄",
        "[查到] 0筆短收，帳目正常",
        "[結果] 該範圍無對不上的採購單",
    ]
    POS = ["PO00116", "PO00192", "PO00530", "PO00664", "PO00231", "PO00405"]

    import random as _r
    for _ in range(110):
        nm = _r.choice(item_names); po = _r.choice(POS)
        a = _r.choice([48, 84, 100, 92, 68]); n = _r.choice([8, 12, 15, 20])
        b = a - n
        # yes
        ctx = _r.choice(YES_CTX).format(po=po, nm=nm, a=a, b=b, n=n)
        add(f"追查{nm}的庫存問題 {ctx}", "judge_cause_found", {"found": "yes"})
        # no
        ctx2 = _r.choice(NO_CTX).format(nm=nm, a=a, b=b)
        add(f"追查{nm}的庫存問題 {ctx2}", "judge_cause_found", {"found": "no"})

    print(f"  gen_v2_judge (C波試訓): +{len(samples)-n0} 條 judge_cause_found")


def _colloquial_samples(item_names):
    """口語化句式補強。
    解決 set_alert(keyword=...) / run_script(script_name=...) 亂路由問題。
    """
    n0 = len(samples)

    # ── A. 「我想要 / 我想 / 我要」前綴 + 查庫存 ──────────────────
    WANT_INV = [
        "我想要查{kw}的庫存", "我想查一下{kw}現在有多少", "我要看{kw}的庫存",
        "我想要知道{kw}還剩多少", "我要查{kw}的現量", "我想看{kw}庫存狀況",
        "我要了解{kw}的庫存", "我想要查一下{kw}", "我要查{kw}",
        "我想知道{kw}有幾個", "能不能查一下{kw}的存量", "幫我查一下{kw}庫存",
        "幫我看看{kw}有多少", "幫我查{kw}現在的存量", "幫我確認{kw}庫存",
    ]
    for nm in item_names:
        for t in random.sample(WANT_INV, 4):
            add(t.format(kw=nm), "query_inventory", {"keyword": nm})

    # ── B. 「我想要 / 我想 / 我要」前綴 + 查進出 ──────────────────
    WANT_MOV = [
        "我想要看{kw}的進出記錄", "我想查{kw}的出貨量", "我要看{kw}最近的動態",
        "我想要知道{kw}這週出了多少", "我要查{kw}的進貨量", "幫我查{kw}的移動記錄",
        "幫我看{kw}本月進出", "我想要查{kw}本週的出貨", "我要查今天{kw}的進貨",
    ]
    for nm in random.sample(item_names, min(30, len(item_names))):
        t = random.choice(WANT_MOV)
        add(t.format(kw=nm), "query_movement", {"keyword": nm, "period": "this_week"})

    # ── C. 採購對帳異常 → search_log ──────────────────────────────
    PO_ANOMALY_TPL = [
        "{kw}採購對帳異常", "{kw}的採購對不上", "採購單{kw}有問題", "{kw}採購對帳查一下",
        "查一下{kw}採購異常", "{kw}進貨對帳有問題", "採購{kw}帳對不上",
        "幫我查{kw}採購對帳", "{kw}採購對不起來", "追查{kw}採購異常",
        "{kw}進貨單對帳異常", "採購{kw}短收了嗎", "查{kw}採購短收",
        "我想要查{kw}採購異常", "我要查{kw}對帳問題",
    ]
    for nm in item_names:
        for t in random.sample(PO_ANOMALY_TPL, 3):
            add(t.format(kw=nm), "search_log", {"keyword": nm})

    # ── D. set_alert 口語化款（帶 keyword + threshold）──────────────
    ALERT_TPL = [
        "我想要{kw}低於{n}個時通知我", "我要在{kw}庫存不足{n}時提醒",
        "幫我設{kw}的庫存警示，低於{n}就通知", "我想設一個{kw}的警報，剩{n}個時叫我",
        "幫我設定{kw}庫存提醒，{n}個以下要通知", "我要{kw}低於{n}時發出警示",
        "設定{kw}庫存警示閾值{n}", "我想要設{kw}的低庫存提醒", "幫我設{kw}警報",
    ]
    thresholds = [10, 20, 30, 50]
    for nm in random.sample(item_names, min(25, len(item_names))):
        n = random.choice(thresholds)
        t = random.choice(ALERT_TPL)
        add(t.format(kw=nm, n=n), "set_alert", {"keyword": nm, "threshold": n})

    # ── E. 切界負樣本：「我想要看銷量/熱銷/低庫存」→ 不走 set_alert ─
    WANT_HOT = [
        "我想要看熱銷商品", "我要看本週銷量", "我想要看本月熱銷排行",
        "我要知道哪些賣最好", "我想看滯銷清單", "幫我看看哪些賣得好",
        "我要看冷門商品", "我想查熱銷排名",
    ]
    for t in WANT_HOT:
        add(t, "list_hot_items", {"rank_type": "hot", "period": "this_month"})

    WANT_LOW = [
        "我想要看哪些快沒貨", "我要看低庫存清單", "我想查庫存不足的商品",
        "幫我看看哪些快缺貨", "我想知道哪些商品庫存低", "我要查快沒了的東西",
    ]
    for t in WANT_LOW:
        add(t, "list_low_stock", {})

    print(f"  _colloquial_samples: +{len(samples)-n0} 條"
          f"（我想要/採購對帳異常/set_alert口語/切界負樣本）")


def pick(items, k):
    if k <= len(items):
        return random.sample(items, k)
    out = list(items)
    while len(out) < k:
        out.append(random.choice(items))
    return out


# ════════════════════════════════════════════════════════════════════
# 字彙池
# ════════════════════════════════════════════════════════════════════

CATEGORY_ZH = {
    "electronics":       ["電子產品", "電子類", "電子", "3C 產品"],
    "appliance_kitchen": ["家電廚具", "家電", "廚具", "廚房用品"],
    "food_beverage":     ["食品飲料", "食品", "飲料", "食品類"],
    "daily_goods":       ["日用品", "日用", "生活用品"],
    "apparel":           ["服飾", "衣服", "服裝"],
    "sports":            ["運動用品", "運動", "運動類"],
}

WAREHOUSE_ZH = {
    "north":   ["北區倉", "北倉", "北部倉", "北區"],
    "central": ["中區倉", "中倉", "中部倉", "中區"],
    "south":   ["南區倉", "南倉", "南部倉", "南區"],
    "all":     ["全部倉", "三個倉", "所有倉"],
}

PERIOD_ZH = {
    "today":      ["今天", "今日", "本日"],
    "this_week":  ["本週", "這週", "這禮拜", "本週內"],
    "this_month": ["本月", "這個月", "這月", "月內"],
}

DIRECTION_ZH = {
    "in":   ["進貨", "入庫", "進倉"],
    "out":  ["出貨", "出庫", "出倉", "賣出"],
    "both": ["進出貨", "進出", "出入"],
}

METRIC_ZH = {
    "stock_value": ["庫存價值", "庫存總額", "貨值"],
    "item_count":  ["商品數量", "件數", "庫存量", "數量"],
    "turnover":    ["週轉率", "週轉", "庫存週轉"],
}

RANK_ZH = {
    "hot":  ["熱銷", "賣最好", "最熱門", "暢銷", "銷量第一"],
    "slow": ["滯銷", "賣最差", "賣不掉", "最冷門", "銷量最差"],
}

# ────────────────────────────────────────────────
# Keyword 樣本源
#   - 從 seed_data.items 抽 name 全名（直接命中）
#   - 抽 substring（半名）模擬訪客口語講法
#   - 加幾個常見俗稱（不在 SKU 名內但很口語）
# ────────────────────────────────────────────────

ITEMS = SEED["items"]
KEYWORD_SAMPLES = []

# 全名（30 個）
for it in ITEMS:
    KEYWORD_SAMPLES.append(it["name"])

# 半名 / 縮寫（30+ 個常用講法）
KEYWORD_SHORT_FORMS = [
    "藍牙耳機", "藍牙喇叭", "行動電源", "快充線", "智慧手環",
    "悶燒罐", "電熨斗", "不沾鍋", "電動牙刷", "果汁機",
    "氣泡水", "咖啡豆", "檸檬茶", "堅果", "蘇打餅",
    "洗衣精", "衛生紙", "沐浴乳", "蚊香", "垃圾袋",
    "素T", "羊毛襪", "羽絨外套", "牛仔褲", "運動內衣",
    "瑜珈墊", "水壺", "健身環", "慢跑鞋", "毛巾",
    # 較通用詞
    "耳機", "喇叭", "充電線",
    "牙刷", "鍋具", "果汁",
    "咖啡", "茶飲",
    "洗劑", "紙巾",
    "外套", "褲子", "T 恤",
    "運動鞋",
]


# ════════════════════════════════════════════════════════════════════
# 1. query_inventory (~800)
# ════════════════════════════════════════════════════════════════════
# (a) keyword × warehouse 變體
TEMPLATES_KW = [
    "{kw}現在有多少", "{kw}庫存", "{kw}還有多少", "{kw}剩多少件",
    "查一下{kw}", "{kw}多少件", "{kw}存量", "看一下{kw}庫存",
    "{kw}有幾件", "{kw}庫存量",
]
TEMPLATES_KW_WH = [
    "{wh}的{kw}還剩多少", "{wh}{kw}庫存", "{wh}{kw}有幾件",
    "查{wh}的{kw}", "{wh}{kw}多少", "{kw}在{wh}有幾件",
    "看一下{wh}的{kw}", "{wh}的{kw}存量",
]

for kw in KEYWORD_SHORT_FORMS:
    for tpl in TEMPLATES_KW:
        add(tpl.format(kw=kw), "query_inventory", {"keyword": kw})

for kw in pick(KEYWORD_SHORT_FORMS, 25):
    for wh_key, zh_list in WAREHOUSE_ZH.items():
        if wh_key == "all":
            continue
        wh_zh = random.choice(zh_list)
        tpl = random.choice(TEMPLATES_KW_WH)
        add(tpl.format(wh=wh_zh, kw=kw), "query_inventory",
            {"keyword": kw, "warehouse": wh_key})

# (b) category 變體
TEMPLATES_CAT = [
    "{cat}庫存", "{cat}類庫存", "{cat}還有多少", "{cat}有什麼",
    "查{cat}", "{cat}類商品", "{cat}存量", "看一下{cat}類庫存",
]
TEMPLATES_CAT_WH = [
    "{wh}的{cat}", "{wh}{cat}庫存", "{wh}的{cat}有什麼",
    "查{wh}的{cat}類", "{wh}{cat}商品", "{cat}在{wh}",
]

for cat_key, cat_zh_list in CATEGORY_ZH.items():
    for cat_zh in cat_zh_list:
        for tpl in TEMPLATES_CAT:
            add(tpl.format(cat=cat_zh), "query_inventory", {"category": cat_key})
        for wh_key, wh_zh_list in WAREHOUSE_ZH.items():
            if wh_key == "all":
                continue
            wh_zh = random.choice(wh_zh_list)
            tpl = random.choice(TEMPLATES_CAT_WH)
            add(tpl.format(wh=wh_zh, cat=cat_zh), "query_inventory",
                {"category": cat_key, "warehouse": wh_key})

# (c) 英文 query 變體 (~30)
EN_KW_TPL = [
    "how much {kw} stock", "{kw} inventory", "check {kw} stock",
    "{kw} quantity",
]
for kw in ["bluetooth earphone", "sparkling water", "coffee", "yoga mat", "tshirt", "laundry detergent"]:
    for tpl in EN_KW_TPL:
        add(tpl.format(kw=kw), "query_inventory", {"keyword": kw})


# ════════════════════════════════════════════════════════════════════
# 2. query_movement (~430)
# ════════════════════════════════════════════════════════════════════
TPL_MOV_PERIOD = [
    "{p}進了什麼貨", "{p}進貨多少", "{p}出貨多少", "{p}進出狀況",
    "{p}所有進出記錄", "{p}有什麼進出", "{p}動了多少",
]
TPL_MOV_PERIOD_DIR = [
    "{p}{dir}多少", "{p}{dir}總量", "{p}{dir}記錄", "{p}{dir}量",
]
TPL_MOV_PERIOD_KW = [
    "{p}{kw}{dir}多少", "{p}的{kw}{dir}", "{p}{kw}動了多少",
    "{kw}{p}{dir}多少",
]

for period_key, p_zh_list in PERIOD_ZH.items():
    for p_zh in p_zh_list:
        # (a) 純 period
        for tpl in TPL_MOV_PERIOD:
            add(tpl.format(p=p_zh), "query_movement", {"period": period_key})
        # (b) period + direction
        for dir_key, dir_zh_list in DIRECTION_ZH.items():
            for dir_zh in dir_zh_list:
                for tpl in TPL_MOV_PERIOD_DIR:
                    add(tpl.format(p=p_zh, dir=dir_zh), "query_movement",
                        {"period": period_key, "direction": dir_key})

# (c) period + keyword + direction
for kw in pick(KEYWORD_SHORT_FORMS, 12):
    for period_key, p_zh_list in PERIOD_ZH.items():
        p_zh = random.choice(p_zh_list)
        for dir_key, dir_zh_list in DIRECTION_ZH.items():
            dir_zh = random.choice(dir_zh_list)
            tpl = random.choice(TPL_MOV_PERIOD_KW)
            add(tpl.format(p=p_zh, kw=kw, dir=dir_zh), "query_movement",
                {"period": period_key, "keyword": kw, "direction": dir_key})


# ════════════════════════════════════════════════════════════════════
# 3. list_low_stock (~140)
# ════════════════════════════════════════════════════════════════════
TPL_LOW = [
    "庫存警示", "缺貨清單", "哪些東西快沒了", "需要補貨的有哪些",
    "快沒貨的東西", "缺貨警示", "補貨建議", "庫存不足清單",
    "存量警報", "庫存告急的商品", "低於安全庫存的有哪些",
]
for q in TPL_LOW:
    add(q, "list_low_stock", {})

TPL_LOW_WH = [
    "{wh}缺貨清單", "{wh}庫存警示", "{wh}快沒貨的東西",
    "{wh}需要補貨的", "{wh}低庫存清單",
]
for wh_key, wh_zh_list in WAREHOUSE_ZH.items():
    if wh_key == "all":
        continue
    for wh_zh in wh_zh_list:
        for tpl in TPL_LOW_WH:
            add(tpl.format(wh=wh_zh), "list_low_stock", {"warehouse": wh_key})

TPL_LOW_CAT = [
    "{cat}類有沒有快缺貨的", "{cat}庫存警示", "{cat}缺貨清單",
    "{cat}需要補貨的有哪些",
]
for cat_key, cat_zh_list in CATEGORY_ZH.items():
    for cat_zh in cat_zh_list:
        for tpl in TPL_LOW_CAT:
            add(tpl.format(cat=cat_zh), "list_low_stock", {"category": cat_key})

# Eng 變體
for q in ["low stock alert", "what's low stock", "restock list", "running low items"]:
    add(q, "list_low_stock", {})


# ════════════════════════════════════════════════════════════════════
# 4. compare_warehouses (~400)
# ════════════════════════════════════════════════════════════════════
TPL_CMP = [
    "{wha}跟{whb}哪個{m}比較多", "{wha}跟{whb}{m}比較",
    "比一下{wha}跟{whb}的{m}", "{wha}{whb}的{m}差多少",
    "{wha}跟{whb}哪個{m}高", "比較{wha}跟{whb}的{m}",
    "{wha}跟{whb}哪個{m}低", "{wha}跟{whb}的{m}誰高",
    "{wha}和{whb}的{m}比一下", "{wha}和{whb}{m}比較",
    "{wha}vs{whb} {m}", "{wha} vs {whb}的{m}",
    "{wha}跟{whb}{m}誰多", "比{wha}和{whb}的{m}",
    "{m}哪個高{wha}還是{whb}", "{wha}{whb}哪個{m}多",
    "看一下{wha}跟{whb}的{m}", "{wha}跟{whb}的{m}",
    "{wha}的{m}跟{whb}比", "比較一下{wha}和{whb}的{m}",
]

WH_KEYS = ["north", "central", "south"]
WH_PAIRS = []
for i, a in enumerate(WH_KEYS):
    for b in WH_KEYS[i+1:]:
        WH_PAIRS.append((a, b))
        WH_PAIRS.append((b, a))   # 兩個方向都覆蓋

# 控制版：每組 (wh_a, wh_b, metric) 抽 ~17 個樣本（隨機選 wh_a/wh_b/metric 同義詞 + 模板）
# 6 對倉 × 3 metric × 17 = 306 條 ≈ 目標 ~300
N_CMP_PER_GROUP = 17
for wh_a, wh_b in WH_PAIRS:
    for metric_key, m_zh_list in METRIC_ZH.items():
        seen = set()
        attempts = 0
        while len(seen) < N_CMP_PER_GROUP and attempts < N_CMP_PER_GROUP * 3:
            attempts += 1
            wh_a_zh = random.choice(WAREHOUSE_ZH[wh_a])
            wh_b_zh = random.choice(WAREHOUSE_ZH[wh_b])
            m_zh = random.choice(m_zh_list)
            tpl = random.choice(TPL_CMP)
            q = tpl.format(wha=wh_a_zh, whb=wh_b_zh, m=m_zh)
            if q in seen:
                continue
            seen.add(q)
            add(q, "compare_warehouses",
                {"warehouse_a": wh_a, "warehouse_b": wh_b, "metric": metric_key})

# Eng
EN_CMP_TPL = [
    "compare {a} and {b} {m}", "{a} vs {b} {m}",
]
EN_WH = {"north": "north warehouse", "central": "central warehouse", "south": "south warehouse"}
EN_METRIC = {"stock_value": "stock value", "item_count": "item count", "turnover": "turnover"}
for wh_a, wh_b in WH_PAIRS[:3]:
    for metric_key in METRIC_ZH:
        for tpl in EN_CMP_TPL:
            add(tpl.format(a=EN_WH[wh_a], b=EN_WH[wh_b], m=EN_METRIC[metric_key]),
                "compare_warehouses",
                {"warehouse_a": wh_a, "warehouse_b": wh_b, "metric": metric_key})


# ════════════════════════════════════════════════════════════════════
# 5. list_hot_items (~170)
# ════════════════════════════════════════════════════════════════════
TPL_HOT = [
    "{p}{rank}的商品", "{p}{rank}是誰", "{p}{rank}排行",
    "{p}哪些商品{rank}", "{p}{rank} TOP 10",
]
TPL_HOT_CAT = [
    "{p}{cat}類{rank}排行", "{cat}類{p}{rank}的商品",
    "{p}{cat}{rank}", "{cat}{p}賣得怎樣",
]

for rank_key, rank_zh_list in RANK_ZH.items():
    for period_key in ["this_week", "this_month"]:
        for p_zh in PERIOD_ZH[period_key]:
            for rank_zh in rank_zh_list:
                for tpl in TPL_HOT:
                    add(tpl.format(p=p_zh, rank=rank_zh), "list_hot_items",
                        {"rank_type": rank_key, "period": period_key})

for rank_key, rank_zh_list in RANK_ZH.items():
    for cat_key, cat_zh_list in CATEGORY_ZH.items():
        for period_key in ["this_week", "this_month"]:
            p_zh = random.choice(PERIOD_ZH[period_key])
            cat_zh = random.choice(cat_zh_list)
            rank_zh = random.choice(rank_zh_list)
            tpl = random.choice(TPL_HOT_CAT)
            add(tpl.format(p=p_zh, cat=cat_zh, rank=rank_zh), "list_hot_items",
                {"rank_type": rank_key, "period": period_key, "category": cat_key})

# Eng
for tpl in ["top selling {p}", "best seller {p}", "what's hot {p}"]:
    add(tpl.format(p="this week"), "list_hot_items",
        {"rank_type": "hot", "period": "this_week"})
    add(tpl.format(p="this month"), "list_hot_items",
        {"rank_type": "hot", "period": "this_month"})
for tpl in ["worst selling {p}", "slow movers {p}"]:
    add(tpl.format(p="this week"), "list_hot_items",
        {"rank_type": "slow", "period": "this_week"})
    add(tpl.format(p="this month"), "list_hot_items",
        {"rank_type": "slow", "period": "this_month"})


# ════════════════════════════════════════════════════════════════════
# === 補強區 (v3.8 round 2 — 修第一輪 e2e fail case) ===
# 加 ~200 條針對性 paraphrase 修：
#   - query_inventory: 「XX 現在有多少 / XX 目前剩多少」(LLM 把「現在」理解成 period 走 movement)
#   - query_inventory: 「{cat} {warehouse}」省略動詞句型
#   - query_movement: 「{p}{cat}進出狀況/所有記錄/全部進出」變體
#   - list_hot_items: 「{cat}{rank}」強化 category slot 抽取
# ════════════════════════════════════════════════════════════════════

# (a) query_inventory:「XX 現在 / 目前 / 當下」修「藍牙耳機現在有多少」fail
TPL_INV_NOW = [
    "{kw}現在有多少", "{kw}目前有多少", "{kw}當下有多少",
    "{kw}現在剩多少", "{kw}目前剩多少",
    "{kw}現在還有多少", "{kw}目前還有多少",
    "{kw}現在幾件", "{kw}目前庫存",
    "現在{kw}庫存多少", "目前{kw}還有多少",
]
for kw in pick(KEYWORD_SHORT_FORMS, 20):
    for tpl in TPL_INV_NOW:
        add(tpl.format(kw=kw), "query_inventory", {"keyword": kw})

# (b) query_inventory: 「{cat}{wh}」省略動詞 (「運動用品中區倉」fail)
for cat_key, cat_zh_list in CATEGORY_ZH.items():
    for cat_zh in cat_zh_list:
        for wh_key, wh_zh_list in WAREHOUSE_ZH.items():
            if wh_key == "all":
                continue
            for wh_zh in wh_zh_list[:2]:
                add(f"{cat_zh}{wh_zh}", "query_inventory",
                    {"category": cat_key, "warehouse": wh_key})
                add(f"{wh_zh}{cat_zh}", "query_inventory",
                    {"category": cat_key, "warehouse": wh_key})

# (c) query_movement: 「{p}{cat}進出狀況 / 所有記錄」(「這個月運動用品進出狀況」/ 「今日所有進出記錄」fail)
TPL_MOV_FIX = [
    "{p}{cat}進出狀況", "{p}{cat}類進出狀況", "{p}{cat}進出記錄",
    "{p}{cat}類進出", "{p}{cat}進貨出貨",
]
for period_key, p_zh_list in PERIOD_ZH.items():
    for p_zh in p_zh_list[:2]:
        for cat_key, cat_zh_list in CATEGORY_ZH.items():
            for cat_zh in cat_zh_list[:2]:
                for tpl in TPL_MOV_FIX:
                    add(tpl.format(p=p_zh, cat=cat_zh), "query_movement",
                        {"period": period_key})

# (d) query_movement: 「{p}所有/全部記錄」(today fail case)
TPL_MOV_ALL = [
    "{p}所有進出記錄", "{p}全部進出記錄", "{p}進出明細",
    "{p}所有記錄", "{p}進出總表", "{p}進出清單",
    "查{p}進出", "看{p}進出",
]
for period_key, p_zh_list in PERIOD_ZH.items():
    for p_zh in p_zh_list:
        for tpl in TPL_MOV_ALL:
            add(tpl.format(p=p_zh), "query_movement", {"period": period_key})

# (e) list_hot_items: 強化 category slot 抽取 (從 raw 看 LLM 常漏 cat)
# 用「{cat}類{rank}」更明確的句型
TPL_HOT_CAT_STRONG = [
    "{p}{cat}類{rank}", "{p}{cat}類{rank}排行",
    "{cat}類{p}{rank}", "{p}{cat}類{rank}的商品",
    "{cat}{p}{rank}排行", "{cat}{p}{rank}",
    "{cat}類{p}{rank}排行 TOP 10",
    "查{p}{cat}類{rank}",
]
for rank_key, rank_zh_list in RANK_ZH.items():
    for cat_key, cat_zh_list in CATEGORY_ZH.items():
        for period_key in ["this_week", "this_month"]:
            for cat_zh in cat_zh_list[:2]:
                for rank_zh in rank_zh_list[:2]:
                    p_zh = random.choice(PERIOD_ZH[period_key])
                    tpl = random.choice(TPL_HOT_CAT_STRONG)
                    add(tpl.format(p=p_zh, cat=cat_zh, rank=rank_zh),
                        "list_hot_items",
                        {"rank_type": rank_key, "period": period_key, "category": cat_key})

# (f) list_hot_items: 「月度/週度」變體 (修「月度最差商品」fail)
TPL_HOT_PERIOD_VARIANT = [
    "月度{rank}商品", "月度{rank}排行", "月度最{rank}的商品",
    "週度{rank}商品", "週度{rank}排行",
    "本月銷量{rank}", "本週銷量{rank}",
]
RANK_SUFFIX = {
    "hot":  ["熱銷", "賣最好", "最熱門", "暢銷"],
    "slow": ["滯銷", "賣最差", "最冷門", "賣不掉"],
}
for rank_key, rank_words in RANK_SUFFIX.items():
    for rank_zh in rank_words:
        for tpl in TPL_HOT_PERIOD_VARIANT:
            # 由模板自己決定週/月
            if "月" in tpl:
                add(tpl.format(rank=rank_zh), "list_hot_items",
                    {"rank_type": rank_key, "period": "this_month"})
            elif "週" in tpl:
                add(tpl.format(rank=rank_zh), "list_hot_items",
                    {"rank_type": rank_key, "period": "this_week"})


# ════════════════════════════════════════════════════════════════════
# 6. query_related_items (~460) — 連帶備貨分析
#    模型只學「{kw} 的連帶 / 買 {kw} 的人還買啥」句型框架、抽 keyword
#    SKU 不進 prompt → 加新商品不用重訓
# ════════════════════════════════════════════════════════════════════

# 連帶分析的 keyword 池:用情境錨點 + 一般商品(讓模型學各種品都能問連帶)
RELATED_KEYWORDS = KEYWORD_SHORT_FORMS + [
    "咖啡機", "帳篷", "尿布", "啤酒", "野炊鍋", "瑜珈墊", "慢跑鞋",
    "洗衣精", "羽絨外套", "濾紙", "防蚊液", "登山水壺", "馬克杯",
    "健身環", "排汗衣", "電解質", "蛋白飲", "毛帽", "熱可可",
]

# (a) 核心連帶句型(「買 X 的人也買什麼」家族)
TPL_REL = [
    "買{kw}的人還會買什麼",
    "買{kw}的人也買了什麼",
    "買{kw}的人通常還買啥",
    "{kw}的連帶商品",
    "{kw}通常跟什麼一起買",
    "{kw}的連帶備貨",
    "{kw}會帶動哪些商品",
    "跟{kw}一起賣的有哪些",
    "{kw}的搭配商品",
    "買{kw}順便會買什麼",
    "{kw}連帶分析",
    "{kw}還會連帶賣出什麼",
    "{kw}的好夥伴商品",
    "和{kw}常一起出貨的",
    "{kw}賣出時要順便補什麼",
    "{kw}熱賣會拉動哪些貨",
    "查{kw}的連帶商品",
    "{kw}通常搭配啥一起買",
    "{kw}帶貨清單",
    "買了{kw}的人還買了",
]

# 每個 keyword 抽 7 個模板(不跑滿 cartesian、避免此 function 過度膨脹)
# ~64 kw × 7 = ~450,跟其他 function 量級平衡
for kw in RELATED_KEYWORDS:
    for tpl in pick(TPL_REL, 7):
        add(tpl.format(kw=kw), "query_related_items", {"keyword": kw})

# (b) category 版(整類的連帶,較少)
TPL_REL_CAT = [
    "{cat}類的連帶商品", "{cat}通常跟什麼一起賣",
    "{cat}類連帶分析",
]
for cat_key, cat_zh_list in CATEGORY_ZH.items():
    for cat_zh in cat_zh_list[:2]:
        for tpl in TPL_REL_CAT:
            add(tpl.format(cat=cat_zh), "query_related_items",
                {"keyword": cat_zh, "category": cat_key})

# (c) 英文變體
EN_REL_TPL = [
    "what's bought with {kw}", "{kw} frequently bought together",
    "related items for {kw}", "what else do {kw} buyers buy",
]
for kw in ["bluetooth earphone", "coffee machine", "diaper", "tent", "yoga mat"]:
    for tpl in EN_REL_TPL:
        add(tpl.format(kw=kw), "query_related_items", {"keyword": kw})


# ════════════════════════════════════════════════════════════════════
# 寫檔 + 統計
# ════════════════════════════════════════════════════════════════════
gen_v2_tools()   # v2 三金剛 + A/B 波樣本（ITEMS 已就緒）
gen_v2_judge()   # C 波 judge_cause_found 試訓樣本

random.shuffle(samples)
with open(OUT, "w", encoding="utf-8") as f:
    for s in samples:
        f.write(json.dumps(s, ensure_ascii=False) + "\n")

print(f"已產出: {OUT}")
print(f"  總筆數: {len(samples)}")
print()
print("--- function 分布 ---")
counter = Counter(s["tool_name"] for s in samples)
for name, count in counter.most_common():
    print(f"  {name:24s} {count:5d}")
print()
print("--- 隨機 10 條樣本 ---")
for s in random.sample(samples, 10):
    print(f"  Q: {s['user_content']}")
    print(f"  → {s['tool_name']}({s['tool_arguments']})")
    print()
