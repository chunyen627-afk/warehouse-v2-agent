#!/bin/bash
# ═══════════════════════════════════════════════════════
# RPI5 warehouse_v2 部署腳本
# 執行方式：在 RPI5 上跑 bash deploy_rpi5.sh
# ═══════════════════════════════════════════════════════
set -e

PROJECT="warehouse_v2"
RPI_DIR="/home/p400/$PROJECT"
EXISTING_MODEL="/home/p400/rpi5-demo/models/functiongemma-270m-it-fine-tune.q8_0.gguf"
PORT=8001

echo "=== RPI5 warehouse_v2 部署 ==="
echo ""

# ── 0. 檢查 ──
echo "[0/5] 檢查環境..."
python3 --version
python3 -c "import llama_cpp" 2>/dev/null && echo "  llama_cpp_python OK" || { echo "  MISSING llama_cpp_python!"; exit 1; }
python3 -c "import fastapi"   2>/dev/null && echo "  fastapi OK" || pip3 install fastapi --break-system-packages -q
python3 -c "import uvicorn"   2>/dev/null && echo "  uvicorn OK" || pip3 install uvicorn --break-system-packages -q
python3 -c "import qrcode"    2>/dev/null && echo "  qrcode OK"  || pip3 install qrcode[pil] --break-system-packages -q
python3 -c "import jinja2"    2>/dev/null && echo "  jinja2 OK"  || pip3 install jinja2 --break-system-packages -q
python3 -c "import websockets" 2>/dev/null && echo "  websockets OK" || pip3 install websockets --break-system-packages -q
echo "  檢查完成"

# ── 1. 目錄結構 ──
echo ""
echo "[1/5] 建立目錄結構..."
mkdir -p "$RPI_DIR/models"
mkdir -p "$RPI_DIR/templates"
mkdir -p "$RPI_DIR/static"
mkdir -p "$RPI_DIR/warehouse_data"
echo "  目錄已建立"

# ── 2. 模型 ──
echo ""
echo "[2/5] 設定模型..."
if [ -f "$RPI_DIR/models/functiongemma-270m-it-fine-tune.q8_0.gguf" ]; then
    echo "  模型已存在，跳過"
elif [ -f "$EXISTING_MODEL" ]; then
    ln -sf "$EXISTING_MODEL" "$RPI_DIR/models/"
    echo "  symlink 模型完成"
else
    echo "  ⚠️ 找不到模型！請手動放入 $RPI_DIR/models/"
fi

# ── 3. 檢查必要檔案 ──
echo ""
echo "[3/5] 檢查必要檔案..."
for f in server.py warehouse.py tools_v2.py seed_data.json system_prompt.txt; do
    if [ -f "$RPI_DIR/$f" ]; then
        echo "  ✓ $f"
    else
        echo "  ✗ $f 缺失！"
    fi
done
for f in index.html display.html; do
    if [ -f "$RPI_DIR/templates/$f" ]; then
        echo "  ✓ templates/$f"
    else
        echo "  ✗ templates/$f 缺失！"
    fi
done

# ── 4. systemd 服務 ──
echo ""
echo "[4/5] 建立 systemd 服務..."

SERVICE_FILE="/etc/systemd/system/warehouse-v2.service"
sudo tee "$SERVICE_FILE" > /dev/null << SERVEOF
[Unit]
Description=Warehouse V2 AI Agent Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=p400
WorkingDirectory=$RPI_DIR
ExecStart=/usr/bin/python3 $RPI_DIR/server.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

Environment=PORT=$PORT
Environment=N_THREADS=4
Environment=N_CTX=2048

[Install]
WantedBy=multi-user.target
SERVEOF

sudo systemctl daemon-reload
echo "  服務檔已建立"

# ── 5. 停止舊服務、啟動新服務 ──
echo ""
echo "[5/5] 啟動服務..."
# 先停掉可能佔用 PORT 的舊服務（不殺 rpi5-demo）
EXISTING_PID=$(sudo lsof -ti:$PORT 2>/dev/null || true)
if [ -n "$EXISTING_PID" ]; then
    echo "  端口 $PORT 被 $EXISTING_PID 佔用，釋放中..."
    sudo kill $EXISTING_PID 2>/dev/null || true
    sleep 1
fi

sudo systemctl stop warehouse-v2.service 2>/dev/null || true
sudo systemctl enable warehouse-v2.service
sudo systemctl start warehouse-v2.service
sleep 3

# 檢查
if systemctl is-active --quiet warehouse-v2.service; then
    echo "  ✓ 服務已啟動"
else
    echo "  ⚠️ 服務啟動失敗，查看 log："
    sudo journalctl -u warehouse-v2.service --no-pager -n 20
fi

echo ""
echo "=== 部署完成 ==="
echo "  URL: http://$(hostname -I | awk '{print $1}'):$PORT"
echo "  狀態: sudo systemctl status warehouse-v2"
echo "  日誌: sudo journalctl -u warehouse-v2 -f"
echo "  停止: sudo systemctl stop warehouse-v2"
