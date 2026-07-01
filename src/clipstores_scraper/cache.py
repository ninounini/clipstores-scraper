"""On-disk cache of store catalogs so a performer's store is scraped once."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from .models import Clip

CACHE_DIR = Path("cache")
DEFAULT_TTL = 7 * 24 * 3600  # a week


def _path(store: str, store_id: str) -> Path:
    return CACHE_DIR / store / f"{store_id}.json"


def load(store: str, store_id: str, ttl: int = DEFAULT_TTL) -> list[Clip] | None:
    path = _path(store, store_id)
    if not path.is_file():
        return None
    if time.time() - path.stat().st_mtime > ttl:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        clips = [Clip(**item) for item in raw]
    except (ValueError, TypeError):  # truncated or old-schema file → cache miss
        return None
    # Cached titles were already entity-decoded when first scraped; Clip()'s
    # __post_init__ would decode them a second time (e.g. "&amp;amp;" -> "&"), so a
    # round-trip wouldn't be identity and fuzzy scores would drift fresh-vs-cached.
    for clip, item in zip(clips, raw, strict=True):
        clip.title = item["title"]
    # An empty catalog is almost always a failed/blocked scrape (age-gate, network
    # blip, bad URL), not a real "0 clips" state. Serving it silently hides every
    # match from that store until a manual --refresh; treat it as a miss so the
    # next access (e.g. triage) re-scrapes instead.
    return clips or None


def save(store: str, store_id: str, clips: list[Clip]) -> None:
    path = _path(store, store_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(c) for c in clips]
    # Write-then-rename so a crash mid-write can't truncate the live cache file.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
