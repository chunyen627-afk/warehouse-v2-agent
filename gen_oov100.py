"""
gen_oov100.py — 隨機產生 100 句 OOV 測試題並透過 eval_http 評測
用法: python gen_oov100.py
"""
import csv, json, random, time, urllib.request, sys
from pathlib import Path

random.seed(42)
HERE = Path(__file__).resolve().parent

# ── 讀 SKU 名稱 ──────────────────────────────────────────────
rows = list(csv.DictReader(open(HERE / "test/warehouse_data/master/items.csv", encoding="utf-8-sig")))
SKUS = [r["name"].strip() for r in rows if r.get("name", "").strip()]

WH = ["北倉", "中倉", "南倉", "北區倉", "中區倉", "南區倉", "北", "中", "南"]

# ── 句型模板 (func, template_fn) ─────────────────────────────
def t_inv_simple(s):    return random.choice([f"{s}還有多少", f"{s}現在幾件", f"查一下{s}", f"{s}庫存量", f"幫我查{s}"])
def t_inv_wh(s):        return random.choice([f"{random.choice(WH)}的{s}庫存", f"{s}在{random.choice(WH)}有幾個", f"查{random.choice(WH)}{s}"])
def t_move(s):          return random.choice([f"{s}最近進出紀錄", f"{s}這個月動了多少", f"查{s}的異動", f"{s}進出狀況"])
def t_low():            return random.choice(["哪些庫存快見底了", "庫存不足清單", "補貨清單", "哪些要補貨了", "低庫存警示", "缺貨商品", "快沒貨的有哪些"])
def t_hot():            return random.choice(["最近賣最好的是啥", "熱銷榜", "銷量前幾名", "這個月賣最多的", "哪個商品最夯", "暢銷品排名"])
def t_compare():        return random.choice(["三倉庫存比一比", "各倉差異", "北中南倉比較", f"比較{random.choice(WH[:3])}和{random.choice(WH[3:6])}庫存"])
def t_expire(s):        return random.choice([f"{s}快到期了嗎", f"查{s}保存期限", f"{s}什麼時候過期"])
def t_expire_gen():     return random.choice(["哪些食品快到期", "快過期的商品", "到期清單", "保存期限快到的有哪些"])
def t_related(s):       return random.choice([f"買{s}還可以搭什麼", f"{s}推薦搭配", f"跟{s}一起買的商品"])
def t_rca(s):           return random.choice([f"{s}帳對不上", f"{s}庫存少了誰動的", f"查{s}異常紀錄"])
def t_rca_gen():        return random.choice(["庫存帳對不上", "誰改了庫存", "查庫存異常"])

TEMPLATES = [
    ("query_inventory",    t_inv_simple,  True,  {}),
    ("query_inventory",    t_inv_wh,      True,  {}),
    ("query_movement",     t_move,        True,  {}),
    ("list_low_stock",     t_low,         False, {}),
    ("list_hot_items",     t_hot,         False, {}),
    ("compare_warehouses", t_compare,     False, {}),
    ("list_expiring_items",t_expire,      True,  {}),
    ("list_expiring_items",t_expire_gen,  False, {}),
    ("query_related_items",t_related,     True,  {}),
    ("search_log",         t_rca,         True,  {}),
    ("search_log",         t_rca_gen,     False, {}),
]

# ── 產生 100 題（依比例分配） ─────────────────────────────────
WEIGHTS = [15, 10, 10, 8, 8, 7, 5, 5, 7, 10, 5]  # 共 90，補 10 句 inventory
assert len(WEIGHTS) == len(TEMPLATES)

cases = []
for (func, tmpl_fn, need_sku, _), n in zip(TEMPLATES, WEIGHTS):
    for _ in range(n):
        sku = random.choice(SKUS) if need_sku else None
        text = tmpl_fn(sku) if sku else tmpl_fn()
        exp_kw = {"keyword": sku[:4]} if sku else {}
        cases.append((text, func, exp_kw))

random.shuffle(cases)
cases = cases[:100]

# ── 評測 ──────────────────────────────────────────────────────
API = "http://localhost:8000/api/query"
VIEW_FUNC = {
    "inventory": "query_inventory", "inventory_single": "query_inventory",
    "movement": "query_movement", "low_stock": "list_low_stock",
    "compare": "compare_warehouses", "hot_items": "list_hot_items",
    "related_items": "query_related_items", "expiring": "list_expiring_items",
    "agent_rca": "search_log", "config_read": "manage_config",
    "script_confirm": "run_script", "report": "generate_report",
}

def get_func(r):
    fn = r.get("_function", "")
    return fn or VIEW_FUNC.get(r.get("view", ""), r.get("view", ""))

def kw_match(exp_kw, r):
    if not exp_kw:
        return True
    exp = exp_kw.get("keyword", "")
    if not exp:
        return True
    act = str(r.get("data", {}).get("keyword", r.get("data", {}).get("target", "")))
    if exp in act or act in exp:
        return True
    if len(exp) >= 2 and len(act) >= 2 and exp[:2] == act[:2]:
        return True
    return False

total = passed = 0
failed = []

print(f"評測 {len(cases)} 題（OOV 新造句）...\n")
t0 = time.time()

for text, exp_func, exp_kw in cases:
    total += 1
    try:
        req = urllib.request.Request(API,
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
    except Exception as e:
        failed.append(f"  HTTP ERR [{text}]: {e}")
        continue

    act = get_func(r)
    ok_func = (act == exp_func)
    ok_kw = kw_match(exp_kw, r)

    if ok_func and ok_kw:
        passed += 1
    else:
        reason = ""
        if not ok_func: reason += f"func {act!r}≠{exp_func!r} "
        if not ok_kw:   reason += f"kw {r.get('data',{}).get('keyword','?')!r}≠{exp_kw.get('keyword','')!r}"
        failed.append(f"  FAIL [{text}] {reason.strip()}")

elapsed = time.time() - t0
print(f"結果：{passed}/{total} = {passed/total*100:.1f}%  ({elapsed:.1f}s)\n")
if failed:
    print(f"失敗 {len(failed)} 題：")
    for f in failed:
        print(f)
