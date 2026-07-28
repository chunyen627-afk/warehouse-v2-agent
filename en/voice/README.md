# 英文版語音：選型評測工具

## 為什麼在這裡

user 定調（2026-07-26）：**語音模型只用歐美、拒絕大陸**。
現行 Fun-ASR-Nano 是阿里（FunAudioLLM）的，SenseVoice 同屬阿里 → **都要換掉**。

這幾支是換模型前的評測工具。放進 warehouse_v2 repo 是為了**異地備份**
（根目錄 `FunctionGemma_Finetune` 沒有 remote）。
**執行時的主要工作目錄仍是 `voice_poc/`**（那裡有 noise 素材與既有音檔）。

## 檔案

| 檔案 | 用途 |
|---|---|
| `gen_en_audio.py` | edge-tts 產英文測試音（免費，47 語音/14 腔調） |
| `gen_en_audio_gcp.py` | Google **Chirp 3 HD** 產音（最擬真，免費額度 1M byte/月） |
| `bench_whisper.py` | 在 RPI5 量 whisper.cpp 的延遲 / WER |
| `sync_audio.sh` | 音檔 push/pull/clean（**RPI5 測完清、備份放 WIN**，user 定調） |

### 2026-07-28 新增：真人錄音 100 句 + 端到端測試

| 檔案 | 用途 |
|---|---|
| `read100_en.txt` | **100 句語料**（5 欄：編號｜英文句｜期望view｜必含關鍵字｜中文意思）|
| `read100_en_對照表.md` | 給 user 看的對照表（含中文意思、資源位置） |
| `read100_en.sh` | **真人錄音**：唸一句錄一句、自動判 PASS/FAIL（RPI5，port 8002） |
| `check_mic_en.sh` | 錄音前體檢（⚠️ 中文版 `check_mic.sh` 寫死 8001，驗英文要用這支） |
| `noise_retest_en.sh` | 拿已錄的乾淨音**自動混噪重測**，不用重念 |
| `gen_read100_demo.py` | 產 100 句示範朗讀（edge-tts），讓 user 知道唸什麼字 |
| `practice_en.py` | 本機練唸（Enter 播放示範、r 重播、s 跳過） |
| `tts_bench.py` | **TTS 端到端基準**：示範音檔 → ASR → 倉管 → 判定 |
| `_probe_en.txt` | 探針批 47 句（刻意寫「作者不會寫的句型」） |

**⚠️ 第 5 欄的中文只顯示給人看，不會送進系統**——英文版後端擋中文，
混進查詢字串會整句被 reject。腳本讀 SENT 只取第 2 欄。

**⚠️ 乾淨錄音存 `audio/user_clean_en/`**，與中文版 `user_clean/` 分開
——那 100 句中文真人音是不可重現資產，絕不能被蓋掉。

## 📊 TTS 端到端基準（2026-07-28，`tts_bench.py`）

100 句 × 3 噪音層 = 300 次辨識，真實賣場環境音（`mall_ambience.mp3`）混入：

| 噪音層 | 通過率 |
|---|---|
| 乾淨 | **92%** |
| light −18dB（一般展場） | **92%** |
| heavy −8dB（尖峰吵雜） | **91%** |

**噪音幾乎不影響**——whisper 對環境音的韌性比預期好。
13 句出現過 FAIL，其中 **5 句三層都掛**才是穩定問題（其餘是隨機波動）。

### 這批抓到、已修的（詳見 commit 03bb19b）
| 破口 | 性質 |
|---|---|
| `what's in central warehouse…` | ASR 聽對、LLM 判對，**防幻覺閘門把 `what's` 當陌生商品清掉正解** |
| `could you tell me the earphone stock` | 禮貌用語整類漏掉 → 回「查無 could 這個商品」 |
| `what about north` | carry-over 詞表漏了 `about` |
| `what's this demo about` | 回熱銷榜（訪客第一句就答非所問） |

**大多數打字訪客也會遇到**——撇號、禮貌用語、追問講法都是。
純語音專屬的只有黏字/聽錯那類。

### 刻意不修的兩句（沒有可用訊號）
- `mobs`（mops 聽錯）→ 最像的是 **mouse** 0.667 而非 mop，硬放寬會導向滑鼠
- `sunheadstock`（黏字）→ 最高分是 elastic 0.526，完全不相干

⇒ 判準：**有訊號能區分正例反例才是修復層的活**；沒訊號的留給補訓語料。

## 🔑 方法論：不是輪數不夠，是輸入源不夠多樣
守衛 892 句全綠、劇情批 5 輪、渲染批 5 輪之後，這批 TTS + 探針**還是**
抓到 5 個破口——因為前面所有句子都是**同一個作者（Claude）打字造的**，
盲點會系統性重複，再跑 20 輪同樣方式也抓不到。
換產生源（TTS 唸→whisper 聽、刻意寫「我不會寫的句型」）立刻見效。

## 實測結果（2026-07-26，edge-tts 300 檔）

whisper.cpp on RPI5（4 核 Cortex-A76）：

| 模型 | 延遲 | WER 乾淨 | WER light | WER heavy |
|---|---|---|---|---|
| **tiny.en** | **0.94s** | **9.3%** | 10.4% | 12.0% |
| base.en | 2.33s | 10.2% | 10.2% | 11.6% |
| *現行 Fun-ASR（中文）* | *2.45s* | — | — | — |

**結論：選 tiny.en** —— 比現行快 2.6 倍、WER 與 base 相當、74MB、OpenAI 出品。

⚠️ **base.en 沒有比 tiny.en 好**（推翻常識推估）：句子短、句型固定，
tiny 容量已夠，模型變大的收益顯現不出來。

### 腔調差異（tiny.en 乾淨層）
US 3.5% ／ AU 5.0% ／ GB 7.0% ／ IN 7.7% ／ **SG 23.3%**

### ⚠️ WER 高估了實際失敗率
抽驗 SG 錯誤句走端到端，多數系統仍答對——**文字端的容錯層在扛**：
- `Powerbank Inventory` → ✅ Power Bank（合成詞拆解）
- `North receive 50 wireless mouse` → ✅ 正確開卡
- `yoga meant` → ❌ 距離太遠沒救到

⇒ 選型不能只看 WER，要看**端到端答對率**。

## ⚠️ TTS 是下限估計
合成音比真人清楚、無口音變異。中文版經驗：**合成音 clean 100%、真人首測 35/52**。
TTS 全過不代表展場可用；**TTS 就掛才是真的不行**。

## 金鑰
`gcp-tts-key.json` 由 user 自行從 GCP 主控台下載，放 `voice_poc/`。
已在 `.gitignore`（`*-key.json`），**絕不進 repo**。
