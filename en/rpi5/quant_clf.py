# -*- coding: utf-8 -*-
"""中文 intent_clf 量化瘦身：512MB → 幾 MB，準確率不該掉。

英文版實測 800MB → 6MB **準確率完全一樣**（99.68%），中文版比照辦理。

做法：fasttext 的 quantize()（product quantization）存成 .ftz，
      直接命名回 intent_clf.bin —— fasttext 會自動辨識格式，
      **推理端程式碼一行都不用改**。

⚠️ 先量化到暫存檔並比對準確率，**確認不掉才覆蓋**。
⚠️ 要 scp 上去執行（中文在 SSH heredoc 會被吃掉）。
"""
import shutil
import time
from pathlib import Path

import fasttext


def pred(model, text):
    """⚠️ 一律走底層 binding，**不要用 model.predict()**——
    fasttext ≤0.9.3 的 predict() 末行是 `np.array(probs, copy=False)`，
    在 numpy≥2 直接 ValueError（RPI5 是 numpy 2.4.4）。
    推理端 intent_clf.py 早就有同一套 workaround（2026-07-16 numpy2 事件）。
    """
    preds = model.f.predict(text.replace("\n", " ") + "\n", 1, 0.0, "strict")
    if not preds:
        return ""
    conf, label = preds[0]
    return label

D = Path.home() / "warehouse_v2"
SRC = D / "intent_clf.bin"
TMP = D / "_intent_clf_quant.ftz"
BAK = D / "intent_clf.bin.bak-fp32"

# 驗證集：**用守衛語料**（1122 句真實查詢，比訓練語料更貼近展場輸入）。
#   格式：`句子 | 期望view | 必含關鍵字`，這裡只取句子——我們比的是
#   「量化前後預測是否一致」，不需要意圖標籤。
guard = D / "regression_corpus.txt"
samples = []
if guard.exists():
    for ln in guard.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        s = ln.split("|")[0].strip()
        # 守衛檔可能有 `> 句子` 或 `分類 句子` 之類前綴，取得乾淨句子
        s = s.lstrip("> ").strip()
        if s and not s.startswith("["):
            samples.append(s)
print(f"驗證樣本 {len(samples)} 句（來源：守衛語料）")

print("載入原模型…")
t0 = time.time()
m = fasttext.load_model(str(SRC))
print(f"  {SRC.stat().st_size/1e6:.1f} MB, {time.time()-t0:.1f}s")

# fasttext 對含換行的輸入會報錯，統一清一次
samples = [s.replace("\n", " ") for s in samples]
before = [pred(m, s) for s in samples] if samples else []

print("量化中（會跑一陣子）…")
t0 = time.time()
m.quantize(qnorm=True, retrain=False, cutoff=100000)
print(f"  完成 {time.time()-t0:.1f}s")
m.save_model(str(TMP))
print(f"  量化後 {TMP.stat().st_size/1e6:.1f} MB")

print("重新載入量化模型驗證…")
mq = fasttext.load_model(str(TMP))
after = [pred(mq, s) for s in samples] if samples else []

if samples:
    same = sum(1 for a, b in zip(before, after) if a == b)
    rate = same / len(samples)
    print(f"\n預測一致率 {same}/{len(samples)} = {rate*100:.2f}%")
    diff = [(s, a, b) for s, a, b in zip(samples, before, after) if a != b][:8]
    if diff:
        print("不一致樣本（前 8）：")
        for s, a, b in diff:
            print(f"  {s[:34]:36} {a} → {b}")
    if rate < 0.99:
        print("\n❌ 一致率 < 99%，**不覆蓋**，請人工判斷")
        raise SystemExit(1)

shutil.copy2(SRC, BAK)
shutil.move(str(TMP), str(SRC))
print(f"\n✅ 已覆蓋 {SRC}（原檔備份 {BAK.name}）")
print(f"   {BAK.stat().st_size/1e6:.1f} MB → {SRC.stat().st_size/1e6:.1f} MB")
