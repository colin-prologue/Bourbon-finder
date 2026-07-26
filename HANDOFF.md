# HANDOFF — nc-bourbon-finder (for a fresh Claude Code / dev session)

Last updated: 2026-07-26. This file is self-contained: everything a new session
needs to continue is here or in the repo. (Deeper research lives in the claude.ai
"Bourbon" project, but you do **not** need it — the essentials are inlined below.)

## TL;DR — where things stand
- Working tool that watches NC's liquor system for Allocation/Limited bourbon.
- **The pivot from email-push to a pulled board is DONE and live** (2026-07-26).
  PRs #9–#14 all merged. Design: `docs/superpowers/specs/2026-07-26-consolidated-report-pivot-design.md`
  — read it first; it carries the reasoning, not just the what.
- **The site is live:** https://colin-prologue.github.io/Bourbon-finder/
  Rendered by `ncbourbon render-site` and deployed from `poll.yml` after every
  poll. `noindex`, no accounts, watchlists in `localStorage`, shareable via a
  `#w=` fragment. Neighbours just need the link.
- **What the pivot fixed**, all verified against production data:
  - Six days had sent 5,994 emails, ~97% noise. Board events are now filtered to
    the watch universe and aggregated one-per-product-per-board: 1,636 in-stock
    store rows collapse to 13 products.
  - `poll-boards` and `poll-catalog` were **silently not running** — `poll.yml`
    picked loops by reading the clock and cron drift meant the window was almost
    never hit, while every run reported success. Now keyed on
    `github.event.schedule`.
  - `warehouse_snapshot` re-keyed to one row per code per report day:
    248,698 → 19,131 rows, 51MB → 5.9MB. The DB is committed on every poll, so
    **the DB's size is the repo's size**. `ncbourbon prune` runs daily.
  - **Weller had never once been searched on any board.** Terms were capped with
    `sorted(terms)[:80]`, so truncation was alphabetical and permanently dropped
    the same 15 brands. Cap removed; terms now cover the whole watch universe.
- **The 166MB already in `.git` was NOT rewritten.** Future growth is fixed; the
  existing history stays unless Colin decides to rewrite it (destructive).
- **Open: #15** (`fix/durham-coverage`, another session). Durham's 60-code cap
  was applied to an unfiltered alphabetical list and reached **3 of 11**
  watch-universe codes. Needs one rebase onto `main`.
- **66/66 tests pass** (`python -m pytest tests/ -q`). Fixtures build into the
  gitignored `tests/fixtures/_build/`, so a test run no longer dirties the tree. Local `.venv` is Python 3.14 (fine — code needs 3.11+ for stdlib `tomllib`); `pip install -r requirements.txt` + `pip install pytest` into it.
- The big change (2026-07): the state's warehouse→board shipment feed (StockShipped) was **retired by NC ABC**, so the "board leg" was rebuilt as direct per-store polling of individual board sites (`poll-boards`).
- **Boards with working store-level adapters:** Wake (own site), Durham (own site), Greensboro (SuiteCommerce) — the **active, in-range set**. New Hanover (ABC/GO) is built and works but is **back-burnered** (see scope below).
- **Scope (added 2026-07-26):** only poll boards within ~1.5h drive of Hillsborough. Active: **Durham, Wake, Greensboro**. Out of range → future expansion, not polled: **New Hanover** (~2.5h, Wilmington) and **Mecklenburg** (~2.5h, Charlotte). `abcgo_boards` is now empty by default; the ABC/GO adapter + New Hanover stay in the code for if we ever want them.
- **Next work:** in-range new-board coverage is exhausted (Orange/Alamance have
  no pollable feed; see roadmap), so favour correctness and the pull surface over
  more board hunts. Greensboro store-name enrichment is live and survives into
  the report via `board_latest.store_display`.
- **Known, unfixed:** Durham coverage (see #15). Also note the report is only as
  fresh as the last poll — boards refresh ~2x/day, so every surface says so
  explicitly rather than implying live data.

## Dev environment / workflow (native, on this machine)
- Python 3.11+ required (`config.py` uses stdlib `tomllib`). Recreate the venv locally if needed — the checked-in `.venv` points at a macOS 3.14 framework path and may be stale.
- Deps: `pip install -r requirements.txt` (requests, beautifulsoup4, lxml, openpyxl; pytest for tests).
- Run tests: `python -m pytest tests/ -q`
- Recon is easy from this machine: the NC ABC + board sites are public and your IP is not blocked, so just `curl`/`requests` them directly. (A prior cloud session had to drive a browser because its sandbox egress was firewalled — you don't have that limitation.)
- Run a loop: `python -m ncbourbon poll-boards` (needs `config.toml`; copy from `config.example.toml`, set SMTP via `NCBOURBON_SMTP_PASSWORD`).
- `poll-shipments` is a deprecated liveness ping (StockShipped retired); don't build on it. The README loop table reflects the current `poll-boards`-based two-stage model.
- Housekeeping: a `_to_delete/` folder holds stale `.git/index.lock` files left by a cloud session that couldn't delete files; safe to remove. `ncbourbon.db` is a local state DB (gitignored data, not code).

## Architecture (two-stage alerting)
The pipeline is: supplier → Raleigh state warehouse → local board → store shelf.
- **Stage A — `poll-stocks`** (every 15–20 min): the statewide warehouse report. Detects Allocation/Limited items arriving in the warehouse and drawdowns (boards ordering). This is the RADAR + watchlist source. Answers "what rare bottle is in the state, and is it moving."
- **Stage B — `poll-boards`** (2–4×/day): per-store inventory across individual board sites. Answers "which shelf is it on right now." Emits `board_restock` alerts.
- Other loops: `poll-catalog` (daily; new NC codes / allocated xlsx), `poll-wake` (legacy standalone Wake path — still present), `digest`, `status`, `backfill`, `history`.
- `poll-shipments` is DEPRECATED: StockShipped is retired. It's now a cheap liveness ping that records health and warns loudly if the state ever restores the feed. Do not build on it.

### Data model
- `sources/*.py` — one module per source. Board adapters return `abcgo.BoardStoreStock(board, plu, name, price, store, qty)`.
- `diff.py::apply_board_snapshot()` — writes `board_stock` (history) + `board_latest` (current). Shared by ALL board adapters. Four rules govern it, each learned from a real failure:
  - **History records transitions, not states.** Rows carry `prev_qty`, so a reader can tell `0 -> 4` (an arrival) from `4 -> 2` (a sale). Without it the report called both an appearance.
  - **One event per (board, code)**, never per store, with every affected store in the body. Per-store keys also defeated the cooldown, which keys on them.
  - **Only the watch universe alerts.** Every row is still persisted — the report and site want the whole picture — but board search returns ~10x more products than anyone watches.
  - **Seeding and coverage changes are silent.** A scope's first complete observation is a baseline (recorded in the `seeded` table, never inferred from stored rows). A first sighting only counts as an arrival if the search-term fingerprint is unchanged; otherwise we merely started looking.
- Join key everywhere = **NC Code, dashless** (e.g. `20624`). Warehouse "NC Code", Wake "PLU", ABC/GO "Code", Durham `/products/<code>` are all the same number. `catalog.normalize_nc_code()` folds the dashed pricing form ("18-650") to dashless.
- `cli.py::_watchlist_terms()` derives search terms from the WHOLE watch universe — Allocation/Limited brands, the state's allocated list, and the literal runs of each `name_pattern` (they are regexes; board endpoints do substring search, so they cannot be sent verbatim). **There is deliberately no cap**: the old `sorted(terms)[:80]` truncated alphabetically and permanently hid every Weller. ~150 terms, ~1,350 requests/day across three boards.
- A source that reports absence rather than an explicit zero will strand stale stock forever. Durham renders no store table when nothing is carried; Wake emits a single `__ALL__` zero row; ABC/GO omits sold-out items entirely. All three need the sellout handled explicitly — **check this first on any new adapter.** Greensboro reports per-store zeros and is immune.

## HOW TO ADD A BOARD (the repeatable recipe)
Each board is ~100–130 lines. Two shapes seen so far; pick whichever the site uses.

1. **Recon** the board site directly (curl/requests). Find: (a) a product search that
   returns items, (b) per-store availability, (c) where the NC code lives. Confirm no login.
2. **Write `sources/<board>.py`** exposing `fetch_<board>_stock(session, terms, timeout) -> list[BoardStoreStock]`
   with `board="<slug>"`. Reuse `abcgo.BoardStoreStock`. **Emit a per-store row for every store the
   source lists, including 0-qty ones, so a later restock is detectable as 0 -> >0.** Do NOT drop a
   store just because it's empty. If the source only ever exposes *in-stock* state (so a sold-out
   item disappears entirely rather than showing 0 — this is how ABC/GO behaves), you cannot observe
   the zero directly: the poll must re-query previously-in-stock codes and pass an `observed` scope
   to `apply_board_snapshot` so it can persist the sellout. See `abcgo.recheck_absent` + issue #2.
   (Durham and Greensboro list 0-qty stores directly, so they need no re-query.)
3. **Wire into `cmd_poll_boards`** (a few lines, same pattern as the Durham block) + a `[boards]` toggle in `config.py` and `config.example.toml`.
4. **Add tests** to `tests/test_parsers.py`: one pure-parse test against a captured HTML/JSON fixture (include an out-of-stock store → qty 0), and one end-to-end with a fake `session`/`fetch` (see `test_durham_fetch_end_to_end` and `test_abcgo_details_to_stock` as templates).
5. Politeness: 1 request per term + 1 per matched code; dedupe codes; cap detail fetches; descriptive User-Agent. Poll a few times/day.

## Reverse-engineered endpoint contracts (already verified live, 2026-07-22)

### ABC/GO platform — `sources/abcgo.py` (JSON API shape)
Per-board host `https://<board>.abcgo.app`. Public, no login. **Required header `X-Requested-With: XMLHttpRequest`** (else HTTP 403). JSON body (form → 415).
- `POST /api/inventory/search` body `{"filter":"<term>"}` → `[{Code, Brand, Size, Retail, OnHand, Stores, ModifiedOn}]`. `Code` = dashless NC code; `OnHand` = board total; `Stores` = # stores carrying.
- `POST /api/inventory/details` body `{"code":"<nc code>"}` → per-store `[{StoreId, BoardId, Address1, City, State, Zip, OnHand}]`.
- Live public footprint is SMALL: only **New Hanover (`nh`)** as of 2026-07-22 (verified by a 160-name subdomain sweep + wildcard CT cert + web search). Platform advertises "new locations daily" — re-probe `<board>.abcgo.app` periodically and add live ones to `[boards] abcgo_boards`. Wake and Mecklenburg are NOT on abcgo.app.

### Durham — `sources/durham.py` (HTML shape)
Own site `https://durhamabc.com`. Public, no login, plain GETs.
- `GET /search?q=<term>` → HTML; each product is `<a href="/products/<NCCODE>?...">`.
- `GET /products/<NCCODE>` → `<h1>` name, a category badge ("Limited / Allocated", ...), and a store `<table>` (headers Store|Address|Phone|Hours|Availability|Directions); Availability cell = "In Stock (N)" or "Out of Stock". PLU == NC code.

### Wake — `sources/wake.py` (HTML shape, pre-existing)
`POST https://wakeabc.com/search-results` body `productSearch=<term>` → `div.wake-product` cards (name, `PLU: <nccode>`, price, per-store "N in stock"). Refreshes ~2×/day.

## Dead ends — do NOT re-chase
- **StockShipped** (`abc2.nc.gov/Search/StockShipped`) — retired. Returns the app's "no longer available" page on GET and POST; removed from site nav; no relocated equivalent (probed several paths, all 404). It was the only statewide warehouse→board feed; nothing replaced it, so there is NO advance "which county will get it" signal — board polling is confirmation, not prediction.
- **abctogo.com** (ABC/GO ordering hub) — separate from the `abcgo.app` inventory sites; age-gated + CSRF-protected (Laravel 419), and its store set is narrower (returned "no store found" for Wilmington though `nh.abcgo.app` is full). Not a useful enumeration or data path.
- **eLicensee** (`<board>abc.elicensee.com`) — the B2B licensee portal for many boards; login-gated, off-limits.
- No public NC allocation methodology / per-board quantity report exists (warehouse-controlled; effectively size/sales-weighted per 2023 journalism). Don't look for a formula.

## Next-boards roadmap — mostly exhausted (recon settled 2026-07-23)
The nearby/metro boards were reconned this session; clean-API options are largely tapped:
- **Greensboro (Guilford)** — **DONE.** SuiteCommerce API (`shop.greensboroabc.com/api/items`), per-store qty inline; `sources/greensboro.py`.
- **Orange** (`orangeabc.com`) — **SKIP.** WordPress brochure site; rare bourbon goes via an annual lottery (entries close Nov 30), nothing to poll.
- **Alamance / Mebane / Burlington** — **SKIP.** Alamance Municipal ABC Board is a brochure page on `burlingtonnc.gov`; no online inventory search, no store site resolves.
- **High Point (Guilford)** — **POOR FIT.** Shopify (`highpointabc.com`), but allocated bottles aren't in the catalog and per-store stock is locked in an embedded Power BI report — only worth it if someone takes on the brittle Power BI scrape.
- **Mecklenburg (Charlotte)** — out of range (~2.5h) AND gated on `abctogo.com` (age-gate + CSRF); no public per-store feed. Future expansion only.
- **New Hanover (Wilmington)** — built + working (ABC/GO), but **back-burnered**: ~2.5h from Hillsborough, out of practical driving range. `abcgo_boards=[]` disables it; re-add `"nh"` to re-enable.
- **Untouched, likely nothing public:** Chatham, Granville, Franklin, Person, Johnston — small boards; expect brochure-only. Verify a pollable per-store feed exists before building.

Pattern: only metro e-commerce boards whose platform exposes allocated bottles + per-store qty via API are buildable. Of those, the ones **in range** of Hillsborough are Wake, Durham, Greensboro (all active); New Hanover is buildable but out of range.

## Gotchas / lessons
- Warehouse report for *today* (NC calendar day) can be empty until generated; `stocks.nc_today()` computes the date in America/New_York and `fetch_and_parse()` falls back to the previous day. Keep this — a UTC scheduler otherwise requests a not-yet-existing report.
- First run of any source now seeds **silently** — a scope with no prior state has nothing to diff, so treating every row as new was never information. (It used to emit one email per pre-existing row: 4,186 on the first `poll-catalog`.) A Gmail filter (from/to self + subject `[NC]` + body "NC Code" → label + skip inbox) was set up to corral alerts and can probably be retired.
- Broker Name on the warehouse report is the supplier's sales rep (a person), NOT a store — irrelevant to routing. It is already absent from the alert email body.

## Immediate next steps
- **#15 (`fix/durham-coverage`)** is the only open PR. It needs one rebase onto
  `main` — its base moved repeatedly while the stack was draining. Durham
  currently reaches 3 of 11 watch-universe codes, so it is the thinnest of the
  three active boards.
- **Watch the first full digest.** Nothing has yet exercised a complete
  poll → report → email cycle on real data at the daily 09:07 UTC slot. That is
  the pivot's actual claim and it is unverified end to end.
- `catalog` health reads stale (last ok 2026-07-22) because the schedule bug
  kept it from running. The first daily run after the fix should clear it; if it
  does not, that is a real failure rather than the old silent one.
- Decide on the 166MB already in `.git`. Future growth is fixed; rewriting the
  existing history is destructive and needs Colin's call.
- Optional future work: TTB COLA early warning, VA ABC (v2) per the research
  report, or the High Point Power BI route.
