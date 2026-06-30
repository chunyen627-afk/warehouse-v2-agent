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
- ✅ 自然語言新增商品（create_item）— 分步引導 + 一句話模式 + 同名防呆（2026-06-30）
- ✅ 自然語言刪除商品（delete_item）— 引導模式 + 原60項保護 + HITL確認（2026-06-30）
- ✅ 庫存排行（rank_type=stock）—「哪個東西庫存最多」（2026-06-30）
- ✅ 3-step RCA loop（search_log→judge_cause_found規則→suggest_action）（2026-06-30）
- ✅ 多裝置同時連線（移除單訪客保護）+ HTTPS + QR code（2026-06-30）
- ✅ 能力地圖 7 類 + chips 新增/刪除按鈕（2026-06-30）

**路由準確率（2026-06-30）**：
- 81 eval: 80/81 (99%)
- OOV v1: 95/97 (98%)
- OOV v2: 77/79 (97.5%)
- v6 模型: eval_loss=0.026, 5,849 筆訓練
- intent_clf: 重訓 489MB, per-label 96-100%

**最終架構**: intent_clf(分類) → LLM(抽參數) → dispatch(攔截清理) → execute

**重要踩雷**：
- 改 code 後 HTTP + WS 兩路徑都要同步修改（見 [[sync_both_sides_after_code_change]]）
- session state（_item_create_state/_item_delete_state）必須在 WS/HTTP 兩路徑都設
- chip 按鈕用 HTTP fetch 不會設 WS session state，要用 sendQuery() 走 WS
- 「警示」同時出現在 `_LOW_STOCK_INTENT_WORDS` 和 C14 `_alert_words`
- 270M 不適合單獨做 routing，intent_clf 主路由 + LLM 抽參數 是最佳架構

**備份**：`_backups/warehouse_v2_20260630_v2.0.zip`（3.3GB，含模型）
3. 🟡 RCA 第二輪 timeout 保護（1h）
4. 🟢 Context carry-over（追問「那中倉呢？」記住上輪 entity，3-5h）
5. 🟢 查完庫存自動帶 Proactive 建議 button（1h）
6. 🟢 自然語言產腳本 Code Generation（較大）
7. 🔴 delete_schedule / delete_alert 前端二次確認卡
8. 🔴 腳本白名單擴充（到期報告 / 補貨清單）
9. 📋 改動同步到 win11_installer/dist/app_warehouse/
10. 📋 TRAINING_BACKLOG.md 補記 Query Rewriting + 路由修復

關聯：[[finetune_import_triggers_training]] [[feedback_auto_restart_server]]
