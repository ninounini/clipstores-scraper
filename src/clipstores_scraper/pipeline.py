"""Shared scrape -> match -> apply flow, used by both the CLI and the TUI.

Keeping this provider-agnostic logic in one place means the dashboard and the
command line can't drift apart: they call the same triage, matching, and apply
functions.
"""

from __future__ import annotations

import base64
import re

import httpx

from . import cache, state
from .config import Config
from .matching import (
    CONFIDENCE_RANK,
    best_match,
    clean_filename,
    destep_text,
    has_bare_family,
    stepped_count,
    titles_equivalent_under_tos,
    tos_penalty,
)
from .models import Clip, MatchCandidate, Performer, PerformerStatus, Scene, SceneData
from .stash import StashClient
from .stores import UA, Logger, StoreScraper, for_url, noop

# Per-field precedence when a scene matched several stores: best data first.
# All eight backends are ranked so a multi-store merge has a deterministic winner.
_SOURCE_RANK = {
    "IWantClips": 0,
    "ManyVids": 1,
    "Clips4Sale": 2,
    "LoyalFans": 3,
    "GoddessSnow": 4,
    "APClips": 5,
    "YourVids": 6,
    "ModelCentro": 7,
}


def supported_store_urls(performer: Performer) -> list[str]:
    """Every URL of the performer's that a registered backend recognizes, one per
    distinct store. A performer often sells on several sites (IWantClips *and*
    ManyVids, say); each is its own scrape target. Two URLs that resolve to the
    same store (same backend + store id, e.g. a profile linked twice) collapse to
    one so we don't scrape it twice."""
    seen: set[tuple[str, str]] = set()
    out: list[str] = []
    for u in performer.urls:
        store = for_url(u)
        if store is None:
            continue
        key = (store.name, store.store_id(u) or u)
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


def triage(stash: StashClient) -> list[PerformerStatus]:
    """One entry per (performer, store), biggest backlog first.

    GraphQL-only and cheap: no browser, no scraping. This is what the dashboard
    lists, and it answers "who is even worth scraping" before any slow work. A
    performer with two stores yields two entries that scrape and apply
    independently; the backlog count is per performer, so both carry the same
    number.
    """
    out: list[PerformerStatus] = []
    for performer in stash.get_all_performers():
        urls = supported_store_urls(performer)
        if not urls:
            continue
        for url in urls:
            store = for_url(url)
            if store is None:  # supported_store_urls guarantees it; stay defensive
                continue
            out.append(
                PerformerStatus(
                    performer=performer,
                    store_url=url,
                    store_name=store.name,
                    # Per store: scenes lacking *this* store's URL, so the two
                    # rows of a multi-store performer can show different backlogs.
                    unmatched_count=stash.count_unmatched_scenes(
                        performer.id, store.domain
                    ),
                )
            )
    out.sort(key=lambda s: s.unmatched_count, reverse=True)
    return out


# ~31,000 years: a "load the cache regardless of age" sentinel for rescrapes.
_NO_TTL = 10**12


def get_catalog(
    store: StoreScraper,
    url: str,
    config: Config,
    refresh: bool = False,
    incremental: bool = False,
    log: Logger = noop,
) -> list[Clip]:
    """Return the store catalog.

    Default: serve the cache if fresh, else do a full scrape. ``refresh`` forces
    a full scrape. ``incremental`` does a rescrape that only fetches clips newer
    than the cache — it seeds the backend with the cached catalog (any age),
    fetches the new ones, and saves the merged result.
    """
    store_id = store.store_id(url) or url
    if incremental:
        known = cache.load(store.name, store_id, ttl=_NO_TTL) or []
        log(f"Rescraping for new clips (have {len(known)} cached)…")
        clips = store.catalog(url, config, log, known=known)
        cache.save(store.name, store_id, clips)
        return clips
    if not refresh:
        cached = cache.load(store.name, store_id)
        if cached is not None:
            log("Using cached catalog.")
            return cached
    log("Scraping store catalog…")
    clips = store.catalog(url, config, log)
    cache.save(store.name, store_id, clips)
    return clips


def pooled_catalog(
    config: Config, performer: Performer, refresh: bool = False, log: Logger = noop
) -> list[Clip]:
    """Every clip the performer sells, pooled across all their supported stores.
    Cache-served per store unless ``refresh``. The clip's ``source`` says which
    store it came from."""
    clips: list[Clip] = []
    for url in supported_store_urls(performer):
        store = for_url(url)
        if store is None:  # supported_store_urls guarantees it; stay defensive
            continue
        clips += get_catalog(store, url, config, refresh=refresh, log=log)
    return clips


def match_scenes(
    scenes: list[Scene],
    clips: list[Clip],
    performer: Performer,
) -> list[MatchCandidate]:
    """Best candidate clip per scene that clears the matching thresholds.

    A clip can be the best match for more than one scene (near-duplicate files
    like "Tease.mp4" and "Tease (WMV version).mp4"). Keep only the strongest
    scene per clip so one store URL never lands on two different scenes.
    """
    best_per_clip: dict[str, MatchCandidate] = {}
    for scene in scenes:
        query = clean_filename(scene.primary_basename, performer.names)
        match = best_match(scene, query, clips, performer.names)
        if match is None:
            continue
        rank = (CONFIDENCE_RANK[match.confidence], match.title_score)
        prev = best_per_clip.get(match.clip.url)
        if prev is None or rank > (CONFIDENCE_RANK[prev.confidence], prev.title_score):
            best_per_clip[match.clip.url] = match
    return list(best_per_clip.values())


def scrape_and_match(
    stash: StashClient,
    config: Config,
    performer: Performer,
    store_url: str,
    refresh: bool = False,
    incremental: bool = False,
    log: Logger = noop,
) -> tuple[int, list[MatchCandidate]]:
    """Full per-performer flow. Returns (catalog_size, candidates). ``incremental``
    rescrapes only newly added clips (see ``get_catalog``); matching then runs
    over the full merged catalog, so callers persisting results keep prior
    decisions and gain matches for the new clips."""
    store = for_url(store_url)
    if store is None:
        raise ValueError(f"No supported store backend for URL: {store_url}")
    scenes = stash.get_unmatched_scenes(performer.id, store.domain)
    clips = get_catalog(
        store, store_url, config, refresh=refresh, incremental=incremental, log=log
    )
    return len(clips), match_scenes(scenes, clips, performer)


def _new_clips_delta(
    incremental: bool, old_catalog: int | None, new_catalog: int
) -> int | None:
    """How many clips a rescrape added: the merged catalog minus the prior size.
    None unless this was an incremental rescrape with a known prior size."""
    if not incremental or old_catalog is None:
        return None
    return max(0, new_catalog - old_catalog)


def scrape_persist(
    conn,
    config: Config,
    performer_id: str,
    store_url: str,
    *,
    incremental: bool = False,
    refresh: bool = False,
    old_catalog: int | None = None,
    log: Logger = noop,
) -> tuple[int, int, int | None]:
    """Scrape one storefront, persist its matches, and stamp it 'scraped'. The
    shared core of every scrape path — the CLI batch, the TUI single scrape, and
    the TUI batch each wrap this with their own status banner / tallies. Returns
    ``(catalog_size, match_count, new_clips)``. The caller sets 'scraping' first
    (the TUI refreshes its table in between) and records 'error' if this raises."""
    with StashClient(config) as stash:
        performer = stash.get_performer(performer_id)
        catalog_size, results = scrape_and_match(
            stash,
            config,
            performer,
            store_url,
            refresh=refresh,
            incremental=incremental,
            log=log,
        )
    state.save_matches(conn, performer_id, store_url, results)
    new_clips = _new_clips_delta(incremental, old_catalog, catalog_size)
    state.set_status(
        conn,
        performer_id,
        store_url,
        "scraped",
        catalog_count=catalog_size,
        new_clips=new_clips,
    )
    return catalog_size, len(results), new_clips


def enrich_one(stash: StashClient, scene_id: str, config: Config) -> tuple[str, str]:
    """Enrich one scene, returning ``(scene_id, status)`` and never raising, so one
    bad scene can't sink a batch. Shared by the CLI and TUI enrich-all loops."""
    try:
        wrote = enrich_scene(stash, scene_id, config)
        return scene_id, "written" if wrote else "nothing to write"
    except Exception as exc:  # noqa: BLE001 - one bad scene must not sink the batch
        return scene_id, f"FAILED: {type(exc).__name__}: {exc}"


def merge_urls(existing: list[str], new_url: str) -> list[str]:
    """Existing URLs plus new_url, order-preserving and deduped.

    sceneUpdate replaces the whole list, so we must send existing + new or we
    clobber URLs the scene already carries. Re-applying the same match becomes a
    no-op (the new URL is already present), making apply idempotent.
    """
    merged = list(existing)
    if new_url not in merged:
        merged.append(new_url)
    return merged


def apply_url(stash: StashClient, scene_id: str, url: str) -> bool:
    """Add url to a scene, union with its *current* URLs. True if written.

    Reads the live URLs at apply time (not a snapshot) so a decision made
    minutes or days earlier still merges correctly and never clobbers.
    """
    existing = stash.get_scene_urls(scene_id)
    merged = merge_urls(existing, url)
    if merged == existing:
        return False
    stash.set_scene_urls(scene_id, merged)
    return True


def _censor_marks(text: str) -> int:
    """Count word-censoring *-masks in prose ("t****", "**** the urge"),
    skimming past markdown-style emphasis ("**Note:"). Only used to RANK store
    descriptions, so the odd markdown miscount is harmless -- markup appears in
    every store's copy of the same text."""
    return (
        len(re.findall(r"\w\*{2,}", text))
        + len(re.findall(r"\*{2,}(?=[a-z])", text))
        + len(re.findall(r"(?<![\w*])\*{3,}(?![\w*])", text))
    )


def _nonempty(d: SceneData) -> bool:
    """True if a store actually returned something usable (not a bounced/blank page)."""
    return bool(
        d.title or d.date or d.details or d.code or d.cover_url or d.studio or d.tags
    )


def merge_details(items: list[SceneData]) -> SceneData:
    """Combine a scene's per-store metadata. Scalar fields take the highest-ranked
    store that has a value (iwc > manyvids > c4s > loyalfans, lower fills gaps);
    tags are the union across all stores, deduped case-insensitively.

    The title additionally prefers the least TOS-mangled variant: a store that
    shows the real word beats a higher-ranked one whose title is censored
    ("****") or carries a forced "step-" on a family relative."""
    ordered = sorted(items, key=lambda d: _SOURCE_RANK.get(d.source, 99))
    by_title = sorted(
        ordered,
        key=lambda d: (tos_penalty(d.title or ""), _SOURCE_RANK.get(d.source, 99)),
    )
    # Details likewise prefer the least-edited prose: a description without
    # "****" masks beats a higher-ranked one riddled with them, and with masks
    # equal, one without forced step- prefixes beats a stepped one.
    by_details = sorted(
        ordered,
        key=lambda d: (
            _censor_marks(d.details or ""),
            stepped_count(d.details or ""),
            _SOURCE_RANK.get(d.source, 99),
        ),
    )
    picks = {"title": by_title, "details": by_details}
    merged = SceneData(source="merged")
    for field_name in ("title", "date", "details", "code", "cover_url", "studio"):
        for d in picks.get(field_name, ordered):
            value = getattr(d, field_name)
            if value:
                setattr(merged, field_name, value)
                break
    seen: dict[str, str] = {}
    for d in items:
        for tag in d.tags:
            key = tag.strip().lower()
            if key and key not in seen:
                seen[key] = tag.strip()
    merged.tags = list(seen.values())
    return merged


def scene_store_details(
    stash: StashClient, scene_id: str, config: Config, log: Logger = noop
) -> tuple[dict, list[SceneData], SceneData | None]:
    """Scrape every store URL on a scene. Returns (current scene state, per-store
    data, merged data) -- merged is None if nothing could be scraped."""
    state = stash.get_scene_detail(scene_id)
    datas: list[SceneData] = []
    for url in state["urls"]:
        store = for_url(url)
        if store is None:
            continue
        try:
            data = store.detail(url, config, log)
        except Exception as exc:  # noqa: BLE001 - one store fails, keep the rest
            log(f"  {store.name}: detail failed — {type(exc).__name__}: {exc}")
            continue
        # Drop empty results (e.g. an IWC clip page that bounced to the home page):
        # they must not count as data, or the scene gets marked done with nothing.
        if data and _nonempty(data):
            datas.append(data)
    return state, datas, (merge_details(datas) if datas else None)


def enrich_scene(
    stash: StashClient, scene_id: str, config: Config, log: Logger = noop
) -> bool:
    """Scrape a scene's store URL(s), merge by precedence, write full metadata.
    Tags union with the scene's existing ones (never removed); the studio is
    inferred from the performers; the matched performer itself is left untouched.
    True if written."""
    state, datas, merged = scene_store_details(stash, scene_id, config, log)
    if merged is None:
        log("  no store metadata scraped")
        return False
    # A scene title that is the same up to TOS mangling but strictly cleaner
    # (uncensored word recovered from the filename, de-stepped relative) was
    # fixed on purpose; re-enriching must not clobber it with the store's
    # mangled variant.
    if (
        state["title"]
        and merged.title
        and tos_penalty(state["title"]) < tos_penalty(merged.title)
        and titles_equivalent_under_tos(state["title"], merged.title)
    ):
        merged.title = state["title"]
    # When the (final) title names a bare family relative, the seller's
    # original wording is un-stepped -- so the store's forced "step-"s in the
    # description are mangling too, and safe to drop.
    if merged.details and merged.title and has_bare_family(merged.title):
        merged.details = destep_text(merged.details)
    tag_ids = stash.ensure_tags(merged.tags) if merged.tags else []
    marker = [config.enrich_tag] if config.enrich_tag else []
    all_tag_ids = list(dict.fromkeys(state["tag_ids"] + tag_ids + marker))
    # Studio: the studio the scene's performers already use elsewhere. The store's
    # own studio name is inconsistent across sites and just spawns duplicates, so we
    # ignore merged.studio here. None (no clear majority) leaves the studio unset.
    studio_id = stash.studio_for_performers(state["performer_ids"])
    cover = _best_cover(datas)
    stash.update_scene_full(
        scene_id,
        title=merged.title,
        date=merged.date,
        details=merged.details,
        code=merged.code,
        studio_id=studio_id,
        tag_ids=all_tag_ids,
        cover_image=cover,
    )
    return True


def _best_cover(datas: list[SceneData]) -> str | None:
    """The store cover with the most pixels: every store's cover is fetched and
    measured, and the largest wins (stores serve the same art at very different
    resolutions). Rank order (same precedence as the merge) breaks ties and
    unmeasurable images, so with nothing measurable this degrades to the old
    highest-ranked-cover-that-downloads behavior."""
    best: str | None = None
    best_area = -1
    for d in sorted(datas, key=lambda d: _SOURCE_RANK.get(d.source, 99)):
        if not d.cover_url:
            continue
        fetched = _fetch_cover(d.cover_url)
        if fetched is not None and fetched[1] > best_area:
            best, best_area = fetched
    return best


def _image_area(data: bytes) -> int:
    """Pixel area (width x height) read off a PNG/JPEG/GIF/WebP header; 0 if
    unrecognized. Just enough to rank covers by resolution, not a decoder --
    truncated data short-slices to small ints and at worst ranks as 0."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big") * int.from_bytes(data[20:24], "big")
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        while i + 9 <= len(data) and data[i] == 0xFF:
            marker = data[i + 1]
            size = int.from_bytes(data[i + 2 : i + 4], "big")
            if marker == 0xDA:  # start of scan with no SOF seen: give up
                break
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h = int.from_bytes(data[i + 5 : i + 7], "big")
                return h * int.from_bytes(data[i + 7 : i + 9], "big")
            i += 2 + size
        return 0
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return int.from_bytes(data[6:8], "little") * int.from_bytes(
            data[8:10], "little"
        )
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        fmt = data[12:16]
        if fmt == b"VP8X":
            return (int.from_bytes(data[24:27], "little") + 1) * (
                int.from_bytes(data[27:30], "little") + 1
            )
        if fmt == b"VP8 ":
            return (int.from_bytes(data[26:28], "little") & 0x3FFF) * (
                int.from_bytes(data[28:30], "little") & 0x3FFF
            )
        if fmt == b"VP8L":
            dims = int.from_bytes(data[21:25], "little")
            return ((dims & 0x3FFF) + 1) * (((dims >> 14) & 0x3FFF) + 1)
    return 0


def _fetch_cover(url: str) -> tuple[str, int] | None:
    """Download a cover; returns (base64 data URI for sceneUpdate, pixel area),
    or None if it can't be fetched (a missing cover shouldn't fail the whole
    enrich)."""
    try:
        resp = httpx.get(
            url, timeout=30.0, follow_redirects=True, headers={"User-Agent": UA}
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    # Sniff the real type from magic bytes: APClips serves PNGs under a .jpg name
    # with a image/jpeg content-type, so trusting either would mislabel the cover.
    data = resp.content
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif data[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = (resp.headers.get("content-type") or "").split(";")[0].strip()
        if not mime.startswith("image/"):
            mime = "image/jpeg"
    uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    return uri, _image_area(data)
