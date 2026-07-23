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
| `poll-boards` | Per-store board sites: New Hanover (ABC/GO), Durham, Greensboro | 2–4×/day | **Stage B (confirmation):** which shelf a rare bottle is on right now — emits `board_restock` |
| `poll-wake` | Wake ABC store search (`wakeabc.com`) | 2–4×/day | store-level Wake restocks with addresses and quantities (separate legacy Wake path) |
| `poll-catalog` | Special Items, New Items, allocated-list xlsx | daily | new NC Codes entering the system (~1 month early) |
| `poll-shipments` | *deprecated* — StockShipped was retired by NC ABC (2026-07) | — | liveness ping only; warns loudly if the state ever restores the feed |

Stage A is the radar (what rare bottle is in the state and moving); Stage B
is confirmation (which store shelf it's on now). There is **no** advance
per-county signal — the warehouse→board shipment feed (StockShipped) was
retired, so board polling confirms rather than predicts.

Alerts: instant email for Allocation/Limited events (deduped with a
cooldown), plus a daily digest of everything in stock. All state lives in
one SQLite file.

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
→ App passwords). First runs seed baselines, so expect a burst of
`stock_new` alerts on the very first `poll-stocks`; that's the current
state of the warehouse, not 60 simultaneous drops.

### Scheduling on your own box (recommended)

```cron
*/20 * * * *  cd /path/to/nc-bourbon-finder && .venv/bin/python -m ncbourbon poll-stocks
15 8,12,17 * * *  cd /path/to/nc-bourbon-finder && .venv/bin/python -m ncbourbon poll-boards && .venv/bin/python -m ncbourbon poll-wake
5 6 * * *  cd /path/to/nc-bourbon-finder && .venv/bin/python -m ncbourbon poll-catalog && .venv/bin/python -m ncbourbon digest
```

### Scheduling on GitHub Actions (no server needed)

Push this repo to GitHub (private is fine), add the
`NCBOURBON_SMTP_PASSWORD` secret, and `.github/workflows/poll.yml` does the
rest (it commits the SQLite DB back to the repo to persist state between
runs). Actions cron is best-effort — minutes of jitter, occasionally more.

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
  confirms shelf presence instead of predicting it. `poll-shipments` is
  kept only as a cheap liveness ping that warns if the feed ever returns.
- NC ABC error pages come back **HTTP 200**; parsers detect them by title.
  Board sites can also serve a 403 (WAF) to datacenter IPs — the fetcher
  treats non-200 as untrusted so a block is never read as a sellout.
- NC Codes appear dashed (`18-650`) in pricing pages and dashless (`18650`)
  in the stock report and Wake PLUs — `normalize_nc_code()` folds them.
- The allocated-list xlsx's landing page shows a stale "Last Updated";
  the tool diffs the file bytes (sha256) instead.
- Mecklenburg ABC has **no** public store-inventory search (old scraping
  guides claiming otherwise are stale). Their channels: the "Spirited
  Mailing List" (sign up by email) and Barrelpalooza events.
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
