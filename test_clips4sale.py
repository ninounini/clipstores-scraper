"""Self-checks for the Clips4Sale backend: URL handling across the variants
found in Stash, and clip-row parsing. Plain asserts, no framework.
Run: uv run python test_clips4sale.py
"""

from __future__ import annotations

from clipstores_scraper.stores.base import strip_html
from clipstores_scraper.stores.clips4sale import (
    Clips4SaleStore,
    _absorb,
    _clip_id,
    _parse_date,
    _to_clip,
)

_STORE = Clips4SaleStore()

# (url, expected store_id or None). Covers the URL-shape variants seen in Stash.
_URL_CASES = [
    ("https://www.clips4sale.com/studio/40001/demo-studio", "40001"),
    ("https://www.clips4sale.com/studio/40002", "40002"),  # no slug
    ("https://www.clips4sale.com/studio/40003/sample-studio/", "40003"),  # trailing /
    (
        "https://www.clips4sale.com/studio/40004/example-studio/Cat0-AllCategories"
        "/Page1/C4SSort-recommended/Limit24/search/Jane%20Doe",
        "40004",
    ),  # in-store search path
    ("https://www.clips4sale.com/40005/short-clips", "40005"),  # missing /studio/
    ("https://wwww.clips4sale.com/studio/40006/x", "40006"),  # typo host
    # Performer profile: handled, keyed p{id} to stay distinct from studio ids.
    ("https://www.clips4sale.com/performers/40007/demo-name", "p40007"),
    ("https://iwantclips.com/store/9/x", None),  # other store
]


def test_handles_and_store_id() -> None:
    for url, expected in _URL_CASES:
        assert _STORE.handles(url) is (expected is not None), url
        assert _STORE.store_id(url) == expected, url


def test_parse_date() -> None:
    assert _parse_date("6/14/26 9:05 PM") == "2026-06-14"
    assert _parse_date("12/1/24 12:00 AM") == "2024-12-01"
    assert _parse_date(None) is None
    assert _parse_date("not a date") is None


def test_to_clip_maps_a_listing_row() -> None:
    c = _to_clip(
        {
            "title": "Cool Clip",
            "time_minutes": 8,
            "date_display": "6/14/26 9:05 PM",
            "link": "/studio/40001/30000001/cool-clip",
        }
    )
    assert c is not None
    assert c.url == "https://www.clips4sale.com/studio/40001/30000001/cool-clip"
    assert c.duration == 480  # minutes -> seconds
    assert c.date == "2026-06-14"
    assert c.source == "Clips4Sale"
    assert _clip_id(c.url) == "30000001"


def test_clean_title_strips_search_highlight() -> None:
    # The search endpoint highlights the matched term with <em>; matching needs
    # the bare title.
    assert strip_html("<em>K</em> Super Teaser") == "K Super Teaser"
    assert strip_html("Tease &amp; Denial") == "Tease & Denial"
    assert strip_html(None) == ""


def test_absorb_filters_by_performer() -> None:
    rows = [
        {"link": "/studio/1/100/a", "title": "A", "performers": [{"id": 30001}]},
        {"link": "/studio/2/200/b", "title": "B", "performers": [{"id": 999}]},
        {
            "link": "/studio/3/300/c",
            "title": "C",
            "performers": [{"id": 30001}, {"id": 5}],
        },
    ]
    # Performer search keeps only rows featuring the target id (collabs included).
    clips: dict = {}
    assert _absorb(clips, rows, performer_id="30001") == 2
    assert set(clips) == {"100", "300"}
    # No filter (studio listing) -> everything is absorbed.
    assert _absorb({}, rows) == 3


def test_to_clip_handles_missing_bits() -> None:
    # No link -> not a usable clip.
    assert _to_clip({"title": "x"}) is None
    # Falls back to the duration key; bad date -> None; title falls back to url.
    c = _to_clip({"duration": 12, "link": "/studio/1/2/s", "date_display": "??"})
    assert c is not None and c.duration == 720 and c.date is None
    assert c.title == "https://www.clips4sale.com/studio/1/2/s"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
