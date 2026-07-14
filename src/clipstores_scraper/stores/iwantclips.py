"""IWantClips backend -- all over HTTP, no browser.

The store page is a JS app, but its clip list comes from a Typesense search
index: the page embeds a short-lived, geo-scoped Typesense API key, and the
listing is a ``multi_search`` filtered by ``member_id``. We read a fresh key off
the store page and query Typesense directly, which returns the whole catalog
(verified to match the rendered grid exactly) with title, exact duration and
publish date -- richer than the old card scrape.

Per-clip detail (description, tags, cover, studio) comes from ``detail()``, which
fetches the clip page's server-rendered HTML. A removed clip 302s to /store, so
its page has no clip <h1> and detail returns None (the merge falls back).

Egress (incl. the index's geo scoping) is handled by a system VPN on the host.
Clip URLs look like:
    https://iwantclips.com/store/<store_id>/<slug>/<clip_id>/<Title-Slug>
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx

from ..config import Config
from ..models import Clip, SceneData
from .base import UA, Logger, hms_to_seconds, log_incremental, noop, strip_html

_STORE_RE = re.compile(r"iwantclips\.com/store/(\d+)/([^/?#]+)", re.IGNORECASE)
_COOKIES = {"iwc-new-design": "on"}
_MAX_PAGES = 200  # safety bound (per_page 250 -> up to 50k clips)

# The store page embeds the Typesense host + a fresh, geo-scoped, expiring search
# key (TypesenseInstantSearchAdapter({server:{apiKey:'...', nodes:[{host:'...'}]}})).
_TS_HOST_RE = re.compile(r"host:\s*'([a-z0-9.]+\.typesense\.net)'")
_TS_KEY_RE = re.compile(r"apiKey:\s*'([A-Za-z0-9%+/=]{40,})'")
_TS_PER_PAGE = 250  # Typesense max page size


def _clip_id(url: str, sid: str) -> str | None:
    """The numeric clip id from a store URL, used to dedupe against the cache."""
    m = re.search(rf"/store/{re.escape(sid)}/[^/]+/(\d+)", url or "")
    return m.group(1) if m else None


def _ts_date(ts: object) -> str | None:
    """A unix timestamp -> ISO date (UTC); None if absent/unparseable."""
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _to_clip(doc: dict) -> Clip:
    url = doc.get("content_url") or (
        "https://iwantclips.com/" + (doc.get("content_path") or "").lstrip("/")
    )
    # The index sometimes returns dev-environment URLs in content_url; those don't
    # exist on the public site (enrichment 404s), so normalize to production.
    url = url.replace("staging.iwantclips.dev", "iwantclips.com")
    return Clip(
        title=(doc.get("title") or "").strip() or url,
        url=url,
        source="IWantClips",
        duration=hms_to_seconds(doc.get("video_length")),
        date=_ts_date(doc.get("publish_time") or doc.get("publish_date")),
    )


def _typesense_config(store_id: str) -> tuple[str, str]:
    """Read a fresh (host, search-key) pair off the store page."""
    url = f"https://iwantclips.com/store/{store_id}/_"
    with httpx.Client(
        headers={"User-Agent": UA},
        cookies=_COOKIES,
        follow_redirects=True,
        timeout=30.0,
    ) as client:
        page = client.get(url).text
    host = _TS_HOST_RE.search(page)
    key = _TS_KEY_RE.search(page)
    if not host or not key:
        raise RuntimeError(
            f"Could not read IWantClips' Typesense config from {url} -- "
            "the store page layout may have changed."
        )
    return host.group(1), key.group(1)


def _ts_search(client: httpx.Client, host: str, key: str, sid: str, page: int) -> dict:
    """One page of a store's clips from the Typesense index (newest first)."""
    body = {
        "searches": [
            {
                "collection": "prod_content",
                "q": "*",
                "query_by": "title",
                "filter_by": f"member_id:{sid}",
                "sort_by": "publish_time:desc",
                "per_page": _TS_PER_PAGE,
                "page": page,
            }
        ]
    }
    resp = client.post(
        f"https://{host}/multi_search",
        headers={"X-TYPESENSE-API-KEY": key},
        json=body,
    )
    resp.raise_for_status()
    return resp.json()["results"][0]


# --- per-clip detail: parse the clip page's server-rendered HTML ---------------


def _parse_date(text: str | None) -> str | None:
    """IWC's 'Published Jan 2, 2006' (or bare 'Jan 2, 2006') -> ISO; None if not."""
    if not text:
        return None
    m = re.search(r"Published\s+(.+)", text)
    s = (m.group(1) if m else text).strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _clean_tags(raw_tags: list[str] | None) -> list[str]:
    """IWC renders hashtags and a 'Keywords: a, b, c' block as element text full of
    commas and newlines (and repeats them across layouts). Flatten to individual,
    deduped tag names -- the comma split + label strip the community scraper does."""
    joined = re.sub(r"Keywords:", "", ",".join(raw_tags or []), flags=re.IGNORECASE)
    out: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,\n]+", joined):
        tag = part.strip()
        key = tag.lower()
        if tag and key not in seen:
            seen.add(key)
            out.append(tag)
    return out


def _cover_url(image: str | None) -> str | None:
    """og:image is often an animated .gif/.mp4 preview; the still frame is the
    same name prefixed 't_' with a .jpg extension (mirrors the community scraper)."""
    if not image:
        return None
    m = re.search(r"([^/]*_[^/]*\.(?:gif|mp4))$", image)
    if m and not m.group(1).startswith("t_"):
        image = image[: m.start()] + "t_" + m.group(1)
    return re.sub(r"\.(?:gif|mp4)$", ".jpg", image)


_TITLE_RE = re.compile(r'<h1[^>]*class="no-style"[^>]*>(.*?)</h1>', re.S)
_DATE_RE = re.compile(r'date fix"[^>]*>.*?<em>(.*?)</em>', re.S)
_OGIMAGE_RE = re.compile(r'<meta[^>]*name="og:image"[^>]*content="([^"]+)"')
_MODEL_RE = re.compile(r'class="modelLink"[^>]*>(.*?)</a>', re.S)
# IWC renders the description twice: a ~100-char truncated teaser
# (js-description, with a "more" toggle) and the full text in a hidden
# js-full-description span. Prefer the full one; fall back to the teaser for any
# layout that omits it (e.g. a short desc that never needed truncating).
_FULL_DESC_RE = re.compile(r'<span class="js-full-description[^"]*">(.*?)</span>', re.S)
_DESC_RE = re.compile(r'<span class="js-description">(.*?)</span>', re.S)
_HASHTAGS_RE = re.compile(r'hashtags[^"]*fix"[^>]*>(.*?)</div>', re.S)
_CATEGORY_RE = re.compile(r'category fix"[^>]*>(.*?)</div>', re.S)
_EM_RE = re.compile(r"<em>(.*?)</em>", re.S)
_A_RE = re.compile(r"<a [^>]*>(.*?)</a>", re.S)


def _parse_clip_html(html_text: str) -> SceneData | None:
    """Parse a clip page's HTML into SceneData, or None if it isn't a clip page
    (a removed/unavailable clip 302s to /store or home -- no clip <h1> there)."""
    title = _TITLE_RE.search(html_text)
    if not title:
        return None
    raw_tags: list[str] = []
    if h := _HASHTAGS_RE.search(html_text):
        raw_tags += [strip_html(e) for e in _EM_RE.findall(h.group(1))]
    if cat := _CATEGORY_RE.search(html_text):
        raw_tags += [strip_html(a) for a in _A_RE.findall(cat.group(1))]
    date = _DATE_RE.search(html_text)
    cover = _OGIMAGE_RE.search(html_text)
    model = _MODEL_RE.search(html_text)
    desc = _FULL_DESC_RE.search(html_text) or _DESC_RE.search(html_text)
    details = (
        strip_html(re.sub(r"<br\s*/?>", "\n", desc.group(1), flags=re.I))
        if desc
        else None
    )
    return SceneData(
        source="IWantClips",
        title=strip_html(title.group(1)) or None,
        date=_parse_date(strip_html(date.group(1))) if date else None,
        details=details or None,
        cover_url=_cover_url(cover.group(1)) if cover else None,
        studio=(strip_html(model.group(1)) or None) if model else None,
        tags=_clean_tags(raw_tags),
    )


class IWantClipsStore:
    name = "iwantclips"
    domain = "iwantclips.com"

    def handles(self, url: str) -> bool:
        return bool(_STORE_RE.search(url or ""))

    def store_id(self, url: str) -> str | None:
        m = _STORE_RE.search(url or "")
        return m.group(1) if m else None

    def catalog(
        self,
        store_url: str,
        config: Config,
        log: Logger = noop,
        known: list[Clip] | None = None,
    ) -> list[Clip]:
        sid = self.store_id(store_url)
        if not sid:
            raise ValueError(f"Not an IWantClips store URL: {store_url}")
        host, key = _typesense_config(sid)
        # Seed known clips so a clip removed from the index since last time is kept.
        # The index stays authoritative for clips it still returns: a hit overwrites
        # the seed below, so a rescrape refreshes stale duration/date (e.g. clips
        # carried over from the pre-Typesense card scrape, which lacked both).
        clips: dict[str, Clip] = {}
        for c in known or []:
            if cid := _clip_id(c.url, sid):
                clips[cid] = c
        seeded = len(clips)
        with httpx.Client(headers={"User-Agent": UA}, timeout=30.0) as client:
            page = 1
            while page <= _MAX_PAGES:
                res = _ts_search(client, host, key, sid, page)
                found = res.get("found") or 0
                hits = res.get("hits") or []
                if page == 1:
                    log(f"  {found} clip(s) in the index")
                before = len(clips)
                for hit in hits:
                    doc = hit.get("document") or {}
                    cid = str(doc.get("content_id") or "")
                    if cid:
                        clips[cid] = _to_clip(doc)
                log(f"  page {page}: +{len(clips) - before} new (total {len(clips)})")
                if not hits or page * _TS_PER_PAGE >= found:
                    break
                page += 1
        log_incremental(log, len(clips), seeded)
        return list(clips.values())

    def detail(self, url: str, config: Config, log: Logger = noop) -> SceneData | None:
        clip_url = url.split("?")[0]
        try:
            with httpx.Client(
                headers={"User-Agent": UA},
                cookies=_COOKIES,
                follow_redirects=True,
                timeout=30.0,
            ) as client:
                resp = client.get(clip_url)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        return _parse_clip_html(resp.text)
