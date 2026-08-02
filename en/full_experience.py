#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""full_experience.py — 100 句完整體驗檢視（2026-08-02）。

user 要求：「把我錄的 100 句都丟進 WS 看渲染，然後看能不能看到語音辨識後的
文字，整個體驗一下；另外看錯誤是否真的都容錯啟動過了」

做三件事：
① **走真實語音路徑**——先套 `_asr_normalize`（`_ASR_FIX_EN` 的載體，
   只掛在 `/api/asr`），再送 WS。少了這步測到的是打字路徑（坑 26）。
② 記錄**訪客實際看到的**：送出的文字（=藍色氣泡內容）+ 系統回答。
③ 統計 **11 個容錯層**各觸發幾次、救回哪幾句——
   驗證「錯誤是否真的都容錯啟動過了」。

容錯層清單（server.py 的 log 標記）：
  asr-fix-en／confirm-asr-rescue／ctx-tf／ctx-tf-en／en-admin／en-comma／
  en-funcword／en-typo-gate／long-gate／mpw-gate-en／qty-decimal-en

用法（RPI5 ~/warehouse_v2_en）：python3 full_experience.py
"""
import asyncio
import json
import re
import ssl
import subprocess
import sys
from pathlib import Path

WS = "wss://localhost:8002/ws?fast=1"
VP = Path.home() / "voice_poc"
RESULT = VP / "_read100_en_result.txt"
CORPUS = VP / "read100_en.txt"

LAYERS = ["asr-fix-en", "confirm-asr-rescue", "ctx-tf-en", "ctx-tf", "en-admin",
          "en-comma", "en-funcword", "en-typo-gate", "long-gate",
          "mpw-gate-en", "qty-decimal-en"]


def load_rows():
    rows = {}
    for line in RESULT.read_text(encoding="utf-8").splitlines():
        p = line.split("|")
        if len(p) >= 5 and p[0].isdigit():
            rows[int(p[0])] = (p[1], p[2])
    return rows


def load_expect():
    exp = {}
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        p = line.split("|")
        if len(p) >= 3 and p[0].isdigit():
            exp[int(p[0])] = (p[2], p[3] if len(p) > 3 else "")
    return exp


def batch_asr_fix(texts):
    """批次套 _asr_normalize（真實語音路徑必經）。"""
    code = ("import sys,os,json;"
            "os.chdir(os.path.expanduser('~/warehouse_v2_en'));"
            "sys.path.insert(0,'.');import server;"
            "print(json.dumps({t: server._asr_normalize(t) "
            "for t in json.loads(sys.argv[1])}, ensure_ascii=False))")
    try:
        r = subprocess.run(["python3", "-c", code,
                            json.dumps(list(texts), ensure_ascii=False)],
                           capture_output=True, text=True, timeout=300)
        for ln in (r.stdout or "").splitlines():
            if ln.startswith("{"):
                return json.loads(ln)
    except Exception as e:
        print(f"(ASR 正規化預載失敗: {e})", file=sys.stderr)
    return {}


def journal_since(sec):
    try:
        r = subprocess.run(
            ["journalctl", "-u", "warehouse-v2-en", "--since", f"-{sec}s",
             "--no-pager"], capture_output=True, text=True, timeout=60)
        return r.stdout or ""
    except Exception:
        return ""


async def ask(ws, text):
    await ws.send(json.dumps({"type": "chat", "text": text}, ensure_ascii=False))
    while True:
        o = json.loads(await asyncio.wait_for(ws.recv(), 120))
        if o.get("type") == "done":
            r = o.get("result") or {}
            return (r.get("view") or "",
                    (r.get("summary") or "").replace("\n", " "))


async def main():
    import websockets
    rows = load_rows()
    exp = load_expect()
    fix = batch_asr_fix([a for _, a in rows.values() if a and a != "ASR空"])

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    recs = []
    print("=" * 100)
    print("100 句完整體驗（訪客看到的藍色氣泡文字 → 系統回答）")
    print("=" * 100)

    async with websockets.connect(WS, ssl=ctx) as ws:
        for n in sorted(rows):
            sent, asr = rows[n]
            if not asr or asr == "ASR空":
                continue
            shown = fix.get(asr, asr)          # 訪客看到的（=送進系統的）
            view, summ = await ask(ws, shown)
            want, kw = exp.get(n, ("", ""))
            hit = True
            if want == "*":
                if view == "error" or not view:
                    hit = False
            elif want and want not in view:
                hit = False
            if hit and kw and kw not in summ:
                hit = False
            recs.append({"n": n, "sent": sent, "asr": asr, "shown": shown,
                         "view": view, "summ": summ, "hit": hit,
                         "fixed": shown != asr})
            mark = "✅" if hit else "❌"
            fx = "  🔧修正" if shown != asr else ""
            print(f"{mark} [{n:>3}] {shown[:46]:<48}→ {view}{fx}")
            if not hit:
                print(f"          原句：{sent[:60]}")
                print(f"          回答：{summ[:66]}")

    ok = sum(1 for r in recs if r["hit"])
    print()
    print("=" * 100)
    print(f"通過 {ok}/{len(recs)} = {ok*100//max(len(recs),1)}%"
          f"｜ASR 出口修正生效 {sum(1 for r in recs if r['fixed'])} 句")
    print("=" * 100)

    # 容錯層觸發統計
    jl = journal_since(60 * 25)
    print()
    print("🔧 容錯層觸發統計（本輪）")
    print("-" * 100)
    total = 0
    for lay in LAYERS:
        hits = re.findall(rf"\[{re.escape(lay)}\][^\n]*", jl)
        total += len(hits)
        status = f"{len(hits):>3} 次" if hits else "  未觸發"
        print(f"  {lay:<22} {status}")
        for h in hits[:3]:
            print(f"        {h.split('] ',1)[-1][:78]}")
    print("-" * 100)
    print(f"合計觸發 {total} 次")

    Path("/tmp/_full_exp.json").write_text(
        json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")
    print("明細：/tmp/_full_exp.json")


if __name__ == "__main__":
    asyncio.run(main())
