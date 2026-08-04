#!/usr/bin/env bash
# regen_cert.sh — 重簽倉管自簽憑證，補齊 SAN（2026-08-04）
#
# 為什麼要重簽：原憑證的 SAN 只有舊機的位址
#   IP: 10.35.219.22 / 10.35.219.64 / 192.168.4.1 / 127.0.0.1
#   DNS: localhost / raspberrypi / raspberrypi.local
# 第二台的區網 IP（192.168.125.178）與主機名（raspberrypi2）都不在裡面
# → 在辦公室用區網 IP 連會多一層「名稱不符」警告。
#
# 🚨 SAN 一定要有 **192.168.4.1**（熱點閘道）——展場訪客手機走的就是這個位址；
#    漏了會讓語音端點（getUserMedia 需要安全上下文）出問題。
#
# ⚠️ 只重簽「倉管自簽憑證」（CN=Warehouse-Demo），
#    不碰 Let's Encrypt 那套（nginx 用的，見 RPI5_RESTORE §7c）。
set -euo pipefail

DAYS=1460                     # 4 年，跟原本效期量級一致
LAN_IP="$(hostname -I | awk '{print $1}')"
HOST="$(hostname)"

echo "本機區網 IP：$LAN_IP｜主機名：$HOST"

for d in warehouse_v2_en warehouse_v2; do
    [ -d "$HOME/$d" ] || { echo "跳過 $d（不存在）"; continue; }
    cd "$HOME/$d"

    # 先備份（出事要能還原）
    [ -f cert.pem ] && cp cert.pem "cert.pem.bak-$(date +%m%d)"
    [ -f key.pem ]  && cp key.pem  "key.pem.bak-$(date +%m%d)"

    CN="Warehouse-Demo"
    [ "$d" = "warehouse_v2" ] && CN="RPI5-Demo"

    cat > /tmp/_san.cnf <<EOF
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = $CN
[v3]
subjectAltName = @alt
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
[alt]
IP.1 = 192.168.4.1
IP.2 = 127.0.0.1
IP.3 = $LAN_IP
DNS.1 = localhost
DNS.2 = $HOST
DNS.3 = $HOST.local
EOF

    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout key.pem -out cert.pem -days "$DAYS" \
        -config /tmp/_san.cnf >/dev/null 2>&1

    chmod 600 key.pem
    chmod 644 cert.pem
    echo "── $d（CN=$CN）"
    openssl x509 -in cert.pem -noout -enddate -ext subjectAltName \
        | sed 's/^/     /'
done

rm -f /tmp/_san.cnf
echo
echo "完成。要生效請重啟服務："
echo "  sudo systemctl restart warehouse-v2 warehouse-v2-en"
