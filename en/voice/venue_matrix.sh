#!/usr/bin/env bash
# venue_matrix.sh — 展場條件矩陣：噪音等級 × 殘響（2026-08-02）
#
# 動機：SEMICON Taiwan 2026（9/2-9/4，南港展覽館 1、2 館，
#   1300 展商／4300 攤位／10 萬人次）。**機器會提前交給客戶**，
#   沒有現場調整的機會 ⇒ 所有信心都要在交機前建立。
#
# 現有數據只涵蓋「噪音」單一變數（light 27.1dB / heavy 22.7dB）。
# 展場還有兩個沒測過的因素：
#   ① **殘響**——南港館挑高＋硬地板，業界文獻明指
#      "30-foot ceilings and hard floors create long reverberation that
#       buries speech"。這是全新變數。
#   ② 尖峰人流（10 萬人次 ÷ 3 天）→ 噪音可能低於 heavy。
#
# ⚠️ SEMICON 是 **B2B 專業展**：噪音以人聲交談為主，
#   **沒有 show girl 麥克風/促銷廣播/背景音樂**（那是消費展）
#   ⇒ mall_ambience（真實人潮交談）的特性正好對得上，
#     故噪音階梯只測到 18dB，不做更極端的 16dB 以下。
#
# 殘響參數：用 aecho + 音量補償（**不可只用 aecho**——它把能量分散到
#   回音尾巴，實測 -22.6→-35.2dB，等於同時削弱人聲，會高估劣化）。
#
# 用法（RPI5 ~/voice_poc）：bash venue_matrix.sh [起始句] [結束句]
set -uo pipefail
cd "$(dirname "$0")"

START="${1:-1}"
END="${2:-100}"
API="https://127.0.0.1:8002/api/asr"
NOISE="noise/mall_ambience.mp3"
CORPUS="read100_en.txt"
OUT="_venue_matrix.txt"
: > "$OUT"

WS_HELPER="_vm_ws.py"
cat > "$WS_HELPER" <<'PYEOF'
import asyncio, json, ssl, sys, websockets
async def go(text):
    c = ssl.create_default_context(); c.check_hostname=False; c.verify_mode=ssl.CERT_NONE
    async with websockets.connect('wss://localhost:8002/ws?fast=1', ssl=c) as ws:
        await ws.send(json.dumps({'type':'chat','text':text}, ensure_ascii=False))
        while True:
            o = json.loads(await asyncio.wait_for(ws.recv(), 90))
            if o.get('type') == 'done':
                r = o.get('result') or {}
                print(json.dumps({'view': r.get('view') or '',
                                  'summary': (r.get('summary') or '').replace('\n',' ')},
                                 ensure_ascii=False))
                return
asyncio.run(go(sys.argv[1]))
PYEOF

pick_audio() {
    local n="$1"
    if [ -f "audio/user_clean_en_vad/${n}.wav" ]; then
        echo "audio/user_clean_en_vad/${n}.wav"
    elif [ -f "audio/user_clean_en/${n}.wav" ]; then
        echo "audio/user_clean_en/${n}.wav"
    else
        echo ""
    fi
}

# 條件矩陣：名稱|噪音dB(空=無)|殘響filter(空=無)
# 噪音劣化曲線（**殘響已評估為低風險、不再測**，見記憶 en_voice_noise_findings）
#   目的：找出「多吵才會出事」的崩潰點，讓交機時能講出安全邊界。
#   -30dB=27.1dB SNR（展場一般）／-22dB=22.7dB（尖峰）／再往下延伸
CONDS=(
  "27.1dB 展場一般|-30|"
  "25.2dB 偏吵|-26|"
  "22.7dB 尖峰|-22|"
  "19.7dB 很吵|-18|"
  "15.7dB 極吵|-14|"
)

mapfile -t LINES < "$CORPUS"

for cond in "${CONDS[@]}"; do
    CNAME="${cond%%|*}"
    rest="${cond#*|}"
    CDB="${rest%%|*}"
    CRV="${rest#*|}"
    OK=0; N=0
    echo "── $CNAME ────────────────────────────"
    for LINE in "${LINES[@]}"; do
        case "$LINE" in ''|'#'*) continue ;; esac
        IFS='|' read -r NUM SENT WANT KW ZH <<< "$LINE"
        [[ "$NUM" =~ ^[0-9]+$ ]] || continue
        [ "$NUM" -lt "$START" ] 2>/dev/null && continue
        [ "$NUM" -gt "$END" ] 2>/dev/null && break
        SRCF=$(pick_audio "$NUM"); [ -z "$SRCF" ] && continue

        MIX=$(mktemp /tmp/vmXXXX.wav)
        DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$SRCF" 2>/dev/null)
        # ⚠️ 殘響必須**人聲與噪音各自套用後才混合**——
        #   混合後再加殘響 = 噪音也被加了回音拖尾疊在人聲上，
        #   實測辨識崩壞（`north received 50 wireless mouse`
        #   → `north receipt 15 whereas miles`），會嚴重高估殘響的傷害。
        #   真實空間裡人聲和噪音是各自產生殘響的。
        if [ -n "$CRV" ]; then
            FC="[0:a]${CRV}[sp];[1:a]atrim=0:${DUR:-5},aresample=16000,${CRV%,*},volume=${CDB}dB[bg];[sp][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,aresample=16000"
        else
            FC="[1:a]atrim=0:${DUR:-5},aresample=16000,volume=${CDB}dB[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,aresample=16000"
        fi
        ffmpeg -y -i "$SRCF" -i "$NOISE" -filter_complex "$FC" \
               -ac 1 -c:a pcm_s16le "$MIX" 2>/dev/null || { rm -f "$MIX"; continue; }

        ASR=$(curl -sk -m 120 -X POST "$API" --data-binary "@$MIX" \
              -H 'Content-Type: application/octet-stream' \
              | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('text','') if d.get('ok') else '')" 2>/dev/null)
        rm -f "$MIX"
        N=$((N+1))
        [ -z "$ASR" ] && continue

        RES=$(python3 "$WS_HELPER" "$ASR" 2>/dev/null)
        VIEW=$(echo "$RES" | python3 -c "import sys,json; print(json.load(sys.stdin).get('view',''))" 2>/dev/null)
        SUMM=$(echo "$RES" | python3 -c "import sys,json; print(json.load(sys.stdin).get('summary','')[:70])" 2>/dev/null)
        HIT=1
        if [ "$WANT" = "*" ]; then
            { [ "$VIEW" = "error" ] || [ -z "$VIEW" ]; } && HIT=0
        else
            echo "$VIEW" | grep -q "$WANT" || HIT=0
        fi
        [ -n "$KW" ] && [ "$HIT" = "1" ] && { echo "$SUMM" | grep -q "$KW" || HIT=0; }
        [ "$HIT" = "1" ] && OK=$((OK+1))
        printf '.'
    done
    echo
    PCT=$(awk "BEGIN{printf \"%d\", $OK*100/($N>0?$N:1)}")
    echo "$CNAME|$OK|$N|$PCT" >> "$OUT"
    echo "   → $OK/$N = ${PCT}%"
done

rm -f "$WS_HELPER"
echo
echo "=========================================="
echo "展場條件矩陣（$START-$END 句）"
printf '%-20s %s\n' "條件" "通過率"
echo "------------------------------------------"
while IFS='|' read -r nm ok n pct; do
    printf '%-20s %3s/%3s = %3s%%\n' "$nm" "$ok" "$n" "$pct"
done < "$OUT"
echo "=========================================="
