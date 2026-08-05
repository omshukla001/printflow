"""Outbound email, over plain SMTP.

Deliberately dependency-free: `smtplib` is in the standard library, so turning
email on is a matter of setting five environment variables rather than adding a
service and a client library.

Email is optional throughout. `is_configured()` is false until MAIL_HOST and
MAIL_FROM are set, and every caller is expected to have a path that still works
when it returns False — for password resets that path is the shop counter.
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage

from flask import current_app

log = logging.getLogger(__name__)


def is_configured():
    """Whether outbound mail can actually be sent."""
    cfg = current_app.config
    return bool(cfg.get('MAIL_HOST') and cfg.get('MAIL_FROM'))


def send(to_address, subject, body):
    """Send one plain-text message. Returns True if the server accepted it.

    Never raises: a mail server that is down must not take a page down with it.
    The caller decides what to do about a False, and for password resets that
    means falling back to a code issued at the counter.
    """
    if not is_configured():
        return False
    if not to_address or '@' not in to_address:
        return False

    cfg = current_app.config
    host = cfg['MAIL_HOST']
    port = int(cfg.get('MAIL_PORT') or 587)
    username = cfg.get('MAIL_USERNAME') or ''
    password = cfg.get('MAIL_PASSWORD') or ''
    use_tls = bool(cfg.get('MAIL_USE_TLS', True))

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = cfg['MAIL_FROM']
    msg['To'] = to_address
    msg.set_content(body)

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15,
                                  context=ssl.create_default_context()) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                if use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                if username:
                    smtp.login(username, password)
                smtp.send_message(msg)
        return True
    except Exception as e:
        # Log the failure, not the message — the body carries a reset code.
        log.warning('Email to %s failed (%s): %s', to_address, subject, e)
        return False
