"""Typed shapes of tool results.

These become each tool's MCP output schema, and results are validated
against them, so they must match what the IMAP/SMTP layer returns exactly.
Several keys ("from") are Python keywords, hence the functional syntax.
"""

from __future__ import annotations

from typing import NotRequired

# typing.TypedDict is not accepted by pydantic before Python 3.12.
from typing_extensions import TypedDict


class AccountInfo(TypedDict):
    name: str
    email: str
    provider: str
    imap_host: str
    smtp_host: str
    auto_save_sent: bool


class Folder(TypedDict):
    name: str
    flags: str
    delimiter: str
    role: str | None


class Address(TypedDict):
    name: str
    email: str


_Headers = TypedDict(
    "_Headers",
    {
        "uid": str | None,
        "folder": str,
        "message_id": str,
        "in_reply_to": str | None,
        "references": list[str],
        "subject": str,
        "from": list[Address],
        "to": list[Address],
        "cc": list[Address],
        "bcc": list[Address],
        "date": str | None,
        "date_iso": str | None,
        "flags": list[str],
        "seen": bool,
        "flagged": bool,
    },
)


class Envelope(_Headers):
    size: int | None


class Attachment(TypedDict):
    part: int
    filename: str
    mime: str
    size: int
    content_id: str | None


class Message(_Headers):
    text: str | None
    html: str | None
    text_full_length: int
    html_full_length: int
    attachments: list[Attachment]


class SavedAttachment(TypedDict):
    path: str
    size: int
    mime: str
    filename: str | None


_Composed = TypedDict(
    "_Composed",
    {
        "message_id": str,
        "from": str,
        "to": list[str],
        "cc": list[str],
        "bcc": list[str],
        "subject": str,
        "saved_to_folder": str | None,
        "size_bytes": int,
    },
)


class SendResult(_Composed):
    refused_recipients: list[str]
    warning: NotRequired[str]


class DraftResult(_Composed):
    pass


class ReadState(TypedDict):
    uid: str
    folder: str
    seen: bool


class FlagState(TypedDict):
    uid: str
    folder: str
    flagged: bool


MoveResult = TypedDict("MoveResult", {"uid": str, "from": str, "to": str})
