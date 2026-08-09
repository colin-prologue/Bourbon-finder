# nc-bourbon-finder

Personal tool that watches North Carolina's liquor system for rare bourbon
(Allocation/Limited items) and emails you when something moves — before it
hits shelves when possible.

Built from verified research (July 2026): endpoint schemas were confirmed
live in-browser and cross-checked by adversarial verification. See
`docs/research-report.md` for the full picture and citations.

## How it works

NC is a control state, but everything funnels through one Raleigh warehouse
whose inventory the ABC Commission publishes. The tool polls several public,
unauthenticated sources and diffs snapshots, mirroring how a bottle actually
travels — supplier → Raleigh warehouse → local board → store shelf — in two
stages:

| Loop | Source | Cadence | What it catches |
|---|---|---|---|
| `poll-stocks` | Warehouse Stock Report (`abc2.nc.gov/StoresBoards/Stocks`) | every 15–20 min | **Stage A (radar):** Allocation/Limited items appearing in state stock; drawdowns as boards order |
| `poll-boards` | Per-store board sites: Durham + Greensboro (in range of Hillsborough) | 2–4×/day | **Stage B (confirmation):** which shelf a rare bottle is on right now — emits `board_restock` |
| `poll-wake` | Wake ABC store search (`wakeabc.com`) | 2–4×/day | store-level Wake restocks with addresses and quantities (separate legacy Wake path) |
| `poll-catalog` | Special Items, New Items, allocated-list xlsx | daily | new NC Codes entering the system (~1 month early) |

Stage A is the radar (what rare bottle is in the state and moving); Stage B
is confirmation (which store shelf it's on now). There is **no** advance
per-county signal — the warehouse→board shipment feed (StockShipped) was
retired, so board polling confirms rather than predicts.

All state lives in one SQLite file.

## Alert policy

**Nothing about a bottle is emailed.** The board is the product: it is
republished after every poll and shows current state, so mail that restates the
page is noise by construction. The first six days of production sent 5,994
emails, ~97% of them noise, and successive rounds of filtering kept shrinking
the number without fixing the shape of the thing.

The only instant mail is **a source that has failed `HEALTH_ALERT_THRESHOLD`
(4) consecutive times**, because a board going quiet is the one fact the board
itself cannot show you. A 6-hour cooldown stops a broken source nagging every
poll. There is no daily cap: it existed only to bound product mail, exempted
health, and so became unreachable once products stopped being mailed.

Two rules still govern what the *report* treats as an event, and both matter
for the page:

- **Store everything, surface little.** Every row a source returns is
  persisted — the report wants the whole inventory picture. Only codes in the
  watch universe (Allocation/Limited in the warehouse, plus the state's
  official allocated list, plus `name_patterns` matches) count as events.
  Board search APIs match loosely: a bourbon watchlist pulled back 285
  Greensboro codes, 28 of which were actually allocated.
- **One event per product, not per store.** A county delivery puts a bottle on
  a dozen shelves at once; that is one thing happening, and the stores belong
  in the detail. On the current data this takes 1,636 in-stock store rows down
  to 13 products.
- **Seeding is not news.** A source with no prior state has nothing to diff, so
  it is persisted silently. The first `poll-catalog` used to emit one email per
  pre-existing row — 4,186 of them.

## The report

```bash
python -m ncbourbon report     # print it
python -m ncbourbon digest     # mail the same thing
```

`report.build_report()` is a pure function of the database — no network, no
formatting — and everything that shows a human what's going on renders that one
object, so the email, the terminal, and anything downstream can't drift into
different answers. It covers:

1. **On a shelf now** — watched products with per-store quantities, restricted
   to boards you actually poll. `board_latest` outlives configuration, so a
   back-burnered board's rows linger; listing shelves 2.5 hours away is how a
   report stops getting read.
2. **Changed recently** — appeared and cleared, from the change-only history.
3. **State warehouse** — what's held and which way it's moving. A falling count
   means boards are ordering, the only forward-looking signal left since the
   shipment feed was retired.
4. **Source freshness** — per-source staleness against each loop's cadence, so
   a silently broken scraper shows up as a stale source rather than as an
   inbox that mysteriously went quiet.

## The site

```bash
python -m ncbourbon render-site --out site
```

Writes `data.json` (the report, verbatim) and `index.html` (a page that reads
it). No framework, no build step, no CDN — it is meant to keep working for
someone on a phone in a parking lot, and every dependency is one more thing
that can be down at that moment.

**Watchlists live in the browser, not on the server.** Each person stars the
products they care about; the selection persists in `localStorage`, and "Copy
my list link" encodes it in the URL fragment so you can hand a neighbour a
pre-filled list. That is the entire multi-user story: no accounts, no
server-side user records, nothing to administer, and one poll serves every
reader. If self-serve email subscriptions ever become the ask, *that* is the
moment to add a backend.

The page carries `noindex` and says plainly that it is a personal hobby tool
reproducing public records, not anything official.

### Publishing it

`poll.yml` renders and deploys to GitHub Pages after every poll. Shelf data only
moves three times a day, but warehouse Total Available falls through the day as
boards order, and that drawdown is the only forward-looking signal left since
the shipment feed was retired — a page showing an eight-hour-old one while
claiming to be the current picture would undercut the point of having a page.

**One-time manual step:** repo Settings → Pages → Source → "GitHub Actions".
There is no way to set that from a workflow.

Running the workflow by hand (Actions → poll → Run workflow) refreshes on
demand; the `loops` input picks which loops to run.

### Getting it by email

Optional, and only for someone who wants it in an inbox — the site needs no
subscription and one poll serves every reader. Set the `NCBOURBON_SUBSCRIBERS`
secret to a JSON list:

```json
[{"name": "Colin", "email": "you@example.com",
  "boards": ["durham", "greensboro", "wake"],
  "patterns": ["Weller", "Blanton"]}]
```

The report is built once and narrowed per person, so everyone's copy comes from
the same observation. Empty `boards` means every active board; empty `patterns`
means everything watched. With no subscribers configured it mails the whole
report to `[alerts] to_addrs`, as before.

Put addresses in the secret, not `config.toml` — **this repo is public and
`config.toml` is committed to it**, and a neighbour's address is theirs. For the
same reason nothing logs an address: Actions logs on a public repo are public,
and GitHub masks the secret as a whole rather than the addresses inside it.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate   # Python 3.11+
pip install -r requirements.txt
cp config.example.toml config.toml               # edit: SMTP + watchlist
export NCBOURBON_SMTP_PASSWORD='your-app-password'
python -m ncbourbon poll-stocks                  # first run seeds the DB
python -m ncbourbon status
```

Gmail: use an App Password (Google Account → Security → 2-Step Verification
→ App passwords). First runs seed baselines silently — a source with nothing
to diff against has no news in it — so expect no alerts at all until the
second poll of each source.

### Scheduling on your own box (recommended)

```cron
*/20 * * * *  cd /path/to/nc-bourbon-finder && .venv/bin/python -m ncbourbon poll-stocks
15 8,12,17 * * *  cd /path/to/nc-bourbon-finder && .venv/bin/python -m ncbourbon poll-boards && .venv/bin/python -m ncbourbon poll-wake
5 6 * * *  cd /path/to/nc-bourbon-finder && .venv/bin/python -m ncbourbon poll-catalog && .venv/bin/python -m ncbourbon digest && .venv/bin/python -m ncbourbon prune
```

`prune` trims history to the retention horizon (`--snapshot-days`, default 365;
`--board-days`, default 90) and VACUUMs. Run it daily, never per poll — it
rewrites the whole file.

### Scheduling on GitHub Actions (no server needed)

Push this repo to GitHub, add the `NCBOURBON_SMTP_PASSWORD` secret, and
`.github/workflows/poll.yml` does the rest (it commits the SQLite DB back to
the repo to persist state between runs).

Actions cron is best-effort — minutes of jitter, occasionally much more — so
which loops run is decided by `github.event.schedule`, the cron expression that
actually fired, never by reading the clock. An earlier version required the run
to land in minutes 10–29 of specific hours; drift meant the board and catalog
loops almost never ran while the workflow reported success every 20 minutes.

Because the DB is committed on every poll, **the database's size is the
repo's size**. History is therefore stored at the granularity it is read
at: the warehouse report is a daily artifact, so `warehouse_snapshot` keeps
one row per code per report day (polling it 72×/day stores the same figure
once), and board/wake history records changes rather than re-readings. The
daily `prune` enforces the retention horizon.

## Politeness & legality

These are public government pages presenting public records, and at least
two third-party trackers poll them openly at the same cadence. Still, be a
good citizen — the defaults already are:

- Poll no faster than sources refresh (15 min stocks; ~2×/day Wake).
- One bulk request per cycle (empty search returns the whole report).
- Identifying User-Agent with contact email (set yours in config.toml).
- Exponential backoff; after 4 consecutive failures the tool emails you and
  the health record shows it — it never hammers a struggling server.

## Known quirks (from live testing)

- **StockShipped was retired by NC ABC** (2026-07) — it was the only
  statewide warehouse→board shipment feed, so there is no advance
  "which county gets it" signal anymore; the board leg (`poll-boards`)
  confirms shelf presence instead of predicting it. A `poll-shipments`
  liveness ping survived for a few weeks and was removed in 2026-08: it
  cost a request on every board poll and held a health row at 57
  consecutive failures by design, which is a permanently lit warning light
  on a working system. The parser is in git history; check the feed by hand
  if you ever suspect it is back.
- NC ABC error pages come back **HTTP 200**; parsers detect them by title.
  Board sites can also serve a 403 (WAF) to datacenter IPs. A blocked
  response yields no usable rows, and the ABC/GO sellout re-check only zeros
  a code whose state was *authoritatively* re-fetched (a trusted 200/JSON
  response) — so a transient 403 is never mistaken for a sellout.
- NC Codes appear dashed (`18-650`) in pricing pages and dashless (`18650`)
  in the stock report and Wake PLUs — `normalize_nc_code()` folds them.
- The allocated-list xlsx's landing page shows a stale "Last Updated";
  the tool diffs the file bytes (sha256) instead.
- **Scope is geographic:** only boards within ~1.5h of Hillsborough are
  polled (Durham, Wake, Greensboro). New Hanover (Wilmington) and
  Mecklenburg (Charlotte) are ~2.5h away — out of practical driving range,
  so they're off by default (`abcgo_boards = []`). New Hanover's adapter
  still works; re-enable by adding `"nh"`. Mecklenburg has **no** public
  store-inventory search anyway (its channels are the "Spirited Mailing
  List" and Barrelpalooza events).
- The state has migrated hosts before (abc.nc.gov → abc2.nc.gov). Header
  checksums raise `SchemaDriftError` and the health loop emails you after
  repeated failures instead of failing silently.

## Extending

- **More boards:** add a module in `ncbourbon/sources/` per board site that
  returns `BoardStoreStock` rows through the shared `poll-boards` path.
  Existing adapters span three source shapes to copy from — ABC/GO JSON
  (`abcgo.py`), plain HTML (`durham.py`), and SuiteCommerce (`greensboro.py`).
  Recon each board's site yourself first; see the "HOW TO ADD A BOARD"
  recipe in `HANDOFF.md`. (Note: many small municipal boards have no public
  inventory at all — verify a pollable per-store feed exists before building.)
- **Virginia (v2):** VA ABC posts live per-store inventory; the proven
  design is snapshot-and-diff several times daily (see research report,
  VABourbon section). VA limited items drop via random unannounced same-day
  releases, one bottle/customer/day.
- **TTB COLA early warning:** poll the public COLA registry for new label
  approvals (BTAC/Van Winkle filings precede releases by weeks-to-months).
- **South Carolina (v3):** license state, no central feed — per-retailer
  adapters.

## Tests

```bash
python -m pytest tests/ -v
```

Fixtures reconstruct the live DOM captured 2026-07-21 (tags, classes,
headers, and sample values transcribed from the real pages). If production
breaks while tests pass, the site changed — check health status and the
`SchemaDriftError` message.
