"""cli/ — operational one-off scripts (run as ``python -m cli.<name>``).

Distinct from ``python -m tailor`` (the product CLI). These are maintenance tools:
e.g. ``migrate_runs`` backfills the SQLite run store from existing on-disk runs
(SPEC_SQLITE_MIGRATION §3)."""
