"""Self-checks for the YourVids backend: URL/handle parsing, catalog-item
mapping, and detail-page parsing. Plain asserts, no framework.
Run: uv run python test_yourvids.py
"""

from __future__ import annotations

from clipstores_scraper.stores.yourvids import (
    YourVidsStore,
    _clean_text,
    _date,
    _jsonld,
    _to_clip,
)

_STORE = YourVidsStore()

# (url, expected store_id or None). Slug is case-folded; the #videos fragment,
# trailing slashes and the www/http variants all reduce to the slug. Clip URLs
# and other hosts are not store URLs.
_URL_CASES = [
    ("https://yourvids.com/creators/demojames", "demojames"),
    ("https://yourvids.com/creators/demojames#videos", "demojames"),
    ("https://www.yourvids.com/creators/DemoJames/", "demojames"),
    ("http://yourvids.com/creators/sample-seller", "sample-seller"),
    ("https://yourvids.com/vids/sample-clip-title", None),
    ("https://yourvids.com/creators", None),
    ("https://notyourvids.com/creators/demojames", None),
    ("https://evil.com/yourvids.com/creators/demojames", None),
]


def test_handles_and_store_id() -> None:
    for url, expected in _URL_CASES:
        assert _STORE.handles(url) is (expected is not None), url
        assert _STORE.store_id(url) == expected, url


def test_date() -> None:
    assert _date("2025-06-13 16:00:13") == "2025-06-13"
    assert _date("2025-06-13T16:00:13+00:00") == "2025-06-13"
    assert _date(None) is None
    assert _date("nope") is None


def test_to_clip_maps_an_api_item() -> None:
    c = _to_clip(
        {
            "id": 12345,
            "title": "Sample Clip Title",
            "video_url": "https://yourvids.com/vids/sample-clip-title",
            "duration": "113:43",  # MM:SS, minutes unbounded
            "created_at": "2025-06-13 16:00:13",
        }
    )
    assert c is not None
    assert c.url == "https://yourvids.com/vids/sample-clip-title"
    assert c.duration == 113 * 60 + 43
    assert c.date == "2025-06-13"
    assert c.source == "YourVids"
    assert _to_clip({"title": "no url"}) is None


def test_clean_text() -> None:
    assert _clean_text("  a<br> <br> b<strong>!</strong>  ") == "a\n\nb!"
    assert _clean_text("<p>one</p><p>two</p>") == "one\ntwo"
    assert _clean_text(" <br> ") is None


def test_detail_parsing() -> None:
    html = """
    <script type="application/ld+json">{"@context":"https://schema.org",
    "@type":"VideoObject","name":"Sample Clip","description":"Short…",
    "thumbnailUrl":"https://cdn.yourvids.com/x/thumb.webp",
    "uploadDate":"2025-06-20T19:10:02+00:00","duration":"PT33M46S",
    "embedUrl":"https://yourvids.com/vids/12345/embed",
    "author":{"@type":"Person","name":"DemoJames"}}</script>
    <meta property="video:release_date" content="2025-06-13T16:00:13+00:00">
    <a href="https://yourvids.com/vids?tag%5B%5D=Alpha">Alpha</a>
    <a href="https://yourvids.com/vids?tag%5B%5D=Blue%20Widget%20%2F%20Gadget">x</a>
    <a href="https://yourvids.com/vids?tag%5B%5D=Alpha">Alpha</a>
    <div id="desktopDescriptionFull" class="hidden">
      <div class="rich-text-content text-base">Full text.<br> <br> More.</div>
    </div>
    """
    ld = _jsonld(html)
    assert ld["name"] == "Sample Clip"

    import clipstores_scraper.stores.yourvids as yv

    assert yv._EMBED_ID_RE.search(ld["embedUrl"]).group(1) == "12345"
    assert _date(yv._RELEASE_RE.search(html).group(1)) == "2025-06-13"
    assert _clean_text(yv._FULL_DESC_RE.search(html).group(1)) == "Full text.\n\nMore."
    tags = [yv.urllib.parse.unquote(t) for t in yv._TAG_RE.findall(html)]
    assert tags == ["Alpha", "Blue Widget / Gadget", "Alpha"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
