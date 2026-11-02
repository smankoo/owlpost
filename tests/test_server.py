from __future__ import annotations

import asyncio
import imaplib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import icloud_folders, make_message
from mcp.client import Client

import owlpost
from owlpost import server

REPO = Path(__file__).resolve().parent.parent

EXPECTED = {
    "list_accounts",
    "list_folders",
    "resolve_folder",
    "search_messages",
    "read_email",
    "save_attachment",
    "get_conversation",
    "send_email",
    "save_draft_email",
    "reply_email",
    "forward_email",
    "mark_read",
    "flag_message",
    "move_email",
    "delete_email",
}

CONFIG = """
[accounts.icloud]
email = "you@icloud.com"
password = "super-secret-app-password"
provider = "icloud"
imap_host = "imap.mail.me.com"
smtp_host = "smtp.mail.me.com"

[accounts.vault]
email = "you@example.com"
password_command = "op read op://Private/mail/password"
imap_host = "imap.example.com"
smtp_host = "smtp.example.com"
auto_save_sent = false
"""


async def _with_client(fn):
    async with Client(server.mcp) as client:
        return await fn(client)


def _tools():
    return {t.name: t for t in asyncio.run(_with_client(lambda c: c.list_tools())).tools}


def _call_result(name: str, args: dict):
    return asyncio.run(_with_client(lambda c: c.call_tool(name, args)))


def _call(name: str, args: dict):
    result = _call_result(name, args)
    assert not result.is_error, result.content
    structured = result.structured_content
    # Lists and scalars are wrapped as {"result": ...}; objects are returned as-is.
    data = structured["result"] if set(structured) == {"result"} else structured
    # The text clients show the model must carry the same data (lists are
    # sent as one text block per item).
    if isinstance(data, str):
        assert [block.text for block in result.content] == [data]
    else:
        texts = [json.loads(block.text) for block in result.content]
        assert texts == (data if isinstance(data, list) else [data])
    return data


@pytest.fixture
def configured(write_config, monkeypatch):
    monkeypatch.setattr(server, "_config_path", write_config(CONFIG))


def test_tool_names_are_stable():
    assert set(_tools()) == EXPECTED


def test_argument_names_and_defaults_are_stable():
    schema = _tools()["search_messages"].input_schema
    props = schema["properties"]
    assert list(props) == [
        "account",
        "folder",
        "from_",
        "to",
        "cc",
        "subject",
        "body",
        "text",
        "since",
        "before",
        "unseen",
        "seen",
        "flagged",
        "has_attachment",
        "raw",
        "limit",
    ]
    assert schema["required"] == ["account"]
    assert props["folder"]["default"] == "INBOX"
    assert props["limit"]["default"] == 50
    assert _tools()["read_email"].input_schema["properties"]["body_max_chars"]["default"] == 50_000
    fwd = _tools()["forward_email"].input_schema
    assert fwd["properties"]["include_attachments"]["default"] is True
    assert set(fwd["required"]) == {"account", "folder", "uid", "to"}


@pytest.mark.parametrize(
    "name",
    ["list_accounts", "list_folders", "resolve_folder", "search_messages", "read_email", "get_conversation"],
)
def test_read_tools_are_read_only(name):
    ann = _tools()[name].annotations
    assert ann.read_only_hint is True
    assert ann.destructive_hint is False
    assert ann.idempotent_hint is True


@pytest.mark.parametrize("name", ["delete_email", "move_email"])
def test_destructive_tools(name):
    ann = _tools()[name].annotations
    assert ann.read_only_hint is False
    assert ann.destructive_hint is True


@pytest.mark.parametrize("name", ["send_email", "reply_email", "forward_email"])
def test_sending_tools_are_open_world(name):
    ann = _tools()[name].annotations
    assert ann.open_world_hint is True
    assert ann.read_only_hint is False
    assert ann.idempotent_hint is False


def test_every_tool_is_documented():
    for name, tool in _tools().items():
        assert tool.annotations.title, name
        assert len(tool.description) > 80, name
        for arg in tool.input_schema.get("properties", {}):
            assert f"{arg}:" in tool.description, f"{name}: argument {arg} is not explained"


def test_list_accounts_never_returns_passwords(configured):
    accounts = _call("list_accounts", {})
    assert [a["name"] for a in accounts] == ["icloud", "vault"]
    blob = json.dumps(accounts)
    assert "super-secret-app-password" not in blob
    assert "op read" not in blob
    assert "password" not in blob
    assert accounts[0] == {
        "name": "icloud",
        "email": "you@icloud.com",
        "provider": "icloud",
        "imap_host": "imap.mail.me.com",
        "smtp_host": "smtp.mail.me.com",
        "auto_save_sent": True,
    }


def test_tools_end_to_end_through_mcp(configured, imap_server):
    icloud_folders(imap_server)
    uid = imap_server.folders["INBOX"].add(make_message(subject="Quarterly numbers"))

    found = _call("search_messages", {"account": "icloud", "subject": "quarterly"})
    assert [m["uid"] for m in found] == [str(uid)]

    assert _call("resolve_folder", {"account": "icloud", "role": "sent"}) == "Sent Messages"
    _call("mark_read", {"account": "icloud", "folder": "INBOX", "uid": str(uid)})
    assert "\\Seen" in imap_server.folders["INBOX"].get(uid).flags

    moved = _call("delete_email", {"account": "icloud", "folder": "INBOX", "uid": str(uid)})
    assert moved == {"uid": str(uid), "from": "INBOX", "to": "Deleted Messages"}
    assert imap_server.logins[0] == ("you@icloud.com", "super-secret-app-password")


def test_unknown_account_is_a_readable_tool_error(configured):
    result = _call_result("list_folders", {"account": "nope"})
    assert result.is_error
    # mcp hides the text of unexpected exceptions; owlpost must pass its own through.
    assert "Unknown account 'nope'. Known: ['icloud', 'vault']" in result.content[0].text


def test_version_is_consistent():
    import tomllib

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    server_json = json.loads((REPO / "server.json").read_text())
    assert owlpost.__version__ == pyproject["project"]["version"] == server_json["version"]
    for pkg in server_json["packages"]:
        if pkg["registryType"] == "pypi":
            assert pkg["version"] == owlpost.__version__
        else:
            assert pkg["identifier"].endswith(":" + owlpost.__version__)


def test_readme_has_registry_marker():
    assert "<!-- mcp-name: io.github.smankoo/owlpost -->" in (REPO / "README.md").read_text()


def test_cli_version():
    out = subprocess.run(
        [sys.executable, "-m", "owlpost.server", "--version"], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == f"owlpost {owlpost.__version__}"


def test_cli_config_flag(write_config, monkeypatch):
    path = write_config(CONFIG)
    monkeypatch.setattr(server.mcp, "run", lambda: None)
    server.main(["--config", str(path)])
    assert server._config_path == path
    assert [a["name"] for a in _call("list_accounts", {})] == ["icloud", "vault"]


def test_stdio_smoke():
    """Spawn the real server over stdio (no network: only list_accounts is called)."""
    subprocess.run(
        [sys.executable, str(REPO / "scripts" / "smoke_stdio.py"), sys.executable, "-m", "owlpost.server"],
        check=True,
        timeout=60,
    )


# ----- every tool through MCP, validated against its output schema ----------


def test_every_tool_round_trips_through_its_output_schema(configured, imap_server, smtp, tmp_path):
    """_call() asserts structured output == text output, so a key missing from
    a models.py TypedDict (and silently dropped by validation) fails here."""
    icloud_folders(imap_server)
    inbox = imap_server.folders["INBOX"]
    uid = str(
        inbox.add(
            make_message(
                subject="Invoice",
                message_id="<inv@example.com>",
                attachments=[("invoice.pdf", b"%PDF-1.4", "application/pdf")],
            ),
            flags={"\\Seen"},
        )
    )
    inbox.add(
        make_message(subject="Re: Invoice", message_id="<r@example.com>", in_reply_to="<inv@example.com>")
    )
    acct = {"account": "icloud"}
    at = {**acct, "folder": "INBOX", "uid": uid}

    folders = _call("list_folders", acct)
    assert {
        "name": "Sent Messages",
        "flags": "\\HasNoChildren \\Sent",
        "delimiter": "/",
        "role": "sent",
    } in folders

    (env,) = _call("search_messages", {**acct, "subject": "Re: Invoice"})
    assert env["in_reply_to"] == "<inv@example.com>" and env["size"] > 0

    msg = _call("read_email", at)
    assert msg["seen"] is True and msg["attachments"][0]["filename"] == "invoice.pdf"
    part = msg["attachments"][0]["part"]

    saved = _call("save_attachment", {**at, "part": part, "save_to": str(tmp_path) + "/"})
    assert saved["filename"] == "invoice.pdf" and (tmp_path / "invoice.pdf").read_bytes() == b"%PDF-1.4"

    thread = _call("get_conversation", {**acct, "message_id": "<inv@example.com>"})
    assert [m["message_id"] for m in thread] == ["<inv@example.com>", "<r@example.com>"]

    sent = _call("send_email", {**acct, "to": ["bob@example.com"], "subject": "Hi", "body": "Hello"})
    assert sent["saved_to_folder"] == "Sent Messages" and sent["refused_recipients"] == []
    assert "warning" not in sent
    draft = _call("save_draft_email", {**acct, "to": ["bob@example.com"], "subject": "Later", "body": "..."})
    assert draft["saved_to_folder"] == "Drafts"
    assert _call("reply_email", {**at, "body": "Paid."})["to"] == ["alice@example.com"]
    fwd = _call("forward_email", {**at, "to": ["acct@example.com"]})
    assert fwd["subject"] == "Fwd: Invoice"

    assert _call("mark_read", {**at, "read": False}) == {"uid": uid, "folder": "INBOX", "seen": False}
    assert _call("flag_message", at) == {"uid": uid, "folder": "INBOX", "flagged": True}
    moved = _call("move_email", {**at, "dest_folder": "Archive"})
    assert moved == {"uid": uid, "from": "INBOX", "to": "Archive"}


def test_tool_calls_reuse_one_imap_login(configured, imap_server):
    icloud_folders(imap_server)
    imap_server.folders["INBOX"].add(make_message())

    async def session(client):
        for _ in range(3):
            await client.call_tool("search_messages", {"account": "icloud"})

    asyncio.run(_with_client(session))
    assert imap_server.connections == 1


# ----- errors and retries ----------------------------------------------------


def test_read_only_tool_retries_a_dropped_connection(configured, imap_server):
    icloud_folders(imap_server)
    imap_server.folders["INBOX"].add(make_message(subject="Still here"))
    imap_server.fail_next = imaplib.IMAP4.abort("socket error: EOF")
    (found,) = _call("search_messages", {"account": "icloud"})
    assert found["subject"] == "Still here"
    assert imap_server.connections == 2


def test_non_idempotent_tool_is_never_retried(configured, imap_server):
    icloud_folders(imap_server)
    uid = imap_server.folders["INBOX"].add(make_message())
    imap_server.fail_next = imaplib.IMAP4.abort("socket error: EOF")
    result = _call_result(
        "move_email", {"account": "icloud", "folder": "INBOX", "uid": str(uid), "dest_folder": "Archive"}
    )
    assert result.is_error
    assert "Lost the connection to the mail server" in result.content[0].text
    assert imap_server.connections == 1
    assert len([e for e in imap_server.log if e[:2] == ("UID", "move")]) == 1


def test_login_failure_reads_cleanly(configured, imap_server):
    icloud_folders(imap_server)
    imap_server.reject_login = True
    result = _call_result("list_folders", {"account": "icloud"})
    assert result.is_error
    assert "IMAP error: [AUTHENTICATIONFAILED] Authentication failed." in result.content[0].text
    assert "b'" not in result.content[0].text


def test_timeout_reads_cleanly(configured, imap_server):
    icloud_folders(imap_server)
    imap_server.fail_next = TimeoutError("timed out")
    result = _call_result("search_messages", {"account": "icloud"})
    assert result.is_error
    assert "did not respond within 30s" in result.content[0].text
    # Timeouts are not retried: that would double the wait.
    assert imap_server.connections == 1


def test_bad_folder_names_the_problem(configured, imap_server):
    icloud_folders(imap_server)
    result = _call_result("search_messages", {"account": "icloud", "folder": "Nope"})
    assert result.is_error
    assert "SELECT Nope failed" in result.content[0].text


def test_server_identity(configured):
    async def info(client):
        return client.server_info

    server_info = asyncio.run(_with_client(info))
    assert server_info.name == "owlpost"
    assert server_info.version == owlpost.__version__
