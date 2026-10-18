"""Shared fakes: an in-memory IMAP server and an SMTP recorder.

The fakes speak just enough of imaplib's client API (return shapes included)
for owlpost's code paths. Nothing here touches the network.
"""

from __future__ import annotations

import email
import re
import textwrap
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from pathlib import Path

import pytest

from owlpost import config as config_mod
from owlpost.account import Account
from owlpost.config import AccountConfig

# ----- message helpers -------------------------------------------------------


def make_message(
    *,
    subject: str = "Hello",
    from_: str = "Alice Example <alice@example.com>",
    to: str = "you@example.com",
    cc: str | None = None,
    message_id: str = "<m1@example.com>",
    in_reply_to: str | None = None,
    references: str | None = None,
    date: str = "Mon, 01 Jan 2024 10:00:00 +0000",
    body: str = "Plain body\n",
    html: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> bytes:
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = from_
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, data, mime in attachments or []:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()


# ----- fake IMAP -------------------------------------------------------------


@dataclass
class FakeMessage:
    uid: int
    raw: bytes
    flags: set[str] = field(default_factory=set)
    thrid: str | None = None

    def header(self, name: str) -> str:
        msg = email.message_from_bytes(self.raw, policy=policy.default)
        return str(msg.get(name) or "")


@dataclass
class FakeFolder:
    name: str
    flags: str = "\\HasNoChildren"
    messages: list[FakeMessage] = field(default_factory=list)
    next_uid: int = 1

    def add(self, raw: bytes, flags: set[str] | None = None, thrid: str | None = None) -> int:
        uid = self.next_uid
        self.next_uid += 1
        self.messages.append(FakeMessage(uid, raw, set(flags or ()), thrid))
        return uid

    def get(self, uid: int) -> FakeMessage | None:
        return next((m for m in self.messages if m.uid == uid), None)


def _unquote(name: str) -> str:
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return name


class FakeIMAPServer:
    """Mailbox state shared by every connection opened during a test."""

    def __init__(self, capabilities: tuple = ("IMAP4REV1", "MOVE", "UIDPLUS")):
        self.folders: dict[str, FakeFolder] = {}
        self.capabilities = capabilities
        self.log: list[tuple] = []  # every command, in order
        self.logins: list[tuple[str, str]] = []
        self.connections = 0

    def add_folder(self, name: str, flags: str = "\\HasNoChildren") -> FakeFolder:
        folder = FakeFolder(name, flags)
        self.folders[name] = folder
        return folder

    def connect(self, host, port=None, ssl_context=None, **_kw) -> FakeIMAPConn:
        self.connections += 1
        return FakeIMAPConn(self, host, port)


class FakeIMAPConn:
    def __init__(self, server: FakeIMAPServer, host, port):
        self.server = server
        self.host = host
        self.port = port
        self.selected: FakeFolder | None = None
        self.readonly = False
        # imaplib exposes capabilities as a tuple of str; some servers return bytes.
        self.capabilities = server.capabilities

    def _log(self, *entry):
        self.server.log.append(entry)

    # --- session ---
    def starttls(self, ssl_context=None):
        self._log("STARTTLS")
        return "OK", [b"Begin TLS"]

    def login(self, user, password):
        self.server.logins.append((user, password))
        return "OK", [b"LOGIN completed"]

    def logout(self):
        return "BYE", [b"logging out"]

    # --- mailbox ---
    def list(self):
        lines = []
        for f in self.server.folders.values():
            lines.append(f'({f.flags}) "/" "{f.name}"'.encode())
        return "OK", lines

    def select(self, mailbox, readonly=False):
        name = _unquote(mailbox)
        self._log("SELECT", name, readonly)
        folder = self.server.folders.get(name)
        if folder is None:
            return "NO", [b"Mailbox does not exist"]
        self.selected = folder
        self.readonly = readonly
        return "OK", [str(len(folder.messages)).encode()]

    def append(self, mailbox, flags, date_time, message):
        name = _unquote(mailbox)
        self._log("APPEND", name, flags, message)
        folder = self.server.folders.get(name)
        if folder is None:
            return "NO", [b"no such mailbox"]
        folder.add(message, {flags} if flags else set())
        return "OK", [b"APPEND completed"]

    def expunge(self):
        self._log("EXPUNGE", self.selected.name)
        self.selected.messages = [m for m in self.selected.messages if "\\Deleted" not in m.flags]
        return "OK", [None]

    # --- UID commands ---
    def uid(self, command, *args):
        command = command.lower()
        self._log("UID", command, *args)
        handler = getattr(self, f"_uid_{command}", None)
        if handler is None:
            return "BAD", [f"unsupported UID {command}".encode()]
        return handler(*args)

    def _uid_search(self, charset, *criteria):
        tokens = [_unquote(t) for t in criteria]
        matches = list(self.selected.messages)
        i = 0
        while i < len(tokens):
            tok = tokens[i].upper()
            if tok == "ALL":
                i += 1
            elif tok == "HEADER":
                hname, value = tokens[i + 1], tokens[i + 2]
                matches = [m for m in matches if value.lower() in m.header(hname).lower()]
                i += 3
            elif tok == "X-GM-THRID":
                matches = [m for m in matches if m.thrid == tokens[i + 1]]
                i += 2
            elif tok in ("FROM", "TO", "CC", "SUBJECT"):
                value = tokens[i + 1].lower()
                matches = [m for m in matches if value in m.header(tok).lower()]
                i += 2
            elif tok == "UNSEEN":
                matches = [m for m in matches if "\\Seen" not in m.flags]
                i += 1
            else:
                i += 1  # accepted but not filtered (dates, BODY, ...)
        return "OK", [" ".join(str(m.uid) for m in matches).encode()]

    def _uid_fetch(self, uid_set, spec):
        out: list = []
        for uid_s in str(uid_set).split(","):
            msg = self.selected.get(int(uid_s))
            if msg is None:
                continue
            flags = " ".join(sorted(msg.flags))
            if "X-GM-THRID" in spec:
                out.append(f"1 (X-GM-THRID {msg.thrid} UID {msg.uid})".encode())
                continue
            if "HEADER.FIELDS" in spec:
                payload = msg.raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
            else:
                payload = msg.raw
                if not self.readonly and "PEEK" not in spec:
                    msg.flags.add("\\Seen")
            meta = f"1 (UID {msg.uid} FLAGS ({flags}) RFC822.SIZE {len(msg.raw)} BODY[] {{{len(payload)}}}"
            out.append((meta.encode(), payload))
            out.append(b")")
        if not out:
            return "OK", [None]
        return "OK", out

    def _uid_store(self, uid, op, flags):
        msg = self.selected.get(int(uid))
        names = set(re.findall(r"\\\w+", flags))
        if op.startswith("+"):
            msg.flags |= names
        else:
            msg.flags -= names
        return "OK", [b"STORE completed"]

    def _uid_copy(self, uid, dest):
        dest_folder = self.server.folders.get(_unquote(dest))
        if dest_folder is None:
            return "NO", [b"no such mailbox"]
        msg = self.selected.get(int(uid))
        dest_folder.add(msg.raw, set(msg.flags) - {"\\Deleted"}, msg.thrid)
        return "OK", [b"COPY completed"]

    def _uid_move(self, uid, dest):
        if "MOVE" not in {c.decode() if isinstance(c, bytes) else c for c in self.capabilities}:
            return "BAD", [b"MOVE not supported"]
        typ, data = self._uid_copy(uid, dest)
        if typ == "OK":
            self.selected.messages = [m for m in self.selected.messages if m.uid != int(uid)]
        return typ, data


# ----- fake SMTP -------------------------------------------------------------


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host, port=None, context=None, **_kw):
        self.host = host
        self.port = port
        self.sent: list[dict] = []
        self.starttls_called = False
        self.login_args = None
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def ehlo(self):
        return 250, b"ok"

    def starttls(self, context=None):
        self.starttls_called = True
        return 220, b"ready"

    def login(self, user, password):
        self.login_args = (user, password)
        return 235, b"ok"

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent.append({"msg": msg, "from_addr": from_addr, "to_addrs": list(to_addrs or [])})
        return {}

    def quit(self):
        self.quit_called = True


# ----- fixtures --------------------------------------------------------------


@pytest.fixture
def imap_server(monkeypatch) -> FakeIMAPServer:
    server = FakeIMAPServer()
    monkeypatch.setattr("owlpost.account.imaplib.IMAP4_SSL", server.connect)
    monkeypatch.setattr("owlpost.account.imaplib.IMAP4", server.connect)
    return server


@pytest.fixture
def smtp(monkeypatch) -> type[FakeSMTP]:
    FakeSMTP.instances = []
    monkeypatch.setattr("owlpost.account.smtplib.SMTP", FakeSMTP)
    monkeypatch.setattr("owlpost.account.smtplib.SMTP_SSL", FakeSMTP)
    return FakeSMTP


def make_account(**overrides) -> Account:
    values = dict(
        name="test",
        email="you@example.com",
        password="app-password",
        provider="generic",
        imap_host="imap.example.com",
        imap_port=993,
        smtp_host="smtp.example.com",
        smtp_port=587,
        auto_save_sent=True,
    )
    values.update(overrides)
    return Account(AccountConfig(**values))


def icloud_folders(server: FakeIMAPServer) -> None:
    server.add_folder("INBOX", "\\HasNoChildren")
    server.add_folder("Sent Messages", "\\HasNoChildren \\Sent")
    server.add_folder("Deleted Messages", "\\HasNoChildren \\Trash")
    server.add_folder("Drafts", "\\HasNoChildren \\Drafts")
    server.add_folder("Archive", "\\HasNoChildren \\Archive")
    server.add_folder("Junk", "\\HasNoChildren \\Junk")


def gmail_folders(server: FakeIMAPServer) -> None:
    server.add_folder("INBOX", "\\HasNoChildren")
    server.add_folder("[Gmail]", "\\HasChildren \\Noselect")
    server.add_folder("[Gmail]/All Mail", "\\All \\HasNoChildren")
    server.add_folder("[Gmail]/Sent Mail", "\\HasNoChildren \\Sent")
    server.add_folder("[Gmail]/Trash", "\\HasNoChildren \\Trash")
    server.add_folder("[Gmail]/Drafts", "\\Drafts \\HasNoChildren")
    server.add_folder("[Gmail]/Starred", "\\Flagged \\HasNoChildren")


@pytest.fixture
def write_config(tmp_path):
    def _write(text: str, name: str = "accounts.toml") -> Path:
        path = tmp_path / name
        path.write_text(textwrap.dedent(text))
        return path

    return _write


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Never read the developer's real config, and reset cached state."""
    monkeypatch.delenv("OWLPOST_CONFIG", raising=False)
    config_mod._command_cache.clear()
    from owlpost import server

    monkeypatch.setattr(server, "_config", None)
    monkeypatch.setattr(server, "_config_path", None)
