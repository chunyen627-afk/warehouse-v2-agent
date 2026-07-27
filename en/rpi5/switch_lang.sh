#!/bin/bash
# 桌面手動切換語言：帶參數 en / ch。
#   ~/啟動_英文版.sh → switch_lang.sh en
#   ~/啟動_中文版.sh → switch_lang.sh ch
#
# 兩版**都已開機自啟**（模型預先載好），所以這支通常只是「切換要看哪一版」，
# 不用等模型載入 → 切換很快。服務萬一沒起來也會自動 start。
LANG_ARG="${1:-en}"
if [ "$LANG_ARG" = "ch" ]; then
  SVC=warehouse-v2;     PORT=8001; NAME="中文版"
else
  SVC=warehouse-v2-en;  PORT=8002; NAME="English"
fi
URL="https://localhost:$PORT"

systemctl is-active --quiet "$SVC" || sudo systemctl start "$SVC"
echo "Starting $NAME ($PORT) ..."
for i in $(seq 1 120); do
  code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 2 "$URL/")
  [ "$code" = "200" ] && break
  sleep 2
done

# 清 Chromium 崩潰標記，否則跳中文彈窗「Chromium 未正確關閉。還原」
PREF="$HOME/.config/chromium/Default/Preferences"
[ -f "$PREF" ] && python3 - "$PREF" <<'PY' 2>/dev/null
import json, sys
try:
    p = sys.argv[1]
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    prof = d.setdefault("profile", {})
    prof["exit_type"] = "Normal"
    prof["exited_cleanly"] = True
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
except Exception:
    pass
PY

pkill -f chromium 2>/dev/null
sleep 2
# 旗標與 launch_warehouse.sh 完全一致（125% 縮放 + 三個防彈窗 + CDP 維護埠）
DISPLAY=:0 chromium-browser \
  --ozone-platform=x11 \
  --start-maximized \
  --force-device-scale-factor=1.25 \
  --ignore-certificate-errors \
  --test-type \
  --lang=en-US \
  --disable-features=Translate,TranslateUI \
  --disable-translate \
  --no-first-run \
  --no-default-browser-check \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --hide-crash-restore-bubble \
  --disable-popup-blocking \
  --remote-debugging-port=9222 \
  --remote-allow-origins=* \
  --disable-background-networking \
  --disable-sync \
  --disable-component-update \
  --disable-domain-reliability \
  --disable-client-side-phishing-detection \
  --safebrowsing-disable-auto-update \
  --metrics-recording-only \
  --no-pings \
  "$URL" &
