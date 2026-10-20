#!/usr/bin/env bash
# Nightly Postgres backup -> compressed dump -> S3-compatible object storage (D20).
#
#   ./scripts/backup_db.sh                # dump, verify, upload, prune
#   ./scripts/backup_db.sh --dry-run      # show what would happen, change nothing
#   ./scripts/backup_db.sh --no-upload    # local dump only
#
# D20 says "nightly pg_dump to object storage". D7 makes it more than hygiene:
# the snapshot history cannot be re-fetched from any vendor at any price, so the
# backup IS the only copy of the engine's edge.
#
# Configuration comes from .env (see .env.example). Requires:
#   sudo apt install -y postgresql-client-16 awscli
#
# TO RESTORE — the pre/post_restore pair is TimescaleDB's documented procedure:
# it suspends background workers and policy jobs so the restore cannot race
# them. A plain restore happens to work on an uncompressed database; do not rely
# on that once chunks start compressing.
#
#   createdb -O engine investment_engine
#   psql -d investment_engine -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
#   psql -d investment_engine -c "SELECT timescaledb_pre_restore();"
#   gunzip -c investment_engine-<stamp>.dump.gz | psql -d investment_engine
#   psql -d investment_engine -c "SELECT timescaledb_post_restore();"
#   python -m db.init --check
#
set -euo pipefail

DRY_RUN=0
UPLOAD=1
for arg in "$@"; do
  case "$arg" in
    --dry-run)   DRY_RUN=1 ;;
    --no-upload) UPLOAD=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -f "$REPO_ROOT/.env" ]]; then
  echo "error: no .env at $REPO_ROOT/.env" >&2
  exit 1
fi

# Read ONLY the variables this script needs, WITHOUT sourcing the file.
# `source .env` executes it as bash: a value containing parentheses, a `$` or a
# backtick either breaks the script or runs as code. (SEC_EDGAR_USER_AGENT in
# .env.example contains parentheses, which is exactly how that was discovered.)
# Values may be optionally single- or double-quoted; the quotes are stripped.
read_env() {
  local key="$1" line
  line="$(grep -m1 -E "^[[:space:]]*${key}=" "$REPO_ROOT/.env" || true)"
  [[ -z "$line" ]] && return 0
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"
  line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

for key in PGDATABASE PGUSER PGPASSWORD PGHOST PGPORT \
           BACKUP_DIR BACKUP_S3_BUCKET BACKUP_S3_ENDPOINT BACKUP_S3_PREFIX \
           BACKUP_RETENTION_DAYS AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY \
           AWS_DEFAULT_REGION; do
  # A real environment variable wins over .env, matching core/settings.py.
  if [[ -z "${!key:-}" ]]; then
    value="$(read_env "$key")"
    [[ -n "$value" ]] && export "$key=$value"
  fi
done

: "${PGDATABASE:?PGDATABASE must be set}"
: "${PGUSER:?PGUSER must be set}"
BACKUP_DIR="${BACKUP_DIR:-$REPO_ROOT/data/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
S3_PREFIX="${BACKUP_S3_PREFIX:-investment-engine}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
# The name pattern is load-bearing: pruning only ever deletes files matching it,
# so a bucket shared with anything else stays untouched.
BASENAME="${PGDATABASE}-${STAMP}.dump.gz"
DUMP_PATH="$BACKUP_DIR/$BASENAME"

run() {
  if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run] $*"; else "$@"; fi
}

mkdir -p "$BACKUP_DIR"

echo "==> dumping $PGDATABASE to $DUMP_PATH"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "  [dry-run] pg_dump ... | gzip > $DUMP_PATH"
else
  # --no-owner / --no-privileges so the dump restores cleanly onto a fresh box
  # with a differently-named role. Plain SQL (not -Fc) so a restore needs only
  # psql and gunzip, which is what you want at 3am on an unfamiliar machine.
  PGPASSWORD="${PGPASSWORD:-}" pg_dump \
      --host="${PGHOST:-localhost}" \
      --port="${PGPORT:-5432}" \
      --username="$PGUSER" \
      --dbname="$PGDATABASE" \
      --no-owner --no-privileges --no-tablespaces \
    | gzip -9 > "$DUMP_PATH.partial"
  # PARTIAL-THEN-RENAME. Only name it correctly once it is complete, so a dump
  # interrupted halfway can never be mistaken for a good backup by the pruner,
  # by an uploader, or by a human at 3am.
  mv "$DUMP_PATH.partial" "$DUMP_PATH"
fi

if [[ $DRY_RUN -eq 0 ]]; then
  echo "    wrote $(du -h "$DUMP_PATH" | cut -f1)"
  if [[ ! -s "$DUMP_PATH" ]]; then
    echo "error: dump is empty — refusing to upload or prune" >&2
    exit 1
  fi
  # A dump that does not contain the four hypertables is not a backup of THIS
  # engine. Refusing here means a misconfigured PGDATABASE cannot quietly
  # replace 30 days of good backups with 30 days of empty ones.
  #
  # ONE decompression pass, collecting every table name. The obvious loop —
  # `gunzip -c "$DUMP" | grep -q "...$table"` per table — is broken under
  # `set -o pipefail`: grep exits as soon as it matches, gunzip takes SIGPIPE
  # and returns 141, and the pipeline reports failure. Worse, it does so only
  # when the table appears EARLY enough in the dump for gunzip to still have
  # output pending, so the check passes or fails depending on alphabetical
  # position. `grep -oE` consumes the whole stream, so nothing is signalled.
  DUMPED_TABLES="$(gunzip -c "$DUMP_PATH" \
    | grep -oE 'CREATE TABLE public\.[a-z_]+' \
    | sed 's/.*\.//' | sort -u)"
  for table in option_chain_snapshots estimate_snapshots prices_daily iv_daily; do
    if ! grep -qx "$table" <<<"$DUMPED_TABLES"; then
      echo "error: dump is missing $table — refusing to upload or prune" >&2
      exit 1
    fi
  done
  echo "    verified: all four hypertables present"
fi

if [[ $UPLOAD -eq 1 && -n "${BACKUP_S3_BUCKET:-}" ]]; then
  AWS_ARGS=()
  [[ -n "${BACKUP_S3_ENDPOINT:-}" ]] && AWS_ARGS+=(--endpoint-url "$BACKUP_S3_ENDPOINT")
  DEST="s3://$BACKUP_S3_BUCKET/$S3_PREFIX/$BASENAME"

  echo "==> uploading to $DEST"
  run aws "${AWS_ARGS[@]}" s3 cp "$DUMP_PATH" "$DEST"

  echo "==> pruning remote objects older than ${RETENTION_DAYS}d"
  CUTOFF="$(date -u -d "$RETENTION_DAYS days ago" +%Y-%m-%d)"
  # Scoped three ways: to our prefix, to our filename pattern, and to the cutoff.
  aws "${AWS_ARGS[@]}" s3 ls "s3://$BACKUP_S3_BUCKET/$S3_PREFIX/" \
    | awk -v cutoff="$CUTOFF" -v db="$PGDATABASE" \
        '$1 < cutoff && $4 ~ "^"db"-.*\\.dump\\.gz$" { print $4 }' \
    | while read -r old; do
        echo "    deleting $old"
        run aws "${AWS_ARGS[@]}" s3 rm "s3://$BACKUP_S3_BUCKET/$S3_PREFIX/$old"
      done
elif [[ $UPLOAD -eq 1 ]]; then
  echo "==> BACKUP_S3_BUCKET is not set; keeping the dump locally only"
  echo "    a backup that lives on the same VM as the database is not a backup"
fi

echo "==> pruning local dumps older than ${RETENTION_DAYS}d"
if [[ $DRY_RUN -eq 1 ]]; then
  find "$BACKUP_DIR" -maxdepth 1 -name "${PGDATABASE}-*.dump.gz" -mtime "+$RETENTION_DAYS" \
    -printf '  [dry-run] would delete %p\n'
else
  find "$BACKUP_DIR" -maxdepth 1 -name "${PGDATABASE}-*.dump.gz" -mtime "+$RETENTION_DAYS" \
    -print -delete
fi

echo "==> done"
