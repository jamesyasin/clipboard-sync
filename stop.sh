#!/bin/bash
# 停止后台监听（同时处理 launchd 常驻服务）
cd "$(dirname "$0")" || exit 1
launchctl bootout "gui/$(id -u)/com.james.clipboard-sync" 2>/dev/null && echo "已卸载常驻服务"
if [ -f sync.pid ]; then
  PID=$(cat sync.pid)
  kill "$PID" 2>/dev/null && echo "已停止 pid=$PID" || echo "进程已不在"
  rm -f sync.pid
else
  PID=$(pgrep -f "sync.py run")
  if [ -n "$PID" ]; then kill $PID && echo "已停止 pid=$PID"; else echo "未在运行"; fi
fi
