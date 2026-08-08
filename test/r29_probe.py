# -*- coding: utf-8 -*-
"""r29 探針：裸數量→三倉同值；北倉明講→僅北倉；各30/沒講 迴歸不變。"""
import sys

sys.path.insert(0, ".")
import warehouse as W

W.init("seed_data.json")
import tools_v2 as T

TESTS = [
    # (句子, 期望安全, 期望北, 期望中, 期望南)
    ("網球二號50元100個", 100, 100, 100, 100),
    ("羽球三號北倉80", 80, 80, 0, 0),
    ("壁球四號三倉各30", 30, 30, 30, 30),
    ("桌球五號200元", 50, 50, 50, 50),
    ("匹克球拍六號500元安全25", 25, 25, 25, 25),
]
bad = 0
for t, es, en_, ec, eso in TESTS:
    r = T.create_item_collect(step=1, raw_text=t)
    it = r.get("data", {}).get("item", {})
    ok = (it and it.get("safety") == es and it.get("stock_north") == en_
          and it.get("stock_central") == ec and it.get("stock_south") == eso)
    if not ok:
        bad += 1
    print("OK " if ok else "NG ", t, "→", it.get("name"),
          "| 安", it.get("safety"),
          "| 北中南", it.get("stock_north"), it.get("stock_central"),
          it.get("stock_south"))
print("bad", bad)
