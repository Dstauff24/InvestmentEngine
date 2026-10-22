"""Static checks on the ops scripts and the mailer's never-raise contract.

The live paths (sending mail, scp, pg_dump) are exercised by hand and by the
step report; what is pinned here are the properties that are easy to break by
accident and expensive to discover in production at 4:30pm.
"""

from __future__ import annotations

import smtplib
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
BACKUP = SCRIPTS / "backup_db.sh"


# ══════════════════════════════════════════════════════════════════════════
# backup_db.sh
# ══════════════════════════════════════════════════════════════════════════


def test_it_does_not_source_dotenv():
    """`source .env` executes the file as bash: a value containing parentheses
    either breaks the script or runs as code. SEC_EDGAR_USER_AGENT in
    .env.example contains parentheses, which is how that was discovered."""
    script = BACKUP.read_text()
    assert "source " not in script.replace("# `source .env`", "")
    assert ". $REPO_ROOT/.env" not in script
    assert "read_env()" in script


def test_it_reads_only_the_keys_it_needs():
    script = BACKUP.read_text()
    for key in ("PGDATABASE", "PGUSER", "BACKUP_S3_BUCKET", "BACKUP_RETENTION_DAYS"):
        assert key in script


def test_a_real_environment_variable_wins_over_dotenv():
    """Matches core/settings.py, so the two cannot disagree about precedence."""
    assert 'if [[ -z "${!key:-}" ]]' in BACKUP.read_text()


def test_it_writes_partial_then_renames():
    """A dump interrupted halfway must never be mistaken for a good backup by
    the pruner, by an uploader, or by a human at 3am."""
    script = BACKUP.read_text()
    assert "$DUMP_PATH.partial" in script
    assert 'mv "$DUMP_PATH.partial" "$DUMP_PATH"' in script
    # The rename must come after the gzip completes, not before.
    assert script.index(".partial") < script.index('mv "$DUMP_PATH.partial"')


def test_it_refuses_an_empty_dump():
    script = BACKUP.read_text()
    assert '[[ ! -s "$DUMP_PATH" ]]' in script
    assert "refusing to upload or prune" in script


def test_it_refuses_a_dump_missing_any_hypertable():
    """A misconfigured PGDATABASE must not quietly replace 30 days of good
    backups with 30 days of empty ones."""
    script = BACKUP.read_text()
    for table in ("option_chain_snapshots", "estimate_snapshots", "prices_daily", "iv_daily"):
        assert table in script
    assert "is missing $table" in script


def test_the_hypertable_check_decompresses_once():
    """The per-table loop was broken under `set -o pipefail`: grep exits on its
    first match, gunzip takes SIGPIPE and returns 141, and the pipeline reports
    failure — racily, depending on where the table sits in the dump."""
    script = BACKUP.read_text()
    assert "grep -oE 'CREATE TABLE public" in script
    assert "DUMPED_TABLES=" in script
    # The broken shape must not come back.
    assert 'gunzip -c "$DUMP_PATH" | grep -q' not in script


def test_retention_defaults_to_thirty_days():
    assert "BACKUP_RETENTION_DAYS:-30" in BACKUP.read_text()


def test_pruning_is_scoped_to_our_own_filename_pattern():
    """A bucket or directory shared with anything else must stay untouched."""
    script = BACKUP.read_text()
    assert "${PGDATABASE}-*.dump.gz" in script
    assert 'db"-.*\\\\.dump\\\\.gz$"' in script or '"^"db"-' in script


def test_the_restore_procedure_is_documented_with_the_timescale_pair():
    """A plain restore happens to work on an uncompressed database; it must not
    be relied on once chunks start compressing."""
    script = BACKUP.read_text()
    assert "timescaledb_pre_restore()" in script
    assert "timescaledb_post_restore()" in script
    assert "db.init --check" in script


def test_no_bucket_keeps_local_dumps_and_does_not_fail():
    """With no object store configured, local dumps + retention is a SUPPORTED mode.

    The droplet takes provider-level snapshots as the outer layer, so local
    dumps are the fast-restore tier rather than the only copy. Failing the
    nightly job because a bucket is absent would turn a deliberate
    configuration into a red cron mail every morning.

    This test replaces one that asserted the string "not a backup" was present.
    That assertion passed by ACCIDENT after the warning was reworded — it was
    matching an unrelated comment about hypertables further down the file, so a
    test named for the local-only warning was checking nothing to do with it.
    """
    script = BACKUP.read_text()

    # The no-bucket branch must not bail out.
    branch = script.split("elif [[ $UPLOAD -eq 1 ]]; then", 1)[1].split("\nfi", 1)[0]
    assert "exit 1" not in branch, "an absent bucket must not fail the nightly job"
    assert "retention" in branch.lower(), "the branch should say retention still applies"

    # ...and the local prune must sit OUTSIDE the upload conditional, or dumps
    # would accumulate for ever in exactly the configuration that keeps them.
    prune = script.index("pruning local dumps older than")
    upload_block_end = script.index("elif [[ $UPLOAD -eq 1 ]]; then")
    assert prune > upload_block_end, "local pruning must not be nested in the S3 branch"


def test_the_hypertable_check_refuses_an_incomplete_dump():
    """The other place the script talks about what is not a backup."""
    script = BACKUP.read_text()
    assert "is not a backup of THIS" in script
    assert "refusing to upload or prune" in script


def test_it_uses_strict_bash():
    assert "set -euo pipefail" in BACKUP.read_text()


# ══════════════════════════════════════════════════════════════════════════
# token_expiry_check.py
# ══════════════════════════════════════════════════════════════════════════


def test_the_warning_window_is_twenty_four_hours():
    from ingest.schwab_client import TOKEN_WARN_BEFORE

    assert TOKEN_WARN_BEFORE.total_seconds() == 24 * 3600


def test_a_missing_token_reports_and_exits_one(tmp_path, monkeypatch, capsys):
    from core import settings
    from scripts import token_expiry_check

    monkeypatch.setattr(settings, "schwab_token_path", lambda: tmp_path / "nope.json")
    assert token_expiry_check.main(["--check"]) == 1
    assert "MISSING" in capsys.readouterr().out


def test_a_fresh_token_reports_ok_and_exits_zero(tmp_path, monkeypatch, capsys):
    from core import settings
    from scripts import token_expiry_check

    path = tmp_path / "schwab_token.json"
    path.write_text("{}")
    monkeypatch.setattr(settings, "schwab_token_path", lambda: path)
    assert token_expiry_check.main(["--check"]) == 0
    assert "STATUS  ok" in capsys.readouterr().out


def test_a_token_inside_the_warning_window_reports_a_warning(tmp_path, monkeypatch, capsys):
    import os
    from datetime import UTC, datetime, timedelta

    from core import settings
    from ingest.schwab_client import REFRESH_TOKEN_LIFETIME
    from scripts import token_expiry_check

    path = tmp_path / "schwab_token.json"
    path.write_text("{}")
    # 6.5 days old: inside the last 24h of a 7-day lifetime.
    when = (datetime.now(UTC) - (REFRESH_TOKEN_LIFETIME - timedelta(hours=12))).timestamp()
    os.utime(path, (when, when))
    monkeypatch.setattr(settings, "schwab_token_path", lambda: path)

    assert token_expiry_check.main(["--check"]) == 0  # warned, not yet fatal
    assert "warning" in capsys.readouterr().out


def test_an_expired_token_exits_one(tmp_path, monkeypatch, capsys):
    import os
    from datetime import UTC, datetime, timedelta

    from core import settings
    from scripts import token_expiry_check

    path = tmp_path / "schwab_token.json"
    path.write_text("{}")
    when = (datetime.now(UTC) - timedelta(days=9)).timestamp()
    os.utime(path, (when, when))
    monkeypatch.setattr(settings, "schwab_token_path", lambda: path)

    assert token_expiry_check.main(["--check"]) == 1
    assert "EXPIRED" in capsys.readouterr().out


def test_check_mode_sends_no_email(tmp_path, monkeypatch):
    from core import settings
    from scripts import token_expiry_check

    sent = []
    monkeypatch.setattr(settings, "schwab_token_path", lambda: tmp_path / "nope.json")
    monkeypatch.setattr(token_expiry_check.notifier, "notify", lambda *a, **k: sent.append(a))
    token_expiry_check.main(["--check"])
    assert sent == []


def test_the_message_explains_that_a_missed_snapshot_is_permanent(tmp_path, monkeypatch):
    from core import settings
    from scripts import token_expiry_check

    captured = {}
    monkeypatch.setattr(settings, "schwab_token_path", lambda: tmp_path / "nope.json")
    monkeypatch.setattr(
        token_expiry_check.notifier,
        "notify",
        lambda subject, body, **k: captured.update(subject=subject, body=body) or True,
    )
    token_expiry_check.main([])
    assert "cannot be backfilled" in captured["body"]
    assert "schwab_auth.py" in captured["body"]


# ══════════════════════════════════════════════════════════════════════════
# alerts/mailer.py — the never-raise contract
# ══════════════════════════════════════════════════════════════════════════


def test_send_never_raises_on_an_smtp_failure(monkeypatch):
    """A mail server outage must not turn a job that succeeded into one that
    failed, nor mask the original error in one that did fail."""
    from alerts import mailer

    monkeypatch.setattr(mailer, "_record", lambda *a, **k: -1)  # skip the DB
    monkeypatch.setattr(
        mailer.smtplib,
        "SMTP",
        lambda *a, **k: (_ for _ in ()).throw(smtplib.SMTPException("server down")),
    )
    assert mailer.send("subject", "body", record=True) is False


def test_send_never_raises_on_a_missing_setting(monkeypatch):
    from alerts import mailer

    monkeypatch.setattr(mailer, "_record", lambda *a, **k: -1)
    monkeypatch.setattr(
        mailer.settings,
        "smtp_host",
        lambda: (_ for _ in ()).throw(mailer.settings.SettingsError("SMTP_HOST is not set")),
    )
    assert mailer.send("subject", "body") is False


def test_send_never_raises_on_a_network_error(monkeypatch):
    from alerts import mailer

    monkeypatch.setattr(mailer, "_record", lambda *a, **k: -1)
    monkeypatch.setattr(
        mailer.smtplib, "SMTP", lambda *a, **k: (_ for _ in ()).throw(OSError("unreachable"))
    )
    assert mailer.send("subject", "body") is False


def test_a_duplicate_dedupe_key_suppresses_the_send(monkeypatch):
    """The same failure on three consecutive retries is one email, not three."""
    from alerts import mailer

    sent = []
    monkeypatch.setattr(mailer, "_record", lambda *a, **k: None)  # row already exists
    monkeypatch.setattr(mailer.smtplib, "SMTP", lambda *a, **k: sent.append(1))
    assert mailer.send("subject", "body", dedupe_key="k") is True
    assert sent == []


def test_the_dedupe_key_is_stable_per_job_and_day():
    from datetime import date

    from alerts.mailer import job_failure_key

    assert job_failure_key("schwab_chains", date(2026, 10, 20)) == (
        "job_failure:schwab_chains:2026-10-20"
    )
    assert job_failure_key("a", date(2026, 10, 20)) != job_failure_key("a", date(2026, 10, 21))


def test_the_subject_carries_the_environment():
    """So a dev-box alert is never mistaken for a production one."""
    assert (
        'f"[{settings.engine_env()}] {subject}"'
        in (Path(__file__).resolve().parent.parent / "alerts" / "mailer.py").read_text()
    )


@pytest.mark.parametrize(
    "alert_type",
    ["daily_digest", "job_failure", "token_expiry", "event_radar", "stop_warning"],
)
def test_the_alert_types_used_are_valid_schema_values(alert_type):
    """They must satisfy the alerts table CHECK constraint."""
    schema = (Path(__file__).resolve().parent.parent / "db" / "schema.sql").read_text()
    assert f"'{alert_type}'" in schema


# ══════════════════════════════════════════════════════════════════════════
# deploy.sh
# ══════════════════════════════════════════════════════════════════════════

DEPLOY = SCRIPTS / "deploy.sh"


def test_the_deploy_makes_no_provider_calls():
    """**A DEPLOY THAT INGESTED WOULD MERGE TWO QUESTIONS.** "Did the deploy
    work" and "did tonight's data arrive" have different answers and different
    fixes, so nothing here may touch a provider — which under D27 means the
    consent flag must be absent, not merely unset."""
    lines = [line for line in DEPLOY.read_text().splitlines() if not line.lstrip().startswith("#")]
    granted = [line for line in lines if "ALLOW_LIVE_CALLS" in line]
    assert granted == [], f"deploy.sh must not grant network consent: {granted}"
    for module in ("ingest.universe", "ingest.schwab_prices", "ingest.fmp_history"):
        runs = [line for line in lines if module in line]
        assert runs == [], f"deploy.sh must not run {module}: {runs}"


def test_it_runs_the_orchestrators_own_preflight_rather_than_a_second_opinion():
    """A separate set of checks would drift from the ones that actually gate
    the 16:30 run."""
    text = DEPLOY.read_text()
    assert "jobs.daily_close --dry-run" in text


def test_the_order_is_pull_then_schema_then_config_then_preflight():
    """Each step is meaningless before the one above it: validating config
    against stale code, or reading a schema a migration has not applied yet."""
    text = DEPLOY.read_text()
    positions = [
        text.index("git pull --ff-only"),
        text.index("-m db.init"),
        text.index("-m core.config"),
        text.index("jobs.daily_close --dry-run"),
    ]
    assert positions == sorted(positions)


def test_it_refuses_to_deploy_over_a_dirty_tree():
    """On a server a local edit is either an unpushed hotfix or a mistake, and
    both want a human rather than a silent overwrite."""
    text = DEPLOY.read_text()
    assert "git status --porcelain" in text
    assert "working tree is dirty" in text


def test_it_exits_with_the_error_count_so_cron_can_branch():
    text = DEPLOY.read_text()
    assert 'exit "$ERRORS"' in text


def test_a_preflight_skip_is_not_counted_as_an_error():
    """ "Schwab is not provisioned" is a known state; failing on it would train
    the operator to ignore the alert that matters."""
    text = DEPLOY.read_text()
    assert "skips above are known states" in text


def test_it_prefers_the_virtualenv_but_still_runs_without_one():
    """The VM has a .venv; a Codespace rehearsal does not."""
    text = DEPLOY.read_text()
    assert ".venv/bin/python" in text
    assert "command -v python3" in text


def test_the_deploy_checks_env_against_the_example():
    """A setting added in code must not reach production without its value —
    the 2026-10-22 VM ran an evening with SHADOW_ACCOUNT_VALUE missing."""
    text = DEPLOY.read_text()
    assert "scripts/env_contract.py" in text
    assert "checking .env against .env.example" in text


def test_the_env_check_runs_before_config_validation_and_preflight():
    """A missing variable makes both of those report confusing secondary
    failures, so the primary cause has to be found first."""
    text = DEPLOY.read_text()
    positions = [
        text.index("env_contract.py"),
        text.index("-m core.config"),
        text.index("jobs.daily_close --dry-run"),
    ]
    assert positions == sorted(positions)
