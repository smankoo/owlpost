from __future__ import annotations

from conftest import gmail_folders, icloud_folders, make_account, make_message

from owlpost.threads import get_thread


def _date(day: int) -> str:
    return f"Mon, {day:02d} Jan 2024 10:00:00 +0000"


def _seed_generic_thread(server):
    icloud_folders(server)
    inbox = server.folders["INBOX"]
    sent = server.folders["Sent Messages"]
    a = inbox.add(make_message(subject="Plans", message_id="<a@example.com>", date=_date(1)))
    sent.add(
        make_message(
            subject="Re: Plans",
            from_="you@example.com",
            to="alice@example.com",
            message_id="<b@example.com>",
            in_reply_to="<a@example.com>",
            references="<a@example.com>",
            date=_date(2),
        )
    )
    c = inbox.add(
        make_message(
            subject="Re: Plans",
            message_id="<c@example.com>",
            in_reply_to="<b@example.com>",
            references="<a@example.com> <b@example.com>",
            date=_date(3),
        )
    )
    inbox.add(make_message(subject="Other", message_id="<z@example.com>", date=_date(4)))
    return a, c


def test_generic_bfs_from_latest_message_id(imap_server):
    _seed_generic_thread(imap_server)
    thread = get_thread(make_account(), message_id="<c@example.com>")
    assert [m["message_id"] for m in thread] == ["<a@example.com>", "<b@example.com>", "<c@example.com>"]
    assert [m["folder"] for m in thread] == ["INBOX", "Sent Messages", "INBOX"]


def test_generic_bfs_from_root_folder_uid(imap_server):
    a, _ = _seed_generic_thread(imap_server)
    thread = get_thread(make_account(), folder="INBOX", uid=str(a))
    # Walks forward from the root through replies (References / In-Reply-To searches).
    assert [m["message_id"] for m in thread] == ["<a@example.com>", "<b@example.com>", "<c@example.com>"]
    assert all(m["message_id"] != "<z@example.com>" for m in thread)


def test_generic_unknown_message_returns_just_nothing(imap_server):
    _seed_generic_thread(imap_server)
    assert get_thread(make_account(), message_id="<nope@example.com>") == []
    assert get_thread(make_account()) == []


def test_generic_never_uses_gmail_extension(imap_server):
    _seed_generic_thread(imap_server)
    get_thread(make_account(), message_id="<a@example.com>")
    assert not any("X-GM-THRID" in str(e) for e in imap_server.log)


def _seed_gmail_thread(server):
    gmail_folders(server)
    all_mail = server.folders["[Gmail]/All Mail"]
    all_mail.add(make_message(subject="Trip", message_id="<g1@example.com>", date=_date(1)), thrid="111")
    all_mail.add(make_message(subject="Unrelated", message_id="<x@example.com>", date=_date(2)), thrid="222")
    # Gmail keeps threads even when subjects/references are broken.
    all_mail.add(
        make_message(subject="Fwd: tickets", message_id="<g3@example.com>", date=_date(5)), thrid="111"
    )
    all_mail.add(make_message(subject="Re: Trip", message_id="<g2@example.com>", date=_date(3)), thrid="111")
    return server.folders["INBOX"].add(make_message(subject="Re: Trip", message_id="<g2@example.com>"))


def test_gmail_thread_by_message_id(imap_server):
    _seed_gmail_thread(imap_server)
    thread = get_thread(make_account(provider="gmail"), message_id="<g2@example.com>")
    assert [m["message_id"] for m in thread] == ["<g1@example.com>", "<g2@example.com>", "<g3@example.com>"]
    assert {m["folder"] for m in thread} == {"[Gmail]/All Mail"}
    searches = [e for e in imap_server.log if e[:2] == ("UID", "search")]
    assert searches[-1][3:] == ("X-GM-THRID", "111")
    # The References graph is not walked on Gmail.
    assert not any("References" in e for e in searches)


def test_gmail_thread_by_folder_uid_translates_to_all_mail(imap_server):
    inbox_uid = _seed_gmail_thread(imap_server)
    thread = get_thread(make_account(provider="gmail"), folder="INBOX", uid=str(inbox_uid))
    assert [m["subject"] for m in thread] == ["Trip", "Re: Trip", "Fwd: tickets"]


def test_gmail_unknown_seed(imap_server):
    _seed_gmail_thread(imap_server)
    assert get_thread(make_account(provider="gmail"), message_id="<nope@example.com>") == []
