"""The contract every store backend implements."""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable
from typing import Protocol

from ..config import Config
from ..models import Clip, SceneData

#: A sink for human-readable scrape progress (the CLI prints it, the TUI logs it).
Logger = Callable[[str], None]

#: The desktop Chrome UA the HTML/JSON backends send. (APClips needs a fuller
#: header set and ModelCentro a Firefox UA, so those two keep their own.)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def noop(_msg: str) -> None:
    """Default logger: discard progress."""


def strip_html(s: str | None) -> str:
    """Drop tags and unescape entities from a fragment of scraped HTML."""
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def hms_to_seconds(text: object) -> int | None:
    """A colon-separated time ('HH:MM:SS' or 'MM:SS') -> seconds; None if absent or
    not all-numeric."""
    if not isinstance(text, str) or not text:
        return None
    parts = text.split(":")
    if not all(p.isdigit() for p in parts):
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + int(p)
    return seconds


#: Statuses worth one retry before a paged backend concludes end-of-catalog. A
#: transient 429/5xx on page N>1 would otherwise break the loop and cache the
#: partial catalog as complete, hiding every later clip until a manual refresh.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def get_page(client, url, *, retry_delay: float = 1.0, **kwargs):
    """GET a catalog page, retrying once on a transient status (429/5xx). Returns
    the response; the caller decides whether a non-200 is end-of-catalog (break)
    or a still-transient error worth raising (so a short catalog isn't cached)."""
    resp = client.get(url, **kwargs)
    if resp.status_code in RETRYABLE_STATUS:
        time.sleep(retry_delay)
        resp = client.get(url, **kwargs)
    return resp


def log_incremental(log: Logger, total: int, seeded: int) -> None:
    """The shared catalog() epilogue: report how many clips an incremental rescrape
    added. No-op when this wasn't a rescrape (seeded == 0)."""
    if seeded:
        log(f"  incremental: {total - seeded} new clip(s) since last scrape")


class StoreScraper(Protocol):
    #: Short stable name, used for cache paths and reporting (e.g. "iwantclips").
    name: str
    #: The store's domain, used to find scenes that lack a URL from this store
    #: (e.g. "iwantclips.com"). Substring-matched against scene URLs.
    domain: str

    def handles(self, url: str) -> bool:
        """True if this backend recognizes the given store URL."""
        ...

    def store_id(self, url: str) -> str | None:
        """Stable id for this store, used as the cache key (e.g. the numeric id)."""
        ...

    def catalog(
        self,
        store_url: str,
        config: Config,
        log: Logger = noop,
        known: list[Clip] | None = None,
    ) -> list[Clip]:
        """Return the performer's store catalog. Network-heavy; cache callers are
        expected to wrap this so it only runs when needed. Progress lines are
        emitted through ``log``.

        ``known`` enables an incremental rescrape: pass the clips already cached
        and the backend seeds them, fetching newest-first and stopping once it
        reaches clips it already has, then returns ``known`` merged with whatever
        is new. Backends that can't do this may ignore it and rescrape in full."""
        ...

    def detail(self, url: str, config: Config, log: Logger = noop) -> SceneData | None:
        """Full metadata for a single clip page, for writing a complete Stash
        scene. ``url`` is a clip URL (not the store URL). None if it can't be
        scraped. Network-heavy; callers fetch only for scenes being enriched."""
        ...
