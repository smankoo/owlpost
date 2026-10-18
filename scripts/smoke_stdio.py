"""Launch owlpost over stdio and check initialize + tools/list.

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

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.serverInfo.name == "owlpost", init.serverInfo
            assert init.instructions and "confirmation" in init.instructions

            tools = {t.name: t for t in (await session.list_tools()).tools}
            missing = EXPECTED_TOOLS - tools.keys()
            extra = tools.keys() - EXPECTED_TOOLS
            assert not missing and not extra, f"missing={sorted(missing)} extra={sorted(extra)}"
            for name, tool in tools.items():
                assert tool.description and tool.annotations, f"{name} lacks description/annotations"
            assert tools["delete_email"].annotations.destructiveHint is True
            assert tools["send_email"].annotations.openWorldHint is True
            assert tools["search_messages"].annotations.readOnlyHint is True

            result = await session.call_tool("list_accounts", {})
            assert not result.isError, result
            payload = json.dumps([c.model_dump() for c in result.content])
            assert "demo" in payload
            assert "smoke-test-not-a-real-password" not in payload

    print(f"ok: {len(tools)} tools: {', '.join(sorted(tools))}")


def main() -> None:
    command = sys.argv[1:] or ["owlpost"]
    asyncio.run(run(command))


if __name__ == "__main__":
    main()
