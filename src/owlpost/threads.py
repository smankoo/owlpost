"""Conversation following.

Two strategies:

* **Gmail**: use the proprietary X-GM-THRID extension. Every message in
  Gmail's view of a thread shares the same thread id, so we look it up once
  and then SEARCH for it across [Gmail]/All Mail.

* **Generic / iCloud**: walk Message-ID + In-Reply-To + References across
  the inbox, sent, archive, all-mail and drafts folders. Each round searches
  every folder once for the whole frontier of newly discovered message-ids,
  until no new ones turn up.
"""

from __future__ import annotations

import email
import imaplib
import re
from email import policy

from .account import Account
from .folders import quote_mailbox, resolve_role
from .messages import fetch_envelopes

# Message-ids per SEARCH command (each becomes three OR'd HEADER terms).
_SEARCH_CHUNK = 10
# Stop walking after this many rounds; real threads converge in a few.
_MAX_ROUNDS = 25


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _seed_message_id(conn: imaplib.IMAP4, folder: str, uid: str) -> str | None:
    typ, _ = conn.select(quote_mailbox(folder), readonly=True)
    if typ != "OK":
        raise RuntimeError(f"SELECT {folder} failed (does the folder exist? see list_folders)")
    typ, data = conn.uid("fetch", str(uid), "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
    for item in data or []:
        if isinstance(item, tuple):
            msg = email.message_from_bytes(item[1], policy=policy.default)
            return (msg.get("Message-ID") or "").strip() or None
    return None


def _uids(data) -> list[bytes]:
    return data[0].split() if data and data[0] else []


def get_thread_gmail(
    account: Account, *, message_id: str | None, uid: str | None, folder: str | None
) -> list[dict]:
    with account.imap() as c:
        all_mail = resolve_role(c, "all") or "[Gmail]/All Mail"
        if not message_id and uid and folder:
            message_id = _seed_message_id(c, folder, uid)
        if not message_id:
            return []

        typ, _ = c.select(quote_mailbox(all_mail), readonly=True)
        if typ != "OK":
            raise RuntimeError(f"SELECT {all_mail} failed")
        typ, data = c.uid("search", None, "HEADER", "Message-ID", _quote(message_id))
        seed = _uids(data)[:1] if typ == "OK" else []
        if not seed:
            return []

        typ, data = c.uid("fetch", seed[0].decode(), "(X-GM-THRID)")
        thrid = None
        for item in data or []:
            line = item[0] if isinstance(item, tuple) else item
            if isinstance(line, (bytes, bytearray)):
                m = re.search(rb"X-GM-THRID (\d+)", line)
                if m:
                    thrid = m.group(1).decode()
        if not thrid:
            return []

        typ, data = c.uid("search", None, "X-GM-THRID", thrid)
        if typ != "OK":
            return []
        envs = fetch_envelopes(c, all_mail, _uids(data))
    envs.sort(key=lambda e: e.get("date_iso") or "")
    return envs


def _reference_criteria(message_ids: list[str]) -> list[str]:
    """IMAP SEARCH tokens matching any message that is, or refers to, one of the ids."""
    terms = [
        ["HEADER", header, _quote(mid)]
        for mid in message_ids
        for header in ("Message-ID", "References", "In-Reply-To")
    ]
    tokens = terms[0]
    for term in terms[1:]:
        tokens = ["OR", *tokens, *term]
    return tokens


def get_thread_generic(
    account: Account,
    *,
    message_id: str | None,
    uid: str | None,
    folder: str | None,
) -> list[dict]:
    """Breadth-first walk of the Message-ID / References graph across folders."""
    with account.imap() as c:
        candidates = ["INBOX"]
        for role in ("sent", "archive", "all", "drafts"):
            f = resolve_role(c, role)
            if f and f not in candidates:
                candidates.append(f)

        seed = message_id
        if not seed and uid and folder:
            seed = _seed_message_id(c, folder, uid)
        if not seed:
            return []

        visited: set[str] = set()
        results: dict[tuple[str, str], dict] = {}
        frontier = [seed]
        for _ in range(_MAX_ROUNDS):
            if not frontier:
                break
            visited.update(frontier)
            discovered: list[str] = []
            for f in candidates:
                typ, _ = c.select(quote_mailbox(f), readonly=True)
                if typ != "OK":
                    continue
                matches: list[bytes] = []
                for i in range(0, len(frontier), _SEARCH_CHUNK):
                    criteria = _reference_criteria(frontier[i : i + _SEARCH_CHUNK])
                    typ, data = c.uid("search", None, *criteria)
                    if typ == "OK":
                        matches += _uids(data)
                new = list(dict.fromkeys(u for u in matches if (f, u.decode()) not in results))
                for env in fetch_envelopes(c, f, new):
                    results[(f, env["uid"])] = env
                    for mid in (env.get("message_id"), env.get("in_reply_to"), *env.get("references", [])):
                        if mid and mid not in visited and mid not in discovered:
                            discovered.append(mid)
            frontier = discovered

    envs = list(results.values())
    envs.sort(key=lambda e: e.get("date_iso") or "")
    return envs


def get_thread(
    account: Account,
    *,
    message_id: str | None = None,
    uid: str | None = None,
    folder: str | None = None,
) -> list[dict]:
    if account.cfg.is_gmail:
        return get_thread_gmail(account, message_id=message_id, uid=uid, folder=folder)
    return get_thread_generic(account, message_id=message_id, uid=uid, folder=folder)
