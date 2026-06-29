"""OOV v2 100 test"""
import urllib.request, json, sys, io, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
API = 'http://localhost:8000/api/query'
lines = open('oov_v2_100.txt', encoding='utf-8').readlines()
total = ok = 0
ng_list = []
for line in lines:
    line = line.strip()
    if not line or line.startswith('#'): continue
    parts = line.split('|', 1)
    if len(parts) != 2: continue
    exp = parts[0].strip(); text = parts[1].strip()
    if not text: continue; total += 1
    try:
        data = json.dumps({'text': text}).encode('utf-8')
        req = urllib.request.Request(API, data=data, headers={'Content-Type': 'application/json'})
        r = json.loads(urllib.request.urlopen(req, timeout=30).read().decode('utf-8'))
    except Exception as e:
        ng_list.append('ERR ' + text[:30] + ': ' + str(e)[:50])
        continue
    view = r.get('view','?'); fn = r.get('_function','')
    if exp == 'clarify': passed = view == 'clarify'
    elif exp in ('list_low_stock','list_hot_items','search_log','query_related_items'): passed = fn == exp or view in ('low_stock','hot_items','agent_rca','related')
    elif exp == 'query_movement': passed = fn == 'query_movement' and view == 'movement'
    elif exp == 'compare_warehouses': passed = fn == 'compare_warehouses' and view not in ('error','clarify')
    elif exp == 'list_expiring_items': passed = fn == 'list_expiring_items' or view == 'expiring'
    else: passed = view not in ('error','clarify','rejected') and fn in ('', exp, 'query_inventory')
    if passed: ok += 1
    else: ng_list.append('NG ' + text[:35] + ' exp=' + exp.ljust(22) + ' fn=' + str(fn).ljust(22) + ' view=' + view)
    time.sleep(0.15)
print('='*70)
print('v6+clf OOV v2: ' + str(ok) + '/' + str(total) + ' (' + str(round(ok/total*100,1)) + '%)')
if ng_list:
    print('\nFailures ' + str(len(ng_list)) + ':')
    for n in ng_list: print('  ' + n)
