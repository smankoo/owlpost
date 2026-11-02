"""Launch owlpost over stdio and check the handshake, tools/list and a tool call.

Both connection modes are exercised: the pre-2026 initialize handshake most
clients still use, and the 2026-07-28 server/discover negotiation.

Usage: python scripts/smoke_stdio.py [command [args...]]   (default: owlpost)

Uses a throwaway config with a fake account, so no mail server is contacted.
The config is passed via OWLPOST_CONFIG; for containers, a literal
`{config_dir}` in the arguments is replaced with the directory holding it:

    python scripts/smoke_stdio.py docker run -i --rm \
        --mount type=bind,src={config_dir},dst=/config,readonly owlpost:dev

Exits non-zero if the handshake fails or the tool list is not what we expect.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import StdioServerParameters
from mcp.client import Client

EXPECTED_TOOLS = {
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

DUMMY_CONFIG = """\
[accounts.demo]
email = "you@example.com"
password = "smoke-test-not-a-real-password"
provider = "generic"
imap_host = "imap.invalid"
smtp_host = "smtp.invalid"
"""


async def check(params: StdioServerParameters, mode: str) -> dict:
    async with Client(params, mode=mode) as client:
        assert client.server_info is not None and client.server_info.name == "owlpost", client.server_info
        assert client.instructions and "confirmation" in client.instructions

        tools = {t.name: t for t in (await client.list_tools()).tools}
        missing = EXPECTED_TOOLS - tools.keys()
        extra = tools.keys() - EXPECTED_TOOLS
        assert not missing and not extra, f"missing={sorted(missing)} extra={sorted(extra)}"
        for name, tool in tools.items():
            assert tool.description and tool.annotations, f"{name} lacks description/annotations"
            assert tool.output_schema, f"{name} lacks an output schema"
        assert tools["delete_email"].annotations.destructive_hint is True
        assert tools["send_email"].annotations.open_world_hint is True
        assert tools["search_messages"].annotations.read_only_hint is True

        result = await client.call_tool("list_accounts", {})
        assert not result.is_error, result
        payload = json.dumps([c.model_dump() for c in result.content])
        assert "demo" in payload
        assert "smoke-test-not-a-real-password" not in payload
        assert result.structured_content["result"][0]["name"] == "demo"

        # Errors must reach the model with their real message.
        result = await client.call_tool("list_folders", {"account": "nope"})
        assert result.is_error and "Unknown account 'nope'" in result.content[0].text, result
        print(f"ok ({mode}, protocol {client.protocol_version})")
    return tools


async def run(command: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "accounts.toml"
        config.write_text(DUMMY_CONFIG)
        # Readable by a container's non-root user too.
        Path(tmp).chmod(0o755)
        config.chmod(0o644)
        args = [a.replace("{config_dir}", tmp) for a in command[1:]]
        params = StdioServerParameters(
            command=command[0],
            args=args,
            env={**os.environ, "OWLPOST_CONFIG": str(config)},
        )
        for mode in ("legacy", "auto"):
            tools = await check(params, mode)

    print(f"ok: {len(tools)} tools: {', '.join(sorted(tools))}")


def main() -> None:
    command = sys.argv[1:] or ["owlpost"]
    asyncio.run(run(command))


if __name__ == "__main__":
    main()
