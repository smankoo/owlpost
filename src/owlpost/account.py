"""IMAP / SMTP connection helpers and the Account wrapper."""

from __future__ import annotations

import imaplib
import smtplib
import ssl
from contextlib import contextmanager
from typing import Iterator

from .config import AccountConfig

# IMAP literal limit bumped — APPEND of large messages with attachments needs this.
imaplib._MAXLINE = 10 * 1024 * 1024  # 10 MB


class Account:
    """Wraps an AccountConfig with connection helpers.

    Connections are short-lived and re-opened per operation. This is the most
    reliable model for IMAP — long-lived idle connections drop randomly and
    state (selected folder, FETCH cursor, etc.) is hard to recover.
    """

    def __init__(self, cfg: AccountConfig):
        self.cfg = cfg

    @property
    def name(self) -> str:
        return self.cfg.name

    @contextmanager
    def imap(self) -> Iterator[imaplib.IMAP4_SSL]:
        conn = imaplib.IMAP4_SSL(self.cfg.imap_host, self.cfg.imap_port)
        try:
            conn.login(self.cfg.email, self.cfg.password)
            yield conn
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    @contextmanager
    def smtp(self) -> Iterator[smtplib.SMTP]:
        ctx = ssl.create_default_context()
        if self.cfg.smtp_security == "ssl":
            conn = smtplib.SMTP_SSL(
                self.cfg.smtp_host, self.cfg.smtp_port, context=ctx
            )
        else:
            conn = smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port)
            conn.ehlo()
            conn.starttls(context=ctx)
            conn.ehlo()
        try:
            conn.login(self.cfg.email, self.cfg.password)
            yield conn
        finally:
            try:
                conn.quit()
            except Exception:
                pass
