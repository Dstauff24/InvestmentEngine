-- D49 correction: the OTHER provider's date for the same report.
--
-- The reaction window assumes that for a report dated T, the actual is public
-- by the close of T+1. That holds when T is the true release date or LATER —
-- but not when it is earlier. If FMP dates a report a day early, the true
-- release can fall after the close of T+1, and the engine would read an actual
-- that was not yet public. The window absorbs a late date and not an early
-- one, so the effective T must be the LATER of the dates we hold.
--
-- Measured 2026-10-22: FMP and Finnhub date the same report one day apart for
-- a large fraction of symbols (ADI 11-23 vs 11-24, CRM 12-01 vs 12-02, CIEN
-- 12-09 vs 12-10) — the classic after-close convention disagreement, where one
-- provider files the release day and the other the reaction day.
--
-- ONE ROW PER REPORT, with a second opinion attached. FMP owns what an
-- earnings row IS (D47); a second provider INSERTING dates would double every
-- report in `days_since_earnings` and leave nothing to arbitrate. So the
-- disagreement is a column, not a row.
--
-- `event_time` belongs to `alt_event_date` when that is the later date (it is
-- Finnhub that publishes the hour). When FMP's own date is the later one,
-- nobody has published a time for it and the release time stays unknown —
-- `features/catalyst.effective_report` encodes exactly that.

ALTER TABLE events
    ADD COLUMN IF NOT EXISTS alt_event_date DATE,
    ADD COLUMN IF NOT EXISTS alt_source     TEXT;

COMMENT ON COLUMN events.alt_event_date IS
    'Another provider''s date for the SAME report, when it disagrees. The '
    'effective report date (D49) is the LATER of this and `event_date`: the '
    'reaction window absorbs a date that is too late, but a date that is too '
    'early would let the engine read an actual before it was published.';
COMMENT ON COLUMN events.alt_source IS
    'Who supplied `alt_event_date` (currently always finnhub).';

-- The knowability read is per symbol and date, and now has to see both.
CREATE INDEX IF NOT EXISTS ix_events_alt_date
    ON events (symbol, event_type, alt_event_date)
    WHERE alt_event_date IS NOT NULL;
