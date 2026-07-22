"""Polite HTTP session: identifying User-Agent, retries with backoff, timeouts.

Politeness rules baked in (see README "Politeness & legality"):
- Never poll a source faster than it refreshes (15 min for the state stock
  report; a couple times a day for Wake ABC).
- Back off exponentially on errors; give up after MAX_RETRIES and let the
  caller record a fetch failure (which trips the drift/health alert if it
  persists) rather than hammering a struggling server.
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 5


def make_session(user_agent: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    return s


def fetch(
    session: requests.Session,
    method: str,
    url: str,
    *,
    data: dict | None = None,
    timeout: int = 60,
) -> requests.Response:
    """GET/POST with retries. Raises requests.RequestException after retries."""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.request(method, url, data=data, timeout=timeout)
            # NC ABC serves error pages with HTTP 200 and title "Server Error";
            # callers detect that via parsers. Here we only retry transport/5xx.
            if resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            if resp.status_code != 200:
                # 403 here usually means a WAF is blocking this IP range
                # (e.g. cloud/datacenter IPs like GitHub Actions runners).
                log.warning(
                    "HTTP %s from %s (%d bytes) — if this is 403 from a cloud "
                    "runner, the site is likely blocking datacenter IPs",
                    resp.status_code, url, len(resp.content),
                )
            return resp
        except requests.RequestException as exc:  # includes HTTPError above
            last_exc = exc
            wait = BACKOFF_BASE_SECONDS * (2**attempt)
            log.warning("fetch %s %s failed (%s); retry in %ss", method, url, exc, wait)
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc
