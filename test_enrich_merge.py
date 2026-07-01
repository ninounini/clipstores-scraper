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


def test_tags_union_dedup_case_insensitive():
    a = SceneData(source="IWantClips", tags=["Feet", "POV"])
    b = SceneData(source="Clips4Sale", tags=["feet", "Tease"])
    merged = merge_details([a, b])
    assert merged.tags == ["Feet", "POV", "Tease"]  # first-seen casing, deduped


def test_cover_falls_back_when_top_ranked_fails():
    # IWC outranks ManyVids, but its cover fails to download — enrich must fall
    # through to the next ranked store's cover, not give up (merge_details keeps
    # only one cover_url, so the fallback lives in _first_cover over the raw datas).
    datas = [
        SceneData(source="ManyVids", cover_url="http://ok/mv.jpg"),
        SceneData(source="IWantClips", cover_url="http://bad/iwc.jpg"),
    ]
    tried = []

    def fake_fetch(url):
        tried.append(url)
        return None if "bad" in url else "data:image/jpeg;base64,AAAA"

    orig = pipeline._fetch_cover
    pipeline._fetch_cover = fake_fetch
    try:
        cover = pipeline._first_cover(datas)
    finally:
        pipeline._fetch_cover = orig
    assert cover == "data:image/jpeg;base64,AAAA"
    assert tried == ["http://bad/iwc.jpg", "http://ok/mv.jpg"]  # IWC first, then MV


if __name__ == "__main__":
    test_scalar_rank_and_gap_fill()
    test_tags_union_dedup_case_insensitive()
    test_cover_falls_back_when_top_ranked_fails()
    print("ok")
