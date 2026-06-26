# PPT 字體大小偏好

**規則**：所有 PPT 內文字字級 **≥ 12 pt**、不要用 11/10/9 pt（讀者看不清）。

## 適用範圍
- `build_pptx.py` / `build_warehouse_pptx.py` / `build_weekly_pptx.py` 等所有 PPT 產生腳本
- 表格內文字 (`add_row_table` 預設 `font_size=11` → 改 `font_size=12`)
- 程式碼塊 (`add_code_block` 預設 `font_size=11` → 改 `font_size=12`)
- 備註欄 (`set_notes` 預設 11 pt → 改 12 pt)
- KPI label 預設 11 pt → 改 12 pt

## 例外（允許 < 12 pt 的場景）
- 頁碼（10 pt OK、本來就是輔助元素）
- 警告 / 補充註腳（10-11 pt OK）

## 改寫的 helper 預設值（之後寫 PPT 套這個）
```python
def add_row_table(..., font_size=12):  # 從 11 → 12
def add_code_block(..., font_size=12): # 從 11 → 12
def add_kpi_row(..., label_size=12):   # 從 11 → 12
def set_notes(...):                    # set Pt(12) 不是 Pt(11)
```

## 觸發
user 2026-05-28 明確要求「簡報的文字大小以後記得要 12 以上」
