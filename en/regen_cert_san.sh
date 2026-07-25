#!/bin/bash
# regen_cert_san.sh — 重簽帶 SAN 的自簽憑證（中英共用）。
#
# 為什麼一定要 SAN（2026-07-25 遠端連線踩到）：
#   舊憑證只有 CN=RPI5-Demo、**沒有 SAN 欄位**。Chrome 58+ 完全忽略 CN、
#   只認 SAN，造成：
#     - 首頁 https：點「繼續前往」建立例外 → 開得起來（UI 看得到）
#     - WebSocket wss：**不提示、直接靜默拒絕** → 卡 Loading、送不出字
#   兩者走不同驗證路徑，所以會出現「畫面正常但不能用」。
#
#   ⚠️ 中文版長期沒踩到，是因為 kiosk 一直用 https://localhost:8001 開，
#      而瀏覽器對 localhost 有豁免、不檢查 SAN。改用 IP 連就會壞。
#
# 用法：bash regen_cert_san.sh  然後重啟服務
set -e
cd "$(dirname "$0")"
[ -f cert.pem ] && cp cert.pem cert.pem.bak-$(date +%Y%m%d-%H%M)
[ -f key.pem ] && cp key.pem key.pem.bak-$(date +%Y%m%d-%H%M)
cat > /tmp/san.cnf << 'EOF'
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = Warehouse-Demo
[v3]
subjectAltName = @alt
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
[alt]
IP.1 = 10.35.219.22
IP.2 = 10.35.219.64
IP.3 = 192.168.4.1
IP.4 = 127.0.0.1
DNS.1 = localhost
DNS.2 = raspberrypi
DNS.3 = raspberrypi.local
EOF
openssl req -x509 -newkey rsa:2048 -nodes -days 730 \
  -keyout key.pem -out cert.pem -config /tmp/san.cnf 2>/dev/null
echo "新憑證 SAN："
openssl x509 -in cert.pem -noout -ext subjectAltName
