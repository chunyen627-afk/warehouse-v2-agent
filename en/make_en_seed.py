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
    en_map, missing = build_en_map(d["items"])
    if missing:
        print("⚠️ 有商品未對到英文名，中止：", missing); sys.exit(1)

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
    print(f"✅ 已英文化：{len(d['items'])} 商品、{len(d['categories'])} 類、{len(d['warehouses'])} 倉")
    print("   items 範例：", d["items"][0]["name"], "|", d["items"][0]["name_zh"])
    print("   category  ：", [c["label"] for c in d["categories"]])
    print("   warehouse ：", [w["label"] for w in d["warehouses"]])


if __name__ == "__main__":
    main()
