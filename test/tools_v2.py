"""
tools_v2.py — v2 三金剛 Agentic 工具（search_log / manage_config / run_script）。

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
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import warehouse as W   # 用 W.state() / W.match_items() / W._err()


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
    """keyword → 命中的 SKU item 清單（沿用 v1 match_items）。"""
    if not keyword:
        return []
    hits = W.match_items(keyword)
    return [h["item"] for h in hits]


# ════════════════════════════════════════════════════════════
# ① search_log — 紀錄檔搜尋 / RCA「PO 對不上」
#    Host 編排：Glob transactions → Grep keyword → 若有短收 → 對 PO → Reason
# ════════════════════════════════════════════════════════════
def search_log(keyword: str = "", time_range: str | None = None, source: str | None = None) -> dict:
    steps: list[dict] = []
    dd = _data_dir()
    tx_dir = dd / "transactions"
    if not tx_dir.exists():
        return W._err("找不到交易紀錄檔目錄")

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
        _trace(steps, "glob", "未指定商品 → 全域掃描所有採購單")
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
               f"掃完 {po_count} 張採購單，JOIN 收貨記錄（receipts）計算應收 vs 實收")
        if all_disc:
            all_disc.sort(key=lambda d: d["gap"], reverse=True)
            _trace(steps, "reason",
                   f"發現 {len(all_disc)} 筆短收，最大：{all_disc[0]['name']} "
                   f"（{all_disc[0]['po_id']}）應收 {all_disc[0]['order_qty']} / "
                   f"實收 {all_disc[0]['received_qty']} → 差 {all_disc[0]['gap']} 件")
            total_gap = sum(d["gap"] for d in all_disc)
            summary = (f"全倉共 {len(all_disc)} 筆採購對帳異常（PO 對不上），合計短收 {total_gap} 件。"
                       f"最大筆：{all_disc[0]['name']} 在 {all_disc[0]['po_id']} 短收 {all_disc[0]['gap']} 件。")
        else:
            _trace(steps, "reason", f"掃完 {po_count} 張採購單，未發現短收")
            summary = "全域掃描完成，目前無採購短收異常。"
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
    _trace(steps, "glob", f"掃 transactions/ → 命中 {len(files)}/{len(all_files)} 個交易檔",
           matched=len(files), total=len(all_files), time_range=time_range or "全部")

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
    kw_disp = keyword or "全部商品"
    _trace(steps, "grep", f"在交易檔中比對「{kw_disp}」→ 找到 {len(rows)} 筆"
           + (f"（截斷顯示前 {MAX_ROWS}）" if truncated else ""),
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
               f"掃採購單（orders/PO）→ 找到 {len(relevant_pos)} 張含「{sku_label}」的 PO",
               sub_lines=[f"{p['po_id']}  {p['date']}  {p['warehouse']}  {p['supplier']}"
                          for p in relevant_pos[:4]]
               + ([f"…另有 {len(relevant_pos)-4} 張"] if len(relevant_pos) > 4 else []))

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
                batch_str = "、".join(
                    f"{b['receipt_date']} 收 {b['received_qty']} 件" for b in batches
                ) or "（無收貨記錄）"
                if gap > 0:
                    compare_lines.append(
                        f"⚠  {po['po_id']}  應收 {order_qty} / 實收 {recv_qty} → 短收 {gap} 件"
                        f"\n   收貨批次：{batch_str}"
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
            display_lines.append(f"…另有 {len(warn_lines)-6} 筆短收")
        if ok_count:
            display_lines.append(f"✓  其餘 {ok_count} 張正常")
        _trace(steps, "read",
               f"逐張比對收貨記錄（receipts）→ 查完 {len(relevant_pos)} 張",
               sub_lines=display_lines)

    # ④ Reason：產出結論
    sup_by_id = {s["supplier_id"]: s["name"] for s in W.state().v2_suppliers}
    WH_LABEL = {"north": "北區倉", "central": "中區倉", "south": "南區倉"}
    if discrepancies:
        d0 = discrepancies[0]
        sup_name = sup_by_id.get(d0["supplier"], d0["supplier"])
        wh_label = WH_LABEL.get(d0["warehouse"], d0["warehouse"])
        # 推理摘要：每步一行，最後是結論
        lines_out = [f"🔍 鎖定商品：{d0['name']}"]
        # 列出每張短收 PO（最多 3 筆）
        for d in discrepancies[:3]:
            wl = WH_LABEL.get(d["warehouse"], d["warehouse"])
            sl = sup_by_id.get(d["supplier"], d["supplier"])
            lines_out.append(
                f"📋 {d['po_id']} ({d['date']}, {wl}, {sl})\n"
                f"   應收 {d['order_qty']} 件 / 實收 {d['received_qty']} 件 → 短收 {d['gap']} 件 ⚠"
            )
        if len(discrepancies) > 3:
            lines_out.append(f"   …另有 {len(discrepancies)-3} 筆短收")
        lines_out.append(
            f"✅ 結論：共 {len(discrepancies)} 筆短收，合計差 "
            f"{sum(d['gap'] for d in discrepancies)} 件，建議聯絡供應商確認。"
        )
        summary = "\n".join(lines_out)
        _trace(steps, "reason",
               f"確認短收：{len(discrepancies)} 筆，最大 {d0['po_id']} 差 {d0['gap']} 件")
        cause_found = True
    else:
        if rows:
            tin  = sum(r["qty"] for r in rows if r["direction"] == "in")
            tout = sum(r["qty"] for r in rows if r["direction"] == "out")
            if sku_ids:
                summary = (f"🔍 鎖定商品：{kw_disp}\n"
                           f"📋 查完所有相關 PO，未發現短收\n"
                           f"✅ 結論：進貨 {tin} 件、出貨 {tout} 件，帳目正常。")
            else:
                summary = (f"🔍 泛查「{kw_disp}」：共 {len(rows)} 筆異動\n"
                           f"   進貨 {tin} 件、出貨 {tout} 件\n"
                           f"💡 輸入具體商品名稱可追查短收原因")
        else:
            summary = f"查無「{kw_disp}」在指定範圍的異動紀錄。"
        _trace(steps, "reason", "未發現短收（已查PO）" if sku_ids else "泛查無PO對帳")
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
    "safety_stock":      ["安全庫存", "安全存量", "安全水位", "safety stock", "safety_stock"],
    "reorder_lead_days": ["前置天數", "補貨前置", "前置時間", "lead time", "lead_days", "前置"],
    "safety_buffer_ratio": ["安全水位倍數", "安全倍數", "buffer", "緩衝倍數"],
    "restock_target_days": ["補貨目標天數", "補到撐", "target days", "撐幾天"],
}


def _resolve_key(key: str) -> str | None:
    """設定項 keyword → 正規 config key（不進模型 enum）。"""
    if not key:
        return None
    k = key.replace(" ", "").lower()
    for canon, aliases in _KEY_ALIASES.items():
        if canon in k:
            return canon
        for a in aliases:
            if a.replace(" ", "").lower() in k:
                return canon
    return None


def _parse_value(value):
    """解析寫入值，判斷相對(+30/-5) vs 絕對(50)。回 (mode, number)。"""
    if value is None:
        return None, None
    sv = str(value).strip()
    if sv.startswith("+"):
        return "delta", int(sv[1:])
    if sv.startswith("-") and sv[1:].isdigit():
        return "delta", -int(sv[1:])
    try:
        return "abs", int(float(sv))
    except ValueError:
        return None, None


def manage_config(action: str = "read", key: str = "", value=None,
                  warehouse: str = "all") -> dict:
    steps: list[dict] = []
    cfg = W.state().v2_config
    canon = _resolve_key(key)
    if not canon:
        return W._err(f"看不懂要查/改哪個設定項：「{key}」")

    # ── read ──
    if action == "read":
        _trace(steps, "read", f"讀取設定 master/config.json → {canon}")
        if canon == "safety_stock":
            base = cfg.get("safety_stock_base", {})
            ov = cfg.get("safety_stock_override", {})
            # 若指定 keyword 對應某些 SKU，回那些；否則回整體說明
            skus = _kw_to_skus(key)  # key 可能含商品名
            rows = []
            target_skus = [it["sku_id"] for it in skus] or list(base.keys())[:10]
            for sku in target_skus:
                name = W._items_by_sku.get(sku, {}).get("name", sku) if hasattr(W, "_items_by_sku") \
                       else W.state()._items_by_sku.get(sku, {}).get("name", sku)
                eff = {}
                for wh in (["north", "central", "south"] if warehouse == "all" else [warehouse]):
                    eff[wh] = ov.get(wh, {}).get(sku, base.get(sku, 0))
                rows.append({"sku_id": sku, "name": name, "by_warehouse": eff, "base": base.get(sku, 0)})
            summary = f"目前安全庫存設定（{len(rows)} 項）：基準值寫在 config，可分倉覆寫。"
            return {"ok": True, "summary": summary, "view": "config_read",
                    "data": {"canon": canon, "rows": rows, "trace": steps}}
        else:
            cur = cfg.get(canon)
            label = {"reorder_lead_days": "補貨前置天數", "safety_buffer_ratio": "安全水位倍數",
                     "restock_target_days": "補貨目標天數"}.get(canon, canon)
            summary = f"目前「{label}」設定為：{cur}。"
            return {"ok": True, "summary": summary, "view": "config_read",
                    "data": {"canon": canon, "current": cur, "label": label, "trace": steps}}

    # ── set：模型只到「抽出意圖」這步；回 pending_confirm 讓 server 二次確認 ──
    if action == "set":
        mode, num = _parse_value(value)
        if mode is None:
            return W._err(f"看不懂要把設定改成什麼值：「{value}」")

        # 算受影響範圍 + 預覽 diff（不寫入！）
        whs = ["north", "central", "south"] if warehouse == "all" else [warehouse]
        if canon == "safety_stock":
            base = cfg.get("safety_stock_base", {})
            ov = cfg.get("safety_stock_override", {})
            skus = _kw_to_skus(key)
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
            verb = f"{'增加' if num >= 0 else '減少'} {abs(num)}" if mode == "delta" else f"設為 {num}"
            wh_label = "全部倉" if warehouse == "all" else \
                       {"north": "北區倉", "central": "中區倉", "south": "南區倉"}.get(warehouse, warehouse)
            scope = "全部商品" if not skus else "、".join(it["name"] for it in skus[:3])
            summary = (f"準備把【{wh_label}】的【{scope}】安全庫存{verb}，"
                       f"共影響 {len(preview)} 項。請確認後才會寫入。")
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
            label = {"reorder_lead_days": "補貨前置天數", "safety_buffer_ratio": "安全水位倍數",
                     "restock_target_days": "補貨目標天數"}.get(canon, canon)
            summary = f"準備把「{label}」從 {old} 改為 {new}。請確認後才會寫入。"
            return {"ok": True, "summary": summary, "view": "config_confirm",
                    "data": {"pending": True, "canon": canon, "old": old, "new": new,
                             "label": label, "trace": steps}}

    return W._err(f"不支援的 config 動作：{action}")


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
    return {"ok": True, "summary": f"已寫入 {changed} 項，並備份到 config.json.bak、記錄到 audit log。",
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
    _trace(steps, "read", f"比對白名單 manifest.json → 「{script_name}」")
    if not sc:
        avail = "、".join(s["label"] for s in _load_manifest().get("scripts", []))
        return W._err(f"「{script_name}」不在可執行白名單內。可用：{avail}", view="error")

    # 安全護欄：只回「待確認」，不直接 subprocess（執行交給 server confirm 後）
    _trace(steps, "confirm", f"命中白名單腳本：{sc['label']}（逾時上限 {sc['timeout_s']}s）")
    summary = f"準備執行白名單腳本【{sc['label']}】：{sc.get('description', sc.get('desc', ''))}。請確認後執行。"
    return {"ok": True, "summary": summary, "view": "script_confirm",
            "data": {"pending": True, "script_id": sc["id"], "label": sc["label"],
                     "desc": sc.get("description", sc.get("desc", "")), "timeout_s": sc["timeout_s"], "trace": steps}}


# 白名單腳本實際指令（server confirm 後呼叫 commit_run_script）
_SCRIPT_CMD = {
    # id → (相對 warehouse_v2/ 的 python 腳本, 額外 args)
    "stock_audit":      ("test/warehouse_data/scripts/stock_audit.py",
                         ["--data-dir", "test/warehouse_data"]),
    "export_movements": ("test/warehouse_data/scripts/export_movements.py",
                         ["--data-dir", "test/warehouse_data", "--days", "30"]),
    "generate_report":  ("test/warehouse_data/scripts/generate_report.py",
                         ["--data-dir", "test/warehouse_data", "--type", "full"]),
}


def commit_run_script(script_id: str, actor: str = "user_confirmed",
                      trace_id: str | None = None) -> dict:
    sc = next((s for s in _load_manifest().get("scripts", []) if s["id"] == script_id), None)
    if not sc:
        return W._err("腳本不存在")
    spec = _SCRIPT_CMD.get(script_id)
    if not spec:
        return W._err(f"腳本 {script_id} 未綁定指令")
    rel, extra = spec
    root = _data_dir().parent.parent          # warehouse_v2/
    script_path = root / rel
    ts = datetime.now().isoformat(timespec="seconds")
    trace_id = trace_id or f"run-{ts}"

    if not script_path.exists():
        return W._err(f"找不到腳本檔：{rel}")

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
        ok, tail = False, f"逾時（>{sc['timeout_s']}s）已中止"
    except Exception as e:
        ok, tail = False, f"執行失敗：{e}"

    # audit
    snap = W.state().snapshot_date or ts[:10]
    with open(_data_dir() / "audit" / f"{snap}_changes.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "trace_id": trace_id, "actor": actor,
                            "action": "run_script", "script_id": script_id, "ok": ok},
                           ensure_ascii=False) + "\n")
    return {"ok": ok, "summary": f"腳本【{sc['label']}】執行{'完成' if ok else '失敗'}。",
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

    _trace(steps, "glob", f"掃全倉 {len(s.warehouses)} 倉 / {len(s.items)} SKU 收集報告素材")

    md = [f"# 倉儲報告 — {('全倉體檢' if rt=='full' else rt)}",
          f"\n> 產生時間：{ts}　資料快照：{snap}　產生者：{actor}（trace {trace_id}）\n"]

    # ── 庫存總覽 ──
    if rt in ("full",):
        ds = W.dashboard_snapshot()
        _trace(steps, "reason", "彙整庫存總覽")
        rows = [[w["label"], f"{w['item_count']:,}", f"NT$ {w['stock_value']:,}"]
                for w in ds["warehouse_summary"]]
        md.append("## 一、庫存總覽")
        md.append(_md_table(["倉別", "總件數", "庫存市值"], rows))
        md.append(f"\n- SKU 總數：{ds['sku_count']}　- 低於安全庫存品項：{ds['low_stock_count']}\n")

    # ── 缺貨警示 ──
    if rt in ("full", "low_stock"):
        r = W.execute("list_low_stock", {})
        warns = r.get("data", {}).get("warnings", []) if isinstance(r.get("data"), dict) else []
        _trace(steps, "read", f"讀缺貨警示 → {len(warns)} 項")
        md.append("## 二、缺貨警示（撐天 / 建議補）")
        rows = [[w.get("name", ""), w.get("warehouse_label", ""), w.get("qty", ""),
                 w.get("days_left", ""), w.get("suggest_qty", "")] for w in warns[:30]]
        md.append(_md_table(["商品", "倉", "現量", "撐天", "建議補"], rows) if rows else "（無）")

    # ── 到期警示 ──
    if rt in ("full", "expiring"):
        r = W.execute("list_expiring_items", {})
        items = r.get("data", {}).get("rows", []) if isinstance(r.get("data"), dict) else []
        _trace(steps, "read", f"讀到期批次 → {len(items)} 項")
        md.append("## 三、保存期限警示")
        rows = [[f"{it.get('level_emoji','')} {it.get('name','')}", it.get("warehouse_label", ""),
                 it.get("days_to_expire", ""), it.get("qty", "")] for it in items[:30]]
        md.append(_md_table(["商品", "倉", "剩餘天數", "數量"], rows) if rows else "（無）")

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
        _trace(steps, "reason", f"掃採購單比對應收/實收 → 發現 {len(discs)} 筆短收")
        md.append("## 四、採購對帳異常（PO 短收）")
        md.append(_md_table(["採購單", "日期", "倉", "商品", "應收", "實收", "短收"], discs)
                  if discs else "（無異常）")

    # ── 報告圖表（matplotlib PNG）：full 報告嵌一張庫存市值長條圖 ──
    chart_file = None
    if rt in ("full", "low_stock"):
        try:
            chart_file = _render_report_chart(rt, ts, reports_dir)
            if chart_file:
                md.insert(2, f"\n![chart](./{chart_file})\n")
                _trace(steps, "act", f"產生圖表 → reports/{chart_file}")
        except Exception as e:
            _trace(steps, "reason", f"圖表略過：{e}")

    md.append(f"\n---\n*本報告由倉管 Agent 自動產生 · {trace_id}*")
    content = "\n".join(md)

    fname = f"{snap}_{rt}_report_{ts[11:19].replace(':', '')}.md"
    fpath = reports_dir / fname
    fpath.write_text(content, encoding="utf-8")
    _trace(steps, "act", f"寫出報告 → reports/{fname}（{len(content)} 字）")

    # audit（actor=agent_auto，記錄自動產出）
    with open(dd / "audit" / f"{snap}_changes.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "trace_id": trace_id, "actor": actor,
                            "action": "generate_report", "report_type": rt,
                            "file": fname}, ensure_ascii=False) + "\n")

    return {"ok": True,
            "summary": f"已產出{('全倉體檢' if rt=='full' else rt)}報告：reports/{fname}"
                       + ("（含圖表）" if chart_file else ""),
            "view": "report_done",
            "data": {"report_type": rt, "file": fname, "path": str(fpath),
                     "chart": chart_file, "preview": content[:1200], "trace": steps}}


def _render_report_chart(rt: str, ts: str, reports_dir: Path) -> str | None:
    """產報告用 PNG 圖表（庫存市值 + 缺貨撐天）。回檔名。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    matplotlib.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "SimHei", "Arial Unicode MS"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    s = W.state()

    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    # 左：各倉庫存市值
    ds = W.dashboard_snapshot()
    labels = [w["label"] for w in ds["warehouse_summary"]]
    vals = [w["stock_value"] for w in ds["warehouse_summary"]]
    axes[0].bar(labels, vals, color=["#4a90d9", "#5cb85c", "#e8a33d"])
    axes[0].set_title("各倉庫存市值 (NT$)")
    axes[0].ticklabel_format(axis="y", style="plain")
    for i, v in enumerate(vals):
        axes[0].text(i, v, f"{v/10000:.0f}萬", ha="center", va="bottom", fontsize=9)

    # 右：缺貨 Top10 撐天
    r = W.execute("list_low_stock", {})
    warns = r.get("data", {}).get("warnings", []) if isinstance(r.get("data"), dict) else []
    warns = sorted([w for w in warns if w.get("days_left") is not None],
                   key=lambda w: w["days_left"])[:10]
    if warns:
        names = [w["name"][:6] for w in warns]
        days = [w["days_left"] for w in warns]
        colors = ["#d9534f" if d <= 7 else "#e8a33d" if d <= 14 else "#5bc0de" for d in days]
        axes[1].barh(names[::-1], days[::-1], color=colors[::-1])
        axes[1].set_title("最快斷貨 Top10 (撐天)")
        axes[1].set_xlabel("天")
    else:
        axes[1].text(0.5, 0.5, "無缺貨", ha="center")
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
    "transactions": "交易紀錄（按日切檔）",
    "orders": "採購單/銷售單",
    "master": "主檔（商品/供應商/設定/庫存）",
    "audit": "異動留底",
    "reports": "已產生的報告",
    "scripts": "可執行腳本白名單",
}


def list_files(area: str = "") -> dict:
    """列出 warehouse_data/ 下某區的檔案（Agent 動態看有什麼可讀）。"""
    steps: list[dict] = []
    dd = _data_dir()

    # 解析 area（keyword fuzzy，預設列所有區的概覽）
    target = None
    if area:
        a = area.replace(" ", "").lower()
        for k, label in _LISTABLE.items():
            if k in a or any(w in area for w in label.split("（")[0]):
                target = k
                break

    if target is None:
        # 沒指定 → 回各區概覽（檔數）
        _trace(steps, "glob", "掃 warehouse_data/ → 列出可讀區域")
        rows = []
        for k, label in _LISTABLE.items():
            d = dd / k
            if d.exists():
                n = sum(1 for _ in d.rglob("*") if _.is_file())
                rows.append({"area": k, "label": label, "file_count": n})
        return {"ok": True, "summary": f"warehouse_data/ 共 {len(rows)} 個可讀區域。",
                "view": "file_list", "data": {"area": None, "rows": rows, "trace": steps}}

    # 指定區 → 列檔（路徑穿越防護：只允許 _LISTABLE 內的區）
    base = (dd / target).resolve()
    if not str(base).startswith(str(dd.resolve())):
        return W._err("不允許存取沙盒外的路徑")
    _trace(steps, "glob", f"列 {target}/ 下的檔案")
    files = sorted(p for p in base.rglob("*") if p.is_file())
    MAX = 60
    rows = [{"name": str(p.relative_to(base)), "size": p.stat().st_size} for p in files[:MAX]]
    return {"ok": True,
            "summary": f"{target}/ 下有 {len(files)} 個檔" + (f"（顯示前 {MAX}）" if len(files) > MAX else ""),
            "view": "file_list",
            "data": {"area": target, "label": _LISTABLE[target], "rows": rows,
                     "total": len(files), "trace": steps}}


# ════════════════════════════════════════════════════════════
# ⑥ set_alert — 第四金剛：邊緣警示規則設定（半固定 enum）
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

    _cond_labels = {"below_safety": "低於安全庫存", "out_of_stock": "缺貨/斷貨",
                    "expiring": "快到期",
                    "below_threshold": f"低於 {threshold} 個" if threshold else "低於指定數量"}
    cond_label = _cond_labels.get(cond, cond)
    scope_txt = "全部商品" if not scope else "、".join(scope_names[:3])
    _trace(steps, "reason", f"準備建立警示規則 {rule_id}：{scope_txt} → {cond_label}")

    # HITL：先回傳草稿讓使用者確認，commit_alert_set() 才真正寫入
    summary = f"確認後將設定警示：當【{scope_txt}】發生「{cond_label}」時主動通知"
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
    src = "shortfall" if any(w in source for w in ("短收", "對不上", "shortfall", "rca")) else "low_stock"

    lines = []
    if src == "low_stock":
        r = W.execute("list_low_stock", {})
        warns = r.get("data", {}).get("warnings", []) if isinstance(r.get("data"), dict) else []
        _trace(steps, "read", f"讀缺貨清單 → {len(warns)} 項")
        # 取建議補貨量 > 0 的，按最急（撐天少）排
        cand = [w for w in warns if w.get("suggest_qty", 0) > 0]
        cand.sort(key=lambda w: w.get("days_left", 999))
        for w in cand[:20]:
            lines.append({"sku_id": w["sku_id"], "name": w["name"],
                          "warehouse": w["warehouse"], "order_qty": w["suggest_qty"],
                          "reason": f"撐 {w.get('days_left')} 天、建議補 {w['suggest_qty']}"})
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
                                  "order_qty": gap, "reason": f"{po['po_id']} 短收 {gap} 件補單"})
        _trace(steps, "read", f"掃採購單短收 → {len(lines)} 項待補")

    if not lines:
        return {"ok": True, "summary": "目前沒有需要補貨的品項，不需產採購單。",
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
    _trace(steps, "reason", f"組採購草稿：{len(lines)} 項、總額 NT$ {total:,}")

    return {"ok": True,
            "summary": f"已根據{'缺貨清單' if src=='low_stock' else '短收紀錄'}產出採購單草稿："
                       f"{len(lines)} 項、預估 NT$ {total:,}。請確認後送出。",
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
    return {"ok": True, "summary": f"採購單草稿 {po_id} 已建立（{len(doc['lines'])} 項、NT$ {doc['total']:,}），存到 PO_draft/。",
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
    scope_txt = "全部商品" if not scope_names else "、".join(scope_names[:3])
    return {"ok": True,
            "summary": f"警示規則 {pending['rule_id']} 已啟用：當【{scope_txt}】發生「{cond_label}」時主動通知。",
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

def _parse_schedule_intent(text: str) -> dict:
    """從自然語言解析排程意圖，回傳 {script_id, freq, time_str}。"""
    import re as _re
    script_id = next((v for k, v in _SCHEDULE_SCRIPT_MAP.items() if k in text), None)
    freq = next((v for k, v in _SCHEDULE_FREQ_MAP.items() if k in text), "daily")
    # 解析時間（幾點）
    m = _re.search(r'(\d{1,2})\s*[點:](\d{0,2})', text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2)) if m.group(2) else 0
        time_str = f"{h:02d}:{mi:02d}"
    else:
        time_str = next((v for k, v in _SCHEDULE_TIME_MAP.items() if k in text), "09:00")
    return {"script_id": script_id, "freq": freq, "time_str": time_str}

def set_schedule(script_name: str = "", freq: str = "daily", time_str: str = "09:00",
                 raw_text: str = "") -> dict:
    """設定定時排程：讓 Agent 在指定時間自動執行腳本。"""
    # 若有 raw_text 就從自然語言解析
    if raw_text and not script_name:
        parsed = _parse_schedule_intent(raw_text)
        script_name = parsed["script_id"] or script_name
        freq = parsed.get("freq", freq)
        time_str = parsed.get("time_str", time_str)

    sc = _match_script(script_name or "")
    if not sc:
        labels = "、".join(s["label"] for s in _load_manifest().get("scripts", []))
        return W._err(f"找不到腳本「{script_name}」，可用：{labels}")

    dd = _data_dir()
    jobs_path = dd / "schedule_jobs.json"
    jobs = []
    if jobs_path.exists():
        jobs = json.loads(jobs_path.read_text("utf-8")).get("jobs", [])

    # 防止重複
    existing = next((j for j in jobs if j["script_id"] == sc["id"] and j["freq"] == freq), None)
    if existing:
        return W._err(f"已有相同排程：{sc['label']} {freq} {existing['time_str']}（ID: {existing['id']}）")

    _freq_labels = {"daily": "每天", "weekly": "每週", "monthly": "每月"}
    freq_label = _freq_labels.get(freq, freq)
    job_id = f"SCH{len(jobs)+1:03d}"

    summary = f"確認後將設定排程：{freq_label} {time_str} 自動執行【{sc['label']}】"
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
            "summary": f"排程已建立：{pending['freq_label']} {pending['time_str']} 自動執行【{pending['script_label']}】",
            "view": "schedule_done", "data": {"job": new_job}}


def list_schedules() -> dict:
    """列出所有定時排程。"""
    dd = _data_dir()
    jobs_path = dd / "schedule_jobs.json"
    if not jobs_path.exists():
        return {"ok": True, "summary": "目前沒有定時排程。", "view": "schedule_list",
                "data": {"jobs": []}}
    jobs = json.loads(jobs_path.read_text("utf-8")).get("jobs", [])
    active = [j for j in jobs if j.get("enabled", True)]
    summary = f"目前有 {len(active)} 個排程啟用中。"
    return {"ok": True, "summary": summary, "view": "schedule_list", "data": {"jobs": active}}


def delete_schedule(job_id: str = "") -> dict:
    """刪除指定排程。"""
    if not job_id:
        return W._err("請指定排程 ID（例如 SCH001）")
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
    return {"ok": True, "summary": f"排程 {job_id} 已刪除。",
            "view": "schedule_deleted", "data": {"job_id": job_id}}


def list_alerts() -> dict:
    """列出目前所有已啟用的警示規則。"""
    dd = _data_dir()
    rules_path = dd / "alert_rules.json"
    if not rules_path.exists():
        return {"ok": True, "summary": "目前沒有任何警示規則。", "view": "alert_list",
                "data": {"rules": []}}
    rules = json.load(open(rules_path, encoding="utf-8")).get("rules", [])
    active = [r for r in rules if r.get("enabled", True)]
    _cond_labels = {"below_safety": "低於安全庫存", "out_of_stock": "缺貨/斷貨",
                    "expiring": "快到期", "below_threshold": "低於指定數量"}
    for r in active:
        r["condition_label"] = _cond_labels.get(r["condition"], r["condition"])
        r["scope_txt"] = "全部商品" if not r.get("scope_names") else "、".join(r["scope_names"][:3])
    summary = f"目前有 {len(active)} 條警示規則啟用中。"
    return {"ok": True, "summary": summary, "view": "alert_list", "data": {"rules": active}}


def delete_alert(rule_id: str = "") -> dict:
    """刪除指定 ID 的警示規則。"""
    if not rule_id:
        return W._err("請指定要刪除的規則 ID（例如 AL001）")
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
    return {"ok": True, "summary": f"警示規則 {rule_id} 已刪除。", "view": "alert_deleted",
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
    _trace(steps, "glob", f"切兩期：本期 {this_start}~{today} / 上期 {last_start}~{this_start}")

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
    _trace(steps, "reason", f"算 {len(rows)} 個 SKU 的變化，取變化最大前 15")

    top = rows[:15]
    up = [r for r in top if r["delta"] > 0][:3]
    down = [r for r in top if r["delta"] < 0][:3]
    parts = []
    if up:
        parts.append("成長最多：" + "、".join(f"{r['name']}(+{r['delta']})" for r in up))
    if down:
        parts.append("衰退最多：" + "、".join(f"{r['name']}({r['delta']})" for r in down))
    summary = "近兩個月出庫變化 — " + "；".join(parts) if parts else "兩期出庫無明顯變化。"
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
        "summary": "好的！第一步：商品叫什麼名字？（任何名稱都可以，例如『環保吸管』）",
        "view": "item_create_step1",
        "data": {"step": 1, "total_steps": 4, "prompt": "請輸入商品名稱"},
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
                return {"ok": True, "summary": f"⚠️ 商品「{_name}」已存在，請改用其他名稱。",
                        "view": "item_create_step1", "data": {"step": 1, "prompt": "請輸入不同的商品名稱"}}
            new_sku = _next_sku(_found_cat)
            pending = {
                "name": _name, "category": _found_cat,
                "price": int(_price_m.group(1)) if _price_m else 0,
                "safety": int(_safety_m.group(1)) if _safety_m else 0,
                "stock_north": int(_north_m.group(1)) if _north_m else 0,
                "stock_central": int(_central_m.group(1)) if _central_m else 0,
                "stock_south": int(_south_m.group(1)) if _south_m else 0,
                "sku": new_sku,
            }
            return {"ok": True, "summary": "已解析商品資訊，請確認", "view": "item_confirm",
                    "data": {"pending": True, "item": pending}}

    # 分步模式
    if step == 1:
        # 防呆：檢查是否已有同名商品
        existing = [it for it in W.state().items if it["name"] == name]
        if existing:
            return {"ok": True, "summary": f"⚠️ 商品「{name}」已存在（SKU: {existing[0]['sku_id']}），請改用其他名稱。",
                    "view": "item_create_step1",
                    "data": {"step": 1, "prompt": "請輸入不同的商品名稱"}}
        return {"ok": True, "summary": f"已記錄商品名稱：「{name}」\n第二步：屬於哪一類？（輸入「取消」可退出）",
                "view": "item_create_step2",
                "data": {"step": 2, "name": name, "prompt": "請選擇類別（或輸入「取消」退出）"}}
    elif step == 2:
        return {"ok": True,
                "summary": f"已記錄：「{name}」→ {category}\n第三步：單價多少？安全庫存幾件？\n例如：150 100（輸入「取消」可退出）",
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
            return W._err(f"價格或安全庫存格式錯誤：{price} / {safety}")
        return {"ok": True,
                "summary": f"已記錄：單價 {price_val} 元，安全庫存 {safety_val} 件\n第四步（可選）：設定初始庫存？\n直接輸入三個數字（北 中 南），例如：50 30 20\n或輸入『跳過』全部設為 0",
                "view": "item_create_step4",
                "data": {"step": 4, "name": name, "category": category,
                         "price": price_val, "safety": safety_val,
                         "prompt": "格式：北 中 南（例如 50 30 20）或輸入跳過"}}
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
            "price": int(price) if price else 0,
            "safety": int(safety) if safety else 0,
            "stock_north": sn, "stock_central": sc, "stock_south": ss,
            "sku": new_sku,
        }
        stock_summary = f"北{sn} 中{sc} 南{ss}" if (sn+sc+ss) > 0 else "全部為 0"
        return {"ok": True,
                "summary": f"📦 準備新增「{name}」\n類別：{category} | 單價：{pending['price']}元 | 安全庫存：{pending['safety']}件\n初始庫存：{stock_summary}",
                "view": "item_confirm",
                "data": {"pending": True, "item": pending}}

    return W._err(f"未知的步驟：{step}")


def commit_create_item(pending: dict, actor: str = "user_confirmed",
                       trace_id: str | None = None) -> dict:
    """HITL 確認後寫入 items.csv + config.json + stock.csv"""
    import csv, shutil
    dd = _data_dir()
    ts = __import__('datetime').datetime.now().isoformat(timespec="seconds")
    trace_id = trace_id or f"item-{ts}"
    item = pending["item"] if "item" in pending else pending

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

    # 3. 寫入 stock.csv（初始庫存）
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
    # 新增到 items 清單（如果還沒有）
    new_sku = item["sku"]
    if not any(it["sku_id"] == new_sku for it in s.items):
        s.items.append({
            "sku_id":       new_sku,
            "name":         item["name"],
            "category":     item["category"],
            "unit_price":   item["price"],
            "safety_stock": item["safety"],
        })
        s._items_by_sku[new_sku] = s.items[-1]
    # 新增各倉庫存
    for wh_key in ("north", "central", "south"):
        qty = item.get(f"stock_{wh_key}", 0)
        s.stock.setdefault(wh_key, {})[new_sku] = qty

    return {"ok": True, "summary": f"✅ 已新增商品「{item['name']}」（SKU: {item['sku']}）",
            "view": "item_done", "data": {"item": item, "trace_id": trace_id}}


# 原始 60 項商品的 SKU 白名單（不可刪除）
_PROTECTED_SKUS = {
    f"{p}{i:02d}"
    for p in ("e", "a", "f", "d", "c", "s")
    for i in range(1, 11)
}


def delete_item_start(keyword: str = "") -> dict:
    """觸發刪除流程：找商品 → HITL 確認 → 軟刪除"""
    if not keyword:
        return W._err("請指定要刪除的商品名稱或 SKU")
    matches = W.match_items(keyword)
    if not matches:
        return W._err(f"找不到「{keyword}」相關商品")
    items = [m["item"] for m in matches[:5]]
    # 過濾受保護商品
    deletable = [it for it in items if it["sku_id"] not in _PROTECTED_SKUS]
    protected = [it for it in items if it["sku_id"] in _PROTECTED_SKUS]
    if not deletable:
        return {"ok": True, "summary": f"「{keyword}」是系統預設商品，無法刪除。",
                "view": "item_delete_denied",
                "data": {"protected": [it["name"] for it in protected]}}
    rows = [{"sku": it["sku_id"], "name": it["name"], "protected": False} for it in deletable]
    if protected:
        rows += [{"sku": it["sku_id"], "name": it["name"] + " 🔒", "protected": True} for it in protected]
    summary = f"找到 {len(items)} 筆相關商品（{len(deletable)} 筆可刪除）：\n"
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
        return W._err("沒有可刪除的商品")

    skus_to_delete = {it["sku_id"] for it in deletable}
    deleted_names = ", ".join(it["name"] for it in deletable)

    # 1. 從 items.csv 移除
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

    # 4. 重生 seed
    from pathlib import Path as _P
    seed_path = _P(__file__).parent / "seed_data.json"
    W.init(seed_path)

    return {"ok": True, "summary": f"✅ 已刪除：{deleted_names}（共 {len(deletable)} 項）",
            "view": "item_done", "data": {"deleted": deleted_names, "trace_id": trace_id}}
