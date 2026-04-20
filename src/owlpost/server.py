"""owlpost MCP server entrypoint (FastMCP)."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .account import Account
from .config import load_config
from .folders import list_folders as imap_list_folders, resolve_role
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


mcp = FastMCP("owlpost")
_config = None


def _get_account(name: str) -> Account:
    global _config
    if _config is None:
        _config = load_config()
    return Account(_config.get(name))


# ----- account / folder discovery --------------------------------------------


@mcp.tool()
def list_accounts() -> list[dict]:
    """List configured mail accounts."""
    global _config
    if _config is None:
        _config = load_config()
    return [
        {
            "name": cfg.name,
            "email": cfg.email,
            "provider": cfg.provider,
            "imap_host": cfg.imap_host,
            "smtp_host": cfg.smtp_host,
            "auto_save_sent": cfg.auto_save_sent,
        }
        for cfg in _config.accounts.values()
    ]


@mcp.tool()
def list_folders(account: str) -> list[dict]:
    """List all folders for an account, with detected SPECIAL-USE roles."""
    acc = _get_account(account)
    with acc.imap() as c:
        return imap_list_folders(c)


@mcp.tool()
def resolve_folder(account: str, role: str) -> str | None:
    """Resolve a special-use role (sent/trash/drafts/inbox/all/archive) to a
    folder name for the given account.
    """
    acc = _get_account(account)
    with acc.imap() as c:
        return resolve_role(c, role)


# ----- search / read ---------------------------------------------------------


@mcp.tool()
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
    """Search a folder using structured filters.

    All string filters use IMAP substring semantics. `since`/`before` are
    YYYY-MM-DD. `raw` lets you pass an IMAP search string directly
    (e.g. 'OR FROM "alice" FROM "bob"'). Returns newest first.
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


@mcp.tool()
def read_email(
    account: str,
    folder: str,
    uid: str,
    include_html: bool = False,
    include_text: bool = True,
    body_max_chars: int = 50_000,
) -> dict[str, Any]:
    """Read a single message: headers, body, attachment list.

    By default returns only the plaintext body (HTML excluded), truncated
    at 50k chars to fit MCP tool result limits. Marketing/HTML mail can be
    huge — set `include_html=True` and a higher `body_max_chars` if needed.
    The full lengths are always reported as `text_full_length` /
    `html_full_length`.
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


@mcp.tool()
def save_attachment(
    account: str, folder: str, uid: str, part: int, save_to: str
) -> dict:
    """Download an attachment to disk. `part` is the 1-based index from
    read_email's `attachments` list. `save_to` may be a directory or a
    full file path.
    """
    acc = _get_account(account)
    with acc.imap() as c:
        return download_attachment(c, folder, uid, part, save_to)


# ----- conversation following ------------------------------------------------


@mcp.tool()
def get_conversation(
    account: str,
    message_id: str | None = None,
    folder: str | None = None,
    uid: str | None = None,
) -> list[dict]:
    """Return all messages in the same conversation/thread as the given
    message. Provide either `message_id`, or `folder`+`uid`. Uses Gmail's
    X-GM-THRID where available, otherwise walks the Message-ID/References
    graph across inbox/sent/archive folders.
    """
    acc = _get_account(account)
    return get_thread(acc, message_id=message_id, uid=uid, folder=folder)


# ----- send / reply / forward ------------------------------------------------


@mcp.tool()
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
    """Send an email. Always saves a copy to the Sent folder for accounts
    where SMTP doesn't do it automatically (e.g. iCloud).
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


@mcp.tool()
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
    """Save an email to the account's Drafts folder without sending it.

    Same parameters as send_email. Uses the account's special-use Drafts
    folder (\\Drafts) and flags the message \\Draft. Use this when the user
    wants to review/edit the message in their mail client before sending.
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


@mcp.tool()
def reply_email(
    account: str,
    folder: str,
    uid: str,
    body: str,
    reply_all: bool = False,
    html: str | None = None,
    attachments: list[str] | None = None,
) -> dict:
    """Reply to a message. Preserves threading headers and quotes the original."""
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


@mcp.tool()
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
    """Forward a message. Original attachments are re-attached by default."""
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


@mcp.tool()
def mark_read(account: str, folder: str, uid: str, read: bool = True) -> dict:
    """Mark a message read or unread."""
    acc = _get_account(account)
    with acc.imap() as c:
        set_flag(c, folder, uid, "\\Seen", read)
    return {"uid": uid, "folder": folder, "seen": read}


@mcp.tool()
def flag_message(account: str, folder: str, uid: str, flagged: bool = True) -> dict:
    """Star/flag (or unstar) a message."""
    acc = _get_account(account)
    with acc.imap() as c:
        set_flag(c, folder, uid, "\\Flagged", flagged)
    return {"uid": uid, "folder": folder, "flagged": flagged}


@mcp.tool()
def move_email(account: str, folder: str, uid: str, dest_folder: str) -> dict:
    """Move a message to another folder."""
    acc = _get_account(account)
    with acc.imap() as c:
        move_message(c, folder, uid, dest_folder)
    return {"uid": uid, "from": folder, "to": dest_folder}


@mcp.tool()
def delete_email(account: str, folder: str, uid: str) -> dict:
    """Move a message to Trash."""
    acc = _get_account(account)
    with acc.imap() as c:
        dest = trash_message(c, folder, uid)
    return {"uid": uid, "from": folder, "to": dest}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
