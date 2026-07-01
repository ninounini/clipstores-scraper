"""Stash GraphQL client: read unmatched scenes, write store URLs back."""

from __future__ import annotations

import threading
from collections import Counter

import httpx

from .config import Config
from .models import Performer, Scene, SceneFile

_PERFORMER_QUERY = """
query ($id: ID!) {
  findPerformer(id: $id) {
    name
    alias_list
    urls
  }
}
"""

# Scenes for this performer still worth matching against a given store: no
# StashDB id, not organized (a scene you've marked done is left alone), *and* no
# URL from that store yet (``url EXCLUDES <domain>``). Scoping by store, not by
# "has any URL", lets one scene collect a URL from each store it sells on; a scene
# that already carries this store's URL was applied before and drops out of *this*
# store's queue instead of resurfacing.
_SCENES_QUERY = """
query ($pid: [ID!], $domain: String!) {
  findScenes(
    scene_filter: {
      performers: { value: $pid, modifier: INCLUDES }
      stash_id_endpoint: { modifier: IS_NULL }
      url: { value: $domain, modifier: EXCLUDES }
      organized: false
    }
    filter: { per_page: -1 }
  ) {
    scenes {
      id
      title
      date
      urls
      files {
        basename
        duration
      }
    }
  }
}
"""

_UPDATE_MUTATION = """
mutation ($id: ID!, $urls: [String!]) {
  sceneUpdate(input: { id: $id, urls: $urls }) {
    id
    urls
  }
}
"""

# All performers, with the URL list we triage for a supported store.
_ALL_PERFORMERS_QUERY = """
query {
  allPerformers { id name alias_list urls }
}
"""

# Performers carrying a marker tag -- the batch's target set (e.g. "[Monitored]").
_PERFORMERS_BY_TAG_QUERY = """
query ($tids: [ID!]) {
  findPerformers(
    performer_filter: { tags: { value: $tids, modifier: INCLUDES, depth: 0 } }
    filter: { per_page: -1 }
  ) {
    performers { id name alias_list urls }
  }
}
"""

# Cheap count of a performer's still-to-match scenes for one store (no StashDB id,
# not organized, no URL from that store), mirroring _SCENES_QUERY so the triage
# count matches what a scrape will work on.
_COUNT_UNMATCHED_QUERY = """
query ($pid: [ID!], $domain: String!) {
  findScenes(
    scene_filter: {
      performers: { value: $pid, modifier: INCLUDES }
      stash_id_endpoint: { modifier: IS_NULL }
      url: { value: $domain, modifier: EXCLUDES }
      organized: false
    }
    filter: { per_page: 1 }
  ) { count }
}
"""

# A single scene's current URLs, read just before apply so we union against the
# live value rather than a possibly-stale snapshot.
_SCENE_URLS_QUERY = """
query ($id: ID!) {
  findScene(id: $id) { urls }
}
"""

# A scene's enrichable state: current scalars (to overwrite), the tags already on
# it (to union), and its performers (to infer the studio from their other scenes).
_SCENE_DETAIL_QUERY = """
query ($id: ID!) {
  findScene(id: $id) {
    id title date details code urls
    studio { id }
    tags { id }
    performers { id }
  }
}
"""

# Studios used by a set of performers' scenes -- the signal we infer a clip-store
# scene's studio from (the store's own studio name is inconsistent across sites).
_PERF_STUDIOS_QUERY = """
query ($pids: [ID!]) {
  findScenes(
    scene_filter: { performers: { value: $pids, modifier: INCLUDES } }
    filter: { per_page: -1 }
  ) {
    scenes { studio { id } }
  }
}
"""

# Every scene carrying a URL from a given store domain (for finding scenes to
# enrich). The caller can pass a scene_filter that also excludes the marker tag,
# so an incremental "enrich the next N" pass skips scenes already done.
_SCENES_WITH_URL_QUERY = """
query ($filter: SceneFilterType!) {
  findScenes(scene_filter: $filter, filter: { per_page: -1 }) {
    scenes { id urls }
  }
}
"""

_ALL_TAGS_QUERY = "query { allTags { id name } }"
_TAG_CREATE = "mutation ($name: String!) { tagCreate(input: { name: $name }) { id } }"
_SCENE_UPDATE_FULL = """
mutation ($input: SceneUpdateInput!) {
  sceneUpdate(input: $input) { id }
}
"""


class StashError(RuntimeError):
    pass


class StashClient:
    def __init__(self, config: Config) -> None:
        self._config = config
        # Resolve-or-create caches, populated lazily and reused across scenes.
        # The lock serializes find-or-create so parallel enrich workers can't both
        # miss the cache and create the same tag/studio twice.
        self._resolve_lock = threading.Lock()
        self._tag_map: dict[str, str] | None = None  # lower(name) -> id
        # performer-id tuple -> inferred studio id (or None)
        self._perf_studio_cache: dict[tuple[str, ...], str | None] = {}
        headers = {"Content-Type": "application/json"}
        if config.stash_api_key:
            headers["ApiKey"] = config.stash_api_key
        self._client = httpx.Client(
            base_url=config.stash_url,
            headers=headers,
            timeout=30.0,
            verify=False,  # Stash is commonly self-hosted with a private cert
        )

    def __enter__(self) -> StashClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _gql(self, query: str, variables: dict) -> dict:
        try:
            resp = self._client.post("", json={"query": query, "variables": variables})
        except httpx.HTTPError as exc:
            raise StashError(
                f"Could not reach Stash at {self._config.stash_url}. "
                f"Is Stash running? ({exc})"
            ) from exc
        if resp.status_code != 200:
            raise StashError(f"Stash returned HTTP {resp.status_code}: {resp.text}")
        payload = resp.json()
        if payload.get("errors"):
            raise StashError(f"Stash GraphQL error: {payload['errors']}")
        return payload["data"]

    def get_performer(self, performer_id: str) -> Performer:
        data = self._gql(_PERFORMER_QUERY, {"id": str(performer_id)})
        node = data.get("findPerformer")
        if not node:
            raise StashError(
                f"No performer with id {performer_id}. "
                "Use the performer ID from the Stash URL, not a studio ID."
            )
        return _performer(node, performer_id)

    def _ensure_tag_map(self) -> None:
        """Populate the name->id tag cache once (caller must hold _resolve_lock).
        Keys are stripped + lowercased to match the lookup, so a Stash tag stored
        with stray whitespace doesn't miss the cache and spawn a near-duplicate."""
        if self._tag_map is None:
            tags = self._gql(_ALL_TAGS_QUERY, {})["allTags"]
            self._tag_map = {t["name"].strip().lower(): t["id"] for t in tags}

    def find_tag_id(self, name: str) -> str | None:
        """A tag's id by name (case-insensitive), or None if it doesn't exist.
        Lookup-only -- unlike ensure_tags it never creates. Shares the tag cache."""
        with self._resolve_lock:
            self._ensure_tag_map()
            return self._tag_map.get(name.strip().lower())

    def performers_with_tag(self, tag_name: str) -> list[Performer]:
        """Every performer carrying ``tag_name`` -- the batch's target set."""
        tid = self.find_tag_id(tag_name)
        if tid is None:
            raise StashError(f"No tag named {tag_name!r} in Stash.")
        data = self._gql(_PERFORMERS_BY_TAG_QUERY, {"tids": [tid]})
        return [_performer(n) for n in data["findPerformers"]["performers"]]

    def get_unmatched_scenes(self, performer_id: str, store_domain: str) -> list[Scene]:
        data = self._gql(
            _SCENES_QUERY, {"pid": [str(performer_id)], "domain": store_domain}
        )
        scenes: list[Scene] = []
        for node in data["findScenes"]["scenes"]:
            files = [
                SceneFile(
                    basename=f.get("basename") or "",
                    duration=_as_seconds(f.get("duration")),
                )
                for f in (node.get("files") or [])
            ]
            scenes.append(
                Scene(
                    id=str(node["id"]),
                    title=node.get("title"),
                    date=node.get("date"),
                    urls=list(node.get("urls") or []),
                    files=files,
                )
            )
        return scenes

    def get_all_performers(self) -> list[Performer]:
        data = self._gql(_ALL_PERFORMERS_QUERY, {})
        return [_performer(n) for n in (data.get("allPerformers") or [])]

    def count_unmatched_scenes(self, performer_id: str, store_domain: str) -> int:
        data = self._gql(
            _COUNT_UNMATCHED_QUERY, {"pid": [str(performer_id)], "domain": store_domain}
        )
        return int(data["findScenes"]["count"])

    def get_scene_urls(self, scene_id: str) -> list[str]:
        data = self._gql(_SCENE_URLS_QUERY, {"id": str(scene_id)})
        node = data.get("findScene") or {}
        return list(node.get("urls") or [])

    def set_scene_urls(self, scene_id: str, urls: list[str]) -> list[str]:
        data = self._gql(_UPDATE_MUTATION, {"id": scene_id, "urls": urls})
        return list(data["sceneUpdate"]["urls"])

    def get_scene_detail(self, scene_id: str) -> dict:
        """A scene's enrichable state: scalars + current urls/studio_id/tag_ids."""
        node = self._gql(_SCENE_DETAIL_QUERY, {"id": str(scene_id)})["findScene"] or {}
        return {
            "id": str(node.get("id") or scene_id),
            "title": node.get("title"),
            "date": node.get("date"),
            "details": node.get("details"),
            "code": node.get("code"),
            "urls": list(node.get("urls") or []),
            "studio_id": (node.get("studio") or {}).get("id"),
            "tag_ids": [t["id"] for t in (node.get("tags") or [])],
            "performer_ids": [p["id"] for p in (node.get("performers") or [])],
        }

    def scenes_with_url(
        self, domain: str, exclude_tag: str | None = None
    ) -> list[tuple[str, list[str]]]:
        """(scene_id, urls) for every *un-StashDB'd*, not-organized scene carrying a
        URL from this store domain. StashDB-identified scenes are skipped -- they
        already have authoritative metadata and enrichment must never overwrite it
        (the tool only fills the StashDB gap); organized scenes are skipped too, as
        ones you've marked done and don't want overwritten. ``exclude_tag`` also
        drops scenes already carrying that tag id (the enrich marker), so an
        incremental pass only sees scenes still to do."""
        scene_filter: dict = {
            "url": {"value": domain, "modifier": "INCLUDES"},
            "stash_id_endpoint": {"modifier": "IS_NULL"},
            "organized": False,
        }
        if exclude_tag:
            scene_filter["tags"] = {"value": [exclude_tag], "modifier": "EXCLUDES"}
        data = self._gql(_SCENES_WITH_URL_QUERY, {"filter": scene_filter})
        return [
            (str(s["id"]), list(s.get("urls") or []))
            for s in data["findScenes"]["scenes"]
        ]

    def ensure_tags(self, names: list[str]) -> list[str]:
        """Tag ids for these names, creating any that don't exist. Order-preserving
        and deduped; case-insensitive match against existing tags. Locked so
        parallel workers never create the same tag twice."""
        with self._resolve_lock:
            self._ensure_tag_map()
            out: list[str] = []
            for name in names:
                key = name.strip().lower()
                if not key:
                    continue
                tid = self._tag_map.get(key)
                if tid is None:
                    tid = self._gql(_TAG_CREATE, {"name": name.strip()})["tagCreate"][
                        "id"
                    ]
                    self._tag_map[key] = tid
                if tid not in out:
                    out.append(tid)
            return out

    def studio_for_performers(self, performer_ids: list[str]) -> str | None:
        """The studio most of these performers' scenes already use -- a far better
        signal than the store's inconsistent studio name. None if no studio is a
        clear majority (e.g. a performer who appears across many studios)."""
        if not performer_ids:
            return None
        key = tuple(sorted(performer_ids))
        if key in self._perf_studio_cache:
            return self._perf_studio_cache[key]
        data = self._gql(_PERF_STUDIOS_QUERY, {"pids": list(performer_ids)})
        counts: Counter[str] = Counter()
        for sc in data["findScenes"]["scenes"]:
            st = sc.get("studio")
            if st:
                counts[st["id"]] += 1
        result = None
        if counts:
            top, n = counts.most_common(1)[0]
            if n >= 2 and n / sum(counts.values()) >= 0.5:  # a clear majority
                result = top
        self._perf_studio_cache[key] = result
        return result

    def update_scene_full(
        self,
        scene_id: str,
        *,
        title: str | None = None,
        date: str | None = None,
        details: str | None = None,
        code: str | None = None,
        studio_id: str | None = None,
        tag_ids: list[str] | None = None,
        cover_image: str | None = None,
    ) -> None:
        """Write enriched metadata. Scalars overwrite (only when a value is given,
        so we never blank a field no store provided); tag_ids is the caller's
        union; cover_image is a base64 data URI."""
        inp: dict[str, object] = {"id": str(scene_id)}
        if title:
            inp["title"] = title
        if date:
            inp["date"] = date
        if details:
            inp["details"] = details
        if code:
            inp["code"] = code
        if studio_id:
            inp["studio_id"] = studio_id
        if tag_ids is not None:
            inp["tag_ids"] = tag_ids
        if cover_image:
            inp["cover_image"] = cover_image
        self._gql(_SCENE_UPDATE_FULL, {"input": inp})


def _performer(node: dict, pid: str | None = None) -> Performer:
    """A Performer from a GraphQL node (id from the node unless ``pid`` overrides)."""
    return Performer(
        id=str(pid if pid is not None else node["id"]),
        name=node.get("name") or "",
        aliases=list(node.get("alias_list") or []),
        urls=list(node.get("urls") or []),
    )


def _as_seconds(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None
