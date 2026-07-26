# Design: from email firehose to a consolidated board

Date: 2026-07-26
Status: proposed — implemented across five stacked branches awaiting review

## Problem

The tool works. The delivery doesn't. Six days of production data:

| kind | alerts | share |
|---|---:|---:|
| `catalog_new` | 4,186 | 70% |
| `board_restock` | 1,636 | 27% |
| `wake_restock` | 135 | 2% |
| `stock_new` | 35 | <1% |
| `stock_drawdown` | 2 | <1% |
| **total** | **5,994** | |

Two days accounted for 5,959 of those: 4,321 on 2026-07-22 and 1,638 on
2026-07-24. That is not a notification stream, it is a denial-of-service on
one's own inbox, and it is why nothing in it gets acted on.

Four distinct causes, each fixable:

### 1. No relevance filter on the board leg

`cmd_poll_boards` derives search terms from the Allocation/Limited watchlist
(first two words of each brand), fans them out to each board's search API —
and then never re-checks the results against the watchlist. Whatever the
board's fuzzy search returns becomes alert-eligible.

Greensboro currently holds 285 distinct codes in `board_latest`. Exactly 28
of them appear on the state's allocated list. The other 257 are collateral:
Crystal Head Vodka Camo, High West Double Rye .375L, and so on. Durham: 3 of
46. The signal-to-noise ratio at the source is roughly 1:10.

### 2. Per-store fan-out

`apply_board_snapshot` emits one event per `(board, plu, store)`. Maker's Mark
Cask Strength Military Edition is presently in stock at 13 of 16 Greensboro
stores. One truckload delivery to a county therefore produces 13 emails about
one product — and the 6-hour cooldown key includes the store, so the cooldown
cannot collapse them.

### 3. Baseline seeding storms

The first `poll-catalog` emits one `catalog_new` per pre-existing row: 4,186
of them. Same shape on each new board adapter's first `poll-boards`. This is
known and documented as "expected once," but it recurs every time a source is
added, and it trains you to ignore the label.

### 4. Write amplification into a git-committed database

`apply_stock_snapshot` writes all ~3,193 warehouse rows on every poll, whether
or not anything changed. At a 20-minute cadence that is 72 full rewrites a day:

```
warehouse_snapshot   248,698 rows   48 MB of a 53 MB database   (6 days)
```

`poll.yml` then commits that database to `main` after every run. `.git` is
166 MB (43 MB packed) after five days and grows ~8 MB/day before compression.
This is unrelated to the notification problem on its face, but it is the thing
that makes any always-current shared surface impossible, so it gets fixed
first.

## What we are actually building

The reframe matters more than any single fix. Boards refresh their own
inventory roughly twice a day; we poll them three times a day. A
`board_restock` email is therefore never a race signal — by the time it lands,
the bottle has been on the shelf for anywhere between a minute and eight
hours. Pretending otherwise is what justified the push-everything design.

So: **stop pushing every atom of change, and maintain a good current picture
instead.** Three surfaces over one data model:

- **The board (pull, primary).** A page that is always current to the last
  poll. Anyone with the link can open it — no account, no install. Each
  person picks their own products and stores; that selection lives in their
  browser. This is where "on demand" lives: you look when you want to know.
- **The daily report (push, once a day).** One email per subscriber, scoped
  to their picks, aggregated by product.
- **Urgent (push, rare).** Only for exact watchlist matches, one email per
  product with every store listed, with a hard daily cap.

Non-goals, deliberately: no accounts, no login, no server-side user records,
no database of neighbors, no real-time promise.

## Architecture

### Watchlists are client-side

The tempting move is a `watchlist` table plus a `ncbourbon watch add/rm/ls`
CLI plus per-user rows. We are not doing that. The site filters in the
browser, so the server never needs to know who wants what.

What the server does need is the **universe** of interesting products —
`allocated_list` ∪ (`stock_latest` where listing_type ∈ Allocation/Limited).
That is derived from the DB and is the same for everyone.

Email subscribers are the one case that genuinely needs server-side
preferences, because an inbox can't filter itself. Three neighbors is three
TOML blocks:

```toml
[[subscribers]]
name = "Colin"
email = "colin@prologuegames.com"
boards = ["durham", "wake", "greensboro"]
patterns = ["Weller", "Blanton", "E.H. Taylor"]
urgent = true
```

No table, no CLI, no accounts. If this ever needs to be self-serve, that is
the moment to add a backend — not before.

### One report object, three renderers

New module `ncbourbon/report.py`:

```python
build_report(conn, cfg, since=None) -> Report   # pure: DB in, dataclass out
render_text(report, subscriber=None) -> str     # email body
render_json(report) -> dict                     # site data
```

`Report` carries:

- `generated_at`, plus per-source freshness and health, so staleness is
  visible rather than implied
- `warehouse` — the radar: allocated/limited codes with current cases and a
  short-window delta
- `shelf` — per `(board, store, code)` on-hand for watchlist codes only, with
  product name, price, and store display name
- `changes` — what moved since the previous report: newly on a shelf, gone
  from a shelf, newly in the catalog

Formatting is separated from computation so the same object feeds an email, a
JSON file, and a terminal without three divergent queries. `build_report` does
no I/O beyond the DB and no network, which makes it directly testable against
a fixture database.

### The site

`ncbourbon render-site --out site/` writes `data.json` next to a static
single-page app. No framework, no build step, no CDN — the repo is public and
the page should keep working when a CDN doesn't. The page:

- lists products currently on a shelf, grouped by product, with the stores
  and quantities under each
- lets you flip to a store-first view (what's at the Hillsborough-adjacent
  stores right now)
- has a star toggle per product; starred products persist in `localStorage`
  and can be exported as a URL fragment, so a neighbor can be handed a
  pre-filled link
- shows "as of <timestamp>" prominently, per source, and greys out anything
  older than its expected refresh interval

Hosting: the repo is public, so GitHub Pages on the free tier works with no
new accounts or spend. The Actions job renders and deploys after each board
poll.

## Branch series

Stacked — each PR bases on the previous one. Review order is merge order.

1. **`fix/write-amplification`** — insert a `warehouse_snapshot` row only when
   `total_available` actually changed for that code (or the report_date is
   new). Add a retention prune and `VACUUM`. Stops the repo bleeding and makes
   the DB small enough to serve from.
2. **`fix/alert-noise`** — restrict board events to watchlist codes; aggregate
   to one event per `(board, product)` carrying every store; suppress
   first-run baseline storms; cap daily volume.
3. **`feat/report`** — `report.py`, `ncbourbon report`, digest rewritten on
   top of it.
4. **`feat/site`** — `ncbourbon render-site` and the static page.
5. **`feat/publish`** — Pages deploy, `[[subscribers]]`, schedule retune.

Branches 1 and 2 are independently valuable: merging only those cuts email
volume by roughly two orders of magnitude and stops the database growth, even
if the site is never shipped.

## Risks and open questions

- **Freshness is a promise we can't fully keep.** Boards refresh ~2×/day. The
  site will be honest about this, but someone driving to a store on a
  six-hour-old reading will occasionally find an empty shelf. Mitigation is
  disclosure, not engineering.
- **Sharing a scraped view publicly is a step up from a personal tool.** The
  underlying data is public records from government sites, and the existing
  README already reasons through the politeness question. Adding readers does
  not add polling load — one poll serves everyone. Still, the page should
  carry `noindex`, link back to each source, and not present itself as
  official. Worth a conscious yes before branch 5 merges.
- **History rewrite is not in scope.** Branch 1 stops future growth; the 166 MB
  already in `.git` stays unless you decide to rewrite history, which is
  destructive and needs your call.
- **`actions/deploy-pages` vs. a force-pushed `gh-pages` branch.** The former
  is cleaner and adds no commits; it requires Pages to be set to "GitHub
  Actions" as its source, which is a one-time setting only you can change.
  Branch 5 assumes it and documents the fallback.
