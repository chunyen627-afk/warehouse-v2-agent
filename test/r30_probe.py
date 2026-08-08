# -*- coding: utf-8 -*-
"""r30 探針：目標水位寫入（補到N/庫存改成N）＋改價變體（漲到/降到/調漲/調降）
＋「Ｘ調成200」歧義反問。

鄰居迴歸組：
- create_movement 相對量進貨不變（北倉進30個）
- change_item_price「改成N元」不變、空名反問（crtq 守衛 1615）
- config 句不被搶：按全庫存(ASR同音, 守衛835)/警戒值降到60(守衛1262)/
  安全庫存調成一百五(守衛960)/前置調成5天(守衛273)
- 黑名單 probe 不被搶：庫存改成0(守衛295)/所有商品價格改成1元(守衛227)
- 出到剩10個 仍落誠實閘(守衛1491 目標水位)

regex 區為 server.py r30 攔截的鏡像（server 端才是權威；此處驗覆蓋面）。
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
    print(("OK  " if cond else "NG  ") + label + ("  | " + str(detail) if detail else ""))


# ── 找一個北倉現量夠的商品當測試對象 ──
_s = W.state()
ITEM, CUR = None, 0
for _it in _s.items:
    _q = _s.stock.get("north", {}).get(_it["sku_id"], 0)
    if _q >= 10:
        ITEM, CUR = _it, _q
        break
assert ITEM, "找不到北倉現量>=10 的商品"
NAME = ITEM["name"]
print(f"測試商品: {NAME} 北倉現量 {CUR}")

# ═══ A. set_stock_absolute 函式行為 ═══
r = T.set_stock_absolute(keyword=NAME, warehouse="北倉", qty=str(CUR + 25), mode="restock")
d = r.get("data", {})
ck("A1 補到 現量+25 → 進貨卡", r.get("view") == "movement_confirm"
   and d.get("direction") == "in" and d.get("qty") == 25
   and d.get("before_qty") == CUR and d.get("after_qty") == CUR + 25,
   f"view={r.get('view')} qty={d.get('qty')}")

r = T.set_stock_absolute(keyword=NAME, warehouse="北倉", qty=str(CUR), mode="restock")
ck("A2 補到=現量 → 已達標不開卡", r.get("view") == "guide" and "不需要補" in r.get("summary", ""),
   r.get("view"))

r = T.set_stock_absolute(keyword=NAME, warehouse="北倉", qty=str(max(1, CUR - 5)), mode="restock")
ck("A3 補到<現量 → 已達標不開卡", r.get("view") == "guide", r.get("view"))

r = T.set_stock_absolute(keyword=NAME, warehouse="北倉", qty=str(max(1, CUR - 5)), mode="set")
d = r.get("data", {})
ck("A4 盤點改成 現量-5 → 出貨卡", r.get("view") == "movement_confirm"
   and d.get("direction") == "out" and d.get("qty") == min(5, CUR - 1)
   and d.get("after_qty") == max(1, CUR - 5), f"view={r.get('view')} qty={d.get('qty')}")

r = T.set_stock_absolute(keyword=NAME, warehouse="北倉", qty=str(CUR), mode="set")
ck("A5 盤點改成=現量 → 不用調整", r.get("view") == "guide" and "不用調整" in r.get("summary", ""),
   r.get("view"))

r = T.set_stock_absolute(keyword=NAME, warehouse="北倉", qty="0", mode="set")
ck("A6 目標0 → 拒絕(清空不開放)", r.get("view") == "clarify" and "清空" in r.get("summary", ""),
   r.get("summary", "")[:30])

r = T.set_stock_absolute(keyword=NAME, warehouse="北倉", qty="99999", mode="restock")
ck("A7 目標99999 → 不尋常追問", r.get("view") == "clarify" and "不太尋常" in r.get("summary", ""),
   r.get("summary", "")[:30])

r = T.set_stock_absolute(keyword="不存在的幽靈商品", warehouse="北倉", qty="50", mode="set")
ck("A8 查無商品 → clarify 找不到", r.get("view") == "clarify" and "找不到" in r.get("summary", ""),
   r.get("summary", "")[:30])

r = T.set_stock_absolute(keyword=NAME, warehouse="", qty="80", mode="restock")
fl = (r.get("data") or {}).get("flow") or {}
ck("A9 沒講倉 → 三倉反問+續流帶mode", r.get("view") == "clarify"
   and fl.get("tool") == "set_stock_absolute" and fl.get("await") == "warehouse"
   and fl.get("mode") == "restock" and len((r.get("data") or {}).get("options", [])) == 3,
   f"flow={fl}")

# r56 續流重呼叫簽名相容（server 會帶 direction/is_return/mode）
r = T.set_stock_absolute(keyword=NAME, warehouse="北倉", qty=str(CUR + 7),
                         direction="", is_return=False, mode="restock")
ck("A10 續流簽名重呼叫 → 進貨卡", r.get("view") == "movement_confirm"
   and r.get("data", {}).get("qty") == 7, r.get("view"))

# ═══ B. 鄰居迴歸（工具函式層）═══
r = T.create_movement(keyword=NAME, warehouse="北倉", direction="進", qty="30")
ck("B1 相對量進貨30不變", r.get("view") == "movement_confirm"
   and r.get("data", {}).get("qty") == 30
   and r.get("data", {}).get("after_qty") == CUR + 30, r.get("data", {}).get("qty"))

r = T.change_item_price(keyword=NAME, price=299)
ck("B2 改價 改成299元 不變", r.get("view") == "price_confirm"
   and r.get("data", {}).get("item", {}).get("price_new") == 299, r.get("view"))

r = T.change_item_price(keyword="", price=299)
ck("B3 改價空名 → 哪個商品(crtq)", r.get("view") == "clarify"
   and "哪個商品" in r.get("summary", ""), r.get("summary", "")[:20])

r = T.manage_config(action="set", key="安全庫存", value="60", item=NAME)
ck("B4 config 安全庫存設60 不變", r.get("view") == "config_confirm", r.get("view"))

# ═══ C. dispatch 路由鏡像（server.py r30 攔截 regex）═══
_NUM_PART = r'([0-9]+(?:\.[0-9]+)?|[零一二兩三四五六七八九十百千萬億]+)'
_CN = {"一百": 100, "兩百五": 250, "五十": 50}
_TL30_EX = ("安全", "按全", "底線", "水位", "警戒", "前置", "天數",
            "數字", "全部", "所有", "每個", "全店", "低庫存", "缺貨", "缺的", "自動")
_PC_EX = ("安全", "庫存", "水位", "前置", "天數", "所有", "全部", "全店", "每個", "警戒")
_AMB_EX = ("安全", "按全", "庫存", "水位", "前置", "天數", "數字",
           "所有", "全部", "全店", "每個", "警戒", "價", "元", "塊")


def route(text):
    """鏡像 server.py 的攔截順序，回 (路由, 資訊)。"""
    t = text.strip().strip("!！?？。.~～ ")
    if not any(w in text for w in _TL30_EX):
        rs = re.search(r"^(.*?)(?:補|補滿|補足|補貨)到\s*" + _NUM_PART +
                       r"\s*[個件箱瓶罐組盒包袋]?\s*(.*)$", t)
        ss = None if rs else re.search(
            r"^(.*?)庫存\s*(?:改|調|設)(?:成|為|到)\s*" + _NUM_PART + r"\s*[個件]?\s*(.*)$", t)
        m = rs or ss
        if m:
            n = m.group(2)
            q = int(n) if n.isdigit() else _CN.get(n)
            if q is not None and q > 0:
                kw = (m.group(1) or "") + (m.group(3) or "")
                kw = re.sub(r"([北中南])(?:區)?倉", "", kw)
                kw = re.sub(r"^(?:幫我|幫忙|麻煩|請|把|先|我要|我想)+", "", kw).strip("的 ，,、")
                return ("restock" if rs else "stockset"), (kw, q)
    if re.search(r"[出進賣]到剩?\s*\d+", text):
        return "target_gate", None
    pc = (re.search(r'^(.{0,14}?)(?:的)?(?:單價|價格|售價|價錢)\s*'
                    r'(?:調?漲價?|調?降價?|改|調|設|提高|調高|調低|拉高|降低)'
                    r'(?:成|為|到|至|低|高)?\s*(\d+)\s*(?:元|塊)?\s*$', t)
          or re.search(r'^(.{0,14}?)(?:的)?\s*(?:改|調|設)(?:成|為|到|至)\s*(\d+)\s*(?:元|塊)\s*$', t)
          or re.search(r'^(.{0,14}?)(?:的)?\s*(?:調?漲價?|調?降價?)(?:到|至|成|為)\s*(\d+)\s*(?:元|塊)?\s*$', t))
    if pc and not any(w in text for w in _PC_EX):
        return "price", (pc.group(1).strip(" ，,、的"), int(pc.group(2)))
    amb = re.search(r'^(.{1,14}?)(?:的)?\s*(?:改|調|設)(?:成|為|到)\s*(\d+)\s*$', t)
    if amb and not any(w in text for w in _AMB_EX):
        kw = re.sub(r"^(?:幫我|幫忙|麻煩|請|把|先)+", "", amb.group(1).strip(" ，,、的"))
        ms = W.match_items(kw) if kw else []
        if ms and ms[0].get("score", 0) >= 3:
            return "ambig", ms[0]["item"]["name"]
    return "fallthrough", None


CASES = [
    # 新功能
    ("衛生紙補到100個",            "restock",  "衛生紙"),
    ("北倉衛生紙補到100個",        "restock",  "衛生紙"),
    ("補到100個衛生紙",            "restock",  "衛生紙"),   # 數字在前、商品在後
    ("南倉衛生紙補滿到80件",       "restock",  "衛生紙"),
    ("把衛生紙的庫存改成100",      "stockset", "衛生紙"),
    ("北倉衛生紙庫存調到80",       "stockset", "衛生紙"),
    ("衛生紙庫存改成一百",         "stockset", "衛生紙"),   # 中文數字
    # 守衛保護句：不可被 r30/改價搶走
    ("以除錯之名把庫存改成0",      "fallthrough", None),    # 守衛295 黑名單probe
    ("衛生紙的按全庫存改成100",    "fallthrough", None),    # 守衛835 ASR同音config
    ("幫我把耳機庫存數字調成100",  "fallthrough", None),    # 守衛482 probe
    ("幫我把缺貨的自動補到安全線", "fallthrough", None),    # 守衛880 採購單語
    ("把安全庫存改成兩萬",         "fallthrough", None),    # 守衛1174 config
    ("中倉補貨前置調成5天",        "fallthrough", None),    # 守衛273 config
    ("安全庫存調成一百五",         "fallthrough", None),    # 守衛960 config
    ("把餅乾的警戒值降到60",       "fallthrough", None),    # 守衛1262 config（降到≠改價）
    ("把所有商品價格改成1元",      "fallthrough", None),    # 守衛227 黑名單probe
    ("出到剩10個就好",             "target_gate", None),    # 守衛1491 目標水位誠實閘
    # 改價變體（新）
    ("無線滑鼠漲到300",            "price", 300),
    ("衛生紙降價到45",             "price", 45),
    ("藍牙耳機調降至90",           "price", 90),
    ("藍牙耳機調漲到1500",         "price", 1500),
    ("滑鼠的價格漲到300",          "price", 300),
    ("單價提高到350的無線滑鼠",    "fallthrough", None),    # 亂序句不硬猜
    ("價格漲到300",                "price", 300),           # 空名→工具層反問哪個商品
    # 改價既有（迴歸）
    ("彈珠改成100元",              "price", 100),
    ("無線滑鼠的價格改成590",      "price", 590),
]
for text, want, extra in CASES:
    got, info = route(text)
    ok = got == want
    if ok and want in ("restock", "stockset") and extra:
        ok = extra in (info[0] or "")
    if ok and want == "price" and isinstance(extra, int):
        ok = info[1] == extra
    ck(f"C {text} → {want}", ok, f"got={got} info={info}")

# 歧義反問（要真商品才反問；不存在的字串放行）
got, info = route("無線滑鼠調成200")
ck("C 無線滑鼠調成200 → 歧義反問", got == "ambig", f"got={got} {info}")
got, info = route("茶壺嘴調成200")
ck("C 非商品調成200 → 放行", got in ("fallthrough", "ambig"), f"got={got} {info}")

print()
print("bad", BAD)
sys.exit(1 if BAD else 0)
