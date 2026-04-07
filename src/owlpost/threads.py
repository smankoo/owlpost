"""Conversation following.

Two strategies:

* **Gmail**: use the proprietary X-GM-THRID extension. Every message in
  Gmail's view of a thread shares the same thread id, so we look it up once
  and then SEARCH for it across [Gmail]/All Mail.

* **Generic / iCloud**: walk Message-ID + In-Reply-To + References across
  the inbox and sent folder. We BFS the reference graph until no new
  message-ids are discovered.
"""

from __future__ import annotations

import email
import imaplib
import re
from email import policy
from typing import Iterable

from .account import Account
from .folders import list_folders, quote_mailbox, resolve_role
from .messages import _envelope_from_msg


def _fetch_envelope(
    conn: imaplib.IMAP4_SSL, folder: str, uid: bytes | str
) -> dict | None:
    typ, data = conn.uid(
        "fetch",
        uid if isinstance(uid, str) else uid.decode(),
        "(BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID "
        "IN-REPLY-TO REFERENCES)] FLAGS)",
    )
    if typ != "OK" or not data:
        return None
    for item in data:
        if isinstance(item, tuple):
            meta, header_bytes = item
            msg = email.message_from_bytes(header_bytes, policy=policy.default)
            uid_m = re.search(rb"UID (\d+)", meta)
            env = _envelope_from_msg(
                msg, uid=uid_m.group(1).decode() if uid_m else None
            )
            env["folder"] = folder
            return env
    return None


def get_thread_gmail(
    account: Account, *, message_id: str | None, uid: str | None, folder: str | None
) -> list[dict]:
    with account.imap() as c:
        all_mail = resolve_role(c, "all") or "[Gmail]/All Mail"
        c.select(quote_mailbox(all_mail), readonly=True)

        # Locate the seed UID in [Gmail]/All Mail
        seed_uid = None
        if message_id:
            typ, data = c.uid(
                "search", None, "HEADER", "Message-ID", message_id
            )
            if typ == "OK" and data and data[0]:
                seed_uid = data[0].split()[0].decode()
        elif uid and folder:
            # Need to translate folder/uid to All Mail uid via Message-ID
            c.select(quote_mailbox(folder), readonly=True)
            typ, data = c.uid(
                "fetch",
                str(uid),
                "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
            )
            for item in data or []:
                if isinstance(item, tuple):
                    msg = email.message_from_bytes(
                        item[1], policy=policy.default
                    )
                    mid = (msg.get("Message-ID") or "").strip()
                    if mid:
                        c.select(quote_mailbox(all_mail), readonly=True)
                        typ, data = c.uid(
                            "search", None, "HEADER", "Message-ID", mid
                        )
                        if typ == "OK" and data and data[0]:
                            seed_uid = data[0].split()[0].decode()
        if not seed_uid:
            return []

        typ, data = c.uid("fetch", seed_uid, "(X-GM-THRID)")
        thrid = None
        for item in data or []:
            if isinstance(item, (bytes, bytearray)):
                m = re.search(rb"X-GM-THRID (\d+)", item)
                if m:
                    thrid = m.group(1).decode()
            elif isinstance(item, tuple):
                m = re.search(rb"X-GM-THRID (\d+)", item[0])
                if m:
                    thrid = m.group(1).decode()
        if not thrid:
            return []

        typ, data = c.uid("search", None, "X-GM-THRID", thrid)
        if typ != "OK" or not data or not data[0]:
            return []
        thread_uids = data[0].split()
        envs: list[dict] = []
        for u in thread_uids:
            env = _fetch_envelope(c, all_mail, u)
            if env:
                envs.append(env)
        envs.sort(key=lambda e: e.get("date_iso") or "")
        return envs


def get_thread_generic(
    account: Account,
    *,
    message_id: str | None,
    uid: str | None,
    folder: str | None,
) -> list[dict]:
    """BFS over the Message-ID / References graph across folders."""
    with account.imap() as c:
        # Folders to search across — inbox + sent + (archive if any)
        candidates = ["INBOX"]
        for role in ("sent", "archive", "all", "drafts"):
            f = resolve_role(c, role)
            if f and f not in candidates:
                candidates.append(f)

        # Seed message-id
        seed: str | None = message_id
        if not seed and uid and folder:
            c.select(quote_mailbox(folder), readonly=True)
            typ, data = c.uid(
                "fetch",
                str(uid),
                "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES IN-REPLY-TO)])",
            )
            for item in data or []:
                if isinstance(item, tuple):
                    msg = email.message_from_bytes(
                        item[1], policy=policy.default
                    )
                    seed = (msg.get("Message-ID") or "").strip() or None
                    break
        if not seed:
            return []

        visited_ids: set[str] = set()
        results: dict[tuple[str, str], dict] = {}
        queue: list[str] = [seed]

        while queue:
            mid = queue.pop()
            if mid in visited_ids:
                continue
            visited_ids.add(mid)
            for f in candidates:
                typ, _ = c.select(quote_mailbox(f), readonly=True)
                if typ != "OK":
                    continue
                # Find messages whose Message-ID matches
                for search_args in (
                    ("HEADER", "Message-ID", mid),
                    ("HEADER", "References", mid),
                    ("HEADER", "In-Reply-To", mid),
                ):
                    typ, data = c.uid("search", None, *search_args)
                    if typ != "OK" or not data or not data[0]:
                        continue
                    for u in data[0].split():
                        env = _fetch_envelope(c, f, u)
                        if not env:
                            continue
                        key = (f, env["uid"])
                        if key in results:
                            continue
                        results[key] = env
                        # Walk references outward
                        for ref in env.get("references") or []:
                            if ref not in visited_ids:
                                queue.append(ref)
                        if env.get("message_id") and env["message_id"] not in visited_ids:
                            queue.append(env["message_id"])
                        if env.get("in_reply_to") and env["in_reply_to"] not in visited_ids:
                            queue.append(env["in_reply_to"])

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
        return get_thread_gmail(
            account, message_id=message_id, uid=uid, folder=folder
        )
    return get_thread_generic(
        account, message_id=message_id, uid=uid, folder=folder
    )
