"""Self-checks for multi-store-per-performer support: store discovery, per-store
match isolation in state.db, and the old->new schema migration. Plain asserts,
no framework. Run: uv run python test_multistore.py
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from clipstores_scraper import state
from clipstores_scraper.models import (
    Clip,
    MatchCandidate,
    Performer,
    PerformerStatus,
    Scene,
    SceneFile,
)
from clipstores_scraper.pipeline import supported_store_urls

_IWC = "https://iwantclips.com/store/9/x"
_MV = "https://www.manyvids.com/Profile/1/x"


def _tmp() -> Path:
    return Path(tempfile.mkdtemp()) / "state.db"


def _match(url: str, source: str) -> MatchCandidate:
    scene = Scene(id="s1", title=None, date=None, files=[SceneFile("f.mp4", 600)])
    return MatchCandidate(scene, Clip("T", url, source, 600), 0.95, 0, None, "high")


def test_supported_store_urls_one_per_store() -> None:
    p = Performer(
        id="1",
        name="N",
        urls=[
            "https://allmylinks.com/x",  # unsupported
            _IWC,  # iwantclips
            "https://www.manyvids.com/Profile/60001/Sam-Rowe/",  # manyvids 60001
            "https://www.manyvids.com/Profile/60001/sam-rowe/Store/Videos",  # dup
            "https://x.com/y",  # unsupported
        ],
    )
    # one URL per distinct store; the duplicate manyvids profile collapses away
    assert supported_store_urls(p) == [
        _IWC,
        "https://www.manyvids.com/Profile/60001/Sam-Rowe/",
    ]


def test_two_stores_one_performer_keep_separate_matches() -> None:
    conn = state.connect(_tmp())
    perf = Performer(id="p", name="Nm")
    state.upsert_performers(
        conn,
        [
            PerformerStatus(perf, _IWC, "iwantclips", 5),
            PerformerStatus(perf, _MV, "manyvids", 5),
        ],
    )
    keys = {(r.id, r.store_name) for r in state.get_performers(conn)}
    assert keys == {("p", "iwantclips"), ("p", "manyvids")}

    state.save_matches(conn, "p", _IWC, [_match(_IWC + "/5/T", "IWantClips")])
    state.save_matches(
        conn, "p", _MV, [_match("https://www.manyvids.com/Video/2/t", "ManyVids")]
    )
    assert len(state.get_matches(conn, "p", _IWC)) == 1
    assert len(state.get_matches(conn, "p", _MV)) == 1

    # Rescraping one store replaces only its own matches, never the other's.
    state.save_matches(conn, "p", _IWC, [])
    assert len(state.get_matches(conn, "p", _IWC)) == 0
    assert len(state.get_matches(conn, "p", _MV)) == 1

    by_key = {(r.id, r.store_name): r for r in state.get_performers(conn)}
    assert by_key[("p", "manyvids")].approved == 1  # high-confidence -> approved
    assert by_key[("p", "iwantclips")].approved == 0
    conn.close()


def test_same_store_two_storefronts_are_separate_rows() -> None:
    # The whole point of URL keying: a performer with two C4S studios (same store
    # name, different URLs) gets a row and a match set for each.
    conn = state.connect(_tmp())
    perf = Performer(id="p", name="Nm")
    a = "https://www.clips4sale.com/studio/100/a"
    b = "https://www.clips4sale.com/studio/200/b"
    state.upsert_performers(
        conn,
        [
            PerformerStatus(perf, a, "clips4sale", 5),
            PerformerStatus(perf, b, "clips4sale", 5),
        ],
    )
    rows = state.get_performers(conn)
    assert len(rows) == 2
    assert {r.store_url for r in rows} == {a, b}
    assert {r.store_name for r in rows} == {"clips4sale"}

    state.save_matches(
        conn, "p", a, [_match("https://www.clips4sale.com/studio/100/11/x", "C4S")]
    )
    state.save_matches(
        conn, "p", b, [_match("https://www.clips4sale.com/studio/200/22/y", "C4S")]
    )
    assert len(state.get_matches(conn, "p", a)) == 1
    assert len(state.get_matches(conn, "p", b)) == 1
    # Rescraping one studio leaves the other's matches untouched.
    state.save_matches(conn, "p", a, [])
    assert len(state.get_matches(conn, "p", a)) == 0
    assert len(state.get_matches(conn, "p", b)) == 1
    conn.close()


_OLD_SCHEMA = """
CREATE TABLE performer (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, store_url TEXT NOT NULL,
    store_name TEXT NOT NULL, unmatched_count INTEGER NOT NULL DEFAULT 0,
    catalog_count INTEGER, scraped_at REAL, status TEXT NOT NULL DEFAULT 'new',
    error TEXT, triaged_at REAL
);
CREATE TABLE match (
    performer_id TEXT NOT NULL, scene_id TEXT NOT NULL, filename TEXT NOT NULL,
    scene_duration INTEGER, clip_title TEXT NOT NULL, clip_url TEXT NOT NULL,
    title_score REAL NOT NULL, duration_delta INTEGER, date_delta INTEGER,
    confidence TEXT NOT NULL, decision TEXT NOT NULL, applied_at REAL,
    PRIMARY KEY (performer_id, scene_id, clip_url)
);
"""


def test_migration_preserves_decisions_and_backfills_store() -> None:
    path = _tmp()
    raw = sqlite3.connect(path)
    raw.executescript(_OLD_SCHEMA)
    raw.execute(
        "INSERT INTO performer (id, name, store_url, store_name, status) "
        "VALUES ('p', 'Nm', ?, 'iwantclips', 'scraped')",
        (_IWC,),
    )
    raw.execute(
        "INSERT INTO match (performer_id, scene_id, filename, clip_title, clip_url, "
        "title_score, confidence, decision) "
        "VALUES ('p', 's1', 'f.mp4', 'T', ?, 0.9, 'high', 'approved')",
        (_IWC + "/5/T",),
    )
    raw.commit()
    raw.close()

    conn = state.connect(path)  # triggers _migrate
    # The reviewed decision survived, and the match got tagged with its store URL.
    rows = state.get_matches(conn, "p", _IWC)
    assert len(rows) == 1 and rows[0].decision == "approved"
    # The widened key now lets the same performer hold a second store.
    perf = Performer(id="p", name="Nm")
    state.upsert_performers(conn, [PerformerStatus(perf, _MV, "manyvids", 0)])
    keys = {(r.id, r.store_name) for r in state.get_performers(conn)}
    assert keys == {("p", "iwantclips"), ("p", "manyvids")}
    conn.close()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
