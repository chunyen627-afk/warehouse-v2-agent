---
name: feedback-auto-restart-server
description: 改完倉管 server 相關程式碼後，要自動重啟 server
metadata: 
  node_type: memory
  type: feedback
  originSessionId: f5448369-2b06-401c-af82-d93240db6da6
---

改完 server.py / tools_v2.py / intent_clf.py / templates/index.html 等倉管相關檔案後，**自動重啟 server**，不用等使用者要求。

**Why:** 使用者每次改完都要手動啟動，很麻煩。

**How to apply:**
- **絕對禁止** `Stop-Process -Name python*` 或 `taskkill /F /IM python.exe`，會連 Headroom proxy 一起殺掉造成 API ConnectionRefused
- 停舊 server 一律用 port 找 PID：
  ```powershell
  $pid8000 = (Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue).OwningProcess
  if ($pid8000) { Stop-Process -Id $pid8000 -Force }
  ```
- 啟動：`Start-Process` 在背景啟動，WorkingDirectory = `warehouse_v2/test`
- Python = `C:\Users\pjunm\AppData\Local\Programs\Python\Python311\python.exe`
- 環境變數：`WAREHOUSE_DATA_MODE=multi`
- 啟動後說「Server 已重啟，請開 http://localhost:8000」
- **重啟前必須先寫 _dbg.py 測試核心邏輯，確認無誤才重啟**，不能直接上線讓使用者踩雷
- 測試完清掉 _dbg.py
