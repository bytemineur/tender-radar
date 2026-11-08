#!/usr/bin/env bash
# 招标雷达 — 部署后自检
#
# 用法：
#   ./deploy/check.sh                          # 检查本机 127.0.0.1:8000
#   ./deploy/check.sh https://tender.example.com
#
# 退出码 0 = 全通过，1 = 有检查项失败。

set -uo pipefail

BASE="${1:-http://127.0.0.1:8000}"
BASE="${BASE%/}"
FAILED=0

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAILED=1; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }

echo "目标: $BASE"
echo

echo "网络与进程"

# 1. 服务端口
if curl -fsS -m 10 "$BASE/api/health" >/tmp/tr_health.json 2>/dev/null; then
  ok "健康检查接口可访问"
  TOTAL=$(python3 -c 'import json;print(json.load(open("/tmp/tr_health.json")).get("total","?"))' 2>/dev/null || echo '?')
  echo "      库内公告: $TOTAL"
else
  bad "健康检查接口访问失败（服务没起来？Nginx 没 reload？）"
fi

# 2. 首页
CODE=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$BASE/")
[ "$CODE" = "200" ] && ok "首页返回 200" || bad "首页返回 $CODE（期望 200）"

# 3. 门禁是否生效（未登录不给数据）
CODE=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$BASE/api/tenders")
if [ "$CODE" = "401" ]; then
  ok "数据接口门禁生效（未登录返回 401）"
elif [ "$CODE" = "200" ]; then
  bad "数据接口未鉴权！未登录就返回 200，请检查 TR_REQUIRE_LOGIN 是否为 true"
else
  warn "数据接口返回 $CODE（期望 401）"
fi

# 4. HTTP → HTTPS 跳转
if [[ "$BASE" == https://* ]]; then
  HOST="${BASE#https://}"
  LOC=$(curl -s -o /dev/null -m 10 -w '%{redirect_url}' "http://$HOST/" 2>/dev/null)
  [[ "$LOC" == https://* ]] && ok "HTTP 自动跳转 HTTPS" || warn "HTTP 未跳转到 HTTPS（检查 Nginx 80 端口配置）"
fi

echo
echo "磁盘"

DF=$(df -P /opt 2>/dev/null | awk 'NR==2{print $5}' | tr -d '%')
if [ -n "$DF" ]; then
  if [ "$DF" -ge 90 ]; then bad "磁盘已用 ${DF}%，接近写满"
  elif [ "$DF" -ge 75 ]; then warn "磁盘已用 ${DF}%"
  else ok "磁盘已用 ${DF}%" ; fi
fi

DB="/opt/tender-radar/data/tender.db"
[ -f "$DB" ] && ok "数据库存在: $(du -h "$DB" | cut -f1)" || warn "未找到 $DB（还没部署到 /opt？）"

BK="/opt/tender-radar/data/backups"
if [ -d "$BK" ]; then
  N=$(ls -1 "$BK"/tender_*.db 2>/dev/null | wc -l | tr -d ' ')
  [ "$N" -gt 0 ] && ok "备份目录有 $N 份备份" || warn "备份目录为空（配好 cron 了吗？）"
else
  warn "没有备份目录，建议配置 scripts/backup_db.sh 定时任务"
fi

echo
if [ "$FAILED" -eq 0 ]; then echo "全部通过。"; else echo "存在失败项，按上面的提示处理。"; fi
exit "$FAILED"
