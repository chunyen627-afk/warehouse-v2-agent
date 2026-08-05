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
    llama-cpp-python fasttext qrcode pillow jieba diskcache
```

#### 🚀 最快做法：`llama_cpp` / `fasttext` **從既有機器搬**（省 30+ 分鐘編譯）
`llama-cpp-python` 在 RPI5 上要現場編譯 30+ 分鐘。
兩台 **Python 版本相同（3.11.2）、架構相同（aarch64）** 就能直接搬編譯產物：
```bash
# 來源機
cd ~/.local/lib/python3.11/site-packages && tar czf /tmp/pypkg.tgz \
    llama_cpp llama_cpp_python-*.dist-info \
    fasttext fasttext-*.dist-info fasttext_pybind.cpython-311-aarch64-linux-gnu.so
# 目標機
mkdir -p ~/.local/lib/python3.11/site-packages
cd ~/.local/lib/python3.11/site-packages && tar xzf /tmp/pypkg.tgz
pip3 install --break-system-packages diskcache typing_extensions   # ⚠️ 見下方雷
```

> 🚨 **雷（2026-08-04 踩到）：搬完 `import llama_cpp` 會缺 `diskcache`**
> ```
> ModuleNotFoundError: No module named 'diskcache'
> ```
> `llama_cpp` 的**純 Python 依賴不會跟著 tar 走**（它們是獨立套件）。
> 搬完一定要補 `pip3 install --break-system-packages diskcache typing_extensions`，
> 然後 `python3 -c "import llama_cpp, fasttext"` 驗證真的載得起來。

> 🚨 **雷：先跑 `pip3 install llama-cpp-python` 會白等**
> 若打算用「搬」的，**不要先讓 pip 去下載編譯**——
> 它會下載 71MB 原始碼並開始編譯，中途 `pkill` 掉才發現白花 10 分鐘。
> **決定用搬的，就直接搬**。

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

#### 🚀 最快做法：**從既有機器整包搬**（2026-08-04 實測，省 40 分鐘編譯）
兩台 RPI5 只要 **Python 版本相同（3.11.2）、架構相同（aarch64）**就能直接搬：
```bash
# 來源機
cd ~ && tar czf /tmp/whisper.tgz \
    whisper.cpp/build/bin/whisper-cli \
    whisper.cpp/models/ggml-small-q5_0.bin \
    whisper.cpp/models/ggml-base.bin \
    $(find whisper.cpp -name "*.so*" -printf "%p ")     # ⚠️ 見下方雷
# 目標機
cd ~ && tar xzf /tmp/whisper.tgz
```

> 🚨 **雷（2026-08-04 踩到）：只搬 `whisper-cli` 執行檔會跑不起來**
> ```
> error while loading shared libraries: libwhisper.so.1: cannot open shared object file
> ```
> `whisper-cli` 依賴 `build/bin/` 底下一整組 `.so`
> （`libwhisper.so*`／`libggml*.so*`／`libparakeet.so*`）。
> **打包時一定要用 `find whisper.cpp -name "*.so*"` 一起收**，
> 而且執行時需要 `LD_LIBRARY_PATH=~/whisper.cpp/build/bin`
> （server 若用 systemd 啟動，要在 unit 裡設 `Environment=LD_LIBRARY_PATH=...`）。

#### 從頭編譯（沒有既有機器可搬時）
```bash
cd ~ && git clone https://github.com/ggerganov/whisper.cpp
cd whisper.cpp && cmake -B build && cmake --build build -j2   # 約 5 分鐘
bash ./models/download-ggml-model.sh small-q5_0   # 英文用（現行）
bash ./models/download-ggml-model.sh base         # 中文用（multilingual）
```

**英文 → `small-q5_0` + `-ac 640`**（2026-07-31 換掉 tiny.en，非母語腔實測後定案）：

| 模型 | 真人台灣腔通過率 | 延遲 |
|---|---|---|
| tiny.en（**舊結論，已淘汰**） | 27% | 0.95s |
| base 多語 | 33% | 2.07s |
| small.en | 40% | 6.69s |
| small 多語 | 53% | 6.66s |
| **small-q5_0 + ac640** | **60%** | **3.45s** |

> ⚠️ **舊結論「tiny.en WER 9.3% 最佳」是 TTS 合成音測的**，真人非母語腔下不成立。
> 兩個反直覺點：①**多語版打敗英文專用版**（訓練時看過大量非母語者英文）
> ②**量化單獨用會變慢**（ARM 無對應指令），搭配 `-ac` 削掉 encoder 成本才變加分。
> 2026-08-02 用 100 句真人錄音再驗一次，現行組合三個噪音層都最高 ⇒ **選型已封閉**。

中文沒有英文專用版可選，只能用 multilingual 的 `base`。

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

> 🚨 **雷（2026-08-04 踩到）：Chromium 150 的 CDP 完全不回應**
> `--remote-debugging-port=9222` 的 DevTools Protocol 是**維護工具**
> （`drive_kiosk.py` / `click_probe.py` / `web_test.py` 都靠它遠端看畫面、送輸入）。
> | Chromium | CDP |
> |---|---|
> | **147**（第一台） | ✅ 正常 |
> | **150**（第二台，2026-08 倉庫版本） | ❌ **連得上、指令送得出，但永遠收不到回應** |
>
> 症狀很難認：`/json` 查得到分頁、WebSocket 也連得上，
> 只有 `Runtime.evaluate` 之後**靜默無回應**（15 秒零訊息）。
> ⚠️ 排查時先確認 **`websockets` 版本是 16.0**——
> 17.0.1 會讓 `wsc.connect()` 直接卡死（更早期的症狀，容易跟這個混淆）。
>
> **影響範圍**：只影響「遠端自動化測試」，
> **展場 demo / kiosk 開機自啟 / 服務本身完全不受影響**。
> ⇒ 第二台若只是展示機，**不必為此降版**；
>   真的需要遠端驅動畫面時，再從官方封存裝回 147。

---

## 7b. 🆕 複製第二台的最快流程（2026-08-04 實證）

從零編譯要 4-6 小時，**從既有機器搬只要約 1 小時**。前提：
**Python 版本相同（3.11.2）、架構相同（aarch64）**。

### 步驟
```bash
# ── 0. 新機先裝系統套件（chromium-browser / wtype / cmake 常缺）
sudo apt install -y python3-pip git ffmpeg chromium-browser grim wtype \
                    build-essential cmake

# ── 1. 純 Python 套件用 pip（快）
pip3 install --break-system-packages \
    fastapi==0.92.0 uvicorn==0.17.6 websockets==16.0 \
    qrcode pillow jieba diskcache
#   ⚠️ websockets **鎖 16.0**（17.x 會讓 CDP 卡死，見 §7）

# ── 2. 編譯型套件從舊機搬（省 30+ 分鐘）
#     舊機：
cd ~/.local/lib/python3.11/site-packages && tar czf /tmp/pypkg.tgz \
    llama_cpp llama_cpp_python-*.dist-info \
    fasttext fasttext-*.dist-info fasttext_pybind.*.so
#     新機：
cd ~/.local/lib/python3.11/site-packages && tar xzf /tmp/pypkg.tgz

# ── 3. whisper 從舊機搬（**.so 一定要一起收**，見 §4.3）
cd ~ && tar czf /tmp/whisper.tgz \
    whisper.cpp/build/bin/whisper-cli \
    whisper.cpp/models/ggml-small-q5_0.bin whisper.cpp/models/ggml-base.bin \
    $(find whisper.cpp -name "*.so*" -printf "%p ")

# ── 4. 程式碼＋模型用 rsync（**不要用 tar 傳大檔**，見下方雷）
#     先讓兩台能直連：舊機產金鑰、公鑰貼到新機 authorized_keys
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 -q     # 舊機
#     然後舊機直接推：
rsync -a --partial \
  --exclude=models/_unused --exclude=__pycache__ --exclude='*.pyc' \
  --exclude='*.log' --exclude=audio \
  ~/warehouse_v2_en/ p400@<新機IP>:~/warehouse_v2_en/
rsync -a --partial --exclude=__pycache__ --exclude='*.pyc' --exclude='*.log' \
  ~/warehouse_v2/ p400@<新機IP>:~/warehouse_v2/
```

> 🚨 **雷：用 `tar` + `scp` 傳 700MB+ 會靜默截斷**
> 實測 `wh_en.tgz`(731M) 傳完解開時 `gzip: unexpected end of file`，
> 而且**檔案數只差 91 個**——不比對數量根本發現不了。
> ⇒ **大目錄一律用 `rsync -a --partial`**（可續傳、只傳差異、自動校驗）。
> 兩台 RPI5 直連比繞經 Windows 中繼快得多。

```bash
# ── 5. systemd 服務（**照抄舊機的 unit**，見 §6）
#   ⚠️ 不要自作聰明加 Environment=LD_LIBRARY_PATH（見下方雷）

# ── 6. kiosk 與桌面
cd ~ && tar czf /tmp/kiosk.tgz launch_warehouse.sh fix_chromium_exit.py \
    health_watchdog.sh .config/autostart/warehouse.desktop Desktop/*.desktop

# ── 7. QR code 重產
python3 ~/gen_qr2.py          # 預設＝熱點 192.168.4.1，產中英兩張到桌面
```

> 🚨 **雷（2026-08-04 踩到）：QR 一定要指向熱點 IP `192.168.4.1`**
> 訪客手機是連 RPI5 開的熱點（SSID `RPI5-Demo`），連上後
> **只有區網、沒有外網也沒有 DNS** ⇒ 用公司 Wi-Fi 的 IP（192.168.125.x）
> 產 QR，**展場現場手機完全連不上**。
> 熱點位址查法：`nmcli -g ipv4.addresses con show rpi5-hotspot`（→ 192.168.4.1/24）。

---

## 7c. HTTPS 憑證（兩套，只有第 2 套要動手）

| 憑證 | 用途 | 重建時要做什麼 |
|---|---|---|
| **自簽**（CN=Warehouse-Demo） | 倉管 server 8001/8002 | ❌ **不用做**——`cert.pem`/`key.pem` 隨 rsync 一起到位 |
| **Let's Encrypt**（`rpi5demo.duckdns.org`） | nginx 對外入口 | ✅ 整包搬 + 設獨立續期 |

### 🎤 為什麼一定要 HTTPS（跟手機語音直接相關）
瀏覽器的 `getUserMedia`（麥克風）有硬性限制：
| 連線方式 | 麥克風 |
|---|---|
| `https://` 任何位址 | ✅ 可用 |
| `http://localhost` | ✅ 可用（本機例外） |
| **`http://192.168.x.x`** | ❌ **完全不可用** |
⇒ 倉管 server 本身就跑 HTTPS（`server_https.py`），**手機直連 8001/8002 即可用語音，不需要經過 nginx**。
會跳一次自簽憑證警告，點「繼續前往」後麥克風正常——這是展場的正常流程。

### nginx / LE 憑證是**舊專案 `rpi5-demo` 的遺留**
代理到 **port 8000**（倉管從來沒用過這個 port），
且 `rpi5-demo.service` 在兩台都是 **disabled + inactive**
⇒ `curl -skI https://localhost` 回 **502 是正常的**，不是壞掉。
保留只為與第一台一致；倉管完全不依賴它。

> ⚠️ **想靠 LE 憑證「不跳警告」在展場行不通**：熱點模式沒有 DNS，
> 手機解析不到 `rpi5demo.duckdns.org`，只能用 IP 連，而 LE 憑證綁網域
> ⇒ 用 IP 連一樣警告。**自簽 + 點過警告是展場唯一可行的方式。**

### 搬移步驟（實測可行，2026-08-04）
```bash
# 舊機
sudo tar czf /tmp/le_certs.tgz -C /etc letsencrypt          # 要整包，不能只 cp live/
sudo tar czf /tmp/nginx_demo.tgz -C /etc \
     nginx/sites-available/rpi5-demo systemd/system/rpi5-demo.service
sudo chown p400 /tmp/le_certs.tgz /tmp/nginx_demo.tgz
scp /tmp/le_certs.tgz /tmp/nginx_demo.tgz ~/duckdns-hook.sh p400@<新機IP>:/tmp/

# 新機
sudo apt install -y nginx certbot        # ⚠️ 兩個都要，重建的機器預設沒有
sudo tar xzf /tmp/le_certs.tgz -C /etc
sudo tar xzf /tmp/nginx_demo.tgz -C /etc
cp /tmp/duckdns-hook.sh ~/ && chmod +x ~/duckdns-hook.sh   # ⚠️ 含 token，別印出內容
sudo ln -sf /etc/nginx/sites-available/rpi5-demo /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl enable --now nginx

# 續期 deploy hook
sudo mkdir -p /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/bash\nsystemctl reload nginx\n' | \
  sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh >/dev/null
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
```
**驗收**：`sudo certbot renew --dry-run` 要看到
`all simulated renewals succeeded`；`systemctl list-timers | grep certbot` 要在。
（certbot 2.x 的 dry-run **不會跑 deploy hook**，那是正常的；hook 本體手動跑一次驗證即可。）

> 🔑 **兩台可各自獨立續期**：走 DNS-01（hook 自動塞 DuckDNS TXT），
> **不需要網域 A 記錄指向本機** ⇒ 互不干擾，也不依賴對方開機。
> renewal conf 裡 `manual_auth_hook = /home/p400/duckdns-hook.sh`，
> 只要 hook 放在同路徑就不用改設定。

### 🔄 重簽自簽憑證（換機器／SAN 不含新 IP 時）
新機的憑證是從舊機 rsync 來的，**SAN 只有舊機的位址** →
用新機的區網 IP 連會多一層「名稱不符」警告。用 `en/regen_cert.sh` 重簽：
```bash
bash ~/regen_cert.sh                      # 自動帶入本機 IP 與 hostname
sudo systemctl restart warehouse-v2 warehouse-v2-en
```
🚨 **SAN 一定要含 `192.168.4.1`**（熱點閘道）——展場訪客手機走的就是這個位址。
驗收：`echo | openssl s_client -connect <IP>:8002 2>/dev/null | openssl x509 -noout -checkip <IP>`
要回 `does match certificate`。

---

## 7d. 🎤 手機當麥克風（**2026-08-04 尚未實測**）

100 句語音實測**全部走桌上的 C930c**，手機錄音這條路**從未驗證**。

### 流程（已從程式碼確認，`templates/index.html:2849/2873`）
```
手機瀏覽器 getUserMedia 錄音 → Blob(audio/webm)
   → POST https://192.168.4.1:8002/api/asr    ← 走熱點區網，**不需外網**
   → RPI5 用 whisper.cpp 辨識 → 回傳文字 → 前端送查詢
```
**手機只負責錄音＋上傳，辨識跑在 RPI5** ⇒ 跟桌機用 C930c 是同一條後端路徑。

### 🔑 三個容易搞混的觀念
1. **HTTPS 是 `getUserMedia` 的硬性要求**，與「辨識在雲端或本機」無關。
   `http://192.168.x.x` 下 `navigator.mediaDevices` **物件根本不存在**
   （前端 2896 行有防護判斷）。
2. **手機不需要開外網**——這正是離線 demo 的賣點
   （kiosk 還刻意用 `--disable-background-networking` 關掉所有對外連線）。
3. **不要改用手機內建語音辨識**（`webkitSpeechRecognition`）：
   那會把音訊送 Google/Apple 雲端，**需要外網**，
   且展示重點會從「自研邊緣 AI」變成「雲端服務」。

### ⚠️ 待驗證的風險
**iOS Safari 對自簽憑證的安全上下文判定較嚴**，
可能出現「接受了憑證警告，但仍拒絕麥克風」。
⇒ 展前務必 **Android Chrome ＋ iOS Safari 各實測一台**。
若 iOS 真的不行，替代方案：mkcert 產可被信任的憑證，
或引導訪客改用 RPI5 螢幕前的 C930c。

> 訪客接受憑證警告的操作（兩平台不同，展場引導要注意）：
> | | Android Chrome | iOS Safari |
> |---|---|---|
> | 步驟 | 進階 → 繼續前往 | 顯示詳細資訊 → 瀏覽此網站 → 確認 |
> ⇒ **iOS 多一步**。

> 🚨 **雷：`Environment=LD_LIBRARY_PATH=~/whisper.cpp/build/bin` 會讓服務起不來**
> ```
> libllama.so: undefined symbol: _Z24ggml_backend_meta_device...
> ```
> whisper.cpp 和 llama-cpp-python **各自帶一份 ggml，版本不相容**。
> 設了全域 `LD_LIBRARY_PATH` 會讓 `libllama.so` 去載到 whisper 的 ggml → 崩潰。
> ⇒ **systemd unit 裡不要設**；whisper 是 server 用 subprocess 呼叫的，
>   它自己能找到同目錄的 `.so`。

### 驗收清單（缺一不可）
| 項目 | 指令 |
|---|---|
| 兩服務 active | `systemctl is-active warehouse-v2 warehouse-v2-en` |
| 模型真的載入 | `journalctl -u warehouse-v2-en \| grep "health.*ready"` |
| **檔案與舊機一致** | `md5sum server.py tools_v2.py warehouse.py templates/index.html` 兩台比對 |
| 麥克風 | `arecord -l \| grep card` 且實錄 3 秒看 RMS > 0.0001 |
| WS 能答 | 送 `bluetooth earphones stock` 應得 `inventory_single` |
| kiosk 雙分頁 | `curl -s localhost:9222/json \| grep -c 8002` |
| 桌面 QR | `ls ~/Desktop/QRcode_*.png` |

> ⚠️ **git commit 對不起來是正常的**：RPI5 上的是**獨立本地 repo**（branch `master`），
> Windows 端才有 GitHub remote（branch `main`）。
> **要比版本請比 `md5sum`，不要比 commit hash。**

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

熱點：SSID `RPI5-Demo` / AP IP `192.168.4.1`
> ⚠️ 密碼請用互動方式設，**不要寫進文件或指令歷史**：
> ```bash
> read -s -p "熱點密碼: " P; sudo nmcli con modify rpi5-hotspot \
>   802-11-wireless-security.psk "$P"; unset P
> ```

#### 🆕 兩台機器的熱點命名（2026-08-04）
| | 第一台 | 第二台 |
|---|---|---|
| 主機名 | `raspberrypi` | `raspberrypi2` |
| 區網 IP | 192.168.125.232 | 192.168.125.178 |
| **熱點 SSID** | `RPI5-Demo` | **`RPI5-Demo-2`** |
| 熱點 IP | 192.168.4.1 | 192.168.4.1（**相同，不衝突**）|

🔑 **熱點 IP 相同不會衝突**——每台開的熱點是**各自獨立的網段**
（像每個家庭路由器都是 192.168.1.1）。手機連上哪台的 SSID，
`192.168.4.1` 就是那台。
⇒ **要區分的是 SSID，不是 IP**。這樣做的好處：
**QR code 內容兩台完全通用**，展場流程與說明不用分岔。

建立第二台熱點（密碼另外用上面的互動方式設）：
```bash
sudo nmcli con add type wifi ifname wlan0 con-name rpi5-hotspot \
     ssid "RPI5-Demo-2" autoconnect no
sudo nmcli con modify rpi5-hotspot 802-11-wireless.mode ap \
     802-11-wireless.band bg ipv4.method manual ipv4.addresses 192.168.4.1/24
sudo nmcli con modify rpi5-hotspot 802-11-wireless-security.key-mgmt wpa-psk
```

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

---

## 15. 🩸 一次還原到底檢查表（2026-08-05 機二重建實錄）

> 機二「照手冊還原」後，user 實際上手一天內踩到 **9 類缺口**——全是手冊
> §1-§12 沒涵蓋的。以後照本章補完＋跑完驗收清單，才算「還原完成」。

### 15.1 缺口清單與補法（照抄即可）

```bash
# ① 系統套件（§1 的清單之外還要這些）
sudo apt install -y foot dnsmasq \
     fonts-noto-color-emoji fonts-noto-core fonts-noto-extra fonts-noto-ui-core \
     fonts-dejavu fonts-dejavu-extra \
     fcitx5 fcitx5-chewing fcitx5-frontend-gtk3 fcitx5-frontend-gtk4 fcitx5-config-qt \
     sox libsox-fmt-all jq xxd wget
fc-cache -f
# ② pip（中文語音鏈的簡繁轉換；漏了 /api/asr 回「OpenCC 未安裝」）
sudo pip3 install opencc-python-reimplemented==0.1.7 --break-system-packages
```

| 缺什麼 | 症狀（實際發生過） |
|---|---|
| `fonts-noto-color-emoji` 等字型 | 頁面所有 📦⚠️⭐ 圖示變 **□ 方塊** |
| `foot` | 桌面 terminal 圖示點了沒反應 |
| `dnsmasq`（完整版，非 dnsmasq-base）＋ `/etc/dnsmasq.conf` | **熱點連得上但拿不到 IP**（手機 169.254.x.x 自派位址、QR 掃了逾時）|
| `fcitx5-chewing` 全家 | 沒有注音輸入 |
| OpenCC pip 包 | 中文語音按了錄完報「OpenCC 未安裝」 |
| `sox/jq/xxd/wget` | check_mic 等維運腳本靜默失敗 |

```bash
# ③ 家目錄腳本 —— 一定要 **全 glob** 搬（含中文檔名！）：
#    桌面圖示的目標是 啟動_中文版.sh → switch_lang.sh，漏了圖示全滅
scp 來源機:'~/*.sh' 來源機:'~/*.py' ~/
chmod +x ~/*.sh
# ④ crontab（看門狗雙保險全靠它；重建後曾整個是空的）
ssh 來源機 crontab -l | crontab -
# ⑤ /etc/dnsmasq.conf 從來源機整檔搬 + enable
ssh 來源機 sudo cat /etc/dnsmasq.conf | sudo tee /etc/dnsmasq.conf
sudo systemctl enable --now dnsmasq
# ⑥ Chromium 受管政策（麥克風授權＋關翻譯列；新 profile 沒它＝語音按了秒退、
#    中文頁跳翻譯長條）
sudo mkdir -p /etc/chromium/policies/managed
ssh 來源機 sudo cat /etc/chromium/policies/managed/warehouse-media.json \
  | sudo tee /etc/chromium/policies/managed/warehouse-media.json
# ⑦ systemd drop-in：開機自動歸零（boot_reset.sh + 20-boot-reset.conf ×2 服務）
#    來源機 /etc/systemd/system/warehouse-v2*.service.d/ 整目錄照搬 + daemon-reload
```

### 15.2 ⚠️ 兩個「新 profile 症候群」的治本設定（已進腳本，確認別退版）

- `launch_warehouse.sh` **和** `switch_lang.sh` 都要有 `--password-store=basic`
  （chromium 啟動點有兩個！只改一支，桌面圖示照樣跳「為新鑰匙圈選擇密碼」
  且整頁卡在 about:blank）
- 政策 `TranslateEnabled:false`（en-US 介面看中文頁會跳翻譯提示列）

### 15.3 還原驗收清單（全過才叫還原完成）

1. `sudo reboot` → 開機自動歸零 log（`journalctl -t boot_reset -b`）→ 服務×2
   ready → kiosk 兩分頁有標題、**無任何對話框** → Live badge 顯示運行中
2. **桌面每顆圖示各點一次**：啟動中文版／啟動英文版／terminal／切換熱點
3. `crontab -l` 有 3 條（net_watchdog / health_watchdog / lxterminal 清理）
4. 畫面圖示無 □ 方塊；中文頁右上無翻譯長條
5. 注音：輸入框 Ctrl+Space 能打中文
6. **語音**：麥克風按下去（不秒退）→ 講一句 → 有辨識結果
7. **熱點實測**（手機）：切熱點 → 手機連 SSID → **拿到 192.168.4.x**（不是
   169.254）→ 掃網頁 QR → 過兩層憑證警告 → 頁面能查詢
8. `./run_guard_zh.sh --smoke` 全綠 → 有時間再跑全量 1122+892+parity
9. 溫度：待機 ≤55°C、壓測零降頻（`vcgencmd get_throttled` = 0x0）

### 15.4 本章自己也踩過的雷（2026-08-05 同日補）

| 雷 | 說明 |
|---|---|
| 裝 dnsmasq 後**必須 `systemctl restart dnsmasq`** | apt 安裝當下服務就用**原廠空設定**先啟動了；之後才蓋 `/etc/dnsmasq.conf` 的話，`enable --now` **不會重啟**（already active）→ 只綁 DNS(53) 不綁 DHCP(67)，症狀跟沒裝一樣。驗收要看 `sudo ss -ulpn \| grep :67` 有 dnsmasq、journal 有 `DHCP, IP range` 行——**別只看 is-active** |
| 切換_熱點.sh 不可寫死 WiFi 設定檔名 | 兩台名字不同（EOSL_P400 / preconfigured），寫死會讓另一台切不回 WiFi。已改成動態抓（`nmcli -t -f NAME,TYPE ... 802-11-wireless` 排除 hotspot），並在開熱點前自檢 dnsmasq |
