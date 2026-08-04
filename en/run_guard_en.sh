#!/bin/bash
# run_guard_en.sh — 英文版全量守衛（892 條），基準隔離版。
#
# 2026-08-03 升級：原本只在開頭 reset，仍出現 2 個假 FAIL
#   （central sent out 100 trail mix / move 20 clothes iron）——
#   成因是守衛**自己前面的寫入句**把庫存出掉，後面同商品的句子就不足。
#   reset 後單獨復驗兩句都正常開卡 ⇒ 實質 892/892。
# ⇒ 補上「關模擬」與「結尾 reset」，讓分數不再受資料狀態影響。
#   （中文版對應腳本：~/warehouse_v2/run_guard_zh.sh，同一套設計）
#
# 用法：./run_guard_en.sh          全量
#       ./run_guard_en.sh --smoke  快篩
set -u
cd ~/warehouse_v2_en || exit 1
PORT=8002
LOG=_guard_en.log

_api() {
    curl -sk -X POST "https://localhost:${PORT}$1" \
         -H 'Content-Type: application/json' -d "$2" --max-time 120 2>/dev/null
}

echo "① 關閉動態模擬（守衛期間庫存不可自己變動）"
_api /api/live_mode '{"action":"stop"}' | head -c 60; echo

echo "② reset 展示資料到 baseline"
_api /api/reset_demo '{"password":"0000"}' | head -c 60; echo

echo "③ 跑守衛（892 條，約 8-10 分鐘）"
python3 regression_ws.py --rpi5 --file regression_corpus_en.txt "$@" > "$LOG" 2>&1
echo "EXIT=$?" >> "$LOG"

echo "④ 結尾 reset（不留髒資料給下一個人）"
_api /api/reset_demo '{"password":"0000"}' | head -c 60; echo

# ⚠️ 一定要把模擬開回來（2026-08-03 user 發現「開網頁預設都沒有啟動模擬」）：
#   ①關掉模擬是守衛的必要條件，但**忘了開回來 = 展場看到靜止的倉庫**。
#   模擬只在服務啟動時 autostart，跑完守衛不會自己回來。
#   ⇒ 展場前若跑過測試，這步就是最後一道保險。
echo "⑤ 重新啟動動態模擬（展場要看到數字在跳）"
_api /api/live_mode '{"action":"start"}' | head -c 70; echo

echo
grep -E "累積回歸|FAIL" "$LOG" | head -30
echo
echo "完整結果：$LOG"
echo "⚠️ 若 FAIL 是寫入句（mv/tf 類）且訊息含 Not enough stock，"
echo "   多半是守衛自己前面的寫入句造成 —— reset 後單獨復驗確認再判定回歸。"
