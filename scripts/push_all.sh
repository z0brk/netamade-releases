#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
用法:
  ./scripts/push_all.sh [-m 提交信息] [-r 远端] [-b 分支]

参数:
  -m, --message   提交信息，默认: "chore: 更新应用中心 <时间戳>"
  -r, --remote    远端名，默认: origin
  -b, --branch    分支名，默认: 当前分支
  -h, --help      显示帮助

每次发布使用无父提交，目标分支必须为当前分支。
允许手动删除旧增强资源；仍拒绝原路径修改历史文件。
统一暂存整个仓库，并以明确远端旧 SHA 的 force-with-lease 推送。
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

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
CURRENT_BRANCH="$(git symbolic-ref --quiet --short HEAD)" || {
  echo "不允许在 detached HEAD 上发布" >&2
  exit 1
}
for STATE in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do
  if [[ -e "$(git rev-parse --git-path "$STATE")" ]]; then
    echo "存在未完成的合并、变基或拣选操作，拒绝发布" >&2
    exit 1
  fi
done

if [[ -z "$BRANCH" ]]; then
  BRANCH="$CURRENT_BRANCH"
fi
if [[ "$BRANCH" != "$CURRENT_BRANCH" ]]; then
  echo "目标分支必须与当前分支一致，请先切换到 $BRANCH" >&2
  exit 1
fi

REF="refs/heads/$BRANCH"
OLD_HEAD="$(git rev-parse HEAD)"
# 固定本轮观察到的远端值，不能用可能被后台 fetch 更新的跟踪分支作租约。
REMOTE_STATE="$(git ls-remote --refs "$REMOTE" "$REF")"
REMOTE_HEAD="${REMOTE_STATE%%$'\t'*}"

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
  MESSAGE="chore: 更新应用中心 $(date '+%Y-%m-%d %H:%M:%S')"
fi

# 发布前核对中心 v2 清单与本地 AEP2。不会代打包或接收平台私钥。
PYTHON="${APPSTORE_PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi
"$PYTHON" "$ROOT/enhancement_manager.py" validate
# 手动清理旧资源可以发布；当前 aeps 引用的文件缺失会被 validate 拒绝。
if [[ -n "$(git diff --name-only --diff-filter=M HEAD -- enhancements/)" ]]; then
  echo "增强历史资源存在原路径修改，拒绝发布。请使用新的 AEP2 发行包；不再需要的旧文件可手动删除。" >&2
  exit 1
fi
while IFS= read -r -d '' ASSET; do
  # 新交付资源也必须是验签通过的 AEP2，不能利用未入索引的文件发布 APK/v1。
  "$PYTHON" "$ROOT/enhancement_manager.py" inspect "$ROOT/$ASSET" >/dev/null
done < <(git diff --name-only --diff-filter=A -z HEAD -- enhancements/; git ls-files --others --exclude-standard -z -- enhancements/)

# 核心流程：统一暂存所有变更，确保项目整体同步
git add -A
git diff --cached --check

TREE="$(git write-tree)"
if [[ "$TREE" == "$(git rev-parse "$OLD_HEAD^{tree}")" && "$(git rev-list --count "$OLD_HEAD")" == 1 ]]; then
  SNAPSHOT="$OLD_HEAD"
  echo "内容未变且已有无父提交，复用当前快照。"
else
  # 不传 -p：新提交没有父提交；不使用 reset --hard，不修改工作区文件。
  SNAPSHOT="$(git commit-tree "$TREE" -m "$MESSAGE")"
  git update-ref -m "发布无父提交快照" "$REF" "$SNAPSHOT" "$OLD_HEAD"
fi
if [[ "$(git rev-list --count "$SNAPSHOT")" != 1 ]]; then
  echo "发布快照必须只有一个可达提交" >&2
  exit 1
fi

# pre-push 钩子负责 LFS 上传；不使用 --no-verify 或无租约的 --force。
git push --force-with-lease="$REF:$REMOTE_HEAD" "$REMOTE" "$REF:$REF"
PUSHED_STATE="$(git ls-remote --refs "$REMOTE" "$REF")"
if [[ "${PUSHED_STATE%%$'\t'*}" != "$SNAPSHOT" ]]; then
  echo "远端分支与本次快照不一致，请检查是否发生并发发布" >&2
  exit 1
fi
echo "已推送: $REMOTE/$BRANCH"
