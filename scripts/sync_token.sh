#!/usr/bin/env bash
# Sync the Schwab OAuth token from this machine to the engine VM (D21 / O8).
#
# O8 is still open on the MECHANISM: this implements the scp option, which is
# the simpler of the two candidates and needs no extra credentials on the VM.
# Replace it if object storage wins, and record the decision.
#
#   ./scripts/sync_token.sh --dry-run   # check connectivity and paths, send nothing
#   ./scripts/sync_token.sh             # sync, then verify on the VM
#
# CONFIGURATION LIVES IN .env, NOT IN THIS FILE:
#
#   ENGINE_HOST      user@host of the VM            (e.g. engine@203.0.113.10)
#   ENGINE_PATH      repo path on the VM            (default /home/engine/InvestmentEngine)
#   ENGINE_SSH_KEY   private key to authenticate with (optional; default agent/ssh config)
#
# The host is deliberately NOT committed. It is the address of a box holding a
# live brokerage token, and a public repo is a poor place to publish where that
# lives. `.env` is gitignored; `.env.example` documents the names.
#
# Parsed with a targeted grep rather than `source .env`: sourcing executes the
# file as bash, and .env legitimately contains values with parentheses and
# spaces (SEC_EDGAR_USER_AGENT) that break under it.
set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

read_env() {
  local key="$1" line
  [[ -f "$REPO_ROOT/.env" ]] || return 0
  line="$(grep -m1 -E "^[[:space:]]*${key}=" "$REPO_ROOT/.env" || true)"
  [[ -z "$line" ]] && return 0
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"
  line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

for key in ENGINE_HOST ENGINE_PATH ENGINE_SSH_KEY SCHWAB_TOKEN_PATH; do
  # A real environment variable wins over .env, matching core/settings.py.
  if [[ -z "${!key:-}" ]]; then
    value="$(read_env "$key")"
    [[ -n "$value" ]] && export "$key=$value"
  fi
done

ENGINE_PATH="${ENGINE_PATH:-/home/engine/InvestmentEngine}"
LOCAL_TOKEN="${SCHWAB_TOKEN_PATH:-$REPO_ROOT/data/schwab_token.json}"
[[ "$LOCAL_TOKEN" = /* ]] || LOCAL_TOKEN="$REPO_ROOT/$LOCAL_TOKEN"

if [[ -z "${ENGINE_HOST:-}" || "$ENGINE_HOST" == *REPLACE_ME* ]]; then
  echo "error: ENGINE_HOST is not set. Add it to .env (see .env.example):" >&2
  echo "       ENGINE_HOST=engine@203.0.113.10" >&2
  exit 2
fi

# Build the ssh/scp argument list once so both calls authenticate identically.
SSH_ARGS=(-o BatchMode=yes -o ConnectTimeout=10)
if [[ -n "${ENGINE_SSH_KEY:-}" ]]; then
  KEY_PATH="${ENGINE_SSH_KEY/#\~/$HOME}"
  if [[ ! -f "$KEY_PATH" ]]; then
    echo "error: ENGINE_SSH_KEY points at $KEY_PATH, which does not exist" >&2
    exit 2
  fi
  # IdentitiesOnly stops ssh offering every key in the agent first, which on a
  # box with several keys can exhaust MaxAuthTries before reaching this one.
  SSH_ARGS+=(-i "$KEY_PATH" -o IdentitiesOnly=yes)
fi
# BatchMode above is deliberate: this runs unattended from a laptop script, and
# a password prompt would hang it rather than fail it.

echo "==> target: $ENGINE_HOST:$ENGINE_PATH"
echo "==> local token: $LOCAL_TOKEN"

echo "==> checking connectivity"
if ! ssh "${SSH_ARGS[@]}" "$ENGINE_HOST" true 2>/dev/null; then
  echo "error: cannot ssh to $ENGINE_HOST without a prompt." >&2
  echo "       check ENGINE_HOST, ENGINE_SSH_KEY, and that the key is authorized." >&2
  exit 1
fi
echo "    ok"

# Verify the destination BEFORE asking for a token: a wrong ENGINE_PATH would
# otherwise scp a brokerage token to an arbitrary directory on the server.
echo "==> checking the remote repo and data directory"
REMOTE_CHECK="$(ssh "${SSH_ARGS[@]}" "$ENGINE_HOST" "
  set -e
  [[ -d '$ENGINE_PATH/.git' ]] || { echo 'no-repo'; exit 0; }
  [[ -d '$ENGINE_PATH/data' ]] || { echo 'no-data-dir'; exit 0; }
  echo ok
" 2>/dev/null || echo unreachable)"

case "$REMOTE_CHECK" in
  ok) echo "    ok" ;;
  no-repo)
    echo "error: $ENGINE_PATH on $ENGINE_HOST is not a git repo — wrong ENGINE_PATH?" >&2
    exit 1 ;;
  no-data-dir)
    echo "error: $ENGINE_PATH/data does not exist on the VM. Run: mkdir -p data/logs" >&2
    exit 1 ;;
  *)
    echo "error: could not inspect $ENGINE_PATH on $ENGINE_HOST" >&2
    exit 1 ;;
esac

if [[ ! -f "$LOCAL_TOKEN" ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "==> [dry-run] no local token at $LOCAL_TOKEN — nothing would be sent"
    echo "    (that is the only remaining step; the server side checks out)"
    exit 0
  fi
  echo "error: no token at $LOCAL_TOKEN" >&2
  echo "       run: python scripts/schwab_auth.py" >&2
  exit 1
fi

# Refuse to ship a token that is already dead — better to notice here than to
# discover it from a failed snapshot job at 4:30pm, which under D7 is a
# permanently missing day.
if ! python3 "$REPO_ROOT/scripts/schwab_auth.py" --status; then
  echo "error: local token is expired or expiring; re-auth before syncing." >&2
  exit 1
fi

if [[ $DRY_RUN -eq 1 ]]; then
  echo "==> [dry-run] would scp $LOCAL_TOKEN -> $ENGINE_HOST:$ENGINE_PATH/data/schwab_token.json.tmp"
  echo "==> [dry-run] would then chmod 600 and atomically mv it into place"
  echo "==> [dry-run] nothing was sent"
  exit 0
fi

echo "==> syncing $LOCAL_TOKEN -> $ENGINE_HOST:$ENGINE_PATH/data/"

# TEMP-THEN-RENAME. scp writes incrementally, so copying straight onto the live
# path would let a snapshot job reading the token mid-transfer see a truncated
# file and fail. `mv` within one filesystem is atomic, so the job sees either
# the old token or the new one and never a half-written one.
scp -q "${SSH_ARGS[@]}" "$LOCAL_TOKEN" "$ENGINE_HOST:$ENGINE_PATH/data/schwab_token.json.tmp"
ssh "${SSH_ARGS[@]}" "$ENGINE_HOST" "
  set -e
  chmod 600 '$ENGINE_PATH/data/schwab_token.json.tmp'
  mv '$ENGINE_PATH/data/schwab_token.json.tmp' '$ENGINE_PATH/data/schwab_token.json'
"

echo "==> done. verifying on the VM:"
ssh "${SSH_ARGS[@]}" "$ENGINE_HOST" \
  "cd '$ENGINE_PATH' && .venv/bin/python scripts/schwab_auth.py --status"
