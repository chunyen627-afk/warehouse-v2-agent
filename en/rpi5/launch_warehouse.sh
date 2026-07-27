#!/bin/bash
# 開機自動開倉管網頁。
#
# ── 2026-07-27：**同時開中英兩個分頁**（user 定調）──────────────────
#   英文 8002 在**前景**（展場主力），中文 8001 開在後面備著。
#   訪客要切語言 → 點瀏覽器分頁即可，不用等服務啟動或模型載入
#   （兩個 systemd 服務都 enabled，開機就把模型預載好了）。
#
#   為什麼敢雙開——實測數據（RPI5 4 核 8GB）：
#     記憶體 2.15G/7.9G（中文 880MB + 英文 798MB），load 0.45 幾乎閒置
#     中英各 1 人同時查 → 最慢 0.28s
#     中英各 2 人同時查 → 最慢 0.48s
#     **中文跑語音辨識（吃滿 4 核）時，英文查詢仍 0.42s**
#   模型閒置時不吃 CPU，而查詢主力是 FastText 分類器（毫秒級），
#   不是每句都跑 LLM ⇒ 雙開對展場真實負載無感。
#   （8 人同秒湧入會有一句排到 8s，但展場不現實。）
EN_URL="https://localhost:8002"
ZH_URL="https://localhost:8001"

# 清 Chromium 崩潰標記，否則開機跳中文彈窗「Chromium 未正確關閉。還原」
# 蓋住畫面右上角（斷電 / reboot / pkill 都會留下 exit_type=Crashed，
# 命令列旗標關不掉，只能在啟動前改 profile）。
# ⚠️ 用獨立腳本不要內嵌 heredoc——巢狀 heredoc 很容易被外層吃掉終止符
#    導致整支腳本語法錯（踩過）。
[ -x "$HOME/fix_chromium_exit.py" ] && python3 "$HOME/fix_chromium_exit.py" 2>/dev/null

# 等**兩個** server 都就緒才開瀏覽器（模型載入要時間，太早開會看到錯誤頁）
for url in "$EN_URL" "$ZH_URL"; do
  for i in $(seq 1 120); do
    code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 2 "$url/")
    [ "$code" = "200" ] && break
    sleep 2
  done
done

# 旗標說明（都是為了展場不跳任何中文對話框）：
#   --test-type              消除「不受支援的命令列標幟」黃色警告列
#   --lang / Translate       關掉 Google 翻譯彈窗（英文／中文繁體）
#   --no-first-run 等        關首次執行精靈、預設瀏覽器詢問、崩潰氣泡
#   --remote-debugging-port  讓維護端可用 DevTools Protocol 檢查畫面/送輸入
#                            （只綁 127.0.0.1，外部連不到）
# ⚠️ 網址順序 = 分頁順序，而 Chromium **第一個網址會獲得焦點**
#    （實測：先寫中文的話，開機後前景是中文版）
#    → **英文寫在前面**，訪客一開機看到的就是英文版（展場主力）。
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
  "$EN_URL" "$ZH_URL"
