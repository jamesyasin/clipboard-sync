#!/bin/bash
# 启动桌面控制面板（浮窗，可拖动/置顶）
cd "$(dirname "$0")"
# 可用 PYTHON 环境变量指定解释器；默认用 PATH 里的 python3
PY="${PYTHON:-$(command -v python3)}"
if [ -z "$PY" ]; then echo "找不到 python3，请先安装 Python 3"; exit 1; fi
exec "$PY" panel.py
