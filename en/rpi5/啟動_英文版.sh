#!/bin/bash
# 桌面手動啟動：英文版（8002）。開機本來就會自啟英文版，這支是給
# 「不小心關掉」或「從中文版切回來」時用的。
SVC=warehouse-v2-en
URL="https://localhost:8002"
systemctl is-active --quiet $SVC || sudo systemctl start $SVC
echo "Starting English warehouse (8002)..."
for i in $(seq 1 120); do
  code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 2 "$URL/")
  [ "$code" = "200" ] && break
  sleep 2
done
pkill -f chromium 2>/dev/null
sleep 1
DISPLAY=:0 chromium-browser --ozone-platform=x11 --start-maximized   --force-device-scale-factor=1.5 --ignore-certificate-errors "$URL" &
