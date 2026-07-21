#!/usr/bin/env bash
# check_zombies.sh — 跑測試前的環境體檢：確認沒有殭屍程序搶資源。
# RPI5 只有 4 核 8GB，幾個殘留就會拖垮倉管 → 每次跑測試前必跑。
#
# 用法：
#   bash check_zombies.sh          # 只檢查、列出可疑程序
#   bash check_zombies.sh --clean  # 檢查 + 清掉殘留測試程序（保留 server）
#
# 判定規則：
#   保留：warehouse server（server.py / server_https.py）、系統程序
#   殘留：regression_ws / ws_convo / ws_inspect / branch_walk / context_fuzz
#         / llama-funasr-* 這些測試工具（跑完該自己結束，還在=殘留）

set -uo pipefail
CLEAN=0
[ "${1:-}" = "--clean" ] && CLEAN=1

echo "=================================================="
echo "環境體檢 $(date '+%Y-%m-%d %H:%M:%S')"
echo "=================================================="

# ── 1. 倉管 server（該有,且只該有一個）──
SRV=$(pgrep -f "server(_https)?\.py" | wc -l)
echo "▶ 倉管 server: $SRV 個 $([ "$SRV" -eq 1 ] && echo '✅' || echo '⚠️ 異常(應為1)')"
[ "$SRV" -gt 1 ] && pgrep -af "server(_https)?\.py"

# ── 2. 測試工具殘留（不該有）──
TOOLS="regression_ws|ws_convo|ws_inspect|branch_walk|context_fuzz|llama-funasr"
ZOMBIE=$(pgrep -af "$TOOLS" 2>/dev/null | grep -v "check_zombies" || true)
ZN=$(echo "$ZOMBIE" | grep -c . || true)
if [ -z "$ZOMBIE" ]; then
    echo "▶ 測試工具殘留: 0 個 ✅"
else
    echo "▶ 測試工具殘留: $ZN 個 ⚠️"
    echo "$ZOMBIE" | sed 's/^/    /'
    if [ "$CLEAN" -eq 1 ]; then
        echo "  → 清理中..."
        echo "$ZOMBIE" | awk '{print $1}' | xargs -r kill -9 2>/dev/null
        sleep 1
        echo "  → 清理完成，剩餘: $(pgrep -cf "$TOOLS" 2>/dev/null || echo 0)"
    else
        echo "  → 加 --clean 可清理"
    fi
fi

# ── 3. 資源 ──
echo "▶ 記憶體:"
free -h 2>/dev/null | sed -n '2p' | awk '{print "    總 "$2" / 已用 "$3" / 可用 "$7}'
echo "▶ 負載: $(uptime | sed 's/.*load average: //')"

# ── 4. 判定 ──
echo "--------------------------------------------------"
if [ "$SRV" -eq 1 ] && [ -z "$ZOMBIE" ]; then
    echo "✅ 環境乾淨，可以開跑"
    exit 0
else
    echo "⚠️ 環境有問題，建議先處理（--clean 清殘留 / 手動處理 server）"
    exit 1
fi
