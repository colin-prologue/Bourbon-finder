"""Render the report as a static site.

Two files: `data.json` (the report, verbatim) and `index.html` (a page that
reads it). Nothing is generated into the markup, so the page can be edited and
previewed against a checked-in data file without running a poll.

No framework, no build step, no CDN. The repo is public and the page is meant
to keep working for a neighbour on a phone in a parking lot; every dependency
is one more thing that can be down at that moment.

Watchlists deliberately live in the browser, not here. Each person stars the
products they care about, it persists in localStorage, and a share link encodes
the selection in the URL fragment. That is the whole multi-user story — no
accounts, no server-side user records, nothing to administer. If self-serve
email subscriptions ever become the ask, that is the moment to add a backend.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import Config
from .report import build_report, render_json

TEMPLATE = Path(__file__).parent / "templates" / "index.html"


PLACEHOLDER = '<script type="application/json" id="report">null</script>'


def _embed(payload: dict) -> str:
    """Serialise a report for inline embedding in a <script> block.

    `</script>` anywhere in the data would close the block early and drop the
    remainder into the document as markup — and product names come from scraped
    feeds, so that is attacker-reachable, not hypothetical. Escaping `<` as its
    unicode form is valid JSON, parses identically, and cannot terminate the
    element.
    """
    return json.dumps(payload, sort_keys=True).replace("<", "\\u003c")


def render_site(conn: sqlite3.Connection, cfg: Config, out_dir: str) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = render_json(build_report(conn, cfg))
    (out / "data.json").write_text(json.dumps(data, indent=1, sort_keys=True))

    # The report is embedded in the page AND written alongside it. Browsers
    # block fetch() from a file:// origin, so a page that only fetched showed
    # nothing but an error when opened straight from disk — which is exactly
    # what the documented `render-site` command produces. Embedding makes the
    # output work on its own; data.json stays for the deployed page to re-fetch
    # a fresher copy, and for anything else that wants the data.
    html = TEMPLATE.read_text()
    if PLACEHOLDER not in html:
        raise RuntimeError("index.html lost its report placeholder")
    html = html.replace(
        PLACEHOLDER,
        f'<script type="application/json" id="report">{_embed(data)}</script>',
    )
    (out / "index.html").write_text(html)
    # Tell Pages not to run the content through Jekyll, which would otherwise
    # swallow files it does not recognise.
    (out / ".nojekyll").write_text("")
    return out
