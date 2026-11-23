#!/bin/sh
# Dump the production PostgreSQL to a dated, compressed file. Run it from a trusted machine:
#   DATABASE_URL='postgresql://user:pass@host/db' ./scripts/backup_db.sh [folder]
# Restore with:  gunzip -c backup.sql.gz | psql "$DATABASE_URL"
set -eu
: "${DATABASE_URL:?set DATABASE_URL to the production database}"
dir="${1:-backups}"; mkdir -p "$dir"
out="$dir/chess-review-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
pg_dump --no-owner --no-privileges "$DATABASE_URL" | gzip > "$out"
echo "wrote $out"
