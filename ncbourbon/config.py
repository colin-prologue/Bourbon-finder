"""Configuration loading for nc-bourbon-finder.

Reads config.toml (path via NCBOURBON_CONFIG env var, default ./config.toml).
SMTP password comes from the NCBOURBON_SMTP_PASSWORD env var, never the file.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


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


@dataclass
class WakeConfig:
    enabled: bool = True
    search_terms: list[str] = field(default_factory=list)


@dataclass
class BoardsConfig:
    watch_boards: list[str] = field(default_factory=list)  # legacy: StockShipped (retired 2026-07)
    # ABC/GO board subdomains to poll for store-level inventory (e.g. "nh").
    abcgo_boards: list[str] = field(default_factory=lambda: ["nh"])
    # Search terms POSTed to each board's inventory API. Empty -> derived from
    # the live Allocation/Limited warehouse watchlist at run time.
    search_terms: list[str] = field(default_factory=list)
    # Durham County ABC (its own site durhamabc.com, not on ABC/GO).
    durham: bool = True


@dataclass
class Config:
    db_path: str = "ncbourbon.db"
    user_agent: str = "nc-bourbon-finder/0.1 (personal hobby tool)"
    request_timeout: int = 60
    alerts: AlertConfig = field(default_factory=AlertConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    boards: BoardsConfig = field(default_factory=BoardsConfig)


def load_config(path: str | None = None) -> Config:
    cfg_path = Path(path or os.environ.get("NCBOURBON_CONFIG", "config.toml"))
    cfg = Config()
    if not cfg_path.exists():
        return cfg
    with open(cfg_path, "rb") as f:
        data = tomllib.load(f)
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
    )
    wk = data.get("wake", {})
    cfg.wake = WakeConfig(
        enabled=wk.get("enabled", True),
        search_terms=list(wk.get("search_terms", [])),
    )
    b = data.get("boards", {})
    cfg.boards = BoardsConfig(
        watch_boards=list(b.get("watch_boards", [])),
        abcgo_boards=list(b.get("abcgo_boards", ["nh"])),
        search_terms=list(b.get("search_terms", [])),
        durham=b.get("durham", True),
    )
    return cfg
