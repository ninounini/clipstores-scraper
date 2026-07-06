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
    _rich_text,
    _to_clip,
)

_STORE = YourVidsStore()

# (url, handled, expected store_id). Slug is case-folded; the #videos fragment,
# trailing slashes and the www/http variants all reduce to the slug. Clip URLs
# are handled too (enrich resolves the store from a matched scene's clip URL)
# but have no store_id — the creator isn't in the URL.
_URL_CASES = [
    ("https://yourvids.com/creators/demojames", True, "demojames"),
    ("https://yourvids.com/creators/demojames#videos", True, "demojames"),
    ("https://www.yourvids.com/creators/DemoJames/", True, "demojames"),
    ("http://yourvids.com/creators/sample-seller", True, "sample-seller"),
    ("https://yourvids.com/vids/sample-clip-title", True, None),
    ("https://yourvids.com/creators", False, None),
    ("https://yourvids.com/boutique", False, None),
    ("https://notyourvids.com/creators/demojames", False, None),
    ("https://evil.com/yourvids.com/creators/demojames", False, None),
    ("https://evil.com/yourvids.com/vids/sample-clip-title", False, None),
]


def test_handles_and_store_id() -> None:
    for url, handled, expected_id in _URL_CASES:
        assert _STORE.handles(url) is handled, url
        assert _STORE.store_id(url) == expected_id, url


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
    assert _clean_text("<p>one</p><p>two</p>") == "one\n\ntwo"
    assert _clean_text(" <br> ") is None


def test_rich_text_tracks_nested_divs() -> None:
    # Pasted markup nests <div>s inside the description; extraction must not
    # stop at the first </div> (seen on real clips: 1500 chars cut to 51).
    h = '<div class="rich-text-content x">a<div>b<div>c</div></div>d</div>tail'
    assert _rich_text(h) == "a<div>b<div>c</div></div>d"
    assert _clean_text(_rich_text(h)) == "a\nb\nc\n\nd"
    assert _rich_text("no description here") is None
    assert _rich_text('<div class="rich-text-content">unbalanced<div>') is None


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
    anchor = html.find(yv._FULL_DESC_ANCHOR)
    assert _clean_text(_rich_text(html, anchor)) == "Full text.\n\nMore."
    tags = [yv.urllib.parse.unquote(t) for t in yv._TAG_RE.findall(html)]
    assert tags == ["Alpha", "Blue Widget / Gadget", "Alpha"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
