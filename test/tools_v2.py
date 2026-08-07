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
    "safety_stock":      ["安全庫存", "安全存量", "安全水位", "警戒值", "警戒水位", "安全量",
                          "庫存底線", "存量底線", "safety stock", "safety_stock"],
    "reorder_lead_days": ["前置天數", "補貨前置", "前置時間", "補貨天數", "lead time", "lead_days", "前置"],
    "safety_buffer_ratio": ["安全水位倍數", "安全倍數", "buffer", "緩衝倍數"],
    "restock_target_days": ["補貨目標天數", "補到撐", "target days", "撐幾天"],
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
            f"找不到商品「{_uk}」，設定沒有改動。請確認商品名稱，"
            "例如「瑜珈墊安全庫存加20」。"),
            "data": {"question": f"找不到商品「{_uk}」，請確認商品名稱",
                     "options": [], "hint": ""}}
    canon = _resolve_key(key)
    if not canon:
        # key 不是合法設定項（LLM 把「空間/容量」這種非設定問題誤投 manage_config）
        # → 不暴露內部設定項名，改友善引導（RPI5 v21：「倉庫空間夠不夠」露「哪個設定項:空間」）
        return {"ok": True, "view": "guide", "summary": (
            "我能調的是庫存相關設定（安全庫存、補貨前置天數）。\n"
            "試試這樣說：「北倉安全庫存改成50」「補貨前置天數設成7天」，\n"
            "或問「安全庫存現在設多少」查目前設定。"
        ), "data": {}}

    # ── read ──
    if action == "read":
        _trace(steps, "read", f"讀取設定 master/config.json → {canon}")
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
                _wh_lbl = {"north": "北區倉", "central": "中區倉", "south": "南區倉"}
                parts = []
                for r in rows[:3]:
                    vals = set(r["by_warehouse"].values())
                    if len(vals) == 1:
                        parts.append(f"「{r['name']}」目前設 {vals.pop()}（三倉同值）")
                    else:
                        seg = "、".join(f"{_wh_lbl[w]} {q}" for w, q in r["by_warehouse"].items())
                        parts.append(f"「{r['name']}」{seg}（基準 {r['base']}）")
                summary = "目前安全庫存：" + "；".join(parts) + "。"
            else:
                # r59：指定倉別時摘要要講出來（「只看南倉的」曾回不含倉別的泛話）
                _sc_lbl = {"north": "北區倉", "central": "中區倉",
                           "south": "南區倉"}.get(warehouse, "")
                summary = (f"目前{_sc_lbl}安全庫存設定（{len(rows)} 項，含分倉覆寫值）如下表。"
                           if _sc_lbl else
                           f"目前安全庫存設定（{len(rows)} 項）：基準值寫在 config，可分倉覆寫。")
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
            # 沒給有效數值（含 LLM 佔位符「+N」）→ 不報 error，改 clarify 友善追問
            # （RPI5 conv100-r4：「安全水位要怎麼設定」諮詢句被判 set 卻無值）
            _lbl = {"reorder_lead_days": "補貨前置天數", "safety_buffer_ratio": "安全水位倍數",
                    "safety_stock": "安全庫存"}.get(canon, "安全庫存")
            return {"ok": True, "view": "clarify", "summary": (
                f"要把「{_lbl}」設成多少呢？例如「{_lbl}改成50」或「加30」。"
            ), "data": {"canon": canon, "label": _lbl, "pending_config": True}}
        # 極端值防呆（r17：「設成十萬」中文數字修好後能正確解析 100000，
        # 但這數量級對 demo 資料絕非本意，會開出影響 183 項的確認卡）→ 追問
        if abs(num) > 9999:
            _lbl2 = {"reorder_lead_days": "補貨前置天數", "safety_buffer_ratio": "安全水位倍數",
                     "safety_stock": "安全庫存"}.get(canon, "安全庫存")
            return {"ok": True, "view": "clarify", "summary": (
                f"「{_lbl2}」設成 {num:,} 不太尋常（一般在 0～9999 之間），"
                "請確認數值後再說一次。"),
                "data": {"question": f"「{_lbl2}」要設成 {num:,}？請確認數值",
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
    _trace(steps, "read", f"比對白名單 manifest.json → 「{script_name}」")
    if not sc:
        # 2026-08-06（EN 同款）：script_name 可能是 **LLM 幻覺出的內部代號**
        #   （EN 實測 'run_stock_check'），直接回顯＝內部識別字外洩，訪客只會
        #   困惑「我沒打過這個字」⇒ 不回顯，只問要跑哪一個。
        #   options 也改成從 manifest 生（原本寫死三個中文字串，manifest 改了
        #   就不同步）。
        _scripts = _load_manifest().get("scripts", [])
        avail = "、".join(s["label"] for s in _scripts)
        return {"ok": True, "view": "clarify",
                "summary": f"想跑哪一個？可執行的有：{avail}",
                "data": {"question": "想跑哪一個腳本？",
                         "options": [s["label"] for s in _scripts], "hint": ""}}

    # 安全護欄：只回「待確認」，不直接 subprocess（執行交給 server confirm 後）
    _trace(steps, "confirm", f"命中白名單腳本：{sc['label']}（逾時上限 {sc['timeout_s']}s）")
    # 訪客講的期間（匯出用）→ 帶到 confirm 那步。
    # `_period_text` 是 server 塞的原句（script_name 只有「匯出」兩字時期間
    # 抽不到）；沒有就退回用 script_name 自己解析。
    _days = _parse_days(_period_text or script_name)
    # ⚠️ 卡片文案要**跟著訪客講的期間走**：manifest 的 description 是寫死的
    #   「合併最近 7 天…」，訪客講「昨天」時卡片卻說 7 天（2026-08-03 實測抓到）
    #   ——後端其實有正確帶 days=1，只有文案騙人，訪客會以為選單沒作用。
    _desc = sc.get("description", sc.get("desc", ""))
    if sc["id"] == "export_movements" and _days:
        _p = "昨天" if _days == 1 else f"最近 {_days} 天"
        _desc = f"合併{_p}進出記錄，產出 CSV"
    summary = f"準備執行白名單腳本【{sc['label']}】：{_desc}。請確認後執行。"
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
        return W._err("腳本不存在")
    spec = _SCRIPT_CMD.get(script_id)
    if not spec:
        return W._err(f"腳本 {script_id} 未綁定指令")
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
        return W._err(f"找不到腳本檔：{script_path.name}")

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
    """產生倉庫報告 —— **一律導向 `stock_audit`，全系統只有這一份報告**。

    ⚠️ 2026-08-03（user 定調）：原本這裡自己產一份 Markdown，跟盤點的
    HTML+CSV **是兩份不同的報告** ⇒ 訪客講「產生報表」和「跑盤點」
    會拿到不一樣的東西，而盤點那份才是最完整的（KPI/需注意/熱銷/
    到期/完整庫存，含撐天與市值）。
    ⇒ 這支改成薄包裝，直接跑同一支腳本。報告類型只留一份、不再分歧。
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

# 2026-08-06 user 定調：排程/警示都可以一直新增（「改時間」的正解就是
#   新增+刪舊），**但要有上限**——展場一天下來訪客可能各堆出幾十筆，
#   排程每筆到點都真的跑腳本（推論資源被排隊佔用）、警示每筆都進背景
#   掃描，清單也會長到看不完。10 個對 demo 綽綽有餘（baseline 各 1 個）。
_MAX_SCHEDULE_JOBS  = 10
_MAX_ALERT_RULES    = 10


def _next_seq_id(existing: list, prefix: str) -> str:
    """產下一個不撞號的流水 ID（SCH001 / AL001）。

    ⚠️ 原本是 `f"{prefix}{len(rules)+1:03d}"`——**刪除後必撞號**：
      3 筆刪掉中間那筆 → len=2 → 下一個又生 003，跟現存的 AL003 同 ID
      ⇒ 刪除時 `rule_id` 比對會一次砍掉兩筆、或砍錯那筆。
      改成「取現存最大號 +1」，刪完再新增也不會回頭撞既有 ID。
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

    # 上限（同排程，擋在確認卡出現前，訪客不會白按一次授權）
    if len(rules) >= _MAX_ALERT_RULES:
        # ⚠️ 存檔的規則只有 id/condition/scope/scope_names（實機驗過），
        #   沒有 scope_txt/cond_label ⇒ 標籤要在這裡自己組，不能直接 .get。
        _cl = {"below_safety": "低於安全庫存", "out_of_stock": "缺貨/斷貨",
               "expiring": "快到期", "below_threshold": "低於指定數量"}
        _cur_al = "、".join(
            f"{'、'.join(r.get('scope_names') or []) or '全部商品'}"
            f"→{_cl.get(r.get('condition', ''), r.get('condition', ''))}"
            for r in rules[:5])
        return {"ok": True, "view": "clarify",
                "summary": (f"警示規則已達上限（{_MAX_ALERT_RULES} 條），無法再新增。\n"
                            f"目前：{_cur_al}\n"
                            f"要騰位子的話，可以說「警示清單」再指定刪掉不要的。"),
                "data": {"question": f"警示已達上限 {_MAX_ALERT_RULES} 條，"
                                     f"要先刪掉舊的嗎？",
                         "options": ["看警示清單"],
                         "actions": ["我的警示清單"],
                         "hint": ""}}
    rule_id = _next_seq_id(rules, "AL")

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
    # ── HTML 版（訪客點連結直接看）──
    #   JSON 是給機器讀的，訪客看不懂 ⇒ 產一份可讀的採購單。
    #   放 audit/ 是因為 `/audit/*.html` 端點已經支援瀏覽器直接開。
    _wh = {"north": "北區倉", "central": "中區倉", "south": "南區倉"}
    _po_rows = "".join(
        f'<tr><td>{ln.get("sku_id","")}</td><td>{ln.get("name","")}</td>'
        f'<td>{_wh.get(ln.get("warehouse",""), ln.get("warehouse",""))}</td>'
        f'<td class="n b">{ln.get("order_qty",0)}</td>'
        f'<td class="r">{ln.get("reason","")}</td></tr>'
        for ln in doc["lines"])
    _po_html = dd / "audit" / f"{po_id}.html"
    try:
        _po_html.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<title>採購單 {po_id}</title><style>
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
<h1>採購單草稿</h1>
<div class="sub">{po_id} &middot; {doc['date']} &middot; <span class="badge">草稿</span>
 &middot; 建立者 {actor}</div>
<div class="kpis">
  <div class="kpi"><div class="k">品項數</div><div class="v">{len(doc['lines'])}</div></div>
  <div class="kpi"><div class="k">總數量</div>
    <div class="v">{sum(int(l.get('order_qty', 0)) for l in doc['lines']):,}</div></div>
  <div class="kpi"><div class="k">預估金額</div>
    <div class="v">NT$ {doc.get('total', 0):,}</div></div>
</div>
<table><thead><tr><th>SKU</th><th>商品</th><th>倉別</th>
<th class="n">訂購量</th><th>原因</th></tr></thead>
<tbody>{_po_rows}</tbody></table>
<div class="sub" style="margin-top:12px">這是草稿，尚未送出給供應商。存於 orders/PO_draft/{po_id}.json</div>
</body></html>""", encoding="utf-8")
    except Exception:
        _po_html = None

    snap = W.state().snapshot_date or ts[:10]
    with open(dd / "audit" / f"{snap}_changes.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "trace_id": trace_id, "actor": actor,
                            "action": "generate_po", "po_id": po_id,
                            "lines": len(doc["lines"]), "total": doc["total"]},
                           ensure_ascii=False) + "\n")
    return {"ok": True, "summary": f"採購單草稿 {po_id} 已建立（{len(doc['lines'])} 項、NT$ {doc['total']:,}），存到 PO_draft/。",
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
    "月報":     "generate_report",
    "週報":     "generate_report",
    # ⚠️ 「日報」漏收 → 「排程每天早上九點出日報」對到空字串，訪客看到
    #   `[error] 找不到腳本「」`（2026-08-03 端到端實測；同句換月報就正常）。
    #   同一個「日報」概念散在 _sched_act_kws / C12 _report_words / intent_clf
    #   詞表，這張 alias 表是最後一個漏的 —— 補要四張一起補（坑 28 同型）。
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
    # 2026-08-06 user 實測：「10點半」曾建成 10:00（「半」被丟）——補
    #   半/一刻/三刻/中文分鐘（三十分/四十五分）。阿拉伯分鐘原本就支援
    #   （「下午2點45分」→14:45）。
    # 2026-08-06 排程百句抓到：「每天二十三點跑盤點」→ **15:00**、
    #   「每天二十五點」→ 17:00。原 regex 只認「十/十一/十二」，「二十三點」
    #   的前綴「二十」沒被吃掉 → 只match到後半「三點」→ 再被 1-6 智慧預設
    #   +12 = 15:00（錯得很隱蔽：訪客講 23 點，系統顯示成功、建在 15:00）。
    #   ⇒ 補中文 13-24（二十一~二十四）；超過 24 的視為無效不解析（沿用
    #     EN 版 'at 25pm' 的邊界教訓：無效時間不可被編造成合法值）。
    m = _re.search(r'((?:[一二兩]?十[一二三四五六七八九]?)|[0-9]{1,2}|'
                   r'[一兩二三四五六七八九])\s*[點:]\s*'
                   r'([0-9]{1,2}|半|一刻|三刻|[一二三四五]?十[一二三四五六七八九]?)?', text)
    if m:
        g = m.group(1)
        if g.isdigit():
            h = int(g)
        elif g in _CN_HOUR:
            h = _CN_HOUR[g]
        else:
            # 中文十位數（二十三 / 十五 / 二十）——_CN_HOUR 只到十二
            _mm = _re.fullmatch(r'([一二兩])?十([一二三四五六七八九])?', g)
            if _mm:
                _tens = 2 if _mm.group(1) in ("二", "兩") else 1
                h = _tens * 10 + (_CN_HOUR.get(_mm.group(2), 0) if _mm.group(2) else 0)
            else:
                h = 9
        if h > 23:
            m = None                       # 無效時刻 → 當作沒解析到（不編造）
    if m:
        g2 = m.group(2) or ""
        _CN_D = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                 "六": 6, "七": 7, "八": 8, "九": 9}
        if g2 == "半":
            mi = 30
        elif g2 == "一刻":
            mi = 15
        elif g2 == "三刻":
            mi = 45
        elif g2.isdigit():
            mi = int(g2)
        elif g2:
            _p = g2.split("十")
            mi = (_CN_D.get(_p[0], 1) if _p[0] else 1) * 10 \
                 + (_CN_D.get(_p[1], 0) if len(_p) > 1 and _p[1] else 0)
        else:
            mi = 0
        # 下午/晚上 + 12 小時制轉換
        if h < 12 and any(w in text for w in ("下午", "晚上", "傍晚", "晚間", "夜裡")):
            h += 12
        # 2026-08-06 user 實測：「每天一點自動執行盤點」建成 01:00——語意上
        #   「對」但訪客十有八九指下午。無時段詞且 1-6 點 → 當下午（+12）；
        #   要真凌晨講「凌晨/半夜」就不轉。
        elif (1 <= h <= 6
              and not any(w in text for w in ("凌晨", "半夜", "早上", "上午",
                                              "清晨", "早晨"))):
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
        return W._err(f"找不到腳本「{script_name}」，可用：{labels}")

    dd = _data_dir()
    jobs_path = dd / "schedule_jobs.json"
    jobs = []
    if jobs_path.exists():
        jobs = json.loads(jobs_path.read_text("utf-8")).get("jobs", [])

    # 2026-08-06 user 定調：排程可無限新增（「改時間」的正解就是新增+刪舊），
    #   但要有上限——展場一天下來訪客可能堆出幾十筆，每筆到點都真的跑腳本
    #   ⇒ 清單難看 + 推論資源被排隊佔用。
    #   擋在**確認卡出現前**：訪客不會白按一次「授權執行」才被拒（比 commit
    #   端才擋體驗好），並直接告訴他怎麼騰位子。
    if len(jobs) >= _MAX_SCHEDULE_JOBS:
        _cur = "、".join(f"{j.get('freq_label','')}{j.get('time_str','')}"
                         f"【{j.get('script_label','')}】" for j in jobs[:5])
        _q_lim = f"排程已達上限 {_MAX_SCHEDULE_JOBS} 個，要先刪掉舊的嗎？"
        return {"ok": True, "view": "clarify",
                "summary": (f"排程數量已達上限（{_MAX_SCHEDULE_JOBS} 個），無法再新增。\n"
                            f"目前：{_cur}\n"
                            f"要騰位子的話，可以說「排程清單」再指定刪掉不要的。"),
                "data": {"question": _q_lim,
                         "options": ["看排程清單"],
                         "actions": ["我有哪些排程"],
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
                "summary": f"已經有一個一樣的排程囉：{sc['label']} {freq} {existing['time_str']}（ID: {existing['id']}）{_alias_note}，不用重複設定。",
                "data": {"question": f"已經有排程「{sc['label']}」（{freq} {existing['time_str']}）在跑囉，需要改時間或取消再跟我說。",
                         "options": [], "hint": ""}}

    _freq_labels = {"daily": "每天", "weekly": "每週", "monthly": "每月"}
    freq_label = _freq_labels.get(freq, freq)
    job_id = _next_seq_id(jobs, "SCH")

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
    """刪除指定排程（HITL：先回確認卡，commit_delete_schedule() 才真正刪）。"""
    if not job_id:
        return W._err("請指定排程 ID（例如 SCH001）")
    dd = _data_dir()
    jobs_path = dd / "schedule_jobs.json"
    if not jobs_path.exists():
        return W._err("找不到排程檔")
    data = json.loads(jobs_path.read_text("utf-8"))
    jobs = data.get("jobs", [])
    job = next((j for j in jobs if j["id"] == job_id), None)
    if not job:
        return W._err(f"找不到排程 {job_id}")
    return {"ok": True,
            "summary": f"確認刪除排程 {job_id}【{job.get('script_label', '')}】？此動作無法復原。",
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
    """刪除指定 ID 的警示規則（HITL：先回確認卡，commit_delete_alert() 才真正刪）。"""
    if not rule_id:
        return W._err("請指定要刪除的規則 ID（例如 AL001）")
    dd = _data_dir()
    rules_path = dd / "alert_rules.json"
    if not rules_path.exists():
        return W._err("找不到警示規則檔")
    data = json.load(open(rules_path, encoding="utf-8"))
    rules = data.get("rules", [])
    rule = next((r for r in rules if r["id"] == rule_id), None)
    if not rule:
        return W._err(f"找不到規則 {rule_id}")
    _cond_labels = {"below_safety": "低於安全庫存", "out_of_stock": "缺貨/斷貨",
                    "expiring": "快到期", "below_threshold": "低於指定數量"}
    cond_label = _cond_labels.get(rule.get("condition"), rule.get("condition", ""))
    return {"ok": True,
            "summary": f"確認刪除警示規則 {rule_id}【{cond_label}】？此動作無法復原。",
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
# r22：新商品建檔時的預設值（安全庫存 + 三倉初始庫存都用這個）。
#   ⚠️ user 定調：不講數量時，安全庫存與三倉庫存**都給同一個預設值**，
#     這樣建好的商品在三個倉庫就不會是 0——展場能立刻查到庫存、
#     看得到三倉分布，也不會跳缺貨警示（剛好等於水位）。
#   50 是跟 live_sim.LiveConfig.default_safety 對齊的數字。
_DEFAULT_SAFETY = 50

# ⚠️ r22：既有 60 筆已全量轉成 **ELE-0001** 三碼格式，新建商品也要用
#   同一套前綴，否則會生出 `e01` 這種舊格式跟主檔不一致（英文版 CDP
#   畫面驗證抓到過）。⇒ 一律取 categories.py 的三碼 prefix。
_CATEGORY_PREFIX = {}
try:
    from categories import CATEGORY_PREFIX as _CAT19_PFX3
    _CATEGORY_PREFIX.update(_CAT19_PFX3)
except Exception:
    _CATEGORY_PREFIX.update({
        "electronics": "e", "appliance_kitchen": "a", "food_beverage": "f",
        "daily_goods": "d", "apparel": "c", "sports": "s",
    })

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


def _next_sku(category: str) -> str:
    """自動產生下一個 SKU 流水號"""
    prefix = _CATEGORY_PREFIX.get(category, "OTH")
    existing = [it["sku_id"] for it in W.state().items
                if it["sku_id"].startswith(prefix)]
    nums = []
    for sid in existing:
        # 三碼格式 `ELE-0001` 數字在連字號後；舊格式 `e01` 在前綴後。
        _tail = sid.split("-", 1)[1] if "-" in sid else sid[len(prefix):]
        try:
            nums.append(int(_tail))
        except ValueError:
            pass
    # ⚠️ 料號**絕不可重用**（業界硬規則）：原本 max(現有)+1，若 ELE-0010
    #   被刪除，下一個新品會再拿到它 → 跟歷史進出紀錄撞號。
    #   ⇒ 記錄「用過的最大號」高水位，只增不減。
    _used_max = _sku_seq_peek(prefix)
    next_num = max(max(nums) + 1 if nums else 1, _used_max + 1)
    _sku_seq_bump(prefix, next_num)
    if len(prefix) >= 3:
        return f"{prefix}-{next_num:04d}"
    return f"{prefix}{next_num:02d}" if next_num <= 99 else f"{prefix}{next_num:04d}"


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
        # ── r22 上架主線（2026-08-07）：**口語價格/安全庫存/倉量** ────────
        #   原本只認「800元」「安全30」「北50」這種標準寫法，真人講的
        #   「賣800」「一個350」「安全庫存抓20」「三倉各50」全抽不到 →
        #   出卡時價格/安全庫存都是 0（假成功，見下方守門註解）。
        _price_m = (_re.search(r'(\d+)\s*元', raw_text)
                    or _re.search(r'(?:賣|售價|定價|價格|單價|一個|一件|一支|一台|'
                                  r'一組|每個|每件)\s*(?:是|賣)?\s*(\d+)', raw_text)
                    or _re.search(r'(\d+)\s*(?:塊|元整)', raw_text))
        _safety_m = (_re.search(r'安全\s*(\d+)', raw_text)
                     or _re.search(r'安全(?:庫存|水位|存量)\s*(?:抓|設|放|留|大概|約)?'
                                   r'\s*(\d+)', raw_text)
                     or _re.search(r'(?:警戒|水位)\s*(?:設|抓)?\s*(\d+)', raw_text))
        # ⚠️ 匹配範圍要**含前後口語詞**——group(0) 會整段移除，範圍不夠
        #   就留殘字（實測「先進北倉100個」只抽到「北倉100」，剩「先進 個」
        #   黏在商品名上）。前綴 先/進/放、後綴 個/件/台 都納入。
        _WH_PRE = r'(?:先|再|另外|然後)?\s*(?:進|放|收|擺|給|到)?\s*'
        _WH_SUF = r'\s*(?:個|件|台|支|組|箱|包)?'
        _north_m = (_re.search(_WH_PRE + r'北(?:區)?倉?\s*(?:進|放|收|擺)?\s*(\d+)' + _WH_SUF,
                               raw_text)
                    or _re.search(r'北\S*?\s*(\d+)' + _WH_SUF, raw_text))
        _south_m = (_re.search(_WH_PRE + r'南(?:區)?倉?\s*(?:進|放|收|擺)?\s*(\d+)' + _WH_SUF,
                               raw_text)
                    or _re.search(r'南\S*?\s*(\d+)' + _WH_SUF, raw_text))
        _central_m = (_re.search(_WH_PRE + r'中(?:區)?倉?\s*(?:進|放|收|擺)?\s*(\d+)' + _WH_SUF,
                                 raw_text)
                      or _re.search(r'中\S*?\s*(\d+)' + _WH_SUF, raw_text))
        # 「三倉各50」「每倉各進30」→ 三倉同量
        _each_m = _re.search(r'(?:三倉|每倉|各倉|三個倉)\s*(?:各|都)?\s*(?:進|放|收)?\s*(\d+)'
                             r'\s*(?:個|件|台|支|組)?', raw_text)
        if _each_m and not (_north_m or _south_m or _central_m):
            _north_m = _south_m = _central_m = _each_m
        # 裸價格保底：「藍牙喇叭 電子 1500 三倉各50」的 1500 前面沒有價格詞。
        #   ⚠️ 必須**排除已被其他欄位吃掉的數字**，否則會把倉量當價格。
        #   判準：句中剩下的、不屬於任何已抽欄位的孤立數字，且只有一個時才收
        #   （兩個以上代表語意不明確，交給分步流程問，不猜）。
        if not _price_m:
            _used = set()
            for _mm in (_safety_m, _each_m, _north_m, _central_m, _south_m):
                if _mm:
                    _used.add(_mm.group(0))
            _rest = raw_text
            for _u in _used:
                _rest = _rest.replace(_u, " ")
            _free = _re.findall(r'(?<![\d])(\d+)(?![\d])', _rest)
            if len(_free) == 1:
                # ⚠️ 要對**原句**再 match 一次——上面是對 _rest（挖空後的字串）
                #   找的，直接用它的 group(0) 會對不上原句、移除時失敗。
                _price_m = _re.search(r'(?<![\d])(' + _re.escape(_free[0]) + r')(?![\d])',
                                      raw_text)
        # ── r22：**抽到什麼就移除什麼**（結構性做法）─────────────────────
        #   原本靠一長串 pattern 猜著剝，順序互相干擾：實測舊 pattern
        #   `北\S*\d+` 搶先把「北倉100」剝掉、只留「先進個」黏在商品名上，
        #   我後加的 pattern 就沒東西可匹配（跟停用詞表膨脹是同一種病）。
        #   ⇒ 上面每個欄位都已經 match 到**完整字串**，直接拿 group(0) 移除，
        #     抽到什麼移除什麼，零順序衝突、也不會誤剝沒抽中的字。
        _name = raw_text
        for _mm in (_price_m, _safety_m, _each_m, _north_m, _central_m, _south_m):
            if _mm:
                _name = _name.replace(_mm.group(0), " ", 1)
        # ⚠️ 欄位數值已由上面 group(0) 移除，這裡只處理**類別詞與指令詞**。
        #   舊的 `北\S*\d+` / `\d+元` / `安全\d+` 已移除——它們會搶在
        #   group(0) 之前把字吃掉造成殘字（見上方註解）。
        # ⚠️ 類別詞用 `\S*` 貪吃會**剝掉真商品名**（r22 誤傷檢查抓到 4 個）：
        #     「電解質運動飲」→「電解質」、「運動壓縮臂套」→ 空字串、
        #     「彈性運動內衣」→「彈性」。成因是 `運動\S*` 把後面整串吃掉。
        #   ⇒ 只剝**獨立出現**的類別詞（前後是空白或句首句尾），
        #     商品名內含類別字的不動。
        #   ⚠️ 分隔符要含**中文標點**——「藍牙鍵盤，電子類，賣800」的
        #     類別詞被逗號黏住，只認空白會剝不掉（實測殘留「，電子類」）。
        _CAT_TOK = r'(?:電子|家電|食品|飲料|日用|服飾|運動)'
        _SEP = r'[\s，,、。.：:；;]'
        for pat in [r'(?:^|' + _SEP + r')' + _CAT_TOK + r'(?:類|品|用品|的)?'
                    r'(?=' + _SEP + r'|$)',
                     r'新增商品\s*',
                     # r22：**指令詞/口語前綴**沒剝掉 → 混進商品名（實測建出
                     #   「幫我新增一個藍牙鍵盤，」「我要加一款新商品叫無線
                     #   充電盤 一個1200 安全庫存抓20」這種垃圾名稱）。
                     #   ⚠️ 長詞排前面（「新增一個」先於「新增」），否則剝完
                     #     會留下殘字。
                     r'(?:幫我|請|麻煩|我要|我想|要)?\s*(?:新增|加入|建立|新建|登錄|上架|加)'
                     r'\s*(?:一個|一款|一支|一件|一項|個|款)?\s*(?:新的?)?'
                     r'(?:商品|品項|東西|產品)?\s*(?:叫做|叫|名字是|名稱是|是)?\s*',
                     # ⚠️ 這條要能吃掉「供應商送來新品」整串——原本只剝到
                     #   「供應商送來」，剩下的「新品」被下一條的「新」吃掉
                     #   一半，商品名變成「品 藍牙喇叭」（實測）。
                     # ⚠️ 「新品」必須排在「新的?」**之前**——否則 `新的?`
                     #   先吃掉「新」，剩「品」黏在商品名上變成「品 藍牙喇叭」
                     #   （實測）。這是 alternation 順序的典型坑：長的排前面。
                     r'^\s*(?:剛到|剛進|剛收到|剛來|供應商(?:送來|送的?|給的?)?|新到|進來)'
                     r'\s*(?:了)?\s*(?:一批|一箱|一車)?'
                     r'\s*(?:新品|新的|新|商品|品項|貨品|貨)?\s*',
                     # 欄位值被 group(0) 移除後可能留下的**引導動詞殘字**
                     #   （「先進北倉100個」移除「北倉100個」後剩「先進」）。
                     #   ⚠️ 只剝句尾/獨立的殘字，不碰商品名本體。
                     r'\s*(?:先|再|另外)?\s*(?:進|放|收|擺|給)\s*$',
                     r'\s*(?:各|都)\s*$',
                     r'\d+\s*(?:個|件|台|支|組)']:
            _name = _re.sub(pat, '', _name).strip()
        _name = _re.sub(r'\s{2,}', ' ', _name).strip(' ，,、。.：:；;-')
        # ── r22 上架主線（2026-08-07）：**出卡判準收緊**（user 定調
        #   「一律問到齊才出卡」）。原本只要 name+category 就出確認卡，
        #   價格/安全庫存抽不到就填 0 ⇒ **假成功**：卡片看起來正常、
        #   按下去就建出一筆價格 0 的商品，資料庫被汙染且不易察覺。
        #   實測「幫我新增一個藍牙鍵盤，電子類，賣800，安全庫存30」
        #   曾出卡成 name='幫我新增一個藍牙鍵盤，' price=0 safety=0。
        #   ⇒ 價格與安全庫存是**營運關鍵欄位**（影響缺貨警示與估值），
        #     缺一個就不出卡，交給分步流程逐項補問。
        #   ⚠️ 倉庫初始量不列入必填——新品可以先建檔、之後再進貨（0 是合理值）。
        # ── r22（user 定調「一句話就進去」，同英文版）：**不再因為缺欄位
        #   反問**。實務上建檔當下常常還不知道售價（掃碼建檔也是這樣），
        #   安全庫存則可以從初始庫存推（進多少就維持多少水位）。
        #   ⇒ 缺的欄位填合理預設、直接出確認卡，卡上把來源標出來讓人看。
        if _name and _found_cat:
            # 防呆：檢查同名
            if any(it["name"] == _name for it in W.state().items):
                return {"ok": True, "summary": f"⚠️ 商品「{_name}」已存在，請改用其他名稱。",
                        "view": "item_create_step1", "data": {"step": 1, "prompt": "請輸入不同的商品名稱"}}
            new_sku = _next_sku(_found_cat)
            _n_qty = int(_north_m.group(1)) if _north_m else 0
            _c_qty = int(_central_m.group(1)) if _central_m else 0
            _s_qty = int(_south_m.group(1)) if _south_m else 0
            _init_total = _n_qty + _c_qty + _s_qty
            # 安全庫存三層 fallback：①明講 ②沒講但有初始庫存 → 等於初始庫存
            #   （倉管直覺：進多少就維持多少，也讓新品不會立刻跳缺貨）
            #   ③都沒有 → 預設 20（佔位值，建完講一句就能改）
            if _safety_m:
                _safety_val, _safety_src = int(_safety_m.group(1)), "stated"
            elif _init_total > 0:
                _safety_val, _safety_src = _init_total, "from_stock"
            else:
                _safety_val, _safety_src = _DEFAULT_SAFETY, "default"
            # ⚠️ **沒講倉庫量 → 三倉都補成安全庫存值**（user 定調
            #   「安全庫存預設不是 0，這樣建好的商品在三個倉庫就不會是 0」）。
            #   缺貨判定是「每個倉各自 < 安全庫存」，若三倉留 0 而安全庫存
            #   有值（不論是明講的 30 還是預設 50），建好**三倉立刻全跳缺貨**
            #   （實測確認）。補成等於水位 ⇒ 剛好不觸發、展場也查得到數字。
            if _init_total == 0 and _safety_val > 0:
                _n_qty = _c_qty = _s_qty = _safety_val
            pending = {
                "name": _name, "category": _found_cat,
                "category_label": W.CATEGORY_LABEL.get(_found_cat, _found_cat),
                "price": int(_price_m.group(1)) if _price_m else 0,
                "safety": _safety_val,
                "stock_north": _n_qty,
                "stock_central": _c_qty,
                "stock_south": _s_qty,
                "sku": new_sku,
                # 價格沒講 → 標記未定價（跟「真的賣 0 元」區分開，
                #   庫存價值統計可據此排除，不汙染報表）
                "price_unset": not bool(_price_m),
                "safety_src": _safety_src,
            }
            _notes = []
            if _safety_src == "from_stock":
                _notes.append(f"安全庫存 {_safety_val}（同初始庫存）")
            elif _safety_src == "default":
                _notes.append(f"安全庫存 {_safety_val}（預設值）")
            if not _price_m:
                _notes.append("售價未設定")
            _sum = ("已解析商品資訊，請確認" if not _notes else
                    "已解析商品資訊——我幫你填了「" + "、".join(_notes)
                    + "」，確認前可以在卡片上改。")
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
        existing = [it for it in W.state().items if it["name"] == name]
        if existing:
            return {"ok": True, "summary": f"⚠️ 商品「{name}」已存在（SKU: {existing[0]['sku_id']}），請改用其他名稱。",
                    "view": "item_create_step1",
                    "data": {"step": 1, "prompt": "請輸入不同的商品名稱"}}
        return {"ok": True, "summary": f"已記錄商品名稱：「{name}」\n第二步：屬於哪一類？（輸入「取消」可退出）",
                "view": "item_create_step2",
                "data": {"step": 2, "name": name, "prompt": "請選擇類別（或輸入「取消」退出）"}}
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
                    "summary": (f"「{category}」不是類別喔。請從：電子產品／家電廚具／"
                                "食品飲料／日用品／服飾／運動用品 選一個（輸入「取消」可退出）"),
                    "view": "item_create_step2",
                    "data": {"step": 2, "name": name,
                             "prompt": "請選擇類別（或輸入「取消」退出）"}}
        category = _cat_key
        _cat_lbl2 = W.CATEGORY_LABEL.get(category, category)
        return {"ok": True,
                "summary": f"已記錄：「{name}」→ {_cat_lbl2}\n第三步：單價多少？安全庫存幾件？\n例如：150 100（輸入「取消」可退出）",
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
        # r75：輸入裡沒有數字（「廚具」曾被吸成單價0/安全0 靜默過關）→ 留在
        # 第三步重問，不帶 0 值前進
        if price_val <= 0:
            return {"ok": True,
                    "summary": ("第三步需要數字喔：單價多少？安全庫存幾件？\n"
                                "例如：150 100（輸入「取消」可退出）"),
                    "view": "item_create_step3",
                    "data": {"step": 3, "name": name, "category": category,
                             "prompt": "格式：單價 安全庫存（例如 150 100，或輸入取消）"}}
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
            "category_label": W.CATEGORY_LABEL.get(category, category),
            "price": int(price) if price else 0,
            "safety": int(safety) if safety else 0,
            "stock_north": sn, "stock_central": sc, "stock_south": ss,
            "sku": new_sku,
        }
        stock_summary = f"北{sn} 中{sc} 南{ss}" if (sn+sc+ss) > 0 else "全部為 0"
        return {"ok": True,
                "summary": f"📦 準備新增「{name}」\n類別：{pending['category_label']} | 單價：{pending['price']}元 | 安全庫存：{pending['safety']}件\n初始庫存：{stock_summary}",
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
    # r75 危險級縱深防禦：名稱空白的商品絕不落地（曾建出商品「」污染主檔）
    if not str(item.get("name", "")).strip():
        return W._err("商品名稱是空的，無法新增。請重新從「新增商品」開始。")

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

    return {"ok": True, "summary": f"✅ 已新增商品「{item['name']}」（SKU: {item['sku']}）",
            "view": "item_done", "data": {"item": item, "trace_id": trace_id}}


# ════════════════════════════════════════════════════════════
# ⑧ create_movement — 自然語言即時進出貨（輕量版，非完整 PO/SO 單據）
#    「北倉進了藍牙耳機50件」/「南倉出貨洗衣精20件」→ HITL 確認 → 真寫入
#    stock.csv + transactions/，重開 server / 網頁重整不會消失。
# ════════════════════════════════════════════════════════════
def _movement_dir_label(direction: str) -> str:
    """direction 可能是內部代碼 in/out，或原始中文詞；統一轉成給使用者看的中文標籤。"""
    d = direction or ""
    if any(w in d for w in ("進", "in")):
        return "進了"
    if any(w in d for w in ("出", "out")):
        return "出貨"
    return d


def create_movement(keyword: str = "", warehouse: str = "", direction: str = "",
                     qty: str = "", is_return: bool = False) -> dict:
    """觸發進出貨流程：找商品/倉別 → 算庫存變化 → 回確認卡（不執行寫入）。
    is_return=True 表示客人退貨（庫存增加，走 in 的算法，但顯示/紀錄標「退貨」）。"""
    if not keyword:
        return W._err("請說明要異動哪個商品，例如「北倉進了藍牙耳機50件」")

    scored = W.match_items(keyword)
    if not scored:
        # r17：找不到商品是輸入問題不是系統錯誤 → clarify 藍卡而非 error 紅卡
        return {"ok": True, "view": "clarify",
                "summary": f"找不到商品「{keyword}」，請確認商品名稱後再說一次。",
                "data": {"question": f"找不到商品「{keyword}」，請確認商品名稱",
                         "options": [], "hint": "例如「北倉進50個藍牙耳機」"}}
    # 分數斷層過濾（同 warehouse.query_inventory 的邏輯）：避免共用規格 token
    # （如「1L」「男款」）讓不相干商品低分命中、誤觸發多筆 clarify。
    if len(scored) > 1:
        top_score = scored[0]["score"]
        scored = [m for m in scored if m["score"] * 2 >= top_score]
    matches = [m["item"] for m in scored]
    if len(matches) > 1:
        opts = [it["name"] for it in matches[:5]]
        _dir_label = _movement_dir_label(direction)
        _qty_txt = f"{qty}件" if qty else ""
        return {"ok": True,
                "summary": f"找到 {len(matches)} 筆「{keyword}」相關商品，你想異動哪個？",
                "view": "clarify",
                "data": {"question": f"找到 {len(matches)} 筆「{keyword}」相關商品，你想異動哪個？",
                         "options": [f"{n} {warehouse or ''}{_dir_label}{_qty_txt}".strip() for n in opts],
                         "hint": "請輸入完整商品名稱重新描述"}}
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
                "summary": (f"一次異動 {_pre_qty:,} 件不太尋常"
                            "（單次上限 9,999 件），請確認數量後再說一次。"),
                "data": {"question": f"要異動 {_pre_qty:,} 件？請確認數量",
                         "options": [], "hint": ""}}

    wh = (warehouse or "").strip()
    _WH_ALIASES = {"north": "north", "北": "north", "北倉": "north", "北區倉": "north", "北區": "north",
                   "central": "central", "中": "central", "中倉": "central", "中區倉": "central", "中區": "central",
                   "south": "south", "南": "south", "南倉": "south", "南區倉": "south", "南區": "south"}
    wh_key = _WH_ALIASES.get(wh, "")
    if not wh_key:
        _dir_label_zh = _movement_dir_label(direction)
        _qty_txt = f"{qty}件" if qty else "50件"
        return {"ok": True, "summary": f"「{item['name']}」要異動哪個倉？",
                "view": "clarify",
                "data": {"question": f"「{item['name']}」要異動哪個倉？",
                         "options": [f"北倉{_dir_label_zh}{item['name']}{_qty_txt}",
                                     f"中倉{_dir_label_zh}{item['name']}{_qty_txt}",
                                     f"南倉{_dir_label_zh}{item['name']}{_qty_txt}"],
                         "hint": "請輸入完整描述，例如「北倉進了{}{}」".format(item['name'], _qty_txt),
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
        return W._err(f"請說明是「進貨」還是「出貨」，例如「{wh}{item['name']}進了{qty or 50}件」")

    try:
        qty_val = int(str(qty).strip() or 0)
    except ValueError:
        qty_val = 0
    _dir_zh = "退貨" if is_return else ("進貨" if dir_key == "in" else "出貨")
    if qty_val < 0:
        # r17：「北倉進貨-20個耳機」負號曾被吞、開出 +20 卡（語意反轉）
        return {"ok": True, "view": "clarify",
                "summary": (f"數量不能是負數喔。要減少庫存請用「出貨」，"
                            f"例如「{WH_LABEL_MAP.get(wh_key, wh_key)}出{abs(qty_val)}件{item['name']}」。"),
                "data": {"question": "數量不能是負數，請重新描述", "options": [], "hint": ""}}
    if qty_val == 0:
        # 缺數量 → clarify 追問（曾是 error 紅字卡，r17 統一成 clarify）
        return {"ok": True, "view": "clarify",
                "summary": f"「{item['name']}」要{_dir_zh}幾件呢？例如「{_dir_zh}50件」。",
                "data": {"question": f"「{item['name']}」要{_dir_zh}幾件？",
                         "options": [f"{_dir_zh}10件", f"{_dir_zh}30件", f"{_dir_zh}50件"],
                         "hint": "請說明數量，例如「進了50件」",
                         "flow": {"tool": "create_movement", "await": "qty",
                                  "keyword": item["name"], "warehouse": wh,
                                  "direction": direction, "is_return": is_return}}}
    if qty_val > 9999:
        # r17：999999 件這種展場搗蛋數字不開卡，追問確認
        return {"ok": True, "view": "clarify",
                "summary": (f"一次{_dir_zh} {qty_val:,} 件不太尋常"
                            "（單次上限 9,999 件），請確認數量後再說一次。"),
                "data": {"question": f"要{_dir_zh} {qty_val:,} 件？請確認數量",
                         "options": [], "hint": ""}}

    s = W.state()
    current_qty = s.stock.get(wh_key, {}).get(sku, 0)
    new_qty = current_qty + qty_val if dir_key == "in" else current_qty - qty_val

    if dir_key == "out" and new_qty < 0:
        return {"ok": False,
                "summary": f"⚠️ 庫存不足，無法出貨。「{item['name']}」{WH_LABEL_MAP[wh_key]}目前僅 {current_qty} 件，不足 {qty_val} 件。",
                "view": "error", "data": {}}

    wh_label = WH_LABEL_MAP[wh_key]
    # 退貨顯示「退貨」、圖示不同，但庫存跟 in 一樣是加（sign=+）
    dir_label = "退貨" if is_return else ("進貨" if dir_key == "in" else "出貨")
    icon = "↩️" if is_return else "📦"
    sign = "+" if dir_key == "in" else "-"
    summary = (f"{icon} 確認{dir_label}\n"
               f"商品：{item['name']}（{sku}）\n"
               f"倉別：{wh_label}\n"
               f"數量：{sign}{qty_val} 件\n"
               f"目前庫存：{current_qty} 件 → {dir_label}後：{new_qty} 件")
    return {"ok": True, "summary": summary, "view": "movement_confirm",
            "data": {"pending": True, "sku": sku, "name": item["name"], "warehouse": wh_key,
                     "warehouse_label": wh_label, "direction": dir_key, "direction_label": dir_label,
                     "qty": qty_val, "before_qty": current_qty, "after_qty": new_qty,
                     "is_return": is_return}}


WH_LABEL_MAP = {"north": "北區倉", "central": "中區倉", "south": "南區倉"}


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
        return W._err("數量異常，無法寫入。")

    with _STOCK_LOCK:
        # 0. 在鎖內重讀當下庫存 → 重驗 → 重算（防 TOCTOU 陳舊寫入）
        s = W.state()
        current = s.stock.get(wh_key, {}).get(sku, 0)
        if dir_key == "out":
            if qty_val > current:
                return {"ok": False,
                        "summary": (f"⚠️ 庫存已變動，無法出貨。「{p['name']}」"
                                    f"{p['warehouse_label']}目前僅 {current} 件，"
                                    f"不足 {qty_val} 件（確認卡開立後庫存被其他操作改過）。"),
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
            "summary": f"✅ 已記錄{p['direction_label']}。{p['name']} {p['warehouse_label']}現有 {after_qty} 件。",
            "view": "movement_done", "data": {"trace_id": trace_id, **_done}}


# ════════════════════════════════════════════════════════════
# ⑧b create_transfer / commit_transfer — 跨倉調貨（A 倉 → B 倉）
#    調貨 = 同時扣來源倉、加目標倉，總量不變。走 HITL 確認卡（同進出貨），
#    來源倉不足擋下，交易紀錄拆成「來源倉 out + 目標倉 in」兩筆（跟現有
#    transactions 格式一致，RCA/報表完全不用改）。2026-07-02 新增。
# ════════════════════════════════════════════════════════════
_WH_ALIASES_TF = {"north": "north", "北": "north", "北倉": "north", "北區倉": "north", "北區": "north",
                  "central": "central", "中": "central", "中倉": "central", "中區倉": "central", "中區": "central",
                  "south": "south", "南": "south", "南倉": "south", "南區倉": "south", "南區": "south"}


def create_transfer(keyword: str = "", from_wh: str = "", to_wh: str = "",
                    qty: str = "") -> dict:
    """觸發調貨流程：找商品 → 解析來源/目標倉 → 檢查來源庫存 → 回確認卡（不寫入）。"""
    if not keyword:
        return {"ok": True, "view": "clarify",
                "summary": "要調哪個商品呢？直接說商品名就可以，例如「衛生紙」。",
                "data": {"question": "要調哪個商品？直接說商品名",
                         "options": [], "hint": ""}}

    scored = W.match_items(keyword)
    if not scored:
        # r17：找不到商品是輸入問題不是系統錯誤 → clarify 藍卡而非 error 紅卡
        return {"ok": True, "view": "clarify",
                "summary": f"找不到商品「{keyword}」，請確認商品名稱後再說一次。",
                "data": {"question": f"找不到商品「{keyword}」，請確認商品名稱",
                         "options": [], "hint": "例如「北倉進50個藍牙耳機」"}}
    if len(scored) > 1:
        top_score = scored[0]["score"]
        scored = [m for m in scored if m["score"] * 2 >= top_score]
    matches = [m["item"] for m in scored]
    if len(matches) > 1:
        opts = [it["name"] for it in matches[:5]]
        return {"ok": True,
                "summary": f"找到 {len(matches)} 筆「{keyword}」相關商品，你想調哪個？",
                "view": "clarify",
                "data": {"question": f"找到 {len(matches)} 筆「{keyword}」相關商品，你想調哪個？",
                         "options": [f"{n} 從{from_wh or ''}調到{to_wh or ''}" for n in opts],
                         "hint": "請輸入完整商品名稱重新描述"}}
    item = matches[0]
    sku = item["sku_id"]

    from_key = _WH_ALIASES_TF.get((from_wh or "").strip(), "")
    to_key = _WH_ALIASES_TF.get((to_wh or "").strip(), "")
    if not from_key or not to_key:
        return {"ok": True, "summary": f"「{item['name']}」要從哪個倉調到哪個倉？",
                "view": "clarify",
                "data": {"question": f"「{item['name']}」要從哪個倉調到哪個倉？",
                         "options": [f"北倉調{item['name']}去南倉{qty or 20}件",
                                     f"南倉調{item['name']}去北倉{qty or 20}件",
                                     f"中倉調{item['name']}去北倉{qty or 20}件"],
                         "hint": "請講清楚來源倉跟目標倉，例如「北倉調{}去南倉」".format(item['name']),
                         # r61：帶上已知的單邊倉——「調10個去南倉」只缺來源，
                         # 訪客答「從北倉調」單邊也要能補
                         "flow": {"tool": "create_transfer", "await": "route",
                                  "keyword": item["name"], "qty": str(qty),
                                  "from_wh": from_wh or "", "to_wh": to_wh or ""}}}
    if from_key == to_key:
        return W._err("來源倉跟目標倉不能是同一個，請確認一下要從哪調到哪。")

    try:
        qty_val = int(str(qty).strip() or 0)
    except ValueError:
        qty_val = 0
    if qty_val <= 0:
        # 缺數量→clarify 追問（非 error 紅字）。RPI5 conv100-r2：「調一批…到」
        # 模糊量詞無精確數，友善問數量比報錯好。帶已知商品/來源/目標讓前端可續填。
        _kwn = keyword or ""
        return {"ok": True, "view": "clarify",
                "summary": f"要調多少{('「'+_kwn+'」') if _kwn else ''}呢？請說個數量，例如「調20件」。",
                "data": {"pending_transfer": True, "keyword": _kwn,
                         "from_wh": from_wh, "to_wh": to_wh,
                         "flow": {"tool": "create_transfer", "await": "qty",
                                  "keyword": _kwn, "from_wh": from_wh, "to_wh": to_wh}}}

    s = W.state()
    from_cur = s.stock.get(from_key, {}).get(sku, 0)
    to_cur = s.stock.get(to_key, {}).get(sku, 0)
    if qty_val > from_cur:
        return {"ok": False,
                "summary": f"⚠️ 庫存不足，無法調貨。「{item['name']}」{WH_LABEL_MAP[from_key]}目前僅 {from_cur} 件，不足 {qty_val} 件。",
                "view": "error", "data": {}}

    from_label, to_label = WH_LABEL_MAP[from_key], WH_LABEL_MAP[to_key]
    summary = (f"🔄 確認調貨\n"
               f"商品：{item['name']}（{sku}）\n"
               f"數量：{qty_val} 件\n"
               f"{from_label}：{from_cur} 件 → {from_cur - qty_val} 件\n"
               f"{to_label}：{to_cur} 件 → {to_cur + qty_val} 件")
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
        return W._err("數量異常，無法調貨。")

    with _STOCK_LOCK:
        # 0. 鎖內重讀 → 重驗 → 重算
        s = W.state()
        from_cur = s.stock.get(from_key, {}).get(sku, 0)
        to_cur = s.stock.get(to_key, {}).get(sku, 0)
        if qty_val > from_cur:
            return {"ok": False,
                    "summary": (f"⚠️ 庫存已變動，無法調貨。「{p['name']}」"
                                f"{p['from_label']}目前僅 {from_cur} 件，"
                                f"不足 {qty_val} 件（確認卡開立後庫存被其他操作改過）。"),
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
            "summary": (f"✅ 已完成調貨。{p['name']} {qty_val} 件從 {p['from_label']}"
                        f"調到 {p['to_label']}。\n"
                        f"{p['from_label']}現有 {from_after} 件、"
                        f"{p['to_label']}現有 {to_after} 件。"),
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
        return W._err("密碼錯誤，無法重置")

    import shutil
    ts = datetime.now().isoformat(timespec="seconds")
    trace_id = trace_id or f"reset-{ts}"
    root = Path(__file__).parent
    baseline = root / "warehouse_data_baseline"
    current = root / "warehouse_data"
    if not baseline.exists():
        return W._err("找不到基準快照 warehouse_data_baseline/，無法重置")

    # 🚨 2026-08-06：rmtree 裸奔曾在「模擬/腳本寫檔中」撞
    #   Directory not empty → **半刪災難**（master/ 只剩 stock.csv、
    #   服務重啟即掛，實案）。改雙 tmp 原子交換：先把新資料建好、
    #   rename 換入——目錄 rename 不受內部檔案被佔用影響。
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

    return {"ok": True, "summary": "✅ 展示資料已重置回初始狀態。",
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

    return {"ok": True, "summary": f"✅ 已刪除：{deleted_names}（共 {len(deletable)} 項）",
            "view": "item_done", "data": {"deleted": deleted_names, "trace_id": trace_id}}
