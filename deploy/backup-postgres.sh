#!/usr/bin/env sh
# Run from the repository root on the VPS. Keeps the newest 14 backups.
set -eu

backup_dir="${BACKUP_DIR:-./backups}"
mkdir -p "$backup_dir"
backup_file="$backup_dir/ai-trader-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"

docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' | gzip > "$backup_file"
find "$backup_dir" -type f -name 'ai-trader-*.sql.gz' -mtime +14 -delete
printf 'Backup written to %s\n' "$backup_file"
