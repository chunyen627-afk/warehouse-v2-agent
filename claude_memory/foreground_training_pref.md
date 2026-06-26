# 用戶偏好：長時間訓練 Claude 幫忙跑、但 user 也要能看終端機畫面

**情境**：跑 `finetune_local.py` / `fetch_snapshot_v3.py` 這類「長時間 + 有 step-by-step output」的指令。

**用戶需求（v3.4 踩雷後確立）**：
1. ❗ **Claude 還是幫忙跑** — user 不想自己手動操作起訓練
2. ❗ **但 user 要能即時用 PowerShell 看到訓練畫面** — 避免像 v3.4 第一次跑 10 小時沒 log、沒 checkpoint、Claude 也沒看到 hang，最後虛 10 小時

**雙保險**：Claude 跑 + user 也能監控。

## 標準作業流程

### 1. Claude 啟動訓練（用 background 或 Monitor，但**必須寫獨立 log 檔**）

```bash
# 推薦：用 background task + tee 寫 log
py -3.11 finetune_local.py 2>&1 | tee _ft.log
```

> ⚠️ Bash 環境用 `tee`，跨 Windows 環境一樣有作用（git bash 自帶）。
> 啟動完 Claude 印出 task ID + log 檔路徑（**確認 log 是寫在專案目錄**，user 才能 cd 過去看）。

### 2. Claude **必須主動給 user 看 task output file 的指令**

User **要的是 Claude task output file 的路徑**（不是專案目錄裡的 `_ft.log`）— 那是 background task 機制自動寫的 stdout 鏡像，**跟 Claude 自己 Read tool 看到的內容一字不差**。

啟動 background task 後 Claude 會拿到 task ID（例如 `bi11yyfev`），路徑模板：

```
$env:TEMP\claude\C--Users-pjunm-OneDrive-Desktop-FunctionGemma-Finetune\<session-id>\tasks\<task-id>.output
```

**User 看畫面指令**（啟動後立刻附上、**不要等 user 問才給**）：

```powershell
Get-Content "$env:TEMP\claude\C--Users-pjunm-OneDrive-Desktop-FunctionGemma-Finetune\<session-id>\tasks\<task-id>.output" -Wait -Tail 30 -Encoding UTF8
```

**⚠️ `-Encoding UTF8` 必加**：PowerShell 5.1 `Get-Content -Wait` 預設用 cp950（系統 locale）讀檔，但 task output 是 UTF-8 → 沒加會中文亂碼。即使 user 設了 `$PROFILE` UTF-8、`[Console]::OutputEncoding` 是 UTF-8 都沒用，`Get-Content` 的 `-Encoding` 預設不繼承 console encoding。

Claude 啟動 task 時會看到完整絕對路徑（在 tool result 裡），**直接複製貼給 user**，不用 user 自己拼路徑。

範本：

```
✅ 訓練啟動 (task ID: bXXX)

【你要看訓練畫面，PowerShell 跑這條】（跟我看到的完全一樣）：

  Get-Content "C:\Users\pjunm\AppData\Local\Temp\claude\...\tasks\bXXX.output" -Wait -Tail 5

我這邊也持續看 + 重大事件主動 ping 你。
```

> **註**：以前曾經給過 `Get-Content _ft.log -Wait` 這種「專案目錄裡的 log 檔」指令 — user 覺得多此一舉、log 還要佔空間。**用 task output file 路徑就好**，反正 background task 機制本來就會寫，user 看的跟 Claude 看的同一份。

### 3. Trainer 設定鐵則（CLAUDE.md 雷 9）

| 設定 | 值 | 為什麼 |
|---|---|---|
| `logging_steps` | **5** | 每 5 step 印 loss / token_acc dict |
| `save_strategy` | **"steps"** | 不用等整 epoch |
| `save_steps` | **200** | ~30-40 分鐘一個 checkpoint，hang 也能 resume |

### 4. Claude 監控 + 主動回報

| 時機 | Claude 動作 |
|---|---|
| 5 分鐘內 | 確認 `[sanity]` 出現 |
| 30 分鐘內 | 確認第一筆 `{'loss': ...}` dict log 出現 |
| 1 小時內 | 確認 `checkpoint-200/` 寫出 |
| epoch eval 出 | 主動報 `eval_mean_token_accuracy` |
| 完訓 | 主動報 `train_runtime` + 進入量化階段 |

任何檢查點沒過 → 立即診斷（不要等 user 來問）。

## 不要做的事

- ❌ 不要要求 user 自己手動起訓練（user 想要 Claude 幫忙）
- ❌ 不要跑了訓練不附「user 看畫面的 PowerShell 指令」
- ❌ 不要用 `&` 丟 background 不寫 log（v3.4 第一次踩過）
- ❌ 不要只 Monitor pipe 不寫 log 檔（v3.4 第二次踩過）
- ❌ 不要 `save_strategy="epoch"`（hang 時 0 線索）

## 範本：啟動訊息

```
✅ 訓練啟動 (task ID: bXXX)，log 寫到 _ft.log

【你要看訓練畫面，PowerShell 跑這個】：

  cd "C:\Users\pjunm\OneDrive\Desktop\FunctionGemma_Finetune"
  Get-Content _ft.log -Wait | Where-Object { $_ -match "'loss':|sanity|epoch|eval_" }

我這邊會持續看 log + 重大事件主動 ping 你。
```
