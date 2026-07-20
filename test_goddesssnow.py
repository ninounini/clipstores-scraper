"""Self-checks for the GoddessSnow backend: canonical clip-URL form and slug
handling across the URL variants. Plain asserts, no framework.
Run: uv run python test_goddesssnow.py
"""

from __future__ import annotations

from clipstores_scraper.stores.goddesssnow import _SLUG_RE, GoddessSnowStore, _clip_url

_STORE = GoddessSnowStore()


def test_clip_url_is_the_vids_form() -> None:
    # Same canonical form the Stash community scraper writes: the /updates/
    # page prints every release date a year late, the _vids page is correct.
    assert _clip_url("example-clip") == (
        "https://www.goddesssnow.com/vod/scenes/example-clip_vids.html"
    )


def test_slug_extracted_from_all_url_forms() -> None:
    for url in (
        "https://www.goddesssnow.com/updates/example-clip.html",
        "https://www.goddesssnow.com/vod/scenes/example-clip.html",
        "https://www.goddesssnow.com/vod/scenes/example-clip_vids.html",
    ):
        m = _SLUG_RE.search(url)
        assert m and m.group(1) == "example-clip", url
        assert _STORE.handles(url)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
