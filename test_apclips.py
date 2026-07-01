"""Self-check for the APClips grid regex: a card missing its title must not
bridge into the next card and steal its title. Plain asserts, no framework.

Run: uv run python test_apclips.py
"""

from __future__ import annotations

from clipstores_scraper.stores.apclips import _CARD_RE


def _card(url: str, dur: str, title: str | None = None) -> str:
    """One grid card, mirroring the real markup _CARD_RE targets."""
    html = (
        f'<a href="{url}" class="thumb-image rounded">\n'
        f'<span class="item-details">{dur}</span>\n'
    )
    if title is not None:
        html += f'<span class="item-title">{title}</span>\n'
    return html + "</a>\n"


def _matches(page: str) -> list[tuple[str, str]]:
    return [(m.group("url"), m.group("title")) for m in _CARD_RE.finditer(page)]


def test_normal_cards_match() -> None:
    page = _card("/v/1", "10:00", "First") + _card("/v/2", "20:00", "Second")
    assert _matches(page) == [("/v/1", "First"), ("/v/2", "Second")]


def test_card_without_title_does_not_steal_the_next_one() -> None:
    # First card has no item-title. Old regex: `.*?` bridged across the card
    # boundary and mis-attributed "Second" to /v/1. Now it must be skipped.
    page = _card("/v/1", "10:00") + _card("/v/2", "20:00", "Second")
    assert _matches(page) == [("/v/2", "Second")]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
