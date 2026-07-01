"""Self-checks for the LoyalFans backend: handle parsing across the URL variants
in Stash, and store-item mapping. Plain asserts, no framework.
Run: uv run python test_loyalfans.py
"""

from __future__ import annotations

from clipstores_scraper.stores.loyalfans import (
    LoyalFansStore,
    _clip_slug,
    _date,
    _to_clip,
)

_STORE = LoyalFansStore()

# (url, expected store_id or None). The handle is case-folded; /store, /media,
# trailing slashes and the no-www/http variants all reduce to the handle.
_URL_CASES = [
    ("https://www.loyalfans.com/demojames", "demojames"),
    ("https://www.loyalfans.com/SampleMidas/store", "samplemidas"),
    ("https://www.loyalfans.com/bluexstream/media", "bluexstream"),
    ("http://loyalfans.com/nora_vale", "nora_vale"),
    ("https://www.loyalfans.com/happyangel42/", "happyangel42"),
    ("https://www.loyalfans.com/Sam-Rowe", "sam-rowe"),
    ("https://iwantclips.com/store/9/x", None),
]


def test_handles_and_store_id() -> None:
    for url, expected in _URL_CASES:
        assert _STORE.handles(url) is (expected is not None), url
        assert _STORE.store_id(url) == expected, url


def test_date() -> None:
    assert _date("2025-06-13 16:00:13") == "2025-06-13"
    assert _date({"date": "2025-06-13 16:00:13"}) == "2025-06-13"
    assert _date(None) is None
    assert _date("nope") is None


def test_to_clip_maps_a_store_item() -> None:
    c = _to_clip(
        {
            "slug": "sample-clip-1000000000000",
            "title": "Sample Clip Title",
            "owner": {"slug": "demojames"},
            "created_at": "2025-06-13 16:00:13",
            "video_object": {"duration": 2405},
        }
    )
    assert c is not None
    assert c.url == (
        "https://www.loyalfans.com/demojames/video/sample-clip-1000000000000"
    )
    assert c.duration == 2405
    assert c.date == "2025-06-13"
    assert c.source == "LoyalFans"
    assert _clip_slug(c.url) == "sample-clip-1000000000000"


def test_to_clip_needs_slug_and_owner() -> None:
    assert _to_clip({"title": "x", "owner": {"slug": "a"}}) is None  # no slug
    assert _to_clip({"slug": "s", "title": "x"}) is None  # no owner


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
