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
import io
import json
import re
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
                   f"preview: {'all' if not skus else len(skus)} items x {len(whs)} "
                   f"warehouses -> {len(preview)} changes")
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
    # 舊值回傳給前端/context，讓 'put it back'（復原）有值可用
    #   （劇情批 r1 S6：訪客改完設定後說「改回去」）
    _undo = None
    if pending.get("preview"):
        _p0 = pending["preview"][0]
        _undo = {"canon": canon, "item": _p0.get("name"),
                 "warehouse": _p0.get("warehouse"), "value": _p0.get("old")}
    elif pending.get("old") is not None:
        _undo = {"canon": canon, "item": None,
                 "warehouse": None, "value": pending.get("old")}
    return {"ok": True, "summary": f"✅ {changed} entries saved, backed up to "
                                   "config.json.bak and recorded in the audit log.",
            "view": "config_done", "data": {"changed": changed, "trace_id": trace_id,
                                            "canon": canon, "undo": _undo}}


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
    # EN build：LLM 常把 script_name 抽成帶雜訊的字串
    #   （'run the month-end stocktake' → 'this_month%20stock_take'）→
    #   URL 編碼、底線、時間詞全混在一起，跟白名單完全對不上。
    #   先正規化：解 URL 編碼、底線/連字號轉空白、剝時間詞與英文動詞。
    q = script_name
    try:
        from urllib.parse import unquote
        q = unquote(q)
    except Exception:
        pass
    q = re.sub(r"[_\-]+", " ", q)
    q = re.sub(r"\b(?:this|last|next)\s+(?:month|week|day|year)\b", " ", q, flags=re.I)
    q = re.sub(r"\b(?:run|execute|start|do|perform|please|the|a|an)\b", " ", q, flags=re.I)
    q = q.replace(" ", "")
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
    # ── r11：**詞級**比對——substring 對付不了「LLM 換字」──────────────
    #   `run the month end stocktake` → LLM 抽成 `month_end_result`
    #   → 'monthendresult' vs alias 'monthendstocktake' 互不包含 → 落空
    #   → 訪客看到「不在白名單」還被列出內部腳本清單。
    #   ⚠️ **字元相似度行不通**（實測後放棄，別再試）：
    #     monthendresult→正解 0.581／次名 0.500（差 0.081，該中）
    #     northwarehouse→誤配 0.621／次名 0.375（差 0.246，不該中）
    #     分數與差距都跟正確性**相反**，取任何門檻都會兩者擇一犧牲。
    #   ⇒ 改用**詞級**：要求「LLM 抽的詞」與候選有 ≥2 個**實詞**重疊。
    #     month+end 命中 stocktake 的 alias（2 詞）；north+warehouse 與
    #     任何腳本都只重疊 0 個實詞 → 安全。
    _q_words = {w for w in re.split(r"[^a-z0-9]+", script_name.lower())
                if len(w) >= 3 and w not in {
                    "the", "run", "this", "that", "and", "for", "please",
                    "month", "week", "day", "year", "last", "next"}}
    # 'month' 在停用詞裡（時間詞），但 month-end 是腳本名的一部分 →
    #   單獨把 monthend 當一個詞補回，避免時間詞被剝光後無詞可比
    if re.search(r"month[\s_-]*end", script_name, re.I):
        _q_words.add("monthend")
    if len(_q_words) >= 2:
        for sc in _load_manifest().get("scripts", []):
            _cand_words = set()
            for cand in ([sc["id"], sc.get("label", "")]
                         + list(sc.get("aliases", []))):
                for w in re.split(r"[^a-z0-9]+", (cand or "").lower()):
                    if len(w) >= 3:
                        _cand_words.add(w)
                if re.search(r"month[\s_-]*end", cand or "", re.I):
                    _cand_words.add("monthend")
            if len(_q_words & _cand_words) >= 2:
                return sc
    return None


def _parse_days(text: str) -> int | None:
    """從訪客的話抽出期間 → 天數。抽不到回 None（用腳本預設）。

    2026-08-03：匯出進出紀錄的期間反問（clarify 選單）選完會送
    「匯出最近 7 天的進出紀錄」這類句子，這支負責把它變成 --days。
    """
    import re as _re2
    t = (text or "").lower()
    if _re2.search(r"昨天|昨日|yesterday", t):
        return 1
    if _re2.search(r"前天|day\s+before\s+yesterday", t):
        return 2
    if _re2.search(r"本週|這週|this\s+week", t):
        return 7
    # ⚠️ 語意＝**往前推一週**（2026-08-03 user 定調），不是「上一個完整週」。
    #   修前回 14（＝往前 8-14 天那一週），訪客講「前一週」卻拿到兩週前的資料。
    if _re2.search(r"上週|上周|前一週|前一周|前1週|last\s+week|past\s+week|"
                   r"previous\s+week", t):
        return 7
    if _re2.search(r"本月|這個月|this\s+month", t):
        return 30
    # 同上：**往前推一個月**（修前回 60＝上一個完整月）。
    if _re2.search(r"上個月|上一個月|前一個月|前1個月|前一月|last\s+month|"
                   r"past\s+month|previous\s+month", t):
        return 30
    # 🆕 前一季（2026-08-03 user 需求）——快照有 91 個有資料的日子，
    #   90 天剛好涵蓋整個資料範圍，是這份 demo 能匯出的最大期間。
    if _re2.search(r"前一季|上一季|上季|前1季|近一季|這一季|本季|"
                   r"last\s+quarter|past\s+quarter|previous\s+quarter|"
                   r"this\s+quarter|last\s+3\s+months|past\s+3\s+months|"
                   r"前三個月|近三個月|過去三個月", t):
        return 90
    m = _re2.search(r"(?:最近|過去|前|past|last|recent)\s*(\d+)\s*(?:天|days?)|"
                    r"(\d+)\s*(?:天內|days?)", t)
    if m:
        n = int(m.group(1) or m.group(2))
        return max(1, min(365, n))
    # 中文數字：「前七天」「最近三天」——user 定調的引導語就是「前七天」，
    # 訪客照著講不能因為沒打阿拉伯數字就漏掉（2026-08-03 端到端實測抓到）。
    m = _re2.search(r"(?:最近|過去|前)\s*([零一二三四五六七八九十兩]+)\s*天", t)
    if m:
        _n = _cjk_num(m.group(1))
        if _n:
            return max(1, min(365, _n))
    return None


def _cjk_num(s: str) -> int:
    """中文數字 → int（只需涵蓋 1..99，期間不會更大）。"""
    _d = {"零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
          "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if not s:
        return 0
    if "十" not in s:
        n = 0
        for ch in s:
            if ch not in _d:
                return 0
            n = n * 10 + _d[ch]
        return n
    a, _, b = s.partition("十")
    return (_d.get(a, 1) if a else 1) * 10 + (_d.get(b, 0) if b else 0)


def run_script(script_name: str = "", **_kw) -> dict:
    _period_text = str(_kw.pop("_period_text", "") or "")
    if not script_name and _kw:
        script_name = str(list(_kw.values())[0])
    steps: list[dict] = []
    sc = _match_script(script_name)
    _trace(steps, "read", f'matched against whitelist manifest.json → "{script_name}"')
    if not sc:
        _scripts = _load_manifest().get("scripts", [])
        avail = ", ".join(s["label"] for s in _scripts)
        # 2026-08-06 排程百句：訪客打錯字（'run a stock cout evry day at 8am'）
        #   時 script_name 是 **LLM 幻覺出的內部代號**（實測 'run_stock_check'），
        #   直接回顯 = 內部識別字外洩，訪客只會困惑「我沒打過這個字」。
        #   ⇒ 不回顯內部字串，只問要跑哪一個（ZH 同款作法）。
        return {"ok": True, "view": "clarify",
                "summary": f'Which one would you like to run? Available: {avail}',
                "data": {"question": 'Which script would you like to run?',
                         # options 送回後端當查詢字串 → 直接用 manifest 的
                         #   label（已英文化），不能寫死中文
                         "options": [f"run {s['label']}" for s in _scripts],
                         "hint": ""}}

    # 安全護欄：只回「待確認」，不直接 subprocess（執行交給 server confirm 後）
    _trace(steps, "confirm", f"whitelisted script matched: {sc['label']} "
                             f"(timeout {sc['timeout_s']}s)")
    # 訪客講的期間（匯出用）→ 帶到 confirm 那步。
    # `_period_text` 是 server 塞的原句（script_name 只有「匯出」兩字時期間
    # 抽不到）；沒有就退回用 script_name 自己解析。
    _days = _parse_days(_period_text or script_name)
    # ⚠️ 卡片文案要**跟著訪客講的期間走**：manifest 的 description 是寫死的
    #   「last 7 days」，訪客講 yesterday 時卡片卻說 7 days（2026-08-03 實測抓到）
    #   ——後端其實有正確帶 days=1，只有文案騙人，訪客會以為選單沒作用。
    _desc = sc.get("description", sc.get("desc", ""))
    if sc["id"] == "export_movements" and _days:
        _p = ("yesterday's inbound/outbound records" if _days == 1
              else f"the last {_days} days of inbound/outbound records")
        _desc = f"Merge {_p} into a CSV"
    summary = f"About to run whitelisted script [{sc['label']}]: {_desc}. Please confirm."
    return {"ok": True, "summary": summary, "view": "script_confirm",
            "data": {"pending": True, "script_id": sc["id"], "label": sc["label"],
                     "desc": _desc, "timeout_s": sc["timeout_s"],
                     "days": _days, "trace": steps}}


# 白名單腳本實際指令（server confirm 後呼叫 commit_run_script）
_SCRIPT_CMD = {
    # id → (scripts/ 下的檔名, 額外 args)。路徑一律從 _data_dir() 推導——
    # 本機是 warehouse_v2/test/warehouse_data、RPI5 是 ~/warehouse_v2/warehouse_data
    # （扁平佈局），寫死 test/ 前綴會在 RPI5 找不到檔（r55 收官批抓到）。
    "stock_audit":      ("stock_audit.py",      []),
    # ⚠️ 預設 7 天（原本 30）——動態模擬把今天灌到十幾萬筆，
    #   30 天的匯出訪客打開只會看到滿滿今天的資料，看不出意義。
    "export_movements": ("export_movements.py", ["--days", "7"]),
    "generate_report":  ("generate_report.py",  ["--type", "full"]),
}


def commit_run_script(script_id: str, actor: str = "user_confirmed",
                      trace_id: str | None = None, days: int | None = None) -> dict:
    """執行白名單腳本。`days` 讓訪客指定期間（匯出進出紀錄用，2026-08-03）。"""
    sc = next((s for s in _load_manifest().get("scripts", []) if s["id"] == script_id), None)
    if not sc:
        return W._err("Script not found")
    spec = _SCRIPT_CMD.get(script_id)
    if not spec:
        return W._err(f"Script {script_id} has no command bound")
    fname, extra = spec
    script_path = _data_dir() / "scripts" / fname
    extra = list(extra)
    # 訪客指定了期間 → 覆寫預設的 --days（沒指定就用 _SCRIPT_CMD 裡的預設）
    if days is not None and "--days" in extra:
        i = extra.index("--days")
        extra[i + 1] = str(max(1, min(365, int(days))))
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
    """產生倉庫報告 —— **一律導向 `stock_audit`，全系統只有這一份報告**。

    ⚠️ 2026-08-03（user 定調）：原本這裡自己產一份 Markdown，跟盤點的
    HTML+CSV **是兩份不同的報告** ⇒ 訪客講「產生報表」和「跑盤點」
    會拿到不一樣的東西，而盤點那份才是最完整的（KPI/需注意/熱銷/
    到期/完整庫存，含撐天與市值）。
    ⇒ 這支改成薄包裝，直接跑同一支腳本。報告類型只留一份、不再分歧。
    （`report_type` 保留參數相容，但不再影響產出——完整報告本來就涵蓋
      low_stock / expiring 那些區塊。）
    """
    return commit_run_script("stock_audit", actor=actor, trace_id=trace_id)


def _generate_report_legacy(report_type: str = "full", actor: str = "agent_auto",
                            trace_id: str | None = None) -> dict:
    """舊版 Markdown 報告產生器（已停用，保留供查證/回退）。"""
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

# 2026-08-06 user 定調（ZH 同款）：排程/警示都可以一直新增，**但要有上限**——
#   展場一天下來訪客可能各堆出幾十筆，排程每筆到點都真的跑腳本（推論資源被
#   排隊佔用）、警示每筆都進背景掃描，清單也會長到看不完。
_MAX_SCHEDULE_JOBS  = 10
_MAX_ALERT_RULES    = 10


def _next_seq_id(existing: list, prefix: str) -> str:
    """產下一個不撞號的流水 ID（SCH001 / AL001）。

    ⚠️ 原本是 `f"{prefix}{len(rules)+1:03d}"`——**刪除後必撞號**：
      3 筆刪掉中間那筆 → len=2 → 下一個又生 003，跟現存的 AL003 同 ID
      ⇒ 刪除時 ID 比對會砍錯/砍雙筆。改成「取現存最大號 +1」。
    """
    mx = 0
    for r in existing or []:
        _v = str(r.get("id", ""))
        if _v.startswith(prefix) and _v[len(prefix):].isdigit():
            mx = max(mx, int(_v[len(prefix):]))
    return f"{prefix}{mx + 1:03d}"


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

    # 上限（同排程）。⚠️ 存檔規則只有 id/condition/scope/scope_names（實機驗過），
    #   沒有 scope_txt/cond_label ⇒ 標籤在這裡自己組。
    if len(rules) >= _MAX_ALERT_RULES:
        _cl = {"below_safety": "below safety stock", "out_of_stock": "out of stock",
               "expiring": "expiring soon", "below_threshold": "below a set quantity"}
        _cur_al = ", ".join(
            f"{', '.join(r.get('scope_names') or []) or 'all items'}"
            f" -> {_cl.get(r.get('condition', ''), r.get('condition', ''))}"
            for r in rules[:5])
        return {"ok": True, "view": "clarify",
                "summary": (f"You've reached the limit of {_MAX_ALERT_RULES} alert "
                            f"rules, so I can't add another one.\n"
                            f"Current: {_cur_al}\n"
                            f'To free up a slot, say "my alerts" and tell me which '
                            f"one to delete."),
                "data": {"question": f"Alert limit of {_MAX_ALERT_RULES} reached. "
                                     f"Delete an old one first?",
                         "options": ["Show my alerts"],
                         "actions": ["my alert list"],
                         "hint": ""}}
    rule_id = _next_seq_id(rules, "AL")

    _cond_labels = {"below_safety": "below safety stock", "out_of_stock": "out of stock",
                    "expiring": "expiring soon",
                    "below_threshold": f"below {threshold} units" if threshold else "below a set quantity"}
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
                        f"{len(lines)} line{'s' if len(lines) > 1 else ''}, estimated NT$ {total:,}. "
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
    # ── HTML 版（訪客點連結直接看）──
    #   JSON 是給機器讀的，訪客看不懂 ⇒ 產一份可讀的採購單。
    #   放 audit/ 是因為 `/audit/*.html` 端點已經支援瀏覽器直接開。
    _wh = {"north": "North", "central": "Central", "south": "South"}
    _po_rows = "".join(
        f'<tr><td>{ln.get("sku_id","")}</td><td>{ln.get("name","")}</td>'
        f'<td>{_wh.get(ln.get("warehouse",""), ln.get("warehouse",""))}</td>'
        f'<td class="n b">{ln.get("order_qty",0)}</td>'
        f'<td class="r">{ln.get("reason","")}</td></tr>'
        for ln in doc["lines"])
    _po_html = dd / "audit" / f"{po_id}.html"
    try:
        _po_html.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<title>Purchase Order {po_id}</title><style>
body{{font-family:system-ui,-apple-system,"Noto Sans TC",sans-serif;margin:0;padding:16px;
background:#131820;color:#e6edf3}}
h1{{font-size:19px;margin:0 0 4px}} .sub{{color:#8b98a5;font-size:13px;margin-bottom:14px}}
.kpis{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}}
.kpi{{background:rgba(255,255,255,.05);border-radius:8px;padding:9px 14px;min-width:110px}}
.kpi .k{{font-size:11px;color:#8b98a5;text-transform:uppercase;letter-spacing:.4px}}
.kpi .v{{font-size:20px;font-weight:700;margin-top:2px}}
.badge{{display:inline-block;background:rgba(246,173,85,.18);color:#f6ad55;
border:1px solid #f6ad55;border-radius:5px;padding:2px 9px;font-size:12px;font-weight:700}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th{{background:#1a2130;text-align:left;padding:7px 9px;border-bottom:2px solid #2d3748;
font-size:11.5px;letter-spacing:.4px;text-transform:uppercase}}
th.n{{text-align:right}}
td{{padding:6px 9px;border-bottom:1px solid #1e2530;white-space:nowrap}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}} td.b{{font-weight:700;color:#90cdf4}}
td.r{{color:#8b98a5;font-size:12px;white-space:normal}}
tr:nth-child(even){{background:rgba(255,255,255,.025)}}
</style></head><body>
<h1>Purchase Order Draft</h1>
<div class="sub">{po_id} &middot; {doc['date']} &middot; <span class="badge">DRAFT</span>
 &middot; created by {actor}</div>
<div class="kpis">
  <div class="kpi"><div class="k">Lines</div><div class="v">{len(doc['lines'])}</div></div>
  <div class="kpi"><div class="k">Total units</div>
    <div class="v">{sum(int(l.get('order_qty', 0)) for l in doc['lines']):,}</div></div>
  <div class="kpi"><div class="k">Est. value</div>
    <div class="v">NT$ {doc.get('total', 0):,}</div></div>
</div>
<table><thead><tr><th>SKU</th><th>Item</th><th>Warehouse</th>
<th class="n">Order qty</th><th>Reason</th></tr></thead>
<tbody>{_po_rows}</tbody></table>
<div class="sub" style="margin-top:12px">This is a draft — not sent to suppliers.
 Saved to orders/PO_draft/{po_id}.json</div>
</body></html>""", encoding="utf-8")
    except Exception:
        _po_html = None
    snap = W.state().snapshot_date or ts[:10]
    with open(dd / "audit" / f"{snap}_changes.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "trace_id": trace_id, "actor": actor,
                            "action": "generate_po", "po_id": po_id,
                            "lines": len(doc["lines"]), "total": doc["total"]},
                           ensure_ascii=False) + "\n")
    return {"ok": True, "summary": f"Purchase order draft {po_id} created ({len(doc['lines'])} lines, "
                                   f"NT$ {doc['total']:,}), saved to PO_draft/.",
            "view": "po_done", "data": {"po_id": po_id, "trace_id": trace_id,
                                        "lines": len(doc["lines"]),
                                        "view_file": f"{po_id}.html" if _po_html else ""}}


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
    # ⚠️ 「日報」漏收 → 「排程每天早上九點出日報」對到空字串，訪客看到
    #   `[error] Script "" not found`（2026-08-03 中文版端到端實測）。
    #   英文側走 _SCHEDULE_SCRIPT_RE_EN 的 `report` 已涵蓋 daily report，
    #   這裡補齊只是讓兩邊表一致（parity）。
    "日報":     "generate_report",
    "年報":     "generate_report",
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

# EN build：英文腳本詞（原表全中文 → 英文排程句抽不到 script_id，
#   set_schedule 會回「Script "" not found」）。
#   ⚠️ 順序＝優先權：長片語在前，避免 'movement report' 被單獨的 'report' 先吃掉。
#   ⚠️ 坑 1：一律詞界，'audit' 不可 substring 撞到別的字。
_SCHEDULE_SCRIPT_RE_EN = [
    (r"\b(?:stock ?take|stocktake|month[- ]end (?:stock ?take|audit|count)|"
     r"stock audit|inventory audit|inventory count|cycle count|"
     # 2026-08-02：UI 排程頁教訪客打 "run a stock count every day at 9am"，
     #   但表裡只有 inventory/cycle count，**沒有 stock count** →
     #   script_id=None → Pre-C-Sched 不攔截 → 排程句被當立即執行。
     r"stock count|daily count|counting stock)\b", "stock_audit"),
    (r"\b(?:low[- ]stock (?:alert|check|report|list)|stock alert|"
     r"shortage (?:alert|check)|reorder check)\b", "stock_audit"),
    (r"\b(?:movement report|movements? export|export movements?|"
     r"in ?/ ?out report|inbound[- /]outbound report|shipment report|"
     r"transaction export)\b", "export_movements"),
    (r"\b(?:health check|full report|weekly report|monthly report|"
     r"summary report|ops report|report|reports)\b", "generate_report"),
    (r"\b(?:export)\b", "export_movements"),
    (r"\b(?:audit)\b", "stock_audit"),
]
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

# EN build：英文頻率詞（原表全中文 → 英文排程句 freq 永遠停在預設 daily，
#   'every monday' / 'weekly' 都會被當每天）。用 regex 是因為要詞界比對，
#   且 'every monday' 這種多詞片語 substring 比對不可靠。
_SCHEDULE_FREQ_RE_EN = [
    (r"\b(?:every\s+(?:month|month\s+end)|monthly|month[- ]end|"
     r"each\s+month)\b", "monthly"),
    (r"\b(?:every\s+(?:week|monday|tuesday|wednesday|thursday|friday|"
     r"saturday|sunday)|weekly|each\s+week)\b", "weekly"),
    # ⚠️ 'daily goods' 是**類別名**（Daily Goods）不是頻率詞——守衛 low 類
    #   'low stock daily goods' 曾被判成排程句 → set_schedule 抽不到腳本 → error
    (r"\b(?:every\s+(?:day|morning|night|evening)|"
     r"daily(?!\s+(?:goods|necessities))|nightly|each\s+day)\b", "daily"),
]
# EN build：英文時段詞（對應 _SCHEDULE_TIME_MAP）
_SCHEDULE_TIME_RE_EN = [
    (r"\b(?:early morning|dawn)\b", "02:00"),
    (r"\b(?:morning|am)\b", "09:00"),
    (r"\b(?:noon|midday|lunch ?time)\b", "12:00"),
    (r"\b(?:afternoon)\b", "14:00"),
    (r"\b(?:evening)\b", "17:00"),
    (r"\b(?:night|tonight|pm)\b", "20:00"),
]

_CN_HOUR = {"零": 0, "一": 1, "兩": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
            "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def _parse_schedule_intent(text: str) -> dict:
    """從自然語言解析排程意圖。
    回傳 {script_id, freq, time_str, freq_explicit, time_explicit}——
    explicit 旗標讓 set_schedule 知道原句有沒有明講，明講的才覆蓋 LLM 給的參數。"""
    import re as _re
    script_id = next((v for k, v in _SCHEDULE_SCRIPT_MAP.items() if k in text), None)
    # EN build：中文腳本表沒中 → 試英文（長片語優先，見 _SCHEDULE_SCRIPT_RE_EN）
    if script_id is None:
        for _p_s, _v_s in _SCHEDULE_SCRIPT_RE_EN:
            if _re.search(_p_s, text, _re.I):
                script_id = _v_s
                break
    _freq_hit = next((v for k, v in _SCHEDULE_FREQ_MAP.items() if k in text), None)
    # EN build：中文頻率表沒中 → 試英文（順序已排成 monthly→weekly→daily，
    #   避免 'every month' 先被 'every' 開頭的 daily 規則吃掉）
    if _freq_hit is None:
        for _p_en, _v_en in _SCHEDULE_FREQ_RE_EN:
            if _re.search(_p_en, text, _re.I):
                _freq_hit = _v_en
                break
    freq = _freq_hit or "daily"
    # 解析時間（幾點）——阿拉伯數字或中文數字（「八點」「十一點」，第9輪測試補：
    # 原本只認阿拉伯，「每天早上八點」落到「早上」預設 09:00 跟既有排程撞名）
    time_str = None
    # ⚠️ EN build（單元測試抓到）：'at 7:30pm' 的 `:` 會中 [點:] 字元類被中文
    #   分支搶走 → pm 資訊丟失變 07:30。句含英文 am/pm 鐘點就跳過中文分支，
    #   交給下面的英文分支正確處理。
    _has_en_ampm = _re.search(r'\b[0-9]{1,2}(?::[0-9]{2})?\s*[ap]\.?m\.?\b',
                              text, _re.I)
    m = None if _has_en_ampm else \
        _re.search(r'([0-9]{1,2}|十[一二]?|[一兩二三四五六七八九])\s*[點:](\d{0,2})', text)
    if m:
        g = m.group(1)
        h = int(g) if g.isdigit() else _CN_HOUR.get(g, 9)
        mi = int(m.group(2)) if m.group(2) else 0
        # 下午/晚上 + 12 小時制轉換
        # ⚠️ EN build：'at 1:30' 的 `:` 會中 [點:] 字元類走到**這條中文分支**
        #   （單元測試抓到）→ 時段語境與 1-6 智慧預設要中英文一起看。
        _pm_zh = _re.search(r'\b(?:afternoon|evening|tonight|night)\b', text, _re.I)
        _am_zh = _re.search(r'\b(?:morning|dawn|sunrise|midnight)\b', text, _re.I)
        if h < 12 and (any(w in text for w in ("下午", "晚上", "傍晚", "晚間", "夜裡"))
                       or _pm_zh):
            h += 12
        elif (1 <= h <= 6 and not _am_zh
              and not any(w in text for w in ("凌晨", "半夜", "早上", "上午",
                                              "清晨", "早晨"))):
            h += 12
        time_str = f"{h:02d}:{mi:02d}"
    else:
        # EN build：英文鐘點（9am / 9:30pm / at 15:00）——原本只認中文「點」，
        #   'at 9am' 解析不到就掉到預設 09:00（碰巧對，但 'at 6pm' 就錯了）
        _m_en = _re.search(r'\b(?:at\s+)?([0-9]{1,2})(?::([0-9]{2}))?\s*'
                           r'([ap])\.?m\.?\b', text, _re.I)
        # ⚠️ 12 小時制要驗範圍：'at 25pm' 的 25 % 12 = 1 → 靜默變成 13:00
        #   （邊界測試抓到：無效時間被編造成合法值）。超出 1-12 視為沒解析到。
        if _m_en and 1 <= int(_m_en.group(1)) <= 12 \
                and (not _m_en.group(2) or int(_m_en.group(2)) < 60):
            h = int(_m_en.group(1)) % 12
            mi = int(_m_en.group(2)) if _m_en.group(2) else 0
            if _m_en.group(3).lower() == "p":
                h += 12
            time_str = f"{h:02d}:{mi:02d}"
        else:
            # 2026-08-06 ZH 同款智慧預設：'daily at 1' 無 am/pm → 原本掉到
            #   詞表→None→**默默變 09:00**（比建錯更難察覺）。裸鐘點 1-6 當
            #   下午（+12）；morning/am 語境才留上午；evening/night 語境 7-11
            #   也 +12。'at 15:00' 24h 直取（h≥12 不再加）。
            _m_24 = _re.search(r'\bat\s+([0-9]{1,2})(?::([0-9]{2}))?\b', text, _re.I)
            if _m_24 and int(_m_24.group(1)) <= 23 \
                    and (not _m_24.group(2) or int(_m_24.group(2)) < 60):
                h = int(_m_24.group(1))
                mi = int(_m_24.group(2)) if _m_24.group(2) else 0
                _pm_ctx = _re.search(r'\b(?:afternoon|evening|tonight|night)\b',
                                     text, _re.I)
                _am_ctx = _re.search(r'\b(?:morning|dawn|sunrise|midnight)\b',
                                     text, _re.I)
                if h < 12 and _pm_ctx:
                    h += 12
                elif 1 <= h <= 6 and not _am_ctx:
                    h += 12
                time_str = f"{h:02d}:{mi:02d}"
            else:
                _t_hit = next((v for k, v in _SCHEDULE_TIME_MAP.items() if k in text), None)
                if _t_hit is None:
                    for _p_t, _v_t in _SCHEDULE_TIME_RE_EN:
                        if _re.search(_p_t, text, _re.I):
                            _t_hit = _v_t
                            break
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

    # 上限（擋在確認卡出現前，訪客不會白按一次授權才被拒）
    if len(jobs) >= _MAX_SCHEDULE_JOBS:
        _cur = ", ".join(f"{j.get('freq_label', '')} {j.get('time_str', '')} "
                         f"[{j.get('script_label', '')}]" for j in jobs[:5])
        return {"ok": True, "view": "clarify",
                "summary": (f"You've reached the limit of {_MAX_SCHEDULE_JOBS} "
                            f"schedules, so I can't add another one.\n"
                            f"Current: {_cur}\n"
                            f'To free up a slot, say "my schedules" and tell me '
                            f"which one to delete."),
                "data": {"question": f"Schedule limit of {_MAX_SCHEDULE_JOBS} reached. "
                                     f"Delete an old one first?",
                         "options": ["Show my schedules"],
                         "actions": ["what schedules do i have"],
                         "hint": ""}}

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
    job_id = _next_seq_id(jobs, "SCH")

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
                    "expiring": "expiring soon", "below_threshold": "below a set quantity"}
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
                    "expiring": "expiring soon", "below_threshold": "below a set quantity"}
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
# ══════════════════════════════════════════════════════════════════════
# r22 上架主線（2026-08-07）：英文建檔的**商品名切出**與**類別自動判斷**
#
# 為什麼要重寫：原本用「剝掉已知詞、剩下的當商品名」，英文實測 **1/10**
#   （中文可行是因為要剝的詞有限；英文贅詞太多 a/the/its/called/new/item…）。
#   實測三種方法：剝除法 1/10、錨定法 4/10、**切段法 9/10** ⇒ 採切段法。
#
# 設計原則：**不需要認識商品**。切名靠句法結構、判類靠品類詞 + head noun，
#   兩者都判不出來就**問使用者**（user 定調保守派：寧可多問，不可猜錯）。
#   實測 60 個真商品：判對 83% / 判錯 3% / 反問 13%。
# ══════════════════════════════════════════════════════════════════════

# 指令詞與虛詞——出現在句子裡但絕不是商品名的一部分
_EN_CMD_WORDS = {
    # ⚠️ 不可收 "set"：它是商品名的常見成分（Cookware Set / wrench set /
    #   tool set），收進來會把 'hex wrench set' 剝成 'hex wrench'（實測）。
    #   句首的 "set up a new item" 由下方的開場白 regex 整段處理，不靠這裡。
    "add", "new", "create", "created", "setup", "register",
    "item", "items", "product", "products", "sku", "please", "pls",
    "i", "we", "want", "wants", "wanna", "would", "like", "need", "to",
    "a", "an", "the", "its", "it's", "is", "are", "this", "that",
    "called", "named", "name", "just", "got", "get", "there", "here",
    "each", "keep", "min", "minimum", "safety", "stock", "price", "priced",
    "sells", "sell", "selling", "sold", "for", "of", "at", "in", "on",
    "with", "and", "or", "put", "into", "onto", "list", "listing",
    "cost", "costs", "worth", "dollars", "dollar", "nt", "twd", "usd",
    "north", "south", "central", "warehouse", "warehouses", "wh",
    "units", "unit", "pcs", "pieces", "piece", "qty", "quantity",
}
# 類別詞——出現代表那一段是屬性段，不是名稱段
_EN_CAT_WORDS = {
    "electronics", "electronic", "appliance", "appliances", "kitchen",
    "food", "beverage", "beverages", "drink", "drinks", "daily", "goods",
    "household", "apparel", "clothing", "clothes", "sports", "sport",
    "outdoor", "outdoors", "fitness",
}


def _norm_item_name(s: str) -> str:
    """商品名正規化，供**重複檢查**用（不改實際存檔的名稱）。

    ⚠️ r22 實測抓到：重複名檢查原本是精確比對，`add item wireless mouse`
      建得出新商品，但主檔已有 `Wireless Mouse`（e07）——大小寫不同就
      漏判 ⇒ 建出重複商品，庫存被拆成兩筆、查詢結果從此不準。
    """
    return " ".join((s or "").lower().replace("-", " ").split())


def _is_mostly_en_text(s: str) -> bool:
    """句子是不是以英文為主（有英文字母且中文字極少）。

    ⚠️ tools_v2 是中英共用檔，切名策略要分流：英文走切段法、中文走剝除法。
    """
    cjk = sum(1 for c in (s or "") if "一" <= c <= "鿿")
    ascii_alpha = sum(1 for c in (s or "") if c.isascii() and c.isalpha())
    return ascii_alpha >= 2 and cjk <= 1


# 泛稱詞——出現在句子裡但**不足以當商品名**（'some stuff' / 'a thing'）。
#   ⚠️ 實測 'just got some new stuff, bamboo toothbrush, 45 each' 曾挑到
#     'some stuff' 那段（它跟 'bamboo toothbrush' 都通過雜訊檢查，
#     但排在前面）⇒ 泛稱詞降權，且整段都是泛稱時視為可疑。
_EN_VAGUE_WORDS = {
    "stuff", "thing", "things", "something", "some", "few", "several",
    "batch", "lot", "bunch", "goods", "material", "materials", "misc",
    "other", "others", "etc", "more", "another",
}


def _en_name_is_suspicious(name: str) -> bool:
    """挖出來的名稱看起來不像真商品名 → 回 True（呼叫端應改成**反問**）。

    ⚠️ 這是最後一道保險：世界上的商品名無限，切名規則不可能永遠對。
      挖錯不可怕，**靜默建出爛資料才可怕** ⇒ 可疑就問，不出卡。
    """
    toks = [t for t in re.findall(r"[A-Za-z0-9'\-]+", name or "")]
    if not toks:
        return True
    real = [t for t in toks
            if t.lower() not in _EN_VAGUE_WORDS
            and t.lower() not in _EN_CMD_WORDS
            and not t.replace('.', '').isdigit()]
    if not real:
        return True                      # 整串都是泛稱/虛詞（'some stuff'）
    # 只剩單一過短的 token（'ab'）也可疑
    if len(real) == 1 and len(real[0]) <= 2:
        return True
    return False


def _en_cut_item_name(raw: str) -> str:
    """從英文建檔句切出商品名（切段法，實測 9/10）。

    做法：用標點切段，挑出「不含指令詞/類別詞/數字」且實詞最多的一段。
    ⚠️ 反過來想才對——不是「剝掉雜訊留下名稱」（那會剝不乾淨），
      而是「切成幾段、挑最像名稱的那段」。這樣陌生商品一樣切得出來，
      因為判準是**這段不是雜訊**，不需要認識這個商品。
    """
    if not raw:
        return ""
    # 先砍掉句首的指令片語（"add a new item called ..." 這種開場白）
    body = re.sub(r'(?i)^\s*(?:please\s+)?(?:can\s+you\s+)?'
                   r'(?:i\s+(?:want|need|would\s+like)\s+to\s+)?'
                   r'(?:just\s+)?(?:got|add|create|new|set\s*up|register|make)\s*'
                   r'(?:an?\s+|the\s+)?(?:new\s+)?'
                   r'(?:item|product|sku|listing)?\s*'
                   r'(?:called|named|is)?\s*[:\-—]?\s*', ' ', raw)
    segs = re.split(r'[,;:—]|--|\.\s', body)
    best, best_score = "", 0
    for seg in segs:
        toks = re.findall(r"[A-Za-z0-9'\-]+", seg)
        if not toks:
            continue
        # ⚠️ 數字要分兩種（實測 'SKF 6204 ball bearing' 曾被剝成
        #   'SKF ball bearing'——**型號是商品名的一部分**，不能剝）：
        #     · 夾在實詞中間 → 型號/規格，保留（6204 在 SKF 與 ball 之間）
        #     · 落在段落頭尾 → 屬性值，剝掉（'890 each' 的 890）
        _keep = []
        for _i, t in enumerate(toks):
            tl = t.lower()
            if tl in _EN_CMD_WORDS or tl in _EN_CAT_WORDS:
                continue
            if t.replace('.', '').isdigit():
                _prev = [x for x in toks[:_i]
                         if not x.replace('.', '').isdigit()
                         and x.lower() not in _EN_CMD_WORDS
                         and x.lower() not in _EN_CAT_WORDS]
                _next = [x for x in toks[_i + 1:]
                         if not x.replace('.', '').isdigit()
                         and x.lower() not in _EN_CMD_WORDS
                         and x.lower() not in _EN_CAT_WORDS]
                if not (_prev and _next):
                    continue          # 頭尾的孤立數字 = 屬性值，剝掉
            _keep.append(t)
        core = _keep
        if not core:
            continue
        # 段內含數字或類別詞 → 那是屬性段（"electronics, 1500 each"），降權
        noisy = (any(t.replace('.', '').isdigit() for t in toks)
                 or any(t.lower() in _EN_CAT_WORDS for t in toks))
        # 泛稱段降權（'some stuff' 不該贏過 'bamboo toothbrush'）——
        #   實詞裡泛稱佔比越高、分數扣越多
        _vague_n = sum(1 for t in core if t.lower() in _EN_VAGUE_WORDS)
        score = len(core) - (2 if noisy else 0) - _vague_n * 2
        if score > best_score:
            best_score, best = score, " ".join(core)
    # 無標點長句（"add item Bluetooth Keyboard electronics price 800"）→
    #   切段法失效，改用第二判準：取**第一個類別詞之前**的實詞
    if not best:
        toks = re.findall(r"[A-Za-z0-9'\-]+", body)
        head_part = []
        for t in toks:
            if t.lower() in _EN_CAT_WORDS or t.replace('.', '').isdigit():
                break
            if t.lower() not in _EN_CMD_WORDS:
                head_part.append(t)
        best = " ".join(head_part)
    return best.strip(" ,:;-—.'\"")


# ── 品類詞表：head=主體詞（決定類別）／mod=修飾詞（單獨不足以定類）────
#   ⚠️ 這個區分是關鍵：'Coffee Filter Papers' 的主體是 **papers**（濾紙，
#     日用品），coffee 只是修飾。把 coffee 當 head 會判成食品（實測錯過）。
#   英文中心詞在後 → 從**句尾往前**找第一個實詞當 head。
_EN_CAT_KW = {
    "electronics": {
        "head": ["earphone", "earphones", "headphone", "headphones", "speaker",
                 "speakers", "mouse", "keyboard", "cable", "charger", "band",
                 "phone", "laptop", "adapter", "monitor", "camera", "tablet",
                 "fan", "powerbank", "battery", "watch", "console", "router",
                 "projector", "printer", "webcam", "microphone", "drive"],
        "mod": ["usb", "bluetooth", "wireless", "smart", "digital", "electric"],
    },
    "appliance_kitchen": {
        "head": ["pan", "pot", "cooker", "kettle", "blender", "iron", "mop",
                 "oven", "fryer", "cookware", "container", "containers", "jar",
                 "knife", "grill", "toaster", "vacuum", "toothbrush",
                 "machine", "dispenser", "steamer", "whisk", "spatula"],
        "mod": ["kitchen", "cooking", "ceramic", "stainless", "nonstick"],
    },
    "food_beverage": {
        "head": ["water", "tea", "beer", "juice", "drink", "nuts", "crackers",
                 "biscuit", "biscuits", "beans", "snack", "snacks", "candy",
                 "chocolate", "milk", "cereal", "rice", "noodle", "noodles",
                 "sauce", "wine", "soda", "coffee"],
        "mod": ["protein", "cocoa", "powder", "organic", "instant", "drip"],
    },
    "daily_goods": {
        "head": ["tissue", "tissues", "detergent", "soap", "wipes", "diaper",
                 "diapers", "gloves", "spray", "refill", "cleaner", "papers",
                 "shampoo", "toothpaste", "wash", "brush", "sponge", "broom"],
        "mod": ["cleaning", "laundry", "antibacterial", "baby", "trash",
                "filter", "repellent", "disposable"],
    },
    "apparel": {
        "head": ["shirt", "t-shirt", "tshirt", "socks", "jacket", "jeans",
                 "bra", "sleeve", "onesie", "beanie", "hat", "pants", "coat",
                 "dress", "sweater", "hoodie", "scarf", "cap", "gloves"],
        "mod": ["cotton", "wool", "denim", "elastic", "wicking", "knit"],
    },
    "sports": {
        "head": ["mat", "dumbbell", "dumbbells", "tent", "lantern", "ball",
                 "racket", "bike", "ring", "rope", "barbell", "treadmill"],
        "mod": ["yoga", "camping", "hiking", "running", "fitness", "sports",
                "sport", "outdoor", "resistance", "workout"],
    },
}
# 天生跨類別的容器/配件詞——一律問，不猜（保守派核心）
#   實測這類佔反問的絕大多數，而且**該問**：手機殼是電子、垃圾袋是日用、
#   水壺是運動，光看詞不可能分辨。
def _default_price(category: str) -> int:
    """r24c（user 定調：售價不重要、數量重要，別滿版 not set）——沒講價
    給**該類別現有商品中位數價**當參考價（退全店中位數、再退 100）。"""
    prices = [it.get("unit_price") or 0 for it in W.state().items
              if it.get("category") == category and (it.get("unit_price") or 0) > 0]
    if not prices:
        prices = [it.get("unit_price") or 0 for it in W.state().items
                  if (it.get("unit_price") or 0) > 0]
    if not prices:
        return 100
    prices.sort()
    med = prices[len(prices) // 2]
    return max(10, int(round(med / 10.0)) * 10)


_EN_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                 "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
                 "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
                 "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
                 "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
                 "ninety": 90}


def _en_spelled_num_normalize(text: str) -> str:
    """語音會唸英文數字（'eight hundred' / 'one thousand two hundred'）→
    正規化成阿拉伯數字，價格規則才接得到（r23 鏡射 zh 的「八百元」轉換；
    r11 實抓 'electronics eight hundred' 整串混進商品名）。
    只轉**帶 hundred/thousand 的序列**——裸 'two' 可能是型號的一部分不動。"""
    import re as _re
    _w = "|".join(_EN_NUM_WORDS)
    pat = _re.compile(
        rf"\b(?:(?:{_w})\s+)?(?:hundred|thousand)"
        rf"(?:\s+(?:and\s+)?(?:{_w}))?(?:\s+(?:{_w}))?\b"
        rf"|\b(?:{_w})\s+(?:hundred|thousand)"
        rf"(?:\s+(?:and\s+)?(?:{_w})(?:\s+(?:{_w}))?)?\b", _re.I)

    def conv(m):
        total, cur = 0, 0
        for t in _re.findall(r"[a-z]+", m.group(0).lower()):
            if t in _EN_NUM_WORDS:
                cur += _EN_NUM_WORDS[t]
            elif t == "hundred":
                cur = (cur or 1) * 100
                total += cur
                cur = 0
            elif t == "thousand":
                cur = (cur or 1) * 1000
                total += cur
                cur = 0
        return str(total + cur)

    return pat.sub(conv, text)


_EN_AMBIGUOUS_HEADS = {
    "towel", "set", "sets", "mug", "case", "bag", "bags", "kit", "bottle",
    "box", "holder", "stand", "cover", "pad", "rack", "tray", "basket",
    "shoes", "boots",          # 跑鞋歸運動或服飾都合理 → 問
    # r22（19 類上線後補）：跨**新舊類別**的歧義詞——實測
    #   'Folding Camping Chair' 因為新增家具類的 chair 被判成家具（真實=運動）。
    #   辦公椅=家具、露營椅=運動；桌燈=家具、露營燈=運動；電扇=電子、
    #   吊扇=家具。這些光看 head 分不出來 ⇒ 一律讓修飾詞決定或反問。
    "chair", "chairs", "table", "tables", "lamp", "mirror", "clock",
    # r23（create100 r11 實抓）：'blood pressure monitor' 被 head=monitor
    #   判成電子（真實=醫療）、'coffee grinder' 被 mod=coffee 拉去食品
    #   （真實=廚具）。monitor 螢幕=電子/血壓計=醫療/嬰兒監視器=母嬰、
    #   grinder 磨豆機=廚具/角磨機=五金——head 分不出來 ⇒ 反問（不猜原則）。
    "monitor", "grinder",
}
_EN_NAME_STOP = {"the", "and", "for", "with", "pack", "pcs", "pc", "men",
                 "mens", "women", "womens", "size", "inch", "pair", "person",
                 "ply", "kg", "ml", "cm", "mm", "new", "large", "small"}


def _en_guess_category(name: str) -> tuple:
    """從商品名猜類別。回 (category|None, reason)。

    None = 判不出來 → **問使用者**（不猜，user 定調保守派）。
    實測 60 個真商品：判對 83% / 判錯 3% / 反問 13%。
    ⚠️ 判錯會靜默寫進主檔（訪客不易察覺），反問只是多一輪對話 ⇒
      設計上一律偏向反問。
    """
    toks = [t for t in re.findall(r"[a-z]+", (name or "").lower())
            if len(t) >= 2]
    if not toks:
        return None, "no-token"
    # head = 句尾往前第一個非停用詞（英文中心詞在後）
    head = next((t for t in reversed(toks) if t not in _EN_NAME_STOP), None)
    if head is None:
        return None, "all-stop"
    if head in _EN_AMBIGUOUS_HEADS:
        return None, f"ambiguous:{head}"
    hits = [c for c, d in _EN_CAT_KW.items() if head in d["head"]]
    # r22：既有 6 類沒命中 → 查**新增 13 類**的品類詞（categories.py）。
    #   ⚠️ 順序有意義：既有 6 類的 head/mod 區分是實測判錯 0 的關鍵，
    #     一定要先讓它表態；新類別只在它沒話說時才補位。
    if not hits and _CAT19_KW:
        hits = [c for c, ws in _CAT19_KW.items()
                if head in ws and c not in _EN_CAT_KW]
    if len(hits) == 1:
        return hits[0], f"head:{head}"
    if len(hits) > 1:
        return None, f"head-multi:{head}"
    # head 沒命中 → 看全句修飾詞，**只有唯一一類**才收（多類=不明確→問）
    mod_hits = set()
    for t in toks:
        for c, d in _EN_CAT_KW.items():
            if t in d["mod"] or t in d["head"]:
                mod_hits.add(c)
    if len(mod_hits) == 1:
        return mod_hits.pop(), "mod-unique"
    return None, f"unclear:{len(mod_hits)}cats"


def classify_add_intent(text: str, has_item_in_master: bool) -> str:
    """`add X 50` 這種模糊句是**建檔**還是**進貨**？回 'create'/'inbound'/'ambiguous'。

    ⚠️ 為什麼需要這支（user 指出的真實衝突）：
      `add` / 中文「加」**兩個意思都通**——加一個「品項」是建檔、
      加「數量」是進貨。而 user 的語感是 `add keyboard 50` 比較像進貨。
    ⚠️ 判準優先序（明確講法照字面走，模糊講法看主檔）：
      ① 品項名詞（item/product/sku/商品/品項）→ 一定是建檔
      ② 建檔動詞（create/register/set up/建立/登錄）→ 建檔
      ③ 進貨動詞（received/restock/got/進貨/收到）→ 進貨
      ④ 帶**只有建檔才需要的欄位**（售價/類別/安全庫存）→ 建檔
         （進貨不會講「這個賣 800」）
      ⑤ 都沒有 → 看主檔：有這個商品就進貨，沒有就 ambiguous 讓上層決定
    """
    t = (text or "").lower()
    # ① 品項名詞——⚠️ **必須跟建檔動詞連用**。誤傷檢查抓到裸 item/items
    #   誤傷 20 句查詢（'which items need reordering' / 'top selling items' /
    #   'expiring stock list'）：items 在查詢句裡太常見，不能單獨當訊號。
    if re.search(r"\b(?:add|create|new|register|set\s*up)\s+(?:an?\s+|the\s+)?"
                 r"(?:new\s+)?(?:item|product|sku|listing)\b", t) \
            or re.search(r"(?:新增|建立|新建|加入|增加|登錄|上架)\s*(?:一[個支件款]?)?\s*"
                         r"(?:新的?)?(?:商品|品項)", text or ""):
        return "create"
    # ② 建檔動詞——⚠️ 不可收 `list`（'expiring stock list' 是查詢）。
    #   create/register/set up 這三個在倉管語境只會是建檔。
    if re.search(r"\b(?:create|register)\b", t) \
            or re.search(r"\bset\s+up\s+(?:an?\s+)?(?:new\s+)?\w", t) \
            or re.search(r"建立|新建|登錄|上架", text or ""):
        return "create"
    # ③ 進貨動詞
    if re.search(r"\b(?:received|receive|restock|restocked|stock\s*in|"
                 r"inbound|arrived|delivered)\b", t) \
            or re.search(r"進貨|收到|入庫|到貨|補貨|"
                         r"[北中南](?:區)?倉?\s*(?:進|收|補)\s*\d", text or ""):
        return "inbound"
    # ④b r23（create100 en r1）：'add a gaming keyboard electronics 2500'——
    #   **類別詞＋裸數字＋add/create 動詞**三條件同時 ⇒ 建檔（先前被 ⑤ 的
    #   主檔模糊比對搶去進貨：keyboard 撞 Mechanical Keyboard）。
    #   r22 誤傷的 89 句是裸類別詞（'electronics stock' 無數字無動詞），
    #   三條件版在 en 守衛複掃只命中 crt 建檔正解。
    if re.search(r"\b(?:add|create|register|new)\b", t) \
            and re.search(r"(?<!\d)\d{2,6}(?!\d)", t) \
            and not re.search(r"\b(?:to|into|at)\s+(?:the\s+)?"
                              r"(?:north|central|south|warehouse)\b", t):
        # r24 誤傷修（en 守衛實抓）：'add 100 whey drink to south' 是對既有
        #   商品的**進貨**——「to 倉別」方向詞在場就不搶建檔
        try:
            from categories import CATEGORIES as _C19ai
            for _cv_ai in _C19ai.values():
                for _a_ai in list(_cv_ai["aliases_en"]) + [_cv_ai["label_en"].lower()]:
                    if re.search(rf"\b{re.escape(_a_ai)}\b", t):
                        return "create"
        except Exception:
            pass
    # ④ 建檔專屬欄位——⚠️ **只認「欄位+數值」的組合**，不可只看詞。
    #   誤傷檢查（守衛 981 句）抓到：原本「含類別詞 → create」誤傷 **89 句**
    #   查詢（'electronics stock' / 'kitchen appliances' / 'daily goods stock'
    #   全被判成建檔）——類別詞在查詢句裡太常見，不能當建檔訊號。
    #   同理裸 price/safety 也不行（'whats the mouse safety stock' 是查設定）。
    #   ⇒ 要求**數值同時出現**：進貨句不會講「賣 800」或「安全庫存 30」。
    if re.search(r"\bprice\s*(?:is|:)?\s*\d|\bsafety\s*(?:stock)?\s*(?:is|:)?\s*\d|"
                 r"\bsells?\s+for\s+\d|\bcosts?\s+\d|\d+\s*each\b", t) \
            or re.search(r"售價\s*\d|單價\s*\d|安全庫存\s*\d|\d+\s*元", text or ""):
        return "create"
    # ⑤ 看主檔
    return "inbound" if has_item_in_master else "ambiguous"


# r22：新商品建檔時的預設值（安全庫存 + 三倉初始庫存都用這個）。
#   ⚠️ user 定調：不講數量時，安全庫存與三倉庫存**都給同一個預設值**，
#     這樣建好的商品在三個倉庫就不會是 0——展場能立刻查到庫存、
#     看得到三倉分布，也不會跳缺貨警示（剛好等於水位）。
#   50 是跟 live_sim.LiveConfig.default_safety 對齊的數字。
_DEFAULT_SAFETY = 50

# ── r22：19 類的料號前綴（新增 13 類用 categories.py 的 prefix_legacy）──
#   ⚠️ 既有 6 類的前綴**不可改**（e/a/f/d/c/s）——料號是主鍵，
#     改了既有 60 筆的歷史進出紀錄全部對不上。
try:
    from categories import CATEGORY_PREFIX_LEGACY as _CAT19_PFX, CATEGORIES as _CAT19
    # 新增 13 類的品類詞（既有 6 類維持 _EN_CAT_KW 的 head/mod 結構——
    #   那是實測判錯 0 的關鍵，不可被覆蓋）
    _CAT19_KW = {k: set(v["keywords"]) for k, v in _CAT19.items()}
except Exception:
    _CAT19_PFX, _CAT19, _CAT19_KW = {}, {}, {}

# ⚠️ r22：既有 60 筆已轉成 **ELE-0001** 三碼格式（全量轉換完成），
#   新建商品也要用同一套前綴，否則會生出 `a01` 這種舊格式跟主檔不一致
#   （CDP 畫面驗證抓到：建檔卡秀 `a01` 而主檔是 ELE-0001）。
#   ⇒ 一律取 categories.py 的三碼 prefix；載不到才退回舊單字母。
_CATEGORY_PREFIX = {}
try:
    from categories import CATEGORY_PREFIX as _CAT19_PFX3
    _CATEGORY_PREFIX.update(_CAT19_PFX3)
except Exception:
    _CATEGORY_PREFIX.update({
        "electronics": "e", "appliance_kitchen": "a", "food_beverage": "f",
        "daily_goods": "d", "apparel": "c", "sports": "s",
    })

def _next_sku(category: str) -> str:
    """自動產生下一個 SKU 流水號"""
    prefix = _CATEGORY_PREFIX.get(category, "OTH")
    existing = [it["sku_id"] for it in W.state().items
                if it["sku_id"].startswith(prefix)]
    nums = []
    for sid in existing:
        # ⚠️ 三碼格式是 `ELE-0001`，數字在連字號後（舊格式 `e01` 在第 1 碼後）。
        #   用 split("-") 同時吃得下兩種，不必判斷格式。
        _tail = sid.split("-", 1)[1] if "-" in sid else sid[len(prefix):]
        try:
            nums.append(int(_tail))
        except ValueError:
            pass
    # ⚠️ 料號**絕不可重用**（業界硬規則）：原本用 max(現有)+1，若 e10 被
    #   刪除，下一個新品會再拿到 e10 → 跟歷史進出紀錄撞號，過去那筆
    #   e10 的交易看起來變成新商品的。⇒ 記錄「用過的最大號」，只增不減。
    _used_max = _sku_seq_peek(prefix)
    next_num = max(max(nums) + 1 if nums else 1, _used_max + 1)
    _sku_seq_bump(prefix, next_num)
    # ⚠️ 位數（r22）：原本寫死 `:02d`，配上「料號永不重用」＝**累計建過
    #   99 個該類商品就用完**（不是同時有 99 個——刪掉的號也不還）。
    #   小店用兩三年就可能撞到。超過 99 不會報錯但會吐 3 碼的 e100，
    #   跟既有 3 碼料號並排時**字串排序會錯**（e10 < e100 < e11）。
    #   ⇒ 100 起改補零到 4 位（e0100），上限 9,999 個。
    #   ⚠️ 既有 60 筆是 2 位（e01~e10）**保持原樣不動**——料號是主鍵，
    #     改了歷史進出紀錄全部對不上（3,193 個檔案含料號）。
    #     新舊並存不影響比對（都是字串精確比對，不靠位數）。
    # r22：三碼前綴用 `ELE-0001` 格式（既有 60 筆已全量轉成這個）；
    #   舊式單字母前綴（categories.py 載不到時的退路）維持 `e01`。
    if len(prefix) >= 3:
        return f"{prefix}-{next_num:04d}"
    return f"{prefix}{next_num:02d}" if next_num <= 99 else f"{prefix}{next_num:04d}"


# 料號流水號高水位（只增不減，防重用）。存在 warehouse_data 供重啟後延續。
_SKU_SEQ_FILE = "sku_seq.json"


def _sku_seq_path():
    from pathlib import Path as _P
    try:
        d = getattr(W.state(), "v2_data_dir", "") or ""
        base = _P(d) if d else _P(__file__).resolve().parent
    except Exception:
        base = _P(__file__).resolve().parent
    return base / _SKU_SEQ_FILE


def _sku_seq_peek(prefix: str) -> int:
    try:
        import json as _j
        p = _sku_seq_path()
        if p.exists():
            return int(_j.loads(p.read_text(encoding="utf-8")).get(prefix, 0))
    except Exception:
        pass
    return 0


def _sku_seq_bump(prefix: str, n: int) -> None:
    try:
        import json as _j
        p = _sku_seq_path()
        d = {}
        if p.exists():
            d = _j.loads(p.read_text(encoding="utf-8"))
        if n > int(d.get(prefix, 0)):
            d[prefix] = n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_j.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass          # 寫不進去不影響建檔（退化成原本的 max+1 行為）


def create_item_start() -> dict:
    """觸發新增商品流程，回第一步問題"""
    return {
        "ok": True,
        # r24：一步建檔文案（不再講 Step 1/4）
        "summary": "Sure! What is the item called? Say the name and I will "
                   "set it up with defaults you can edit on the confirm card. "
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
        # r23：拼字數字先正規化（'eight hundred' → 800），見 helper 註解
        raw_text = _en_spelled_num_normalize(raw_text)
        # 嘗試解析：名稱 + 類別 + 價格 + 安全庫存 + 倉庫庫存
        # ⚠️ r23（中文詞表掃描）：**這段原本全中文** → 英文的「老手一句話」
        #   完全不支援：`add item Bluetooth Keyboard electronics price 800`
        #   整串被當成商品名（Name recorded: "Bluetooth Keyboard electronics
        #   price 800 safety 30"），走完流程會建出名字荒謬的商品。
        # ── r23（create100）：類別詞認 **19 類**（categories.py 單一來源）──
        #   舊版硬編 6 個 seed 類 → 'pet' / 'hardware' 等 13 類明講也抽不到。
        #   ⚠️ 順序照 CATEGORIES 定義序——'hiking backpack outdoor' 要讓
        #     sports 的 outdoor 先命中，不能被 luggage 的 backpack 搶走
        #     （generic 別名 bag/backpack 排在後面的類別，只當兜底）。
        _cat_map = {"電子": "electronics", "家電": "appliance_kitchen", "食品": "food_beverage",
                     "飲料": "food_beverage", "日用": "daily_goods", "服飾": "apparel", "運動": "sports"}
        _rt_low = raw_text.lower()
        _trail_cat_tok = ""     # r23b：名字尾端類別詞（見下方語音無逗號註解）
        _found_cat = next(
            (v for k, v in _cat_map.items() if k in raw_text), "")
        if not _found_cat:
            from categories import CATEGORIES as _C19cm
            # r23b：**先切商品名、把名字挖掉再找類別詞**——'oolong tea bags 180'
            #   的 bags 是商品名一部分，整句掃會被 luggage 的 bags 別名搶走
            #   （en r1 實測判錯 luggage）。名字挖掉後剩的才是真的「講類別」。
            _rt_for_cat = _rt_low
            try:
                if _is_mostly_en_text(raw_text):
                    _nm_pre = _en_cut_item_name(raw_text)
                    if _nm_pre:
                        _rt_for_cat = _rt_low.replace(_nm_pre.lower(), " ", 1)
            except Exception:
                pass
            # r23c：多個類別詞同時在句中（'kitchen paper towels … household'）
            #   → 取**位置最後**的那個——講類別的慣例在名字後面；名字裡的
            #   類別字（kitchen）在前面，不該贏。
            _best_pos, _best_cat = -1, ""
            for _ck, _cv in _C19cm.items():
                for _a in list(_cv["aliases_en"]) + [_ck, _cv["label_en"].lower()]:
                    for _m_a in _re.finditer(rf"\b{_re.escape(_a)}\b", _rt_for_cat):
                        if _m_a.start() > _best_pos:
                            _best_pos, _best_cat = _m_a.start(), _ck
            if _best_cat:
                _found_cat = _best_cat
            # r23b（語音無逗號）：'add item wiper blades automotive 450' 的
            #   類別詞被切名法**吸進名字尾端** → 挖名後的殘句找不到類別。
            #   ⇒ 名字尾 token 若是**非通用**類別別名 → 當類別、名字剝尾。
            #   通用詞（car/food/bag/game…）常是商品名結尾（rc car/dog food/
            #   sleeping bag），不做尾端判定、留給保守猜測（錯建比反問傷）。
            if not _found_cat and _nm_pre:
                _toks_nm = _nm_pre.lower().split()
                _GENERIC_TAIL = {"car", "auto", "food", "drink", "snack",
                                 "bag", "bags", "backpack", "book", "toy",
                                 "game", "games", "music", "water"}
                if len(_toks_nm) >= 2 and _toks_nm[-1] not in _GENERIC_TAIL:
                    for _ck, _cv in _C19cm.items():
                        _als = list(_cv["aliases_en"]) + [_ck,
                                                          _cv["label_en"].lower()]
                        if _toks_nm[-1] in _als:
                            _found_cat = _ck
                            _trail_cat_tok = _toks_nm[-1]
                            break
        # 價格：中文「500元」／英文 price 800 / $800 / 800 dollars
        _price_m = (_re.search(r'(\d+)\s*元', raw_text)
                    or _re.search(r'\bprice\s*(?:is|:)?\s*(\d+)', _rt_low)
                    or _re.search(r'\$\s*(\d+)', raw_text)
                    or _re.search(r'(\d+)\s*(?:dollars?|nt\$?|twd)\b', _rt_low)
                    # r22：**英文口語價格**沒收 → 'sells for 1200' / '450 each'
                    #   / 'costs 690' 全抽不到，出卡時價格 0（假成功）
                    or _re.search(r'\b(?:sells?|selling|sold|goes|going)\s+for\s+(\d+)',
                                  _rt_low)
                    or _re.search(r'\b(?:costs?|priced\s+at|retails?\s+(?:at|for))\s+(\d+)',
                                  _rt_low)
                    or _re.search(r'(\d+)\s*(?:each|apiece|per\s+(?:unit|piece|item))\b',
                                  _rt_low))
        # 安全庫存：中文「安全50」／英文 safety 50 / safety stock 50 / min 50
        _safety_m = (_re.search(r'安全\s*(\d+)', raw_text)
                     or _re.search(r'\bsafety(?:\s*stock)?\s*(?:is|:)?\s*(\d+)', _rt_low)
                     or _re.search(r'\bmin(?:imum)?\s*(?:is|:)?\s*(\d+)', _rt_low)
                     # r22：口語安全庫存 'keep 20 minimum' / 'keep at least 15'
                     or _re.search(r'\bkeep\s+(?:at\s+least\s+)?(\d+)', _rt_low)
                     or _re.search(r'\breorder\s+(?:at|point)\s*:?\s*(\d+)', _rt_low)
                     or _re.search(r'\balert\s+(?:at|when)\s*(\d+)', _rt_low))
        _north_m = (_re.search(r'北\S*\s*(\d+)', raw_text)
                    or _re.search(r'\bnorth\s*:?\s*(\d+)', _rt_low)
                    or _re.search(r'(\d+)\s*(?:units?\s*)?(?:to|in|at)\s+north\b', _rt_low))
        _south_m = (_re.search(r'南\S*\s*(\d+)', raw_text)
                    or _re.search(r'\bsouth\s*:?\s*(\d+)', _rt_low))
        _central_m = (_re.search(r'中\S*\s*(\d+)', raw_text)
                      or _re.search(r'\bcentral\s*:?\s*(\d+)', _rt_low))
        # r23：'40 in each warehouse' / '40 each warehouse' → 三倉同量
        #   （沒這條時 40 會被裸價格保底吃成單價；中文版本來就有「三倉各40」）
        #   ⚠️ 要求後面有 warehouse 字樣——'350 each' 是單價，不能搶。
        _each_en = _re.search(r'(\d+)\s*(?:units?\s*)?(?:in\s+)?each'
                              r'(?:\s+of\s+the\s+three)?\s+warehouses?\b', _rt_low)
        if _each_en and not (_north_m or _south_m or _central_m):
            _north_m = _south_m = _central_m = _each_en
        # ── r22：**裸價格保底**（同中文版）──────────────────────────
        #   'add hiking backpack, 1500, safety 10' 的 1500 前面沒有價格詞，
        #   所有價格規則都接不到 → 判成缺價格而反問（行為安全但不夠聰明）。
        #   ⇒ 把已被其他欄位吃掉的數字挖掉，若**剛好剩一個**孤立數字，
        #     視為價格。剩兩個以上代表語意不明確 → 不猜，交給分步流程問。
        if not _price_m:
            _rest_en = raw_text
            for _mm in (_safety_m, _north_m, _central_m, _south_m):
                if _mm:
                    _rest_en = _rest_en.replace(_mm.group(0), " ", 1)
            _free_en = _re.findall(r'(?<![\d])(\d+)(?![\d])', _rest_en)
            if len(_free_en) == 1:
                # ⚠️ 要對**原句**再 match 一次——上面是對挖空後的字串找的，
                #   直接用它的位置對不上原句。
                _price_m = _re.search(
                    r'(?<![\d])(' + _re.escape(_free_en[0]) + r')(?![\d])', raw_text)
        # ── r22：英文改用**切段法**（見 _en_cut_item_name 註解）─────────
        #   原本的剝除法實測只有 1/10——英文贅詞太多（a/the/its/called/
        #   new/item…），剝不乾淨就整串當商品名。切段法實測 10/10。
        #   中文維持原剝除法（中文要剝的詞有限，且 r22 已修好）。
        if _is_mostly_en_text(raw_text):
            _name = _en_cut_item_name(raw_text)
            # r23b：尾端 token 已判定為類別 → 從名字剝掉
            #   （'wiper blades automotive' → 名 'wiper blades' 類 automotive）
            if (_trail_cat_tok and _name
                    and _name.lower().split()[-1:] == [_trail_cat_tok]):
                _name = _name[:_name.lower().rfind(_trail_cat_tok)].strip(" ,-")
            # r23c：切段法整段切空的兜底（'kitchen paper towels' 因段內含
            #   類別字 kitchen 被屬性段降權整段丟掉）→ 改剝除法救回：
            #   拿掉欄位值/指令詞/**已判定的類別詞**後剩下的就是名字。
            if not _name and _found_cat:
                _nm2 = raw_text
                for _mm in (_price_m, _safety_m, _north_m, _central_m, _south_m):
                    if _mm:
                        _nm2 = _nm2.replace(_mm.group(0), " ", 1)
                _nm2 = _re.sub(r"\b(?:add|create|register|new|item|product|sku|"
                               r"a|an|the|please|help|me|for|thing|each|called)\b",
                               " ", _nm2, flags=_re.I)
                from categories import CATEGORIES as _C19nb
                _cv_nb = _C19nb.get(_found_cat, {})
                for _a2 in (list(_cv_nb.get("aliases_en", [])) + [_found_cat]
                            + [_cv_nb.get("label_en", "").lower()]):
                    if _a2:
                        _nm2 = _re.sub(rf"\b{_re.escape(_a2)}\b", " ", _nm2,
                                       flags=_re.I)
                _nm2 = _re.sub(r"\d+", " ", _nm2)
                _nm2 = _re.sub(r"\s+", " ", _nm2).strip(" ,.-&")
                if _nm2 and not _en_name_is_suspicious(_nm2):
                    _name = _nm2
            # ⚠️ 最後一道保險（r22）：切出來的名稱看起來不像真商品名
            #   （'some stuff' / 'a thing' / 單一過短 token）→ **不往下走**，
            #   回第一步重問。世界上的商品名無限，切名規則不可能永遠對；
            #   挖錯不可怕，靜默建出爛資料才可怕。
            if _name and _en_name_is_suspicious(_name):
                return {"ok": True,
                        "summary": ("Sorry — I couldn't work out the item name "
                                    "from that.\nStep 1: what is the item called? "
                                    '(say "cancel" to exit)'),
                        "view": "item_create_step1",
                        "data": {"step": 1, "prompt": "Please enter the item name"}}
        else:
            _name = raw_text
            for pat in [r'電子\S*', r'家電\S*', r'食品\S*', r'日用\S*', r'服飾\S*',
                        r'運動\S*', r'\d+元', r'安全\d+', r'北\S*\d+', r'南\S*\d+',
                        r'中\S*\d+', r'新增商品\s*']:
                _name = _re.sub(pat, '', _name).strip()
            _name = _re.sub(r'\s{2,}', ' ', _name).strip(' ,:;-')
        # ── r22：使用者沒明講類別 → 從商品名猜（保守派：判不出來就問）──
        #   實測 60 個真商品：判對 48、判錯 **0**、反問 12。
        #   ⚠️ 只在使用者沒講類別時才猜；他講了就以他為準。
        _cat_guessed = False
        if _name and not _found_cat and _is_mostly_en_text(raw_text):
            _g_cat, _g_why = _en_guess_category(_name)
            if _g_cat:
                _found_cat = _g_cat
                _cat_guessed = True
        # ── r22（user 定調「一句話就進去」）：**不再因為缺欄位反問**。
        #   先前版本缺售價/安全庫存就回 step 3 追問，但實務上：
        #     · 建檔當下**本來就常不知道售價**（掃碼建檔也是這樣）
        #     · 安全庫存可以從初始庫存推（進多少就維持多少）
        #   ⇒ 缺的欄位填合理預設，直接出確認卡，卡上秀出來讓人看。
        #   ⚠️ 商品名仍是唯一必填（沒名字這筆資料沒意義，見 suspicious 檢查）。
        if _name and _found_cat:
            # 防呆：檢查同名
            if any(_norm_item_name(it["name"]) == _norm_item_name(_name)
                   for it in W.state().items):
                return {"ok": True, "summary": f'⚠️ Item "{_name}" already exists. Please use a different name.',
                        "view": "item_create_step1", "data": {"step": 1, "prompt": "Please enter a different item name"}}
            new_sku = _next_sku(_found_cat)
            _n_qty = int(_north_m.group(1)) if _north_m else 0
            _c_qty = int(_central_m.group(1)) if _central_m else 0
            _s_qty = int(_south_m.group(1)) if _south_m else 0
            _init_total = _n_qty + _c_qty + _s_qty
            # ── r22 安全庫存三層 fallback（user 定調，節省建檔步驟）──────
            #   ① 明講的最優先
            #   ② 沒明講但有初始庫存 → **安全庫存 = 初始庫存**
            #      （倉管直覺：進了多少就維持多少水位；也讓新品建完
            #        不會立刻跳缺貨警示）
            #   ③ 都沒有 → 預設值（_DEFAULT_SAFETY，見該常數註解）
            if _safety_m:
                _safety_val, _safety_src = int(_safety_m.group(1)), "stated"
            elif _init_total > 0:
                # r23：從初始庫存推的水位取**單倉最大值**不是總和——缺貨判定
                #   是每倉各自 < 安全庫存，'40 in each warehouse' 推成 120
                #   會讓三倉建完立刻全跳缺貨（假成功；中文版函式探針實抓）
                _safety_val, _safety_src = max(_n_qty, _c_qty, _s_qty), "from_stock"
            else:
                _safety_val, _safety_src = _DEFAULT_SAFETY, "default"
            # ⚠️ **沒講倉庫量 → 三倉都補成安全庫存值**（user 定調
            #   「安全庫存預設不是 0，這樣建好的商品在三個倉庫就不會是 0」）。
            #   缺貨判定是「每個倉各自 < 安全庫存」，若三倉留 0 而安全庫存
            #   有值（不論是明講的 30 還是預設 50），建好**三倉立刻全跳缺貨**
            #   （實測確認）。補成等於水位 ⇒ 剛好不觸發、展場也查得到數字。
            if _init_total == 0 and _safety_val > 0:
                _n_qty = _c_qty = _s_qty = _safety_val
            # r24c：沒講價 → 類別中位數參考價（price_unset 保留標記）
            _price_val = (int(_price_m.group(1)) if _price_m
                          else _default_price(_found_cat))
            pending = {
                "name": _name, "category": _found_cat,
                "category_label": W.CATEGORY_LABEL.get(_found_cat, _found_cat),
                "price": _price_val,
                "safety": _safety_val,
                "stock_north": _n_qty,
                "stock_central": _c_qty,
                "stock_south": _s_qty,
                "sku": new_sku,
                # r22：類別是系統猜的 → 前端可據此標示「可修改」
                "category_guessed": _cat_guessed,
                # r22：價格沒講 → 標記未定價（跟「真的賣 0 元」區分開，
                #   庫存價值統計可據此排除，不汙染報表）
                "price_unset": not bool(_price_m),
                # r22：安全庫存哪來的（stated/from_stock/default）
                "safety_src": _safety_src,
            }
            # ⚠️ 猜的/推的欄位要**講出來**，讓人有機會改（HITL 原則）。
            _notes = []
            if _cat_guessed:
                _notes.append(f'category "{W.CATEGORY_LABEL.get(_found_cat, _found_cat)}"')
            if _safety_src == "from_stock":
                _notes.append(f"safety stock {_safety_val} (same as opening stock)")
            elif _safety_src == "default":
                _notes.append(f"safety stock {_safety_val} (default)")
            if not _price_m:
                _notes.append(f"reference price NT$ {_price_val} "
                              "(category median, editable)")
            _sum = ("Item details parsed — please confirm" if not _notes else
                    "Item details parsed — I filled in " + ", ".join(_notes)
                    + ". Change anything on the card before confirming.")
            return {"ok": True, "summary": _sum, "view": "item_confirm",
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
        existing = [it for it in W.state().items
                    if _norm_item_name(it["name"]) == _norm_item_name(name)]
        if existing:
            return {"ok": True, "summary": f'⚠️ Item "{name}" already exists (SKU: {existing[0]["sku_id"]}). '
                           "Please use a different name.",
                    "view": "item_create_step1",
                    "data": {"step": 1, "prompt": "Please enter a different item name"}}
        # r24（user 定調 2026-08-08）：**one step——名字進來直接出確認卡**。
        #   原四步流程展場太拖。類別先讓 head-noun 猜（實測判對 83%/錯 3%），
        #   猜不到歸 Other；價格未定、安全庫存/三倉給預設。
        #   卡上全看得到、可改可取消（HITL 確認關卡不變）。
        _g24, _ = _en_guess_category(name)
        _cat24 = _g24 or "other"
        new_sku = _next_sku(_cat24)
        _p24 = _default_price(_cat24)   # r24c：參考價（同類中位數）
        pending = {
            "name": name, "category": _cat24,
            "category_label": W.CATEGORY_LABEL.get(_cat24, _cat24),
            "price": _p24, "safety": _DEFAULT_SAFETY,
            "stock_north": _DEFAULT_SAFETY, "stock_central": _DEFAULT_SAFETY,
            "stock_south": _DEFAULT_SAFETY, "sku": new_sku,
            "category_guessed": bool(_g24),
            "price_unset": True, "safety_src": "default",
        }
        _lbl24 = pending["category_label"]
        return {"ok": True,
                "summary": (f'Got it — "{name}"! I filled in: category '
                            f'"{_lbl24}", safety stock {_DEFAULT_SAFETY} '
                            f"(default), reference price NT$ {_p24} "
                            "(editable).\n"
                            "Change anything on the card before confirming, "
                            f'or say the full thing (e.g. "add item {name} '
                            'electronics 500") to redo.'),
                "view": "item_confirm",
                "data": {"pending": True, "item": pending}}
    elif step == 2:
        # r75：類別欄要驗證＋正規化成主檔 key——「陶瓷馬克杯」曾被當類別吸收
        # 造成整條流程欄位錯位；中文原字入檔會生出幻影類別（SKU 也拿到 x 前綴）
        # EN build：補英文類別別名。第二步的提示語是英文
        #   （"electronics / appliance & kitchen / food & beverage /
        #     daily goods / apparel / sports"），訪客照著打 "daily goods"
        #   （帶空格）對不到主檔 key "daily_goods" → 卡在第二步出不去。
        _cat_zh2key = {"electronic": "electronics", "electronics": "electronics",
                       "appliance": "appliance_kitchen", "kitchen": "appliance_kitchen",
                       "appliance & kitchen": "appliance_kitchen",
                       "appliance and kitchen": "appliance_kitchen",
                       "food": "food_beverage", "beverage": "food_beverage",
                       "drink": "food_beverage", "food & beverage": "food_beverage",
                       "food and beverage": "food_beverage",
                       "daily": "daily_goods", "daily goods": "daily_goods",
                       "daily good": "daily_goods", "household": "daily_goods",
                       "apparel": "apparel", "clothing": "apparel", "clothes": "apparel",
                       "sport": "sports", "sports": "sports", "fitness": "sports",
                       "電子": "electronics", "3c": "electronics",
                       "家電": "appliance_kitchen", "廚具": "appliance_kitchen", "廚房": "appliance_kitchen",
                       "食品": "food_beverage", "飲料": "food_beverage",
                       "日用": "daily_goods", "生活": "daily_goods",
                       "服飾": "apparel", "衣": "apparel",
                       "運動": "sports"}
        _cat_key = next((v for k, v in _cat_zh2key.items()
                         if k in (category or "").lower()), "")
        # r23：6 類以外 → 查 categories.py 全 19 類別名（step-2 卡死修補）
        if not _cat_key:
            from categories import CATEGORIES as _C19s2
            _clow = (category or "").lower()
            for _ck, _cv in _C19s2.items():
                _als = list(_cv["aliases_en"]) + [_ck, _cv["label_en"].lower()] \
                       + [a for a in _cv["aliases_zh"] if len(a) >= 2]
                if any((a in _clow) if a.isascii() else (a in (category or ""))
                       for a in _als):
                    _cat_key = _ck
                    break
        if not _cat_key:
            from categories import CATEGORIES as _C19chk
            if category in _C19chk:
                _cat_key = category
        if not _cat_key:
            return {"ok": True,
                    "summary": (f'"{category}" is not a category. You can say: '
                                "electronics / kitchen / food / daily / apparel / sports / "
                                "hardware / beauty / medical / stationery / pet / automotive / "
                                "furniture / baby / media / industrial / toys / luggage "
                                '(say "cancel" to exit)'),
                    "view": "item_create_step2",
                    "data": {"step": 2, "name": name,
                             # r8：類別填錯時的**重問**路徑（happy path 的
                             #   1711 早已英文化，這條邊界分支漏了）
                             "prompt": 'Choose a category (or say "cancel" to exit)'}}
        category = _cat_key
        _cat_lbl2 = W.CATEGORY_LABEL.get(category, category)
        return {"ok": True,
                "summary": f'Recorded: "{name}" → {_cat_lbl2}\n'
                           "Step 3: unit price and safety stock?\n"
                           'e.g. "150 100" (say "cancel" to exit)',
                "view": "item_create_step3",
                "data": {"step": 3, "name": name, "category": category,
                         # r8：這行是**前端卡片顯示的提示**（summary 早已英文化，
                         #   單這個 prompt 漏了）→ 訪客在 step 3 看到整句中文
                         "prompt": 'Format: unit price, safety stock '
                                   '(e.g. "150 100", or say "cancel")'}}
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
                "summary": f'📦 Ready to add "{name}"\n'
                           f"Category: {pending['category_label']} | "
                           f"Unit price: {pending['price']} | "
                           f"Safety stock: {pending['safety']}\n"
                           f"Initial stock: {stock_summary}",
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
        # EN build：再濾一層「只靠**共用修飾詞**命中」的候選。
        #   'Electric Mop'(16) 會把只沾到 Electric 的 'Electric Toothbrush'(8)
        #   留下（8*2>=16）→ 誤報「matches 2 items」（主檔只有一個 Mop）。
        #   判準：查詢詞裡有某個 token 只出現在第一名、不在候選名裡 → 候選
        #   不是訪客要的。⚠️ 真歧義（coffee → 5 個咖啡商品）每個都含 coffee，
        #   不會被這條濾掉，仍正常反問。
        if len(scored) > 1 and keyword:
            _q_toks = [t for t in re.split(r"[\s\-/]+", str(keyword).lower())
                       if len(t) >= 3 and t.isascii()]
            if _q_toks:
                _top_nm = scored[0]["item"]["name"].lower()
                _disc = [t for t in _q_toks if t in _top_nm]
                if _disc:
                    scored = [m for m in scored
                              if m is scored[0]
                              or all(t in m["item"]["name"].lower() for t in _disc)]
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
        _wh_en = WH_LABEL_MAP.get(wh_key, wh_key or "north")
        return {"ok": True, "view": "clarify",
                "summary": f'How many {item["name"]} were {_dir_en}? '
                           f'e.g. "{_dir_en} 50".',
                # ⚠️ options 是**送回後端的查詢字串** → 要用英文倉名。
                #   wh 是訪客原始輸入（可能是中文「北倉」，或已正規化的 key），
                #   直接塞進去會產生中文選項 → 英文版後端 reject（一點就壞）。
                "data": {"question": f'How many {item["name"]} were {_dir_en}?',
                         "options": [f"{_wh_en} {_dir_en} 10 {item['name']}",
                                     f"{_wh_en} {_dir_en} 30 {item['name']}",
                                     f"{_wh_en} {_dir_en} 50 {item['name']}"],
                         "hint": f'e.g. "{_dir_en} 50"',
                         # flow 是內部續流程狀態（訪客看不到），但存**正規化
                         #   的 key** 比較乾淨，也避免中文值流到別處
                         "flow": {"tool": "create_movement", "await": "qty",
                                  "keyword": item["name"], "warehouse": wh_key or wh,
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


# 動態模擬的來源 —— 這些 actor 的異動不寫 audit log（見 commit_movement 註解）
_SIM_ACTORS = {"pda_scan", "wms_sync", "ecom_order"}


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

        # ⚠️ **動態模擬的異動不寫 audit log**（2026-08-03）：
        #   audit log 是「誰改了什麼」的稽核軌跡、給人看的；模擬是背景常態
        #   流量，不是人的操作。200× 下每 2.7 秒 60 筆 ⇒ 實測一天衝到 34MB
        #   （比所有匯出檔加起來還大）。資料本身（stock.csv / transactions/）
        #   照常寫，查詢與報表不受影響。
        if actor not in _SIM_ACTORS:
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
        # mv-hot-sync（2026-08-02）：**同時補一筆 movements**——
        #   原本只熱更新 s.stock，導致「查庫存有變、查進出紀錄查不到那筆」
        #   （query_movement 讀的是 s.movements，全檔只讀不寫）。
        #   展場風險：訪客查不到剛記的那筆 → 以為沒成功 → **重複進貨**。
        #   date 用 snap_date（demo 基準日），與 transactions CSV 一致，
        #   否則查「today」仍對不上。
        try:
            s.movements.append({"date": snap_date, "sku_id": sku,
                                "warehouse": wh_key, "direction": dir_key,
                                "qty": qty_val})
        except Exception:
            pass

    # ⚠️ r22（已移除「第一次進貨自動設安全庫存」）：改成**建檔當下**就把
    #   安全庫存與三倉庫存都設成預設值（user 定調），進貨就回歸純進貨，
    #   不再有隱含的水位副作用。相關邏輯在 create_item_collect。
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
        # EN build：再濾一層「只靠**共用修飾詞**命中」的候選。
        #   'Electric Mop'(16) 會把只沾到 Electric 的 'Electric Toothbrush'(8)
        #   留下（8*2>=16）→ 誤報「matches 2 items」（主檔只有一個 Mop）。
        #   判準：查詢詞裡有某個 token 只出現在第一名、不在候選名裡 → 候選
        #   不是訪客要的。⚠️ 真歧義（coffee → 5 個咖啡商品）每個都含 coffee，
        #   不會被這條濾掉，仍正常反問。
        if len(scored) > 1 and keyword:
            _q_toks = [t for t in re.split(r"[\s\-/]+", str(keyword).lower())
                       if len(t) >= 3 and t.isascii()]
            if _q_toks:
                _top_nm = scored[0]["item"]["name"].lower()
                _disc = [t for t in _q_toks if t in _top_nm]
                if _disc:
                    scored = [m for m in scored
                              if m is scored[0]
                              or all(t in m["item"]["name"].lower() for t in _disc)]
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
        # mv-hot-sync（2026-08-02）：調撥同樣要補 movements，且**記兩筆**
        #   （來源倉 out、目標倉 in），與 transactions CSV 的寫法一致。
        try:
            s.movements.append({"date": snap_date, "sku_id": sku,
                                "warehouse": from_key, "direction": "out",
                                "qty": qty_val})
            s.movements.append({"date": snap_date, "sku_id": sku,
                                "warehouse": to_key, "direction": "in",
                                "qty": qty_val})
        except Exception:
            pass

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

    # 🚨 2026-08-06（ZH 半刪災難同款修，兩版同步）：rmtree 裸奔在寫檔中會
    #   撞 Directory not empty → 半刪。雙 tmp 原子交換。
    _tmp_new = root / "warehouse_data_new_tmp"
    _tmp_old = root / "warehouse_data_old_tmp"
    shutil.rmtree(_tmp_new, ignore_errors=True)
    shutil.rmtree(_tmp_old, ignore_errors=True)
    shutil.copytree(baseline, _tmp_new)
    current.rename(_tmp_old)
    _tmp_new.rename(current)
    shutil.rmtree(_tmp_old, ignore_errors=True)

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
