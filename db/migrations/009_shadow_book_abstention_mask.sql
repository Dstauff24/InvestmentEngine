-- D41: which pillars actually scored when this idea was called.
--
-- The composite is computed over scoring pillars with the weights
-- renormalized, so a composite of 72 built from four pillars and one built
-- from two are different claims — and nothing in the number itself says which.
-- Without this column, D39's calibration curve would average them, and the
-- question D41 explicitly defers ("are thinner composites less reliable?")
-- could never be asked of the data.
--
-- Two columns rather than one: `scored_pillars` says WHICH (a reader wants to
-- know it was Technical and Catalyst, not just "two of them"), and
-- `pillars_scored` is what a GROUP BY needs. Deriving the count in SQL from a
-- text list every time a calibration query runs is the kind of friction that
-- stops the query being written.
--
-- Nullable: rows predating the scoring layer never had pillars at all.

ALTER TABLE shadow_book
    ADD COLUMN IF NOT EXISTS scored_pillars TEXT,
    ADD COLUMN IF NOT EXISTS pillars_scored SMALLINT
        CHECK (pillars_scored IS NULL OR pillars_scored BETWEEN 0 AND 4);

CREATE INDEX IF NOT EXISTS ix_shadow_book_pillars
    ON shadow_book (pillars_scored, recipe, called_on DESC);

COMMENT ON COLUMN shadow_book.scored_pillars IS
    'D41 abstention mask: comma-separated pillar letters that SCORED when this '
    'idea was called, e.g. "A,B,C". Technical (A) is always present — D41 '
    'requires it for an idea to exist. An abstaining pillar neither agreed nor '
    'conflicted.';
COMMENT ON COLUMN shadow_book.pillars_scored IS
    'How many of the four pillars scored. D39 calibration groups on this: a '
    'composite of 72 from two pillars is not the same claim as 72 from four.';
