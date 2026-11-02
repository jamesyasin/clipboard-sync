#!/bin/bash
# 可选：把「监听」装成系统常驻服务（开机自启、崩溃自拉起，无界面）。
#
# ⚠️ 如果你用控制面板（start_panel.sh / panel.py）操作，请不要装这个服务。
#    面板本身就是监听器：打开窗口即开始监听，关闭窗口立即停止、不留后台进程。
#    同时装服务会出现两份监听，同一张图被传两次。
cd "$(dirname "$0")"
PLIST="$HOME/Library/LaunchAgents/com.james.clipboard-sync.plist"
launchctl bootout "gui/$(id -u)/com.james.clipboard-sync" 2>/dev/null
if launchctl bootstrap "gui/$(id -u)" "$PLIST"; then
  echo "✅ 常驻服务已启动（无界面，后台一直跑）"
  echo "停止：launchctl bootout gui/$(id -u)/com.james.clipboard-sync"
else
  echo "❌ 启动失败。请在本机「终端」里执行本脚本（受限 shell 注册 launchd 会报 Input/output error）"
fi
