#!/usr/bin/env bash
# 启动智扫通服务。可用环境变量覆盖：PYTHON / PORT / HOST。
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python}"
PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

if [ ! -f web/dist/index.html ]; then
  echo "前端产物不存在，开始构建..."
  (cd web && { [ -d node_modules ] || npm install --no-fund --no-audit; } && npm run build)
fi

echo "启动服务：http://${HOST}:${PORT}"
exec "$PYTHON" -m server --host "$HOST" --port "$PORT"
