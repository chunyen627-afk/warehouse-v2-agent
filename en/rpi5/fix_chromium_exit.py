#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""開機前清掉 Chromium 的崩潰標記。

不清的話開機會跳中文彈窗「Chromium 未正確關閉。還原」蓋住畫面右上角
（斷電 / systemctl reboot / pkill 都會留下 exit_type=Crashed，
 命令列旗標關不掉，只能改 profile）。

由 launch_warehouse.sh 呼叫。⚠️ 獨立成檔而非內嵌 heredoc——
巢狀 heredoc 的終止符很容易被外層吃掉，導致整支 shell 腳本語法錯（踩過）。
"""
import json
import os

PREF = os.path.expanduser("~/.config/chromium/Default/Preferences")

try:
    with open(PREF, encoding="utf-8") as f:
        d = json.load(f)
    prof = d.setdefault("profile", {})
    prof["exit_type"] = "Normal"
    prof["exited_cleanly"] = True
    with open(PREF, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
except Exception:
    pass    # 缺檔／壞檔都不該擋住開機流程
