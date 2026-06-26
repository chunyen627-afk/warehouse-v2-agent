"""
migrate_to_v2.py — 把 v1 的 seed_data.json 拆成 v2 的 warehouse_data/ 多檔結構。

設計原則：
  1. seed_data.json 是 source of truth，本腳本可重跑、冪等（每次重建 warehouse_data/）。
  2. transactions 按「日 × 方向」切檔 → 逼 Agent 用 Glob+Read（v2 vs v1 單檔查表的分水嶺）。
  3. PO 從 in-movements 聚合，並『故意』在少數幾筆製造「PO 應收量 ≠ 實際入庫量」差異，
     給 search_log 的 RCA「PO 對不上」一個真實可追的場景。
  4. safety_stock 維持寫死（config.json），不導入 dynamic 安全庫存（守 CLAUDE.md 設計）。
     config.json 多一層「分倉 override」，撐 manage_config「南倉安全庫存全部 +30」招牌題。

用法：
  cd warehouse_v2
  py -3.11 data_tools/migrate_to_v2.py            # 重建 test/warehouse_data/
  py -3.11 data_tools/migrate_to_v2.py --verify   # 重建後驗證可被 loader 載回
"""
import csv
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # warehouse_v2/
SEED = ROOT / "test" / "seed_data.json"
WD   = ROOT / "test" / "warehouse_data"

# RCA「PO 對不上」要的可重現亂數
random.seed(20260622)

# ── 供應商（v1 沒有，全新造；簡版：名稱 + 前置天數 + 負責類別）──
# 對應 RCA「PO 對不上」要追到供應商，也給補貨建議「前置天數」來源。
SUPPLIERS = [
    # supplier_id, name,        lead_time_days, categories
    ("SUP01", "宏鼎電子",   7,  ["electronics"]),
    ("SUP02", "全廚實業",   10, ["appliance_kitchen"]),
    ("SUP03", "鮮食物流",   3,  ["food_beverage"]),
    ("SUP04", "潔家日用",   5,  ["daily_goods"]),
    ("SUP05", "織品紡織",   14, ["apparel"]),
    ("SUP06", "動能運動",   9,  ["sports"]),
]


def load_seed() -> dict:
    with open(SEED, "r", encoding="utf-8") as f:
        return json.load(f)


def w_csv(path: Path, header: list[str], rows: list[list]):
    """寫 UTF-8 BOM CSV（Excel 可開、對齊 v1 items_editable.csv 慣例）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(header)
        wr.writerows(rows)


def w_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ════════════════════════════════════════════════════════════
# master/  — 主檔（少變動）
# ════════════════════════════════════════════════════════════
def build_master(seed: dict):
    cat_label = {c["key"]: c["label"] for c in seed["categories"]}

    # items.csv
    rows = [
        [it["sku_id"], it["name"], it["category"], cat_label.get(it["category"], it["category"]),
         it["unit_price"], it["safety_stock"]]
        for it in seed["items"]
    ]
    w_csv(WD / "master" / "items.csv",
          ["sku_id", "name", "category", "category_label", "unit_price", "safety_stock"], rows)

    # suppliers.csv
    cat_to_sup = {}
    srows = []
    for sid, name, lead, cats in SUPPLIERS:
        srows.append([sid, name, lead, "|".join(cats)])
        for c in cats:
            cat_to_sup[c] = sid
    w_csv(WD / "master" / "suppliers.csv",
          ["supplier_id", "name", "lead_time_days", "categories"], srows)

    # stock.csv — 當前庫存『快照』真值（不可從 movements 推算，期初存量未知）。
    #   seed.stock 是 source of truth，直接搬。loader 讀回不重算。
    stock_rows = []
    for wh, skus in seed["stock"].items():
        for sku, qty in skus.items():
            stock_rows.append([wh, sku, qty])
    stock_rows.sort(key=lambda r: (r[0], r[1]))
    w_csv(WD / "master" / "stock.csv", ["warehouse", "sku_id", "qty"], stock_rows)

    # config.json — manage_config 的讀寫目標
    #   safety_stock_base：各 SKU 全倉基準（= seed 寫死值）
    #   safety_stock_override：分倉覆寫（撐「南倉 +30」；初始空，set 時寫入）
    #   其餘為可讀寫的營運參數。
    config = {
        "_comment": "manage_config 的讀寫目標。key 走 keyword 比對（不進模型 enum）。",
        "safety_stock_base": {it["sku_id"]: it["safety_stock"] for it in seed["items"]},
        "safety_stock_override": {wh["key"]: {} for wh in seed["warehouses"]},
        "reorder_lead_days": 7,          # 補貨前置天數（manage_config 可改）
        "safety_buffer_ratio": 1.0,      # 安全水位倍數（manage_config 可改）
        "restock_target_days": 14,       # 補到撐幾天（v3.9.1 補貨預測用）
    }
    w_json(WD / "master" / "config.json", config)
    return cat_to_sup


# ════════════════════════════════════════════════════════════
# transactions/  — 進出記錄按「日_方向」切檔
# ════════════════════════════════════════════════════════════
def build_transactions(seed: dict):
    buckets = defaultdict(list)   # (date, direction) -> rows
    for m in seed["movements"]:
        buckets[(m["date"], m["direction"])].append(
            [m["date"], m["sku_id"], m["warehouse"], m["direction"], m["qty"]]
        )
    n_files = 0
    for (date, direction), rows in sorted(buckets.items()):
        rows.sort(key=lambda r: (r[2], r[1]))   # by warehouse, sku
        w_csv(WD / "transactions" / f"{date}_{direction}.csv",
              ["date", "sku_id", "warehouse", "direction", "qty"], rows)
        n_files += 1
    return n_files


# ════════════════════════════════════════════════════════════
# orders/  — SO 從 seed.orders；PO 從 in-movements 聚合（含故意差異）
# ════════════════════════════════════════════════════════════
def build_orders(seed: dict, cat_to_sup: dict):
    item_cat = {it["sku_id"]: it["category"] for it in seed["items"]}

    # SO：銷售/出貨單，直接映射 seed.orders
    for o in seed["orders"]:
        w_json(WD / "orders" / "SO" / f"{o['order_id']}.json", {
            "order_id": o["order_id"],
            "type": "SO",
            "date": o["date"],
            "lines": o["lines"],
        })

    # PO：把 in-movements 依 (date, warehouse, supplier) 聚合成採購單
    #   一張 PO = 同日同倉同供應商的進貨彙整。
    po_group = defaultdict(lambda: defaultdict(int))  # (date,wh,sup) -> {sku: qty}
    for m in seed["movements"]:
        if m["direction"] != "in":
            continue
        sup = cat_to_sup.get(item_cat.get(m["sku_id"], ""), "SUP01")
        po_group[(m["date"], m["warehouse"], sup)][m["sku_id"]] += m["qty"]

    # 排序好給穩定 PO 編號
    keys = sorted(po_group.keys())

    # ── 故意製造「對不上」的差異：挑 4 張 PO，讓『單據數量』比『實際入庫』多/少 ──
    #    這就是 RCA 要追的東西：search_log 比對 transactions(實收) vs PO(應收)。
    discrepancy_idx = set(random.sample(range(len(keys)), k=min(4, len(keys))))
    discrepancies = []

    po_id_n = 0
    for i, key in enumerate(keys):
        date, wh, sup = key
        po_id_n += 1
        po_id = f"PO{po_id_n:05d}"
        skus = po_group[key]
        lines = []
        for sku, recv_qty in sorted(skus.items()):
            order_qty = recv_qty           # 預設：訂多少收多少
            note = ""
            if i in discrepancy_idx and not note:
                # 對這張 PO 的第一個 SKU 動手腳：單據比實收多 5~20（短收 → 要追原因）
                delta = random.choice([8, 12, 15, 20])
                order_qty = recv_qty + delta
                note = "short_received"   # 短收：應收 order_qty、實收 recv_qty
                discrepancies.append({
                    "po_id": po_id, "date": date, "warehouse": wh, "supplier": sup,
                    "sku_id": sku, "order_qty": order_qty, "received_qty": recv_qty,
                    "gap": order_qty - recv_qty,
                })
            lines.append({
                "sku_id": sku,
                "order_qty": order_qty,      # 單據上的應收量
                "received_qty": recv_qty,    # 實際入庫量（= transactions 的真值）
                "note": note,
            })
        w_json(WD / "orders" / "PO" / f"{po_id}.json", {
            "po_id": po_id, "type": "PO", "date": date,
            "warehouse": wh, "supplier": sup, "lines": lines,
        })

    return po_id_n, len(seed["orders"]), discrepancies


# ════════════════════════════════════════════════════════════
# audit/  — 異動留底（初始：一筆 migration 紀錄）
# ════════════════════════════════════════════════════════════
def build_audit(seed: dict):
    date = seed.get("snapshot_date", "2026-05-26")
    log = WD / "audit" / f"{date}_changes.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": f"{date}T00:00:00",
            "trace_id": "migrate-v2-0001",
            "actor": "system",                 # system | user_confirmed | agent_auto
            "action": "migrate",
            "detail": "seed_data.json → warehouse_data/ 初始化",
        }, ensure_ascii=False) + "\n")


# ════════════════════════════════════════════════════════════
# scripts/manifest.json — run_script 白名單（腳本名走 keyword 比對）
# ════════════════════════════════════════════════════════════
def build_manifest():
    manifest = {
        "_comment": "run_script 白名單。script_name 走 keyword 比對（不進模型 enum）。aliases 給 fuzzy match。",
        "scripts": [
            {
                "id": "stock_audit",
                "label": "月底盤點",
                "aliases": ["盤點", "月底盤點", "庫存盤點", "stock audit", "audit"],
                "desc": "掃全倉產出盤點報告 CSV",
                "timeout_s": 60,
            },
            {
                "id": "export_movements",
                "label": "匯出進出記錄",
                "aliases": ["匯出", "匯出進出", "匯出記錄", "export movements", "export"],
                "desc": "把 transactions 匯成 Excel CSV",
                "timeout_s": 60,
            },
            {
                "id": "regenerate_seed",
                "label": "重產種子資料",
                "aliases": ["重產", "重新產生", "重生資料", "regenerate", "regen seed"],
                "desc": "從 items CSV 重生 seed_data.json",
                "timeout_s": 120,
            },
        ],
    }
    w_json(WD / "scripts" / "manifest.json", manifest)


def main():
    if not SEED.exists():
        print(f"[err] 找不到 seed：{SEED}")
        sys.exit(1)

    # 清空重建（冪等）
    import shutil
    for sub in ["master", "transactions", "orders", "audit", "scripts"]:
        d = WD / sub
        if d.exists():
            shutil.rmtree(d)

    seed = load_seed()
    print(f"[1/6] master/  — items {len(seed['items'])} / suppliers {len(SUPPLIERS)} / config")
    cat_to_sup = build_master(seed)

    print(f"[2/6] transactions/  — 切檔中…")
    n_tx = build_transactions(seed)
    print(f"        → {n_tx} 個檔（{len(seed['movements'])} movements）")

    print(f"[3/6] orders/  — SO + PO 聚合…")
    n_po, n_so, discreps = build_orders(seed, cat_to_sup)
    print(f"        → PO {n_po} 張 / SO {n_so} 張")
    print(f"        → 故意製造 {len(discreps)} 筆『PO 對不上』供 RCA：")
    for d in discreps:
        print(f"           {d['po_id']} {d['warehouse']} {d['sku_id']}: 應收 {d['order_qty']} / 實收 {d['received_qty']} (短 {d['gap']})")

    print(f"[4/6] audit/  — 初始 migration 紀錄")
    build_audit(seed)

    print(f"[5/6] scripts/manifest.json  — 白名單 3 支")
    build_manifest()

    print(f"[6/6] 完成 → {WD}")

    if "--verify" in sys.argv:
        verify()


def verify():
    print("\n[verify] 驗證 warehouse_data/ 可被載回…")
    ok = True
    items = list(csv.DictReader(open(WD / "master" / "items.csv", encoding="utf-8-sig")))
    print(f"  items.csv: {len(items)} 列  例:{items[0]['sku_id']} {items[0]['name']}")
    sups = list(csv.DictReader(open(WD / "master" / "suppliers.csv", encoding="utf-8-sig")))
    print(f"  suppliers.csv: {len(sups)} 列")
    cfg = json.load(open(WD / "master" / "config.json", encoding="utf-8"))
    print(f"  config.json: safety_stock_base {len(cfg['safety_stock_base'])} SKU, lead {cfg['reorder_lead_days']}天")
    tx = list((WD / "transactions").glob("*.csv"))
    print(f"  transactions/: {len(tx)} 檔")
    po = list((WD / "orders" / "PO").glob("*.json"))
    so = list((WD / "orders" / "SO").glob("*.json"))
    print(f"  orders/: PO {len(po)} / SO {len(so)}")
    man = json.load(open(WD / "scripts" / "manifest.json", encoding="utf-8"))
    print(f"  manifest.json: {len(man['scripts'])} 支腳本")
    print("[verify] OK" if ok else "[verify] FAIL")


if __name__ == "__main__":
    main()
