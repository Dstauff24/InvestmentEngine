-- `v_day_status` must judge each job by its LATEST run, not by every row ever
-- written for the day.
--
-- Found live 2026-10-21. A `daily_close` was killed by a timeout partway
-- through, leaving `fmp_estimates` abandoned and `schwab_chains` partial. The
-- re-run succeeded at every single step — estimates took its first snapshot,
-- chains wrote 39,046 rows — and `day_complete` stayed FALSE, because the view
-- counted the superseded rows from the killed attempt.
--
-- The consequence is not cosmetic. `day_complete` can never be recovered by
-- re-running: one transient provider blip permanently poisons that date. Gate 4
-- starts the D7 clock on the FIRST `day_complete = true`, so a bad first night
-- would have meant the clock never started and the operator re-running the job
-- correctly, seeing every step green, and being told the day had failed.
--
-- Retry history is NOT discarded: every attempt stays in `job_runs`, which is
-- the audit trail. The view is the day's STATUS, and the status of a job that
-- failed and then succeeded is "succeeded". A new `attempts` column keeps the
-- retries visible so a day that needed three goes does not look serene.
--
-- `partial` deliberately still fails the day for a critical job: an incomplete
-- snapshot is a snapshot we do not have (D7/D22), and 431 of 433 chains is a
-- permanent hole in the series, not a rounding error.

CREATE OR REPLACE VIEW v_day_status AS
WITH latest AS (
    -- One row per (day, job): the most recent attempt. `id` breaks ties when
    -- two runs share a started_at, which the clock's resolution allows.
    SELECT DISTINCT ON (session_date, job_name)
           session_date, job_name, status, is_critical
    FROM   job_runs
    ORDER  BY session_date, job_name, started_at DESC, id DESC
),
counts AS (
    SELECT session_date, count(*) AS attempts
    FROM   job_runs
    GROUP  BY session_date
)
-- Column ORDER matters: `CREATE OR REPLACE VIEW` may only append columns, so
-- `attempts` goes last rather than beside the other counts where it reads
-- better. Postgres rejects the tidier arrangement outright.
SELECT l.session_date,
       count(*)                                                        AS jobs_run,
       count(*) FILTER (WHERE l.status = 'failed')                      AS jobs_failed,
       count(*) FILTER (WHERE l.status = 'running')                     AS jobs_running,
       count(*) FILTER (WHERE l.is_critical)                            AS critical_jobs,
       count(*) FILTER (WHERE l.is_critical AND l.status = 'success')    AS critical_ok,
       count(*) FILTER (WHERE l.is_critical) > 0
         AND count(*) FILTER (WHERE l.is_critical AND l.status <> 'success') = 0
                                                                        AS day_complete,
       c.attempts
FROM   latest l
JOIN   counts c USING (session_date)
GROUP  BY l.session_date, c.attempts;

COMMENT ON VIEW v_day_status IS
    'Per-day job status, judged by each job''s LATEST run so a successful retry '
    'recovers the day. Every attempt remains in job_runs; `attempts` exposes the '
    'retry count. A critical job that is partial or skipped still fails the day.';
