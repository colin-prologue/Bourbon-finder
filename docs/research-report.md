# Finding Rare Bourbon in North Carolina: Data Sources, Scraper Architecture, and Signals Playbook

**Prepared for:** Colin (Prologue Games) — personal rare-bourbon-finding tool, NC first, VA/SC later
**Date:** July 21, 2026
**Method:** Deep-research workflow (103 agents; 21 sources fetched; 97 claims extracted; top 25 adversarially verified by 3 independent verifiers each — 21 confirmed, 4 refuted) merged with live endpoint reconnaissance performed in-browser against NC ABC, Wake ABC, Mecklenburg ABC, and VABourbon on July 21, 2026. Confidence labels reflect that process. Decisions already locked: standalone self-hosted codebase, email alerts, alert scope = Allocation + Limited items.

---

## Executive summary

North Carolina looks like the hardest state to monitor — a control state fragmented across ~170 independent local ABC boards — but it is actually one of the easiest, because everything funnels through one place. Every bottle of liquor sold in NC passes through a single state-owned warehouse in Raleigh (operated under contract by LB&B Associates on a bailment system, per NCGS 18B-204), and the ABC Commission publishes that warehouse's inventory, its shipments to every local board, and its full product/price catalog on public, unauthenticated, server-rendered web pages. One POST request returns the entire ~3,200-product daily warehouse stock report, and every row carries a `Listing Type` flag — `Listed`, `Limited`, `Barrel`, or `Allocation` — that is precisely the rare-bourbon filter your alerting needs. A second statewide endpoint reports warehouse-to-board shipments, which means you can see rare bottles moving toward a specific county **before they hit shelves** without scraping 170 board websites.

The pipeline your tool models is: **supplier → Raleigh warehouse → board shipment → store shelf**, with a consumer-facing last mile that takes one of three forms depending on the board — unannounced shelf stocking, announced same-day drops, or advance-window lotteries. Each stage emits a scrapable signal that precedes the next, and two independent third-party trackers (ncabc.ullberg.us and ncbourboninsider.com) already prove the pipeline is both scrapeable and predictive.

---

## Part 1 — The NC scraping surface

### 1.1 Tier 1: Statewide endpoints (the core of the tool)

All of these were live-verified on July 21, 2026. They are classic ASP.NET MVC pages on `abc2.nc.gov` (the old `abc.nc.gov` paths 302-redirect there): server-rendered HTML, plain form POSTs, **no login, no CSRF tokens, no JavaScript required**. A plain HTTP client (Python `requests` + an HTML parser) is sufficient; no headless browser needed.

| Endpoint | Method / body | Returns | Verified |
|---|---|---|---|
| `https://abc2.nc.gov/StoresBoards/Stocks` | POST `ReportDate=7/20/2026&BrandName=` (form-urlencoded) | **Entire daily warehouse stock report** (~3,183 products) when `BrandName` is empty | high (3–0, + live recon) |
| `https://abc2.nc.gov/StoresBoards/ExportExcel` | POST (same form) | Excel export of the stock report | high (live recon) |
| `https://abc2.nc.gov/Search/StockShipped` | Form w/ two dropdowns (Board, Product) | Warehouse→board shipments: `Number of Bottles Shipped, NC Code, Product Name, Board Name` | high (3–0) |
| `https://abc2.nc.gov/Pricing/PriceList` | POST `NCCode=&BrandName=<term>` | Quarterly price list rows: `NC Code, Supplier, Brand Name, Age, Proof, Size, Retail Price, MXB Price` | high (3–0, + live recon) |
| `https://abc2.nc.gov/Pricing/ExportData` | POST | Excel export of price list | high (live recon) |
| `https://abc2.nc.gov/Pricing/SpecialItems` | GET | Static HTML table (~550+ special items), same schema as price list | high (3–0) |
| `https://abc2.nc.gov/Pricing/ItemReports/2` | GET | "New Items" report — new NC Codes entering the system | medium |
| `https://www.abc.nc.gov/local-abc-boards/public-allocated-and-limited-distribution-list/open` | GET | Raw `.xlsx` — the official "Public Allocated and Limited Distribution List," replaced in place at this stable URL | high (3–0) |
| `https://abc2.nc.gov/Pricing/ViewItemDetails/<id>` | GET | Per-item detail page keyed by NC Code | high (3–0) |
| `https://abc2.nc.gov/Search/ABCStoreLocator`, `/Districts` | GET | All stores / all ~170 boards directory | medium (nav-verified) |

**The Warehouse Stock Report is the crown jewel.** Schema (9 columns, confirmed verbatim): `NC Code | Brand Name | Listing Type | Total Available | Size | Cases Per Pallet | Supplier | Supplier Allotment | Broker Name`. The page states verbatim that "the stock report information is updated every 15 minutes" and that Total Available reflects orders in process — so you can watch allocated stock draw down in near-real time as boards order it. The `ReportDate` field accepts past dates, so historical daily reports are retrievable for backfilling a time series.

Live snapshot from July 20, 2026 recon, by Listing Type: Listed 2,806 (2,560 in stock), Barrel 197 (11 in stock), Limited 119 (15 in stock), Allocation 61 (18 in stock). Example rows: `27090 | Blanton's Single Barrel | Allocation | 13 cases`, `17286 | Old Fitzgerald BIB 10Y Spring 26 Decanter | Allocation | 433`, `19659 | Blanton's Straight From The Barrel | Limited | 0`. Names carry useful suffixes: `(BTB)` = By The Barrel picks, `(NCABC BTB)` = commission barrel picks. Your Allocation+Limited alert scope maps to ~180 items at any time — a very tractable watch set, maintained for you by the state.

**StockShipped is the prediction layer.** A shipment of an Allocation-flagged NC Code to, say, the Durham board is the strongest single precursor of shelf appearance there. One caveat: the board dropdown is JS-populated, so enumerate board IDs once manually (or from `/Districts`) and replay the form POST per board; whether the data carries a date/history dimension is an open question worth probing early.

**The quarterly price list is a one-month-early signal.** The supplier-facing system at `pricing.abc.nc.gov` (login-only, but its public page discloses the schedule) shows price-book quarters of Aug–Oct, Nov–Jan, Feb–Apr, May–Jul, with supplier submission windows three months ahead, and states that pricing data appears on the public site **one month before the effective date**. Diffing the price list and Special Items pages for new NC Codes tells you what's entering NC before the warehouse ever stocks it. A code is assigned at listing, before boards can order — verified examples on Special Items include NC-exclusive barrel picks like `19-634 WhistlePig 15Y NC Barrel Pick #4` ($199.95).

**The allocated-list xlsx is diffable at a stable URL.** The bytes are served directly at `/open`; the attached file (23.5 KB, dated Jan 12, 2026) is replaced in place, and the page's "Last Updated" field is misleading (still says 2023) — diff the file bytes, not the page. Update cadence appears infrequent/irregular; treat it as authoritative reference, not a fast signal. Its internal schema hasn't been parsed yet — do that first, since it may contain per-board allocation quantities.

**Dead end to avoid:** `abc2.nc.gov/Search/Product` is the beer & wine *label-approval registry* (wine/beer class filters only, no spirits, no inventory). Verified live from both directions; don't target it.

### 1.2 Tier 2: Local board inventory (the last mile)

The statewide price list explicitly disclaims availability ("Contact your Local ABC Board for availability of items") — store-shelf quantities exist only at board level, and each board's tooling is different.

**Wake ABC (Raleigh) — fully mapped, ready to scrape.** WordPress with a custom plugin (`wakeabc-inventory` v1.5). POST `productSearch=<query>` (form-urlencoded) to `https://wakeabc.com/search-results` — no nonce required — returns server-rendered product cards: name (with `(BTB)` markers), `PLU: 18650` (the NC Code without its dash), price and size (`151.95 USD | .750L`), then per-store availability with street addresses ("7200 Sandy Fork Rd. Raleigh — 1 in stock") or "All Locations Out of Stock." Their page states inventory refreshes only "a couple times a day," and warns quantities can be pre-claimed by mixed-beverage accounts and that staff may not hold allocated bottles. Poll 2–4×/day; more is wasted.

**Mecklenburg ABC (Charlotte) — no public store-level inventory search found.** Important negative result: the `bourbon_finder` GitHub repo's Mecklenburg selectors (`meckabc.com/Products/Product-Search`, `#search-input`, `.product-location-link`) were **refuted 0–3** — the site has since been rebuilt on the Revize municipal CMS with static product-category pages. Meck's rare-bourbon channels are its **Specialty Products Lottery** (announced via the "Spirited Mailing List" email signup) and **Barrelpalooza** events (recurring; Nov 2025 and Jan 2026 confirmed; lottery-at-event format). Their "ABC To Go" online-ordering platform is the most likely place live stock exists — unmapped, worth a recon pass.

**Other boards (Durham, Greensboro, Cape Fear, Southport, etc.) — map board by board.** Durham ABC runs a "Drop Zone" page (`durhamabc.com/drops`) for same-day announced drops. Southport ABC documents both unannounced shelf stocking and morning-of drop emails. Since the Mecklenburg refutation shows third-party selector lists go stale, verify each board's tooling directly before coding against it. Practical approach: start with the boards nearest you plus the largest metros, and lean on StockShipped for everywhere else.

### 1.3 Prior art (proof this works)

**ncabc.ullberg.us — "NC ABC Inventory Tracker."** Independent tracker that polls the warehouse stock report at the state's own 15-minute cadence (~3,200 products, self-attested), keys product pages to official NC Codes (its `/products/58472` matches `ViewItemDetails` for the same code), and runs a Board Shipments view consuming StockShipped data. Client-rendered SPA. Its existence demonstrates the state tolerates full-catalog polling every 15 minutes.

**ncbourboninsider.com.** Layers predictions on delivery data plus historical board patterns and crowdsourced sightings, scoring board+bottle combos as "Likely in stock / Pre-shelf / May be depleted" (2–1 verified — treat as plausible). Its specific claimed mechanics (32 boards, 8-minute polling, Facebook webhooks) were refuted 1–2; don't copy that description as architecture.

**github.com/aiuso/bourbon_finder.** Open-source Python/Selenium NC tracker (warehouse every 15 min; Mecklenburg daily at 10:01 AM with a 10:15 verification pass; Twilio SMS + Discord alerts). Useful as a reference for cadence and alert plumbing; its DOM selectors are stale (the author himself abandoned static-HTML parsing after site changes) — and per the recon above, Selenium is unnecessary since the state pages accept plain POSTs.

**vabourbon.com — the architecture template.** Virginia tracker that snapshots VA ABC inventory five times daily (7:50, 8:50, 9:50 AM, 11:30 AM, 7:00 PM), diffs snapshots into "Movers & Shakers" (top deliveries/returns, top sales), tracks per-product last-seen dates and delivery days, and runs a Discord with drop channels plus a $2/mo Patreon tier for analytics and morning email digests. Their about page notes items appear on the VA ABC site as each store scans its delivery — rolling inventory through the day. Snapshot-and-diff against a state feed, with email digest and instant alerts, is exactly the shape of your tool.

### 1.4 Recommended architecture (synthesis — medium confidence as a composition; each component high)

A single Python service (or GitHub Actions cron for the slower loops + one small always-on poller for the fast loop) with SQLite and SMTP:

1. **Fast loop — every 15 minutes:** POST the empty-search Stocks form; parse the ~3,200-row table; upsert into SQLite keyed on (NC Code, report date). Diff `Total Available` for rows with Listing Type ∈ {Limited, Allocation} (your alert scope) — new stock appearing, or rapid drawdown, both matter. Alert on change via email.
2. **Delivery loop — a few times daily:** replay StockShipped per board of interest; diff bottles-shipped for watched NC Codes; a nonzero delta to a board = "pre-shelf" alert with the board name.
3. **Catalog loop — daily:** diff PriceList + SpecialItems + ItemReports/2 for new NC Codes (one-month-early entry signal); byte-diff the allocated xlsx.
4. **Shelf loop — 2–4×/day:** Wake ABC POST for your watchlist terms; parse per-store quantities. Add boards as you map them.
5. **Calendar/announcement loop:** watch board lottery/drop pages (Wake `/lottery/`, Durham `/drops`) and subscribe by email where offered (Meck's Spirited Mailing List) around known anchors.
6. **Resilience:** NC ABC has already migrated hosts once (abc.nc.gov → abc2.nc.gov); build schema-drift detection (column-header checksum; alert yourself when a parse fails rather than silently returning empty).

**Politeness and legality.** These are public, unauthenticated government pages presenting public records; nothing found suggests scraping them is prohibited, and multiple third parties do so openly at 15-minute cadence. No adverse ToS was found during verification (and none of the government endpoints sits behind a robots.txt block — the only robots-restricted site encountered in this research was vabourbon.com itself). Still: poll no faster than a source refreshes (15 min for Stocks is the verified floor; ~2×/day for Wake), set a descriptive User-Agent with contact email, back off on errors, and prefer the Excel exports when you want bulk data in one request. This isn't legal advice — just the observed landscape.

---

## Part 2 — How allocated bourbon flows into NC, and the signals that precede drops

### 2.1 The flow

Distilleries ship to the state-owned Raleigh warehouse, where product remains distillery-owned under bailment until local boards order it (bailment charge $2.75/case). The Commission lists items (assigning NC Codes, flagging Limited/Allocation/Barrel), boards place orders against supplier allotments, LB&B trucks deliver to boards, and each board then chooses its consumer channel. Every arrow in that chain is observable: listing (price list/Special Items), warehouse arrival (Stocks report), board delivery (StockShipped), shelf (board inventory tools).

### 2.2 The three consumer channels (verified 3–0 against primary board sources)

Boards mix all three — model channel per board *and per release*, not one channel per board:

1. **Unannounced shelf stocking** — bottles quietly hit shelves (e.g., Southport ABC putting "a few on the shelf"). Only inventory polling catches these.
2. **Timed drop events** — announced same-morning, gone in minutes (Durham ABC's Drop Zone; Southport's morning-of emails). Email/page monitoring catches these; inventory polling usually confirms too late.
3. **Advance-window lotteries** — entries open days-to-weeks ahead. **Wake ABC's December holiday lottery is your hardest calendar anchor:** 2025 entries closed Wednesday Dec 10 at 10:00 AM with winners posted by Dec 17, covering 37 allocated products; December winners PDFs confirm the same cadence in 2024. Mecklenburg runs Specialty Products Lotteries (announced via its Spirited Mailing List) plus Barrelpalooza events in the Nov–Jan window.

### 2.3 Leading-indicator ladder (earliest → latest)

1. **TTB COLA filings (months ahead, federal).** The public COLA Registry (`ttbonline.gov/colasonline/publicSearchColasBasic.do`) shows label approvals — new BTAC/Van Winkle/limited-edition labels appear within days of approval, weeks-to-months before release. Basic search takes product name (with `%` wildcards), class/type, origin, and a required completed-date range; records carry approved/expired/surrendered/revoked status; label images available since 1999. A ready-made Apify actor ("TTB COLA Registry Scraper") queries it live without auth if you'd rather not write that scraper. Treat approvals as probabilistic, not guarantees. *(Extracted but not adversarially verified — the registry itself is a well-known primary source.)*
2. **Release calendars (months ahead).** BTAC and Pappy land in fall; Old Fitzgerald decanters in spring/fall (the warehouse data itself showed `Old Fitzgerald BIB 10Y Spring 26 Decanter` as an Allocation item); Four Roses Limited in fall. Maintain a static seasonal table; refine dates from COLA filings and press.
3. **NC listing (≈1 month ahead).** New NC Code in the quarterly price list / Special Items / New Items report — pricing publishes one month before effective date.
4. **Warehouse arrival (days-to-weeks ahead).** The item shows nonzero `Total Available` in the Stocks report. Drawdown velocity tells you boards are ordering.
5. **Board shipment (days ahead).** StockShipped shows bottles moving to a named board. Strongest local precursor.
6. **Shelf/drop/lottery (day of).** Board inventory tools, drop pages, lottery announcements, and community chatter (r/bourbon, NC bourbon Facebook groups, VABourbon-style Discords for VA) confirm ground truth.

A note on refuted signal claims: the ideas that the price list has a "Boutique Collection" rare-flag section (0–3) and that board Facebook pages/email are *primary* drop channels statewide (1–2) both failed verification. Announcement channels are real but per-board and unproven in general — rely on the state data feeds as your backbone and treat social/email as supplements you validate board by board.

### 2.4 Virginia and South Carolina (for later expansion)

**Virginia — easier than NC; do it second.** VA ABC is fully state-run (~400 state stores, no local boards) with live per-store inventory on abc.virginia.gov — richer than anything NC publishes. Current limited-availability policy (verified July 2026): **random, unannounced same-day drops** — "the timing and store locations will be random to discourage individuals from lining up outside stores" — one bottle per customer per day, in-store only. Your VA module is VABourbon's design: snapshot the site's inventory for whiskey categories several times daily, diff for deliveries. (VABourbon's snapshot times cluster around store-opening hours because items appear as stores scan deliveries.)

**South Carolina — different game entirely.** License state: private retail stores (red-dot stores, Total Wine, grocery-adjacent liquor stores), no state inventory system, no central feed. Finding allocated bourbon in SC is retailer-relationship and store-by-store work; the scrapable surfaces are private retailers' own stock pages (e.g., Total Wine's site) and community trackers. Treat SC as a v3 with per-retailer adapters and heavier reliance on community signals.

---

## Open questions (next probes, in priority order)

1. Parse the allocated-list `.xlsx` — does it contain per-board allocation quantities or distribution methods? (Could be a map of exactly which boards get rare bottles.)
2. Does StockShipped expose a date/history dimension, and can all ~170 boards be enumerated from its dropdown? Determines whether shipments work as a time series or only as snapshots.
3. Map the next tier of board inventory tools (Durham, Greensboro, Cape Fear, Meck's ABC To Go) by direct recon — never trust third-party selector lists (the Mecklenburg refutation).
4. Probe historical `ReportDate` depth on the Stocks report for backfilling a training series.

## Verification appendix

**Refuted during adversarial verification (do not build on these):** Special Items "directs users to boards" framing (1–2); "Boutique Collection" rare-flag sections on the price list (0–3); NC Bourbon Insider's 32-board/8-minute/Facebook-webhook mechanics (1–2); bourbon_finder's Mecklenburg URL and CSS selectors (0–3).

**Access notes from this research session:** the NC government endpoints presented no bot blocks; vabourbon.com's robots.txt disallows crawlers on user paths (site content was reviewed in-browser instead); nc-whiskey.com is a dead domain (DNS fails — its cached articles survive only in search snippets); archive.org was unreachable from the research sandbox (sandbox restriction, not site policy).

**Time-sensitivity:** all live verifications date to July 21, 2026. NC ABC has migrated hosts before; expect URL/DOM drift and monitor for it.

## Sources

State of North Carolina (primary): [Warehouse Stock Status](https://abc2.nc.gov/StoresBoards/Stocks) · [StockShipped](https://abc2.nc.gov/Search/StockShipped) · [Quarterly Price List](https://abc2.nc.gov/Pricing/PriceList) · [Special Items](https://abc2.nc.gov/Pricing/SpecialItems) · [Allocated & Limited Distribution List (.xlsx)](https://www.abc.nc.gov/local-abc-boards/public-allocated-and-limited-distribution-list/open) · [NC ABC Pricing System schedule](https://pricing.abc.nc.gov/) · [Product Search (beer/wine — dead end)](https://abc2.nc.gov/Search/Product)

Local boards (primary): [Wake ABC inventory search](https://wakeabc.com/search-our-inventory/) · [Wake ABC lottery](https://wakeabc.com/lottery/) · [Wake 2025 winners PDF](https://wakeabc.com/wp-content/uploads/2025/12/Winners-Web.pdf) · [Durham ABC drops](https://durhamabc.com/drops) · [Southport ABC allocated process](https://southportabcstore.com/allocated-bourbon-process) · [Mecklenburg ABC Specialty Products Lottery](https://www.meckabc.com/store_operations/specialty_products_lottery.php)

Trackers & prior art: [NC ABC Inventory Tracker](https://ncabc.ullberg.us/) · [NC Bourbon Insider](https://www.ncbourboninsider.com/) · [bourbon_finder (GitHub)](https://github.com/aiuso/bourbon_finder) · [VABourbon](https://vabourbon.com/) · [Bourbon Hacker on tracking stock levels](https://www.bourbonhacker.com/blog/how-to-track-allocated-bourbon-stock-levels)

Signals & comparison states: [TTB Public COLA Registry](https://www.ttbonline.gov/colasonline/publicSearchColasBasic.do) · [TTB COLA Registry Scraper (Apify)](https://apify.com/crawlerbros/ttb-cola-registry-scraper) · [Virginia ABC limited availability policy](https://www.abc.virginia.gov/products/limited-availability) · [2026 release calendar (OnlyDrams)](https://www.onlydrams.app/blog/2026-bourbon-whiskey-release-calendar) · [SC DOR liquor licensing](https://dor.sc.gov/alcohol-beverage-licensing-abl/liquor-licensing)
