#!/bin/bash
# run_guard_zh.sh — 中文版全量守衛（1122 條），基準隔離版。
#
# 為什麼要隔離（2026-08-03 建立，起因是今天兩版都踩到）：
#   守衛分數會被「資料狀態」影響，導致每次都要人工判讀
#   「這個 FAIL 是回歸還是資料髒了」。今天實例：
#     · 中文 1113/1122 —— 其中 4 個 FAIL 是我 reset 後庫存變充足，
#       缺貨類守衛失去前提（答案其實正確，只是關鍵字對不上）
#     · 英文 890/892 —— 2 個 FAIL 是守衛**自己前面的寫入句**
#       把 Steam Iron / Mixed Nuts 出掉，後面的句子就庫存不足
#   ⇒ 同一份語料在不同資料狀態下得到不同分數 = 分數不可信。
#
# 三道隔離：
#   ① 關動態模擬 —— 模擬每 2.7 秒改一次庫存，守衛全程假設「除非我寫入否則不動」
#   ② 開頭 reset —— 清掉前面測試/劇情批的殘留
#   ③ 結尾 reset —— 不把髒資料留給下一個人（今天就是這樣互相污染的）
#
# 用法：./run_guard_zh.sh          全量
#       ./run_guard_zh.sh --smoke  快篩（每區塊首句）
set -u
cd ~/warehouse_v2 || exit 1
PORT=8001
OTHER_PORT=8002   # 英文版——跑守衛時兩版模擬都要關（血案：模擬吃 94.9% CPU，守衛活性 116→9 句/分）
LOG=_guard_zh.log

_api() {  # $1=path  $2=json
    curl -sk -X POST "https://localhost:${PORT}$1" \
         -H 'Content-Type: application/json' -d "$2" --max-time 120 2>/dev/null
}

_api_other() {
    curl -sk -X POST "https://localhost:${OTHER_PORT}$1" \
         -H 'Content-Type: application/json' -d "$2" --max-time 120 2>/dev/null
}

echo "① 關閉動態模擬（守衛期間庫存不可自己變動；兩版都關，另一版模擬會吃光 CPU）"
_api /api/live_mode '{"action":"stop"}' | head -c 60; echo
_api_other /api/live_mode '{"action":"stop"}' | head -c 60; echo

echo "② reset 展示資料到 baseline"
_api /api/reset_demo '{"password":"0000"}' | head -c 60; echo

echo "③ 跑守衛（1122 條，約 10-15 分鐘）"
python3 regression_ws.py --rpi5 "$@" > "$LOG" 2>&1
echo "EXIT=$?" >> "$LOG"

echo "④ 結尾 reset（不留髒資料給下一個人）"
_api /api/reset_demo '{"password":"0000"}' | head -c 60; echo

# ⚠️ 一定要把模擬開回來（2026-08-03 user 發現「開網頁預設都沒有啟動模擬」）：
#   ①關掉模擬是守衛的必要條件，但**忘了開回來 = 展場看到靜止的倉庫**。
#   模擬只在**服務啟動**時 autostart（重開網頁不會，那是 server 端狀態），
#   跑完守衛不會自己回來 ⇒ 這步是展場前跑過測試後的最後一道保險。
echo "⑤ 重新啟動動態模擬（展場要看到數字在跳；兩版都開回）"
_api /api/live_mode '{"action":"start"}' | head -c 70; echo
_api_other /api/live_mode '{"action":"start"}' | head -c 70; echo

echo
grep -E "累積回歸|FAIL" "$LOG" | head -30
echo
echo "完整結果：$LOG"
echo "⚠️ 若 FAIL 是寫入句（mv/tf 類）且訊息含「庫存不足」，"
echo "   多半是守衛自己前面的寫入句造成 —— reset 後單獨復驗確認再判定回歸。"
