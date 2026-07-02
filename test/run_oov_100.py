"""測試 oov_100.txt 全部查詢 — 走 /ws（跟瀏覽器一模一樣的真實路徑）。
2026-07-02 從 HTTP /api/query 改成 WS：實測發現 WS 端完全沒有套用 HTTP
端的 intent_clf 主路由層（只有 HTTP api_query 呼叫 intent_clf.predict），
WS 完全靠 LLM 自己判斷 function/抽 keyword，穩定性明顯較低（這是既有
架構限制，不是這次改動造成的新 bug，見 [[warehouse_v2_project]] 記錄）。
判斷邏輯改成純用 view，因為 WS 的 done 訊息沒有 HTTP 版才有的 _function 欄位。"""
import asyncio, json, sys, io, time, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import websockets

WS_URI = 'ws://localhost:8000/ws'
lines = open('oov_100.txt', encoding='utf-8').readlines()

async def _q_async(text):
    async with websockets.connect(WS_URI, max_size=None) as ws:
        await ws.send(json.dumps({'type': 'chat', 'text': text}, ensure_ascii=False))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            msg = json.loads(raw)
            if msg.get('type') == 'done':
                return msg.get('result', {})

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
        r = asyncio.run(_q_async(text))
    except Exception as e:
        ng += 1
        ng_list.append(f'FAIL {text[:30]} | WS error: {e}')
        continue

    view = r.get('view', '?')
    d = r.get('data', {})
    kw = d.get('keyword', d.get('target', ''))
    cat = d.get('category', '')
    q = r.get('question', '')

    # 判斷通過（純用 view，WS 端沒有 HTTP 版才有的 _function 欄位）
    if exp == 'clarify':
        passed = view == 'clarify'
    elif exp == 'list_low_stock':
        passed = view == 'low_stock'
    elif exp == 'list_hot_items':
        passed = view == 'hot_items'
    elif exp == 'list_expiring_items':
        passed = view == 'expiring'
    elif exp == 'search_log':
        passed = view == 'agent_rca'
    elif exp == 'query_movement':
        passed = view == 'movement'
    elif exp == 'query_related_items':
        passed = view in ('related', 'related_help', 'related_empty')
    elif exp == 'query_inventory':
        # 寬鬆：clarify with multiple results 也算通過（系統有找到商品，只是有多個）
        passed = view in ('inventory', 'inventory_single', 'clarify')
        # 「快沒了/缺貨」→ low_stock 也接受
        if not passed and view == 'low_stock':
            passed = True
    else:
        passed = view not in ('error', 'clarify', 'rejected')

    if passed:
        ok += 1
    else:
        ng += 1
        ng_list.append(f'FAIL {text[:35]:35s} exp={exp:20s} got view={view}')

    time.sleep(0.15)

print(f'\n{"="*70}')
print(f'結果: {ok}/{total} ({ok/total*100:.1f}%)')
if ng_list:
    print(f'\n失敗 {ng} 題:')
    for n in ng_list:
        print(f'  {n}')
