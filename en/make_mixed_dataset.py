# -*- coding: utf-8 -*-
"""
make_mixed_dataset.py — 合併中英語料成英文版補訓的混合訓練料。
英文為主力（教英文意圖/keyword/容錯），中文當防遺忘複習（保留中文理解力）。
輸出：training_data_mixed.jsonl（中文 + 英文，打散）
中文版模型不受影響——這份只給英文版專訓的模型用。
"""
import json, random
from pathlib import Path

random.seed(42)
HERE = Path(__file__).parent
ZH = HERE / "training_data.jsonl"
EN = HERE / "training_data_en.jsonl"
OUT = HERE / "training_data_mixed.jsonl"


def load(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def main():
    zh = load(ZH)
    en = load(EN)
    for r in zh:
        r["_lang"] = "zh"
    for r in en:
        r["_lang"] = "en"
    mixed = zh + en
    random.shuffle(mixed)
    # 寫出（去掉 _lang 標記，保持與訓練格式一致）
    with open(OUT, "w", encoding="utf-8") as f:
        for r in mixed:
            f.write(json.dumps({k: v for k, v in r.items() if k != "_lang"},
                               ensure_ascii=False) + "\n")
    print(f"[mixed] zh {len(zh)} + en {len(en)} = {len(mixed)} -> {OUT.name}")
    print(f"        英文佔比 {len(en)*100//len(mixed)}%  中文佔比 {len(zh)*100//len(mixed)}%")
    # tool 分布
    from collections import Counter
    c = Counter(r["tool_name"] for r in mixed)
    print("        tool 分布 top:", dict(c.most_common(5)))


if __name__ == "__main__":
    main()
