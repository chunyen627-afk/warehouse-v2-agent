# -*- coding: utf-8 -*-
"""
gen_en_dataset.py — 英文訓練語料生成器（英文版補訓用）。
對應中文版 generate_dataset.py，但全英文：15 個 function-call tool ×
英文商品變體 × 英文口語模板。重點補探針測壞的弱點：
  - stock/inventory/how many...left → query_inventory
  - 錯字（earphon, powr bank）、模糊/俗稱（the charger thing）
  - RCA（why is X wrong / who moved X）→ search_log
  - 意圖多樣（movement/hot/compare/alert/config...）
輸出：training_data_en.jsonl（{user_content, tool_name, tool_arguments}）
tool schema、enum 值與中文版完全一致（category/period/metric/warehouse 用英文 key）。
keyword 用英文商品名/變體（模型抽出來要能 substring 對到英文 seed）。
"""
import json, random
from pathlib import Path
from item_names_en import ITEM_EN, CATEGORY_EN, WAREHOUSE_EN

random.seed(42)
OUT = Path(__file__).parent / "training_data_en.jsonl"

# ── 商品：主名 + 變體（給 keyword）──────────────────────────
# 每商品一組講法：主名 + aliases。訓練時 keyword 用「較短的核心講法」，
# 讓模型學會抽短名（對應中文版 KEYWORD_SHORT_FORMS）。
# 先建全體商品英文主名（給 canonical 唯一性檢查用）
_ALL_NAMES = [(_en, _en.lower()) for _sku, _zh, _en, _al in ITEM_EN]

def _best_name(kw):
    """substring 打分找 kw 對到的商品主名（模擬 match_items）。"""
    kwl = kw.lower(); toks = kwl.split()
    best_s, best_n = 0, None
    for name, nl in _ALL_NAMES:
        s = sum(len(t) for t in toks if t in nl)
        if kwl in nl:
            s += 5
        if s > best_s:
            best_s, best_n = s, name
    return best_n

ITEM_KWS = []          # [(display_variants, canonical_kw)] canonical=拿去當 tool arg
for _sku, _zh, _en, _aliases in ITEM_EN:
    # 變體池：主名 + 別名（去重、保序）
    pool = [_en] + list(_aliases)
    seen = set(); variants = []
    for v in pool:
        k = v.lower()
        if k not in seen:
            seen.add(k); variants.append(v)
    # canonical keyword：選「能唯一對到自己商品」的最短別名；都不行則用完整主名
    canonical = None
    for cand in sorted(_aliases, key=len):        # 短的優先
        if _best_name(cand) == _en:
            canonical = cand; break
    if canonical is None:
        canonical = _en
    ITEM_KWS.append((variants, canonical))

CATS = ["electronics", "appliance_kitchen", "food_beverage",
        "daily_goods", "apparel", "sports"]
WHS = ["north", "central", "south"]

_rows = []
def add(user_content, tool_name, tool_args):
    _rows.append({
        "user_content": user_content,
        "tool_name": tool_name,
        "tool_arguments": json.dumps(tool_args, ensure_ascii=False),
    })

def wh_variant(wh):
    return random.choice(WAREHOUSE_EN[wh][1])

def cat_variant(cat):
    return random.choice(CATEGORY_EN[cat])


# ── 錯字製造器（模擬展場亂打，對應中文錯字容錯）──────────────
def make_typo(word):
    """對英文詞做一個常見錯字：漏字母/換相鄰鍵/漏尾。回原詞或錯字版。"""
    w = word
    if len(w) < 4:
        return w
    kind = random.random()
    i = random.randint(1, len(w) - 2)
    if kind < 0.4:                      # 漏一個字母
        return w[:i] + w[i+1:]
    elif kind < 0.7:                    # 疊一個字母
        return w[:i] + w[i] + w[i:]
    elif kind < 0.85:                   # 尾巴 s 掉/加
        return w[:-1] if w.endswith("s") else w + "s"
    else:                              # 相鄰鍵誤觸（簡化：換成鄰近母音）
        return w[:i] + random.choice("aeiou") + w[i+1:]


print(f"[init] {len(ITEM_KWS)} items, seed 42")


# ══════════════════════════════════════════════════════════════
# 1. query_inventory  —— {keyword} 或 {category}(+warehouse)
# ══════════════════════════════════════════════════════════════
# (a) 單品 keyword × 模板
INV_KW_TPL = [
    "{kw} stock", "{kw} inventory", "how many {kw} left",
    "how many {kw} do we have", "how much {kw} is left",
    "check {kw} stock", "{kw} in stock", "do we have {kw}",
    "how many {kw} in stock", "{kw} quantity", "show me {kw} stock",
    "what's the stock of {kw}", "{kw} count", "any {kw} left",
    "how many {kw} are there", "stock level of {kw}",
]
INV_KW_WH_TPL = [
    "{kw} stock in {wh}", "how many {kw} in {wh}", "{wh} {kw} stock",
    "{kw} in {wh}", "check {kw} in the {wh}", "how many {kw} at {wh}",
    "{wh} {kw} inventory", "stock of {kw} in {wh}",
]
for variants, canon in ITEM_KWS:
    # 每商品抽數個變體 × 數個模板（控制總量 ~ 60*17 ≈ 1000）
    for kw in random.sample(variants, min(3, len(variants))):
        for tpl in random.sample(INV_KW_TPL, 6):
            add(tpl.format(kw=kw), "query_inventory", {"keyword": canon})
    # 帶倉別（每商品 2 條）
    for _ in range(2):
        kw = random.choice(variants); wh = random.choice(WHS)
        tpl = random.choice(INV_KW_WH_TPL)
        add(tpl.format(kw=kw, wh=wh_variant(wh)),
            "query_inventory", {"keyword": canon, "warehouse": wh})

# (a2) 更多口語變體（純英文模型：句型多樣性要夠）
INV_KW_TPL2 = [
    "we got any {kw}", "is there {kw} in stock", "how's the {kw} stock looking",
    "need to know {kw} stock", "tell me the {kw} numbers", "{kw} on hand",
    "current {kw} stock", "how many units of {kw}", "{kw} availability",
    "what's left of {kw}", "remaining {kw}", "{kw} still available",
    "look up {kw}", "find {kw} stock", "{kw} — how many",
]
for variants, canon in ITEM_KWS:
    for kw in random.sample(variants, min(2, len(variants))):
        for tpl in random.sample(INV_KW_TPL2, 4):
            add(tpl.format(kw=kw), "query_inventory", {"keyword": canon})

# (b) 錯字變體（探針弱點！earphon / powr bank）——每商品 4 條（純英文要更強）
for variants, canon in ITEM_KWS:
    for _ in range(4):
        kw = random.choice(variants)
        typo = " ".join(make_typo(w) for w in kw.split())
        if typo != kw:
            tpl = random.choice(INV_KW_TPL + INV_KW_TPL2)
            add(tpl.format(kw=typo), "query_inventory", {"keyword": canon})

# (c) 類別查詢 × 模板
CAT_TPL = [
    "{cat} stock", "{cat} inventory", "show me {cat}", "list {cat}",
    "what {cat} do we have", "{cat} items", "all {cat} stock",
    "how much {cat} do we have", "check {cat} inventory",
]
CAT_WH_TPL = [
    "{cat} in {wh}", "{wh} {cat} stock", "{cat} inventory at {wh}",
    "show {cat} in the {wh}",
]
for cat in CATS:
    for catname in CATEGORY_EN[cat]:
        for tpl in CAT_TPL:
            add(tpl.format(cat=catname), "query_inventory", {"category": cat})
    for _ in range(4):
        catname = cat_variant(cat); wh = random.choice(WHS)
        tpl = random.choice(CAT_WH_TPL)
        add(tpl.format(cat=catname, wh=wh_variant(wh)),
            "query_inventory", {"category": cat, "warehouse": wh})

print(f"[1] query_inventory: {sum(1 for r in _rows if r['tool_name']=='query_inventory')}")


# ══════════════════════════════════════════════════════════════
# 2. query_movement —— {period, keyword?, direction?}
#    period: today/this_week/this_month | direction: in/out/both
# ══════════════════════════════════════════════════════════════
PERIODS = {
    "today": ["today", "today's", "for today"],
    "this_week": ["this week", "this week's", "weekly", "over this week"],
    "this_month": ["this month", "this month's", "monthly", "over this month"],
}
# (a) 全店 period × direction
MOVE_TPL = {
    "both": ["{p} movements", "{p} in and out", "stock movements {p}",
             "{p} in/out records", "what moved {p}", "{p} transactions"],
    "in": ["{p} incoming stock", "{p} stock received", "what came in {p}",
           "{p} inbound", "{p} received goods", "goods in {p}"],
    "out": ["{p} outgoing stock", "{p} shipments", "what shipped {p}",
            "{p} outbound", "{p} goods out", "sales out {p}"],
}
for pkey, pvars in PERIODS.items():
    for direction, tpls in MOVE_TPL.items():
        for pv in pvars:
            for tpl in tpls:
                add(tpl.format(p=pv), "query_movement",
                    {"period": pkey, "direction": direction})
# (b) 單品 + period（帶 keyword）
MOVE_KW_TPL = [
    "{kw} movements {p}", "how much {kw} moved {p}", "{p} {kw} in and out",
    "{kw} in/out {p}", "{p} {kw} activity", "{kw} transactions {p}",
    "how many {kw} shipped {p}", "{kw} received {p}",
]
for variants, canon in ITEM_KWS:
    for _ in range(18):
        kw = random.choice(variants)
        pkey = random.choice(list(PERIODS)); pv = random.choice(PERIODS[pkey])
        tpl = random.choice(MOVE_KW_TPL)
        args = {"period": pkey, "keyword": canon}
        if "shipped" in tpl:
            args["direction"] = "out"
        elif "received" in tpl:
            args["direction"] = "in"
        else:
            args["direction"] = "both"
        add(tpl.format(kw=kw, p=pv), "query_movement", args)
# (c) 無 period 的裸 movement 句（預設 today 由 server 補；這裡給 this_week 常見）
BARE_MOVE = [
    ("any movements", "both"), ("show me the in and out", "both"),
    ("what came in", "in"), ("what shipped out", "out"),
    ("recent stock activity", "both"), ("goods received", "in"),
    ("what went out", "out"), ("stock in and out", "both"),
]
for txt, d in BARE_MOVE:
    for pkey in ["today", "this_week"]:
        add(txt, "query_movement", {"period": pkey, "direction": d})
print(f"[2] query_movement: {sum(1 for r in _rows if r['tool_name']=='query_movement')}")


# ══════════════════════════════════════════════════════════════
# 3. search_log (RCA 追根因) —— {keyword}
#    探針弱點！why is X wrong / who moved X / reconciliation
# ══════════════════════════════════════════════════════════════
RCA_TPL = [
    "why is the {kw} count wrong", "who moved the {kw}",
    "{kw} numbers don't add up", "{kw} count looks off",
    "the {kw} stock doesn't match", "check the {kw} discrepancy",
    "why is {kw} short", "{kw} reconciliation", "audit the {kw}",
    "trace the {kw} shortage", "{kw} inventory mismatch",
    "what happened to the {kw}", "the {kw} numbers are strange",
    "investigate the {kw} count", "{kw} purchase doesn't match",
    "why did {kw} go missing", "look into the {kw} shortfall",
]
for variants, canon in ITEM_KWS:
    for kw in random.sample(variants, min(2, len(variants))):
        for tpl in random.sample(RCA_TPL, 7):
            add(tpl.format(kw=kw), "search_log", {"keyword": canon})
print(f"[3] search_log: {sum(1 for r in _rows if r['tool_name']=='search_log')}")


# ══════════════════════════════════════════════════════════════
# 4. list_hot_items —— {rank_type, period, category?}
#    rank_type: hot/slow | period: this_week/this_month
# ══════════════════════════════════════════════════════════════
HOT_PERIODS = {"this_week": ["this week", "weekly", "this week's"],
               "this_month": ["this month", "monthly", "this month's"]}
HOT_TPL = {
    "hot": ["{p} best sellers", "{p} top selling items", "what sold best {p}",
            "{p} hot items", "{p} sales ranking", "top sellers {p}",
            "best selling products {p}", "{p} top 10", "what's selling most {p}"],
    "slow": ["{p} slow movers", "{p} worst selling items", "what sold least {p}",
             "{p} dead stock", "least popular {p}", "slowest sellers {p}",
             "{p} bottom sellers", "what's not selling {p}"],
}
for pkey, pvars in HOT_PERIODS.items():
    for rt, tpls in HOT_TPL.items():
        for pv in pvars:
            for tpl in tpls:
                add(tpl.format(p=pv), "list_hot_items",
                    {"rank_type": rt, "period": pkey})
# 帶類別
HOT_CAT_TPL = {
    "hot": ["{p} best selling {cat}", "top {cat} {p}", "{p} {cat} top sellers"],
    "slow": ["{p} slow moving {cat}", "worst {cat} {p}", "{p} {cat} dead stock"],
}
for cat in CATS:
    for rt, tpls in HOT_CAT_TPL.items():
        for _ in range(10):
            pkey = random.choice(list(HOT_PERIODS)); pv = random.choice(HOT_PERIODS[pkey])
            tpl = random.choice(tpls); catname = cat_variant(cat)
            add(tpl.format(p=pv, cat=catname), "list_hot_items",
                {"rank_type": rt, "period": pkey, "category": cat})
# 裸「best seller / top 10」句（預設 this_week）
for txt, rt in [("best sellers", "hot"), ("top selling items", "hot"),
                ("what's hot", "hot"), ("top 10", "hot"), ("ranking", "hot"),
                ("slow movers", "slow"), ("dead stock", "slow"),
                ("what's not selling", "slow")]:
    for pkey in ["this_week", "this_month"]:
        add(txt, "list_hot_items", {"rank_type": rt, "period": pkey})
print(f"[4] list_hot_items: {sum(1 for r in _rows if r['tool_name']=='list_hot_items')}")


# ══════════════════════════════════════════════════════════════
# 5. query_related_items (搭售/連帶) —— {keyword}
# ══════════════════════════════════════════════════════════════
REL_TPL = [
    "what's bought with {kw}", "what goes with {kw}",
    "{kw} related items", "people who buy {kw} also buy",
    "what else sells with {kw}", "{kw} bundle", "cross-sell for {kw}",
    "frequently bought with {kw}", "recommend items for {kw}",
    "what pairs with {kw}", "customers buying {kw} also get",
]
for variants, canon in ITEM_KWS:
    for kw in random.sample(variants, min(3, len(variants))):
        for tpl in random.sample(REL_TPL, 5):
            add(tpl.format(kw=kw), "query_related_items", {"keyword": canon})
print(f"[5] query_related_items: {sum(1 for r in _rows if r['tool_name']=='query_related_items')}")


# ══════════════════════════════════════════════════════════════
# 6. compare_warehouses —— {warehouse_a, warehouse_b, metric}
#    metric: item_count/stock_value/turnover
# ══════════════════════════════════════════════════════════════
METRIC_WORDS = {
    "stock_value": ["stock value", "inventory value", "value", "worth"],
    "item_count": ["item count", "how many items", "number of items", "quantity"],
    "turnover": ["turnover", "turnover rate", "how fast stock moves"],
}
CMP_TPL = [
    "compare {a} and {b} by {m}", "{a} vs {b} {m}",
    "which has more {m}, {a} or {b}", "{a} or {b}, which {m} is higher",
    "{m} of {a} compared to {b}", "how does {a} {m} compare to {b}",
]
for wa in WHS:
    for wb in WHS:
        if wa == wb:
            continue
        for metric, mws in METRIC_WORDS.items():
            for _ in range(16):
                tpl = random.choice(CMP_TPL)
                add(tpl.format(a=wh_variant(wa), b=wh_variant(wb), m=random.choice(mws)),
                    "compare_warehouses",
                    {"warehouse_a": wa, "warehouse_b": wb, "metric": metric})
print(f"[6] compare_warehouses: {sum(1 for r in _rows if r['tool_name']=='compare_warehouses')}")


# ══════════════════════════════════════════════════════════════
# 7. list_low_stock —— {category?}  缺貨警示
# ══════════════════════════════════════════════════════════════
LOW_TPL = [
    "what's low on stock", "what's running low", "low stock alert",
    "what needs restocking", "which items are almost out",
    "show me low stock", "what's about to run out", "shortage list",
    "what do we need to reorder", "items below safety stock",
    "what's nearly out of stock", "restock list", "low inventory items",
    "what's running out", "which products are short",
]
for tpl in LOW_TPL:
    for _ in range(4):                # 加權（全店缺貨是高頻查詢）
        add(tpl, "list_low_stock", {})
LOW_CAT_TPL = ["low stock {cat}", "which {cat} are running low",
               "{cat} that need restocking", "low {cat} items", "short on {cat}",
               "{cat} almost out", "restock which {cat}"]
for cat in CATS:
    for tpl in LOW_CAT_TPL:
        for _ in range(3):
            add(tpl.format(cat=cat_variant(cat)), "list_low_stock", {"category": cat})
print(f"[7] list_low_stock: {sum(1 for r in _rows if r['tool_name']=='list_low_stock')}")


# ══════════════════════════════════════════════════════════════
# 8. manage_config —— {action, key, value?}  action: read/set
# ══════════════════════════════════════════════════════════════
# 常見 config：safety stock（安全庫存）、restock target（補貨目標）
CFG_KEYS = ["safety stock", "restock target", "reorder point", "safety level"]
for variants, canon in ITEM_KWS[:30]:      # 半數商品即可
    kw = random.choice(variants)
    key = random.choice(CFG_KEYS)
    val = str(random.choice([20, 30, 50, 80, 100]))
    # set
    for tpl in [f"set {kw} {key} to {val}", f"change {kw} {key} to {val}",
                f"update {kw} {key} to {val}"]:
        add(tpl, "manage_config", {"action": "set", "key": f"{canon} {key}", "value": val})
    # read
    for tpl in [f"what's the {kw} {key}", f"check {kw} {key}", f"{kw} {key}"]:
        add(tpl, "manage_config", {"action": "read", "key": f"{canon} {key}"})
print(f"[8] manage_config: {sum(1 for r in _rows if r['tool_name']=='manage_config')}")


# ══════════════════════════════════════════════════════════════
# 9. set_alert —— {keyword, threshold}
# ══════════════════════════════════════════════════════════════
ALERT_TPL = [
    "alert me when {kw} drops below {n}", "notify me if {kw} goes under {n}",
    "set an alert for {kw} below {n}", "warn me when {kw} is less than {n}",
    "tell me if {kw} falls below {n}", "remind me when {kw} under {n}",
]
for variants, canon in ITEM_KWS:
    kw = random.choice(variants); n = random.choice([20, 30, 50, 80, 100])
    tpl = random.choice(ALERT_TPL)
    add(tpl.format(kw=kw, n=n), "set_alert", {"keyword": canon, "threshold": n})
print(f"[9] set_alert: {sum(1 for r in _rows if r['tool_name']=='set_alert')}")


# ══════════════════════════════════════════════════════════════
# 10-15. 小量 tool
# ══════════════════════════════════════════════════════════════
# run_script
for t in ["run month-end stock audit", "do a stock count", "run stock audit",
          "perform inventory count", "monthly stocktake"]:
    add(t, "run_script", {"script_name": "盤點"})
for t in ["export movements", "export transactions", "export the movement log"]:
    add(t, "run_script", {"script_name": "匯出"})
for t in ["regenerate seed data", "regen seed", "rebuild the dataset"]:
    add(t, "run_script", {"script_name": "重產"})

# generate_report
for t in ["generate a full warehouse report", "export inventory report",
          "give me a full report", "create a warehouse report", "full stock report",
          "produce an overview report"]:
    add(t, "generate_report", {"report_type": "full"})
for t, rt in [("low stock report", "low_stock"), ("shortage report", "low_stock"),
              ("expiry report", "expiring"), ("expiring items report", "expiring"),
              ("reconciliation report", "rca"), ("discrepancy report", "rca")]:
    add(t, "generate_report", {"report_type": rt})

# list_files
for t, area in [("list the record files", "master"), ("what data can I query", ""),
                ("show me the folders", ""), ("list files", ""),
                ("what's in the orders folder", "orders"),
                ("show report files", "reports"),
                ("list transaction files", "transactions"),
                ("audit files", "audit"), ("script files", "scripts")]:
    add(t, "list_files", {"area": area} if area else {})

# generate_po
for t, src in [("create a PO for the shortfall", "shortfall"),
               ("purchase order for short items", "shortfall"),
               ("reorder the low stock items", "low_stock"),
               ("generate a purchase order for low stock", "low_stock"),
               ("raise a PO for what's running low", "low_stock"),
               ("order the short-received items", "shortfall")]:
    add(t, "generate_po", {"source": src})

# compare_periods
for t in ["this week vs last week shipments", "compare this month to last month out",
          "how do this week's shipments compare to last week",
          "week over week outbound", "this month vs last month sales out"]:
    add(t, "compare_periods", {"metric": "out"})

# judge_cause_found（RCA 判定，內部工具，少量）
for variants, canon in ITEM_KWS[:20]:
    kw = random.choice(variants)
    add(f"investigating {kw} shortage [result] no abnormal record found for {kw}",
        "judge_cause_found", {"found": "no"})
    add(f"checking {kw} discrepancy [result] found the abnormal record for {kw}",
        "judge_cause_found", {"found": "yes"})

print(f"[10-15] misc tools done")


# ══════════════════════════════════════════════════════════════
# 16. 模糊/俗稱描述句（探針弱點：the thing that charges my phone）
#     用「功能描述 → 商品」的口語，教模型從描述抽到正確商品 keyword
# ══════════════════════════════════════════════════════════════
# (canonical 已保證對得到；這裡用「描述句」當 user_content，arg 用 canonical)
DESC2ITEM = [
    ("the thing that charges my phone", "power bank"),
    ("something to charge my phone", "power bank"),
    ("the portable charger thingy", "power bank"),
    ("those wireless ear things", "bluetooth earphones"),
    ("the things you put in your ears", "bluetooth earphones"),
    ("the thing you talk to for music", "bluetooth speaker"),
    ("the mat for doing yoga", "yoga mat"),
    ("the mat you exercise on", "yoga mat"),
    ("the thing that cleans your teeth", "electric toothbrush"),
    ("the brush for teeth", "electric toothbrush"),
    ("the machine that makes coffee", "coffee machine"),
    ("the thing that makes coffee", "coffee machine"),
    ("the thing you type on", "keyboard"),
    ("the clicky thing for the computer", "mouse"),
    ("the thing you point and click with", "mouse"),
    ("the fizzy water", "sparkling water"),
    ("the bubbly water", "sparkling water"),
    ("the thing you sleep in when camping", "camping tent"),
    ("the light for camping", "camping lantern"),
    ("the chair you fold up for camping", "camping chair"),
    ("the shoes for running", "running shoes"),
    ("the bag for a laptop", "laptop bag"),
    ("the case for a phone", "phone case"),
    ("the fan for your desk", "desk fan"),
    ("the small blender", "blender"),
    ("the pan that food doesn't stick to", "non-stick pan"),
    ("the hat to block the sun", "sun hat"),
    ("the warm hat for winter", "beanie"),
    ("the wipes for babies", "baby wipes"),
    ("the spray for mosquitoes", "mosquito repellent"),
]
QUERY_WRAP = ["how many {d} do we have", "{d} stock", "do we have any {d}",
              "how much {d} is left", "check the {d}"]
for desc, canon in DESC2ITEM:
    # 這些 canonical 用 _best_name 驗證能對到；直接用 desc 當 keyword 抽取目標=canonical
    add(desc, "query_inventory", {"keyword": canon})
    for tpl in random.sample(QUERY_WRAP, 2):
        add(tpl.format(d=desc), "query_inventory", {"keyword": canon})
print(f"[16] fuzzy desc: added")


# ══════════════════════════════════════════════════════════════
# 輸出
# ══════════════════════════════════════════════════════════════
random.shuffle(_rows)
with open(OUT, "w", encoding="utf-8") as f:
    for r in _rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

from collections import Counter
c = Counter(r["tool_name"] for r in _rows)
print(f"\n=== 英文語料生成完成：{len(_rows)} 筆 → {OUT.name} ===")
for k, v in c.most_common():
    print(f"  {k}: {v}")
