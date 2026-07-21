"""NC ABC Warehouse Stock Report (the crown jewel).

Endpoint (live-verified 2026-07-21):
    POST https://abc2.nc.gov/StoresBoards/Stocks
    Content-Type: application/x-www-form-urlencoded
    Body: ReportDate=M/D/YYYY&BrandName=<query>

An EMPTY BrandName returns the ENTIRE daily stock report (~3,200 products).
The page states data refreshes every 15 minutes; "Total Available" reflects
orders in process. ReportDate accepts past dates (historical reports).

Response is server-rendered HTML with two tables sharing the class
"table table-bordered table-striped":
  1. detail table  — 9 columns, header includes "Listing Type"
  2. summary table — 3 columns (NC Code, Brand Name, Total Available)
We parse table 1, identified by its "Listing Type" header (robust if the
page reorders tables). Listing Type values observed: Listed, Limited,
Barrel, Allocation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from bs4 import BeautifulSoup

from ..http import fetch

log = logging.getLogger(__name__)

URL = "https://abc2.nc.gov/StoresBoards/Stocks"

EXPECTED_HEADERS = [
    "NC Code",
    "Brand Name",
    "Listing Type",
    "Total Available",
    "Size",
    "Cases Per Pallet",
    "Supplier",
    "Supplier Allotment",
    "Broker Name",
]


@dataclass
class StockRow:
    nc_code: str
    brand_name: str
    listing_type: str
    total_available: int
    size: str
    cases_per_pallet: str
    supplier: str
    supplier_allotment: str
    broker: str


class SchemaDriftError(RuntimeError):
    """Raised when the page no longer matches the verified schema."""


def fetch_stock_report(session, report_date: date | None = None, brand: str = "", timeout: int = 60) -> str:
    d = report_date or date.today()
    body = {"ReportDate": f"{d.month}/{d.day}/{d.year}", "BrandName": brand}
    resp = fetch(session, "POST", URL, data=body, timeout=timeout)
    return resp.text


def parse_stock_report(html: str) -> list[StockRow]:
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.get_text(strip=True) if soup.title else "")
    if "Server Error" in title or "Page Not Found" in title:
        raise SchemaDriftError(f"NC ABC returned an error page: {title!r}")

    detail_table = None
    for table in soup.find_all("table"):
        first_row = table.find("tr")
        if not first_row:
            continue
        headers = [c.get_text(strip=True) for c in first_row.find_all(["th", "td"])]
        if "Listing Type" in headers:
            if headers != EXPECTED_HEADERS:
                raise SchemaDriftError(f"Stock table headers changed: {headers}")
            detail_table = table
            break
    if detail_table is None:
        raise SchemaDriftError("No table with a 'Listing Type' header found")

    rows: list[StockRow] = []
    for tr in detail_table.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) != 9:
            continue
        try:
            total = int(cells[3].replace(",", "") or 0)
        except ValueError:
            total = 0
        rows.append(
            StockRow(
                nc_code=cells[0],
                brand_name=cells[1],
                listing_type=cells[2],
                total_available=total,
                size=cells[4],
                cases_per_pallet=cells[5],
                supplier=cells[6],
                supplier_allotment=cells[7],
                broker=cells[8],
            )
        )
    if not rows:
        raise SchemaDriftError("Stock table parsed to zero rows")
    return rows
