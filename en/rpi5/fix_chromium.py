# -*- coding: utf-8 -*-
"""清掉 Chromium 的兩個展場干擾（都會顯示**中文**給訪客看）：

① 「Chromium 未正確關閉。還原」崩潰復原提示
   → profile 的 exit_type 改成 Normal、exited_cleanly 設 true
② Google 翻譯彈窗（英文／中文繁體）
   → 命令列 --disable-features=Translate 在新版 Chromium 不一定生效，
     改寫 profile 偏好設定（translate.enabled=false + 語言白名單）

⚠️ 要 scp 上去執行；先關掉 chromium 再跑，否則會被回寫覆蓋。
"""
import json
import os
import glob

CFG = os.path.expanduser("~/.config/chromium")
targets = []
for name in ("Default", "Profile 1"):
    p = os.path.join(CFG, name, "Preferences")
    if os.path.exists(p):
        targets.append(p)
targets += [p for p in glob.glob(os.path.join(CFG, "*", "Preferences"))
            if p not in targets]

if not targets:
    print("no Preferences found under", CFG)

for p in targets:
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        print("skip", p, e)
        continue

    prof = d.setdefault("profile", {})
    prof["exit_type"] = "Normal"          # ① 不再顯示「未正確關閉」
    prof["exited_cleanly"] = True

    # ② 關翻譯
    d.setdefault("translate", {})["enabled"] = False
    d["translate_blocked_languages"] = ["en"]
    tl = d.setdefault("translate_language_blacklist", [])
    if "en" not in tl:
        tl.append("en")
    intl = d.setdefault("intl", {})
    intl["accept_languages"] = "en-US,en"
    intl["selected_languages"] = "en-US,en"

    # 關掉「要不要設為預設瀏覽器」
    d.setdefault("browser", {})["should_check_default_browser"] = False

    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    print("patched", p)
