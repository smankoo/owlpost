"""IMAP / SMTP connection helpers and the Account wrapper."""

from __future__ import annotations

import imaplib
import smtplib
import ssl
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress

from .config import AccountConfig

# Constructors, looked up through this module so tests can swap in fakes
# without patching imaplib/smtplib (and their exception classes) globally.
IMAP4 = imaplib.IMAP4
IMAP4_SSL = imaplib.IMAP4_SSL
SMTP = smtplib.SMTP
SMTP_SSL = smtplib.SMTP_SSL

# IMAP literal limit bumped — APPEND of large messages with attachments needs this.
imaplib._MAXLINE = 10 * 1024 * 1024  # 10 MB

# Socket timeout for every IMAP/SMTP operation, in seconds. Without one, a
# server that stops answering hangs the tool call forever.
IMAP_TIMEOUT = 30
SMTP_TIMEOUT = 60

# Logged-in IMAP connections are kept for reuse, but only briefly: providers
# (and NAT boxes, and laptop sleep) silently drop idle sessions, and a dead
# socket would otherwise stall the next call until IMAP_TIMEOUT.
POOL_IDLE_SECONDS = 60
POOL_MAX_IDLE = 2
# How long the NOOP health check on a reused connection may take.
PROBE_TIMEOUT = 5

# Connection-level failures worth one retry on a fresh connection. Protocol
# errors (NO/BAD replies, failed logins) and timeouts are not retried.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    imaplib.IMAP4.abort,
    ConnectionError,
    EOFError,
    ssl.SSLEOFError,
    ssl.SSLZeroReturnError,
)


def _shutdown(conn: imaplib.IMAP4) -> None:
    """Close the socket without talking to the server (it may be dead)."""
    with suppress(Exception):
        conn.shutdown()


def _logout(conn: imaplib.IMAP4) -> None:
    with suppress(Exception):
        conn.logout()


class Account:
    """Wraps an AccountConfig with connection helpers.

    IMAP connections come from a small per-account pool: a connection is
    checked out for exactly one operation, which SELECTs whatever folder it
    needs, so no folder or FETCH state carries over between operations. Idle
    connections expire after POOL_IDLE_SECONDS and are health-checked with
    NOOP before reuse; a connection that saw any error is discarded. This
    avoids a TLS handshake and LOGIN per tool call (iCloud throttles rapid
    reconnects) without the fragility of one long-lived session.
    """

    def __init__(self, cfg: AccountConfig):
        self.cfg = cfg
        self._idle: list[tuple[float, imaplib.IMAP4]] = []
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self.cfg.name

    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if not self.cfg.tls_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # ----- IMAP --------------------------------------------------------------

    def _connect_imap(self) -> imaplib.IMAP4:
        ctx = self._ssl_context()
        if self.cfg.imap_security == "starttls":
            conn: imaplib.IMAP4 = IMAP4(self.cfg.imap_host, self.cfg.imap_port, timeout=IMAP_TIMEOUT)
        else:
            conn = IMAP4_SSL(self.cfg.imap_host, self.cfg.imap_port, ssl_context=ctx, timeout=IMAP_TIMEOUT)
        try:
            if self.cfg.imap_security == "starttls":
                conn.starttls(ssl_context=ctx)
            conn.login(self.cfg.email, self.cfg.resolve_password())
        except BaseException:
            _shutdown(conn)
            raise
        return conn

    def _healthy(self, conn: imaplib.IMAP4) -> bool:
        sock = getattr(conn, "sock", None)
        try:
            if sock is not None:
                sock.settimeout(PROBE_TIMEOUT)
            typ, _ = conn.noop()
            if sock is not None:
                sock.settimeout(IMAP_TIMEOUT)
            return typ == "OK"
        except Exception:
            return False

    def _checkout(self) -> imaplib.IMAP4:
        while True:
            with self._lock:
                if not self._idle:
                    break
                last_used, conn = self._idle.pop()
            if time.monotonic() - last_used > POOL_IDLE_SECONDS:
                _shutdown(conn)
                continue
            if self._healthy(conn):
                return conn
            _shutdown(conn)
        return self._connect_imap()

    def _checkin(self, conn: imaplib.IMAP4) -> None:
        with self._lock:
            if len(self._idle) < POOL_MAX_IDLE:
                self._idle.append((time.monotonic(), conn))
                return
        _logout(conn)

    @contextmanager
    def imap(self) -> Iterator[imaplib.IMAP4]:
        """A logged-in IMAP connection for one operation."""
        conn = self._checkout()
        try:
            yield conn
        except BaseException:
            # The session may be mid-command or mid-literal; never reuse it.
            _shutdown(conn)
            raise
        self._checkin(conn)

    def close(self) -> None:
        """Log out every idle pooled connection."""
        with self._lock:
            idle, self._idle = self._idle, []
        for _, conn in idle:
            _logout(conn)

    # ----- SMTP --------------------------------------------------------------

    @contextmanager
    def smtp(self) -> Iterator[smtplib.SMTP]:
        ctx = self._ssl_context()
        if self.cfg.smtp_security == "ssl":
            conn = SMTP_SSL(self.cfg.smtp_host, self.cfg.smtp_port, context=ctx, timeout=SMTP_TIMEOUT)
        else:
            conn = SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=SMTP_TIMEOUT)
        try:
            if self.cfg.smtp_security != "ssl":
                conn.ehlo()
                conn.starttls(context=ctx)
                conn.ehlo()
            conn.login(self.cfg.email, self.cfg.resolve_password())
            yield conn
        finally:
            with suppress(Exception):
                conn.quit()
