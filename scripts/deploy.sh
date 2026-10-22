#!/usr/bin/env bash
# Deploy the current GitHub main onto this machine (D30/D31).
#
#   ./scripts/deploy.sh              # pull, migrate, validate, preflight
#   ./scripts/deploy.sh --check      # validate and preflight only, pull nothing
#   ./scripts/deploy.sh --no-pull    # deploy what is already checked out
#
# RUNS ON THE ENGINE HOST, not from a laptop. It is the counterpart to
# scripts/sync_token.sh: that one pushes a credential to the VM, this one is
# what the VM runs to catch up with the repo.
#
# **IT WRITES NOTHING TO THE MARKET DATA AND MAKES NO NETWORK CALLS TO A
# PROVIDER.** Every step is either local (git, pip, schema) or a read-only
# check. There is no ALLOW_LIVE_CALLS here on purpose (D27): a deploy that
# quietly started ingesting would make "did the deploy work" and "did tonight's
# data arrive" the same question, and they have different answers and different
# fixes. The ingestion catch-up is a separate, deliberate runbook — see
# README.md "Deep history, once (O7)".
#
# Ordering is not a preference:
#
#   1. git pull        — nothing below this means anything against stale code.
#   2. dependencies    — a migration or a job may need a package the lock added.
#   3. db.init         — migrations BEFORE schema.sql, which db/init.py enforces;
#                        and before config validation, because a cross-file
#                        invariant can read the schema.
#   4. config validate — a bad [OPEN] value must fail here, not at 16:30.
#   5. preflight       — the orchestrator's own checks, so this reports exactly
#                        what tonight's run will see rather than a second
#                        opinion about it.
#
# Exit code is the number of hard errors found, so cron or a human can branch
# on it. A preflight SKIP is not an error: "Schwab is not provisioned" is a
# known state, and treating it as a failure trains the operator to ignore the
# one that matters.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DO_PULL=1
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --check)   CHECK_ONLY=1; DO_PULL=0 ;;
    --no-pull) DO_PULL=0 ;;
    -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# The interpreter: the VM's virtualenv if there is one, else whatever python3
# is on PATH (which is the Codespace case, where this runs as a rehearsal).
PYTHON="$REPO_ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

ERRORS=0
step() { printf '\n==> %s\n' "$1"; }
fail() { printf '    FAILED: %s\n' "$1"; ERRORS=$((ERRORS + 1)); }
ok()   { printf '    ok%s\n' "${1:+: $1}"; }

printf '====================================================================\n'
printf 'DEPLOY  %s\n' "$(date -u '+%Y-%m-%d %H:%M:%SZ')"
printf 'host    %s\n' "$(hostname)"
printf 'repo    %s\n' "$REPO_ROOT"
printf 'python  %s (%s)\n' "$PYTHON" "$("$PYTHON" --version 2>&1)"
printf '====================================================================\n'

# ── 1. code ─────────────────────────────────────────────────────────────────
BEFORE="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [[ $DO_PULL -eq 1 ]]; then
  step "pulling main"
  if [[ -n "$(git status --porcelain)" ]]; then
    # Refusing is the right default on a server: a local edit here is either a
    # hotfix somebody forgot to push or a mistake, and both want a human.
    fail "working tree is dirty — commit, stash or discard before deploying"
    git status --short | sed 's/^/      /'
  elif git pull --ff-only 2>&1 | sed 's/^/    /'; then
    ok
  else
    fail "git pull --ff-only (diverged? run git log --oneline HEAD..origin/main)"
  fi
fi
AFTER="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
printf '    %s -> %s  (%s)\n' "$BEFORE" "$AFTER" "$(git log -1 --format='%s' 2>/dev/null | cut -c1-60)"

# ── 2. dependencies ─────────────────────────────────────────────────────────
if [[ $CHECK_ONLY -eq 0 && "$BEFORE" != "$AFTER" ]]; then
  step "installing dependencies"
  if "$PYTHON" -m pip install -q -r requirements.txt 2>&1 | sed 's/^/    /'; then
    ok
  else
    fail "pip install -r requirements.txt"
  fi
fi

# ── 3. schema ───────────────────────────────────────────────────────────────
step "database: migrations then schema (db.init)"
if [[ $CHECK_ONLY -eq 1 ]]; then
  if "$PYTHON" -m db.init --check 2>&1 | sed 's/^/    /'; then ok "schema is current"; else fail "db.init --check"; fi
else
  if "$PYTHON" -m db.init 2>&1 | tail -20 | sed 's/^/    /'; then ok; else fail "db.init"; fi
fi

# ── 4. config ───────────────────────────────────────────────────────────────
step "config validation (every cross-file invariant)"
if "$PYTHON" -m core.config 2>&1 | tail -15 | sed 's/^/    /'; then ok; else fail "config is invalid"; fi

# ── 5. preflight ────────────────────────────────────────────────────────────
# The orchestrator's OWN checks, so this reports what tonight's run will see.
step "pipeline preflight (no network, nothing written)"
if "$PYTHON" -m jobs.daily_close --dry-run 2>&1 | sed 's/^/    /'; then ok; else fail "preflight"; fi

# ── summary ─────────────────────────────────────────────────────────────────
step "state"
"$PYTHON" - <<'PY' 2>&1 | sed 's/^/    /' || true
from core import clock, db
from core.budget import DayBudget

day = clock.session_date()
def one(sql, params=None):
    row = db.fetch_one(sql, params)
    return row or {}

print(f"session_date (ET)   {day}")
print(f"active universe     {len(db.active_symbols())} symbols"
      f" + {len(db.benchmark_symbols())} benchmark(s)")
bars = one("SELECT count(*) n, min(session_date) lo, max(session_date) hi FROM prices_daily")
print(f"prices_daily        {bars.get('n', 0):,} bars  {bars.get('lo')} .. {bars.get('hi')}")
acts = one("SELECT count(*) n, min(ex_date) lo FROM corporate_actions")
print(f"corporate_actions   {acts.get('n', 0):,} rows  back to {acts.get('lo')}")
earn = one("SELECT count(*) n, min(event_date) lo, max(event_date) hi FROM events "
           "WHERE event_type = 'earnings'")
print(f"earnings dates      {earn.get('n', 0):,}  {earn.get('lo')} .. {earn.get('hi')}")
iv = one("SELECT count(*) n, max(session_date) hi FROM iv_daily")
print(f"iv_daily (D7 clock) {iv.get('n', 0):,} rows, latest {iv.get('hi')}")
est = one("SELECT count(*) n, min(snapshot_date) lo FROM estimate_snapshots")
print(f"estimate snapshots  {est.get('n', 0):,} rows, clock started {est.get('lo')}")
book = one("SELECT count(*) n, count(*) FILTER (WHERE outcome = 'open') AS open FROM shadow_book")
print(f"shadow book         {book.get('n', 0):,} calls, {book.get('open', 0)} open")
budget = DayBudget.for_fmp()
print(f"FMP budget today    {budget.spent(day)} spent / {budget.budget}")
status = one("SELECT day_complete FROM v_day_status WHERE session_date = %s", (day,))
print(f"day_complete({day}) {status.get('day_complete')}")
PY

printf '\n====================================================================\n'
if [[ $ERRORS -eq 0 ]]; then
  printf 'DEPLOY OK at %s — preflight skips above are known states, not errors.\n' "$AFTER"
else
  printf 'DEPLOY FOUND %d ERROR(S) at %s — fix before the 16:30 run.\n' "$ERRORS" "$AFTER"
fi
printf '====================================================================\n'
exit "$ERRORS"
