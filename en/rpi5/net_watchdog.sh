#!/bin/bash
# 網路 watchdog v2（2026-07-14）：動態偵測目前閘道，失聯 2 次才重連。
# 展場換手機熱點也適用（閘道自動抓，不寫死）；完全沒連線時也會嘗試重連。
GW=$(ip route | awk '/^default/ {print $3; exit}')
reconnect() {
  logger -t net_watchdog "reconnecting wlan0 (gw=${GW:-none})"
  nmcli device disconnect wlan0 >/dev/null 2>&1
  sleep 3
  nmcli device connect wlan0 >/dev/null 2>&1
}
if [ -z "$GW" ]; then
  # 沒有預設路由 = 根本沒連上網路 → 直接嘗試重連
  reconnect
  exit 0
fi
if ! ping -c1 -W3 "$GW" >/dev/null 2>&1; then
  sleep 5
  if ! ping -c1 -W3 "$GW" >/dev/null 2>&1; then
    reconnect
  fi
fi
