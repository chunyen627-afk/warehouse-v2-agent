# -*- coding: utf-8 -*-
"""r30 EN 探針：目標水位寫入（restock to N / set stock to N）＋改價變體
（raise/lower/price-of/price up to）＋「set X to N」歧義反問。

鄰居迴歸組：
- create_movement 相對量不變（restock 30 X at south＝+30，守衛1062）
- config 句不被搶：safety stock to N（守衛730-760）/ restock target
- 商品名含 refill（mosquito repellent refill，守衛264-270）不誤觸
- sell down to 10 left → 誠實閘（曾誤配 Down Jacket）
- change_item_price 既有形不變、空名反問

regex 區為 en/server.py r30 攔截的鏡像（server 端才是權威；此處驗覆蓋面）。
"""
import re
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
    print(("OK  " if cond else "NG  ") + label + ("  | " + str(detail)[:100] if detail else ""))


_s = W.state()
ITEM, CUR = None, 0
for _it in _s.items:
    _q = _s.stock.get("north", {}).get(_it["sku_id"], 0)
    if _q >= 10:
        ITEM, CUR = _it, _q
        break
assert ITEM, "no item with north stock >= 10"
NAME = ITEM["name"]
print(f"test item: {NAME} north={CUR}")

# ═══ A. set_stock_absolute 函式行為 ═══
r = T.set_stock_absolute(keyword=NAME, warehouse="north", qty=str(CUR + 25), mode="restock")
d = r.get("data", {})
ck("A1 restock to cur+25 -> inbound card", r.get("view") == "movement_confirm"
   and d.get("direction") == "in" and d.get("qty") == 25 and d.get("after_qty") == CUR + 25,
   f"view={r.get('view')} qty={d.get('qty')}")

r = T.set_stock_absolute(keyword=NAME, warehouse="north", qty=str(CUR), mode="restock")
ck("A2 restock to cur -> already at target", r.get("view") == "guide"
   and "no restock needed" in r.get("summary", ""), r.get("view"))

r = T.set_stock_absolute(keyword=NAME, warehouse="north", qty=str(max(1, CUR - 5)), mode="set")
d = r.get("data", {})
ck("A3 set to cur-5 -> outbound card", r.get("view") == "movement_confirm"
   and d.get("direction") == "out" and d.get("after_qty") == max(1, CUR - 5),
   f"view={r.get('view')}")

r = T.set_stock_absolute(keyword=NAME, warehouse="north", qty=str(CUR), mode="set")
ck("A4 set to cur -> nothing to adjust", r.get("view") == "guide"
   and "nothing to adjust" in r.get("summary", ""), r.get("view"))

r = T.set_stock_absolute(keyword=NAME, warehouse="north", qty="0", mode="set")
ck("A5 target 0 -> refused", r.get("view") == "clarify" and "wipe" in r.get("summary", ""),
   r.get("summary", "")[:40])

r = T.set_stock_absolute(keyword=NAME, warehouse="north", qty="99999", mode="restock")
ck("A6 target 99999 -> unusual", r.get("view") == "clarify"
   and "unusual" in r.get("summary", ""), r.get("view"))

r = T.set_stock_absolute(keyword="ghost item xyzzy", warehouse="north", qty="50", mode="set")
ck("A7 unknown item -> no item found", r.get("view") == "clarify"
   and "No item found" in r.get("summary", ""), r.get("summary", "")[:40])

r = T.set_stock_absolute(keyword=NAME, warehouse="", qty="80", mode="restock")
fl = (r.get("data") or {}).get("flow") or {}
ck("A8 no warehouse -> 3-option clarify + flow mode", r.get("view") == "clarify"
   and fl.get("tool") == "set_stock_absolute" and fl.get("await") == "warehouse"
   and fl.get("mode") == "restock" and len((r.get("data") or {}).get("options", [])) == 3,
   f"flow={fl}")

r = T.set_stock_absolute(keyword=NAME, warehouse="north", qty=str(CUR + 7),
                         direction="", is_return=False, mode="restock")
ck("A9 flow re-call signature", r.get("view") == "movement_confirm"
   and r.get("data", {}).get("qty") == 7, r.get("view"))

# ═══ B. 鄰居（工具函式層）═══
r = T.create_movement(keyword=NAME, warehouse="north", direction="in", qty="30")
ck("B1 relative inbound 30 unchanged", r.get("view") == "movement_confirm"
   and r.get("data", {}).get("qty") == 30 and r.get("data", {}).get("after_qty") == CUR + 30,
   r.get("data", {}).get("qty"))

r = T.change_item_price(keyword=NAME, price=299)
ck("B2 price change 299 unchanged", r.get("view") == "price_confirm", r.get("view"))

r = T.change_item_price(keyword="", price=299)
ck("B3 price empty keyword -> which item", r.get("view") == "clarify", r.get("view"))

r = T.manage_config(action="set", key="safety stock", value="60", item=NAME)
ck("B4 config safety stock 60 unchanged", r.get("view") == "config_confirm", r.get("view"))

# ═══ C. dispatch 路由鏡像（en/server.py r30）═══
_EX30 = re.compile(r"\b(?:safety|minimum|threshold|buffer|target|lead|days?|"
                   r"all|every|each|alert|auto|automatic|shortage|number|numbers)\b", re.I)
_AMB_EX = re.compile(r"\b(?:price|stock|inventory|safety|minimum|threshold|target|"
                     r"lead|days?|alert|schedule|quantity|count|warehouse|all|every)\b", re.I)


def route(text):
    t = text.strip().strip("!！?？。.~～ ")
    if not _EX30.search(text):
        wh_m = re.search(r"\b(north|central|south)\b", t, re.I)
        wh = wh_m.group(1).lower() if wh_m else ""
        tc = re.sub(r"\b(?:in|at|to|for)?\s*(?:the\s+)?(?:north|central|south)\s*(?:warehouse)?\b",
                    " ", t, flags=re.I)
        tc = re.sub(r"\s+", " ", tc).strip()
        rs = re.search(r"^(?:please\s+|can\s+you\s+|could\s+you\s+|help\s+me\s+)?"
                       r"(?:restock|top\s+up|refill|bring)\s+(.+?)\s+"
                       r"(?:back\s+)?(?:up\s+)?to\s+(\d+)\s*(?:units?|pcs|pieces)?\s*$", tc, re.I)
        ss = None if rs else re.search(
            r"^(?:please\s+|can\s+you\s+)?(?:set|change|update|adjust|make)\s+"
            r"(?:the\s+)?(.+?)(?:'s)?\s+(?:stock|inventory|count|quantity)\s+"
            r"(?:to|at)\s+(\d+)\s*(?:units?|pcs)?\s*$", tc, re.I)
        m = rs or ss
        if m and int(m.group(2)) > 0:
            kw = re.sub(r"^(?:the|my|our)\s+", "", m.group(1).strip(" ,."), flags=re.I)
            return ("restock" if rs else "stockset"), (kw, int(m.group(2)), wh)
    if (re.search(r"\b(?:sell|ship|draw)\s+(?:\w+\s+){0,3}down\s+to\s+\d+", text, re.I)
            or re.search(r"\bdown\s+to\s+\d+\s+(?:left|remaining)\b", text, re.I)
            or re.search(r"\buntil\s+(?:only\s+)?\d+\s+(?:are\s+)?left\b", text, re.I)):
        return "drain_gate", None
    pc = (re.search(r"^(?:change|set|update)\s+(.{0,30}?)(?:'s)?\s+price\s+(?:to\s+)?(\d+)\s*$", t, re.I)
          or re.search(r"^(.{0,30}?)(?:'s)?\s+price\s+(?:up\s+|down\s+)?to\s+(\d+)\s*$", t, re.I)
          or re.search(r"^(?:raise|increase|bump|lower|drop|reduce|cut|decrease)\s+"
                       r"(?:the\s+)?price\s+(?:of|for)\s+(.+?)\s+to\s+(\d+)\s*$", t, re.I))
    if pc and not re.search(r"safety|stock|all\s+items|every|warehouse|lead\s*time", text, re.I):
        return "price", (pc.group(1).strip(" ,.-"), int(pc.group(2)))
    amb = re.search(r"^(?:set|change|update)\s+(?:the\s+)?(.+?)\s+to\s+(\d+)\s*$", t, re.I)
    if amb and not _AMB_EX.search(text):
        kw = amb.group(1).strip(" ,.")
        ms = W.match_items(kw) if kw else []
        if ms and ms[0].get("score", 0) >= 3:
            return "ambig", ms[0]["item"]["name"]
    return "fallthrough", None


CASES = [
    # 新功能
    ("restock wireless mouse to 100",            "restock",  ("wireless mouse", 100, "")),
    ("restock north wireless mouse to 100",      "restock",  ("wireless mouse", 100, "north")),
    ("restock wireless mouse to 100 at south",   "restock",  ("wireless mouse", 100, "south")),
    ("top up power bank to 80",                  "restock",  None),
    ("please refill juice blender to 60",        "restock",  None),
    ("bring wireless mouse back up to 120",      "restock",  ("wireless mouse", 120, "")),
    ("set wireless mouse stock to 100",          "stockset", ("wireless mouse", 100, "")),
    ("change juice blender inventory to 60",     "stockset", None),
    ("adjust the usb fan count to 45",           "stockset", ("usb fan", 45, "")),
    # 守衛保護句：不可被搶
    ("set mouse safety stock to 50",             "fallthrough", None),   # 守衛742 cfg
    ("set restock target to 7",                  "fallthrough", None),   # config 鍵
    ("restock 30 wireless mouse at south",       "fallthrough", None),   # 守衛1062 相對量mv
    ("mosquito repellent refill on hand",        "fallthrough", None),   # 守衛264 商品名refill
    ("what needs restocking",                    "fallthrough", None),   # 守衛627 low
    ("restock all items to 100",                 "fallthrough", None),   # all 排除
    ("set stock to 0",                           "fallthrough", None),   # 歸零放行原路徑
    ("sell down to 10 left",                     "drain_gate", None),    # Down Jacket 誤配防
    ("ship it down to 5 left",                   "drain_gate", None),
    # 改價（新＋既有迴歸）
    ("raise wireless mouse price to 300",        "price", 300),
    ("lower the price of wireless mouse to 100", "price", 100),
    ("wireless mouse price up to 300",           "price", 300),
    ("change wireless mouse price to 590",       "price", 590),
    ("wireless mouse price to 450",              "price", 450),
    ("cut the price of usb fan to 99",           "price", 99),
]
for text, want, extra in CASES:
    got, info = route(text)
    ok = got == want
    if ok and want in ("restock", "stockset") and extra:
        ok = (extra[0].lower() in info[0].lower() and info[1] == extra[1]
              and info[2] == extra[2])
    if ok and want == "price" and isinstance(extra, int):
        ok = info[1] == extra
    ck(f"C {text} -> {want}", ok, f"got={got} info={info}")

got, info = route("set wireless mouse to 200")
ck("C set wireless mouse to 200 -> ambig", got == "ambig", f"got={got} {info}")
got, info = route("set alert to 200")
ck("C set alert to 200 -> not intercepted", got == "fallthrough", f"got={got}")

print()
print("bad", BAD)
sys.exit(1 if BAD else 0)
