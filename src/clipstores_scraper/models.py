"""Core data types shared across the store backends, matching, and Stash I/O."""

from __future__ import annotations

import html
from dataclasses import dataclass, field


@dataclass(slots=True)
class Clip:
    """One item in a performer's store catalog."""

    title: str
    url: str
    source: str
    duration: int | None = None  # seconds
    date: str | None = None  # ISO date, YYYY-MM-DD

    def __post_init__(self) -> None:
        # Titles come straight from scraped HTML/JSON, so they can carry raw
        # entities (&amp;, &#39;). Decode once at the source — every store and the
        # cache agree, and an undecoded entity can't leak into Stash later.
        self.title = html.unescape(self.title)


@dataclass(slots=True)
class SceneData:
    """Full per-scene metadata scraped from one store's clip page.

    Scalars are merged across a scene's stores by rank; ``tags`` are unioned.
    ``cover_url`` is carried (not the bytes) so the merge can pick the rank
    winner and only the chosen image is fetched. ``source`` is the store name,
    used to rank scalars."""

    source: str
    title: str | None = None
    date: str | None = None  # ISO YYYY-MM-DD
    details: str | None = None
    code: str | None = None  # store clip id
    cover_url: str | None = None
    studio: str | None = None
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Same entity-decoding as Clip, for the text fields that reach Stash.
        # cover_url/code are left alone (a URL/id, not display text).
        for name in ("title", "details", "studio"):
            value = getattr(self, name)
            if value:
                setattr(self, name, html.unescape(value))
        if self.tags:
            self.tags = [html.unescape(t) for t in self.tags]


@dataclass(slots=True)
class SceneFile:
    basename: str
    duration: int | None = None  # seconds


@dataclass(slots=True)
class Scene:
    """A Stash scene that has no StashDB match yet."""

    id: str
    title: str | None
    date: str | None
    urls: list[str] = field(default_factory=list)
    files: list[SceneFile] = field(default_factory=list)

    @property
    def _primary_file(self) -> SceneFile | None:
        """The file the match should describe: the longest (the real video, not
        a trailer/sample). max is stable, so ties keep the first file."""
        if not self.files:
            return None
        return max(self.files, key=lambda f: f.duration or 0)

    @property
    def primary_basename(self) -> str:
        f = self._primary_file
        return f.basename if f else (self.title or "")

    @property
    def duration(self) -> int | None:
        f = self._primary_file
        return f.duration if f else None


@dataclass(slots=True)
class Performer:
    id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        """All known name variants, for stripping out of filenames/titles."""
        out = [self.name, *self.aliases]
        return [n for n in out if n]


@dataclass(slots=True)
class PerformerStatus:
    """A triaged performer: has a supported store URL and N unmatched scenes."""

    performer: Performer
    store_url: str
    store_name: str
    unmatched_count: int


@dataclass(slots=True)
class MatchCandidate:
    """A proposed scene -> clip link, with the evidence behind it."""

    scene: Scene
    clip: Clip
    title_score: float  # 0..1
    duration_delta: int | None  # |seconds|, None if either side unknown
    date_delta: int | None  # |days|, None if either side unknown
    confidence: str  # "high" | "medium" | "low"
