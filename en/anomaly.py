"""
anomaly.py — 主動異常偵測 pipeline（不靠使用者問，背景定時掃描）。

對齊業界 anomaly pipeline 標準型：
  掃描器（5 類規則 + 統計門檻控誤報）
    → AlertManager（分級 critical/warning/info + 去重 + 告警抑制）
    → Notifier 分發層（站內 WS 真接 + alerts.log 留底 + Slack/Teams/LINE/Email 接口預留）

職責分離：純偵測邏輯，不依賴 FastAPI。server 起背景線程呼叫 scan_once() / run_scheduler()。
判準全是「規則寫得出來」的確定性偵測 → 用 Python，不丟給 270M（模型本職是路由）。
"""
import json
import statistics
import threading
import time
from collections import defaultdict
from datetime import date as _date, timedelta
from pathlib import Path

import warehouse as W


# ════════════════════════════════════════════════════════════
# 設定（可調）
# ════════════════════════════════════════════════════════════
class AnomalyConfig:
    scan_interval_s = 300          # 背景掃描間隔（預設 5 分鐘，可設）
    log_max_bytes = 5 * 1024 * 1024  # alerts.log 輪替上限（超過就轉存 .1）
    days_left_critical = 7          # 撐天 ≤ 此 → critical
    days_left_warning = 14          # 撐天 ≤ 此 → warning
    expiry_warning_days = 14        # 效期 ≤ 此天還有量 → warning
    expiry_min_qty = 5              # 到期量門檻（低於不報，免洗版）
    burst_sigma = 3.0               # 出庫暴量：偏離均值 > N 倍標準差
    burst_min_history = 10          # 至少 N 天歷史才算暴量（樣本不足不報）
    dormant_days = 60               # 連續零出庫 ≥ 此 → 呆滯
    dormant_min_value = 2000        # 呆滯品庫存市值門檻（低於不報）
    suppress_hours = 6              # 同一告警 N 小時內不重報（告警抑制）


# ════════════════════════════════════════════════════════════
# 模擬時鐘 — 資料是凍結 seed，"今天" = snapshot_date
# ════════════════════════════════════════════════════════════
def _today() -> _date:
    snap = W.state().snapshot_date or "2026-05-26"
    return _date.fromisoformat(snap)


def _wh_label(s, key: str) -> str:
    """倉庫 key → 顯示名（north → North）。訪客看得到的字一律走這支。"""
    for wh in s.warehouses:
        if wh["key"] == key:
            return wh.get("label") or key
    return key


# ════════════════════════════════════════════════════════════
# 偵測規則（每條回 list[alert dict]）
#   alert = {key, level, type, title, detail, data}
#   key = 去重指紋（同一異常每次掃描 key 相同）
# ════════════════════════════════════════════════════════════
def _detect_po_short(s) -> list[dict]:
    """① PO 短收 / 對不上（critical）。"""
    out = []
    dd = Path(s.v2_data_dir) / "orders" / "PO"
    if not dd.exists():
        return out
    for pj in sorted(dd.glob("*.json")):
        po = json.load(open(pj, encoding="utf-8"))
        for ln in po["lines"]:
            if ln.get("note") == "short_received":
                gap = ln["order_qty"] - ln["received_qty"]
                nm = s._items_by_sku.get(ln["sku_id"], {}).get("name", ln["sku_id"])
                out.append({
                    "key": f"po_short:{po['po_id']}:{ln['sku_id']}",
                    "level": "critical", "type": "po_short",
                    "title": f"PO discrepancy: {nm} short {gap} units",
                    "detail": f"{po['po_id']} ({po['warehouse']}) ordered {ln['order_qty']}, received {ln['received_qty']}",
                    "data": {"po_id": po["po_id"], "sku_id": ln["sku_id"], "name": nm,
                             "gap": gap, "warehouse": po["warehouse"]},
                })
    return out


def _detect_low_stock(s) -> list[dict]:
    """② 跌破安全線 / 快斷貨（critical/warning）。複用 list_low_stock 的撐天。"""
    out = []
    r = W.execute("list_low_stock", {})
    warns = r.get("data", {}).get("warnings", []) if isinstance(r.get("data"), dict) else []
    for w in warns:
        dleft = w.get("days_left")
        if dleft is None:
            continue
        if dleft <= AnomalyConfig.days_left_critical:
            level = "critical"
        elif dleft <= AnomalyConfig.days_left_warning:
            level = "warning"
        else:
            level = "info"   # 已低於安全線、但短期不會斷貨 → 注意級
        verb = "Running out" if level != "info" else "Below safety stock"
        out.append({
            "key": f"low:{w['sku_id']}:{w['warehouse']}",
            "level": level, "type": "low_stock",
            "title": f"{verb}: {w['name']} - {dleft} days left",
            "detail": f"{w.get('warehouse_label','')} on hand {w.get('qty')}, safety {w.get('safety_stock')}, suggest {w.get('suggest_qty')}",
            "data": {"sku_id": w["sku_id"], "name": w["name"], "warehouse": w["warehouse"],
                     "days_left": dleft, "suggest_qty": w.get("suggest_qty")},
        })
    return out


def _detect_burst(s) -> list[dict]:
    """③ 出庫暴增/暴跌（warning）。統計門檻：偏離該 SKU 日均 > N 倍標準差。"""
    out = []
    # 每 (sku) 的每日出庫序列
    daily = defaultdict(lambda: defaultdict(int))   # sku -> {date: qty}
    for m in s.movements:
        if m["direction"] == "out":
            daily[m["sku_id"]][m["date"]] += m["qty"]
    today = _today()
    recent = {(today - timedelta(days=i)).isoformat() for i in range(3)}  # 近 3 天視為「最新」
    for sku, series in daily.items():
        vals = list(series.values())
        if len(vals) < AnomalyConfig.burst_min_history:
            continue
        mean = statistics.mean(vals)
        sd = statistics.pstdev(vals)
        if sd == 0:
            continue
        for d, q in series.items():
            if d not in recent:
                continue
            z = (q - mean) / sd
            if abs(z) >= AnomalyConfig.burst_sigma:
                nm = s._items_by_sku.get(sku, {}).get("name", sku)
                direction = "spike" if z > 0 else "drop"
                out.append({
                    "key": f"burst:{sku}:{d}",
                    "level": "warning", "type": "burst",
                    "title": f"Outbound {direction}: {nm} shipped {q} units on {d}",
                    "detail": f"daily avg {mean:.0f}, deviation {z:+.1f}σ (possible fraud / theft / system error)",
                    "data": {"sku_id": sku, "name": nm, "date": d, "qty": q,
                             "mean": round(mean, 1), "z": round(z, 2)},
                })
    return out


def _detect_expiry(s) -> list[dict]:
    """④ 快到期還有量（warning）。效期 ≤ N 天且庫存 ≥ 門檻。"""
    out = []
    today = _today()
    for b in s.batches:
        try:
            exp = _date.fromisoformat(b["expire_date"])
        except Exception:
            continue
        dleft = (exp - today).days
        if 0 <= dleft <= AnomalyConfig.expiry_warning_days and b.get("qty", 0) >= AnomalyConfig.expiry_min_qty:
            nm = s._items_by_sku.get(b["sku_id"], {}).get("name", b["sku_id"])
            out.append({
                "key": f"expiry:{b['sku_id']}:{b['warehouse']}:{b['expire_date']}",
                "level": "warning", "type": "expiry",
                "title": f"Expiring with stock: {nm} has {b['qty']} units, {dleft} days left",
                "detail": f"{_wh_label(s, b['warehouse'])} expires {b['expire_date']} (write-off risk)",
                "data": {"sku_id": b["sku_id"], "name": nm, "warehouse": b["warehouse"],
                         "days_left": dleft, "qty": b["qty"]},
            })
    return out


def _detect_dormant(s) -> list[dict]:
    """⑤ 呆滯品（info）。連續 ≥ N 天零出庫但庫存市值 ≥ 門檻。"""
    out = []
    today = _today()
    cutoff = (today - timedelta(days=AnomalyConfig.dormant_days)).isoformat()
    last_out = defaultdict(str)   # sku -> 最後出庫日
    for m in s.movements:
        if m["direction"] == "out":
            if m["date"] > last_out[m["sku_id"]]:
                last_out[m["sku_id"]] = m["date"]
    for it in s.items:
        sku = it["sku_id"]
        lo = last_out.get(sku, "")
        if lo and lo >= cutoff:
            continue   # 近期有出庫，不算呆滯
        total = sum(s.stock.get(wh["key"], {}).get(sku, 0) for wh in s.warehouses)
        value = total * it["unit_price"]
        if value >= AnomalyConfig.dormant_min_value and total > 0:
            out.append({
                "key": f"dormant:{sku}",
                "level": "info", "type": "dormant",
                "title": f"Dormant: {it['name']} no outbound for {AnomalyConfig.dormant_days}+ days",
                "detail": f"{total} units, tied-up value NT$ {value:,}" + (f", last outbound {lo}" if lo else ", no outbound records"),
                "data": {"sku_id": sku, "name": it["name"], "qty": total, "value": value, "last_out": lo},
            })
    return out


_DETECTORS = [_detect_po_short, _detect_low_stock, _detect_burst, _detect_expiry, _detect_dormant]


# ════════════════════════════════════════════════════════════
# AlertManager — 分級 / 去重 / 告警抑制
# ════════════════════════════════════════════════════════════
class AlertManager:
    _LEVEL_ORDER = {"critical": 0, "warning": 1, "info": 2}

    def __init__(self):
        self._seen: dict[str, float] = {}   # key -> 上次發出時間戳
        self._lock = threading.Lock()

    def scan(self) -> list[dict]:
        """跑所有偵測器，回所有當前異常（未去重）。"""
        s = W.state()
        alerts = []
        for det in _DETECTORS:
            try:
                alerts.extend(det(s))
            except Exception as e:
                # 單一偵測器失敗不影響其他（業界 pipeline 韌性）
                alerts.append({"key": f"detector_err:{det.__name__}", "level": "info",
                               "type": "detector_error", "title": f"Detector {det.__name__} failed",
                               "detail": str(e)[:100], "data": {}})
        # ── 套用 set_alert 設的訂閱規則：被使用者「關注」的商品異常 → 升級 ──
        alerts = self._apply_subscriptions(s, alerts)
        alerts.sort(key=lambda a: self._LEVEL_ORDER.get(a["level"], 9))
        return alerts

    def _apply_subscriptions(self, s, alerts: list[dict]) -> list[dict]:
        """讀 alert_rules.json（set_alert 寫的）：被訂閱商品的對應異常標記 ⭐ 並升級。"""
        try:
            rp = Path(s.v2_data_dir) / "alert_rules.json"
            if not rp.exists():
                return alerts
            rules = json.load(open(rp, encoding="utf-8")).get("rules", [])
        except Exception:
            return alerts
        cond_type = {"below_safety": "low_stock", "out_of_stock": "low_stock", "expiring": "expiry"}
        for a in alerts:
            if a.get("subscribed"):
                continue                      # 已標過（多條規則命中同一告警）→ 不重複疊 ⭐
            sku = a.get("data", {}).get("sku_id")
            for r in rules:
                if not r.get("enabled", True):
                    continue
                if cond_type.get(r["condition"]) != a["type"]:
                    continue
                if r["scope"] and sku not in r["scope"]:
                    continue
                a["subscribed"] = True
                a["title"] = "⭐ " + a["title"]
                if a["level"] == "info":      # 被訂閱 → info 升 warning
                    a["level"] = "warning"
                break                         # 一個告警被訂閱就是被訂閱，不論幾條規則命中
        return alerts

    def filter_new(self, alerts: list[dict]) -> list[dict]:
        """去重 + 告警抑制：只回「新出現」或「超過抑制窗」的告警。"""
        now = time.time()
        window = AnomalyConfig.suppress_hours * 3600
        fresh = []
        with self._lock:
            for a in alerts:
                last = self._seen.get(a["key"])
                if last is None or (now - last) >= window:
                    self._seen[a["key"]] = now
                    fresh.append(a)
        return fresh

    def reset_suppression(self):
        with self._lock:
            self._seen.clear()


# ════════════════════════════════════════════════════════════
# Notifier 分發層 — 站內 WS + log 真接；外部管道接口預留
# ════════════════════════════════════════════════════════════
class Notifier:
    """換管道是設定問題不是工程問題：核心產告警 → 分發層決定推哪些。"""

    def __init__(self, ws_push=None):
        self.ws_push = ws_push       # async function(payload) — server 注入
        self._log_path = None

    @staticmethod
    def _rotate_if_needed(log_path: Path):
        """檔案超過上限就轉存 .1（只留一份舊檔）。

        ⚠️ 為何需要：`_seen`（告警抑制狀態）是**記憶體 dict**，服務一重啟就清空
        → 重啟後第一次掃描把當前全部告警重寫一遍。穩態下抑制窗運作正常
        （實測整晚零重複），但檔名綁 `snapshot_date`（固定 2026-05-26）
        ⇒ **永遠不換檔、只會單調增長**。機器交客戶後若每天開關機，
        一年約 6.5MB；量不大，但沒人能 SSH 進去清 ⇒ 加保險。
        ⚠️ **刻意不做**：①不把 `_seen` 持久化——重啟後訪客**本來就該**重新看到
        當前告警（那正是 banner 的用途），持久化會讓重開機後 banner 空白＝退步。
        ②不改檔名綁真實日期——會與 transactions/ 的 demo 基準日不一致。
        """
        try:
            if log_path.exists() and log_path.stat().st_size >= AnomalyConfig.log_max_bytes:
                bak = log_path.with_suffix(log_path.suffix + ".1")
                if bak.exists():
                    bak.unlink()
                log_path.rename(bak)
        except Exception:
            pass   # 輪替失敗不能影響告警本身（留底是次要職責）

    def _log(self, alerts: list[dict]):
        s = W.state()
        dd = Path(s.v2_data_dir)
        log_path = dd / "audit" / f"{s.snapshot_date or 'unknown'}_alerts.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_if_needed(log_path)
        import datetime
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with open(log_path, "a", encoding="utf-8") as f:
            for a in alerts:
                f.write(json.dumps({"ts": ts, "actor": "anomaly_scanner",
                                    **{k: a[k] for k in ("key", "level", "type", "title")}},
                                   ensure_ascii=False) + "\n")

    async def dispatch(self, alerts: list[dict]):
        if not alerts:
            return
        # [真接] 留底
        self._log(alerts)
        # [真接] 站內 WS 即時推播（只推 critical/warning，info 只記錄）
        pushable = [a for a in alerts if a["level"] in ("critical", "warning")]
        if pushable and self.ws_push:
            await self.ws_push({"type": "anomaly_alert", "alerts": pushable,
                                "counts": _count_levels(alerts)})
        # [接口預留] 外部管道：填 URL/token 即生效
        for a in alerts:
            if a["level"] == "critical":
                _notify_slack(a)     # TODO: 設 SLACK_WEBHOOK_URL
                _notify_line(a)      # TODO: 設 LINE_NOTIFY_TOKEN
                _notify_email(a)     # TODO: 設 SMTP_*


# ── 外部管道接口（空殼 + TODO，填設定即生效）──────────────────
import os as _os


def _notify_slack(alert: dict):
    """Slack / Teams webhook。設 env SLACK_WEBHOOK_URL 即生效。"""
    url = _os.getenv("SLACK_WEBHOOK_URL", "")
    if not url:
        return  # 未設定 → 跳過（接口已備好）
    try:
        import urllib.request
        body = json.dumps({"text": f"[{alert['level'].upper()}] {alert['title']}\n{alert['detail']}"}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _notify_line(alert: dict):
    """LINE Notify。設 env LINE_NOTIFY_TOKEN 即生效。"""
    token = _os.getenv("LINE_NOTIFY_TOKEN", "")
    if not token:
        return
    try:
        import urllib.request, urllib.parse
        data = urllib.parse.urlencode({"message": f"[{alert['level']}] {alert['title']}"}).encode()
        req = urllib.request.Request("https://notify-api.line.me/api/notify", data=data,
                                     headers={"Authorization": f"Bearer {token}"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _notify_email(alert: dict):
    """Email SMTP。設 env SMTP_HOST/SMTP_USER/SMTP_PASS/ALERT_TO 即生效。"""
    host = _os.getenv("SMTP_HOST", "")
    if not host:
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(f"{alert['title']}\n\n{alert['detail']}", _charset="utf-8")
        msg["Subject"] = f"[Warehouse alert - {alert['level']}] {alert['title']}"
        msg["From"] = _os.getenv("SMTP_USER", "")
        msg["To"] = _os.getenv("ALERT_TO", "")
        with smtplib.SMTP(host, int(_os.getenv("SMTP_PORT", "587"))) as srv:
            srv.starttls()
            srv.login(_os.getenv("SMTP_USER", ""), _os.getenv("SMTP_PASS", ""))
            srv.send_message(msg)
    except Exception:
        pass


def _count_levels(alerts: list[dict]) -> dict:
    c = {"critical": 0, "warning": 0, "info": 0}
    for a in alerts:
        c[a["level"]] = c.get(a["level"], 0) + 1
    return c


# ════════════════════════════════════════════════════════════
# 對外 API
# ════════════════════════════════════════════════════════════
_MANAGER = AlertManager()
_NOTIFIER = Notifier()
_scheduler_started = False


def set_ws_push(fn):
    """server 注入 WS 推播函式。"""
    _NOTIFIER.ws_push = fn


def scan_once(only_new: bool = False) -> dict:
    """跑一次掃描。only_new=True 只回新告警（背景用）；False 回全部（手動查詢用）。"""
    alla = _MANAGER.scan()
    result = _MANAGER.filter_new(alla) if only_new else alla
    return {"alerts": result, "all_count": len(alla), "counts": _count_levels(alla)}


async def scan_and_dispatch():
    """背景掃描 + 推播（只推新告警）。"""
    alla = _MANAGER.scan()
    fresh = _MANAGER.filter_new(alla)
    await _NOTIFIER.dispatch(fresh)
    return {"new": len(fresh), "all": len(alla), "counts": _count_levels(alla)}


def run_scheduler(loop, interval_s: int | None = None):
    """背景線程：定時掃描 + 推播。loop = server 的 asyncio event loop。"""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    interval = interval_s or AnomalyConfig.scan_interval_s
    import asyncio

    def _worker():
        time.sleep(8)   # 等 server ready
        while True:
            try:
                fut = asyncio.run_coroutine_threadsafe(scan_and_dispatch(), loop)
                fut.result(timeout=30)
            except Exception:
                pass
            time.sleep(interval)

    threading.Thread(target=_worker, daemon=True).start()
