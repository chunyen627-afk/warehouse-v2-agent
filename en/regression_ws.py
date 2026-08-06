"""累積式 WS 回歸題庫 — 每輪口語測試的題目測完「不刪」，全部併進來。

背景（2026-07-03）：第 1~8 輪各 100 題口語測試當時測完即刪，後續修正
（rewrite 刪除 / guide 排除詞 / RCA 詞擴充…）理論上可能打壞舊輪已過的題，
卻無從驗證。從第 9 輪起所有題目累積在 regression_corpus.txt，修完任何
dispatch / rewrite / 關鍵字清單，跑這支全量驗證。

用法：
    python regression_ws.py            # 跑全部（commit 前必跑）
    python regression_ws.py --smoke    # 快篩：每個註解區塊只取首句（~186 句、
                                       #   ~5 分鐘），開發迭代用；區塊=corpus 裡
                                       #   一段 # 註解後的連續句子=一個 bug/機制，
                                       #   首句即該規則路徑的代表守衛
    python regression_ws.py mv tf      # 只跑指定類別

題庫格式（regression_corpus.txt）：
    類別|題目               # 井號開頭為註解
    類別|題目|內容關鍵字     # 第三欄（選填）：回答 summary 必須包含該字串
內容欄是「view 對但內容錯」級 bug 的守衛（第8輪起）——例如「瑜珈墊安全庫存
加20」view=config_confirm 永遠對，但影響範圍曾是全部商品 183 項，只有驗
summary 含「瑜珈墊」才擋得住回退。
類別 → 判定規則見 ACCEPT。
"""
import asyncio, json, ssl, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import websockets
from pathlib import Path

# --rpi5：在 RPI5 本機跑全量回歸（跟訪客同一條路）
# ⚠️ EN build：英文版服務在 **8002**（8001 是中文版）。原本沿用 8001 →
#    整批守衛其實測到中文版、回答全中文、must 斷言全 FAIL（2026-07-25 抓到）。
if "--rpi5" in sys.argv:
    sys.argv.remove("--rpi5")
    WS_URI = 'wss://localhost:8002/ws?fast=1'
    SSL_CTX = ssl.create_default_context()
    SSL_CTX.check_hostname = False
    SSL_CTX.verify_mode = ssl.CERT_NONE
else:
    WS_URI = 'ws://localhost:8000/ws?fast=1'
    SSL_CTX = None
CORPUS = Path(__file__).parent / "regression_corpus.txt"
# --file X：跑替代語料（r31 短句掃蕩 _sweep_r31.txt 等，格式同 corpus）
if "--file" in sys.argv:
    _fi = sys.argv.index("--file")
    CORPUS = Path(__file__).parent / sys.argv[_fi + 1]
    del sys.argv[_fi:_fi + 2]

ACCEPT = {
    "mv":    lambda v: v == "movement_confirm",
    "mvq":   lambda v: v == "clarify",   # r81：缺數量寫入 → 追問幾件
    "tf":    lambda v: v == "transfer_confirm",
    "tf_insuff": lambda v: v == "error",   # 來源倉庫存不足 → 擋下是正確行為
    "tf_clarify": lambda v: v in ("transfer_confirm", "clarify"),  # 單倉調貨 → 問來源倉
    "inv":   lambda v: v in ("inventory", "inventory_single", "clarify", "low_stock"),
    "low":   lambda v: v == "low_stock",
    "exp":   lambda v: v == "expiring",
    "hot":   lambda v: v == "hot_items",
    "rca":   lambda v: v == "agent_rca",
    "cfg":   lambda v: v in ("config_confirm", "config", "config_read"),
    # config 諮詢/缺值/搗蛋負數 → clarify 友善追問（不 crash 不亂寫）
    "cfg_clarify": lambda v: v in ("clarify", "config_read", "guide"),
    "rel":   lambda v: v in ("related", "related_help", "related_empty"),
    "mvt":   lambda v: v == "movement",
    # vague：模糊指涉。若**明確指到單一商品**（'the earphones' 只有一款
    #   藍牙耳機），直答單品才對——反問「你是指哪個」反而是明知故問，
    #   違反不猜原則的反面（該猜時就猜）。故 inventory_single 也算通過。
    "vague": lambda v: v in ("clarify", "guide", "rejected", "inventory",
                             "inventory_single"),
    "noex":  lambda v: v in ("clarify", "rejected", "error", "related_empty", "guide",
                              "expiring_empty"),
    "any":   lambda v: v not in ("error", "clarify", "rejected"),
    # 2026-08-06 補課批（ZH 同款）：排程句的正確結果有兩種，都不可判紅
    "sched":  lambda v: v in ("schedule_confirm", "schedule_list", "clarify"),
    "schedq": lambda v: v == "clarify",
    # 訪客閒聊/搗蛋防禦（第17輪）：優雅拒絕/引導/追問，不可幻覺商品或開卡
    "chat":   lambda v: v in ("rejected", "guide", "clarify"),
    "guidey": lambda v: v in ("guide", "rejected", "clarify"),
    "probe":  lambda v: v in ("rejected", "guide", "clarify", "error"),
    # 半倉管（第19輪）：問有沒有賣/多少錢，顯示庫存是好回答；只擋寫入確認卡
    "semi":   lambda v: v not in ("movement_confirm", "transfer_confirm",
                                   "config_confirm", "po_confirm", "alert_confirm",
                                   "schedule_confirm", "item_list", "item_delete_denied",
                                   "error"),
}


async def _q_once(text):
    llm_hit = False
    async with websockets.connect(WS_URI, ssl=SSL_CTX, max_size=None) as ws:
        await ws.send(json.dumps({'type': 'chat', 'text': text}, ensure_ascii=False))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=45)
            msg = json.loads(raw)
            if msg.get('type') == 'perf' and msg.get('mode') == 'llm':
                llm_hit = True   # 方案2側錄：這句進了 LLM（RPI5 子集成員）
            if msg.get('type') == 'done':
                r = msg.get('result', {})
                r['_llm_hit'] = llm_hit
                return r


async def _q(text, tries=3):
    """握手逾時會自動重連。

    server 同時被兩個測試（或劇本）打時，WebSocket opening handshake 偶爾逾時，
    產生「WS error: timed out during opening handshake」的幽靈 FAIL——每次逾時
    的句子還都不一樣，看起來活像真 bug。追過三次都不是。連線層的抖動不該算進
    回歸結果，重連即可。
    """
    for i in range(tries):
        try:
            return await _q_once(text)
        except Exception:
            if i == tries - 1:
                raise
            await asyncio.sleep(1.5 * (i + 1))


def main():
    smoke = "--smoke" in sys.argv
    if smoke:
        sys.argv.remove("--smoke")
    only = set(sys.argv[1:])
    qa = []
    in_block = False   # smoke 模式：每個註解區塊只收首句
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            in_block = False
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        cat, text = parts[0], parts[1]
        must = parts[2] if len(parts) >= 3 else ""
        if cat not in ACCEPT or not text:
            continue
        if only and cat not in only:
            continue
        if smoke and in_block:
            continue          # 同區塊第 2 句起跳過
        in_block = True
        qa.append((cat, text, must))
    if smoke:
        print(f"[smoke] 快篩模式：{len(qa)} 句（每區塊首句；commit 前仍須跑全量）")

    total = ok = 0
    fails = []
    llm_lines = []   # 方案2側錄：進 LLM 的句子 → 另存子集檔供 RPI5 快驗
    for cat, text, must in qa:
        total += 1
        try:
            r = asyncio.run(_q(text))
        except Exception as e:
            fails.append((cat, text, f"WS error: {e}", ""))
            continue
        view = r.get("view", "?")
        summary = r.get("summary") or ""
        if r.get("_llm_hit"):
            llm_lines.append(f"{cat}|{text}" + (f"|{must}" if must else ""))
        if not ACCEPT[cat](view):
            fails.append((cat, text, view, summary[:70]))
        elif must and must not in summary:
            fails.append((cat, text, f"{view}(內容缺「{must}」)", summary[:70]))
        else:
            ok += 1

    print(f"\n{'='*66}\n累積回歸: {ok}/{total} ({ok/total*100:.1f}%)\n")
    for cat, text, view, s in fails:
        print(f"  FAIL [{cat}] {text}\n       -> view={view} | {s}")

    # 全量（非 smoke、非類別篩選）才寫子集，樣本才完整。
    # 日常 RPI5 快驗：python3 regression_ws.py --rpi5 --file <子集檔>
    # 動 LLM 相關層（fuzzy/校正/rewrite/prompt/keyword 抽取）或展前 → 仍跑全量。
    if llm_lines and not smoke and not only:
        sub = CORPUS.with_name(CORPUS.stem + "_llmsub.txt")
        sub.write_text(
            f"# LLM-hit 子集（自動產生：{CORPUS.name} 全量跑完側錄，"
            f"{len(llm_lines)}/{total} 句進過 LLM）\n" + "\n".join(llm_lines) + "\n",
            encoding="utf-8")
        print(f"\n[llm-subset] {len(llm_lines)}/{total} 句 → {sub.name}")


if __name__ == "__main__":
    main()
