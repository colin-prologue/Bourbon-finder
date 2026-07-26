# HANDOFF — nc-bourbon-finder (for a fresh Claude Code / dev session)

Last updated: 2026-07-26. This file is self-contained: everything a new session
needs to continue is here or in the repo. (Deeper research lives in the claude.ai
"Bourbon" project, but you do **not** need it — the essentials are inlined below.)

## TL;DR — where things stand
- Working tool that watches NC's liquor system for Allocation/Limited bourbon.
- **In flight (2026-07-26): the pivot from email-push to a pulled board.** Five
  stacked PRs, #10 → #14, designed in #9 (`docs/superpowers/specs/2026-07-26-*`).
  Read the spec first. In merge order: stop snapshot write amplification; fix
  alert noise; add `report.py`; add the static site; publish to Pages + fix the
  Actions schedule. Nothing is merged yet — awaiting Colin's review.
- **The alert firehose is the thing being fixed.** Six days of production sent
  5,994 emails, ~97% noise: no relevance filter on the board leg, one event per
  store instead of per product, and baseline-seeding storms. See #11.
- **`poll-boards` and `poll-catalog` were silently not running.** `poll.yml`
  decided what to run by reading the clock; Actions cron drift meant the window
  was almost never hit. Boards last succeeded 2026-07-24, catalog 2026-07-22,
  while the workflow reported success every 20 minutes. Fixed in #14 by keying
  dispatch on `github.event.schedule`.
- **The DB is committed to git, so its size is the repo's size.** `.git` hit
  166MB in five days. #10 re-keys `warehouse_snapshot` to one row per code per
  day (248,698 → 19,131 rows; 51MB → 5.9MB) and adds `ncbourbon prune`. The
  history already written is NOT rewritten — that needs Colin's call.
- **Status: board leg shipped and live on `main`.** `poll-boards` (PRs #1/#3) has per-store adapters for New Hanover (ABC/GO), Durham, and Greensboro; the ABC/GO sellout→missed-restock bug is fixed (issue #2, PR #4).
- **41/41 tests pass** (`python -m pytest tests/ -q`). Local `.venv` is Python 3.14 (fine — code needs 3.11+ for stdlib `tomllib`); `pip install -r requirements.txt` + `pip install pytest` into it.
- The big change (2026-07): the state's warehouse→board shipment feed (StockShipped) was **retired by NC ABC**, so the "board leg" was rebuilt as direct per-store polling of individual board sites (`poll-boards`).
- **Boards with working store-level adapters:** Wake (own site), Durham (own site), Greensboro (SuiteCommerce) — the **active, in-range set**. New Hanover (ABC/GO) is built and works but is **back-burnered** (see scope below).
- **Scope (added 2026-07-26):** only poll boards within ~1.5h drive of Hillsborough. Active: **Durham, Wake, Greensboro**. Out of range → future expansion, not polled: **New Hanover** (~2.5h, Wilmington) and **Mecklenburg** (~2.5h, Charlotte). `abcgo_boards` is now empty by default; the ABC/GO adapter + New Hanover stay in the code for if we ever want them.
- **Next work:** review and merge #10–#14 in order. In-range new-board coverage
  is exhausted (Orange/Alamance have no pollable feed; see roadmap), so favour
  correctness and the pull surface over more board hunts. Greensboro store-name
  enrichment landed on `main` (f2157bf) and now survives into the report via
  `board_latest.store_display`.
- **Known, unfixed:** `durham` silently caps at 60 of ~295 matched codes per run
  (logged as a warning). Worth deciding whether to page through the rest.

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
- `diff.py::apply_board_snapshot()` — writes `board_stock` (history) + `board_latest` (dedupe), emits `board_restock` on any (board, plu, store) going 0 → >0. Shared by ALL board adapters.
- Join key everywhere = **NC Code, dashless** (e.g. `20624`). Warehouse "NC Code", Wake "PLU", ABC/GO "Code", Durham `/products/<code>` are all the same number. `catalog.normalize_nc_code()` folds the dashed pricing form ("18-650") to dashless.
- `cli.py::cmd_poll_boards()` derives search terms from the live Allocation/Limited watchlist (`_watchlist_terms`, first 2 words of each brand) unless `[boards] search_terms` is set, then fans out to every ABC/GO board in `[boards] abcgo_boards` plus Durham (`[boards] durham`).

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
- **Review the pivot stack #10 → #14, in that order** (design in #9). They are
  stacked, so each PR's base is the previous branch; merging out of order will
  fight you. #10 and #11 are independently valuable — merging just those cuts
  email volume by roughly two orders of magnitude and stops the DB growth, even
  if the site never ships.
- **One manual step before #14 does anything visible:** repo Settings → Pages →
  Source → "GitHub Actions". No workflow can set it.
- Decide on the 166MB already in `.git`. #10 stops future growth but does not
  rewrite history.
- Decide whether the site should be public-linkable. It is `noindex`, carries a
  "not affiliated with NC ABC" note, and adds no polling load — one poll serves
  every reader — but sharing it is still a step beyond a personal tool.
- No clean new-board targets remain (see roadmap). Optional future work: TTB COLA early warning, VA ABC (v2) per the research report, or the High Point Power BI route.
