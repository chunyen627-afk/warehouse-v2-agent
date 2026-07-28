# 訓練集待補清單（下次重訓時做）

> ⛔ **補訓已決定跳過**（user 定調 2026-07-26 深夜）——除非有大幅效益再重新評估。
> RPI5 8002 跑的**就是**英文微調版（md5 `213593b5…`），r1-r5 的收斂成果是
> 「英文微調模型＋規則層＋FastText clf」一起跑出來的。重訓真正買到的只有
> SYSTEM_PROMPT 去中文＋幾類已有規則兜住的長尾，投入產出不成比例。
> 下面的條目繼續累積，等重評估通過再一起用。

## 【2026-07-27】英文版：語音英文化上線（commit 3fe3e6a）

Fun-ASR-Nano（阿里）→ whisper.cpp **tiny.en**（來源約束：只用歐美模型）。
延遲 **2.45s → 1.1s**，端到端可用（含寫入真的落地）。守衛維持 891/892。

**最大教訓：文字端 r1-r5 全綠 ≠ 語音可用。** ASR 的產物型態（首字大寫、
專有名詞大寫、句末標點）打穿了系統裡 **21 個小寫英文詞表**——打字訪客不會
打大寫，所以文字端五輪收斂一句都沒暴露。

新增回歸資產 `en/_conv_en_voice.txt`（句子全是 `/api/asr` 的真實輸出）。

### 🔜 補訓語料新增項（r5-voice）
- **`how does the transfer work` 回全店概覽**（追了三輪沒解）——GUIDE_KEYWORDS
  已補英文「問功能怎麼用」但沒生效，`_is_guide_request` 開頭的「含具體商品/
  倉庫→當查詢」排除似乎把 `transfer` 當關鍵詞先擋掉。邊緣句，未深追。
- **`which warehouse has the most stock` 在 context 下被汙染**：clf 判
  query_inventory 無參數 → carry-over 補上一句的商品（Sparkling Water）
  → 回單品庫存。**既有行為非本輪回歸**（獨立連線測通過）。
  「which warehouse」是新的全域問題，不該繼承上一句商品。
- ASR 聽錯到無法救的（合理失敗，回 clarify 誠實反問即正確）：
  `camping tech`／`yoga meant`／`street met`／`urgent wine`／`wireless metals`

## 【2026-07-26 深夜】英文版：劇情批 r5 並發／超長 context／資料極值（commit 6fee9f5）

user 指定的三條未測向量，真 BUG **5 → 0**。守衛 891/892（唯一 FAIL 仍是刻意
不修的 `do we have scks`）、r1-r4 複驗零回歸、check_views 覆蓋完整、
branch_walk 24+8 異常 0。

**工具**：`ws_convo.py` 加 `[para]…[/para]` 真並發語法（原 `>@B` 只是輪流）；
可疑詞表補英文（原本全中文＝英文版 ⚠️ 標記形同虛設，r5 的 5 個破口一個都沒標）；
`run_guard_en.sh` 補 reset（守衛前資料髒會誤報 FAIL，r5 踩過一次）。

**並發結果全部正確、零修復**：vid 隔離乾淨、pending 不互踩、同 SKU 競態
無 lost update（141→161→191 兩筆都算到）。

**資料極值（此前從沒走過）**：baseline **沒有任何 0 庫存、也沒有全店低於安全
庫存者** → 出貨清成 0 才真走到。查詢/缺貨/比較/再出貨全對，無除零/NaN/負數。

**修掉**：①招呼與告別回中文（展場第一句與最後一句；英文說法 15 句只中 3 句）
②虛詞被當商品名（again/lowest/below）③單品安全庫存詢問退化成全店清單
（carry-over 補對 keyword 卻餵給不吃 keyword 的 tool，資訊靜默蒸發）
④wifi password 等場館問題掉進「查不到的商品」。

### 🔜 補訓語料新增項（r5）
- **單品「還能撐幾天」**：`how many days of camping tent left` 回庫存概況。
  `days_left` 只有 low_stock 卡有、單品卡沒有 → 屬**功能擴充**非破口，
  收斂輪刻意未動。要嘛補語料教它導向 low_stock{keyword}，要嘛單品卡加欄位。
- `anything below the minimum` 回全店概覽（既有長尾，非本輪造成）。

## 【2026-07-26】英文版：修 6 個訪客可見破口 + 動態中文殘留（commit d38515c）

**測法**（user 定調，抓破口的關鍵）：WS 全文 + **邊界路徑**，不只看 view 通過率。
ws_inspect 的 ⚠️ 標記只抓到 1 句，實際有 6 個破口——**醜回答不會被標成可疑**。

**修掉**：①GUIDE_MSG 整段中文 ②`set alert`→全店概覽（C14 第二關全中文）
③歧義查詢回無關概覽（英文快路徑回 "" 把歧義=沒商品名）④**類別查詢整條壞掉**
（6 類 5 個回「找不到」，20+ 處 cat_zh_map 鍵全中文）⑤`this month` 排行永遠回
this week（C4 hard-return 全中文，C4b 有補但輪不到）⑥新增商品 `cancel` 卡死。
另清 8 處 clarify hint/options + 健康訊息 + timeout/失敗 + 語音錯誤訊息。

**守衛**：873/892 (97.9%)，與基準持平。剩 19 句仍是雙錯字長尾（刻意不修）。

## 【2026-07-26 傍晚】英文版：劇情批 r1 + 三鐵則審查（commit f8e751e）

user 定調「**能修復就靠修復，不然訓練不完**」→ 先跑生成收斂再補訓。
**英文版第一次跑跨句對話測試**（中文版已 r85）。一輪抓到 11 個結構性 bug，
最大宗是 **carry-over 整條對英文失效**（追問詞表全中文）。守衛維持 891/892。

**收斂 5 步驟已寫進記憶 en_port_recurring_traps「收斂流程」段**，之後每輪照跑：
劇情批 → 審到畫面 → check_views → branch_walk → 全量守衛。
⚠️ 工具要先英文化：`ws_convo.py`/`branch_walk.py` 都曾寫死 8001（第三、四支了）。

### 對補訓的意義
這輪再次驗證「**先查結構性 bug、再考慮補語料**」：11 個 bug 沒有一個需要模型
幫忙，全是規則層的中文殘留或判準邊界。⇒ **維持先收斂、暫不補訓**的決定。
新增補訓素材候選：跨句追問句型（劇情批 r1 的 S1-S10 可轉成語料）。

## 【2026-07-26 下午】英文版：排程功能整條接上 + 危險破口（commit 1d84eaa / e5d14e1）

user 問「還有啥功能沒做」→ 掃描發現**排程功能對英文整條不通**（跟類別查詢
同一類坑：Pre-C-Sched 8 個詞表全中文，中文版本來就靠正則攔截不靠 LLM）。
已修：Pre-C-Sched 五條路 + C12 排程讓路 + tools_v2 解析器（頻率/鐘點/腳本名）
+ `_ABORT_EXEMPT` 英文豁免 + 守門員黑名單豁免（WS 端有自己一份，兩處都要改）。

**🚨 順手抓到的危險破口**：裸 `alert me` → LLM 吐 `target:'no item'`
→ 低分(2)比到 Ceramic **No**n-stick Pan → **確認卡上寫著訪客沒提過的商品**，
按確認就真的建規則。已加 target 接地驗證（佔位詞/分數<4 → 清空退回全店警示）。

### 🔴 SYSTEM_PROMPT 已去中文，但**故意沒部署**（雷 6）
`build_function_declarations.py` 有 11 處中文（`like 藍牙耳機 or 氣泡水`、
`(缺貨)`、`(熱銷/滯銷)`…），訓練時會拼進**每一筆**樣本（finetune_local.py:122）
→ 6284 筆全帶中文範例。已改成英文並重產 `system_prompt.txt`（零中文、6265 字元），
**commit 了但沒 scp 到 RPI5**（RPI5 仍是舊版 md5 `1e287e16…`，與現行模型一致）。
⇒ **補訓時這份會自動生效**（finetune 從 build_function_declarations 重產），
訓練完把新模型 + 新 prompt 一起上 RPI5。

### ✅ 雙錯字長尾：**已靠修復層解掉 18/19**（2026-07-26 下午，守衛 873 → 891/892）
user 定調「能修復就靠修復，不然訓練不完」→ 重新拆解後發現**不是模型問題**，
是 8 個結構性 bug（閘門吃掉正解 / 投票太粗 / 同分並列跳過模糊層 / alias 值
假歧義 / 一個陌生 token 一票否決 / alias 鍵門檻 / 整句 vs core / 合成詞拆解）。
方法論已寫進記憶 en_port_recurring_traps 坑 9，**下次收斂直接照做**：
先量化 token ratio（真錯字 0.83-0.95、誤配 <0.77）→ 逐層測輸入輸出定位
→ 絕不放寬全域門檻 → 放寬前先拿 OOV 詞撞安全集 → 每輪跑全量守衛。

**剩 1 句留給補訓**：`do we have scks`（scks→socks 與 hair→chair 字元層面
完全相同，都是 insert/0.889，而 hair dryer 必須維持「查無」。要區分只能靠
英文詞典判斷「hair 是真詞、scks 不是」）。
⇒ 判斷「該不該留給補訓」的標準：**有沒有可用訊號能區分正例與反例**。

### ⚠️ 補訓語料要補的（本輪暴露、規則層不該硬修的）
2. **英文類別詞當查詢主體**：`Electronics stock` / `all Daily Goods stock` 這類
   目前靠 `_CAT_WORDS_EN` 規則表兜（server.py `_category_from_en`）。語料裡
   **類別查詢幾乎只有中文樣本**，建議 gen_en_dataset 補「英文類別 × query_inventory
   帶 category 參數」，讓模型自己抽 category 而非靠規則表。
3. **`this month` / `this week` 期間詞**：模型對英文期間詞抽 period 不穩（route
   直達時靠 C4 硬校）。語料可加期間變體（this month / monthly / this quarter）。
4. **英文排程句 → set_schedule**（07-26 下午新增）：LLM 對
   `schedule a daily low stock report at 9am` 抽成 **manage_config**，
   完全靠 Pre-C-Sched 正則兜。語料裡排程樣本應該只有中文 → 建議
   gen_en_dataset 補「英文排程句 × set_schedule」含頻率/時間/腳本三種參數。
5. **裸警示句的 target**（07-26 下午新增）：`alert me` LLM 吐
   `target:'no item'` 這種佔位字串（不是合法商品名也不是空）。語料可教
   「沒指定商品 → target 留空」，減少對接地驗證的依賴。

### 教訓（可推廣，已寫進記憶 en_port_recurring_traps 坑 7）
**「規則層加英文分支」本身會製造新誤配**——第一版「歧義回詞幹」讓虛詞殘片
（op / by）也過關，跑出 `"op" matches 3 items`，守衛 873→869。修法是給詞幹加
**三重門檻**（長度≥4 + 虛詞黑名單 + 原句詞界驗證）。
⇒ 每次在英文分支放寬條件後，**必須跑全量守衛**，小批探針看不出這種回歸。

## 【待辦・路線 B】功能描述變體「可教學化」— 讓新增商品也能有模糊查詢支援（2026-07-07 記，暫緩，現行採路線 A 手工維護）

**問題背景**：目前功能描述模糊查詢（「刷牙的→電動牙刷」「煮咖啡的→咖啡機」）
是 `warehouse_v2/test/server.py` 的 `_DESCRIPTOR_ALIASES` 一張 **58 條手寫
regex 死表**，每條的目標商品名寫死。後果：
- ✅ 新增商品後，**直接講商品名**能查（走 fuzzy/extractor，讀即時清單）
- ❌ 新增商品的**功能描述句**零支援（`_descriptor_hit` 回 None → 掉回 LLM，
  270M 不會語意映射，行為不保證）

**現行策略（路線 A）**：每次新增商品，人工補 2-4 條描述 regex + RPI5 實測 +
進 `regression_corpus.txt`。準、雙平台一致、可控，但不會自己長。

**路線 B（未來做，user 2026-07-07 認可方向、暫緩）**：新增商品流程「可教學化」
1. 改 `tools_v2.py` 的 `create_item_collect` 流程，多一步（可跳過）問
   「客人可能會怎麼形容這個商品？」收集 2-3 個俗稱/描述詞
2. 寫進一張**活表** `descriptor_aliases.json`（資料檔，非 code），格式
   `{"描述詞": "商品全名"}`
3. `_descriptor_hit` 同時查死表（現有 58 條）+ 活表（使用者教的），
   活表用 substring 比對即可（不需 regex 精準度）
4. 新增後**即時生效不用重啟**（活表每次查詢時讀，或 state 變動時 reload）

**為何選 B 不選 C**：路線 C（embedding 語意相似度自動泛化）會拖慢 RPI5、
且相似度誤判＝「亂猜」，與展示主打的「準、不亂答」衝突。B 是使用者**明確教**
的映射，守住「準」，且「現場教系統認新講法」本身是可展示的 demo 亮點
（比靜態 58 條更抓眼球）。

**實作前提**：等目前系統（功能描述直達 + 三軌衝突修復）完全穩定後再動
（user 要求「先把目前系統都測得很穩定再說」）。相關記憶
`warehouse_v2_convergence`。

---


## 【已完成 2026-07-03】調貨/調倉（跨倉調撥）— 已實作並測試通過（commit 38506e4+bfd0117）

**做完了**：create_transfer/commit_transfer + C13a 攔截 + 前端確認卡 + 中文數字
到千位。專項測試 27/27 全對。詳見 memory `warehouse_v2_project`。以下保留原始
規劃供參考。

**當初現況**：目前完全沒有調貨功能。現有的 `create_movement`（進出貨）只能做
**單倉**的進貨或出貨——某個倉「多了 N 個」或「少了 N 個」。使用者若在網頁講
「北倉調 20 個藍牙耳機去南倉」，系統不會正確處理（實測會跌到 query_movement
或別的功能）。

**調貨跟進出貨的本質差異**：調貨是把貨從 A 倉「搬到」B 倉，一次要**同時扣
A 倉、加 B 倉**兩個動作連動，總量不變。這是比單倉進出貨複雜一階的資料結構，
不是 `create_movement` 的自然延伸。

**做的時候要先跟 user 確認的設計問題**：
1. 調貨要不要也走 HITL 確認卡（顯示「A 倉 -N、B 倉 +N」的預覽）
2. 來源倉庫存不足時怎麼處理（擋下？還是允許負庫存？）
3. 交易紀錄怎麼記（一筆 transfer？還是拆成 A 倉 out + B 倉 in 兩筆？）
4. 自然語言怎麼抽「來源倉」跟「目標倉」（「從北倉調到南倉」「北倉調去南倉」
   「把北倉的貨移到南倉」等講法的來源/目標順序判斷）

**關聯的「退貨」也一併待做**：退回庫存，可能還涉及金額/供應商，同屬新功能範疇。

---

## 2026-07-02 warehouse_v2：進出貨自然語言覆蓋率巡查（實測 ~50 句，修 4 個真bug）

實測約50句真實講法（含錯字、多倉組合、複合動作、邊界量詞），發現並修復：
- 動詞覆蓋缺口：「來了」單獨作進貨動詞、「買走了/買走」出貨口語未被識別
- 量詞清單缺「對/頂/張/把/副」（如「賣了8支」沒問題但「銷貨1頂」漏判）
- **架構性 bug**：句子同時提到兩個倉名（如「北倉跟南倉的藍牙耳機各出貨了
  10個跟15個」）會被 intent_clf 誤判成 compare_warehouses，且
  `_correct_function_call` 開頭的 early-return 保護機制讓 C13b 完全沒機會
  修正。已把 C13b 移到 early-return 之前，多倉語意含糊時改用 clarify 而非
  硬猜（讓使用者拆開描述）
- 「多了/少了」語意模糊（盤點差異 vs 進出貨口語）比照既有「庫存加/減N」
  模式改用 clarify

**待做（本次確認先不實作，先記錄）**：
- 「調貨」（跨倉調撥，需同時扣一倉加另一倉）
- 「退貨」（退回庫存，可能還涉及金額/供應商）
- 這兩個都不是現有 `create_movement` 的自然延伸，屬於新功能範疇，等後續
  評估要不要做時再設計

三批 OOV 回歸（33/97/79題）全程確認無退步，逐輪修復後才 commit+push+同步
RPI5，遵循既有的「100題實測找真正失敗模式」方法論（見 memory
`feedback_dispatch_rule_design`）。

---

## 2026-07-01 warehouse_v2：即時進出貨功能規劃（討論定案，尚未動手）

**背景**：老闆需求「自然語言建倉」，確認範圍是「自然語言新增商品」（已完成），不是新增倉庫實體。討論延伸到「新增商品後能否自然語言控制進出貨數量」——**目前不行，系統整體是唯讀的**（除了新增/刪除商品、警示/排程/採購單設定），庫存數字寫入初始值後就再也動不了。

**決策：做輕量版進出貨，不做完整 PO/SO 單據結構**
- 理由1：完整單據（下單→驗收→短收判斷→庫存連動）四環節任一漏了數字就對不上，展場風險最高
- 理由2：跟已驗證的「270M 是路由器不是決策者」結論一致（見 memory `edge_agent_model_size`），複雜多環節流程本來就超出小模型能穩定處理的範圍
- 理由3：可以之後再疊完整單據，不衝突

**輕量版設計（暫定）**：
```
使用者：「北倉進了藍牙耳機 50 件」/「南倉出貨洗衣精 20 件」
→ 解析 [商品, 倉別, 方向(進/出), 數量] → HITL 確認卡 → 確認後：
  1. 寫一筆 transactions/{today}_{in|out}.csv
  2. 同步更新 stock.csv 對應數字（+50 或 -20）
  3. 出貨若庫存不足 → 攔下提示「庫存不夠，只有 X 件」
```

**已知風險（規劃時要一併設計）**：
- `stock.csv` 是進出貨跟「新增商品」共用的同一份檔案，兩個 commit 動作若同時被觸發（例如新增商品同時有人在測進出貨），檔案 I/O 層級可能互相覆蓋（讀取整份→append→寫回沒有鎖保護）。目前「新增商品」單獨用沒事，但做了進出貨後這個風險會變實際，需要跟今天修的 `llm_lock`（模型推論並發鎖）一樣的思路，加檔案寫入鎖。

**故事線目標**：新增商品 → 進貨 → 查庫存變多 → 出貨 → 查庫存變少，讓庫存數字在展示中真的動起來（目前查詢功能雖多，但數字永遠停在種子快照，2026-05-26）。

**現況確認（既有 PO/SO 資料，避免誤會已經做了）**：
- `orders/PO`、`orders/SO`、`receipts/` 目錄裡的 JSON 都是**種子資料**（模擬歷史紀錄，給 RCA 短收分析、購物籃分析用），不是使用者能新增的東西
- `generate_po`（幫我把缺貨的產採購單）是**系統根據現況自動彙整生成**採購單草稿，不是「使用者說要跟供應商A訂貨」這種主動下單，這個功能本身完整且有 HITL，跟這次要做的進出貨是不同性質

---


## 2026-06-24 Session: S4 模型部署 + 38/38 HTTP test 全通過
- S4 checkpoint-459 (loss ~0.027) → Q8_0 GGUF 轉換完成，部署至 `warehouse_v2/test/models/`
- `_http_test.py` (NEW 38-case): 使用 `/api/query` HTTP 端點，完全避免 WebSocket session 競爭問題
- `/api/query` (NEW): `server.py` 新增 HTTP POST 端點供測試使用
- `server.py` C0（NEW）: 未知函式名（模型幻覺如 `calculate_summary`）→ 從 user_text 關鍵詞推斷正確函式
- `server.py` C11-pre 擴展：key 為空字串時也能從 user_text 推斷（含「前置天數」→ key='前置天數'）
- `server.py` C11b 擴展：新增「改成N」直接設值（而非只支援「加N」「減N」的差值格式）
- `server.py` C17a（NEW）: 補全缺 warehouse 參數（從 user_text 擷取「南倉/北倉…」）
- `server.py` C11-pre0（NEW）: manage_config action read→set（user_text 含設值動詞時）
- `_WH_NOISE`/`_QTY_NOISE` module-level: `_extract_sku_keyword` 預清理倉庫名和數量詞
- `index.html` reconnect timer: 1500ms → 8000ms（防止 browser 競爭測試 session）

## 2026-06-24 Session: 36-case smoke test 全通過修正
- `server.py`: `_correct_function_call` 簽名加第三回傳值 `hard_corrected`，C18 加 `not _hard` guard 防止蓋過 C7/C8/C10/C12/C14 的確定性校正
- `server.py`: 新增 C11-pre（管config key「補貨」→「前置天數」）、C11b（value「全部加N」→「+N」從user_text補救）、C13（明確庫存查詢 hard-return query_inventory）
- `server.py`: C14 提前 return 前在內部做 set_alert 參數清理（含「低於N個」→ below_threshold+threshold）
- `server.py`: search_log OOV 前先做 `_extract_sku_keyword` 預清理（防「抗菌洗衣精帳」OOV觸發誤判）
- `server.py`: ② clarify 加 `_po_direct` 排除（「幫我把缺貨的產採購單」不攔）；① clarify 改偵測多倉庫名（「北倉和南倉」→ 不攔）
- `tools_v2.py`: `set_alert` 加 `threshold` 參數、加 `below_threshold` condition 支援
- `_logic_test.py`、`_smoke_test.py`（NEW）: 28+36 case smoke test 套件，全數通過

## 2026-06-24 Session: S4 重訓 + OOV fuzzy match
- `generate_dataset.py`: 補 489 條口語化樣本（我想要/採購對帳/幫我查/set_alert口語款/切界負樣本），總筆數 4926→5415
- `server.py`: 新增 `_detect_oov()` — keyword 不在 SKU 清單時 fuzzy match 推測候選商品，攔截 query_inventory/query_movement/search_log，score ≥ 60 才觸發，回傳 clarify 選項讓使用者確認
- `finetune_local.py`: batch=2（配合 3060 12GB），CUDA_VISIBLE_DEVICES=1
- S4 訓練中（3060, step ~8/459, ETA ~5h）

## 2026-06-24 Session: Clarify + FastText + 模糊搜尋 + 參數清理
- `server.py`: _FILLER 加「我想要/我想/想要/想看/想知道/想查/我要/要查/要看」；⓪ 規則（t_clean 空→通用選單）；C17b set_alert 清理；C17c generate_po/commit_po 清理；C18 FastText mismatch 修正；RCA 詞加入 gatekeeper + _ALL_INTENT_WORDS；C8 改成黑名單
- `tools_v2.py`: `if not skus:` (不再需要 `_is_global` gate)；search_log 摘要邏輯修正
- `warehouse.py`: `_KW_TO_CAT` + `_suggest_on_empty()`；query_inventory/query_movement 無 match 改回 clarify
- `templates/index.html`: 選單改數字列表 + `resolveInput()` 讓使用者輸入 1-4 選擇
- `intent_clf.py` / `intent_clf.bin` (NEW): FastText 意圖分類器，jieba，98.78%，<2ms CPU
- 需補訓練：口語化句式（「我想要」「幫我查」「帳對不上」）→ 增加 training_data.jsonl 覆蓋率
- 同步 `win11_installer/dist/app_warehouse/`

## 2026-06-23 Bug Fix: search_log 泛查不說「未發現短收異常」
- `tools_v2.py` L199 分支：有 `sku_ids` 才說「查過 PO 後未發現短收」，無 match 說「泛查、建議用商品名」。
- 同步 `win11_installer/dist/app_warehouse/tools_v2.py`。
- 無需重訓（純 Python 邏輯，校正層無關）。

> 展場上線發現的「校正層擋著、但訓練集本該覆蓋」的講法。
> 累積一批後一次重訓：`py -3.11 generate_dataset.py` → `py -3.11 finetune_local.py` → 量化 → sync。
> 預估每次重訓 ~7 小時（3070），所以累積到至少 5-10 條再做才划算。

---

## ✅ 倉管 v2「真 Agent」完成（2026-06-22，新工作區 `warehouse_v2/`）

v1（純 Tool Call）→ v2（Agent）。三金剛 + 多檔資料層 + Agentic Loop（server 編排）。
- **新增 3 function**：search_log（RCA 追 PO 對不上）/ manage_config（read|set，HITL確認+.bak+audit）/ run_script（白名單）
- **資料層**：seed → `test/warehouse_data/`（master/transactions按日切/orders[PO+SO]/audit/scripts），`migrate_to_v2.py` 重建、`loader_v2.py` 載回
- **校正層**：新增 C8-C11
- **重訓**：full FT 387step ~9.7s/it（3070清空後不撞雷4），**loss 0.81→0.0296**，Q8_0 量化
- **驗收**：端到端 16/16、三金剛 9/9、RCA 真追到 PO00116 短收、v1 七 function 零回歸
- **詳見** `warehouse_v2/V2_PLAN.md`
- ⚠️ 雷：① stock 不可從 movements 累加（用 stock.csv 快照）② `import finetune_local` 會啟動訓練（用 regex 讀常數）③ 重訓後 `system_prompt.txt` 要從 root 複製到 `test/`（兩個不同檔，漏了模型吐亂碼）

**v2.1 待做**：B 類 judge_cause_found（壓縮 context 試訓）+ set_alert（半固定 enum）

---

## ⚠️ LoRA 在這專案不可行（2026-05-21 驗證）

**設定**：`r=16, lora_alpha=32, target_modules=[q/k/v/o_proj]`，trainable 0.55%
**訓練指標**：epoch 3 eval_loss 0.0749 / token_acc 99.14%（看似很好）
**Q8_0 raw 命中率**：**16.9%（11/65）** vs full FT 88.9%（**慘掉 72 pp**）

**症狀**：
- Sector enum 嚴重幻覺：`transport_heavy_fleet` / `plastic_cable` / `geris` / `oil_gas_field` / `woven_silk` / `electric_machinery` / `cloud computing` / `AI theme`（帶空格）
- Currency pair 全錯：`FX` / `US>JPY` / `ETFs over period` / `主流|日本|想要`
- 多語污染：模型 generate 時吐西里爾字符 `і` 害 cp950 console crash（雷 3 應驗）

**根因**：LoRA 只動 0.55% 參數沒辦法蓋掉 base Gemma3 的「30+8 個 enum 死記」需求。token_acc 99.14% 是平均高，但結構性 token (function name + enum) 在 LoRA 模式下沒被壓住。

**未來嘗試管道**：
1. 拉 LoRA rank 到 r=64 / alpha=128（trainable ~2.2%）— 風險仍可能不夠
2. 用 LoRA + 全 attention + MLP target_modules（`q,k,v,o,gate,up,down_proj`，trainable ~5%）
3. 直接放棄 LoRA — 7h full FT 換 88-90% raw 比較划算

**結論**：本專案標準路徑回 **full FT**。LoRA 留待未來「資料集成熟、enum 完全收斂」時可重試。

---

## ⚠️ 雷 9：啟動長時間訓練必須寫獨立 log 檔（2026-05-22）

**踩雷情境**：v3.4 full FT 跑了 10+ 小時，但因為沒寫 log 檔，user 完全看不到 step / loss / epoch 進度，也無法判斷是否 hang。最後只能憑 GPU util 80% 跟 python process 還活猜「應該有在跑」，但無法 debug，最後選擇重開重跑（白費 10 小時）。

**錯誤的啟動方式（這次踩到）**：
1. ❌ `py finetune_local.py > log.log 2>&1 &` — Bash `&` 在父 shell 退出時 process 被砍
2. ❌ `Monitor 直接 pipe py finetune_local.py 2>&1 | grep ...` — stdout 沒落地、Monitor grep filter 漏掉 TRL 0.26.2 的 dict 格式 step log（`{'loss': 0.08, ...}`），user 跟 Claude 兩邊都看不到進度
3. ❌ `save_strategy="epoch"` + 沒 step-level log → epoch 1 跑滿 3.4h 前完全無 checkpoint 訊號，hang 也分不出來

**正確啟動方式（鐵則）**：

```powershell
# PowerShell：Tee-Object 同時印終端機 + 寫 log
py -3.11 finetune_local.py 2>&1 | Tee-Object -FilePath _ft.log
```

```bash
# Git Bash / Linux：tee 同效
py -3.11 finetune_local.py 2>&1 | tee _ft.log
```

**另開視窗追蹤**：

```powershell
Get-Content _ft.log -Wait -Tail 50
```

**Trainer 設定鐵則**（finetune_local.py 已套，2026-05-22）：

| 設定 | 舊值 | 新值 | 理由 |
|---|---|---|---|
| `logging_steps` | 10 | **5** | log 更頻繁，loss 變化看得清楚 |
| `save_strategy` | "epoch" | **"steps"** | 不用等整 epoch 才落 checkpoint |
| `save_steps` | （無） | **200** | ~30-40 分鐘一個 checkpoint，hang 也能恢復 |

**為什麼不只用 Monitor**：Monitor 只能看 stdout，不能 replay 歷史。如果中途斷掉 / 沒接到 / filter 漏，progress 就消失。**log 檔是永久 source of truth**，Monitor 是即時通知層，兩者各司其職。

**檢查清單**（每次起 finetune 前）：
- [ ] 確認 `logging_steps=5` + `save_steps=200`
- [ ] 用 `Tee-Object` / `tee` 啟動，不用 `>` 或 `&`
- [ ] 啟動後 5 分鐘內看到 `[sanity]` 訊息（max_length check）
- [ ] 啟動後 30 分鐘內看到第一筆 `{'loss': X.XX, 'epoch': ...}` log
- [ ] 啟動後 1 小時內看到第一個 `checkpoint-200/` 資料夾

任何一條沒滿足 = 立刻停下檢查、不要白等。

---

## 為什麼累積而不是邊踩邊重訓

- 重訓一次 = 7 小時微調 + 量化 + sync RPI5 + 重跑 E2E（半天工程）
- 校正層加一條規則 = 5 分鐘
- 展場期間：校正層擋著夠用 + 觀眾不知道內部怎麼路由

## 何時觸發重訓

- 校正層條目超過 10 條（規則疊規則維護成本上升）
- 或新踩雷的講法**靠校正擋不住**（要在 LLM 層判斷）
- 或展場結束後正式收尾

---

## 待補項目

### 1. 「大盤 / 加權指數 / TAIEX」當主詞 ⚠️ 高優先

**踩雷日期**：2026-05-18
**踩雷講法**：「YTD 加權指數」
**LLM 誤判**：`query_sector_trend{semiconductor, 3y}`
**校正層**：`server.py` 校正 0d（「大盤 / 加權指數 / TAIEX」強制路由）

**訓練集現況**：
- `query_market_overview` 只 124 條（其他 function 動輒 500-1000）
- `generate_dataset.py:583 gen_query_market_overview()` 用 `random.sample(ZH_TPL, 3)`，每組 period × 詞只抽 3/10 模板
- 反序講法（「加權指數 YTD」「TAIEX 本月」）完全沒覆蓋
- 「加權指數」當主詞的訓練樣本只在 1 個模板出現

**下次重訓時改 `generate_dataset.py`**：
- `gen_query_market_overview()` 把 `random.sample(ZH_TPL, 3)` 改成全部 10 模板
- 加反序模板：`{period_word_in_back}` 形式（「加權指數{period}」「TAIEX{period}」）
- 加「純大盤詞、不接 period」：「大盤呢」「加權指數呢」「TAIEX 怎樣」
- 英文補同樣的反序 + 全模板

**估算**：124 → ~533 條（增加 +409，整 dataset +6.5%）

---

### 2. 「近 1 天 / 近一日」被 LLM 吃成 lookback「1 年」⚠️ 中優先

**踩雷日期**：2026-05-18
**踩雷講法**：「AI 近 1 天」「半導體 近一日」
**LLM 誤判**：`query_sector_trend{ai_theme, 1y}`（把「天/日」當「年」吃）
**校正層**：`server.py` 校正 1b（「近 1 天/日」強制路由 `query_sector_performance{period:yesterday}`）

**訓練集現況**：
- `generate_dataset.py` 沒涵蓋「近 N 天/日」這類短跨度時間詞
- 「近 1 天」「過去一日」「最近兩天」這種口語樣本完全缺
- 訓練集 lookback 詞清一色「1 年/3 年/5 年」，模型學會「近 + 數字 = lookback」但分不出單位

**下次重訓時改 `generate_dataset.py`**：
- query_sector_performance 加「近 N 天/日」（N=1~7）模板對應 yesterday / this_week
- query_sector_trend 訓練樣本明確區隔「年」單位，不接受「天/日/月」當 lookback
- 估算：+50~80 條

---

### 3. risk 高頻詞「最大回檔 / 抗跌嗎 / 波動率」⚠️ 高優先

**踩雷日期**：2026-05-18（stress_phrases.py 500 句 / 7 條同類失敗）
**踩雷講法**：「光電最大回檔」「橡膠抗跌嗎」「ESG最大回檔」「電子通路抗跌嗎」
**LLM 誤判**：`query_sector_leaders{by:performance}` 或 `query_sector_performance`
**校正層**：`server.py` 校正 10（risk 觸發詞強制路由 query_sector_risk）

**訓練集現況**：
- `query_sector_risk` 樣本 706 條，但模板偏「{sec} 風險 / Beta / 波動」
- 「最大回檔」「抗跌嗎」「回檔多少」這種口語講法**完全沒有訓練樣本**
- LLM 看到「最大」+ sector 直接撞 leaders（市值最大）

**下次重訓時改 `generate_dataset.py`**：
- query_sector_risk 補模板：「{sec}最大回檔」「{sec}抗跌嗎」「{sec}回檔多少」「{sec}穩不穩」「{sec}波動度」
- 中英各 50 條，估算 +100 條

---

### 4. valuation 高頻詞「便宜嗎 / PE / 本益比」⚠️ 高優先

**踩雷日期**：2026-05-18（stress_phrases.py / 5 條同類失敗）
**踩雷講法**：「EV便宜嗎」「電子通路PE」「風電便宜嗎」「電腦周邊便宜嗎」「晶圓代工估值」
**LLM 誤判**：`query_sector_risk` 或 `query_sector_performance{today}`
**校正層**：`server.py` 校正 11（valuation 觸發詞強制路由 query_sector_valuation）

**訓練集現況**：
- `query_sector_valuation` 樣本 504 條，模板偏「{sec} 估值 / PE / PB」
- 「便宜嗎 / 划算嗎 / 貴嗎」這種口語完全缺
- 「晶圓代工估值」這種別名 + 估值的組合也缺

**下次重訓時改 `generate_dataset.py`**：
- query_sector_valuation 補模板：「{sec}便宜嗎」「{sec}貴嗎」「{sec}划算嗎」「{sec}估值如何」
- sector 別名（晶圓代工 / 面板 / 三大電信 / EV）擴充
- 估算 +80 條

---

### 5. money_flow 獨立名詞「三大法人 / 籌碼」⚠️ 高優先

**踩雷日期**：2026-05-18（stress_phrases.py / 9 條同類失敗）
**踩雷講法**：「ESG本週三大法人」「面板上季三大法人」「文創去年籌碼」「電子上季籌碼」「玻璃陶瓷YTD籌碼」
**LLM 誤判**：`query_sector_leaders` 或 `query_sector_performance`
**校正層**：`server.py` 校正 12（籌碼/三大法人/買賣超 觸發詞強制路由 query_money_flow）

**訓練集現況**：
- `query_money_flow` 樣本 886 條，但 96% 是「外資/投信/自營商 + 動作」句型
- 「{sec}{period}三大法人」「{sec}籌碼」這種**只有名詞、沒指定法人別**的講法樣本不足
- LLM 看到「上季 / YTD + 類股」就走 performance；看到「市值」相關詞就走 leaders

**下次重訓時改 `generate_dataset.py`**：
- query_money_flow 補模板（entity=all）：「{sec}{period}三大法人」「{sec}{period}籌碼」「{sec}{period}買賣超」
- 「文創去年籌碼」這種無動作詞模板需要明確覆蓋
- 估算 +150 條

---

### 6. LLM 幻覺 sector enum「department store / opto_components」⚠️ 低優先（白名單已擋）

**踩雷日期**：2026-05-18
**踩雷講法**：「百貨和高股息比較」「比較電子文創」
**LLM 誤判**：`compare_sectors{sector_b:"department store"}` 等不存在的 enum
**校正層**：`server.py` 校正 0 已升級為白名單（不在 30 enum 內 → 從 user_text 補抓真實 sector）

**訓練集現況**：
- compare_sectors 367 條，可能其中 sector enum 一致性不夠強
- 「百貨」這個詞 alias 為 trade_dept，但訓練時 sector_b 可能偶爾吐英文 hallucination

**下次重訓時**：
- 檢查 generate_dataset.py compare 樣本的 sector_b 是否 100% 來自 30 enum
- 確認所有 alias（百貨 / EV / 面板 / 三大電信 / 文創）都有對應到正確 enum

---

### 7. seed_data 資料缺：last_quarter 在某些 sector

**踩雷日期**：2026-05-18
**踩雷講法**：「上季漲最多的類股」「文創和風電比較」「上季橡膠和建材」
**LLM 路由**：正確（compare_sectors / all）
**校正層**：**無法處理**（這不是 LLM 錯，是 seed 缺資料）

**根因**：fetch_snapshot_v3.py 算 `performance.last_quarter` 在某些 sector 缺值（可能因為 daily 資料起點不夠舊）

**修法（不是訓練集）**：
- 改 `fetch_snapshot_v3.py`：last_quarter 缺 → 從 daily 動態算（compare_sectors / query_sector_performance 都該 fallback）
- 或在 finance.py compare_sectors 加 last_quarter fallback 用 daily 算

> **注意**：此項不算進「累積 5-10 條重訓」門檻，是純資料層問題。

---

### 8. 黃金（GOLD）作為第 8 個 currency ⚠️ 中優先

**踩雷日期**：2026-05-19
**踩雷講法**：「黃金走勢」「金價多少」「gold YTD」
**LLM 誤判**：未見訓練樣本 → 隨機路由（可能走 performance / market_overview / sector_*）
**校正層**：`server.py` 校正 13（user_text 含黃金/金價/gold → query_currency{pair:GOLD}）+ CURRENCY_ZH_MAP 加 mapping

**資料層**（不是訓練問題）：
- `fetch_snapshot_v3.py` 已加 GoldPrice 抓取 + compress_gold normalize
- finance.CURRENCY_LABEL 已加 GOLD
- finance.query_currency 已支援 unit=USD/oz 顯示

**下次重訓時改 `generate_dataset.py`**：
- CURRENCIES_ZH 加 GOLD：「黃金」「金價」「gold」「黃金價格」
- query_currency 模板擴 GOLD（USD/oz 單位）約 70 條（其他 currency 平均每個 50 條，黃金可比照）
- SYSTEM_PROMPT enum 加 GOLD

**展前一晚**：要跑 `fetch_snapshot_v3.py` 才會把 GOLD 寫進 seed_data.json。
**檔案結構**：GOLD 跟其他 currency 一樣放 `currencies.GOLD` 下，多了 `unit: "USD/oz"` 旗標讓 finance 用「（USD/oz）」而不是「（GOLD/TWD）」。

---

### 9. chip 改 leaders 觸發詞「前N大 / 龍頭 / 大廠」⚠️ 中優先

**踩雷日期**：2026-05-19
**踩雷講法**：「{sec} 前5大」（v3.2 分層 chip「龍頭」按鈕原本送這串）
**LLM 誤判**：`query_sector_performance{period:5Y}`（5Y 無效）或 `query_sector_trend{lookback:5y}`
**校正層**：`server.py` 校正 14（user_text 含前N大/Top N/龍頭/大廠/大公司 → query_sector_leaders{by:market_cap}）+ chip query 改成「市值最大」當訓練集主流講法

**訓練集現況**：
- `query_sector_leaders` 923 條，但 by:market_cap 模板偏「市值最大/最大公司」，「前N大/Top N/龍頭/大廠」幾乎沒覆蓋
- 訪客口語講法跟訓練集分佈嚴重偏離

**下次重訓時改 `generate_dataset.py`**：
- query_sector_leaders by:market_cap 加「前 {N} 大、Top {N}、龍頭、大廠、大公司」模板
- 估增 ~150 條（每 sector × 5 模板）

---

### 10. chip 改主入口「{傳產 sector} 今天」誤判到 sector_risk ⚠️ 中優先

**踩雷日期**：2026-05-19
**踩雷講法**：「鋼鐵 今天」「塑化 今天」（v3.2 分層 chip 改主入口直接送「XX 今天」後發現）
**LLM 誤判**：`query_sector_risk{sector:steel}`（穩定 3/3 重現 — 不是隨機）
**校正層**：`server.py` 校正 15（query_sector_risk + user_text 含明確 period 詞且無 risk 觸發詞 → 拉回 query_sector_performance）

**訓練集現況**：
- 傳產類股（鋼鐵 / 塑化 / 紡織 / 食品...）+ 短 period 詞（今天 / 本月 / 本週）的 performance 樣本不足
- 訓練集 query_sector_performance 1,046 條雖多，但 sector × period 矩陣的傳產類股欄位偏稀
- 同樣的「半導體今天」「金融今天」LLM 不會錯 — 因為這兩類股訓練樣本飽和

**下次重訓時改 `generate_dataset.py`**：
- query_sector_performance 模板對「鋼鐵 / 塑化 / 紡織 / 食品 / 紙業 / 橡膠 / 玻璃陶瓷 / 貿易百貨 / 觀光餐旅 / 文創」這類傳產類股 + 短 period 詞（today/yesterday/this_month/this_week）強制加樣本（每組 sector × period 至少 5 條）
- 估增 ~200 條（10 sector × 4 period × 5 模板）

**展場期間**：校正 15 擋著，影響面僅限「{傳產} + 純 period 詞、無 risk 詞」這個窄組合。273 case chip 測試重訓前驗證已過 OK。

---

### 11. 「{sector} + last_* period」LLM 路由錯到 trend / valuation ⚠️ 中優先

**踩雷日期**：2026-05-20
**踩雷講法**：「AI 去年」「AI概念 去年」「高股息 去年」「AI 上月」「高股息 上月」
**LLM 誤判**：
- 「AI 去年」→ `query_sector_trend{ai_theme, 3y}`（3/3 穩定 fail）
- 「高股息 去年」→ `query_sector_valuation{high_dividend}`（3/3 穩定 fail）
**校正層**：`server.py` 校正 16（func ∈ {trend, valuation, leaders, money_flow} + 含 period 詞 + 不含對應意圖詞 → 拉回 query_sector_performance）

**訓練集現況**：
- training_data.jsonl 含「去年」39 條，全部對 query_sector_performance — 但仍敵不過 LLM 對「AI / 高股息 + 時間詞」的偏置
- 推測「AI 近三年」「AI 三年累積」這類 trend 樣本在 AI sector 過多，讓 LLM 把「AI + 任何時間詞」一律當 trend
- 「高股息」+ 時間詞訓練樣本稀少 → fallback 到 valuation（殖利率主題類股）

**下次重訓時改 `generate_dataset.py`**：
- query_sector_performance「{ai_sector} + last_*」明確補滿：ai_theme / server_theme / high_dividend × {去年、上月、上季、上週} 至少各 10 條
- 估增 ~200 條（4 sector × 4 period × 5-10 模板）

**展場期間**：校正 16 擋著，影響面是「{任何 sector} + last_* / today/yesterday/this_*」這個組合的全 30 sector 救援。273 chip 測試已驗證沒回歸。

---

### 12. 建議型 query 沒帶風格詞時 LLM 路由散落（方案 B 用 校正 17 兜底）⚠️ 中優先

**踩雷日期**：2026-05-20
**踩雷講法**：「最近最適合投資哪一類股」「該買哪個產業」「有什麼類股可以買」「哪個類股有潛力」「看好哪個產業」「哪檔好」「現在該買什麼」「哪個類股比較好」「可以買什麼類股」
**LLM 誤判**：9 條建議型 query 散落到 sector_trend / sector_ranking / error / guide / rejected 5 種不同地方，只 2 條走對 recommend_allocation

**校正層**：`server.py` 校正 17（建議意圖詞 + 無風格詞 + 無具體 sector → recommend_allocation{balanced}）；同時：
- 守門員 `GATEKEEPER_KEYWORDS` 加「該買 / 適合 / 看好 / 推薦 / 哪檔 / 哪個 / 哪一 / 潛力 / 值得 / 想買 / 可以買 / 買什麼 / recommend / suggest / worth / invest / pick / best」（讓「哪檔好 / 現在該買什麼」過守門員）
- `_is_guide_request` 加建議意圖排除（「有什麼類股可以買」原本撞 GUIDE_KEYWORDS「有什麼」變 guide）
- 校正 2「all 漲最多 → compare_sectors」加排除：含建議意圖詞時不觸發（讓給校正 17）

**訓練集現況**：
- recommend_allocation 90 條全部要帶風格詞（保守/穩健/積極/衝/均衡）才能觸發
- 「該買哪個 / 哪個有潛力 / 推薦類股」這類「沒帶風格詞的建議型」訓練樣本 0 條
- 是 11 個 function 中訓練樣本最少的，所以校正規則特別多

**下次重訓時改 `generate_dataset.py`**：
- query_recommend_allocation 加「無風格詞建議型」~200 條：
  - 「該買哪個產業」「推薦類股」「適合投資哪個」「有什麼類股可以買」→ 全部標 `balanced`
  - 「哪個類股有潛力」「看好哪個產業」「哪檔好」→ 也標 `balanced`
- 中英雙語各 100 條
- 估增 ~200 條（recommend_allocation 從 90 → 290 條）

**展場期間**：校正 17 擋著夠用，9 條 query 已驗證全走 allocation。

---

### 13. 模糊時間詞「最近 / 最新 / 表現」+ 期間變體 LLM 路由不穩定 ⚠️ 中優先

**踩雷日期**：2026-05-20
**踩雷講法**：
- 模糊時間詞：「AI 最近表現」「AI 最新」「半導體 最近表現」「金融 最近表現」（53 條訓練樣本「最近」無 period、10 種 period 各 1-10 條 → LLM 隨機）
- 期間變體：「AI 上一週」「AI 前一週」「AI 最近一週」LLM 吐 this_week（應為 last_week）

**LLM 誤判**：
- 「AI 最近表現」→ `query_sector_performance{ai_theme, last_quarter}`（穩定 fail）
- 「半導體 最近表現」→ `{last_week}`、「金融 最近表現」→ `{last_quarter}` — 每個 sector bias 不一樣
- 「AI 上一週」/「前一週」/「最近一週」→ `{this_week}` 而非 `{last_week}`

**校正層**：
- 校正 18（模糊時間詞 + 無明確 period + 無 trend 意圖 → query_sector_performance{this_month}）
- 校正 16b（sector_performance + user_text period 跟 LLM period 不一致 → 覆蓋）
- `_PERIOD_KW_MAP` / `_PERIOD_KW_RE` 加變體「上一季/前一季/最近一季」「上一個月/前一個月/最近一個月」「上一週/前一週/最近一週」
- 「⏰ 換期間」chip UX 兜底（方案 A）

**訓練集現況**：
- training_data.jsonl 對「最近」標 53 條無 period + 10 種 period 各 1-10 條
- 「表現」（不含最近）101 條無 period + 16 種 period（含 13 種 YYYY-MM 自訂月）
- 「上一週 / 前一週 / 最近一週 / 上一季 / 前一季」這類變體**完全沒覆蓋**
- LLM 對特定 sector × 模糊詞 bias 不一致（AI / 半導體 / 金融 各自吐不同 period）

**下次重訓時改 `generate_dataset.py`**：
- 模糊時間詞統一標 `this_month`：「{sector} 最近」「{sector} 最新」「{sector} 怎樣」「{sector} 現在如何」每 sector 各 5 條 → +150 條
- 期間變體：「{sector} 上一週 / 前一週 / 最近一週」→ `last_week`、「{sector} 上一季 / 前一季 / 最近一季」→ `last_quarter`、「{sector} 上一個月 / 前一個月 / 最近一個月」→ `last_month` 每 sector × 3 變體 × 3 期間 → 約 +270 條
- 估增 ~420 條（recommend_allocation 模板擴增另計）

**展場期間**：校正 18 / 16b + 期間 chip 三層兜底，**訪客點 chip 永遠能切到對的期間**，UX 上不影響使用。

---

### 15. compare_sectors 路由 ── 複合 sector 詞被誤拆 ⚠️ 中優先（v3.4 backlog #14）

**踩雷日期**：2026-05-22（v3.4 chip test 唯一 fail）
**踩雷講法**：「AI 伺服器 YTD 表現」
**LLM 誤判**：`compare_sectors{sector_a: server_theme, sector_b: it_service, period: ytd}`

LLM 把「AI 伺服器」當「AI（ai_theme）+ 伺服器（server_theme）」兩個 sector，加上「表現」沒明確比較詞但偏 compare 訓練樣本被吸引 → 走 compare_sectors。實際 user 想看的是「server_theme 的 YTD 表現」單一 sector。

**校正層**：校正 19（compare_sectors + sector_a != all + user_text 無比較詞 → query_sector_performance{sector_a, period}），chip test 從 320/321 → 321/321 全綠

**訓練集現況**：
- 「AI 伺服器」訓練樣本主要走 leaders/risk/valuation（chip dimension）共 ~30 條
- 「{複合 sector} {period} 表現」這類組合：query_sector_performance 中沒覆蓋
- 複合 sector 詞清單：「AI 伺服器 / 電動車 / AI 概念 / 高股息 / ESG」5 個（其餘都是單一詞）

**下次重訓時改 `generate_dataset.py`**：
- 5 個複合 sector × 10 個 period × 5 種講法 = +250 條 query_sector_performance case
- 範例：「AI 伺服器 YTD 表現」「電動車 本月漲多少」「高股息 上月走勢」
- 同時加「AI 伺服器 跟 半導體 比較」這類**真正 compare**的訓練樣本，避免重訓後 compare 太弱

**展場期間**：校正 19 已修，chip test 100% 通過。

---

### 16. 軟動態 window 前綴「前 N 天/週/月」LLM 沒見過 ⚠️ 中優先（v3.4 backlog #15）

**踩雷日期**：2026-05-22
**踩雷講法**：「半導體 前 4 天」「AI 前 1 週表現」「金融 前 3 個月表現」
**問題**：訓練集對「前 N 天/週/月」**0 條覆蓋**（「近 N」有 205 條、「過去 N」有 187 條，獨缺「前 N」）
**LLM 行為**：「前 N」沒見過 → 隨便吐 period（this_week / last_quarter ...）

**校正層 (v3.4 已修)**：
- `_EXPLICIT_WIN_RE` 前綴清單加「前」：`(?:過去|近|最近|前)\s*(\d+|[一二三四五六七八九十兩半])\s*(年|個月|月|週|周|禮拜|星期|天|日)`
- `_extract_explicit_window_days` 排除清單加「前 + 一 + 季/月/週/年」（避免誤殺「前一週/前一個月/前一季」這些 last_* 模式）

**訓練集現況**：
- training_data.jsonl 全文搜尋 `前\s*\d+\s*[天日週周月年]`: **0 條**
- training_data.jsonl 全文搜尋 `近\s*\d+\s*[天日週周月年]`: 205 條
- training_data.jsonl 全文搜尋 `過去\s*\d+\s*[天日週周月年]`: 187 條
- 「前幾天 / 近幾天」（不是 N 而是「幾」）兩個也都 0 條

**下次重訓時改 `generate_dataset.py`**：
- 在「近 N」「過去 N」既有模板基礎上加「前 N」變體
  - performance：「{sector} 前 N 天」「{sector} 前 N 週」「{sector} 前 N 個月」 各 36 sector × 3 unit × 3 N (4/7/30 天) = +324 條
  - trend：「{sector} 前 N 年」 36 × 3 unit (1/3/5 y) = +108 條
- 「前幾天」「近幾天」這類**口語不帶數字**：估 +50 條（標到 period=this_week 或 last_week）
- 合計 +480 條

**展場期間**：校正層已套，「前 N」/「前一週」/「前一個月」都能正確路由。

---

### 17. fetch_snapshot 抓資料優化 — 砍掉 finance.py 沒用到的欄位 + **增量更新** ✅ v3.6 已做（2026-05-25）

**踩雷日期**：2026-05-22（v3.5 完訓後盤點）
**問題**：fetch_snapshot_v3.py 跑一次 **~63 分鐘**（用 update_snapshot.ps1，含 sync + 重打 zip），但有不少抓了**沒用**：

| 抓的資料 | 用途 | 浪費程度 |
|---|---|---|
| `allocation_models` (3 套配置) | `self.allocation_models = seed.get(...)` 載入一次後**從沒被讀過**（v3.3 撤 `recommend_allocation` function 後變死碼）| 純廢碼 |
| `daily.valuation` 5y 逐日 (36 sector × ~1236 筆) | finance.py 只用最新一筆 PE/PB/殖利率 | 96% 浪費 |
| `leaders` 5 檔個股 × 1y 逐日 | finance.py 只用預聚合排行清單，**1y daily 從沒讀** | 95% 浪費 |
| TWSE 個股 valuation API call | TWSE 官方有 `BWIBBU_d.json` 一次拿全部 31 行業 PE/殖利率/PB，可取代 36 個 `TaiwanStockPER` | 個別 call 比批次慢 |

**預估省時**（v3.6 做完）：

| 改項 | 省時 | 工程 |
|---|---|---|
| 砍 allocation_models (純清死碼) | 0 min | 10 min |
| 砍 daily.valuation 5y daily, 改抓最新一筆 | ~5 min | 15 min |
| 砍 leaders 1y 細項，改抓最新一筆 (市值/量/殖利率/perf_1m) | ~10 min | 20 min |
| `BWIBBU_d` 取代 36 個 `TaiwanStockPER` | ~5 min | 30 min |
| **合計** | **~20 min**（63 → ~43 min）| ~75 min |

**展場期間**：不急 — fetch 一週才跑一次，省 20 分鐘 vs 工程 75 分鐘，**展場結束後再做**。

**附帶 seed_data.json 瘦身**：6.84 MB → 估 ~5.5 MB（精算後修整）

**實作（v3.6 2026-05-22）**：
1. ✅ `per_rows` 從 5y daily 改抓「最近 35 天」（finance.py 只用 latest）— `start_date=(SNAPSHOT_DATE - timedelta(days=35))`
2. ✅ leaders `ticker_prices` 從 1y 改抓「最近 35 天」（保 30 天 perf_1m 計算 + 5 天假日 buffer）
3. ✅ `daily.valuation` 寫空 list（finance.py 完全沒讀，未來要用再加回來）
4. ⏸ `allocation_models` 保留（hardcoded 不抓 API、不省時，user 決定不動）
5. ⏸ TWSE `BWIBBU_d` 批次 PER（未做，工程 30 min 但省 ~5 min，未來再做）

**實際省時待驗證**：原估 ~15 分鐘（63 → ~48 min），user 下次跑 update_snapshot.ps1 後實測。

---

**v3.6 增量更新（2026-05-25 加碼）**：
過去資料**永遠不變**，每次重抓整份 5y 太笨。實作「**增量模式**」：

| 改項 | 內容 |
|---|---|
| `fetch_snapshot_v3.py` | 加 `--incremental` / `--full` / `--dry-run` / `--force-full` flag |
| `verify_schema()` | 偵測 schema_version / sector set / currency set / ETF proxy / data_source 對齊 — 不符印 diff + 互動詢問 |
| `merge_daily()` | 合併新舊 daily list、去重（同 date 留新的）、排序 |
| `get_last_date()` | 從 daily list 取最末日期 → 各 source 個別算增量起點 |
| `fetch_twse_incremental()` | 只抓 last_date+1 → today，無 checkpoint（每天才幾筆） |
| `incremental_main()` | 7 步：讀 seed → 驗 schema → 算 last_date → 估算 call → TWSE 增量 → FinMind 增量 (per-sector merge prices + 補 money_flow + 刷 valuation latest + 重抓 leaders 35d) → 重算 allocation_models |
| `update_snapshot.ps1` | 加 `-Mode incremental/full` `-DryRun` `-ForceFull` 參數，**預設 incremental** |

**預估省時**：
| 場景 | full | incremental |
|---|---|---|
| 每日跑（補 1 個交易日）| ~40 分 | **~30-60 秒** |
| 週末/連假後（補 3-5 天）| ~40 分 | **~1-2 分** |
| 隔月跑（補 22 天）| ~40 分 | **~5-8 分** |
| schema 改 → 自動 fallback `--full` | ~40 分 | ~40 分 |

**dry_run 驗證**（2026-05-25 跑完當天）：
```
[2/7] 驗 schema 對齊
  ✓ schema 對齊（3），可增量
[3/7] 算各資料源 last_date
  TWSE sector last_date: max=2026-05-25 → 增量起點=2026-05-26
  USD/JPY/EUR/CNY/HKD/GBP/AUD last_date=2026-05-22  ← 匯率收盤晚 1-3 天正常
  GOLD last_date=2026-05-25
  TAIEX last_date=2026-05-25
[4/7] 估算 API call 數
  TWSE: 預估 0 call（今天剛跑過、無新交易日）
  FinMind: 預估 ~150 call (~0.1 分)
```

**保留所有歷史**（不砍 5y 外的）— user 偏好：seed_data 從 6.84 → 4.50 MB（v3.5 瘦身），每年再漲 ~0.5 MB 可忽略。

**⚠️ v3.6 已知限制**：增量版**沒寫 checkpoint**（當初判斷「快」就不寫），但 FinMind 一次 ~334 call，剛跑過 full / 連發兩波會撞 600/hour quota。撞到 quota 第 10 個 sector 就 exit、9 個 sector 已抓的成果**丟在記憶體**（seed_data 還是舊版、沒被動）。

**對應策略**：
1. **目前作法（保守）**：等 1 小時整點 quota reset 後重跑（seed_data 還是 5/22 版、會從頭跑）
2. **未來想做**（v3.7+ 候選）：增量版加 `.fetch_incremental_checkpoint.json`，撞 quota 後保留已抓 sector，下次重跑接力。工程 ~30 min。

**踩過的時間點**：2026-05-25 撞 quota（剛跑完 full、增量是同一小時內第二波）

---

### 18. RPI5 完全離線部署包 ⚠️ 中優先（v3.6 候選，user 思考中）

**踩雷日期**：2026-05-22（user 提出需求）
**需求**：類比 win11_installer/ 給 RPI5 一鍵離線部署包，**含開機自動 kiosk display 頁 + 手機掃 QR 進 index**。

**user 待決策**：
1. **RPI5 上網方式**：自架 WiFi 熱點 / 接現場 WiFi / DuckDNS HTTPS — 影響 QR code 編碼跟 install.sh 是否設 hostapd
2. **wheel 取得**：手上有 RPI5 → ssh `pip download` / 工作站 cross-platform download（llama-cpp-python 可能要從 source build）
3. **訪客畫面**：kiosk 模式 / window 模式 / 不開瀏覽器

**包設計（暫定）**：
```
rpi5demo_v3.5/                        ← tar.gz ~430-500 MB
├── install.sh                        ← 離線裝 + 註冊 systemd + chromium kiosk
├── run.sh                            ← 手動啟動
├── README.txt
├── requirements.txt
├── wheels/                           ← ARM64 wheel 全包 (~150 MB)
├── systemd/
│   ├── rpi5-demo.service             ← FastAPI server
│   └── rpi5-display.service          ← chromium --kiosk auto-open
└── test/                             ← 同 win11_installer/app/ 結構
    └── (server.py / finance.py / seed_data.json / models/*.gguf / ...)
```

**install.sh 流程**：
1. `pip install --no-index --find-links wheels/ -r requirements.txt`
2. 部署到 `/opt/rpi5demo/`
3. 註冊 systemd 兩個 service + boot 啟動
4. （選）設 hostapd WiFi 熱點

**display.html 已就緒**：v3.2 已加 `<img src="/qr.png">`，但 `EXTERNAL_URL` 環境變數要改成讀本機 IP（離線版場景）

**展場期間**：暫不做，等 user 決定上述 3 個決策後再啟動。

---

## v3.3 重大改版（本次重訓一起做）

### 14. **資料源升級：行業類股改用 TWSE 官方類指數（MI_INDEX）+ sector 從 30 → 36**

**改動日期**：2026-05-20

**問題本質**：v3.2 前所有 30 sector 的 performance / trend / risk / daily.prices 都是「單一代表股」估算。例如「半導體本月 +6.32%」實際是「2330 台積電本月」。最嚴重 case：玻璃陶瓷靠 1802 台玻估算，但 5 年漲幅跟 TWSE 玻璃陶瓷類指數差距大。

**解決方案**：
- 行業類股 prices/performance/trend/risk 改抓 **TWSE 證交所 MI_INDEX** 官方類指數（這就是奇摩股市顯示的同一個值）
- valuation / leaders / money_flow 仍走 FinMind 代表股（雙軌並存，FinMind 沒有類股級的 PE / 法人聚合）
- 主題類股 (5 個) 仍走代表股 fallback（TWSE 沒有 AI / 高股息等主題分類）

**Sector 變化（30 → 36）**：
- 砍 `petrochemical`（「石化」語意不對齊 TWSE「塑膠化工類指數」，且訓練樣本僅 80 條）
- 砍 `culture_creative`（TWSE 沒對應分類、FinMind 代表股只 1 家、訓練樣本僅 112 條）
- 加 8 個對應 TWSE 官方類指數的新 sector：
  - `cement`（水泥類指數）/ `plastics`（塑膠類指數）/ `electric_cable`（電器電纜類指數）
  - `chemical`（化學類指數，取代 petrochemical）/ `utilities`（油電燃氣類指數）
  - `digital_cloud`（數位雲端類指數）/ `sports_leisure`（運動休閒類指數）/ `home_living`（居家生活類指數）

**訓練集影響**：
- training_data.jsonl：6260 → **9495 條（+3235 條）**
- 8 個新 sector 每個平均 ~150 條訓練樣本覆蓋（cement/plastics/electric_cable/chemical/utilities/digital_cloud/sports_leisure/home_living）
- **query_market_overview 129 → 1098（×8.5 倍）**：救校正 0d backlog #1「大盤 / 加權指數 / TAIEX」當主詞 + 反序講法
- **recommend_allocation 90 → 578（×6.4 倍）**：救校正 17 backlog #12「該買哪 / 推薦類股 / 適合投資 / 值得進場 / 看好哪個」27+ 句口語覆蓋
- **suggest_sector（新 function）+582 條**：方案 C — 即時動能 / 超賣 / 多頭 / 價值四種 criterion，27+ 句口語覆蓋
- SYSTEM_PROMPT enum 30 → 36 sector，function 11 → 12
- 重訓需動（generate_dataset.py / build_function_declarations.py 已調整完成）

### 15. **方案 C 新 function：`suggest_sector(criterion)`** ⭐ v3.3 新

**改動日期**：2026-05-21

**function 設計**：
- criterion enum: `momentum / oversold / breakout / value`
- 用 daily.prices 即時演算（RSI / SMA / 60 日漲幅 / PE+yield 綜合分）
- 跟 `recommend_allocation` 區隔：後者是長期配置（保守/平衡/積極），前者是即時排序

**演算法**：
- `momentum`：近 60 日漲幅排序
- `oversold`：RSI(14) < 50 由低到高排序
- `breakout`：close > 50MA > 200MA 多頭排列
- `value`：PE 低 + 殖利率高綜合排序

**校正規則 19**（暫時 backlog #14）：含 criterion 觸發詞（動能 / 超賣 / 多頭排列 / 低估的）→ 強制 `suggest_sector` 路由。重訓 580+ 條訓練樣本後可移除。

**chip UI**：主選單第 4 顆「💡 推薦」展開 4 個 criterion chip。
**view**：複用 `sector_ranking`（不新增 view 渲染邏輯）。

**影響到的檔案**：
- `fetch_snapshot_v3.py`：加 `SECTOR_TWSE_INDEX_NAME` 對照表 + `fetch_twse_sectors_5y()` 函式（1260 calls × 1.5s sleep = ~30-40 分鐘）
- `test/seed_data.json`：重抓（新 schema 加 `twse_index_name` 跟 `data_source` 欄位）
- `test/finance.py`：SECTOR_LABEL 更新；query_sector_performance.data 加 `data_source` 揭露
- `test/server.py`：SECTOR_ZH_MAP / GATEKEEPER_KEYWORDS 加 8 個新 sector 觸發詞
- `test/templates/index.html`：chip 30 → 36、SECTOR_LABEL JS dict、SECTOR_GROUPS.industry
- `_test_all_chips.py` / `_test_corrections_smoke.py` / `_test_chart_colors.py` / `test_cases.py`：測試 case 加新 sector 覆蓋

**備份**：`backup_pre_twse_37_sector_2026-05-20/`（含 27 個 runtime 檔 + 4 份 GGUF + BF16 checkpoint，共 1.68 GB）

**估算**：+1192 訓練樣本（已準備好）

---

## ✅ v3.3 重訓後校正規則砍剩餘清單（2026-05-21 驗證）

### 已驗證可砍 ✅

| # | 規則 | 訓練樣本 | 驗證結果 | 動作 |
|---|---|---|---|---|
| **0d** | 「大盤 / 加權指數 / TAIEX」→ market_overview | 129 → **1098 條** (×8.5) | **10/10 全中**（10 種主詞 × period 變體不靠校正都路由對）| **可砍** |

### 必留（function 已撤、需 reject 規則）✅

| # | 規則 | 為何留 |
|---|---|---|
| **17** | 建議型 → reject (v3.3 改) | 砍 recommend_allocation 後，LLM 對「該買哪 / 推薦類股 / 動能最強 / 低估」散落到 5+ 個 function（leaders/valuation/performance/risk/trend/etf）。沒有訓練「reject」這類 query，必須校正層擋。 |

### 待驗證可能可砍（重訓後測 stress 才知道）

| # | 規則 | 訓練擴張情況 |
|---|---|---|
| 1 | 「lookback 詞」→ trend | trend 訓練 888 條（+164）|
| 3 | 「成交量/量大」→ by:volume | leaders 訓練 1137 條（+214）|
| 10 | 「最大回檔/抗跌」→ risk | risk 訓練 872 條（+166）|
| 11 | 「便宜嗎/PE」→ valuation | valuation 訓練 618 條（+114）|
| 12 | 「籌碼/三大法人」→ money_flow | money_flow 訓練 1070 條（+184）|

→ 跑 stress / 找 fail case，確認 LLM raw 命中率 > 95% 才砍。

---

## ⚡ 下次重訓速度優化清單（v3.4+ 做）

v3.3 重訓 7-8 小時（vs 預期 4 小時）— 撞 Windows CUDA Sysmem Fallback（VRAM 97% 滿、compute idle 等 RAM swap、功耗 84W / 名義 220W）。

**user 決策（2026-05-21）：繼續用 3070 訓練，LoRA 是下次必做重點，不換 GPU。**

### ⚠️ Windows Multi-GPU DDP 不可行（2026-05-21 驗證）

嘗試 `accelerate launch --multi_gpu --num_processes=2`，但 Windows PyTorch wheel 沒編 libuv 支援：
```
RuntimeError: use_libuv was requested but PyTorch was build without libuv support
```

設 `USE_LIBUV=0` / `TORCH_DISABLE_LIBUV=1` / `PYTORCH_DISABLE_LIBUV=1` 全沒效（PyTorch C++ 端 default 強制 use_libuv=True 但 wheel 沒編）。

**未來嘗試管道**：
1. 用 WSL2 跑 Linux PyTorch（最直接）
2. 升級到 PyTorch 3.x 看 wheel 是否補上 libuv
3. monkey-patch TCPStore.__init__ 強塞 use_libuv=False（hack）

→ **這次放棄 multi-GPU，繼續單卡 LoRA**。

### 1. **改用 LoRA finetune**（最高 ROI，提速 ~3-4x）⭐⭐⭐ user 指定優先

| 項目 | Full FT (現在) | LoRA (建議) |
|---|---|---|
| 更新參數 | 268M 全部 | ~5M (adapter only) |
| VRAM | 7.9 GB | **~3 GB** |
| 速度 | 35-40 s/step | **~10 s/step** |
| 總時間 | 7h | **~2h** |
| 模型品質 | 99.4% acc | 99.0-99.3% (略低 0.1-0.3pp，可接受) |
| 部署 | 直接合進 GGUF | merge_and_unload 後合進 GGUF |

**做法**：`finetune_local.py` 加 PEFT config（~30 行 code）

```python
from peft import LoraConfig, get_peft_model
lora_config = LoraConfig(
    r=16, lora_alpha=32,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    lora_dropout=0.05, bias="none",
    task_type="CAUSAL_LM",
)
model = get_peft_model(model, lora_config)
```

訓練完用 `model.merge_and_unload()` 合 adapter 進 base model 再轉 GGUF。

**驗證標準**：第一次 LoRA 訓練後跟 v3.3 full FT 比 token accuracy / eval_loss 差 < 0.5%、E2E 命中率差 < 2% 就採用永久 LoRA path。

### 2. **關 Nvidia CUDA Sysmem Fallback**（治本，1 分鐘）

Nvidia 控制台 → 管理 3D 設定 → 全域 → `CUDA - Sysmem Fallback Policy` → **Prefer No Sysmem Fallback**

效果：VRAM 滿直接 OOM 而不偷溢出 → 立即發現問題、降參數而非默默變 6x 慢。

### 3. ~~改用 GPU 1/2 (3060 12GB) 訓練~~ — **user 不採用，繼續用 3070**

理由：LoRA 把 VRAM 從 7.9 GB → ~3 GB，3070 8GB 完全綽綽有餘，不需換卡。

### 4. **訓練前重開機，關閉吃 VRAM 程式**

- Chrome（GPU 加速 ~500MB-1GB）
- VS Code GPU 渲染
- Discord / Slack / Teams（Electron + GPU）
- Wallpaper Engine 等桌布動畫

訓練前 `nvidia-smi` 確認 3070 VRAM 使用 < 500 MB 再啟動。

### 5. **packing=True 嘗試**

`finetune_local.py` 現在 `packing=False`。改 True 把多短樣本拼進同一個 1152 token window，減 padding 浪費 30-50% compute。要驗證 EOS 不跨樣本污染。

### 6. **降 max_length 1152 → 1024**（最後手段）

sanity 顯示 max=998 / p95=988 — 降到 1024 留 26 tokens headroom 還夠，VRAM 省 ~22%（O(n²)）。但極長樣本會被砍。

---

### 預估綜合提速（3070 + LoRA path，user 指定）

| 階段 | 做了什麼 | 預估訓練時間（3070）|
|---|---|---|
| v3.3 現在 | full FT、3070、撞 Sysmem Fallback | **~7-8 小時** |
| **v3.4 第一步** | LoRA 改寫 + 關 Sysmem Fallback + 訓練前重開機 | **~1.5-2 小時** ⭐ |
| v3.5 進階 | 上面 + packing=True | **~1 小時** |

關鍵節省來自 LoRA：
- VRAM 7.9 GB → ~3 GB → 不撞 Sysmem Fallback → compute 真的滿載
- 268M 全更新 → 5M adapter 更新 → 反向傳播計算量大幅減
- 3070 從「memory-bound 等 swap」→「compute-bound 全跑」

---

## 待補項目模板（之後遇到照這格式記）

### N. 「踩雷講法簡述」⚠️ 優先級

**踩雷日期**：YYYY-MM-DD
**踩雷講法**：訪客原文
**LLM 誤判**：`function{args}`
**校正層**：server.py 校正 X
**訓練集現況**：哪個 function 樣本不足 / 哪個講法沒覆蓋
**下次重訓時**：改 `generate_dataset.py` 哪段
**估算**：增加幾條
- 2026-07-05 warehouse_v2 conv100 收斂第5輪（commit 6190a77）：全新100句抓 25 破口全修——config item 商品範圍/compare 倉名 rewrite 資訊銷毀/排程句 rewrite 銷毀/跨訪客刪除 pending 污染/缺貨與RCA口語詞/守門黑白名單。守衛庫 192→227，本機回歸 100%。
- 2026-07-05 warehouse_v2 conv100 收斂第6輪（commit 0e1cb57）：全新100句抓 25 破口全修——新動詞窮舉（抓/支援/往/新到/走了/掃走/訂了）、「三個倉」被量詞吃成qty=3、dispatch 庫存排行攔截劫走業績句、資料庫匯出搗蛋開卡。守衛庫 227→261，本機回歸 100%、RPI5 驗證 53/53。
- 2026-07-05 warehouse_v2 conv100 收斂第7輪（commit 8947ab7）：25 真bug 全修（衛生棉fuzzy開錯卡尾字判準/帳面誤傷/各倉+進出劫持），亂打組 20/20 全過。收斂標準改為「真bug清零」（危險+答非所問歸零，優雅拒絕不算），測試批改擬真分布。守衛庫 261→295。
- 2026-07-05 warehouse_v2 conv100 收斂第8輪（commit 94e082f）：首批擬真分布 100 句真bug 10 個全修（fuzzy幻覺三連發=score門檻+接地檢查、熱銷rewrite資訊銷毀、庫存排行劫guide範例句）。regression_ws 升級 view+內容雙驗（第三欄關鍵字）。守衛庫 295→312、回歸 100%。第9批=收斂驗證批。
- 2026-07-05 warehouse_v2 conv100 收斂第9輪（見 git log）：擬真批真bug 10→5 全修（行動電源幻覺/叫貨/週報表排程/雙倉單品比較拒/系統壞掉劫到期）。守衛庫 312→318。第10批=收斂驗證。
- 2026-07-06 warehouse_v2 conv100 收斂第10輪：真bug 5→3 全修（C18 clf蓋寫compare繞過守衛=重大架構洞、賣不好/有人買詞）。守衛庫 318→323。真bug軌跡 10→5→3。
- 2026-07-06 warehouse_v2 conv100 收斂第11輪：真bug 7 全修（通常還拿/出貨幾台/直接開單+gate同步/帳對嗎/賣多少錢/幫我看設定/免費送我列表洩漏）。守衛庫 323→331。軌跡 10→5→3→7。
- 2026-07-06 warehouse_v2 conv100 收斂達成（commit 21bddda，第12輪）：擬真批真bug 10→5→3→7→3 穩定≤5、危險級連續五批 0，user 拍板收斂。守衛庫 336 句（view+內容雙驗）100%。已知長尾記於記憶 warehouse_v2_convergence。
- 2026-07-06 warehouse_v2 conv100 第13輪加驗（見 git log）：真bug 6 全修，重點=category 幻覺通殺（_drop_ungrounded_category 四路覆蓋，真商品曾被錯類別濾成找不到）。守衛庫 336→345。危險級持續 0，收斂狀態維持。
- 2026-07-06 warehouse_v2 conv100 第14輪：真bug 僅 2（歷史最低）全修（related kw雜訊重抽 C6b、不用聽指令注入變體）。守衛庫 345→347。軌跡 10→5→3→7→3→6→2。
- 2026-07-06 warehouse_v2 conv100 第15輪收官（見 git log）：真bug 2 全修（C1c LLM kw接地、算零元列表洩漏）。15輪軌跡 10→5→3→7→3→6→2→2、危險級連續八批0。守衛庫 347→350。任務完成。
- 2026-07-06 warehouse_v2 展前優化項4（見 git log）：movement 支援 昨天/上週 真日期（原回本週近似值）+ C2e 原句時間詞覆寫 + oov空提示修。回歸 352/352 零回退，RPI5 部署完成。
- 2026-07-06 warehouse_v2：regression_ws 加 --rpi5，RPI5 首次全量回歸 351/352 抓到平台分歧句（進貨多少→manage_config），protect 詞修復後雙平台 352/352。工作流定案：本地快篩、RPI5 實戰。
- 2026-07-10 warehouse_v2 r17「每句跨界」100句收斂（見 git log）：真bug A級12+B級6 全修——RCA「少貨」通殺修、config不存在商品曾開183項卡、中文數字「萬」、同音錯字正規化層(_TYPO_NORM)、進出貨數量防呆(負數/0/9999上限)、C13c無數量追問、descriptor寫入意圖守衛、meta-gate後設取消句、movement rewrite資訊銷毀第五例(_mv_keep)、C7到期keyword/category注入、安全庫存排名→config read。守衛庫+24句=591，本地591/591。
- 2026-07-10 warehouse_v2 r18「攻擊面錯開」100句收斂（見 git log）：真bug A級7+中級11 全修——「萬」進全數量regex、C13a模糊量詞「全部」+OOV放寬2字、雙寫入複合句clarify（曾開方向相反調貨卡=危險級）、RCA裸「不對」收窄、反向詞序「30個耳機進北倉」、Pre-C-Sched裸「自動」移除、related髒kw接地（單字「人」曾錨到露營帳篷）、rescue加list_files、SKU代號直查、兩商品銷量比較新功能、C7指名不存在商品誠實化、rewrite資訊銷毀第六例(_exp_keep)。守衛庫+21=612。
- 2026-07-10 warehouse_v2 r19「第三攻擊面」100句收斂（見 git log）：真bug A級6+中級13 全修——缺貨詞「缺最兇」曾回熱銷榜、刪排程曾進商品刪除、rewrite資訊銷毀第七例(_alert_keep設警示句)、列表攔截排除警示語境（曾吐60項全清單）、descriptor兩倉/期間詞無條件排除（r17 mv_qty同構造漏洞）、量詞「打」、全形數字正規化、空格斷開進出貨（_detect_clarify放行）、中文數字「億」、報廢=出貨、日報、最貴/最便宜直答、yoga別名、「別人的」黑名單、C1s平台分歧修（嬰兒用品RPI5）。守衛庫+22=634。
- 2026-07-10 warehouse_v2 r20「收斂驗證輪」100句收斂（見 git log）：真bug A級9+中級13 全修。核心=通用實體守衛（句帶商品/倉名/類別→跳過所有固定句rewrite，通殺已累計11例的資訊銷毀類）。另修：中文口語尾數（一百五曾=105寫進設定）、compare誤投進出統計、hot category幻覺接地、兩商品庫存比較、C3/C7倉名注入、黑名單裸「歸零」窄化、量詞十幾/十來/打/範圍、C13a零倉分支、XSS字串黑名單、「類似/相關」related意圖鏈。守衛庫+25=658。
- 2026-07-10 warehouse_v2 r21「收斂達標輪」100句（commit 577084f）：真bug A級4（<5 收斂達標🎉）+中級8 全修——「一箱半」曾開-1件卡（數值錯）、C7時間殘字「這週」、否定糾正句kw殘渣（不是/我要噪音詞）、兩商品比較後綴「哪個庫存多」、C3 else分支category/warehouse注入、config「一天半」追問、descriptor「賣得好嗎」銷況。收斂軌跡 r17=12→r18=7→r19=6→r20=9→r21=4。守衛庫+21=678。
- 2026-07-10~11 warehouse_v2 r22+r23 收斂確認兩輪（RPI5 實測，見 git log）：r22 A級6句/4根因（descriptor mv_qty 送/到+一批、low「就沒貨」、C7 kw剝到期語接地、C3e low幻覺防閘+RCA/熱銷豁免）；r23 A級3-4（C1g LLM幻覺kw全域接地、guide加「熱賣」、C7時間量詞、警示管理句→list_alerts、gate三表同步教訓「拍檔/對味」、rescue幻覺kw接地+移除「哪些貨」）。**r21=4、r23=3-4 連續兩輪 <5，收斂正式確認**。守衛庫 690→709。
- 2026-07-11 warehouse_v2 OOV-100 招牌驗收（見 git log）：user 定調「可接受錯字/模糊詞」=最大特色，100 句全 OOV 專場（同音錯字25/注音殘字15/模糊描述25/英文俗稱15/混合連擊20）RPI5 實測 68→**98/100 命中**（僅 2 句合理 clarify、0 錯商品、0 rejected）。大補帖：_TYPO_NORM +30 條（籃芽/滑鼡/瑜咖/悶稍/垃圾帶/揚聲器…+earphone/speaker/powerbank/keyboard/Tshirt/beer 英文俗稱）、_DESCRIPTOR_ALIASES 優先條目 8 條（照亮帳篷的燈→露營燈、充手機的寶→行動電源、防止蚊子咬/電解質/睡外面的帳/烤肉鍋具/野餐椅/咖啡渣紙）+防太陽。守衛庫+37=746。
- 2026-07-13 warehouse_v2 r24 擬真驗證批（commit 6fa7368）：新攻擊面單輪挖 12 真bug+2 平台分歧全修——guide 商品滑窗判準（結構性終結 SPECIFIC 枚舉盲區）、倉別接地進 C13 hard-return、三倉排名/出清疑問句/剛到/RCA·config·related 詞表補漏、config read 顯示實際數值、寶寶擦屁股→濕紙巾優先條目、律紙/化鼠 TYPO。守衛庫 748→764 雙平台 100%。教訓：C13 hard-return 在 C17a-pre 之前，倉別/參數接地要在每個 hard-return 出口各補一份。
- 2026-07-13 warehouse_v2 r25 新攻擊面批（時間長尾/語氣反轉/兩實體/數字長尾/社工搗蛋）：真bug 16（含危險邊緣4）+時間長尾4 全修，見 git log。守衛庫 785 雙平台 100%。教訓：①rescue 每分支 cand 都要接地②rewrite 資訊銷毀第十例=否定句語意反轉③C7b hard-return 會蓋掉 C2e 的 period④key 抽取要最長匹配。真bug軌跡 r24=12→r25=16（新攻擊面持續出土，未收斂）。
- 2026-07-14 warehouse_v2 r26 批（多輪壓縮/序數/屬性/擬人/極端值/規格詞）：真bug 16 全修（危險4：兩萬→2000錯值卡/1.5截斷/白拿開卡/ㄎㄚㄈㄟ錯商品），見 git log。守衛庫 802 雙平台 100%。教訓：①LLM 幻覺 value 也要原句覆寫（C9-val，同 C9-key）②新規則要放同意圖 hard-return 之前（C5-diff 被 C2d 搶）③函式內 import re as _re 會遮蔽頂層。真bug軌跡 r24=12→r25=16→r26=16。
- 2026-07-14 warehouse_v2 r27 批（時段/極長句/多錯字疊加/英文/量詞錯用/功能衝突/禮貌命令式/規格詞）：真bug 16 全修（危險1：盤點語境開script卡），見 git log。守衛庫 821 雙平台 100%。教訓：①C7 到期「指名未知商品」殘渣要濾噪音字②寬詞（不夠）要給問句形豁免③C4-prod 也要接地。真bug軌跡 r24=12→r25=16→r26=16→r27=16（高原期，出題角度未耗盡）。
- 2026-07-14 warehouse_v2 r28 批（倒裝/台語/客套否定/多商品列舉/價格條件/巢狀引用/標點轟炸）：真bug 10（危險1：偷偷成本開alert卡）+minor 6 全修，見 git log。守衛庫 836 雙平台 100%。亮點：倒裝4/4、台語3/4、客套否定4/4、標點轟炸3/3 全過。真bug軌跡 r24=12→r25=16→r26=16→r27=16→r28=10（首降，收斂跡象）。
- 2026-07-14 warehouse_v2 r29 批（三槽全滿/錯置規格/半截句/雙重否定/職場口語/極短句）：真bug 6（major3+minor3）全修，見 git log。守衛庫 842 雙平台 100%。真bug軌跡 r24=12→16→16→16→10→6（收斂中）。r30=確認輪，若 <5 連兩輪達標。
- 2026-07-14 warehouse_v2 r30 確認輪+長度閘門（commit 見 git log）：真bug 6+minor 7 全修；長度閘門>30字不進LLM（確定性層接手或優雅引導）；短句掃蕩產生器 gen_sweep_r31.py 953句。定位v2：短句(2~12字含錯字)=產品本體須近100%、長句=壓力測試不追100%。守衛庫 855 雙平台 100%。軌跡 12→16→16→16→10→6→6。
- 2026-07-14 warehouse_v2 r31 短句掃蕩認證（commit 見 git log）：953 句全枚舉雙平台 100%＋守衛 855 雙平台 100%。短句空間=產品本體正式立線。修：查耳機被_too_short吞/裸功能詞反問/倉+品極短句/C-cat-short。另：長度閘門>30字、RPI5 Wi-Fi省電永久關+動態閘道watchdog（白天掉線4次自癒方案）。回歸自此=雙套(corpus+sweep)。
- 2026-07-14 warehouse_v2 r32 多輪流程掃蕩（新空間）：新工具 test/ws_convo.py（同一條 WS 連線連發劇本、可重播「按確認」、斷言 view/內容/禁止集）+ 劇本 test/convo_r32.txt（27 情境 92 輪）。真bug 6 全修（危險1：新增商品流程中說「算了」→ meta-gate 只回 clarify 沒清 _item_create_state_ws → 後續每句被吞成商品欄位＝流程劫持）。根因兩條：①確定性 dispatch 直答（r24-r31 大量新增的 hard-return continue）完全不寫 context，_update_ctx 只掛 LLM 路徑 → carry-over 被架空（「那個進出紀錄呢」回全部商品）→ 修法：send(done) 單一咽喉統一 _ctx_absorb，新出口自動涵蓋；②server 沒有 pending 卡片記憶（只活在前端 DOM）→ 對卡片說「好」被守門員拒、說「不對是100個」把 100 幻覺成「運動毛巾 100x30cm」→ 修法：_pending_by_vid + 引導層（產品決策：寫入授權只認按鈕，打字一律引導）。新鐵律：hard-return 出口不只要「自帶參數接地」，也要「寫回 context」。回歸自此=三套（corpus 855 + sweep 953 + convo 92輪）。
- 2026-07-14 warehouse_v2 r33 多輪掃蕩（放棄詞長尾/確認曖昧回應/追問邊緣/流程異常）：真bug 7 全修。①_ctx_absorb 未接地→clarify 的失敗 keyword「進30個」被吸成 last_sku（r32 自種，違反接地鐵律）②放棄詞只認「取消」二字→「我不要了」「先不要」「退出」在流程中被吞成商品名、「算了不用」「不要了」在卡片時被守門員回教學文（守門員排在 meta-gate 之前）→ 新增統一 abort 閘門（守門員之前，涵蓋流程/卡片/閒置三情境+排程警示豁免）③卡片在時「按確認」「幫我按」「這樣對嗎」→教學文 ④_CTX_GLOBAL 放單字「全」→「安全庫存多少」誤殺 ⑤create-gate 詞表窄 ⑥delete-gate 沒防呆（gate 未同步）⑦空 context 代詞句→全店統計。回退一次：ctx-empty 沒接地就攔→「這個帳篷賣多少錢」「這個月營收多少」被當純追問（代詞≠追問！代詞後接名詞是描述句、接月/週是時間片語）。四套雙平台全綠。
- 2026-07-14 warehouse_v2 r34 多輪掃蕩（多人交錯@B/卡片競態/長對話漂移/跨功能污染/寫入鏈）：真bug 6+minor 1 全修，斷言全過、全靠逐句看回答挖出。①_ctx_expand 寫入分支永遠走不到（觸發條件漏 has_write）→「有賣滑鼠嗎」→「北倉進20個」回「找不到商品『進20個』」；r32 的 B8 斷言只寫 not:error → clarify 也算過 = 假綠（教訓：斷言鬆緊本身就是測試品質）②_ctx_absorb 沒吸清單榜首（warnings/rankings）→「最急的那個還剩幾個」找不到 ③config 卡商品存在 item/preview 不是 name → 設定完 B 問「那個」回到 A ④缺單品缺貨判定 + 安全庫存覆寫在 v2_config["safety_stock_override"] 不在 item 上（改完設定再問「快缺貨了嗎」拿舊值）⑤context 存全名「瑜珈墊 6mm」組句打壞下游抽詞（單句「瑜珈墊搭配什麼賣」好、展開後反問）→ 只留主幹 ⑥RCA 詞表有「兜不攏」沒「兜不起來」。工具升級：ws_convo 加 --reset（劇本會寫資料，連跑會互相污染）+ >@B 跨連線語法（驗 vid 隔離，E2 交錯 12 輪全對）。多輪真bug軌跡 r32=6→r33=7→r34=6。
- 2026-07-14 warehouse_v2 r35 多輪掃蕩（訪客不理性：反悔鏈/追問錯字/極限省略/口氣變化/卡片與流程打架）：真bug 8 全修，斷言全過、全靠逐句看回答挖出。①追問「北倉多少」「中倉多少」→ 回全店 60 項概覽（_CTX_WH_ONLY 只認「南倉呢」，不認倉別+量詞）②追問句錯字/注音殘字讓功能詞失效：「那個近出紀錄呢」→ 回庫存、「安全ㄎ存多少」→ 回全店泛答（_TYPO_NORM 沒收追問功能詞的錯字）③反悔句「還是80好了」→ 守門員教學文、「我是說南倉」→「找不到『我是說』相關商品」(_PEND_FIX 詞表缺)④極限省略「南」「北」「呢」「咧」→ 教學文（單字倉別/純語助詞沒進 expand 觸發）⑤「哪一倉最多」→ 回全店三倉總排名，不是該商品的分倉比較 → 新增單品分倉極值直答 ⑥裸功能詞「進出」回今天、「進出紀錄」回本月（期間語意不一致）→ _CTX_BARE_CANON 正規化 ⑦英文追問 stock?/how many → 找不到商品(minor)。
  【多輪空間結論】真bug軌跡 r32=6→r33=7→r34=6→r35=8，四輪未收斂。原因非工程品質，而是空間性質：多輪 bug 幾乎全是「短句追問」（南/呢/北倉多少/進出），而多輪短句空間 = 953 單句 × N 種追問形，比單句大一個數量級，四輪只是隨機採樣不同角落。要達到 r31 等級的「可證明」保證，需比照做全枚舉 gen_convo_sweep.py（60商品 × 首句型 × 追問形含錯字/省略/語助詞/倉別/寫入），把「短句=產品本體」的保證從單句延伸到多輪。
- 2026-07-15 warehouse_v2 r36 多輪全枚舉(gen_convo_sweep.py):把「多輪」定義成可窮舉空間 = 60商品 × 33追問形 = 1980情境。首跑抓真bug:①_re 遮蔽 crash(server.py 3934 裸 import re as _re 讓 _correct_function_call 整個函式 _re 變區域變數 → C2c 用 _re 拋 UnboundLocalError → 校正鏈中斷 → 卡死全枚舉近100分;正是 CLAUDE.md 記的鐵律,拿掉裸 import 用頂層)②C7b 3520「match不到就清空keyword」對黏功能詞尾巴的低分商品(牛仔褲 match 牛仔長褲 score=3)直接放棄 → 回全部商品(7商品:牛仔褲/USB風扇/素T/登山水壺/慢跑鞋/沐浴乳/紙尿布)→ 修:match不到先剝功能詞尾巴再抽。全枚舉全綠1980/1980。守衛865/865。
- 2026-07-15 warehouse_v2 r37 未知商品抗性 + 反問路線定調:①新增商品後用新名追問(commit熱更新state,match/carry-over接得住)②問不存在商品→優雅clarify找不到,不幻覺③新商品名撞既有前綴(露營燈罩vs LED露營燈)→定調「太模糊不硬猜,回疑似清單請訪客選,反問也是互動;避免猜錯才是展場底線」→ clarify清單升級:列全(上限8)+每項附庫存概況+score斷層過濾④新增流程給怪名(符號/超長/功能詞/亂碼)→流程防呆不crash不劫持。威脅模型=訪客想玩到掛非求實用,評判=不當掉+不答錯,clarify反問一律算過。
- 2026-07-15 反問內容可測化:ws_convo 加 clarify 內容斷言(cand:has=X清單必含/hasnot=X不可含/count<=N不爆版),讓「反問對不對」從信仰變可回歸驗證(列全、列對、不亂列)。前端:clarify卡片加取消按鈕(訪客不會乖乖打「取消」二字→用按鈕)+每項顯示庫存概況。展前待辦。
- 2026-07-15 r37 深挖抓到兩個隱性 bug(答非所問/平台分歧,非危險級但接近「玩到掛」):③N1 context 污染:「鋼琴烤漆保養油庫存」(不存在商品但自帶商品名)在前句 clarify/查詢後 → _ctx_expand 誤當追問 → carry-over 到前句商品(回LED露營燈)或回熱銷榜。根因:_ctx_expand 的 _has_real_item 只認真商品,不存在的商品名被判「無實體」當追問 → 修:has_bare 觸發前先剝功能詞,剩≥3字實質描述(非代詞/倉別/疑問詞)= 自帶商品名不接地,交下游回「找不到」。無誤傷(純函式7case+全枚舉1980全綠)。④OOV 空選單 clarify(平台分歧,RPI5 專屬):「USB風扇進出紀錄」本機回單品、RPI5 回「找不到USB風扇,你是指?」但選單空(反問卻沒選項=死路=玩到掛)。根因:OOV 命中判定用 substring,「USB風扇」不是「桌上型 USB 風扇」substring(空格),RPI5 LLM 抽「USB風扇」誤判沒命中→fuzzy撈不到→空選單。修:命中判定改用 match_items(score≥5,不受空格影響,兩平台一致)→ 靜默修成真商品名。修後 USB風扇雙平台一致。教訓:①substring 判商品命中對空格脆弱,該用 match_items ②反問清單一定要有選項,空選單=死路 ③平台分歧(RPI5 llm vs 本機 route 抽詞不同)只有雙平台跑才抓得到。守衛865/865。
- 2026-07-16 [待決策·非危險級] 無量詞進出貨 bug 族：「北倉進50藍牙耳機」「出20滑鼠」（漏打量詞「個」，展場訪客打字快很常見）→ 被 C13b 之前的規則攔成 query_inventory（答錯：要進貨卻查庫存）。根因：C13b 進出貨偵測的正則要求「數字+量詞」，缺量詞就漏；且不同句型被不同前置 dispatch 攔，有的到得了 C13b 有的到不了 = dispatch 排序問題，非單點可修。已加 C13b-nomu fallback（方向詞+數字+真商品名 match≥3 → 算進出貨），部分句型改善（「補30藍牙喇叭到南倉」查庫存→clarify），但「北倉進50藍牙耳機」「出20滑鼠」仍被前置規則攔走。非危險級（不當掉、不錯寫，最多查到庫存數字，訪客會發現補「個」）。深夜未硬修 dispatch 排序（避免越改越亂），C13b-nomu 保留在 code（淨改善無誤傷）但未單獨 commit，等這族完整處理。醒來決定：要不要系統性重排 dispatch 讓無量詞進出貨都到得了 C13b。
- 2026-07-16 [決策：不修] 無量詞進出貨 bug 族（「進50藍牙耳機」漏打量詞→回庫存查詢）：user 決定不修、存查。理由：非危險級（不當掉/不錯寫，訪客補「個」即可自我修正）、發生率低、系統性重排 dispatch 風險大不划算。C13b-nomu fallback 保留在 code（淨改善無誤傷），未來若要收，用「前置守門」（dispatch 最前偵測 方向詞+數字+真商品名 match≥3 直接開卡）而非重排排序。
- 2026-07-16 [✅已決策：A回清單] user 定調原則（2026-07-16 口述）：「**不喜歡用猜的——不確定就一定要把相似的商品反問訪客；如果沒有此商品，也要提醒訪客可以新增商品**」。實作=下一批（並列查詢批 commit 後）：①歧義短稱表（帽子→毛帽/遮陽帽；電動/運動/咖啡/露營/嬰兒→各自候選）導入既有多筆 clarify 清單路（含每項庫存概況）②`_suggest_on_empty` 空手 fallback 措辭改友善+加「新商品可以說『新增商品』建立」提醒。原記錄：「咖啡還剩多少」→ 亂猜「濾掛咖啡」（5個咖啡商品挑一個）、「運動的庫存」→ 猜「電解質運動飲」（4個運動商品）、「露營庫存」→ 猜一個。都沒問訪客。這跟 07-15 定調「太模糊不硬猜、回疑似清單請訪客選」（露營燈罩那題）**矛盾**。兩種正確行為：A)回清單「你是指咖啡機/咖啡壺/咖啡豆/濾掛/黑咖啡?」（照定調，不猜）B)猜最相關一個直答（現況，順但可能猜錯）。現在是 B。**不擅自改**（改成回清單會改變很多短稱查詢體驗，是產品決策）。醒來決定 A 還 B。註：「嬰兒進出紀錄」→「3筆相關商品」合計（攤開但也沒讓選）。歧義短稱清單：電動(2)/運動(4)/咖啡(5)/露營(4)/嬰兒(3)。
- 2026-07-16 [觀察·不修] 白拿查詢化：「送我兩箱啤酒」→ 回啤酒庫存（白拿防禦只在進出貨流程 C13b 擋開卡，這句沒進出詞走查詢→回庫存）。非危險級（不當掉/不錯寫/沒真送），可接受降級。不修：「送我X」修起來易誤傷正常句（「送我到北倉的貨」）。搗蛋批其餘（注入/角色扮演/危險詞/極端輸入/SQL）全正確擋下。probe_troll 記錄在案。

- 2026-07-16 [✅決策：FastText 留著] user 定調：clf 修活留用（速度+分歧暴露面減半+C18 恢復）；**等之後 LLM 換大一點（3B/7B、晶片加速階段）路由能力足夠時再評估拿掉**。討論脈絡：clf 死掉三週雙平台照樣全綠（=縱深防禦實證，270M 本來就會出完整 function call 一人分飾意圖+參數），但 clf 活著=毫秒級路由+LLM 分歧面砍半。
- 2026-07-16 [🔥重大bug·r42修復中] **RPI5 上 intent_clf 主路由整條靜默死亡**：fasttext 0.9.3 `predict()` 末行 `np.array(probs, copy=False)` 在 numpy≥2（RPI5=2.4.4）直接 ValueError，被 intent_clf.predict 的 except 吃掉回 ("unknown",1.0)——結果=「intent_clf primary」路由 0 次觸發、C18 clf 校正全滅，每句 fallback LLM。**發現途徑**=方案2 的 clf 雙平台 dump 認證（RPI5 dump 全 unknown|1.0）。**驚人事實**：35+ 輪 RPI5 全綠都是在 clf 死掉狀態下驗的——LLM+校正層獨自扛住，是架構縱深的意外實證。**影響**：RPI5 每句多付 LLM 推理時間（修好後過半句子變毫秒級路由）、C18 保護恢復、簡報「98.9% 路由」修好才在展示機成立。修法=intent_clf.py predict 加 ValueError fallback 走 `_MODEL.f.predict` 底層 binding（不經 numpy、兩平台通用、不動系統套件），RPI5 preview 驗證 4/4 正確。**r42=此單檔修復＋雙平台真全量**（clf 復活=路由大變，必須全量不能用子集）。
- 2026-07-16 [優化·展後] intent_clf.bin 512MB 瘦身：大小全來自 fasttext 預設 `bucket=2000000`（200萬 hash 槽 × dim64 × 4B = 512MB，倉管詞彙只用零頭）。重訓時 bucket 降 20 萬 + `model.quantize()` 壓成 .ftz → 估縮到幾十 MB，RPI5 省 0.5GB RAM、載入變快、14 類窄域精度幾乎不掉。需重訓+雙平台全量重驗，展後做。
- 2026-07-16 [研究·待排程] 縮短 RPI5 測試時間兩路：①短期捷徑=RPI5 只跑「LLM-hit 子集」（分歧只發生在進 LLM 的句子；從 perf mode 欄撈守衛庫中 mode=llm 的句子清單，RPI5 全量改跑子集，展前/動 LLM 相關層時才真全量）②中期=QEMU aarch64 容器「假 RPI5」（Docker+binfmt，同 binary bit-exact；單台速度≈真機但可多開平行分片，全量 2hr→~30min；建好後用 probe_ambig.txt+守衛庫比對認證，展前最終驗收仍真機跑一次）。user 2026-07-16 提出需求「主機降精度對齊 RPI5」——降精度本身不可行（x86 AVX vs ARM NEON 浮點運算順序差異，無開關可對齊），QEMU 是正解。

## ✅ 交接筆記 v6（2026-07-20——r74–r85 **寫入契約收斂 + 砍收斂數據續跑，已收官到 R85**）
**收官定案（user 拍板）**：bug 逐句枚舉修不完，改用**寫入窄門契約**收斂——寫入抽三要素（商品＋數量＋倉別），齊則開卡、缺則 clarify 不猜、搗蛋直接拒、漏打字友善纠錯。查詢維持寬鬆。契約文件 `寫入操作規範_WRITE_CONTRACT.md`。r82-r85 user 追加「跑到 R85、砍收斂數據」：專注挖真 bug 但**不再入庫**（守衛 1122 停原地當回歸網，convo/sweep/fuzz 不再累積）→ 每輪從 ~100 分縮到 ~20 分（省掉 convo 2060 景+fuzz 380 對的全鏈）。
**最終狀態**：守衛 **1122**／convo 2067／sweep 953／branch 34+8／fuzz 409（含 write_contract 29）全綠；**展場熱門 29 句全綠且答案乾淨**。r74–r85 修 ~100 真 bug，其中**危險級 6**（空名商品落地、查無頂替寫入、裸數字寫入×2、歸零洩清單、代詞寫入補錯商品）。commit 尾：r81 `3661b3e`→fuzz `8dc86c6`→r82 `46f879d`→r83 `d108200`→r84 `ba207ad`→r85 `d58037b`。總結報告 `收斂總結報告_r74-r85.md`。
**關鍵教訓**：①枚舉修不完、原則才收斂（把個案共同解寫成規則）②核心路由改動（is_meaningful_input/clf primary）必跑全量守衛——r81 各引入 1 回歸全靠守衛 1122 抓出 ③寫入才是危險源（寫錯會落地、訪客沒發現），查詢寬鬆寫入嚴格 ④漏打字≠搗蛋。
**下一步候選（等 user 拍板）**：語音 POC（Fun-ASR-Nano）、展前基準日對齊。展場已可上線。
**暫緩（backlog）**：①「北倉補50 電動牙刷」——「補」+裸數字（無量詞）開卡漏判成查詢 clarify（「補50個」「進50」正常；「補」比「進出」更易被當補貨查詢，觸發鏈複雜，深挖成本高暫緩）。②「掃地機器人有貨嗎」查無被 fuzzy 亂中除塵電動拖把（查詢 fuzzy 邊界，非危險）。③「一箱幾瓶/總共幾瓶」單位換算、「最貴最便宜差多少」差額計算、「兩個倉加起來」倉別加總（規格/計算類，demo 無資料）。
- 2026-07-20 [r82 fuzz 擴生命週期·已完成] `context_fuzz.py` 新增 **write_contract_fuzz**（29 句）機器化守《寫入操作規範》六類鐵律（full 開卡／miss 追問／nf 查無不頂替／sab 搗蛋拒／lim 比例負數擋／typo 漏打字纠錯）。把 r74-r81 逐句人工挖的寫入破口固化成每輪必跑的常設防線——以後改寫入路由違反契約立即 FAIL，同類危險級 bug（錯誤寫入落地/裸數字被吞/查無頂替）不再需重複人工挖。RPI5 全綠：context 380 FAIL 0、write_contract 29 FAIL 0（1 WARN=傘概覽非危險）。commit `8dc86c6`。fuzz 總量 380→409。
**曲線 r74-r85**：9→10→12→12→14→8→11→契約→(r82)6→(r83)3→(r84)3→(r85)3。r82-r85 危險級僅 1（代詞寫入），破口降到 3-6 且多為計算類/demo 無資料的功能缺口（誠實 clarify 非 bug）。

## ✅ 交接筆記 v5（2026-07-19 凌晨——r55f–r68 十四輪自主收斂**已全部收官停止**）
**🎯 目標更新（user 2026-07-19）**：續跑補到 **r70**；**r70 若還錯很多就繼續收斂**（user 追加），r70 乾淨（少量拋光級）才停。r69=context_fuzz 滿版上線輪（19 前置×20 追問=380 對，含 6 種確認卡+選單/清單/比較/寫入完成態＋「打字絕不直接寫入」不變量），r70=判定輪。
**（history）r68 停止指令**：r68 全綠 commit（`5f48e93`）後曾收官。
**最終狀態**：14 輪全 commit+push；守衛 **1050**／掃蕩 953／convo **2036 情境**／branch 42／fuzz 128 全綠。危險級/邊緣共修 7 個（3 假全綠變種+腳本路徑+2 寫入流程+舊卡作廢），連 11 輪 0。總修復 ~110 真 bug。報告：`warehouse_v2/收斂總結報告_r55-r64.md`（r65-r68 補充見下方逐輪記錄）。commit 鏈尾段：`426b4f7`(r65)→`cb4b950`(r66)→`b34fe10`(r67)→`5f48e93`(r68)。
**下一步候選（等 user 拍板）**：①語音 POC（WIN11 架 Fun-ASR-Nano→20句對比→RPI5 整合；r63 已證語音長串輸入形全過）②展前 demo 基準日對齊+真機全量 ③fuzz 擴 12 前置。
**工作樹**：乾淨（warehouse_data 測試殘留與歷史 untracked 產物慣例不 commit）。RPI5 上 `_r5*_ _r6*_` 驗收檔可清。

## ⚠️ 交接筆記 v3（2026-07-18 凌晨，user 就寢、全權委託自主跑到 r60——接手讀這裡）
**授權**：user 拍板 r55f 後續跑 r56→r60；context fuzz 工具「需要就做」已授權。危險級 0 標準不變。
**目前進行**：RPI5 `_r55c_*` 驗證鏈（views/guard989/sweep953/convo2001/branch/conv100 session 重跑）跑完等收割 → 全綠 commit r55f 大批（本機工作樹：server.py/warehouse.py/tools_v2.py/index.html/corpus/convo×2/ws_inspect(--session)/branch_walk/check_views/regression_ws(noex+expiring_empty)/gen_convo_sweep 修語法）。r55f 內容詳見下方 2026-07-18 記錄。
**流程模板（每輪）**：`_conv100_rNN.txt` 出批（跨句劇情加重）→ scp 上 RPI5 → `ws_inspect --rpi5 --session --file` → 逐句審（危險級>真bug>暫緩）→ 修 → 守衛/convo 補句 → 本機探針 → 全檔 scp + restart → RPI5 四關（guard/sweep/convo/branch）+ 批重跑全綠 → commit → 下一輪。r56 批已備（`test/_conv100_r56.txt`：寫入邊緣×數量極限×空間方位×規格詢問+亂打18%）。r57-r59 角度庫：多輪新形/複合三連/商品名訛變/展場快打/混合復驗；r60=終極抽樣收官+收斂總結報告（曲線 r43=14(2危)→17→5→4→4→1→0→4(1危)→2→0→0→r54≈2→r55f=15(2危)→…）。
**context fuzz 構想（授權可做）**：仿 branch_walk——前置動作（排行/警示/到期/庫存/config/確認卡）× 追問句型（序數/倉別/期間/最急/它還剩/取消/確認）笛卡兒抽樣自動掃，斷言=不 error、不答非所問類別、不污染（前置商品≠追問答案時要接對）。做成 `context_fuzz.py` 入每輪必跑。
**RPI5**：p400@192.168.125.232 key ~/.ssh/rpi5_warehouse，扁平佈局 ~/warehouse_v2/（server.py 在根、templates/ 子目錄），服務 warehouse-v2.service，reset=POST /api/reset_demo pw 0000。**只殺 port PID、只用 Python311（本機）**。
**user 醒來看進度**：`ssh -i ~/.ssh/rpi5_warehouse p400@192.168.125.232 "cd ~/warehouse_v2; tail -5 _r5*_guard.txt; ls _conv100_*"`＋本機 `git -C warehouse_v2 log --oneline -8`。

## ⚠️ 交接筆記 v2（2026-07-17 傍晚，user 暫停開新對話——接手第一件事讀這裡）
**目標已改**：跑到 **r55 收官**（原 r60 縮短）。目前卡在 r54/r55 整包驗證中途。

**RPI5 上正在跑**（接手先收割）：`_r56_guard/_r56_sweep/_r56_convo/_r56_branch.txt`＋`_r56_DONE` 四關驗證鏈（驗 r54/55 整包：口語確認代按+三渲染器+概覽抽樣+C11d帶值選單+C9-gen通稱config+序數加寬）。守衛曾 976/978——2 FAIL 是概覽措辭舊斷言，已修斷言。
**已暫存待換版**：RPI5 `server_r56fix.py`(=本機 server.py, md5 4f9230a0 含台語黑名單) + `corpus_r56fix.txt`(md5 f5691360)。**接手流程**：等/確認 _r56_DONE → `cp server_r56fix.py server.py; cp corpus_r56fix.txt regression_corpus.txt; rm *_r56fix*` → restart service → 重跑 guard（`python3 regression_ws.py --rpi5 > _r56b_guard.txt`）→ 全綠後 commit 大批。
**本機未 commit 工作樹**（=大批內容，全部要一起 commit）：test/server.py（口語確認代按 _VIEW2ACTION_WS+voice-confirm gate／C9-gen／C9-pct 兩成／C11d 帶值選項+sentinel strip+通稱展開／序數加寬／台語黑名單／排除式否定 C-excl…）、test/warehouse.py（概覽跨類別抽樣 12 筆）、test/templates/index.html（**inventory/alert_list/schedule_list 三個新渲染器**——user 實測抓到概覽表格從沒渲染過）、regression_corpus.txt（+r53/54/55 守衛，含台語守衛反轉 chat|少年仔…）、_convo_sweep.txt/_convo_hand.txt（+r54 口語確認景+r55 config續流景）、新工具 branch_walk.py（選單分支全遍歷）/check_views.py（view 覆蓋審計）。
**commit 訊息素材**：r54=口語確認代按（好/ok/確認→真按卡，負向詞防護）+畫面級修復（user 抓到「以下為前10筆」表格沒渲染）+視圖審計補3渲染器；r55=分支遍歷抓 config 選單丟值/通稱 config 路+台語=搗蛋定調。
**r55 收官批已備**：`_conv100_r55.txt`（混沌雜燴 100 句含跨句劇情）——大批 commit 後跑 `ws_inspect --rpi5 --file` 審完修完即收官，出**收斂總結報告**給 user（曲線：r43=14(2危)→17→5→4→4→1→0→4(1危)→2→0→0→r54≈2→r55待審；守衛庫 865→978+；多輪 1980→1995）。
**新規則（user 今日定調，違者打回）**：①台語專屬字（佇/叨位/攏總/偌濟/閣有/啥物/逐家）=搗蛋→優雅拒（已入 GATEKEEPER 黑名單，帶商品名也拒）；歹勢/拍謝/欸=台灣國語不拒 ②逐句審要審到**畫面**（summary 承諾的表格前端畫不畫得出來；check_views.py 每輪跑）③**選單分支全遍歷**（branch_walk.py：每個 clarify 選項+序數路都要實走）④驗收=RPI5 單平台三套全量；本機只做探針。
**語音 POC 待辦（暫定A已拍板）**：Fun-ASR-Nano WIN11 架設→20句對比→RPI5 整合（按住說話+熱詞60商品名+_PEND_OK/取消詞入熱詞）；麥克風=Fifine AM8 待採購。

- 2026-07-20 [語音 POC·真人麥克風打通+真人聲修復] commit `b441062`。**麥克風全鏈打通**（先用 webcam C930c 代替未到貨的 AM8——任何 USB 麥克風同一條路徑）：新建 `voice_poc/check_mic.sh` 六項體檢（硬體/預設來源/實錄音量/送ASR/瀏覽器權限），**全過**。⚠️ **踩雷預告**：RPI5 預設輸入來源原本是 `hdmi...monitor`（HDMI 音訊迴路），插 webcam 後 PipeWire 自動搶到預設才沒事；**AM8 到貨不保證同樣順利**，若錄不到聲音就是這關，用 `pactl set-default-source` 一行解決（check_mic.sh 會直接指出）。**真人聲測出合成音測不到的東西**：user 唸「北倉進五十個滑鼠」→ ASR「**北藏近五十個華族**」三字全錯。修：①「藏」補進倉別同音表（守衛/sweep 含「藏」皆 0 次）②發音層**捲舌音節還原**救「華族」。③**修復後文字上畫面**（user 指定「直接顯示修復後的就好」）：原本氣泡顯示 ASR 原始錯字→訪客以為系統沒聽懂，其實內部已修好；語音路徑 `/api/asr` 回傳的本就是修正後文字、打字路徑新增 `user_fixed` frame 讓前端改寫氣泡。④護欄測試 `voice_poc/test_asr_norm.py` 移進專案（原只在暫存區）。驗收：守衛 1122/1122、fuzz 29 句 FAIL 0、護欄 22 項全過。
- 2026-07-20 [🩸血淚·門檻放寬誤配] **守衛擋下一個誤傷檢查表漏掉的危險誤配**。為救「華族」把發音門檻 0.82→0.78 並做字元折疊(sh→s)，12 個非商品詞誤傷檢查**全過看似安全**，但守衛抓到：「南倉出5箱衛生棉」→ **誤配「三層抽取衛生紙」開出貨卡扣 5 件**（衛生棉非主檔商品，應回查無；訪客要出A系統卻動B庫存＝寫錯資料，比找不到更嚴重）。量化後發現**無解**：衛生棉→衛生紙 0.8000、華族→滑鼠 0.8000 **完全同分**，純調門檻無法區分。**解法：改用音節還原(zu→zhu)，方向相反**——字元折疊是把商品名變模糊去遷就錯字，音節還原是把錯字還原回正確音 → 華族 0.8333 救得到、衛生棉 0.7826 擋掉，門檻維持安全的 0.82。**教訓（設計誤傷檢查表必看）**：要優先放「**主檔有相似商品**」的非商品詞（衛生棉vs衛生紙、牙刷vs牙線之類），而非牙膏/雨傘那種主檔根本沒相似品的安全詞——原 12 詞清單全是後者才會漏掉。已寫進 test_asr_norm.py 註解。
- 2026-07-20 [語音 POC·全語音展場驗收 24/24] commit `d857f41`。user 要求「跑一輪全由語音輸入」→ 新建 `voice_poc/gen_session_audio.py`（8 場景 24 句**連續對話**音檔）+ `e2e_session.py`（**每場景一條獨立 WS 連線＝一位訪客**，上下文才真實延續）。場景：查詢追問代稱/巡檢序數/進貨寫入+口語確認/出貨反悔取消/調撥/倉別比較/澄清選單/閒聊招呼告別。**抓到一條真失敗並修**：「它快到期嗎」→ ASR「快到**齊**嗎」→ rejected（問到期被當搗蛋）。**對照實驗定位**：`它/他快到期嗎`皆 expiring ✅、`它/他快到齊嗎`皆敗 → 敗因是**期→齊**與代稱無關。加規則 ⑤「到齊」→「到期」（守衛「到齊」0 次、「到期」17 次，邊界乾淨；**刻意不用更寬的「齊→期」**避免碰「備齊/湊齊/齊全」；驗過守衛 317 行「冬天快到了」不受影響）。**結果 23/24 → 24/24 = 100%**，守衛 1122/1122 零回歸，ASR 2.85s/句。**最關鍵驗證**：c 場景**全語音進貨真的落地**——「北倉進五十個滑鼠」(ASR 聽成華數→修正+發音層救回)→開卡→「確認」(口語代按)→真寫入→查證 293→343 件；d「算了不要」取消、e「好」完成調撥皆通過＝**HITL 確認鏈在語音下完全可用、訪客全程不碰鍵盤**。9 句 ASR 字面不一致（藍芽/華數/藍雅爾基/哈嘍/溼紙巾）但結果全對＝容錯層在扛。
- 2026-07-20 [🩸自造回歸·「加」誤劫警示建立] commit `dd2839e`。**convo 從 FAIL 1 變 FAIL 4**——上一批加的「加」寫入動詞闖禍：「**加一條** 電子產品低於20通知我」是**建立警示規則**，`intent_clf` 本來正確判 `set_alert`（**conf=1.00**），卻被 C13b「加+數字+**量詞（條）**」規則劫走成 `create_movement`，「20」被當進貨量抽走 → 剩「電子產品低於 通知我」查無商品；後三條 FAIL 是連鎖（警示沒建立、刪除當然失敗）。修：`_add_ok` 排除**警示語境**（通知我/提醒我/警示/低於/高於）。⚠️ **差點犯第二個錯**：原想連「一條」也排除，查語料發現「**條」是正當量詞**（守衛「訂走20條運動毛巾」「25條USB-C快充線」），「北倉加20條毛巾」是合理進貨句 → **只排語意不排量詞**。實測 6/6。**教訓**：新增寫入動詞時，量詞會讓它撞上「加一條規則／設一則提醒」這類**非商品的計量單位**，排除條件要針對語意而非字面。
- 2026-07-20 [語音·heavy 噪音層 20/20 + 「谷→補」變體] heavy（賣場人潮 -8dB，展場最壞情況）端到端 **20/20 接住**，ASR 2.75s/句。但**逐句審發現 3 條「顯示✅實則答錯」**（`asr_to_warehouse.py` 只驗有無 error、不驗開對卡，且走 WS 直送**未經 /api/asr 修正層**）：s11/s12 經修正層會修好，s14「中倉**補**一百個衛生紙」→「中倉**谷**」是**新變體**——非同音（bu vs gu）而是**吵雜環境辨識劣化**。已加規則（限「谷+數字+量詞」；守衛/sweep 含「谷」皆 0 次、商品名無「谷」、驗過「山谷/谷倉」不誤傷）。
- 2026-07-20 [🔧斷言去綁浮動資料] `hand-r80「照建議補」` 是**裸句**、依當下最急項開卡，斷言卻寫死「電動牙刷」＝r80 當時資料 → reset 後最急項變彈力健身環，**恆 FAIL 掩蓋真問題**。改成只驗「確認進貨」（行為對即可，不綁商品名）。改的是源頭 `_convo_hand.txt`（sweep 由它併入產生，重生會自動帶入）。**convo 首次達成 2069 情境斷言失敗 0。**
- 2026-07-20 [工具·真人語音測試套件] user 要求「準備 100 句實際用 webcam 唸」+「能不能把我的聲音加上吵雜聲，這樣安靜地方也能測」→ 建 `voice_poc/read100.txt`（100 句附期望 view+關鍵字，8 段：基本查詢20/**寫入20**/缺貨到期10/排行比較15/**RCA帳務10**/多輪追問10/澄清確認8/招呼邊界7）+ `read100.sh`（依序提示→錄4秒→ASR含修正→送WS→**自動判定**；支援續錄 `bash read100.sh 30`、區間 `1 40`、混噪 `1 heavy`）+ `live_noise_test.sh`（一次錄音、clean/light/heavy **三層對比**）。**噪音模擬解掉「辦公室無法驗證展場噪音」的限制**：錄真人聲後 ffmpeg 混入真實賣場人潮素材（與合成音檔同一份 Pixabay 錄音）。**建議先跑乾淨拿基準、再跑 heavy 對比**——才分得出錯誤是口音還是噪音造成（兩者修法完全不同）。⚠️ 第 90 句「確認」會真的寫入資料（刻意測完整寫入流程），測完 reset。
- 2026-07-20 [寫入動詞「加」+ 盤點差異「多了」] **①「加」補進寫入動詞**（w18「幫我在北倉加五十個滑鼠」原走查詢）。⚠️ **改對地方才有用**：初版只改 C13b 單字動詞判定、測試仍失敗——追 log 發現句子在更早的 **`dispatch-ws 功能描述直達`** 就被判成庫存查詢，C13b 根本沒執行到 → 真正要改的是 `_DESC_NONQUERY_INTENT` 排除表。**守衛語料先查邊界救了一命**：「安全庫存加15/加20」是 cfg 改設定、「三個倉加起來」是 inv 查詢，無條件把「加」當進貨會打壞三條 → 加 `_add_ok` 排除（安全庫存/安全線/加起來/水位）。**②「多了/少了」=盤點差異（user 定調：屬於帳對不上）**——測出**不對稱破口**：「少了」早就進 RCA、「多了」沒有，但盤點溢出（重複入帳/退貨沒沖銷）一樣要追。加 `怎麼多/為什麼多/變多/多出來/溢出/多了` 進 `_RCA_INTENT_WORDS`；「多了」不能當裸詞（corpus 兩條 `chat|差不多了 謝謝你` 告別語）→ 在 `_has_rca_word` 剝掉「差不多了」（沿用它原本剝「多少」的既有做法）。**③ 更深的修**：光加詞不夠，「北倉多了五十個滑鼠」（商品在句尾）仍失敗——LLM 誤投 `run_script{盤點}` → gate-rescue 缺腳本詞就**直接救成 query_inventory 回庫存數字**＝答非所問（問「為什麼多出來」答「現在有幾個」）→ 救援前先過 `_has_rca_word` 改導 search_log。**④ 帳務差異引導**（user 指定）：「實際比帳面多了五十個」有數字無商品名原回 rejected（正經帳務問題被當搗蛋）→ 改 clarify 引導補商品名，選項用**動作型**（哪些商品有異常/採購對帳異常/哪些商品快缺貨）而非寫死商品名（展場資料會變）。驗收 9/9：泛問「帳對不上」直接列全倉 6 筆異常、「盤點少了一些」開盤點腳本、告別語「差不多了」走 guide 未誤判、搗蛋句仍正確拒。守衛 1122/1122 零回歸（r92/r93/r94 三輪各驗一批）。
- 2026-07-20 [✅已完成·見上一條] ~~[⚠️待補·寫入動詞缺口]~~ 「**加**」「**多了**」不在寫入動詞表 → 「幫我在北倉加五十個滑鼠」走 clarify、「北倉多了五十個滑鼠」回庫存查詢。**打字/語音都失敗**（非 ASR 問題），是既有長尾。展場訪客自然講法（尤其「加」很口語），建議補進 C13b 寫入動詞判定。**注意**：要動 server 寫入動詞表＝影響打字路徑，須跑全量守衛+sweep 驗證；且「多了」語意模糊（可能是陳述而非指令），補時要確認不會把「庫存多了很多」這類感想句誤判成寫入。**（後續修正：user 定調「多了/少了」不是寫入而是**盤點差異＝帳對不上**，已改導 RCA 而非寫入動詞——見上一條。原本把它歸類為寫入動詞是誤判。）**
- 2026-07-20 [語音 POC·寫入句同音正規化] user 拍板「進出貨很重要」→ 做。**先量測不猜**：新產 20 句寫入句音檔（`voice_poc/gen_write_audio.py`，涵蓋進/出/調/補/收/退/入庫+口語變體）跑真實 ASR 探測錯誤分布 → **暴露比動詞更嚴重的問題**：⚠️**「倉」被聽錯 5 次**（昌/蒼/槍，比「進→近」3 次還多，倉別錯＝進錯倉庫）、滑鼠→華數 6 次（發音層已能救）、調→掉 1 次、數字國字→阿拉伯 5 次（無害）。**基準慘況**：12 句 ASR 錯字寫入句餵 WS 只有 **1/12** 正確開卡（「中倉近100個衛生紙」直接回庫存查詢＝訪客以為進貨了其實只是查詢）。**實作**：`server.py` 加 `_ASR_FIX` + `_asr_normalize()`，**只掛 /api/asr 出口、不碰 warehouse.py** → 打字訪客零影響、守衛零風險。四條規則**限定上下文**（守衛語料實測邊界）：①方位詞+昌/蒼/槍→倉（三字守衛出現 0 次）②`(?<!最)近`+數字+量詞→進（**排除「最近」與時間單位**——初版誤把「最近一個月進貨多少」改成「最進一個月」，守衛 14+ 條含「最近」）③掉+數量+給/到X倉→調（守衛「刪掉排程」「掉一半」無調撥目標，安全）④數字後「臺」→「台」（**OpenCC s2twp 把台轉臺，但 server 各處量詞字元類只收「台」→ 語音路徑必踩**；實測「二十臺藍牙喇叭」開不出卡、「二十台」正常；限數字後故「臺灣/舞臺/臺北倉」不誤傷）。**成效 1/12 → 18/20 = 90%**（倉別修正全生效）。剩 2 條 **w18「加」/w19「多了」是既有寫入動詞覆蓋缺口、打字同樣失敗**（非 ASR 問題、非本次回歸）——展場自然講法，值得補但要動 server 寫入動詞表+跑全量守衛，**待後續**。另 w10「南倉出30電動牙刷」回 error 是**正確擋超賣**（南倉僅 15 件），已改測試期望值為 error 以保護這道防線。
- 2026-07-20 [語音 POC·ASR 串接+前端語音 UI] ASR→OpenCC→倉管 WS 全鏈打通。**後端**：`server.py` 加 `/api/asr`（收前端錄音 → ffmpeg 轉 16k mono → llama-funasr-cli → OpenCC `s2twp` 轉繁 → 剝標點 → 回文字）+ `/api/voice_status`（前端探測本地 ASR 可用性）。**前端** `index.html`：原本用瀏覽器 Web Speech API（`webkitSpeechRecognition`）——**展場致命傷：要連 Google 雲端，離線直接 network error**（原碼第 2516 行自己也承認）→ 改為**本地 ASR 優先、瀏覽器辨識回退**。互動維持 **Siri 式「點一下自動結束」**：MediaRecorder 錄音 + AudioContext RMS 靜音偵測（門檻 0.015、靜音 1200ms 收尾、需先偵測到說話才啟動收尾防一開口就被切、15s 硬上限防展場吵雜永不收尾）。**端到端實測（RPI5）**：clean 20/20=100%、light 20/20=100%（原記錄 79%，發音容錯層補上差距），ASR 2.76-2.86s/句、倉管 0.3s/句；`/api/asr` 實測 3.0s 回「藍芽耳機庫存」。**意外收穫**：OpenCC `s2twp` 把「蓝牙」轉成「藍**芽**」（主檔是藍**牙**），正好由發音容錯層救回（3 分）——轉繁用語差異與發音層互補。
- 2026-07-20 [⚠️語音待決策] 端到端雖 100% 接住，但**逐句審出 3 句「沒 error 但答錯」**（接住率≠答對率）：①s09「哪些東西快到期」ASR 錯成「快到**齊**」→ 回今日進出貨（答非所問）②s11「北倉**進**五十個滑鼠」ASR 錯成「北倉**近**五十個**華數**」→ 查無商品（**寫入句失敗**）③s06「三個倉**各**多少」錯成「三個倉**個**多少」→ 回三倉總價值排名。②根因已查明：單獨「華數」發音層救得到（3 分），但「近五十個華數」含數字+量詞「個」被 `_WRITE_NOISE` 擋掉——這是當初為守住 63 條守衛**刻意設的界線**。**待 user 決策**：要不要讓發音層在寫入句也生效（放寬有回歸風險，需重跑全量守衛驗證）。
- 2026-07-20 [語音 POC·發音容錯層] commit `b9494a0`。ASR 錯字多為「同音字形遠」（滑鼠→華數、藍牙耳機→藍雅爾基），字形 LCS 救不到→**轉拼音比對**；注音輸入選錯字同理，等同熱詞 context biasing、RPI5 零負擔。實作：`warehouse.py` match_items 在**字形完全失敗**（無結果或最高分 <3）才跑 `_phonetic_match` fallback，救回一律 3 分（低於字形正解 7-9 分）→ 字形永遠優先、零回歸；pypinyin 純 Python，未裝則整層靜默停用。**觸發從嚴**（教訓：初版太寬打壞 63 條守衛，寫入句雜訊被亂配商品）：排除寫入動詞/倉別/供應鏈詞/數字/量詞、純中文 >6 字不救、門檻 0.82 → 只救乾淨短查詢詞。**RPI5 四關全綠**：guard 1122/1122（本機唯一 FAIL「給我看個厲害的」在 RPI5 過，證實既知平台分歧）、convo 2069 情境 FAIL 1（非回歸，見下）、branch 34+序數 8 異常 0、fuzz 29 句 FAIL 0。救回實測：華數/滑輸/無限花鼠→無線滑鼠、藍雅爾基→無線藍牙耳機、壓縮b套→運動壓縮臂套。**誤配壓測**：牙膏/牙線/手錶/耳罩/滑板/毛毯等音近但主檔沒有的詞**全回空不硬配**；亂打/寫入句/純注音符號皆不觸發。⚠️ 已知邊界：純注音符號（ㄏㄨㄚˊㄕㄨˇ）救不到（CJK 過濾濾掉），實際注音輸入法送出的是漢字故不影響，僅語音 UI 若直傳原始注音才需處理。
- 2026-07-20 [🔧斷言老化] convo `hand-r80-類別總值與照建議補` 的「照建議補」是**裸句**（沒指定商品），系統依**當下最急項**開卡，斷言卻寫死「電動牙刷」＝r80 當時資料；reset 後基準最急項為彈力健身環 → FAIL。已驗「照建議補」在 match_items 回空、發音層未介入，**與發音層無關**。同 998 行「守衛吃基準日資料」家族，但這條是**斷言本身綁浮動資料**（比資料漂移更根本）：修法應改斷言為「不綁特定商品、只驗 movement_confirm」或改用固定商品的情境。**待修**。
- 2026-07-19 [r80 判定輪審修] `_conv100_r80.txt` 36 句（修復家族變形復驗）。真 bug ~11（0 危險）：①**「中倉調100過來南倉」調貨裸數字+缺商品名**（qty 抽取補裸數字形、目標介系詞補「過來/來」、純調貨缺商品用 ctx last_sku 補回不退概覽）②「剛排的那個刪了」刪除詞「刪了」+「剛排/剛設」代稱漏（刪除閘+fullmatch+直指最後一筆全補）③「電子類總值多少」掉今天進出（單一類別總值直答）④「差幾件」週對週後追問（重算差值）⑤「照建議補」→直接開最急項進貨卡（HITL 行動意圖）⑥「懂了 平均一件多少」平均單價放寬 ⑦掉最多/處理完/補好了→rewrite ⑧就總值啦/大家辛苦/886/懂了→告別+全域詞。**曲線 r74-r80：9→10→12→12→14→8→11**。守衛 1107→1111、convo 2064→2067。≥5 → 續跑 r81。
- 2026-07-19 [r79 判定輪審修] `_conv100_r79.txt` 35 句壓力日（超量/負數/中文數字/棄卡/時間邊界/搗蛋注入）。**明顯變乾淨**：超量誠實擋、負數擋、中文數字寫入、時間邊界誠實 clarify、搗蛋/注入句全拒。真 bug ~8（1 危險邊緣）：①**「北倉出400衛生紙」無量詞寫入被查詢吞**（描述直達 mv_qty 排除+C13b 動詞判定+qty 抽取三處都吃量詞——裸數字形補齊，排除日期形+要求倉名保精度；-400 全鏈驗證）②「那全出」比例語漏 token ③「哪個倉最操」→週轉比較 rewrite ④「先不補」abort ⑤「還有什麼要處理/處理完了嗎」→警示 rewrite ⑥「就醬 88888」告別 ⑦最操倉追問 ctx（暫緩：倉別身分 ctx）。守衛 1100→1107、convo 2062→2064。RPI5 全鏈綠 commit `dc3f46c`。曲線 r74-r79：9→10→12→12→14→8（壓力日輪明顯轉乾淨）。≥5 → 續跑 r80（36 句：修復家族變形復驗＋調貨裸數字＋類別×名次）。
- 2026-07-19 [r78 判定輪審修] `_conv100_r78.txt` 37 句（採購單/安全線生命週期、跨期差異追問）。真 bug ~14（0 危險，3 誤導級）：①**「安全線多少」兩型都拿庫存數回答**（安全線→安全庫存 typo-norm 正規化+config keywords）②「剛開的採購單在哪看」又開一張 po_confirm（report-where 收 採購單/PO + orders/PO_draft dir + json 副檔名）③「帳篷跟睡袋各剩多少」一邊查無整句掉 LLM 只回單品（pair 比較一邊查無→答有的+誠實找不到；睡袋根本不是商品）④它短收幾件→search_log kw 直達（rca 最大筆商品 absorb 接地）⑤改完了嗎→config 生效驗證追問 ⑥全部倉都改150（guide 誤吃修+_cfg_bare57 全倉形）⑦改回原本→誠實引導（不記舊值）⑧差最多→compare_periods 直達 ⑨盤點什麼時候跑→排程 rewrite ⑩現在先跑一次→排程後跑腳本（voice-confirm 負向詞「先」複合化）⑪結果咧→成果直答 ⑫第二急的是什麼（缺貨序數身分形）⑬前三名出貨加總 ⑭對帳有沒有問題→rca、出最多 rewrite、告別 去忙/有事叫我/知道了。守衛 1091→1100、convo 2059→2062。驗證輪補修 3（安全線正規化豁免「補到安全線」補貨語、裸改值全倉形保留「全部」標記防平台亂猜倉〔RPI5 曾猜中區倉〕、convo 排程情境斷言放寬）。RPI5 全鏈綠 commit `6362c5b`。≥5 → 續跑 r79（36 句壓力日：超量/負數/中文數字寫入、卡片棄置久回、時間邊界、搗蛋注入）。
- 2026-07-19 [r77 判定輪審修] `_conv100_r77.txt` 38 句（退貨鏈/倉別比較/總值變形/門檻警示）。**危險級 1（錯誤寫入落地！）**：「進20個保溫瓶」查無商品→ C13b kw 帶「那」代詞被 kw_is_proxy 誤判→ ctx 舊商品（耳機）頂替開卡→查無 clarify 沒作廢舊卡→「確認」把耳機+20 寫進帳（訪客以為進了保溫瓶）。修三層：①_resolve_followup 注入前剝代詞驗殘詞，查無絕不頂替 ②寫入句查無 clarify 一律作廢舊寫入卡（_cur_text 切面）③單品缺貨判定同款誠實化（「保溫瓶還缺嗎」曾拿耳機答）。真 bug 另 ~11：上週退貨統計被黑名單擋（豁免+進貨方向直答+記在哪說明）、「算了 看熱銷第二名」被 meta 收口吞需求（功能詞 bypass+_bs 純觀看形）、進出出個報表→匯出腳本 rewrite、連帶序數（last_related absorb+直答——連帶第N家族收口）、平均一件多少錢/北中南各值多少→總值直答、「剛設的那條看一下」→list_alerts、評論式「正常嗎」簡答、告別 881/ok沒問題/沒問題。守衛 1083→1091、convo 2057→2059。驗證輪補修 5（防護誤傷面收斂：誠實反問排除全域/疑問/名次詞、昨天出貨 rewrite→確定性直答〔RPI5 期間解析平台分歧〕、meta 收口只放行放棄詞後需求、剝代詞驗殘詞先濾語氣殘渣救 r60 流程、_bs 純觀看形）。RPI5 全鏈綠 commit `ae8429d`。≥5 → 續跑 r78（37 句：採購單/安全線生命週期、跨期差異追問、口誤更正）。
- 2026-07-19 [r76 判定輪審修] `_conv100_r76.txt` 37 句（排程建立變形/巡檢反悔/英文句/亂打15%）。真 bug ~12（0 危險、1 危險邊緣）：①**危險邊緣：「那就照建議補 北倉進13個電動牙刷」被撐天直答的「建議補」token 吞掉**——卡沒開、訪客以為補了（撐天直答加寫入動詞+數字排除）②**「排程 每週一早上八點匯出進出記錄」被 _resolve_followup 的「進出記錄」hint 踩掉 set_schedule 還注入 ctx 商品**（管理/寫入類 func 加追問覆蓋豁免——第 12 例資訊銷毀）③「進貨單價多少」的「進貨單」撞 C15 誤開採購草稿卡（價格問句排除）④反悔後「就出3件 確認」斷鏈（voice-confirm 加結尾確認+數量一致代按路）⑤第三名上週賣幾件→_bs 名次×期間出量分支（含 rewrite 後形「出貨多少」）⑥第一個缺的補到安全線要進幾個→缺貨序數建議補直答 ⑦改成每週五→改排程誠實閘（刪掉重設引導）⑧生意如何→熱銷 rewrite ⑨有什麼異常/被改過→rca rewrite（\1 保倉別過實體守衛）⑩哈囉開店啦→招呼回覆 ⑪英文介面誠實閘+怎麼教 guide ⑫ok瞭解/明天再處理→告別 token；算了維持原樣→meta 收口（守門員補每週/維持等詞）。守衛 1074→1083、convo 2055→2057。驗證輪補修 3：「價格改成」出黑名單（誠實閘接手）、last_view 不被 clarify/error 覆蓋、**追問豁免收窄**（r76x 曾把 create_movement 一併豁免 → 改量句 ctx 注入失效、打破 r60 舊卡作廢守衛——豁免只留排程/腳本/警示/採購管理類）。RPI5 全鏈綠 commit `e899527`。≥5 → 續跑 r77（38 句：退貨鏈/倉別缺貨比較/平均單價/佔比/門檻警示/報表收尾）。
- 2026-07-19 [r75 判定輪審修] `_conv100_r75.txt` 37 句（首含新增商品→進貨→查→下架完整生命週期）。**危險級 1**：「幫我新增商品」殘字「幫我」走 raw_text 解析失敗→靜默落 step1 空名前進→**建出商品「」污染主檔**，且空名讓 `_text_has_item_name` 的 `"" in s` 恆真→守門員對亂打字全放行（假放行連鎖）。修三層：入口剝填充詞、step1 空名/step2 非類別/step3 無數字一律留步重問、commit 空名拒絕。真 bug 另 9：②類別存中文原字→幻影類別+x 前綴 SKU（step2 正規化成主檔 key）③**item_delete_confirm 卡「確認刪除」沒接口語確認鏈**（老缺口，_VIEW2ACTION_WS 補 item_delete）④描述別名遮蔽新商品（「鑄鐵平底鍋」被「平底鍋→不沾鍋」搶——_descriptor_hit 句含完整商品名讓路）⑤刪除流程中亂打吐 error frame→友善退出 ⑥價格改成299→改價誠實閘 ⑦之前設的排程還在嗎→排程查詢直達 ⑧報告在哪看曾重跑報告→最新產出物直答（只認 md/csv/png）⑨哪一類最不值錢/南倉整體值多少→總值直答雙向+觸發詞 ⑩好都沒問題/巡到這88→告別 token。「剛加的那條刪掉」→ 直指最後一筆。守衛 1065→1074、convo 2053→2055。驗證輪補修 3（描述別名讓路句內完整名以救排除式守衛、「價格改成」移出黑名單、剛建完直刪 alert_done/schedule_done 也接）。RPI5 全鏈綠 commit `63b0888`。≥5 → 續跑 r76（37 句：排程改期/名次差/RCA/英文句/寫入反悔+口語確認帶內容）。
- 2026-07-19 [r74 判定輪審修] `_conv100_r74.txt` 42 句（首含排程建立→列表→刪除完整生命週期）。真 bug 9（0危，2 個語義誤導級）：①**schedule_list 後「刪掉它」誤入商品刪除**（「它」被 ctx_expand 展開成商品——加 last_view/last_sched_jobs ctx、expand 旁路、刪除閘排程分支、schedule_delete_confirm 入 _PENDING_VIEWS+_VIEW2ACTION_WS 口語確認鏈+job_id 傳遞；單筆直出確認卡、多筆問 ID）②排程判重回「已有月底盤點」讓人霧煞煞（alias note 點明盤點=缺貨檢查）③「那看庫存總值」被 ctx 注入成單品（總值入 _CTX_GLOBAL＋**全店/分倉總值直答**新 dispatch）④「哪一類最值錢」誤觸 r64 類別身分（exclusion+**類別總值排行直答**新 dispatch）⑤北倉剩幾台（_CTX_WH_ONLY 尾詞補量詞 台/雙/頂/罐…）⑥照這樣多久賣完→撐天 token ⑦誰進步/退步最多→C16 compare_periods＋守門員放行 ⑧要出的單/預定出貨→**訂單誠實閘**（沒有訂單系統明說）⑨沒了下班掰（下班了?/回家了? 可選尾）＋明天自動→排程觸發詞。9≥5 → 續跑 r75。守衛 +7（1057→1064）、convo +3 景（2048→2051）。
- 2026-07-19 [r73 判定輪審修] `_conv100_r73.txt` 31 句收店巡檢。真 bug 6：出貨最多→熱銷 rewrite、週轉最快的倉→週轉比較 rewrite、動得快（_pvs 動詞+比字槽＋_DESC_BLOCK 防直達搶）、毛巾快沒了嗎（單品缺貨 token）、不管它（abort 結尾錨定放寬 12 字）、夠撐到下週（撐到/夠撐 token+gate）。狀語鏈「保險起見再進10個好了」完美接續 ✓。**高原觀察**：r70-r73 連四輪 6-7 個，全為措辭長尾（同義詞/追問覆蓋），危險級連 13 輪 0——發現率已達詞彙覆蓋漸近線，<5 門檻可能需多輪或調整定義。續跑 r74。
- 2026-07-19 [🔧流程修正·資料漂移] r72 驗收守衛 1 FAIL 根因＝**批次寫入殘留**（上輪批把牙刷補到安全線上，守衛斷言吃基準日資料）。修法：**驗收鏈開頭必加 reset_demo**（過去只在 convo 前與批前 reset，守衛跑在最前面吃到漂移）。之後所有鏈模板照改。
- 2026-07-19 [r72 判定輪審修] `_conv100_r72.txt` 36 句店長巡檢式。巡檢寫入鏈/反悔改量（舊卡作廢複驗 ✓）/滯銷清倉全通、零危險。真 bug 6：還缺嗎（單品缺貨判定補 token+ctx fallback）、都達標了嗎（rewrite→警示清單）、買10組總共多少錢（**數量×單價試算**新 dispatch）、前三名各剩多少（**逐名列庫存**新 dispatch）、第一名北倉夠嗎（_bs stock words+夠嗎——實際走到單品缺貨判定給更好答案）、第二名呢（identity 條件改 last_hot_period 防 last_func 被蓋）。6>5 → 續跑 r73。convo +3 景。
- 2026-07-19 [r71 判定輪審修] `_conv100_r71.txt` 35 句。真 bug 7（0危）：**60 項總覽 rows[0] 盲吸 last_sku**（污染家族第 4 變種——「倉租」掉概覽後「輸的那個」回從沒查過的耳機 e01；generic inventory 比照 config_read 不吸）、倉租/租金→黑名單、照這速度撐多久被守門員拒（gate keywords 補撐多久/日銷）、保險起見狀語被當商品、會不會不夠賣→單品缺貨判定 token、那不用補囉→abort、昨天賣最好→排行日粒度誠實 clarify。**暫緩**：今天呢接排行（period×rank followup 既決）、指涉倉別寫入（從最多的倉調）。7>5 → 續跑 r72。守衛 +3、convo +3 景。
- 2026-07-19 [r70 判定輪審修] `_conv100_r70.txt` 46 句全新混沌。寫入鏈×3/比例攔/糾錯/盤點→報告全通、零危險。真 bug 6（追問長尾級）：「北倉的調成400」被 ctx 寫入展開吃掉（**「調成」的調撞 _CTX_WRITE——config 改寫群整組上移到展開之前**，第三次驗證「改寫要趕在消費者之前」的教訓）、「現在北倉的設定多少」（regex 加倉別槽）、「第一項建議補多少」extract 垃圾詞蓋 ctx（比不到退回 last_sku 再試）、撐得過 token、冠亞季軍→第N名 pair、r68 ordinal-attr 邊角（功能型選單選項不是商品——加 _has_real_item 檢查）。判定：6>5 未達乾淨線 → **續跑 r71 判定輪**（user 規則：錯很多就繼續）。convo +4 景。
- 2026-07-19 [r69 fuzz 滿版輪] context_fuzz 128→**380 對**（19 前置：查詢8+確認卡6+選單/商品清單/類別清單/比較/寫入完成態；20 追問：+30件/改成50/算了照原本的/第一個）。核心新斷言：**「打字絕不直接寫入」不變量**（任何前置下裸數量/裸改值/任何句都不可直達 done view，最多開新確認卡）＋6 種卡片「確認必執行/取消必收口」。滿版首跑 **FAIL 0**——寫入安全全面成立。WARN 修 3：冷 context「只看南倉的」→ 南倉的庫存（概覽摘要加「南區倉視角」前綴）、setup 抖動重試一次、menu 重問=合理不改。工具句路由教訓：改寫目標句要先探路由（「南倉庫存概覽」曾被當商品名）。
- 2026-07-18 [r68 輪審修] `_conv100_r68.txt` 42 句混沌複驗。真 bug 6（0危）：**複合寫入被到期分流吃掉**（「最急那批處理掉 出586件北倉氣泡水」——r56 到期重秀加寫入動詞+數字讓路，重要）、選單序數+屬性後綴（「第二個多少錢」曾回概覽——ordinal-attr 取選項主幹接屬性問句）、上週勒（period stem 補勒/咧）、撐得了/建議補 token（「最緊急那個建議補多少」半答→撐天直答含建議補量）、先醬=先這樣。**暫緩**：雙名次 identity（第三名跟第五名是什麼 只答第三）、補一半就好（比例補貨 gate 家族）。convo +4 景、守衛 +1。曲線：…→6→5→**6(0危)**——危險級連 11 輪 0，長尾拋光穩態 2-6/輪。
- 2026-07-18 [r67 輪審修] `_conv100_r67.txt` 42 句（長尾掃蕩：未觸商品口語×同義大掃描）。**28 個商品口語/量詞句 26 中**（幾咖/幾把/幾雙/幾張全過、斷貨/現貨/存貨同義全過）、口語寫入（給北倉補/抓10個/幫我出）全通。真 bug 5（0危）：橡膠的那種（pair）、襪子跟毛帽比較（通稱表進比較路——_pv_resolve67）、熱銷第十名被 hot rewrite 銷毀名次（固定句資訊銷毀**第十二例**：帶第N名一律保留原句）、倒數第一名→滯銷 rewrite、今天就到這告別前綴。守衛 1044→1049。曲線：…→2→6→**5(0危)**。
- 2026-07-18 [r66 輪審修] `_conv100_r66.txt` 39 句全新抽樣。真 bug 6（0危，全長尾拋光級）：最慘的是哪個（缺貨同義 rewrite）、篩選一下前綴剝除、日銷 token 入撐天直答、排汗的口語短稱（pair 排汗的→排汗衣的，不撞完整名）、收工告別、補到哪才夠（目標水位無數字形——暫緩）。**暫緩**：最便宜的清潔用品（類別別名清潔用品→日用品未映射＋價格極值類別過濾）、代詞雙品比較（兩個哪個賣得好）、指涉數量寫入（進那個數量的）、到期子區間追問（7天內的呢）、最大批是哪批。**收斂語意判讀**：嚴格數字標準（<5 連兩輪）一直被長尾禮貌/同義詞項打斷（r63=3→r64=12→r65=2→r66=6），但危險級連 9 輪 0、寫入路徑在轟炸下零失守——實質收斂已達，剩餘是無止境的措辭長尾。守衛 1039→1044。
- 2026-07-18 [r65 輪審修] `_conv100_r65.txt` 39 句（同義動詞×連續糾錯×危險家族複驗）。真 bug 僅 **2**（0危）：route 續流不認「撥/挪/搬」動詞、行內糾錯句（「出10個 打錯 是出20個」曾回找不到商品——化簡須在 ctx 展開**之前**，rewrite 表來不及）。同義動詞（入庫/出庫/撥/挪）寫入鏈全通、危險家族複驗全綠（連帶垃圾詞/污染/警戒八成）、疑問否定 3/3。**暫緩**：它跟第N名哪個庫存多（名次比較既決）。convo +2 景。曲線：…→8→3→12→**2(0危)**。
- 2026-07-18 [r64 終極混沌輪審修] `_conv100_r64.txt` 51 句高難度連續劇情。長鏈全通（到期批出貨鏈/分倉 config/route 單邊/寫入鏈）。**假全綠家族又一變種**：LLM 同給 keyword+錯 category（「電解質運動飲」+「運動用品類」交集空 → 到期回 ✅ 沒有，實際南倉有 22 天批）——warehouse.list_expiring_items：kw 命中即丟 category。真 bug 12（1 重要+11 中小）：po 卡「不要好了我自己叫貨」不取消（卡在場 不要/不用 開頭句視為放棄，排除我要/查/看/換）、最會賣的飲料（類別排行口語 rewrite——教訓：帶 \\1 群組才過實體閘）、廚具類熱銷→第二名（rank 類別 ctx last_hot_cat 沿用+名次價格追問）、它是廚具還是食品（類別身分直答，注意 ctx 展開後句長）、審計紀錄/今天改過什麼設定→紀錄檔、夠賣多久 撐天 token、維持原樣 ASK、謝了/掰啦各位/ㄅㄞˋㄅㄞˋ 告別。**暫緩（r64）**：名次間比較（第五名跟第一名差多少——既決家族）、指涉倉別寫入（出30個 就最多的那個倉）、處理建議 followup。守衛 1034→1039、convo +4 景。曲線：…→5(0危)→8(0危)→3(0危)→**12(0危，1 假全綠家族修復)**。
- 2026-07-18 [r63 輪審修] `_conv100_r63.txt` 38 句（語音輸入形×三維組合×命令/禮貌極端×英文整句）。**語音長串 5/5 全過**（無標點碎念「欸那個幫我看一下就是那個衛生紙…」全接住——語音 POC 前景好）、命令口氣 5/5、極端禮貌 3/3、英文查詢 4/5。真 bug 僅 **3**（0危）：「今天進貨的東西有哪些」回熱銷榜（rewrite 補+吃整句尾巴）、「昨天有異動嗎」被庫存 fast-path 搶（rewrite 進出紀錄）、「好啦下班了 掰」告別（bye+下班了/回家了、開頭客套填充）。**暫緩（r63）**：進貨量倉別比較（compare 指標家族）、英文寫入句（transfer 10 mouse→顯示庫存半答）、上週進的還沒賣掉（複合語意近似答）。守衛 1029→1034。曲線：…→6(1危邊)→5(0危)→8(0危新角度)→**3(0危)**——首次 <5。
- 2026-07-18 [🔥r62 修正的修正·流程教訓] 首驗爆出：①我的時段 gate／促銷 gate 打破 3 條既有守衛（「中午前的異動」「下午有出貨嗎」＝守衛接受的整天近似、「衛生紙有優惠嗎」＝守衛接受的顯示庫存半答）——**動 gate 前必先 grep corpus 既有守衛，守衛既定行為優先**（時段 gate 全撤、促銷 gate 加 not _has_real_item 只攔無商品檔期句）②2 字詞幹修正誤殺「那ㄍ快到期嗎」60 條多輪守衛（代詞殘字 那ㄍ 不在代詞表→被當自帶商品名）——加 [那這它牠] 排除。守衛庫的價值實證：兩類回歸都是 RPI5 全鏈抓回來的。
- 2026-07-18 [r62 輪審修] `_conv100_r62.txt` 59 句（全新角度：促銷檔期語×數量變體×閒聊夾帶×疑問花式）。零危險。真 bug 8：**倉管退貨句被 r44 購物黑名單誤殺**（「退貨3個耳機 北倉」——退貨+數量+倉/商品 豁免，is_return 卡正常開）、**2 字未知商品從 ctx 展開縫隙漏過**（「奶瓶還有多少庫存」曾回保鮮盒——stem==2 且 match 不到＝自帶商品名不展開）、促銷/檔期語（打折/主打/買一送一 曾回商品庫存）→ 優雅明說沒建價格資料、時段粒度（下午出了幾件 曾默默回整天）→ 誠實 clarify（每天/排程句讓路）、「這幾天的進出」被 ctx 黏舊商品 → rewrite 本週統計、「它上週賣幾個」回庫存 → rewrite 出貨多少、config 後「現在設定多少」、「改回50」動詞組缺改回、差不多了謝謝你告別。**暫緩（r62）**：三品空格並列比較（毛帽 遮陽帽 襪子）、類別清單後「第一個剩多少」不穩（rows 沒進選單記憶——nondeterministic）、耳機不會缺貨吧回全域警示（半答）、前天呢接排行 ctx。閒聊夾帶查詢 5/5 全過、疑問花式 5/5 全過。守衛 1025→1031、convo +3 景。曲線：…→8→8→6(1危邊)→5(0危)→**8(0危)**——新角度輪必挖到存量，符合對抗性生成預期。
- 2026-07-18 [r61 輪審修] `_conv100_r61.txt` 63 句（卡片生命週期轟炸）。零危險（所有誤流程「確認」都安全落空）。真 bug 5：「剛剛那個進貨還在嗎」無卡時被 ctx 幻覺成新寫入 clarify（加 pending-status 誠實回覆）、「南倉的改成130」裸改值缺倉別前綴、「北倉現在幾個」wh_only 不吃「現在/目前」、調貨 route 續流不接單邊倉（「從北倉調」——flow 帶已知 from/to、單邊補缺側）、寫入 flow 被亂打單發消耗（改比照卡片：rejected/guide 存活、只在命中時消耗）。設計確認 OK：卡片在場「不對 是南倉進」→FIX 引導後訪客按確認執行原卡＝HITL 自主選擇；兩卡連發後蓋前 ✓；取消重來 ✓。convo +5 景。曲線：15(2危)→12(2危邊)→12→8→8→6(1危邊)→**5(0危)**——再一輪 <5 即達收斂標準。
- 2026-07-18 [r60 終極抽樣審修] `_conv100_r60.txt` 71 句一鏡到底長劇情。**危險邊緣 1**：確認卡在場說「那出200件就好」→ 新寫入 clarify 開了但**舊卡沒清**，接著「確認」執行舊的 -300（訪客要 200）——修：寫入 flow clarify 出現＝舊卡作廢（absorb pop pending）。真 bug 5：「調一點…調20個」的量詞副詞（一點/一些）被 residue 當商品名、裸改值條件過嚴（config 讀後查了庫存 last_func 被蓋——放寬成有 last_sku+無卡即接，確認卡把關）、家電廚具類有哪些被拒（類別清單口語 rewrite）、吹風機被「吹風」描述搶成 USB 風扇（負向斷言 吹風(?!機)）、北中南倉哪個最強掉庫存排行（_cmp_keep+_ent_hit 對倉別最上級雙重例外——固定句比較列三倉排名不損資訊）。拋光：中倉加油。長劇情整體品質高：寫入鏈/反悔/追問/告別全通。守衛 1020→1025、convo +2 景。曲線終值：**r55f=15(2危)→r56=12(2危邊)→r57=12→r58=8→r59=8→r60=6(1危邊)**。
- 2026-07-18 [r59 輪審修] `_conv100_r59.txt` 65 句（收斂驗證：歷輪修復交叉複驗）。危險級複驗全過（污染鏈/查無寫入/寫入續流/目標水位/負數）。真 bug 8（6 重要+2 拋光）：**generic config_read rows[0] 亂入 last_sku**（污染家族新變種：「警戒值用八成算」10 項清單第一筆耳機被存 → 「快到期的東西」被污染成查耳機到期——absorb 對 config_read >1 rows 不吸）、「全部的啞鈴都出光」被 guide 總覽搶（SPECIFIC 加出光/出掉/清光）、「第七名剩幾個」排行 ctx 被中間追問蓋掉（改認 last_hot_period）、「上週的排行呢」默默回本週榜（rank 期間 gate 加上週）、「最急的那批放哪」守門員攔（gate 前 ctx 改寫——r57 教訓再現）、「鍋子價格」extractor 抽不到（價格詞剝除+通稱表選單）、沒了掰掰88、講中文好嗎。守衛 1015→1020、convo +3 景。曲線：15(2危)→12(2危邊)→12→8→**8(0危)**。
- 2026-07-18 [r58 輪審修] `_conv100_r58.txt` 73 句（訛變×快打×長輩腔×情緒×混合復驗）。訛變/長輩腔/情緒句大範圍過關（15 訛變句 13 中）。真 bug 8：**排除式換看**（「不要衛生紙 我要看濕紙巾」「衛生紙就算了 看一下尿布好了」曾回被排除的 A＝語意反轉 r16 家族——新 dispatch 取「要/看」後的 B 直查，**必須放所有庫存 dispatch 之前**否則 A 先被接走）、排行身分後 rank context 被蓋（「第四名是啥」→「第四名剩多少」斷鏈——identity 答完 restore last_func）、北倉的設定「給我看」尾綴、裸「價格」詞（露營全套價格繞過組合詞選單）、**撐幾天直答落地**（r44④解除：days_left/daily_burn/suggest_qty 直用，單品+ctx 代詞都接）、悶少罐/電風扇訛變、罵我→黑名單。**暫緩（r58）**：帳篷椅子的價格分開報（黏寫既決）、少年仔句→guide（台語腔守衛既定）、另一個牌子唯一款。守衛 1008→1015、convo +2 景。曲線：r55f=15(2危)→r56=12(2危邊)→r57=12(0危)→r58=8(0危)。
- 2026-07-18 [r57 輪審修] `_conv100_r57.txt` 81 句（多輪新形×複合三連×混合復驗）。**回歸 1**（r56 新規則誤傷）：「出掉20件 北倉」帶確切數字仍被比例出貨攔——加數字豁免。真 bug 11：接續詞（換一個/下一個/繼續 曾回「沒有此商品」）、寫入副詞殘留（「馬上出10個」的馬上被當商品名——residue 剝副詞+沿用 last_wh 倉別）、卡片暫停詞（先等一下/還在嗎→hold 引導不取消）、排行第N名身分追問（「第五名是什麼」——**守門員在 ctx 改寫之前**是關鍵教訓：裸短句要在 gate 前改寫成含功能詞完整句）、config 讀後倉別追問（「北倉的呢」曾回庫存）、config 讀後裸改值（「改成90」）、哪個倉最滿→三倉排名、通稱+全部價格（帽子全部多少錢+通稱表展開）、上個月排行誠實 clarify、代詞銷量比較（「它跟不沾鍋哪個賣得好」ctx 接地）、告別+道謝混雜（辛苦了88——token 聯集比對）。**暫緩（r57）**：另一個牌子（唯一款誠實說明）、複合三連只答一件（既決家族）、北南各缺什麼 sequential、安全庫存誰最高（半答 generic read）、名次間比較（第二名跟第三名哪個多）、分類倉別比較（北倉日用品vs南倉日用品——r44③既決）。守衛 1000→1009、convo +5 景。
- 2026-07-18 [r56 輪審修] `_conv100_r56.txt` 93 句（寫入邊緣×數量極限×空間方位×規格詢問+亂打18%）+ context_fuzz 首跑 128 對。**危險邊緣 2**：①「奶茶進30杯」查無商品的寫入句被 `_ctx_expand` 寫入展開黏上 context 舊商品（變「奶茶進30杯無線滑鼠」→ 訪客答倉別就進錯貨）——修：寫入展開前剝動詞/數字/單位/倉名，殘留 ≥2 字＝自帶商品名不展開 ②「進30個毛帽→問倉→答北倉」變庫存查詢＝寫入流程斷裂——修：**寫入續流**（tools_v2 三處 clarify 帶 flow 槽位 + WS `_write_flow_by_vid` 接短答「北倉/25件/北倉到南倉」重呼叫，abort/reset 一併清）。真 bug 修復：負數量當查詢、大數量攔截順序（qty 上限提前到問倉前）、目標水位式「出到剩10個」誠實不支援（照數字開卡會錯量）、全出/出一半→問確切數量、空間方位句（在哪裡/幾坪/放得下/哪一排…）優雅明說沒建檔（曾回「沒有『在哪裡』這個商品」醜 clarify）、「最急的那批放哪個倉」被 r55f 的最緊急 rewrite 搶走（收窄成全句比對+expiring context 分流）、最少人買→滯銷排行、上個月改誠實 clarify（過去近似成本月＝拿錯數字不自知）、沒事掰掰、yog墊/shuei壺英拼。fuzz 首跑另抓 3 缺口：庫存後「昨天呢」被拒（期間追問擴到 query_inventory）、排行後裸序數「第二個」（改寫第N名剩多少）、全店進出後「只看南倉的」回概覽（無 last_sku 也接期間/倉別，記 last_mv_plabel）＋RPI5 平台獨有：模型偶發幻覺函式名（calculate_safety_stock）→ no_function 死路——修 clf 高信心 fallback。**暫緩記錄（r56）**：第二名跟第三名哪個多（排行名次比較）、出一箱衛生紙一箱是幾包（複合規格問句；箱=件既決 2026-07-09）、藍芽喇叭跟藍牙耳機是同一個嗎（是非題）、北中南倉各有幾種商品（分倉品項數）。守衛 989→1001、convo 2001→+10 景。
- 2026-07-18 [🎯目標再延·user 拍板] r60 後**繼續多測幾輪**（r61+），流程照舊（RPI5-only 驗收），跑到 user 喊停或「危險 0＋真 bug<5 連兩輪」達標。每輪 ~1.5-2h（批跑10分+審修20-40分+全鏈驗收70分）。
- 2026-07-18 [🎯目標更新·user 拍板] r55f commit 後**不收官、續跑到 r60**。r56-r60 出批角度（沿用 r50+ 角度庫）：寫入邊緣/數量極限/複合三連/展場快打/空間方位/規格詢問/多輪新形，r60=終極抽樣（混沌雜燴2.0）收官。流程不變：出批→RPI5 ws_inspect 逐句審（跨句劇情批用 --session）→修→三套+branch 全綠→commit，每輪 ~1h。收斂標準沿用：危險級 0＋單批真 bug<5 連兩輪。
- 2026-07-18 [r55 收官批審修] `_conv100_r55.txt` 103 句以 **ws_inspect --session 單連線**跑（新加 --session 模式；每句獨立連線會把跨句劇情全打斷＝假破口）。真破口：**危險級 2**——①「連帶第一名的庫存」查無後垃圾詞「第一名 庫存」被 `_update_ctx`（無驗證）存進 context，下一句「快過期的有哪些」被 `_resolve_followup` 污染成查不存在商品 → 回「✅ 沒有快到期」**假全綠**（修三層：_update_ctx 加 match_items 驗證／_resolve_followup 全域句不注入／warehouse.list_expiring_items 查無商品回 expiring_empty 明說）②盤點腳本 `_SCRIPT_CMD` 寫死 `test/` 前綴路徑 → RPI5 扁平佈局「找不到腳本檔」error（改從 _data_dir() 推導）。真 bug 修復：最緊急的是哪個→庫存警示、第三名剩多少（排行後追問+期間沿用）、週對週進出比較直答、補貨成本直答、露營全套多少錢→組合詞價格選單、椅子/燈勒短稱、北倉的設定、月底結算→盤點、上次盤點結果在哪→file_list、取消空回答+前端取消文案寫死「新增商品」、算了照原本的誤取消（照原本/照舊/維持 豁免 abort）、等等改15個→改卡引導、告別/道謝友善收尾（掰掰/謝謝辛苦了 曾回教學文）。**暫緩記錄**：黏寫「衛生紙濕紙巾尿布三個都查」只答首件（r40 黏寫不追既決）、慢跑鞋女款半答（商品屬性 schema 既決）、它還剩幾天直答（r44④既決）、誰最強/最弱倉 compare 追問、pending 卡數量修改直改（現走引導）、連帶第N名序數。順手修：gen_convo_sweep.py 字串斷行語法錯（一直是壞的）、branch_walk 序數鏈式 clarify 誤判。守衛 +13 句（989）、convo +6 景（2001）。
- 2026-07-18 [平台分歧觀察] `hot|給我看個厲害的|熱銷`（r48 句）本機跑 clarify「沒有厲害這個商品」×3 確定性，RPI5 r56 全綠有過——本機無歷史紀錄可比（該句入庫後全量只在 RPI5 跑過），依「RPI5 過=過」不追本機。

## 自主收斂進度（2026-07-17，原 r60 → **縮短至 r55**）
**曲線**：r43=14(2危)→r44=17(0危)→r45=5→r46=4→r47=4→r48=1（危險 0×6輪；r46+r47 連兩輪<5=收斂達標，續跑補數據）。commit 鏈：2d431d2(r43)→735b9e8(r44)→8638734(r45)→3c54528(r46)→947380e(r47+fast-mode)→r48 驗證中。守衛庫 926→943。fast-mode（?fast=1 sleep0，實證全文逐字同）讓 RPI5 三套 65→~40 分。r49 批已備（未來時態/情緒/日韓混/亂貨號/長輩腔/單位錯用）。r50+ 角度庫：混合復驗/多輪新形/寫入邊緣/數量極限/商品名訛變/複合三連/展場快打/空間方位/規格詢問/混沌雜燴/r60 終極抽樣。流程：巡檢→審→修→RPI5三套→commit，每輪~1h。觀察項：USB風扇 carry-over 到期句單次 flake；「不要衛生紙我要濕紙巾」本機/RPI5 平台不同答（RPI5 對）。
- 2026-07-17 [結論·不動] 本機測試慢的主因非算力（推理 88 vs 30 t/s 本機大勝）：①逐字串流動畫 8ms/字固定稅（兩平台同稅，抹平硬體差）②Windows asyncio/socket 每連線開銷（convo 1980 連線放大）。路徑裡的 OneDrive 只是資料夾名稱、同步已移除（user 證實），非因素。驗收已改 RPI5 單平台，本機慢不再影響週期。
- 2026-07-17 [✅暫定A（user 拍板）] STT 走陸系開源全離線：首選 **Fun-ASR-Nano-2512**（GGUF 單執行檔、CPU 邊緣、與 llama.cpp 同棧）備選 SenseVoice-small（sherpa-onnx）；單機 RPI5 直接展；熱詞表灌 60 商品名。下一步=WIN11 POC（收斂輪空檔做）：架起來→user 唸 20 句 vs 手機對比→過關→RPI5 整合（kiosk 按住說話鈕+塞輸入框）→麥克風採購 Fifine AM8。展位擺設：RPI5 透明架+「運算都在這」標籤、AM8 標「只是麥克風」、USB 線走明線；講解橋段=中途拔麥改打字證明大腦在 Pi。
- 2026-07-17 [原需求記錄：老闆要全本地語音、訪客不用手機，可加第二台 RPI5] 提案更新=**單台 RPI5 直接展**（RAM 合計 ~2.4/8GB；CPU 錯峰：push-to-talk 錄音 0 CPU→放開 STT ~1s→再進管線，STT/LLM 從不同時跑；體感全程 2-3.5s；33hr/44°C 實績 STT 短脈衝無虞）。第二台降級為加分項（多訪客並行/備援）。**模型來源議題（user 問非大陸替代）**：SenseVoice=阿里開源、全離線零外流（顧慮=觀感非資安）。「非大陸+手機級中文+RPI5單機」三條件無解、三選二：①品質→SenseVoice ②來源+單機→Whisper small/base（品質降級，initial_prompt 塞商品名+既有TYPO/fuzzy兜底）③來源+品質→聯發科 Breeze-ASR(台)+Jetson Orin Nano ~8k（破壞單機故事+同業敏感）。POC 建議 SenseVoice/whisper 雙架對比讓老闆選。**爬文補充（2026-07-17）**：無人蒸餾 Google/Apple（閉源拿不到 teacher）；distil-whisper 僅英文、large-v3-turbo 809M Pi5 太慢；非陸系（Cohere 2B/聯發科 Breeze large級）全跑不進 Pi5 單機。新發現 **Fun-ASR-Nano-2512**（阿里 2025-12，GGUF 單執行檔免 Python、CPU 邊緣專用、與 llama.cpp 同棧）——來源可接受時取代 SenseVoice 當首選。手機/平板路徑取消（無多餘裝置），連線功能純備用。**麥克風採購建議（2026-07-17）**：首選 Fifine AM8（動圈心型 USB-C+XLR ~NT$1,200-1,500，Linux 免驅動）；備選 Samson Q2U（~NT$2,500）/K668 預算版；避開全指向會議麥與便宜 USB 電容（吵場全收）。動圈+心型+push-to-talk+手持遞麥=展場抗噪組合。**引擎定調 SenseVoice-small**（阿里 2024，中文 CER 勝 whisper-large-v3=手機等級；非自回歸 int8 Pi5 上 4 秒語音 <1 秒出字；sherpa-onnx 跑）＋**熱詞表灌 60 商品名/倉名**→領域詞辨識反超手機（悶燒罐/濾掛/臂套 通用引擎會輸）。差距=吵雜環境（手機陣列麥+硬體降噪），解法=千元級指向性麥克風+push-to-talk。POC=WIN11 先架、user 唸 20 句 vs 手機對比、眼見為憑再採購。故事「一台聽一台想、全程端上」；辨識錯字被 TYPO/fuzzy 二次吸收=組合技。單機序列版當降級備案。工期 1-2 天+採購指向性麥克風；最大風險=展場噪音（push-to-talk+貼近講+辨識文字可修再送）。POC：先 WIN11 驗 sherpa-onnx 中文品質（半天）。待 user/老闆拍板再動工。
- 2026-07-17 [✅決策：RPI5 本地 STT 不做→被上列新需求取代] whisper tiny/base 中文品質不如手機內建（user 判斷正確），small 以上 RPI5 跑不動互動速度。語音體驗走訪客手機鍵盤語音。**展前待辦**：離線熱點下實測手機語音（Android 需預載離線中文包/iPhone 新機多可離線）＋櫃檯備一台裝好離線包的示範手機＋打字 fallback 話術。
- 2026-07-17 [優化·r50後] 全量瘦身三招（user 認可方向）：①**測試連線關打字機動畫**（WS ?test=1 → sleep 0，同 code path 照送 token frame；全鏈省 ~45 分 → 25-30 分）——r50 後獨立批+全量驗證 ②多輪等價類瘦身（商品名結構 4-6 類抽代表，1980→~500 保證明力）③守衛庫歸檔=**建議不做**（fast-mode 後全量僅~15分、歸檔省太少；歸檔風險=建庫初衷「刪了打壞無從發現」重演；smoke 已解快篩需求）——觸發點：破 2,000 句再議，屆時用機制覆蓋分析（還原歷史修復看哪些句亮）挑真冗餘，不憑感覺。①+②後全量 ≈25 分，子集糾結消失。
- 2026-07-17 [🔥流程教訓·r54] **畫面級審查缺口**：「有哪些商品」summary 說「以下為前10筆」但 view=inventory **前端根本沒渲染器**、表格從沒畫出來——ws_inspect 只看文字，user 實測畫面才抓到。修復=①補 inventory/alert_list/schedule_list 三個渲染器 ②新增 `check_views.py`（server view vs 前端渲染器覆蓋審計，白名單=文字自足型）**每輪必跑** ③逐句審原則升級：涉清單/表格的回答要意識到「summary 承諾的內容前端畫不畫得出來」。
**驗收流程（user 2026-07-17 凌晨再簡化）**：批次驗收改 **RPI5 三套全量單平台為準**（本機全量停跑——符合既有「單向驗收 RPI5 過=過」原則）；本機只留修 code 時的快速探針。RPI5 全綠即 commit。
**收斂標準（user 2026-07-17 凌晨定案）**：危險級 0（永不鬆）＋單批真 bug <5、連續兩輪＝收斂（絕對 0 不可收——對抗性生成必挖到東西，沿用 r17-r23 成功標準）。軌跡：r43=14（2危險邊緣）→ r44=17（**0 危險**，全答非所問輕級）→ r45 起看 <5。
- **r43 已結案**（commit 2d431d2）：全新100句挖出 14 真bug（2危險邊緣：config% 全店卡/歧義寫入）全修，守衛庫 882→898，雙平台六套 100%。教訓：C11e「無item config追問」誤傷11句守衛即撤——**倉別/全域 config 不指名商品=合法既有行為，確認卡即保險**；新 corpus 句的類別欄要對照 ACCEPT 表（any 不收 clarify）。
- 修復點：plq ≥3比較兩兩比+尾巴剝除、兩商品泛比較、C11d 歧義寫入選單、C11f %值追問、Layer2.5 token級不猜、fuzzy 中文接地、gate 拒絕前商品救援、排行第N+庫存、庫存最少→low_stock、注音×5、空手回顯門檻
- 暫緩記錄：複合兩步句（查A順便B 只回一件）、商品屬性 schema（幾入/容量/尺寸 半答）、一真一假並列（牛奶跟衛生紙 只答真者）、倉容量語（爆倉/位子 回概覽）——回答不醜級
- **r44 已審修**（驗證中）：103 句挖 17 真bug句/12 修復點，落地 8 點（C4-mv 進出量問句轉 movement＋三 hard-return 出口讓路、movement kw 接地、gate 進出貨救援、委婉句商品尾 2 字判準、tissue/towel TYPO、購物+觀念問題句黑名單）。**暫緩 4 點記錄**：①上週+排行 period（last_week 支援待查）②「這禮拜誰進貨進最兇」排行只有出貨向 ③兩倉單品「北倉南倉誰的衛生紙多」/類別比較「A類和B類哪類多」→ 比較家族擴充（下輪修）④日銷/撐幾天直答（daily_burn/days_left 資料在，需 forecast 回答格式）⑤露營相關夠不夠→單品 low_stock（歧義+low_stock 未進清單）。亂打/emoji/疑問否定/委婉組全過=r43 強化有效。
- **下一步 r44**：新角度出批（多輪壓縮單句/時間×商品×倉三維/量詞單位長尾/疑問否定/委婉假設/英文混句進階），RPI5 ws_inspect 逐句審，真bug<自標準：修到單批 0

## 交接筆記（2026-07-17 凌晨，全部結案、無未 commit 改動）
**本日四批全落地（皆雙平台六套全綠+push）**：
1. `e15d9ae` 並列查詢攔截 plq-gate（r40）
2. `31157c4` 不猜原則批（r41）：歧義短稱回清單、查無商品提醒可新增、測試工具 LLM-hit 側錄
3. `1d70c7f` **r42 clf 復活**：fasttext×numpy2 靜默死亡修復＋金絲雀自檢＋reload自癒＋/health clf 欄位；三份 *_llmsub.txt 子集入庫
4. `9e6d442`+`43c5feb` 簡報：小主角 FastText 專頁、六套/50+條措辭、42輪/882句數字、clf 事件講稿彩蛋
**新測試流程（r43 起生效）**：日常改動=本機全量＋RPI5 只跑 `*_llmsub.txt` 子集（守衛255/掃蕩58/多輪743，估30分內）；動 LLM 相關層（fuzzy/校正/rewrite/prompt/keyword抽取/clf）或展前=雙平台真全量。子集每次全量跑完自動重生。clf 認證：882句 dump diff 兩平台 標籤0差/信心0差/門檻0翻盤。
**RPI5 基建新增**：systemd StartLimitIntervalSec=0（防 crash-loop 放棄）＋ health_watchdog.sh cron 每分鐘（連5次無回應/failed→自動 restart）＋ clf 週期自檢自癒。
**待決策/待辦**：展前=demo 基準日對齊＋真機全量驗收；展後=intent_clf.bin 瘦身（bucket+quantize）、QEMU 假 RPI5（如需離線驗證環境）、換大 LLM 時評估拿掉 FastText。

## ⚠️ 舊交接筆記（2026-07-16，session 結束時未 commit 的狀態）
**（✅已結案 2026-07-16 晚：commit e15d9ae + push。雙平台守衛865+掃蕩953+多輪1980 三套全綠。以下留存歷史）**
**未 commit 改動**：test/server.py + test/regression_corpus.txt
**內容**：並列查詢攔截（第7個bug，照 user 定調「同時問兩種以上庫存→請分開問，比較題保留」）
- server.py：加 plq-gate（多商品並列查詢→clarify請分開問），比較詞加「比一下/比比看/誰比較」
- regression_corpus.txt：3句守衛斷言改期望「分開」（966運動毛巾跟登山水壺、1025防蚊液跟蚊香液、1255衛生紙跟濕紙巾跟尿布）
**已驗**：分隔符版驗過乾淨（跟/和有效、單商品長名不誤傷、比較題保留、比一下已修）。守衛曾864/865（那1個是舊斷言過時，已改）。
**未驗**：改斷言後的守衛完整重跑、全枚舉、雙平台。**commit 前必須跑完這些確認全綠**。
**已退回**：「掃2字短稱」處理黏寫（衛生紙濕紙巾尿布無分隔）誤傷嚴重（單商品被拆），已移除。黏寫罕見句不追。
**下一步**：重開 session → 重啟server → 守衛→全枚舉→雙平台 → 全綠才 commit 這批。
**待user決策（backlog 上方有詳記）**：①歧義短稱「咖啡/運動/露營」直接猜vs回清單 ②無量詞進出貨（已決定不修）③白拿查詢化（判斷不修）
