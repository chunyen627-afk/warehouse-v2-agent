# 用戶偏好：跑完即清的 temp 檔不留檔

**情境**：訓練 stdout log、一次性 debug script、跑完用不到的中間檔。

**用戶要求**：「那些 log 以後都不要了，佔空間」— 不留垃圾。

## 清掃名單（訓練完成 / debug 結束後 Claude 主動刪）

| 檔案 | 用途 | 何時刪 |
|---|---|---|
| `_ft.log`、`_ft_v34.log`、`_ft_*.log` | 訓練 stdout 落地 log | 訓練完成 + 驗證 OK 後 |
| `_scan_pollution.py` | v3.4 LoRA 失敗時掃西里爾字符的 debug script | 確認訓練資料乾淨後 |
| `_ft_v34_full.log` | 同上 | 同上 |
| 其他 `_*.log` / `_*_debug.py` | 一次性 debug 產物 | 用完即刪 |

**已加進 `.gitignore`** 避免不小心 commit。

## 不要清的

- `.fetch_checkpoint.json` — fetch_snapshot 撞 quota 時的中斷點，下次 resume 用，留著
- `TRAINING_BACKLOG.md` / `CLAUDE.md` — 文件
- `functiongemma-270m-it-fine-tune.bf16.gguf` / `*.q8_0.gguf` — model 產物，量化後馬上會 sync 到 `test/models/`，但根目錄這份也留著當「最新版本備份」（每次重訓會覆蓋）
- `_test_*.py` — 帶 `_test_` 開頭但常駐專案的測試腳本（`_test_corrections_smoke.py` / `_test_all_chips.py` / `_test_chart_colors.py` / `_test_allocation_dynamic.py`），看名字常駐就不要刪

## 觸發時機

訓練完成 + 量化 + sync + `test_gguf.py` 驗證 OK 後，主動跟用戶確認：
> 「驗證 OK，可以幫你清掉 `_ft_v34.log`（XX MB）跟其他 debug 暫存嗎？」
