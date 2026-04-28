#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
用法:
  ./scripts/push_all.sh [-m 提交信息] [-r 远端] [-b 分支]

参数:
  -m, --message   提交信息，默认: "chore: update appstore <时间戳>"
  -r, --remote    远端名，默认: origin
  -b, --branch    分支名，默认: 当前分支
  -h, --help      显示帮助
EOF
}

REMOTE="origin"
BRANCH=""
MESSAGE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -m|--message)
      MESSAGE="${2:-}"
      shift 2
      ;;
    -r|--remote)
      REMOTE="${2:-}"
      shift 2
      ;;
    -b|--branch)
      BRANCH="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "当前目录不是 Git 仓库" >&2
  exit 1
fi

if [[ -z "$BRANCH" ]]; then
  BRANCH="$(git branch --show-current)"
fi
if [[ -z "$BRANCH" ]]; then
  echo "无法识别当前分支，请用 -b 指定分支" >&2
  exit 1
fi

if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  echo "远端不存在: $REMOTE" >&2
  exit 1
fi

if rg -q "filter=lfs" .gitattributes 2>/dev/null; then
  if ! git lfs version >/dev/null 2>&1; then
    echo "检测到 LFS 规则，但 git-lfs 不可用。请先安装并执行: git lfs install" >&2
    exit 1
  fi
fi

if [[ -z "$MESSAGE" ]]; then
  MESSAGE="chore: update appstore $(date '+%Y-%m-%d %H:%M:%S')"
fi

# 核心流程：统一暂存所有变更，确保项目整体同步
git add -A

if git diff --cached --quiet; then
  echo "没有可提交的变更，跳过 commit。"
else
  git commit -m "$MESSAGE"
fi

# 核心流程：推送到指定远端与分支
git push "$REMOTE" "$BRANCH"
echo "已推送: $REMOTE/$BRANCH"
