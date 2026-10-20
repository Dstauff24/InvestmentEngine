# Investment Decision Engine

Personal investment decision-support engine for a Schwab brokerage account
(swings, long holds, options). Ingests quantitative + qualitative data, scores
tickers through a 4-pillar confluence model, surfaces complete trade plans in
two tiers, tracks every idea in a Shadow Book, and emails a daily digest.
**The human makes every final decision.**

Phase 1 is the brain: no UI, no order placement, read-only broker access.

- Conventions and validated API findings: [CLAUDE.md](CLAUDE.md)
- Canonical decision log D1–D31 + go-live checklist: [docs/engine-decisions-and-todo.md](docs/engine-decisions-and-todo.md)
- Provisional config values: [docs/open-parameters.md](docs/open-parameters.md)
- Rebuild work order: [docs/REBUILD_KICKOFF.md](docs/REBUILD_KICKOFF.md)

> **Process rule (D30):** commit and push after every completed step. GitHub is
> the system of record; a Codespace is disposable. The first version of this
> repo was lost to an unpushed Codespace.

---

## Repo layout

```
config/        scoring.yaml, risk.yaml, universe.yaml, watchlist.txt, constituents_static.txt
core/          settings.py (sole reader of os.environ), config.py, db.py, clock.py,
               ratelimit.py, jobs.py, netguard.py
db/            schema.sql, init.py, migrations/
ingest/        fmp_client.py, universe.py, fmp_fundamentals.py, fmp_estimates.py, fmp_events.py,
               finnhub_client.py, finnhub_news.py, schwab_client.py, schwab_prices.py,
               schwab_chains.py, runner.py
features/      adjustment.py, iv.py
scoring/       pillars.py, composite.py, recipes/
decision/      idea_builder.py, tiering.py, risk_manager.py
shadow_book/   logger.py, outcome_tracker.py, analytics.py
alerts/        mailer.py, email_digest.py
jobs/          daily_close.py
backtest/      (Phase 2)
scripts/       fmp_probe.py, schwab_auth.py, sync_token.sh, backup_db.sh, token_expiry_check.py
tests/         fixture-driven; no network, no DB by default
docs/          decision log, open parameters, rebuild kickoff
data/          runtime artifacts (Schwab token, dumps, logs) — gitignored
```

`core/` holds everything more than one job needs: the rate limiter, the
`job_runs` writer, and the network consent gate all have exactly one home.

---

## Setup — fresh Ubuntu 24.04 VM

Target host is a small cloud VM (Hetzner CX22 / DigitalOcean 1 GB), per D21.

### 1. Base packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git curl gnupg lsb-release \
                    ca-certificates postgresql-common awscli
```

### 2. Timezone

Set the **host** clock to Eastern so cron fires at a stable time relative to the
market close across DST changes. Everything stored in the database stays UTC
(CLAUDE.md: dual time keying).

```bash
sudo timedatectl set-timezone America/New_York
timedatectl   # verify
```

### 3. Postgres 16 + TimescaleDB (D20)

```bash
# PGDG repo
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y

# TimescaleDB repo
sudo sh -c 'echo "deb https://packagecloud.io/timescale/timescaledb/ubuntu/ \
  $(lsb_release -cs) main" > /etc/apt/sources.list.d/timescaledb.list'
curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey \
  | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/timescaledb.gpg

sudo apt update
sudo apt install -y postgresql-16 timescaledb-2-postgresql-16 postgresql-client-16

# Tune for this box and enable the preload library
sudo timescaledb-tune --quiet --yes
sudo systemctl restart postgresql
```

Create the role and database:

```bash
sudo -u postgres psql <<'SQL'
CREATE ROLE engine LOGIN PASSWORD 'REPLACE_WITH_PGPASSWORD';
CREATE DATABASE investment_engine OWNER engine;
-- Scratch database for the destructive integration suite. Create it even on the
-- VM: it is what keeps `pytest -m integration` away from collected history.
CREATE DATABASE ie_test OWNER engine;
SQL
```

> Postgres **15 or newer** is required: the schema uses
> `UNIQUE NULLS NOT DISTINCT`. The `timescaledb` extension itself is created by
> `db/init.py`, which is idempotent and safe to re-run.

### 4. Application

**The VM's distro Python is the reference interpreter.** Create the venv with it
explicitly (`/usr/bin/python3` — 3.12 on Ubuntu 24.04), not with whatever newer
build a dev machine or Codespace happens to have on `PATH`. Ruff targets `py311`
so lint catches syntax the VM could not run, but only an explicit interpreter
keeps the installed wheels identical to what production resolves.

```bash
git clone https://github.com/Dstauff24/InvestmentEngine ~/engine && cd ~/engine
/usr/bin/python3 -m venv .venv            # distro Python, not a newer one on PATH
source .venv/bin/activate
python --version                          # confirm it is the distro version
pip install -r requirements.txt          # add -r requirements-dev.txt on a dev box

cp .env.example .env && chmod 600 .env
$EDITOR .env                             # DB creds, FMP, Finnhub, SMTP, Schwab
mkdir -p data/logs
```

Every variable is documented in [.env.example](.env.example). **Never commit
`.env` or the Schwab token file** — `.gitignore` blocks both.

```bash
python -m core.config      # validate config/*.yaml before anything else
python -m db.init          # create/upgrade the schema (idempotent)
python -m db.init --check  # verify only; exits 1 if anything is missing
```

### 5. Populate the universe source

While FMP's constituents endpoints are plan-gated (D29), the universe builds
from [config/constituents_static.txt](config/constituents_static.txt). That file
ships empty with sourcing instructions and a dated header; **populate it before
gate 1a** or the universe builder will refuse rather than write an empty
universe.

### 6. Backups

Nightly `pg_dump`, gzipped, to S3-compatible object storage (D20). Set
`BACKUP_S3_BUCKET` and the `AWS_*` credentials in `.env`.

```bash
./scripts/backup_db.sh --dry-run   # show what it would do
./scripts/backup_db.sh             # dump, upload, prune to BACKUP_RETENTION_DAYS
```

This is more than hygiene. The D7 snapshot history cannot be re-fetched from any
vendor at any price, so the backup is the only copy of the engine's edge. The
script refuses to upload or prune a dump that is empty or missing any of the four
hypertables.

**Restoring** — the `pre_restore`/`post_restore` pair suspends TimescaleDB's
background workers so the restore cannot race them:

```bash
createdb -O engine investment_engine
psql -d investment_engine -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
psql -d investment_engine -c "SELECT timescaledb_pre_restore();"
gunzip -c investment_engine-<stamp>.dump.gz | psql -d investment_engine
psql -d investment_engine -c "SELECT timescaledb_post_restore();"
python -m db.init --check
```

---

## Schwab authentication — run on the LAPTOP, not the VM (D21 / O8)

The OAuth flow needs an interactive browser, so the token is minted on a machine
that has one and synced to the VM. Schwab's **refresh token lives 7 days**, which
makes this a weekly chore rather than one-time setup.

```bash
# on the laptop, with SCHWAB_APP_KEY / SCHWAB_APP_SECRET / SCHWAB_CALLBACK_URL set
python scripts/schwab_auth.py            # writes data/schwab_token.json (chmod 600)
python scripts/schwab_auth.py --status   # token age and time remaining
./scripts/sync_token.sh                  # copy it to the VM
```

The script picks its flow from the callback URL:

| `SCHWAB_CALLBACK_URL` | Flow | What happens |
|---|---|---|
| `https://127.0.0.1` (no port) | **manual** | prints a link; you paste back the redirected URL |
| `https://127.0.0.1:8182` | **server** | binds that port and catches the redirect itself |

Override with `--manual` or `--server`. The configured callback is port-less, so
**manual is the default and the settled mechanism** — there is no port for a
local redirect server to bind, and forcing `--server` fails with
`RedirectServerExitedError`. The manual flow works anywhere: a laptop, an SSH
session, a Codespace.

### The manual flow, step by step

1. Run `python scripts/schwab_auth.py`. It prints an authorization link.
2. Open the link in any browser, log in, complete 2FA, and click **Allow**.
3. The browser lands on `https://127.0.0.1/...`, which **looks like an error
   page**. That is expected — nothing is listening there. The URL in the address
   bar is still exactly what is needed.
4. Copy the **entire** address, including everything after the `?` (that is where
   the `code` parameter lives), and paste it at the `Redirect URL>` prompt.

> **The pasted URL is a secret.** It carries a single-use authorization code that
> can be exchanged for a token. schwab-py reads it straight from the prompt; it is
> never echoed and never logged, error output is redacted, and HTTP loggers are
> clamped during auth so a verbose run cannot dump the token exchange. Do not
> paste it into a chat, a ticket, or any shell that records history. It expires
> within a couple of minutes, so finish promptly.

`scripts/token_expiry_check.py` emails `ALERT_EMAIL_ONFAIL` 24h before expiry. A
lapsed token means a missed D7 snapshot, and a missed day cannot be bought back.

---

## Running

```bash
source .venv/bin/activate

python -m core.config                 # validate config, print a summary
python -m db.init --check             # schema health
python -m core.jobs --status          # today's job_runs; is the day complete?
python -m core.jobs --clear-stale     # release runs abandoned by a hard kill

# Anything that reaches a provider needs explicit consent (D27)
ALLOW_LIVE_CALLS=1 python scripts/fmp_probe.py          # what does the plan cover?
ALLOW_LIVE_CALLS=1 python -m ingest.universe            # rebuild the universe (D14)
ALLOW_LIVE_CALLS=1 python -m ingest.universe --dry-run  # report only, write nothing
ALLOW_LIVE_CALLS=1 python -m jobs.daily_close           # the full post-close pipeline
```

Run `ingest.fmp_events --backfill-actions` once before the first backtest:
`corporate_actions` must reach back at least as far as the price history the
backtest reads, or `features/adjustment.py` produces a wrong total-return series
for every earlier bar — silently, with no error (D24, O7).

Every job writes a row to `job_runs` (start, end, status, rows_written, error).
The orchestrator refuses to mark a day complete if a critical snapshot job failed.

---

## Cron (post-close, trading days)

`crontab -e` as the app user. The host clock is Eastern (§2), so these times are ET.

```cron
# ── Investment Decision Engine ────────────────────────────────────────────────
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
ENGINE=/home/engine/engine
MAILTO=you@example.com

# Post-close pipeline. 16:30 ET Mon-Fri. ONE entry for the whole pipeline:
# dependency order lives in jobs/daily_close.py, so a half-run day is impossible
# and job_runs keeps a single coherent story. Exit 1 (a failed step, or a day
# left incomplete) also emails ALERT_EMAIL_ONFAIL.
30 16 * * 1-5  cd $ENGINE && ALLOW_LIVE_CALLS=1 .venv/bin/python -m jobs.daily_close >> data/logs/daily_close.log 2>&1

# Universe rebuild — Sunday 08:00 ET. Weekly, not daily: index membership and
# 20-day average volume do not move overnight, and every other table FKs to
# `tickers`, so rebuilding mid-week under a bad provider response is costly.
0 8 * * 0      cd $ENGINE && ALLOW_LIVE_CALLS=1 .venv/bin/python -m ingest.universe >> data/logs/universe.log 2>&1

# Database backup — 22:00 ET daily, after the pipeline has written the day.
0 22 * * *     cd $ENGINE && ./scripts/backup_db.sh >> data/logs/backup.log 2>&1

# Schwab token expiry warning — 09:00 ET daily. Daily, not weekly: the token
# lasts 7 days and the warning must land 24h+ before it dies, whichever weekday
# that falls on. The alert de-dupes, so this is one email, not seven.
0 9 * * *      cd $ENGINE && .venv/bin/python -m scripts.token_expiry_check >> data/logs/token.log 2>&1
```

Notes on the schedule:

- **`ALLOW_LIVE_CALLS=1` is required** on any entry that reaches a provider
  (D27). Consent is per-invocation and belongs here, visible in the crontab,
  rather than as a standing grant in `.env`.
- **16:30 ET** gives the close half an hour to settle. Market holidays are not
  handled by a calendar: a holiday produces jobs that write zero rows, which is
  visible in `job_runs` and harmless.
- The orchestrator releases abandoned `running` rows from a previous crash before
  starting, so a hard kill does not wedge the next night's run.
- `mkdir -p data/logs` first; `data/` is gitignored.

---

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check .          # lint
ruff format .         # format
pytest                # unit tests — fixture-driven, no network, no DB

# Integration tests TRUNCATE the tables they touch, so they get their own
# database. Note the per-invocation PGDATABASE: no override flag is needed,
# because `ie_test` is a scratch-looking name.
PGDATABASE=ie_test pytest -m integration
```

### Two databases, separated by protection class

| Database | Holds | The suite |
|---|---|---|
| `investment_engine` | live collected data (D7 history) | **refuses to run** — the name is PROTECTED in `tests/dbguard.py`, and `INTEGRATION_DB_OK=1` does **not** unlock it |
| `ie_test` | nothing of value | truncates freely, no override needed |

`PGDATABASE` in `.env` points at `investment_engine` in development as well as
on the VM, so ordinary work touches the real database and the destructive suite
has to be pointed elsewhere deliberately.

This is not theoretical tidiness. On 2026-10-20 both roles were served by one
database, and a reflexive `INTEGRATION_DB_OK=1 pytest -m integration` wiped a
day of live collection mid-session. An override that gets typed from muscle
memory is not a protection, so the live name is now refused outright rather than
merely unmatched by the scratch pattern. If you genuinely need to reset the
collection database, do it by hand, where the decision is visible.

Rules the test suite enforces, and reviewers should hold the line on:

- No hard-coded weights, thresholds, or risk numbers — `config/*.yaml` only (D25).
- Ingestion is idempotent: re-running a day upserts, never duplicates.
- The four history tables are **append-only**. A static test fails the build on
  any `DELETE`/`TRUNCATE`/`DROP` against them, and requires every `INSERT` to
  carry `ON CONFLICT … DO UPDATE`. D7's edge is the day-over-day diff; a deleted
  day cannot be re-fetched from any vendor at any price.
- `prices_daily` stores raw unadjusted bars; adjusted series are computed at read
  time from `corporate_actions` (D24).
- All timestamps stored UTC; displayed in America/New_York.
- **No network calls in tests.** Enforced by `core/netguard.py`, not trusted:
  every client refuses without `ALLOW_LIVE_CALLS=1`, and inside pytest it refuses
  even with it.
