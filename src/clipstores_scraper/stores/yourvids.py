"""YourVids (yourvids.com) backend.

A Laravel site with a public JSON catalog API: ``GET /api/creators/{slug}/videos
?page=N`` pages through a creator's vids (title, clip URL, MM:SS duration,
created_at), no login needed. Stash stores the profile URL
(``yourvids.com/creators/{slug}``, sometimes with a ``#videos`` fragment); the
slug is the key. Clip URLs look like ``yourvids.com/vids/{clip-slug}``.

The catalog isn't strictly newest-first (creators can pin/feature clips), so an
incremental rescrape can't stop at the first known clip; ``known`` is ignored
and every scrape is full — at 20 clips/page that's a handful of requests.

``detail()`` reads the clip page: schema.org JSON-LD carries the title, cover
and numeric clip id, but its ``description`` is truncated and its ``uploadDate``
drifts from the catalog's created_at — so the full description comes from the
page body and the date from the ``video:release_date`` meta (which matches the
catalog).
"""

from __future__ import annotations

import json
import re
import urllib.parse

import httpx

from ..config import Config
from ..models import Clip, SceneData
from .base import UA, Logger, get_page, hms_to_seconds, noop

_ORIGIN = "https://yourvids.com"
_MAX_PAGES = 500  # safety bound (a 10,000-clip store is 500 pages)

_JSONLD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_EMBED_ID_RE = re.compile(r"/vids/(\d+)/embed")
_RELEASE_RE = re.compile(r'property="video:release_date" content="([^"]+)"')
# The hidden full-description block; the visible one is truncated with a "…".
_FULL_DESC_RE = re.compile(
    r'id="desktopDescriptionFull".*?class="rich-text-content[^"]*"[^>]*>(.*?)</div>',
    re.S,
)
_ANY_DESC_RE = re.compile(r'class="rich-text-content[^"]*"[^>]*>(.*?)</div>', re.S)
_TAG_RE = re.compile(r'href="[^"]*[?&]tag%5B%5D=([^"&]+)"')


def _path_parts(url: str) -> list[str] | None:
    """The path segments of a yourvids.com URL; None for other hosts."""
    p = urllib.parse.urlparse(url or "")
    host = p.netloc.lower()
    if host != "yourvids.com" and not host.endswith(".yourvids.com"):
        return None
    return [seg for seg in p.path.split("/") if seg]


def _creator_slug(url: str) -> str | None:
    """The creator slug from a profile URL (`/creators/{slug}`, any sub-path or
    ``#videos`` fragment); None for anything else, including clip URLs."""
    parts = _path_parts(url)
    if parts and len(parts) >= 2 and parts[0] == "creators":
        return parts[1].lower()
    return None


def _is_clip_url(url: str) -> bool:
    """True for clip pages (`/vids/{clip-slug}`) — what a matched scene links."""
    parts = _path_parts(url)
    return bool(parts and len(parts) >= 2 and parts[0] == "vids")


def _date(created_at: object) -> str | None:
    """'YYYY-MM-DD HH:MM:SS' (or ISO 8601) -> the day."""
    if isinstance(created_at, str) and len(created_at) >= 10:
        return created_at[:10]
    return None


def _to_clip(item: dict) -> Clip | None:
    url = item.get("video_url")
    if not url:
        return None
    return Clip(
        title=(item.get("title") or "").strip() or url.rstrip("/").rsplit("/", 1)[-1],
        url=url,
        source="YourVids",
        duration=hms_to_seconds(item.get("duration")),  # "113:43" == 113 min
        date=_date(item.get("created_at")),
    )


def _clean_text(fragment: str) -> str | None:
    """Scraped rich-text HTML -> plain text with paragraph breaks kept."""
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", fragment)
    text = re.sub(r"<[^>]+>", "", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


class YourVidsStore:
    name = "yourvids"
    domain = "yourvids.com"

    def handles(self, url: str) -> bool:
        # Both URL shapes: creator profiles (scrape targets on performers) and
        # clip pages (what enrich resolves from a matched scene's URL).
        return _creator_slug(url) is not None or _is_clip_url(url)

    def store_id(self, url: str) -> str | None:
        # None for clip URLs: the creator isn't in the URL. Callers fall back
        # to the URL itself as the cache key.
        return _creator_slug(url)

    def catalog(
        self,
        store_url: str,
        config: Config,
        log: Logger = noop,
        known: list[Clip] | None = None,
    ) -> list[Clip]:
        slug = self.store_id(store_url)
        if not slug:
            raise ValueError(f"Not a YourVids creator URL: {store_url}")
        clips: dict[str, Clip] = {}
        with httpx.Client(
            headers={"User-Agent": UA, "Accept": "application/json"},
            follow_redirects=True,
            timeout=30.0,
        ) as client:
            page, last = 1, 1
            while page <= last and page <= _MAX_PAGES:
                resp = get_page(
                    client,
                    f"{_ORIGIN}/api/creators/{slug}/videos",
                    params={"page": page},
                )
                # Raise rather than break: a still-transient error mid-catalog
                # must not cache a partial catalog as complete.
                resp.raise_for_status()
                data = (resp.json() or {}).get("data") or {}
                if page == 1:
                    meta = data.get("pagination") or {}
                    last = int(meta.get("total_pages") or 1)
                    log(f"  store: {meta.get('total')} clip(s), {last} page(s)")
                before = len(clips)
                for item in data.get("videos") or []:
                    clip = _to_clip(item)
                    if clip is not None and clip.url not in clips:
                        clips[clip.url] = clip
                log(f"  page {page}: +{len(clips) - before} new (total {len(clips)})")
                page += 1
        return list(clips.values())

    def detail(self, url: str, config: Config, log: Logger = noop) -> SceneData | None:
        try:
            with httpx.Client(
                headers={"User-Agent": UA}, follow_redirects=True, timeout=30.0
            ) as client:
                resp = client.get(url)
                resp.raise_for_status()
        except httpx.HTTPError:
            return None
        h = resp.text
        ld = _jsonld(h)
        title = (ld.get("name") or "").strip() or None
        if not title:
            return None
        code = _EMBED_ID_RE.search(ld.get("embedUrl") or "")
        release = _RELEASE_RE.search(h)
        date = (_date(release.group(1)) if release else None) or _date(
            ld.get("uploadDate")
        )
        desc = _FULL_DESC_RE.search(h)
        if desc:
            details = _clean_text(desc.group(1))
        else:  # no hidden block on short descriptions; take the longest visible one
            fragments = _ANY_DESC_RE.findall(h)
            details = _clean_text(max(fragments, key=len)) if fragments else None
        # Tag links repeat (desktop + mobile layouts); dedupe, preserve order.
        tags = list(
            dict.fromkeys(urllib.parse.unquote(t).strip() for t in _TAG_RE.findall(h))
        )
        return SceneData(
            source="YourVids",
            title=title,
            date=date,
            details=details,
            code=code.group(1) if code else None,
            cover_url=ld.get("thumbnailUrl") or None,
            studio=(ld.get("author") or {}).get("name") or None,
            tags=[t for t in tags if t],
        )


def _jsonld(h: str) -> dict:
    """The page's schema.org VideoObject, or {} if absent/unparseable."""
    m = _JSONLD_RE.search(h)
    if not m:
        return {}
    try:
        ld = json.loads(m.group(1))
    except ValueError:
        return {}
    return ld if isinstance(ld, dict) else {}
