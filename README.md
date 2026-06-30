# 🏭 Warehouse Agent v2

> **FunctionGemma 270M 微調模型 × 倉管 AI Agent**  
> 用邊緣級小模型實現生產可用的倉庫管理智慧助理

[![Python](https://img.shields.io/badge/Python-3.11-blue)](https://python.org)
[![Model](https://img.shields.io/badge/Model-v6_5,849筆訓練-orange)](https://huggingface.co/google/gemma-3-1b-it)
[![81 Eval](https://img.shields.io/badge/81_Eval-99%25-brightgreen)]()
[![OOV v1](https://img.shields.io/badge/OOV_v1-98%25-brightgreen)]()
[![OOV v2](https://img.shields.io/badge/OOV_v2-97.5%25-brightgreen)]()
[![intent_clf](https://img.shields.io/badge/intent_clf-489MB_主路由-blue)]()

---

## 📖 專案簡介

本專案是一套**完整的倉管 AI Agent**，以 Google FunctionGemma 270M 微調模型為核心推理引擎，搭配多層校正架構與 ReAct Loop，實現自然語言倉庫查詢、異常追查、腳本執行、主動警示與定時排程。

**設計理念：** 270M 小模型負責意圖路由（Function Call），Server 端負責業務邏輯編排，LLM 只做「它擅長的事」——語義理解，不做「它不擅長的事」——精確計算與業務規則。

```
使用者輸入
    │
    ▼
[Query Rewriting]   ← 口語 → 標準句（60+條規則）
    │
    ▼
[intent_clf 主路由]  ← FastText 512MB 分類器（意圖分類 98.9% 精準）
    │
    ▼
[FunctionGemma 270M]  ← 輔助參數提取（keyword/倉庫/時間）
    │
    ▼
[dispatch 攔截層]   ← 口語 pattern 強制路由 + keyword 清理
    │
    ▼
[校正層 C0-C18 + Pre-C]  ← 最後防線修正
    │
    ▼
[業務工具執行]  ← 七金剛查詢 / 三金剛 Agent / 腳本執行
    │
    ▼
前端展示（WebSocket 即時串流）
```

---

## 🎯 核心功能

### 📊 七金剛 — 倉庫查詢
| 功能 | 說明 | 範例 |
|------|------|------|
| `query_inventory` | 庫存查詢（商品 / 倉庫 / 類別） | 「北區倉洗衣精還有多少？」 |
| `query_movement` | 進出記錄（時間範圍 / 方向） | 「上個月出貨記錄」 |
| `list_low_stock` | 缺貨警示（低於安全庫存） | 「哪些商品快沒貨了？」 |
| `compare_warehouses` | 倉庫比較（任意兩倉對比） | 「北倉跟中倉差多少？」 |
| `list_hot_items` | 熱銷排行（期間 / 類別） | 「最近賣最好的是什麼？」 |
| `list_expiring_items` | 到期預警（N 天內） | 「本月快過期的商品」 |
| `query_related_items` | 相關商品推薦 | 「跟洗衣精類似的有哪些？」 |

### 🤖 三金剛 — Agent 進階工具
| 功能 | 說明 |
|------|------|
| `search_log` | 搜尋異常日誌，啟動 RCA 根因分析 |
| `manage_config` | 調整安全庫存 / 補貨閾值（HITL 確認） |
| `run_script` | 執行白名單腳本，產出 CSV / MD 報告並下載 |

### ⚡ 第四金剛 — 主動警示
- **`set_alert`** — 設定缺貨 / 到期警示規則，持久化到 `alert_rules.json`
- 背景每小時掃描一次，觸發時透過 WebSocket 主動推送通知
- 右側 Panel 可查看 / 刪除現有警示規則

### ⏰ 第五金剛 — 定時排程
- **`set_schedule`** — 自然語言設定排程（「每天早上9點跑盤點」）
- APScheduler 每分鐘檢查，到時自動執行腳本
- 右側 Panel 可查看 / 刪除排程，執行結果即時回傳

---

## 🧠 技術亮點

### Query Rewriting（查詢改寫）
使用者口語輸入 → 53 條 Regex 規則 → 標準句型 → LLM 精準路由

```
「北中南倉差多少」  →  「比較各倉庫庫存」  →  compare_warehouses
「快沒貨了」       →  「哪些商品缺貨警示」  →  list_low_stock
「跑盤點」         →  「執行腳本 月底盤點」  →  run_script
```

### 多層校正架構（C0–C18 + Pre-C）
LLM 輸出不穩定是 270M 小模型的先天限制，解法是 **Server 端後處理**：

- **Pre-C-Schedule / Movement / Compare / Alert** — LLM 前/後強制路由
- **C0** — OOV（未知函式名）偵測
- **C8** — RCA 意圖詞保護（「帳不對 / 差異 / 少了」優先走 RCA）
- **C13** — 明確庫存意圖 hard-return（不被 C18 覆蓋）
- **OOV keyword 前後綴清理** — 「有洗衣精」→「洗衣精」、「洗衣精剩」→「洗衣精」

### ReAct 3-Step Loop（RCA 根因分析）
```
使用者：「抗菌洗衣精帳對不上」
    │
    ├─ Step 1: search_log → 掃 PO + 比對收貨 → 找到短收
    │
    ├─ Step 2: judge_cause_found（規則判斷，不需 LLM）
    │          → ✅ 已確認根因：短收 15 件，供應商 SUP04
    │
    ├─ Step 3: suggest_action（LLM 推理建議）
    │          → 📧 聯絡供應商 / 📋 補開採購單 / 👁 持續監控
    │
    └─ 前端：Agent 追蹤卡顯示三步 Tool Call + 💡建議
```

### v3 新增（2026-06-30）
- **intent_clf 主路由**：512MB FastText 分類器先決定 function，LLM 只抽參數
- **OOV 引擎重寫**：80+ 雜詞清單 + 多層 fallback fuzzy（threshold 40）
- **錯字容錯**：汽泡水→氣泡水、悶燒鍋→悶燒罐 全自動修復
- **庫存排行**：「哪個東西庫存最多」→ 📦 TOP 10
- **HTTPS + 多裝置**：手機掃 QR 連線，語音輸入可用
- **3-step RCA**：judge_cause_found 改用規則判斷，不需模型

### 路由準確率（2026-06-30）
| 測試 | 題數 | 準確率 | 說明 |
|------|------|--------|------|
| 81 eval | 81 | **99%** (80/81) | 標準測試集 |
| OOV v1 | 97 | **98%** (95/97) | 口語錯字/不完整/贅詞 |
| OOV v2 | 79 | **97.5%** (77/79) | 全新純中文口語 |

**最終架構**: `intent_clf(分類) → LLM(抽參數) → dispatch(攔截) → execute`
- v6 模型: 5,849 筆訓練, eval_loss=0.026
- intent_clf: 489MB, per-label 96-100%
- OOV 引擎: fuzzy threshold 40, 雜詞清單 80+ 詞

---

## 🗂️ 專案結構

```
warehouse_v2/
├── test/                          ← RPI5 部署核心（自足）
│   ├── server.py                  ← FastAPI 主伺服器 + WebSocket
│   ├── warehouse.py               ← 業務邏輯（七金剛實作）
│   ├── tools_v2.py                ← 三金剛 + 排程 + 警示工具
│   ├── anomaly.py                 ← 背景異常掃描
│   ├── intent_clf.py              ← FastText 意圖分類器（輔助）
│   ├── loader_v2.py               ← 資料載入器
│   ├── system_prompt.txt          ← LLM System Prompt
│   ├── seed_data.json             ← 商品主檔（60 SKU × 3 倉）
│   ├── templates/
│   │   └── index.html             ← 前端 UI（WebSocket 即時串流）
│   ├── static/
│   │   └── chart.umd.min.js       ← Chart.js
│   └── warehouse_data/
│       ├── master/                ← CSV 庫存快照
│       ├── logs/                  ← 異常日誌
│       ├── audit/                 ← 腳本執行產出（CSV / MD）
│       ├── alert_rules.json       ← 警示規則（持久化）
│       ├── schedule_jobs.json     ← 定時排程（持久化）
│       └── scripts/               ← 腳本白名單
│           ├── manifest.json      ← 腳本清單
│           ├── stock_audit.py     ← 月底盤點
│           ├── export_movements.py← 進出記錄匯出
│           └── generate_report.py ← 庫存體檢報告
│
├── data_tools/                    ← 資料維護工具
│   ├── items_editable.csv         ← 商品主檔（Excel 可編輯）
│   └── regenerate_seed_from_csv.py← CSV → seed_data.json
│
├── generate_dataset.py            ← 訓練資料生成（JSONL）
├── finetune_local.py              ← 本機微調腳本（Unsloth）
├── build_function_declarations.py ← Function Schema 產生器
├── train_intent_clf.py            ← FastText 分類器訓練
├── system_prompt.txt              ← System Prompt 主檔
├── V2_PLAN.md                     ← 架構設計文件
└── claude_memory/                 ← AI 協作記憶（踩雷紀錄）
    ├── MEMORY.md                  ← 記憶索引
    └── warehouse_v2_project.md    ← 專案進度與待辦
```

---

## 🚀 快速開始

### 環境需求
- Python 3.11
- llama-cpp-python（需 CUDA 或 CPU 版）
- FastAPI / uvicorn / websockets
- APScheduler

```bash
pip install fastapi uvicorn websockets apscheduler llama-cpp-python
```

### 啟動伺服器

```bash
cd warehouse_v2/test
python server.py
# 瀏覽器開啟 http://localhost:8000
```

### 模型檔（需自行準備）
模型權重因超過 GitHub 限制未包含在此 repo。  
需將微調後的 GGUF 檔放到：
```
test/models/functiongemma-270m-it-fine-tune.q8_0.gguf
```

微調流程：
```bash
# 1. 生成訓練資料
python generate_dataset.py

# 2. 微調（需 GPU，使用 Unsloth）
python finetune_local.py

# 3. 轉換為 GGUF Q8_0 格式（用 llama.cpp）
```

### 更新商品資料

```bash
# 編輯 Excel
data_tools/items_editable.csv

# 重生 seed_data.json
cd warehouse_v2
python data_tools/regenerate_seed_from_csv.py
```

---

## 💬 支援的自然語言查詢

```
# 庫存查詢
「北區倉有多少洗衣精？」
「電動牙刷庫存」
「查一下庫存」（查全部）

# 異常追查（RCA）
「抗菌洗衣精帳對不上」
「庫存差異追查」
「庫存怎麼少了」

# 腳本執行
「跑盤點」 / 「月底盤點」
「匯出進出記錄」
「產體檢報告」

# 定時排程
「每天早上9點跑盤點」
「每週自動匯出進出記錄」
「查看排程」 / 「刪除排程」

# 主動警示
「庫存不足時提醒我」
「設定缺貨警示」
「查看警示規則」
```

---

## 🏗️ 系統架構

```
┌─────────────────────────────────────────────┐
│              瀏覽器前端                       │
│   WebSocket 即時串流 / HITL 確認卡            │
└──────────────────┬──────────────────────────┘
                   │ ws / http
┌──────────────────▼──────────────────────────┐
│           FastAPI Server (server.py)         │
│                                              │
│  [Query Rewriting] → [FunctionGemma 270M]   │
│       ↓                    ↓                 │
│  [Pre-C 攔截層]    [C0-C18 校正層]           │
│       ↓                    ↓                 │
│  [業務工具 Dispatch]                          │
│       ├── warehouse.py（七金剛）              │
│       ├── tools_v2.py（三金剛 + 排程 + 警示）│
│       └── ReAct Loop（RCA 根因分析）          │
│                                              │
│  背景任務：alert 掃描(1h) / schedule(1min)   │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│           資料層 (warehouse_data/)           │
│  master CSV / logs / alert_rules.json        │
│  schedule_jobs.json / scripts/               │
└─────────────────────────────────────────────┘
```

---

## 📋 待辦 / Roadmap

- [ ] Context carry-over（「那中倉呢？」記住上輪商品名）
- [ ] 查完庫存自動帶 Proactive 建議 button
- [ ] set_schedule 重複時改 HITL 問覆蓋
- [ ] RCA 第二輪 timeout 保護
- [ ] 自然語言產腳本（Code Generation）
- [ ] 腳本白名單擴充（到期報告 / 補貨清單）

---

## 📝 設計筆記

`claude_memory/` 目錄包含開發過程中的 AI 協作記憶，記錄了：
- 踩過的雷（模型載入 / 訓練 / 部署）
- 架構決策與取捨原因
- 使用者偏好與工作流程

這些記憶讓 AI 助理在跨 session 的長期開發中保持一致性。

---

## 🙏 致謝

- [Google FunctionGemma](https://huggingface.co/google/gemma-3-1b-it) — 基底模型
- [Unsloth](https://github.com/unslothai/unsloth) — 高效微調框架
- [llama.cpp](https://github.com/ggerganov/llama.cpp) — 邊緣推理引擎
- [Claude Code](https://claude.ai/code) — AI 協作開發

---

*最後更新：2026-06-30 | v6 模型 5,849 筆 | intent_clf 489MB 主路由 | 81 eval: 99% | OOV v1: 98% | OOV v2: 97.5%*
