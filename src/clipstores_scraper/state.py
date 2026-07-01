"""SQLite state for the dashboard.

The catalog cache already lets a store be scraped once; this adds the *operating*
state on top: which performers exist, their last scrape result, and every
proposed match with its review decision. Persisting decisions means the
dashboard survives restarts and a re-scrape never silently discards work you've
already reviewed or applied.

One file (``state.db`` in the working directory), two tables:
  * ``performer`` -- triage + last-scrape status, one row per (performer, store
    URL). A performer with several storefronts on one site (e.g. multiple C4S
    studios) gets a row each, keyed by URL rather than just store name.
  * ``match``     -- one proposed scene->clip link, with its decision.

Decisions: ``approved`` (high-confidence, auto), ``pending`` (medium, needs a
human), ``rejected``, ``applied`` (written to Stash).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .models import MatchCandidate, PerformerStatus

DB_PATH = Path("state.db")

# Kept as a single statement so the performer-PK migration can recreate the table
# with one conn.execute inside its transaction (executescript force-commits).
_PERFORMER_DDL = """
CREATE TABLE IF NOT EXISTS performer (
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
    PRIMARY KEY (id, store_url)
);
"""

_SCHEMA = (
    _PERFORMER_DDL
    + """
CREATE TABLE IF NOT EXISTS match (
    performer_id   TEXT NOT NULL,
    store_url      TEXT NOT NULL DEFAULT '',
    store_name     TEXT NOT NULL DEFAULT '',
    scene_id       TEXT NOT NULL,
    filename       TEXT NOT NULL,
    scene_duration INTEGER,
    clip_title     TEXT NOT NULL,
    clip_url       TEXT NOT NULL,
    title_score    REAL NOT NULL,
    duration_delta INTEGER,
    date_delta     INTEGER,
    confidence     TEXT NOT NULL,
    decision       TEXT NOT NULL,
    applied_at     REAL,
    PRIMARY KEY (performer_id, scene_id, clip_url)
);
"""
)

# Default decision per confidence tier when a match is first seen.
_DEFAULT_DECISION = {"high": "approved", "medium": "pending", "low": "rejected"}


@dataclass(slots=True)
class PerformerRow:
    id: str
    name: str
    store_url: str
    store_name: str
    unmatched_count: int
    catalog_count: int | None
    scraped_at: float | None
    status: str
    error: str | None
    # new clips found by the last incremental rescrape (None if never rescraped)
    last_new_clips: int | None = None
    # aggregated match decisions
    pending: int = 0
    approved: int = 0
    rejected: int = 0
    applied: int = 0

    @property
    def matched(self) -> int:
        return self.pending + self.approved + self.rejected + self.applied


@dataclass(slots=True)
class MatchRow:
    store_name: str
    scene_id: str
    filename: str
    clip_title: str
    clip_url: str
    title_score: float
    duration_delta: int | None
    date_delta: int | None
    confidence: str
    decision: str
    applied_at: float | None


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    # check_same_thread=False: the TUI opens short-lived connections from worker
    # threads. WAL + a busy timeout keep those concurrent writes from colliding.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older state.db up to the current schema. CREATE TABLE IF NOT
    EXISTS won't alter a table that predates these changes, so do it here. Each
    step is a no-op on an already-current db."""
    perf = {r["name"]: r["pk"] for r in conn.execute("PRAGMA table_info(performer)")}
    if "last_new_clips" not in perf:
        with conn:
            conn.execute("ALTER TABLE performer ADD COLUMN last_new_clips INTEGER")

    # The performer key has grown over time: id -> (id, store_name) -> (id,
    # store_url). The URL key lets a performer hold a row per *storefront* (e.g.
    # several Clips4Sale studios), not just per site. SQLite can't alter a primary
    # key, so when store_url isn't part of it yet, rebuild and copy rows across.
    if perf.get("store_url", 0) == 0:
        # One transaction for the whole rebuild: a crash between the RENAME and the
        # copy must not orphan _performer_old (which would skip the migration
        # forever, since the recreated table already has store_url in its PK).
        # conn.execute (not executescript, which force-commits) keeps it atomic.
        with conn:
            conn.execute("ALTER TABLE performer RENAME TO _performer_old")
            conn.execute(_PERFORMER_DDL)  # recreate keyed by (id, store_url)
            conn.execute(
                """
                INSERT OR IGNORE INTO performer
                    (id, name, store_url, store_name, unmatched_count,
                     catalog_count, scraped_at, status, error, triaged_at,
                     last_new_clips)
                SELECT id, name, store_url, store_name, unmatched_count,
                       catalog_count, scraped_at, status, error, triaged_at,
                       last_new_clips
                FROM _performer_old
                """
            )
            conn.execute("DROP TABLE _performer_old")

    mcols = {r["name"] for r in conn.execute("PRAGMA table_info(match)")}
    # Tag each existing match with the store its clip URL belongs to (older dbs).
    if "store_name" not in mcols:
        from .stores import for_url

        with conn:
            conn.execute("ALTER TABLE match ADD COLUMN store_name TEXT")
            urls = [
                r["clip_url"]
                for r in conn.execute("SELECT DISTINCT clip_url FROM match")
            ]
            for url in urls:
                store = for_url(url)
                conn.execute(
                    "UPDATE match SET store_name = ? WHERE clip_url = ?",
                    (store.name if store else "", url),
                )
    # Scope matches by storefront URL too. A clip URL doesn't always carry its
    # store's id (ManyVids /Video/ links don't), so derive the URL from the
    # performer row instead: pre-rebuild there was one row per (id, store_name),
    # so that row's store_url is the storefront these matches came from.
    if "store_url" not in mcols:
        with conn:
            conn.execute(
                "ALTER TABLE match ADD COLUMN store_url TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(
                """
                UPDATE match SET store_url = COALESCE(
                    (SELECT p.store_url FROM performer p
                      WHERE p.id = match.performer_id
                        AND p.store_name = match.store_name), '')
                """
            )


def upsert_performers(conn: sqlite3.Connection, rows: list[PerformerStatus]) -> None:
    """Refresh the performer table from a triage pass, preserving scrape state."""
    now = time.time()
    with conn:
        for s in rows:
            conn.execute(
                """
                INSERT INTO performer
                    (id, name, store_url, store_name, unmatched_count, triaged_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id, store_url) DO UPDATE SET
                    name = excluded.name,
                    store_name = excluded.store_name,
                    unmatched_count = excluded.unmatched_count,
                    triaged_at = excluded.triaged_at
                """,
                (
                    s.performer.id,
                    s.performer.name,
                    s.store_url,
                    s.store_name,
                    s.unmatched_count,
                    now,
                ),
            )


def set_status(
    conn: sqlite3.Connection,
    performer_id: str,
    store_url: str,
    status: str,
    *,
    error: str | None = None,
    catalog_count: int | None = None,
    new_clips: int | None = None,
) -> None:
    """Update one (performer, storefront) row's scrape status. ``new_clips``
    records how many clips a just-finished rescrape added; it's set verbatim (not
    coalesced), so a regular scrape or a transition to 'scraping'/'error' clears
    any stale count."""
    scraped_at = time.time() if status == "scraped" else None
    with conn:
        conn.execute(
            """
            UPDATE performer
               SET status = ?,
                   error = ?,
                   catalog_count = COALESCE(?, catalog_count),
                   scraped_at = COALESCE(?, scraped_at),
                   last_new_clips = ?
             WHERE id = ? AND store_url = ?
            """,
            (
                status,
                error,
                catalog_count,
                scraped_at,
                new_clips,
                performer_id,
                store_url,
            ),
        )


def reset_stale_scraping(conn: sqlite3.Connection) -> int:
    """Demote any 'scraping' rows back to 'new'. Returns the number reset.

    Status is persisted mid-scrape, so a performer interrupted by a quit/crash
    is left marked 'scraping' with nothing actually running. Called at startup
    (when nothing can be in flight) this re-queues those for the next scrape-all.
    """
    with conn:
        cur = conn.execute(
            "UPDATE performer SET status = 'new' WHERE status = 'scraping'"
        )
    return cur.rowcount


def performers_to_scrape(conn: sqlite3.Connection) -> list[PerformerRow]:
    """Performers worth scraping (not yet 'scraped' and with unmatched scenes),
    in the same order the dashboard lists them. This is the scrape-all work
    queue; because completed performers are 'scraped', it shrinks as work
    finishes and resumes correctly after a restart. Performers with no unmatched
    scenes are skipped -- there's nothing to match a catalog against."""
    return [
        r
        for r in get_performers(conn)
        if r.status != "scraped" and r.unmatched_count > 0
    ]


def performers_to_rescrape(conn: sqlite3.Connection) -> list[PerformerRow]:
    """Performers worth rescraping: those already scraped (pick up clips added
    since) plus those that previously errored (retry them), as long as they still
    have unmatched scenes. (Performers with nothing left to match are skipped.)"""
    return [
        r
        for r in get_performers(conn)
        if r.status in ("scraped", "error") and r.unmatched_count > 0
    ]


def save_matches(
    conn: sqlite3.Connection,
    performer_id: str,
    store_url: str,
    candidates: list[MatchCandidate],
) -> None:
    """Replace one (performer, storefront)'s matches, carrying over prior
    decisions.

    A re-scrape must not wipe decisions you've already made: for any
    (scene, clip) pair that existed before, we keep its decision and applied
    timestamp; genuinely new pairs get the default for their confidence tier.
    Scoped to this storefront so rescraping one leaves the others' matches.
    """
    with conn:
        prior = {
            (r["scene_id"], r["clip_url"]): (r["decision"], r["applied_at"])
            for r in conn.execute(
                "SELECT scene_id, clip_url, decision, applied_at "
                "FROM match WHERE performer_id = ? AND store_url = ?",
                (performer_id, store_url),
            )
        }
        conn.execute(
            "DELETE FROM match WHERE performer_id = ? AND store_url = ?",
            (performer_id, store_url),
        )
        for c in candidates:
            key = (c.scene.id, c.clip.url)
            decision, applied_at = prior.get(
                key, (_DEFAULT_DECISION[c.confidence], None)
            )
            conn.execute(
                """
                INSERT INTO match (
                    performer_id, store_url, scene_id, filename, scene_duration,
                    clip_title, clip_url, title_score, duration_delta,
                    date_delta, confidence, decision, applied_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    performer_id,
                    store_url,
                    c.scene.id,
                    c.scene.primary_basename,
                    c.scene.duration,
                    c.clip.title,
                    c.clip.url,
                    c.title_score,
                    c.duration_delta,
                    c.date_delta,
                    c.confidence,
                    decision,
                    applied_at,
                ),
            )


def get_performers(conn: sqlite3.Connection) -> list[PerformerRow]:
    """Every (performer, storefront) row with its match-decision tallies, sorted
    by performer name; a performer's storefronts sort next to each other."""
    counts: dict[tuple[str, str], dict[str, int]] = {}
    for r in conn.execute(
        "SELECT performer_id, store_url, decision, COUNT(*) n "
        "FROM match GROUP BY performer_id, store_url, decision"
    ):
        counts.setdefault((r["performer_id"], r["store_url"]), {})[r["decision"]] = r[
            "n"
        ]

    out: list[PerformerRow] = []
    for r in conn.execute(
        "SELECT * FROM performer ORDER BY name COLLATE NOCASE, store_name, store_url"
    ):
        c = counts.get((r["id"], r["store_url"]), {})
        out.append(
            PerformerRow(
                id=r["id"],
                name=r["name"],
                store_url=r["store_url"],
                store_name=r["store_name"],
                unmatched_count=r["unmatched_count"],
                catalog_count=r["catalog_count"],
                scraped_at=r["scraped_at"],
                status=r["status"],
                error=r["error"],
                last_new_clips=r["last_new_clips"],
                pending=c.get("pending", 0),
                approved=c.get("approved", 0),
                rejected=c.get("rejected", 0),
                applied=c.get("applied", 0),
            )
        )
    return out


def get_matches(
    conn: sqlite3.Connection,
    performer_id: str,
    store_url: str | None = None,
    decision: str | None = None,
) -> list[MatchRow]:
    """A performer's matches, best first. ``store_url`` limits to one storefront
    (None = pooled across all the performer's stores); ``decision`` filters to one
    decision."""
    sql = "SELECT * FROM match WHERE performer_id = ?"
    params: list[object] = [performer_id]
    if store_url is not None:
        sql += " AND store_url = ?"
        params.append(store_url)
    if decision is not None:
        sql += " AND decision = ?"
        params.append(decision)
    # confidence is text, so a plain sort would put 'low' before 'medium';
    # rank high > medium > low explicitly, then best title score first.
    sql += (
        " ORDER BY CASE confidence"
        " WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,"
        " title_score DESC"
    )
    return [
        MatchRow(
            store_name=r["store_name"],
            scene_id=r["scene_id"],
            filename=r["filename"],
            clip_title=r["clip_title"],
            clip_url=r["clip_url"],
            title_score=r["title_score"],
            duration_delta=r["duration_delta"],
            date_delta=r["date_delta"],
            confidence=r["confidence"],
            decision=r["decision"],
            applied_at=r["applied_at"],
        )
        for r in conn.execute(sql, params)
    ]


def set_decision(
    conn: sqlite3.Connection,
    performer_id: str,
    scene_id: str,
    clip_url: str,
    decision: str,
) -> None:
    with conn:
        conn.execute(
            "UPDATE match SET decision = ? "
            "WHERE performer_id = ? AND scene_id = ? AND clip_url = ?",
            (decision, performer_id, scene_id, clip_url),
        )


def approve_all(conn: sqlite3.Connection, performer_id: str) -> int:
    """Approve every not-yet-applied match for a performer (all storefronts).
    Returns the number of rows changed. Already-applied matches are left alone."""
    with conn:
        cur = conn.execute(
            "UPDATE match SET decision = 'approved' "
            "WHERE performer_id = ? AND decision != 'applied'",
            (performer_id,),
        )
    return cur.rowcount


def mark_applied(
    conn: sqlite3.Connection,
    performer_id: str,
    scene_id: str,
    clip_url: str,
) -> None:
    with conn:
        conn.execute(
            "UPDATE match SET decision = 'applied', applied_at = ? "
            "WHERE performer_id = ? AND scene_id = ? AND clip_url = ?",
            (time.time(), performer_id, scene_id, clip_url),
        )
