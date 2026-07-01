"""Scrape full metadata for already-linked scenes and write complete Stash scenes.

The enrich routine behind the dashboard's `E` (enrich-all). Not a clipstores-scraper
subcommand; run it from the dashboard, or directly for a dry run / one-off:

  python -m clipstores_scraper.enrich_cli            # dry run: preview a few scenes
  python -m clipstores_scraper.enrich_cli --apply    # enrich EVERY remaining scene
  python -m clipstores_scraper.enrich_cli --scene 7  # specific scene id(s); repeatable

It picks scenes that carry a supported store URL and aren't yet marked done (the
CLIPSTORE_ENRICH_TAG marker), most-linked first. A dry run previews the first few
and writes nothing; --apply enriches them all in parallel and marks each one, so
a re-run resumes where it left off. Ctrl-C stops immediately. Egress: host VPN.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Annotated

import typer

from . import interrupt
from .config import Config
from .models import SceneData
from .pipeline import enrich_one, scene_store_details
from .stash import StashClient, StashError
from .stores import REGISTRY, for_url

app = typer.Typer(add_completion=False, help=__doc__)

_PREVIEW_N = 4  # scenes shown by a dry run (--apply does all of them)


@app.command()
def run(
    scene: Annotated[
        list[int] | None,
        typer.Option(help="Scene id(s) to enrich; repeatable. Default: auto-pick."),
    ] = None,
    apply: Annotated[
        bool, typer.Option(help="Write to Stash (default: dry-run preview).")
    ] = False,
) -> None:
    config = Config.from_env()
    interrupt.arm()  # Ctrl-C stops immediately (handled by interrupt's watcher thread)
    try:
        with StashClient(config) as stash:
            # --apply enriches everything (resumable, Ctrl-C stops it); a dry run
            # just previews the first few.
            ids = (
                [str(s) for s in scene]
                if scene
                else _scenes_to_enrich(stash, config, None if apply else _PREVIEW_N)
            )
            if not ids:
                typer.echo("No scenes to enrich.")
                return
            if apply:
                _apply_all(stash, ids, config)
            else:
                typer.echo(f"Previewing {len(ids)} scene(s).\n")
                for scene_id in ids:
                    _preview(stash, scene_id, config)
    except StashError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


def _scenes_to_enrich(
    stash: StashClient, config: Config, limit: int | None
) -> list[str]:
    """Scenes to enrich: any carrying a supported store URL and not yet marked
    done, most-linked first. ``limit`` caps the count (None = all). Skipping the
    marker tag (when set) is what lets repeated runs walk the backlog in batches."""
    urls_by_scene: dict[str, list[str]] = {}
    for store in REGISTRY:
        for scene_id, urls in stash.scenes_with_url(store.domain, config.enrich_tag):
            urls_by_scene[scene_id] = urls
    scored = [
        (len({s.name for u in urls if (s := for_url(u))}), int(scene_id))
        for scene_id, urls in urls_by_scene.items()
    ]
    scored.sort(reverse=True)  # most stores first, then highest id
    return [str(scene_id) for _, scene_id in scored[:limit]]


def _apply_all(stash: StashClient, ids: list[str], config: Config) -> None:
    """Enrich + write every scene, up to scrape_concurrency at a time. Each worker
    scrapes over HTTP; tag/studio create is locked in StashClient so no
    duplicates. One line per scene as it finishes (order is completion order)."""
    workers = max(1, config.scrape_concurrency)
    total = len(ids)
    typer.echo(f"Applying {total} scene(s), up to {workers} in parallel.\n")

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed(
            pool.submit(enrich_one, stash, sid, config) for sid in ids
        ):
            scene_id, status = future.result()
            done += 1
            line = f"[{done}/{total}] scene {scene_id}: {status}"
            if status.startswith("FAILED"):
                typer.secho(line, fg=typer.colors.RED)
            else:
                typer.echo(line)


def _preview(stash: StashClient, scene_id: str, config: Config) -> None:
    typer.secho(f"── scene {scene_id} ──", bold=True)
    _, datas, merged = scene_store_details(stash, scene_id, config, log=typer.echo)
    for data in datas:
        _show(data, data.source)
    if merged:
        _show(merged, "MERGED")
    else:
        typer.echo("  (nothing scraped)")
    typer.echo("")


def _show(data: SceneData, label: str) -> None:
    details = (data.details or "").replace("\n", " ")
    if len(details) > 80:
        details = details[:77] + "…"
    typer.secho(f"  [{label}]", fg=typer.colors.CYAN)
    typer.echo(f"    title:   {data.title}")
    typer.echo(f"    date:    {data.date}    code: {data.code}")
    typer.echo(f"    studio:  {data.studio}")
    typer.echo(f"    tags:    {', '.join(data.tags) or '—'}")
    typer.echo(f"    cover:   {data.cover_url or '—'}")
    typer.echo(f"    details: {details or '—'}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
