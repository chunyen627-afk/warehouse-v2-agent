#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""error_ux.py — 錯誤路徑的**訪客體驗**（2026-08-02，全新角度）。

前面測的都是「正常流程對不對」「惡意輸入擋不擋」，
還沒測過：**系統回錯誤時，訪客看到的訊息能不能讓他自己走下去。**

展場情境：訪客不會 debug，看到看不懂的錯誤就放棄／叫人。
所以錯誤訊息的標準比正確答案更嚴：
  ① 不可出現技術術語（traceback／None／null／exception／KeyError／HTTP 5xx）
  ② 不可只說「失敗」而不說**為什麼**
  ③ 要給**下一步**（怎麼改講法／有哪些選項）
  ④ 不可洩漏內部路徑或欄位名（server.py／sku_id／warehouse_data/）

測的錯誤情境（訪客真的會遇到的）：
  出貨超過庫存、調撥超過庫存、查不存在的商品、查不存在的倉別、
  空輸入、超長輸入、只有標點、只有數字、只有動詞沒受詞。

⚠️ 純查詢/被擋的路徑，不改資料。

用法（RPI5 ~/warehouse_v2_en）：python3 error_ux.py
"""
import asyncio
import json
import re
import ssl

WS = "wss://localhost:8002/ws?fast=1"

# 技術術語黑名單——出現在訪客可見文字裡就是 FAIL
LEAK = re.compile(
    r"traceback|exception|keyerror|valueerror|typeerror|attributeerror|"
    r"\bnone\b|\bnull\b|undefined|nan\b|"
    r"server\.py|tools_v2|warehouse_data/|seed_data|sku_id|"
    r"http\s*5\d\d|internal error|stack|__|\bdict\b|\blist index\b",
    re.I)

CASES = [
    ("① 出貨超過庫存", "north shipped 999 wireless mouse"),
    ("② 調撥超過庫存", "transfer 999 wireless mouse from north to south"),
    ("③ 不存在的商品", "how many unicorn horns do we have"),
    ("④ 不存在的倉別", "east warehouse stock"),
    ("⑤ 只有標點", "???"),
    ("⑥ 只有數字", "12345"),
    ("⑦ 只有動詞沒受詞", "transfer"),
    ("⑧ 超長輸入", "please tell me " * 30 + "the stock"),
    ("⑨ 亂打鍵盤", "asdkjhaskjdhaskjdh"),
    ("⑩ 混合亂碼", "st0ck  #$%^ wireless m0use"),
    ("⑪ 出貨到不存在的倉", "transfer 10 wireless mouse from north to east"),
    ("⑫ 商品名超長", "stock of " + "a" * 120),
]


async def main():
    import websockets
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    print("=" * 84)
    print("錯誤路徑的訪客體驗（訊息要看得懂、有原因、有下一步、不洩漏內部）")
    print("=" * 84)

    ok_n = bad_n = 0
    async with websockets.connect(WS, ssl=ctx) as ws:
        for name, sent in CASES:
            await ws.send(json.dumps({"type": "chat", "text": sent},
                                     ensure_ascii=False))
            view = summ = ""
            try:
                while True:
                    o = json.loads(await asyncio.wait_for(ws.recv(), 120))
                    if o.get("type") == "done":
                        r = o.get("result") or {}
                        view = r.get("view") or ""
                        summ = (r.get("summary") or "").replace("\n", " ")
                        break
            except Exception as e:
                print(f"  ❌ {name:20} 例外 {e!r}")
                bad_n += 1
                continue

            prob = []
            leak = LEAK.search(summ)
            if leak:
                prob.append(f"洩漏技術術語 {leak.group(0)!r}")
            if not summ.strip():
                prob.append("空回答")
            elif len(summ.strip()) < 12:
                prob.append(f"回答過短（{len(summ.strip())} 字）")
            # 錯誤類 view 要給出路（含建議/例子/問句）
            if view in ("error",) and not re.search(
                    r"\?|e\.g\.|try|please|say |check|instead", summ, re.I):
                prob.append("error 但沒給下一步")

            if prob:
                bad_n += 1
                print(f"  ❌ {name:20} view={view}")
                print(f"       {'; '.join(prob)}")
                print(f"       回答：{summ[:88]}")
            else:
                ok_n += 1
                print(f"  ✅ {name:20} view={view:18} {summ[:52]}")

    print()
    print("=" * 84)
    print(f"錯誤體驗 {ok_n + bad_n} 案：通過 {ok_n}、未過 {bad_n}")
    print("=" * 84)


if __name__ == "__main__":
    asyncio.run(main())
