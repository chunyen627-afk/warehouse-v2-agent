# 倉管 v2「真 Agent」實作計畫書

> 對齊日期：2026-06-22
> 範式承襲 v1（已驗證）：LLM 抽 keyword → server 校正 → match → 動態算。

---

## ✅ v2.1「更開放 Agent」成果（2026-06-23）

把「半有→真有」：讓 Agent 能寫檔、動態找檔。對照業界（270M=路由器、3B-8B 才做決策）後，A+B+C 漸進實作。

| 波 | 能力 | 270M 表現 | 結果 |
|---|---|---|---|
| **A** | `generate_report` 掃全倉寫 markdown 報告→reports/（4段：總覽/缺貨/到期/PO異常） | ✅ 學會 | 真有，訓進模型 |
| **B** | `list_files` 動態 ls warehouse_data/ 看有哪些檔可讀（沙盒防護） | ✅ 學會 | 真有，訓進模型 |
| **C** | `judge_cause_found` 讀 context 判 yes/no 自主決策 | ❌ 學不起來（1/6） | **270M 極限**，決策回歸 server 編排 |

- **重訓**：max_length 1152→1216（容納 C 波 context，VRAM 7.3GB 不撞雷4），loss 0.028。
- **校正新增**：C12（報告意圖→generate_report，蓋過 C3/C7）、C13（找檔→list_files）。
- **端到端複驗**：A/B/三金剛/v1 共 18 案路由全綠、報告真寫檔、報告意圖 vs 查詢意圖 6/6 分清。
- **結論**：A+B 是 270M 能做的「真開放」；C（自主決策）證實是尺寸極限，業界用 3B+ 才做 → 留 server 編排或未來換 base。判 declaration 不進 prompt。
- **GGUF**：v2.1 部署 test/models/ + win11；v2.0 已驗證版備份在 `~/.cache/.../v2.0_verified.q8_0.gguf` 可回滾。

---

## ✅ 終極版（2026-06-23）— 補完所有規劃功能（15 function）

把規劃過沒做的全部實現（都在 tools_v2.py，校正 C14-C16，前端 view 全加）：
- **set_alert（第四金剛 Edge Alerter）**：半固定 enum condition（below_safety|out_of_stock|expiring）+ target keyword → 寫 `alert_rules.json`，**串 anomaly.py**：被訂閱商品的異常 ⭐ 升級。
- **generate_po（閉環）**：缺貨清單 / PO 短收 → 自動產採購單草稿（含供應商+金額）→ HITL 確認 → 寫 `orders/PO_draft/`。
- **compare_periods（跨期比較）**：近兩個月出庫量，找變化最大 SKU（成長/衰退）。
- **報告加圖表**：generate_report 嵌 matplotlib PNG（各倉市值長條 + 缺貨撐天橫條），`/reports/{fname}` 端點供讀（路徑穿越防護）。
- **UX 引導**：開場「能力地圖」（6 類事 + 可點範例）+ 動態引導（讀當下異常丟具體可點問題）→ 解「知道能打字但想不出問什麼」。
- **驗收**：端到端 12/12（含 4 新功能路由）+ 圖表端點 200 + 路徑穿越擋 404 + set_alert 串 anomaly 訂閱升級 + v1/三金剛零回歸。

---

## ✅ 主動異常偵測（2026-06-23）— 業界標準 anomaly pipeline

不靠使用者問，系統自己掃全倉找問題。判準全是「規則寫得出來」的確定性偵測 → 用 Python（`anomaly.py`），不丟 270M。

- **架構**：背景線程定時掃（`AnomalyConfig.scan_interval_s` 預設 300s）→ AlertManager（分級+去重+告警抑制 `suppress_hours`）→ Notifier 分發層。
- **5 類偵測**：PO 短收(critical) / 跌破安全線·快斷貨(critical/warning/info) / 出庫暴量(warning,>3σ統計門檻控誤報) / 快到期囤貨(warning) / 呆滯品(info)。實測 6 critical + 7 warning + 41 info。
- **Notifier 分發層**：[真接] WS 站內推播 + `audit/*_alerts.log` 留底；[接口預留] Slack/Teams webhook、LINE Notify、Email SMTP（填 env 即生效，換管道是設定問題不是工程）。
- **雙軌**：背景推 + `/anomalies` 端點手動拉。
- **前端**：進站自動拉一次（`pullAnomalies`）彈異常橫幅（紅/橙/黃分級可展開）+ WS `anomaly_alert` 即時更新。
- **凍結資料**：模擬時鐘（今天=snapshot_date）。
- 關鍵設計判準：**規則寫得出來 → 用程式（可靠、不幻覺）；寫不出來要模糊判斷 → 才用模型，且要 3B+**。異常偵測屬前者。

---

## v2.0 梯次（三金剛 + 多檔資料層）

---

## 0. 已鎖定的對齊決策（不再變動）

| # | 決策 | 結論 |
|---|---|---|
| D1 | Loop 架構 | **Host 編排**。模型只出單步 JSON（+v2.1 的 B 類小標籤）。270M 不跑自由 reasoning loop。 |
| D2 | set_alert.condition | **半固定 enum** `below_safety \| out_of_stock \| expiring`，target 走 keyword。自由條件留 v3。→ **v2.1 才做** |
| D3 | choose_next_source | **不交給模型**。追查順序 `log→orders→config` 由 server 規則固定，模型只做 `judge_cause_found`（v2.1）。 |
| D4 | 重訓梯次 | **v2.0**=A 類三金剛（一次重訓）；**v2.1**=B 類 judge_cause_found（壓縮 context 試訓）+ set_alert。 |
| D5 | keyword 免重訓鐵則 | 會變動清單（SKU/log 檔名/設定項/腳本名）一律 keyword 抽取 + server match，**絕不寫進 enum**（雷 6）。 |

---

## 1. v2.0 Function Declarations（確定版，3 個新 function）

```text
search_log(keyword, time_range?, source?)
  keyword:    自由抽取（RCA 關鍵字，如「藍牙耳機」「扣帳異常」）
  time_range: today | this_week | this_month        ← 沿用 v1 period enum
  source:     keyword 抽取（log 檔名片段，server 比對，不寫 enum）
  切界 vs query_movement：
    query_movement = 聚合統計（進出量、條形圖）
    search_log     = 逐筆追異常（RCA）
    觸發詞拉開：「對不上 / 異常 / 誰改的 / 查原因 / 怎麼少這麼多 / 扣帳」

manage_config(action, key, value?, warehouse?)
  action:     read | set            ← 用 set 不用 write（避免跟「寫庫存」混淆）
  key:        keyword 抽取（設定項名，如「安全庫存」「補貨前置天數」，server 比對，不寫 enum）
  value:      寫入值；server 端判斷「+30」(增量) vs「50」(絕對值)
  warehouse:  north | central | south | all          ← 撐「南倉全部+30」招牌題
  護欄：唯一會寫入 → 訓練樣本只教抽意圖；server 端二次確認 + 寫前 .bak

run_script(script_name)
  script_name: keyword 抽取（server 比對 manifest.json 白名單，不寫 enum）
  護欄：v2.0 砍掉 args（注入/型別風險）。白名單腳本穩了 v3 再加。
```

合計：v1 七個 function → v2.0 **十個** function。

---

## 2. 資料層遷移：seed_data.json → warehouse_data/

### 2-1. 來源現況（seed_data.json schema v3）
items 60 / stock 3倉 / movements 5687 / orders 1519 / batches 154 / shelf_life 17 / association_meta

### 2-2. 目標多檔結構
```
warehouse/test/warehouse_data/
├─ master/
│  ├─ items.csv                  ← 60 SKU（sku_id,name,category,unit_price,shelf_life_days）
│  ├─ suppliers.csv              ← 供應商（新增，撐 RCA「PO 對不上」對供應商）
│  └─ config.json               ← 設定項清單（safety_stock 各倉、補貨前置天數、安全水位倍數）
│                                  ← manage_config 的讀寫目標 + key 比對清單來源
├─ transactions/
│  └─ <YYYY-MM-DD>_<in|out|adjust>.csv   ← movements 按日切檔（5687 筆 → ~90 天 × 3 類）
│                                          ← search_log 的 Glob 目標、逼模型抽 source
├─ orders/
│  ├─ PO/<po_id>.json            ← 採購單（在途，撐補貨/RCA 對單）
│  └─ SO/<so_id>.json            ← 銷售單（1519 orders → SO，撐連帶分析既有資產）
├─ audit/
│  └─ <YYYY-MM-DD>_changes.log   ← 異動留底（manage_config set 寫這裡）
└─ scripts/
   └─ manifest.json              ← run_script 白名單（腳本名 enum 清單，server 比對）
```

### 2-3. 遷移原則
- **不破壞 v1**：保留 seed_data.json，新增 `loader_v2.py` 從 warehouse_data/ 載入；
  warehouse.py 加開關 `DATA_MODE = "seed" | "multi"`，v1 七 function 兩種模式都能跑。
- safety_stock 維持「寫死在檔」（config.json），**不導入 dynamic 安全庫存**（守 CLAUDE.md 設計，
  且上次 Qwen 亂加的 `_calculate_dynamic_safety_stock` 已確認是 bug）。
- transactions 按日切檔是**故意的**：簡報 p7 要逼模型 Glob→Read，這是 v2 跟 v1「單檔查表」的關鍵差異點。

---

## 3. server.py dispatcher 升級（Host 編排 Agentic Loop）

v1 dispatcher = 單發（收 JSON → execute → 回）。v2.0 加「**多步狀態機**」，但**狀態由 server 管**：

```
search_log RCA 範例（Host 固定編排，模型只在 judge 點出標籤 → v2.1 才接）：
  ① 模型出 search_log{keyword,time_range}
  ② server: Glob transactions/ → Grep keyword → 截斷 N 筆
  ③ server: 若需對單 → Glob orders/PO → Read → 算差異
  ④ server: 結果濃縮成 1-2 行 markdown 回前端 trace
  （v2.0：步驟順序全 server 寫死；v2.1：在 ② 後插 judge_cause_found 決定停不停）

manage_config set 範例（寫入護欄）：
  ① 模型出 manage_config{action:set, key:安全庫存, value:+30, warehouse:south}
  ② server: 比對 key 清單 → 算受影響 SKU 清單 → 回前端「確認要改 N 項？」
  ③ 訪客確認 → server: 寫前 .bak → 改 config.json → 寫 audit log → 回 diff
```

新增校正規則（C 系列接續 v1 C1-C7）：
- **C8**：「誰改的 / 對不上 / 怎麼少 / 異常」→ 強轉 search_log（防撞 query_movement）
- **C9**：「改成 / 設成 / 調整 / +N」+ 設定項詞 → 強轉 manage_config
- **C10**：「跑一次 / 重產 / 執行」+ 腳本意圖 → 強轉 run_script
- **C11**：manage_config set 缺 warehouse → fallback 引導補（類比 v1 C5）

---

## 4. 訓練資料生成（generate_dataset.py 擴充）

承襲現有範式（模板池 + `add()`）。各 function 估算條數：

| function | 模板方向 | 中英 | 估算 |
|---|---|---|---|
| search_log | keyword × time_range × RCA 觸發詞（對不上/異常/誰改的/怎麼少/扣帳） | 中97/英3 | ~400 |
| manage_config (read) | 「現在的{設定項}是多少」「查{倉}{設定項}」 | | ~200 |
| manage_config (set) | 「{倉}{設定項}改成{值}」「全部+{N}」「{SKU}安全庫存設{N}」 | | ~300 |
| run_script | 「跑一次{腳本}」「重新{腳本}」+ 白名單腳本別名 | | ~200 |
| **負樣本/切界** | v1 query_movement vs search_log 對撞樣本（明確標 movement） | | ~150 |
| **小計** | | | **~1250** |

- 現有 training_data.jsonl ~3384 條 → v2.0 約 **4600 條**。
- SYSTEM_PROMPT 加 3 個 function declaration（會變長 → 注意雷 4 max_length，下節驗）。

---

## 5. 重訓 + 量化（守雷 4 / 雷 9）

1. `build_function_declarations.py` 加 3 function → 重生 system_prompt.txt → **量 token 數**（v1 594，加完估 ~750，仍 < 雷 4 危險區）。
2. `generate_dataset.py` 生 → 量 max_length（sanity，確認 p95 < 1024，守雷 4）。
3. `finetune_local.py` 重訓：**Tee-Object 寫 log + logging_steps=5 + save_steps=200**（雷 9 鐵則）。
   - 由 Claude 幫跑 + 附 user 可看的 PowerShell `Get-Content _ft.log -Wait -Tail 50`（你的偏好）。
4. Q8_0 量化（守「llama-quantize 出 BF16.gguf 要改名 bf16」雷）。
5. 驗收：`test_e2e.py` 跑 v1 七 function 不回歸 + 新三 function raw 命中率分項報告。

---

## 6. 前端（templates/index.html）

- 加「🔧 Agent 模式」入口：show 多步 trace（Glob→Grep→Read→Reason 一步步浮現）= 簡報 p7「視覺震撼」。
- manage_config set 的「確認改 N 項」對話框（寫入護欄 UX）。
- v1 色彩語意不動。

---

## 7. v2.0 執行階段（建議順序，每階段可獨立驗收）

| 階段 | 產出 | 驗收 | 狀態 |
|---|---|---|---|
| **S1** 資料層 | warehouse_data/ + loader_v2 + DATA_MODE | v1 七 function multi 零回歸 | ✅ |
| **S2** dispatcher | tools_v2.py 三金剛 + C8-C11 + confirm 端點 | 手打JSON正確 + RCA追到PO對不上 + C8-C11 7/7 | ✅ |
| **S3** 訓練資料 | generate_dataset(4555條,三金剛634) + 9 decl | 生成OK、樣本 max 1057<1152 不踩雷4 | ✅ |
| **S4** 重訓量化 | Q8_0 GGUF | full FT loss **0.81→0.0296**、Q8_0 部署 test/models/ | ✅ |
| **S5** 前端 | Agent trace UI + HITL 確認框 + 🤖管家chip | 6 view + chip + 語法OK | ✅ |
| **S6** 同步 | TRAINING_BACKLOG✓ / win11 同步待裁示 | — | 🔄 |

**S4 實測**：3070清空(關 llama-server+ComfyUI)→ full FT 387step ~9.7s/it VRAM6.6GB 不撞雷4；loss 0.81→0.0296、eval 0.0307；Q8_0 279MB 部署 test/models/。

**🎉 端到端驗收（2026-06-22 完成）**：
- 模型 raw 命中（v1 舊測試集）93.1%（比 v1 77.6% 高很多）
- **v2 自測：總 16/16、三金剛 9/9**（search_log 3/3·manage_config 3/3·run_script 3/3·v1七function 7/7）
- **RCA 深度驗證**：「智慧手環怎麼少這麼多」→ 真追到 PO00116 應收48/實收36/短12、trace[glob→grep→read→reason]、cause_found=True
- **WS 全鏈**：agent_rca trace 卡片、config_confirm HITL gate、v1 inventory 都正確回傳
- 校正層 C8-C11 全作用（C8 query_inventory→search_log、C10 manage_config→run_script）
- ⚠️ 踩雷已記 memory：①stock 用快照不累加 ②import finetune_local 會啟訓練 ③重訓後 system_prompt.txt 要 root→test/ 複製

---

## 8. 待你裁示的開放項（生資料前要定）

1. **suppliers.csv 要不要做**：RCA「PO 對不上」要對供應商才完整，但會多一層資料。先做簡版（供應商名+前置天數）？
2. **config.json 設定項清單**：除了 safety_stock，補貨前置天數 / 安全水位倍數 要不要納入 manage_config 可改範圍？
3. **manifest.json 白名單腳本**：先收哪幾支？建議 `regenerate_seed`（重產資料）/ `export_movements`（匯出盤點）/ `stock_audit`（月底盤點）三支既有工具。
```
