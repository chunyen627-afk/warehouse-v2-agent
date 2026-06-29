---
name: eval_http_tool
description: HTTP-based 路由評測工具 eval_http.py，避免 WebSocket 單訪客 crash
metadata:
  type: reference
---

## HTTP 評測工具 eval_http.py（2026-06-29）

`warehouse_v2/eval_http.py` — 走 HTTP `/api/query` 的路由評測腳本。

**為什麼不用 test_e2e.py**：test_e2e.py 走 WebSocket，但 server 有單訪客保護（新連線關舊連線），導致 WS 在第二題 crash（`RuntimeError: Cannot call 'send' once a close message has been sent`）。

**使用**：
```powershell
$py = "C:\Users\pjunm\AppData\Local\Programs\Python\Python311\python.exe"
cd warehouse_v2
& $py eval_http.py
```

**依賴**：server 的 `/api/query` response 頂層有 `_function` 欄位（2026-06-29 加入），用來判斷實際執行的 function。OOV clarify / reject / error 則從 `view` 推斷。

**限制**：只測 routing（function name + keyword），不測 summary 內容正確性。

關聯：[[warehouse_v2_project]] [[run_full_eval_after_server_change]]
