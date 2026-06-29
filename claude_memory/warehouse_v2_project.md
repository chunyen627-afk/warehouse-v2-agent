---
name: warehouse_v2_project
description: 倉管 v2「真 Agent」改造的位置、架構決策、進度（2026-06 啟動）
metadata: 
  node_type: memory
  type: project
  originSessionId: b038c70f-c688-4acf-bf6d-95181312930d
---

倉管 v1（純 Tool Call）升級成 v2（Agent）。**新工作區 `warehouse_v2/`**，`warehouse_v2/test/` 自足直接上 RPI5。

**架構決策（已鎖定）**：
- Loop 由 server 編排，270M 只出單步 JSON。
- 資料層：`test/warehouse_data/`，stock 讀 master/stock.csv 快照真值。
- `test/tools_v2.py` 三金剛：search_log（RCA）/ manage_config（HITL）/ run_script（白名單）。
- 校正層 C0-C18 + Pre-C 攔截（server.py）。
- 訓練：full FT Q8_0，不用 LoRA。

**目前功能（2026-06-29）**：
- ✅ 七金剛（query_inventory / query_movement / list_low_stock / compare_warehouses / list_hot_items / query_related_items / list_expiring_items）
- ✅ 三金剛（search_log / manage_config / run_script）
- ✅ RCA ReAct 2-step loop（search_log → rca_context → suggest_action），前端 Agent trace 卡
- ✅ 腳本執行 + CSV 下載（stock_audit / movement_export / health_check）
- ✅ set_alert + alert_rules.json 持久化 + 背景 3600s 掃描
- ✅ set_schedule + schedule_jobs.json 持久化 + 背景 60s 檢查 + APScheduler
- ✅ list_alerts / delete_alert / list_schedules / delete_schedule
- ✅ 右側 Alert Panel（⚡警示規則 / ⏰定時排程 兩 tab）
- ✅ HITL 確認卡（manage_config / set_schedule / set_alert）
- ✅ Query Rewriting（_rewrite_query，53 條規則，LLM 前標準化輸入）
- ✅ Pre-C 攔截層（Movement / Compare / Alert-Set / Schedule / RCA 意圖詞保護）
- ✅ OOV keyword 前後綴清理（dispatch 前清理「有洗衣精」→「洗衣精」）
- ✅ query_inventory 空 keyword → 全倉前 10 筆概覽
- ✅ set_alert 接受 raw_text，condition 解析失敗預設 below_safety
- ✅ OOV 模糊匹配引擎 `_fuzzy_score()`（剝規格 + 雙向滑窗 + 字元重疊，2026-06-29）
- ✅ C3/C4/C6 hard-return 防護（防止後續規則推翻正確路由，2026-06-29）
- ✅ eval_http.py HTTP 路由評測工具（2026-06-29）
- ✅ test_oov.py OOV 容錯測試 33 題（2026-06-29）

**路由準確率**：81 題測試（2026-06-29）
- 56/81 (69%) — 較 100% 下降，主因 query_related_items keyword 提取 gap + 英文 mixed input
- 「庫存警示」等核心查詢已透過 C3 hard-return 修復

**重要踩雷（2026-06-29）**：
- 「警示」同時出現在 `_LOW_STOCK_INTENT_WORDS` 和 C14 `_alert_words`，造成路由衝突
- 改 `server.py` 後必須跑 `eval_http.py` 確認未退步（見 [[run_full_eval_after_server_change]]）
- 新增意圖詞前 grep 檢查是否與其他規則詞重疊（見 [[intent_word_overlap_gotcha]]）

**備份**：`_backups/warehouse_v2_test_20260626_1221.zip`（1.2GB，含模型 GGUF）

**待做清單（優先序）**：
1. 🟡 set_alert 後立即跑一次掃描（30 min）
2. 🟡 set_schedule 重複時改 HITL 問覆蓋（1h）
3. 🟡 RCA 第二輪 timeout 保護（1h）
4. 🟢 Context carry-over（追問「那中倉呢？」記住上輪 entity，3-5h）
5. 🟢 查完庫存自動帶 Proactive 建議 button（1h）
6. 🟢 自然語言產腳本 Code Generation（較大）
7. 🔴 delete_schedule / delete_alert 前端二次確認卡
8. 🔴 腳本白名單擴充（到期報告 / 補貨清單）
9. 📋 改動同步到 win11_installer/dist/app_warehouse/
10. 📋 TRAINING_BACKLOG.md 補記 Query Rewriting + 路由修復

關聯：[[finetune_import_triggers_training]] [[feedback_auto_restart_server]]
