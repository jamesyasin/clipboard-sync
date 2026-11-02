#!/bin/bash
# 把本地仓库推送到 GitHub
#
# 用法：
#   ./push_github.sh                 # 推到 jamesyasin/clipboard-sync
#   ./push_github.sh 仓库名           # 推到指定仓库
#   GITHUB_USER=xxx ./push_github.sh # 换账号
#
# 说明：走 SSH（git@github.com），不需要 token 落盘。
#       首次推送前，仓库需要在 GitHub 上已存在（GitHub 不支持 push 自动创建）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

USER="${GITHUB_USER:-jamesyasin}"
REPO="${1:-clipboard-sync}"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "❌ 当前目录不是 git 仓库：$ROOT"
  exit 1
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"

echo "== 目标：$USER/$REPO (分支 $BRANCH) =="

# 确认远端仓库存在
if ssh -T -o StrictHostKeyChecking=no -o ConnectTimeout=8 "git@github.com" 2>&1 | grep -qi "permission denied"; then
  echo "❌ SSH 认证失败，请先确认 ~/.ssh 里有已添加到 GitHub 的 key"
  exit 1
fi
if ! git ls-remote "git@github.com:$USER/$REPO.git" >/dev/null 2>&1; then
  echo "❌ 远端仓库不存在：https://github.com/$USER/$REPO"
  echo "   请先到 https://github.com/new 创建一个空仓库（不要勾 README / .gitignore / LICENSE）"
  exit 1
fi

# 敏感信息自检：绝不把密钥推上去
echo "== 敏感信息自检 =="
if git grep -I -l -E "app_secret|gVDsFw|HAO7bBU5|ou_[a-z0-9]{16,}|cli_[a-z0-9]{8,}|/Users/james" --cached 2>/dev/null; then
  echo "❌ 检出敏感信息，已中止推送"
  exit 1
fi
echo "   ✅ 无密钥 / token / 本机路径残留"

git remote remove origin 2>/dev/null || true
git remote add origin "git@github.com:$USER/$REPO.git"

echo "== 推送 =="
git push -u origin "$BRANCH"

echo
echo "✅ 完成：https://github.com/$USER/$REPO"
