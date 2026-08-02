#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""long_convo_en.py — 長對話脈絡漂移（10+ 輪，2026-08-02）。

先前多輪測試最多 3 輪（session_en 9 句但分屬不同商品、convo_asr 2-3 輪）。
展場訪客一站可能問十幾句，要抓的是**只有長對話才會累積出來的問題**：
  ① 脈絡漂移——第 12 輪的「它」還記得第 2 輪鎖的商品嗎？該記得嗎？
  ② 舊脈絡污染——換了商品之後，代稱還黏在舊的上
  ③ 狀態殘留——開過確認卡又取消，之後的查詢會不會受影響
  ④ 中途插入無關句（閒聊、搗蛋）後脈絡是否還在

判準：每一步標註期望（view 或 summary 關鍵字），逐步驗證。
  ⚠️ view 可能有 LLM 不確定性（實測 `and south` 8 次有 1 次判 movement），
  所以能用 summary 關鍵字驗的優先用關鍵字。

用法（RPI5 ~/warehouse_v2_en）：python3 long_convo_en.py
"""
import asyncio
import json
import ssl

WS = "wss://localhost:8002/ws?fast=1"

# (句子, 期望 summary 關鍵字 或 "", 說明)
SCRIPT_A = [
    ("bluetooth earphones stock", "Earphones", "① 鎖定耳機"),
    ("what about north", "Earphones", "② 代稱→北倉"),
    ("how about central", "Earphones", "③ 代稱→中倉"),
    ("show me its movements", "Earphones", "④ 代稱→進出"),
    ("wireless mouse stock", "Mouse", "⑤ **換商品**"),
    ("and south", "Mouse", "⑥ 代稱應跟到滑鼠，不可還是耳機"),
    ("what is running low", "", "⑦ 插入無關查詢"),
    ("what about north", "", "⑧ 插入後的代稱（不強制，只驗不 error）"),
    ("yoga mat stock", "Yoga Mat", "⑨ 再換商品"),
    ("is it below safety stock", "Yoga Mat", "⑩ 代稱→安全庫存"),
    ("hello", "", "⑪ 閒聊插入"),
    ("show me its movements", "Yoga Mat", "⑫ 閒聊後代稱仍應是瑜珈墊"),
]

SCRIPT_B = [
    ("trash bags stock", "Trash Bags", "① 鎖定垃圾袋"),
    ("north received 50 trash bags", "Trash Bags", "② 開進貨卡"),
    ("cancel", "", "③ 取消"),
    ("what about south", "Trash Bags", "④ 取消後代稱仍在？"),
    ("facial tissue stock", "Tissue", "⑤ 換商品"),
    ("transfer 10 from north to south", "Tissue", "⑥ 代稱式調撥"),
    ("cancel", "", "⑦ 取消"),
    ("best sellers this week", "", "⑧ 插入排行"),
    ("steam iron stock", "Steam Iron", "⑨ 換商品"),
    ("and central", "Steam Iron", "⑩ 代稱→中倉"),
    ("what can you do", "", "⑪ 問功能（導覽）"),
    ("show me its movements", "Steam Iron", "⑫ 導覽後代稱仍應是蒸氣熨斗"),
]


async def run_script(name, steps):
    import websockets
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ok = bad = 0
    print(f"\n{'=' * 70}\n{name}\n{'=' * 70}")
    # ⚠️ 整串共用一條連線＝同一位訪客，脈絡才會累積
    async with websockets.connect(WS, ssl=ctx) as ws:
        for text, kw, note in steps:
            await ws.send(json.dumps({"type": "chat", "text": text},
                                     ensure_ascii=False))
            view = summ = ""
            while True:
                o = json.loads(await asyncio.wait_for(ws.recv(), 120))
                if o.get("type") == "done":
                    r = o.get("result") or {}
                    view = r.get("view") or ""
                    summ = (r.get("summary") or "").replace("\n", " ")
                    break
            prob = []
            if view == "error" or not view:
                prob.append("error/空")
            if kw and kw.lower() not in summ.lower():
                prob.append(f"缺 {kw!r}（脈絡漂移？）")
            if prob:
                bad += 1
                print(f"  ❌ {note:34} {text[:34]:36} → {view}")
                print(f"     {'; '.join(prob)}")
                print(f"     回答：{summ[:66]}")
            else:
                ok += 1
                print(f"  ✅ {note:34} {text[:34]:36} → {view}")
    return ok, bad


async def main():
    t_ok = t_bad = 0
    for name, steps in (("劇本 A：換商品 × 插入無關句 × 閒聊", SCRIPT_A),
                        ("劇本 B：確認卡取消 × 換商品 × 導覽", SCRIPT_B)):
        ok, bad = await run_script(name, steps)
        t_ok += ok
        t_bad += bad
    print()
    print("=" * 70)
    print(f"長對話 {t_ok + t_bad} 步：通過 {t_ok}、未過 {t_bad}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
