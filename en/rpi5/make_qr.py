# -*- coding: utf-8 -*-
"""產生桌面 QR：中英各一張，圖上標示語言與連線資訊。

⚠️ 兩個踩雷（都實測過）：
1. **不要用 SSH heredoc 內嵌中文**——中文在 shell 傳輸中被吃掉，畫成豆腐塊。
   這支要 scp 上去再執行。
2. **字型要逐行選**：RPI5 上
     Droid Sans Fallback → 中文正常、**英文數字是豆腐塊**
     DejaVu Sans        → 英文數字正常、**中文是豆腐塊**
   兩者剛好互補，所以按該行有沒有 CJK 分別選字型。
   （實測黑點數：Droid 中文 498/英文 350↓；DejaVu 中文 243↓/英文 479）
"""
import re
import qrcode
from PIL import Image, ImageDraw, ImageFont

CJK_FONT = "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"
LAT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
LAT_FONT_R = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
HAS_CJK = re.compile(r"[一-鿿]")


def font_for(text, size, bold=False):
    """該行含中文 → Droid；純英數 → DejaVu。混排行請自己拆成兩行。"""
    if HAS_CJK.search(text):
        return ImageFont.truetype(CJK_FONT, size)
    return ImageFont.truetype(LAT_FONT if bold else LAT_FONT_R, size)


def make(url, rows, out):
    """rows = [(文字, 字級, 粗體)]，每行獨立選字型。"""
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qw, qh = img.size

    items = [(t, font_for(t, s, b)) for t, s, b in rows]
    d0 = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    text_w = max(d0.textbbox((0, 0), t, font=f)[2] for t, f in items)
    W = max(qw, int(text_w) + 24)
    H = qh + 8 + sum(int(d0.textbbox((0, 0), t, font=f)[3]) + 9 for t, f in items)

    canvas = Image.new("RGB", (W, H), "white")
    canvas.paste(img, ((W - qw) // 2, 0))
    d = ImageDraw.Draw(canvas)
    y = qh + 6
    for t, f in items:
        bb = d.textbbox((0, 0), t, font=f)
        d.text(((W - (bb[2] - bb[0])) / 2, y), t, fill="black", font=f)
        y += int(bb[3]) + 9
    canvas.save(out)
    print("saved", out, canvas.size)


make("https://192.168.4.1:8002",
     [("英文版", 30, True), ("English", 24, True),
      ("WiFi: RPI5-Demo / demo1234", 17, False),
      ("192.168.4.1:8002", 15, False)],
     "/home/p400/Desktop/QRcode_英文版.png")

make("https://192.168.4.1:8001",
     [("中文版", 30, True), ("Chinese", 24, True),
      ("WiFi: RPI5-Demo / demo1234", 17, False),
      ("192.168.4.1:8001", 15, False)],
     "/home/p400/Desktop/QRcode_中文版.png")
