"""Configuration loading for nc-bourbon-finder.

Reads config.toml (path via NCBOURBON_CONFIG env var, default ./config.toml).
SMTP password comes from the NCBOURBON_SMTP_PASSWORD env var, never the file.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _nc_code(code) -> str:
    """'18-152' -> '18152'. Same rule as `sources.catalog.normalize_nc_code`,
    duplicated rather than imported: that module pulls in BeautifulSoup and the
    HTTP layer, and config has no business depending on either."""
    return re.sub(r"[^0-9]", "", str(code or ""))


@dataclass
class AlertConfig:
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    from_addr: str = ""
    to_addrs: list[str] = field(default_factory=list)
    cooldown_hours: float = 6.0  # don't repeat the same alert key within this window

    @property
    def smtp_password(self) -> str:
        return os.environ.get("NCBOURBON_SMTP_PASSWORD", "")

    @property
    def enabled(self) -> bool:
        return bool(self.smtp_host and self.to_addrs)


@dataclass
class WatchConfig:
    listing_types: list[str] = field(default_factory=lambda: ["Allocation", "Limited"])
    name_patterns: list[str] = field(default_factory=list)
    drawdown_alert_fraction: float = 0.5
    # NC codes to drop from the watch universe, whatever the state says about
    # them. Everything else here widens the net — these are the only knob that
    # narrows it.
    #
    # This exists because the universe is DERIVED, not chosen: it is whatever NC
    # currently flags Allocation/Limited plus the state's whole allocated list,
    # which is how tequila and flavoured Crown Royal ended up on a bourbon
    # watchlist. Excluding a code removes it from the page, from the report, and
    # from the search terms sent to the boards — so this also shrinks the number
    # of requests each board poll makes.
    exclude_codes: set[str] = field(default_factory=set)


@dataclass
class WakeConfig:
    enabled: bool = True
    search_terms: list[str] = field(default_factory=list)


@dataclass
class BoardsConfig:
    # ABC/GO board subdomains to poll (e.g. "nh" = New Hanover / Wilmington).
    # Off by default: the only live ABC/GO board (New Hanover) is ~2.5h from
    # Hillsborough — out of practical driving range, so it's back-burnered like
    # Mecklenburg. Opt in by adding a subdomain if an in-range board appears.
    abcgo_boards: list[str] = field(default_factory=list)
    # Search terms POSTed to each board's inventory API. Empty -> derived from
    # the live Allocation/Limited warehouse watchlist at run time.
    search_terms: list[str] = field(default_factory=list)
    # Durham County ABC (its own site durhamabc.com, not on ABC/GO).
    durham: bool = True
    # Greensboro (Guilford) ABC — SuiteCommerce storefront shop.greensboroabc.com.
    greensboro: bool = True


@dataclass
class Subscriber:
    """One person who gets the daily report by email.

    Neighbours mostly want the site — it needs no account and one poll serves
    every reader. This exists only for the case an inbox cannot serve itself:
    a mailed summary, scoped to the boards someone can actually drive to and,
    optionally, the brands they care about.
    """
    name: str = ""
    email: str = ""
    boards: list[str] = field(default_factory=list)    # empty = every active board
    patterns: list[str] = field(default_factory=list)  # empty = everything watched


def load_subscribers(data: dict) -> list[Subscriber]:
    """Subscribers come from NCBOURBON_SUBSCRIBERS (a JSON list) in preference
    to config.toml.

    This repo is public and config.toml is committed to it. A neighbour's email
    address is theirs, not ours, so the documented path puts the list in a
    secret and the config file is only appropriate for a private checkout.
    """
    raw = os.environ.get("NCBOURBON_SUBSCRIBERS", "").strip()
    entries = json.loads(raw) if raw else data.get("subscribers", [])
    return [
        Subscriber(
            name=s.get("name", ""),
            email=s.get("email", ""),
            boards=list(s.get("boards", [])),
            patterns=list(s.get("patterns", [])),
        )
        for s in entries
        if s.get("email")
    ]


@dataclass
class Config:
    db_path: str = "ncbourbon.db"
    user_agent: str = "nc-bourbon-finder/0.1 (personal hobby tool)"
    request_timeout: int = 60
    alerts: AlertConfig = field(default_factory=AlertConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    boards: BoardsConfig = field(default_factory=BoardsConfig)
    subscribers: list[Subscriber] = field(default_factory=list)


def load_config(path: str | None = None) -> Config:
    cfg_path = Path(path or os.environ.get("NCBOURBON_CONFIG", "config.toml"))
    cfg = Config()
    if not cfg_path.exists():
        cfg.subscribers = load_subscribers({})   # the env var still applies
        return cfg
    with open(cfg_path, "rb") as f:
        data = tomllib.load(f)
    cfg.subscribers = load_subscribers(data)
    general = data.get("general", {})
    cfg.db_path = general.get("db_path", cfg.db_path)
    cfg.user_agent = general.get("user_agent", cfg.user_agent)
    cfg.request_timeout = general.get("request_timeout", cfg.request_timeout)
    a = data.get("alerts", {})
    cfg.alerts = AlertConfig(
        smtp_host=a.get("smtp_host", ""),
        smtp_port=a.get("smtp_port", 587),
        smtp_user=a.get("smtp_user", ""),
        from_addr=a.get("from_addr", a.get("smtp_user", "")),
        to_addrs=list(a.get("to_addrs", [])),
        cooldown_hours=a.get("cooldown_hours", 6.0),
    )
    w = data.get("watch", {})
    cfg.watch = WatchConfig(
        listing_types=list(w.get("listing_types", ["Allocation", "Limited"])),
        name_patterns=list(w.get("name_patterns", [])),
        drawdown_alert_fraction=w.get("drawdown_alert_fraction", 0.5),
        # Normalised on the way in. NC codes appear dashed (`18-152`) on pricing
        # pages and dashless (`18152`) in the stock report and Wake PLUs, and a
        # code pasted from the wrong page would otherwise match nothing and
        # silently exclude nothing — a config typo that fails open is worse than
        # one that errors, because nobody goes looking.
        exclude_codes={_nc_code(c) for c in w.get("exclude_codes", []) if _nc_code(c)},
    )
    wk = data.get("wake", {})
    cfg.wake = WakeConfig(
        enabled=wk.get("enabled", True),
        search_terms=list(wk.get("search_terms", [])),
    )
    b = data.get("boards", {})
    cfg.boards = BoardsConfig(
        abcgo_boards=list(b.get("abcgo_boards", [])),
        search_terms=list(b.get("search_terms", [])),
        durham=b.get("durham", True),
        greensboro=b.get("greensboro", True),
    )
    return cfg
