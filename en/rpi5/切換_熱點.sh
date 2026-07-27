#!/bin/bash
# 手動切換熱點 <-> WiFi。開機預設連 WiFi（EOSL_P400），要給訪客手機掃 QR
# 連進來時才開熱點。
# ⚠️ 熱點模式下 wlan0 被佔用、掃不到 WiFi，也就沒有外網 → ZeroTier 會被
#    watchdog 停掉（設計如此，離線展示零空轉）→ 遠端連不進來是正常的。
if nmcli -t -f NAME connection show --active | grep -q '^rpi5-hotspot$'; then
  echo "目前是熱點模式 → 切回 WiFi"
  sudo nmcli connection down rpi5-hotspot
  sleep 2
  sudo nmcli connection up EOSL_P400
else
  echo "目前是 WiFi → 開熱點 (RPI5-Demo / demo1234, 192.168.4.1)"
  sudo nmcli connection up rpi5-hotspot
fi
sleep 2
nmcli -t -f NAME,DEVICE,STATE connection show --active | grep wlan0
echo
echo "Press Enter to close."
read
