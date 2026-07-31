#!/usr/bin/env bash
# read100_en.sh — 真人唸 100 句**英文**，跑完整語音鏈並自動判定。
#   （改自中文版 read100.sh：語料換英文、port 8001→8002）
#
# ⚠️ 不用刻意模仿標準腔——展場訪客本來就不是母語者，
#    非母語腔 + 吵雜環境才是我們要測的真實下限。
#    不確定怎麼唸：先在本機 `python practice_en.py` 跟讀示範音檔。
#
# 為什麼要真人唸：先前所有語音數據都來自 Edge TTS 合成音——沒有口音、
# 沒有語速變化、沒有真實麥克風特性。真人聲才測得出展場實況
# （真人實測已抓到合成音從未產生的錯法，如「北倉」→「北藏」）。
#
# 用法（RPI5 ~/voice_poc）：
#   bash read100_en.sh              從第 1 句開始，乾淨環境
#   bash read100_en.sh 30           從第 30 句續錄（中斷不用重來）
#   bash read100_en.sh 1 heavy      混入賣場人潮噪音，模擬展場
#   bash read100_en.sh 21 40        只錄 21-40 句（B 段寫入句）
#   bash read100_en.sh 5 5 x3       第 5 句連錄 3 次，比對錯法穩不穩定
#                                （診斷「該換模型還是換麥克風」）
#
# 操作：看到句子 → 按 Enter → 「開始錄音」後才出聲 → 錄 4 秒 → 顯示結果。
#       中途按 Ctrl+C 可停，結果已存 _read100_result.txt
#
# ⚠️ 修正（2026-07-20）：舊版 read 從語料檔讀 stdin，導致「不按 Enter 也
#    往下衝、來不及錄」。已改成互動一律走 /dev/tty，語料先讀進陣列。
set -uo pipefail
cd "$(dirname "$0")"

START="${1:-1}"
ARG2="${2:-}"
ARG3="${3:-}"
END=100
NOISE_LV=""
REPEAT=1
CORPUS="read100_en.txt"

# --fails：只測前兩輪失敗的句子（retest_fails.txt）
if [ "$START" = "--fails" ]; then
    CORPUS="retest_fails_en.txt"
    START=1
    # --fails 後面第一個參數才是噪音層次
    ARG2="$ARG3"
    ARG3="${4:-}"
fi

# 第二參數：噪音層次 或 結束句號
case "$ARG2" in
    ""|clean) ;;
    light|heavy) NOISE_LV="$ARG2" ;;
    *[0-9]*) END="$ARG2" ;;
esac
# 第三參數：xN = 每句連錄 N 次（診斷模式）
case "$ARG3" in
    x[0-9]*) REPEAT="${ARG3#x}" ;;
esac

SEC=5
# ── VAD 模式（2026-07-31 新增，預設開啟）─────────────────────────
#   VAD=1：模擬**網頁前端**的錄音行為——講完靜音 1.2s 自動停，不用等滿 5 秒。
#          參數對齊 templates/index.html 的 startVAD()（HANG=1200/MAXLEN=12000）。
#          好處：①測起來跟展場訪客走的路徑一致 ②順便驗證語音擷取區段對不對
#          ③音檔長度＝實際語音長度，不會前後帶一堆靜音
#   VAD=0：舊行為（arecord 固定錄 SEC 秒）
#   用法：VAD=0 bash read100_en.sh   → 切回固定秒數
VAD="${VAD:-1}"
MAXSEC=12   # 硬上限，配合 whisper -ac 640 的 12.8s 容量
NOISE="noise/mall_ambience.mp3"
API="https://127.0.0.1:8002/api/asr"
WS_HELPER="_read100_en_ws.py"
RUN_TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
RUN_DIR="runs/${RUN_TIMESTAMP}"
mkdir -p "${RUN_DIR}/audio"
OUT="${RUN_DIR}/result.log"

MAIN_OUT="_read100_en_result.txt"
[ "$START" = "1" ] && : > "$MAIN_OUT"

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

# 錄一次音 → 回 ASR 文字（透過全域變數 REC_ASR）。混噪在此統一處理。
#   $1 = 句號（給存檔用）。乾淨錄音存 audio/user_clean_en/NN.wav，
#   ⚠️ 路徑刻意與中文版 user_clean/ 分開——那 100 句中文真人音是
#      不可重現的資產，絕不能被英文錄音蓋掉。
#   讓「念一次乾淨版」後可事後自動混噪（light/heavy）重測，不用重念。
record_once() {
    local RAW MIX SEND DB _num="${1:-}"
    RAW=$(mktemp /tmp/r100XXXX.wav)
    if [ "$VAD" = "1" ]; then
        # ⚠️ 不用 sox 的 silence 等前導——實測在安靜房間會**無限等待**
        #   （timeout 124、0 bytes 檔案）或判定無內容不寫檔。改成：
        #   arecord 錄滿上限（行為可預期）→ ffmpeg 裁掉尾端靜音。
        #   結果等同前端 VAD：音檔長度＝實際語音長度。
        _rawfull=$(mktemp /tmp/r100fXXXX.wav)
        arecord -f S16_LE -r 16000 -c 1 -d "$MAXSEC" "$_rawfull" 2>/dev/null &
        _arec_pid=$!
        # 邊錄邊偵測：每 0.3s 取樣，連續 1.2s 低於門檻就提早結束
        _quiet=0
        for _i in $(seq 1 $((MAXSEC * 10 / 3))); do
            sleep 0.3
            kill -0 $_arec_pid 2>/dev/null || break
            _sz=$(stat -c%s "$_rawfull" 2>/dev/null || echo 0)
            [ "$_sz" -lt 16000 ] && continue    # 還沒錄到 0.5s，先不判
            _lvl=$(timeout 2 ffmpeg -sseof -0.35 -i "$_rawfull" -af volumedetect -f null /dev/null 2>&1                    | grep mean_volume | sed -E 's/.*mean_volume: (-?[0-9.]+).*//')
            if [ -n "$_lvl" ] && awk "BEGIN{exit !($_lvl < -42)}" 2>/dev/null; then
                _quiet=$((_quiet + 1))
                [ "$_quiet" -ge 4 ] && { kill $_arec_pid 2>/dev/null; break; }
            else
                _quiet=0
            fi
        done
        wait $_arec_pid 2>/dev/null
        # 裁掉尾端靜音（保留 0.3s 尾巴，避免切掉最後一個音節）
        ffmpeg -y -loglevel error -i "$_rawfull"             -af "areverse,silenceremove=start_periods=1:start_threshold=-42dB:start_silence=0.3,areverse"             "$RAW" 2>/dev/null || cp "$_rawfull" "$RAW"
        # ⚠️ 全靜音（訪客按了沒講話）裁切後會是**空檔**——退回原始檔，
        #   讓下游 ASR 正常回「聽不出內容」，而不是拿空檔去辨識。
        _cut=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$RAW" 2>/dev/null)
        if [ -z "$_cut" ] || awk "BEGIN{exit !(${_cut:-0} < 0.3)}" 2>/dev/null; then
            cp "$_rawfull" "$RAW"
            printf "     [VAD] ⚠️ 沒偵測到語音（沒講話？太小聲？）
" > /dev/tty
        fi
        rm -f "$_rawfull"
        _vdur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$RAW" 2>/dev/null)
        printf "     [VAD] 錄到 %.1fs（講完自動停）
" "${_vdur:-0}" > /dev/tty
    else
        arecord -f S16_LE -r 16000 -c 1 -d "$SEC" "$RAW" 2>/dev/null
    fi
    # 每次都保存原始錄音到本次 run 的資料夾
    if [ -n "$_num" ]; then
        cp "$RAW" "${RUN_DIR}/audio/${_num}_raw.wav"
    fi
    # 保存乾淨錄音（只在非混噪模式存，避免存到已混噪的）
    if [ -z "$NOISE_LV" ] && [ -n "$_num" ]; then
        mkdir -p audio/user_clean_en
        cp "$RAW" "audio/user_clean_en/${_num}.wav"
    fi
    SEND="$RAW"
    if [ -n "$NOISE_LV" ] && [ -f "$NOISE" ]; then
        DB=$([ "$NOISE_LV" = "heavy" ] && echo "-8" || echo "-18")
        MIX=$(mktemp /tmp/r100mXXXX.wav)
        ffmpeg -y -i "$RAW" -i "$NOISE" -filter_complex \
            "[1:a]atrim=0:${SEC},volume=${DB}dB,aresample=16000[bg];\
             [0:a][bg]amix=inputs=2:duration=first:dropout_transition=0,\
             aecho=0.8:0.7:6:0.15,aresample=16000" \
            -ac 1 -c:a pcm_s16le "$MIX" 2>/dev/null && SEND="$MIX"
        # 保存混入噪音後的音檔
        if [ -n "$_num" ]; then
            cp "$MIX" "${RUN_DIR}/audio/${_num}_mixed.wav"
        fi
    fi
    # 量錄音音量並回饋——小聲是辨識失敗的常見隱因（訊噪比低、摩擦音糊掉）。
    #   讓使用者即時看到夠不夠大聲，排除「音量太小」這個變數。
    REC_MEAN=$(ffmpeg -i "$RAW" -af volumedetect -f null /dev/null 2>&1 \
               | grep mean_volume | sed -E 's/.*mean_volume: (-?[0-9.]+).*/\1/')
    if [ -n "$REC_MEAN" ]; then
        if awk "BEGIN{exit !($REC_MEAN < -30)}" 2>/dev/null; then
            printf "     🔈 音量 %s dB ⚠️ 偏小（正常講話約 -12~-6）——大聲點會更好認\n" "$REC_MEAN" > /dev/tty
        else
            printf "     🔊 音量 %s dB（充足）\n" "$REC_MEAN" > /dev/tty
        fi
    fi
    REC_ASR=$(curl -sk -m 120 -X POST "$API" --data-binary "@$SEND" \
          -H 'Content-Type: application/octet-stream' \
          | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('text','') if d.get('ok') else '')" 2>/dev/null)
    rm -f "$RAW"; [ "$SEND" != "$RAW" ] && rm -f "$SEND"
}

echo "=========================================================="
echo "真人語音測試 100 句（英文版 · 8002）"
[ -n "$NOISE_LV" ] && echo "噪音層次：$NOISE_LV（模擬展場）" || echo "環境：乾淨"
[ "$REPEAT" -gt 1 ] && echo "診斷模式：每句連錄 $REPEAT 次"
echo "從第 $START 句到第 $END 句"
echo "=========================================================="
echo "提示：看到「🔴 開始錄音」再出聲，正常語速正常音量。"
echo ""

# 語料先讀進陣列（不佔 stdin），互動全走 /dev/tty
mapfile -t LINES < "$CORPUS"

OK=0; BAD=0; N=0

for LINE in "${LINES[@]}"; do
    case "$LINE" in ''|'#'*) continue ;; esac
    # 第 5 欄 ZH = 中文意思（只顯示給人看，**不會**送進系統——
    #   英文版後端擋中文，混進查詢字串會整句被 reject）
    IFS='|' read -r NUM SENT WANT KW ZH <<< "$LINE"
    
    # 確保 NUM 是純數字，過濾掉含有 BOM (Byte Order Mark) 或格式錯誤的行
    if ! [[ "$NUM" =~ ^[0-9]+$ ]]; then
        continue
    fi
    
    [ "$NUM" -lt "$START" ] 2>/dev/null && continue
    [ "$NUM" -gt "$END" ] 2>/dev/null && break

    echo "----------------------------------------------------------"
    echo "[$NUM/100] 請唸：$SENT"
    [ -n "${ZH:-}" ] && echo "          （中文意思：$ZH）"

    while true; do
        printf "按 Enter 準備錄音..." > /dev/tty
        # ⚠️ 關鍵修正：從終端機讀，不是從語料檔；先清 stdin 殘留
        read -r < /dev/tty
        # 倒數，讓使用者反應過來
        printf "\r  🔴 開始錄音（%s 秒）——請唸！          \n" "$SEC" > /dev/tty

        record_once "$NUM"   # 設定 REC_ASR，並存乾淨錄音供事後混噪

        if [ -z "$REC_ASR" ]; then
            echo "   ❌ ASR 無輸出（音量太小？沒對準錄音時機？）"
        else
            MARK=""
            [ "$REC_ASR" != "$SENT" ] && MARK="  [聽成：$REC_ASR]"

            # 正常模式：送倉管判定
            RES=$(python3 "$WS_HELPER" "$REC_ASR" 2>/dev/null)
            VIEW=$(echo "$RES" | python3 -c "import sys,json; print(json.load(sys.stdin).get('view',''))" 2>/dev/null)
            SUMM=$(echo "$RES" | python3 -c "import sys,json; print(json.load(sys.stdin).get('summary','')[:60])" 2>/dev/null)

            HIT=1
            if [ "$WANT" = "*" ]; then
                { [ "$VIEW" = "error" ] || [ -z "$VIEW" ]; } && HIT=0
            else
                echo "$VIEW" | grep -q "$WANT" || HIT=0
            fi
            if [ -n "$KW" ] && [ "$HIT" = "1" ]; then
                echo "$SUMM" | grep -q "$KW" || HIT=0
            fi

            if [ "$HIT" = "1" ]; then
                echo "   ✅ $VIEW$MARK"
            else
                echo "   ❌ $VIEW（期望 $WANT）$MARK"
                echo "      回答：$SUMM"
            fi
        fi

        printf "滿意這次結果嗎？(輸入 y 儲存並換下一題 / 直接按 Enter 重錄)：" > /dev/tty
        read -r SATISFIED < /dev/tty
        if [ "$SATISFIED" = "y" ] || [ "$SATISFIED" = "Y" ]; then
            # 使用者滿意，將最後一次結果存入 log
            if [ -z "$REC_ASR" ]; then
                echo "$NUM|$SENT|ASR空||FAIL|" >> "$OUT"
                echo "$NUM|$SENT|ASR空||FAIL" >> "$MAIN_OUT"
                BAD=$((BAD+1))
            elif [ "$HIT" = "1" ]; then
                echo "$NUM|$SENT|$REC_ASR|$VIEW|PASS|${REC_MEAN:-}" >> "$OUT"
                echo "$NUM|$SENT|$REC_ASR|$VIEW|PASS" >> "$MAIN_OUT"
                OK=$((OK+1))
            else
                echo "$NUM|$SENT|$REC_ASR|$VIEW|FAIL|${REC_MEAN:-}" >> "$OUT"
                echo "$NUM|$SENT|$REC_ASR|$VIEW|FAIL" >> "$MAIN_OUT"
                BAD=$((BAD+1))
            fi
            N=$((N+1))
            break # 進入下一題
        else
            echo "   🔁 重錄..."
        fi
    done


done

rm -f "$WS_HELPER"
echo ""
echo "=========================================================="
echo "本次錄了 $N 句：通過 $OK、未過 $BAD"
[ "$N" -gt 0 ] && echo "通過率 $(awk "BEGIN{printf \"%.0f\", $OK/$N*100}")%"
echo "完整結果：$OUT"
echo "=========================================================="
