#!/usr/bin/env bash
# 创建并行开发 worktree(#289)。规范正文见 AGENTS.md「Git 工作流 → 多 worktree 并行开发」。
#
# 用法: scripts/create-worktree.sh <N> <feat分支名> [base-ref]
#   例: scripts/create-worktree.sh 3 feat/my-issue-290 origin/m/research-backtest
#
# 幂等:每步检测已存在即跳过,中断后可直接重跑。
#
# 效果(N=3 为例):
#   - worktree 位于主目录同级 ../FinDashboard-wt3,切到 <feat分支名>
#   - data_cache / data_releases 以 NTFS junction 共享主目录真实数据(零拷贝)
#   - 独立 .env:库 findashboard_wt3 / findashboard_wt3_test,API 8003 / web 5175 /
#     MCP 8766,OpenCode / MCP 关闭(运行时全局锚定主目录)
#   - uv sync 复用主目录 .uv-cache;alembic 建表;web npm install
#
# 警告:worktree 内的 data_cache / data_releases 是 junction。删除链接只能
#   `cmd //c rmdir <名字>`;绝对不要对它们执行 rm -rf(Git Bash 会穿透删除主目录真实数据)。

set -euo pipefail

if [ $# -lt 2 ]; then
  echo "用法: $0 <N> <feat分支名> [base-ref=origin/m/research-backtest]" >&2
  exit 1
fi

N="$1"
BRANCH="$2"
BASE="${3:-origin/m/research-backtest}"

MAIN="$(cd "$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")" && pwd)"
WT="$(cd "$MAIN/.." && pwd)/FinDashboard-wt$N"
# 原生 exe(uv 等)不识别 MSYS 的 /c/... 路径,需要 Windows 盘符形式
MAIN_WIN="$(cygpath -w "$MAIN")"
MAIN_MIXED="$(cygpath -m "$MAIN")"
echo "主目录: $MAIN"
echo "worktree: $WT (base=$BASE)"

git fetch origin --prune

# 1) worktree + 分支(注册检测用 rev-parse,避免 porcelain 路径格式差异)
REG=false
if [ -d "$WT" ]; then
  wt_git="$(git -C "$WT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  main_git="$(git -C "$MAIN" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  [ -n "$wt_git" ] && [ "$wt_git" = "$main_git" ] && REG=true
fi
if $REG; then
  echo "[1/7] worktree 已注册,跳过 add(当前分支: $(git -C "$WT" branch --show-current))"
elif git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  git worktree add "$WT" "$BRANCH"
  echo "[1/7] worktree 已创建,检出既有分支 $BRANCH"
else
  git worktree add -b "$BRANCH" "$WT" "$BASE"
  echo "[1/7] worktree 已创建并切到新分支 $BRANCH"
fi
cd "$WT"

# 2) junction 共享昂贵数据(只存主目录一份)
for d in data_cache data_releases; do
  if [ -e "$d" ]; then
    echo "[2/7] $d 已存在,跳过 junction"
  elif [ -d "$MAIN/$d" ]; then
    cmd //c "mklink /J \"${d}\" \"${MAIN_WIN}\\${d}\""
    echo "[2/7] $d -> 主目录 junction"
  else
    echo "[2/7] 主目录不存在 $d,跳过(首次数据同步后重跑本脚本再补)"
  fi
done

# 3) 独立 .env(pydantic 按 CWD 加载)
if [ -f .env ]; then
  echo "[3/7] .env 已存在,跳过改写"
else
  cp "$MAIN/.env" .env
  sed -i \
    -e "s#^\(FINBOARD_DB_URL=.*\)findashboard\$#\1findashboard_wt${N}#" \
    -e "s#^\(FINBOARD_TEST_DB_URL=.*\)findashboard_test\$#\1findashboard_wt${N}_test#" \
    -e "s#^FINBOARD_MCP_PORT=.*#FINBOARD_MCP_PORT=$((8763 + N))#" \
    -e "s#^FINBOARD_OPENCODE_ENABLED=.*#FINBOARD_OPENCODE_ENABLED=false#" \
    -e "s#^FINBOARD_OPENCODE_WEB_ENABLED=.*#FINBOARD_OPENCODE_WEB_ENABLED=false#" \
    -e "s#^FINBOARD_MCP_ENABLED=.*#FINBOARD_MCP_ENABLED=false#" \
    .env
  echo "[3/7] .env 已写入独立库/端口配置"
fi

# 4) 独立数据库(连接参数取自主目录 .env;测试库由 conftest 自动建表,无需迁移)
echo "[4/7] 创建数据库 findashboard_wt${N} / findashboard_wt${N}_test(已存在则跳过)"
(
  cd "$MAIN"
  uv run python - "$N" <<'PY'
import re
import sys

import psycopg
from pathlib import Path

n = sys.argv[1]
env = Path(".env").read_text(encoding="utf-8")
line = next(l for l in env.splitlines() if l.startswith("FINBOARD_DB_URL="))
m = re.search(r"postgresql\+psycopg://([^:@/]+):([^@]+)@([^:/]+):(\d+)/([^/?\s]+)", line)
user, pw, host, port, dbname = m.groups()
conn = psycopg.connect(
    host=host, port=int(port), user=user, password=pw, dbname=dbname, autocommit=True
)
for name in (f"findashboard_wt{n}", f"findashboard_wt{n}_test"):
    if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
        print(f"  {name}: 已存在,跳过")
    else:
        conn.execute(f'CREATE DATABASE "{name}"')
        print(f"  {name}: 已创建")
conn.close()
PY
)

# 5) Python 依赖(一次性复用主目录 uv 下载缓存;须用 Windows 盘符路径)
UV_CACHE_DIR="$MAIN_MIXED/.uv-cache" uv sync --all-packages
echo "[5/7] uv sync 完成"

# 6) 数据库迁移(worktree .env → findashboard_wtN)
uv run alembic upgrade head
echo "[6/7] alembic 迁移完成"

# 7) 前端依赖(node_modules 各 worktree 独立,不共享)
if [ -d web/node_modules ]; then
  echo "[7/7] web/node_modules 已存在,跳过 npm install"
else
  if command -v npm >/dev/null 2>&1; then
    NPM=npm
  else
    NPM="$HOME/AppData/Local/Programs/nodejs/npm.cmd"
  fi
  (cd web && "$NPM" install)
  echo "[7/7] npm install 完成"
fi

cat <<EOF

完成。日常命令(在 $WT 下):
  make dev API_PORT=$((8000 + N)) WEB_PORT=$((5172 + N))   # 后端 + 前端预览(独立库/端口)
  uv run pytest tests/unit -v                              # 单元测试
提示: data_cache / data_releases 是 junction,删除链接用 cmd //c rmdir,禁止 rm -rf。
EOF
