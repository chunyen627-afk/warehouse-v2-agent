#!/bin/bash
# sync_audio.sh — 語音測試音檔的「傳上去測 → 拉回備份 → 清 RPI5」流程
#
# user 定調（2026-07-26）：RPI5 上測完就清、備份放 WIN 主機。
#   理由：RPI5 是展場機（磁碟給模型與資料用），音檔可由 gen_en_audio*.py 重現，
#   但重現要跑 TTS（Chirp 會吃 GCP 額度）→ 留一份在 WIN 最省。
#
# ⚠️ 音檔已在 .gitignore（不進 repo），所以 WIN 這份就是唯一備份。
#
# 用法：
#   bash sync_audio.sh push en_gcp     # WIN → RPI5（要測之前）
#   bash sync_audio.sh pull en_gcp     # RPI5 → WIN（測完拉回，含 bench 結果）
#   bash sync_audio.sh clean en_gcp    # 清掉 RPI5 上那份（拉回後才做）
#   bash sync_audio.sh status          # 兩邊各佔多少
set -u
RPI="p400@10.35.219.22"
KEY="$HOME/.ssh/rpi5_warehouse"
HERE="$(cd "$(dirname "$0")" && pwd)"
CMD="${1:-status}"
SET="${2:-en}"

case "$CMD" in
  push)
    echo "▶ 打包 $SET → RPI5"
    tar czf "/tmp/_aud_$SET.tgz" -C "$HERE/audio" "$SET" || exit 1
    scp -i "$KEY" "/tmp/_aud_$SET.tgz" "$RPI:/tmp/" >/dev/null
    ssh -i "$KEY" "$RPI" "mkdir -p ~/voice_poc/audio && tar xzf /tmp/_aud_$SET.tgz -C ~/voice_poc/audio && rm -f /tmp/_aud_$SET.tgz && find ~/voice_poc/audio/$SET -name '*.wav' | wc -l"
    rm -f "/tmp/_aud_$SET.tgz"
    ;;
  pull)
    echo "▶ 從 RPI5 拉回 $SET（含 bench 結果）"
    ssh -i "$KEY" "$RPI" "cd ~/voice_poc && tar czf /tmp/_back_$SET.tgz audio/$SET _bench_*$SET* 2>/dev/null; ls -la /tmp/_back_$SET.tgz | awk '{print \$5}'"
    scp -i "$KEY" "$RPI:/tmp/_back_$SET.tgz" "/tmp/" >/dev/null
    tar xzf "/tmp/_back_$SET.tgz" -C "$HERE" && echo "✓ 已還原到 $HERE"
    ssh -i "$KEY" "$RPI" "rm -f /tmp/_back_$SET.tgz"
    rm -f "/tmp/_back_$SET.tgz"
    ;;
  clean)
    echo "▶ 清 RPI5 上的 $SET（確認已 pull 回來再做）"
    ssh -i "$KEY" "$RPI" "du -sh ~/voice_poc/audio/$SET 2>/dev/null; rm -rf ~/voice_poc/audio/$SET && echo '已刪除'; df -h ~ | tail -1"
    ;;
  status)
    echo "── WIN ──"
    du -sh "$HERE/audio"/* 2>/dev/null
    echo "── RPI5 ──"
    ssh -i "$KEY" "$RPI" "du -sh ~/voice_poc/audio/* 2>/dev/null; echo '磁碟:'; df -h ~ | tail -1"
    ;;
  *)
    echo "用法: bash sync_audio.sh {push|pull|clean|status} [音檔集名]"; exit 1;;
esac
