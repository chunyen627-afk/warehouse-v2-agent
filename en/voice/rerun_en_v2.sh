#!/usr/bin/env bash
# rerun_en.sh — 拿**已錄好的**真人音檔重跑整條語音鏈，不用重錄。
#
# 為什麼需要這支：_read100_en_result.txt 存的是**錄音當下**的判定，
#   之後加了 _ASR_FIX_EN 容錯規則 → 舊結果沒有反映現在的系統行為
#   （實測第 22/25/31 句舊記錄是 FAIL，現在 ASR 端已輸出完全正確的文字）。
#   ⇒ 改規則後要重跑才知道真實通過率，否則會低估。
#
# 音檔來源：audio/user_clean_en/NN.wav（read100_en.sh 存的最終版＝user 按 y 那次）
#   第 1-14 句已由 vad_repro_en.sh 後製裁切，存 audio/user_clean_en_vad/
#
# 混噪：與 read100_en.sh / noise_retest_en.sh 同參數
#   light = -18dB（一般展場）／heavy = -8dB（尖峰吵雜）＋ aecho 輕混響
#
# 用法（RPI5 ~/voice_poc）：
#   bash rerun_en.sh 1 38              # 乾淨重跑
#   bash rerun_en.sh 1 38 light        # 混 light 噪音
#   bash rerun_en.sh 1 38 heavy        # 混 heavy 噪音
set -uo pipefail
cd "$(dirname "$0")"

START="${1:-1}"
END="${2:-38}"
NOISE_LV="${3:-}"
CORPUS="read100_en.txt"
API="https://127.0.0.1:8002/api/asr"
NOISE="noise/mall_ambience.mp3"

SUF=""
[ -n "$NOISE_LV" ] && SUF="_${NOISE_LV}"
[ "${SRC:-human}" = "tts" ] && SUF="_tts${SUF}"
OUT="_rerun_en_v2${SUF}.txt"
: > "$OUT"

WS_HELPER="_rerun_en_ws.py"
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

# 音檔來源：human（user 真人）或 tts（Edge TTS US 腔合成音，003 位數命名）
#   SRC=tts 用來做**對照組**——真人 heavy 只剩 16%，要分辨那是
#   「混噪參數太狠」還是「真人聲在噪音下特別脆弱」，唯一方法是
#   拿同一套混噪參數跑合成音：TTS 也崩＝參數問題，TTS 撐住＝真人聲問題。
#   ⚠️ 英文 TTS **從未跑過混噪**（_noise_retest_*.txt 裡是中文版數據），
#     先前「TTS 92%」是乾淨環境值，拿它對照噪音結果並不成立。
SRC_KIND="${SRC:-human}"

pick_audio() {
    local n="$1"
    if [ "$SRC_KIND" = "tts" ]; then
        local p
        p=$(printf "audio/read100_en_demo/%03d.mp3" "$n")
        [ -f "$p" ] && echo "$p" || echo ""
        return
    fi
    if [ -f "audio/user_clean_en_vad/${n}.wav" ]; then
        echo "audio/user_clean_en_vad/${n}.wav"
    elif [ -f "audio/user_clean_en/${n}.wav" ]; then
        echo "audio/user_clean_en/${n}.wav"
    else
        echo ""
    fi
}

mapfile -t LINES < "$CORPUS"
OK=0; BAD=0; N=0

echo "=========================================="
echo "重跑音檔 $START-$END 句　來源：${SRC_KIND}　環境：${NOISE_LV:-clean}"
echo "=========================================="

for LINE in "${LINES[@]}"; do
    case "$LINE" in ''|'#'*) continue ;; esac
    IFS='|' read -r NUM SENT WANT KW ZH <<< "$LINE"
    [[ "$NUM" =~ ^[0-9]+$ ]] || continue
    [ "$NUM" -lt "$START" ] 2>/dev/null && continue
    [ "$NUM" -gt "$END" ] 2>/dev/null && break

    SRCF=$(pick_audio "$NUM")
    if [ -z "$SRCF" ]; then
        echo "$NUM|$SENT|(無音檔)||SKIP" >> "$OUT"
        continue
    fi

    SEND="$SRCF"
    MIX=""
    if [ -n "$NOISE_LV" ] && [ -f "$NOISE" ]; then
        # ── 2026-08-02 重新校準（原 -18/-8 過度嚴苛）────────────────
        #   拿「user 家手機播人潮音」的真實錄音當基準（C930c 實測）：
        #     安靜 36.6dB／手機50% 35.3dB／手機100%(user 覺得蠻吵) 31.5dB
        #   舊參數換算 SNR：light(-18)=19.7dB、heavy(-8)=10.4dB
        #   ⇒ **舊 light 比 user 家最大音量還嚴苛 12dB**、heavy 差 21dB
        #     （12dB≈「一般交談」到「得用喊的」的差距）＝測的不是展場。
        #   新對應：-30dB→27.1dB(展場一般)／-22dB→22.7dB(尖峰)
        DB=$([ "$NOISE_LV" = "heavy" ] && echo "-22" || echo "-30")
        DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$SRCF" 2>/dev/null)
        MIX=$(mktemp /tmp/rrXXXX.wav)
        # ⚠️ **必須帶 normalize=0**：amix 預設把每路各衰減 1/n，
        #   兩路輸入＝人聲被砍掉約 12dB。實測初版（無 normalize=0）
        #   混完 mean_volume 從 -22.6 掉到 -36.0，而 light(-18) 與
        #   heavy(-8) 混完只差 0.5dB（-36.0 vs -35.5）＝**噪音大小根本沒差別**，
        #   測到的是「人聲被壓小」不是「展場有噪音」。
        #   假數據：light 47% / heavy 8%（真值見修正後重測）。
        ffmpeg -y -i "$SRCF" -i "$NOISE" -filter_complex \
            "[1:a]atrim=0:${DUR:-5},aresample=16000,volume=${DB}dB[bg];\
             [0:a][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,\
             aresample=16000" \
            -ac 1 -c:a pcm_s16le "$MIX" 2>/dev/null && SEND="$MIX"
    fi

    ASR=$(curl -sk -m 120 -X POST "$API" --data-binary "@$SEND" \
          -H 'Content-Type: application/octet-stream' \
          | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('text','') if d.get('ok') else '')" 2>/dev/null)
    [ -n "$MIX" ] && rm -f "$MIX"

    if [ -z "$ASR" ]; then
        echo "[$NUM] ❌ ASR 無輸出 | $SENT"
        echo "$NUM|$SENT|ASR空||FAIL" >> "$OUT"
        BAD=$((BAD+1)); N=$((N+1)); continue
    fi

    RES=$(python3 "$WS_HELPER" "$ASR" 2>/dev/null)
    VIEW=$(echo "$RES" | python3 -c "import sys,json; print(json.load(sys.stdin).get('view',''))" 2>/dev/null)
    SUMM=$(echo "$RES" | python3 -c "import sys,json; print(json.load(sys.stdin).get('summary','')[:70])" 2>/dev/null)

    HIT=1
    if [ "$WANT" = "*" ]; then
        { [ "$VIEW" = "error" ] || [ -z "$VIEW" ]; } && HIT=0
    else
        echo "$VIEW" | grep -q "$WANT" || HIT=0
    fi
    if [ -n "$KW" ] && [ "$HIT" = "1" ]; then
        echo "$SUMM" | grep -q "$KW" || HIT=0
    fi

    MARK=""
    [ "$ASR" != "$SENT" ] && MARK="  [聽成：$ASR]"
    if [ "$HIT" = "1" ]; then
        echo "[$NUM] ✅ $VIEW$MARK"
        echo "$NUM|$SENT|$ASR|$VIEW|PASS" >> "$OUT"
        OK=$((OK+1))
    else
        echo "[$NUM] ❌ $VIEW（期望 $WANT）$MARK"
        echo "        回答：$SUMM"
        echo "$NUM|$SENT|$ASR|$VIEW|FAIL" >> "$OUT"
        BAD=$((BAD+1))
    fi
    N=$((N+1))
done

rm -f "$WS_HELPER"
echo ""
echo "=========================================="
echo "來源 ${SRC_KIND}｜環境 ${NOISE_LV:-clean}｜共 $N 句：通過 $OK、未過 $BAD"
[ "$N" -gt 0 ] && echo "通過率 $(awk "BEGIN{printf \"%.0f\", $OK/$N*100}")%"
echo "結果：$OUT"
echo "=========================================="
