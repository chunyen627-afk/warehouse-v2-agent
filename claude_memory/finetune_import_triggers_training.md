---
name: finetune_import_triggers_training
description: 雷——import finetune_local.py 會直接啟動訓練（無 __main__ 保護）
metadata: 
  node_type: memory
  type: feedback
  originSessionId: f5448369-2b06-401c-af82-d93240db6da6
---

**絕不** `import finetune_local`（或 `python -c "import finetune_local"`）來求值它的常數（OUTPUT_DIR / LOCAL_CACHE 等）。該腳本在模組頂層直接呼叫 `trainer.train()`，**沒有 `if __name__=="__main__"` 保護**，import 會立刻啟動第二個訓練、搶 GPU、害原訓練變慢甚至 OOM。

**Why**：2026-06-22 v2 重訓時踩到——為了查 OUTPUT_DIR 路徑 `python -c "import finetune_local"`，結果啟了 PID 14124 第二訓練，搶走原訓練(29160)的 GPU 兩分鐘（s/it 從 9.7 跳 19.6），緊急 Stop-Process 才救回。

**How to apply**：要取腳本常數，一律用純文字解析（regex / ast.parse 讀檔），不要 import。例：`re.search(r'OUTPUT_DIR\s*=\s*(.+)', open(f).read())`。

關聯：[[warehouse_v2_project]]、loss 落點在 `~/.cache/functiongemma_finetune/checkpoints/runs/*/events.out.tfevents.*`（report_to=tensorboard，stdout 看不到 loss dict，要讀 tfevents 或 checkpoint 的 trainer_state.json）。
