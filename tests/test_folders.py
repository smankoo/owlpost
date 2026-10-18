from __future__ import annotations

import pytest
from conftest import gmail_folders, icloud_folders, make_account

from owlpost.folders import list_folders, quote_mailbox, resolve_role


def _conn(server):
    return make_account().imap()


def test_icloud_special_use_roles(imap_server):
    icloud_folders(imap_server)
    with _conn(imap_server) as c:
        roles = {f["name"]: f["role"] for f in list_folders(c)}
        assert roles == {
            "INBOX": "inbox",
            "Sent Messages": "sent",
            "Deleted Messages": "trash",
            "Drafts": "drafts",
            "Archive": "archive",
            "Junk": "junk",
        }
        assert resolve_role(c, "sent") == "Sent Messages"
        assert resolve_role(c, "TRASH") == "Deleted Messages"


def test_gmail_special_use_roles(imap_server):
    gmail_folders(imap_server)
    with _conn(imap_server) as c:
        assert resolve_role(c, "sent") == "[Gmail]/Sent Mail"
        assert resolve_role(c, "all") == "[Gmail]/All Mail"
        assert resolve_role(c, "drafts") == "[Gmail]/Drafts"
        assert resolve_role(c, "trash") == "[Gmail]/Trash"
        assert resolve_role(c, "flagged") == "[Gmail]/Starred"
        folders = {f["name"]: f for f in list_folders(c)}
        assert folders["[Gmail]"]["role"] is None
        assert folders["[Gmail]/All Mail"]["delimiter"] == "/"


def test_generic_server_without_special_use_falls_back_to_names(imap_server):
    for name in ("INBOX", "Sent Items", "Trash", "Drafts", "Projects"):
        imap_server.add_folder(name)
    with _conn(imap_server) as c:
        assert resolve_role(c, "sent") == "Sent Items"
        assert resolve_role(c, "trash") == "Trash"
        assert resolve_role(c, "drafts") == "Drafts"
        assert resolve_role(c, "inbox") == "INBOX"
        assert resolve_role(c, "junk") is None
        assert resolve_role(c, "archive") is None


def test_special_use_wins_over_name_fallback(imap_server):
    imap_server.add_folder("INBOX")
    imap_server.add_folder("Sent")  # a plain folder that happens to be called Sent
    imap_server.add_folder("Sent Mail", "\\HasNoChildren \\Sent")
    with _conn(imap_server) as c:
        assert resolve_role(c, "sent") == "Sent Mail"


@pytest.mark.parametrize(
    "name, quoted",
    [
        ("INBOX", '"INBOX"'),
        ("Sent Messages", '"Sent Messages"'),
        ("[Gmail]/All Mail", '"[Gmail]/All Mail"'),
        ('Odd "quoted" \\ name', '"Odd \\"quoted\\" \\\\ name"'),
    ],
)
def test_quote_mailbox(name, quoted):
    assert quote_mailbox(name) == quoted
