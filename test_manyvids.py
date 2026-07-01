"""Self-checks for the ManyVids RSC-stream parser. Plain asserts, no framework.

Run: uv run python test_manyvids.py
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from clipstores_scraper.stores import manyvids
from clipstores_scraper.stores.base import RETRYABLE_STATUS, get_page, hms_to_seconds
from clipstores_scraper.stores.manyvids import (
    _parse_videos,
    _to_clip,
)


def _page(*videos: dict) -> str:
    # Mimic ManyVids' Next.js stream: the video objects live inside a JSON
    # string pushed via self.__next_f, so every quote in them is escaped once.
    # Real ManyVids RSC is minified (no spaces), so build the fixture that way.
    compact = "".join(json.dumps(v, separators=(",", ":")) for v in videos)
    inner = "lead:" + compact + ":tail"
    payload = json.dumps(inner)[1:-1]  # escape as it appears inside push("...")
    return f'<script>self.__next_f.push([1,"{payload}"])</script>'


def test_parses_videos_through_escaped_stream() -> None:
    # A title with embedded quotes and a slash is the case a flat regex botches.
    vids = [
        {"id": "1000001", "title": 'He said "hi" 1/2', "slug": "he-said-hi"},
        {"id": "1000002", "title": "Plain", "slug": "plain", "price": {"x": "}"}},
    ]
    got = _parse_videos(_page(*vids))
    assert [v["id"] for v in got] == ["1000001", "1000002"], got
    assert got[0]["title"] == 'He said "hi" 1/2'  # escaped quotes survived
    assert got[1]["price"] == {"x": "}"}  # brace inside a string didn't end it


def test_duration_seconds() -> None:
    assert hms_to_seconds("28:58") == 28 * 60 + 58
    assert hms_to_seconds("1:02:33") == 3753
    assert hms_to_seconds("0:45") == 45
    assert hms_to_seconds(None) is None
    assert hms_to_seconds("n/a") is None


def test_to_clip_maps_fields() -> None:
    c = _to_clip(
        {
            "id": "1000001",
            "title": "Cool",
            "slug": "cool-vid",
            "duration": "28:58",
            "launchDate": "2026-04-17T06:17:09.000Z",
        }
    )
    assert c is not None
    assert c.url == "https://www.manyvids.com/Video/1000001/cool-vid"
    assert c.duration == 1738
    assert c.date == "2026-04-17"
    assert c.source == "ManyVids"


def test_to_clip_falls_back_to_slug_title() -> None:
    c = _to_clip({"id": "5", "slug": "no-title-here"})
    assert c is not None and c.title == "no title here" and c.date is None


class _Resp:
    def __init__(self, status: int = 200, text: str = "") -> None:
        self.status_code = status
        self.text = text
        self.url = "https://www.manyvids.com/Profile/1/_/Store/Videos"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    """Minimal httpx.Client stand-in: hands back a canned response per call."""

    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = responses
        self.calls = 0

    def get(self, url: str, **kw: object) -> _Resp:
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return r


def test_get_page_retries_once_on_transient() -> None:
    c = _Client([_Resp(503), _Resp(200)])
    resp = get_page(c, "u", retry_delay=0)
    assert resp.status_code == 200 and c.calls == 2  # retried, then succeeded


def test_get_page_gives_up_after_one_retry() -> None:
    c = _Client([_Resp(503), _Resp(503)])
    resp = get_page(c, "u", retry_delay=0)
    assert resp.status_code == 503 and c.calls == 2  # one retry, still transient
    assert resp.status_code in RETRYABLE_STATUS  # caller will raise, not truncate


def test_get_page_no_retry_on_404() -> None:
    c = _Client([_Resp(404)])
    resp = get_page(c, "u", retry_delay=0)
    assert resp.status_code == 404 and c.calls == 1  # end of catalog, no retry


def test_scan_stops_after_two_empty_pages() -> None:
    # RSC pages 1-2 have clips, 3-4 are empty (second empty ends the scan), 5 has a
    # clip that must never be fetched. Drives the real catalog() page loop through a
    # patched get_page — no network.
    pages = {
        1: _page({"id": "1", "title": "A", "slug": "a", "duration": "1:00"}),
        2: _page({"id": "2", "title": "B", "slug": "b", "duration": "2:00"}),
        3: _page(),
        4: _page(),
        5: _page({"id": "9", "title": "Z", "slug": "z"}),
    }
    seen: list[int] = []

    def fake_get_page(client: object, url: str, **kw: object) -> _Resp:
        page = int(url.split("page=")[1]) if "page=" in url else 1
        seen.append(page)
        return _Resp(200, pages[page])

    class _CM:  # httpx.Client(...) context manager stub
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *a: object) -> None:
            return None

    # Patch the names in manyvids' namespace (not the real httpx/time modules).
    orig_get_page, orig_httpx, orig_time = (
        manyvids.get_page,
        manyvids.httpx,
        manyvids.time,
    )
    manyvids.get_page = fake_get_page
    manyvids.httpx = SimpleNamespace(Client=lambda *a, **k: _CM())
    manyvids.time = SimpleNamespace(sleep=lambda _s: None)  # no page delay in tests
    try:
        clips = manyvids.ManyVidsStore().catalog(
            "https://www.manyvids.com/Profile/1/_/Store/Videos", config=None
        )
    finally:
        manyvids.get_page, manyvids.httpx, manyvids.time = (
            orig_get_page,
            orig_httpx,
            orig_time,
        )
    assert {c.title for c in clips} == {"A", "B"}, clips
    assert 5 not in seen, seen  # stopped at the second empty page


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
