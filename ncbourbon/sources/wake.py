"""Wake ABC store-level inventory search.

Endpoint (live-verified 2026-07-21):
    POST https://wakeabc.com/search-results
    Content-Type: application/x-www-form-urlencoded
    Body: productSearch=<query>
    (WordPress + custom plugin "wakeabc-inventory" v1.5; no nonce required.)

DOM structure (captured live):
    div.wake-product
      h4                      -> product name, e.g. "BLANTON'S GOLD SNGL BARR SELECT (BTB)"
      p > small               -> "PLU: 18650"   (PLU == NC Code without dash)
      span.price              -> "151.95 USD"
      span.size               -> ".750L"
      div.inventory-collapse
        ul > li               -> "<street address> ... N in stock"   (in-stock stores)
        p.out-of-stock        -> "All Locations Out of Stock"

Their site states inventory refreshes only "a couple times a day" — poll
2-4x/day, no more. Quantities can be pre-claimed by mixed-beverage accounts.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from ..http import fetch

log = logging.getLogger(__name__)

URL = "https://wakeabc.com/search-results"

IN_STOCK_RE = re.compile(r"(\d+)\s+in stock", re.I)
PLU_RE = re.compile(r"PLU:\s*(\d+)")


@dataclass
class WakeStoreStock:
    plu: str
    name: str
    price: str
    store: str
    qty: int


def fetch_wake_search(session, term: str, timeout: int = 60) -> str:
    resp = fetch(session, "POST", URL, data={"productSearch": term}, timeout=timeout)
    return resp.text


def parse_wake_results(html: str) -> list[WakeStoreStock]:
    soup = BeautifulSoup(html, "lxml")
    out: list[WakeStoreStock] = []
    for card in soup.select("div.wake-product"):
        name_el = card.find("h4")
        name = name_el.get_text(strip=True) if name_el else ""
        plu_m = PLU_RE.search(card.get_text(" ", strip=True))
        plu = plu_m.group(1) if plu_m else ""
        price_el = card.select_one("span.price")
        price = price_el.get_text(strip=True) if price_el else ""
        if not plu:
            continue
        stocked = False
        for li in card.select("div.inventory-collapse li"):
            text = li.get_text(" ", strip=True)
            m = IN_STOCK_RE.search(text)
            if not m:
                continue
            qty = int(m.group(1))
            store = IN_STOCK_RE.sub("", text).strip(" -–|")
            out.append(WakeStoreStock(plu=plu, name=name, price=price, store=store, qty=qty))
            stocked = True
        if not stocked:
            # keep an explicit zero row so diffs can detect restocks
            out.append(WakeStoreStock(plu=plu, name=name, price=price, store="__ALL__", qty=0))
    return out
