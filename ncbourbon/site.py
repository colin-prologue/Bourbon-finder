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
import shutil
import sqlite3
from pathlib import Path

from .config import Config
from .report import build_report, render_json

TEMPLATE = Path(__file__).parent / "templates" / "index.html"


def render_site(conn: sqlite3.Connection, cfg: Config, out_dir: str) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = render_json(build_report(conn, cfg))
    (out / "data.json").write_text(json.dumps(data, indent=1, sort_keys=True))
    shutil.copyfile(TEMPLATE, out / "index.html")
    # Tell Pages not to run the content through Jekyll, which would otherwise
    # swallow files it does not recognise.
    (out / ".nojekyll").write_text("")
    return out
