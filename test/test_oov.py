"""
test_oov.py — OOV 容錯系統性驗證
打 /api/query HTTP endpoint，測打字錯誤 / 不完整 / 模糊輸入的補救能力
"""
import urllib.request, json, sys, io
# 強制 UTF-8 stdout
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

API = "http://localhost:8000/api/query"

def q(text, label=""):
    """打 API，回 (ok, view, func_name, keyword, summary_preview)"""
    data = json.dumps({"text": text}).encode("utf-8")
    try:
        req = urllib.request.Request(API, data=data, headers={"Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
    except Exception as e:
        return False, "error", "", "", str(e)
    ok = r.get("ok", False)
    view = r.get("view", "?")
    d = r.get("data", {})
    kw = d.get("keyword", d.get("target", ""))
    summary = r.get("summary", "")[:80]
    return ok, view, kw, summary

# ── 已知 SKU 清單（從 seed_data 抓，避免 hardcode）──
KNOWN_SKUS = [
    "藍牙耳機", "無線藍牙耳機", "氣泡水", "檸檬氣泡水", "悶燒罐", "不鏽鋼悶燒罐",
    "咖啡豆", "咖啡機", "全自動咖啡機", "洗衣精", "抗菌洗衣精 4kg", "濃縮洗衣精",
    "衛生紙", "瑜珈墊", "慢跑鞋", "牛仔褲", "羊毛襪", "羽絨外套", "素T",
    "蚊香", "垃圾袋", "沐浴乳", "蘇打餅", "堅果", "檸檬茶", "電熨斗",
    "不沾鍋", "果汁機", "行動電源", "充電線", "水壺", "健身環", "運動毛巾",
    "尿布", "帳篷", "運動襪",
]

print("=" * 72)
print("OOV 容錯測試")
print("=" * 72)

tests = []
total = ok_count = clarify_count = error_count = 0

def add(label, text, expect_ok=True, expect_view=None, expect_kw_contains=None):
    tests.append((label, text, expect_ok, expect_view, expect_kw_contains))

# ════════════════════════════════════════════════════════
# A. 錯字 / 相似音（typo）
# ════════════════════════════════════════════════════════
add("A1 錯字-芽",    "藍芽耳機庫存",       True, None, "藍牙耳機")
add("A2 錯字-汽",    "汽泡水剩多少",       True, None, "氣泡水")
add("A3 錯字-鍋",    "悶燒鍋庫存",         True, None, "悶燒罐")
add("A4 錯字-洗衣",  "洗衣經還剩幾件",     True, None, "洗衣精")
add("A5 錯字-電燙斗","電燙斗有多少",       True, None, "電熨斗")
add("A6 錯字-慢跑",  "慢跑鞋剩多少",       True, None, "慢跑鞋")  # 正確字，對照組

# ════════════════════════════════════════════════════════
# B. 不完整 / 部分商品名
# ════════════════════════════════════════════════════════
add("B1 部分-耳機",  "耳機庫存",           True, None, "藍牙耳機")
add("B2 部分-洗衣",  "洗衣還有多少",       True, None, "洗衣精")
add("B3 部分-咖啡",  "咖啡剩多少",         True, None, "咖啡")  # 可能咖啡豆或咖啡機
add("B4 部分-牛仔",  "牛仔褲庫存",         True, None, "牛仔褲")
add("B5 部分-瑜珈",  "瑜珈剩幾個",         True, None, "瑜珈墊")

# ════════════════════════════════════════════════════════
# C. 多餘雜詞 / 口語填充
# ════════════════════════════════════════════════════════
add("C1 雜詞-幫我查","幫我查一下藍牙耳機的庫存還有多少", True, None, "藍牙耳機")
add("C2 雜詞-我想",  "我想知道氣泡水剩多少", True, None, "氣泡水")
add("C3 雜詞-請問",  "請問卫生纸還有幾件",   True, None, "衛生紙")  # 簡體字
add("C4 雜詞-那個",  "那個洗衣精好像快沒了", True, None, "洗衣精")
add("C5 雜詞-看一下","看一下悶燒罐",         True, None, "悶燒罐")

# ════════════════════════════════════════════════════════
# D. 英文 / 中英混合
# ════════════════════════════════════════════════════════
add("D1 英文-bt",    "bluetooth earphone stock", True, None, "bluetooth")
add("D2 中英混合",   "藍牙 earphone 庫存",  True, None, "藍牙")
add("D3 英文-coffee","coffee machine stock",True, None, "coffee")

# ════════════════════════════════════════════════════════
# E. 模糊 / 只有商品名沒有動作
# ════════════════════════════════════════════════════════
add("E1 只商品名",   "藍牙耳機",           True, "clarify", None)  # 預期給選單
add("E2 只商品名2",  "悶燒罐",             True, "clarify", None)
add("E3 只倉庫名",   "北倉",               True, "clarify", None)
add("E4 模糊-查",    "查",                 True, "clarify", None)

# ════════════════════════════════════════════════════════
# F. 不存在的商品
# ════════════════════════════════════════════════════════
add("F1 不存在-電視","電視機庫存",         True, None, None)  # 應 clarify 或查無
add("F2 不存在-藍牙喇叭","藍牙喇叭剩多少", True, None, "藍牙喇叭")  # 也沒這商品
add("F3 不存在-手機","手機殼庫存",         True, None, None)
add("F4 不存在-餅乾","餅乾剩多少",         True, None, "蘇打餅")  # fuzzy 可能對到蘇打餅

# ════════════════════════════════════════════════════════
# G. 邊界 case
# ════════════════════════════════════════════════════════
add("G1 超短",       "水",                 True, "clarify", None)  # 太短 → clarify
add("G2 純數字",     "123",                False, None, None)  # reject
add("G3 空字串",     "",                   False, None, None)
add("G4 純英文短",   "stock",              True, None, "stock")

# ════════════════════════════════════════════════════════
# H. RCA OOV（錯字 + 帳對不上）
# ════════════════════════════════════════════════════════
add("H1 RCA-錯字",   "藍芽耳機帳對不上",   True, None, "藍牙耳機")
add("H2 RCA-錯字2",  "洗衣經帳對不上",     True, None, "洗衣精")

# ── 執行 ──
for label, text, expect_ok, expect_view, expect_kw in tests:
    total += 1
    ok, view, kw, summary = q(text, label)

    # 判斷結果
    status = "OK" if ok == expect_ok else "NG"
    if expect_view and view != expect_view:
        status = f"{status}(view={view})"
    if expect_kw and expect_kw not in str(kw):
        status = f"{status}(kw={kw!r})"

    if ok:
        ok_count += 1
    elif view == "clarify":
        clarify_count += 1
    else:
        error_count += 1

    kw_str = str(kw)[:30] if kw else "-"
    print(f"  [{status}] {label}: {text!r}")
    print(f"         → view={view}  kw={kw_str}  {summary[:60]}")

print()
print(f"結果: {ok_count}/{total} OK, {clarify_count} clarify, {error_count} error")
