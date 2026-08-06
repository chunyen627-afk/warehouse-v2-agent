#!/bin/bash
# trace12.sh — 12 句破口逐一定位「卡在哪一層」（2026-08-04）
#
# 執行順序（WS 端，已從 server.py 抓出行號驗證）：
#   ① is_meaningful_input  9164   守門員
#   ② _detect_clarify      9197   匯出反問
#   ③ intent_clf primary  13591   分類器（可能 skip LLM）
#   ④ LLM 推論
#   ⑤ Pre-C-Sched         13738
#   ⑥ Pre-C10             13879   ← 英文匯出詞
#   ⑦ Pre-C-Movement      13924
#   ⑧ Pre-C-Cmp2          13970   ← 匯出定案
#   ⑨ _correct_function_call      C1~C18
#
# 每句跑完立刻抓 log，看**哪些層印了標記**、最終 call 是什麼。
cd ~/warehouse_v2_en || exit 1

QS=(
  "give me a csv of the movements"
  "can i get the movement records please"
  "give me the movement log for yesterday"
  "i'd like to see last week's movements"
  "export movements previous week"
  "export movements past month"
  "exprot movemnts last week"
  "warehouse health check"
  "which items grew the most"
  "i need a purchase order for items running out"
  "how many bluetooth earphones moved last week"
  "download movements last quarter"
)

for q in "${QS[@]}"; do
  echo "=============================================================="
  echo "Q: $q"
  timeout 90 python3 ws_inspect.py --rpi5 "$q" 2>&1 | grep -E 'view=' | head -1
  sleep 1
  journalctl -u warehouse-v2-en --since "25 sec ago" --no-pager 2>/dev/null \
    | grep -oE '\[(守門員|clarify|intent_clf primary|Pre-C-Sched|Pre-C10|Pre-C-Mov[a-z]*|Pre-C-Cmp2|校正 C[0-9]+[a-z]*|C1[0-9]|en-admin|en-funcword)\][^|]{0,60}' \
    | sed 's/^/    /' | head -8
  sleep 1
done
