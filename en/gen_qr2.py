#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_qr2.py — 產生中英文版 QR code（2026-08-04，第二台重建用）。

舊版 `gen_qr.py` 把 IP 寫死成熱點的 192.168.4.1，
第二台固定放公司走區網 ⇒ 改成**自動抓本機區網 IP**，
換網段/換機器都不用改程式。

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


if __name__ == "__main__":
    ip = sys.argv[1] if len(sys.argv) > 1 else lan_ip()
    make(f"https://{ip}:8002", "EN", f"{DESKTOP}/QRcode_英文版.png")
    make(f"https://{ip}:8001", "ZH", f"{DESKTOP}/QRcode_中文版.png")
    print(f"\n本機區網 IP：{ip}")
