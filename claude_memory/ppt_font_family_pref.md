# PPT 字體偏好

**規則**:做 PPT 時:
- **中文** → **微軟正黑體**(Microsoft JhengHei)
- **英文、數字、code、enum、function 名** → **Arial**

## 適用範圍
- `build_pptx.py` / `build_warehouse_pptx.py` / `build_weekly_pptx*.py` 等所有 PPT 產生腳本
- 跟 `ppt_font_size_pref.md`(≥12pt)合併套用

## 改寫的 helper(下次寫 PPT 套這個)
```python
FONT_ZH = "Microsoft JhengHei"   # 微軟正黑體(原本就是)
FONT_EN = "Arial"                # 英文/數字/code → 從 Consolas 改成 Arial
```

helper 預設:
- `add_text(..., font=FONT_ZH)` 中文用
- `add_text(..., font=FONT_EN)` 英文/數字/code 用
- `add_row_table` 的 `mono_cols` → 改用 FONT_EN(Arial)而不是 Consolas
- `add_code_block` 的字體 → 改 FONT_EN(Arial)

## 例外(等寬字體仍可用的場景)
- 純程式碼長段(`add_code_block` 純 code 區塊)若用戶**明確要求**等寬,才用 Consolas
- 預設一律走 Arial

## 觸發
user 2026-06-04 明確要求「中文我愛好微軟正體字、英文或文字就是 arial 請幫我記住」

## 已知衝突
- 之前 build_*pptx.py 是 Microsoft JhengHei + **Consolas**(等寬)
- 下次改 PPT 時把 `FONT_EN = "Consolas"` 一律換成 `FONT_EN = "Arial"`
- 已產出的 .pptx 不回頭改、下次再修才套用(user 說「下次再修」)
