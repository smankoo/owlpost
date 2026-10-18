from __future__ import annotations

import email
import re
from email import policy

import pytest
from conftest import gmail_folders, icloud_folders, make_account, make_message

from owlpost.sending import forward_message, reply_message, save_draft, send_message


def _parse(raw: bytes):
    return email.message_from_bytes(raw, policy=policy.default)


def _appends(server):
    return [e for e in server.log if e[0] == "APPEND"]


@pytest.fixture
def icloud(imap_server, smtp):
    icloud_folders(imap_server)
    return imap_server


def test_send_uses_crlf_and_saves_to_sent(icloud, smtp):
    result = send_message(
        make_account(),
        to=["bob@example.com"],
        cc=["carol@example.com"],
        bcc=["hidden@example.com"],
        subject="Lunch?",
        body="Line one\nLine two\n",
    )

    (conn,) = smtp.instances
    assert conn.starttls_called
    assert conn.login_args == ("you@example.com", "app-password")
    assert conn.quit_called
    (sent,) = conn.sent
    assert sent["from_addr"] == "you@example.com"
    assert sent["to_addrs"] == ["bob@example.com", "carol@example.com", "hidden@example.com"]

    raw = sent["msg"].as_bytes()
    assert b"\r\n" in raw
    assert re.search(rb"(?<!\r)\n", raw) is None, "bare LF would break iCloud APPEND"
    assert b"Bcc" not in raw
    assert sent["msg"]["Message-ID"].endswith("@example.com>")

    (append,) = _appends(icloud)
    assert append[1:3] == ("Sent Messages", "\\Seen")
    assert append[3] == raw
    assert result["saved_to_folder"] == "Sent Messages"
    assert result["message_id"] == sent["msg"]["Message-ID"]
    assert result["bcc"] == ["hidden@example.com"]


def test_send_without_auto_save_never_touches_imap(imap_server, smtp):
    gmail_folders(imap_server)
    result = send_message(
        make_account(provider="gmail", auto_save_sent=False),
        to=["bob@example.com"],
        subject="Hi",
        body="Body",
    )
    assert result["saved_to_folder"] is None
    assert imap_server.connections == 0
    assert len(smtp.instances[0].sent) == 1


def test_send_over_implicit_tls(icloud, smtp):
    send_message(
        make_account(smtp_security="ssl", smtp_port=465), to=["b@example.com"], subject="s", body="b"
    )
    assert smtp.instances[0].port == 465
    assert not smtp.instances[0].starttls_called


def test_send_html_and_attachment(icloud, smtp, tmp_path):
    f = tmp_path / "invoice.pdf"
    f.write_bytes(b"%PDF-1.4 data")
    send_message(
        make_account(),
        to=["b@example.com"],
        subject="Invoice",
        body="Plain",
        html="<p>Rich</p>",
        attachments=[str(f)],
    )
    msg = _parse(smtp.instances[0].sent[0]["msg"].as_bytes())
    assert msg.get_body(("plain",)).get_content().strip() == "Plain"
    assert msg.get_body(("html",)).get_content().strip() == "<p>Rich</p>"
    (att,) = list(msg.iter_attachments())
    assert att.get_filename() == "invoice.pdf"
    assert att.get_content_type() == "application/pdf"
    assert att.get_content() == b"%PDF-1.4 data"


def test_send_threading_headers(icloud, smtp):
    send_message(
        make_account(),
        to=["b@example.com"],
        subject="Re: x",
        body="b",
        in_reply_to="<p@example.com>",
        references=["<root@example.com>"],
    )
    msg = smtp.instances[0].sent[0]["msg"]
    assert msg["In-Reply-To"] == "<p@example.com>"
    assert msg["References"] == "<root@example.com> <p@example.com>"


def test_send_references_not_duplicated(icloud, smtp):
    send_message(
        make_account(),
        to=["b@example.com"],
        subject="Re: x",
        body="b",
        in_reply_to="<p@example.com>",
        references=["<root@example.com>", "<p@example.com>"],
    )
    assert smtp.instances[0].sent[0]["msg"]["References"] == "<root@example.com> <p@example.com>"


# ----- reply / forward -------------------------------------------------------


def _original(server, **kw):
    defaults = dict(
        subject="Budget",
        from_="Alice Example <alice@example.com>",
        to="you@example.com, dave@example.com",
        cc="You Again <YOU@example.com>, erin@example.com",
        message_id="<orig@example.com>",
        references="<root@example.com>",
        body="Can you check the numbers?\nThanks\n",
    )
    defaults.update(kw)
    return server.folders["INBOX"].add(make_message(**defaults))


def test_reply_sets_threading_and_quotes(icloud, smtp):
    uid = _original(icloud)
    result = reply_message(make_account(), folder="INBOX", uid=str(uid), body="Looks good.")

    msg = smtp.instances[0].sent[0]["msg"]
    assert msg["Subject"] == "Re: Budget"
    assert msg["To"] == "alice@example.com"
    assert msg["Cc"] is None
    assert msg["In-Reply-To"] == "<orig@example.com>"
    assert msg["References"] == "<root@example.com> <orig@example.com>"
    text = msg.get_content()
    assert text.startswith("Looks good.")
    assert "alice@example.com wrote:" in text
    assert "> Can you check the numbers?" in text
    assert result["saved_to_folder"] == "Sent Messages"


def test_reply_all_excludes_self_case_insensitively(icloud, smtp):
    uid = _original(icloud)
    reply_message(make_account(), folder="INBOX", uid=str(uid), body="ok", reply_all=True)
    sent = smtp.instances[0].sent[0]
    assert sent["msg"]["Cc"] == "dave@example.com, erin@example.com"
    assert sent["to_addrs"] == ["alice@example.com", "dave@example.com", "erin@example.com"]


def test_reply_does_not_stack_re_prefix(icloud, smtp):
    uid = _original(icloud, subject="RE: Budget")
    reply_message(make_account(), folder="INBOX", uid=str(uid), body="ok")
    assert smtp.instances[0].sent[0]["msg"]["Subject"] == "RE: Budget"


def test_forward_reattaches_original_attachments(icloud, smtp):
    uid = _original(icloud, attachments=[("q3.csv", b"a,b\n1,2\n", "text/csv")])
    forward_message(make_account(), folder="INBOX", uid=str(uid), to=["frank@example.com"], body="FYI")
    msg = _parse(smtp.instances[0].sent[0]["msg"].as_bytes())
    assert msg["Subject"] == "Fwd: Budget"
    assert msg["To"] == "frank@example.com"
    assert "---------- Forwarded message ----------" in msg.get_body(("plain",)).get_content()
    (att,) = list(msg.iter_attachments())
    assert att.get_filename() == "q3.csv"
    assert att.get_content().replace("\r\n", "\n") == "a,b\n1,2\n"


def test_forward_without_attachments(icloud, smtp):
    uid = _original(icloud, subject="Fw: Budget", attachments=[("q3.csv", b"1", "text/csv")])
    forward_message(
        make_account(), folder="INBOX", uid=str(uid), to=["frank@example.com"], include_attachments=False
    )
    msg = _parse(smtp.instances[0].sent[0]["msg"].as_bytes())
    assert msg["Subject"] == "Fw: Budget"
    assert list(msg.iter_attachments()) == []


# ----- drafts ----------------------------------------------------------------


def test_save_draft_appends_to_drafts_and_sends_nothing(icloud, smtp):
    result = save_draft(
        make_account(),
        to=["bob@example.com"],
        subject="Draft me",
        body="Not yet",
        in_reply_to="<p@example.com>",
    )
    assert smtp.instances == []
    (append,) = _appends(icloud)
    assert append[1:3] == ("Drafts", "\\Draft")
    stored = _parse(append[3])
    assert stored["Subject"] == "Draft me"
    assert stored["In-Reply-To"] == "<p@example.com>"
    assert re.search(rb"(?<!\r)\n", append[3]) is None
    assert result["saved_to_folder"] == "Drafts"
    assert icloud.folders["Drafts"].messages[0].flags == {"\\Draft"}


def test_save_draft_gmail_folder(imap_server, smtp):
    gmail_folders(imap_server)
    result = save_draft(make_account(provider="gmail"), to=["b@example.com"], subject="s", body="b")
    assert result["saved_to_folder"] == "[Gmail]/Drafts"


def test_save_draft_without_drafts_folder(imap_server, smtp):
    imap_server.add_folder("INBOX")
    with pytest.raises(RuntimeError, match="No Drafts folder"):
        save_draft(make_account(), to=["b@example.com"], subject="s", body="b")
