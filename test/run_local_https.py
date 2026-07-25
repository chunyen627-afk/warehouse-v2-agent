# -*- coding: utf-8 -*-
"""
run_local_https.py — 本機用 HTTPS 起中文版（8001）。

為什麼要 https（不能只跑 server.py）：
  1. 前端 index.html 的 WebSocket 跟隨頁面協定
     （`location.protocol === 'https:' ? 'wss:' : 'ws:'`）——瀏覽器用 https
     開頁但 server 只有 http 時，wss 連不上 → **畫面卡在 Loading、送不出字**。
  2. 麥克風權限：瀏覽器只在 https（或 localhost）才給 getUserMedia。

跟 RPI5 的 server_https.py 同構，差在路徑不寫死（那支是 /home/p400/...）。
用法：cd warehouse_v2/test && <Python311> run_local_https.py
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
sys.path.insert(0, str(HERE))
os.environ.setdefault("PORT", "8001")

import uvicorn  # noqa: E402
import server   # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        server.app,
        host="0.0.0.0",
        port=int(os.environ["PORT"]),
        ssl_keyfile=str(HERE / "key.pem"),
        ssl_certfile=str(HERE / "cert.pem"),
        log_level="warning",
    )
