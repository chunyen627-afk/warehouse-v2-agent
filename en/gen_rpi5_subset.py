# -*- coding: utf-8 -*-
"""gen_rpi5_subset.py — 從 RPI5 實跑 log 產「LLM-hit 子集」（方案2，2026-07-16 user 定案）

原理：平台分歧只可能發生在進 LLM 的句子（llama.cpp 浮點；確定性層兩平台跑同一份
Python code 不可能分岔，clf 同 bin 同結果）。RPI5 全量因此只需重驗 LLM-hit 子集，
時間砍 6-7 成。動 LLM 相關層（fuzzy/校正/rewrite/prompt/keyword 抽取）或展前 →
仍跑真全量。

用法：
    # 1) 先撈 RPI5 實跑 log（一次全量後）：
    #    ssh ... "journalctl -u warehouse-v2.service --since 'HH:MM' --no-pager \
    #       | grep -aE 'User vid=|vid=[0-9]+ model='" > _r40_rpi5_routing.log
    # 2) 產子集：
    python gen_rpi5_subset.py _r40_rpi5_routing.log
    # → _rpi5_subset_guard.txt（regression_corpus 中 LLM-hit 的句子，格式同 corpus）
    # → _rpi5_subset_sweep.txt（_sweep_r31 中 LLM-hit 的句子）
    # 之後 RPI5 快驗：python3 regression_ws.py --rpi5 --file _rpi5_subset_guard.txt
"""
import io, re, sys
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

HERE = Path(__file__).parent
log_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "_r40_rpi5_routing.log"

# ── 解析 log：vid → (text, llm_hit) ──
user_re = re.compile(r"User vid=(\d+): (.*)$")
model_re = re.compile(r"vid=(\d+) model=")
texts, llm_vids = {}, set()
for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
    m = user_re.search(line)
    if m:
        vid = int(m.group(1))
        # 同 vid 多輪（convo 劇本）：只記首句、任一輪進 LLM 都算 hit
        texts.setdefault(vid, m.group(2).strip())
        continue
    m = model_re.search(line)
    if m:
        llm_vids.add(int(m.group(1)))

hit_texts = {t for v, t in texts.items() if v in llm_vids}
all_texts = set(texts.values())
print(f"log 句數（vid 數）: {len(texts)}，進 LLM: {len(llm_vids)} "
      f"({len(llm_vids)/max(len(texts),1)*100:.0f}%)")

# ── 對照 corpus / sweep，抽出 LLM-hit 行 ──
for src, out in (("regression_corpus.txt", "_rpi5_subset_guard.txt"),
                 ("_sweep_r31.txt", "_rpi5_subset_sweep.txt")):
    lines_out, total, hit, unseen = [], 0, 0, 0
    for raw in (HERE / src).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        total += 1
        sent = parts[1]
        if sent in hit_texts:
            hit += 1
            lines_out.append(raw)
        elif sent not in all_texts:
            # log 裡沒出現過（新加句/跑批時不存在）→ 保守起見納入子集
            unseen += 1
            lines_out.append(raw + "  # unseen→保守納入")
    (HERE / out).write_text(
        f"# LLM-hit 子集（自動產生 by gen_rpi5_subset.py，來源 {src}）\n"
        f"# 進LLM {hit}/{total}（{hit/max(total,1)*100:.0f}%）+ 未見過保守納入 {unseen}\n"
        + "\n".join(lines_out) + "\n",
        encoding="utf-8")
    print(f"{src}: {total} 句 → 子集 {hit} + unseen {unseen} → {out}")
