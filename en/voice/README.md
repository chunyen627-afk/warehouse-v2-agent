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
