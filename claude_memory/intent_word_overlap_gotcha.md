---
name: intent_word_overlap_gotcha
description: 意圖詞重疊導致路由衝突 —「警示」同時出現在 C3 低庫存和 C14 alert 規則
metadata:
  type: feedback
---

## 意圖詞重疊導致路由衝突（2026-06-29）

**雷**：「警示」這個詞同時出現在兩個意圖詞清單：
- `_LOW_STOCK_INTENT_WORDS`（C3 缺貨意圖）→ 預期路由 `list_low_stock`
- C14 的 `_alert_words`（警示設定意圖）→ 預期路由 `set_alert`

當 C3 因 `func_name == "list_low_stock"` 跳過（不做事），C14 看到「警示」就把路由改成 `set_alert`。「庫存警示」這類核心查詢就壞了。

**修法**：C3/C4/C6 加 hard-return（見 [[correction_layer_hard_return_pattern]]）

**教訓**：以後新增意圖詞到任何 tuple 之前，**一定要 grep 檢查該詞是否已存在其他規則的 tuple**，避免詞彙重疊造成路由 ambiguity。特別是 C3（缺貨）、C7（到期）、C8（RCA）、C14（警示設定）這幾個容易打架的領域。

**How to apply**：加意圖詞前跑：
```bash
grep -n "新詞" server.py
```
確認只有一處匹配，或確認多處匹配是合理的（如「庫存」同時用於 inventory 和 low_stock 的排除邏輯）。

關聯：[[correction_layer_hard_return_pattern]] [[warehouse_v2_project]]
