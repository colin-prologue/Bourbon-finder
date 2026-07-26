"""Durham County ABC store-level inventory — a standalone board adapter.

Durham runs its own site (NOT on ABC/GO). Two steps, both public, no login,
plain GETs returning server-rendered HTML (verified live 2026-07-22):

  GET /search?q=<term>       -> results fragment; each product is an anchor
                                <a href="/products/<NCCODE>?q=..." class="card">
                                carrying a category badge (<span>) and the product
                                name (<h3>). The <NCCODE> in the path is the NC
                                Code (dashless, == PLU).
  GET /products/<NCCODE>     -> product detail page with:
                                  <h1>  = product name
                                  a category badge ("Limited / Allocated", ...)
                                  a <table> (headers: Store | Address | Phone |
                                  Hours | Availability | Directions) with one row
                                  per store; Availability cell = "In Stock (N)"
                                  or "Out of Stock".

We reuse abcgo.BoardStoreStock (board="durham") so Durham rows flow through the
same apply_board_snapshot / board_restock path as ABC/GO boards.

Politeness: 1 GET per search term + 1 GET per code we decide is worth a detail
fetch. The search card already states the category, so the ~57% of matches that
are ordinary shelf stock (Bourbon, Vodka, Minis...) cost no request at all —
see `_tier`. Poll a few times/day.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

from bs4 import BeautifulSoup

from ..http import fetch
from .abcgo import BoardStoreStock

log = logging.getLogger(__name__)

BASE = "https://durhamabc.com"
BOARD = "durham"
PRODUCT_HREF_RE = re.compile(r"/products/(\d+)")
IN_STOCK_RE = re.compile(r"In Stock\s*\((\d+)\)", re.I)
PRICE_RE = re.compile(r"\$\s?([\d,]+\.\d{2})")
# Durham's own badge for the bottles this tool exists to catch. Matches
# "Limited / Allocated", "Allocated", "Limited".
ALLOCATED_BADGE_RE = re.compile(r"allocat|limited", re.I)
# Durham's empty-state text. A search that legitimately matched nothing says
# so; a blocked or broken one just has no cards. See `search_cards`.
NO_RESULTS_MARKER = "No products found"
# Safety ceiling on per-run detail requests. Sized ~1.2x the live
# allocated population (127 of 295 matches, measured 2026-07-26) so it does
# not bind in normal operation — it is a runaway guard, not a coverage
# policy. When it does bind, `Coverage.fetched < relevant` says so in the
# report rather than only in the log.
MAX_DETAIL_FETCHES = 150
REQUEST_DELAY_SECONDS = 0.3  # `http.fetch` has no throttle of its own


@dataclass(frozen=True)
class Card:
    """One product as the search fragment describes it, before any detail GET."""
    code: str
    category: str   # "" when the badge could not be parsed
    name: str


@dataclass
class Coverage:
    """What one run looked at, for the report's source-freshness section.

    `relevant` is what we would have fetched with no ceiling; `fetched` is what
    we did. They differ only when MAX_DETAIL_FETCHES binds, which is the case a
    human needs to see — a silent shortfall reads as full coverage.
    """
    matched: int        # every distinct code the searches returned
    relevant: int       # tier 1 + tier 2: worth a detail fetch
    fetched: int        # actually detail-fetched this run
    classified: bool    # False when no card yielded a category badge
    # Codes whose per-store state this run determined authoritatively — the ones
    # we actually fetched. Feeds apply_board_snapshot's `observed`. Tier 3 must
    # never appear here: we skipped it, which is not the same as knowing it.
    observed: set[str] = field(default_factory=set)


def search_cards(session, term: str, timeout: int = 60) -> list[Card]:
    """GET the search fragment for `term`; return the deduped product cards,
    preserving first-seen order.

    Codes whose anchor we can't parse a badge out of still come back, with an
    empty `category`. `_tier` fetches those rather than dropping them: if
    Durham reskins its cards, the failure must cost extra requests, never
    silent coverage loss.
    """
    url = f"{BASE}/search?q={quote(term)}"
    resp = fetch(session, "GET", url, timeout=timeout)
    # A search we could not read is not a search that found nothing. `fetch`
    # returns non-200 bodies rather than raising, so a WAF 403 on one term
    # parsed as zero cards: its products left the `relevant` denominator
    # entirely, and because other terms still supplied badges the run reported
    # `fetched == relevant` and healthy while silently skipping watched
    # bottles. A believable empty result says so on the page.
    if resp.status_code != 200:
        raise RuntimeError(f"durham search {term!r}: HTTP {resp.status_code}")
    soup = BeautifulSoup(resp.text, "lxml")
    cards: dict[str, Card] = {}
    for a in soup.find_all("a", href=PRODUCT_HREF_RE):
        m = PRODUCT_HREF_RE.search(a["href"])
        if not m or m.group(1) in cards:
            continue
        span = a.find("span")
        h3 = a.find("h3")
        cards[m.group(1)] = Card(
            code=m.group(1),
            category=span.get_text(strip=True) if span else "",
            name=h3.get_text(strip=True) if h3 else "",
        )
    # Anything the raw href scan sees but the anchor walk missed (markup we
    # don't model yet) is kept as an unclassified card, for the same reason.
    for m in PRODUCT_HREF_RE.finditer(resp.text):
        cards.setdefault(m.group(1), Card(code=m.group(1), category="", name=""))
    if not cards and NO_RESULTS_MARKER not in resp.text:
        # Zero cards AND no empty-state marker: this is not a search page.
        # NC ABC sites serve error pages with HTTP 200, so status alone does
        # not settle it.
        raise RuntimeError(f"durham search {term!r}: no results and no empty-state marker")
    return list(cards.values())


def _tier(card: Card, priority_codes, name_patterns) -> int:
    """1 = always fetch, 2 = fetch if there is room, 3 = never fetch.

    Tier 1 is the watch universe plus name-pattern matches. Pattern matching
    has to happen here, on the search card, because `pattern_matched_codes`
    resolves patterns against names already in `board_latest` — a bottle we
    never fetch never gets a name stored, so a pattern-only watch could never
    match it. Reading the name off the card breaks that circle for free.

    Both reads fail open, and they fail open independently. An unparseable
    badge is the obvious case. The quieter one is an unparseable *name* while
    the badge still works: a pattern-only bottle in an ordinary category would
    miss the regex, fall to tier 3, and never be fetched — restoring exactly
    the circle above — while classification still looked healthy, so nothing
    would say coverage had a hole in it. A name we cannot read is a question we
    cannot answer, not a no.
    """
    if card.code in priority_codes:
        return 1
    if card.name and any(re.search(p, card.name, re.I) for p in name_patterns):
        return 1
    if not card.category:
        return 2                                    # unreadable badge: fail open
    if not card.name and name_patterns:
        return 2                                    # unreadable name: fail open
    if ALLOCATED_BADGE_RE.search(card.category):
        return 2
    return 3


def parse_product(html: str) -> dict:
    """Parse a /products/<code> detail page into
    {name, price, category, stores:[(store_address, qty)]}."""
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    name = h1.get_text(strip=True) if h1 else ""
    pm = PRICE_RE.search(soup.get_text(" ", strip=True))
    price = f"${pm.group(1)}" if pm else ""
    category = ""
    for el in soup.find_all(string=True):
        t = el.strip()
        if t in ("Limited / Allocated", "Allocated", "Limited", "Barrel", "Listed"):
            category = t
            break
    stores: list[tuple[str, int]] = []
    for table in soup.find_all("table"):
        header = table.find("tr")
        headers = [c.get_text(strip=True) for c in header.find_all(["th", "td"])] if header else []
        if "Availability" not in headers or "Address" not in headers:
            continue
        addr_i = headers.index("Address")
        avail_i = headers.index("Availability")
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) <= max(addr_i, avail_i):
                continue
            address = re.sub(r"\s+", " ", cells[addr_i]).strip()
            m = IN_STOCK_RE.search(cells[avail_i])
            qty = int(m.group(1)) if m else 0
            if address:
                stores.append((address, qty))
        break
    return {"name": name, "price": price, "category": category, "stores": stores}


def details_stores(session, code: str, timeout: int = 60) -> dict:
    """Parse one product page, and say whether it was a product page at all.

    `fetch` returns non-200 bodies rather than raising (only 5xx does), and NC
    ABC sites serve error pages with HTTP 200 besides — so the caller cannot
    tell a real page from a WAF block by status alone, and both parse to an
    empty `stores` list. That ambiguity is dangerous here: an empty parse is
    also how a genuine "no store carries this" reads.

    A real product page always carries a price and a category badge, stocked or
    not. Durham's 404 renders an <h1> of "Product Not Found" and neither — so
    the <h1> alone is not evidence. If Durham ever drops both from its markup
    we stop marking pages observed, which costs us sellout detection but never
    invents a restock; that is the right direction to fail in.
    """
    resp = fetch(session, "GET", f"{BASE}/products/{code}", timeout=timeout)
    info = parse_product(resp.text)
    info["valid"] = resp.status_code == 200 and bool(info["price"] or info["category"])
    return info


def fetch_durham_stock(
    session,
    terms: list[str],
    *,
    priority_codes=frozenset(),
    name_patterns=(),
    timeout: int = 60,
) -> tuple[list[BoardStoreStock], Coverage]:
    """Search Durham for each watchlist term, then pull per-store detail for the
    matched codes worth one.

    Returns flat per-store BoardStoreStock rows (board='durham'), including
    0-qty rows so diffs detect a later restock, plus the run's `Coverage`.

    `priority_codes` is the watch universe (see `diff.watch_codes`); those are
    fetched first so the ceiling can only ever cost us bottles nobody is
    watching. Ordering matters: before this, the run sliced the first 60 codes
    in term-alphabetical order and reached 3 of the 11 watched bottles Durham
    actually carried.
    """
    cards: list[Card] = []
    seen: set[str] = set()
    for term in terms:
        for card in search_cards(session, term, timeout=timeout):
            if card.code not in seen:
                seen.add(card.code)
                cards.append(card)
    # A single unbadged card fails open on its own (tier 2). Every card losing
    # its badge is the different, louder problem: the selector stopped working.
    classified = any(c.category for c in cards)
    tiered = [(_tier(c, priority_codes, name_patterns), c) for c in cards]
    # Stable sort: tier 1 first, first-seen order preserved within each tier.
    wanted = [c for tier, c in sorted(tiered, key=lambda tc: tc[0]) if tier <= 2]

    out: list[BoardStoreStock] = []
    observed: set[str] = set()
    unreadable = 0
    for i, card in enumerate(wanted[:MAX_DETAIL_FETCHES]):
        if i:
            time.sleep(REQUEST_DELAY_SECONDS)
        info = details_stores(session, card.code, timeout=timeout)
        if not info["valid"]:
            # Not a page we can believe. Saying nothing leaves the last known
            # state standing, which is merely stale; calling it authoritative
            # would zero every store for this code and then fire a burst of
            # false board_restock alerts on the next healthy poll — sending
            # someone driving to a store for a bottle that never moved.
            unreadable += 1
            continue
        # A product no store carries renders with no store table at all (HTTP
        # 200, no <table>, but price and badge intact — 62 of 127 relevant codes
        # on 2026-07-26), so it yields no rows. That is an answer, not a miss:
        # the code is observed, and apply_board_snapshot zeroes its stale
        # board_latest rows. Without this, a Durham bottle that sold out
        # everywhere would stay "in stock" forever and never fire a restock.
        observed.add(card.code)
        for address, qty in info["stores"]:
            out.append(
                BoardStoreStock(
                    board=BOARD,
                    plu=card.code,
                    name=info["name"],
                    price=info["price"],
                    store=address,
                    qty=qty,
                )
            )
    coverage = Coverage(
        matched=len(cards),
        relevant=len(wanted),
        # Pages we could not read do not count as read — otherwise a run that a
        # WAF blocked end to end reports full coverage. Counting only believed
        # pages makes `fetched < relevant` the one signal for both a bound
        # ceiling and a blocked poll, and the report says so either way.
        fetched=len(observed),
        classified=classified,
        observed=observed,
    )
    if unreadable:
        log.warning(
            "durham: %d of %d detail pages were not product pages (WAF block or "
            "error page?) — left untouched rather than zeroed",
            unreadable, min(len(wanted), MAX_DETAIL_FETCHES),
        )
    if not classified and cards:
        log.warning(
            "durham: no category badge parsed from %d cards — the search markup "
            "likely changed; fetching unfiltered", len(cards)
        )
    if coverage.fetched < coverage.relevant:
        log.warning(
            "durham: fetched %d of %d relevant codes this run (ceiling %d)",
            coverage.fetched, coverage.relevant, MAX_DETAIL_FETCHES,
        )
    return out, coverage
