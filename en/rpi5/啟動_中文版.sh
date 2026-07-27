#!/bin/bash
# 桌面手動啟動：中文版（8001）。中文版 systemd 是 disabled（開機不自啟），
# 這支會先把它啟動起來再開瀏覽器——所以掃 CH QR 前要先點這個。
SVC=warehouse-v2
URL="https://localhost:8001"
systemctl is-active --quiet $SVC || sudo systemctl start $SVC
echo "啟動中文版倉管（8001）..."
for i in $(seq 1 120); do
  code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 2 "$URL/")
  [ "$code" = "200" ] && break
  sleep 2
done
pkill -f chromium 2>/dev/null
sleep 1
DISPLAY=:0 chromium-browser --ozone-platform=x11 --start-maximized   --force-device-scale-factor=1.5 --ignore-certificate-errors "$URL" &
