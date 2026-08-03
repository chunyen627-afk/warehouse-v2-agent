#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""soak_monitor.py — 長時間嚴苛條件監控（中英同時 200× + 60 商品全動）。

user 要求：用最嚴苛條件開機好幾天，看數據會怎樣。

每 10 分鐘記錄一次（append 到 _soak.csv），看四類趨勢：
  ① 資源：load / 記憶體 / 溫度 / 各服務 RSS（抓記憶體洩漏）
  ② 資料健康：負數庫存 / 歸零 / 暴走總量 / stock.csv 完整性
  ③ 警報分布：low_stock / burst / expiry 各幾筆（看有沒有失控）
  ④ 模擬活性：count 有沒有持續增加（確認沒卡死）

⚠️ 這支只讀不寫，可以安全地跟展示同時跑。
用法：setsid nohup python3 soak_monitor.py > _soak.log 2>&1 &
"""
import csv
import json
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

OUT = Path(__file__).parent / "_soak.csv"
INTERVAL = 600          # 10 分鐘一筆
PORTS = ("8002", "8001")
ROOTS = {"8002": Path("/home/p400/warehouse_v2_en"),
         "8001": Path("/home/p400/warehouse_v2")}


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def _get(url, timeout=45):
    with urllib.request.urlopen(url, context=_ctx(), timeout=timeout) as r:
        return json.loads(r.read())


def _post(port, payload):
    req = urllib.request.Request(
        f"https://localhost:{port}/api/live_mode",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, context=_ctx(), timeout=30) as r:
        return json.loads(r.read())


def sysinfo():
    la = float(open("/proc/loadavg").read().split()[0])
    mem = subprocess.run(["free", "-m"], capture_output=True, text=True).stdout
    line = [l for l in mem.splitlines() if l.startswith("Mem:")][0].split()
    temp = 0.0
    try:
        temp = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
    except Exception:
        pass
    rss = {}
    for svc in ("warehouse-v2-en", "warehouse-v2"):
        pid = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", svc],
                             capture_output=True, text=True).stdout.strip()
        if pid and pid != "0":
            o = subprocess.run(["ps", "-o", "rss=", "-p", pid],
                               capture_output=True, text=True).stdout.strip()
            rss[svc] = int(o) // 1024 if o.isdigit() else 0
    return {"load": la, "mem_avail": int(line[6]), "temp": round(temp, 1),
            "rss_en": rss.get("warehouse-v2-en", 0), "rss_zh": rss.get("warehouse-v2", 0)}


def csv_rows(port):
    p = ROOTS[port] / "warehouse_data/master/stock.csv"
    try:
        rows = list(csv.DictReader(open(p, encoding="utf-8-sig")))
        for r in rows:
            int(r["qty"]); r["warehouse"]; r["sku_id"]
        return len(rows)
    except Exception:
        return -1


def probe(port):
    """回這一版的健康指標。任何一項失敗都記 -1，不中斷監控。"""
    d = {"port": port}
    try:
        st = _post(port, {})
        d["live_on"] = int(bool(st.get("on")))
        d["count"] = st.get("count", -1)
        d["speedup"] = st.get("speedup", -1)
    except Exception:
        d.update(live_on=-1, count=-1, speedup=-1)
    try:
        g = _get(f"https://localhost:{port}/api/live_grid")["grid"]
        per = [(r, w) for r in g for w in ("north", "central", "south")]
        d["items"] = len(g)
        d["neg"] = sum(1 for r, w in per if r["per"][w] < 0)
        d["zero"] = sum(1 for r, w in per if r["per"][w] == 0)
        d["total"] = sum(r["total"] for r in g)
        d["max_total"] = max((r["total"] for r in g), default=0)
        d["low"] = sum(1 for r, w in per
                       if (r.get("safety") or 0) > 0 and r["per"][w] < r["safety"])
    except Exception:
        d.update(items=-1, neg=-1, zero=-1, total=-1, max_total=-1, low=-1)
    try:
        a = _get(f"https://localhost:{port}/anomalies")
        types = {}
        for x in a.get("alerts", []):
            types[x["type"]] = types.get(x["type"], 0) + 1
        d["alerts"] = a.get("all_count", -1)
        d["burst"] = types.get("burst", 0)
        d["low_alert"] = types.get("low_stock", 0)
        d["expiry"] = types.get("expiry", 0)
    except Exception:
        d.update(alerts=-1, burst=-1, low_alert=-1, expiry=-1)
    d["csv_rows"] = csv_rows(port)
    return d


FIELDS = ["ts", "port", "load", "mem_avail", "temp", "rss_en", "rss_zh",
          "live_on", "count", "speedup", "items", "neg", "zero", "total",
          "max_total", "low", "alerts", "burst", "low_alert", "expiry", "csv_rows"]


def main():
    new = not OUT.exists()
    print(f"soak 監控啟動，每 {INTERVAL//60} 分鐘記錄 → {OUT}")
    while True:
        sysi = sysinfo()
        ts = datetime.now().isoformat(timespec="seconds")
        with open(OUT, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
                new = False
            for port in PORTS:
                row = {"ts": ts, **sysi, **probe(port)}
                w.writerow({k: row.get(k, "") for k in FIELDS})
                print(f"[{ts}] {port} load={row['load']} mem={row['mem_avail']}MB "
                      f"temp={row['temp']}C rss_en={row['rss_en']} rss_zh={row['rss_zh']} "
                      f"count={row['count']} neg={row['neg']} zero={row['zero']} "
                      f"total={row['total']} low={row['low']} "
                      f"alerts={row['alerts']}(burst={row['burst']}) csv={row['csv_rows']}",
                      flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
