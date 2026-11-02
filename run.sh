#!/bin/bash
# 启动后台监听（幂等：已在运行则不重复启动）
cd "$(dirname "$0")" || exit 1
PY="${PYTHON:-$(command -v python3)}"
if [ -z "$PY" ]; then echo "找不到 python3，请先安装 Python 3"; exit 1; fi
mkdir -p logs
if pgrep -f "sync.py run" > /dev/null; then
  echo "已在运行，pid: $(pgrep -f 'sync.py run' | tr '\n' ' ')"
  exit 0
fi
nohup "$PY" sync.py run >> logs/stdout.log 2>&1 &
echo $! > sync.pid
sleep 1
echo "已启动 pid=$(cat sync.pid)，日志: logs/sync.log"
