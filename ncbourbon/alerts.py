"""Email via SMTP: broken-source warnings, and the daily digest.

Nothing about a bottle is mailed. Product events belong to the board, which is
republished after every poll — mail that restates the page is noise by
construction, and the only fact the page cannot carry is that it stopped being
updated. So the instant path exists for exactly one thing: a source that has
failed repeatedly.


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


def alert(
    conn: sqlite3.Connection,
    cfg: AlertConfig,
    kind: str,
    key: str,
    subject: str,
    body: str,
) -> None:
    """Send an instant alert unless the same (kind, key) fired recently.

    Only broken sources reach here now — product events are not mailed at all
    (see the note above `_health` in cli.py). That removes the daily cap along
    with them: the cap existed to bound product noise and explicitly exempted
    `health`, so with products gone it could never fire. Keeping unreachable
    throttling would have been worse than none, because it read as protection
    that was not there.

    Losing the cap loses nothing real. It was never a policy, and the one time
    it bound it was wrong to: 37 pre-cap rows from an older build sat in its
    rolling 24h window and silently dropped a whole run's worth of genuine
    alerts. A budget that can be poisoned by its own history is not a backstop.
    """
    if recently_alerted(conn, kind, key, cfg.cooldown_hours):
        log.info("suppressed duplicate alert %s/%s", kind, key)
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
