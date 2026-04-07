"""IMAP read/search/flag/move ops + MIME parsing."""

from __future__ import annotations

import email
import email.utils
import imaplib
import re
from datetime import date, datetime
from email import policy
from email.message import EmailMessage
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
        "references": [
            r for r in re.split(r"\s+", msg.get("References", "") or "") if r
        ],
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
        # caller knows what they're doing
        return raw.split()

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
        parts += ["OR", "HEADER", "Content-Type", "multipart/mixed",
                 "HEADER", "Content-Disposition", "attachment"]
    if not parts:
        parts = ["ALL"]
    return parts


def search(
    conn: imaplib.IMAP4_SSL,
    folder: str,
    criteria: list[str],
    limit: int = 50,
) -> list[dict]:
    typ, _ = conn.select(quote_mailbox(folder), readonly=True)
    if typ != "OK":
        raise RuntimeError(f"SELECT {folder} failed")
    typ, data = conn.uid("search", None, *criteria)
    if typ != "OK":
        raise RuntimeError(f"SEARCH failed: {data!r}")
    uids = data[0].split() if data and data[0] else []
    # Most recent last → reverse so we return newest first
    uids = list(reversed(uids))[:limit]
    if not uids:
        return []
    # Fetch envelopes in one shot
    uid_set = b",".join(uids).decode()
    typ, fetch_data = conn.uid(
        "fetch",
        uid_set,
        "(BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID "
        "IN-REPLY-TO REFERENCES)] FLAGS RFC822.SIZE)",
    )
    return _parse_fetch_envelopes(fetch_data, folder)


def _parse_fetch_envelopes(fetch_data, folder: str) -> list[dict]:
    """imaplib FETCH responses are notoriously gnarly. We pair the metadata
    line (with UID and FLAGS) with the literal header block that follows it.
    """
    out: list[dict] = []
    pending_meta: bytes | None = None
    for item in fetch_data:
        if isinstance(item, tuple):
            meta, header_bytes = item
            uid_m = re.search(rb"UID (\d+)", meta)
            flags_m = re.search(rb"FLAGS \(([^)]*)\)", meta)
            size_m = re.search(rb"RFC822\.SIZE (\d+)", meta)
            uid = uid_m.group(1).decode() if uid_m else None
            flags = (
                flags_m.group(1).decode().split() if flags_m else []
            )
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


def read_message(
    conn: imaplib.IMAP4_SSL,
    folder: str,
    uid: str,
    *,
    include_html: bool = False,
    include_text: bool = True,
    body_max_chars: int = 50_000,
) -> dict[str, Any]:
    typ, _ = conn.select(quote_mailbox(folder), readonly=True)
    if typ != "OK":
        raise RuntimeError(f"SELECT {folder} failed")
    typ, data = conn.uid("fetch", str(uid), "(BODY.PEEK[] FLAGS)")
    if typ != "OK" or not data or data[0] is None:
        raise RuntimeError(f"FETCH uid={uid} failed")
    raw = None
    flags: list[str] = []
    for item in data:
        if isinstance(item, tuple):
            meta, body = item
            raw = body
            flags_m = re.search(rb"FLAGS \(([^)]*)\)", meta)
            if flags_m:
                flags = flags_m.group(1).decode().split()
    if raw is None:
        raise RuntimeError(f"No body returned for uid={uid}")
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

    if not include_text:
        text_body = None
    else:
        text_body = _truncate(text_body)
    if not include_html:
        html_body = None
    else:
        html_body = _truncate(html_body)

    env = _envelope_from_msg(msg, uid=str(uid))
    env["folder"] = folder
    env["text_full_length"] = text_full_len
    env["html_full_length"] = html_full_len
    env["flags"] = flags
    env["text"] = text_body
    env["html"] = html_body
    env["attachments"] = attachments
    return env


def _walk_body(msg: EmailMessage):
    text_body = None
    html_body = None
    attachments = []
    part_index = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        part_index += 1
        ctype = part.get_content_type()
        disp = part.get_content_disposition() or ""
        filename = part.get_filename()
        if disp == "attachment" or filename:
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                {
                    "part": part_index,
                    "filename": _decode_header(filename) or f"part-{part_index}",
                    "mime": ctype,
                    "size": len(payload),
                    "content_id": (part.get("Content-ID") or "").strip("<>") or None,
                }
            )
        elif ctype == "text/plain" and text_body is None:
            try:
                text_body = part.get_content()
            except Exception:
                text_body = (part.get_payload(decode=True) or b"").decode(
                    "utf-8", "replace"
                )
        elif ctype == "text/html" and html_body is None:
            try:
                html_body = part.get_content()
            except Exception:
                html_body = (part.get_payload(decode=True) or b"").decode(
                    "utf-8", "replace"
                )
    return text_body, html_body, attachments


def download_attachment(
    conn: imaplib.IMAP4_SSL,
    folder: str,
    uid: str,
    part_index: int,
    save_to: str,
) -> dict:
    typ, _ = conn.select(quote_mailbox(folder), readonly=True)
    if typ != "OK":
        raise RuntimeError(f"SELECT {folder} failed")
    typ, data = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
    raw = None
    for item in data:
        if isinstance(item, tuple):
            raw = item[1]
            break
    if raw is None:
        raise RuntimeError("attachment fetch failed")
    msg = email.message_from_bytes(raw, policy=policy.default)
    counter = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        counter += 1
        if counter == part_index:
            payload = part.get_payload(decode=True) or b""
            from pathlib import Path
            raw_save = save_to
            p = Path(save_to).expanduser()
            # Treat as directory if: explicit trailing slash, existing dir,
            # or no file extension. Otherwise treat as full file path.
            looks_like_dir = (
                raw_save.endswith("/")
                or p.is_dir()
                or p.suffix == ""
            )
            if looks_like_dir:
                p.mkdir(parents=True, exist_ok=True)
                fname = part.get_filename() or f"attachment-{counter}.bin"
                p = p / fname
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(payload)
            return {
                "path": str(p),
                "size": len(payload),
                "mime": part.get_content_type(),
                "filename": part.get_filename(),
            }
    raise RuntimeError(f"part {part_index} not found in uid={uid}")


# ----- mutating ops ----------------------------------------------------------


def _select_writable(conn: imaplib.IMAP4_SSL, folder: str) -> None:
    typ, _ = conn.select(quote_mailbox(folder))
    if typ != "OK":
        raise RuntimeError(f"SELECT {folder} failed")


def set_flag(
    conn: imaplib.IMAP4_SSL, folder: str, uid: str, flag: str, on: bool
) -> None:
    _select_writable(conn, folder)
    op = "+FLAGS" if on else "-FLAGS"
    typ, data = conn.uid("store", str(uid), op, f"({flag})")
    if typ != "OK":
        raise RuntimeError(f"STORE failed: {data!r}")


def move_message(
    conn: imaplib.IMAP4_SSL, folder: str, uid: str, dest_folder: str
) -> None:
    _select_writable(conn, folder)
    # Prefer MOVE extension when supported; fall back to COPY+DELETE
    capabilities = conn.capabilities or ()
    if any(b"MOVE" in c for c in capabilities):
        typ, data = conn.uid("move", str(uid), quote_mailbox(dest_folder))
        if typ != "OK":
            raise RuntimeError(f"MOVE failed: {data!r}")
    else:
        typ, data = conn.uid("copy", str(uid), quote_mailbox(dest_folder))
        if typ != "OK":
            raise RuntimeError(f"COPY failed: {data!r}")
        conn.uid("store", str(uid), "+FLAGS", "(\\Deleted)")
        conn.expunge()


def trash_message(
    conn: imaplib.IMAP4_SSL, folder: str, uid: str
) -> str:
    trash = resolve_role(conn, "trash") or "Trash"
    move_message(conn, folder, uid, trash)
    return trash
