# 🏭 Warehouse Agent v2

> **FunctionGemma 270M 微調模型 × 倉管 AI Agent**  
> 用邊緣級小模型實現生產可用的倉庫管理智慧助理  
> **中文版（8001）／英文版（8002）雙語並存，全離線跑在 Raspberry Pi 5**

[![Python](https://img.shields.io/badge/Python-3.11-blue)](https://python.org)
[![Model](https://img.shields.io/badge/Model-FunctionGemma_270M-orange)](https://huggingface.co/google/gemma-3-1b-it)
[![中文守衛](https://img.shields.io/badge/中文守衛_1122句-RPI5_100%25-brightgreen)]()
[![英文守衛](https://img.shields.io/badge/英文守衛_892句-891_99.9%25-brightgreen)]()
[![劇情批](https://img.shields.io/badge/劇情批_r1--r5+語音+大小寫-全綠-brightgreen)]()
[![intent_clf](https://img.shields.io/badge/intent__clf-6MB量化_99.68%25-blue)]()
[![語音](https://img.shields.io/badge/語音-中文FunASR_/_英文whisper_tiny.en-blueviolet)]()
[![離線](https://img.shields.io/badge/展場-100%25離線可用-success)]()

---

## 📚 文件導覽（接手先看這裡）

| 文件 | 內容 | 什麼時候看 |
|---|---|---|
| **README.md**（本檔） | 專案是什麼、有什麼功能、怎麼跑 | 第一次接觸 |
| **[DEV_NOTES.md](DEV_NOTES.md)** | **踩雷、測試方法論、實測數據** | 要改東西之前 |
| **[RPI5_RESTORE.md](RPI5_RESTORE.md)** | 從零部署／重灌還原（14 節 + 驗收清單） | RPI5 壞了、換機 |
| [../TRAINING_BACKLOG.md](../TRAINING_BACKLOG.md) | 待辦與決策紀錄 | 想知道為什麼沒做某件事 |

> 只有這四份，**刻意維持精簡**——文件太多會讓接手的人（或 AI）
> 讀一堆過時內容、浪費時間還可能被誤導。
> 2026-07-27 已清掉四份過時文件（收斂總結報告 ×2、V2_PLAN、寫入操作規範），
> 其中仍有效的結論（寫入契約鐵律、270M 能力邊界）**已併進 DEV_NOTES**。

> 💡 這些文件**不依賴任何 AI 助理的記憶系統**——不管接手的是新同事、
> Claude Code、Hermes 或別的工具，讀這幾份就能接上。
> 改動系統時請一併更新對應文件。

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
[intent_clf 主路由]  ← FastText 分類器（意圖分類 98.9%；英文版量化後僅 6MB）
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
- **intent_clf 主路由**：FastText 分類器先決定 function，LLM 只抽參數
  （英文版實測**量化到 6MB 準確率完全不掉**：`quantize(qnorm=True, cutoff=100000)`
  存成 .ftz 直接命名 `intent_clf.bin` 即可，fasttext 會自動辨識格式）
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

### 第二次收斂戰役 + 短句空間認證（2026-07-11 ～ 07-14，r16-r31 共 16 輪）
每輪換**全新攻擊面**出 100 句（詞彙邊緣同義、語氣反轉、多錯字疊加、台語、倒裝、
極長句、社工搗蛋、時段長尾、功能衝突複合……單句角度累計掃過 35+ 種）。

| 指標 | 結果 |
|------|------|
| 真 bug 軌跡（r24-r30，每輪新角度） | 12 → 16 → 16 → 16 → 10 → 6 → 6（修族化後穩定下降） |
| 危險級 | 持續 0（搗蛋/注入/白拿/錯值卡全擋） |
| **短句全枚舉認證（r31）** | **953 句（60 商品短稱 × 12 模板＋裸名＋類別＋功能短句）雙平台 100%** |
| 守衛庫 | 352 → **855 句**，雙平台 100% 零回退 |

**產品定位（定案）**：「**短短的自然語言（2~12 字、含錯字/注音/英文俗稱）→ 一秒拿到
正確答案，全程 RPi5 CPU**」。短句空間有限可窮舉＝可證明的產品保證；長句（>30 有效
字元）不進 LLM，確定性層接得住就答（56 字長句靠直達照答），接不住優雅引導。

**核心工程鐵律（16 輪淬鍊）**：任何 hard-return / rescue / clf-skip 出口都要**自帶
參數接地與推導**（倉別/類別/期間/數值/kw 真商品驗證），不能指望後面的層——本戰役
八次中招全是這一條。回歸自此雙套：`regression_ws.py`（守衛庫）＋
`regression_ws.py --file _sweep_r31.txt`（短句掃蕩），兩套雙平台全綠才 commit。

### 多輪流程掃蕩（2026-07-14，r32）

前 31 輪的測試工具都是「一句一條 WS 連線」，而 session state（context / 新增商品
流程 / 確認卡）綁在連線上——**跨輪行為從來沒被測過**。新工具
`ws_convo.py` 在同一條連線上連發整個劇本（可重播「按確認」按鈕），
`convo_r32.txt` 29 情境 111 輪：確認卡出現後亂回話、追問鏈、寫入流程中途插隊。

首跑挖出 6 個真 bug，根因兩條：

| 根因 | 症狀 | 修法 |
|------|------|------|
| 確定性直答不寫 context（`_update_ctx` 只掛 LLM 路徑，r24-r31 新增的數十個 dispatch hard-return 出口全繞過） | carry-over 被架空：「無線滑鼠還剩幾個」→「那個進出紀錄呢」回**全部商品**統計 | `send(done)` 單一咽喉統一 `_ctx_absorb`，未來新出口自動涵蓋 |
| server 沒有確認卡記憶（pending 只活在前端 DOM） | 對卡片說「好」被守門員拒；說「不對是100個」→ 100 被 match 成「運動毛巾 100x30cm」幻覺回庫存 | `_pending_by_vid` + 引導層（寫入授權**只認按鈕**，打字一律引導） |

另修危險級一枚：新增商品流程中說「算了」→ meta-gate 只回 clarify **沒清流程狀態**
→ 後續每一句都被吞成商品欄位（流程劫持）。

**鐵律新增一條對偶**：hard-return 出口不只要「自帶參數接地」，也要「**寫回 context**」。

### 多輪四輪戰役（2026-07-14 ～ 07-15，r32-r35）

| 輪次 | 攻擊面 | 真 bug |
|------|--------|--------|
| r32 | 主幹：確認卡後亂回話 / 追問鏈 / 寫入流程插隊 | 6（危險1：流程劫持） |
| r33 | 放棄詞長尾 / 確認的曖昧回應 / 追問邊緣 | 7 |
| r34 | 多人交錯（`>@B` 兩條連線）/ 卡片競態 / 長對話漂移 / 寫入鏈 | 6 |
| r35 | 訪客不理性：反悔鏈 / 追問打錯字 / 省略到一個字 | 8 |

**代表性修補**：統一 abort 閘門（放棄是跨情境意圖，必須在守門員之前處理）、
追問展開涵蓋寫入方向（「有賣滑鼠嗎」→「北倉進20個」）、極限省略接地
（「南」「呢」「北倉多少」）、追問句錯字正規化（「那個**近**出紀錄呢」）、
單品缺貨判定（讀 `safety_stock_override` 覆寫層）、單品分倉極值。

**多輪空間尚未收斂（軌跡 6→7→6→8）**，但原因不是工程品質——這四輪的 bug 幾乎
全是「短句追問」（南/呢/北倉多少/進出）。多輪短句空間 ≈ 953 單句 × N 種追問形，
比單句大一個數量級，四輪只是隨機採樣不同角落。要達到 r31 等級的**可證明**保證，
需比照做全枚舉（`gen_convo_sweep.py`：60 商品 × 首句型 × 追問形），把「短句＝
產品本體」的保證從單句延伸到多輪。

回歸自此五套（守衛 855 ＋ 掃蕩 953 ＋ 多輪劇本 r32-r35 共 102 情境 400+ 輪），
全部雙平台 100%。工具：`ws_convo.py`（同連線劇本、可重播確認鍵、`>@B` 多訪客
交錯、`--reset` 防劇本互相污染、握手逾時自動重連）。

---

## 🎙️ 語音輸入（Voice POC，2026-07-20 ～ 07-21）

展場訪客用**講的**查倉管（比打字自然）。ASR 全程跑在 RPI5 CPU，**完全離線**。

### 全鏈架構

```
訪客講話（前端 Siri 式：點一下 → 講 → 靜音自動結束）
    │
    ▼
[瀏覽器 MediaRecorder 錄音 + AudioContext VAD 靜音偵測]
    │
    ▼
[/api/asr]  ffmpeg 轉 16k mono → Fun-ASR-Nano（GGUF, llama.cpp）
    │
    ▼
[OpenCC s2twp]  簡體 → 繁體（順便轉台灣用語）
    │
    ▼
[同音修正層 _asr_normalize]  倉別/動詞/量詞/異體字（掛 /api/asr 出口，不碰倉管核心）
    │
    ▼
[倉管 WS]  ← 既有守衛庫 + 發音容錯層接手
```

### 兩大容錯層（真人聲實測磨出來的）

- **發音容錯層**（`warehouse.py`）：ASR 錯字多為「同音字形遠」（滑鼠→華數/華族），
  字形比對救不到 → **轉拼音比對** + **捲舌音節還原**（zh/z 混淆）。字形優先、發音救底，
  零回歸。門檻 0.82 防誤配（實測「衛生棉 vs 衛生紙」同分過不了門檻）。
- **語音同音修正**（`server.py` `_asr_normalize`）：只掛 `/api/asr` 出口、**不碰倉管核心**
  → 打字訪客零影響、守衛零風險。涵蓋倉別（總/藏/昌→中/倉）、動詞（近→進、谷→補）、
  量詞（臺→台）、異體字（溼→濕、賬→帳、周→週）等真人聲實測抓到的錯法。

### 三環境噪音測試（同一份真人錄音自動混噪，念一次測三種）

| 環境 | 通過率 | 說明 |
|------|--------|------|
| 乾淨（正常音量） | 78% | 系統實際 ~87%（扣除多輪測試架構的假失敗） |
| light（一般展場 -18dB） | 77% | **噪音幾乎零影響（-1%）** |
| heavy（尖峰吵雜 -8dB） | 73% | 輕微影響（-5%），純噪音壞的僅 5 句 |

**結論：webcam + 270M + 正常音量＝展場夠用，不需升級模型或麥克風。** 失敗幾乎全是
ASR 整詞聽錯（訪客看辨識文字重講可解），非系統 bug。關鍵變數是**音量**（小聲時
摩擦音糊掉），展場正常音量對麥講即可。

### 工具（`voice_poc/`）

`read100.sh`（100 句真人測試 + 即時分貝計 + 存錄音）、`noise_retest.sh`（用存檔自動
混噪重測）、`test_asr_norm.py`（同音修正護欄，改規則前必跑）、`check_mic.sh`（麥克風
六項體檢）、`calib_vad.py`（VAD 門檻校準）。

---

## 🌍 英文版（2026-07-25 ～ 07-27）

老闆要**全英文版**（介面 + 問答 + 語音都英文）。策略：**中文版凍結當基準、
英文版獨立並存**，兩版可同時跑、可單獨跑。

| | 中文版 | 英文版 |
|---|---|---|
| repo 目錄 | `test/` | `en/` |
| RPI5 目錄 | `~/warehouse_v2/` | `~/warehouse_v2_en/` |
| Port | 8001 | **8002** |
| 開機自啟 | ✗（桌面捷徑手動起） | **✓** |
| 模型 | 中文微調版 | **英文微調版**（獨立訓練） |
| 語音 | Fun-ASR-Nano | **whisper tiny.en** |

### 為什麼不是「翻譯層」而是重訓一顆
探針實測（拿現有中文模型餵英文句）：乾淨查詢答得出來，但**招牌能力全滅**
——英文錯字、模糊描述、寫入/RCA 意圖全部歸零。純翻譯版＝只能乾淨英文查詢、
容錯 Agent 全失效。三方對照（eval_en 34 句）：

| 模型 | 命中 | 特徵 |
|---|---|---|
| base 未微調 | 4/34 (11%) | 看得懂英文，**不懂這套系統的 tool 慣例** |
| 中文微調版 | 11/34 (32%) | tool 慣例**跨語言遷移**，但搗蛋句會硬湊 |
| **英文微調版** | **25/34 (73%)** | 基本查詢 12/12、錯字全中、RCA 3/3 |

⇒ 微調買的不只是語言，是**領域判斷**。

### 移植過程的核心教訓
> 真正的工作量不在模型，在**散落各處的語言相關守衛**。
> 找法＝**逐句追 log 看實際執行路徑**，不要只看輸入輸出猜。

歸納出 19 類反覆出現的坑（完整清單見開發記憶），最典型的幾類：

| 坑 | 說明 |
|---|---|
| **中文鍵的對照表** | dict 的鍵是中文、值才是 slug → 整條功能對英文靜默失效（類別查詢 6 類壞 5 類） |
| **中文字元制的長度門檻** | 英文字元數是中文 2-3 倍，`len(text) > 30` 之類在英文全部提早觸發 |
| **短字串 substring 必誤爆** | `"po"` ∈ re**po**rt、`"quit"` ∈ mos**quit**o → 英文一律要求詞界 `\b` |
| **演算法假設了中文分詞** | `split()[0]` 剝規格尾巴在中文安全、**英文商品名被腰斬**（曾改錯商品的安全庫存） |
| **ASR 大小寫打穿規則層** | whisper 一律首字大寫，而 21 個英文詞表全小寫 → 校正層根本執行不到 |

### 收斂成果
- **守衛庫 892 句**：651 → **891/892 (99.9%)**
  唯一 FAIL `do we have scks` 刻意不修——`scks→socks` 與 `hair→chair`
  在字元層面完全相同（都是 insert、都是 0.889），要區分需英文詞典依賴。
  **判斷「該留給補訓」的標準：有沒有可用訊號能區分正例與反例。**
- **劇情批 r1-r5**（跨句 context）+ **語音批** + **大小寫批** 全綠
  - r5 補三個未測向量：**並發**（兩訪客同時操作，vid 隔離 / pending 不互踩 /
    同 SKU 競態無 lost update）、**超長 context**（24 句不漂移）、
    **資料極值**（出貨清成 0 庫存，無除零/NaN/負數）

---

## 🎤 英文語音（whisper.cpp）

Fun-ASR（阿里）→ **whisper tiny.en**（OpenAI），符合「只用歐美模型」的約束。

| 模型 | 延遲 | WER 乾淨 | 備註 |
|---|---|---|---|
| **tiny.en (74MB)** | **0.94s** | **9.3%** | ✅ 選它 |
| base.en (148MB) | 2.33s | 10.2% | **更大反而沒更準** |
| Fun-ASR（中文） | 2.45s | — | 換掉 |

倉管查詢句短、句型固定，tiny 容量已夠 ⇒ 比原本**快 2.6 倍**。

**英文語音原本是結構性不可用**：取字邏輯寫死「取最後一行**含中文**的輸出」
→ 英文結果不含中文字 → 一律回「聽不出內容」。

⚠️ **WER 高估了實際失敗率**——要看端到端答對率：
`Powerbank Inventory`→✅ 合成詞拆解、`North receive 50 wireless mouse`→✅
正確開卡（時態/數字不影響路由）。**文字端的容錯層在扛**。

---

## 🖥️ RPI5 部署 / 還原

完整還原手冊：**[`RPI5_RESTORE.md`](RPI5_RESTORE.md)**
（寫到「新對話拿到就能直接還原」的程度，14 節 + 驗收清單 + 禁止事項）

配套設定檔全部納管在 [`en/rpi5/`](en/rpi5/)：systemd unit、`server_https.py`、
`launch_warehouse.sh`、桌面捷徑腳本、watchdog、QR 產生器。

手冊裡最容易漏的三件事：
1. **模型檔不在 git**（`.gitignore` 排除 `*.gguf`/`*.bin`）——要另外復原，
   且**不要用 ZeroTier 傳**（頻寬低必斷成殘檔，md5 不符還很難查）
2. **HTTPS 憑證必須帶 SAN**——沒有的話首頁能開但 **wss 靜默拒絕**，
   畫面卡 Loading 且 server 零連線紀錄（最難查的一種壞法）
3. **禁止事項那節**——`pkill -f python`、venv 跑 systemd、SSH 直接起
   chromium，都是踩過才寫進去的

### 展場整備
- kiosk **125% 縮放**（15 吋 1920×1080 實測最佳：可視高 864px、
  快捷列一次顯示 11 顆按鈕、寫入確認卡一屏放得下不用捲）
- **頂部三條收窄 29%**（header 40→24px）——關鍵是用 **id 選擇器**覆蓋
  標題列按鈕，它們各自寫死 width/height，class 權重蓋不掉
- **三個中文彈窗清零**（旗標警告 / Google 翻譯 / 崩潰復原）
- 桌面：中文命名捷徑 ①②③ + 中英兩張 QR

### 遠端維護：像訪客一樣操作畫面
[`en/drive_kiosk.py`](en/drive_kiosk.py) 透過 CDP（9222，只綁 127.0.0.1）
填輸入框 → 送出 → 等回答 → **截圖**。

> 補上「審到畫面」的最後一哩：`ws_convo.py` 走 WebSocket 只看得到 JSON，
> 這支看得到**訪客實際看到的渲染結果**。
> 而 `getBoundingClientRect()` 量測比截圖更精準——「標題列太粗」用目測
> 只知道粗，量測直接指出是 `#close-btn`(36px) 撐高的。

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
├── en/                            ← 🌍 英文版（活躍開發，RPI5 8002）
│   ├── server.py / warehouse.py / tools_v2.py …  ← 與 test/ 同構，可 diff 對照
│   ├── templates/index.html       ← 英文 UI（含 body.compact-top 精簡頂部）
│   ├── item_names_en.py           ← 60 商品英文名 + 別名/俗稱
│   ├── alias_en.py                ← 英文俗稱對照（battery pack → Power Bank…）
│   ├── descriptor_en.py           ← 功能描述層（訪客講不出商品名時）
│   ├── gen_en_dataset.py          ← 英文訓練語料生成（6284 筆純英文）
│   ├── train_intent_clf_en.py     ← 英文 FastText（99.68%，量化後 6MB）
│   ├── gen_guard_en.py            ← 英文守衛庫生成 → regression_corpus_en.txt
│   ├── drive_kiosk.py             ← 🆕 CDP 驅動 kiosk（像訪客一樣打字 + 截圖）
│   ├── _conv_en_r1~r5.txt         ← 劇情批（跨句 context）
│   ├── _conv_en_voice.txt         ← 🆕 語音回歸批（句子全是 /api/asr 真實輸出）
│   ├── _conv_en_case.txt          ← 🆕 大小寫回歸批（ASR/手機輸入法產物）
│   ├── voice/                     ← 英文語音評測工具（whisper 選型）
│   └── rpi5/                      ← 🆕 RPI5 部署設定檔（systemd/kiosk/桌面腳本）
│
├── data_tools/                    ← 資料維護工具
│   └── regenerate_seed_from_csv.py← CSV → warehouse_data/ 重生
│
├── generate_dataset.py            ← 訓練資料生成（JSONL，讀 warehouse_data/master/items.csv）
├── finetune_local.py              ← 本機微調腳本（Unsloth）
├── train_intent_clf.py            ← FastText 分類器訓練
├── system_prompt.txt              ← System Prompt 主檔
├── RPI5_RESTORE.md                ← 🆕 RPI5 完整還原手冊
├── RPI5_RESTORE.md                ← RPI5 部署／還原手冊
└── DEV_NOTES.md                   ← 踩雷、測試方法論、實測數據
```

### 測試工具（都在 `test/` 與 `en/`）

| 工具 | 測什麼 | 抓得到哪類問題 |
|---|---|---|
| `regression_ws.py` | 守衛庫全量（單句） | 回歸——**唯一防線**，小批探針看不出 |
| `ws_convo.py` | 劇情批（同連線跨句） | carry-over、pending 卡互動、並發 vid 隔離 |
| `check_views.py` | server 發的 view vs 前端渲染器 | summary 承諾了但畫面畫不出來 |
| `branch_walk.py` | clarify 每個選項 + 序數路實走 | 選項誤配（點 A 得 B） |
| `drive_kiosk.py` | CDP 真實操作 + 截圖 | 只有渲染才看得到的（文字擠在一起、被截斷） |
| `check_zombies.sh` | 殘留程序 | 測試前必跑——殘留會拖垮 4 核 8GB 的 RPI5 |

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

**本機開發**（http，前端 WS 走 ws://）：
```bash
cd warehouse_v2/test   # 中文版
python server.py       # → http://localhost:8000
```

**本機模擬展場**（https，**語音與 wss 必須走這個**）：
```bash
cd warehouse_v2/en && python run_local_https.py   # 英文版 → https://localhost:8002
```
> ⚠️ 不能只跑 `server.py`：前端 WS **跟隨頁面協定**（https 頁面發 wss），
> 純 http server 會**卡 Loading、送不出字**；麥克風權限也只有 https/localhost 才給。

**RPI5 展場**：見 [`RPI5_RESTORE.md`](RPI5_RESTORE.md)（systemd 開機自啟 8002）

### 模型檔（需自行準備）
模型權重因超過 GitHub 限制未包含在此 repo（`.gitignore` 排除 `*.gguf`/`*.bin`）：

| 檔案 | 大小 | 放哪 |
|---|---|---|
| 中文版 GGUF | 291MB | `test/models/functiongemma-270m-it-fine-tune.q8_0.gguf` |
| **英文版 GGUF** | 291MB | `en/models/en_q8_0.gguf`（md5 `213593b5…`） |
| 英文 intent_clf | 6MB | `en/intent_clf.bin`（量化版，800MB→6MB 準確率不掉） |
| whisper tiny.en | 74MB | RPI5 `~/whisper.cpp/models/ggml-tiny.en.bin` |

⚠️ **模型版本一律用 md5 對照**，別靠檔名——中英兩顆檔名相近但能力差很多
（曾誤判 8002 跑的是中文模型）。

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
- [x] 第二戰役 r16-r31：新攻擊面 16 輪 + 短句全枚舉 953 句雙平台 100% 認證
- [x] 長度閘門（>30 字不進 LLM）+ RPI5 網路自癒（省電關閉+watchdog）
- [x] 守衛庫升級 view+內容雙驗（第三欄「回答必含關鍵字」）
- [x] regression_ws --rpi5（RPI5 全量回歸、雙平台驗收）
- [x] movement 支援昨天/上週真日期查詢
- [x] r32-r35 多輪四輪戰役（27 真bug：流程劫持/carry-over 復活/確認卡口語層/統一 abort 閘門/極限省略接地）
- [ ] 多輪短句全枚舉 gen_convo_sweep.py（多輪空間軌跡 6→7→6→8 未收斂，需比照 r31 窮舉才能給可證明保證）
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

*最後更新：2026-07-27*

**中文版**：v6 模型 5,849 筆訓練 | 35 輪收斂：守衛庫 1122＋短句全枚舉 953
＋多輪劇本 2069 情境，雙平台（WIN11 + RPI5）100%、危險級持續 0 | 語音全鏈離線

**英文版**：獨立微調（6,284 筆純英文語料）| intent_clf **量化 6MB**（800MB→6MB
準確率完全不掉，99.68%）| 守衛庫 **891/892 (99.9%)** | 劇情批 r1-r5 + 語音批
+ 大小寫批全綠 | 語音 whisper tiny.en **1.1s/句** | RPI5 8002 開機自啟
