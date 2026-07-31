#!/usr/bin/env bash
# rec_vad.sh — 用 sox 模擬**網頁前端的 VAD 錄音**（講完自動停，不用固定秒數）
#
# 為什麼要這支：read100_en.sh 錄固定 5 秒，但展場訪客走的是網頁路徑——
# 前端用 Web Audio API 算 RMS，**偵測到靜音 1.2 秒就自動送出**。
# 兩者的差別會影響測試結果：
#   ① 固定 5 秒：句子前後帶大量靜音，且訪客得等滿 5 秒
#   ② VAD：講完就送，音檔長度＝實際語音長度（更接近展場體感）
# 這支讓錄音行為與網頁一致，順便驗證「語音擷取的區段對不對」。
#
# 參數對齊 templates/index.html 的 startVAD()：
#   SILENCE = 0.015 (RMS)  → sox 的 -35dB 門檻（實測相近）
#   HANG    = 1200 ms      → 靜音 1.2 秒收尾
#   MAXLEN  = 12000 ms     → 硬上限 12 秒（配合 whisper -ac 640 的 12.8s 容量）
#
# 用法：
#   bash rec_vad.sh out.wav        錄一段（講完自動停）
#   bash rec_vad.sh out.wav 8      自訂硬上限 8 秒
set -uo pipefail

OUT="${1:-/tmp/vad_rec.wav}"
MAXSEC="${2:-12}"

# sox silence 參數說明：
#   1 0.1 3%    → 前導：偵測到 >3% 音量才開始錄（去掉開頭靜音）
#   1 1.2 3%    → 結尾：靜音持續 1.2 秒就停（＝前端的 HANG）
#   trim 0 MAX  → 硬上限
rec -q -r 16000 -c 1 -b 16 "$OUT" \
    silence 1 0.1 3% 1 1.2 3% \
    trim 0 "$MAXSEC" 2>/dev/null

if [ ! -f "$OUT" ]; then
    echo "❌ 錄音失敗（麥克風沒接？被佔用？）" >&2
    exit 1
fi

DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT" 2>/dev/null)
MEAN=$(ffmpeg -i "$OUT" -af volumedetect -f null /dev/null 2>&1 \
       | grep mean_volume | sed -E 's/.*mean_volume: (-?[0-9.]+).*/\1/')
printf "  ⏱ %.1fs  🔊 %s dB\n" "${DUR:-0}" "${MEAN:-?}" >&2
