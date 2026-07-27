#!/bin/bash
# health_watchdog.sh — server 卡死/failed 自動重啟（2026-07-17，numpy2 事件後三層復活之二）
# cron 每分鐘跑；連續 5 次 /health 無回應或 stage=failed → systemctl restart。
# 啟動中(starting/loading，模型載入~2min)視為活著不計失敗。
STATE=/tmp/wh_health_fails
H=$(curl -sk -m 10 https://localhost:8001/health 2>/dev/null)
if [ -n "$H" ] && ! echo "$H" | grep -q '"failed"'; then
  echo 0 > $STATE; exit 0
fi
N=$(cat $STATE 2>/dev/null || echo 0); N=$((N+1)); echo $N > $STATE
if [ $N -ge 5 ]; then
  echo "$(date): health fail x$N → restart (last: ${H:0:120})" >> /home/p400/warehouse_v2/health_watchdog.log
  sudo systemctl restart warehouse-v2.service
  echo 0 > $STATE
fi
