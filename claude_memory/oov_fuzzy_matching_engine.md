---
name: oov_fuzzy_matching_engine
description: OOV 模糊匹配引擎 _fuzzy_score() — 剝規格+雙向滑窗+字元重疊，解決 SequenceMatcher 被 DB 規格詞稀釋問題
metadata:
  type: reference
---

## OOV 模糊匹配引擎（2026-06-29）

`server.py` 的 `_fuzzy_score(keyword, name) → float(0-100)` 取代單純 `SequenceMatcher`。

**為什麼要換**：`SequenceMatcher` 把全名一起比，DB 裡「氣泡水 500ml」的「500ml」把相似度從 67% 稀釋到 36%，導致 OOV 無法匹配。

**三步驟**：
1. 剝規格詞 — regex `\d+(\.\d+)?\s*(ml|kg|g|mm|...)` 去掉數字+單位，及變體標籤（男款/女款/兒童…）
2. 雙向滑窗 — keyword 在 core name 上滑、core 在 keyword 上滑，取最大 SequenceMatcher ratio
3. 字元重疊（Dice coefficient）— 對 ≤3 字短 keyword 的錯字額外加分（×0.85 權重）

**閾值**：`_extract_sku_keyword` ≥55、`_detect_oov` ≥60（clarify）、≥85（auto-fix）

**使用位置**：`_extract_sku_keyword()` ③-b、`_detect_oov()` scoring

**測試**：`test/test_oov.py` 33 題，涵蓋錯字/部分名/雜詞/英文/RCA OOV

關聯：[[warehouse_v2_project]]
