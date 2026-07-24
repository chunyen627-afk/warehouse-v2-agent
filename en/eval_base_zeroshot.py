# -*- coding: utf-8 -*-
"""
eval_base_zeroshot.py — 測「base 原版模型（零訓練）」對英文的 function-calling 表現。
科學對照組：用與訓練/推理完全相同的 prompt 格式，看 base 不經任何微調能做到什麼。
在 CPU 跑（270M 很小），不佔 GPU（訓練中）。
用法：python eval_base_zeroshot.py [eval_en.txt]
輸出：每句的 期望tool / base實際輸出tool / 是否命中 + 總命中率
"""
import sys, json, re
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
EVAL_F = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "eval_en.txt"
BASE_MODEL = str(HERE.parent.parent / "functiongemma-270m-it")

START_TURN = "<start_of_turn>"
END_TURN   = "<end_of_turn>"
START_CALL = "<start_function_call>"
END_CALL   = "<end_function_call>"

from build_function_declarations import SYSTEM_PROMPT

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

print(f"[load] base = {BASE_MODEL} (CPU)")
tok = AutoTokenizer.from_pretrained(BASE_MODEL)
model = AutoModelForCausalLM.from_pretrained(
    BASE_MODEL, torch_dtype=torch.float32, device_map="cpu")
model.eval()
print("[load] done")

# 讀評測集：expected_tool|sentence
cases = []
for line in open(EVAL_F, encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    if "|" in line:
        exp, sent = line.split("|", 1)
        cases.append((exp.strip(), sent.strip()))
print(f"[eval] {len(cases)} 句\n")

def ask(sent):
    prompt = (f"{SYSTEM_PROMPT}{START_TURN}user\n{sent}\n{END_TURN}\n"
              f"{START_TURN}model\n")
    ids = tok(prompt, return_tensors="pt")
    with torch.no_grad():
        out = model.generate(**ids, max_new_tokens=48, do_sample=False,
                             pad_token_id=tok.pad_token_id or 0)
    gen = tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=False)
    return gen

def parse_tool(gen):
    m = re.search(r"call:([a-z_]+)", gen)
    return m.group(1) if m else "(none)"

hit = 0
rows = []
for i, (exp, sent) in enumerate(cases, 1):
    gen = ask(sent)
    got = parse_tool(gen)
    ok = (got == exp)
    # rejected 類：base 不會輸出 rejected，只要它沒硬湊一個 tool 就算合理
    if exp == "rejected" and got == "(none)":
        ok = True
    hit += ok
    rows.append((ok, exp, got, sent, gen.strip()[:70]))
    print(f"[{i:2d}] {'OK ' if ok else 'MISS'} exp={exp:20s} got={got:20s} | {sent[:40]}")

print("\n" + "=" * 60)
print(f"base 零訓練命中率: {hit}/{len(cases)} = {hit*100//len(cases)}%")
print("=" * 60)
# 存檔供對照
with open(HERE / "_eval_base_result.txt", "w", encoding="utf-8") as f:
    for ok, exp, got, sent, gen in rows:
        f.write(f"{'OK' if ok else 'MISS'}|{exp}|{got}|{sent}|{gen}\n")
    f.write(f"TOTAL|{hit}/{len(cases)}\n")
print(f"已存 _eval_base_result.txt")
