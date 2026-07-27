#!/bin/bash
# 開機自動開倉管網頁：等 server 就緒（模型載入完）才開瀏覽器
URL="https://localhost:8002"

# ── 展場乾淨畫面（2026-07-27）─────────────────────────────────────
# 「Chromium 未正確關閉。還原」中文彈窗會蓋住畫面右上角。成因是上次
# chromium 沒有優雅結束（斷電、systemctl reboot、pkill 都算），profile 裡
# 留下 exit_type=Crashed。命令列旗標關不掉它，只能在啟動前把標記清乾淨。
PREF="$HOME/.config/chromium/Default/Preferences"
if [ -f "$PREF" ]; then
  python3 - "$PREF" <<'PY' 2>/dev/null
import json, sys
p = sys.argv[1]
try:
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
fi

for i in $(seq 1 120); do
  code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 2 "$URL/")
  if [ "$code" = "200" ]; then
    break
  fi
  sleep 2
done

# 旗標說明（都是為了展場不跳任何中文對話框）：
#   --test-type              消除「不受支援的命令列標幟」黃色警告列
#   --lang / Translate       關掉 Google 翻譯彈窗（英文／中文繁體）
#   --no-first-run 等        關首次執行精靈、預設瀏覽器詢問、崩潰氣泡
#   --remote-debugging-port  讓維護端可用 DevTools Protocol 檢查畫面/送輸入
#                            （只綁 127.0.0.1，外部連不到）
exec chromium-browser \
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
  "$URL"
