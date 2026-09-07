#!/usr/bin/env bash
# ============================================================================
# N字战法交易系统 —— 云服务器部署脚本(设计目标: /root 目录, root用户执行)
# 用法: bash deploy.sh
#   INSTALL_DIR=/root/NPatternStrategy   # 可覆盖安装位置(默认 /root/NPatternStrategy)
#   NSTRAT_PORT=8000                     # 可覆盖服务端口(默认取 server/config.json)
# 特点: 全部使用脚本自身相对定位与变量, 无硬编码路径; systemd 常驻+开机自启。
# ============================================================================
set -euo pipefail

# ---- 定位项目根(相对本脚本), 不用绝对地址 ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---- 安装目标(默认部署到 /root 下) ----
INSTALL_DIR="${INSTALL_DIR:-/root/NPatternStrategy}"

echo "==> 部署 N字战法交易系统"
echo "    项目目录 : ${PROJ_ROOT}"
echo "    安装目录 : ${INSTALL_DIR}"
echo "    运行用户 : $(id -un)"

if [ "$(id -u)" != "0" ]; then
  echo "!! 建议以 root 执行(systemd 服务安装需要)。继续尝试..."
fi

# ---- 同步代码到安装目录(若已在目标目录则跳过) ----
if [ "$(readlink -f "${PROJ_ROOT}")" != "$(readlink -f "${INSTALL_DIR}")" ]; then
  echo "==> 复制项目到 ${INSTALL_DIR}"
  mkdir -p "$(dirname "${INSTALL_DIR}")"
  rm -rf "${INSTALL_DIR}"
  cp -r "${PROJ_ROOT}" "${INSTALL_DIR}"
fi
cd "${INSTALL_DIR}"

# ---- 依赖检查 ----
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "!! 未找到 ${PYTHON_BIN}, 尝试安装 python3..."
  apt-get update -y && apt-get install -y python3 python3-venv python3-pip || yum install -y python3 python3-pip
fi

# ---- 虚拟环境与依赖 ----
VENV_DIR="${INSTALL_DIR}/.venv"
if [ ! -x "${VENV_DIR}/bin/python" ]; then
  echo "==> 创建虚拟环境"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi
echo "==> 安装依赖"
"${VENV_DIR}/bin/pip" install --upgrade pip -q
"${VENV_DIR}/bin/pip" install -r requirements.txt -q

# ---- 首次启动配置(config.json / data 目录, 相对定位) ----
CFG="${INSTALL_DIR}/server/config.json"
if [ ! -f "${CFG}" ]; then
  cp server/config.example.json "${CFG}"
fi
PORT="${NSTRAT_PORT:-$(sed -n 's/.*"port"[ ]*:[ ]*\([0-9]*\).*/\1/p' "${CFG}" | head -1)}"
PORT="${PORT:-8000}"

# ---- systemd 服务(路径来自变量, 写入后再替换) ----
SERVICE_FILE="/etc/systemd/system/nstrategy.service"
echo "==> 安装 systemd 服务 ${SERVICE_FILE} (端口 ${PORT})"
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=N字战法 自动选股与自我执行交易系统
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment=NSTRAT_HOST=0.0.0.0
Environment=NSTRAT_PORT=${PORT}
ExecStart=${VENV_DIR}/bin/python ${INSTALL_DIR}/run.py
Restart=always
RestartSec=10
StandardOutput=append:${INSTALL_DIR}/data/logs/service.log
StandardError=append:${INSTALL_DIR}/data/logs/service.err.log

[Install]
WantedBy=multi-user.target
EOF

mkdir -p "${INSTALL_DIR}/data/logs"
systemctl daemon-reload
systemctl enable nstrategy.service >/dev/null 2>&1 || true
systemctl restart nstrategy.service || { echo "服务启动失败, 查看: journalctl -u nstrategy -n 50"; exit 1; }

echo ""
echo "==> 部署完成 ✔"
echo "    访问: http://<服务器IP>:${PORT}"
echo "    状态: systemctl status nstrategy"
echo "    日志: journalctl -u nstrategy -f"
