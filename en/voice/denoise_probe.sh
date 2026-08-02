#!/usr/bin/env bash
# denoise_probe.sh — 測「送進 whisper 前先降噪」能救回多少句。
#
# 動機（2026-08-02 量測）：user 的錄音品質其實**優於** TTS
#   （幀內 SNR 30-39dB vs TTS 28-31dB、音量也不輸），
#   差別在**語音占比**：真人 26-35%、TTS 55-69%。
#   ⇒ user 詞間停頓多，音檔 65-74% 是空隙；噪音均勻鋪滿時間軸，
#     那些空隙裡噪音成為主要內容 → whisper 在空隙幻聽出
#     `boat courses because` / `well-nosed mouths` 這類無中生有的詞。
#   user 英文不流利、無法唸得連貫（也不該要求——展場訪客同樣不連貫），
#   ⇒ 改由**系統端前處理**解決，不要求使用者改變唸法。
#
# 現況：/api/asr 只做格式轉換（16k mono），**零前處理**。
#
# 候選（全部只加 ffmpeg -af，不動模型、不影響延遲上限 ~4s）：
#   A none      基準
#   B afftdn    FFT 降噪（估噪音底再減），配 highpass 去低頻隆隆聲
#   C +gapcut   B + 切掉詞間靜音（提高語音密度）
#   D +dynnorm  B + 動態正規化（拉平音量起伏）
#
# ⚠️ 單句測過 C 反而最差（切空隙破壞了 whisper 需要的節奏線索），
#   但需全批確認——單句下結論已經害我錯一次（-ac 那輪）。
set -uo pipefail
cd "$(dirname "$0")"

START="${1:-1}"
END="${2:-38}"
LV="${3:-light}"
DB=$([ "$LV" = "heavy" ] && echo "-8" || echo "-18")
M="$HOME/whisper.cpp/models/ggml-small-q5_0.bin"
C="$HOME/whisper.cpp/build/bin/whisper-cli"
NOISE="noise/mall_ambience.mp3"
OUT="_denoise_probe_${LV}.txt"
: > "$OUT"

declare -A FILTERS=(
  [A_none]=""
  [B_afftdn]="highpass=f=100,afftdn=nf=-25"
  [C_gapcut]="highpass=f=100,afftdn=nf=-25,silenceremove=stop_periods=-1:stop_duration=0.15:stop_threshold=-32dB"
  [D_dynnorm]="highpass=f=100,afftdn=nf=-25,dynaudnorm=f=150:g=15"
)
ORDER=(A_none B_afftdn C_gapcut D_dynnorm)

norm() { echo "$1" | tr 'A-Z' 'a-z' | sed "s/[^a-z0-9' ]/ /g" | tr -s ' ' | sed 's/^ //;s/ $//'; }

declare -A HIT
for k in "${ORDER[@]}"; do HIT[$k]=0; done
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
      -ac 1 -c:a pcm_s16le /tmp/dp_base.wav 2>/dev/null || continue

    EXP=$(norm "$SENT")
    LINEOUT="$NUM|$SENT"
    for k in "${ORDER[@]}"; do
        fl="${FILTERS[$k]}"
        if [ -z "$fl" ]; then
            cp /tmp/dp_base.wav /tmp/dp_x.wav
        else
            ffmpeg -y -i /tmp/dp_base.wav -af "$fl" -ar 16000 -ac 1 /tmp/dp_x.wav 2>/dev/null \
              || cp /tmp/dp_base.wav /tmp/dp_x.wav
        fi
        TXT=$("$C" -m "$M" -f /tmp/dp_x.wav -nt -l en -ac 640 2>/dev/null | tr '\n' ' ')
        G=$(norm "$TXT")
        # 判準：**商品名詞是否保住**——用整句相等太嚴，
        #   實際系統有容錯層，關鍵是關鍵詞有沒有被聽對。
        #   這裡用「期望句的詞有多少出現在辨識結果」當代理指標。
        okc=0; tot=0
        for w in $EXP; do
            tot=$((tot+1))
            case " $G " in *" $w "*) okc=$((okc+1));; esac
        done
        pct=$(awk "BEGIN{printf \"%d\", $okc*100/($tot>0?$tot:1)}")
        [ "$pct" -ge 80 ] && HIT[$k]=$(( ${HIT[$k]} + 1 ))
        LINEOUT="$LINEOUT|$k:${pct}%:$G"
    done
    echo "$LINEOUT" >> "$OUT"
    N=$((N+1))
    printf '.'
done
echo
echo "=============================================="
echo "環境 $LV｜$N 句｜詞命中率 >=80% 的句數："
for k in "${ORDER[@]}"; do
    printf '  %-12s %2d / %d  (%.0f%%)\n' "$k" "${HIT[$k]}" "$N" \
      "$(awk "BEGIN{print ${HIT[$k]}*100/($N>0?$N:1)}")"
done
echo "明細：$OUT"
echo "=============================================="
