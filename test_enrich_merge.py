"""Merge precedence for multi-store scene enrichment."""

from clipstores_scraper import pipeline
from clipstores_scraper.models import SceneData
from clipstores_scraper.pipeline import merge_details


def test_scalar_rank_and_gap_fill():
    iwc = SceneData(source="IWantClips", title="IWC Title", details="iwc details")
    mv = SceneData(source="ManyVids", title="MV Title", code="999", date="2024-01-02")
    c4s = SceneData(source="Clips4Sale", title="C4S Title", studio="C4S Studio")
    merged = merge_details([c4s, mv, iwc])  # input order must not matter
    assert merged.title == "IWC Title"  # iwc outranks all
    assert merged.code == "999"  # iwc/c4s lack it -> manyvids fills the gap
    assert merged.date == "2024-01-02"  # only manyvids has it
    assert merged.studio == "C4S Studio"  # only c4s has it
    assert merged.details == "iwc details"


def test_title_prefers_least_tos_mangled():
    # IWC outranks LoyalFans, but its title is censored / force-stepped; the
    # clean LoyalFans title must win. Other scalars keep pure rank order.
    iwc = SceneData(source="IWantClips", title="**** Oily Soles", details="iwc")
    lf = SceneData(source="LoyalFans", title="Mesmerizing Oily Soles", details="lf")
    merged = merge_details([iwc, lf])
    assert merged.title == "Mesmerizing Oily Soles"
    assert merged.details == "iwc"  # rank order untouched for other fields

    iwc = SceneData(source="IWantClips", title="Step-Mommy Movie Night")
    lf = SceneData(source="LoyalFans", title="Mommy Movie Night")
    assert merge_details([iwc, lf]).title == "Mommy Movie Night"
    # A censored title (weight 2) loses to a merely stepped one (weight 1).
    mv = SceneData(source="ManyVids", title="**** Movie Night")
    iwc = SceneData(source="IWantClips", title="Step-Mommy Movie Night")
    assert merge_details([mv, iwc]).title == "Step-Mommy Movie Night"


def test_titles_equivalent_under_tos():
    from clipstores_scraper.matching import titles_equivalent_under_tos as eq

    assert eq("Mommy Movie Night", "Step-Mommy Movie Night")
    assert eq("Mesmerizing High Heels", "**** High Heels")
    assert eq("Bro Tricks Sis into Breeding", "Step-Bro Tricks Step-Sis into Breeding")
    assert not eq("Garden Notes", "Kitchen Notes")
    assert not eq("Mommy Movie Night", "Mommy Game Night")
    # Different words hidden by the same mask are not provably equal -- but the
    # wildcard only vouches for the censored side vs a clear side.
    assert not eq("**** Night", "**** Day")
    # Euphemism swaps (mesmerize vs hypnotic) are NOT equivalence: recovery
    # must come from a real source (another store / the filename), never from
    # a word-substitution dictionary.
    assert not eq("Hypnotic Submission", "Mesmerize Submission")


def test_destep_and_family_evidence():
    from clipstores_scraper.matching import destep_text, has_bare_family

    assert destep_text("Stepmom Won't Fuck Small Dicked Stepson") == (
        "Mom Won't Fuck Small Dicked Son"
    )
    assert destep_text("for step-Mommy and STEP-DADDY") == "for Mommy and DADDY"
    assert has_bare_family("Mommy Movie Night")
    assert not has_bare_family("Step-Mommy Movie Night")
    assert not has_bare_family("Garden Notes")


def test_details_prefer_unstepped_prose():
    # Equal on censor masks (none), so the un-stepped LoyalFans description
    # must beat higher-ranked IWC's step-forced prose.
    iwc = SceneData(source="IWantClips", details="Your step-sister turns up.")
    lf = SceneData(source="LoyalFans", details="Your sister turns up.")
    assert merge_details([iwc, lf]).details == "Your sister turns up."


def test_details_prefer_fewest_censor_masks():
    # IWC outranks LoyalFans but its description is riddled with masks; the
    # clean LoyalFans prose must win, while markdown "**Note:" is not a mask.
    iwc = SceneData(
        source="IWantClips", details="You will **** the urge and s*** deeply."
    )
    lf = SceneData(
        source="LoyalFans",
        details="You will fight the urge and sink deeply. **Note: custom.",
    )
    assert merge_details([iwc, lf]).details.startswith("You will fight")


def test_tags_union_dedup_case_insensitive():
    a = SceneData(source="IWantClips", tags=["Feet", "POV"])
    b = SceneData(source="Clips4Sale", tags=["feet", "Tease"])
    merged = merge_details([a, b])
    assert merged.tags == ["Feet", "POV", "Tease"]  # first-seen casing, deduped


def _pick_cover(datas, fetches):
    """Run _best_cover with a faked _fetch_cover; fetches maps url -> result."""
    tried = []

    def fake_fetch(url):
        tried.append(url)
        return fetches[url]

    orig = pipeline._fetch_cover
    pipeline._fetch_cover = fake_fetch
    try:
        return pipeline._best_cover(datas), tried
    finally:
        pipeline._fetch_cover = orig


def test_cover_falls_back_when_top_ranked_fails():
    # IWC outranks ManyVids, but its cover fails to download — enrich must fall
    # through to the next ranked store's cover, not give up (merge_details keeps
    # only one cover_url, so the fallback lives in _best_cover over the raw datas).
    datas = [
        SceneData(source="ManyVids", cover_url="http://ok/mv.jpg"),
        SceneData(source="IWantClips", cover_url="http://bad/iwc.jpg"),
    ]
    cover, tried = _pick_cover(
        datas, {"http://bad/iwc.jpg": None, "http://ok/mv.jpg": ("data:mv", 100)}
    )
    assert cover == "data:mv"
    assert tried == ["http://bad/iwc.jpg", "http://ok/mv.jpg"]  # IWC first, then MV


def test_cover_highest_resolution_wins():
    # A scene with iwc + c4s + mv links: c4s serves the biggest image, so its
    # cover wins even though iwc outranks it in the merge.
    datas = [
        SceneData(source="IWantClips", cover_url="http://iwc"),
        SceneData(source="Clips4Sale", cover_url="http://c4s"),
        SceneData(source="ManyVids", cover_url="http://mv"),
    ]
    cover, tried = _pick_cover(
        datas,
        {
            "http://iwc": ("data:iwc", 640 * 360),
            "http://mv": ("data:mv", 800 * 450),
            "http://c4s": ("data:c4s", 1920 * 1080),
        },
    )
    assert cover == "data:c4s"
    assert len(tried) == 3  # every candidate was fetched and measured


def test_cover_rank_breaks_resolution_ties():
    # Same area (incl. the both-unmeasurable 0/0 case): the higher-ranked store wins.
    datas = [
        SceneData(source="ManyVids", cover_url="http://mv"),
        SceneData(source="IWantClips", cover_url="http://iwc"),
    ]
    cover, _ = _pick_cover(
        datas, {"http://iwc": ("data:iwc", 0), "http://mv": ("data:mv", 0)}
    )
    assert cover == "data:iwc"


def test_image_area_parses_common_headers():
    png = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (640).to_bytes(4, "big")
        + (480).to_bytes(4, "big")
    )
    jpeg = (
        b"\xff\xd8"  # SOI
        + b"\xff\xe0\x00\x10"
        + b"\x00" * 14  # APP0, length 16
        + b"\xff\xc0\x00\x11\x08"
        + (480).to_bytes(2, "big")
        + (640).to_bytes(2, "big")  # SOF0
    )
    gif = b"GIF89a" + (640).to_bytes(2, "little") + (480).to_bytes(2, "little")
    webp = (
        b"RIFF\x00\x00\x00\x00WEBPVP8X\x0a\x00\x00\x00"
        + b"\x00" * 4
        + (639).to_bytes(3, "little")
        + (479).to_bytes(3, "little")
    )
    for blob in (png, jpeg, gif, webp):
        assert pipeline._image_area(blob) == 640 * 480, blob[:12]
    assert pipeline._image_area(b"not an image") == 0
    assert pipeline._image_area(png[:10]) == 0  # truncated: rank as unmeasurable


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
