"""Email alerts via SMTP. Instant alerts + daily digest.

Password comes from NCBOURBON_SMTP_PASSWORD (never stored in config/repo).
For Gmail: create an App Password (Google Account -> Security -> 2-Step
Verification -> App passwords) and use smtp.gmail.com:587.
"""
from __future__ import annotations

import logging
import smtplib
import sqlite3
from email.message import EmailMessage

from .config import AlertConfig
from .db import log_alert, now_iso, recently_alerted

log = logging.getLogger(__name__)


def send_email(cfg: AlertConfig, subject: str, body: str) -> bool:
    if not cfg.enabled:
        log.info("ALERT (email disabled) %s\n%s", subject, body)
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.from_addr
    msg["To"] = ", ".join(cfg.to_addrs)
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
            s.starttls()
            if cfg.smtp_user:
                s.login(cfg.smtp_user, cfg.smtp_password)
            s.send_message(msg)
        return True
    except Exception:
        log.exception("email send failed")
        return False


def sent_today(conn: sqlite3.Connection) -> int:
    """Instant alerts actually delivered in the last 24h. Health warnings and
    suppression records are excluded — the cap exists to stop product noise,
    and must never gag the alert that says the scraper is broken."""
    return conn.execute(
        "SELECT COUNT(*) FROM alert_log WHERE sent_at > "
        "strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day') "
        "AND kind NOT IN ('health') AND kind NOT LIKE 'capped:%' "
        # Only delivered mail counts. alert() logs a row even when the send
        # fails, so counting every row meant an SMTP outage could burn the whole
        # day's budget without a single email arriving — and then suppress the
        # real alerts for 24h after service came back.
        "AND message LIKE '[sent]%'"
    ).fetchone()[0]


def alert(
    conn: sqlite3.Connection,
    cfg: AlertConfig,
    kind: str,
    key: str,
    subject: str,
    body: str,
) -> None:
    """Send an instant alert unless the same (kind, key) fired recently, or the
    day's budget is spent.

    The cap is a backstop, not a policy: with relevance filtering and
    per-product aggregation the real volume sits in the low single digits a
    day. If it ever binds, something upstream has regressed — so the skipped
    alert is recorded under `capped:<kind>`, which keeps it out of the cooldown
    for the real (kind, key) and lets the report say how much was dropped.
    """
    if recently_alerted(conn, kind, key, cfg.cooldown_hours):
        log.info("suppressed duplicate alert %s/%s", kind, key)
        return
    if kind != "health" and cfg.max_daily_alerts and sent_today(conn) >= cfg.max_daily_alerts:
        log.warning("daily alert cap (%d) reached; skipping %s/%s", cfg.max_daily_alerts, kind, key)
        log_alert(conn, f"capped:{kind}", key, f"[capped] {subject}")
        return
    sent = send_email(cfg, subject, body)
    log_alert(conn, kind, key, f"[{'sent' if sent else 'logged'}] {subject}")


def send_digest(conn: sqlite3.Connection, cfg: AlertConfig) -> None:
    """Daily digest: current Allocation/Limited items with stock, recent alerts."""
    rows = conn.execute(
        "SELECT nc_code, brand_name, listing_type, total_available FROM stock_latest "
        "WHERE listing_type IN ('Allocation','Limited') AND total_available > 0 "
        "ORDER BY listing_type, brand_name"
    ).fetchall()
    recent = conn.execute(
        "SELECT sent_at, message FROM alert_log WHERE sent_at > datetime('now','-1 day') "
        "ORDER BY id DESC LIMIT 40"
    ).fetchall()
    lines = [f"NC bourbon digest — {now_iso()}", ""]
    lines.append(f"Allocation/Limited items with warehouse stock ({len(rows)}):")
    for r in rows:
        lines.append(
            f"  {r['nc_code']}  {r['brand_name']}  [{r['listing_type']}]  {r['total_available']} cases"
        )
    lines.append("")
    lines.append(f"Alerts in the last 24h ({len(recent)}):")
    for r in recent:
        lines.append(f"  {r['sent_at']}  {r['message']}")
    send_email(cfg, "NC bourbon daily digest", "\n".join(lines))
