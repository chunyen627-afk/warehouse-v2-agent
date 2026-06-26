---
name: edge_agent_model_size
description: 業界邊緣倉儲 Agent 的模型尺寸定位 — 270M 是「路由器」不是「決策者」
metadata: 
  node_type: memory
  type: reference
  originSessionId: f5448369-2b06-401c-af82-d93240db6da6
---

業界(2026)倉儲/邊緣 Agent 的模型尺寸實況：
- **雲端真自主 Agent**（SAP/Manhattan/Blue Yonder、Claude Code 那類）：GPT-4/5、Claude、DeepSeek-V3 等 100B–1T+，A/B/C 全開放（任意讀寫檔、開放 reasoning loop、自決每一步）。
- **邊緣 SLM-agent（NVIDIA 2026 主推）**：主流 **3B–8B**（Phi-4-mini 3.8B / Qwen2.5 7B / Nemotron Nano / SmolLM2），INT4、Jetson Orin NX、sub-100ms。NVIDIA 論點：Agent 多數子任務不需大模型，SLM 省 10–30× 成本。
- **270M（本專案 FunctionGemma）**：業界定位是「**SLM Router**」——專職意圖辨識+抽參數，真正 reasoning 交給 server 編排或更大模型。

**關鍵結論**：[[warehouse_v2_project]] 的 v2.0 架構（270M 做路由、server 編排多步 Loop）**正是業界對 270M 的標準用法，沒做錯**。

**對「衝開放 Agent」的決策影響**：
- A（寫報告/寫檔）、B（動態找檔）→ 270M 穩做得到，是合理的「真開放」。
- C（270M 自己決定下一步追哪個檔 / 自主決策）→ 業界沒人用 270M 做，都用 3B+。硬衝是挑戰尺寸極限，有研究/展示價值但不穩、非業界做法。
- user 決策（2026-06）：**A+B 先做實、C 試但不強求**；若要看齊業界，另一條路是換 base 到 Qwen2.5-3B（需評估 RPI5/本機跑不跑得動）。

**實測結論（2026-06-23，v2.1 訓練後）**：
- A 波 generate_report（寫報告）、B 波 list_files（動態找檔）→ **270M 學得會**，端到端 OK。
- C 波 judge_cause_found（讀 context 判斷 yes/no 決策）→ **270M 學不起來**，220 條訓練樣本後端到端只 1/6，模型多半路由回 search_log 而非輸出 judge。**親自驗證了「270M 是路由器不是決策者」**。
- → C 類自主決策應留給 server 規則編排（v2.0 本來的做法），或換 3B+ 模型。judge declaration 不進 prompt 是對的。
