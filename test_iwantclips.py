"""Self-checks for the IWantClips Typesense parsing and catalog merge. Plain
asserts, no framework, no network (Typesense calls are monkeypatched).
Run: uv run python test_iwantclips.py
"""

from __future__ import annotations

from clipstores_scraper.config import Config
from clipstores_scraper.models import Clip
from clipstores_scraper.stores import iwantclips
from clipstores_scraper.stores.base import hms_to_seconds
from clipstores_scraper.stores.iwantclips import IWantClipsStore, _to_clip

_STORE = "https://iwantclips.com/store/50001/DemoCreator"


def test_parse_exact_duration_and_date() -> None:
    doc = {
        "content_id": "5000001",
        "content_url": _STORE + "/5000001/x",
        "title": "x",
        "video_length": "00:03:46",  # HH:MM:SS, second-granular
        "publish_time": "1692022469",  # unix seconds, as a string
    }
    clip = _to_clip(doc)
    assert clip.duration == 226, clip.duration  # not minute-rounded
    assert clip.date == "2023-08-14", clip.date
    assert hms_to_seconds("57:31") == 57 * 60 + 31  # bare MM:SS too


def test_rescrape_refreshes_known_and_keeps_removed(monkeypatch) -> None:
    # A known clip carried over from the old card scrape: minute-rounded, no date.
    stale = Clip("Old", _STORE + "/5000001/x", "IWantClips", duration=180, date=None)
    # ...and one the index no longer returns (removed since).
    removed = Clip(
        "Gone", _STORE + "/999/y", "IWantClips", duration=300, date="2020-01-01"
    )

    monkeypatch.setattr(iwantclips, "_typesense_config", lambda sid: ("h", "k"))
    monkeypatch.setattr(
        iwantclips,
        "_ts_search",
        lambda client, host, key, sid, page: {
            "found": 1,
            "hits": [
                {
                    "document": {
                        "content_id": "5000001",
                        "content_url": _STORE + "/5000001/x",
                        "title": "New",
                        "video_length": "00:03:46",
                        "publish_time": "1692022469",
                    }
                }
            ],
        },
    )

    cfg = Config(stash_url="http://x", stash_api_key="")
    out = {
        c.url.rsplit("/", 2)[1]: c
        for c in IWantClipsStore().catalog(_STORE, cfg, known=[stale, removed])
    }
    # Still-indexed clip refreshed from Typesense, not left stale.
    assert out["5000001"].duration == 226, out["5000001"].duration
    assert out["5000001"].date == "2023-08-14", out["5000001"].date
    # Clip absent from the index is retained from the seed.
    assert out["999"].duration == 300 and out["999"].date == "2020-01-01"


class _MP:
    """Tiny monkeypatch stand-in so this runs without pytest."""

    def __init__(self) -> None:
        self._undo: list = []

    def setattr(self, obj, name, val) -> None:
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)

    def undo(self) -> None:
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)


if __name__ == "__main__":
    import inspect

    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            mp = _MP()
            try:
                fn(mp) if "monkeypatch" in inspect.signature(fn).parameters else fn()
            finally:
                mp.undo()
            print(f"ok  {name}")
    print("all checks passed")
