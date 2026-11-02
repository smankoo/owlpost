"""IMAP read/search/flag/move ops + MIME parsing."""

from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import os
import re
from collections.abc import Iterator
from datetime import date, datetime
from email import policy
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from .folders import quote_mailbox, resolve_role

# ----- helpers ---------------------------------------------------------------


def _decode_header(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parts = email.header.decode_header(value)
        out = []
        for text, charset in parts:
            if isinstance(text, bytes):
                out.append(text.decode(charset or "utf-8", errors="replace"))
            else:
                out.append(text)
        return "".join(out)
    except Exception:
        return value


def _parse_addrs(value: str | None) -> list[dict]:
    if not value:
        return []
    return [
        {"name": _decode_header(name) or "", "email": addr}
        for name, addr in email.utils.getaddresses([value])
        if addr
    ]


def _envelope_from_msg(msg: EmailMessage, uid: str | None = None) -> dict:
    return {
        "uid": uid,
        "message_id": (msg.get("Message-ID") or "").strip(),
        "in_reply_to": (msg.get("In-Reply-To") or "").strip() or None,
        "references": [r for r in re.split(r"\s+", msg.get("References", "") or "") if r],
        "subject": _decode_header(msg.get("Subject")) or "",
        "from": _parse_addrs(msg.get("From")),
        "to": _parse_addrs(msg.get("To")),
        "cc": _parse_addrs(msg.get("Cc")),
        "bcc": _parse_addrs(msg.get("Bcc")),
        "date": msg.get("Date"),
        "date_iso": _date_iso(msg.get("Date")),
    }


def _date_iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
        return dt.isoformat()
    except Exception:
        return None


def _imap_date(d: str | date) -> str:
    """Convert YYYY-MM-DD or date to IMAP date format (DD-Mon-YYYY)."""
    if isinstance(d, str):
        d = datetime.strptime(d, "%Y-%m-%d").date()
    return d.strftime("%d-%b-%Y")


# ----- search ----------------------------------------------------------------

_RAW_TOKEN_RE = re.compile(r'"(?:\\.|[^"\\])*"|\S+')

# Header fields fetched for envelopes. UID must be explicit: Proton Bridge
# omits it otherwise, unlike iCloud/Gmail which echo it on a UID FETCH.
_ENVELOPE_FETCH = (
    "(UID BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID "
    "IN-REPLY-TO REFERENCES)] FLAGS RFC822.SIZE)"
)


def build_search_criteria(
    *,
    from_: str | None = None,
    to: str | None = None,
    cc: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    text: str | None = None,
    since: str | None = None,
    before: str | None = None,
    unseen: bool = False,
    seen: bool = False,
    flagged: bool = False,
    has_attachment: bool = False,
    header_message_id: str | None = None,
    raw: str | None = None,
) -> list[str]:
    """Build a list of IMAP search tokens. Returned tokens are passed as
    individual arguments to conn.uid('search', None, *tokens) so values
    are properly quoted by imaplib.
    """
    if raw:
        # Caller knows what they're doing; split on whitespace but keep
        # quoted strings such as FROM "Jane Doe" in one token.
        return _RAW_TOKEN_RE.findall(raw)

    def q(v: str) -> str:
        # imaplib only auto-quotes when it sees specials; strings with spaces
        # need explicit IMAP-quoted form. Escape backslashes and quotes.
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    parts: list[str] = []
    if from_:
        parts += ["FROM", q(from_)]
    if to:
        parts += ["TO", q(to)]
    if cc:
        parts += ["CC", q(cc)]
    if subject:
        parts += ["SUBJECT", q(subject)]
    if body:
        parts += ["BODY", q(body)]
    if text:
        parts += ["TEXT", q(text)]
    if since:
        parts += ["SINCE", _imap_date(since)]
    if before:
        parts += ["BEFORE", _imap_date(before)]
    if unseen:
        parts += ["UNSEEN"]
    if seen:
        parts += ["SEEN"]
    if flagged:
        parts += ["FLAGGED"]
    if header_message_id:
        parts += ["HEADER", "Message-ID", header_message_id]
    if has_attachment:
        # Not standard IMAP — best effort using BODY search.
        parts += [
            "OR",
            "HEADER",
            "Content-Type",
            "multipart/mixed",
            "HEADER",
            "Content-Disposition",
            "attachment",
        ]
    if not parts:
        parts = ["ALL"]
    return parts


def select_folder(conn: imaplib.IMAP4, folder: str, *, readonly: bool = True) -> None:
    typ, _ = conn.select(quote_mailbox(folder), readonly=readonly)
    if typ != "OK":
        raise RuntimeError(f"SELECT {folder} failed (does the folder exist? see list_folders)")


def search(
    conn: imaplib.IMAP4,
    folder: str,
    criteria: list[str],
    limit: int = 50,
) -> list[dict]:
    select_folder(conn, folder)
    typ, data = conn.uid("search", None, *criteria)
    if typ != "OK":
        raise RuntimeError(f"SEARCH failed: {data!r}")
    uids = data[0].split() if data and data[0] else []
    # UIDs ascend with arrival, so the last ones are the newest.
    newest = list(reversed(uids))[: max(limit, 0)]
    return fetch_envelopes(conn, folder, newest)


def fetch_envelopes(conn: imaplib.IMAP4, folder: str, uids: list[bytes] | list[str]) -> list[dict]:
    """Fetch envelopes for UIDs in the selected folder with one UID FETCH.

    Results come back in the order of `uids`: servers answer a FETCH in
    their own (usually ascending) order regardless of how the set is written.
    """
    wanted = [u.decode() if isinstance(u, bytes) else str(u) for u in uids]
    if not wanted:
        return []
    typ, fetch_data = conn.uid("fetch", ",".join(wanted), _ENVELOPE_FETCH)
    if typ != "OK":
        raise RuntimeError(f"FETCH failed: {fetch_data!r}")
    by_uid = {env["uid"]: env for env in _parse_fetch_envelopes(fetch_data, folder)}
    return [by_uid[u] for u in wanted if u in by_uid]


def _parse_fetch_envelopes(fetch_data, folder: str) -> list[dict]:
    """imaplib FETCH responses are notoriously gnarly. We pair the metadata
    line (with UID and FLAGS) with the literal header block that follows it.
    """
    out: list[dict] = []
    for item in fetch_data or []:
        if isinstance(item, tuple):
            meta, header_bytes = item
            uid_m = re.search(rb"UID (\d+)", meta)
            flags_m = re.search(rb"FLAGS \(([^)]*)\)", meta)
            size_m = re.search(rb"RFC822\.SIZE (\d+)", meta)
            uid = uid_m.group(1).decode() if uid_m else None
            flags = flags_m.group(1).decode().split() if flags_m else []
            size = int(size_m.group(1)) if size_m else None
            msg = email.message_from_bytes(header_bytes, policy=policy.default)
            env = _envelope_from_msg(msg, uid=uid)
            env["folder"] = folder
            env["flags"] = flags
            env["size"] = size
            env["seen"] = "\\Seen" in flags
            env["flagged"] = "\\Flagged" in flags
            out.append(env)
        # Closing b')' lines are bytes — ignore
    return out


# ----- read full message -----------------------------------------------------


def fetch_raw(conn: imaplib.IMAP4, folder: str, uid: str) -> tuple[bytes, list[str]]:
    """Return (RFC 822 bytes, flags) for one message, without marking it read."""
    select_folder(conn, folder)
    typ, data = conn.uid("fetch", str(uid), "(UID BODY.PEEK[] FLAGS)")
    if typ != "OK":
        raise RuntimeError(f"FETCH uid={uid} failed: {data!r}")
    for item in data or []:
        if isinstance(item, tuple):
            meta, body = item
            flags_m = re.search(rb"FLAGS \(([^)]*)\)", meta)
            return body, flags_m.group(1).decode().split() if flags_m else []
    raise RuntimeError(f"No message with uid={uid} in {folder}; it may have been moved or deleted")


def parse_message(
    raw: bytes,
    *,
    folder: str,
    uid: str,
    flags: list[str],
    include_html: bool = False,
    include_text: bool = True,
    body_max_chars: int = 50_000,
) -> dict[str, Any]:
    msg = email.message_from_bytes(raw, policy=policy.default)
    text_body, html_body, attachments = _walk_body(msg)
    text_full_len = len(text_body) if text_body else 0
    html_full_len = len(html_body) if html_body else 0

    def _truncate(s: str | None) -> str | None:
        if s is None:
            return None
        if len(s) <= body_max_chars:
            return s
        return s[:body_max_chars] + f"\n\n[... truncated, {len(s) - body_max_chars} more chars ...]"

    env = _envelope_from_msg(msg, uid=str(uid))
    env["folder"] = folder
    env["flags"] = flags
    env["seen"] = "\\Seen" in flags
    env["flagged"] = "\\Flagged" in flags
    env["text"] = _truncate(text_body) if include_text else None
    env["html"] = _truncate(html_body) if include_html else None
    env["text_full_length"] = text_full_len
    env["html_full_length"] = html_full_len
    env["attachments"] = attachments
    return env


def read_message(
    conn: imaplib.IMAP4,
    folder: str,
    uid: str,
    *,
    include_html: bool = False,
    include_text: bool = True,
    body_max_chars: int = 50_000,
) -> dict[str, Any]:
    raw, flags = fetch_raw(conn, folder, uid)
    return parse_message(
        raw,
        folder=folder,
        uid=str(uid),
        flags=flags,
        include_html=include_html,
        include_text=include_text,
        body_max_chars=body_max_chars,
    )


def leaf_parts(msg: EmailMessage) -> Iterator[tuple[int, EmailMessage]]:
    """Yield (1-based index, part) for every non-multipart part.

    This numbering is the `part` index read_email reports and
    save_attachment accepts, so every caller must walk the same way.
    """
    index = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        index += 1
        yield index, part


def is_attachment(part: EmailMessage) -> bool:
    return part.get_content_disposition() == "attachment" or bool(part.get_filename())


def _walk_body(msg: EmailMessage):
    text_body = None
    html_body = None
    attachments = []
    for index, part in leaf_parts(msg):
        ctype = part.get_content_type()
        if is_attachment(part):
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                {
                    "part": index,
                    "filename": _decode_header(part.get_filename()) or f"part-{index}",
                    "mime": ctype,
                    "size": len(payload),
                    "content_id": (part.get("Content-ID") or "").strip("<>") or None,
                }
            )
        elif ctype == "text/plain" and text_body is None:
            text_body = _part_text(part)
        elif ctype == "text/html" and html_body is None:
            html_body = _part_text(part)
    return text_body, html_body, attachments


def _part_text(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except Exception:
        # Unknown or lying charset: decode what we can rather than fail the read.
        return (part.get_payload(decode=True) or b"").decode("utf-8", "replace")


def safe_filename(name: str | None, fallback: str) -> str:
    """Reduce an attacker-controlled attachment filename to a bare file name.

    Strips directory components (both separators) so that a name like
    "../../.zshrc" cannot write outside the chosen directory.
    """
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip().replace("\x00", "")
    if base in ("", ".", ".."):
        return fallback
    return base


def download_attachment(
    conn: imaplib.IMAP4,
    folder: str,
    uid: str,
    part_index: int,
    save_to: str,
) -> dict:
    raw, _ = fetch_raw(conn, folder, uid)
    msg = email.message_from_bytes(raw, policy=policy.default)
    for index, part in leaf_parts(msg):
        if index != part_index:
            continue
        payload = part.get_payload(decode=True) or b""
        p = Path(save_to).expanduser()
        # Treat as directory if: explicit trailing slash, existing dir,
        # or no file extension. Otherwise treat as full file path.
        looks_like_dir = save_to.endswith(("/", os.sep)) or p.is_dir() or p.suffix == ""
        if looks_like_dir:
            p.mkdir(parents=True, exist_ok=True)
            p = p / safe_filename(part.get_filename(), f"attachment-{index}.bin")
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(payload)
        return {
            "path": str(p),
            "size": len(payload),
            "mime": part.get_content_type(),
            "filename": part.get_filename(),
        }
    raise RuntimeError(f"part {part_index} not found in uid={uid}; use the part numbers from read_email")


# ----- mutating ops ----------------------------------------------------------


def _select_writable(conn: imaplib.IMAP4, folder: str) -> None:
    select_folder(conn, folder, readonly=False)


def set_flag(conn: imaplib.IMAP4, folder: str, uid: str, flag: str, on: bool) -> None:
    _select_writable(conn, folder)
    op = "+FLAGS" if on else "-FLAGS"
    typ, data = conn.uid("store", str(uid), op, f"({flag})")
    if typ != "OK":
        raise RuntimeError(f"STORE failed: {data!r}")


def move_message(conn: imaplib.IMAP4, folder: str, uid: str, dest_folder: str) -> None:
    _select_writable(conn, folder)
    # Prefer MOVE extension when supported; fall back to COPY+DELETE
    capabilities = tuple(c.decode() if isinstance(c, bytes) else c for c in (conn.capabilities or ()))
    if "MOVE" in capabilities:
        typ, data = conn.uid("move", str(uid), quote_mailbox(dest_folder))
        if typ != "OK":
            raise RuntimeError(f"MOVE failed: {data!r}")
    else:
        typ, data = conn.uid("copy", str(uid), quote_mailbox(dest_folder))
        if typ != "OK":
            raise RuntimeError(f"COPY failed: {data!r}")
        conn.uid("store", str(uid), "+FLAGS", "(\\Deleted)")
        conn.expunge()


def trash_message(conn: imaplib.IMAP4, folder: str, uid: str) -> str:
    trash = resolve_role(conn, "trash") or "Trash"
    move_message(conn, folder, uid, trash)
    return trash
