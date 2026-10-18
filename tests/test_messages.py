from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest
from conftest import FakeIMAPServer, gmail_folders, icloud_folders, make_account, make_message

from owlpost.messages import (
    _decode_header,
    download_attachment,
    move_message,
    read_message,
    set_flag,
    trash_message,
)


def _read(server, raw: bytes, **kw):
    uid = server.folders["INBOX"].add(raw)
    with make_account().imap() as c:
        return read_message(c, "INBOX", str(uid), **kw)


@pytest.fixture
def mailbox(imap_server):
    icloud_folders(imap_server)
    return imap_server


# ----- MIME parsing ----------------------------------------------------------


def test_plain_text(mailbox):
    msg = _read(mailbox, make_message(body="Hi there,\nSee you soon.\n"))
    # Mail arrives over IMAP with CRLF line endings, which the body keeps.
    assert msg["text"] == "Hi there,\r\nSee you soon.\r\n"
    assert msg["html"] is None
    assert msg["attachments"] == []
    assert msg["subject"] == "Hello"
    assert msg["date_iso"] == "2024-01-01T10:00:00+00:00"
    assert msg["text_full_length"] == len("Hi there,\r\nSee you soon.\r\n")
    # read_email must not mark the message read
    assert ("SELECT", "INBOX", True) in mailbox.log
    assert "\\Seen" not in mailbox.folders["INBOX"].messages[0].flags


def test_html_only(mailbox):
    m = EmailMessage(policy=policy.SMTP)
    m["From"] = "news@example.com"
    m["To"] = "you@example.com"
    m["Subject"] = "Newsletter"
    m["Message-ID"] = "<n@example.com>"
    m.set_content("<p>Big <b>sale</b></p>", subtype="html")

    msg = _read(mailbox, m.as_bytes())
    assert msg["text"] is None
    assert msg["html"] is None  # excluded unless asked for
    assert msg["html_full_length"] > 0

    msg = _read(mailbox, m.as_bytes(), include_html=True)
    assert "<b>sale</b>" in msg["html"]


def test_multipart_alternative_with_attachment(mailbox):
    raw = make_message(
        body="See attached.\n",
        html="<p>See attached.</p>",
        attachments=[("report.pdf", b"%PDF-1.4 fake", "application/pdf")],
    )
    msg = _read(mailbox, raw, include_html=True)
    assert msg["text"] == "See attached.\r\n"
    assert msg["html"].strip() == "<p>See attached.</p>"
    assert msg["attachments"] == [
        {"part": 3, "filename": "report.pdf", "mime": "application/pdf", "size": 13, "content_id": None}
    ]


def test_encoded_headers_and_filenames(mailbox):
    outer = MIMEMultipart()
    outer["From"] = "=?utf-8?q?Ren=C3=A9e_Exemple?= <renee@example.com>"
    outer["To"] = "you@example.com"
    outer["Subject"] = "=?utf-8?b?w4ljaMOpYW5jZSBkZSBmYWN0dXJl?="
    outer["Message-ID"] = "<enc@example.com>"
    outer.attach(MIMEText("Bonjour", "plain", "utf-8"))
    att = MIMEApplication(b"data", "octet-stream")
    att.add_header("Content-Disposition", "attachment", filename=("utf-8", "", "reçu.txt"))
    outer.attach(att)

    msg = _read(mailbox, outer.as_bytes())
    assert msg["subject"] == "Échéance de facture"
    assert msg["from"] == [{"name": "Renée Exemple", "email": "renee@example.com"}]
    assert msg["text"] == "Bonjour"
    assert msg["attachments"][0]["filename"] == "reçu.txt"


def test_decode_header_passthrough():
    assert _decode_header(None) is None
    assert _decode_header("plain") == "plain"


def test_truncation(mailbox):
    msg = _read(mailbox, make_message(body="x" * 500), body_max_chars=100)
    assert msg["text"].startswith("x" * 100)
    assert "truncated, 402 more chars" in msg["text"]  # body ends with CRLF
    assert msg["text_full_length"] == 502


def test_threading_headers_exposed(mailbox):
    raw = make_message(
        message_id="<c@example.com>",
        in_reply_to="<b@example.com>",
        references="<a@example.com> <b@example.com>",
    )
    msg = _read(mailbox, raw)
    assert msg["message_id"] == "<c@example.com>"
    assert msg["in_reply_to"] == "<b@example.com>"
    assert msg["references"] == ["<a@example.com>", "<b@example.com>"]


def test_unknown_uid_raises(mailbox):
    with make_account().imap() as c, pytest.raises(RuntimeError, match="uid=99"):
        read_message(c, "INBOX", "99")


# ----- attachments -----------------------------------------------------------


def test_download_attachment_to_directory(mailbox, tmp_path):
    uid = mailbox.folders["INBOX"].add(make_message(attachments=[("notes.txt", b"hello file", "text/plain")]))
    with make_account().imap() as c:
        info = download_attachment(c, "INBOX", str(uid), 2, str(tmp_path / "out") + "/")
    assert info["filename"] == "notes.txt"
    assert (tmp_path / "out" / "notes.txt").read_bytes() == b"hello file"
    assert info["size"] == 10


def test_download_attachment_to_file_path(mailbox, tmp_path):
    uid = mailbox.folders["INBOX"].add(make_message(attachments=[("a.bin", b"\x00\x01", "application/zip")]))
    target = tmp_path / "nested" / "renamed.zip"
    with make_account().imap() as c:
        info = download_attachment(c, "INBOX", str(uid), 2, str(target))
    assert info["path"] == str(target)
    assert target.read_bytes() == b"\x00\x01"


def test_download_missing_part(mailbox, tmp_path):
    uid = mailbox.folders["INBOX"].add(make_message())
    with make_account().imap() as c, pytest.raises(RuntimeError, match="part 5 not found"):
        download_attachment(c, "INBOX", str(uid), 5, str(tmp_path))


# ----- flags / move ----------------------------------------------------------


def test_set_flag_on_and_off(mailbox):
    uid = mailbox.folders["INBOX"].add(make_message())
    msg = mailbox.folders["INBOX"].get(uid)
    with make_account().imap() as c:
        set_flag(c, "INBOX", str(uid), "\\Flagged", True)
        assert "\\Flagged" in msg.flags
        set_flag(c, "INBOX", str(uid), "\\Flagged", False)
        assert "\\Flagged" not in msg.flags
    # Mutations must SELECT read-write.
    assert ("SELECT", "INBOX", False) in mailbox.log


def test_move_uses_move_extension(mailbox):
    uid = mailbox.folders["INBOX"].add(make_message())
    with make_account().imap() as c:
        move_message(c, "INBOX", str(uid), "Archive")
    assert mailbox.folders["INBOX"].messages == []
    assert len(mailbox.folders["Archive"].messages) == 1
    commands = [e[1] for e in mailbox.log if e[0] == "UID"]
    assert "move" in commands
    assert "copy" not in commands


@pytest.mark.parametrize("caps", [("IMAP4REV1",), (b"IMAP4REV1", b"UIDPLUS")])
def test_move_fallback_copy_store_expunge(monkeypatch, caps):
    server = FakeIMAPServer(capabilities=caps)
    monkeypatch.setattr("owlpost.account.imaplib.IMAP4_SSL", server.connect)
    icloud_folders(server)
    keep = server.folders["INBOX"].add(make_message(message_id="<keep@example.com>"))
    uid = server.folders["INBOX"].add(make_message(message_id="<go@example.com>"))

    with make_account().imap() as c:
        move_message(c, "INBOX", str(uid), "Archive")

    uid_cmds = [e for e in server.log if e[0] == "UID"]
    assert [e[1] for e in uid_cmds] == ["copy", "store"]
    assert uid_cmds[0][2:] == (str(uid), '"Archive"')
    assert uid_cmds[1][2:] == (str(uid), "+FLAGS", "(\\Deleted)")
    assert ("EXPUNGE", "INBOX") in server.log
    assert [m.uid for m in server.folders["INBOX"].messages] == [keep]
    assert server.folders["Archive"].messages[0].header("Message-ID") == "<go@example.com>"


def test_move_with_bytes_capabilities_still_uses_move(monkeypatch):
    server = FakeIMAPServer(capabilities=(b"IMAP4REV1", b"MOVE"))
    monkeypatch.setattr("owlpost.account.imaplib.IMAP4_SSL", server.connect)
    icloud_folders(server)
    uid = server.folders["INBOX"].add(make_message())
    with make_account().imap() as c:
        move_message(c, "INBOX", str(uid), "Archive")
    assert "move" in [e[1] for e in server.log if e[0] == "UID"]


def test_trash_resolves_provider_folder(imap_server):
    gmail_folders(imap_server)
    uid = imap_server.folders["INBOX"].add(make_message())
    with make_account().imap() as c:
        assert trash_message(c, "INBOX", str(uid)) == "[Gmail]/Trash"
    assert len(imap_server.folders["[Gmail]/Trash"].messages) == 1
