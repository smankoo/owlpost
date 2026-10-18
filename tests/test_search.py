from __future__ import annotations

from datetime import date

import pytest
from conftest import icloud_folders, make_account, make_message

from owlpost.messages import _imap_date, build_search_criteria, search


def test_no_filters_means_all():
    assert build_search_criteria() == ["ALL"]


def test_multi_word_values_are_quoted():
    crit = build_search_criteria(from_="Alice Example", subject="quarterly report draft")
    assert crit == ["FROM", '"Alice Example"', "SUBJECT", '"quarterly report draft"']


def test_quotes_and_backslashes_are_escaped():
    crit = build_search_criteria(subject='say "hi" \\ bye')
    assert crit == ["SUBJECT", '"say \\"hi\\" \\\\ bye"']


def test_date_filters_use_imap_format():
    crit = build_search_criteria(since="2024-03-05", before="2024-12-25")
    assert crit == ["SINCE", "05-Mar-2024", "BEFORE", "25-Dec-2024"]


def test_imap_date_accepts_date_objects():
    assert _imap_date(date(2025, 1, 9)) == "09-Jan-2025"


def test_invalid_date_raises():
    with pytest.raises(ValueError):
        build_search_criteria(since="05/03/2024")


def test_combined_filters_order_and_flags():
    crit = build_search_criteria(
        to="you@example.com",
        cc="team",
        body="invoice",
        text="paid",
        unseen=True,
        flagged=True,
        has_attachment=True,
    )
    assert crit[:10] == [
        "TO",
        '"you@example.com"',
        "CC",
        '"team"',
        "BODY",
        '"invoice"',
        "TEXT",
        '"paid"',
        "UNSEEN",
        "FLAGGED",
    ]
    assert crit[10:] == [
        "OR",
        "HEADER",
        "Content-Type",
        "multipart/mixed",
        "HEADER",
        "Content-Disposition",
        "attachment",
    ]


def test_raw_overrides_everything():
    assert build_search_criteria(raw='OR FROM "a" FROM "b"', subject="ignored") == [
        "OR",
        "FROM",
        '"a"',
        "FROM",
        '"b"',
    ]


def test_search_returns_newest_first_with_limit_and_readonly(imap_server):
    icloud_folders(imap_server)
    inbox = imap_server.folders["INBOX"]
    for i in range(5):
        inbox.add(
            make_message(subject=f"Weekly report {i}", message_id=f"<r{i}@example.com>"),
            flags={"\\Seen"} if i % 2 else set(),
        )
    inbox.add(make_message(subject="Unrelated"))

    with make_account().imap() as c:
        results = search(c, "INBOX", build_search_criteria(subject="weekly report"), limit=3)

    assert [r["subject"] for r in results] == ["Weekly report 4", "Weekly report 3", "Weekly report 2"]
    first = results[0]
    assert first["folder"] == "INBOX"
    assert first["uid"] == "5"
    assert first["message_id"] == "<r4@example.com>"
    assert first["from"] == [{"name": "Alice Example", "email": "alice@example.com"}]
    assert first["seen"] is False
    assert results[1]["seen"] is True
    assert first["size"] > 0
    # Searching must never mark mail as read.
    assert ("SELECT", "INBOX", True) in imap_server.log
    # The quoted multi-word value reached the server as a single argument.
    search_cmd = next(e for e in imap_server.log if e[:2] == ("UID", "search"))
    assert search_cmd[3:] == ("SUBJECT", '"weekly report"')
    # UID must be requested explicitly in the FETCH (Proton Bridge omits it otherwise).
    fetch_cmd = next(e for e in imap_server.log if e[:2] == ("UID", "fetch"))
    assert fetch_cmd[3].startswith("(UID ")


def test_search_no_results(imap_server):
    icloud_folders(imap_server)
    with make_account().imap() as c:
        assert search(c, "INBOX", ["ALL"]) == []


def test_search_unknown_folder(imap_server):
    icloud_folders(imap_server)
    with make_account().imap() as c, pytest.raises(RuntimeError, match="SELECT Nope failed"):
        search(c, "Nope", ["ALL"])
