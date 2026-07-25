"""
tools_v2.py — v2 Agent 進階工具（search_log / manage_config / run_script）。

職責分離：
  - 模型只出單步 JSON（function + 參數）。多步探索（Glob→Read→Reason）由本檔的
    Host 邏輯固定編排 → 270M 不跑自由 loop（守對齊決策 D1）。
  - 會變動的清單（log 檔名 / 設定項 / 腳本名）一律走 keyword 比對，不進模型 enum（D5）。

回傳格式沿用 v1：{ok, summary, data, view}。view 字串給前端路由 + Agent trace 浮現。

依賴 warehouse.state()：
  state().v2_config     master/config.json
  state().v2_suppliers  master/suppliers.csv
  state().v2_data_dir   warehouse_data/ 絕對路徑
  state().items / stock / _items_by_sku  （沿用 v1 索引做 keyword→SKU）
"""
import csv
import json
import subprocess
import sys
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import warehouse as W   # 用 W.state() / W.match_items() / W._err()

# ── stock.csv 寫入鎖 ──────────────────────────────────────────
# stock.csv 是進出貨/調貨/新增/刪除商品共用的檔案，這四個 commit 都是
# read-modify-write，多裝置（手機掃 QR + 平板）同時確認會互相蓋寫。
# 跟 server.py 的 llm_lock 同思路：所有寫入序列化。用 RLock 是因為
# commit 內部可能呼叫也要拿鎖的輔助函式。
_STOCK_LOCK = threading.RLock()


# ════════════════════════════════════════════════════════════
# 共用：trace 步驟記錄（給前端「Agent 多步」浮現用）
# ════════════════════════════════════════════════════════════
def _trace(steps: list[dict], kind: str, detail: str, **extra) -> None:
    """累積一個 Agent 步驟。kind: glob|grep|read|reason|act|confirm|verify。"""
    steps.append({"kind": kind, "detail": detail, **extra})


def _data_dir() -> Path:
    s = W.state()
    dd = getattr(s, "v2_data_dir", "") or ""
    if dd and Path(dd).exists():
        return Path(dd)
    # fallback：相對 seed 路徑推
    return Path(s.seed_path).parent / "warehouse_data"


def _period_dates(time_range: str | None) -> set[str] | None:
    """把 period enum 轉成日期集合（用 snapshot_date 當今天）。None = 不限。"""
    if not time_range:
        return None
    from datetime import date, timedelta
    snap = W.state().snapshot_date or "2026-05-26"
    today = date.fromisoformat(snap)
    if time_range == "today":
        return {snap}
    if time_range == "this_week":
        return {(today - timedelta(days=i)).isoformat() for i in range(7)}
    if time_range == "this_month":
        return {(today - timedelta(days=i)).isoformat() for i in range(30)}
    return None


# RCA 查詢的雜訊詞（模型常把「XX帳對不上」整句當 keyword，要剝掉才 match 得到商品）
_RCA_NOISE = ["帳對不上", "對不上", "對不起來", "兜不攏", "怎麼少這麼多", "怎麼少", "為什麼少",
              "為什麼短少", "短少", "少貨原因", "是不是少貨了", "少貨", "是誰動的", "誰動的", "誰改的",
              "庫存差異", "差異", "扣帳異常", "異常", "入庫數量不對", "入庫對不上", "短收了嗎", "短收",
              "的庫存問題", "庫存問題", "查一下", "幫我追", "為什麼", "怎麼會"]
# 疑問/泛詞（剝完這些若為空 = 沒指定商品 → 全域掃）。不放「的」等會誤砍商品名的常用字。
_RCA_GENERIC = ["有哪些", "哪些", "有沒有", "有那些", "那些", "採購單", "PO", "po",
                "全部", "所有", "幫我", "看看", "了嗎", "嗎", "呢", "　", " "]


def _kw_to_skus(keyword: str) -> list[dict]:
    """keyword → 命中的 SKU item 清單（沿用 v1 match_items）。
    只取最高分群：「慢跑鞋 男款」的「男款」token 曾把素T/牛仔褲 男款一起抓進
    config 影響範圍（9 項而非 3 項，conv100-r12）。"""
    if not keyword:
        return []
    hits = W.match_items(keyword)
    if not hits:
        return []
    top = hits[0].get("score", 0)
    return [h["item"] for h in hits if h.get("score", 0) >= top]


# ════════════════════════════════════════════════════════════
# ① search_log — 紀錄檔搜尋 / RCA「PO 對不上」
#    Host 編排：Glob transactions → Grep keyword → 若有短收 → 對 PO → Reason
# ════════════════════════════════════════════════════════════
def search_log(keyword: str = "", time_range: str | None = None, source: str | None = None) -> dict:
    steps: list[dict] = []
    dd = _data_dir()
    tx_dir = dd / "transactions"
    if not tx_dir.exists():
        return W._err("Transaction log directory not found")

    # keyword → 目標 SKU（可能多個；空 keyword = 全部）
    #   先剝掉 RCA 雜訊詞（模型常把「XX帳對不上」整句當 keyword）再 match。
    clean_kw = keyword or ""
    for nz in _RCA_NOISE:
        clean_kw = clean_kw.replace(nz, "")
    # 再剝疑問/泛詞（「有哪些 / 哪些 / 有沒有 / PO / 採購單」等，剩下才是真商品線索）
    for gz in _RCA_GENERIC:
        clean_kw = clean_kw.replace(gz, "")
    clean_kw = clean_kw.strip()
    skus = _kw_to_skus(clean_kw) if clean_kw else []
    sku_ids = {it["sku_id"] for it in skus}
    sku_names = {it["sku_id"]: it["name"] for it in skus}

    # ── 全域掃所有 PO 短收：沒 match 到具體商品 → 動態 JOIN receipts 計算 ──
    if not skus:
        po_dir      = dd / "orders" / "PO"
        rec_dir     = dd / "receipts"
        _trace(steps, "glob", "no item specified → scanning all purchase orders")
        all_disc = []
        po_count = 0
        for pj in sorted(po_dir.glob("*.json")):
            po       = json.load(open(pj, encoding="utf-8"))
            po_count += 1
            # 讀該 PO 的收貨記錄（JOIN）
            rec_file = rec_dir / (po["po_id"] + "_receipts.json")
            receipts = json.load(open(rec_file, encoding="utf-8")) if rec_file.exists() else []
            recv_by_sku = defaultdict(int)
            for r in receipts:
                recv_by_sku[r["sku_id"]] += r["received_qty"]
            for ln in po["lines"]:
                order_qty = ln["order_qty"]
                recv_qty  = recv_by_sku.get(ln["sku_id"], 0)
                gap       = order_qty - recv_qty
                if gap > 0:
                    nm = W.state()._items_by_sku.get(ln["sku_id"], {}).get("name", ln["sku_id"])
                    all_disc.append({
                        "po_id": po["po_id"], "date": po["date"], "warehouse": po["warehouse"],
                        "supplier": po["supplier"], "sku_id": ln["sku_id"], "name": nm,
                        "order_qty": order_qty, "received_qty": recv_qty, "gap": gap,
                    })
        _trace(steps, "read",
               f"scanned {po_count} purchase orders, JOINed receipts to compare "
               "ordered vs received")
        if all_disc:
            all_disc.sort(key=lambda d: d["gap"], reverse=True)
            _trace(steps, "reason",
                   f"found {len(all_disc)} short-received records, largest: {all_disc[0]['name']} "
                   f"({all_disc[0]['po_id']}) ordered {all_disc[0]['order_qty']} / "
                   f"received {all_disc[0]['received_qty']} → short {all_disc[0]['gap']} units")
            total_gap = sum(d["gap"] for d in all_disc)
            summary = (f"{len(all_disc)} purchase reconciliation issues found (PO mismatch), {total_gap} units short in total. "
                       f"Largest: {all_disc[0]['name']} short {all_disc[0]['gap']} units on {all_disc[0]['po_id']}.")
        else:
            _trace(steps, "reason", f"scanned {po_count} purchase orders, no shortfall found")
            summary = "Scan complete: no purchase shortfall issues found."
        return {"ok": True, "summary": summary, "view": "agent_rca",
                "data": {"keyword": keyword, "rows": [], "row_count": 0, "truncated": False,
                         "discrepancies": all_disc, "cause_found": bool(all_disc), "trace": steps}}

    # ① Glob：依 time_range 篩日期，依 source 篩檔名
    want_dates = _period_dates(time_range)
    all_files = sorted(tx_dir.glob("*.csv"))
    files = []
    for fp in all_files:
        stem = fp.stem  # YYYY-MM-DD_direction
        date_part = stem[:10]
        if want_dates is not None and date_part not in want_dates:
            continue
        if source and source not in stem:   # source 走 keyword 子字串比對（不 enum）
            continue
        files.append(fp)
    _trace(steps, "glob", f"scanned transactions/ → {len(files)}/{len(all_files)} log files matched",
           matched=len(files), total=len(all_files), time_range=time_range or "all")

    # ② Grep：逐檔找命中 SKU 的進出筆
    rows = []
    for fp in files:
        with open(fp, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if sku_ids and r["sku_id"] not in sku_ids:
                    continue
                rows.append({**r, "qty": int(r["qty"]), "_file": fp.name})
    rows.sort(key=lambda r: (r["date"], r["warehouse"]))
    MAX_ROWS = 200
    truncated = len(rows) > MAX_ROWS
    shown = rows[:MAX_ROWS]
    kw_disp = keyword or "all items"
    _trace(steps, "grep", f'matched "{kw_disp}" in transaction logs → {len(rows)} records'
           + (f" (showing first {MAX_ROWS})" if truncated else ""),
           hits=len(rows), truncated=truncated)

    # ③ RCA：3 大步驟 + sub_lines（PO 明細），不逐筆 _trace
    discrepancies = []
    if sku_ids:
        po_dir    = dd / "orders" / "PO"
        rec_dir   = dd / "receipts"
        po_dates  = want_dates
        sku_label = " / ".join(sku_names.values()) or keyword

        # ── Step A：掃採購單 ──
        relevant_pos = []
        for pj in sorted(po_dir.glob("*.json")):
            po = json.load(open(pj, encoding="utf-8"))
            if po_dates is not None and po["date"] not in po_dates:
                continue
            if any(ln["sku_id"] in sku_ids for ln in po["lines"]):
                relevant_pos.append(po)
        _trace(steps, "glob",
               f'scanned purchase orders (orders/PO) → {len(relevant_pos)} POs contain "{sku_label}"',
               sub_lines=[f"{p['po_id']}  {p['date']}  {p['warehouse']}  {p['supplier']}"
                          for p in relevant_pos[:4]]
               + ([f"…and {len(relevant_pos)-4} more"] if len(relevant_pos) > 4 else []))

        # ── Step B：逐張比對收貨記錄 ──
        compare_lines = []
        normal_count  = 0
        for po in relevant_pos:
            rec_file = rec_dir / (po["po_id"] + "_receipts.json")
            receipts = json.load(open(rec_file, encoding="utf-8")) if rec_file.exists() else []
            recv_by_sku  = defaultdict(int)
            recv_batches: dict[str, list[dict]] = defaultdict(list)
            for r in receipts:
                recv_by_sku[r["sku_id"]] += r["received_qty"]
                recv_batches[r["sku_id"]].append(r)
            for ln in po["lines"]:
                if ln["sku_id"] not in sku_ids:
                    continue
                sku       = ln["sku_id"]
                order_qty = ln["order_qty"]
                recv_qty  = recv_by_sku.get(sku, 0)
                gap       = order_qty - recv_qty
                batches   = recv_batches.get(sku, [])
                batch_str = ", ".join(
                    f"{b['receipt_date']} received {b['received_qty']}" for b in batches
                ) or "(no receipt records)"
                if gap > 0:
                    compare_lines.append(
                        f"⚠  {po['po_id']}  ordered {order_qty} / received {recv_qty} "
                        f"→ short {gap} units"
                        f"\n   Receipt batches: {batch_str}"
                    )
                    discrepancies.append({
                        "po_id": po["po_id"], "date": po["date"],
                        "warehouse": po["warehouse"], "supplier": po["supplier"],
                        "sku_id": sku, "name": sku_names.get(sku, sku),
                        "order_qty": order_qty, "received_qty": recv_qty, "gap": gap,
                        "batches": batches,
                    })
                else:
                    normal_count += 1
        # sub_lines 只顯示短收行（⚠），正常行合併為一行計數
        warn_lines   = [l for l in compare_lines if l.startswith("⚠")]
        ok_count     = normal_count
        display_lines = warn_lines[:6]
        if len(warn_lines) > 6:
            display_lines.append(f"…and {len(warn_lines)-6} more shortfalls")
        if ok_count:
            display_lines.append(f"✓  {ok_count} other POs are fine")
        _trace(steps, "read",
               f"compared receipts PO by PO → {len(relevant_pos)} checked",
               sub_lines=display_lines)

    # ④ Reason：產出結論
    sup_by_id = {s["supplier_id"]: s["name"] for s in W.state().v2_suppliers}
    WH_LABEL = {"north": "North", "central": "Central", "south": "South"}
    if discrepancies:
        d0 = discrepancies[0]
        sup_name = sup_by_id.get(d0["supplier"], d0["supplier"])
        wh_label = WH_LABEL.get(d0["warehouse"], d0["warehouse"])
        # 推理摘要：每步一行，最後是結論
        lines_out = [f"🔍 Item: {d0['name']}"]
        # 列出每張短收 PO（最多 3 筆）
        for d in discrepancies[:3]:
            wl = WH_LABEL.get(d["warehouse"], d["warehouse"])
            sl = sup_by_id.get(d["supplier"], d["supplier"])
            lines_out.append(
                f"📋 {d['po_id']} ({d['date']}, {wl}, {sl})\n"
                f"   ordered {d['order_qty']} / received {d['received_qty']} "
                f"→ short {d['gap']} units ⚠"
            )
        if len(discrepancies) > 3:
            lines_out.append(f"   …and {len(discrepancies)-3} more shortfalls")
        lines_out.append(
            f"✅ Conclusion: {len(discrepancies)} shortfalls totalling "
            f"{sum(d['gap'] for d in discrepancies)} units. "
            "Suggest contacting the supplier to confirm."
        )
        summary = "\n".join(lines_out)
        _trace(steps, "reason",
               f"confirmed {len(discrepancies)} shortfalls, largest {d0['po_id']} "
               f"short {d0['gap']} units")
        cause_found = True
    else:
        if rows:
            tin  = sum(r["qty"] for r in rows if r["direction"] == "in")
            tout = sum(r["qty"] for r in rows if r["direction"] == "out")
            if sku_ids:
                summary = (f"🔍 Item: {kw_disp}\n"
                           f"📋 Checked all related POs, no shortfall found\n"
                           f"✅ Conclusion: {tin} in, {tout} out - records are consistent.")
            else:
                summary = (f"🔍 Broad search \"{kw_disp}\": {len(rows)} movements\n"
                           f"   {tin} in, {tout} out\n"
                           f"💡 Type a specific item name to trace shortfall causes")
        else:
            summary = f"No movement records found for \"{kw_disp}\" in the given range."
        _trace(steps, "reason", "no shortfall found (POs checked)" if sku_ids
               else "broad search, no PO reconciliation")
        cause_found = False

    # 補充現存量 + 安全庫存，供第二輪 LLM 推理建議行動
    rca_context = {}
    if discrepancies and sku_ids:
        st  = W.state()
        stock_all = getattr(st, "stock", {}) or {}
        total_qty = sum(
            stock_all.get(wh, {}).get(sid, 0)
            for wh in stock_all for sid in sku_ids
        )
        ss_val = next(
            (it.get("safety_stock", 0) for it in st.items if it["sku_id"] in sku_ids),
            0,
        )
        total_gap = sum(d["gap"] for d in discrepancies)
        main_supplier = discrepancies[0]["supplier"] if discrepancies else ""
        rca_context = {
            "sku_ids": list(sku_ids),
            "sku_name": discrepancies[0]["name"] if discrepancies else keyword,
            "total_stock": total_qty,
            "safety_stock": ss_val,
            "total_gap": total_gap,
            "main_supplier": main_supplier,
            "disc_count": len(discrepancies),
        }

    return {
        "ok": True, "summary": summary, "view": "agent_rca",
        "data": {
            "keyword": keyword, "time_range": time_range, "source": source,
            "rows": shown, "row_count": len(rows), "truncated": truncated,
            "discrepancies": discrepancies, "cause_found": cause_found,
            "trace": steps, "rca_context": rca_context,
        },
    }


# ════════════════════════════════════════════════════════════
# ② manage_config — 設定檔讀寫（唯一會寫入；寫入要二次確認 + .bak + audit）
#    模型只抽意圖；實際寫入由 server 二次確認後 commit（見 commit_config_set）。
# ════════════════════════════════════════════════════════════
_KEY_ALIASES = {
    # EN build：英文別名補齊（原本只有 safety stock / lead time / buffer 三個，
    #   英文訪客常說的 reorder point / safety level / minimum stock 都對不到）
    "safety_stock":      ["安全庫存", "安全存量", "安全水位", "警戒值", "警戒水位", "安全量",
                          "庫存底線", "存量底線", "safety stock", "safety_stock",
                          "safety level", "reorder point", "reorder level",
                          "minimum stock", "min stock", "stock floor",
                          "safety threshold", "alert level"],
    "reorder_lead_days": ["前置天數", "補貨前置", "前置時間", "補貨天數", "lead time", "lead_days", "前置",
                          "lead days", "reorder lead", "restock lead",
                          "replenishment time", "delivery days"],
    "safety_buffer_ratio": ["安全水位倍數", "安全倍數", "buffer", "緩衝倍數",
                            "buffer ratio", "safety multiplier", "buffer multiplier"],
    "restock_target_days": ["補貨目標天數", "補到撐", "target days", "撐幾天",
                            "restock target", "target coverage", "days of cover"],
}

# EN build：設定項的訪客可見標籤（原本散在四處各寫一份中文 dict）
_CONFIG_LABEL_EN = {
    "safety_stock": "safety stock",
    "reorder_lead_days": "reorder lead time (days)",
    "safety_buffer_ratio": "safety buffer ratio",
    "restock_target_days": "restock target days",
}


def _resolve_key(key: str) -> str | None:
    """設定項 keyword → 正規 config key（不進模型 enum）。"""
    if not key:
        return None
    k = key.replace(" ", "").lower()
    # r25：最長別名優先——「安全水位倍數」曾被 safety_stock 的「安全水位」搶先
    # 命中，回錯設定項。全部 (別名, canon) 攤平後按長度降冪比對。
    pairs = [(canon, canon) for canon in _KEY_ALIASES]
    for canon, aliases in _KEY_ALIASES.items():
        pairs.extend((a, canon) for a in aliases)
    for alias, canon in sorted(pairs, key=lambda p: -len(p[0])):
        if alias.replace(" ", "").lower() in k:
            return canon
    return None


def _parse_value(value):
    """解析寫入值，判斷相對(+30/-5) vs 絕對(50)。回 (mode, number)。"""
    if value is None:
        return None, None
    sv = str(value).strip()
    # +N/-N 但 N 不是數字（LLM 把範例佔位符「+N」當值 → int('N') crash，
    # RPI5 conv100-r4 抓到）→ 當沒給值，回 None 讓上層轉 read/clarify
    def _numf(x):
        try:
            f = float(x)
            return int(f) if f == int(f) else f   # r26：保留小數（倍數1.5 曾被截成1）
        except ValueError:
            return None
    if sv.startswith("+"):
        n = _numf(sv[1:])
        return ("delta", n) if n is not None else (None, None)
    if sv.startswith("-"):
        n = _numf(sv[1:])
        return ("delta", -n) if n is not None else (None, None)
    n = _numf(sv)
    return ("abs", n) if n is not None else (None, None)


def manage_config(action: str = "read", key: str = "", value=None,
                  warehouse: str = "all", item: str = "") -> dict:
    # item：server 校正層從原句抽出的商品名（LLM 常漏抽，導致「瑜珈墊安全庫存
    # 加20」的影響範圍變成全部商品 183 項，RPI5 conv100-r5 抓到）
    steps: list[dict] = []
    cfg = W.state().v2_config
    # 指名了商品但庫裡沒有（server 校正層 r17 sentinel）：「吹風機安全庫存
    # 設成30」曾 fallback 成【全部商品】183 項確認卡 → 誠實說找不到
    if isinstance(item, str) and item.startswith("__unknown__:"):
        _uk = item.split(":", 1)[1]
        return {"ok": True, "view": "clarify", "summary": (
            f'No item found for "{_uk}" — nothing was changed. '
            'Please check the item name, e.g. '
            '"increase yoga mat safety stock by 20".'),
            "data": {"question": f'No item found for "{_uk}". '
                                 'Please check the item name',
                     "options": [], "hint": ""}}
    canon = _resolve_key(key)
    if not canon:
        # key 不是合法設定項（LLM 把「空間/容量」這種非設定問題誤投 manage_config）
        # → 不暴露內部設定項名，改友善引導（RPI5 v21：「倉庫空間夠不夠」露「哪個設定項:空間」）
        return {"ok": True, "view": "guide", "summary": (
            "I can adjust stock-related settings (safety stock, "
            "reorder lead time).\n"
            'Try: "set north safety stock to 50", '
            '"set reorder lead time to 7 days",\n'
            'or ask "what is the safety stock now" to check current settings.'
        ), "data": {}}

    # ── read ──
    if action == "read":
        _trace(steps, "read", f"read settings master/config.json → {canon}")
        if canon == "safety_stock":
            base = cfg.get("safety_stock_base", {})
            ov = cfg.get("safety_stock_override", {})
            # 若指定 keyword 對應某些 SKU，回那些；否則回整體說明
            skus = _kw_to_skus(item) or _kw_to_skus(key)  # item/key 可能含商品名
            rows = []
            target_skus = [it["sku_id"] for it in skus] or list(base.keys())[:10]
            for sku in target_skus:
                name = W._items_by_sku.get(sku, {}).get("name", sku) if hasattr(W, "_items_by_sku") \
                       else W.state()._items_by_sku.get(sku, {}).get("name", sku)
                eff = {}
                for wh in (["north", "central", "south"] if warehouse == "all" else [warehouse]):
                    eff[wh] = ov.get(wh, {}).get(sku, base.get(sku, 0))
                rows.append({"sku_id": sku, "name": name, "by_warehouse": eff, "base": base.get(sku, 0)})
            # 指名商品時把實際數值講出來（r24：「露營馬克杯的安全庫存設多少」
            # 曾只回「基準值寫在 config」的空話）；沒指名才回整體說明。
            if skus:
                _wh_lbl = {"north": "North", "central": "Central", "south": "South"}
                parts = []
                for r in rows[:3]:
                    vals = set(r["by_warehouse"].values())
                    if len(vals) == 1:
                        parts.append(f'"{r["name"]}" is set to {vals.pop()} '
                                     '(same across all 3 warehouses)')
                    else:
                        seg = ", ".join(f"{_wh_lbl[w]} {q}" for w, q in r["by_warehouse"].items())
                        parts.append(f'"{r["name"]}" {seg} (base {r["base"]})')
                summary = "Current safety stock: " + "; ".join(parts) + "."
            else:
                # r59：指定倉別時摘要要講出來（「只看南倉的」曾回不含倉別的泛話）
                _sc_lbl = {"north": "North", "central": "Central",
                           "south": "South"}.get(warehouse, "")
                summary = (f"Current {_sc_lbl} safety stock settings "
                           f"({len(rows)} items, including per-warehouse "
                           "overrides) are shown in the table below."
                           if _sc_lbl else
                           f"Current safety stock settings ({len(rows)} items): "
                           "base values live in config and can be overridden "
                           "per warehouse.")
            return {"ok": True, "summary": summary, "view": "config_read",
                    "data": {"canon": canon, "rows": rows, "trace": steps}}
        else:
            cur = cfg.get(canon)
            label = _CONFIG_LABEL_EN.get(canon, canon)
            summary = f"\"{label}\" is currently set to {cur}."
            return {"ok": True, "summary": summary, "view": "config_read",
                    "data": {"canon": canon, "current": cur, "label": label, "trace": steps}}

    # ── set：模型只到「抽出意圖」這步；回 pending_confirm 讓 server 二次確認 ──
    if action == "set":
        mode, num = _parse_value(value)
        if mode is None:
            # 沒給有效數值（含 LLM 佔位符「+N」）→ 不報 error，改 clarify 友善追問
            # （RPI5 conv100-r4：「安全水位要怎麼設定」諮詢句被判 set 卻無值）
            _lbl = _CONFIG_LABEL_EN.get(canon, "safety stock")
            return {"ok": True, "view": "clarify", "summary": (
                f'What should "{_lbl}" be set to? '
                f'e.g. "set {_lbl} to 50" or "increase by 30".'
            ), "data": {"canon": canon, "label": _lbl, "pending_config": True}}
        # 極端值防呆（r17：「設成十萬」中文數字修好後能正確解析 100000，
        # 但這數量級對 demo 資料絕非本意，會開出影響 183 項的確認卡）→ 追問
        if abs(num) > 9999:
            _lbl2 = _CONFIG_LABEL_EN.get(canon, "safety stock")
            return {"ok": True, "view": "clarify", "summary": (
                f'Setting "{_lbl2}" to {num:,} is unusual '
                "(normally between 0 and 9999). "
                "Please confirm the value and try again."),
                "data": {"question": f'Set "{_lbl2}" to {num:,}? '
                                     "Please confirm the value",
                         "options": [], "hint": ""}}

        # 只有安全水位倍數允許小數，其餘設定項取整（r26）
        if canon != "safety_buffer_ratio":
            num = int(num)

        # 算受影響範圍 + 預覽 diff（不寫入！）
        whs = ["north", "central", "south"] if warehouse == "all" else [warehouse]
        if canon == "safety_stock":
            base = cfg.get("safety_stock_base", {})
            ov = cfg.get("safety_stock_override", {})
            skus = _kw_to_skus(item) or _kw_to_skus(key)
            target_skus = [it["sku_id"] for it in skus] or list(base.keys())
            preview = []
            for sku in target_skus:
                name = W.state()._items_by_sku.get(sku, {}).get("name", sku)
                for wh in whs:
                    old = ov.get(wh, {}).get(sku, base.get(sku, 0))
                    new = old + num if mode == "delta" else num
                    new = max(0, new)
                    preview.append({"sku_id": sku, "name": name, "warehouse": wh, "old": old, "new": new})
            _trace(steps, "reason",
                   f"預覽：{'全部' if not skus else len(skus)} 商品 × {len(whs)} 倉 → 共 {len(preview)} 項異動")
            verb = (f"{'increase' if num >= 0 else 'decrease'} by {abs(num)}"
                    if mode == "delta" else f"set to {num}")
            wh_label = "all warehouses" if warehouse == "all" else \
                       {"north": "North", "central": "Central", "south": "South"}.get(warehouse, warehouse)
            scope = "all items" if not skus else ", ".join(it["name"] for it in skus[:3])
            summary = (f"About to {verb} the safety stock of [{scope}] "
                       f"in [{wh_label}], affecting {len(preview)} entries. "
                       "Please confirm to apply.")
            return {
                "ok": True, "summary": summary, "view": "config_confirm",
                "data": {
                    "pending": True, "canon": canon, "mode": mode, "num": num,
                    "warehouse": warehouse, "scope_skus": [it["sku_id"] for it in skus],
                    "preview": preview, "trace": steps,
                },
            }
        else:
            old = cfg.get(canon)
            new = (old + num) if mode == "delta" else num
            label = _CONFIG_LABEL_EN.get(canon, canon)
            summary = f"About to change \"{label}\" from {old} to {new}. Please confirm to apply."
            return {"ok": True, "summary": summary, "view": "config_confirm",
                    "data": {"pending": True, "canon": canon, "old": old, "new": new,
                             "label": label, "trace": steps}}

    return W._err(f"Unsupported config action: {action}")


def commit_config_set(pending: dict, actor: str = "user_confirmed",
                      trace_id: str | None = None) -> dict:
    """server 收到訪客『確認』後呼叫，真正寫入 config.json + .bak + audit log。"""
    dd = _data_dir()
    cfg_path = dd / "master" / "config.json"
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    canon = pending["canon"]
    ts = datetime.now().isoformat(timespec="seconds")
    trace_id = trace_id or f"cfg-{ts}"

    # 1) 寫前備份 .bak
    bak = cfg_path.with_suffix(".json.bak")
    bak.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2) 套用變更
    changed = 0
    if canon == "safety_stock":
        ov = cfg.setdefault("safety_stock_override", {})
        for p in pending["preview"]:
            ov.setdefault(p["warehouse"], {})[p["sku_id"]] = p["new"]
            changed += 1
    else:
        cfg[canon] = pending["new"]
        changed = 1

    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3) audit log（actor / trace_id / 誰確認的 — 對齊業界 HITL 規範）
    snap = W.state().snapshot_date or ts[:10]
    log_path = dd / "audit" / f"{snap}_changes.log"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": ts, "trace_id": trace_id, "actor": actor,
            "action": "config_set", "canon": canon,
            "detail": pending, "changed": changed,
        }, ensure_ascii=False) + "\n")

    # 4) 熱更新記憶體 state，讓後續查詢立即生效
    W.state().v2_config = cfg
    return {"ok": True, "summary": f"✅ {changed} entries saved, backed up to "
                                   "config.json.bak and recorded in the audit log.",
            "view": "config_done", "data": {"changed": changed, "trace_id": trace_id, "canon": canon}}


# ════════════════════════════════════════════════════════════
# ③ run_script — 白名單腳本執行（enum 走 manifest 比對，禁開放 Bash）
# ════════════════════════════════════════════════════════════
def _load_manifest() -> dict:
    dd = _data_dir()
    mp = dd / "scripts" / "manifest.json"
    if not mp.exists():
        return {"scripts": []}
    return json.load(open(mp, encoding="utf-8"))


_SCRIPT_VERB_PREFIX = ("幫我跑", "幫我執行", "幫我做", "請跑", "請執行", "跑一下", "跑個",
                       "跑一次", "做一次", "執行", "跑", "做", "產出", "產生", "重新產生")

def _match_script(script_name: str) -> dict | None:
    """腳本名 keyword → manifest 白名單項（fuzzy）。"""
    if not script_name:
        return None
    # 剝掉動詞前綴
    q = script_name.replace(" ", "")
    for prefix in sorted(_SCRIPT_VERB_PREFIX, key=len, reverse=True):
        if q.startswith(prefix):
            q = q[len(prefix):]
            break
    q = q.lower()
    for sc in _load_manifest().get("scripts", []):
        if sc["id"] in q or q in sc["id"]:
            return sc
        for a in sc.get("aliases", []):
            if a.replace(" ", "").lower() in q or q in a.replace(" ", "").lower():
                return sc
        label_q = sc.get("label", "").replace(" ", "")
        if label_q in q or q in label_q:
            return sc
    return None


def run_script(script_name: str = "", **_kw) -> dict:
    if not script_name and _kw:
        script_name = str(list(_kw.values())[0])
    steps: list[dict] = []
    sc = _match_script(script_name)
    _trace(steps, "read", f'matched against whitelist manifest.json → "{script_name}"')
    if not sc:
        _scripts = _load_manifest().get("scripts", [])
        avail = ", ".join(s["label"] for s in _scripts)
        return {"ok": True, "view": "clarify",
                "summary": f'"{script_name}" is not on the whitelist. '
                           f'Available: {avail}',
                "data": {"question": f'"{script_name}" is not on the whitelist. '
                                     'Which one do you want to run?',
                         # options 送回後端當查詢字串 → 直接用 manifest 的
                         #   label（已英文化），不能寫死中文
                         "options": [f"run {s['label']}" for s in _scripts],
                         "hint": ""}}

    # 安全護欄：只回「待確認」，不直接 subprocess（執行交給 server confirm 後）
    _trace(steps, "confirm", f"whitelisted script matched: {sc['label']} "
                             f"(timeout {sc['timeout_s']}s)")
    summary = f"About to run whitelisted script [{sc['label']}]: {sc.get('description', sc.get('desc', ''))}. Please confirm."
    return {"ok": True, "summary": summary, "view": "script_confirm",
            "data": {"pending": True, "script_id": sc["id"], "label": sc["label"],
                     "desc": sc.get("description", sc.get("desc", "")), "timeout_s": sc["timeout_s"], "trace": steps}}


# 白名單腳本實際指令（server confirm 後呼叫 commit_run_script）
_SCRIPT_CMD = {
    # id → (scripts/ 下的檔名, 額外 args)。路徑一律從 _data_dir() 推導——
    # 本機是 warehouse_v2/test/warehouse_data、RPI5 是 ~/warehouse_v2/warehouse_data
    # （扁平佈局），寫死 test/ 前綴會在 RPI5 找不到檔（r55 收官批抓到）。
    "stock_audit":      ("stock_audit.py",      []),
    "export_movements": ("export_movements.py", ["--days", "30"]),
    "generate_report":  ("generate_report.py",  ["--type", "full"]),
}


def commit_run_script(script_id: str, actor: str = "user_confirmed",
                      trace_id: str | None = None) -> dict:
    sc = next((s for s in _load_manifest().get("scripts", []) if s["id"] == script_id), None)
    if not sc:
        return W._err("Script not found")
    spec = _SCRIPT_CMD.get(script_id)
    if not spec:
        return W._err(f"Script {script_id} has no command bound")
    fname, extra = spec
    script_path = _data_dir() / "scripts" / fname
    extra = ["--data-dir", str(_data_dir()), *extra]
    root = _data_dir().parent                 # server.py 所在目錄（兩平台皆是）
    ts = datetime.now().isoformat(timespec="seconds")
    trace_id = trace_id or f"run-{ts}"

    if not script_path.exists():
        return W._err(f"Script file not found: {script_path.name}")

    try:
        import os as _os
        _env = _os.environ.copy()
        _env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, str(script_path), *extra],
            cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=sc["timeout_s"],
            env=_env,
        )
        ok = proc.returncode == 0
        tail = (proc.stdout or "")[-500:]
    except subprocess.TimeoutExpired:
        ok, tail = False, f"Timed out (>{sc['timeout_s']}s), aborted"
    except Exception as e:
        ok, tail = False, f"Execution failed: {e}"

    # audit
    snap = W.state().snapshot_date or ts[:10]
    with open(_data_dir() / "audit" / f"{snap}_changes.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "trace_id": trace_id, "actor": actor,
                            "action": "run_script", "script_id": script_id, "ok": ok},
                           ensure_ascii=False) + "\n")
    return {"ok": ok, "summary": f"Script [{sc['label']}] "
                                 f"{'completed' if ok else 'failed'}.",
            "view": "script_done", "data": {"script_id": script_id, "ok": ok,
                                            "output_tail": tail, "trace_id": trace_id}}


# ════════════════════════════════════════════════════════════
# ④ generate_report — 產生報告（A 波：Agent 自己寫檔案）
#    寫到 warehouse_data/reports/（沙盒內、免確認）。
#    report_type: full | low_stock | expiring | rca   （keyword 抽取，不嚴格 enum）
# ════════════════════════════════════════════════════════════
_REPORT_ALIASES = {
    "full":      ["全倉", "體檢", "總覽", "完整", "全部", "整體", "健檢", "盤點報告"],
    "low_stock": ["缺貨", "補貨", "低庫存", "安全庫存"],
    "expiring":  ["到期", "過期", "效期", "保存期限"],
    "rca":       ["異常", "對不上", "短收", "差異", "追查"],
}


def _resolve_report_type(rt: str) -> str:
    if not rt:
        return "full"
    k = rt.replace(" ", "").lower()
    for canon, aliases in _REPORT_ALIASES.items():
        if canon in k or any(a in rt for a in aliases):
            return canon
    return "full"


def _md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def generate_report(report_type: str = "full", actor: str = "agent_auto",
                    trace_id: str | None = None) -> dict:
    """掃全倉產出 markdown 報告，寫到 reports/。免確認（只寫專用目錄）。"""
    steps: list[dict] = []
    rt = _resolve_report_type(report_type)
    dd = _data_dir()
    reports_dir = dd / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    s = W.state()
    snap = s.snapshot_date or "?"
    ts = datetime.now().isoformat(timespec="seconds")
    trace_id = trace_id or f"rpt-{ts}"

    _trace(steps, "glob", f"scanned {len(s.warehouses)} warehouses / {len(s.items)} SKUs "
                          "to collect report data")

    _RT_TITLE = {"full": "Full Health Check", "low_stock": "Low Stock",
                 "expiring": "Expiring Items", "rca": "Reconciliation"}
    md = [f"# Warehouse Report — {_RT_TITLE.get(rt, rt)}",
          f"\n> Generated: {ts} | Data snapshot: {snap} | By: {actor} "
          f"(trace {trace_id})\n"]

    # ── 庫存總覽 ──
    if rt in ("full",):
        ds = W.dashboard_snapshot()
        _trace(steps, "reason", "compiling stock overview")
        rows = [[w["label"], f"{w['item_count']:,}", f"NT$ {w['stock_value']:,}"]
                for w in ds["warehouse_summary"]]
        md.append("## 1. Stock Overview")
        md.append(_md_table(["Warehouse", "Total Units", "Stock Value"], rows))
        md.append(f"\n- Total SKUs: {ds['sku_count']}　"
                  f"- Below safety stock: {ds['low_stock_count']}\n")

    # ── 缺貨警示 ──
    if rt in ("full", "low_stock"):
        r = W.execute("list_low_stock", {})
        warns = r.get("data", {}).get("warnings", []) if isinstance(r.get("data"), dict) else []
        _trace(steps, "read", f"read low-stock alerts → {len(warns)} items")
        md.append("## 2. Low Stock Alerts (days left / suggested reorder)")
        rows = [[w.get("name", ""), w.get("warehouse_label", ""), w.get("qty", ""),
                 w.get("days_left", ""), w.get("suggest_qty", "")] for w in warns[:30]]
        md.append(_md_table(["Item", "Warehouse", "On Hand", "Days Left", "Suggest"],
                            rows) if rows else "(none)")

    # ── 到期警示 ──
    if rt in ("full", "expiring"):
        r = W.execute("list_expiring_items", {})
        items = r.get("data", {}).get("rows", []) if isinstance(r.get("data"), dict) else []
        _trace(steps, "read", f"read expiring batches → {len(items)} items")
        md.append("## 3. Expiry Alerts")
        rows = [[f"{it.get('level_emoji','')} {it.get('name','')}", it.get("warehouse_label", ""),
                 it.get("days_to_expire", ""), it.get("qty", "")] for it in items[:30]]
        md.append(_md_table(["Item", "Warehouse", "Days Left", "Qty"], rows)
                  if rows else "(none)")

    # ── RCA 異常彙整（掃所有 PO 短收）──
    if rt in ("full", "rca"):
        po_dir = dd / "orders" / "PO"
        discs = []
        for pj in sorted(po_dir.glob("*.json")):
            po = json.load(open(pj, encoding="utf-8"))
            for ln in po["lines"]:
                if ln.get("note") == "short_received":
                    nm = s._items_by_sku.get(ln["sku_id"], {}).get("name", ln["sku_id"])
                    discs.append([po["po_id"], po["date"], po["warehouse"], nm,
                                  ln["order_qty"], ln["received_qty"],
                                  ln["order_qty"] - ln["received_qty"]])
        _trace(steps, "reason", f"compared ordered vs received across POs → "
                                f"{len(discs)} shortfalls found")
        md.append("## 4. Purchase Reconciliation Issues (PO shortfalls)")
        md.append(_md_table(["PO", "Date", "Warehouse", "Item", "Ordered",
                             "Received", "Short"], discs)
                  if discs else "(no issues)")

    # ── 報告圖表（matplotlib PNG）：full 報告嵌一張庫存市值長條圖 ──
    chart_file = None
    if rt in ("full", "low_stock"):
        try:
            chart_file = _render_report_chart(rt, ts, reports_dir)
            if chart_file:
                md.insert(2, f"\n![chart](./{chart_file})\n")
                _trace(steps, "act", f"rendered chart → reports/{chart_file}")
        except Exception as e:
            _trace(steps, "reason", f"chart skipped: {e}")

    md.append(f"\n---\n*Generated automatically by the Warehouse Agent · {trace_id}*")
    content = "\n".join(md)

    fname = f"{snap}_{rt}_report_{ts[11:19].replace(':', '')}.md"
    fpath = reports_dir / fname
    fpath.write_text(content, encoding="utf-8")
    _trace(steps, "act", f"wrote report → reports/{fname} ({len(content)} chars)")

    # audit（actor=agent_auto，記錄自動產出）
    with open(dd / "audit" / f"{snap}_changes.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "trace_id": trace_id, "actor": actor,
                            "action": "generate_report", "report_type": rt,
                            "file": fname}, ensure_ascii=False) + "\n")

    return {"ok": True,
            "summary": f"{_RT_TITLE.get(rt, rt)} report generated: reports/{fname}"
                       + (" (with chart)" if chart_file else ""),
            "view": "report_done",
            "data": {"report_type": rt, "file": fname, "path": str(fpath),
                     "chart": chart_file, "preview": content[:1200], "trace": steps}}


def _render_report_chart(rt: str, ts: str, reports_dir: Path) -> str | None:
    """產報告用 PNG 圖表（庫存市值 + 缺貨撐天）。回檔名。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    # EN build：圖表標籤已全英文 → 先找 Latin 字型（RPI5 沒中文字型時
    #   原設定會 fallback 到 DejaVu 並噴一堆 missing glyph warning）
    matplotlib.rcParams["font.sans-serif"] = ["DejaVu Sans", "Arial", "Liberation Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    s = W.state()

    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    # 左：各倉庫存市值
    ds = W.dashboard_snapshot()
    labels = [w["label"] for w in ds["warehouse_summary"]]
    vals = [w["stock_value"] for w in ds["warehouse_summary"]]
    axes[0].bar(labels, vals, color=["#4a90d9", "#5cb85c", "#e8a33d"])
    axes[0].set_title("Stock Value by Warehouse (NT$)")
    axes[0].ticklabel_format(axis="y", style="plain")
    for i, v in enumerate(vals):
        # 「萬」是中文計數單位 → 英文版用 K/M
        _lbl = f"{v/1e6:.1f}M" if v >= 1e6 else f"{v/1e3:.0f}K"
        axes[0].text(i, v, _lbl, ha="center", va="bottom", fontsize=9)

    # 右：缺貨 Top10 撐天
    r = W.execute("list_low_stock", {})
    warns = r.get("data", {}).get("warnings", []) if isinstance(r.get("data"), dict) else []
    warns = sorted([w for w in warns if w.get("days_left") is not None],
                   key=lambda w: w["days_left"])[:10]
    if warns:
        # 名稱截斷放寬到 18 字元——英文商品名比中文長，6 字元只剩 "Wirel"
        names = [w["name"][:18] for w in warns]
        days = [w["days_left"] for w in warns]
        colors = ["#d9534f" if d <= 7 else "#e8a33d" if d <= 14 else "#5bc0de" for d in days]
        axes[1].barh(names[::-1], days[::-1], color=colors[::-1])
        axes[1].set_title("Top 10 Running Out (days left)")
        axes[1].set_xlabel("days")
        axes[1].tick_params(axis="y", labelsize=7)
    else:
        axes[1].text(0.5, 0.5, "No low stock", ha="center")
    plt.tight_layout()

    fname = f"chart_{rt}_{ts[11:19].replace(':', '')}.png"
    fig.savefig(reports_dir / fname, dpi=90)
    plt.close(fig)
    return fname


# ════════════════════════════════════════════════════════════
# ⑤ list_files — 動態檔案發現（B 波：Agent 自己看有哪些檔可讀）
#    限定在 warehouse_data/ 沙盒內，不能跳出去（路徑穿越防護）。
# ════════════════════════════════════════════════════════════
_LISTABLE = {
    "transactions": "Transaction logs (split by date)",
    "orders": "Purchase / sales orders",
    "master": "Master data (items / suppliers / settings / stock)",
    "audit": "Change audit trail",
    "reports": "Generated reports",
    "scripts": "Whitelisted scripts",
}
# area 比對用的英文同義詞（label 已英文化，原本靠中文 label 拆字比對失效）
_LISTABLE_ALIAS = {
    "transactions": ("transaction", "movement", "log", "logs", "history"),
    "orders": ("order", "orders", "purchase", "po", "sales"),
    "master": ("master", "item", "items", "supplier", "suppliers",
               "setting", "settings", "config", "stock"),
    "audit": ("audit", "trail", "change", "changes"),
    "reports": ("report", "reports"),
    "scripts": ("script", "scripts", "whitelist"),
}


def list_files(area: str = "") -> dict:
    """列出 warehouse_data/ 下某區的檔案（Agent 動態看有什麼可讀）。"""
    steps: list[dict] = []
    dd = _data_dir()

    # 解析 area（keyword fuzzy，預設列所有區的概覽）
    target = None
    if area:
        a = area.replace(" ", "").lower()
        _a_words = set(area.lower().split())
        for k, label in _LISTABLE.items():
            if k in a or (_a_words & set(_LISTABLE_ALIAS.get(k, ()))):
                target = k
                break

    if target is None:
        # 沒指定 → 回各區概覽（檔數）
        _trace(steps, "glob", "scanned warehouse_data/ → listing readable areas")
        rows = []
        for k, label in _LISTABLE.items():
            d = dd / k
            if d.exists():
                n = sum(1 for _ in d.rglob("*") if _.is_file())
                rows.append({"area": k, "label": label, "file_count": n})
        return {"ok": True,
                "summary": f"warehouse_data/ has {len(rows)} readable areas.",
                "view": "file_list", "data": {"area": None, "rows": rows, "trace": steps}}

    # 指定區 → 列檔（路徑穿越防護：只允許 _LISTABLE 內的區）
    base = (dd / target).resolve()
    if not str(base).startswith(str(dd.resolve())):
        return W._err("Access outside the sandbox is not allowed")
    _trace(steps, "glob", f"listing files under {target}/")
    files = sorted(p for p in base.rglob("*") if p.is_file())
    MAX = 60
    rows = [{"name": str(p.relative_to(base)), "size": p.stat().st_size} for p in files[:MAX]]
    return {"ok": True,
            "summary": f"{target}/ contains {len(files)} files"
                       + (f" (showing first {MAX})" if len(files) > MAX else ""),
            "view": "file_list",
            "data": {"area": target, "label": _LISTABLE[target], "rows": rows,
                     "total": len(files), "trace": steps}}


# ════════════════════════════════════════════════════════════
# ⑥ set_alert — 自動化工具：邊緣警示規則設定（半固定 enum）
#    condition: below_safety | out_of_stock | expiring（不用自由字串，270M 好抽）
#    target: keyword（哪個商品/倉，可空=全部）
#    寫入 alert_rules.json，背景異常掃描會讀它（跟 anomaly.py 串）。
# ════════════════════════════════════════════════════════════
_ALERT_COND_ALIASES = {
    "below_safety": ["低於安全", "低於安全庫存", "安全庫存", "快缺", "庫存不足", "below safety"],
    "out_of_stock": ["缺貨", "斷貨", "沒貨", "零庫存", "out of stock", "斷料"],
    "expiring":     ["到期", "過期", "效期", "快過期", "保存期限", "expiring"],
}


def _resolve_condition(text: str) -> str | None:
    if not text:
        return None
    t = text.replace(" ", "").lower()
    for canon, al in _ALERT_COND_ALIASES.items():
        if canon in t or any(a.replace(" ", "").lower() in t for a in al):
            return canon
    return None


def set_alert(condition: str = "", target: str = "",
              threshold: int = None, raw_text: str = "") -> dict:
    """設定主動警示規則。寫到 alert_rules.json，背景掃描讀取。"""
    steps: list[dict] = []
    # raw_text fallback：Pre-C 直接傳入原始句子時，從中推斷 condition
    if not condition and raw_text:
        condition = raw_text
    cond = _resolve_condition(condition) or _resolve_condition(target)
    # below_threshold 是特殊條件，_resolve_condition 不認識，直接接受
    if not cond and condition == "below_threshold":
        cond = "below_threshold"
    # 預設：低於安全庫存警示（最常見意圖，不報錯）
    if not cond:
        cond = "below_safety"
    # target → SKU（可空=全部）
    skus = _kw_to_skus(target) if target else []
    scope = [it["sku_id"] for it in skus]
    scope_names = [it["name"] for it in skus]

    dd = _data_dir()
    rules_path = dd / "alert_rules.json"
    rules = []
    if rules_path.exists():
        rules = json.load(open(rules_path, encoding="utf-8")).get("rules", [])
    rule_id = f"AL{len(rules) + 1:03d}"

    _cond_labels = {"below_safety": "below safety stock", "out_of_stock": "out of stock",
                    "expiring": "快到期",
                    "below_threshold": f"低於 {threshold} 個" if threshold else "低於指定數量"}
    cond_label = _cond_labels.get(cond, cond)
    scope_txt = "all items" if not scope else ", ".join(scope_names[:3])
    _trace(steps, "reason", f"creating alert rule {rule_id}: {scope_txt} -> {cond_label}")

    # HITL：先回傳草稿讓使用者確認，commit_alert_set() 才真正寫入
    summary = f"On confirm, an alert will be set: notify when [{scope_txt}] hits \"{cond_label}\""
    return {"ok": True, "summary": summary, "view": "alert_confirm",
            "data": {"rule_id": rule_id, "condition": cond, "condition_label": cond_label,
                     "scope": scope, "scope_names": scope_names,
                     "rules_path": str(rules_path), "existing_rules": rules,
                     "trace": steps}}


# ════════════════════════════════════════════════════════════
# ⑦ generate_po — 閉環：缺貨/RCA → 自動產採購單草稿（待人確認）
#    source: low_stock | shortfall（短收補單）
#    產 PO 草稿到 orders/PO_draft/，HITL 確認後才轉正式 PO。
# ════════════════════════════════════════════════════════════
def generate_po(source: str = "low_stock") -> dict:
    """根據缺貨清單 / PO 短收，自動產一張採購單草稿（待確認）。"""
    steps: list[dict] = []
    s = W.state()
    src = ("shortfall" if any(w in str(source).lower() for w in
                              ("短收", "對不上", "shortfall", "rca", "short",
                               "discrepanc", "mismatch", "reconcil"))
           else "low_stock")

    lines = []
    if src == "low_stock":
        r = W.execute("list_low_stock", {})
        warns = r.get("data", {}).get("warnings", []) if isinstance(r.get("data"), dict) else []
        _trace(steps, "read", f"read low-stock list → {len(warns)} items")
        # 取建議補貨量 > 0 的，按最急（撐天少）排
        cand = [w for w in warns if w.get("suggest_qty", 0) > 0]
        cand.sort(key=lambda w: w.get("days_left", 999))
        for w in cand[:20]:
            lines.append({"sku_id": w["sku_id"], "name": w["name"],
                          "warehouse": w["warehouse"], "order_qty": w["suggest_qty"],
                          "reason": f"{w.get('days_left')} days left, "
                                    f"suggest ordering {w['suggest_qty']}"})
    else:
        # 短收補單：掃 PO 找 short_received
        dd = _data_dir() / "orders" / "PO"
        for pj in sorted(dd.glob("*.json")):
            po = json.load(open(pj, encoding="utf-8"))
            for ln in po["lines"]:
                if ln.get("note") == "short_received":
                    gap = ln["order_qty"] - ln["received_qty"]
                    nm = s._items_by_sku.get(ln["sku_id"], {}).get("name", ln["sku_id"])
                    lines.append({"sku_id": ln["sku_id"], "name": nm, "warehouse": po["warehouse"],
                                  "order_qty": gap,
                                  "reason": f"{po['po_id']} short {gap} units, reorder"})
        _trace(steps, "read", f"scanned POs for shortfalls → {len(lines)} to reorder")

    if not lines:
        return {"ok": True,
                "summary": "Nothing needs reordering right now — "
                           "no purchase order required.",
                "view": "po_confirm", "data": {"lines": [], "trace": steps}}

    # 對應供應商 + 算金額
    cat_sup = {}
    for sup in s.v2_suppliers:
        for c in sup.get("categories", "").split("|"):
            cat_sup[c] = sup["name"]
    total = 0
    for ln in lines:
        it = s._items_by_sku.get(ln["sku_id"], {})
        ln["unit_price"] = it.get("unit_price", 0)
        ln["amount"] = ln["unit_price"] * ln["order_qty"]
        ln["supplier"] = cat_sup.get(it.get("category", ""), "—")
        total += ln["amount"]
    _trace(steps, "reason", f"assembled PO draft: {len(lines)} lines, "
                            f"total NT$ {total:,}")

    return {"ok": True,
            "summary": ("Purchase order draft generated from the "
                        f"{'low-stock list' if src == 'low_stock' else 'shortfall records'}: "
                        f"{len(lines)} lines, estimated NT$ {total:,}. "
                        "Please confirm to submit."),
            "view": "po_confirm",
            "data": {"pending": True, "source": src, "lines": lines, "total": total, "trace": steps}}


def commit_po(pending: dict, actor: str = "user_confirmed", trace_id: str | None = None) -> dict:
    """訪客確認後，把草稿寫成正式 PO 草稿檔 + audit。"""
    dd = _data_dir()
    draft_dir = dd / "orders" / "PO_draft"
    draft_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    trace_id = trace_id or f"po-{ts}"
    po_id = f"POD{ts[11:19].replace(':', '')}"
    doc = {"po_id": po_id, "type": "PO_draft", "date": s_date(), "status": "draft",
           "created_by": actor, "lines": pending.get("lines", []), "total": pending.get("total", 0)}
    (draft_dir / f"{po_id}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    snap = W.state().snapshot_date or ts[:10]
    with open(dd / "audit" / f"{snap}_changes.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "trace_id": trace_id, "actor": actor,
                            "action": "generate_po", "po_id": po_id,
                            "lines": len(doc["lines"]), "total": doc["total"]},
                           ensure_ascii=False) + "\n")
    return {"ok": True, "summary": f"Purchase order draft {po_id} created ({len(doc['lines'])} lines, "
                                   f"NT$ {doc['total']:,}), saved to PO_draft/.",
            "view": "po_done", "data": {"po_id": po_id, "trace_id": trace_id, "lines": len(doc["lines"])}}


def commit_alert_set(pending: dict, actor: str = "user_confirmed", trace_id: str | None = None) -> dict:
    """使用者授權後，把警示規則寫入 alert_rules.json + audit。"""
    dd = _data_dir()
    rules_path = dd / "alert_rules.json"
    ts = datetime.now().isoformat(timespec="seconds")
    trace_id = trace_id or f"alert-{ts}"

    rules = pending.get("existing_rules", [])
    rule = {"id": pending["rule_id"], "condition": pending["condition"],
            "scope": pending.get("scope", []), "scope_names": pending.get("scope_names", []),
            "created": ts, "enabled": True}
    rules.append(rule)
    rules_path.write_text(json.dumps({"rules": rules}, ensure_ascii=False, indent=2), encoding="utf-8")

    snap = W.state().snapshot_date or ts[:10]
    with open(dd / "audit" / f"{snap}_changes.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "trace_id": trace_id, "actor": actor,
                            "action": "set_alert", "rule_id": pending["rule_id"],
                            "condition": pending["condition"],
                            "scope": pending.get("scope", [])},
                           ensure_ascii=False) + "\n")

    cond_label = pending.get("condition_label", pending["condition"])
    scope_names = pending.get("scope_names", [])
    scope_txt = "all items" if not scope_names else ", ".join(scope_names[:3])
    return {"ok": True,
            "summary": f"Alert rule {pending['rule_id']} is now active: notify when [{scope_txt}] hits \"{cond_label}\".",
            "view": "alert_done",
            "data": {"rule_id": pending["rule_id"], "condition": pending["condition"],
                     "condition_label": cond_label, "scope_names": scope_names, "trace_id": trace_id}}


# ════════════════════════════════════════════════════════════
# ⑧ 定時排程工具：set_schedule / list_schedules / delete_schedule
# ════════════════════════════════════════════════════════════
_SCHEDULE_SCRIPT_MAP = {
    "盤點":     "stock_audit",
    "月底盤點": "stock_audit",
    "進出記錄": "export_movements",
    "匯出":     "export_movements",
    "體檢報告": "generate_report",
    "報告":     "generate_report",
    "月報":     "generate_report",
    "週報":     "generate_report",
    # 「每天半夜兩點自動跑庫存體檢」只講「體檢」沒講「報告」（conv100-r6）
    "體檢":     "generate_report",
    "健檢":     "generate_report",
    # 「每天晚上七點自動出缺貨警示」→ 盤點腳本本來就是掃全倉跟安全庫存比對，
    # 語意等同缺貨檢查（RPI5 conv100-r5）
    "缺貨警示": "stock_audit",
    "庫存警示": "stock_audit",
    "缺貨":     "stock_audit",
    "警示":     "stock_audit",
    # 「每週三下午三點出貨報表」（conv100-r9）
    "出貨報表": "export_movements",
    "進出報表": "export_movements",
    "報表":     "generate_report",
}
_SCHEDULE_TIME_MAP = {
    "早上": "09:00", "上午": "09:00", "早": "09:00",
    "中午": "12:00", "下午": "14:00", "傍晚": "17:00",
    "晚上": "20:00", "晚": "20:00", "凌晨": "02:00",
}
_SCHEDULE_FREQ_MAP = {
    "每天": "daily", "每日": "daily", "天天": "daily",
    "每週": "weekly", "每周": "weekly", "每星期": "weekly",
    "每月": "monthly", "每個月": "monthly", "月底": "monthly",
}

_CN_HOUR = {"零": 0, "一": 1, "兩": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
            "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def _parse_schedule_intent(text: str) -> dict:
    """從自然語言解析排程意圖。
    回傳 {script_id, freq, time_str, freq_explicit, time_explicit}——
    explicit 旗標讓 set_schedule 知道原句有沒有明講，明講的才覆蓋 LLM 給的參數。"""
    import re as _re
    script_id = next((v for k, v in _SCHEDULE_SCRIPT_MAP.items() if k in text), None)
    _freq_hit = next((v for k, v in _SCHEDULE_FREQ_MAP.items() if k in text), None)
    freq = _freq_hit or "daily"
    # 解析時間（幾點）——阿拉伯數字或中文數字（「八點」「十一點」，第9輪測試補：
    # 原本只認阿拉伯，「每天早上八點」落到「早上」預設 09:00 跟既有排程撞名）
    time_str = None
    m = _re.search(r'([0-9]{1,2}|十[一二]?|[一兩二三四五六七八九])\s*[點:](\d{0,2})', text)
    if m:
        g = m.group(1)
        h = int(g) if g.isdigit() else _CN_HOUR.get(g, 9)
        mi = int(m.group(2)) if m.group(2) else 0
        # 下午/晚上 + 12 小時制轉換
        if h < 12 and any(w in text for w in ("下午", "晚上", "傍晚", "晚間", "夜裡")):
            h += 12
        time_str = f"{h:02d}:{mi:02d}"
    else:
        _t_hit = next((v for k, v in _SCHEDULE_TIME_MAP.items() if k in text), None)
        time_str = _t_hit
    return {"script_id": script_id, "freq": freq, "time_str": time_str or "09:00",
            "freq_explicit": _freq_hit is not None,
            "time_explicit": (m is not None) or (time_str is not None)}

def set_schedule(script_name: str = "", freq: str = "daily", time_str: str = "09:00",
                 raw_text: str = "") -> dict:
    """設定定時排程：讓 Agent 在指定時間自動執行腳本。

    raw_text 明講的 freq/時間一律優先於 LLM 傳的參數——LLM 常自己亂填
    script_name 導致原本「只有 script_name 為空才解析 raw_text」的邏輯整段
    跳過，freq 停在預設 daily 而誤判成重複排程（第9輪測試抓到：
    「每週一匯出進出報表」被回「已有相同排程 daily」）。"""
    if raw_text:
        parsed = _parse_schedule_intent(raw_text)
        if not script_name:
            script_name = parsed["script_id"] or script_name
        if parsed["freq_explicit"]:
            freq = parsed["freq"]
        if parsed["time_explicit"]:
            time_str = parsed["time_str"]

    sc = _match_script(script_name or "")
    if not sc:
        labels = "、".join(s["label"] for s in _load_manifest().get("scripts", []))
        return W._err(f'Script "{script_name}" not found. Available: {labels}')

    dd = _data_dir()
    jobs_path = dd / "schedule_jobs.json"
    jobs = []
    if jobs_path.exists():
        jobs = json.loads(jobs_path.read_text("utf-8")).get("jobs", [])

    # 防止重複——script + freq + 時間三者都相同才算重複
    # （同腳本同頻率但不同時間是合法的兩個排程，第9輪測試修）
    existing = next((j for j in jobs if j["script_id"] == sc["id"]
                     and j["freq"] == freq and j.get("time_str") == time_str), None)
    if existing:
        # r74：訪客說「看警示」被 alias 對到盤點腳本，回「已有月底盤點」讓人一頭
        # 霧水——點明兩者是同一件事
        _alias_note = ("（盤點腳本就是掃全倉比對安全庫存，缺貨警示由它負責）"
                       if sc["id"] == "stock_audit"
                       and any(w in (raw_text or "") for w in ("警示", "缺貨")) else "")
        return {"ok": True, "view": "clarify",
                "summary": f"A matching schedule already exists: {sc['label']} {freq} "
                           f"{existing['time_str']} (ID: {existing['id']}){_alias_note}. "
                           "No need to set it up again.",
                "data": {"question": f'Schedule "{sc["label"]}" ({freq} {existing["time_str"]}) is already '
                                     "running. Tell me if you want to change the time or cancel it.",
                         "options": [], "hint": ""}}

    _freq_labels = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}
    freq_label = _freq_labels.get(freq, freq)
    job_id = f"SCH{len(jobs)+1:03d}"

    summary = f"On confirm, a schedule will be set: run [{sc['label']}] automatically {freq_label} at {time_str}"
    return {"ok": True, "summary": summary, "view": "schedule_confirm",
            "data": {"job_id": job_id, "script_id": sc["id"], "script_label": sc["label"],
                     "freq": freq, "freq_label": freq_label, "time_str": time_str}}


def commit_schedule_set(pending: dict, actor: str = "user", trace_id: str = "") -> dict:
    """使用者確認後真正寫入 schedule_jobs.json 並通知 APScheduler。"""
    import datetime as _dt
    dd = _data_dir()
    jobs_path = dd / "schedule_jobs.json"
    jobs = []
    if jobs_path.exists():
        jobs = json.loads(jobs_path.read_text("utf-8")).get("jobs", [])
    ts = _dt.datetime.now().isoformat(timespec="seconds")
    new_job = {
        "id":           pending["job_id"],
        "script_id":    pending["script_id"],
        "script_label": pending["script_label"],
        "freq":         pending["freq"],
        "freq_label":   pending["freq_label"],
        "time_str":     pending["time_str"],
        "enabled":      True,
        "created":      ts,
        "actor":        actor,
    }
    jobs.append(new_job)
    jobs_path.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True,
            "summary": f"Schedule created: [{pending['script_label']}] will run "
                                   f"{pending['freq_label']} at {pending['time_str']}",
            "view": "schedule_done", "data": {"job": new_job}}


def list_schedules() -> dict:
    """列出所有定時排程。"""
    dd = _data_dir()
    jobs_path = dd / "schedule_jobs.json"
    if not jobs_path.exists():
        return {"ok": True, "summary": "No scheduled jobs right now.", "view": "schedule_list",
                "data": {"jobs": []}}
    jobs = json.loads(jobs_path.read_text("utf-8")).get("jobs", [])
    active = [j for j in jobs if j.get("enabled", True)]
    summary = f"{len(active)} scheduled jobs are active."
    return {"ok": True, "summary": summary, "view": "schedule_list", "data": {"jobs": active}}


def delete_schedule(job_id: str = "") -> dict:
    """刪除指定排程（HITL：先回確認卡，commit_delete_schedule() 才真正刪）。"""
    if not job_id:
        return W._err("Please specify a schedule ID (e.g. SCH001)")
    dd = _data_dir()
    jobs_path = dd / "schedule_jobs.json"
    if not jobs_path.exists():
        return W._err("Schedule file not found")
    data = json.loads(jobs_path.read_text("utf-8"))
    jobs = data.get("jobs", [])
    job = next((j for j in jobs if j["id"] == job_id), None)
    if not job:
        return W._err(f"Schedule {job_id} not found")
    return {"ok": True,
            "summary": f"Delete schedule {job_id} [{job.get('script_label', '')}]? "
                       "This cannot be undone.",
            "view": "schedule_delete_confirm",
            "data": {"job_id": job_id, "job": job}}


def commit_delete_schedule(job_id: str = "", actor: str = "user_confirmed", trace_id: str | None = None) -> dict:
    """使用者確認後，真正從 schedule_jobs.json 刪除排程 + audit。"""
    dd = _data_dir()
    jobs_path = dd / "schedule_jobs.json"
    if not jobs_path.exists():
        return W._err("找不到排程檔")
    data = json.loads(jobs_path.read_text("utf-8"))
    jobs = data.get("jobs", [])
    new_jobs = [j for j in jobs if j["id"] != job_id]
    if len(new_jobs) == len(jobs):
        return W._err(f"找不到排程 {job_id}")
    jobs_path.write_text(json.dumps({"jobs": new_jobs}, ensure_ascii=False, indent=2), encoding="utf-8")
    ts = datetime.now().isoformat(timespec="seconds")
    trace_id = trace_id or f"schdel-{ts}"
    with open(dd / "audit" / f"{s_date()}_changes.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "trace_id": trace_id, "actor": actor,
                            "action": "delete_schedule", "job_id": job_id},
                           ensure_ascii=False) + "\n")
    return {"ok": True, "summary": f"Schedule {job_id} deleted.",
            "view": "schedule_deleted", "data": {"job_id": job_id}}


def list_alerts() -> dict:
    """列出目前所有已啟用的警示規則。"""
    dd = _data_dir()
    rules_path = dd / "alert_rules.json"
    if not rules_path.exists():
        return {"ok": True, "summary": "No alert rules set up right now.", "view": "alert_list",
                "data": {"rules": []}}
    rules = json.load(open(rules_path, encoding="utf-8")).get("rules", [])
    active = [r for r in rules if r.get("enabled", True)]
    _cond_labels = {"below_safety": "below safety stock", "out_of_stock": "out of stock",
                    "expiring": "快到期", "below_threshold": "低於指定數量"}
    for r in active:
        r["condition_label"] = _cond_labels.get(r["condition"], r["condition"])
        r["scope_txt"] = "all items" if not r.get("scope_names") else ", ".join(r["scope_names"][:3])
    summary = f"{len(active)} alert rules are active."
    return {"ok": True, "summary": summary, "view": "alert_list", "data": {"rules": active}}


def delete_alert(rule_id: str = "") -> dict:
    """刪除指定 ID 的警示規則（HITL：先回確認卡，commit_delete_alert() 才真正刪）。"""
    if not rule_id:
        return W._err("Please specify the rule ID to delete (e.g. AL001)")
    dd = _data_dir()
    rules_path = dd / "alert_rules.json"
    if not rules_path.exists():
        return W._err("Alert rules file not found")
    data = json.load(open(rules_path, encoding="utf-8"))
    rules = data.get("rules", [])
    rule = next((r for r in rules if r["id"] == rule_id), None)
    if not rule:
        return W._err(f"Rule {rule_id} not found")
    _cond_labels = {"below_safety": "below safety stock", "out_of_stock": "out of stock",
                    "expiring": "快到期", "below_threshold": "低於指定數量"}
    cond_label = _cond_labels.get(rule.get("condition"), rule.get("condition", ""))
    return {"ok": True,
            "summary": f"Delete alert rule {rule_id} [{cond_label}]? This cannot be undone.",
            "view": "alert_delete_confirm",
            "data": {"rule_id": rule_id, "rule": rule, "condition_label": cond_label}}


def commit_delete_alert(rule_id: str = "", actor: str = "user_confirmed", trace_id: str | None = None) -> dict:
    """使用者確認後，真正從 alert_rules.json 刪除規則 + audit。"""
    dd = _data_dir()
    rules_path = dd / "alert_rules.json"
    if not rules_path.exists():
        return W._err("找不到警示規則檔")
    data = json.load(open(rules_path, encoding="utf-8"))
    rules = data.get("rules", [])
    before = len(rules)
    rules = [r for r in rules if r["id"] != rule_id]
    if len(rules) == before:
        return W._err(f"找不到規則 {rule_id}")
    rules_path.write_text(json.dumps({"rules": rules}, ensure_ascii=False, indent=2), encoding="utf-8")
    ts = datetime.now().isoformat(timespec="seconds")
    trace_id = trace_id or f"aldel-{ts}"
    with open(dd / "audit" / f"{s_date()}_changes.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "trace_id": trace_id, "actor": actor,
                            "action": "delete_alert", "rule_id": rule_id},
                           ensure_ascii=False) + "\n")
    return {"ok": True, "summary": f"Alert rule {rule_id} deleted.", "view": "alert_deleted",
            "data": {"rule_id": rule_id}}


def s_date():
    return W.state().snapshot_date or "2026-05-26"


# ════════════════════════════════════════════════════════════
# ⑧ compare_periods — 跨期比較：這個月 vs 上個月 哪些變化大
# ════════════════════════════════════════════════════════════
def compare_periods(metric: str = "out") -> dict:
    """比較最近兩個月的出庫量，找變化最大的 SKU。"""
    steps: list[dict] = []
    s = W.state()
    from datetime import date as _d, timedelta as _td
    today = _d.fromisoformat(s.snapshot_date or "2026-05-26")
    this_start = today - _td(days=30)
    last_start = today - _td(days=60)
    _trace(steps, "glob", f"split into two periods: current {this_start}~{today} / "
                          f"previous {last_start}~{this_start}")

    this_p = defaultdict(int)
    last_p = defaultdict(int)
    for m in s.movements:
        if m["direction"] != "out":
            continue
        d = _d.fromisoformat(m["date"])
        if this_start <= d <= today:
            this_p[m["sku_id"]] += m["qty"]
        elif last_start <= d < this_start:
            last_p[m["sku_id"]] += m["qty"]

    rows = []
    for sku in set(this_p) | set(last_p):
        a, b = last_p.get(sku, 0), this_p.get(sku, 0)
        if a == 0 and b == 0:
            continue
        delta = b - a
        pct = (delta / a * 100) if a else (100 if b else 0)
        nm = s._items_by_sku.get(sku, {}).get("name", sku)
        rows.append({"sku_id": sku, "name": nm, "last": a, "this": b,
                     "delta": delta, "pct": round(pct, 1)})
    rows.sort(key=lambda r: abs(r["delta"]), reverse=True)
    _trace(steps, "reason", f"computed change for {len(rows)} SKUs, "
                            "taking the 15 largest swings")

    top = rows[:15]
    up = [r for r in top if r["delta"] > 0][:3]
    down = [r for r in top if r["delta"] < 0][:3]
    parts = []
    if up:
        parts.append("Biggest growth: "
                     + ", ".join(f"{r['name']} (+{r['delta']})" for r in up))
    if down:
        parts.append("Biggest decline: "
                     + ", ".join(f"{r['name']} ({r['delta']})" for r in down))
    summary = ("Outbound change over the last two months — " + "; ".join(parts)
               if parts else "No significant change between the two periods.")
    return {"ok": True, "summary": summary, "view": "period_compare",
            "data": {"rows": top, "trace": steps}}


# ════════════════════════════════════════════════════════════
# ④ create_item — 自然語言新增商品（分步引導 + HITL）
# ════════════════════════════════════════════════════════════
_CATEGORY_PREFIX = {
    "electronics": "e", "appliance_kitchen": "a", "food_beverage": "f",
    "daily_goods": "d", "apparel": "c", "sports": "s",
}

def _next_sku(category: str) -> str:
    """自動產生下一個 SKU 流水號"""
    prefix = _CATEGORY_PREFIX.get(category, "x")
    existing = [it["sku_id"] for it in W.state().items if it["sku_id"].startswith(prefix)]
    nums = []
    for sid in existing:
        try:
            nums.append(int(sid[1:]))
        except ValueError:
            pass
    next_num = max(nums) + 1 if nums else 1
    return f"{prefix}{next_num:02d}"


def create_item_start() -> dict:
    """觸發新增商品流程，回第一步問題"""
    return {
        "ok": True,
        "summary": "Sure! Step 1: what is the item called? "
                   '(any name works, e.g. "Reusable Straw")',
        "view": "item_create_step1",
        "data": {"step": 1, "total_steps": 4, "prompt": "Enter the item name"},
    }


def create_item_collect(step: int = 1, name: str = "", category: str = "",
                         price: str = "", safety: str = "", stock_north: str = "0",
                         stock_central: str = "0", stock_south: str = "0",
                         raw_text: str = "") -> dict:
    """收集訪客輸入，依 step 推進流程"""
    # 如果 raw_text 有內容，嘗試從中解析多個欄位（老手一句話模式）
    if raw_text and step == 1:
        import re as _re
        # 嘗試解析：名稱 + 類別 + 價格 + 安全庫存 + 倉庫庫存
        _cat_map = {"電子": "electronics", "家電": "appliance_kitchen", "食品": "food_beverage",
                     "飲料": "food_beverage", "日用": "daily_goods", "服飾": "apparel", "運動": "sports"}
        _found_cat = next((v for k, v in _cat_map.items() if k in raw_text), "")
        _price_m = _re.search(r'(\d+)\s*元', raw_text)
        _safety_m = _re.search(r'安全\s*(\d+)', raw_text)
        _north_m = _re.search(r'北\S*\s*(\d+)', raw_text)
        _south_m = _re.search(r'南\S*\s*(\d+)', raw_text)
        _central_m = _re.search(r'中\S*\s*(\d+)', raw_text)
        # 去掉已知欄位後剩下的當名稱
        _name = raw_text
        for pat in [r'電子\S*', r'家電\S*', r'食品\S*', r'日用\S*', r'服飾\S*', r'運動\S*',
                     r'\d+元', r'安全\d+', r'北\S*\d+', r'南\S*\d+', r'中\S*\d+', r'新增商品\s*']:
            _name = _re.sub(pat, '', _name).strip()
        if _name and _found_cat:
            # 防呆：檢查同名
            if any(it["name"] == _name for it in W.state().items):
                return {"ok": True, "summary": f'⚠️ Item "{_name}" already exists. Please use a different name.',
                        "view": "item_create_step1", "data": {"step": 1, "prompt": "請輸入不同的商品名稱"}}
            new_sku = _next_sku(_found_cat)
            pending = {
                "name": _name, "category": _found_cat,
                "category_label": W.CATEGORY_LABEL.get(_found_cat, _found_cat),
                "price": int(_price_m.group(1)) if _price_m else 0,
                "safety": int(_safety_m.group(1)) if _safety_m else 0,
                "stock_north": int(_north_m.group(1)) if _north_m else 0,
                "stock_central": int(_central_m.group(1)) if _central_m else 0,
                "stock_south": int(_south_m.group(1)) if _south_m else 0,
                "sku": new_sku,
            }
            return {"ok": True, "summary": "Item details parsed — please confirm", "view": "item_confirm",
                    "data": {"pending": True, "item": pending}}
        # r75：只給了名稱沒給類別（「新增商品 保溫杯」）→ 名稱接進分步流程，
        # 從第二步問類別（過去靜默丟掉名稱、空名前進）
        if _name and not name:
            name = _name

    # 分步模式
    if step == 1:
        # r75 危險級：名稱空白曾一路推進到建出商品「」（「幫我新增商品」的殘字
        # 走 raw_text 解析失敗後掉進這裡）——空名一律留在第一步重問
        if not (name or "").strip():
            return create_item_start()
        # 防呆：檢查是否已有同名商品
        existing = [it for it in W.state().items if it["name"] == name]
        if existing:
            return {"ok": True, "summary": f'⚠️ Item "{name}" already exists (SKU: {existing[0]["sku_id"]}). '
                           "Please use a different name.",
                    "view": "item_create_step1",
                    "data": {"step": 1, "prompt": "請輸入不同的商品名稱"}}
        return {"ok": True,
                "summary": f'Name recorded: "{name}"\n'
                           "Step 2: which category? electronics / appliance & "
                           "kitchen / food & beverage / daily goods / apparel / "
                           'sports (say "cancel" to exit)',
                "view": "item_create_step2",
                "data": {"step": 2, "name": name, "prompt": 'Choose a category (or say "cancel" to exit)'}}
    elif step == 2:
        # r75：類別欄要驗證＋正規化成主檔 key——「陶瓷馬克杯」曾被當類別吸收
        # 造成整條流程欄位錯位；中文原字入檔會生出幻影類別（SKU 也拿到 x 前綴）
        _cat_zh2key = {"電子": "electronics", "3c": "electronics",
                       "家電": "appliance_kitchen", "廚具": "appliance_kitchen", "廚房": "appliance_kitchen",
                       "食品": "food_beverage", "飲料": "food_beverage",
                       "日用": "daily_goods", "生活": "daily_goods",
                       "服飾": "apparel", "衣": "apparel",
                       "運動": "sports"}
        _cat_key = next((v for k, v in _cat_zh2key.items()
                         if k in (category or "").lower()), "")
        if not _cat_key and category in _cat_zh2key.values():
            _cat_key = category
        if not _cat_key:
            return {"ok": True,
                    "summary": (f'"{category}" is not a category. Please pick one of: '
                                "electronics / appliance & kitchen / food & beverage / "
                                'daily goods / apparel / sports (say "cancel" to exit)'),
                    "view": "item_create_step2",
                    "data": {"step": 2, "name": name,
                             "prompt": "請選擇類別（或輸入「取消」退出）"}}
        category = _cat_key
        _cat_lbl2 = W.CATEGORY_LABEL.get(category, category)
        return {"ok": True,
                "summary": f'Recorded: "{name}" → {_cat_lbl2}\n'
                           "Step 3: unit price and safety stock?\n"
                           'e.g. "150 100" (say "cancel" to exit)',
                "view": "item_create_step3",
                "data": {"step": 3, "name": name, "category": category,
                         "prompt": "格式：單價 安全庫存（例如 150 100，或輸入取消）"}}
    elif step == 3:
        # dispatch 已把 "100 20" 拆成 price=100, safety=20 → 直接取整數
        # 若 safety 沒值 → 從 price 字串再拆一次
        try:
            if safety and safety != "0":
                price_val = int(price)
                safety_val = int(safety)
            else:
                raw_ps = (price or "").replace("元", " ").replace("件", " ").replace("，", ",")
                nums = [int(p.strip()) for p in raw_ps.replace(" ", ",").split(",") if p.strip().lstrip("-").isdigit()]
                price_val = nums[0] if len(nums) >= 1 else 0
                safety_val = nums[1] if len(nums) >= 2 else 0
        except (ValueError, IndexError):
            return W._err(f"Invalid price or safety stock: {price} / {safety}")
        # r75：輸入裡沒有數字（「廚具」曾被吸成單價0/安全0 靜默過關）→ 留在
        # 第三步重問，不帶 0 值前進
        if price_val <= 0:
            return {"ok": True,
                    "summary": ("Step 3 needs numbers: unit price and safety stock?\n"
                                'e.g. "150 100" (say "cancel" to exit)'),
                    "view": "item_create_step3",
                    "data": {"step": 3, "name": name, "category": category,
                             "prompt": 'Format: price safety_stock '
                                       '(e.g. "150 100", or say "cancel")'}}
        return {"ok": True,
                "summary": f"Recorded: unit price {price_val}, "
                           f"safety stock {safety_val}\n"
                           "Step 4 (optional): set initial stock?\n"
                           'Enter three numbers (north central south), '
                           'e.g. "50 30 20"\n'
                           'or say "skip" to set all to 0',
                "view": "item_create_step4",
                "data": {"step": 4, "name": name, "category": category,
                         "price": price_val, "safety": safety_val,
                         "prompt": 'Format: north central south '
                                   '(e.g. "50 30 20") or say "skip"'}}
    elif step == 4:
        # 支援 positional 格式：10 20 30 → 北10 中20 南30
        raw_stock = str(stock_north) if stock_north else ""
        if not any(kw in raw_stock for kw in ("北", "中", "南", "跳")):
            parts = raw_stock.replace(",", " ").split()
            nums = [int(p) for p in parts if p.lstrip("-").isdigit()]
            if len(nums) == 3:
                stock_north, stock_central, stock_south = str(nums[0]), str(nums[1]), str(nums[2])
        try:
            sn = int(stock_north) if stock_north else 0
            sc = int(stock_central) if stock_central else 0
            ss = int(stock_south) if stock_south else 0
        except ValueError:
            sn = sc = ss = 0
        new_sku = _next_sku(category)
        pending = {
            "name": name, "category": category,
            "category_label": W.CATEGORY_LABEL.get(category, category),
            "price": int(price) if price else 0,
            "safety": int(safety) if safety else 0,
            "stock_north": sn, "stock_central": sc, "stock_south": ss,
            "sku": new_sku,
        }
        stock_summary = f"North {sn}, Central {sc}, South {ss}" if (sn+sc+ss) > 0 else "all 0"
        return {"ok": True,
                "summary": f"📦 準備新增「{name}」\n類別：{pending['category_label']} | 單價：{pending['price']}元 | 安全庫存：{pending['safety']}件\n初始庫存：{stock_summary}",
                "view": "item_confirm",
                "data": {"pending": True, "item": pending}}

    return W._err(f"Unknown step: {step}")


def commit_create_item(pending: dict, actor: str = "user_confirmed",
                       trace_id: str | None = None) -> dict:
    """HITL 確認後寫入 items.csv + config.json + stock.csv"""
    import csv, shutil
    dd = _data_dir()
    ts = __import__('datetime').datetime.now().isoformat(timespec="seconds")
    trace_id = trace_id or f"item-{ts}"
    item = pending["item"] if "item" in pending else pending
    # r75 危險級縱深防禦：名稱空白的商品絕不落地（曾建出商品「」污染主檔）
    if not str(item.get("name", "")).strip():
        return W._err('Item name is empty, cannot add. Please start over with "add item".')

    # 1. 寫入 items.csv
    items_path = dd / "master" / "items.csv"
    shutil.copy2(items_path, str(items_path) + ".bak")
    with open(items_path, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([item["sku"], item["name"], item["category"],
                         item.get("category_label", ""), item["price"], item["safety"]])

    # 2. 寫入 config.json（安全庫存 base）
    cfg_path = dd / "master" / "config.json"
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    cfg.setdefault("safety_stock_base", {})[item["sku"]] = item["safety"]
    shutil.copy2(cfg_path, str(cfg_path) + ".bak")
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3. 寫入 stock.csv（初始庫存）—— 進鎖，跟進出貨/調貨的寫入序列化
    with _STOCK_LOCK:
        stock_path = dd / "master" / "stock.csv"
        shutil.copy2(stock_path, str(stock_path) + ".bak")
        with open(stock_path, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            for wh, qty in [("north", item.get("stock_north", 0)),
                             ("central", item.get("stock_central", 0)),
                             ("south", item.get("stock_south", 0))]:
                if qty > 0:
                    writer.writerow([wh, item["sku"], qty])

    # 4. audit log
    audit_dir = dd / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    log_path = audit_dir / f"{ts[:10]}_changes.log"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "trace_id": trace_id, "actor": actor,
                            "action": "create_item", "item": item}, ensure_ascii=False) + "\n")

    # 5. 熱更新記憶體（直接塞進 State，不依賴 seed_data.json）
    import warehouse as W_mod
    s = W_mod._STATE
    new_sku = item["sku"]
    new_item_entry = {
        "sku_id":       new_sku,
        "name":         item["name"],
        "category":     item["category"],
        "unit_price":   item["price"],
        "safety_stock": item["safety"],
    }
    if not any(it["sku_id"] == new_sku for it in s.items):
        s.items.append(new_item_entry)
        s._items_by_sku[new_sku] = s.items[-1]
    for wh_key in ("north", "central", "south"):
        qty = item.get(f"stock_{wh_key}", 0)
        s.stock.setdefault(wh_key, {})[new_sku] = qty

    # 6. 持久化到 warehouse_data/master/（重啟後不消失）
    master = Path(__file__).parent / "warehouse_data" / "master"
    # items.csv：若 SKU 不存在才追加
    items_path = master / "items.csv"
    existing = list(csv.DictReader(open(items_path, encoding="utf-8-sig")))
    if not any(r["sku_id"] == new_sku for r in existing):
        fieldnames = list(existing[0].keys()) if existing else ["sku_id","name","category","category_label","unit_price","safety_stock","shelf_life_days"]
        with open(items_path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            row = {k: "" for k in fieldnames}
            row.update({"sku_id": new_sku, "name": item["name"],
                        "category": item["category"], "category_label": item.get("category_label",""),
                        "unit_price": item["price"], "safety_stock": item["safety"]})
            w.writerow(row)
    # stock.csv：更新或新增各倉庫存（全檔重寫 → 進鎖防並發蓋寫）
    with _STOCK_LOCK:
        stock_path = master / "stock.csv"
        stock_rows = list(csv.DictReader(open(stock_path, encoding="utf-8-sig")))
        updated = {(r["warehouse"], r["sku_id"]): r for r in stock_rows}
        for wh_key in ("north", "central", "south"):
            qty = item.get(f"stock_{wh_key}", 0)
            updated[(wh_key, new_sku)] = {"warehouse": wh_key, "sku_id": new_sku, "qty": qty}
        with open(stock_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["warehouse","sku_id","qty"])
            w.writeheader(); w.writerows(updated.values())

    return {"ok": True, "summary": f'✅ Item "{item["name"]}" added (SKU: {item["sku"]})',
            "view": "item_done", "data": {"item": item, "trace_id": trace_id}}


# ════════════════════════════════════════════════════════════
# ⑧ create_movement — 自然語言即時進出貨（輕量版，非完整 PO/SO 單據）
#    「北倉進了藍牙耳機50件」/「南倉出貨洗衣精20件」→ HITL 確認 → 真寫入
#    stock.csv + transactions/，重開 server / 網頁重整不會消失。
# ════════════════════════════════════════════════════════════
def _movement_dir_label(direction: str) -> str:
    """direction 可能是內部代碼 in/out，或原始中文詞；統一轉成給使用者看的標籤。
    EN build：回英文標籤——這個值會被拼進 clarify 的 options，而 options
    是**會送回後端的查詢字串**，中文的話英文版後端會直接 reject（訪客一點
    就壞）。"""
    d = direction or ""
    if any(w in d for w in ("進", "in")):
        return "received"
    if any(w in d for w in ("出", "out")):
        return "shipped"
    return d


def create_movement(keyword: str = "", warehouse: str = "", direction: str = "",
                     qty: str = "", is_return: bool = False) -> dict:
    """觸發進出貨流程：找商品/倉別 → 算庫存變化 → 回確認卡（不執行寫入）。
    is_return=True 表示客人退貨（庫存增加，走 in 的算法，但顯示/紀錄標「退貨」）。"""
    if not keyword:
        return W._err('Please tell me which item to update, '
                      'e.g. "north received 50 bluetooth earphones".')

    scored = W.match_items(keyword)
    if not scored:
        # r17：找不到商品是輸入問題不是系統錯誤 → clarify 藍卡而非 error 紅卡
        return {"ok": True, "view": "clarify",
                "summary": f'No item found for "{keyword}". '
                           'Please check the item name and try again.',
                "data": {"question": f'No item found for "{keyword}". '
                                     'Please check the item name',
                         "options": [],
                         "hint": 'e.g. "north received 50 bluetooth earphones"'}}
    # 分數斷層過濾（同 warehouse.query_inventory 的邏輯）：避免共用規格 token
    # （如「1L」「男款」）讓不相干商品低分命中、誤觸發多筆 clarify。
    if len(scored) > 1:
        top_score = scored[0]["score"]
        scored = [m for m in scored if m["score"] * 2 >= top_score]
    matches = [m["item"] for m in scored]
    if len(matches) > 1:
        opts = [it["name"] for it in matches[:5]]
        _dir_label = _movement_dir_label(direction)
        _qty_txt = f" {qty}" if qty else ""
        # options 會被送回後端當查詢字串 → 組成後端聽得懂的英文句
        # （`north received 50 Wireless Mouse`），不是純標籤
        _wh_txt = f"{warehouse} " if warehouse else ""
        return {"ok": True,
                "summary": f'"{keyword}" matches {len(matches)} items. '
                           'Which one do you want to update?',
                "view": "clarify",
                "data": {"question": f'"{keyword}" matches {len(matches)} items. '
                                     'Which one do you want to update?',
                         "options": [f"{_wh_txt}{_dir_label}{_qty_txt} {n}".strip()
                                     for n in opts],
                         "hint": "Please give the full item name"}}
    item = matches[0]
    sku = item["sku_id"]

    # r56：數量上限/負數要在「問倉別」之前攔——「進99999999個氣泡水」缺倉別時
    # 曾先問倉，訪客答完倉才見上限（或根本沒攔）
    try:
        _pre_qty = int(str(qty).strip() or 0)
    except ValueError:
        _pre_qty = 0
    if _pre_qty > 9999:
        return {"ok": True, "view": "clarify",
                "summary": (f"{_pre_qty:,} units in one go is unusual "
                            "(limit is 9,999 per operation). "
                            "Please confirm the quantity and try again."),
                "data": {"question": f"Update {_pre_qty:,} units? "
                                     "Please confirm the quantity",
                         "options": [], "hint": ""}}

    wh = (warehouse or "").strip()
    _WH_ALIASES = {"north": "north", "北": "north", "北倉": "north", "North": "north", "北區": "north",
                   "central": "central", "中": "central", "中倉": "central", "Central": "central", "中區": "central",
                   "south": "south", "南": "south", "南倉": "south", "South": "south", "南區": "south"}
    wh_key = _WH_ALIASES.get(wh, "")
    if not wh_key:
        _dir_label_en = _movement_dir_label(direction) or "received"
        _qty_txt = f"{qty}" if qty else "50"
        return {"ok": True,
                "summary": f'Which warehouse for "{item["name"]}"?',
                "view": "clarify",
                "data": {"question": f'Which warehouse for "{item["name"]}"?',
                         # options 是送回後端的查詢字串 → 組成完整英文寫入句
                         "options": [f"north {_dir_label_en} {_qty_txt} {item['name']}",
                                     f"central {_dir_label_en} {_qty_txt} {item['name']}",
                                     f"south {_dir_label_en} {_qty_txt} {item['name']}"],
                         "hint": 'e.g. "north {} {} {}"'.format(
                             _dir_label_en, _qty_txt, item['name']),
                         # r56：寫入續流——追問倉別後訪客只答「北倉」也要能接回進出貨
                         # （曾變成庫存查詢、流程斷裂）。server WS 層讀這包重呼叫。
                         "flow": {"tool": "create_movement", "await": "warehouse",
                                  "keyword": item["name"], "direction": direction,
                                  "qty": str(qty), "is_return": is_return}}}

    # 退貨（客人退回來）= 庫存增加，走 in 的算法，不需要判斷方向詞
    if is_return:
        dir_key = "in"
    else:
        dir_key = "in" if any(w in direction for w in ("進", "入", "到貨", "收貨", "in")) else \
                  "out" if any(w in direction for w in ("出", "出貨", "出庫", "賣", "out")) else ""
    if not dir_key:
        return W._err('Please say whether it is inbound or outbound, '
                      f'e.g. "{wh} received {qty or 50} {item["name"]}".')

    try:
        qty_val = int(str(qty).strip() or 0)
    except ValueError:
        qty_val = 0
    _dir_en = "returned" if is_return else ("received" if dir_key == "in" else "shipped")
    if qty_val < 0:
        # r17：「北倉進貨-20個耳機」負號曾被吞、開出 +20 卡（語意反轉）
        return {"ok": True, "view": "clarify",
                "summary": ("Quantity cannot be negative. To reduce stock, "
                            'use "shipped", e.g. '
                            f'"{WH_LABEL_MAP.get(wh_key, wh_key)} shipped '
                            f'{abs(qty_val)} {item["name"]}".'),
                "data": {"question": "Quantity cannot be negative, "
                                     "please rephrase",
                         "options": [], "hint": ""}}
    if qty_val == 0:
        # 缺數量 → clarify 追問（曾是 error 紅字卡，r17 統一成 clarify）
        return {"ok": True, "view": "clarify",
                "summary": f'How many {item["name"]} were {_dir_en}? '
                           f'e.g. "{_dir_en} 50".',
                "data": {"question": f'How many {item["name"]} were {_dir_en}?',
                         "options": [f"{wh} {_dir_en} 10 {item['name']}",
                                     f"{wh} {_dir_en} 30 {item['name']}",
                                     f"{wh} {_dir_en} 50 {item['name']}"],
                         "hint": f'e.g. "{_dir_en} 50"',
                         "flow": {"tool": "create_movement", "await": "qty",
                                  "keyword": item["name"], "warehouse": wh,
                                  "direction": direction, "is_return": is_return}}}
    if qty_val > 9999:
        # r17：999999 件這種展場搗蛋數字不開卡，追問確認
        return {"ok": True, "view": "clarify",
                "summary": (f"{qty_val:,} units {_dir_en} in one go is unusual "
                            "(limit is 9,999 per operation). "
                            "Please confirm the quantity and try again."),
                "data": {"question": f"{_dir_en.capitalize()} {qty_val:,} units? "
                                     "Please confirm the quantity",
                         "options": [], "hint": ""}}

    s = W.state()
    current_qty = s.stock.get(wh_key, {}).get(sku, 0)
    new_qty = current_qty + qty_val if dir_key == "in" else current_qty - qty_val

    if dir_key == "out" and new_qty < 0:
        return {"ok": False,
                "summary": f'⚠️ Not enough stock to ship. "{item["name"]}" has '
                           f'only {current_qty} units in {WH_LABEL_MAP[wh_key]}, '
                           f'short of {qty_val}.',
                "view": "error", "data": {}}

    wh_label = WH_LABEL_MAP[wh_key]
    # 退貨顯示「退貨」、圖示不同，但庫存跟 in 一樣是加（sign=+）
    dir_label = "Return" if is_return else ("Inbound" if dir_key == "in" else "Outbound")
    icon = "↩️" if is_return else "📦"
    sign = "+" if dir_key == "in" else "-"
    summary = (f"{icon} Confirm {dir_label}\n"
               f"Item: {item['name']} ({sku})\n"
               f"Warehouse: {wh_label}\n"
               f"Quantity: {sign}{qty_val} units\n"
               f"Current stock: {current_qty} → after: {new_qty} units")
    return {"ok": True, "summary": summary, "view": "movement_confirm",
            "data": {"pending": True, "sku": sku, "name": item["name"], "warehouse": wh_key,
                     "warehouse_label": wh_label, "direction": dir_key, "direction_label": dir_label,
                     "qty": qty_val, "before_qty": current_qty, "after_qty": new_qty,
                     "is_return": is_return}}


WH_LABEL_MAP = {"north": "North", "central": "Central", "south": "South"}


def commit_movement(pending: dict, actor: str = "user_confirmed",
                     trace_id: str | None = None) -> dict:
    """HITL 確認後真正寫入 stock.csv + transactions/ + 熱更新記憶體。

    確認卡上的 before/after 只是「開卡當下」的預覽——開卡到按確認之間庫存
    可能被別的操作改過（另一張卡 / 另一台裝置），所以 commit 一律在鎖內
    重讀當下庫存、重新驗證、重新計算，絕不直接寫 pending 帶來的絕對值
    （否則會把中間發生的異動整筆蓋掉，帳就對不上了）。
    """
    import shutil
    dd = _data_dir()
    ts = datetime.now().isoformat(timespec="seconds")
    trace_id = trace_id or f"mv-{ts}"
    p = pending
    sku, wh_key, dir_key = p["sku"], p["warehouse"], p["direction"]
    qty_val = int(p["qty"])
    if qty_val <= 0:
        return W._err("Invalid quantity, cannot write.")

    with _STOCK_LOCK:
        # 0. 在鎖內重讀當下庫存 → 重驗 → 重算（防 TOCTOU 陳舊寫入）
        s = W.state()
        current = s.stock.get(wh_key, {}).get(sku, 0)
        if dir_key == "out":
            if qty_val > current:
                return {"ok": False,
                        "summary": ('⚠️ Stock changed — cannot ship. '
                                    f'"{p["name"]}" now has only {current} units '
                                    f'in {p["warehouse_label"]}, short of {qty_val} '
                                    '(stock was changed by another operation '
                                    'after this card was created).'),
                        "view": "error", "data": {}}
            after_qty = current - qty_val
        else:
            after_qty = current + qty_val

        # 1. 更新 stock.csv（找到既有那行、改數字；沒有就新增一行）
        stock_path = dd / "master" / "stock.csv"
        shutil.copy2(stock_path, str(stock_path) + ".bak")
        rows = list(csv.DictReader(open(stock_path, encoding="utf-8-sig")))
        found = False
        for r in rows:
            if r["warehouse"] == wh_key and r["sku_id"] == sku:
                r["qty"] = str(after_qty)
                found = True
                break
        if not found:
            rows.append({"warehouse": wh_key, "sku_id": sku, "qty": str(after_qty)})
        with open(stock_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["warehouse", "sku_id", "qty"])
            w.writeheader(); w.writerows(rows)

        # 2. 補一筆 transactions/{today}_{in|out}.csv（稽核軌跡）
        snap_date = W.state().snapshot_date or ts[:10]
        tx_dir = dd / "transactions"
        tx_dir.mkdir(parents=True, exist_ok=True)
        tx_path = tx_dir / f"{snap_date}_{dir_key}.csv"
        is_new = not tx_path.exists()
        with open(tx_path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["date", "sku_id", "warehouse", "direction", "qty"])
            w.writerow([snap_date, sku, wh_key, dir_key, qty_val])

        # 3. audit log（退貨標 create_return，交易紀錄仍記 in，方便 RCA/報表統一處理）
        audit_dir = dd / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        _audit_action = "create_return" if p.get("is_return") else "create_movement"
        with open(audit_dir / f"{snap_date}_changes.log", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts, "trace_id": trace_id, "actor": actor,
                                "action": _audit_action, "sku": sku, "warehouse": wh_key,
                                "direction": dir_key, "qty": qty_val,
                                "before": current, "after": after_qty},
                               ensure_ascii=False) + "\n")

        # 4. 熱更新記憶體
        s.stock.setdefault(wh_key, {})[sku] = after_qty

    _done = {**p, "before_qty": current, "after_qty": after_qty}
    return {"ok": True,
            "summary": f"✅ {p['direction_label']} recorded. {p['name']} now has "
                       f"{after_qty} units in {p['warehouse_label']}.",
            "view": "movement_done", "data": {"trace_id": trace_id, **_done}}


# ════════════════════════════════════════════════════════════
# ⑧b create_transfer / commit_transfer — 跨倉調貨（A 倉 → B 倉）
#    調貨 = 同時扣來源倉、加目標倉，總量不變。走 HITL 確認卡（同進出貨），
#    來源倉不足擋下，交易紀錄拆成「來源倉 out + 目標倉 in」兩筆（跟現有
#    transactions 格式一致，RCA/報表完全不用改）。2026-07-02 新增。
# ════════════════════════════════════════════════════════════
_WH_ALIASES_TF = {"north": "north", "北": "north", "北倉": "north", "North": "north", "北區": "north",
                  "central": "central", "中": "central", "中倉": "central", "Central": "central", "中區": "central",
                  "south": "south", "南": "south", "南倉": "south", "South": "south", "南區": "south"}


def create_transfer(keyword: str = "", from_wh: str = "", to_wh: str = "",
                    qty: str = "") -> dict:
    """觸發調貨流程：找商品 → 解析來源/目標倉 → 檢查來源庫存 → 回確認卡（不寫入）。"""
    if not keyword:
        return {"ok": True, "view": "clarify",
                "summary": "Which item do you want to transfer? "
                           'Just say the item name, e.g. "toilet paper".',
                "data": {"question": "Which item do you want to transfer?",
                         "options": [], "hint": ""}}

    scored = W.match_items(keyword)
    if not scored:
        # r17：找不到商品是輸入問題不是系統錯誤 → clarify 藍卡而非 error 紅卡
        return {"ok": True, "view": "clarify",
                "summary": f'No item found for "{keyword}". '
                           'Please check the item name and try again.',
                "data": {"question": f'No item found for "{keyword}". '
                                     'Please check the item name',
                         "options": [],
                         "hint": 'e.g. "transfer 20 bluetooth earphones '
                                 'from north to south"'}}
    if len(scored) > 1:
        top_score = scored[0]["score"]
        scored = [m for m in scored if m["score"] * 2 >= top_score]
    matches = [m["item"] for m in scored]
    if len(matches) > 1:
        opts = [it["name"] for it in matches[:5]]
        # options 送回後端 → 組成完整英文調貨句（缺的倉留給後續 clarify 問）
        _rt = (f" from {from_wh}" if from_wh else "") + (f" to {to_wh}" if to_wh else "")
        _q = f"{qty} " if qty else ""
        return {"ok": True,
                "summary": f'"{keyword}" matches {len(matches)} items. '
                           'Which one do you want to transfer?',
                "view": "clarify",
                "data": {"question": f'"{keyword}" matches {len(matches)} items. '
                                     'Which one do you want to transfer?',
                         "options": [f"transfer {_q}{n}{_rt}".strip() for n in opts],
                         "hint": "Please give the full item name"}}
    item = matches[0]
    sku = item["sku_id"]

    from_key = _WH_ALIASES_TF.get((from_wh or "").strip(), "")
    to_key = _WH_ALIASES_TF.get((to_wh or "").strip(), "")
    if not from_key or not to_key:
        _q20 = qty or 20
        return {"ok": True,
                "summary": f'Which warehouse to which for "{item["name"]}"?',
                "view": "clarify",
                "data": {"question": f'Which warehouse to which for '
                                     f'"{item["name"]}"?',
                         "options": [f"transfer {_q20} {item['name']} from north to south",
                                     f"transfer {_q20} {item['name']} from south to north",
                                     f"transfer {_q20} {item['name']} from central to north"],
                         "hint": 'e.g. "transfer {} {} from north to south"'.format(
                             _q20, item['name']),
                         # r61：帶上已知的單邊倉——「調10個去南倉」只缺來源，
                         # 訪客答「從北倉調」單邊也要能補
                         "flow": {"tool": "create_transfer", "await": "route",
                                  "keyword": item["name"], "qty": str(qty),
                                  "from_wh": from_wh or "", "to_wh": to_wh or ""}}}
    if from_key == to_key:
        return W._err("Source and destination warehouse cannot be the same. "
                      "Please confirm where to transfer from and to.")

    try:
        qty_val = int(str(qty).strip() or 0)
    except ValueError:
        qty_val = 0
    if qty_val <= 0:
        # 缺數量→clarify 追問（非 error 紅字）。RPI5 conv100-r2：「調一批…到」
        # 模糊量詞無精確數，友善問數量比報錯好。帶已知商品/來源/目標讓前端可續填。
        _kwn = keyword or ""
        _kwq = f' "{_kwn}"' if _kwn else ""
        return {"ok": True, "view": "clarify",
                "summary": f"How many{_kwq} do you want to transfer? "
                           'Please give a quantity, e.g. "transfer 20".',
                "data": {"pending_transfer": True, "keyword": _kwn,
                         "from_wh": from_wh, "to_wh": to_wh,
                         "flow": {"tool": "create_transfer", "await": "qty",
                                  "keyword": _kwn, "from_wh": from_wh, "to_wh": to_wh}}}

    s = W.state()
    from_cur = s.stock.get(from_key, {}).get(sku, 0)
    to_cur = s.stock.get(to_key, {}).get(sku, 0)
    if qty_val > from_cur:
        return {"ok": False,
                "summary": f'⚠️ Not enough stock to transfer. "{item["name"]}" has '
                           f'only {from_cur} units in {WH_LABEL_MAP[from_key]}, '
                           f'short of {qty_val}.',
                "view": "error", "data": {}}

    from_label, to_label = WH_LABEL_MAP[from_key], WH_LABEL_MAP[to_key]
    summary = (f"🔄 Confirm Transfer\n"
               f"Item: {item['name']} ({sku})\n"
               f"Quantity: {qty_val} units\n"
               f"{from_label}: {from_cur} → {from_cur - qty_val} units\n"
               f"{to_label}: {to_cur} → {to_cur + qty_val} units")
    return {"ok": True, "summary": summary, "view": "transfer_confirm",
            "data": {"pending": True, "sku": sku, "name": item["name"],
                     "from_wh": from_key, "from_label": from_label,
                     "to_wh": to_key, "to_label": to_label, "qty": qty_val,
                     "from_before": from_cur, "from_after": from_cur - qty_val,
                     "to_before": to_cur, "to_after": to_cur + qty_val}}


def commit_transfer(pending: dict, actor: str = "user_confirmed",
                    trace_id: str | None = None) -> dict:
    """HITL 確認後真正寫入：來源倉扣、目標倉加，交易記兩筆（out + in），熱更新記憶體。

    同 commit_movement：在鎖內重讀當下庫存、重驗來源倉足夠、重算 after，
    不直接寫 pending 帶來的絕對值（防 TOCTOU 陳舊寫入蓋掉中間異動）。
    """
    import shutil
    dd = _data_dir()
    ts = datetime.now().isoformat(timespec="seconds")
    trace_id = trace_id or f"tf-{ts}"
    p = pending
    sku = p["sku"]
    from_key, to_key = p["from_wh"], p["to_wh"]
    qty_val = int(p["qty"])
    if qty_val <= 0:
        return W._err("Invalid quantity, cannot transfer.")

    with _STOCK_LOCK:
        # 0. 鎖內重讀 → 重驗 → 重算
        s = W.state()
        from_cur = s.stock.get(from_key, {}).get(sku, 0)
        to_cur = s.stock.get(to_key, {}).get(sku, 0)
        if qty_val > from_cur:
            return {"ok": False,
                    "summary": ('⚠️ Stock changed — cannot transfer. '
                                f'"{p["name"]}" now has only {from_cur} units '
                                f'in {p["from_label"]}, short of {qty_val} '
                                '(stock was changed by another operation '
                                'after this card was created).'),
                    "view": "error", "data": {}}
        from_after = from_cur - qty_val
        to_after = to_cur + qty_val

        # 1. 更新 stock.csv（來源倉、目標倉兩行都改）
        stock_path = dd / "master" / "stock.csv"
        shutil.copy2(stock_path, str(stock_path) + ".bak")
        rows = list(csv.DictReader(open(stock_path, encoding="utf-8-sig")))
        _seen_from = _seen_to = False
        for r in rows:
            if r["sku_id"] == sku and r["warehouse"] == from_key:
                r["qty"] = str(from_after); _seen_from = True
            elif r["sku_id"] == sku and r["warehouse"] == to_key:
                r["qty"] = str(to_after); _seen_to = True
        if not _seen_from:
            rows.append({"warehouse": from_key, "sku_id": sku, "qty": str(from_after)})
        if not _seen_to:
            rows.append({"warehouse": to_key, "sku_id": sku, "qty": str(to_after)})
        with open(stock_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["warehouse", "sku_id", "qty"])
            w.writeheader(); w.writerows(rows)

        # 2. 交易紀錄拆兩筆：來源倉 out、目標倉 in（跟現有格式一致）
        snap_date = W.state().snapshot_date or ts[:10]
        tx_dir = dd / "transactions"
        tx_dir.mkdir(parents=True, exist_ok=True)
        for _dir, _wh in (("out", from_key), ("in", to_key)):
            tx_path = tx_dir / f"{snap_date}_{_dir}.csv"
            is_new = not tx_path.exists()
            with open(tx_path, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if is_new:
                    w.writerow(["date", "sku_id", "warehouse", "direction", "qty"])
                w.writerow([snap_date, sku, _wh, _dir, qty_val])

        # 3. audit log
        audit_dir = dd / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        with open(audit_dir / f"{snap_date}_changes.log", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts, "trace_id": trace_id, "actor": actor,
                                "action": "create_transfer", "sku": sku,
                                "from_wh": from_key, "to_wh": to_key, "qty": qty_val,
                                "from_before": from_cur, "from_after": from_after,
                                "to_before": to_cur, "to_after": to_after},
                               ensure_ascii=False) + "\n")

        # 4. 熱更新記憶體
        s.stock.setdefault(from_key, {})[sku] = from_after
        s.stock.setdefault(to_key, {})[sku] = to_after

    _done = {**p, "from_before": from_cur, "from_after": from_after,
             "to_before": to_cur, "to_after": to_after}
    return {"ok": True,
            "summary": (f"✅ Transfer complete. {qty_val} units of {p['name']} "
                        f"moved from {p['from_label']} to {p['to_label']}.\n"
                        f"{p['from_label']} now has {from_after} units, "
                        f"{p['to_label']} now has {to_after} units."),
            "view": "transfer_done", "data": {"trace_id": trace_id, **_done}}


# ════════════════════════════════════════════════════════════
# ⑨ reset_demo_data — 展示資料一鍵重置（防止展場被玩爛回不去）
#    warehouse_data_baseline/ 是展前建立的乾淨快照，重置 = 整個資料夾換回去。
#    不走對話式 dispatch，走前端獨立按鈕 + 密碼驗證（server.py /api/reset_demo）。
# ════════════════════════════════════════════════════════════
_RESET_PASSWORD = "0000"


def commit_reset_demo_data(password: str = "", actor: str = "user_confirmed",
                            trace_id: str | None = None) -> dict:
    """密碼驗證通過後，把 warehouse_data/ 整個換回 warehouse_data_baseline/ 並重新載入 State。"""
    if password != _RESET_PASSWORD:
        return W._err("Wrong password, cannot reset")

    import shutil
    ts = datetime.now().isoformat(timespec="seconds")
    trace_id = trace_id or f"reset-{ts}"
    root = Path(__file__).parent
    baseline = root / "warehouse_data_baseline"
    current = root / "warehouse_data"
    if not baseline.exists():
        return W._err("Baseline snapshot warehouse_data_baseline/ not found, cannot reset")

    shutil.rmtree(current)
    shutil.copytree(baseline, current)

    # 重新載入 State（跟開機 init() 用同一份 seed_path）
    W.reset()

    return {"ok": True, "summary": "✅ Demo data has been reset to its initial state.",
            "view": "reset_done", "data": {"trace_id": trace_id}}


# 原始 60 項商品的 SKU 白名單（不可刪除）
_PROTECTED_SKUS = {
    f"{p}{i:02d}"
    for p in ("e", "a", "f", "d", "c", "s")
    for i in range(1, 11)
}


def delete_item_start(keyword: str = "") -> dict:
    """觸發刪除流程：找商品 → HITL 確認 → 軟刪除"""
    if not keyword:
        return W._err("Please specify the item name or SKU to delete")
    matches = W.match_items(keyword)
    if not matches:
        return W._err(f'No items found matching "{keyword}"')
    items = [m["item"] for m in matches[:5]]
    # 過濾受保護商品
    deletable = [it for it in items if it["sku_id"] not in _PROTECTED_SKUS]
    protected = [it for it in items if it["sku_id"] in _PROTECTED_SKUS]
    if not deletable:
        return {"ok": True, "summary": f'"{keyword}" is a built-in demo item and cannot be deleted.',
                "view": "item_delete_denied",
                "data": {"protected": [it["name"] for it in protected]}}
    rows = [{"sku": it["sku_id"], "name": it["name"], "protected": False} for it in deletable]
    if protected:
        rows += [{"sku": it["sku_id"], "name": it["name"] + " 🔒", "protected": True} for it in protected]
    summary = f"Found {len(items)} matching items ({len(deletable)} deletable):\n"
    summary += "\n".join(f"  {'🔒 ' if it['sku_id'] in _PROTECTED_SKUS else '🗑 '}{it['sku_id']} {it['name']}" for it in items[:10])
    return {"ok": True, "summary": summary, "view": "item_delete_confirm" if deletable else "item_delete_denied",
            "data": {"keyword": keyword, "items": rows, "deletable_count": len(deletable),
                     "protected_count": len(protected), "pending": True}}


def commit_delete_item(pending: dict, actor: str = "user_confirmed",
                       trace_id: str | None = None) -> dict:
    """HITL 確認後刪除商品（軟刪除：從 items.csv 移除 + 重生 seed）"""
    import csv, shutil
    dd = _data_dir()
    ts = __import__('datetime').datetime.now().isoformat(timespec="seconds")
    trace_id = trace_id or f"del-{ts}"
    keyword = pending.get("keyword", "")

    matches = W.match_items(keyword)
    deletable = [m["item"] for m in matches if m["item"]["sku_id"] not in _PROTECTED_SKUS]
    if not deletable:
        return W._err("No deletable items")

    skus_to_delete = {it["sku_id"] for it in deletable}
    deleted_names = ", ".join(it["name"] for it in deletable)

    # 1. 從 items.csv 移除（全檔重寫 → 進鎖，避免跟新增商品的追加互相蓋寫）
    with _STOCK_LOCK:
        items_path = dd / "master" / "items.csv"
        shutil.copy2(items_path, str(items_path) + ".bak")
        rows = list(csv.DictReader(open(items_path, encoding="utf-8-sig")))
        kept = [r for r in rows if r["sku_id"] not in skus_to_delete]
        with open(items_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader(); w.writerows(kept)

    # 2. 從 config.json 移除 safety_stock
    cfg_path = dd / "master" / "config.json"
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    for sku in skus_to_delete:
        cfg.get("safety_stock_base", {}).pop(sku, None)
        for wh in ("north", "central", "south"):
            cfg.get("safety_stock_override", {}).get(wh, {}).pop(sku, None)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3. audit log
    audit_dir = dd / "audit"; audit_dir.mkdir(parents=True, exist_ok=True)
    with open(audit_dir / f"{ts[:10]}_changes.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "trace_id": trace_id, "actor": actor,
                            "action": "delete_items", "skus": list(skus_to_delete)}, ensure_ascii=False) + "\n")

    # 4. 熱更新記憶體（從 warehouse_data/ 重載）
    import warehouse as W_mod
    W_mod.init(Path(__file__).parent / "warehouse_data")

    return {"ok": True, "summary": f"✅ Deleted: {deleted_names} ({len(deletable)} items)",
            "view": "item_done", "data": {"deleted": deleted_names, "trace_id": trace_id}}
