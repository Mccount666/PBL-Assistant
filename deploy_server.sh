#!/usr/bin/env bash
# ============================================================
# PBL 讨论助手 —— 腾讯云服务器一键部署脚本
# 用法（在服务器上以 root 执行）：
#   bash deploy_server.sh
# ============================================================
set -e

APP_DIR="/opt/pbl"
SERVICE="pbl"
PORT="${PORT:-8600}"

echo "=========================================="
echo " PBL 讨论助手 · 服务器部署"
echo "=========================================="

# ---------- 1. 系统依赖 ----------
if command -v apt-get >/dev/null 2>&1; then
  echo "[1/6] 安装系统依赖 (apt)..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip curl >/dev/null
elif command -v yum >/dev/null 2>&1; then
  echo "[1/6] 安装系统依赖 (yum)..."
  yum install -y -q python3 python3-pip curl >/dev/null
else
  echo "!! 未识别的包管理器，请手动安装 python3 / python3-venv"
  exit 1
fi

PY_VER=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "    Python $PY_VER"

# ---------- 2. 目录 ----------
echo "[2/6] 准备目录 $APP_DIR ..."
mkdir -p "$APP_DIR"
mkdir -p "$APP_DIR/data" "$APP_DIR/reports"

# ---------- 3. 虚拟环境 + 依赖 ----------
echo "[3/6] 创建虚拟环境并安装 Python 依赖..."
if [ ! -d "$APP_DIR/venv" ]; then
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements-server.txt"

# ---------- 4. 防火墙（本机） ----------
echo "[4/6] 检查本机防火墙..."
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "$PORT"/tcp >/dev/null 2>&1 || true
  echo "    ufw 已放行 $PORT/tcp"
elif command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld; then
  firewall-cmd --permanent --add-port="$PORT"/tcp >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
  echo "    firewalld 已放行 $PORT/tcp"
else
  echo "    本机防火墙未启用或无需配置"
fi

# ---------- 5. systemd 服务 ----------
echo "[5/6] 注册 systemd 服务 ($SERVICE)..."
cat > /etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=PBL Discussion Assistant (FastAPI)
After=network.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
Environment=PYTHONIOENCODING=utf-8
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/venv/bin/uvicorn voice_server:app --host 0.0.0.0 --port ${PORT} --proxy-headers
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
sleep 3

# ---------- 6. 验证 ----------
echo "[6/6] 健康检查..."
OK=0
for i in 1 2 3 4 5; do
  if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    OK=1
    break
  fi
  sleep 2
done

PUBLIC_IP=$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo "你的服务器IP")

echo ""
echo "=========================================="
if [ "$OK" = "1" ]; then
  echo " ✅ 部署成功"
  echo ""
  echo " 访问地址： http://${PUBLIC_IP}:${PORT}/"
  echo ""
  echo " 常用命令："
  echo "   查看状态  systemctl status ${SERVICE}"
  echo "   查看日志  journalctl -u ${SERVICE} -f"
  echo "   重启服务  systemctl restart ${SERVICE}"
else
  echo " ⚠️  服务已安装但健康检查未通过"
  echo " 请查看日志： journalctl -u ${SERVICE} -n 50 --no-pager"
fi
echo "=========================================="
echo ""
echo "📌 别忘了在腾讯云控制台 → 防火墙/安全组 放行 ${PORT}/TCP"