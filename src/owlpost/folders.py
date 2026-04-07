"""Folder listing + special-use (\\Sent, \\Trash, ...) resolution.

Both iCloud and Gmail support the IMAP SPECIAL-USE extension (RFC 6154), so
we discover the right folder name dynamically instead of hardcoding
"Sent Messages" vs "[Gmail]/Sent Mail".
"""

from __future__ import annotations

import imaplib
import re
from functools import lru_cache

# Map our friendly role names to IMAP SPECIAL-USE flags.
SPECIAL_USE_FLAGS = {
    "sent": "\\Sent",
    "trash": "\\Trash",
    "drafts": "\\Drafts",
    "junk": "\\Junk",
    "archive": "\\Archive",
    "all": "\\All",
    "flagged": "\\Flagged",
    "important": "\\Important",
}

# LIST response: (\HasNoChildren \Sent) "/" "Sent Messages"
_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) "(?P<delim>[^"]*)" (?P<name>.+)')


def _decode_mailbox_name(raw: bytes) -> str:
    raw = raw.strip()
    # IMAP mailbox names may be quoted; strip outer quotes
    if raw.startswith(b'"') and raw.endswith(b'"'):
        raw = raw[1:-1]
    # Modified UTF-7 decoding for non-ASCII names; for ASCII this is a no-op
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def list_folders(conn: imaplib.IMAP4_SSL) -> list[dict]:
    """Return [{name, flags, delimiter, role}] for every folder."""
    typ, data = conn.list()
    if typ != "OK":
        raise RuntimeError(f"LIST failed: {typ}")
    out: list[dict] = []
    for line in data:
        if not line:
            continue
        m = _LIST_RE.match(line)
        if not m:
            continue
        flags = m.group("flags").decode("ascii", "replace")
        delim = m.group("delim").decode("ascii", "replace")
        name = _decode_mailbox_name(m.group("name"))
        role = None
        for friendly, flag in SPECIAL_USE_FLAGS.items():
            if flag in flags:
                role = friendly
                break
        # Inbox is special-cased — never tagged with SPECIAL-USE
        if role is None and name.upper() == "INBOX":
            role = "inbox"
        out.append(
            {"name": name, "flags": flags, "delimiter": delim, "role": role}
        )
    return out


def resolve_role(conn: imaplib.IMAP4_SSL, role: str) -> str | None:
    """Return the folder name for a given role, or None if not found."""
    role = role.lower()
    if role == "inbox":
        return "INBOX"
    for f in list_folders(conn):
        if f["role"] == role:
            return f["name"]
    # Common fallbacks
    fallbacks = {
        "sent": ["Sent", "Sent Messages", "Sent Items", "[Gmail]/Sent Mail"],
        "trash": ["Trash", "Deleted Messages", "[Gmail]/Trash"],
        "drafts": ["Drafts", "[Gmail]/Drafts"],
        "all": ["[Gmail]/All Mail", "All Mail", "Archive"],
    }
    known = {f["name"] for f in list_folders(conn)}
    for candidate in fallbacks.get(role, []):
        if candidate in known:
            return candidate
    return None


def quote_mailbox(name: str) -> str:
    """Quote a mailbox name for use in IMAP commands."""
    # Escape backslashes and quotes per RFC 3501
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
