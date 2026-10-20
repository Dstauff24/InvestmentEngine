#!/usr/bin/env bash
# Sync the Schwab OAuth token from this laptop to the engine VM (D21 / O8).
#
# O8 is still open on the MECHANISM: this implements the scp option, which is
# the simpler of the two candidates and needs no extra credentials on the VM.
# Replace it if object storage wins, and record the decision.
#
# Before first use set ENGINE_HOST below (or export it), and confirm you can
# `ssh "$ENGINE_HOST"` without a password prompt.
#
#   ./scripts/sync_token.sh
#
set -euo pipefail

ENGINE_HOST="${ENGINE_HOST:-engine@REPLACE_ME}"   # e.g. engine@203.0.113.10
ENGINE_PATH="${ENGINE_PATH:-/home/engine/engine}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_TOKEN="${SCHWAB_TOKEN_PATH:-$REPO_ROOT/data/schwab_token.json}"

if [[ "$ENGINE_HOST" == *REPLACE_ME* ]]; then
  echo "error: set ENGINE_HOST (edit this file or export it) before syncing." >&2
  exit 2
fi

if [[ ! -f "$LOCAL_TOKEN" ]]; then
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

echo "syncing $LOCAL_TOKEN -> $ENGINE_HOST:$ENGINE_PATH/data/"

# TEMP-THEN-RENAME. scp writes incrementally, so copying straight onto the live
# path would let a snapshot job reading the token mid-transfer see a truncated
# file and fail. `mv` within one filesystem is atomic, so the job sees either
# the old token or the new one and never a half-written one.
scp -q "$LOCAL_TOKEN" "$ENGINE_HOST:$ENGINE_PATH/data/schwab_token.json.tmp"
ssh "$ENGINE_HOST" "
  set -e
  chmod 600 '$ENGINE_PATH/data/schwab_token.json.tmp'
  mv '$ENGINE_PATH/data/schwab_token.json.tmp' '$ENGINE_PATH/data/schwab_token.json'
"

echo "done. verifying on the VM:"
ssh "$ENGINE_HOST" "cd '$ENGINE_PATH' && .venv/bin/python scripts/schwab_auth.py --status"
