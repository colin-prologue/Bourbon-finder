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


def alert(
    conn: sqlite3.Connection,
    cfg: AlertConfig,
    kind: str,
    key: str,
    subject: str,
    body: str,
) -> None:
    """Send an instant alert unless the same (kind, key) fired recently."""
    if recently_alerted(conn, kind, key, cfg.cooldown_hours):
        log.info("suppressed duplicate alert %s/%s", kind, key)
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
