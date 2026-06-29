---
name: run_full_eval_after_server_change
description: 改 server.py 後必須跑 eval_http.py 確認沒退步才能 commit
metadata:
  type: feedback
---

## 改 server.py 後必須跑完整路由評測（2026-06-29）

**雷**：加 `_fuzzy_score()` 和 spec-stripping regex 看似只影響 OOV，但插入位置在 `_EXTRA_NOISE` 之前，讓所有後續行號位移。加上 C3/C4/C6 hard-return 修改，實際上影響了整個校正層的行為。

本次改動後跑 `eval_http.py` 81 題，從預期 100% 掉到 65%——因為 C3/C4/C6 的修復雖救了「庫存警示」，但 query_related_items 的 keyword 提取仍有 gap，以及 C3 對英文 "low stock alert" 的匹配有問題。

**SOP**：任何 `server.py` 改動（特別是 `_correct_function_call` 或 rewrite 相關）→ 啟動 server → 跑 `eval_http.py` 確認沒退步 → 才能 commit。

**Why**：server.py 的校正層 C0-C18 有複雜的交互作用，看似局部的改動可能觸發遠端的規則衝突（如「警示」在 C3 和 C14 重疊）。

**How to apply**：
```powershell
# 1. 改完 code → 重啟 server
$py = "C:\Users\pjunm\AppData\Local\Programs\Python\Python311\python.exe"
$pid8000 = (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue).OwningProcess
if ($pid8000) { Stop-Process -Id $pid8000 -Force }
Start-Process $py -ArgumentList "server.py" -NoNewWindow -WorkingDirectory "warehouse_v2\test"

# 2. 等 server ready → 跑評測
cd warehouse_v2
& $py eval_http.py
```

關聯：[[eval_http_tool]] [[correction_layer_hard_return_pattern]] [[warehouse_v2_project]]
