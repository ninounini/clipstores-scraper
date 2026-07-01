"""Scraped titles/details/studio/tags must not keep raw HTML entities, or they
leak into Stash on enrich (e.g. "Inhale &amp; Eat It"). Plain asserts, no
framework. Run: uv run python test_models_unescape.py
"""

from __future__ import annotations

from clipstores_scraper.models import Clip, SceneData


def test_clip_title_decodes_entities() -> None:
    c = Clip(title="Inhale &amp; Eat It", url="u", source="IWantClips")
    assert c.title == "Inhale & Eat It"


def test_scenedata_decodes_text_fields() -> None:
    d = SceneData(
        source="s",
        title="A &amp; B",
        details="it&#39;s mine",
        studio="X &amp; Co",
        tags=["foot &amp; sole", "joi"],
    )
    assert d.title == "A & B"
    assert d.details == "it's mine"
    assert d.studio == "X & Co"
    assert d.tags == ["foot & sole", "joi"]


def test_decode_is_idempotent() -> None:
    # Stores that already call html.unescape must not be double-mangled.
    assert Clip(title="Inhale & Eat It", url="u", source="s").title == "Inhale & Eat It"


def test_cover_url_and_code_left_alone() -> None:
    d = SceneData(source="s", cover_url="https://x/a?b=1&amp;c=2", code="123&45")
    assert d.cover_url == "https://x/a?b=1&amp;c=2"
    assert d.code == "123&45"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
