"""Self-check for the performer-PK migration: a legacy state.db (keyed by
(id, store_name)) is rebuilt keyed by (id, store_url) in one transaction, with
its rows copied across. Plain asserts, no framework.

Run: uv run python test_state.py
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from clipstores_scraper import state

# Older schema: store_url is a column but NOT part of the primary key — the exact
# shape _migrate() must detect (perf["store_url"].pk == 0) and rebuild.
_LEGACY = """
CREATE TABLE performer (
    id              TEXT NOT NULL,
    name            TEXT NOT NULL,
    store_url       TEXT NOT NULL,
    store_name      TEXT NOT NULL,
    unmatched_count INTEGER NOT NULL DEFAULT 0,
    catalog_count   INTEGER,
    scraped_at      REAL,
    status          TEXT NOT NULL DEFAULT 'new',
    error           TEXT,
    triaged_at      REAL,
    last_new_clips  INTEGER,
    PRIMARY KEY (id, store_name)
);
"""


def test_migration_rebuilds_and_copies_rows() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "state.db"
        raw = sqlite3.connect(path)
        raw.executescript(_LEGACY)
        raw.execute(
            "INSERT INTO performer (id, name, store_url, store_name, status) "
            "VALUES ('7', 'Bob', 'http://s/7', 'C4S', 'scraped')"
        )
        raw.commit()
        raw.close()

        conn = state.connect(path)  # runs _migrate
        try:
            pk = {
                r["name"]: r["pk"] for r in conn.execute("PRAGMA table_info(performer)")
            }
            assert pk["store_url"] > 0, pk  # store_url is now part of the PK
            assert pk["store_name"] == 0, pk  # store_name no longer keys it
            row = conn.execute(
                "SELECT name, status FROM performer WHERE id = '7'"
            ).fetchone()
            assert row["name"] == "Bob" and row["status"] == "scraped"  # row survived
            # The scratch table from the rebuild is gone.
            left = conn.execute(
                "SELECT name FROM sqlite_master WHERE name = '_performer_old'"
            ).fetchall()
            assert left == [], left
        finally:
            conn.close()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
