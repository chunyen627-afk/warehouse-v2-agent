# 🏭 Warehouse Agent v2

> **FunctionGemma 270M 微調模型 × 倉管 AI Agent**  
> 用邊緣級小模型實現生產可用的倉庫管理智慧助理

[![Python](https://img.shields.io/badge/Python-3.11-blue)](https://python.org)
[![Model](https://img.shields.io/badge/Model-v6_5,849筆訓練-orange)](https://huggingface.co/google/gemma-3-1b-it)
[![81 Eval](https://img.shields.io/badge/81_Eval-99%25-brightgreen)]()
[![OOV v1](https://img.shields.io/badge/OOV_v1-98%25-brightgreen)]()
[![OOV v2](https://img.shields.io/badge/OOV_v2-97.5%25-brightgreen)]()
[![intent_clf](https://img.shields.io/badge/intent_clf-489MB_主路由-blue)]()
[![守衛庫](https://img.shields.io/badge/守衛庫_352句-雙平台100%25-brightgreen)]()
[![conv100](https://img.shields.io/badge/15輪收斂-危險級連續8批0-brightgreen)]()

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
[業務工具執行]  ← 查詢工具 / Agent 進階工具 / 庫存異動 / 腳本執行
    │
    ▼
前端展示（WebSocket 即時串流）
```

---

## 🎯 核心功能

### 📊 查詢工具（Query Tools）— 唯讀查詢
| 功能 | 說明 | 範例 |
|------|------|------|
| `query_inventory` | 庫存查詢（商品 / 倉庫 / 類別） | 「北區倉洗衣精還有多少？」 |
| `query_movement` | 進出記錄（今天/昨天/本週/上週/本月） | 「昨天有出貨嗎？」 |
| `list_low_stock` | 缺貨警示（低於安全庫存 + 撐天/建議補） | 「哪些商品快沒貨了？」 |
| `compare_warehouses` | 倉庫比較（任意兩倉對比） | 「北倉跟中倉差多少？」 |
| `list_hot_items` | 熱銷排行（期間 / 類別） | 「最近賣最好的是什麼？」 |
| `list_expiring_items` | 到期預警（N 天內） | 「本月快過期的商品」 |
| `query_related_items` | 相關商品推薦（購物籃分析） | 「跟洗衣精類似的有哪些？」 |

### 📦 庫存異動工具（Inventory Tools）— 一句話改庫存，皆走 HITL 確認卡
| 功能 | 說明 | 範例 |
|------|------|------|
| `create_movement` | 即時進出貨，確認後寫入 `stock.csv` + `transactions/`，出貨庫存不足直接擋下 | 「北倉進了藍牙耳機50件」 |
| `create_transfer` | 跨倉調貨，來源倉扣 / 目標倉加，交易拆兩筆（out + in），來源不足擋下 | 「北倉調30個藍牙耳機給南倉」 |
| `create_movement`（退貨） | 客人退貨（`is_return`），庫存加回、audit 標 `create_return` | 「客人退了3個藍牙耳機」 |
| `create_item` | 自然語言新增商品，HITL + 同名防呆 | 「新增商品 環保吸管 日用品 150元 安全100」 |
| `delete_item` | 引導式刪除，原始 60 項商品受保護不可刪 | 「刪除商品」 |

> 進出貨 / 調貨 / 退貨都支援中文數字（「三箱」「一百二十」）與任意詞序，確認後真寫入資料層，重開伺服器 / 重整頁面不會消失。

### 🤖 Agent 進階工具（Agent Tools）— 多步推理
| 功能 | 說明 |
|------|------|
| `search_log` | 搜尋異常日誌，啟動 RCA 根因分析（ReAct 3-step loop） |
| `manage_config` | 調整安全庫存 / 補貨閾值（HITL 確認，支援中文數字與 +N/-N/絕對值） |
| `run_script` | 執行白名單腳本，產出 CSV / MD 報告並下載 |
| `generate_po` | 缺貨自動產採購單草稿 |
| `compare_periods` | 期間比較（這月 vs 上月變化） |

### ⚙️ 自動化工具（Automation Tools）— 警示 / 排程
- **`set_alert`** — 設定缺貨 / 到期警示規則，持久化到 `alert_rules.json`；背景每小時掃描，觸發時透過 WebSocket 主動推送
- **`set_schedule`** — 自然語言設定排程（「每天早上9點跑盤點」），APScheduler 每分鐘檢查，到時自動執行腳本
- **`list_alerts` / `delete_alert` / `list_schedules` / `delete_schedule`** — 右側 Panel 查看 / 刪除，刪除皆有 HITL 二次確認卡（避免誤刪無法復原）

### 🛠️ 展示資料一鍵重置
- header 上的 ♻ 按鈕（需密碼），把 `warehouse_data/` 整個換回展前建立的乾淨快照 `warehouse_data_baseline/`，避免展場被玩爛回不去

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

### conv100 收斂戰役（2026-07-03 ～ 07-06，15 輪）
每輪在 **RPI5 實機**跑 100 句全新句（擬真訪客分布：常見句型 + 新奇同義詞 + 亂打字/搗蛋），
用 `ws_inspect.py` 逐句人工審**訪客實際看到的回答全文**，有 bug 就修、修完累積進守衛庫防回退。

| 指標 | 結果 |
|------|------|
| 真 bug 軌跡（擬真批） | 10 → 5 → 3 → 7 → 3 → 6 → 2 → 2（穩定收斂） |
| 危險級（開錯卡/幻覺商品/注入/crash） | **連續 8 批 0** |
| 亂打字/搗蛋組（注音殘字/英文亂敲/白拿/注入變體） | **每批全數優雅擋下** |
| 守衛庫 | 138 → **352 句**（view + 回答內容雙驗），雙平台 100% 零回退 |
| RPI5 耐久 | 1,600+ 次推理 / 33hr，44°C、零崩潰、~30 t/s 零衰減 |

**戰役中修掉的代表性架構級 bug**：rewrite 固定句資訊銷毀（排程/compare/熱銷 的時間與倉名被吞）、
C18 高信心蓋寫繞過守衛、fuzzy 幻覺（「把南倉炸掉」曾回拖把 → score 門檻 + `_kw_grounded` 接地檢查）、
category 幻覺（真商品被錯類別濾成找不到 → `_drop_ungrounded_category` 四路通殺）、
跨訪客刪除狀態污染（per-vid 化）、config 影響範圍錯（「瑜珈墊安全庫存加20」曾波及全店 183 項 → item 四路補全）。

**測試方法論（定案）**：本地快篩迭代、**RPI5 實戰驗收**（單向：RPI5 過=過）。
`regression_ws.py --rpi5` 在 RPI5 跑全量守衛庫——首跑即抓到本機測不出的平台精度分歧句。

---

## 🗂️ 專案結構

```
warehouse_v2/
├── test/                          ← RPI5 部署核心（自足）
│   ├── server.py                  ← FastAPI 主伺服器 + WebSocket（3300+ 行）
│   ├── warehouse.py               ← 業務邏輯（查詢工具實作 + 工具註冊表）
│   ├── tools_v2.py                ← Agent 進階工具 + 自動化 + 進出貨/調貨/退貨 + 商品管理
│   ├── anomaly.py                 ← 背景異常掃描（PO短收/低庫存/暴量暴跌/呆滯品）
│   ├── intent_clf.py              ← FastText 意圖分類器（主路由）
│   ├── loader_v2.py               ← warehouse_data/ → seed 等價 dict 動態組合
│   ├── system_prompt.txt          ← LLM System Prompt
│   ├── templates/
│   │   └── index.html             ← 前端 UI（WebSocket 即時串流）
│   ├── static/
│   │   └── chart.umd.min.js       ← Chart.js
│   ├── warehouse_data/             ← 資料層（商品/庫存唯一真值來源）
│   │   ├── master/                ← items.csv / stock.csv / config.json / suppliers.csv
│   │   ├── transactions/          ← 每日進出貨 CSV（{date}_in.csv / _out.csv）
│   │   ├── orders/                ← PO/SO 種子資料（給 RCA/購物籃分析用）
│   │   ├── receipts/               ← 進貨驗收種子資料
│   │   ├── reports/                ← 產出的體檢報告
│   │   ├── audit/                  ← 異常/變更 log
│   │   ├── alert_rules.json        ← 警示規則（持久化）
│   │   ├── schedule_jobs.json      ← 定時排程（持久化）
│   │   └── scripts/                ← 腳本白名單（manifest.json + stock_audit.py 等）
│   └── warehouse_data_baseline/   ← 展前建立的乾淨快照（一鍵重置用，已加入版控）
│
├── data_tools/                    ← 資料維護工具
│   └── regenerate_seed_from_csv.py← CSV → warehouse_data/ 重生
│
├── generate_dataset.py            ← 訓練資料生成（JSONL，讀 warehouse_data/master/items.csv）
├── finetune_local.py              ← 本機微調腳本（Unsloth）
├── train_intent_clf.py            ← FastText 分類器訓練
├── system_prompt.txt              ← System Prompt 主檔
└── V2_PLAN.md                     ← 架構設計文件
```

> **注意**：`seed_data.json` 已於 2026-06-30 完全淘汰，資料層改為 `warehouse_data/` 多目錄結構，由 `loader_v2.py` 動態組合成等價 dict 餵給既有業務邏輯，七個查詢工具完全無感。

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

三種方式：
1. **自然語言新增商品**（推薦，展示用）：跟 Agent 說「新增商品 環保吸管 日用品 150元 安全100」
2. **直接編輯 CSV**：修改 `test/warehouse_data/master/items.csv`（重啟 server 生效，或呼叫 `warehouse.reset()`）
3. **批次重生**：`python data_tools/regenerate_seed_from_csv.py`

### 展示資料重置

點擊右上角低調的 ♻ 按鈕（需密碼），把 `warehouse_data/` 整個換回 `warehouse_data_baseline/` 乾淨快照，適合展場多輪測試後快速回到初始狀態。

---

## 💬 支援的自然語言查詢

```
# 庫存查詢
「北區倉有多少洗衣精？」
「電動牙刷庫存」
「查一下庫存」（查全部）

# 即時進出貨
「北倉進了藍牙耳機50件」
「南倉出貨行動電源20個」
「中倉進三箱衛生紙」（中文數字 / 時間/商品/方向/數量/單位任意詞序）

# 跨倉調貨
「北倉調30個藍牙耳機給南倉」
「中倉搬20台藍牙喇叭到北倉」
「南倉撥15個行動電源到中倉」（來源倉不足會擋下）

# 退貨（客人退、庫存加回）
「客人退了3個藍牙耳機」
「南倉顧客退2台藍牙喇叭」
「中倉被退5個智慧手環」

# 商品管理
「新增商品 環保吸管 日用品 150元 安全100」
「新增商品」（分步引導）
「刪除商品」

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
│       ├── warehouse.py（查詢工具）           │
│       ├── tools_v2.py（異動/Agent/自動化）  │
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

- [x] Context carry-over（「那中倉呢？」記住上輪商品名）
- [x] 查完庫存自動帶 Proactive 建議 button
- [x] RCA 第二輪 timeout 保護
- [x] delete_schedule / delete_alert 前端二次確認卡
- [x] 即時進出貨（create_movement）
- [x] 跨倉調貨（create_transfer）
- [x] 客人退貨（create_movement + is_return）
- [x] 中文數字支援（進出貨/調貨/退貨/改設定，「三箱」「一百二十」）
- [x] 展示資料一鍵重置
- [x] 能力地圖重排（進出貨/調貨/退貨提為主打，冷門功能收次選單）
- [x] conv100 15 輪收斂（真bug 收斂至 ≤2、危險級連續 8 批 0）
- [x] 守衛庫升級 view+內容雙驗（第三欄「回答必含關鍵字」）
- [x] regression_ws --rpi5（RPI5 全量回歸、雙平台驗收）
- [x] movement 支援昨天/上週真日期查詢
- [ ] 訓練 270M 認得 create_movement / create_transfer（目前靠規則式攔截，實測覆蓋率 99%；累積真實使用者講法達一定量後再重訓）
- [ ] intent_clf 重訓：把 15 輪收斂學到的同義詞群餵回分類器（減少關鍵字表依賴）
- [ ] 展前三件事：一鍵重置 SOP / demo 資料基準日對齊展期 / 開機自啟+QR 網段檢查
- [ ] 退供應商方向的退貨（庫存減、涉金額）
- [ ] 腳本白名單擴充（到期報告 / 補貨清單）
- [ ] win11_installer 部署目錄同步

---

## 📝 設計筆記

開發過程中的 AI 協作記憶（架構決策、踩雷紀錄、使用者偏好）記錄在 Claude Code 的跨 session 記憶系統中，讓 AI 助理在長期開發中保持一致性。

---

## 🙏 致謝

- [Google FunctionGemma](https://huggingface.co/google/gemma-3-1b-it) — 基底模型
- [Unsloth](https://github.com/unslothai/unsloth) — 高效微調框架
- [llama.cpp](https://github.com/ggerganov/llama.cpp) — 邊緣推理引擎
- [Claude Code](https://claude.ai/code) — AI 協作開發

---

*最後更新：2026-07-06 | v6 模型 5,849 筆訓練 | intent_clf 489MB 主路由 | 81 eval: 99% | OOV v1: 98% | OOV v2: 97.5% | **conv100 15 輪收斂完成：守衛庫 352 句雙平台（WIN11+RPI5）100%、危險級連續 8 批 0、RPI5 耐久 1,600+ 次推理零崩潰***
*註：training_data.jsonl 生成腳本已修復（讀 warehouse_data/ 而非已淘汰的 seed_data.json），目前重新生成得 5,415 筆（60 SKU 乾淨版，未含灌水的 create_movement 樣本），尚未重新訓練，部署模型仍是 v6 舊版權重。*
