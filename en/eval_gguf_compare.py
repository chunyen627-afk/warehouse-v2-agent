# -*- coding: utf-8 -*-
"""
eval_gguf_compare.py — 三方模型對照（純 LLM 層，本機 llama-cpp-python 跑 gguf）。
比較同一批英文句在不同模型下產出的 function call：
  1. base      (functiongemma-270m-it，未微調)
  2. zh-tuned  (中文語料微調，即現在 RPI5 8002 在跑的那顆)
  3. en-tuned  (英文語料微調，本次訓練產物)
prompt 格式與訓練/推理完全一致（SYSTEM_PROMPT + user + model turn）。
⚠️ 這測「LLM 單獨選 tool + 抽參數」的裸實力；生產環境有 FastText 先路由，
   所以分數不等於系統整體表現（系統表現另用 ws_inspect 測）。
用法：python eval_gguf_compare.py <model.gguf> [eval_en.txt]
"""
import sys, re
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

MODEL = Path(sys.argv[1])
EVAL_F = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "eval_en.txt"

from build_function_declarations import SYSTEM_PROMPT
START_TURN = "<start_of_turn>"
END_TURN   = "<end_of_turn>"

cases = []
for line in open(EVAL_F, encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "|" not in line:
        continue
    exp, sent = line.split("|", 1)
    cases.append((exp.strip(), sent.strip()))

print(f"[model] {MODEL.name}")
print(f"[eval ] {len(cases)} 句 — 載入模型中 ...")

from llama_cpp import Llama
llm = Llama(model_path=str(MODEL), n_ctx=2048, n_threads=6,
            verbose=False, logits_all=False)
print("[model] loaded\n")


def ask(sent):
    prompt = (f"{SYSTEM_PROMPT}{START_TURN}user\n{sent}\n{END_TURN}\n"
              f"{START_TURN}model\n")
    out = llm(prompt, max_tokens=48, temperature=0.0, echo=False,
              stop=["<end_function_call>", END_TURN])
    return out["choices"][0]["text"]


def parse_tool(gen):
    m = re.search(r"call:([a-z_]+)", gen or "")
    return m.group(1) if m else "(none)"


hit = 0
rows = []
for i, (exp, sent) in enumerate(cases, 1):
    gen = ask(sent)
    got = parse_tool(gen)
    ok = (got == exp) or (exp == "rejected" and got == "(none)")
    hit += ok
    rows.append((ok, exp, got, sent, (gen or "").strip()[:60]))
    print(f"[{i:2d}] {'OK ' if ok else 'MISS'} exp={exp:20s} got={got:20s} | {sent[:38]}")

print("\n" + "=" * 58)
print(f"{MODEL.stem}: {hit}/{len(cases)} = {hit*100//len(cases)}%")
print("=" * 58)
out_f = HERE / f"_eval_{MODEL.stem}.txt"
with open(out_f, "w", encoding="utf-8") as f:
    for ok, exp, got, sent, gen in rows:
        f.write(f"{'OK' if ok else 'MISS'}|{exp}|{got}|{sent}|{gen}\n")
    f.write(f"TOTAL|{hit}/{len(cases)}\n")
print(f"已存 {out_f.name}")
