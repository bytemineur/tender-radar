#!/usr/bin/env bash
# 打包一份干净的部署包：只含代码与配置，排除 .venv / data / 调试产物。
#
# 用法（项目根目录）：
#   bash scripts/pack.sh                 # 输出到 ./tender-radar-deploy.tar.gz
#   bash scripts/pack.sh /tmp/out.tgz    # 指定输出路径
#   PACK_WITH_ENV=1 bash scripts/pack.sh # 连 .env 一起打（服务器要用现成配置时）
#
# 默认**不带 .env**：里面有 SMTP 密码和管理员邮箱，分发/进仓库都不该有。
# 要直接部署到自己的服务器，用 PACK_WITH_ENV=1 打一份，传完就别再外传。
#
# 打完会自动自检：确认没有混进 .venv / data / shots，且站点规则齐全。

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

OUT="${1:-$ROOT/tender-radar-deploy.tar.gz}"
OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"

INCLUDE=(
  app web configs deploy scripts
  main.py requirements.txt
  Dockerfile docker-compose.yml .dockerignore
  README.md DEPLOY.md .env.example
)
WITH_ENV="${PACK_WITH_ENV:-0}"

echo "==> 打包根目录：$ROOT"

# 打之前先清掉字节码缓存，免得把本机路径烧进包里。
# 必须先把 .venv 整棵子树 prune 掉再找：写成 `-not -path './.venv/*'` 的话
# find 照样会把 180MB 的虚拟环境全遍历一遍（只是不匹配），白白多花一分钟。
find . -path ./.venv -prune -o -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

rm -f "$OUT"
# --exclude='.env' 必须排在 .env 之前才生效：tar 是按参数顺序逐条匹配的，
# 一旦先显式列出 .env，后面再有 exclude 也拦不住它。
EXCLUDES=(
  --exclude='.venv'
  --exclude='.git'
  --exclude='data'
  --exclude='__pycache__'
  --exclude='*.pyc'
  --exclude='scripts/shots'
  --exclude='*.tar.gz'
)
if [ "$WITH_ENV" != "1" ]; then
  EXCLUDES+=(--exclude='.env')
else
  INCLUDE+=(.env)
fi

tar -czf "$OUT" "${EXCLUDES[@]}" "${INCLUDE[@]}"

echo
echo "==> 产物：$OUT"
echo "==> 体积：$(du -h "$OUT" | cut -f1)"
echo

# ── 自检 ──────────────────────────────────────────────────────────────
echo "==> 自检"

# 先落一份文件清单。不要写成 `tar -tzf x | grep -q`：grep -q 命中后会提前退出，
# tar 收到 SIGPIPE 返回非 0，配合 set -o pipefail 会被误判成「文件缺失」。
LIST_FILE="$(mktemp)"
trap 'rm -f "$LIST_FILE" 2>/dev/null || true' EXIT
tar -tzf "$OUT" > "$LIST_FILE"

if [ "$WITH_ENV" = "1" ]; then
  # 打了 .env 时只拦其余垃圾，并额外确认 .env 确实在里面
  DIRTY="$(grep -E '\.venv|/data/|shots/' "$LIST_FILE" || true)"
  if grep -Fxq ".env" "$LIST_FILE"; then
    echo "⚠️  含 .env（SMTP 密码在内）：只传自己的服务器，别外传、别进仓库"
  else
    echo "❌ 指定了 PACK_WITH_ENV=1 但包里没有 .env"
    exit 1
  fi
else
  DIRTY="$(grep -E '\.venv|/data/|\.env$|shots/' "$LIST_FILE" || true)"
fi
if [ -n "$DIRTY" ]; then
  echo "❌ 混入垃圾："
  echo "$DIRTY"
  exit 1
fi
echo "✅ 无 .venv / data / shots"

SITES="$(grep -cE 'configs/sites/[^/]+\.yaml' "$LIST_FILE" || true)"
SITES="${SITES:-0}"
if [ "$SITES" -gt 0 ]; then
  echo "✅ 站点规则 ${SITES} 个"
else
  echo "❌ 站点规则 0 个，说明 configs 没打进去"
  exit 1
fi

for f in main.py requirements.txt app/main.py web/index.html web/app.html configs/app.yaml; do
  if grep -Fxq "$f" "$LIST_FILE"; then
    echo "✅ $f"
  else
    echo "❌ 缺失 $f"
    exit 1
  fi
done

echo
echo "==> 上传到服务器："
echo "    scp $(basename "$OUT") user@server:/opt/tender-radar/"
echo "    ssh user@server 'cd /opt/tender-radar && tar -xzf $(basename "$OUT")'"
