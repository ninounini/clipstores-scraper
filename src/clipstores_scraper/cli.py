"""clipstores-scraper — find clip-store URLs for Stash scenes and write them back.

  uv run clipstores-scraper             # open the dashboard
  uv run clipstores-scraper scrape      # scrape un-scraped performers (resumable)
  uv run clipstores-scraper scrape --ids 9,8   # scrape just these performers
  uv run clipstores-scraper rescrape    # rescrape scraped performers; only new clips

Headless scrape/rescrape fill state.db; you review, apply and enrich in the
dashboard (run with no arguments). Ctrl-C stops a batch at once — progress is
saved per performer, so a re-run resumes where it left off.

The ``performers``/``candidates``/``images``/``link`` commands are hidden plumbing
for the scene-matcher subagent (see the README), not part of the everyday CLI.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Annotated

import typer

from . import agent_cli, interrupt, pipeline, state
from .config import Config
from .stash import StashClient, StashError

app = typer.Typer(add_completion=False, help=__doc__, no_args_is_help=False)

_IDS_HELP = "Comma-separated Stash performer ids to target; omit for all."


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        # Lazy import keeps textual (and the whole TUI) out of headless runs.
        from .tui import main as tui_main

        tui_main()


@app.command()
def scrape(ids: Annotated[str | None, typer.Option(help=_IDS_HELP)] = None) -> None:
    """Scrape performers' catalogs (parallel, resumable); review in the dashboard.
    --ids targets specific performers, otherwise every un-scraped one."""
    _batch(incremental=False, ids=_parse_ids(ids))


@app.command()
def rescrape(ids: Annotated[str | None, typer.Option(help=_IDS_HELP)] = None) -> None:
    """Rescrape performers, fetching only newly added clips. --ids targets specific
    performers, otherwise every already-scraped one."""
    _batch(incremental=True, ids=_parse_ids(ids))


def _parse_ids(raw: str | None) -> list[str]:
    """Split a --ids value ("9, 8 ,7") into a clean id list; [] means 'all'."""
    return [p.strip() for p in raw.split(",") if p.strip()] if raw else []


def _batch(*, incremental: bool, ids: list[str]) -> None:
    """Headless scrape/rescrape, persisting to state.db. Mirrors the TUI's S/R
    batch without the UI: triage to refresh the worklist, then a bounded thread
    pool. With ``ids`` it scrapes exactly those performers (any status); without,
    the status-based batch. Resumable — each performer is marked as it finishes,
    and Ctrl-C hard-stops (interrupt.arm) with progress saved."""
    config = Config.from_env()
    interrupt.arm()
    verb = "Rescrape" if incremental else "Scrape"
    conn = state.connect()
    try:
        with StashClient(config) as stash:
            state.upsert_performers(conn, pipeline.triage(stash))  # refresh worklist
        state.reset_stale_scraping(conn)  # re-queue anything a crash left mid-scrape
        if ids:
            wanted = set(ids)
            queue = [r for r in state.get_performers(conn) if r.id in wanted]
            missing = wanted - {r.id for r in queue}
            if missing:
                typer.secho(
                    f"No supported store URL for id(s): {', '.join(sorted(missing))}",
                    fg=typer.colors.YELLOW,
                    err=True,
                )
        else:
            queue = (
                state.performers_to_rescrape(conn)
                if incremental
                else state.performers_to_scrape(conn)
            )
        if not queue:
            typer.echo(f"{verb}: nothing to do.")
            return
        workers = max(1, config.scrape_concurrency)
        total = len(queue)
        typer.echo(f"{verb}: {total} performer(s), up to {workers} at a time.\n")
        done = failed = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scrape") as p:
            for fut in as_completed(
                p.submit(_scrape_one, config, r, incremental) for r in queue
            ):
                ok, line = fut.result()
                done += ok
                failed += not ok
                typer.echo(f"[{done + failed}/{total}] {line}")
        typer.echo(f"\n{verb} finished: {done} done, {failed} failed.")
    except StashError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()


def _scrape_one(
    config: Config, r: state.PerformerRow, incremental: bool
) -> tuple[bool, str]:
    """Scrape one storefront and persist its matches. Own DB conn + StashClient:
    this runs in a pool thread. Errors are recorded as 'error' status and returned,
    never raised, so one bad performer doesn't sink the batch."""
    conn = state.connect()
    label = f"{r.name} [{r.store_name}]"
    try:
        state.set_status(conn, r.id, r.store_url, "scraping")
        catalog_size, n_matches, new_clips = pipeline.scrape_persist(
            conn,
            config,
            r.id,
            r.store_url,
            incremental=incremental,
            old_catalog=r.catalog_count,
        )
        extra = "" if new_clips is None else f", {new_clips} new"
        return True, f"{label}: {catalog_size} clips{extra}, {n_matches} match(es)."
    except Exception as exc:  # noqa: BLE001 - record it, let the batch go on
        state.set_status(
            conn, r.id, r.store_url, "error", error=f"{type(exc).__name__}: {exc}"
        )
        return False, f"{label}: failed — {type(exc).__name__}: {exc}"
    finally:
        conn.close()


# Agent plumbing for the scene-matcher subagent. Registered hidden — callable as
# `clipstores-scraper <cmd>` but kept out of --help, so the everyday surface stays
# scrape/rescrape. (ponytail: one binary, the functions stay in agent_cli.)
app.command(hidden=True)(agent_cli.performers)
app.command(hidden=True)(agent_cli.candidates)
app.command(hidden=True)(agent_cli.images)
app.command(hidden=True)(agent_cli.link)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
