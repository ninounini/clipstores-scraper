"""APClips (apclips.com) backend.

Server-rendered HTML behind Cloudflare, so requests need a full browser header
set or they 403. A performer's store is ``apclips.com/<slug>``; the catalog is
the paginated ``/<slug>/videos`` grid (each card carries title, clip URL and a
MM:SS duration). ``detail()`` reads a single clip page for date, description,
tags and cover -- none of which the grid exposes.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from urllib.parse import urlparse

import httpx

from ..config import Config
from ..models import Clip, SceneData
from .base import Logger, log_incremental, noop

_HOST = "https://apclips.com"
# Cloudflare 403s a bare UA; a full Chrome header set gets through.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}
_MAX_PAGES = 100

# One grid card: clip URL, MM:SS (or H:MM:SS) duration, then the title span. The
# gap before item-title is bounded so it can't cross into the *next* card's anchor
# (class="thumb-image); otherwise a card missing item-title would steal the
# following card's title. re.S is intentional — the gap spans newlines within one
# card, the lookahead is what keeps it from spanning cards.
_CARD_RE = re.compile(
    r'href="(?P<url>/[^"]+)" class="thumb-image[^"]*"[^>]*>\s*'
    r'<span class="item-details">(?P<dur>[\d:]+)</span>'
    r'(?:(?!class="thumb-image).)*?'
    r'<span class="item-title[^"]*">(?P<title>[^<]+)</span>',
    re.S,
)
_DATE_RE = re.compile(r'<time datetime="([^"]+)"')
_DESC_RE = re.compile(r'class="[^"]*full-desc[^"]*"[^>]*>(.*?)</', re.S)
_TAG_RE = re.compile(r'class="tag-link[^"]*"[^>]*>([^<]+)<')
_NAME_RE = re.compile(r'data-content-name="([^"]*)"')
_CODE_RE = re.compile(r'data-content-code="video-(\d+)"')
# The seller's designated cover (the "scene picture") -- either a /ui/img promo
# with the title on it, or a mojocloud frame they picked. This is the store's own
# thumbnail, so it's the right cover whatever the host.
_THUMB_RE = re.compile(r'data-content-thumb="([^"]+)"')
# Last resort when a clip has no designated thumb: the first CDN frame-grab.
_FRAME_THUMB_RE = re.compile(r'https://[^"\s]*mojocloud[^"\s]*video-thumbs[^"\s]*\.jpg')
_ORDINAL_RE = re.compile(r"(\d+)(?:st|nd|rd|th)")


def _slug(url: str) -> str | None:
    """The store/performer slug -- the first path segment of an apclips URL
    (handles ``www.`` and a trailing ``/videos``)."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[0] if parts else None


def _parse_duration(text: str) -> int | None:
    """'31:47' -> 1907, '1:02:03' -> 3723."""
    nums = [int(p) for p in text.strip().split(":") if p.isdigit()]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def _parse_date(text: str | None) -> str | None:
    """The ``<time datetime="…">`` attribute is ISO 8601 ('2025-02-11T…'); some
    pages also show a human 'Feb 11th, 2025'. Try ISO first, then the human form.
    None if unparseable."""
    if not text:
        return None
    text = text.strip()
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        pass
    cleaned = _ORDINAL_RE.sub(r"\1", text)  # drop the ordinal suffix
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _clean(s: str | None) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    text = re.sub(r"\\(['\"\\])", r"\1", text)  # APClips JS-escapes attrs: mommy\'s
    return text.strip()


class APClipsStore:
    name = "apclips"
    domain = "apclips.com"

    def handles(self, url: str) -> bool:
        host = urlparse(url or "").netloc.lower()
        return host == self.domain or host.endswith("." + self.domain)

    def store_id(self, url: str) -> str | None:
        return _slug(url) if self.handles(url) else None

    def catalog(
        self,
        store_url: str,
        config: Config,
        log: Logger = noop,
        known: list[Clip] | None = None,
    ) -> list[Clip]:
        slug = _slug(store_url)
        if not slug:
            raise ValueError(f"Not an APClips store URL: {store_url}")
        clips: dict[str, Clip] = {c.url: c for c in (known or [])}
        seeded = len(clips)
        with httpx.Client(
            headers=_HEADERS, follow_redirects=True, timeout=30.0
        ) as client:
            for page in range(1, _MAX_PAGES + 1):
                resp = client.get(f"{_HOST}/{slug}/videos?page={page}")
                if resp.status_code != 200:
                    break
                before = len(clips)
                for m in _CARD_RE.finditer(resp.text):
                    url = _HOST + m.group("url")
                    if url in clips:
                        continue
                    clips[url] = Clip(
                        title=m.group("title"),
                        url=url,
                        source="APClips",
                        duration=_parse_duration(m.group("dur")),
                        date=None,  # not on the grid; detail() has it
                    )
                new = len(clips) - before
                log(f"  page {page}: +{new} new (total {len(clips)})")
                if new == 0:  # no cards (end of grid) or all-known (incremental)
                    break
        log_incremental(log, len(clips), seeded)
        return list(clips.values())

    def detail(self, url: str, config: Config, log: Logger = noop) -> SceneData | None:
        try:
            with httpx.Client(
                headers=_HEADERS, follow_redirects=True, timeout=30.0
            ) as client:
                resp = client.get(url)
                resp.raise_for_status()
        except httpx.HTTPError:
            return None
        h = resp.text
        name = _NAME_RE.search(h)
        descs = _DESC_RE.findall(h)
        code = _CODE_RE.search(h)
        date = _DATE_RE.search(h)
        # Prefer the seller's designated thumb; fall back to a CDN frame-grab.
        thumb = _THUMB_RE.search(h)
        value = thumb.group(1) if thumb else ""
        if value and "vid_empty" not in value:
            cover_url = value if value.startswith("http") else _HOST + value
        else:
            frame = _FRAME_THUMB_RE.search(h)
            cover_url = frame.group(0) if frame else None
        # tag-link text repeats (shown in two places); dedupe, preserve order.
        tags = list(dict.fromkeys(t.strip() for t in _TAG_RE.findall(h) if t.strip()))
        title = _clean(name.group(1)) if name else None
        if not title:
            return None
        return SceneData(
            source="APClips",
            title=title,
            date=_parse_date(date.group(1)) if date else None,
            details=_clean(max(descs, key=len)) if descs else None,
            code=code.group(1) if code else None,
            cover_url=cover_url,
            studio=None,  # one-performer store; studio inferred from the performer
            tags=tags,
        )
