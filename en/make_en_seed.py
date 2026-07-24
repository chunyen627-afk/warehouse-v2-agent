# -*- coding: utf-8 -*-
"""
make_en_seed.py — 把 en/seed_data.json 的顯示名英文化。
改：items[].name(→英文,原中文存 name_zh)、categories[].label、warehouses[].label。
不動：sku_id / key / 數量 / 日期 / movements / orders / stock / batches（都用代號引用）。
association_meta(搭售俏皮話)含中文=回答文字，屬「回答英文化」階段，這步先不動。
用法：python make_en_seed.py   （原地覆蓋 seed_data.json，先自動備份 .zh.bak）
"""
import json, shutil, sys
from pathlib import Path
from item_names_en import build_en_map, WAREHOUSE_EN, WH_ZH2KEY, CATEGORY_LABEL_EN

SEED = Path(__file__).parent / "seed_data.json"


def main():
    d = json.load(open(SEED, encoding="utf-8"))
    # 可重複執行：seed 已英文化(有 name_zh)則用 name_zh 建對照
    already_en = all("name_zh" in it for it in d["items"])
    items_for_map = ([{**it, "name": it["name_zh"]} for it in d["items"]]
                     if already_en else d["items"])
    en_map, missing = build_en_map(items_for_map)
    if missing:
        print("[warn] 未對到英文名，中止:", missing); sys.exit(1)
    if already_en:
        print("[seed] 已英文化，跳過 seed、只處理 items.csv")
        en_items_csv(en_map, d)
        print("[done] items.csv englishized"); return

    # 備份中文原版（只備一次，不覆蓋既有備份）
    bak = SEED.with_suffix(".json.zh.bak")
    if not bak.exists():
        shutil.copy(SEED, bak); print(f"已備份中文版 → {bak.name}")

    # 1. items.name → 英文（原中文留 name_zh）
    for it in d["items"]:
        it["name_zh"] = it["name"]
        it["name"] = en_map[it["sku_id"]]["name_en"]

    # 2. categories.label → 英文
    for c in d["categories"]:
        c["label_zh"] = c["label"]
        c["label"] = CATEGORY_LABEL_EN.get(c["key"], c["label"])

    # 3. warehouses.label → 英文
    for w in d["warehouses"]:
        w["label_zh"] = w["label"]
        key = w["key"] if w["key"] in WAREHOUSE_EN else WH_ZH2KEY.get(w["label"], w["key"])
        if key in WAREHOUSE_EN:
            w["label"] = WAREHOUSE_EN[key][0]

    json.dump(d, open(SEED, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[seed] {len(d['items'])} items, {len(d['categories'])} cats, {len(d['warehouses'])} whs -> EN")

    # ── 主資料 warehouse_data/master/items.csv 也英文化（multi 模式真正讀這個）──
    en_items_csv(en_map, d)
    print("[done] items.csv + seed_data.json englishized")


def en_items_csv(en_map, seed_dict):
    """warehouse_data/master/items.csv 的 name + category_label 英文化（by sku_id）。"""
    import csv
    csvp = Path(__file__).parent / "warehouse_data" / "master" / "items.csv"
    if not csvp.exists():
        print("[csv] items.csv 不存在，略過"); return
    bak = csvp.with_suffix(".csv.zh.bak")
    if not bak.exists():
        shutil.copy(csvp, bak)
    # category key -> 英文 label（用 seed 已英文化的 categories）
    cat_en = {c["key"]: c["label"] for c in seed_dict["categories"]}
    rows = list(csv.DictReader(open(csvp, encoding="utf-8-sig")))
    fields = rows[0].keys() if rows else []
    for r in rows:
        sid = r.get("sku_id", "")
        if sid in en_map:
            r["name"] = en_map[sid]["name_en"]
        if r.get("category") in cat_en:
            r["category_label"] = cat_en[r["category"]]
    with open(csvp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(fields))
        w.writeheader(); w.writerows(rows)
    print(f"[csv] items.csv {len(rows)} rows -> EN name+category_label")


if __name__ == "__main__":
    main()
