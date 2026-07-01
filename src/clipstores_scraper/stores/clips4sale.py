"""Clips4Sale catalog backend.

C4S is a Remix app: append ``?_data=<routeId>`` to any page URL and the server
returns that page's loader JSON. So, like ManyVids, no browser is needed -- the
catalog comes back as JSON, a page at a time, over plain HTTP. Cloudflare fronts
the site but doesn't challenge these routes; a desktop User-Agent is enough.

Two kinds of URL are handled:

* ``clips4sale.com/studio/{id}/{slug}`` -- a storefront. Paged directly off the
  studio listing route (20 clips/page, newest-first), keyed by the numeric
  studio id. Also seen id-only, with the ``/studio/`` prefix dropped, or with a
  typo host.
* ``clips4sale.com/performers/{id}/{slug}`` -- a performer profile (clips across
  many studios). The profile route only returns ~12 recent clips and has no
  pagination, so we enumerate via the global clip search keyed on the slug and
  keep the rows that actually feature this performer id (40/page, capped at 100
  pages by the site). Keyed by ``p{id}`` to stay distinct from studio ids.

Clip URLs look like ``clips4sale.com/studio/{studioId}/{clipId}/{slug}``.
"""

from __future__ import annotations

import html
import json
import math
import re
import time
from datetime import datetime
from urllib.parse import quote

import httpx

from ..config import Config
from ..models import Clip, SceneData
from .base import (
    RETRYABLE_STATUS,
    UA,
    Logger,
    get_page,
    log_incremental,
    noop,
    strip_html,
)

_HOST = "https://www.clips4sale.com"
# Studio id: digits right after the host, with or without the /studio/ prefix.
# Deliberately does NOT match /performers/{id} (handled separately below).
_STUDIO_RE = re.compile(r"clips4sale\.com/(?:studio/)?(\d+)", re.IGNORECASE)
_PERFORMER_RE = re.compile(r"clips4sale\.com/performers/(\d+)/([^/?#]+)", re.IGNORECASE)
_CLIP_ID_RE = re.compile(r"/studio/\d+/(\d+)")

# Loader route ids. Passed decoded; httpx percent-encodes them to the exact form
# the server matches. A mismatched route id yields HTTP 403, so these must track
# the C4S Remix build.
_STUDIO_ROUTE = "routes/($lang).studio.$id_.$studioSlug.$"
_CLIP_ROUTE = "routes/($lang).studio.$id_.$clipId.$clipSlug"
_SEARCH_ROUTE = "routes/($lang).clips.search.$"
_CLIP_URL_RE = re.compile(r"/studio/(\d+)/(\d+)/([^/?#]+)")
# Studio paging: {n} is a path segment (?page= is ignored); page size is locked
# at 20 server-side; ClipDate-desc gives stable newest-first.
_STUDIO_PAGE_PATH = "Cat0-AllCategories/Page{n}/ClipDate-desc/Limit24"
_STUDIO_PAGE_SIZE = 20
_SEARCH_MAX_PAGES = 100  # the global search route is capped at 100 pages by C4S

_HEADERS = {"User-Agent": UA, "Accept": "application/json"}
_MAX_PAGES = 500  # safety bound (a 10k-clip store is 500 pages)
_PAGE_DELAY = 0.5  # ponytail: be civil; Cloudflare is passive but watching


def _parse_date(text: str | None) -> str | None:
    """C4S date_display 'M/D/YY h:mm AM/PM' -> ISO 'YYYY-MM-DD'; None if unparsable."""
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%m/%d/%y %I:%M %p").date().isoformat()
    except ValueError:
        return None


def _clean_details(text: str | None) -> str:
    """Like strip_html but keep paragraph breaks: <br> -> newline first."""
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.IGNORECASE)
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _detail_tags(clip: dict) -> list[str]:
    """Category + related categories + keywords, mirroring the community scraper."""
    out: list[str] = []
    if cat := clip.get("category_name"):
        out.append(cat)
    out += [
        r["category"]
        for r in clip.get("related_category_links") or []
        if r.get("category")
    ]
    out += [k["keyword"] for k in clip.get("keyword_links") or [] if k.get("keyword")]
    return [t.strip() for t in out if t.strip()]


def _to_clip(c: dict) -> Clip | None:
    link = c.get("link") or ""
    if not link:
        return None
    url = link if link.startswith("http") else _HOST + link
    minutes = c.get("time_minutes")
    if minutes is None:
        minutes = c.get("duration")
    try:
        duration = int(minutes) * 60 if minutes is not None else None
    except (TypeError, ValueError):
        duration = None
    return Clip(
        title=strip_html(c.get("title")) or url,
        url=url,
        source="Clips4Sale",
        duration=duration,
        date=_parse_date(c.get("date_display") or c.get("dateDisplay")),
    )


def _clip_id(url: str) -> str | None:
    m = _CLIP_ID_RE.search(url or "")
    return m.group(1) if m else None


def _seed(known: list[Clip] | None) -> dict[str, Clip]:
    """Known clips keyed by clip id, so an incremental rescrape stops once it
    reaches them (the listings are newest-first)."""
    clips: dict[str, Clip] = {}
    for c in known or []:
        cid = _clip_id(c.url)
        if cid:
            clips[cid] = c
    return clips


def _absorb(
    clips: dict[str, Clip], rows: list[dict], performer_id: str | None = None
) -> int:
    """Add new clips from ``rows`` to ``clips``; return how many were new. When
    ``performer_id`` is given (performer search), keep only rows that feature it."""
    before = len(clips)
    for rec in rows:
        if performer_id is not None:
            ids = {str(p.get("id")) for p in (rec.get("performers") or [])}
            if performer_id not in ids:
                continue
        clip = _to_clip(rec)
        if clip is None:
            continue
        cid = _clip_id(clip.url)
        if cid and cid not in clips:
            clips[cid] = clip
    return len(clips) - before


def _studio_total(data: dict) -> int:
    count = int(data.get("clipsCount") or 0)
    return math.ceil(count / _STUDIO_PAGE_SIZE) if count else _MAX_PAGES


def _search_total(data: dict) -> int:
    pages = int(data.get("totalClipPages") or 0)
    return min(pages, _SEARCH_MAX_PAGES) if pages else _SEARCH_MAX_PAGES


def _page_loop(client, page_url, route, total_from, log, known, performer_id=None):
    """Walk a paged _data route, collecting clips until the page count is reached
    or two pages in a row add nothing new (covers a transient empty page and the
    point where an incremental rescrape catches up to known clips)."""
    clips = _seed(known)
    seeded = len(clips)
    total = _MAX_PAGES
    empty_pages = 0
    for page_no in range(1, _MAX_PAGES + 1):
        if page_no > 1:
            time.sleep(_PAGE_DELAY)
        resp = get_page(client, page_url(page_no), params={"_data": route})
        if resp.status_code != 200:
            # A page-1 failure (e.g. the route-id drift this file warns about → 403)
            # is a real error, not "0 clips"; and a transient 429/5xx that survives a
            # retry must not truncate + cache the catalog. Both raise so the empty or
            # short result never gets cached as a valid catalog.
            if page_no == 1 or resp.status_code in RETRYABLE_STATUS:
                resp.raise_for_status()
            break  # past the last page
        data = resp.json()
        if page_no == 1:
            total = total_from(data)
            log(f"  ~{total} page(s) to scan")
        new = _absorb(clips, data.get("clips") or [], performer_id)
        log(f"  page {page_no}: +{new} new (total {len(clips)})")
        if new == 0:
            empty_pages += 1
            if empty_pages >= 2:
                break
        else:
            empty_pages = 0
        if page_no >= total:
            break
    log_incremental(log, len(clips), seeded)
    return list(clips.values())


class Clips4SaleStore:
    name = "clips4sale"
    domain = "clips4sale.com"

    def handles(self, url: str) -> bool:
        url = url or ""
        return bool(_PERFORMER_RE.search(url) or _STUDIO_RE.search(url))

    def store_id(self, url: str) -> str | None:
        pm = _PERFORMER_RE.search(url or "")
        if pm:
            return f"p{pm.group(1)}"  # distinct from studio ids in the cache
        sm = _STUDIO_RE.search(url or "")
        return sm.group(1) if sm else None

    def catalog(
        self,
        store_url: str,
        config: Config,
        log: Logger = noop,
        known: list[Clip] | None = None,
    ) -> list[Clip]:
        pm = _PERFORMER_RE.search(store_url or "")
        if pm:
            return self._performer_catalog(pm.group(1), pm.group(2), log, known)
        sid = self.store_id(store_url)
        if not sid:
            raise ValueError(f"Not a Clips4Sale URL: {store_url}")
        return self._studio_catalog(sid, log, known)

    def detail(self, url: str, config: Config, log: Logger = noop) -> SceneData | None:
        m = _CLIP_URL_RE.search(url or "")
        if not m:
            return None
        sid, cid, slug = m.group(1), m.group(2), m.group(3)
        with httpx.Client(
            headers=_HEADERS, follow_redirects=True, timeout=30.0
        ) as client:
            resp = client.get(
                f"{_HOST}/studio/{sid}/{cid}/{slug}", params={"_data": _CLIP_ROUTE}
            )
        if resp.status_code != 200:
            return None
        # The clip route returns newline-delimited JSON; the first line is the loader.
        data = json.loads(resp.text.splitlines()[0])
        clip = data.get("clip") or {}
        if not clip:
            return None
        cover = clip.get("cdn_previewlg_link") or clip.get("previewLink")
        return SceneData(
            source="Clips4Sale",
            title=strip_html(clip.get("title")) or None,
            date=_parse_date(clip.get("date_display")),
            details=_clean_details(clip.get("description")) or None,
            code=str(clip["id"]) if clip.get("id") else None,
            cover_url=cover or None,
            studio=(clip.get("studio") or {}).get("name") or None,
            tags=_detail_tags(clip),
        )

    def _studio_catalog(
        self, sid: str, log: Logger, known: list[Clip] | None
    ) -> list[Clip]:
        with httpx.Client(
            headers=_HEADERS, follow_redirects=True, timeout=30.0
        ) as client:
            # The slug is cosmetic but must be in the path; id-only redirects to
            # the canonical slug, so resolve it once.
            head = client.get(f"{_HOST}/studio/{sid}")
            head.raise_for_status()
            m = re.search(r"/studio/\d+/([^/?#]+)", str(head.url))
            slug = m.group(1) if m else "store"
            base = f"{_HOST}/studio/{sid}/{slug}"
            return _page_loop(
                client,
                lambda n: f"{base}/{_STUDIO_PAGE_PATH.format(n=n)}",
                _STUDIO_ROUTE,
                _studio_total,
                log,
                known,
            )

    def _performer_catalog(
        self, pid: str, slug: str, log: Logger, known: list[Clip] | None
    ) -> list[Clip]:
        kw = quote(slug, safe="")
        with httpx.Client(
            headers=_HEADERS, follow_redirects=True, timeout=30.0
        ) as client:
            return _page_loop(
                client,
                lambda n: (
                    f"{_HOST}/clips/search/{kw}/category/0/storesPage/1/clipsPage/{n}"
                ),
                _SEARCH_ROUTE,
                _search_total,
                log,
                known,
                performer_id=pid,
            )
