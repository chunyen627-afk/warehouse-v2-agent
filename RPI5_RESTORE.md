# RPI5 倉管系統 —— 完整還原手冊

> **用途**：RPI5 重灌 / 換機 / 系統壞掉時，照這份從零還原到展場可用狀態。
> **寫給**：完全沒有前後文的新對話（或未來的自己）。每一步都寫清楚「為什麼」，
> 因為多數步驟是踩過雷才長成現在這樣，照抄不理解會再踩一次。
>
> 最後驗證：**2026-07-27**（實機重開機驗證通過：服務自啟、kiosk 自動開、
> 語音可用、桌面捷徑正常）

---

## 0. 先確認你要還原的是什麼

| 項目 | 值 |
|---|---|
| 硬體 | Raspberry Pi 5（4 核 / 8GB），使用者 `p400` |
| OS | Raspberry Pi OS (Debian 12 bookworm)，桌面 **wayfire**（Wayland） |
| 中文版 | `~/warehouse_v2/` → HTTPS **8001**（凍結不改，開機**不**自啟） |
| 英文版 | `~/warehouse_v2_en/` → HTTPS **8002**（活躍，開機**自啟**，展場主力） |
| 遠端連線 | ZeroTier 固定 IP **10.35.219.22** |
| SSH | `ssh -i ~/.ssh/rpi5_warehouse p400@10.35.219.22` |

**程式碼來源**：GitHub `chunyen627-afk/warehouse-v2-agent`（private）
- repo 根 = `warehouse_v2/`，其中 `test/` = 中文版、`en/` = 英文版
- ⚠️ **模型檔不在 git**（`.gitignore` 排除 `*.gguf` / `*.bin`），必須另外復原，見 §4

---

## 1. 系統套件

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv git ffmpeg chromium-browser \
                    grim wtype build-essential cmake
```

| 套件 | 為什麼需要 |
|---|---|
| `ffmpeg` | 語音端點把瀏覽器送來的 webm 轉 16k mono wav |
| `chromium-browser` | kiosk 展示瀏覽器（**不要換**，launch 腳本的旗標是為它調的） |
| `grim` | Wayland 截圖（維護時遠端看畫面用；X11 的 scrot 在這台**沒用**） |
| `cmake` / `build-essential` | 編譯 whisper.cpp（§4.3） |

---

## 2. Python 套件

⚠️ **不要用 venv**——systemd 服務直接用 `/usr/bin/python3`，套件要裝在系統層。

```bash
pip3 install --break-system-packages \
    fastapi==0.92.0 uvicorn==0.17.6 websockets \
    llama-cpp-python fasttext qrcode pillow jieba
```

實測可用版本（2026-07-27）：

| 套件 | 版本 | 備註 |
|---|---|---|
| fastapi | 0.92.0 | |
| uvicorn | 0.17.6 | |
| websockets | 16.0 | |
| llama_cpp | 0.3.20 | **編譯很久（30+ 分鐘）**，耐心等 |
| fasttext | — | 意圖分類器 `intent_clf.bin` 要用 |
| numpy | 2.4.4 | ⚠️ 見下方警告 |
| Pillow | 9.4.0 | QR 圖產生 |
| jieba | 0.42.1 | 中文版斷詞（英文版用不到，但中文版要） |
| qrcode | — | |

### 🚨 numpy 2.x × fasttext 的已知地雷
`fasttext` 在 numpy 2.x 下**可能靜默壞掉**（不報錯、但分類器失效，
系統照跑只是路由變差）。還原後**一定要驗**：

```bash
cd ~/warehouse_v2_en && python3 -c "
import fasttext
m = fasttext.load_model('intent_clf.bin')
print(m.predict('bluetooth earphones stock'))   # 應回 query_inventory 且 conf 高
"
```
沒回正常結果就是踩到了 → 降 numpy 或重編 fasttext。

---

## 3. 取得程式碼

```bash
cd ~
git clone https://github.com/chunyen627-afk/warehouse-v2-agent.git _repo
# repo 結構：根目錄就是 warehouse_v2 的內容
mkdir -p ~/warehouse_v2_en ~/warehouse_v2
cp -r ~/_repo/en/*   ~/warehouse_v2_en/
cp -r ~/_repo/test/* ~/warehouse_v2/
```

⚠️ **RPI5 上是「扁平佈局」**：`~/warehouse_v2_en/` 底下直接是
`server.py` / `warehouse.py` / `tools_v2.py` / `templates/`…
（不是 repo 那種 `en/` 巢狀）。搞錯會 import 失敗。

---

## 4. 模型檔（**不在 git，最容易漏**）

| 檔案 | 大小 | 路徑 | 說明 |
|---|---|---|---|
| `en_q8_0.gguf` | 291MB | `~/warehouse_v2_en/models/` | **英文微調版 LLM**，md5 `213593b50f2e5ffe597750aa171034da` |
| `intent_clf.bin` | 6MB | `~/warehouse_v2_en/` | 英文 FastText 意圖分類器（量化版，800MB→6MB 準確率不掉） |
| `ggml-tiny.en.bin` | 74MB | `~/whisper.cpp/models/` | **英文**語音（選 tiny 不是 base，見 §4.3） |
| `ggml-base.bin` | 142MB | `~/whisper.cpp/models/` | **中文**語音（multilingual，見 §4.4） |
| 中文版 gguf | 291MB | `~/warehouse_v2/models/` | md5 `d74848c133b92029f7b88dde0ac6da87`（與英文版**不同**） |
| 中文 intent_clf | **4.2MB** | `~/warehouse_v2/` | 量化版（512MB→4.2MB，1122 句預測 100% 一致） |

> 🚩 **語音模型硬約束（user 定調）：只用歐美模型，不用大陸模型。**
> 原本的 Fun-ASR-Nano / SenseVoice（阿里 FunAudioLLM）**已於 2026-07-27
> 全部刪除**（模型 910MB + runtime + 原始碼，全機掃描零殘留）。
> 還原時**不要**再裝回去——中英文語音都改用 whisper.cpp。

### 4.1 從哪裡拿
- **WIN 備份**：`FunctionGemma_Finetune/warehouse_v2/en/models/en_q8_0.gguf`
- ⚠️ **不要用 ZeroTier 傳 gguf**：隧道頻寬低，291MB 傳到一半就斷
  （踩過，傳成 55M/120M 殘檔、md5 不符）。**用 USB 隨身碟或同網段 scp**。
- 傳完**一定要對 md5**，殘檔會讓模型載入失敗但錯誤訊息看不出原因。

### 4.2 確認英文版跑的是英文模型
```bash
md5sum ~/warehouse_v2_en/models/en_q8_0.gguf
# 213593b5… = 英文微調版 ✅
# d74848c1… = 中文版（放錯了，英文能力會差很多）
```
> 這點曾經誤判過：以為 8002 跑的是中文模型，其實 7/25 就換成英文版了。
> **談模型版本一律 md5 對照，別靠檔名或記憶。**

### 4.3 whisper.cpp（中英文語音共用同一個 build）
```bash
cd ~ && git clone https://github.com/ggerganov/whisper.cpp
cd whisper.cpp && cmake -B build && cmake --build build -j2   # 約 5 分鐘
bash ./models/download-ggml-model.sh tiny.en    # 英文用
bash ./models/download-ggml-model.sh base       # 中文用（multilingual）
```
> ⚠️ `tiny.en` / `base.en` 是**英文專用**版（同尺寸下英文更準，容量全給英文）；
> 中文沒有專用版，只能用 multilingual 的 `tiny`/`base`/`small`。
> 這就是中英用不同模型的原因——不是沒統一，是**英文有專用版可用、中文沒有**。

**英文 → `tiny.en`**（⚠️ 不要 base.en，RPI5 實測）：

| 模型 | 延遲 | WER 乾淨 |
|---|---|---|
| **tiny.en (74MB)** | **0.94s** | **9.3%** |
| base.en (148MB) | 2.33s | 10.2% |

倉管查詢句短、句型固定，tiny 容量已夠，變大沒收益反而慢 2.5 倍。

### 4.4 中文為什麼是 `base`（不是 tiny 也不是 small）
拿 user 錄的**真人**音檔實測（⚠️ 不要用合成音判斷——中文版經驗是合成音
clean 100%、真人首測只有 35/52，會嚴重高估）：

| 模型 | 端到端（查詢/寫入） | 延遲 | |
|---|---|---|---|
| Fun-ASR（阿里，已刪） | 78%（歷史） | 2.8s | 來源不合規 |
| whisper tiny | — | 0.97s | ❌ 中文太差（無線滑鼠→「古仙华属」） |
| whisper small | 16/20、10/11 | **6.8s** | 準但慢 3 倍 |
| **whisper base ＋ ASR 規則** | **17/20、11/12** | **2.3s** | ✅ **又快又準** |

**base 的 encode 只要 2.1s、small 要 5.7s**（時間幾乎全花在 encoder，
decode 只佔 0.1s）。base 字面錯誤較多，但補了同音規則後端到端**反而最高**。
⚠️ **選型不能只看 WER，要看端到端答對率**——這是這個專案第三次驗證。
⇒ 最終比原本的阿里模型**更準（85% vs 78%）也更快（2.3s vs 2.8s）**。
⚠️ **現行部署是 base（2.3s 這行），不是 small（6.8s 那行）**——
2026-07-27 選型當天別處誤把 small 的延遲記成 base 的，該處已於
2026-07-29 用 20 句真人音重測更正為 2.15s（見 `test/server.py`
_VOICE_MODEL 上方註解）。這裡的表格本身沒錯，只是容易被摘錯行。

`-t` 執行緒調校**無效**：whisper 預設就是 4 執行緒（= RPI5 全部核心）。

### 4.5 intent_clf 量化瘦身（中英都做了）
FastText 分類器可以量化到 **1/100 大小而準確率不掉**：

| | 原始 | 量化後 | 驗證 |
|---|---|---|---|
| 英文 | 800MB | **6MB** | 99.68%（一模一樣） |
| 中文 | 512MB | **4.2MB** | 1122 句預測 **100% 一致** |

```python
m = fasttext.load_model("intent_clf.bin")
m.quantize(qnorm=True, retrain=False, cutoff=100000)
m.save_model("out.ftz")     # 直接改名回 intent_clf.bin 即可
```
> fasttext 會自動辨識格式，**推理端程式碼一行都不用改**。
> 腳本：`en/rpi5/quant_clf.py`（會先比對量化前後預測，一致率 <99% 就不覆蓋）

🚨 **量化腳本裡不能用 `model.predict()`**——fasttext ≤0.9.3 的 predict()
末行是 `np.array(probs, copy=False)`，在 **numpy ≥2 直接 ValueError**。
要走底層 `model.f.predict(text + "\n", 1, 0.0, "strict")`。
（推理端 `intent_clf.py` 早有同款 workaround，見 §2 的 numpy 警告。）

⚠️ **測 `intent_clf.predict()` 前要先呼叫 `intent_clf.load()`**——
沒載入時 `_MODEL is None` 會直接回 `("unknown", 1.0)`，
**看起來就像分類器死掉**（我因此誤判過一次）。

### 4.6 🚨 換 ASR 模型 ≠ 沿用舊的同音規則
`server.py` 的 `_ASR_FIX` 是**為特定 ASR 的錯誤模式**磨出來的。
換模型後舊規則可能一條都接不到——實測：

| 舊規則（為 Fun-ASR 調） | whisper 的錯法 |
|---|---|
| 昌/蒼/槍/藏 → 倉 | 錯成「南**參**」「**被倉**」，接不到 |
| 近+數量 → 進 | 沒出現這種錯 |
| — | 「入庫」→「**陸庫**」（新的） |

補了三條後寫入句 8/12 → **10/11**。
⚠️ **加任何 ASR 規則前先撞守衛語料 + 商品主檔，零命中才敢加。**

---

## 5. HTTPS 憑證（**必須帶 SAN，否則語音和 WS 會壞**）

```bash
cd ~/warehouse_v2_en && bash regen_cert_san.sh    # repo 內附
```

🚨 **為什麼一定要 SAN**：Chrome 58+ **只認 SAN 不認 CN**。憑證沒 SAN 時：
- 首頁點「繼續前往」**能開**（看起來正常）
- 但 **wss 被靜默拒絕** → 畫面卡在 Loading、訪客送不出任何字
- server log **完全沒有連線紀錄**（最難查的一種壞法）

中文版長期沒踩到，是因為 kiosk 走 `localhost`（有豁免）；**手機掃 QR 進來就會踩**。

另外，**麥克風權限只有 https / localhost 才給** → 語音也依賴這個。

---

## 6. systemd 服務

### 6.1 英文版（開機自啟）
`/etc/systemd/system/warehouse-v2-en.service`：
```ini
[Unit]
Description=Warehouse V2 AI Agent EN (HTTPS 8002)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=p400
WorkingDirectory=/home/p400/warehouse_v2_en
ExecStart=/usr/bin/python3 /home/p400/warehouse_v2_en/server_https.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PORT=8002
Environment=N_THREADS=4
Environment=N_CTX=2048
MemoryMax=3G
ExecStartPre=/bin/sleep 5

[Install]
WantedBy=multi-user.target
```
> `MemoryMax=3G` 是為了中英兩版並存時不互相排擠（8GB 機器，實測兩版
> 同時跑用 2.9G，健康）。`ExecStartPre=sleep 5` 等網路就緒。

`server_https.py` 內容（**路徑寫死，中英各一份不能共用**）：
```python
import os, uvicorn
os.chdir('/home/p400/warehouse_v2_en')
os.environ['PORT'] = '8002'
import server
uvicorn.run(server.app, host='0.0.0.0', port=8002,
            ssl_keyfile='key.pem', ssl_certfile='cert.pem')
```

### 6.2 啟用
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now warehouse-v2-en    # 英文版
sudo systemctl enable --now warehouse-v2       # 中文版（2026-07-27 起也自啟）
```

**user 定調（2026-07-27）：兩版都開機預載**——模型先載好，桌面切換語言時
不用等模型載入。實測兩版並存記憶體 **2.6G / 7.9G**，很寬裕。
（原本中文版是 disabled、要用時才手動起，切換要等 20+ 秒載模型。）

桌面捷徑 ①② 都呼叫同一支 `~/switch_lang.sh en|ch`（旗標只有一份，
避免兩份不同步——原本中文版那支還停在 150% 縮放、缺三個防彈窗旗標）。

---

## 7. Kiosk 自動開瀏覽器

`~/.config/autostart/warehouse.desktop`：
```ini
[Desktop Entry]
Name=Warehouse
Exec=/home/p400/launch_warehouse.sh
Type=Application
```

`~/launch_warehouse.sh` —— **repo 有這份，直接複製**（另需 `fix_chromium_exit.py`）。

### 🖥️ 開機行為（user 定調 2026-07-27）
**同時開中英兩個分頁，英文在前景**：

| 層 | 中文 8001 | 英文 8002 |
|---|---|---|
| systemd 服務（載模型） | ✅ 預載 | ✅ 預載 |
| 瀏覽器分頁 | 開在第 2 頁備著 | **第 1 頁、前景** |

訪客要切語言 → **點分頁**即可，不用等服務啟動或模型載入。

⚠️ **網址順序 = 分頁順序，Chromium 的第一個網址獲得焦點**
（實測過：先寫中文的話開機前景就是中文版）→ 英文必須寫前面。

**為什麼敢雙開**——壓測數據（`en/rpi5/stress_both.py`）：

| 情境 | 最慢回應 |
|---|---|
| 中英各 1 人同時 | 0.28s |
| 中英各 2 人同時 | 0.48s |
| **中文跑語音（吃滿 4 核）+ 英文查詢** | **0.42s** |
| 中英各 4 人（8 請求同秒） | 8.2s（全成功，展場不現實） |

記憶體 2.15G/7.9G、load 0.45 幾乎閒置。模型閒置時不吃 CPU，
查詢主力是 FastText（毫秒級）不是每句跑 LLM ⇒ 雙開對真實負載無感。

### 腳本做的三件事
1. **啟動前清 Chromium 崩潰標記**（呼叫 `~/fix_chromium_exit.py`）
   否則開機跳中文彈窗「Chromium 未正確關閉。還原」蓋住畫面
   （斷電 / reboot / pkill 都會留下 `exit_type=Crashed`，命令列旗標關不掉）
   ⚠️ **獨立成檔不要內嵌 heredoc**——巢狀 heredoc 的終止符會被外層吃掉，
   整支 shell 腳本語法錯（踩過，機器帶著壞腳本重開機）
2. **等兩個 server 都就緒才開瀏覽器**（各輪詢到回 200，最多 240 秒）
3. **一串旗標**，每個都是為了展場不跳中文對話框：

| 旗標 | 解決什麼 |
|---|---|
| `--test-type` | 消除黃色警告列「你正在使用不受支援的命令列標幟」 |
| `--lang=en-US` + `--disable-features=Translate,TranslateUI` | Google 翻譯彈窗（英文／中文繁體） |
| `--no-first-run` `--no-default-browser-check` `--disable-infobars` | 首次執行精靈、預設瀏覽器詢問 |
| `--disable-session-crashed-bubble` `--hide-crash-restore-bubble` | 崩潰復原氣泡 |
| `--ignore-certificate-errors` | 自簽憑證 |
| **`--force-device-scale-factor=1.25`** | **15 吋 1920×1080 的最佳縮放**，見下 |
| `--remote-debugging-port=9222` | 維護用（只綁 127.0.0.1），見 §11 |

### ⚠️ 離線 demo 與 Chromium 背景連線（**已知限制，不要再試**）
實測發現 kiosk 會常駐連著 Google 推播（GCM `:5228`）與 Google 服務（`:443`）。
倉管頁面完全用不到，純粹是 Chromium 預設行為。

**兩種方法都試過、都失敗**（2026-07-27，別再重複踩）：

| 方法 | 結果 |
|---|---|
| 命令列旗標（`--disable-background-networking` 等 8 個） | 連線數不減反增，**無效** |
| 企業政策 JSON（`/etc/chromium/`、`/etc/chromium-browser/`、`/etc/opt/chrome/` 三個路徑都放） | `chrome://policy` 顯示「**尚未設定任何值**」，完全沒讀取 |

這版 Chromium（Debian 147.0，Raspberry Pi OS 打包）不吃標準政策路徑。

**結論與現況**：
- 旗標**保留**（無副作用，確實減少部分背景行為），政策檔**已移除**
- **影響很小**：實測 NetworkManager + wpa_supplicant 才 0% CPU / 32MB、
  開機至今 8MB 流量
- **展場實際情境下不成問題**：開熱點給訪客掃 QR 時 wlan0 被佔用、
  **本來就沒有外網**，那些連線自然斷掉
- 真的很在意的話，最可靠的做法是**展場當天不連 WiFi**（一個動作），
  比跟 Chromium 纏鬥實在

### 縮放為什麼是 125%
15 吋 1920×1080（約 147 PPI），訪客站距 50-70cm：

| 縮放 | CSS 可視高 | 三條頂部佔比 | 快捷列可見按鈕 |
|---|---|---|---|
| 150%（舊值） | 720px | 10.4% | 6 顆 |
| **125%（現行）** | **864px** | **8.6%** | **11 顆** |
| 100% | 1080px | 6.9% | 更多但字偏小 |

125% 下寫入確認卡（4 行表格 + 2 顆按鈕）**一屏放得下不用捲**，150% 會被截斷。

---

## 8. 前端精簡（`body.compact-top`）

`en/templates/index.html` 有一段 `body.compact-top` CSS，把標題列 / 快捷列 /
異常警示條三條收矮，把垂直空間讓給對話區。

⚠️ **兩個踩過的坑**：
1. **這段必須放在所有基礎樣式之後**——同權重 CSS 後者勝，放前面會被
   `#chips-bar .chip` 之類覆蓋（改了沒效果）。
2. **標題列右側三顆按鈕要用 id 選擇器覆蓋**。它們各自用 id 寫死
   `width/height`，`body.compact-top header button` 的 class 權重蓋不掉。
   實測（CDP 量測，非目測）：header padding 只有 2+2、文字都 ≤15px，
   但 `#close-btn` 36px 把整條撐高 → 現已收成 20/18/16px。

---

## 9. 網路設定

### 9.1 WiFi / 熱點（user 定調）
```bash
# 開機預設連 WiFi，熱點手動開
sudo nmcli connection modify rpi5-hotspot connection.autoconnect no
sudo nmcli connection modify rpi5-hotspot connection.autoconnect-priority 0
```
🚨 **重灌後一定要檢查這項**：熱點若 `autoconnect=yes` 且優先權高（曾經是 100），
開機會自動開熱點 → wlan0 被佔用連不上 WiFi → 無外網 → ZeroTier watchdog
依設計停掉 ZeroTier → **完全連不進去**（只能接螢幕鍵盤救）。

熱點：SSID `RPI5-Demo` / 密碼 `demo1234` / AP IP `192.168.4.1`

### 9.2 ZeroTier（遠端固定 IP）
```bash
curl -s https://install.zerotier.com | sudo bash
sudo zerotier-cli join 154a350c864d8bdd     # network id
# → 到 ZeroTier 後台授權這台，才會拿到 10.35.219.22
```
- watchdog `~/zt_watchdog_loop.sh` + `zt-watchdog.service`（enable）
  **按外網狀態啟停 ZeroTier**：AP 模式（無外網）→ 停掉零空轉；
  有外網 → 自動上線。
- ⚠️ **watchdog 要有 grace period**：連續 3 次（60 秒）都 offline 才 restart。
  太積極會反覆打斷正在進行的重連，越 restart 越慢（v2 踩過）。

---

## 10. 桌面（展場現場操作用）

三個捷徑 + 兩張 QR，都放**最右邊一欄**（`x=1790`）：

| 桌面項目 | 對應腳本 | 作用 |
|---|---|---|
| ① 啟動英文版 (8002) | `~/啟動_英文版.sh` | 關掉後重開用（開機本來就自啟） |
| ② 啟動中文版 (8001) | `~/啟動_中文版.sh` | **會自動先 start 服務**再開瀏覽器 |
| ③ 切換熱點 / WiFi | `~/切換_熱點.sh` | 開熱點給訪客掃 QR；再點一次切回 |
| QRcode_英文版.png | — | 掃碼進 `192.168.4.1:8002` |
| QRcode_中文版.png | — | 掃碼進 `192.168.4.1:8001` |

### 10.1 圖示要能拖曳
```bash
# desktop-items-*.conf 裡 sort=mtime 會讓位置被系統接管、拖不動
sed -i 's/^sort=.*/sort=name;ascending;/' \
  ~/.config/pcmanfm/LXDE-pi/desktop-items-*.conf
```
位置寫在同一個檔的 `[檔名]` 區塊（`x=` / `y=`）。改完重啟桌面：
```bash
pkill -f 'wfrespawn pcmanfm'; sleep 1; pkill -f 'pcmanfm --desktop'; sleep 3
setsid nohup wfrespawn pcmanfm --desktop --profile LXDE-pi >/dev/null 2>&1 &
```
⚠️ 只 kill `pcmanfm --desktop` 而不 kill `wfrespawn pcmanfm` 的話，桌面
**不會重生**（會變全黑只剩桌布沒有圖示）。踩過一次。

### 10.2 QR 圖產生（`make_qr.py`，repo 內附）
⚠️ **兩個雷**：
1. **不要用 SSH heredoc 內嵌中文** → 中文在 shell 傳輸被吃掉，畫成豆腐塊。
   **scp 腳本上去再執行**。
2. **字型要逐行選**（RPI5 上兩個字型剛好互補）：
   - `DroidSansFallbackFull.ttf` → 中文正常、**英數是豆腐塊**
   - `DejaVuSans.ttf` → 英數正常、**中文是豆腐塊**

### 10.3 PNG 預設開啟程式
```bash
xdg-mime default chromium.desktop image/png
```
系統**沒裝任何看圖程式**，預設是 `display-im6`（ImageMagick，Wayland 下開不起來）
→ 點桌面 QR 圖沒反應。設成 chromium 即可。

---

## 11. 遠端維護：像訪客一樣操作畫面

`launch_warehouse.sh` 已開 `--remote-debugging-port=9222`（只綁 127.0.0.1）。
配合 repo 的 `en/drive_kiosk.py`：

```bash
scp -i ~/.ssh/rpi5_warehouse en/drive_kiosk.py p400@10.35.219.22:/tmp/
ssh -i ~/.ssh/rpi5_warehouse p400@10.35.219.22 \
  "cd /tmp && python3 drive_kiosk.py 'bluetooth earphones stock' 'whats running low'"
```
它會：填輸入框 → 觸發 input 事件 → 點送出 → 等回答穩定 → **截圖**到
`/tmp/shot_NN.png` + 印出回答文字。

> 這是「審到畫面」的最後一哩：`ws_convo.py` 走 WebSocket 只看得到 JSON，
> 這支看得到**訪客實際看到的渲染結果**（例如「✅ ConfirmInbound 少空格」
> 這種只有截圖才發現的問題）。
> 量測比截圖更精準：`getBoundingClientRect()` 可直接量出是哪個元素撐高版面。

---

## 12. 還原後的驗收清單（**全部要過才算完成**）

```bash
# ① 服務
systemctl is-enabled warehouse-v2-en   # → enabled
systemctl is-active  warehouse-v2-en   # → active
curl -sk https://localhost:8002/ -o /dev/null -w '%{http_code}\n'   # → 200

# ② 語音
curl -sk https://localhost:8002/api/voice_status    # → {"ok":true,...}
cd ~/voice_poc/audio/en && curl -sk -X POST https://localhost:8002/api/asr \
  --data-binary @US-male/q01.wav -H 'Content-Type: application/octet-stream'
# → {"ok":true,"text":"how many bluetooth earphones are left","sec":~1.1}

# ③ 模型版本
md5sum ~/warehouse_v2_en/models/en_q8_0.gguf   # → 213593b5…

# ④ 意圖分類器沒被 numpy 2.x 弄壞（見 §2）

# ⑤ 全量守衛（約 12 分鐘）
cd ~/warehouse_v2_en && bash check_zombies.sh        # 先確認沒殘留
setsid nohup ./run_guard_en.sh > /dev/null 2>&1 &
# 跑完看 _guard_en.log → 應為 891/892
# 唯一 FAIL 是 `do we have scks`（刻意不修：scks→socks 與 hair→chair
# 字元層面完全相同，要區分需英文詞典依賴）

# ⑥ 劇情批（跨句 context）
for r in 1 2 3 4 5; do python3 ws_convo.py --file _conv_en_r$r.txt --rpi5 --reset --quiet; done
python3 ws_convo.py --file _conv_en_voice.txt --rpi5 --reset --quiet   # 語音批
python3 ws_convo.py --file _conv_en_case.txt  --rpi5 --reset --quiet   # 大小寫批

# ⑦ 重開機驗證（最重要，展場當天就是這個流程）
sudo systemctl reboot
# 回來後：kiosk 自動開 8002、無中文彈窗、語音可用、桌面捷徑正常
```

---

## 13. 絕對不要做的事

| ❌ 禁止 | 為什麼 |
|---|---|
| `pkill -f python` / `killall python3` | 會殺掉所有服務；只能用 port 找 PID 殺目標 |
| 用 venv 跑 systemd 服務 | 服務走 `/usr/bin/python3`，venv 裡的套件它看不到 |
| ZeroTier 傳 gguf 大檔 | 頻寬低必斷，殘檔 md5 不符還很難查 |
| 從 SSH 直接起 chromium | 缺 X11/Wayland 環境（`Missing X server or $DISPLAY`）→ 起不來還會把 kiosk 弄掉；要重開機或用桌面捷徑 |
| 只 kill `pcmanfm --desktop` | 桌面不會重生（全黑），要連 `wfrespawn pcmanfm` 一起 |
| 遠端改 `wf-panel-pi.ini` | 曾把面板圖示搞消失且救不回（AP 模式下連不進去） |
| 改 SYSTEM_PROMPT 卻不重訓 | prompt 會拼進每一筆訓練樣本，改了不訓＝訓練/推理不一致 |

---

## 14. 目前狀態速查（2026-07-27）

- 守衛 **891/892**（唯一 FAIL `do we have scks`，刻意不修）
- 劇情批 r1-r5 + 語音批 + 大小寫批 **全綠**
- 語音：whisper tiny.en，**1.1s/句**，端到端含寫入落地驗證通過
- 補訓：**已決定跳過**（除非有大幅效益再評估）——RPI5 跑的已是英文微調版，
  重訓只多買到「SYSTEM_PROMPT 去中文 + 幾類已有規則兜住的長尾」，
  投入產出不成比例
- 兩版並存記憶體 2.9G / 7.9G，磁碟 35G / 57G
