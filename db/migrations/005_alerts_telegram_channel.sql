-- Telegram joins email as an alert channel (D16, amended 2026-10-21).
--
-- Email is the digest channel and stays so. Telegram is the channel that
-- actually reaches a phone: the engine runs unattended on a VM at 16:30 ET, and
-- a failed SACRED snapshot job (D7/D22) is worth a push notification rather
-- than an email read the next morning. The snapshot history cannot be
-- re-fetched at any price, so the hours between "the job failed" and "someone
-- noticed" are the whole cost.
--
-- Widening a CHECK constraint, not replacing a column: `alerts` rows written
-- before today keep their 'email' channel and stay valid.

ALTER TABLE alerts DROP CONSTRAINT IF EXISTS alerts_channel_check;

ALTER TABLE alerts
    ADD CONSTRAINT alerts_channel_check
    CHECK (channel = ANY (ARRAY['email'::text, 'log'::text, 'telegram'::text]));

COMMENT ON COLUMN alerts.channel IS
    'Where this alert went. One row PER CHANNEL, each with its own dedupe_key, '
    'so a Telegram outage cannot suppress the email and vice versa.';
