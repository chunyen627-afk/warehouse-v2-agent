#!/bin/bash
# make_restore_kit.sh — 產生 RPI5 重建包（2026-08-03 建立）
#
# 起因：user 問「RPI5 壞了怎麼快速重建」。盤點後發現**只備份 warehouse_v2/
#   是不夠的** —— 程式碼還原了，機器仍不會開機自啟、不會開 kiosk、
#   不會切熱點，因為系統層設定完全不在任何備份範圍內：
#     · systemd 3 個 service + override.conf（開機自啟、環境變數）
#     · 根目錄 8 支 .sh（kiosk 啟動 / 網路守護 / 語言切換 / 熱點 / ZeroTier）
#     · crontab 3 條（net_watchdog、health_watchdog、每小時清 lxterminal）
#     · ~/.config/autostart/warehouse.desktop（開機開瀏覽器）
#
# 這支只收「機器上獨有、重灌就沒了」的東西，**不含模型**
#   （模型 ~1G，另外用 scp 備份一次即可，不常變）。
#
# 用法：./make_restore_kit.sh          → ~/rpi5_restore_kit_YYYYMMDD.tar.gz
#       產生後 scp 回 Windows 保存。
set -u
TS=$(date +%Y%m%d_%H%M)
KIT=~/rpi5_restore_kit_$TS
mkdir -p "$KIT"/{systemd,scripts,config,env}

echo "① systemd unit 與 override"
sudo cp /etc/systemd/system/warehouse-v2.service        "$KIT/systemd/" 2>/dev/null
sudo cp /etc/systemd/system/warehouse-v2-en.service     "$KIT/systemd/" 2>/dev/null
sudo cp -r /etc/systemd/system/warehouse-v2.service.d   "$KIT/systemd/" 2>/dev/null
sudo cp /etc/systemd/system/zt-watchdog.service         "$KIT/systemd/" 2>/dev/null
systemctl list-unit-files --state=enabled > "$KIT/systemd/enabled_units.txt" 2>/dev/null
sudo chown -R "$USER":"$USER" "$KIT/systemd" 2>/dev/null

echo "② 根目錄腳本（kiosk / 守護 / 切換）"
cp ~/*.sh "$KIT/scripts/" 2>/dev/null
ls ~/*.sh > "$KIT/scripts/_list.txt" 2>/dev/null

echo "③ crontab 與 autostart"
crontab -l > "$KIT/config/crontab.txt" 2>/dev/null
cp -r ~/.config/autostart "$KIT/config/" 2>/dev/null

echo "④ 環境記錄（重灌後照這個裝）"
python3 --version              > "$KIT/env/versions.txt" 2>&1
pip3 list --format=freeze     >> "$KIT/env/versions.txt" 2>&1
uname -a                      >> "$KIT/env/versions.txt" 2>&1
cat /etc/os-release           >> "$KIT/env/versions.txt" 2>&1
# 模型清單（本包不含檔案本體，記下該補哪些）
find ~/warehouse_v2 ~/warehouse_v2_en -name '*.gguf' -o -name 'intent_clf.bin' 2>/dev/null \
    | xargs -r ls -la > "$KIT/env/models_needed.txt" 2>/dev/null

echo "⑤ 打包"
tar czf "$KIT.tar.gz" -C ~ "$(basename "$KIT")" 2>/dev/null
rm -rf "$KIT"
ls -lh "$KIT.tar.gz"
echo
echo "✅ 完成。請 scp 回 Windows 保存："
echo "   scp -i ~/.ssh/rpi5_warehouse p400@192.168.125.232:$KIT.tar.gz ."
echo
echo "⚠️ 這包**不含模型與程式碼**："
echo "   · 程式碼 → warehouse_v2/ 與 warehouse_v2_en/（已有 git + scp 備份）"
echo "   · 模型   → *.gguf / intent_clf.bin（~1G，另外 scp 一次即可）"
