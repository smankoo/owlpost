"""owlpost MCP server entrypoint (FastMCP)."""

from __future__ import annotations

import argparse

# Force-load the IDNA codec before any IMAP/socket code runs. Without this,
# imaplib's hostname encoding fails with "unknown encoding: idna" under
# minimal/stripped environments (frozen builds, some uvx/venv setups).
import encodings.idna  # noqa: F401
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import __version__
from .account import Account
from .config import load_config
from .folders import list_folders as imap_list_folders
from .folders import resolve_role
from .messages import (
    build_search_criteria,
    download_attachment,
    move_message,
    read_message,
    search,
    set_flag,
    trash_message,
)
from .sending import forward_message, reply_message, save_draft, send_message
from .threads import get_thread

INSTRUCTIONS = """\
owlpost gives you IMAP/SMTP access to the user's configured mailboxes.
Call list_accounts first to discover account names; every other tool takes
one of those names as `account`. Messages are addressed by folder + uid as
returned by search_messages. send_email, reply_email and forward_email send
real mail immediately and cannot be undone: show the user the recipients,
subject and body and get explicit confirmation before calling them. Prefer
save_draft_email when the user may want to review the message first.
"""

mcp = FastMCP("owlpost", instructions=INSTRUCTIONS)
_config = None
# Set by --config; otherwise OWLPOST_CONFIG or the default path is used.
_config_path: Path | None = None

# Annotation presets. "Open world" is reserved for tools that reach people
# outside the user's mailbox (i.e. anything that sends mail).
_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_SENDS_MAIL = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}


def _annotations(title: str, **hints: bool) -> ToolAnnotations:
    return ToolAnnotations(title=title, **hints)


def _load() -> Any:
    global _config
    if _config is None:
        _config = load_config(_config_path)
    return _config


def _get_account(name: str) -> Account:
    return Account(_load().get(name))


# ----- account / folder discovery --------------------------------------------


@mcp.tool(annotations=_annotations("List accounts", **_READ_ONLY))
def list_accounts() -> list[dict]:
    """List the mail accounts configured in owlpost's accounts.toml.

    Returns one entry per account with its `name` (the value to pass as
    `account` to every other tool), email address, provider (icloud, gmail
    or generic), IMAP/SMTP hostnames and whether sent mail is auto-saved.
    Passwords and password sources are never included. Takes no arguments
    and does not contact any mail server.
    """
    return [
        {
            "name": cfg.name,
            "email": cfg.email,
            "provider": cfg.provider,
            "imap_host": cfg.imap_host,
            "smtp_host": cfg.smtp_host,
            "auto_save_sent": cfg.auto_save_sent,
        }
        for cfg in _load().accounts.values()
    ]


@mcp.tool(annotations=_annotations("List folders", **_READ_ONLY))
def list_folders(account: str) -> list[dict]:
    """List every IMAP folder (mailbox) in an account.

    Each entry has the folder `name` to use in other tools, its raw IMAP
    flags, the hierarchy delimiter, and a detected `role` (inbox, sent,
    trash, drafts, junk, archive, all, flagged, important) based on RFC 6154
    SPECIAL-USE flags, or null for ordinary folders.

    Args:
        account: Account name from list_accounts.
    """
    acc = _get_account(account)
    with acc.imap() as c:
        return imap_list_folders(c)


@mcp.tool(annotations=_annotations("Resolve folder role", **_READ_ONLY))
def resolve_folder(account: str, role: str) -> str | None:
    """Resolve a special-use role to the provider-specific folder name.

    For example "sent" becomes "Sent Messages" on iCloud and
    "[Gmail]/Sent Mail" on Gmail. Uses SPECIAL-USE flags first and falls back
    to common folder names. Returns null if no matching folder exists.

    Args:
        account: Account name from list_accounts.
        role: One of inbox, sent, trash, drafts, junk, archive, all, flagged
            or important (case-insensitive).
    """
    acc = _get_account(account)
    with acc.imap() as c:
        return resolve_role(c, role)


# ----- search / read ---------------------------------------------------------


@mcp.tool(annotations=_annotations("Search messages", **_READ_ONLY))
def search_messages(
    account: str,
    folder: str = "INBOX",
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
    raw: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Search one folder with structured filters and return message envelopes, newest first.

    All filters are combined with AND. String filters use IMAP
    case-insensitive substring matching; multi-word values are quoted for
    you. Each result has uid, folder, message_id, subject, from/to/cc, date,
    flags, size, seen and flagged; pass folder + uid to read_email for the
    body. The folder is opened read-only, so searching never marks mail read.

    Args:
        account: Account name from list_accounts.
        folder: Folder to search (default "INBOX"); use list_folders or
            resolve_folder for others.
        from_: Substring of the From header (name or address).
        to: Substring of the To header.
        cc: Substring of the Cc header.
        subject: Substring of the Subject.
        body: Substring of the message body.
        text: Substring of headers or body.
        since: Only messages on or after this date (YYYY-MM-DD).
        before: Only messages before this date (YYYY-MM-DD).
        unseen: Only unread messages.
        seen: Only read messages.
        flagged: Only flagged/starred messages.
        has_attachment: Best-effort filter for messages with attachments
            (multipart/mixed or an attachment disposition header).
        raw: A raw IMAP SEARCH string, e.g. 'OR FROM "alice" FROM "bob"'.
            When set, every other filter is ignored.
        limit: Maximum number of results (default 50).
    """
    acc = _get_account(account)
    criteria = build_search_criteria(
        from_=from_,
        to=to,
        cc=cc,
        subject=subject,
        body=body,
        text=text,
        since=since,
        before=before,
        unseen=unseen,
        seen=seen,
        flagged=flagged,
        has_attachment=has_attachment,
        raw=raw,
    )
    with acc.imap() as c:
        return search(c, folder, criteria, limit=limit)


@mcp.tool(annotations=_annotations("Read email", **_READ_ONLY))
def read_email(
    account: str,
    folder: str,
    uid: str,
    include_html: bool = False,
    include_text: bool = True,
    body_max_chars: int = 50_000,
) -> dict[str, Any]:
    """Read one message: decoded headers, body and attachment list, without marking it read.

    By default only the plain-text body is returned, truncated at 50,000
    characters to fit MCP result limits; the untruncated sizes are always
    reported as text_full_length and html_full_length. Attachments are
    listed with a 1-based `part` index for save_attachment. Threading
    headers (message_id, in_reply_to, references) are included.

    Args:
        account: Account name from list_accounts.
        folder: Folder containing the message.
        uid: Message UID from search_messages.
        include_html: Also return the HTML body (default false). HTML mail
            can be very large.
        include_text: Return the plain-text body (default true).
        body_max_chars: Truncate each body to this many characters
            (default 50000).
    """
    acc = _get_account(account)
    with acc.imap() as c:
        return read_message(
            c,
            folder,
            uid,
            include_html=include_html,
            include_text=include_text,
            body_max_chars=body_max_chars,
        )


@mcp.tool(
    annotations=_annotations(
        "Save attachment", readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
def save_attachment(account: str, folder: str, uid: str, part: int, save_to: str) -> dict:
    """Download one attachment from a message and write it to the local filesystem.

    The mailbox is not modified. Returns the written path, size, MIME type
    and original filename. An existing file at the target path is
    overwritten.

    Args:
        account: Account name from list_accounts.
        folder: Folder containing the message.
        uid: Message UID.
        part: 1-based part index from read_email's `attachments` list.
        save_to: Destination. A directory (trailing slash, existing
            directory, or a path without an extension) keeps the original
            filename; anything else is used as the full file path. `~` is
            expanded and missing directories are created.
    """
    acc = _get_account(account)
    with acc.imap() as c:
        return download_attachment(c, folder, uid, part, save_to)


# ----- conversation following ------------------------------------------------


@mcp.tool(annotations=_annotations("Get conversation", **_READ_ONLY))
def get_conversation(
    account: str,
    message_id: str | None = None,
    folder: str | None = None,
    uid: str | None = None,
) -> list[dict]:
    """Return every message in the same conversation as a given message, oldest first.

    On Gmail this uses the X-GM-THRID thread id across All Mail. Elsewhere it
    follows the Message-ID / In-Reply-To / References graph across the
    inbox, sent, archive, all-mail and drafts folders, so your own replies
    are included. Returns envelopes (no bodies); use read_email for content.
    Identify the seed message with either `message_id`, or `folder` + `uid`.

    Args:
        account: Account name from list_accounts.
        message_id: Message-ID header of any message in the thread,
            including angle brackets.
        folder: Folder of the seed message (used with uid).
        uid: UID of the seed message (used with folder).
    """
    acc = _get_account(account)
    return get_thread(acc, message_id=message_id, uid=uid, folder=folder)


# ----- send / reply / forward ------------------------------------------------


@mcp.tool(annotations=_annotations("Send email", **_SENDS_MAIL))
def send_email(
    account: str,
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
    """Send a new email over SMTP immediately; this cannot be undone, so confirm with the user first.

    When the account has auto_save_sent enabled (needed for iCloud, whose
    SMTP does not keep a copy) the message is also appended to the Sent
    folder. Returns the generated Message-ID, recipients and the folder the
    copy was saved to (null if none). To answer an existing message prefer
    reply_email, which fills in recipients and threading for you.

    Args:
        account: Account name from list_accounts; mail is sent from its address.
        to: Recipient email addresses.
        subject: Subject line.
        body: Plain-text body.
        cc: Cc recipient addresses.
        bcc: Bcc recipient addresses (delivered but never written to headers).
        html: Optional HTML alternative to the plain-text body.
        attachments: Local file paths to attach.
        in_reply_to: Message-ID this message replies to, for threading.
        references: Message-IDs of earlier messages in the thread.
    """
    acc = _get_account(account)
    return send_message(
        acc,
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        html=html,
        attachments=attachments,
        in_reply_to=in_reply_to,
        references=references,
    )


@mcp.tool(
    annotations=_annotations(
        "Save draft", readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
def save_draft_email(
    account: str,
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
    """Compose an email and save it to the account's Drafts folder without sending it.

    The message is appended to the special-use Drafts folder with the
    \\Draft flag, so the user can review, edit and send it from their own
    mail client. Use this instead of send_email whenever the user wants to
    check a message first. Each call creates a new draft.

    Args:
        account: Account name from list_accounts.
        to: Recipient email addresses.
        subject: Subject line.
        body: Plain-text body.
        cc: Cc recipient addresses.
        bcc: Bcc recipient addresses.
        html: Optional HTML alternative to the plain-text body.
        attachments: Local file paths to attach.
        in_reply_to: Message-ID this draft replies to, for threading.
        references: Message-IDs of earlier messages in the thread.
    """
    acc = _get_account(account)
    return save_draft(
        acc,
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        html=html,
        attachments=attachments,
        in_reply_to=in_reply_to,
        references=references,
    )


@mcp.tool(annotations=_annotations("Reply to email", **_SENDS_MAIL))
def reply_email(
    account: str,
    folder: str,
    uid: str,
    body: str,
    reply_all: bool = False,
    html: str | None = None,
    attachments: list[str] | None = None,
) -> dict:
    """Reply to a message and send the reply immediately; confirm with the user first.

    Addresses the reply to the original sender (plus the original To/Cc,
    minus your own address, when reply_all is true), prefixes the subject
    with "Re:" if needed, sets In-Reply-To and References so clients thread
    it correctly, and quotes the original plain-text body below yours.

    Args:
        account: Account name from list_accounts.
        folder: Folder containing the message being answered.
        uid: UID of the message being answered.
        body: Your reply text; the quoted original is appended after it.
        reply_all: Also reply to the original To and Cc recipients.
        html: Optional HTML alternative for the reply.
        attachments: Local file paths to attach.
    """
    acc = _get_account(account)
    return reply_message(
        acc,
        folder=folder,
        uid=uid,
        body=body,
        reply_all=reply_all,
        html=html,
        attachments=attachments,
    )


@mcp.tool(annotations=_annotations("Forward email", **_SENDS_MAIL))
def forward_email(
    account: str,
    folder: str,
    uid: str,
    to: list[str],
    body: str = "",
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    attachments: list[str] | None = None,
    include_attachments: bool = True,
) -> dict:
    """Forward a message to new recipients and send it immediately; confirm with the user first.

    The subject gets a "Fwd:" prefix, the original headers and plain-text
    body are included below your note, and the original attachments are
    re-attached unless include_attachments is false.

    Args:
        account: Account name from list_accounts.
        folder: Folder containing the message to forward.
        uid: UID of the message to forward.
        to: Recipient email addresses.
        body: Optional note placed above the forwarded content.
        cc: Cc recipient addresses.
        bcc: Bcc recipient addresses.
        attachments: Extra local file paths to attach.
        include_attachments: Re-attach the original message's attachments
            (default true).
    """
    acc = _get_account(account)
    return forward_message(
        acc,
        folder=folder,
        uid=uid,
        to=to,
        body=body,
        cc=cc,
        bcc=bcc,
        attachments=attachments,
        include_attachments=include_attachments,
    )


# ----- flags / move / delete -------------------------------------------------


_SETS_FLAG = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


@mcp.tool(annotations=_annotations("Mark read/unread", **_SETS_FLAG))
def mark_read(account: str, folder: str, uid: str, read: bool = True) -> dict:
    """Mark a message as read or unread by setting or clearing its \\Seen flag.

    Args:
        account: Account name from list_accounts.
        folder: Folder containing the message.
        uid: Message UID.
        read: True to mark read (default), false to mark unread.
    """
    acc = _get_account(account)
    with acc.imap() as c:
        set_flag(c, folder, uid, "\\Seen", read)
    return {"uid": uid, "folder": folder, "seen": read}


@mcp.tool(annotations=_annotations("Flag message", **_SETS_FLAG))
def flag_message(account: str, folder: str, uid: str, flagged: bool = True) -> dict:
    """Flag (star) or unflag a message by setting or clearing its \\Flagged flag.

    Args:
        account: Account name from list_accounts.
        folder: Folder containing the message.
        uid: Message UID.
        flagged: True to flag (default), false to remove the flag.
    """
    acc = _get_account(account)
    with acc.imap() as c:
        set_flag(c, folder, uid, "\\Flagged", flagged)
    return {"uid": uid, "folder": folder, "flagged": flagged}


_MOVES_MAIL = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}


@mcp.tool(annotations=_annotations("Move email", **_MOVES_MAIL))
def move_email(account: str, folder: str, uid: str, dest_folder: str) -> dict:
    """Move a message from one folder to another.

    Uses IMAP MOVE when the server supports it, otherwise COPY followed by
    flagging the original \\Deleted and EXPUNGE. The message gets a new UID
    in the destination folder, so the old uid is no longer valid afterwards.

    Args:
        account: Account name from list_accounts.
        folder: Folder the message is currently in.
        uid: Message UID in that folder.
        dest_folder: Destination folder name (see list_folders).
    """
    acc = _get_account(account)
    with acc.imap() as c:
        move_message(c, folder, uid, dest_folder)
    return {"uid": uid, "from": folder, "to": dest_folder}


@mcp.tool(annotations=_annotations("Delete email", **_MOVES_MAIL))
def delete_email(account: str, folder: str, uid: str) -> dict:
    """Delete a message by moving it to the account's Trash folder.

    The Trash folder is discovered via SPECIAL-USE (e.g. "Deleted Messages"
    on iCloud, "[Gmail]/Trash" on Gmail). The message is not permanently
    expunged from Trash, so it can still be recovered from the mail client.

    Args:
        account: Account name from list_accounts.
        folder: Folder the message is currently in.
        uid: Message UID in that folder.
    """
    acc = _get_account(account)
    with acc.imap() as c:
        dest = trash_message(c, folder, uid)
    return {"uid": uid, "from": folder, "to": dest}


def main(argv: list[str] | None = None) -> None:
    global _config_path
    parser = argparse.ArgumentParser(
        prog="owlpost",
        description="High-fidelity IMAP/SMTP MCP server. Speaks MCP over stdio.",
    )
    parser.add_argument("--version", action="version", version=f"owlpost {__version__}")
    parser.add_argument(
        "--config",
        metavar="PATH",
        type=Path,
        help="path to accounts.toml (default: $OWLPOST_CONFIG or ~/.config/owlpost/accounts.toml)",
    )
    args = parser.parse_args(argv)
    if args.config is not None:
        _config_path = args.config
    mcp.run()


if __name__ == "__main__":
    main()
