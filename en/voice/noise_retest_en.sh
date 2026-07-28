#!/usr/bin/env bash
# noise_retest_en.sh — 拿「已存的乾淨錄音」自動混噪重測，不用重念。（英文版）
#   改自中文版 noise_retest.sh：語料換英文、port 8001→8002、
#   讀 audio/user_clean_en/（與中文的 user_clean/ 分開，那是不可重現資產）。
#
# 前提：先用 read100_en.sh 念過乾淨版（錄音存 audio/user_clean_en/NN.wav）。
# 這支對每個存檔自動混入賣場人潮噪音 → 送完整語音鏈 → 自動判定。
# 全自動、無互動——Claude 可直接跑，不需 user 在場。
#
# 用法（RPI5 ~/voice_poc）：
#   bash noise_retest_en.sh light      # 一般展場噪音 -18dB
#   bash noise_retest_en.sh heavy      # 尖峰吵雜 -8dB
set -uo pipefail
cd "$(dirname "$0")"

LV="${1:-heavy}"
DB=$([ "$LV" = "heavy" ] && echo "-8" || echo "-18")
NOISE="noise/mall_ambience.mp3"
CLEAN_DIR="audio/user_clean_en"
CORPUS="read100_en.txt"
API="https://127.0.0.1:8002/api/asr"
OUT="_noise_retest_en_${LV}.txt"

[ -d "$CLEAN_DIR" ] || { echo "❌ 找不到 $CLEAN_DIR——請先用 read100.sh 念乾淨版"; exit 1; }
N_CLEAN=$(ls "$CLEAN_DIR"/*.wav 2>/dev/null | wc -l)
[ "$N_CLEAN" -gt 0 ] || { echo "❌ $CLEAN_DIR 沒有錄音"; exit 1; }

: > "$OUT"
cat > _nr_ws.py <<'PYEOF'
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
                                  'summary': (r.get('summary') or '').replace('\n',' ')}, ensure_ascii=False))
                return
asyncio.run(go(sys.argv[1]))
PYEOF

# 讀語料的期望值（NN → view,kw）
declare -A WANT KW
while IFS='|' read -r num sent want kw; do
    case "$num" in ''|'#'*) continue ;; esac
    WANT[$num]="$want"; KW[$num]="$kw"
done < "$CORPUS"

echo "=========================================================="
echo "自動混噪重測（$LV, ${DB}dB）—— 用已存乾淨錄音，不用重念"
echo "共 $N_CLEAN 句乾淨錄音"
echo "=========================================================="

OK=0; BAD=0; N=0
for wav in $(ls "$CLEAN_DIR"/*.wav | sort -t/ -k3 -n); do
    num=$(basename "$wav" .wav)
    want="${WANT[$num]:-}"; kw="${KW[$num]:-}"
    [ -z "$want" ] && continue

    # 混噪（與 read100.sh 同參數：背景音量 + aecho 空間感）
    MIX=$(mktemp /tmp/nrXXXX.wav)
    dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$wav" 2>/dev/null | cut -d. -f1)
    dur=${dur:-4}
    ffmpeg -y -i "$wav" -i "$NOISE" -filter_complex \
        "[1:a]atrim=0:${dur},volume=${DB}dB,aresample=16000[bg];\
         [0:a][bg]amix=inputs=2:duration=first:dropout_transition=0,\
         aecho=0.8:0.7:6:0.15,aresample=16000" \
        -ac 1 -c:a pcm_s16le "$MIX" 2>/dev/null

    ASR=$(curl -sk -m 120 -X POST "$API" --data-binary "@$MIX" \
          -H 'Content-Type: application/octet-stream' \
          | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('text','') if d.get('ok') else '')" 2>/dev/null)
    rm -f "$MIX"

    if [ -z "$ASR" ]; then
        echo "[$num] ❌ ASR 空"; echo "$num|ASR空||FAIL" >> "$OUT"; BAD=$((BAD+1)); N=$((N+1)); continue
    fi

    RES=$(python3 _nr_ws.py "$ASR" 2>/dev/null)
    VIEW=$(echo "$RES" | python3 -c "import sys,json; print(json.load(sys.stdin).get('view',''))" 2>/dev/null)
    SUMM=$(echo "$RES" | python3 -c "import sys,json; print(json.load(sys.stdin).get('summary','')[:50])" 2>/dev/null)

    HIT=1
    if [ "$want" = "*" ]; then { [ "$VIEW" = "error" ] || [ -z "$VIEW" ]; } && HIT=0
    else echo "$VIEW" | grep -q "$want" || HIT=0; fi
    [ -n "$kw" ] && [ "$HIT" = "1" ] && { echo "$SUMM" | grep -q "$kw" || HIT=0; }

    if [ "$HIT" = "1" ]; then
        echo "[$num] ✅ $VIEW（聽成:$ASR）"; echo "$num|$ASR|$VIEW|PASS" >> "$OUT"; OK=$((OK+1))
    else
        echo "[$num] ❌ $VIEW（聽成:$ASR）"; echo "$num|$ASR|$VIEW|FAIL" >> "$OUT"; BAD=$((BAD+1))
    fi
    N=$((N+1))
done

rm -f _nr_ws.py
echo ""
echo "=========================================================="
echo "$LV 噪音下：通過 $OK / $N = $(awk "BEGIN{printf \"%.0f\", $OK/$N*100}")%"
echo "結果：$OUT"
echo "=========================================================="
