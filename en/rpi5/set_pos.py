# -*- coding: utf-8 -*-
"""把我們新增的五個桌面項目移到最右邊一欄（螢幕 1920x1080）。

⚠️ 要 scp 上去執行，不要用 SSH heredoc（中文檔名會在 shell 傳輸中被吃掉）。
pcmanfm 只在啟動時讀這份設定，所以寫完要重啟 pcmanfm --desktop 才生效。
"""
import configparser
import os

CONF = os.path.expanduser(
    "~/.config/pcmanfm/LXDE-pi/desktop-items-HDMI-A-1.conf")

# 最右欄：1920 寬，圖示格約 100px，留 ~110px 邊距
X = 1790
ITEMS = [
    ("1_啟動英文版.desktop", 40),
    ("2_啟動中文版.desktop", 160),
    ("3_切換熱點.desktop", 280),
    ("QRcode_英文版.png", 400),
    ("QRcode_中文版.png", 540),
]

cp = configparser.RawConfigParser()
cp.optionxform = str          # 保留原本大小寫
cp.read(CONF, encoding="utf-8")

for name, y in ITEMS:
    if not cp.has_section(name):
        cp.add_section(name)
    cp.set(name, "x", str(X))
    cp.set(name, "y", str(y))
    print(f"{name:28} -> x={X} y={y}")

with open(CONF, "w", encoding="utf-8") as f:
    cp.write(f, space_around_delimiters=False)
print("written:", CONF)
