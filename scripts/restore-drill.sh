#!/usr/bin/env sh
set -eu

if [ "$#" -ne 1 ] || [ ! -s "$1" ]; then
  echo "Usage: $0 /absolute/path/to/backup.dump" >&2
  exit 2
fi

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
backup_file=$(CDPATH= cd -- "$(dirname -- "$1")" && pwd)/$(basename -- "$1")
drill_db="rubika_restore_drill_$(date -u +%Y%m%d%H%M%S)"

cd "$repo_dir"
cleanup() {
  docker compose exec -T db sh -c \
    'dropdb --if-exists --force -U "$POSTGRES_USER" "$1"' sh "$drill_db" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker compose exec -T db sh -c \
  'createdb -U "$POSTGRES_USER" "$1"' sh "$drill_db"
docker compose exec -T db sh -c \
  'pg_restore --exit-on-error --no-owner --no-acl -U "$POSTGRES_USER" -d "$1"' \
  sh "$drill_db" < "$backup_file"
docker compose exec -T db sh -c \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -c '\''SELECT COUNT(*) AS users FROM users; SELECT COUNT(*) AS orders FROM orders;'\''' \
  sh "$drill_db"
echo "Restore drill passed in temporary database: $drill_db"
