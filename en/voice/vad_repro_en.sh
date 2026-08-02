#!/usr/bin/env bash
# vad_repro_en.sh — 把「固定 5 秒」錄的真人音檔補做 VAD 裁切後重跑判定。
#
# 背景：第 1-14 句是舊版 read100_en.sh（VAD 三個 bug 未修前）錄的，
#   固定錄滿 5 秒、句尾拖 1.5-2.6s 靜音，且判定行從未寫進 _read100_en_result.txt。
#   已量測確認 14 句人聲全部完整（最晚結束 3.44s）＝聲音是好的，只差後製。
#   ⇒ 不重錄，改用與修好版 record_once() 相同的裁切 + 同一套判定規則補跑。
#
# 裁切參數刻意與 read100_en.sh 對齊（尾端 silenceremove -42dB / 保留 0.3s）。
# 額外裁前導靜音：舊檔開頭有 0.34-1.32s 空白（按 Enter 到開口的反應時間），
#   修好版是「偵測到說話才算開始」，等效於前面不留長空白。
#
# 用法（RPI5 ~/voice_poc）：
#   bash vad_repro_en.sh 1 14            # 產生結果，寫 _vad_repro_result.txt
#   KEEP=1 bash vad_repro_en.sh 1 14     # 同時把裁好的檔覆蓋回 user_clean_en/
set -uo pipefail
cd "$(dirname "$0")"

START="${1:-1}"
END="${2:-14}"
SRC="audio/user_clean_en"
OUTDIR="audio/user_clean_en_vad"
OUT="_vad_repro_result.txt"
API="https://127.0.0.1:8002/api/asr"
CORPUS="read100_en.txt"
KEEP="${KEEP:-0}"

mkdir -p "$OUTDIR"
: > "$OUT"

WS_HELPER="_vad_repro_ws.py"
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

mapfile -t LINES < "$CORPUS"
OK=0; BAD=0; N=0

for LINE in "${LINES[@]}"; do
    case "$LINE" in ''|'#'*) continue ;; esac
    IFS='|' read -r NUM SENT WANT KW ZH <<< "$LINE"
    [[ "$NUM" =~ ^[0-9]+$ ]] || continue
    [ "$NUM" -lt "$START" ] 2>/dev/null && continue
    [ "$NUM" -gt "$END" ] 2>/dev/null && break

    SRCF="$SRC/${NUM}.wav"
    [ -f "$SRCF" ] || { echo "$NUM|$SENT|(無音檔)||SKIP" >> "$OUT"; continue; }

    CUT="$OUTDIR/${NUM}.wav"
    # 前後靜音都裁：areverse 兩次夾住尾端，前端直接 silenceremove
    #   -42dB / 0.3s 與 read100_en.sh 的尾端裁切一致
    ffmpeg -y -loglevel error -i "$SRCF" \
        -af "silenceremove=start_periods=1:start_threshold=-42dB:start_silence=0.3,areverse,silenceremove=start_periods=1:start_threshold=-42dB:start_silence=0.3,areverse" \
        "$CUT" 2>/dev/null || cp "$SRCF" "$CUT"

    _cut=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$CUT" 2>/dev/null)
    if [ -z "$_cut" ] || awk "BEGIN{exit !(${_cut:-0} < 0.3)}" 2>/dev/null; then
        cp "$SRCF" "$CUT"
        _cut=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$CUT" 2>/dev/null)
    fi
    _orig=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$SRCF" 2>/dev/null)

    ASR=$(curl -sk -m 120 -X POST "$API" --data-binary "@$CUT" \
          -H 'Content-Type: application/octet-stream' \
          | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('text','') if d.get('ok') else '')" 2>/dev/null)

    printf "[%s/%s] %.1fs→%.1fs | %s\n" "$NUM" "$END" "${_orig:-0}" "${_cut:-0}" "$SENT"

    if [ -z "$ASR" ]; then
        echo "   ❌ ASR 無輸出"
        echo "$NUM|$SENT|ASR空||FAIL" >> "$OUT"
        BAD=$((BAD+1)); N=$((N+1)); continue
    fi

    RES=$(python3 "$WS_HELPER" "$ASR" 2>/dev/null)
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

    MARK=""
    [ "$ASR" != "$SENT" ] && MARK="  [聽成：$ASR]"
    if [ "$HIT" = "1" ]; then
        echo "   ✅ $VIEW$MARK"
        echo "$NUM|$SENT|$ASR|$VIEW|PASS" >> "$OUT"
        OK=$((OK+1))
    else
        echo "   ❌ $VIEW（期望 $WANT）$MARK"
        echo "      回答：$SUMM"
        echo "$NUM|$SENT|$ASR|$VIEW|FAIL" >> "$OUT"
        BAD=$((BAD+1))
    fi
    N=$((N+1))
done

rm -f "$WS_HELPER"
[ "$KEEP" = "1" ] && for i in $(seq "$START" "$END"); do
    [ -f "$OUTDIR/$i.wav" ] && cp "$OUTDIR/$i.wav" "$SRC/$i.wav"
done

echo ""
echo "=========================================="
echo "補跑 $N 句：通過 $OK、未過 $BAD"
[ "$N" -gt 0 ] && echo "通過率 $(awk "BEGIN{printf \"%.0f\", $OK/$N*100}")%"
echo "結果：$OUT"
echo "=========================================="
