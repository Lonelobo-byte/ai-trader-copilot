#!/usr/bin/env sh
# Destructive: replaces the current database. Usage: ./deploy/restore-postgres.sh backups/file.sql.gz
set -eu

backup_file="${1:?Usage: ./deploy/restore-postgres.sh backups/file.sql.gz}"
[ -f "$backup_file" ] || { echo "Backup file not found: $backup_file" >&2; exit 1; }
printf 'This replaces the current PostgreSQL database. Type RESTORE to continue: '
read answer
[ "$answer" = "RESTORE" ] || { echo 'Restore cancelled.'; exit 1; }

gunzip -c "$backup_file" | docker compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
printf 'Restore completed.\n'
