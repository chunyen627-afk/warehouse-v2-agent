"""測試 oov_100.txt 全部查詢"""
import urllib.request, json, sys, io, time, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

API = 'http://localhost:8000/api/query'
lines = open('oov_100.txt', encoding='utf-8').readlines()

total = ok = ng = 0
ng_list = []

for line in lines:
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    # 格式: expected_func | query_text
    parts = line.split('|', 1)
    if len(parts) != 2:
        continue
    exp = parts[0].strip()
    text = parts[1].strip()
    if not text:
        continue
    total += 1

    try:
        data = json.dumps({'text': text}).encode('utf-8')
        req = urllib.request.Request(API, data=data, headers={'Content-Type': 'application/json'})
        r = json.loads(urllib.request.urlopen(req, timeout=30).read().decode('utf-8'))
    except Exception as e:
        ng += 1
        ng_list.append(f'FAIL {text[:30]} | HTTP error: {e}')
        continue

    view = r.get('view', '?')
    fn = r.get('_function', '')
    d = r.get('data', {})
    kw = d.get('keyword', d.get('target', ''))
    cat = d.get('category', '')
    q = r.get('question', '')

    # 判斷通過
    if exp == 'clarify':
        passed = view == 'clarify'
    elif exp == 'list_low_stock':
        passed = fn == 'list_low_stock' or view == 'low_stock'
    elif exp == 'list_hot_items':
        passed = fn == 'list_hot_items' or view == 'hot_items'
    elif exp == 'list_expiring_items':
        passed = fn == 'list_expiring_items' or view == 'expiring'
    elif exp == 'search_log':
        passed = fn == 'search_log' or view == 'agent_rca'
    elif exp == 'query_movement':
        passed = fn == 'query_movement' and view == 'movement'
    elif exp == 'query_related_items':
        passed = fn == 'query_related_items' and view in ('related', 'related_help')
    elif exp == 'query_inventory':
        # 寬鬆：clarify with multiple results 也算通過（系統有找到商品，只是有多個）
        passed = view not in ('error', 'rejected') and fn in ('', 'query_inventory')
        # 「快沒了/缺貨」→ low_stock 也接受
        if not passed and view == 'low_stock':
            passed = True
    else:
        passed = view not in ('error', 'clarify', 'rejected') and (fn == exp or fn == '')

    if passed:
        ok += 1
    else:
        ng += 1
        ng_list.append(f'FAIL {text[:35]:35s} exp={exp:20s} got fn={str(fn):20s} view={view}')

    time.sleep(0.15)

print(f'\n{"="*70}')
print(f'結果: {ok}/{total} ({ok/total*100:.1f}%)')
if ng_list:
    print(f'\n失敗 {ng} 題:')
    for n in ng_list:
        print(f'  {n}')
