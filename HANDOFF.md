# HANDOFF — nc-bourbon-finder (for a fresh Claude Code / dev session)

Last updated: 2026-07-22. This file is self-contained: everything a new session
needs to continue is here or in the repo. (Deeper research lives in the claude.ai
"Bourbon" project, but you do **not** need it — the essentials are inlined below.)

## TL;DR — where things stand
- Working tool that watches NC's liquor system for Allocation/Limited bourbon and emails on movement.
- **Current branch: `board-leg-abcgo`** (based on `main`, changes UNCOMMITTED — review with `git diff main`, then commit).
- **15/15 tests pass** on Python 3.11 (`python -m pytest tests/ -q`).
- The big recent change: the state's warehouse→board shipment feed (StockShipped) was **retired by NC ABC**, so the "board leg" was rebuilt as direct per-store polling of individual board sites (new `poll-boards` command).
- **Boards with working store-level adapters:** Wake (own site), New Hanover (ABC/GO), Durham (own site).
- **Next work:** add nearby boards — Orange (priority: user lives there), then Alamance (Burlington/Mebane), Guilford (Greensboro/High Point), Chatham, Granville, Franklin, Person, Johnston.

## Dev environment / workflow (native, on this machine)
- Python 3.11+ required (`config.py` uses stdlib `tomllib`). Recreate the venv locally if needed — the checked-in `.venv` points at a macOS 3.14 framework path and may be stale.
- Deps: `pip install -r requirements.txt` (requests, beautifulsoup4, lxml, openpyxl; pytest for tests).
- Run tests: `python -m pytest tests/ -q`
- Recon is easy from this machine: the NC ABC + board sites are public and your IP is not blocked, so just `curl`/`requests` them directly. (A prior cloud session had to drive a browser because its sandbox egress was firewalled — you don't have that limitation.)
- Run a loop: `python -m ncbourbon poll-boards` (needs `config.toml`; copy from `config.example.toml`, set SMTP via `NCBOURBON_SMTP_PASSWORD`).
- NOTE: `README.md` is slightly stale — it still lists `poll-shipments` as an active pre-shelf signal. It's now a deprecated liveness ping (see below). Update the README when convenient.
- Housekeeping: a `_to_delete/` folder holds stale `.git/index.lock` files left by a cloud session that couldn't delete files; safe to remove. `ncbourbon.db` is a local state DB (gitignored data, not code).

## Architecture (two-stage alerting)
The pipeline is: supplier → Raleigh state warehouse → local board → store shelf.
- **Stage A — `poll-stocks`** (every 15–20 min): the statewide warehouse report. Detects Allocation/Limited items arriving in the warehouse and drawdowns (boards ordering). This is the RADAR + watchlist source. Answers "what rare bottle is in the state, and is it moving."
- **Stage B — `poll-boards`** (2–4×/day): per-store inventory across individual board sites. Answers "which shelf is it on right now." Emits `board_restock` alerts.
- Other loops: `poll-catalog` (daily; new NC codes / allocated xlsx), `poll-wake` (legacy standalone Wake path — still present), `digest`, `status`, `backfill`, `history`.
- `poll-shipments` is DEPRECATED: StockShipped is retired. It's now a cheap liveness ping that records health and warns loudly if the state ever restores the feed. Do not build on it.

### Data model
- `sources/*.py` — one module per source. Board adapters return `abcgo.BoardStoreStock(board, plu, name, price, store, qty)`.
- `diff.py::apply_board_snapshot()` — writes `board_stock` (history) + `board_latest` (dedupe), emits `board_restock` on any (board, plu, store) going 0 → >0. Shared by ALL board adapters.
- Join key everywhere = **NC Code, dashless** (e.g. `20624`). Warehouse "NC Code", Wake "PLU", ABC/GO "Code", Durham `/products/<code>` are all the same number. `catalog.normalize_nc_code()` folds the dashed pricing form ("18-650") to dashless.
- `cli.py::cmd_poll_boards()` derives search terms from the live Allocation/Limited watchlist (`_watchlist_terms`, first 2 words of each brand) unless `[boards] search_terms` is set, then fans out to every ABC/GO board in `[boards] abcgo_boards` plus Durham (`[boards] durham`).

## HOW TO ADD A BOARD (the repeatable recipe)
Each board is ~100–130 lines. Two shapes seen so far; pick whichever the site uses.

1. **Recon** the board site directly (curl/requests). Find: (a) a product search that
   returns items, (b) per-store availability, (c) where the NC code lives. Confirm no login.
2. **Write `sources/<board>.py`** exposing `fetch_<board>_stock(session, terms, timeout) -> list[BoardStoreStock]`
   with `board="<slug>"`. Skip items with 0 total on-hand before fetching detail; include per-store
   0-qty rows so a later restock is detectable. Reuse `abcgo.BoardStoreStock`.
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

## Next-boards roadmap (recon → adapter → test → commit, per board)
Priority order (closest to user in Durham/Orange/Wake, and likeliest to get allocation):
1. **Orange** — `orangeabc.com` (user lives here; not yet covered). Check for a public search vs. only a gated eLicensee portal.
2. **Alamance area** — separate municipal boards: Burlington, Graham, **Mebane** (nearly on user's doorstep), Elon.
3. **Guilford** — separate boards: **Greensboro ABC**, High Point ABC (large metro → real allocation).
4. **Chatham** (Pittsboro/Siler City), **Granville** (Butner/Creedmoor/Oxford), **Franklin** (Louisburg), **Person** (Roxboro), **Johnston** (Clayton/Smithfield).
None of these are on ABC/GO (already probed), so each needs its own site check: expect either a Durham-style public search (buildable), a gated eLicensee portal (skip), or nothing public.

## Gotchas / lessons
- Warehouse report for *today* (NC calendar day) can be empty until generated; `stocks.nc_today()` computes the date in America/New_York and `fetch_and_parse()` falls back to the previous day. Keep this — a UTC scheduler otherwise requests a not-yet-existing report.
- First `poll-catalog` run baseline-seeds ~hundreds of "new listing" emails (one per existing Special Items row). Expected once. A Gmail filter (from/to self + subject `[NC]` + body "NC Code" → label + skip inbox) was set up to corral alerts.
- Broker Name on the warehouse report is the supplier's sales rep (a person), NOT a store — irrelevant to routing. It is already absent from the alert email body.

## Immediate next steps
1. Review `git diff main` on `board-leg-abcgo`; commit; add `poll-boards` to the scheduler (`.github/workflows/poll.yml` and/or cron, ~2–4×/day).
2. Recon + build the Orange adapter first, then work down the roadmap.
3. Update the stale `README.md` loop table (poll-shipments → poll-boards).
