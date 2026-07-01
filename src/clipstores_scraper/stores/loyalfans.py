"""LoyalFans catalog backend.

LoyalFans is an Angular SPA over a JSON REST API; the HTML is an empty shell, so
we talk to the API directly -- no browser. A creator's paid Video Store (the
full-length scenes, the matchable content) is public: ``POST /api/v2/videos-store
{slug}`` returns the whole store catalog for any creator, no login or token
needed. The only setup is an anonymous cookie/XSRF bootstrap (a Laravel
double-submit token), and the API rate-limits bursts by quietly returning an
empty page -- so we pace requests and re-bootstrap if a mid-catalog page comes
back empty.

Stash stores LoyalFans as profile URLs (``loyalfans.com/{handle}``, sometimes
with a ``/store`` or ``/media`` suffix); the handle (first path segment, treated
case-insensitively) is the key. Clip URLs look like
``loyalfans.com/{handle}/video/{clip-slug}``. The subscription feed is skipped --
it's mostly short promos, not scenes.
"""

from __future__ import annotations

import re
import time
import urllib.parse

import httpx

from ..config import Config
from ..models import Clip, SceneData
from .base import UA, Logger, log_incremental, noop

_ORIGIN = "https://www.loyalfans.com"
_API = f"{_ORIGIN}/api/v2"
# The handle is the first path segment; /store, /media and trailing slashes fall
# away because the charset stops at '/'. Profile URLs only -- there are no clip
# URLs in Stash, but a clip URL would harmlessly resolve to its creator.
_HANDLE_RE = re.compile(r"loyalfans\.com/([A-Za-z0-9_.-]+)", re.IGNORECASE)
_CLIP_SLUG_RE = re.compile(r"/video/([^/?#]+)")

_PER_PAGE = 24
_MAX_PAGES = 200  # safety bound (a 4800-clip store is 200 pages)
_PAGE_DELAY = 0.5  # ponytail: pace to dodge the empty-page rate limit


def _session() -> tuple[httpx.Client, dict]:
    """An anonymous LoyalFans session: bootstrap the XSRF cookie, return the
    client plus the headers (Origin + double-submit token) every call needs."""
    client = httpx.Client(
        follow_redirects=True,
        timeout=30.0,
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    # system-status seeds XSRF-TOKEN; it 403s without the Origin header.
    client.post(
        f"{_API}/system-status", headers={"Origin": _ORIGIN, "Referer": _ORIGIN + "/"}
    )
    headers = {"Origin": _ORIGIN, "Referer": _ORIGIN + "/"}
    xsrf = client.cookies.get("XSRF-TOKEN")
    if xsrf:
        headers["X-XSRF-TOKEN"] = urllib.parse.unquote(xsrf)
    return client, headers


def _date(created_at: object) -> str | None:
    """created_at is "YYYY-MM-DD HH:MM:SS" (string) or {date: "..."}; take the day."""
    if isinstance(created_at, dict):
        created_at = created_at.get("date")
    if isinstance(created_at, str) and len(created_at) >= 10:
        return created_at[:10]
    return None


def _to_clip(item: dict) -> Clip | None:
    slug = item.get("slug")
    owner = (item.get("owner") or {}).get("slug")
    if not slug or not owner:
        return None
    duration = (item.get("video_object") or {}).get("duration")
    return Clip(
        title=(item.get("title") or "").strip() or slug,
        url=f"{_ORIGIN}/{owner}/video/{slug}",
        source="LoyalFans",
        duration=int(duration) if isinstance(duration, (int, float)) else None,
        date=_date(item.get("created_at")),
    )


def _clip_slug(url: str) -> str | None:
    m = _CLIP_SLUG_RE.search(url or "")
    return m.group(1) if m else None


class LoyalFansStore:
    name = "loyalfans"
    domain = "loyalfans.com"

    def handles(self, url: str) -> bool:
        return bool(_HANDLE_RE.search(url or ""))

    def store_id(self, url: str) -> str | None:
        m = _HANDLE_RE.search(url or "")
        # Case-fold: the platform treats handles case-insensitively, and Stash
        # stores them mixed-case, so this is the stable cache key + API slug.
        return m.group(1).lower() if m else None

    def catalog(
        self,
        store_url: str,
        config: Config,
        log: Logger = noop,
        known: list[Clip] | None = None,
    ) -> list[Clip]:
        slug = self.store_id(store_url)
        if not slug:
            raise ValueError(f"Not a LoyalFans profile URL: {store_url}")

        # Seed known clips so an incremental rescrape stops once it reaches them;
        # the store lists newest-first.
        clips: dict[str, Clip] = {}
        for c in known or []:
            cs = _clip_slug(c.url)
            if cs:
                clips[cs] = c
        seeded = len(clips)

        client, headers = _session()
        try:
            last_page = 1
            empty_pages = 0
            # 1-based: the page param 0 and 1 both return page 1, so the real
            # range is 1..last_page (param last_page is the final, partial page).
            page = 1
            while page <= last_page and page <= _MAX_PAGES:
                if page > 1:
                    time.sleep(_PAGE_DELAY)
                data = None
                for attempt in range(2):
                    resp = client.post(
                        f"{_API}/videos-store?ngsw-bypass=true",
                        headers=headers,
                        json={"slug": slug, "limit": _PER_PAGE, "page": page},
                    )
                    if resp.status_code != 200:
                        break
                    data = resp.json()
                    items = data.get("list") or []
                    # An empty page (including page 1, the most throttle-exposed
                    # request) means the rate limit kicked in; re-bootstrap a
                    # fresh session and retry once before trusting the empty.
                    if items or attempt == 1:
                        break
                    client.close()
                    client, headers = _session()
                if data is None:
                    break  # non-200, treat as end
                if page == 1:
                    meta = data.get("page_meta") or {}
                    last_page = int(meta.get("last_page") or 1)
                    log(f"  store: {meta.get('total')} clip(s), {last_page} page(s)")
                before = len(clips)
                for item in data.get("list") or []:
                    clip = _to_clip(item)
                    if clip is None:
                        continue
                    cs = _clip_slug(clip.url)
                    if cs and cs not in clips:
                        clips[cs] = clip
                new = len(clips) - before
                log(f"  page {page}: +{new} new (total {len(clips)})")
                if new == 0:
                    empty_pages += 1
                    if empty_pages >= 2:
                        break
                else:
                    empty_pages = 0
                page += 1
        finally:
            client.close()
        log_incremental(log, len(clips), seeded)
        return list(clips.values())

    def detail(self, url: str, config: Config, log: Logger = noop) -> SceneData | None:
        slug = _clip_slug(url)
        if not slug:
            return None
        client, headers = _session()
        try:
            resp = client.get(f"{_ORIGIN}/api/v1/social/post/{slug}", headers=headers)
            if resp.status_code != 200:
                return None
            post = resp.json().get("post") or {}
        finally:
            client.close()
        if not post:
            return None
        return SceneData(
            source="LoyalFans",
            title=(post.get("title") or "").strip() or None,
            date=_date(post.get("created_at")),
            details=_clean_content(post.get("content")),
            cover_url=(post.get("video_object") or {}).get("poster") or None,
            studio=(post.get("owner") or {}).get("display_name") or None,
            tags=[
                t.strip("#. ") for t in (post.get("hashtags") or []) if t.strip("#. ")
            ],
        )


def _clean_content(content: str | None) -> str | None:
    """Drop the trailing #hashtags (we capture them as tags) and tidy ellipses,
    mirroring the community scraper."""
    if not content:
        return None
    content = content.replace("<br />", "\n")
    content = re.sub(r"#\w+\b", "", content).replace(". . .", "...").strip()
    return content or None
