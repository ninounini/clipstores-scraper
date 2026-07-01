"""Runtime configuration, loaded from environment (.env supported)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _default_concurrency() -> int:
    """Auto-adapt to the machine: one worker per logical CPU. Scraping is all HTTP,
    so this is cheap; override with CLIPSTORE_SCRAPE_CONCURRENCY if needed. Falls
    back to 4 when the CPU count can't be determined."""
    return os.cpu_count() or 4


@dataclass(slots=True)
class Config:
    stash_url: str
    stash_api_key: str

    # Parallelism for scrape-all and enrich --apply. Defaults to the CPU thread
    # count; all scraping is HTTP. (Egress: system VPN.)
    scrape_concurrency: int = field(default_factory=_default_concurrency)

    # Tag id stamped on every scene enrichment writes to (a marker, e.g. a
    # "clipstores-scraper" tag). None disables it.
    enrich_tag: str | None = None

    @classmethod
    def from_env(cls) -> Config:
        url = os.environ.get("STASH_URL", "").strip()
        if not url:
            raise SystemExit(
                "STASH_URL is not set. Copy .env.example to .env and fill it in."
            )
        return cls(
            stash_url=url,
            stash_api_key=os.environ.get("STASH_API_KEY", "").strip(),
            scrape_concurrency=_int_env(
                "CLIPSTORE_SCRAPE_CONCURRENCY", _default_concurrency(), minimum=1
            ),
            enrich_tag=os.environ.get("CLIPSTORE_ENRICH_TAG", "").strip() or None,
        )


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default
