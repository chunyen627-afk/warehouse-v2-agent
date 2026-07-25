# -*- coding: utf-8 -*-
"""
make_en_agent_data.py — 把 Agent Tools 用到的**資料檔**英文化（EN build）。

背景：先前的英文化（make_en_seed / make_en_ui / make_en_quips）處理了商品名、
UI、搭售俏皮話，但 Agent Tools 讀的另外兩份資料檔還是中文，訪客看得到：
  1. warehouse_data/scripts/manifest.json  → 腳本 label/description
     （run_script 的確認卡直接顯示 label：「即將執行白名單腳本【月底盤點】」）
  2. warehouse_data/master/suppliers.csv   → 供應商名稱
     （RCA 對帳卡會印供應商：「PO-xxx (2026-07-01, North, 全球電子)」）

可重複執行：原值備份到 *.zh.bak，已英文化過就跳過。
用法：cd ~/warehouse_v2_en && python3 make_en_agent_data.py
"""
import csv
import io
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
DD = HERE / "warehouse_data"

# ── 腳本 manifest ────────────────────────────────────────────────────────────
SCRIPT_EN = {
    "stock_audit": (
        "Month-end Stocktake",
        "Scan all warehouses, compare against safety stock, output a CSV report",
    ),
    "export_movements": (
        "Export Movement Log",
        "Merge the last 30 days of inbound/outbound records into a CSV",
    ),
    "generate_report": (
        "Generate Health Report",
        "Produce a full Markdown warehouse health report "
        "(low stock + best sellers + overview)",
    ),
}

# ── 供應商名稱 ───────────────────────────────────────────────────────────────
#   對照主檔實際的 6 家（SUP01-06），依原名語感取英文名
SUPPLIER_EN = {
    "宏鼎電子": "Grand Ding Electronics",
    "全廚實業": "AllChef Industrial",
    "鮮食物流": "FreshFood Logistics",
    "潔家日用": "CleanHome Daily",
    "織品紡織": "Weave Textiles",
    "動能運動": "Kinetic Sports",
}


def do_manifest() -> None:
    p = DD / "scripts" / "manifest.json"
    if not p.exists():
        print(f"  [skip] {p} 不存在")
        return
    bak = p.with_suffix(".json.zh.bak")
    if not bak.exists():
        shutil.copy2(p, bak)
    data = json.load(io.open(bak, encoding="utf-8"))
    n = 0
    for sc in data.get("scripts", []):
        en = SCRIPT_EN.get(sc.get("id"))
        if not en:
            continue
        sc["label_zh"] = sc.get("label")
        sc["label"] = en[0]
        key = "description" if "description" in sc else "desc"
        sc[key + "_zh"] = sc.get(key)
        sc[key] = en[1]
        n += 1
    io.open(p, "w", encoding="utf-8").write(
        json.dumps(data, ensure_ascii=False, indent=2))
    print(f"  manifest.json: {n} 個腳本英文化")


def do_suppliers() -> None:
    p = DD / "master" / "suppliers.csv"
    if not p.exists():
        print(f"  [skip] {p} 不存在")
        return
    bak = p.with_suffix(".csv.zh.bak")
    if not bak.exists():
        shutil.copy2(p, bak)
    rows = list(csv.DictReader(io.open(bak, encoding="utf-8-sig")))
    if not rows:
        print("  [skip] suppliers.csv 空的")
        return
    fields = list(rows[0].keys())
    if "name_zh" not in fields:
        fields.append("name_zh")
    n = 0
    for r in rows:
        zh = r.get("name", "")
        en = SUPPLIER_EN.get(zh)
        if en:
            r["name_zh"] = zh
            r["name"] = en
            n += 1
        else:
            r.setdefault("name_zh", zh)
    with io.open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"  suppliers.csv: {n}/{len(rows)} 家供應商英文化")
    _miss = {r.get("name_zh", "") for r in rows
             if r.get("name") == r.get("name_zh")} - {""}
    if _miss:
        print(f"  ⚠️ 未對應（仍中文）：{sorted(_miss)}")


if __name__ == "__main__":
    print("英文化 Agent Tools 資料檔：")
    do_manifest()
    do_suppliers()
    print("完成（原值存在 *.zh.bak）")
