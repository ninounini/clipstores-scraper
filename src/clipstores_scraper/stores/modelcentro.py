"""ModelCentro / AdultCentro backend -- a performer's own self-hosted site.

ModelCentro powers ~120 single-performer sites (brookelynnebriar.com, etc.), all
sharing one JSON API at ``/sapi/{key1}/{key2}/content.load``. The two keys are
embedded in every page and rotate ~daily: ``key1`` is the reversed ``"ah"`` token
and ``key2`` is the ``"aet"`` token, both lifted from the HTML (mirroring the
Stash ModelCentroAPI community scraper).

Each site is one performer, so one ``ModelCentroStore(domain)`` instance is
registered per site. The catalog is the paginated ``preset=scene`` listing (id,
title, ``length`` seconds, ``publishDate``); ``detail()`` re-queries a single id
to also get the cover, description and tags.
"""

from __future__ import annotations

import re
from datetime import datetime

import httpx

from ..config import Config
from ..models import Clip, SceneData
from .base import Logger, log_incremental, noop

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:79.0) Gecko/20100101 Firefox/79.0"
_AH_RE = re.compile(r'"ah":"([A-Za-z0-9_-]+)"')
_AET_RE = re.compile(r'"aet":(\d+)')
_SCENE_ID_RE = re.compile(r"/scene/(\d+)")
# Opener is "[" (the tag convention); closer may be "]" or a mistyped ")".
_TRAILING_TAGS_RE = re.compile(r"\s*\[([^\[\]()]+)[\])]\s*$")
_PAGE = 100
_MAX_PAGES = 60  # safety bound (~6000 clips)


def _split_title_tags(title: str) -> tuple[str, list[str]]:
    """Pull a trailing ``[Tag, Tag, ...]`` block out of the title into tags.

    These sellers stuff tags into the title ("... Play [JOI Game, Sensual, CEI]")
    and the API's own tag list often omits them, so the bracket is the only place
    they live. Strip it from the title (cleaner match against the filename) and
    return the comma-split tags."""
    m = _TRAILING_TAGS_RE.search(title or "")
    if not m:
        return (title or "").strip(), []
    tags = [t.strip() for t in m.group(1).split(",") if t.strip()]
    return title[: m.start()].strip(), tags


def _items(payload: dict) -> list[dict]:
    """The API returns ``response.collection`` as a list or an id-keyed dict."""
    col = (payload.get("response") or {}).get("collection")
    if isinstance(col, list):
        return col
    if isinstance(col, dict):
        return list(col.values())
    return []


def _publish_date(item: dict) -> str | None:
    """ISO date from ``sites.collection[<id>].publishDate`` ('YYYY-MM-DD HH:MM:SS')."""
    col = ((item.get("sites") or {}).get("collection")) or {}
    for site in col.values():
        raw = (site or {}).get("publishDate")
        if isinstance(raw, str) and raw:
            try:
                return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").date().isoformat()
            except ValueError:
                return raw[:10]
    return None


def _cover(item: dict) -> str | None:
    primary = ((item.get("_resources") or {}).get("primary")) or []
    return primary[0].get("url") if primary else None


def _tags(item: dict) -> list[str]:
    col = ((item.get("tags") or {}).get("collection")) or {}
    return [t.get("alias", "").strip() for t in col.values() if t.get("alias")]


class ModelCentroStore:
    name = "modelcentro"

    def __init__(self, domain: str) -> None:
        self.domain = domain

    def handles(self, url: str) -> bool:
        return self.domain in (url or "").lower()

    def store_id(self, url: str) -> str | None:
        # One site = one performer = one cache file, keyed by domain.
        return self.domain if self.handles(url) else None

    def _api_keys(self, client: httpx.Client) -> tuple[str, str]:
        """The rotating (key1, key2) lifted from any page on the site."""
        resp = client.get(f"https://{self.domain}/videos")
        resp.raise_for_status()
        ah = _AH_RE.search(resp.text)
        aet = _AET_RE.search(resp.text)
        if not (ah and aet):
            raise ValueError(f"No ModelCentro API keys on {self.domain}")
        return ah.group(1)[::-1], aet.group(1)  # key1 is reversed

    def catalog(
        self,
        store_url: str,
        config: Config,
        log: Logger = noop,
        known: list[Clip] | None = None,
    ) -> list[Clip]:
        clips: dict[str, Clip] = {}
        for c in known or []:
            if m := _SCENE_ID_RE.search(c.url):
                clips[m.group(1)] = c
        seeded = len(clips)
        with httpx.Client(
            headers={"User-Agent": _UA}, follow_redirects=True, timeout=30.0
        ) as client:
            k1, k2 = self._api_keys(client)
            base = f"https://{self.domain}/sapi/{k1}/{k2}/content.load"
            for page in range(_MAX_PAGES):
                url = (
                    f"{base}?_method=content.load&tz=1&limit={_PAGE}"
                    f"&offset={page * _PAGE}&order[publishDate]=desc"
                    f"&transitParameters[preset]=scene"
                )
                items = _items(client.get(url, headers={"Referer": store_url}).json())
                if not items:
                    break
                before = len(clips)
                for it in items:
                    cid = str(it.get("id"))
                    # Keep the full title (bracketed tags included) for matching:
                    # the Stash filenames carry the same bracket. detail() strips it
                    # for the title actually written to Stash.
                    length = it.get("length")
                    clips[cid] = Clip(
                        title=it.get("title") or "",
                        url=f"https://{self.domain}/scene/{cid}/",
                        source="ModelCentro",
                        # Coerce like every sibling backend: a stray str length
                        # would crash duration math in matching.py.
                        duration=int(length) if str(length).isdigit() else None,
                        date=_publish_date(it),
                    )
                new = len(clips) - before
                log(f"  page {page + 1}: +{new} new (total {len(clips)})")
                # date-desc listing: once a full page is all-known, the rest is older.
                if seeded and new == 0:
                    break
                if len(items) < _PAGE:
                    break
        log_incremental(log, len(clips), seeded)
        return list(clips.values())

    def detail(self, url: str, config: Config, log: Logger = noop) -> SceneData | None:
        m = _SCENE_ID_RE.search(url)
        if not m:
            return None
        cid = m.group(1)
        try:
            with httpx.Client(
                headers={"User-Agent": _UA}, follow_redirects=True, timeout=30.0
            ) as client:
                k1, k2 = self._api_keys(client)
                api = (
                    f"https://{self.domain}/sapi/{k1}/{k2}/content.load"
                    f"?_method=content.load&tz=1&filter[id][fields][0]=id"
                    f"&filter[id][values][0]={cid}"
                    f"&transitParameters[v1]=ykYa8ALmUD&transitParameters[preset]=scene"
                )
                items = _items(client.get(api, headers={"Referer": url}).json())
        except (httpx.HTTPError, ValueError):
            return None
        if not items:
            return None
        it = items[0]
        title, title_tags = _split_title_tags(it.get("title") or "")
        # Union the API tags with the ones lifted from the title, deduped.
        tags = list(_tags(it))
        seen = {t.lower() for t in tags}
        tags += [t for t in title_tags if t.lower() not in seen]
        return SceneData(
            source="ModelCentro",
            title=title or None,
            date=_publish_date(it),
            details=it.get("description") or None,
            code=cid,
            cover_url=_cover(it),
            studio=None,  # one-performer site; studio is inferred from the performer
            tags=tags,
        )
