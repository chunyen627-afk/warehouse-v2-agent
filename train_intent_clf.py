"""
訓練 FastText 意圖分類器
從 training_data.jsonl 產生訓練格式，輸出 test/intent_clf.bin
用法: python train_intent_clf.py
"""
import json, pathlib, tempfile, random, re

ROOT   = pathlib.Path(__file__).parent
JSONL  = ROOT / "training_data.jsonl"
OUT    = ROOT / "test" / "intent_clf.bin"
REPORT = ROOT / "test" / "intent_clf_report.txt"

# judge_cause_found 是 search_log 的子步驟，合併進 search_log
MERGE = {"judge_cause_found": "search_log"}

def to_ft_label(name: str) -> str:
    return "__label__" + MERGE.get(name, name)

def char_ngram(text: str) -> str:
    """jieba 分詞（FastText 需要空白分隔的 token）"""
    import jieba
    jieba.setLogLevel(60)  # 靜音
    return " ".join(jieba.cut(text.strip()))

# ── 讀資料 ──────────────────────────────────────────────────────────────────
records = []
for line in open(JSONL, encoding="utf-8"):
    d = json.loads(line)
    label = to_ft_label(d["tool_name"])
    text  = char_ngram(d["user_content"])
    records.append(f"{label} {text}")

random.seed(42)
random.shuffle(records)

split = int(len(records) * 0.9)
train_records = records[:split]
valid_records = records[split:]

# ── 上採樣少數類別，讓各類別訓練數量接近最大類別 ──────────────────────────
from collections import Counter
label_groups: dict[str, list[str]] = {}
for r in train_records:
    lbl = r.split(" ", 1)[0]
    label_groups.setdefault(lbl, []).append(r)

max_count = max(len(v) for v in label_groups.values())
balanced = []
for lbl, rows in label_groups.items():
    # 少數類別重複取樣到 max_count
    multiplier = max_count // len(rows)
    remainder  = max_count % len(rows)
    balanced += rows * multiplier + random.sample(rows, remainder)

random.shuffle(balanced)
train_records = balanced
print(f"After balancing → Train: {len(train_records)}  Valid: {len(valid_records)}")

print(f"Train: {len(train_records)}  Valid: {len(valid_records)}")

# ── 寫暫存訓練檔 ────────────────────────────────────────────────────────────
import tempfile, os
with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", encoding="utf-8",
                                 delete=False) as f:
    train_path = f.name
    f.write("\n".join(train_records))

with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", encoding="utf-8",
                                 delete=False) as f:
    valid_path = f.name
    f.write("\n".join(valid_records))

# ── 訓練 ────────────────────────────────────────────────────────────────────
try:
    import fasttext
except ImportError:
    print("安裝 fasttext...")
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "fasttext-wheel"])
    import fasttext

model = fasttext.train_supervised(
    input        = train_path,
    epoch        = 100,
    lr           = 0.3,
    wordNgrams   = 2,
    minCount     = 1,
    dim          = 64,
    loss         = "softmax",
    verbose      = 2,
)

os.unlink(train_path)

# ── 評估 ────────────────────────────────────────────────────────────────────
with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", encoding="utf-8",
                                 delete=False) as f:
    valid_path2 = f.name
    f.write("\n".join(valid_records))

n, prec, rec = model.test(valid_path2)
os.unlink(valid_path2)

report_lines = [
    f"Samples: {len(records)}  Train: {len(train_records)}  Valid: {len(valid_records)}",
    f"Precision@1: {prec:.4f}  Recall@1: {rec:.4f}  N={n}",
    "",
    "Per-label accuracy on valid set:",
]

# per-label
from collections import defaultdict
correct = defaultdict(int)
total   = defaultdict(int)
for line in valid_records:
    parts = line.split(" ", 1)
    true_label = parts[0]
    text = parts[1] if len(parts) > 1 else ""
    try:
        pred, prob = model.predict(text)
        pred_label = pred[0]
    except Exception:
        pred_label = ""
    total[true_label] += 1
    if pred_label == true_label:
        correct[true_label] += 1

for lbl in sorted(total):
    acc = correct[lbl] / total[lbl] if total[lbl] else 0
    report_lines.append(f"  {lbl.replace('__label__',''):25} {correct[lbl]:3}/{total[lbl]:3}  {acc:.0%}")

report = "\n".join(report_lines)
print(report)
REPORT.write_text(report, encoding="utf-8")

# ── 儲存 ────────────────────────────────────────────────────────────────────
model.save_model(str(OUT))
print(f"\n✅ 模型已存: {OUT}  ({OUT.stat().st_size//1024} KB)")
