---
name: feedback-no-kill-python
description: 禁止用 taskkill /F /IM python.exe 或 Stop-Process -Name python 重啟 server，會殺掉 Headroom proxy
metadata: 
  node_type: memory
  type: feedback
  originSessionId: b038c70f-c688-4acf-bf6d-95181312930d
---

絕對不可以用 `taskkill /F /IM python.exe` 或 `Stop-Process -Name python` 來重啟 server。

**Why:** 這會把 Headroom proxy 一起殺掉，造成 ConnectionRefused，使用者要手動重啟 proxy 才能恢復。

**How to apply:** 每次需要重啟 warehouse_v2/test/server.py 時，用 port 找 PID 的方式：
```powershell
$pid8000 = (Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue).OwningProcess
if ($pid8000) { Stop-Process -Id $pid8000 -Force }
Start-Process -FilePath "C:\Users\pjunm\AppData\Local\Programs\Python\Python311\python.exe" -ArgumentList "server.py" -NoNewWindow
```
只殺佔用目標 port 的程序，不影響其他 Python 程序。
