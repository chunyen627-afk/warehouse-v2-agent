# -*- coding: utf-8 -*-
"""clf_dump.py — intent_clf 雙平台一致性認證（方案2 一次性證據，2026-07-16）

把守衛庫每句的 (label, conf) dump 成檔，WIN11 / RPI5 各跑一次後 diff：
- 全同 → 「clf 兩平台同結果」從信仰變證據，LLM-hit 子集策略成立
- 有差 → 差異句強制併入 RPI5 子集（並查 jieba / fasttext 版本差）

用法：
    python clf_dump.py [corpus檔] [輸出檔]     # 預設 regression_corpus.txt
    diff _clf_dump_win11.txt _clf_dump_rpi5.txt
"""
import io, sys
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
corpus = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "regression_corpus.txt"
out = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "_clf_dump.txt"

import intent_clf
intent_clf.load()

try:
    import jieba, fasttext
    ver = f"jieba={getattr(jieba, '__version__', '?')} fasttext={getattr(fasttext, '__version__', '?')}"
except Exception:
    ver = "ver=?"

lines = []
for raw in corpus.read_text(encoding="utf-8").splitlines():
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        continue
    parts = raw.split("|")
    if len(parts) < 2:
        continue
    sent = parts[1].strip()
    label, conf = intent_clf.predict(sent)
    # conf 取 6 位小數：既能抓到分流差異（0.8 門檻壓線），又不會被最後一兩個
    # bit 的無害雜訊灌爆 diff
    lines.append(f"{label}|{conf:.6f}|{sent}")

out.write_text(f"# clf dump — {ver}\n" + "\n".join(lines) + "\n", encoding="utf-8")
print(f"{len(lines)} 句 → {out.name}（{ver}）")
