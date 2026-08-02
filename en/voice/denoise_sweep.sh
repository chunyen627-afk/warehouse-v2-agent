#!/usr/bin/env bash
# denoise_sweep.sh — 掃描降噪強度，找「light 逼近 clean」的最佳前處理。
#
# 第一輪（denoise_probe.sh）結論：
#   C_gapcut（切詞間靜音）**是災難**（詞命中 0-57%，整句被毀）
#     ⇒ whisper 需要詞間停頓當節奏/斷詞線索，切掉比留著更糟。
#     ⇒ 方向修正：不是「提高語音密度」，而是
#        **保留空隙、只把空隙裡的噪音壓下去**。
#   B_afftdn / D_dynnorm 略優於基準，且已有救回實例
#     （`power bank event hurry` → `power bank inventory`，66%→100%）
#   ⇒ 本輪只掃 afftdn 強度 nf（noise floor）與是否搭配 dynaudnorm。
#
# nf 越負＝降噪越保守；越接近 0＝越激進（但會吃掉齒音/摩擦音）。
# 目標：light 通過率逼近 clean 的 79%。
#
# ⚠️ 判準改用**真實系統判定**（跑 /api/asr 之外的完整鏈太慢，
#   這裡先用詞命中率當代理指標篩掉明顯壞的，最後贏家再跑完整 rerun_en.sh 驗證）。
set -uo pipefail
cd "$(dirname "$0")"

START="${1:-1}"
END="${2:-38}"
LV="${3:-light}"
DB=$([ "$LV" = "heavy" ] && echo "-8" || echo "-18")
M="$HOME/whisper.cpp/models/ggml-small-q5_0.bin"
C="$HOME/whisper.cpp/build/bin/whisper-cli"
NOISE="noise/mall_ambience.mp3"
OUT="_denoise_sweep_${LV}.txt"
: > "$OUT"

# 候選前處理：只動降噪強度，一律保留詞間空隙
declare -A FILTERS=(
  [base]=""
  [nf20]="highpass=f=100,afftdn=nf=-20"
  [nf25]="highpass=f=100,afftdn=nf=-25"
  [nf30]="highpass=f=100,afftdn=nf=-30"
  [nf25dyn]="highpass=f=100,afftdn=nf=-25,dynaudnorm=f=150:g=15"
  [nf20dyn]="highpass=f=100,afftdn=nf=-20,dynaudnorm=f=150:g=15"
)
ORDER=(base nf20 nf25 nf30 nf25dyn nf20dyn)

norm() { echo "$1" | tr 'A-Z' 'a-z' | sed "s/[^a-z0-9' ]/ /g" | tr -s ' ' | sed 's/^ //;s/ $//'; }

declare -A SUM
for k in "${ORDER[@]}"; do SUM[$k]=0; done
N=0

mapfile -t LINES < read100_en.txt
for LINE in "${LINES[@]}"; do
    case "$LINE" in ''|'#'*) continue ;; esac
    IFS='|' read -r NUM SENT WANT KW ZH <<< "$LINE"
    [[ "$NUM" =~ ^[0-9]+$ ]] || continue
    [ "$NUM" -lt "$START" ] 2>/dev/null && continue
    [ "$NUM" -gt "$END" ] 2>/dev/null && break

    f="audio/user_clean_en_vad/${NUM}.wav"
    [ -f "$f" ] || f="audio/user_clean_en/${NUM}.wav"
    [ -f "$f" ] || continue

    DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null)
    ffmpeg -y -i "$f" -i "$NOISE" -filter_complex \
      "[1:a]atrim=0:${DUR},aresample=16000,volume=${DB}dB[bg];\
       [0:a][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,\
       aecho=0.8:0.7:6:0.15,aresample=16000" \
      -ac 1 -c:a pcm_s16le /tmp/ds_base.wav 2>/dev/null || continue

    EXP=$(norm "$SENT")
    LINEOUT="$NUM|$SENT"
    for k in "${ORDER[@]}"; do
        fl="${FILTERS[$k]}"
        if [ -z "$fl" ]; then
            cp /tmp/ds_base.wav /tmp/ds_x.wav
        else
            ffmpeg -y -i /tmp/ds_base.wav -af "$fl" -ar 16000 -ac 1 /tmp/ds_x.wav 2>/dev/null \
              || cp /tmp/ds_base.wav /tmp/ds_x.wav
        fi
        TXT=$("$C" -m "$M" -f /tmp/ds_x.wav -nt -l en -ac 640 2>/dev/null | tr '\n' ' ')
        G=$(norm "$TXT")
        okc=0; tot=0
        for w in $EXP; do
            tot=$((tot+1))
            case " $G " in *" $w "*) okc=$((okc+1));; esac
        done
        pct=$(awk "BEGIN{printf \"%d\", $okc*100/($tot>0?$tot:1)}")
        SUM[$k]=$(( ${SUM[$k]} + pct ))
        LINEOUT="$LINEOUT|$k:${pct}:$G"
    done
    echo "$LINEOUT" >> "$OUT"
    N=$((N+1))
    printf '.'
done
echo
echo "=============================================="
echo "環境 $LV｜$N 句｜平均詞命中率："
for k in "${ORDER[@]}"; do
    printf '  %-10s %.1f%%\n' "$k" "$(awk "BEGIN{print ${SUM[$k]}/($N>0?$N:1)}")"
done
echo "明細：$OUT"
echo "=============================================="
