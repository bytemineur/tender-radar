#!/usr/bin/env bash
# 每日备份数据库（SQLite）
# 用法：crontab -e 加入
#   30 2 * * * /path/to/tender-radar/scripts/backup_db.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="$ROOT/data/tender.db"
DIR="$ROOT/data/backups"

[ -f "$DB" ] || { echo "数据库不存在: $DB"; exit 1; }
mkdir -p "$DIR"

STAMP="$(date +%Y%m%d_%H%M)"
OUT="$DIR/tender_$STAMP.db"

# SQLite 在线备份：避免直接 cp 拿到写了一半的文件
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB" ".backup '$OUT'"
else
  cp "$DB" "$OUT"
fi

echo "已备份: $OUT"

# 只保留最近 14 份
ls -1t "$DIR"/tender_*.db 2>/dev/null | tail -n +15 | xargs -r rm -f
