"""ManyVids catalog backend.

Unlike IWantClips, ManyVids needs no browser: its store page is a Next.js app
that server-renders the whole catalog into the page's RSC stream (the
``self.__next_f.push([...])`` script chunks). Each video object already carries
title, duration ("MM:SS") and launch date, so we just fetch the page over HTTP,
recover the streamed JSON, and read the videos straight off it -- no Playwright,
no proxies, no age gate.

Any profile URL variant (``/Profile/<id>``, ``.../Store``, ``.../Store/Videos``,
right or wrong slug) 308-redirects to the canonical one, so the numeric profile
id is the only thing that matters. Pagination is ``?page=N`` against the
canonical URL. Clip URLs look like ``manyvids.com/Video/<id>/<slug>``.
"""

from __future__ import annotations

import html
import json
import re
import time

import httpx

from ..config import Config
from ..models import Clip, SceneData
from .base import (
    RETRYABLE_STATUS,
    UA,
    Logger,
    get_page,
    hms_to_seconds,
    log_incremental,
    noop,
)

_HOST = "https://www.manyvids.com"
_PROFILE_RE = re.compile(r"manyvids\.com/Profile/(\d+)", re.IGNORECASE)
_VIDEO_ID_RE = re.compile(r"manyvids\.com/Video/(\d+)", re.IGNORECASE)
# The RSC stream: each chunk is a JSON-escaped string inside push([N,"..."]).
_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[\d+,"(.*?)"\]\)', re.S)
# A catalog video object always opens with id (quoted, numeric) then title.
# community/price/etc. carry no quoted numeric "id", so this only anchors videos.
_VIDEO_ANCHOR = re.compile(r'\{"id":"\d+","title":')

_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
_MAX_PAGES = 200  # safety bound
_PAGE_DELAY = 0.5  # ponytail: be civil between pages; MV has no documented limit


def _decode_flight(html_text: str) -> str:
    """The page's RSC chunks, JSON-unescaped and concatenated into one blob."""
    parts: list[str] = []
    for raw in _CHUNK_RE.findall(html_text):
        try:
            parts.append(json.loads(f'"{raw}"'))
        except json.JSONDecodeError:
            continue
    return "".join(parts)


def _parse_videos(html_text: str) -> list[dict]:
    """Every catalog video object embedded in the page's RSC stream. Each anchor
    sits at the '{' of a video object; raw_decode reads exactly that object (it's
    string-aware, so braces inside titles don't throw it off)."""
    flight = _decode_flight(html_text)
    decoder = json.JSONDecoder()
    out: list[dict] = []
    for m in _VIDEO_ANCHOR.finditer(flight):
        try:
            obj, _ = decoder.raw_decode(flight, m.start())
        except json.JSONDecodeError:
            continue
        out.append(obj)
    return out


def _to_clip(v: dict) -> Clip | None:
    vid = str(v.get("id") or "")
    if not vid:
        return None
    slug = v.get("slug") or ""
    return Clip(
        title=(v.get("title") or slug.replace("-", " ")).strip(),
        url=f"{_HOST}/Video/{vid}/{slug}",
        source="ManyVids",
        duration=hms_to_seconds(v.get("duration")),
        date=(v.get("launchDate") or "")[:10] or None,
    )


def _video_id(url: str) -> str | None:
    m = _VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else None


class ManyVidsStore:
    name = "manyvids"
    domain = "manyvids.com"

    def handles(self, url: str) -> bool:
        # Match the store/profile URL (catalog) *and* a /Video/ clip URL, so
        # enrichment recognizes the clip URLs scenes actually carry.
        return bool(_PROFILE_RE.search(url or "") or _VIDEO_ID_RE.search(url or ""))

    def store_id(self, url: str) -> str | None:
        m = _PROFILE_RE.search(url or "")
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
            raise ValueError(f"Not a ManyVids profile URL: {store_url}")

        # Seed with what we already have so an incremental rescrape stops once it
        # reaches known clips. The store lists newest-first, so new clips appear
        # before old ones, same as the IWantClips backend.
        clips: dict[str, Clip] = {}
        for c in known or []:
            vid = _video_id(c.url)
            if vid:
                clips[vid] = c
        seeded = len(clips)

        # The slug is irrelevant -- any value redirects to the canonical URL.
        base = f"{_HOST}/Profile/{sid}/_/Store/Videos"
        canon = base
        empty_pages = 0
        with httpx.Client(
            headers=_HEADERS, follow_redirects=True, timeout=30.0
        ) as client:
            for page_no in range(1, _MAX_PAGES + 1):
                if page_no > 1:
                    time.sleep(_PAGE_DELAY)
                resp = get_page(
                    client, canon if page_no == 1 else f"{canon}?page={page_no}"
                )
                if page_no == 1:
                    resp.raise_for_status()
                    canon = str(resp.url).split("?")[0]  # canonical, post-redirect
                elif resp.status_code != 200:
                    if resp.status_code in RETRYABLE_STATUS:  # still failing post-retry
                        resp.raise_for_status()  # don't cache a truncated catalog
                    break  # 404 etc: genuinely past the last page
                before = len(clips)
                for v in _parse_videos(resp.text):
                    clip = _to_clip(v)
                    if clip is None:
                        continue
                    vid = _video_id(clip.url)
                    if vid and vid not in clips:
                        clips[vid] = clip
                new = len(clips) - before
                log(f"  page {page_no}: +{new} new (total {len(clips)})")
                # Require two empty pages before trusting "done": a transient
                # hiccup yields one spurious empty page that mimics end-of-catalog.
                if new == 0:
                    empty_pages += 1
                    if empty_pages >= 2:
                        break
                else:
                    empty_pages = 0
        log_incremental(log, len(clips), seeded)
        return list(clips.values())

    def detail(self, url: str, config: Config, log: Logger = noop) -> SceneData | None:
        vid = _video_id(url)
        if not vid:
            return None
        with httpx.Client(
            headers={"User-Agent": UA, "Accept": "application/json"},
            follow_redirects=True,
            timeout=30.0,
        ) as client:
            resp = client.get(f"{_HOST}/bff/store/video/{vid}")
        if resp.status_code != 200:  # e.g. 410 once a clip is taken down
            return None
        data = resp.json().get("data") or {}
        model = data.get("model") or {}
        return SceneData(
            source="ManyVids",
            title=html.unescape(data.get("title") or "").strip() or None,
            date=(data.get("launchDate") or "")[:10] or None,
            details=html.unescape(data.get("description") or "").strip() or None,
            code=str(data["id"]) if data.get("id") else None,
            cover_url=data.get("screenshot") or None,
            studio=(model.get("displayName") or "").strip() or None,
            tags=[
                lbl
                for t in (data.get("tagList") or [])
                if (lbl := (t.get("label") or "").strip())
            ],
        )
