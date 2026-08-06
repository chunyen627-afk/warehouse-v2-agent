#!/bin/bash
sleep 10
echo ""
python3 -c "
import qrcode, socket, os
ip = '192.168.4.1'
port = 8001
url = 'https://' + ip + ':' + str(port)
print()
print('◆' * 50)
print(f'  WiFi: RPI5-Demo  |  密码: demo1234')
print(f'  扫描 QR Code 进入仓库助理:')
print('◆' * 50)
print()
qr = qrcode.QRCode(box_size=3, border=2)
qr.add_data(url)
qr.make(fit=True)
qr.print_ascii(invert=True)
print()
print(f'  URL: {url}')
print('◆' * 50)
"
