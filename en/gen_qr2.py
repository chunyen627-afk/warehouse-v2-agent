#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_qr2.py — 產生中英文版 QR code（2026-08-04，第二台重建用）。

🚨 **展場一定要用熱點 IP `192.168.4.1`**——
訪客手機是連 RPI5 開的熱點（SSID `RPI5-Demo`），
連上後**只有區網、沒有外網也沒有 DNS**，
所以 QR 必須指向熱點位址；用公司 Wi-Fi 的 IP（192.168.125.x）**手機連不到**。
（曾經誤產成區網 IP，2026-08-04 修正。）

預設 = 熱點 IP；要產區網版（自己在辦公室測）才傳參數：
  python3 gen_qr2.py            # 熱點 192.168.4.1（展場用，預設）
  python3 gen_qr2.py lan        # 自動抓本機區網 IP
  python3 gen_qr2.py 10.0.0.5   # 指定 IP

產出（桌面）：
  QRcode_英文版.png  → https://<本機IP>:8002
  QRcode_中文版.png  → https://<本機IP>:8001

⚠️ 檔名沿用舊機的（`QRcode_英文版` / `QRcode_中文版`），
   桌面圖示排列與展場說明才不用改。
"""
import socket
import subprocess
import sys

import qrcode
from PIL import Image, ImageDraw, ImageFont

DESKTOP = "/home/p400/Desktop"


def lan_ip():
    """抓實際對外的區網 IP（不是 127.0.0.1）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))       # 不會真的送封包，只為取路由來源位址
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True)
        return (out.stdout or "").split()[0] if out.stdout.strip() else "127.0.0.1"


def make(url, label, path):
    qr = qrcode.QRCode(box_size=10, border=3)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    # 底部加一行網址，方便現場對照（掃不到時可以手動輸入）
    w, h = img.size
    bar = 46
    canvas = Image.new("RGB", (w, h + bar), "white")
    canvas.paste(img, (0, 0))
    d = ImageDraw.Draw(canvas)
    try:
        f = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 17)
    except Exception:
        f = ImageFont.load_default()
    text = f"{label}  {url}"
    tw = d.textlength(text, font=f)
    d.text(((w - tw) / 2, h + 12), text, fill="black", font=f)
    canvas.save(path)
    print(f"{path}  ->  {url}")


HOTSPOT_IP = "192.168.4.1"      # nmcli con show rpi5-hotspot → ipv4.addresses

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "lan":
        ip = lan_ip()
    elif arg:
        ip = arg
    else:
        ip = HOTSPOT_IP             # 預設＝展場用的熱點位址
    make(f"https://{ip}:8002", "EN", f"{DESKTOP}/QRcode_英文版.png")
    make(f"https://{ip}:8001", "ZH", f"{DESKTOP}/QRcode_中文版.png")
    print(f"\nQR 指向（熱點/展場用）：{ip}")
