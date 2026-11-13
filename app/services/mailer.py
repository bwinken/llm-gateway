"""Outgoing mail through the company's Exchange server (EWS + NTLM).

Same method as AuthCenter (its ``app/mail/exchange.py``): ``exchangelib``
against ``https://<EXCHANGE_SERVER>/EWS/Exchange.asmx`` with a service
account, and an account's mailbox is ``<username>@<COMPANY_EMAIL_DOMAIN>``.
Mail is on only when all five settings are present in ``.env``; nothing is
configurable from the admin UI, so the password never passes through it.

``send`` is blocking (exchangelib is synchronous): call it from a thread,
never from the event loop. It never raises: a failure comes back as a short
summary (exception type and first line, no credentials) and is logged.
``exchangelib`` is imported on first use, so a broken install cannot stop
the gateway from serving.
"""

from __future__ import annotations

import os
import threading

from app.core.logger import logger

# Seconds. exchangelib's default is 120, far too long for an admin's click.
EWS_TIMEOUT = 20

_account = None
_account_key: tuple | None = None
_account_lock = threading.Lock()


def settings() -> dict[str, str]:
    """The mail settings, read from the environment on each call."""
    return {
        "server": os.getenv("EXCHANGE_SERVER", "").strip(),
        "user": os.getenv("EXCHANGE_USER", "").strip(),
        "password": os.getenv("EXCHANGE_PASSWORD", ""),
        "sender": os.getenv("EXCHANGE_EMAIL", "").strip(),
        "domain": os.getenv("COMPANY_EMAIL_DOMAIN", "").strip().lstrip("@").lower(),
        # Where the gateway is reached by browsers, for links in mail.
        "public_url": os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
    }


def is_enabled() -> bool:
    s = settings()
    return all((s["server"], s["user"], s["password"], s["sender"], s["domain"]))


def status() -> dict:
    """What the admin page shows: never the password."""
    s = settings()
    missing = [
        name for name, key in (
            ("EXCHANGE_SERVER", "server"), ("EXCHANGE_USER", "user"), ("EXCHANGE_PASSWORD", "password"),
            ("EXCHANGE_EMAIL", "sender"), ("COMPANY_EMAIL_DOMAIN", "domain"),
        ) if not s[key]
    ]
    return {
        "enabled": not missing, "missing": missing, "server": s["server"], "sender": s["sender"],
        "domain": s["domain"], "public_url": s["public_url"],
    }


def address_for(username: str) -> str:
    """An account's mailbox: ``<username>@<COMPANY_EMAIL_DOMAIN>`` ("" when
    the domain is not configured)."""
    domain = settings()["domain"]
    username = (username or "").strip()
    if not username or not domain:
        return ""
    if "@" in username:
        return username
    return f"{username}@{domain}"


def in_company_domain(address: str) -> bool:
    """Whether ``address`` is a mailbox of the configured domain. Test mail
    is limited to these, so the gateway cannot be used to mail anyone else."""
    domain = settings()["domain"]
    address = (address or "").strip()
    if not domain or address.count("@") != 1 or any(c.isspace() for c in address):
        return False
    local, _, host = address.partition("@")
    return bool(local) and host.lower() == domain


def _get_account():
    """The sender's exchangelib Account, built once per settings. Building
    it does not connect; the first send does."""
    global _account, _account_key
    s = settings()
    key = (s["server"], s["user"], s["password"], s["sender"])
    with _account_lock:
        if _account is None or _account_key != key:
            from exchangelib import DELEGATE, NTLM, Account, Configuration, Credentials
            from exchangelib.protocol import BaseProtocol

            BaseProtocol.TIMEOUT = EWS_TIMEOUT
            config = Configuration(
                service_endpoint=f"https://{s['server']}/EWS/Exchange.asmx",
                credentials=Credentials(s["user"], s["password"]),
                auth_type=NTLM,
            )
            _account = Account(
                primary_smtp_address=s["sender"], config=config, autodiscover=False, access_type=DELEGATE,
            )
            _account_key = key
        return _account


def _send_sync(to: str | list[str], subject: str, html_body: str, images: dict[str, bytes] | None = None) -> None:
    from exchangelib import FileAttachment, HTMLBody, Mailbox, Message

    account = _get_account()
    recipients = [to] if isinstance(to, str) else list(to)
    message = Message(
        account=account,
        author=Mailbox(email_address=account.primary_smtp_address),
        subject=subject,
        body=HTMLBody(html_body),
        to_recipients=[Mailbox(email_address=address) for address in recipients],
    )
    # Pictures the body shows as <img src="cid:NAME">: a mail client renders
    # neither SVG nor a data: URI, so a chart travels as an inline PNG.
    for name, content in (images or {}).items():
        message.attach(FileAttachment(
            name=f"{name}.png", content=content, content_type="image/png", is_inline=True, content_id=name,
        ))
    message.send(save_copy=False)  # a notification: no copy in Sent Items


def send(
    to: str | list[str], subject: str, html_body: str, images: dict[str, bytes] | None = None,
) -> tuple[bool, str]:
    """Send one HTML mail, to one address or to several in the same mail.
    ``images`` are PNGs the body refers to as ``cid:<name>``. Blocking.
    Returns ``(ok, error summary)``."""
    if not is_enabled():
        return False, "Mail is not configured."
    shown = to if isinstance(to, str) else ", ".join(to)
    try:
        if images:
            _send_sync(to, subject, html_body, images)
        else:
            _send_sync(to, subject, html_body)
    except Exception as exc:
        lines = str(exc).strip().splitlines()
        summary = f"{type(exc).__name__}: {lines[0] if lines else ''}"[:200]
        logger.error("Mail failed | to={} subject={!r} error={}", shown, subject, summary)
        return False, summary
    logger.info("Mail sent | to={} subject={!r}", shown, subject)
    return True, ""
