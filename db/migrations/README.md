# Migrations

`db/schema.sql` is the canonical definition of a **fresh** database. Once a table
holds production data, do not change an existing column there — add a numbered
file here, then mirror the end state back into `schema.sql` so a fresh install
still comes out identical.

Naming: `NNN_short_description.sql`, e.g. `001_add_short_interest.sql`.

Rules enforced by `db/init.py`:

- Files are applied once, in filename order, each inside its own transaction.
- The applied filename plus a SHA-256 of its contents go into `schema_migrations`.
- Editing a file that has already been applied is a hard error. Write a new one.
