---
name: feedback-python311-server
description: 啟動 server.py 必須明確指定 Python311，不能用裸 python 指令
metadata: 
  node_type: memory
  type: feedback
  originSessionId: b038c70f-c688-4acf-bf6d-95181312930d
---

啟動 warehouse_v2 server 一定要用絕對路徑 `C:\Users\pjunm\AppData\Local\Programs\Python\Python311\python.exe`，絕對不能用裸 `python`。

**Why:** `python` 在 PowerShell 解析到 `hermes-agent/venv/Scripts/python.exe`（Headroom proxy 的虛擬環境），該環境沒有 `llama_cpp`，導致 ModuleNotFoundError → HEALTH=failed → 所有按鈕壞掉。使用者已多次遇到這個問題，是反覆踩的雷。Python311 才有 llama_cpp。

**How to apply:** 所有 python 指令（server.py / 工具腳本 / 測試腳本）一律用：
```powershell
$py = "C:\Users\pjunm\AppData\Local\Programs\Python\Python311\python.exe"
# 啟動 server
$pid8000 = (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue).OwningProcess
if ($pid8000) { Stop-Process -Id $pid8000 -Force }
Start-Process $py -ArgumentList "server.py" -NoNewWindow -WorkingDirectory "C:\Users\pjunm\OneDrive\Desktop\FunctionGemma_Finetune\warehouse_v2\test"
```
