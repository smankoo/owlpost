from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import icloud_folders, make_message

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


def _tools():
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def _call(name: str, args: dict):
    result = asyncio.run(server.mcp.call_tool(name, args))
    # FastMCP returns (content, structured) for tools with structured output,
    # and a list of content blocks otherwise.
    if isinstance(result, tuple):
        return result[1].get("result", result[1])
    (block,) = result
    return json.loads(block.text)


@pytest.fixture
def configured(write_config, monkeypatch):
    monkeypatch.setattr(server, "_config_path", write_config(CONFIG))


def test_tool_names_are_stable():
    assert set(_tools()) == EXPECTED


def test_argument_names_and_defaults_are_stable():
    schema = _tools()["search_messages"].inputSchema
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
    assert _tools()["read_email"].inputSchema["properties"]["body_max_chars"]["default"] == 50_000
    fwd = _tools()["forward_email"].inputSchema
    assert fwd["properties"]["include_attachments"]["default"] is True
    assert set(fwd["required"]) == {"account", "folder", "uid", "to"}


@pytest.mark.parametrize(
    "name",
    ["list_accounts", "list_folders", "resolve_folder", "search_messages", "read_email", "get_conversation"],
)
def test_read_tools_are_read_only(name):
    ann = _tools()[name].annotations
    assert ann.readOnlyHint is True
    assert ann.destructiveHint is False
    assert ann.idempotentHint is True


@pytest.mark.parametrize("name", ["delete_email", "move_email"])
def test_destructive_tools(name):
    ann = _tools()[name].annotations
    assert ann.readOnlyHint is False
    assert ann.destructiveHint is True


@pytest.mark.parametrize("name", ["send_email", "reply_email", "forward_email"])
def test_sending_tools_are_open_world(name):
    ann = _tools()[name].annotations
    assert ann.openWorldHint is True
    assert ann.readOnlyHint is False
    assert ann.idempotentHint is False


def test_every_tool_is_documented():
    for name, tool in _tools().items():
        assert tool.annotations.title, name
        assert len(tool.description) > 80, name
        for arg in tool.inputSchema.get("properties", {}):
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


def test_unknown_account_is_a_tool_error(configured):
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="Unknown account"):
        _call("list_folders", {"account": "nope"})


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
