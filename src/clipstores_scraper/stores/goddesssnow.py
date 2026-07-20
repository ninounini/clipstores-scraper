"""GoddessSnow.com backend -- Goddess Alexandra Snow's own site.

Mirrors the Stash GoddessSnow community scraper. Per-clip detail lives on the
``/vod/scenes/<slug>_vids.html`` page -- the ``/updates/<slug>.html`` page has a
release date that's off by a year and a truncated description -- so ``detail()``
rewrites any clip URL to the ``_vids`` form and reads title, date, description,
tags and cover off it.

The catalog is the site-wide movie listing (``/categories/movies_<n>_d.html``).
It yields title + URL only (the listing has no duration), so matching for this
store leans on the title. Clip URLs are stored in the ``_vids`` form -- the
same canonical URL the Stash community scraper writes -- so the page a stored
link opens shows the true release date.
"""

from __future__ import annotations

import re
from datetime import datetime

import httpx

from ..config import Config
from ..models import Clip, SceneData
from .base import UA, Logger, log_incremental, noop, strip_html

_HOST = "https://www.goddesssnow.com"
_DOMAIN_RE = re.compile(r"goddesssnow\.com", re.IGNORECASE)
# clip slug from an /updates/<slug>.html or /vod/scenes/<slug>[_vids].html URL
_SLUG_RE = re.compile(
    r"goddesssnow\.com/(?:updates|vod/scenes)/([^/?#]+?)(?:_vids)?\.html", re.IGNORECASE
)
_MAX_PAGES = 300  # safety bound for the movie listing

_TITLE_RE = re.compile(r'class="title_bar"[^>]*>.*?<span>(.*?)</span>', re.S)
_DATE_RE = re.compile(r'class="release-date"[^>]*>(.*?)</span>', re.S)
_DESC_RE = re.compile(r'class="update_description"[^>]*>(.*?)</span>', re.S)
_TAGS_BLOCK_RE = re.compile(r'class="update_tags"[^>]*>(.*?)</span>', re.S)
_A_RE = re.compile(r"<a[^>]*>(.*?)</a>", re.S)
_COVER_RE = re.compile(r'class="VOD_update".*?<img[^>]*\bsrc0_4x="([^"]+)"', re.S)
_LISTING_SLUG_RE = re.compile(r"/updates/([^\"]+?)\.html")


def _clip_url(slug: str) -> str:
    """Canonical clip URL: the /vod/scenes/<slug>_vids.html form the community
    scraper writes. The /updates/ page prints every date a year late."""
    return f"{_HOST}/vod/scenes/{slug}_vids.html"


def _parse_date(text: str | None) -> str | None:
    """'Release Date: 05/24/2021' (MM/DD/YYYY) -> ISO; None if unparseable."""
    if not text:
        return None
    s = re.sub(r"^\s*Release Date:\s*", "", strip_html(text))
    try:
        return datetime.strptime(s, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


class GoddessSnowStore:
    name = "goddesssnow"
    domain = "goddesssnow.com"

    def handles(self, url: str) -> bool:
        return bool(_DOMAIN_RE.search(url or ""))

    def store_id(self, url: str) -> str | None:
        # One site (one studio), so the whole catalog is a single store.
        return "goddesssnow" if self.handles(url) else None

    def catalog(
        self,
        store_url: str,
        config: Config,
        log: Logger = noop,
        known: list[Clip] | None = None,
    ) -> list[Clip]:
        clips: dict[str, Clip] = {}
        for c in known or []:
            if m := _SLUG_RE.search(c.url):
                c.url = _clip_url(m.group(1))  # migrate cached /updates/ URLs
                clips[m.group(1)] = c
        seeded = len(clips)
        with httpx.Client(
            headers={"User-Agent": UA}, follow_redirects=True, timeout=30.0
        ) as client:
            empty = 0
            for page in range(1, _MAX_PAGES + 1):
                resp = client.get(f"{_HOST}/categories/movies_{page}_d.html")
                if resp.status_code != 200:
                    break
                before = len(clips)
                for slug in _LISTING_SLUG_RE.findall(resp.text):
                    if slug not in clips:
                        clips[slug] = Clip(
                            title=slug.replace("-", " "),
                            url=_clip_url(slug),
                            source="GoddessSnow",
                            duration=None,
                            date=None,
                        )
                new = len(clips) - before
                log(f"  page {page}: +{new} new (total {len(clips)})")
                if new == 0:
                    empty += 1
                    if empty >= 2:  # two empty pages -> end of listing
                        break
                else:
                    empty = 0
        log_incremental(log, len(clips), seeded)
        return list(clips.values())

    def detail(self, url: str, config: Config, log: Logger = noop) -> SceneData | None:
        m = _SLUG_RE.search(url)
        if not m:
            return None
        vids = f"{_HOST}/vod/scenes/{m.group(1)}_vids.html"
        try:
            with httpx.Client(
                headers={"User-Agent": UA}, follow_redirects=True, timeout=30.0
            ) as client:
                resp = client.get(vids)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        h = resp.text
        title = _TITLE_RE.search(h)
        if not title:
            return None
        tags_block = _TAGS_BLOCK_RE.search(h)
        tags = (
            [c for a in _A_RE.findall(tags_block.group(1)) if (c := strip_html(a))]
            if tags_block
            else []
        )
        date = _DATE_RE.search(h)
        desc = _DESC_RE.search(h)
        cover = _COVER_RE.search(h)
        return SceneData(
            source="GoddessSnow",
            title=strip_html(title.group(1)) or None,
            date=_parse_date(date.group(1)) if date else None,
            details=strip_html(desc.group(1)) if desc else None,
            cover_url=(_HOST + cover.group(1)) if cover else None,
            studio=None,  # the studio is inferred from the performer
            tags=tags,
        )
