#!/bin/bash
# ZeroTier 看門狗 v3（2026-07-22）：修正 v2 太積極的錯誤。
# v2 一看到 offline 就 restart，反而打斷 ZeroTier 正在進行的重連 → 越 restart
# 越慢。v3 給 ZeroTier 時間自己連：連續 3 次（60 秒）都 offline 才 restart。
FAIL=0
while true; do
  if ip route | grep -q '^default' && ping -c1 -W3 8.8.8.8 >/dev/null 2>&1; then
    systemctl is-active --quiet zerotier-one || sudo systemctl start zerotier-one
    ST=$(sudo zerotier-cli status 2>/dev/null | awk '{print $5}')
    if [ "$ST" = "ONLINE" ]; then
      FAIL=0                      # 上線了，歸零
    else
      FAIL=$((FAIL+1))
      # 連續 3 次（約 60 秒）還沒上線 = 真的卡住，才 restart（不打斷正常重連）
      if [ $FAIL -ge 3 ]; then
        sudo systemctl restart zerotier-one
        FAIL=0
      fi
    fi
  else
    # 無外網（當 AP/離線）→ 停 ZeroTier 省資源
    systemctl is-active --quiet zerotier-one && sudo systemctl stop zerotier-one
    FAIL=0
  fi
  sleep 20
done
