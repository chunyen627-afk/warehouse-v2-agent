# -*- coding: utf-8 -*-
"""check_views.py — server 發出的 view vs 前端渲染器覆蓋審計（r54 起每輪跑）
背景：「有哪些商品」summary 說「以下為前10筆」但 view=inventory 沒有前端渲染器，
表格從沒畫出來——ws_inspect 只看文字看不到畫面級破口，user 實測才抓到。
文字型 view（summary 自帶全部內容）列白名單。"""
import re, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
TEXT_ONLY = {"guide", "rejected", "clarify", "error", "item_list", "related_empty",
             "expiring_empty", "reset_done", "alert_deleted", "schedule_deleted"}
srv = set()
for f in ("server.py", "warehouse.py", "tools_v2.py"):
    srv |= set(re.findall(r'["\']view["\']\s*[:=]\s*["\']([a-z_]+)["\']', open(f, encoding="utf-8").read()))
fe = set(re.findall(r"view === '([a-z_]+)'", open("templates/index.html", encoding="utf-8").read()))
missing = sorted(srv - fe - TEXT_ONLY)
print(f"server {len(srv)} views / frontend {len(fe)} renderers / 白名單 {len(TEXT_ONLY)}")
if missing:
    print("❌ 缺渲染器:", missing); sys.exit(1)
print("✅ 視圖覆蓋完整")
