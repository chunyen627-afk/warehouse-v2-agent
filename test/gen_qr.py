import qrcode
url = 'https://192.168.4.1:8001'
img = qrcode.make(url)
img.save('/home/p400/Desktop/warehouse_qr.png')
print('QR saved to Desktop')
