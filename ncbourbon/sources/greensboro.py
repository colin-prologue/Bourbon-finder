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

Store names are NOT in the payload — only numeric location `internalid`s — so
stores are keyed by a stable id-based label for now (see STORE_NAMES / follow-up
to enrich with real addresses). We reuse abcgo.BoardStoreStock (board=
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

# Numeric SuiteCommerce location internalid -> human store label. Empty until the
# id->address map is captured; _store_label falls back to a stable id-based key
# so restock diffing works meanwhile. (Populating this later re-keys stores once.)
STORE_NAMES: dict[int, str] = {}


def _store_label(internalid) -> str:
    try:
        sid = int(internalid)
    except (TypeError, ValueError):
        return f"Greensboro store {internalid}"
    return STORE_NAMES.get(sid, f"Greensboro store #{sid}")


def _price(item: dict) -> str:
    val = (item.get("onlinecustomerprice_detail") or {}).get("onlinecustomerprice")
    return "" if val is None else str(val)


def search(session, term: str, timeout: int = 60, limit: int = 50) -> list[dict]:
    """Items search for `term`. Returns the raw item dicts (each already carries
    per-store on-hand under quantityavailableforstorepickup_detail.locations)."""
    url = (
        f"{BASE}{SEARCH_PATH}?q={quote(term)}&limit={limit}&offset=0"
        "&fieldset=details&country=US&language=en&currency=USD"
    )
    resp = fetch(session, "GET", url, timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


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
                    store=_store_label(loc.get("internalid")),
                    qty=qty,
                )
            )
    return out
