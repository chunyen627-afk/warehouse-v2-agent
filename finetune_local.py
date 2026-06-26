"""
FunctionGemma 270M 本地微調 (Windows / RTX 3070 8GB on physical GPU 0)
原始版本：Google FunctionGemma Cookbook (Colab A100)
本檔：將 Colab notebook 的步驟 1~9 移植成單檔 Python。

歷史（v2 訓練集 3350 sample）：
  v1.0  鎖 GPU 0 (3060 12GB)，batch=2/grad_accum=16，v1 訓練集每 step ~10s
  v2.0  嘗試 GPU 0 (3070 8GB)，batch=1/grad_accum=32 — 每 step 33s（VRAM 滿）
  v2.1  改 GPU 1 (3060 12GB)，batch=2/grad_accum=16 — 每 step 43s（更慢！）
  v2.2  換回 GPU 0 (3070 8GB)，batch=1/grad_accum=32 — 3070 SM 算力勝出，ETA 2h53m

執行：
    python finetune_local.py
"""

import os
# 鎖 GPU 0 (RTX 3070 8GB)：v2 訓練集實測 3070 SM 算力贏過 3060
# 要改卡的話在 shell 設 CUDA_VISIBLE_DEVICES 環境變數覆蓋
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
# 8GB VRAM 碎片化緩解（必須在 import torch 前設定才有效）
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
# Windows DDP 修補：PyTorch Windows wheel 沒編 libuv，但 distributed elastic 預設 use_libuv=True
#   → TCPStore init 失敗。設這個環境變數讓 elastic 改用普通 socket（不要 libuv）
os.environ.setdefault("USE_LIBUV", "0")  # PyTorch 2.4+ 認這個

import json
import torch
from pathlib import Path

# =============================================================================
# 0. 路徑與全域設定
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
print(f"工作目錄：{BASE_DIR}")

# 所有中間產物都寫到本機快取，Drive 只放：訓練資料、最終模型、GGUF、源碼、文件
LOCAL_CACHE     = Path.home() / ".cache" / "functiongemma_finetune"
LOCAL_CACHE.mkdir(parents=True, exist_ok=True)

TRAINING_FILE   = BASE_DIR / "training_data.jsonl"          # 輸入資料（保留在 Drive）
SYSTEM_PROMPT_F = BASE_DIR / "system_prompt.txt"            # 推理時要用，留 Drive
FINAL_MODEL_DIR = BASE_DIR / "functiongemma-270m-it-fine-tune"  # 最終模型，留 Drive

# 中間產物 → 本機 C:\Users\<USER>\.cache\functiongemma_finetune\
TRAIN_DUMP      = LOCAL_CACHE / "formatted_train.jsonl"     # 預覽用，每次重生
TEST_DUMP       = LOCAL_CACHE / "formatted_test.jsonl"
OUTPUT_DIR      = LOCAL_CACHE / "checkpoints"               # checkpoints + runs/

# 完全離線：使用 root 共用的 base model（finance/ 跟 warehouse/ 共用同一份避免重複下載）
# BASE_DIR = warehouse/  → ../functiongemma-270m-it = root 那份
BASE_MODEL = str(BASE_DIR.parent / "functiongemma-270m-it")

# 單卡 / 雙卡訓練自動偵測：
#   單卡: python finetune_local.py            → 鎖 GPU 0
#   雙卡: accelerate launch --multi_gpu --num_processes=2 finetune_local.py
# 偵測 accelerate / torchrun 設定的環境變數，避免在分散式情境下誤鎖 GPU
IN_DISTRIBUTED = any(v in os.environ for v in ("LOCAL_RANK", "WORLD_SIZE", "RANK"))
WORLD_SIZE     = int(os.environ.get("WORLD_SIZE", "1"))

if torch.cuda.is_available():
    if not IN_DISTRIBUTED and "CUDA_VISIBLE_DEVICES" not in os.environ:
        os.environ["CUDA_VISIBLE_DEVICES"] = "0"
    n_visible = torch.cuda.device_count()
    mode = f"分散式 x{WORLD_SIZE}" if IN_DISTRIBUTED else "單卡"
    print(f"CUDA 可用 — 模式: {mode}, 可見 GPU 數: {n_visible}")
    for i in range(n_visible):
        print(f"  GPU {i}: {torch.cuda.get_device_name(i)}")
else:
    raise SystemExit("找不到 CUDA。請先執行 setup_env.bat 安裝 CUDA 版 PyTorch。")

# =============================================================================
# 1. 完全離線模式：強制 transformers / huggingface_hub 不要連網
# =============================================================================
os.environ["HF_HUB_OFFLINE"]    = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
if not Path(BASE_MODEL).exists():
    raise FileNotFoundError(
        f"找不到本地基礎模型目錄：{BASE_MODEL}\n"
        f"請確認 functiongemma-270m-it/ 已放在 {BASE_DIR}"
    )
print(f"離線模式：使用本地模型 {BASE_MODEL}")

# =============================================================================
# 2. 訓練資料檢查
# =============================================================================
if not TRAINING_FILE.exists():
    raise FileNotFoundError(f"找不到訓練資料：{TRAINING_FILE}")
with open(TRAINING_FILE, "r", encoding="utf-8") as f:
    n_lines = sum(1 for _ in f)
print(f"訓練資料筆數：{n_lines}")

# =============================================================================
# 3. 工具宣告（帳本版 v2 — 從 build_function_declarations 匯入，10 個 function）
# =============================================================================
START_TURN = "<start_of_turn>"
END_TURN   = "<end_of_turn>"
START_DECL = "<start_function_declaration>"
END_DECL   = "<end_function_declaration>"
START_CALL = "<start_function_call>"
END_CALL   = "<end_function_call>"
ESCAPE     = "<escape>"

from build_function_declarations import FUNCTION_DECLARATIONS, SYSTEM_PROMPT

with open(SYSTEM_PROMPT_F, "w", encoding="utf-8") as f:
    f.write(SYSTEM_PROMPT)
print(f"已寫出 {SYSTEM_PROMPT_F.name}")

# =============================================================================
# 4. 構建訓練樣本
# =============================================================================
from datasets import Dataset

def create_training_example(sample):
    user_content = sample["user_content"]
    tool_name    = sample["tool_name"]
    tool_args    = json.loads(sample["tool_arguments"])

    prompt = (
        f"{SYSTEM_PROMPT}{START_TURN}user\n"
        f"{user_content}\n"
        f"{END_TURN}\n"
        f"{START_TURN}model\n"
    )

    params_str = ",".join([f"{k}:{ESCAPE}{v}{ESCAPE}" for k, v in tool_args.items()])
    completion = f"{START_CALL}call:{tool_name}{{{params_str}}}{END_CALL}"
    return {"text": prompt + completion}

raw_data = []
with open(TRAINING_FILE, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            raw_data.append(json.loads(line))
print(f"載入 {len(raw_data)} 筆原始資料")

dataset = Dataset.from_list(raw_data)
dataset = dataset.map(create_training_example, remove_columns=dataset.features)
dataset = dataset.train_test_split(test_size=0.1, shuffle=True, seed=42)

dataset["train"].to_json(str(TRAIN_DUMP), force_ascii=False)
dataset["test"].to_json(str(TEST_DUMP),  force_ascii=False)
print(f"  train: {len(dataset['train'])} / test: {len(dataset['test'])}")

print("\n樣本預覽：")
print("-" * 60)
print(dataset["train"][0]["text"][:600])
print("-" * 60)

# =============================================================================
# 5. 載入基礎模型
# =============================================================================
from transformers import AutoTokenizer, AutoModelForCausalLM

print(f"\n載入基礎模型 {BASE_MODEL} ...")
model = AutoModelForCausalLM.from_pretrained(
    BASE_MODEL,
    torch_dtype=torch.bfloat16,
    device_map="auto",
    attn_implementation="eager",
)
tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
print(f"  參數量：{model.num_parameters():,}  ({model.num_parameters()*2/1e9:.2f} GB bf16)")
print(f"  Device: {model.device}")

# =============================================================================
# 5c. LoRA finetune (v3.4 新增；環境變數 USE_LORA=1 啟用)
# =============================================================================
#   3070 8GB 撞 Sysmem Fallback → 訓練 7.5h（vs 預期 4h）
#   LoRA path：更新 ~5M adapter (vs full 268M)、VRAM 7.9 GB → ~3 GB、預估 1.5-2h
#   訓練後 merge_and_unload 合進 base，下游 GGUF 量化流程不變
USE_LORA = os.environ.get("USE_LORA", "0") == "1"
if USE_LORA:
    from peft import LoraConfig, get_peft_model, TaskType
    print("\n[LoRA] 啟用 LoRA finetune（USE_LORA=1）")
    lora_config = LoraConfig(
        r=16,                                    # rank，越大越接近 full FT 但越慢
        lora_alpha=32,                           # scaling factor = lora_alpha / r = 2x
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
                                                 # 只訓 attention 4 個 proj（最常見配置）
        lora_dropout=0.05,
        bias="none",
        task_type=TaskType.CAUSAL_LM,
    )
    model = get_peft_model(model, lora_config)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    print(f"[LoRA] trainable: {trainable:,} / {total:,} ({trainable*100/total:.2f}%)")
else:
    print("\n[訓練模式] full fine-tune（要切 LoRA 跑 USE_LORA=1）")

# =============================================================================
# 5b. Sanity check：訓練樣本最長 token 數 必須 ≤ max_length
# =============================================================================
# 上一輪災難（eval_loss=0.016 但生成 0/34 命中）的根因就是 max_length 把 completion
# 從右邊砍掉，模型只看到 declaration 沒看到答案。這裡先量一下避免再踩坑。
_sample_lens = [len(tokenizer(row["text"]).input_ids) for row in dataset["train"]]
_max_len = max(_sample_lens)
_p95_len = sorted(_sample_lens)[int(len(_sample_lens) * 0.95)]
print(f"\n[sanity] 訓練樣本 token 長度: max={_max_len}, p95={_p95_len}, n={len(_sample_lens)}")
print(f"[sanity] SYSTEM_PROMPT 單獨: {len(tokenizer(SYSTEM_PROMPT).input_ids)} tokens")

# =============================================================================
# 6. 訓練設定
# =============================================================================
from trl import SFTConfig, SFTTrainer

# 3070 8GB / Gemma3 vocab=262K：logits float32 在 batch=1,seq=1024 ≈ 1.08GB
# batch=1 + grad_accum=32 (effective batch = 32) + gradient_checkpointing
training_args = SFTConfig(
    output_dir=str(OUTPUT_DIR),
    dataset_text_field="text",

    max_length=1344,           # v3.3 純查資料：SYSTEM_PROMPT 938 tokens（36 sector + 10 function）
                               #   max sample ~997 + 15% headroom = 1152 OK
                               # v3.1: 1152 / v3.3 中間版（含方案 C）暫升到 1280 後撤回
    packing=False,
    num_train_epochs=3,                      # epoch 3 後 eval_loss 改善 < 2%，再跑只是 overfit + 浪費時間
    per_device_train_batch_size=2,           # 3060 12GB：batch=2 可用
    per_device_eval_batch_size=2,
    # effective batch = world_size × per_device × grad_accum = 32（不論單卡或雙卡）
    gradient_accumulation_steps=max(1, 32 // (2 * WORLD_SIZE)),

    learning_rate=1e-5,
    lr_scheduler_type="cosine",
    optim="adamw_torch_fused",
    warmup_ratio=0.1,

    logging_steps=5,                          # v3.4 縮到 5（從 10），看 loss 變化更頻繁
    eval_strategy="epoch",
    save_strategy="steps",                    # v3.4 改 step（從 epoch），避免 hang 時整 epoch 浪費
    save_steps=200,                           # 每 200 step 落一次 checkpoint（~30-40 分鐘一次）
    save_total_limit=2,             # 中間 checkpoint 只保留最近 2 份
    save_only_model=True,           # 不寫 optimizer.pt / scheduler.pt（每份省 ~1 GB）

    gradient_checkpointing=True,             # 3070 8GB activation 太大、必須 checkpoint
                                              # （LoRA 模式 forward 重算 2 次拖慢、但 8GB 沒得選）
    bf16=True,

    report_to="tensorboard",
    push_to_hub=False,
    dataloader_num_workers=0,                # Windows 多 worker 易出問題
)

# 防呆：max_length 必須大於最長訓練樣本，否則 completion 會被截掉
assert training_args.max_length >= _max_len, (
    f"max_length={training_args.max_length} 比最長訓練樣本 ({_max_len} tokens) 小，"
    f"completion 會被砍掉。請把 max_length 拉到 >= {_max_len}（建議 {(_max_len // 256 + 1) * 256}）。"
)
print(f"[sanity] OK: max_length={training_args.max_length} >= 最長樣本 {_max_len}")

# =============================================================================
# 7. 訓練
# =============================================================================
trainer = SFTTrainer(
    model=model,
    args=training_args,
    train_dataset=dataset["train"],
    eval_dataset=dataset["test"],
    processing_class=tokenizer,              # TRL 0.26.2 用法
)

print("\n開始訓練 ...")
print(f"  train: {len(dataset['train'])}  eval: {len(dataset['test'])}")
print("-" * 50)
# v3.4 雷 9：自動 resume — 偵測 FINAL_MODEL_DIR 下有 checkpoint-* 就接著跑
_existing_ckpts = sorted(FINAL_MODEL_DIR.glob("checkpoint-*")) if FINAL_MODEL_DIR.exists() else []
if _existing_ckpts:
    latest_ckpt = _existing_ckpts[-1]
    print(f"[resume] 偵測到 checkpoint {latest_ckpt.name}，從這裡接著訓練")
    train_result = trainer.train(resume_from_checkpoint=str(latest_ckpt))
else:
    print("[resume] 沒 checkpoint，從頭訓練")
    train_result = trainer.train()
print("-" * 50)
print(f"訓練完成，final loss = {train_result.training_loss:.4f}")

# =============================================================================
# 8. 儲存最終模型
# =============================================================================
# LoRA 訓練完 → merge adapter 合進 base model（讓下游 GGUF 量化流程不用改）
if USE_LORA:
    print("\n[LoRA] merge_and_unload — 合 adapter 進 base model ...")
    model = model.merge_and_unload()
    print("[LoRA] 合併完成，繼續走 trainer.save_model 路徑")
    # 把 merged model attach 回 trainer（讓 save_model 拿到 merged 不是 PEFT wrapper）
    trainer.model = model

print(f"\n儲存最終模型至 {FINAL_MODEL_DIR}")
trainer.save_model(str(FINAL_MODEL_DIR))
tokenizer.save_pretrained(str(FINAL_MODEL_DIR))

# =============================================================================
# 9. 微調後測試 — 格式 + 工具名稱 + 參數三重驗證
# =============================================================================
import re

# 測試案例從共用 test_cases.py 載入（與 test_model.py / test_gguf.py 嚴格一致）
import sys as _sys
_sys.path.insert(0, str(BASE_DIR))
from test_cases import CASES as test_cases

# 抓 function name: <start_function_call>call:NAME{...}
TOOL_RE = re.compile(r"<start_function_call>call:(\w+)\{")
# 抓參數: key:<escape>VALUE<escape>
ARG_RE  = re.compile(r"(\w+):<escape>([^<]*)<escape>")


def parse_args(resp: str) -> dict:
    """從 model 輸出抽出 args dict，數字 string 會自動轉 int。"""
    args = {}
    for k, v in ARG_RE.findall(resp):
        # 試著轉成 int（給 amount 等數字欄位）
        if v.lstrip("-").isdigit():
            args[k] = int(v)
        else:
            args[k] = v
    return args


def args_match(actual: dict, expected: dict) -> tuple[bool, list]:
    """檢查 expected 的每個 key 是否都正確在 actual 裡。回傳 (是否全對, 錯誤清單)。"""
    diffs = []
    for k, v in expected.items():
        a = actual.get(k, "<missing>")
        if a != v:
            diffs.append(f"{k}: got={a!r} expected={v!r}")
    return (len(diffs) == 0, diffs)


print("\n" + "=" * 78)
print(" 微調後測試 — 格式 + 工具名稱 + 參數驗證")
print("=" * 78)

model.eval()

n_total      = len(test_cases)
n_format_ok  = 0
n_tool_ok    = 0
n_full_ok    = 0  # tool name + args 全對
results_by_tool = {}     # tool_name -> [full_pass, total]

for prompt, expected_tool, expected_args in test_cases:
    full = f"{SYSTEM_PROMPT}{START_TURN}user\n{prompt}\n{END_TURN}\n{START_TURN}model\n"
    inputs = tokenizer(full, return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=100,
            do_sample=False,
            pad_token_id=tokenizer.pad_token_id,
        )
    resp = tokenizer.decode(
        out[0][inputs["input_ids"].shape[1]:],
        skip_special_tokens=False,
    ).strip()

    m = TOOL_RE.search(resp)
    has_format = m is not None
    actual_tool = m.group(1) if m else None
    tool_ok    = (actual_tool == expected_tool)

    actual_args = parse_args(resp) if has_format else {}
    args_ok, diffs = args_match(actual_args, expected_args) if tool_ok else (False, [])

    full_ok = tool_ok and args_ok

    if has_format: n_format_ok += 1
    if tool_ok:    n_tool_ok   += 1
    if full_ok:    n_full_ok   += 1

    bucket = results_by_tool.setdefault(expected_tool, [0, 0])
    bucket[1] += 1
    if full_ok:
        bucket[0] += 1

    if not has_format:
        flag = "[FORMAT FAIL]"
    elif not tool_ok:
        flag = f"[TOOL: {actual_tool}]"
    elif not args_ok:
        flag = "[ARGS]"
    else:
        flag = "[OK]"

    short = resp[:110] + ("..." if len(resp) > 110 else "")
    print(f"  {flag:18s} expect={expected_tool:18s} prompt={prompt!r}")
    print(f"     -> {short}")
    if not args_ok and tool_ok:
        for d in diffs:
            print(f"        ! {d}")

print()
print("=" * 78)
print(" 結果總結")
print("=" * 78)
print(f"  格式正確       : {n_format_ok}/{n_total}  ({n_format_ok/n_total*100:5.1f}%)")
print(f"  工具名正確     : {n_tool_ok}/{n_total}  ({n_tool_ok/n_total*100:5.1f}%)")
print(f"  工具名+參數全對: {n_full_ok}/{n_total}  ({n_full_ok/n_total*100:5.1f}%)  ← 真正命中率")
print()
print("  各 function 全對命中率:")
for tool in sorted(results_by_tool.keys()):
    p, t = results_by_tool[tool]
    bar = "█" * p + "·" * (t - p)
    print(f"    {tool:20s} {p}/{t}  {bar}")
print("=" * 78)
print("\n全部完成。")
