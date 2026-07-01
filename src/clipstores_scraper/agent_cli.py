"""Agent-facing CLI behind the `scene-matcher` subagent.

This is the deterministic prep the agent shouldn't do by hand: score a whole
catalog (including the sub-gate "gray zone" the matcher drops), reach clip covers
across every store, and write a link back clobber-safe. The *judgement* — is this
the same scene? — stays with the agent.

  clipstores-scraper candidates "Jessica Dynamic"   # JSON worksheet (read-only)
  clipstores-scraper images 123 https://store/clip   # both covers to /tmp, for eyes
  clipstores-scraper link 123 https://store/clip [--enrich]   # write URL to Stash

These are registered on the single ``clipstores-scraper`` binary (see cli.py);
the ``app`` below stays for direct ``python -m`` use and the tests.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import httpx
import typer

from .config import Config
from .matching import (
    DATE_TOLERANCE,
    DURATION_TOLERANCE,
    _date_delta_days,
    clean_filename,
    score_clip,
    title_score,
)
from .models import Clip, Performer, Scene
from .pipeline import (
    apply_url,
    enrich_scene,
    get_catalog,
    supported_store_urls,
)
from .stash import StashClient, StashError
from .stores import UA, for_url

app = typer.Typer(add_completion=False, help=__doc__)

_TOP_K = 6  # candidates surfaced per scene
_RANK_FLOOR = 0.4  # drop clips weaker than this (unrelated noise)


def _err(msg: str) -> None:
    typer.echo(msg, err=True)


def _stash_base(config: Config) -> str:
    """Stash web root (the /graphql endpoint stripped), for screenshot/scene URLs."""
    return config.stash_url.rstrip("/").removesuffix("/graphql").rstrip("/")


def _short(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:12]


def _resolve_performer(stash: StashClient, who: str) -> Performer:
    """A performer by Stash id (all-digits) or by a unique name/alias substring."""
    if who.isdigit():
        return stash.get_performer(who)
    q = who.lower()
    matches = [
        p
        for p in stash.get_all_performers()
        if q in p.name.lower() or any(q in a.lower() for a in p.aliases)
    ]
    if not matches:
        raise typer.BadParameter(f"No performer matching {who!r}.")
    if len(matches) > 1:
        names = ", ".join(f"{p.name} (id {p.id})" for p in matches[:10])
        raise typer.BadParameter(f"{who!r} matches several: {names}. Use the id.")
    return matches[0]


def rank_candidates(
    scene: Scene, query: str, clips: list[Clip], names: list[str], k: int = _TOP_K
) -> list[dict]:
    """Top-k clips for a scene by title + corroboration, *including sub-gate ones*
    (the gray zone the deterministic matcher drops). Each item carries the raw
    signals plus the matcher's gate verdict (gate == None means it was dropped)."""
    scored: list[dict] = []
    for clip in clips:
        ts = title_score(query, clip.title, names)
        dd = (
            abs(scene.duration - clip.duration)
            if scene.duration and clip.duration
            else None
        )
        td = _date_delta_days(scene.date, clip.date)
        dur_ok = dd is not None and dd <= DURATION_TOLERANCE
        date_ok = td is not None and td <= DATE_TOLERANCE
        rank = ts + (0.3 if dur_ok else 0.0) + (0.2 if date_ok else 0.0)
        if rank < _RANK_FLOOR:
            continue
        cand = score_clip(scene, query, clip, names)
        scored.append(
            {
                "clip_title": clip.title,
                "clip_url": clip.url,
                "source": clip.source,
                "clip_duration": clip.duration,
                "clip_date": clip.date,
                "title_score": round(ts, 3),
                "duration_delta": dd,
                "date_delta": td,
                "gate": cand.confidence if cand else None,
                "rank": round(rank, 3),
            }
        )
    scored.sort(key=lambda c: c["rank"], reverse=True)
    return scored[:k]


@app.command()
def performers(tag: str = "[Monitored]") -> None:
    """List performers carrying a Stash tag (default ``[Monitored]``) as JSON: id,
    name, and the supported store domains found on their profile. A performer whose
    ``stores`` is empty has no supported store URL -- the catalog gap that silently
    hides matches. The matching workflow fans out over this list."""
    config = Config.from_env()
    try:
        with StashClient(config) as stash:
            perfs = stash.performers_with_tag(tag)
    except StashError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    out = []
    for p in perfs:
        domains = sorted({s.domain for u in p.urls if (s := for_url(u)) is not None})
        out.append({"id": p.id, "name": p.name, "stores": domains})
    out.sort(key=lambda e: (not e["stores"], e["name"].lower()))
    missing = sum(1 for e in out if not e["stores"])
    _err(f"{len(out)} performer(s) tagged {tag!r}; {missing} with no supported store.")
    typer.echo(json.dumps(out, ensure_ascii=False, indent=2))


@app.command()
def candidates(performer: str) -> None:
    """Print a JSON worksheet of candidate clips for a performer's unmatched,
    non-organized scenes. Read-only: serves the catalog cache (a cold cache does
    trigger one scrape, logged to stderr)."""
    config = Config.from_env()
    try:
        with StashClient(config) as stash:
            perf = _resolve_performer(stash, performer)
            urls = supported_store_urls(perf)
            if not urls:
                raise typer.BadParameter(
                    f"{perf.name} (id {perf.id}) has no supported store URL."
                )
            base = _stash_base(config)
            worksheet: dict[str, dict] = {}
            for url in urls:
                store = for_url(url)
                if store is None:
                    continue
                scenes = stash.get_unmatched_scenes(perf.id, store.domain)
                clips = get_catalog(store, url, config, log=_err)
                for scene in scenes:
                    query = clean_filename(scene.primary_basename, perf.names)
                    cands = rank_candidates(scene, query, clips, perf.names)
                    if not cands:
                        continue
                    entry = worksheet.setdefault(
                        scene.id,
                        {
                            "scene_id": scene.id,
                            "scene_title": scene.title,
                            "scene_basename": scene.primary_basename,
                            "scene_duration": scene.duration,
                            "scene_date": scene.date,
                            "scene_url": f"{base}/scenes/{scene.id}",
                            "scene_screenshot": f"{base}/scene/{scene.id}/screenshot",
                            "candidates": [],
                        },
                    )
                    entry["candidates"].extend(cands)
    except StashError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    # A scene can surface from several stores; keep its strongest few overall.
    out = []
    for entry in worksheet.values():
        entry["candidates"].sort(key=lambda c: c["rank"], reverse=True)
        entry["candidates"] = entry["candidates"][:_TOP_K]
        out.append(entry)
    out.sort(key=lambda e: e["candidates"][0]["rank"], reverse=True)
    _err(f"{len(out)} scene(s) with candidates for {perf.name} (id {perf.id}).")
    typer.echo(json.dumps(out, ensure_ascii=False, indent=2))


@app.command()
def images(scene_id: str, clip_url: str) -> None:
    """Download the Stash scene cover and the clip's store cover to /tmp and print
    their paths, for a side-by-side visual check (same performer? same scene?)."""
    config = Config.from_env()
    tmp = Path(tempfile.gettempdir())
    base = _stash_base(config)
    headers = {"ApiKey": config.stash_api_key} if config.stash_api_key else {}
    scene_path = tmp / f"scenematch_scene_{scene_id}.jpg"
    shot_url = f"{base}/scene/{scene_id}/screenshot"
    ok = _download(shot_url, scene_path, headers, verify=False)
    typer.echo(f"scene_cover: {scene_path}" if ok else "scene_cover: (unavailable)")

    store = for_url(clip_url)
    if store is None:
        raise typer.BadParameter(f"No store backend for {clip_url}")
    data = store.detail(clip_url, config, log=_err)
    cover = data.cover_url if data else None
    if not cover:
        typer.echo("clip_cover:  (none scraped)")
        return
    clip_path = tmp / f"scenematch_clip_{_short(clip_url)}.jpg"
    ok = _download(cover, clip_path, {"User-Agent": UA}, verify=True)
    typer.echo(f"clip_cover:  {clip_path}" if ok else "clip_cover:  (unavailable)")


def _download(url: str, path: Path, headers: dict, *, verify: bool) -> bool:
    """Fetch url to path. False (not an exception) if it can't be had, so one
    missing cover never aborts the visual check."""
    try:
        resp = httpx.get(
            url, headers=headers, timeout=30.0, follow_redirects=True, verify=verify
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        return False
    path.write_bytes(resp.content)
    return True


@app.command()
def link(scene_id: str, clip_url: str, enrich: bool = False) -> None:
    """Write the clip URL onto a Stash scene (unioned with existing URLs, never
    clobbering). With --enrich, also scrape + write full metadata. Run this only on
    the user's explicit approval of this row."""
    config = Config.from_env()
    try:
        with StashClient(config) as stash:
            wrote = apply_url(stash, scene_id, clip_url)
            typer.echo(
                f"url {'written' if wrote else 'already present'}: "
                f"scene {scene_id} <- {clip_url}"
            )
            if enrich:
                did = enrich_scene(stash, scene_id, config, log=_err)
                typer.echo(f"enrich: {'written' if did else 'nothing scraped'}")
    except StashError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


def main() -> None:
    app()


if __name__ == "__main__":
    main()
