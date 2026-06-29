"""從 oov_100.txt 生成 training_data.jsonl 擴充資料"""
import json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

lines = open('oov_100.txt', encoding='utf-8').readlines()
new_records = []

for line in lines:
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    parts = line.split('|', 1)
    if len(parts) != 2:
        continue
    tool_name = parts[0].strip()
    user_text = parts[1].strip()
    if not user_text or tool_name == 'clarify':
        continue

    # Generate tool_arguments based on function type
    args = {}
    if tool_name in ('query_inventory', 'query_movement', 'search_log', 'query_related_items'):
        # Extract keyword from user text (same logic as _extract_sku_keyword)
        import server
        kw = server._extract_sku_keyword(user_text)
        if kw:
            args['keyword'] = kw

    if tool_name == 'query_movement':
        if any(w in user_text for w in ('今天','今日')):
            args['period'] = 'today'
        elif any(w in user_text for w in ('這週','本週','這禮拜')):
            args['period'] = 'this_week'
        elif any(w in user_text for w in ('本月','這個月')):
            args['period'] = 'this_month'
        if any(w in user_text for w in ('出貨','出庫','賣出')):
            args['direction'] = 'out'
        elif any(w in user_text for w in ('進貨','入庫')):
            args['direction'] = 'in'

    if tool_name == 'list_hot_items':
        if any(w in user_text for w in ('滯銷','賣最差','最差')):
            args['rank_type'] = 'slow'
        else:
            args['rank_type'] = 'hot'

    record = {
        "user_content": user_text,
        "tool_name": tool_name,
        "tool_arguments": json.dumps(args, ensure_ascii=False) if args else '{}'
    }
    new_records.append(json.dumps(record, ensure_ascii=False))

# Append to training file
train_file = '../training_data.jsonl'
with open(train_file, 'a', encoding='utf-8') as f:
    for r in new_records:
        f.write(r + '\n')

print(f'Added {len(new_records)} training records to {train_file}')
