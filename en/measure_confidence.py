#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""measure_confidence.py — 量測「系統在猜」的訊號分布（2026-08-02）。

user 的問題（比「加個重說提示」更根本）：
  **「應該要研究啥情況會請對方再說一次，而不是直接拿錯誤的句子隨便猜」**

實測 100 句真人錄音，20 個 ASR 聽錯的案例裡有 **6 個最危險**——
系統**猜了、而且答得像真的**（訪客可能就這樣接受）：
  22 `central shipped 20 earphones` → 聽成 shed → **回庫存查詢**（訪客要記出貨）
  40 `south shipped 22 jackets`     → 聽成 ship's → 同上
  72 `create a purchase order`      → 聽成 approaches → **回缺貨清單**
  73 `run the month end stocktake`  → 聽成 mouse and the start tick → **回滑鼠庫存**
  15 `electric mop inventory`       → 聽成 monk's → 回全店概覽
  74 `what files do you have`       → 聽成 file → 回庫存概覽

本支**只量測不修改**：對每句 ASR 輸出記錄
  ① 抽出的 keyword 在主檔的最高分（match_items score）
  ② clf 預測與信心
  ③ 最終 view
目的是看**危險案例與正常案例的分數分布能不能分開**——
分得開才立得起門檻；分不開就得換判準（不是硬調數字）。
（教訓來源：早上 `sports` vs `sports towel` 用絕對門檻兩邊都會錯，
 量了才知道判準是**分數分布**不是絕對值。）

用法（RPI5 ~/warehouse_v2_en）：python3 measure_confidence.py
"""
import asyncio
import json
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

WS = "wss://localhost:8002/ws?fast=1"
RESULT = Path.home() / "voice_poc" / "_read100_en_result.txt"
CORPUS = Path.home() / "voice_poc" / "read100_en.txt"

# 已知「系統猜了且答得像真的」的危險案例
DANGER = {15, 22, 40, 72, 73, 74}


def load_rows():
    """取每句的 (原句, ASR 輸出, 當時 view, PASS/FAIL)。"""
    rows = {}
    for line in RESULT.read_text(encoding="utf-8").splitlines():
        p = line.split("|")
        if len(p) >= 5 and p[0].isdigit():
            rows[int(p[0])] = (p[1], p[2], p[3], p[4])
    return rows


def kw_score(text):
    """ASR 輸出抽出的 keyword 在主檔的最高分。"""
    try:
        import warehouse as W
        import server as S
        kw = S._extract_sku_keyword(text) or ""
        if not kw:
            return "", 0
        m = W.match_items(kw)
        return kw, (m[0].get("score", 0) if m else 0)
    except Exception as e:
        return f"(err {e})", -1


def clf_pred(text):
    try:
        import intent_clf
        f, c = intent_clf.predict(text)
        return f, c
    except Exception:
        return "", 0.0


async def ask(text):
    import websockets
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    async with websockets.connect(WS, ssl=ctx) as ws:
        await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))
        while True:
            o = json.loads(await asyncio.wait_for(ws.recv(), 120))
            if o.get("type") == "done":
                r = o.get("result") or {}
                return r.get("view") or ""


def main():
    import warehouse as W
    W.init("seed_data.json")

    rows = load_rows()
    recs = []
    for n in sorted(rows):
        sent, asr, _, verdict = rows[n]
        if not asr or asr == "ASR空":
            continue
        kw, sc = kw_score(asr)
        cf, cc = clf_pred(asr)
        view = asyncio.run(ask(asr))
        misheard = (asr != sent)
        recs.append({"n": n, "sent": sent, "asr": asr, "kw": kw, "score": sc,
                     "clf": cf, "conf": cc, "view": view,
                     "misheard": misheard, "danger": n in DANGER})
        print(".", end="", flush=True)
    print()

    print("=" * 96)
    print("🚨 危險案例（系統猜了且答得像真的）")
    print(f"{'句':<4}{'kw 抽出':<24}{'主檔分':<8}{'clf':<20}{'信心':<7}{'view'}")
    print("-" * 96)
    for r in recs:
        if r["danger"]:
            print(f"{r['n']:<4}{r['kw'][:22]:<24}{r['score']:<8}"
                  f"{r['clf'][:18]:<20}{r['conf']:<7.2f}{r['view']}")

    print()
    print("✅ 正常案例（ASR 正確且 PASS）取樣 12 筆")
    print("-" * 96)
    ok = [r for r in recs if not r["misheard"] and not r["danger"]][:12]
    for r in ok:
        print(f"{r['n']:<4}{r['kw'][:22]:<24}{r['score']:<8}"
              f"{r['clf'][:18]:<20}{r['conf']:<7.2f}{r['view']}")

    print()
    print("=" * 96)
    d = [r["score"] for r in recs if r["danger"]]
    o = [r["score"] for r in recs if not r["misheard"] and not r["danger"]]
    dc = [r["conf"] for r in recs if r["danger"]]
    oc = [r["conf"] for r in recs if not r["misheard"] and not r["danger"]]
    if d and o:
        print(f"主檔分  危險案例 {min(d)}~{max(d)}（中位 {sorted(d)[len(d)//2]}）"
              f"｜正常 {min(o)}~{max(o)}（中位 {sorted(o)[len(o)//2]}）")
    if dc and oc:
        print(f"clf信心 危險案例 {min(dc):.2f}~{max(dc):.2f}"
              f"｜正常 {min(oc):.2f}~{max(oc):.2f}")
    print("⇒ 兩組分數若重疊 → 立不起門檻，要換判準")
    print("=" * 96)

    out = Path("/tmp/_confidence.json")
    out.write_text(json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"明細：{out}")


if __name__ == "__main__":
    main()
