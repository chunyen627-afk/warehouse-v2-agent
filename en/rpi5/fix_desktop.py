# -*- coding: utf-8 -*-
"""更新桌面捷徑說明（兩版都已開機自啟，原本寫「中文版不自啟」已過時）。
⚠️ scp 上去執行，不要 SSH heredoc（中文會被吃掉）。
"""
from pathlib import Path

D = Path.home() / "Desktop"
items = {
    "1_啟動英文版.desktop": (
        "① 啟動英文版 (8002)",
        "English warehouse — 開機已預載，這個是切回英文版／關掉後重開用",
        "/home/p400/啟動_英文版.sh"),
    "2_啟動中文版.desktop": (
        "② 啟動中文版 (8001)",
        "中文版倉管 — 開機已預載（切換不用等模型載入）",
        "/home/p400/啟動_中文版.sh"),
    "3_切換熱點.desktop": (
        "③ 切換熱點 / WiFi",
        "開熱點給訪客掃 QR（192.168.4.1）；再點一次切回 WiFi",
        "lxterminal -e /home/p400/切換_熱點.sh"),
}

for fn, (name, comment, exec_) in items.items():
    p = D / fn
    p.write_text(
        "[Desktop Entry]\n"
        f"Name={name}\n"
        f"Comment={comment}\n"
        f"Exec={exec_}\n"
        f"Icon={'network-wireless' if '熱點' in fn else 'chromium-browser'}\n"
        "Terminal=false\n"
        "Type=Application\n",
        encoding="utf-8")
    p.chmod(0o755)
    print("updated", fn)
