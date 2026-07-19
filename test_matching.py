"""Self-checks for the review fixes: 1:1 clip→scene matching and primary-file pick.

Plain asserts, no framework. Run: uv run python test_matching.py
"""

from __future__ import annotations

from clipstores_scraper.matching import title_score
from clipstores_scraper.models import Clip, Performer, Scene, SceneFile
from clipstores_scraper.pipeline import match_scenes


def _scene(sid: str, basename: str, *durations: int | None) -> Scene:
    durs = durations or (None,)
    files = [SceneFile(basename=basename, duration=d) for d in durs]
    return Scene(id=sid, title=None, date=None, files=files)


def test_primary_file_is_the_longest() -> None:
    # A 5s trailer plus the real 600s video: title and duration must both come
    # from the video, not whichever file happens to be first.
    s = _scene("1", "trailer.mp4", 5)
    s.files.append(SceneFile(basename="real-video.mp4", duration=600))
    assert s.duration == 600
    assert s.primary_basename == "real-video.mp4"


def test_one_clip_never_lands_on_two_scenes() -> None:
    perf = Performer(id="p", name="Nobody")
    clip = Clip(
        title="Cool Scene", url="http://x/clip/1", source="IWantClips", duration=600
    )
    a = _scene("A", "Cool Scene.mp4", 600)  # duration agrees -> high
    b = _scene("B", "Cool Scene.mp4")  # no duration       -> medium
    results = match_scenes([a, b], [clip], perf)
    assert len(results) == 1, results
    assert results[0].scene.id == "A"  # the stronger (corroborated) scene wins
    assert results[0].confidence == "high"


def test_distinct_clips_keep_distinct_scenes() -> None:
    perf = Performer(id="p", name="Nobody")
    clips = [
        Clip(title="First Thing", url="http://x/clip/1", source="IWantClips"),
        Clip(title="Second Thing", url="http://x/clip/2", source="IWantClips"),
    ]
    results = match_scenes(
        [_scene("A", "First Thing.mp4"), _scene("B", "Second Thing.mp4")], clips, perf
    )
    assert {r.clip.url for r in results} == {"http://x/clip/1", "http://x/clip/2"}


def test_leading_date_prefix_does_not_block_match() -> None:
    # "26-07-2023 - <performer> - garden notes.mov" must match the catalog's
    # "garden notes": the date prefix is noise, not part of the title.
    perf = Performer(id="p", name="nova blake")
    clip = Clip(
        title="garden notes",
        url="http://x/clip/1",
        source="ManyVids",
        duration=3451,
        date="2023-07-26",
    )
    s = _scene("301", "26-07-2023 - nova blake - garden notes.mov", 3450)
    s.date = "2023-07-26"
    results = match_scenes([s], [clip], perf)
    assert len(results) == 1, results
    assert results[0].confidence == "high", results[0]


def test_downloader_stamp_does_not_block_match() -> None:
    # Our downloader appends "_Downloaded_YYYY_MM_DD_HH_MM_SS"; those tokens must
    # not dilute the title score. The stem here matches the clip title exactly.
    perf = Performer(id="p", name="Bestcreatorx")
    clip = Clip(
        title="It was supposed to be a quiet weekend afternoon",
        url="http://x/clip/1",
        source="IWantClips",
    )
    s = _scene(
        "302",
        "Bestcreatorx_It_was_supposed_to_be_a_quiet_weekend_afternoon"
        "_Downloaded_2024_01_01_00_00_00.mp4",
    )
    results = match_scenes([s], [clip], perf)
    assert len(results) == 1, results
    assert results[0].title_score >= 0.85, results[0]


def test_c4s_format_suffix_does_not_block_match() -> None:
    # C4S sellers append format/quality noise; stripping it lets the title stand
    # on its own (>= TITLE_MIN) instead of needing duration/date to corroborate.
    perf = Performer(id="p", name="Nobody")
    clip = Clip(
        title="Sunday Morning Studio Session (HD MP4 VERSION)",
        url="http://x/clip/1",
        source="Clips4Sale",
    )
    s = _scene("1", "Sunday Morning Studio Session.mp4")
    results = match_scenes([s], [clip], perf)
    assert len(results) == 1, results
    assert results[0].title_score >= 0.85, results[0]


def test_leading_honorific_does_not_block_match() -> None:
    # "Goddess <name> <title>" on the store vs "<title>" in the filename: after
    # the name strip exposes the leading honorific, it must be dropped too.
    perf = Performer(id="p", name="Jane")
    clip = Clip(
        title="Goddess Jane Garden Studio Tour",
        url="http://x/clip/1",
        source="IWantClips",
    )
    s = _scene("1", "Garden Studio Tour.mp4")
    results = match_scenes([s], [clip], perf)
    assert len(results) == 1, results
    assert results[0].title_score >= 0.85, results[0]


def test_corroboration_exception_below_gate() -> None:
    # Title alone is below TITLE_MIN (0.85) -- the studio renamed "aunt" to
    # "step-aunt" on this store -- but duration AND date both agree, so the clip
    # qualifies as a (reviewable) medium match.
    perf = Performer(id="p", name="nova blake")
    clip = Clip(
        title="step-aunt's watercolor demonstration",
        url="http://x/1",
        source="LoyalFans",
        duration=600,
        date="2023-05-01",
    )
    s = _scene("9", "aunt-s-watercolor-demonstration.mov", 600)
    s.date = "2023-05-01"
    results = match_scenes([s], [clip], perf)
    assert len(results) == 1, results
    assert results[0].confidence == "medium", results[0]
    assert results[0].title_score < 0.85, results[0]


def test_below_gate_needs_both_corroborators() -> None:
    # Same weak title and matching duration, but no date on either side: the
    # exception requires BOTH duration and date, so this stays unmatched.
    perf = Performer(id="p", name="nova blake")
    clip = Clip(
        title="step-aunt's watercolor demonstration",
        url="http://x/1",
        source="LoyalFans",
        duration=600,
    )
    s = _scene("9", "aunt-s-watercolor-demonstration.mov", 600)
    assert match_scenes([s], [clip], perf) == []


def test_self_standing_title_is_pending_not_rejected() -> None:
    # A title that clears TITLE_MIN (0.85) but lands below TITLE_HIGH (0.90) with
    # no duration/date corroboration must be a reviewable "medium" (pending), not
    # "low" (which _DEFAULT_DECISION auto-rejects, silently dropping a valid match).
    perf = Performer(id="p", name="Nobody")
    clip = Clip(title="A Long Slow Morning", url="http://x/1", source="IWantClips")
    s = _scene("1", "The Long Slow Morning.mp4")  # ~0.857, no duration, no date
    results = match_scenes([s], [clip], perf)
    assert len(results) == 1, results
    assert 0.85 <= results[0].title_score < 0.90, results[0]
    assert results[0].confidence == "medium", results[0]


def test_overlapping_alias_does_not_strand_prefix() -> None:
    # Aliases "Aria Sterling" and "Divine Aria Sterling" overlap. Stripping the
    # short one first must not orphan "Divine" in the query (alias-overlap bug):
    # the filename should reduce to the real title alone, matching the clip.
    perf = Performer(
        id="501",
        name="Aria Sterling",
        aliases=["Divine Aria Sterling", "DivineAriaSterling"],
    )
    clip = Clip(
        title="LE JARDIN", url="http://x/clip/1", source="IWantClips", duration=886
    )
    s = _scene("303", "Divine_Aria_Sterling_LE_JARDIN.mp4", 886)
    results = match_scenes([s], [clip], perf)
    assert len(results) == 1, results
    assert results[0].confidence == "high"


def test_accent_split_filename_matches_clip() -> None:
    # The downloader drops accented chars, splitting a word where the accent was
    # ("Pédagogique" -> "P dagogique"). The orphan letter must reattach so the
    # title still matches the (unaccented) store clip (accent-split bug).
    perf = Performer(id="502", name="Mira Vale")
    clip = Clip(
        title="Morning Pedagogique",
        url="http://x/1",
        source="ManyVids",
        duration=684,
    )
    s = _scene("304", "Mira_Vale_Morning_P_dagogique.mp4", 684)
    results = match_scenes([s], [clip], perf)
    assert len(results) == 1, results
    assert results[0].confidence == "high", results[0]


def test_accent_split_trailing_orphan_matches_clip() -> None:
    # Two accents dropped mid- and end-word ("obsédé" -> "obs d"): the trailing
    # orphan "d" must glue backward ("obsd" ~ "obsede") so the clip matches,
    # while a real extra word like "step-aunt" vs "aunt" must NOT (accent bug).
    perf = Performer(id="501", name="Aria Sterling")
    clip = Clip(
        title="Winter obsédé", url="http://x/1", source="IWantClips", duration=431
    )
    s = _scene("305", "Aria_Sterling_Winter_obs_d_.mp4", 431)
    results = match_scenes([s], [clip], perf)
    assert len(results) == 1, results
    assert results[0].confidence == "high", results[0]


def test_split_possessive_matches_clip() -> None:
    # The downloader turns "Brother's" into "brother s"; the lone "s" must glue
    # back ("brothers") so it matches the clip's possessive, while a real extra
    # word like "step-aunt" vs "aunt" must still NOT match (the gate guard below).
    perf = Performer(id="p", name="Penny Lane")
    clip = Clip(
        title="Brother's Birthday Card",
        url="http://x/1",
        source="APClips",
        duration=600,
    )
    s = _scene("c", "brother s birthday card.mp4", 600)
    results = match_scenes([s], [clip], perf)
    assert len(results) == 1 and results[0].confidence == "high", results


def test_accent_fold_ignores_diacritics() -> None:
    # "Café" must match "Cafe": accents fold before scoring.
    perf = Performer(id="p", name="Nobody")
    clip = Clip(title="Café Crème", url="http://x/1", source="ManyVids", duration=600)
    s = _scene("c", "Cafe Creme.mp4", 600)
    results = match_scenes([s], [clip], perf)
    assert len(results) == 1 and results[0].confidence == "high", results


def test_sequence_number_difference_is_not_a_match() -> None:
    # "Part 2" vs "Part 3": identical but for a trailing number — the guard must
    # zero it so near-duplicate sequels don't get cross-matched.
    assert title_score("Bratty Tease Part 2", "Bratty Tease Part 3", []) == 0.0
    # A genuine title still scores (the guard only fires on a lone numeric diff).
    assert title_score("Bratty Tease Part 2", "Bratty Tease Part 2", []) > 0.9


def test_day_series_numbers_must_agree() -> None:
    # Episode series ("Day 1/2/3"): differing numbers are a hard zero wherever
    # they sit in the title, equal numbers score normally, and an un-numbered
    # side is the ambiguous case -- capped below TITLE_MIN (never self-standing)
    # but above zero so duration+date corroboration can still rescue it.
    assert title_score("Garden Diary Day 2", "Garden Diary Day 3", []) == 0.0
    assert title_score("Garden Diary Day 12", "Garden Diary Day 1", []) == 0.0
    assert title_score("Garden Diary Day 2", "Garden Diary Day 2", []) == 1.0
    assert 0.7 <= title_score("Garden Diary Day 2", "Garden Diary", []) < 0.85
    assert 0.7 <= title_score("Garden Diary", "Garden Diary Day 1", []) < 0.85


def test_numbering_style_variants_still_match() -> None:
    # The same number written differently on the two sides must compare equal:
    # "Day2"/"Pt.2"/"ep.21" vs their spaced twins, and dotted/dashed ranges
    # ("1.2", "1-4") vs the store title's punctuation-stripped form.
    assert title_score("Diary Day2", "Diary Day 2", []) == 1.0
    assert (
        title_score(
            "Sketch'd ep.21: Charcoal Study", "Sketch'd ep.21: Charcoal Study", []
        )
        == 1.0
    )
    assert title_score("Weekend Marathon 1-4", "Weekend Marathon 1-4", []) == 1.0
    assert title_score("Overdrive EXTREME 1.2", "Overdrive EXTREME 1.2", []) == 1.0
    # Leetspeak (censor-dodging digit-for-letter swaps) is NOT a sequence
    # number: "Meditati0n 3" must still match "Meditation 3", not conflict on the 0.
    assert title_score("Morning Meditation 3", "Morning Meditati0n 3", []) > 0.9


def test_sequel_number_missing_from_clip_is_not_high() -> None:
    # The scene is part 2 but this store only carries the un-numbered part 1,
    # with a duration delta inside tolerance. A ~0.95 title used to auto-apply
    # as "high"; the one-sided number cap must reject it (the date is years
    # off, so corroboration can't rescue it).
    perf = Performer(id="p", name="nova blake")
    part1 = Clip(
        title="Evening Pottery Class",
        url="http://mv/1",
        source="ManyVids",
        duration=556,
        date="2023-01-15",
    )
    s = _scene("401", "Nova_Blake_Evening_Pottery_Class_2.mp4", 518)
    s.date = "2026-06-23"
    assert match_scenes([s], [part1], perf) == []


def test_sequel_number_missing_but_corroborated_is_medium() -> None:
    # Some stores title part 2 without the "2". Exact duration AND date
    # agreement must rescue it -- but only as a reviewable "medium", not "high".
    perf = Performer(id="p", name="nova blake")
    clip = Clip(
        title="Evening Pottery Class",
        url="http://lf/1",
        source="LoyalFans",
        duration=518,
        date="2026-06-23",
    )
    s = _scene("401", "Nova_Blake_Evening_Pottery_Class_2.mp4", 518)
    s.date = "2026-06-23"
    results = match_scenes([s], [clip], perf)
    assert len(results) == 1, results
    assert results[0].confidence == "medium", results[0]


def test_numbered_sequel_prefers_its_own_number() -> None:
    # Both parts in the catalog: part 2 must win outright for the part-2 scene.
    perf = Performer(id="p", name="nova blake")
    clips = [
        Clip(
            title="Evening Pottery Class",
            url="http://x/1",
            source="IWantClips",
            duration=556,
        ),
        Clip(
            title="Evening Pottery Class 2",
            url="http://x/2",
            source="IWantClips",
            duration=518,
        ),
    ]
    s = _scene("401", "Nova_Blake_Evening_Pottery_Class_2.mp4", 518)
    results = match_scenes([s], clips, perf)
    assert len(results) == 1, results
    assert results[0].clip.url == "http://x/2"
    assert results[0].confidence == "high"


def test_equal_confidence_prefers_higher_title_score() -> None:
    # Two "medium" candidates (no duration/date corroboration): best_match's
    # tiebreak must keep the exact title over the near-miss.
    perf = Performer(id="p", name="Nobody")
    clips = [
        Clip(title="The Titles", url="http://x/near", source="IWantClips"),
        Clip(title="The Title", url="http://x/exact", source="IWantClips"),
    ]
    results = match_scenes([_scene("A", "The Title.mp4")], clips, perf)
    assert len(results) == 1, results
    assert results[0].clip.url == "http://x/exact"  # exact beat the near-miss
    assert results[0].confidence == "medium"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
