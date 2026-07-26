"""Greensboro (Guilford) ABC store-level inventory — a standalone board adapter.

Greensboro runs its retail storefront on NetSuite SuiteCommerce at
https://shop.greensboroabc.com (public, no login; verified live 2026-07-22).
Unlike ABC/GO's two-step search+details, a single items search returns every
store's on-hand inline, so one GET per term yields all per-store quantities:

  GET /api/items?q=<term>&fieldset=details&country=US&language=en&currency=USD
    -> {"total": N, "items": [ {
         "itemid": "<NC code, dashless>",          # the universal join key
         "displayname": "<name>",
         "onlinecustomerprice_detail": {"onlinecustomerprice": <float>},
         "quantityavailableforstorepickup_detail": {
             "locations": [ {"internalid": <store id>,
                             "qtyavailableforstorepickup": <float>}, ... ]
         }, ... } ] }

Every store (including 0-qty) is always present in `locations`, so this adapter
emits per-store 0 rows for free and is immune to the sellout-suppresses-restock
gap tracked for ABC/GO (issue #2).

Store names are NOT in the items payload — only numeric location `internalid`s.
So each row's board_latest KEY (`store`) is a stable id-based string
(_store_key), while the human name/address (STORE_NAMES, sourced separately from
the storefront's Location.Service.ss) rides along in `store_display` for alert
text only. Keeping the key decoupled from the display means enriching or
correcting STORE_NAMES never re-keys a store's rows — so it can't reset a diff
baseline and mis-fire board_restock. We reuse abcgo.BoardStoreStock (board=
"greensboro") so rows flow through the same apply_board_snapshot / board_restock
path as the other boards.

Politeness: 1 GET per search term (no per-item detail fetch needed). Poll a few
times/day.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from ..http import fetch
from .abcgo import BoardStoreStock

log = logging.getLogger(__name__)

BASE = "https://shop.greensboroabc.com"
SEARCH_PATH = "/api/items"
BOARD = "greensboro"

# Numeric SuiteCommerce location internalid -> human store label (name +
# address). Captured 2026-07-22 from shop.greensboroabc.com/scs/services/
# Location.Service.ss (the store-locator service the storefront's pickup
# selector calls; paginates 20/page, records carry internalid/name/address1/
# city). Guilford ABC has 19 retail "Store N" locations; non-retail records
# (Home Office 23, Law Enforcement 24, Warehouse 43/124) are intentionally
# omitted — if one ever appears in an item payload, _store_display falls back
# to the stable key.
#
# This map is DISPLAY-ONLY: it feeds store_display, never the board_latest
# primary key. The key (see _store_key) stays id-based forever, so editing this
# map — fixing an address, adding a store — never re-keys existing rows and so
# never mis-fires board_restock. Refresh with the Location.Service.ss pull above.
STORE_NAMES: dict[int, str] = {
    25: "Store 1 - Brassfield, 3707-E Battleground Plaza, Greensboro",
    26: "Store 10 - MXB, 115 N. Cedar Street, Greensboro",
    27: "Store 11 - Ring Road, 2731 Ring Road, Greensboro",
    28: "Store 12 - Hickory Branch, 500 Hickory Branch, Greensboro",
    29: "Store 13 - Lawndale, 2417 Lawndale Dr. (Ste. C & D), Greensboro",
    30: "Store 14 - Summerfield, 4548 US Hwy 220 (102 & 103), Greensboro",
    31: "Store 15 - Burlington Road, 3919 Burlington Road, Greensboro",
    32: "Store 16 - Fleming Road, 2309 Fleming Road, Greensboro",
    33: "Store 17 - Bantiff Way, 106 Bantiff Way, Greensboro",
    34: "Store 18 - Redbourne, 1214 Redbourne Drive, Greensboro",
    35: "Store 2 - Rotherwood, 1101 Rotherwood Road, Greensboro",
    36: "Store 3 - E. Market, 3100 E. Market Street, Greensboro",
    37: "Store 4 - Stonesthrow, 3741 Farmington Road, Greensboro",
    38: "Store 5 - W. Market, 4633 W. Market Street, Greensboro",
    39: "Store 6 - Pisgah Church, 307-A Pisgah Church Road, Greensboro",
    40: "Store 7 - Cedar Street, 115 N. Cedar Street, Greensboro",
    41: "Store 8 - W. Wendover, 4411 W. Wendover Ave., Greensboro",
    42: "Store 9 - Randleman Road, 2701 Randleman Road, Greensboro",
    125: "Store 19 - Hicone, 4712 Hicone Road, Greensboro",
}


def _store_key(internalid) -> str:
    """STABLE per-store key for the board_latest primary key. Derived only from
    the immutable location internalid — deliberately NOT from STORE_NAMES — so
    enriching display names never re-keys a store's rows (which would reset its
    diff baseline and fire a spurious board_restock). Matches the id-based label
    the pre-enrichment adapter already wrote, so existing rows keep their key."""
    try:
        sid = int(internalid)
    except (TypeError, ValueError):
        return f"Greensboro store {internalid}"
    return f"Greensboro store #{sid}"


def _store_display(internalid) -> str:
    """Human label for alert text; falls back to the stable key when the id
    isn't in STORE_NAMES (new/unmapped location)."""
    try:
        sid = int(internalid)
    except (TypeError, ValueError):
        return _store_key(internalid)
    return STORE_NAMES.get(sid, _store_key(internalid))


def _price(item: dict) -> str:
    val = (item.get("onlinecustomerprice_detail") or {}).get("onlinecustomerprice")
    return "" if val is None else str(val)


MAX_ITEMS = 1000  # runaway guard: stop paging a single term past this many items


def search(session, term: str, timeout: int = 60, page_size: int = 50) -> list[dict]:
    """Items search for `term`, following offsets until the results cover the
    reported `total` (SuiteCommerce caps each page at `page_size`). Returns the
    raw item dicts (each already carries per-store on-hand under
    quantityavailableforstorepickup_detail.locations). Never silently caps at
    one page — a watched bottle beyond the first page would otherwise be missed."""
    out: list[dict] = []
    offset = 0
    while True:
        url = (
            f"{BASE}{SEARCH_PATH}?q={quote(term)}&limit={page_size}&offset={offset}"
            "&fieldset=details&country=US&language=en&currency=USD"
        )
        resp = fetch(session, "GET", url, timeout=timeout)
        try:
            data = resp.json()
        except ValueError:
            break
        page = data.get("items") if isinstance(data, dict) else None
        if not isinstance(page, list) or not page:
            break
        out.extend(page)
        offset += len(page)
        total = data.get("total") if isinstance(data, dict) else None
        if len(page) < page_size or (isinstance(total, int) and offset >= total):
            break
        if offset >= MAX_ITEMS:
            log.warning("greensboro: term %r exceeded %d items; truncating", term, MAX_ITEMS)
            break
    return out


def fetch_greensboro_stock(session, terms: list[str], timeout: int = 60) -> list[BoardStoreStock]:
    """Search Greensboro for each watchlist term, dedupe matched items by NC code
    across terms, and return flat per-store BoardStoreStock rows (board=
    'greensboro'), including 0-qty rows so diffs detect a later restock."""
    by_code: dict[str, dict] = {}
    for term in terms:
        for item in search(session, term, timeout=timeout):
            code = str(item.get("itemid") or "")
            if code and code not in by_code:
                by_code[code] = item
    return items_to_stock(list(by_code.values()))


def items_to_stock(items: list[dict]) -> list[BoardStoreStock]:
    """Flatten SuiteCommerce items into per-store BoardStoreStock rows, one per
    location (including 0-qty stores so a later restock is detectable)."""
    out: list[BoardStoreStock] = []
    for item in items:
        code = str(item.get("itemid") or "")
        if not code:
            continue
        name = item.get("displayname") or ""
        price = _price(item)
        locs = (item.get("quantityavailableforstorepickup_detail") or {}).get("locations") or []
        for loc in locs:
            try:
                qty = int(loc.get("qtyavailableforstorepickup") or 0)
            except (TypeError, ValueError):
                qty = 0
            out.append(
                BoardStoreStock(
                    board=BOARD,
                    plu=code,
                    name=name,
                    price=price,
                    store=_store_key(loc.get("internalid")),
                    qty=qty,
                    store_display=_store_display(loc.get("internalid")),
                )
            )
    return out
