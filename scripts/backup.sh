#!/usr/bin/env sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
backup_dir="$repo_dir/backups"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_file="$backup_dir/rubika-$stamp.dump"

umask 077
mkdir -p "$backup_dir"
cd "$repo_dir"
docker compose exec -T db sh -c \
  'pg_dump --format=custom --no-owner --no-acl -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > "$backup_file"
test -s "$backup_file"
docker compose exec -T db sh -c 'pg_restore --version' >/dev/null
echo "Backup created: $backup_file"
