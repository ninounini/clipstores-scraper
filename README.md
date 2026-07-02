# clipstores-scraper

Find the clip-store URLs for the scenes that aren't in StashDB, then optionally
scrape their metadata into your Stash — all from one dashboard.

It works on a scene when all of these are true:

- it has a **performer**,
- that performer has a **supported store URL** (see [Stores](#stores)),
- it has **no StashDB id** — i.e. it isn't in StashDB,
- and it isn't marked **Organized** — a scene you've marked done is left alone.

> **Tip** — I organize media one folder per performer: put all of performer X's
> clips in her folder, then in Stash filter scenes by that path and add the
> performer to all of them at once.

## Quickstart

Runs with [uv](https://docs.astral.sh/uv/), a small program that installs and
launches Python apps. Install it once:
`curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`).

```bash
git clone https://github.com/ninounini/clipstores-scraper.git
cd clipstores-scraper
uv sync
cp .env.example .env        # set STASH_URL, STASH_API_KEY, CLIPSTORE_ENRICH_TAG
uv run clipstores-scraper   # open the dashboard
```

No git? Click **Code → Download ZIP**, unzip, and open a terminal in that
folder — every command in this README runs from there.

Do the one-time [prep in Stash](#before-this-tool-prep-in-stash) first — skip it
and `t` finds 0 performers. Scraping runs on this computer, not on your Stash
instance — useful now that the EU's age-verification checks block many stores: if
you can run a VPN here, scrape here. Then work top to bottom:

**`t`** sync from Stash → **`S`** scrape all (or **`R`** rescrape) → **`enter`**
review → **`A`** apply (write the approved links to Stash) → **`E`** enrich
(optional — see [Enrich](#enrich)).

That's the whole loop. Everything below is detail.

## Dashboard

One row per performer, with all her storefronts pooled — so every action spans
every store she sells on. Scrape, review, apply, enrich, all from one screen.

| key              | action                                                           |
| ---------------- | ---------------------------------------------------------------- |
| `t`              | sync performers from Stash (biggest backlog first)               |
| `s`              | full rescan of selected performer (ignores cache)                |
| `S` (or `b`)     | scrape all un-scraped performers (parallel; press again to stop) |
| `r`              | rescrape selected performer — only newly added clips             |
| `R`              | rescrape all scraped performers (parallel; again = stop)         |
| `l`              | open/close live scrape log                                       |
| `/`              | filter by name or store (esc to clear)                           |
| `c`              | browse the performer's whole clip catalog; copy URLs by hand     |
| `enter` / `v`    | review selected performer's matches (approve / reject)           |
| `a`              | apply this performer's approved matches to Stash                 |
| `A`              | apply every performer's approved matches to Stash                |
| `E`              | enrich every linked scene                                        |
| `q` / `ctrl+c`×2 | quit                                                             |

High-confidence matches are pre-approved; review is just for the uncertain ones.
State (performers, matches, decisions) lives in `state.db` and survives
restarts — re-scrapes never discard reviewed work. Scrape/rescrape run in
parallel and are resumable: Ctrl-C or a crash loses nothing, re-run to pick up
the rest. Rescrape is incremental (assumes newest clips first); want a full
rebuild? Delete `cache/<store>/<id>.json` and scrape again.

Why dump the whole catalog and match locally instead of searching the store?
Store search is unreliable. A local catalog lets us cross-check duration + date —
that's what makes auto-linking safe.

## Before this tool: prep in Stash

This tool only does the last two steps — **match** and **enrich**. First, in
Stash:

1. **Scrape your performers with StashDB** so they have their store URLs — this
   tool reads them from the performer's "URLs" field.
2. **Run the StashDB scene scraper** — the scenes it can't identify (no StashDB
   id) are what this tool then matches and enriches.

## Enrich

Optional, and mostly a nice-to-have: it earns its keep when your Stash can't
reach the clip stores directly — e.g. from the EU, where many now require age
verification.
This tool scrapes behind your VPN and writes the metadata in. If your Stash *can*
reach the stores, skip it — apply the URL and let Stash's own community scrapers
pull the metadata.

It writes title, date, details, cover, code, and **all tags from every linked store**
(missing tags are created). Scene linked to several stores? Scalar fields follow
**IWantClips > ManyVids > Clips4Sale > LoyalFans**, lower stores fill gaps.
**Studio is guessed from the performer's other scenes** (store studio names are
inconsistent); no clear majority → left blank. The performer is untouched. Only
scenes without a StashDB id are touched, and each is stamped with your
`CLIPSTORE_ENRICH_TAG` marker (with the tag set, re-runs skip already-enriched
scenes).

## Headless (optional)

Prefer to scrape from cron or over SSH? Same scrape, no UI. Results land in
`state.db`; review, apply and enrich in the dashboard later.

```bash
uv run clipstores-scraper scrape                 # every un-scraped performer (parallel, resumable)
uv run clipstores-scraper scrape --ids 9999,8888 # just these performers
uv run clipstores-scraper rescrape               # revisit scraped performers; only new clips
```

`--ids` is a comma-separated list of Stash performer ids; omit it for all.

## AI matching

Optional, for [Claude Code](https://claude.com/claude-code) users. The dashboard
only auto-links when title + duration or date agree. For the harder cases —
renamed, abbreviated or censored titles, clips with no duration — the bundled
**scene-matcher** AI agent also compares cover images, so it finds matches the
dashboard misses. Open Claude Code in this folder and ask in plain language:

> find performers tagged [Monitored]

> match scenes for performer X

It compares the covers and writes only the matches you approve.

## Stores

| Store                              | Method                  |
| ---------------------------------- | ----------------------- |
| IWantClips                         | HTTP (Typesense + HTML) |
| ManyVids                           | HTTP (JSON)             |
| Clips4Sale                         | HTTP (JSON)             |
| LoyalFans                          | HTTP (JSON)             |
| APClips                            | HTTP                    |
| goddesssnow.com                    | HTTP (HTML)             |
| brookelynnebriar.com (ModelCentro) | HTTP (JSON API)         |

## Development

```bash
uv run ruff check .
uv run ruff format .
```
