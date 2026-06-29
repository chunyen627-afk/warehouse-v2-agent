---
name: correction_layer_hard_return_pattern
description: 校正層 C3/C4/C6 hard-return 防護 — LLM 正確時防止後續規則推翻
metadata:
  type: reference
---

## 校正層 hard-return 防護模式（2026-06-29）

**問題**：C3 偵測到「庫存警示」含缺貨意圖詞，但若 LLM 剛好輸出 `list_low_stock`，C3 的 `func_name != "list_low_stock"` 條件為 False → 跳過。後續 C14 看到「警示」就覆蓋成 `set_alert`。→ 最重要的「庫存警示」查詢壞掉。

**修法**：C3/C4/C6 命中意圖詞時，不論 LLM 輸出什麼，都 hard-return：
```python
if intent_matched:
    if func_name != target_func:
        # redirect + hard=True
        return target_func, new_args, True
    else:
        # 已正確，但仍 hard-return 防止被後面規則推翻
        return func_name, func_args, True
```

**受影響的校正規則**：C3（缺貨→list_low_stock）、C4（熱銷/滯銷→list_hot_items）、C6（連帶→query_related_items）

**雷**：「警示」這個詞同時出現在 `_LOW_STOCK_INTENT_WORDS` 和 C14 的 `_alert_words`，造成路由 ambiguity。以後加意圖詞要檢查是否跟其他規則的詞重疊。

關聯：[[warehouse_v2_project]] [[intent_word_overlap_gotcha]]
