"""
eval_http.py — HTTP-based routing accuracy eval
用法: python eval_http.py
"""
import json, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_cases import CASES, E2E_EXTRA_CASES

API = "http://localhost:8000/api/query"
ALL = CASES + E2E_EXTRA_CASES

# view → function 對照
VIEW_FUNC = {
    "inventory": "query_inventory", "inventory_single": "query_inventory",
    "movement": "query_movement", "low_stock": "list_low_stock",
    "compare": "compare_warehouses", "hot_items": "list_hot_items",
    "related_items": "query_related_items", "expiring": "list_expiring_items",
    "agent_rca": "search_log", "config_read": "manage_config",
    "config_confirm": "manage_config", "config_done": "manage_config",
    "script_confirm": "run_script", "script_done": "run_script",
    "report": "generate_report", "po_confirm": "generate_po", "po_done": "generate_po",
    "alert_confirm": "set_alert", "alert_done": "set_alert",
    "schedule_confirm": "set_schedule", "schedule_done": "set_schedule",
    "compare_help": "compare_warehouses",
}

total = 0
passed = 0
failed = []

def get_func_name(r):
    """從 response 推實際執行的 function"""
    # 優先用 server 回傳的 _function（頂層）
    fn = r.get("_function", "")
    if fn:
        return fn
    # 從 view 推
    view = r.get("view", "")
    return VIEW_FUNC.get(view)

def get_keyword(r):
    d = r.get("data", {})
    return d.get("keyword", d.get("target", ""))

def kw_match(exp_args, actual_kw):
    """檢查 keyword 是否符合預期"""
    if not exp_args:
        return True
    exp_kw = exp_args.get("keyword", exp_args.get("target", ""))
    if not exp_kw:
        return True
    return exp_kw in str(actual_kw)

print(f"評測 {len(ALL)} 題...")
t0 = time.time()

for user_text, exp_func, exp_args in ALL:
    total += 1
    data = json.dumps({"text": user_text}).encode("utf-8")
    try:
        req = urllib.request.Request(API, data=data, headers={"Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))
    except Exception as e:
        failed.append(f"FAIL [{user_text}] HTTP error: {e}")
        continue

    act_func = get_func_name(r)
    act_kw = get_keyword(r)

    # 判斷
    func_ok = (act_func == exp_func) if exp_func else (act_func is None or r.get("view") in ("clarify", "guide", "rejected", "error"))
    kw_ok = kw_match(exp_args, act_kw)

    if func_ok and kw_ok:
        passed += 1
        # print(f"  ✓ [{user_text}] → {act_func}")
    else:
        exp_str = f"{exp_func}({exp_args.get('keyword','')})" if exp_func else "reject"
        act_str = f"{act_func}({act_kw})" if act_func else r.get('view','?')
        failed.append(f"FAIL [{user_text}] exp={exp_str} act={act_str}")

    time.sleep(0.25)  # 避免打太快

elapsed = time.time() - t0
print(f"\n{'='*60}")
print(f"結果: {passed}/{total} ({passed/total*100:.1f}%)  耗時 {elapsed:.0f}s")
if failed:
    print(f"\n失敗 ({len(failed)}):")
    for f in failed:
        print(f)
else:
    print("全部通過 ✓")
