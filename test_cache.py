"""Self-check: an empty cached catalog must read back as a miss, not a hit.

Plain asserts, no framework. Run: uv run python test_cache.py
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from clipstores_scraper import cache
from clipstores_scraper.models import Clip


def test_empty_catalog_is_a_miss() -> None:
    with tempfile.TemporaryDirectory() as d:
        cache.CACHE_DIR = Path(d)
        cache.save("iwantclips", "999", [])  # a failed/blocked scrape
        assert cache.load("iwantclips", "999") is None  # not served as []


def test_nonempty_catalog_round_trips() -> None:
    with tempfile.TemporaryDirectory() as d:
        cache.CACHE_DIR = Path(d)
        clips = [Clip(title="X", url="http://x/1", source="IWantClips", duration=60)]
        cache.save("iwantclips", "999", clips)
        got = cache.load("iwantclips", "999")
        assert got is not None and len(got) == 1 and got[0].title == "X"


def test_stale_catalog_is_a_miss() -> None:
    with tempfile.TemporaryDirectory() as d:
        cache.CACHE_DIR = Path(d)
        clips = [Clip(title="X", url="http://x/1", source="IWantClips", duration=60)]
        cache.save("iwantclips", "999", clips)
        path = cache._path("iwantclips", "999")
        old = time.time() - cache.DEFAULT_TTL - 10  # backdate past the TTL
        os.utime(path, (old, old))
        assert cache.load("iwantclips", "999") is None  # expired -> miss
        # A caller passing a longer TTL (the rescrape sentinel) still hits.
        assert cache.load("iwantclips", "999", ttl=10**12) is not None


def test_corrupt_or_old_schema_file_is_a_miss() -> None:
    with tempfile.TemporaryDirectory() as d:
        cache.CACHE_DIR = Path(d)
        path = cache._path("iwantclips", "999")
        path.parent.mkdir(parents=True, exist_ok=True)
        # A field Clip() doesn't accept (old/newer schema) -> TypeError -> miss.
        path.write_text('[{"title": "X", "gone_field": 1}]', encoding="utf-8")
        assert cache.load("iwantclips", "999") is None
        # Truncated / non-JSON -> ValueError -> miss.
        path.write_text("{ not json", encoding="utf-8")
        assert cache.load("iwantclips", "999") is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
