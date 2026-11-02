"""Composing, sending, replying, forwarding."""

from __future__ import annotations

import email
import imaplib
import mimetypes
import time
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from .account import Account
from .folders import quote_mailbox, resolve_role
from .messages import fetch_raw, is_attachment, leaf_parts, parse_message, read_message, safe_filename


def _build_message(
    *,
    from_addr: str,
    to: list[str],
    cc: list[str] | None,
    bcc: list[str] | None,
    subject: str,
    body: str,
    html: str | None,
    attachments: list[str] | None,
    in_reply_to: str | None,
    references: list[str] | None,
    attachment_parts: list[tuple[bytes, str, str]] | None = None,
) -> EmailMessage:
    """Build an EmailMessage with CRLF policy so IMAP APPEND works.

    `attachments` are local file paths; `attachment_parts` are in-memory
    (data, mime type, filename) triples, e.g. the parts of a forwarded message.
    """
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        # Bcc is intentionally not added to the on-the-wire headers
        pass
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@", 1)[1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        ref_chain = list(references or [])
        if in_reply_to not in ref_chain:
            ref_chain.append(in_reply_to)
        msg["References"] = " ".join(ref_chain)

    if html:
        msg.set_content(body or "")
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(body or "")

    parts = list(attachment_parts or [])
    for path in attachments or []:
        p = Path(path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"attachment not found: {p}")
        ctype, _ = mimetypes.guess_type(str(p))
        parts.append((p.read_bytes(), ctype or "application/octet-stream", p.name))
    for data, ctype, filename in parts:
        maintype, subtype = ctype.split("/", 1) if "/" in ctype else ("application", "octet-stream")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def _append_to_drafts(account: Account, msg: EmailMessage) -> str:
    with account.imap() as c:
        drafts = resolve_role(c, "drafts")
        if not drafts:
            raise RuntimeError("No Drafts folder found for this account")
        typ, data = c.append(
            quote_mailbox(drafts),
            "\\Draft",
            imaplib.Time2Internaldate(time.time()),
            msg.as_bytes(),
        )
        if typ != "OK":
            raise RuntimeError(f"APPEND to {drafts} failed: {data!r}")
        return drafts


def save_draft(
    account: Account,
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    html: str | None = None,
    attachments: list[str] | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> dict:
    """Build a message and APPEND it to the account's Drafts folder. Nothing is sent."""
    msg = _build_message(
        from_addr=account.cfg.email,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body=body,
        html=html,
        attachments=attachments,
        in_reply_to=in_reply_to,
        references=references,
    )
    saved_to = _append_to_drafts(account, msg)
    return {
        "message_id": msg["Message-ID"],
        "from": account.cfg.email,
        "to": to,
        "cc": cc or [],
        "bcc": bcc or [],
        "subject": subject,
        "saved_to_folder": saved_to,
        "size_bytes": len(msg.as_bytes()),
    }


def _append_to_sent(account: Account, msg: EmailMessage) -> str | None:
    if not account.cfg.auto_save_sent:
        return None
    with account.imap() as c:
        sent = resolve_role(c, "sent")
        if not sent:
            return None
        typ, data = c.append(
            quote_mailbox(sent),
            "\\Seen",
            imaplib.Time2Internaldate(time.time()),
            msg.as_bytes(),
        )
        if typ != "OK":
            raise RuntimeError(f"APPEND to {sent} failed: {data!r}")
        return sent


def send_message(
    account: Account,
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    html: str | None = None,
    attachments: list[str] | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    attachment_parts: list[tuple[bytes, str, str]] | None = None,
) -> dict:
    msg = _build_message(
        from_addr=account.cfg.email,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body=body,
        html=html,
        attachments=attachments,
        in_reply_to=in_reply_to,
        references=references,
        attachment_parts=attachment_parts,
    )
    recipients = list(to) + list(cc or []) + list(bcc or [])
    with account.smtp() as s:
        # Raises if every recipient is refused; otherwise returns the refused ones.
        refused = s.send_message(msg, from_addr=account.cfg.email, to_addrs=recipients) or {}
    result = {
        "message_id": msg["Message-ID"],
        "from": account.cfg.email,
        "to": to,
        "cc": cc or [],
        "bcc": bcc or [],
        "subject": subject,
        "saved_to_folder": None,
        "size_bytes": len(msg.as_bytes()),
        "refused_recipients": sorted(refused),
    }
    # The mail has gone out. A failure from here on must not surface as a
    # tool error, or the caller may reasonably retry and send it twice.
    try:
        result["saved_to_folder"] = _append_to_sent(account, msg)
    except Exception as e:
        result["warning"] = f"Sent, but saving a copy to the Sent folder failed: {e}"
    return result


# ----- reply / forward -------------------------------------------------------


def _quote_body(original: dict) -> str:
    sender = original.get("from") or [{}]
    sender_str = sender[0].get("email") if sender else ""
    date_str = original.get("date") or ""
    text = original.get("text") or ""
    quoted = "\n".join("> " + line for line in text.splitlines())
    return f"\n\nOn {date_str}, {sender_str} wrote:\n{quoted}\n"


def _normalize_subject(prefix: str, subject: str) -> str:
    s = (subject or "").strip()
    low = s.lower()
    if prefix == "Re:" and low.startswith("re:"):
        return s
    if prefix == "Fwd:" and (low.startswith("fwd:") or low.startswith("fw:")):
        return s
    return f"{prefix} {s}".strip()


def reply_message(
    account: Account,
    *,
    folder: str,
    uid: str,
    body: str,
    reply_all: bool = False,
    html: str | None = None,
    attachments: list[str] | None = None,
) -> dict:
    with account.imap() as c:
        original = read_message(c, folder, uid)

    orig_from = [a["email"] for a in original.get("from") or []]
    orig_to = [a["email"] for a in original.get("to") or []]
    orig_cc = [a["email"] for a in original.get("cc") or []]

    to = orig_from
    cc: list[str] = []
    if reply_all:
        # Reply-all: keep To+Cc minus our own address
        me = account.cfg.email.lower()
        cc = [a for a in (orig_to + orig_cc) if a.lower() != me and a not in to]

    in_reply_to = original.get("message_id") or None
    references = original.get("references") or []
    if in_reply_to and in_reply_to not in references:
        references = list(references) + [in_reply_to]

    subject = _normalize_subject("Re:", original.get("subject") or "")
    full_body = body + _quote_body(original)

    return send_message(
        account,
        to=to,
        cc=cc,
        subject=subject,
        body=full_body,
        html=html,
        attachments=attachments,
        in_reply_to=in_reply_to,
        references=references,
    )


def forward_message(
    account: Account,
    *,
    folder: str,
    uid: str,
    to: list[str],
    body: str = "",
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    attachments: list[str] | None = None,
    include_attachments: bool = True,
) -> dict:
    """Forward a message. Original attachments are re-attached by default."""
    with account.imap() as c:
        raw, flags = fetch_raw(c, folder, uid)
    original = parse_message(raw, folder=folder, uid=uid, flags=flags)

    subject = _normalize_subject("Fwd:", original.get("subject") or "")
    sender = (original.get("from") or [{}])[0]
    intro = (
        f"\n\n---------- Forwarded message ----------\n"
        f"From: {sender.get('name', '')} <{sender.get('email', '')}>\n"
        f"Date: {original.get('date', '')}\n"
        f"Subject: {original.get('subject', '')}\n"
        f"To: {', '.join(a.get('email', '') for a in original.get('to') or [])}\n\n"
    )
    full_body = body + intro + (original.get("text") or "")

    parts: list[tuple[bytes, str, str]] = []
    if include_attachments:
        msg = email.message_from_bytes(raw, policy=policy.default)
        for index, part in leaf_parts(msg):
            if is_attachment(part):
                parts.append(
                    (
                        part.get_payload(decode=True) or b"",
                        part.get_content_type(),
                        safe_filename(part.get_filename(), f"attachment-{index}.bin"),
                    )
                )

    return send_message(
        account,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body=full_body,
        attachments=attachments,
        attachment_parts=parts,
    )
