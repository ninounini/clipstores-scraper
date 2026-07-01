"""Self-checks for the scene-matcher agent's prep: gray-zone ranking and the
Stash-base derivation. Plain asserts, no framework. Run: uv run python test_agent_cli.py
"""

from __future__ import annotations

from clipstores_scraper.agent_cli import _stash_base, rank_candidates
from clipstores_scraper.config import Config
from clipstores_scraper.matching import clean_filename
from clipstores_scraper.models import Clip, Scene, SceneFile


def _cfg(url: str) -> Config:
    return Config(stash_url=url, stash_api_key="")


def test_stash_base_strips_graphql() -> None:
    assert _stash_base(_cfg("https://stash.x/graphql")) == "https://stash.x"
    assert _stash_base(_cfg("https://stash.x/graphql/")) == "https://stash.x"
    assert _stash_base(_cfg("https://stash.x")) == "https://stash.x"


def test_corroboration_outranks_weak_title_and_flags_gray_zone() -> None:
    # The file was renamed: its title barely matches the real clip, but duration
    # and date nail it. The deterministic matcher rejects this (sub-title-gate),
    # yet it must lead the worksheet — flagged gate=None — over a weak title with
    # no corroboration. That gray-zone retitle is exactly the agent's job.
    scene = Scene(
        id="1",
        title=None,
        date="2020-01-09",
        files=[SceneFile(basename="renamed_clip.mp4", duration=425)],
    )
    names = ["Jane Doe"]
    query = clean_filename(scene.primary_basename, names)
    clips = [
        Clip("Sunday Garden Tour", "u1", "IWantClips", 420, "2020-01-09"),  # real clip
        Clip("renamed something else", "u2", "IWantClips", 9, "1999-01-01"),  # weak
    ]
    out = rank_candidates(scene, query, clips, names)

    assert out[0]["clip_url"] == "u1", "corroboration must outrank a weak bare title"
    assert out[0]["gate"] is None, "gray-zone: the matcher would have dropped it"
    assert out[0]["duration_delta"] == 5
    assert out[0]["date_delta"] == 0


def test_floor_drops_zero_signal_clip() -> None:
    # A title that cleans away to nothing (just a resolution tag) scores 0 and,
    # with no duration/date to corroborate, must fall below the rank floor.
    scene = Scene(id="3", title=None, date=None, files=[SceneFile("Beach Walk.mp4")])
    names: list[str] = []
    query = clean_filename(scene.primary_basename, names)
    clips = [Clip("1080p", "drop", "IWantClips")]
    assert rank_candidates(scene, query, clips, names) == []


def test_clean_title_match_passes_the_gate() -> None:
    # A clean title hit should carry the matcher's own gate verdict, not None.
    scene = Scene(id="2", title=None, date=None, files=[SceneFile("Beach Walk.mp4")])
    names: list[str] = []
    query = clean_filename(scene.primary_basename, names)
    clips = [Clip("Beach Walk", "u9", "IWantClips")]
    out = rank_candidates(scene, query, clips, names)
    assert out and out[0]["clip_url"] == "u9"
    assert out[0]["gate"] in {"low", "medium", "high"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
