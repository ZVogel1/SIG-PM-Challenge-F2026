from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import Settings, settings

log = logging.getLogger("pmcup.notify")


def email_configured(cfg: Settings | None = None) -> bool:
    cfg = cfg or settings
    return bool(
        cfg.notify_email_to
        and cfg.smtp_host
        and cfg.smtp_user
        and cfg.smtp_password
    )


def send_email(
    subject: str,
    body: str,
    cfg: Settings | None = None,
    *,
    force: bool = False,
) -> bool:
    """Send a plain-text email. Returns True on success. No-ops if not configured."""
    cfg = cfg or settings
    if not force and not (cfg.notify_on_stop or cfg.notify_on_errors):
        return False
    if not email_configured(cfg):
        log.warning("Email notify skipped — set NOTIFY_EMAIL_TO + SMTP_* in .env")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.notify_email_from or cfg.smtp_user
    msg["To"] = cfg.notify_email_to
    msg.set_content(body)

    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            if cfg.smtp_use_tls:
                smtp.starttls()
                smtp.ehlo()
            smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(msg)
        log.info("Notification email sent to %s", cfg.notify_email_to)
        return True
    except Exception:  # noqa: BLE001
        log.exception("Failed to send notification email")
        return False


def notify_alert(subject: str, body: str, cfg: Settings | None = None) -> None:
    cfg = cfg or settings
    if not cfg.notify_on_errors and not cfg.notify_on_stop:
        return
    send_email(subject, body, cfg=cfg, force=True)


def notify_bots_stopped(reason: str, *, cycle: int | None = None, extra: str = "") -> None:
    cfg = settings
    if not cfg.notify_on_stop:
        return
    subject = f"[Predictions Cup] Bots stopped — {reason}"
    body = (
        f"Your Predictions Cup bot runner stopped.\n\n"
        f"Reason: {reason}\n"
        f"Cycle: {cycle}\n"
        f"{extra}\n\n"
        f"Check status with: ./pmcup bots status\n"
        f"Restart with: ./pmcup bots run\n"
        f"Logs: data/bots/runner.log\n"
    )
    send_email(subject, body, cfg=cfg, force=True)


def notify_cycle_failures(fail_streak: int, *, cycle: int, error: str) -> None:
    cfg = settings
    if not cfg.notify_on_errors:
        return
    subject = f"[Predictions Cup] {fail_streak} failed bot cycles"
    body = (
        f"Bot cycles are failing but the runner is staying up.\n\n"
        f"Fail streak: {fail_streak}\n"
        f"Cycle: {cycle}\n"
        f"Last error: {error}\n\n"
        f"Check: data/bots/runner.log and ./pmcup bots status\n"
    )
    send_email(subject, body, cfg=cfg, force=True)


def notify_order_failures(failures: list[str]) -> None:
    cfg = settings
    if not cfg.notify_on_errors or not failures:
        return
    subject = f"[Predictions Cup] {len(failures)} order failure(s)"
    body = "Some bot orders failed this cycle:\n\n" + "\n".join(f"- {f}" for f in failures[:20])
    send_email(subject, body, cfg=cfg, force=True)
