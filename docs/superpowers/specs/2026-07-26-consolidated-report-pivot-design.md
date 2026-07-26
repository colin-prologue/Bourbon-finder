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

Five distinct causes, each fixable. The first four were diagnosed up front; the
fifth was found while implementing and is about what the tool *collects* rather
than what it sends:

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

### 5. A silent blind spot in what gets collected at all

Found while implementing, not in the original diagnosis, and the most
consequential of the five for what the tool is actually for.

A board is only ever asked about products we name. Search terms came from
Allocation/Limited rows in `stock_latest` alone, so of 312 codes in the watch
universe, **129 (41%) were never searched** — 119 on the state's allocated list
but not currently flagged in the warehouse, 10 matched only by `name_patterns`.
Such a product could sit in the watchlist, appear in the report's universe, and
never once be collected.

Worse, the term list was capped with `sorted(terms)[:80]`. The truncation is
*alphabetical*, so it dropped the same 15 brands on every run since the cap was
added:

```
Uncle Nearest · Very Old · Weller (×6) · Widow Jane · Wild Turkey
Willett Wheated · Woodford Reserve · Wyoming Whiskey · Yellowstone Small
```

Weller is Allocation-flagged and had **never once been searched on any board**.
A cap on an alphabetically sorted list is not sampling; it is a permanent blind
spot at the end of the alphabet.

The four causes above make the tool unreadable. This one makes it quietly
incomplete, which is worse: nothing about the output suggests a gap.

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
  person stars the *products* they care about; that selection lives in their
  browser. This is where "on demand" lives: you look when you want to know.
- **The daily report (push, once a day).** One email per subscriber, scoped
  to their brands and their **whole boards**, aggregated by product.
- **Urgent (push, rare).** Only for exact watchlist matches, one email per
  product **per board**, with every affected store in that board listed, and a
  hard daily cap.

Two points of precision, because both were misread on review:

**Scoping is by product and whole board, never by individual store.** There is
no store-level preference anywhere — not in the subscriber record, and not in
the browser either, where starring applies to products. The store-first view on
the site is a way of *arranging* what is shown, not a filter. If someone
eventually wants "only the two Durham stores near me," that needs stable store
identifiers in the subscriber record and does not exist yet.

**Per board, not globally per product.** Aggregating urgent alerts across
boards sounds tidier and is wrong: the alert key is also the cooldown key, so a
single global key would mean Blanton's landing in Durham at 10am silences the
Greensboro alert at 3pm. That is a different county and a different drive —
genuinely new information, not a duplicate. One email per product per board
keeps the cooldowns independent, and the per-store fan-out that actually caused
the noise is still collapsed.

Non-goals, deliberately: no accounts, no login, no server-side user records,
no database of neighbors, no real-time promise.

## Architecture

### Watchlists are client-side

The tempting move is a `watchlist` table plus a `ncbourbon watch add/rm/ls`
CLI plus per-user rows. We are not doing that. The site filters in the
browser, so the server never needs to know who wants what.

What the server does need is the **universe** of interesting products:

    allocated_list
      ∪ (stock_latest where listing_type ∈ Allocation/Limited)
      ∪ (any stored product whose name matches watch.name_patterns)

That third term is easy to forget and matters. `name_patterns` exists precisely
so a Pappy bottle the state happens to classify `Listed` or `Barrel` still
counts, and `diff._watched()` already honours it for warehouse alerts. If the
report and site derive their universe from listing type alone, a
pattern-only product becomes alertable but invisible — the two surfaces
disagree about what is being watched. Patterns match a *name*, and a code's
name is only known per row, so the universe has to be resolved against stored
product names rather than from the code lists alone.

The universe is derived from the DB and is the same for everyone.

Email subscribers are the one case that genuinely needs server-side
preferences, because an inbox can't filter itself. Three neighbours is three
JSON objects — in a **secret**, not in the config file:

```
NCBOURBON_SUBSCRIBERS='[{"name":"Colin","email":"you@example.com",
                         "boards":["durham","wake","greensboro"],
                         "patterns":["Weller","Blanton","E.H. Taylor"]}]'
```

This repository is public and `config.toml` is tracked in it, so a neighbour's
address must not go there — it is theirs, not ours, and the poll workflow reads
the checked-out config directly. A `[[subscribers]]` block in `config.toml` is
supported for a private local checkout only, and the env var wins over it. The
same reasoning applies downstream: nothing may log a subscriber's address,
because Actions logs on a public repo are public and GitHub masks the secret as
a whole rather than the individual addresses inside it.

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
new accounts or spend. The Actions job renders and deploys after **every**
poll, warehouse runs included.

Publishing only after board polls was the first instinct — shelf data only
moves three times a day, so why redeploy 72 times? Because the warehouse
figures are not static between report days: Total Available falls through the
day as boards order, and that drawdown is the one forward-looking signal left
since the shipment feed was retired. A page that showed an eight-hour-old
drawdown while claiming to be the current picture would undercut the whole
premise. Deploying is a few seconds and three deploys an hour sits well under
the Pages rate limit, so the freshness is worth more than the tidier
deployment history.

## Branch series

Stacked — each PR bases on the previous one. Review order is merge order.

1. **`fix/write-amplification`** — re-key `warehouse_snapshot` to
   `(nc_code, report_date)`, since the warehouse report is a daily artifact,
   and collapse existing rows to the last reading of each day: 248,698 rows →
   19,131, 51MB → 5.9MB. Board and Wake history record *transitions* rather
   than states (`prev_qty`), so a sale can be told from an arrival. A Wake
   total sellout clears the per-store rows it leaves behind. Retention prune
   and `VACUUM`.
2. **`fix/alert-noise`** — restrict board events to the watch universe;
   aggregate to one event per `(board, product)` carrying every store; record
   seeding explicitly per scope; derive board search terms from the *whole*
   universe and remove the alphabetical term cap; cap daily volume on
   delivered mail.
3. **`feat/report`** — `report.py`, `ncbourbon report`, digest rewritten on
   top of it. Resolves name-patterns against stored names so the report and
   the alerting agree on what is watched.
4. **`feat/site`** — `ncbourbon render-site` and the static page, with the
   report embedded so it works opened straight off disk.
5. **`feat/publish`** — Pages deploy, subscribers, and the schedule fix.

Branches 1 and 2 are independently valuable: merging only those cuts email
volume by roughly two orders of magnitude, stops the database growth, and
closes the coverage blind spot — even if the site is never shipped.

### Two invariants that emerged during implementation

Neither was in the original design; both are now load-bearing, and both were
found by running the thing rather than reading it.

**History records transitions, not states.** Storing only the new quantity made
`4 → 2` (a sale) indistinguishable from `0 → 4` (an arrival), so one restock
followed by two sales rendered as "3 appeared" — the per-store fan-out this
pivot exists to remove, reappearing one layer up in the report.

**A first sighting is only news if coverage did not change.** A code never seen
on a board is ambiguous: it just landed, or we just started looking. Search
terms are derived from the allocated list, which the state updates on its own,
so coverage shifts without anyone touching the config — and each shift
announced already-shelved bottles as fresh restocks. Both board and Wake polls
now fingerprint their term set: unchanged fingerprint means a first sighting is
a genuine arrival; changed means newly-covered ground, collected but silent.

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
- **`actions/deploy-pages` requires a one-time manual setting.** Pages → Source
  must be set to "GitHub Actions"; no workflow can do it. Branch 5 assumes it
  and says so in both the workflow and the README. Until it is set, the deploy
  job is the only part of the stack that does nothing.
- **Politeness posture changed, deliberately.** Closing the coverage blind spot
  takes board requests from ~720/day to ~1,350/day across three boards at three
  polls a day. The README's politeness section reasons about cadence rather
  than volume, and this is still one request per term per board on public
  government pages at a few polls a day — but it is a real increase and was
  Colin's explicit call, not a default.
- **Durham remains knowingly incomplete.** `sources/durham.py` caps at 60
  matched codes per run, logged as a warning. Widening the terms took its match
  count from 295 to 482, so it now covers roughly 12% rather than 20%. Tracked
  separately; the cap predates this work. Until it is addressed, Durham shelf
  data is thinner than the site implies, and the shortfall is visible only in a
  log line.
