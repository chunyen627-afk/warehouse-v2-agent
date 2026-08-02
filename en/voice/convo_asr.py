#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""convo_asr.py — 連續對話 × ASR 錯法（還沒測過的維度）。

先前各批測的都是**單句**：
  variant_probe  = 單句 × 拼字變形
  asr_replay     = 單句 × 真實 ASR 錯法
  session_en     = 連續對話 × 正確文字
本批補上缺的那格：**連續對話 × ASR 聽錯**——這才是展場實況
（訪客連續問，而且每一句都可能被聽錯）。

為什麼這格重要：多輪的脈絡（its / what about north / and south）
建立在**前一句被正確理解**之上。若第 1 句被聽錯導致鎖錯商品，
後面每一句都會錯 → **單句測不出的連鎖失效**。

情境設計：每個 scenario 是一位訪客的連續發問，
  句子刻意混入真實 ASR 錯法（取自 _rerun_en_v2*.txt 實際輸出）。
判準：最後一句的 view 是否符合預期（前面是鋪陳脈絡）。

用法（RPI5 ~/voice_poc）：python3 convo_asr.py
"""
import asyncio
import json
import ssl
import sys

WS = "wss://localhost:8002/ws?fast=1"

# (情境名, [(送出的句子, 說明)...], 最後一句期望的 view)
SCENARIOS = [
    ("A 鎖定商品後追問倉別（首句被聽錯）", [
        ("bluetooth earphone stock", "ASR 吞了複數 s"),
        ("what about north", "代稱追問"),
    ], "inventory_single"),

    ("B 首句正確、追問句被聽錯", [
        ("wireless mouse stock", "正確"),
        ("in south", "ASR 把 and south 聽成 in south"),
    ], "inventory"),

    ("C 代稱 + ASR 錯字", [
        ("bluetooth earphones stock", "正確"),
        ("is it below safety star", "safety stock→safety star"),
    ], "clarify"),

    ("D 寫入流程中途被聽錯", [
        ("north received 50 wireless mouse", "正確開卡"),
        ("cancel", "取消"),
    ], "item_cancelled"),

    ("E 連續三輪，中間被聽錯", [
        ("wireless mouse stock", "正確"),
        ("show me is movements", "its→is"),
        ("what about north", "代稱"),
    ], "inventory"),

    ("F 警示流程 + ASR 錯法", [
        ("set an error for yoga mat", "alert→error（batch2 已修）"),
        ("cancel", "取消"),
    ], "item_cancelled"),

    ("G 排行後追問", [
        ("show me the top sales", "sellers→sales（batch2 已修）"),
        ("what about north", "代稱追問"),
    ], "inventory"),

    ("H 寫入 + 撇號錯法 + 確認", [
        ("south ship's 22 down jackets", "shipped→ship's（batch3 已修）"),
        ("confirm", "口語確認"),
    ], "movement_done"),
]


def preload_asr_fix(texts):
    """批次套用 `_ASR_FIX_EN`（`/api/asr` 出口的正規化）。

    ⚠️ **不能省**：`_asr_normalize` 只掛在 `/api/asr`，走 WS 送純文字
    不會經過它 → 測到的是「打字路徑」，ASR 規則效果反映不出來
    （asr_replay 踩過同一個坑，救回率被低估 7 個百分點）。
    """
    import subprocess
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


async def run():
    import websockets
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    fix = preload_asr_fix([t for _, turns, _ in SCENARIOS for t, _ in turns])

    ok = bad = 0
    for name, turns, want in SCENARIOS:
        print(f"\n── {name} " + "─" * max(0, 52 - len(name)))
        # ⚠️ 一個 scenario 一條連線＝一位訪客，脈絡才會延續
        async with websockets.connect(WS, ssl=ctx) as ws:
            view = summary = ""
            for text, note in turns:
                sent_text = fix.get(text, text)
                await ws.send(json.dumps({"type": "chat", "text": sent_text},
                                         ensure_ascii=False))
                while True:
                    o = json.loads(await asyncio.wait_for(ws.recv(), 90))
                    if o.get("type") == "done":
                        r = o.get("result") or {}
                        view = r.get("view") or ""
                        summary = (r.get("summary") or "").replace("\n", " ")
                        break
                mk = "" if sent_text == text else f" [修正→{sent_text[:26]}]"
                print(f"   ▸ {text:44} → {view:18} ({note}){mk}")
        if want in view:
            ok += 1
            print(f"   ✅ 最終 view={view}")
        else:
            bad += 1
            print(f"   ❌ 最終 view={view}（期望 {want}）")
            print(f"      回答：{summary[:66]}")

    print()
    print("=" * 60)
    print(f"連續對話×ASR錯法 {ok + bad} 個情境：通過 {ok}、未過 {bad}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run())
