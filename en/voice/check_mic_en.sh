#!/usr/bin/env bash
# check_mic_en.sh — 錄音前的一鍵體檢（英文版 8002）。
#   改自中文版 check_mic.sh：port 8001→8002。
#   ⚠️ 錄英文一定要用這支——中文版對英文句本來就會失敗，會得到誤導結果。
#
# 用途：語音全鏈在合成音檔上已 24/24 通過，但「真的用麥克風錄音」這條路徑
# 從未實測（先前 RPI5 上 arecord -l 是空的）。這支腳本把插上麥克風後會踩到
# 的每個關卡逐一驗過，避免展場當天才發現。
#
# 用法（RPI5）：bash check_mic.sh
set -uo pipefail
cd "$(dirname "$0")"

PASS=0; FAIL=0
ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL+1)); }
warn() { echo "  ⚠️  $1"; }

echo "=================================================="
echo "AM8 麥克風體檢 $(date '+%Y-%m-%d %H:%M:%S')"
echo "=================================================="

# ① 硬體有沒有被認到
echo ""
echo "① 錄音裝置"
if arecord -l 2>/dev/null | grep -q "^card"; then
    arecord -l 2>/dev/null | grep "^card" | sed 's/^/     /'
    ok "系統認到錄音裝置"
    CARD=$(arecord -l 2>/dev/null | grep -m1 "^card" | sed -E 's/card ([0-9]+).*device ([0-9]+).*/\1,\2/')
else
    bad "沒有任何錄音裝置（麥克風沒插好 / USB 沒供電 / 需重開機）"
    CARD=""
fi

# ② 預設輸入來源是不是 AM8（RPI5 常有 HDMI 音訊搶當預設）
echo ""
echo "② 預設輸入來源"
if command -v pactl >/dev/null 2>&1; then
    DEF=$(pactl get-default-source 2>/dev/null || echo "")
    if [ -n "$DEF" ]; then
        echo "     $DEF"
        case "$DEF" in
            *[Uu][Ss][Bb]*|*[Ff]ifine*|*AM8*) ok "預設來源看起來是 USB 麥克風" ;;
            *auto_null*|"") bad "預設來源是空的（沒有可用輸入）" ;;
            *) warn "預設來源不像 USB 麥克風——若錄不到聲音，用下行指令改：
        pactl set-default-source <來源名稱>
        （可用 'pactl list short sources' 看全部）" ;;
        esac
    else
        warn "pactl 取不到預設來源（PipeWire 可能還沒起來）"
    fi
else
    warn "沒有 pactl，跳過"
fi

# ③ 實際錄 3 秒，確認收得到聲音（不是只有靜音）
echo ""
echo "③ 實錄測試（請對著麥克風講話，3 秒）"
if [ -n "$CARD" ]; then
    TMP=$(mktemp /tmp/micXXXX.wav)
    if arecord -D "plughw:$CARD" -f S16_LE -r 16000 -c 1 -d 3 "$TMP" 2>/dev/null; then
        SIZE=$(stat -c%s "$TMP")
        if [ "$SIZE" -gt 1000 ]; then
            ok "錄到 $SIZE bytes"
            # 用 ffmpeg 量音量，全靜音代表沒真的收到聲
            VOL=$(ffmpeg -i "$TMP" -af volumedetect -f null /dev/null 2>&1 \
                  | grep max_volume | sed -E 's/.*max_volume: (-?[0-9.]+).*/\1/')
            if [ -n "$VOL" ]; then
                echo "     最大音量 ${VOL} dB"
                # -50dB 以下幾乎等於沒收到聲音
                if awk "BEGIN{exit !($VOL < -50)}"; then
                    bad "音量過低——麥克風靜音鍵沒開？增益太低？"
                else
                    ok "音量正常"
                fi
            fi
            # ④ 直接餵 ASR，驗證全鏈
            echo ""
            echo "④ 送 ASR 辨識（驗證全鏈）"
            R=$(curl -sk -m 120 -X POST https://127.0.0.1:8002/api/asr \
                --data-binary "@$TMP" -H 'Content-Type: application/octet-stream')
            echo "     $R"
            if echo "$R" | grep -q '"ok":true'; then
                ok "ASR 有辨識出內容"
            else
                bad "ASR 沒辨識出內容（音量太小或講話太短？）"
            fi
        else
            bad "錄音檔太小（$SIZE bytes）"
        fi
    else
        bad "arecord 錄音失敗（裝置被佔用？權限？）"
    fi
    rm -f "$TMP"
else
    warn "沒有裝置，跳過實錄"
fi

# ⑤ 瀏覽器端麥克風權限
echo ""
echo "⑤ Chromium 麥克風權限"
PREF="$HOME/.config/chromium/Default/Preferences"
if [ -f "$PREF" ]; then
    if python3 -c "
import json,sys
d=json.load(open('$PREF'))
ex=d.get('profile',{}).get('content_settings',{}).get('exceptions',{}).get('media_stream_mic',{})
hit=[k for k,v in ex.items() if 'localhost:8002' in k and v.get('setting')==1]
sys.exit(0 if hit else 1)
" 2>/dev/null; then
        ok "https://localhost:8002 已授權麥克風"
    else
        bad "未授權——開頁面後點麥克風鈕，跳出提示要按「允許」"
    fi
else
    warn "找不到 Chromium 設定檔（還沒開過瀏覽器？）"
fi

echo ""
echo "=================================================="
echo "通過 $PASS 項、失敗 $FAIL 項"
if [ "$FAIL" -eq 0 ]; then
    echo "✅ 麥克風全鏈就緒，可以直接用語音操作"
else
    echo "⚠️  上面 ❌ 的項目要先解決"
fi
echo "=================================================="
