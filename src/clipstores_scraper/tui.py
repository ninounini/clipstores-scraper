"""Textual dashboard for driving the scraper over many performers.

Stash-driven: it lists the performers already in Stash that have a supported
store URL, shows each one's backlog and scrape/review state, and lets you scrape,
review the uncertain matches, and apply -- all from one screen. One row per
performer: a performer's storefronts are pooled, so every action (scrape, review,
apply, clip browse) spans all the stores she sells on. The slow, blocking work
(scraping, Stash I/O) runs in thread workers so the UI stays responsive, and
every result is persisted in ``state.db`` so the dashboard survives restarts.

  t  triage/refresh the performer list from Stash
  s  full rescan of the selected performer (ignores the cache, re-fetches all)
  S (or b)  scrape all un-scraped performers (resumable; press again to stop)
  r  rescrape the selected performer (fetch only newly added clips)
  R  rescrape all scraped performers + retry errored ones (press again to stop)
  l  open/close the live scrape log
  /  filter performers by name or store (esc to clear)
  c  browse the performer's whole clip catalog (all stores); copy URLs by hand
  enter / v  review the selected performer's matches
  a  apply this performer's approved matches to Stash
  A  apply every listed performer's approved matches to Stash
  E  enrich every linked scene (scrape full metadata into Stash); resumable
  q (or ctrl+c twice)  quit

The scrape-all batch is resumable: progress is persisted per performer in
``state.db``, so stopping (S), or even quitting (q) mid-run, loses nothing --
relaunch and press S again to pick up the performers that aren't done yet.

Rescrape (r / R) is incremental: it seeds the store backend with the clips
already cached and stops once it reaches clips it already has, so only the newly
added ones are fetched. Existing review decisions are preserved; new clips just
add new matches.
"""

from __future__ import annotations

import threading
import time
import webbrowser
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime

from rich.markup import escape
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from . import pipeline, state
from .config import Config
from .enrich_cli import _scenes_to_enrich
from .models import Clip
from .stash import StashClient

# Distinguishable colors for the live log. During a parallel scrape-all many
# performers' lines interleave, so each performer is given one stable color
# (hashed from its name) for every line it emits, making the stream readable.
_LOG_PALETTE = (
    "cyan",
    "green",
    "magenta",
    "yellow",
    "blue",
    "bright_red",
    "bright_green",
    "bright_magenta",
    "bright_cyan",
    "bright_yellow",
    "orange1",
    "spring_green2",
)


def _log_color(key: str) -> str:
    """A stable palette color for a key (e.g. a performer name)."""
    return _LOG_PALETTE[zlib.crc32(key.encode("utf-8")) % len(_LOG_PALETTE)]


def _dur(seconds: int | None) -> str:
    """Seconds as m:ss for the catalog table (clip durations are minute-ish)."""
    if not seconds:
        return "-"
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def _ago(ts: float | None) -> str:
    if not ts:
        return "-"
    delta = time.time() - ts
    if delta < 90:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return datetime.fromtimestamp(ts).strftime("%b %d")


_STATUS_LABEL = {
    "new": "·",
    "scraping": "⏳ scraping",
    "scraped": "✓ scraped",
    "error": "✗ error",
}


def _new_clips_label(n: int | None) -> str:
    """Render the last rescrape's new-clip count for the dashboard column."""
    if n is None:
        return "-"
    if n == 0:
        return "[dim]no new clips[/]"
    return f"[green]{n} new clips[/]"


@dataclass(slots=True)
class PerfGroup:
    """One performer's storefronts collapsed into a single dashboard row. A
    performer often sells the same scenes on several stores; the dashboard lists
    them once and the per-store rows (``rows``) drive the actual scrape/apply work
    under the hood. Counts aggregate across the stores."""

    id: str
    name: str
    rows: list[state.PerformerRow] = field(default_factory=list)

    @property
    def store_label(self) -> str:
        return ", ".join(sorted(r.store_name for r in self.rows))

    @property
    def unmatched(self) -> int:
        return sum(r.unmatched_count for r in self.rows)

    @property
    def catalog(self) -> int | None:
        vals = [r.catalog_count for r in self.rows if r.catalog_count is not None]
        return sum(vals) if vals else None

    @property
    def approved(self) -> int:
        return sum(r.approved for r in self.rows)

    @property
    def pending(self) -> int:
        return sum(r.pending for r in self.rows)

    @property
    def applied(self) -> int:
        return sum(r.applied for r in self.rows)

    @property
    def matched(self) -> int:
        return sum(r.matched for r in self.rows)

    @property
    def scraped_at(self) -> float | None:
        ts = [r.scraped_at for r in self.rows if r.scraped_at]
        return max(ts) if ts else None

    @property
    def last_new_clips(self) -> int | None:
        vals = [r.last_new_clips for r in self.rows if r.last_new_clips is not None]
        return sum(vals) if vals else None


class VimDataTable(DataTable):
    """A DataTable with vim-style row navigation on top of the arrow keys:
    j/k down/up, d/u half-page, g/G top/bottom. (h/l are omitted: a row cursor
    has no horizontal position, and `l` is the dashboard's Logs shortcut.)"""

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "scroll_top", "Top", show=False),
        Binding("G", "scroll_bottom", "Bottom", show=False),
        Binding("d", "half_page(1)", "Half page down", show=False),
        Binding("u", "half_page(-1)", "Half page up", show=False),
    ]

    def action_half_page(self, direction: int) -> None:
        if not self.row_count:
            return
        half = max(1, self.size.height // 2)
        target = self.cursor_row + direction * half
        self.move_cursor(row=max(0, min(self.row_count - 1, target)))


class SearchInput(Input):
    """The performer filter box. Escape clears and hides it; enter just hides
    it (keeping the filter), handing focus back to the grid either way."""

    BINDINGS = [
        Binding("escape", "dismiss(True)", "Clear", show=False),
        Binding("enter", "dismiss(False)", "Done", show=False),
    ]

    def action_dismiss(self, clear: bool) -> None:
        self.app.close_search(clear=clear)  # type: ignore[attr-defined]


class CatalogSearchInput(Input):
    """The catalog filter box. Same escape/enter behaviour as SearchInput, but
    scoped to the CatalogScreen rather than the dashboard."""

    BINDINGS = [
        Binding("escape", "dismiss(True)", "Clear", show=False),
        Binding("enter", "dismiss(False)", "Done", show=False),
    ]

    def action_dismiss(self, clear: bool) -> None:
        self.screen.close_search(clear=clear)  # type: ignore[attr-defined]


class CatalogScreen(Screen):
    """Browse a performer's whole clip catalog, pooled across every store they
    sell on. For hand-linking the scenes the matcher can't: filter to the clip,
    copy its URL (y) and paste it onto the Stash scene yourself; open it (o) to
    eyeball the clip first. Read-only — nothing is written to Stash."""

    CSS = """
    #cat-title { padding: 0 1; text-style: bold; }
    #cat-table { height: 1fr; }
    #cat-search { dock: bottom; display: none; }
    #cat-search.visible { display: block; }
    #cat-url { dock: bottom; height: 1; color: $text-muted; padding: 0 1; }
    """

    BINDINGS = [
        Binding("slash", "search", "Filter"),
        Binding("y", "copy", "Copy URL"),
        Binding("o", "open", "Open"),
        Binding("r", "reload", "Rescrape"),
        Binding("escape,q", "back", "Back"),
    ]

    def __init__(self, performer_id: str, name: str) -> None:
        super().__init__()
        self._pid = performer_id
        self._name = name
        self._clips: list[Clip] = []  # everything pooled
        self._shown: list[Clip] = []  # current filtered view
        self._filter = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="cat-title")
        yield VimDataTable(id="cat-table", cursor_type="row", zebra_stripes=True)
        yield CatalogSearchInput(
            id="cat-search", placeholder="Filter clips by title or store…"
        )
        yield Static("", id="cat-url")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#cat-table", DataTable).add_columns(
            "store", "dur", "date", "title"
        )
        self._set_title("loading catalog…")
        self._load(refresh=False)

    def _set_title(self, msg: str) -> None:
        if not self.is_mounted:  # screen popped mid-load; nothing to update
            return
        self.query_one("#cat-title", Static).update(f"{self._name} — {msg}")

    @work(thread=True, exclusive=True, group="catalog")
    def _load(self, refresh: bool) -> None:
        app = self.app
        try:
            with StashClient(app.config) as stash:  # type: ignore[attr-defined]
                performer = stash.get_performer(self._pid)
            clips = pipeline.pooled_catalog(
                app.config,  # type: ignore[attr-defined]
                performer,
                refresh=refresh,
                log=lambda m: app.call_from_thread(app.push_log, m, key=self._name),  # type: ignore[attr-defined]
            )
            clips.sort(key=lambda c: (c.source, c.title.lower()))
            app.call_from_thread(self._loaded, clips)
        except Exception as exc:  # noqa: BLE001 - surface the failure in the title
            app.call_from_thread(
                self._set_title, f"load failed — {type(exc).__name__}: {exc}"
            )

    def _loaded(self, clips: list[Clip]) -> None:
        if not self.is_mounted:  # screen popped before the scrape returned
            return
        self._clips = clips
        stores = len({c.source for c in clips})
        self._set_title(
            f"{len(clips)} clips · {stores} store(s)  "
            "(/ filter · y copy · o open · r rescrape · esc back)"
        )
        self._apply_filter()

    def _apply_filter(self) -> None:
        needle = self._filter.lower()
        self._shown = [
            c
            for c in self._clips
            if not needle or needle in c.title.lower() or needle in c.source.lower()
        ]
        table = self.query_one("#cat-table", DataTable)
        table.clear()
        for c in self._shown:
            table.add_row(c.source, _dur(c.duration), c.date or "-", c.title)
        self._show_url()

    def _current(self) -> Clip | None:
        table = self.query_one("#cat-table", DataTable)
        if not self._shown or table.cursor_row < 0:
            return None
        return self._shown[table.cursor_row]

    def _show_url(self) -> None:
        c = self._current()
        self.query_one("#cat-url", Static).update(c.url if c else "")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "cat-table":
            self._show_url()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "cat-table":
            self.action_open()  # enter opens the clip

    def action_search(self) -> None:
        box = self.query_one("#cat-search", CatalogSearchInput)
        box.add_class("visible")
        box.focus()

    def close_search(self, *, clear: bool) -> None:
        box = self.query_one("#cat-search", CatalogSearchInput)
        box.remove_class("visible")
        if clear and self._filter:
            box.value = ""  # fires on_input_changed -> resets filter
        self.query_one("#cat-table", DataTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "cat-search":
            self._filter = event.value.strip()
            self._apply_filter()

    def action_copy(self) -> None:
        c = self._current()
        if c is None:
            return
        self.app.copy_to_clipboard(c.url)
        self.app.notify(f"Copied: {c.url}", title="Clipboard", timeout=3)

    def action_open(self) -> None:
        c = self._current()
        if c is not None:
            webbrowser.open(c.url)

    def action_reload(self) -> None:
        self._set_title("rescraping…")
        self._load(refresh=True)

    def action_back(self) -> None:
        self.app.pop_screen()


class LogScreen(Screen):
    """A full-screen, scrollable view of scrape progress, live as it streams."""

    BINDINGS = [Binding("l,escape,q", "close", "Close")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="logview", wrap=True, markup=True, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        view = self.query_one("#logview", RichLog)
        view.border_title = "scrape log — l/esc to close"
        for line in self.app.log_lines:  # type: ignore[attr-defined]
            view.write(line)

    def write_line(self, line: str) -> None:
        self.query_one("#logview", RichLog).write(line)

    def action_close(self) -> None:
        self.app.pop_screen()


class ReviewScreen(Screen):
    """Approve / reject a single performer's proposed matches."""

    BINDINGS = [
        Binding("a", "approve", "Approve"),
        Binding("A", "approve_all", "Approve all"),
        Binding("r", "reject", "Reject"),
        Binding("space", "toggle", "Toggle"),
        Binding("escape,q", "back", "Back"),
    ]

    def __init__(self, performer_id: str, name: str) -> None:
        super().__init__()
        self._pid = performer_id
        self._name = name
        self._rows: list[state.MatchRow] = []
        self._decision_col = None  # ColumnKey, set in on_mount

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="review-title")
        table = VimDataTable(id="review-table", cursor_type="row", zebra_stripes=True)
        yield table
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#review-table", DataTable)
        cols = table.add_columns(
            "decision", "conf", "score", "durΔ", "store", "scene file", "clip title"
        )
        self._decision_col = cols[0]
        self._reload()

    @staticmethod
    def _mark(decision: str) -> str:
        return {
            "approved": "[green]approved[/]",
            "pending": "[yellow]pending[/]",
            "rejected": "[red]rejected[/]",
            "applied": "[blue]applied[/]",
        }.get(decision, decision)

    @staticmethod
    def _row_key(m: state.MatchRow) -> str:
        return f"{m.scene_id}|{m.clip_url}"

    def _reload(self) -> None:
        conn = self.app.conn  # type: ignore[attr-defined]
        self._rows = state.get_matches(conn, self._pid)
        table = self.query_one("#review-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        for m in self._rows:
            dur = "-" if m.duration_delta is None else f"{m.duration_delta}s"
            table.add_row(
                self._mark(m.decision),
                m.confidence,
                f"{m.title_score:.2f}",
                dur,
                m.store_name,
                m.filename[:44],
                m.clip_title[:36],
                key=self._row_key(m),
            )
        title = (
            f"Review — {self._name}   "
            f"({len(self._rows)} matches; a=approve A=approve all "
            f"r=reject space=toggle esc=back)"
        )
        self.query_one("#review-title", Static).update(title)
        if self._rows:
            table.move_cursor(row=min(cursor, len(self._rows) - 1))

    def _current(self) -> state.MatchRow | None:
        table = self.query_one("#review-table", DataTable)
        if not self._rows or table.cursor_row < 0:
            return None
        return self._rows[table.cursor_row]

    def _decide(self, decision: str) -> None:
        m = self._current()
        if m is None or m.decision == "applied":
            return
        state.set_decision(self.app.conn, self._pid, m.scene_id, m.clip_url, decision)  # type: ignore[attr-defined]
        # Update just this row's decision cell in place. The match order doesn't
        # depend on the decision, so there's no need to rebuild the table — which
        # would reset the scroll position back to the top.
        m.decision = decision
        self.query_one("#review-table", DataTable).update_cell(
            self._row_key(m), self._decision_col, self._mark(decision)
        )

    def action_approve(self) -> None:
        self._decide("approved")

    def action_approve_all(self) -> None:
        """Approve every match for this performer in one go (skips applied ones)."""
        changed = state.approve_all(self.app.conn, self._pid)  # type: ignore[attr-defined]
        self._reload()
        self.app.notify(f"Approved {changed} match(es) for {self._name}.", timeout=3)

    def action_reject(self) -> None:
        self._decide("rejected")

    def action_toggle(self) -> None:
        m = self._current()
        if m is not None and m.decision != "applied":
            self._decide("rejected" if m.decision == "approved" else "approved")

    def action_back(self) -> None:
        self.app.pop_screen()
        self.app.refresh_table()  # type: ignore[attr-defined]


class DashboardApp(App):
    """The performer dashboard."""

    CSS = """
    #activity { dock: bottom; height: 1; padding: 0 1; display: none;
                color: $text; background: $accent; text-style: bold; }
    #activity.on { display: block; }
    #note { dock: bottom; height: 1; color: $text-muted; padding: 0 1; }
    #search { dock: bottom; display: none; }
    #search.visible { display: block; }
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        Binding("t", "triage", "Triage"),
        Binding("s", "scrape", "Rescan"),
        # "b" (batch) kept as a hidden alias in case a terminal ever delivers
        # Shift+S as a plain "s".
        Binding("S", "scrape_all", "Scrape all"),
        Binding("b", "scrape_all", "Scrape all", show=False),
        Binding("r", "rescrape", "Rescrape"),
        Binding("R", "rescrape_all", "Rescrape all"),
        Binding("l", "logs", "Logs"),
        Binding("slash", "search", "Search"),
        Binding("c", "catalog", "Clips"),
        Binding("v", "review", "Review"),
        Binding("a", "apply", "Apply approved"),
        Binding("A", "apply_all", "Apply all"),
        Binding("E", "enrich_all", "Enrich all"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.config = Config.from_env()
        self.conn = state.connect()
        self._rows: list[PerfGroup] = []
        self._filter = ""
        self._batch_active = False
        self._batch_stop = threading.Event()
        # Keyed by (performer_id, store_url): a performer can have several
        # storefront rows, and only the one being scraped should show as queued.
        self._batch_queue: set[tuple[str, str]] = set()
        self._batch_label = "Scrape-all"
        # Shared tallies for the parallel scrape-all, guarded by the lock since
        # several scrape threads update them at once.
        self._batch_lock = threading.Lock()
        self._batch_done = 0
        self._batch_failed = 0
        self._batch_running = 0
        self._batch_total = 0
        self._last_ctrl_c = 0.0
        self._enrich_active = False
        self.log_lines: deque[str] = deque(maxlen=2000)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield VimDataTable(id="grid", cursor_type="row", zebra_stripes=True)
        yield SearchInput(id="search", placeholder="Filter by performer or store…")
        yield Static("", id="activity")
        yield Static("", id="note")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#grid", DataTable)
        table.add_columns(
            "performer",
            "stores",
            "unmatched",
            "catalog",
            "approved",
            "pending",
            "applied",
            "scraped",
            "status",
            "new clips",
        )
        # Nothing can be scraping at startup; clear any status left behind by a
        # quit/crash mid-scrape so those performers re-queue for scrape-all.
        state.reset_stale_scraping(self.conn)
        self.refresh_table()
        if not self._rows:
            self.note("No performers yet — press t to triage from Stash.")
        else:
            todo = sum(
                1
                for g in self._rows
                if any(r.status != "scraped" and r.unmatched_count > 0 for r in g.rows)
            )
            if todo:
                self.note(f"{todo} performer(s) to scrape — press S to scrape all.")

    # ---- helpers -------------------------------------------------------
    def note(self, msg: str) -> None:
        self.query_one("#note", Static).update(msg)

    def set_activity(self, msg: str | None) -> None:
        """Show (or clear) the persistent activity banner above the note line.
        Used for background work — triage and scrape-all — so there's always a
        visible 'something is happening' indicator, not just a transient note."""
        bar = self.query_one("#activity", Static)
        if msg:
            bar.update(msg)
            bar.add_class("on")
        else:
            bar.remove_class("on")

    def push_log(self, msg: str, *, key: str | None = None) -> None:
        """Record a progress line: keep it in the buffer, surface it on the note
        line, and stream it into the log view if that screen is open.

        ``key`` (a performer name) tints the whole line one stable color so
        interleaved scrape output is easy to follow. The message is escaped so
        literal brackets (e.g. "[2/5]") render as text rather than markup."""
        text = escape(msg)
        if key:
            text = f"[{_log_color(key)}]{text}[/]"
        line = f"{datetime.now():%H:%M:%S}  {text}"
        self.log_lines.append(line)
        self.note(text)
        if isinstance(self.screen, LogScreen):
            self.screen.write_line(line)

    def _grouped(self, rows: list[state.PerformerRow]) -> list[PerfGroup]:
        """Collapse the per-(performer, store) rows into one PerfGroup each,
        preserving the incoming name order (get_performers sorts by name)."""
        groups: dict[str, PerfGroup] = {}
        for r in rows:
            group = groups.get(r.id)
            if group is None:
                group = groups[r.id] = PerfGroup(id=r.id, name=r.name)
            group.rows.append(r)
        return list(groups.values())

    def _group_status(self, group: PerfGroup) -> str:
        """One status cell for a collapsed row: the most 'active' store wins, so a
        performer mid-scrape reads as scraping even if her other stores are done."""
        statuses = {r.status for r in group.rows}
        if "scraping" in statuses:
            return _STATUS_LABEL["scraping"]
        if any((r.id, r.store_url) in self._batch_queue for r in group.rows):
            return "[cyan]… queued[/]"
        if "error" in statuses:
            return _STATUS_LABEL["error"]
        if statuses == {"scraped"}:
            return _STATUS_LABEL["scraped"]
        return _STATUS_LABEL["new"]

    def refresh_table(self) -> None:
        table = self.query_one("#grid", DataTable)
        cursor = table.cursor_row
        # Only performers with something left to match; 0-unmatched ones are done
        # (or already fully URL'd) and just clutter the worklist. Press t to
        # re-triage after applying so finished rows drop off.
        groups = [
            g for g in self._grouped(state.get_performers(self.conn)) if g.unmatched > 0
        ]
        if self._filter:
            needle = self._filter.lower()
            groups = [
                g
                for g in groups
                if needle in g.name.lower() or needle in g.store_label.lower()
            ]
        self._rows = groups
        table.clear()
        for g in groups:
            table.add_row(
                g.name[:28],
                g.store_label[:26],
                str(g.unmatched),
                "-" if g.catalog is None else str(g.catalog),
                f"[green]{g.approved}[/]" if g.approved else "0",
                f"[yellow]{g.pending}[/]" if g.pending else "0",
                f"[blue]{g.applied}[/]" if g.applied else "0",
                _ago(g.scraped_at),
                self._group_status(g),
                _new_clips_label(g.last_new_clips),
            )
        if self._rows:
            table.move_cursor(row=min(max(cursor, 0), len(self._rows) - 1))

    def _selected(self) -> PerfGroup | None:
        table = self.query_one("#grid", DataTable)
        if not self._rows or table.cursor_row < 0:
            return None
        return self._rows[table.cursor_row]

    @property
    def _on_dashboard(self) -> bool:
        # App-level key bindings stay live even while a sub-screen (Review/Log)
        # is on top, so e.g. pressing `v` from inside a ReviewScreen would push
        # another one and you'd have to escape out of each. These dashboard
        # actions only make sense when the grid is the active screen.
        return self.screen is self.screen_stack[0]

    # ---- actions -------------------------------------------------------
    def action_help_quit(self) -> None:
        """Ctrl+C handler. Textual binds Ctrl+C here (not straight to quit) so a
        reflexive press can't kill the app by accident. We require two: the
        first warns, a second within a couple of seconds quits. Mid-scrape is
        safe to quit — progress is persisted and scrape-all resumes on relaunch."""
        now = time.monotonic()
        if now - self._last_ctrl_c <= 2.0:
            self.exit()
            return
        self._last_ctrl_c = now
        hint = "Scrape-all will resume on next launch. " if self._batch_active else ""
        self.notify(
            f"{hint}Press Ctrl+C again to quit.",
            title="Quit?",
            severity="warning",
            timeout=3,
        )

    def action_triage(self) -> None:
        if not self._on_dashboard:
            return
        self.set_activity("⟳ Refreshing from Stash… (this can take a moment)")
        self.push_log("Refreshing from Stash…")
        self._triage_worker()

    def action_scrape(self) -> None:
        # `s` is a full rescan: ignore the cache and re-fetch the whole catalog.
        self._scrape_selected(incremental=False, refresh=True)

    def action_rescrape(self) -> None:
        self._scrape_selected(incremental=True)

    def _scrape_selected(self, *, incremental: bool, refresh: bool = False) -> None:
        if not self._on_dashboard:
            return
        if self._batch_active:
            self.note(f"{self._batch_label} is running — stop it first.")
            return
        group = self._selected()
        if group is None:
            return
        # Scrape every storefront that still has scenes to match and isn't already
        # scraping; each store is its own catalog, so they fan out as separate
        # workers.
        targets = [
            r for r in group.rows if r.unmatched_count > 0 and r.status != "scraping"
        ]
        if not targets:
            self.note(f"{group.name}: nothing to scrape.")
            return
        verb = "Rescraping" if incremental else "Full rescan of"
        self.push_log(
            f"{verb} {group.name} — {len(targets)} store(s)… (press l to watch)",
            key=group.name,
        )
        for r in targets:
            self._scrape_worker(
                r.id,
                r.store_url,
                r.store_name,
                group.name,
                incremental,
                r.catalog_count,
                refresh,
            )

    def action_scrape_all(self) -> None:
        self._start_batch(label="Scrape-all", incremental=False)

    def action_rescrape_all(self) -> None:
        self._start_batch(label="Rescrape-all", incremental=True)

    def _start_batch(self, *, label: str, incremental: bool) -> None:
        if not self._on_dashboard:
            return
        if self._batch_active:  # any running batch: this keypress means "stop"
            self._batch_stop.set()
            self.set_activity(f"⟳ {self._batch_label}: stopping after in-flight…")
            return
        self._batch_stop.clear()
        self._batch_active = True
        self._batch_label = label
        key = "R" if incremental else "S"
        self.set_activity(f"⟳ {label}: starting… ({key} to stop, q to quit & resume)")
        self.push_log(f"{label}: starting… ({key} to stop; q quits & resumes later)")
        self._scrape_all_worker(incremental)

    def action_logs(self) -> None:
        if isinstance(self.screen, LogScreen):
            self.pop_screen()
        elif self._on_dashboard:
            self.push_screen(LogScreen())

    def action_search(self) -> None:
        search = self.query_one("#search", SearchInput)
        search.add_class("visible")
        search.focus()

    def close_search(self, *, clear: bool) -> None:
        """Hand focus back to the grid; optionally drop the active filter."""
        search = self.query_one("#search", SearchInput)
        search.remove_class("visible")
        if clear and self._filter:
            search.value = ""  # fires Input.Changed -> resets _filter + refresh
        self.query_one("#grid", DataTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search":
            return
        self._filter = event.value.strip()
        self.refresh_table()
        if self._filter and not self._rows:
            self.note(f"No performers match “{self._filter}”.")

    def action_catalog(self) -> None:
        if not self._on_dashboard:
            return
        row = self._selected()
        if row is not None:
            self.push_screen(CatalogScreen(row.id, row.name))

    def action_review(self) -> None:
        if not self._on_dashboard:
            return
        group = self._selected()
        if group is None:
            return
        if group.matched == 0:
            self.note(f"{group.name} has no matches yet — scrape first (s).")
            return
        self.push_screen(ReviewScreen(group.id, group.name))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # The grid DataTable handles `enter` itself (select_cursor), so it never
        # reaches an app-level key binding; hook its selection message instead.
        # Guard on the id: the ReviewScreen's own table bubbles here too.
        if event.data_table.id == "grid":
            self.action_review()

    def action_apply(self) -> None:
        if not self._on_dashboard:
            return
        group = self._selected()
        if group is None:
            return
        if group.approved == 0:
            self.note(f"{group.name} has no approved matches.")
            return
        self.note(f"Applying {group.approved} approved URL(s) for {group.name} …")
        self._apply_worker(group.id, group.name)

    def action_apply_all(self) -> None:
        if not self._on_dashboard:
            return
        targets = [(g.id, g.name) for g in self._rows if g.approved > 0]
        if not targets:
            self.note("No approved matches to apply on any performer.")
            return
        total = sum(g.approved for g in self._rows if g.approved > 0)
        self.note(
            f"Applying {total} approved URL(s) across {len(targets)} performer(s) …"
        )
        self._apply_all_worker(targets)

    def action_enrich_all(self) -> None:
        """Enrich every linked scene in Stash — the TUI equivalent of
        `clipstores-scraper enrich --apply`. Spans all of Stash, not just the row."""
        if not self._on_dashboard:
            return
        if self._enrich_active:
            self.note("Enrich-all is already running.")
            return
        self._enrich_active = True
        self.set_activity("⟳ Enrich-all: finding linked scenes…")
        self.push_log("Enrich-all: finding scenes with a store URL…")
        self._enrich_all_worker()

    # ---- workers (run off the UI thread) -------------------------------
    @work(thread=True, group="triage", exclusive=True)
    def _triage_worker(self) -> None:
        conn = state.connect()
        try:
            with StashClient(self.config) as stash:
                rows = pipeline.triage(stash)
            state.upsert_performers(conn, rows)
            msg = f"Triage done: {len(rows)} performer(s) with a supported store URL."
        except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
            msg = f"Triage failed: {type(exc).__name__}: {exc}"
        finally:
            conn.close()
        self.call_from_thread(self.set_activity, None)
        self.call_from_thread(self.refresh_table)
        self.call_from_thread(self.push_log, msg)
        self.call_from_thread(self.notify, msg, title="Refresh")

    @work(thread=True, group="scrape")
    def _scrape_worker(
        self,
        performer_id: str,
        store_url: str,
        store_name: str,
        name: str,
        incremental: bool = False,
        old_catalog: int | None = None,
        refresh: bool = False,
    ) -> None:
        conn = state.connect()
        label = f"{name} [{store_name}]"
        try:
            state.set_status(conn, performer_id, store_url, "scraping")
            self.call_from_thread(self.refresh_table)
            catalog_size, n_matches, new_clips = pipeline.scrape_persist(
                conn,
                self.config,
                performer_id,
                store_url,
                refresh=refresh,
                incremental=incremental,
                old_catalog=old_catalog,
                log=lambda m: self.call_from_thread(
                    self.push_log, f"{label}: {m}", key=name
                ),
            )
            extra = "" if new_clips is None else f", {new_clips} new"
            msg = f"{label}: done — {catalog_size} clips{extra}, {n_matches} match(es)."
        except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
            state.set_status(
                conn,
                performer_id,
                store_url,
                "error",
                error=f"{type(exc).__name__}: {exc}",
            )
            msg = f"{label}: scrape failed — {type(exc).__name__}: {exc}"
        finally:
            conn.close()
        self.call_from_thread(self.refresh_table)
        self.call_from_thread(self.push_log, msg, key=name)

    @work(thread=True, group="scrape-all", exclusive=True)
    def _scrape_all_worker(self, incremental: bool = False) -> None:
        """Coordinate a parallel scrape (or rescrape) of many performers.

        Hands each performer to a bounded thread pool (config.scrape_concurrency)
        and tallies results as they land. A plain scrape-all takes every
        un-scraped performer and marks each 'scraped' as it finishes, so progress
        survives a stop or a quit. A rescrape-all (``incremental``) revisits
        already-scraped performers and fetches only clips added since last time.
        Stopping prevents not-yet-started performers from starting; the ones
        already in flight run to completion."""
        label = self._batch_label
        conn = state.connect()
        msg: str | None = None
        with self._batch_lock:
            self._batch_done = self._batch_failed = self._batch_running = 0
            self._batch_total = 0
        try:
            queue = (
                state.performers_to_rescrape(conn)
                if incremental
                else state.performers_to_scrape(conn)
            )
            total = len(queue)
            if total == 0:
                msg = (
                    f"{label}: nothing to do — no performers with unmatched scenes "
                    + ("that have been scraped." if incremental else "left to scrape.")
                )
                return
            self._batch_total = total
            # Mark the whole batch 'queued' so it's clearly a batch in flight.
            self._batch_queue = {(r.id, r.store_url) for r in queue}
            self.call_from_thread(self.refresh_table)
            workers = max(1, self.config.scrape_concurrency)
            self.call_from_thread(
                self.push_log,
                f"{label}: {total} performer(s), up to {workers} at a time.",
            )
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="scrape-all"
            ) as pool:
                futures = [pool.submit(self._scrape_one, r, incremental) for r in queue]
                for fut in as_completed(futures):
                    fut.result()  # tasks swallow their own errors; surface bugs
            done, failed = self._batch_done, self._batch_failed
            left = total - done - failed
            key = "R" if incremental else "S"
            if self._batch_stop.is_set():
                msg = (
                    f"{label} stopped: {done} done, {failed} failed, "
                    f"{left} left — press {key} to resume."
                )
            else:
                msg = f"{label} finished: {done} done, {failed} failed."
        except Exception as exc:  # noqa: BLE001 - surface a batch-level failure
            msg = f"{label} aborted: {type(exc).__name__}: {exc}"
        finally:
            conn.close()
            self._batch_active = False
            self._batch_queue = set()
            self.call_from_thread(self.set_activity, None)
            self.call_from_thread(self.refresh_table)
            if msg:
                self.call_from_thread(self.push_log, msg)
                self.call_from_thread(self.notify, msg, title=label)

    def _scrape_one(self, r: state.PerformerRow, incremental: bool = False) -> None:
        """Scrape a single performer inside the scrape-all pool. Mirrors
        _scrape_worker but feeds the shared batch tallies and banner. Runs in a
        pool thread, so it uses its own DB connection and StashClient."""
        if self._batch_stop.is_set():
            return  # asked to stop before this one started — leave it queued
        # set.discard is atomic under the GIL
        self._batch_queue.discard((r.id, r.store_url))
        with self._batch_lock:
            self._batch_running += 1
        self._update_banner()
        conn = state.connect()
        label = f"{r.name} [{r.store_name}]"
        try:
            state.set_status(conn, r.id, r.store_url, "scraping")
            self.call_from_thread(self.refresh_table)
            self.call_from_thread(self.push_log, f"{label}: scraping…", key=r.name)
            catalog_size, n_matches, new_clips = pipeline.scrape_persist(
                conn,
                self.config,
                r.id,
                r.store_url,
                incremental=incremental,
                old_catalog=r.catalog_count,
                log=lambda m, lbl=label, n=r.name: self.call_from_thread(
                    self.push_log, f"{lbl}: {m}", key=n
                ),
            )
            with self._batch_lock:
                self._batch_done += 1
                seen, total = self._batch_done + self._batch_failed, self._batch_total
            extra = "" if new_clips is None else f", {new_clips} new"
            self.call_from_thread(
                self.push_log,
                f"[{seen}/{total}] {label}: {catalog_size} clips{extra}, "
                f"{n_matches} match(es).",
                key=r.name,
            )
        except Exception as exc:  # noqa: BLE001 - record it, let the batch go on
            state.set_status(
                conn, r.id, r.store_url, "error", error=f"{type(exc).__name__}: {exc}"
            )
            with self._batch_lock:
                self._batch_failed += 1
                seen, total = self._batch_done + self._batch_failed, self._batch_total
            self.call_from_thread(
                self.push_log,
                f"[{seen}/{total}] {label}: failed — {type(exc).__name__}: {exc}",
                key=r.name,
            )
        finally:
            conn.close()
            with self._batch_lock:
                self._batch_running -= 1
            self._update_banner()
            self.call_from_thread(self.refresh_table)

    def _update_banner(self) -> None:
        """Refresh the activity banner from the shared batch tallies."""
        with self._batch_lock:
            done, failed = self._batch_done, self._batch_failed
            running, total = self._batch_running, self._batch_total
        stopping = (
            "  (stopping — letting in-flight finish…)"
            if (self._batch_stop.is_set())
            else ""
        )
        self.call_from_thread(
            self.set_activity,
            f"⟳ {self._batch_label} · {done + failed}/{total} done · "
            f"{running} running · {failed} failed{stopping}",
        )

    @work(thread=True, group="apply")
    def _apply_worker(self, performer_id: str, name: str) -> None:
        conn = state.connect()
        try:
            with StashClient(self.config) as stash:
                written, approved = self._apply_performer(conn, stash, performer_id)
            msg = f"{name}: applied {written} new URL(s) ({approved} approved)."
        except Exception as exc:  # noqa: BLE001
            msg = f"{name}: apply failed — {type(exc).__name__}: {exc}"
        finally:
            conn.close()
        self.call_from_thread(self.refresh_table)
        self.call_from_thread(self.push_log, msg, key=name)

    @work(thread=True, group="apply")
    def _apply_all_worker(self, targets: list[tuple[str, str]]) -> None:
        """Apply every listed performer's approved matches, one after another."""
        conn = state.connect()
        written = failed = 0
        try:
            with StashClient(self.config) as stash:
                for i, (performer_id, name) in enumerate(targets, 1):
                    try:
                        w, approved = self._apply_performer(conn, stash, performer_id)
                        written += w
                        self.call_from_thread(
                            self.push_log,
                            f"[{i}/{len(targets)}] {name}: applied {w} new URL(s) "
                            f"({approved} approved).",
                            key=name,
                        )
                    except Exception as exc:  # noqa: BLE001 - one fails, go on
                        failed += 1
                        self.call_from_thread(
                            self.push_log,
                            f"[{i}/{len(targets)}] {name}: apply failed — "
                            f"{type(exc).__name__}: {exc}",
                            key=name,
                        )
                    self.call_from_thread(self.refresh_table)
            tail = f" ({failed} performer(s) failed)" if failed else ""
            msg = (
                f"Apply-all done: {written} new URL(s) across "
                f"{len(targets)} performer(s){tail}."
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"Apply-all failed — {type(exc).__name__}: {exc}"
        finally:
            conn.close()
        self.call_from_thread(self.refresh_table)
        self.call_from_thread(self.push_log, msg)
        self.call_from_thread(self.notify, msg, title="Apply all")

    @work(thread=True, group="enrich", exclusive=True)
    def _enrich_all_worker(self) -> None:
        """Scrape full metadata for every linked scene and write it back, up to
        scrape_concurrency at a time. Resumable: the enrich-tag marker means a
        re-run skips scenes already done. Mirrors enrich_cli._apply_all, logging
        to the dashboard instead of stdout."""
        workers = max(1, self.config.scrape_concurrency)
        msg: str | None = None
        done = failed = 0
        try:
            with StashClient(self.config) as stash:
                ids = _scenes_to_enrich(stash, self.config, None)
                total = len(ids)
                if total == 0:
                    msg = "Enrich-all: no scenes to enrich."
                    return
                self.call_from_thread(
                    self.push_log,
                    f"Enrich-all: {total} scene(s), up to {workers} at a time.",
                )

                with ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix="enrich"
                ) as pool:
                    for fut in as_completed(
                        pool.submit(pipeline.enrich_one, stash, sid, self.config)
                        for sid in ids
                    ):
                        scene_id, status = fut.result()
                        done += 1
                        if status.startswith("FAILED"):
                            failed += 1
                        self.call_from_thread(
                            self.push_log,
                            f"[{done}/{total}] scene {scene_id}: {status}",
                        )
                        self.call_from_thread(
                            self.set_activity,
                            f"⟳ Enrich-all · {done}/{total} done · {failed} failed",
                        )
            msg = f"Enrich-all finished: {total} scene(s), {failed} failed."
        except Exception as exc:  # noqa: BLE001 - surface a run-level failure
            msg = f"Enrich-all aborted: {type(exc).__name__}: {exc}"
        finally:
            self._enrich_active = False
            self.call_from_thread(self.set_activity, None)
            self.call_from_thread(self.refresh_table)
            if msg:
                self.call_from_thread(self.push_log, msg)
                self.call_from_thread(self.notify, msg, title="Enrich all")

    @staticmethod
    def _apply_performer(
        conn, stash: StashClient, performer_id: str
    ) -> tuple[int, int]:
        """Write a performer's approved URLs (across all their stores) to Stash,
        marking each applied. Returns (newly written, approved seen). Runs on a
        worker thread."""
        approved = state.get_matches(conn, performer_id, decision="approved")
        written = 0
        for m in approved:
            try:
                if pipeline.apply_url(stash, m.scene_id, m.clip_url):
                    written += 1
                state.mark_applied(conn, performer_id, m.scene_id, m.clip_url)
            except Exception:  # noqa: BLE001 - skip one, keep going
                continue
        return written, len(approved)


def main() -> None:
    DashboardApp().run()


if __name__ == "__main__":
    main()
