# 英文 100 句 · 錄音對照表

**唸的時候不用模仿標準腔**——展場訪客本來就不是母語者，你的腔就是要測的真實情境。
示範音檔只是讓你知道**唸什麼字**，不是要你模仿口音。

| 資源 | 位置 |
|---|---|
| 示範音檔（單句） | `audio/read100_en_demo/001.mp3` ~ `100.mp3` |
| 整段跟讀 | `audio/read100_en_demo/_all.mp3` |
| 本機練習 | `python practice_en.py`（Enter 播放、r 重播、s 跳過） |
| 錄音前體檢 | `bash check_mic_en.sh`（RPI5） |
| 正式錄音 | `bash read100_en.sh`（RPI5） |
| 事後混噪重測 | `bash noise_retest_en.sh light\|heavy`（不用重念） |

> 示範音檔是 edge-tts（微軟免費 TTS）合成的，**只是範讀參考**，不是評測數據。
> 題目已用文字乾跑驗證 **100/100 全過**——你錄的時候若 FAIL，是語音鏈的問題，不是題目寫錯。

| # | 唸這句 | 中文意思 | 期望結果 | 說明 |
|---|---|---|---|---|
| | **A. 基本庫存查詢（20 句，最高頻）──** | | | |
| 1 | `bluetooth earphones stock` | 藍牙耳機庫存 | inventory_single | 查單一商品庫存，回答要出現「Earphones」 |
| 2 | `how many electric toothbrushes are left` | 電動牙刷還剩幾支 | inventory_single | 查單一商品庫存，回答要出現「Toothbrush」 |
| 3 | `wireless mouse count` | 無線滑鼠數量 | inventory_single | 查單一商品庫存，回答要出現「Mouse」 |
| 4 | `power bank inventory` | 行動電源庫存 | inventory_single | 查單一商品庫存，回答要出現「Power Bank」 |
| 5 | `whats the stock of yoga mat` | 瑜珈墊庫存多少 | inventory_single | 查單一商品庫存，回答要出現「Yoga Mat」 |
| 6 | `do we have camping tent` | 有露營帳篷嗎 | inventory_single | 查單一商品庫存，回答要出現「Tent」 |
| 7 | `how many sports towels do we have` | 運動毛巾有幾條 | inventory_single | 查單一商品庫存，回答要出現「Towel」 |
| 8 | `check the coffee machine stock` | 查咖啡機庫存 | inventory_single | 查單一商品庫存，回答要出現「Coffee Machine」 |
| 9 | `laptop bag inventory` | 筆電包庫存 | inventory_single | 查單一商品庫存，回答要出現「Laptop Bag」 |
| 10 | `how much sparkling water is left` | 氣泡水還剩多少 | inventory_single | 查單一商品庫存，回答要出現「Sparkling Water」 |
| 11 | `mechanical keyboard stock` | 機械鍵盤庫存 | inventory_single | 查單一商品庫存，回答要出現「Keyboard」 |
| 12 | `bluetooth speaker count` | 藍牙喇叭數量 | inventory_single | 查單一商品庫存，回答要出現「Speaker」 |
| 13 | `how many trash bags left` | 垃圾袋還剩幾包 | inventory_single | 查單一商品庫存，回答要出現「Trash Bags」 |
| 14 | `facial tissue stock` | 面紙庫存 | inventory_single | 查單一商品庫存，回答要出現「Tissue」 |
| 15 | `show me the electric mop inventory` | 看電動拖把庫存 | inventory_single | 查單一商品庫存，回答要出現「Mop」 |
| 16 | `steam iron stock in north` | 北倉蒸氣熨斗庫存 | inventory_single | 查單一商品庫存，回答要出現「Iron」 |
| 17 | `whats in central warehouse for wireless mouse` | 中倉的無線滑鼠有多少 | inventory_single | 查單一商品庫存，回答要出現「Mouse」 |
| 18 | `sun hat stock` | 遮陽帽庫存 | inventory_single | 查單一商品庫存，回答要出現「Sun Hat」 |
| 19 | `hiking water bottle count` | 登山水壺數量 | inventory_single | 查單一商品庫存，回答要出現「Bottle」 |
| 20 | `how many down jackets do we have` | 羽絨外套有幾件 | inventory_single | 查單一商品庫存，回答要出現「Jacket」 |
| | **B. 進出貨寫入（20 句，最重要——會寫資料）──** | | | |
| 21 | `north received 50 wireless mouse` | 北倉進了 50 個無線滑鼠 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Mouse」 |
| 22 | `central shipped 20 bluetooth earphones` | 中倉出了 20 個藍牙耳機 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Earphones」 |
| 23 | `south got 100 sparkling water` | 南倉收到 100 瓶氣泡水 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Sparkling Water」 |
| 24 | `add 30 yoga mats to north` | 北倉加 30 個瑜珈墊 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Yoga Mat」 |
| 25 | `ship out 15 power banks from central` | 中倉出貨 15 個行動電源 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Power Bank」 |
| 26 | `north warehouse received 40 sports towels` | 北倉收到 40 條運動毛巾 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Towel」 |
| 27 | `take 25 camping tents out of south` | 從南倉出 25 頂露營帳篷 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Tent」 |
| 28 | `central received 60 facial tissue` | 中倉收到 60 包面紙 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Tissue」 |
| 29 | `transfer 20 wireless mouse from north to south` | 20 個無線滑鼠從北倉調到南倉 | transfer_confirm | 開調撥確認卡，回答要出現「Mouse」 |
| 30 | `move 10 coffee machines from central to north` | 10 台咖啡機從中倉調到北倉 | transfer_confirm | 開調撥確認卡，回答要出現「Coffee Machine」 |
| 31 | `send 30 yoga mats from south to central` | 30 個瑜珈墊從南倉送到中倉 | transfer_confirm | 開調撥確認卡，回答要出現「Yoga Mat」 |
| 32 | `north got a delivery of 80 trash bags` | 北倉到貨 80 包垃圾袋 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Trash Bags」 |
| 33 | `we shipped 12 laptop bags from north` | 我們從北倉出了 12 個筆電包 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Laptop Bag」 |
| 34 | `south received 45 sun hats` | 南倉收到 45 頂遮陽帽 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Sun Hat」 |
| 35 | `remove 18 steam irons from central` | 中倉扣掉 18 台蒸氣熨斗 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Iron」 |
| 36 | `add 200 hiking water bottles to north` | 北倉加 200 個登山水壺 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Bottle」 |
| 37 | `central sent out 35 mechanical keyboards` | 中倉出了 35 個機械鍵盤 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Keyboard」 |
| 38 | `north received 55 bluetooth speakers` | 北倉收到 55 個藍牙喇叭 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Speaker」 |
| 39 | `transfer 15 electric mops from north to central` | 15 台電動拖把從北倉調到中倉 | transfer_confirm | 開調撥確認卡，回答要出現「Mop」 |
| 40 | `south shipped 22 down jackets` | 南倉出貨 22 件羽絨外套 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Jacket」 |
| | **C. 缺貨與警示（10 句）──** | | | |
| 41 | `what is running low` | 哪些快沒了 | low_stock | 缺貨清單 |
| 42 | `show me the low stock list` | 給我缺貨清單 | low_stock | 缺貨清單 |
| 43 | `which items need restocking` | 哪些商品需要補貨 | low_stock | 缺貨清單 |
| 44 | `anything below safety stock` | 有低於安全庫存的嗎 | low_stock | 缺貨清單 |
| 45 | `whats about to run out` | 什麼快要用完了 | low_stock | 缺貨清單 |
| 46 | `alert me when earphones drop below 30` | 耳機低於 30 個時通知我 | alert_confirm | 開警示設定卡，回答要出現「Earphones」 |
| 47 | `set an alert for yoga mat` | 幫瑜珈墊設個警示 | alert_confirm | 開警示設定卡，回答要出現「Yoga Mat」 |
| 48 | `what alerts do i have` | 我有哪些警示 | alert_list | 列出警示規則 |
| 49 | `show my alert rules` | 顯示我的警示規則 | alert_list | 列出警示規則 |
| 50 | `which products are short` | 哪些商品短缺 | low_stock | 缺貨清單 |
| | **D. 排行與比較（15 句）──** | | | |
| 51 | `best sellers this week` | 本週熱銷 | hot_items | 熱銷/滯銷榜 |
| 52 | `what sold the most this month` | 這個月賣最多的是什麼 | hot_items | 熱銷/滯銷榜 |
| 53 | `show me the top sellers` | 給我熱銷排行 | hot_items | 熱銷/滯銷榜 |
| 54 | `which items are slow movers` | 哪些商品滯銷 | hot_items | 熱銷/滯銷榜 |
| 55 | `compare north and south` | 比較北倉和南倉 | compare_warehouses | 倉庫比較 |
| 56 | `compare central and south by turnover` | 用週轉率比較中倉和南倉 | compare_warehouses | 倉庫比較 |
| 57 | `which warehouse has more stock north or south` | 北倉和南倉哪個庫存多 | compare_warehouses | 倉庫比較 |
| 58 | `compare all three warehouses` | 比較三個倉庫 | compare_warehouses | 倉庫比較 |
| 59 | `what came in today` | 今天進了什麼 | movement | 進出貨紀錄 |
| 60 | `show me todays inbound` | 顯示今天的進貨 | movement | 進出貨紀錄 |
| 61 | `what went out this week` | 這週出了什麼 | movement | 進出貨紀錄 |
| 62 | `movements for wireless mouse this month` | 無線滑鼠這個月的進出 | movement | 進出貨紀錄 |
| 63 | `which items expire soon` | 哪些商品快到期 | expiring | 到期批次 |
| 64 | `show me expiring batches` | 顯示到期批次 | expiring | 到期批次 |
| 65 | `whats expiring in the next 30 days` | 未來 30 天內到期的 | expiring | 到期批次 |
| | **E. 帳務追查 RCA（10 句）──** | | | |
| 66 | `why is the toothbrush count off` | 為什麼牙刷數量對不上 | agent_rca | 帳務追查 |
| 67 | `who moved the wireless mouse` | 誰動了無線滑鼠 | agent_rca | 帳務追查 |
| 68 | `the earphone numbers dont match` | 耳機的數字對不起來 | agent_rca | 帳務追查 |
| 69 | `explain the yoga mat shortfall` | 說明瑜珈墊的短缺 | agent_rca | 帳務追查 |
| 70 | `check the coffee machine discrepancy` | 查咖啡機的差異 | agent_rca | 帳務追查 |
| 71 | `generate a full inventory report` | 產一份完整庫存報表 | report_done | 產報表 |
| 72 | `create a purchase order for low stock items` | 幫缺貨商品開採購單 | po_confirm | 採購授權卡 |
| 73 | `run the month end stocktake` | 執行月底盤點 | script_confirm | 腳本授權卡 |
| 74 | `what files do you have` | 你有哪些檔案 | file_list | 檔案/腳本清單 |
| 75 | `what scripts can you run` | 你能跑哪些腳本 | file_list | 檔案/腳本清單 |
| | **F. 多輪追問（10 句，要連續唸才有意義）──** | | | |
| 76 | `bluetooth earphones stock` | 藍牙耳機庫存 | inventory_single | 查單一商品庫存，回答要出現「Earphones」 |
| 77 | `what about north` | 那北倉呢 | inventory_single | 查單一商品庫存，回答要出現「Earphones」 |
| 78 | `how about central` | 中倉呢 | inventory_single | 查單一商品庫存，回答要出現「Earphones」 |
| 79 | `is it below safety stock` | 它低於安全庫存嗎 | * | 不限，只要不出錯 |
| 80 | `show me its movements` | 顯示它的進出紀錄 | movement | 進出貨紀錄 |
| 81 | `wireless mouse stock` | 無線滑鼠庫存 | inventory_single | 查單一商品庫存，回答要出現「Mouse」 |
| 82 | `and south` | 那南倉呢 | * | 不限，只要不出錯，回答要出現「Mouse」 |
| 83 | `set its safety stock to 100` | 把它的安全庫存設成 100 | config_confirm | 設定確認卡，回答要出現「Mouse」 |
| 84 | `cancel` | 取消 | * | 不限，只要不出錯 |
| 85 | `what else do coffee beans buyers get` | 買咖啡豆的人還會買什麼 | related | 搭售推薦 |
| | **G. 澄清與口語確認（8 句）──** | | | |
| 86 | `coffee stock` | 咖啡庫存 | clarify | 反問你要哪一個 |
| 87 | `the second one` | 第二個 | * | 不限，只要不出錯 |
| 88 | `mosquito repellent stock` | 防蚊液庫存 | clarify | 反問你要哪一個 |
| 89 | `the first one` | 第一個 | * | 不限，只要不出錯 |
| 90 | `north received 50 wireless mouse` | 北倉進了 50 個無線滑鼠 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Mouse」 |
| 91 | `confirm` | 確認 | movement_done | 寫入完成，回答要出現「Mouse」 |
| 92 | `central shipped 20 yoga mats` | 中倉出了 20 個瑜珈墊 | movement_confirm | 開進出貨確認卡（會寫資料），回答要出現「Yoga Mat」 |
| 93 | `never mind` | 算了不用 | * | 不限，只要不出錯 |
| | **H. 招呼閒聊與邊界（7 句）──** | | | |
| 94 | `hello` | 哈囉 | guide | 導覽/招呼 |
| 95 | `what can you do` | 你會做什麼 | guide | 導覽/招呼 |
| 96 | `help` | 求助 | guide | 導覽/招呼 |
| 97 | `do you have hair dryers` | 你們有吹風機嗎 | * | 不限，只要不出錯 |
| 98 | `whats the wifi password` | wifi 密碼是什麼 | * | 不限，只要不出錯 |
| 99 | `tell me a joke` | 講個笑話 | rejected | 婉拒（搗蛋句） |
| 100 | `thanks bye` | 謝謝掰掰 | guide | 導覽/招呼 |

## F/G 段要**連續唸**

76-85 和 86-93 是多輪對話，靠上一句的 context。中途離開再回來會失效，
要從該段開頭重錄：`bash read100_en.sh 76` / `bash read100_en.sh 86`。

## 站位

展場訪客大概站 50-70cm，錄的時候保持同樣距離。太近會爆音、太遠訊噪比掉。
腳本每句都會顯示音量 dB，偏小會提醒你。