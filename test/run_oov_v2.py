"""OOV v2 100 test — 走 /ws（跟瀏覽器一模一樣的真實路徑）。
2026-07-02 從 HTTP /api/query 改成 WS：WS 的 done 訊息不像 HTTP 版
api_query 會額外加 _function 欄位，判斷邏輯改成純用 view（每個工具的
view 值本身就有足夠辨識度，不需要 _function 輔助）。"""
import asyncio, json, sys, io, time, pathlib
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import websockets
WS_URI = 'ws://localhost:8000/ws'
TEST_FILE = str(pathlib.Path(__file__).parent / 'oov_v2_100.txt')

async def _q_async(text):
    async with websockets.connect(WS_URI, max_size=None) as ws:
        await ws.send(json.dumps({'type': 'chat', 'text': text}, ensure_ascii=False))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            msg = json.loads(raw)
            if msg.get('type') == 'done':
                return msg.get('result', {})

file_lines = open(TEST_FILE, encoding='utf-8').readlines()
total = ok = 0
ng_list = []
print(f'Loaded {len(file_lines)} lines')

for line in file_lines:
    line = line.strip()
    if not line or line.startswith('#'): continue
    parts = line.split('|', 1)
    if len(parts) != 2: continue
    exp = parts[0].strip(); text = parts[1].strip()
    if not text: continue
    total += 1
    try:
        r = asyncio.run(_q_async(text))
    except Exception as e:
        ng_list.append(f'FAIL {text[:35]:35s} exp={exp:20s} | WS error: {str(e)[:50]}')
        continue
    view = r.get('view','?')
    if exp == 'clarify': passed = view == 'clarify'
    elif exp == 'list_low_stock': passed = view == 'low_stock'
    elif exp == 'list_hot_items': passed = view == 'hot_items'
    elif exp == 'search_log': passed = view == 'agent_rca'
    elif exp == 'query_related_items': passed = view in ('related', 'related_help', 'related_empty')
    elif exp == 'query_movement': passed = view == 'movement'
    elif exp == 'compare_warehouses': passed = view == 'compare_warehouses'
    elif exp == 'list_expiring_items': passed = view == 'expiring'
    elif exp == 'query_inventory':
        passed = view in ('inventory', 'inventory_single', 'low_stock')
    else: passed = view not in ('error', 'clarify', 'rejected')
    if passed:
        ok += 1
    else:
        ng_list.append(f'FAIL {text[:35]:35s} exp={exp:20s} got view={view}')
    time.sleep(0.15)

if total > 0:
    print('OOV v2: ' + str(ok) + '/' + str(total) + ' (' + str(round(ok/total*100,1)) + '%)')
    if ng_list:
        print(f'\n失敗 {len(ng_list)} 題:')
        for n in ng_list:
            print(f'  {n}')
else:
    print('No valid test lines found in ' + TEST_FILE)
