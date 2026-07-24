"""
訓練英文版 FastText 意圖分類器（EN build）
從 training_data_en.jsonl 產生訓練格式，輸出 en/intent_clf.bin
與中文版差異：
  - 輸入用純英文語料 training_data_en.jsonl
  - **不用 jieba**（英文本來就以空白分詞；jieba 對英文只會原樣切、多餘）
  - 輸出到 en/ 自己的 intent_clf.bin（不覆蓋中文版）
用法: python train_intent_clf_en.py
"""
import json, pathlib, tempfile, random, re

ROOT   = pathlib.Path(__file__).parent
JSONL  = ROOT / "training_data_en.jsonl"
OUT    = ROOT / "intent_clf.bin"                 # EN：放 en/ 根目錄（server 讀這裡）
REPORT = ROOT / "intent_clf_report_en.txt"

# judge_cause_found 是 search_log 的子步驟，合併進 search_log（同中文版）
MERGE = {"judge_cause_found": "search_log"}

def to_ft_label(name: str) -> str:
    return "__label__" + MERGE.get(name, name)

def norm_en(text: str) -> str:
    """英文正規化：小寫 + 標點與詞分開（FastText 用空白分詞，英文天生就有）。"""
    t = text.strip().lower()
    t = re.sub(r"([?!,.])", r" \1 ", t)      # 標點獨立成 token
    t = re.sub(r"\s+", " ", t).strip()
    return t

# ── 讀資料 ──────────────────────────────────────────────────────────────────
records = []
for line in open(JSONL, encoding="utf-8"):
    d = json.loads(line)
    label = to_ft_label(d["tool_name"])
    text  = norm_en(d["user_content"])
    records.append(f"{label} {text}")

random.seed(42)
random.shuffle(records)

split = int(len(records) * 0.9)
train_records = records[:split]
valid_records = records[split:]

# ── 上採樣少數類別，讓各類別訓練數量接近最大類別 ──────────────────────────
label_groups: dict[str, list[str]] = {}
for r in train_records:
    lbl = r.split(" ", 1)[0]
    label_groups.setdefault(lbl, []).append(r)

max_count = max(len(v) for v in label_groups.values())
balanced = []
for lbl, rows in label_groups.items():
    balanced.extend(rows)
    need = max_count - len(rows)
    if need > 0:
        balanced.extend(random.choices(rows, k=need))
random.shuffle(balanced)

print(f"[data] 原始 {len(records)} 筆 / train {len(train_records)} / valid {len(valid_records)}")
print(f"[data] 類別數 {len(label_groups)}，上採樣後 train {len(balanced)} 筆")
for lbl, rows in sorted(label_groups.items(), key=lambda x: -len(x[1])):
    print(f"       {lbl.replace('__label__',''):24s} {len(rows)}")

# ── 訓練 ────────────────────────────────────────────────────────────────────
import fasttext

with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
    f.write("\n".join(balanced))
    train_path = f.name
with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
    f.write("\n".join(valid_records))
    valid_path = f.name

print("\n[train] fasttext.train_supervised ...")
model = fasttext.train_supervised(
    input        = train_path,
    epoch        = 25,
    lr           = 0.5,
    wordNgrams   = 2,
    dim          = 100,
    loss         = "softmax",
    minCount     = 1,
    verbose      = 1,
)

n, p, r = model.test(valid_path)
print(f"[valid] n={n} precision={p:.4f} recall={r:.4f}")

# per-label 準確率
from collections import defaultdict
total, correct = defaultdict(int), defaultdict(int)
for line in valid_records:
    parts = line.split(" ", 1)
    true_label = parts[0]
    text = parts[1] if len(parts) > 1 else ""
    pred = model.predict(text)[0]
    pred_label = pred[0] if pred else ""
    total[true_label] += 1
    if pred_label == true_label:
        correct[true_label] += 1

report_lines = [f"EN intent_clf  valid n={n} precision={p:.4f} recall={r:.4f}", ""]
report_lines.append("Per-label accuracy on valid set:")
for lbl in sorted(total, key=lambda x: -total[x]):
    acc = correct[lbl] / total[lbl] if total[lbl] else 0
    report_lines.append(f"  {lbl.replace('__label__',''):25} {correct[lbl]:3}/{total[lbl]:3}  {acc:.0%}")

model.save_model(str(OUT))
REPORT.write_text("\n".join(report_lines), encoding="utf-8")
print("\n".join(report_lines))
print(f"\n[out] {OUT}  ({OUT.stat().st_size/1024/1024:.0f} MB)")
print(f"[out] {REPORT}")
