"""Email alerts via SMTP. Instant alerts + daily digest.

Password comes from NCBOURBON_SMTP_PASSWORD (never stored in config/repo).
For Gmail: create an App Password (Google Account -> Security -> 2-Step
Verification -> App passwords) and use smtp.gmail.com:587.
"""
from __future__ import annotations

import logging
import smtplib
import sqlite3
from dataclasses import replace
from email.message import EmailMessage

from .config import AlertConfig
from .db import log_alert, recently_alerted

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
    except smtplib.SMTPRecipientsRefused as exc:
        # This exception carries the rejected addresses. On a public repo the
        # Actions log is public, so record only how many were refused.
        log.error("email send failed: %d recipient(s) refused", len(exc.recipients))
        return False
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


def _subject(report) -> str:
    n = len(report.shelf)
    return (
        f"NC bourbon — {n} on shelves near you"
        if n
        else "NC bourbon — nothing on shelves today"
    )


def send_digest(conn: sqlite3.Connection, cfg) -> None:
    """The daily report, mailed once per subscriber.

    The report is built once and narrowed per person, so everyone's copy is
    derived from the same observation. With no subscribers configured this
    mails the whole thing to `alerts.to_addrs`, which is the single-user case
    and the historical behaviour.
    """
    from .report import build_report, for_subscriber, render_text

    report = build_report(conn, cfg)
    if not cfg.subscribers:
        send_email(cfg.alerts, _subject(report), render_text(report))
        return
    for sub in cfg.subscribers:
        theirs = for_subscriber(report, sub)
        one = replace(cfg.alerts, to_addrs=[sub.email])
        send_email(one, _subject(theirs), render_text(theirs))
        # Never log the address. This runs in Actions on a public repo, so the
        # log is public, and GitHub masks the NCBOURBON_SUBSCRIBERS secret as a
        # whole rather than the individual addresses inside it — logging one
        # would undo the reason for putting them in a secret at all.
        log.info("digest -> subscriber %r (%d products)", sub.name or "unnamed", len(theirs.shelf))
