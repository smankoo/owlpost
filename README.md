# owlpost

<!-- mcp-name: io.github.smankoo/owlpost -->

[![PyPI](https://img.shields.io/pypi/v/owlpost.svg)](https://pypi.org/project/owlpost/)
[![CI](https://github.com/smankoo/owlpost/actions/workflows/ci.yml/badge.svg)](https://github.com/smankoo/owlpost/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![smankoo/owlpost MCP server](https://glama.ai/mcp/servers/smankoo/owlpost/badges/score.svg)](https://glama.ai/mcp/servers/smankoo/owlpost)

A high-fidelity IMAP/SMTP MCP server for iCloud, Gmail, and other mail
providers. Built because every other email MCP server I tried got tripped up
by real-world IMAP quirks (CRLF line endings, `RFC822` vs `BODY.PEEK[]`,
provider-specific Sent folders, multi-word search values).

owlpost is the one I actually use day-to-day from Claude Code to read,
search, follow conversations, and send mail.

```bash
uvx owlpost
```

## Contents

- [Features](#features)
- [Tools](#tools)
- [Install](#install)
- [Configure](#configure)
- [Client setup](#client-setup) — Claude Desktop, Claude Code, Cursor, VS Code, Docker
- [Provider notes](#provider-notes) — iCloud, Gmail, Proton Bridge, generic
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Features

- **Multi-account** — configure as many mailboxes as you want, switch by name
- **iCloud and Gmail tested end-to-end**, with provider-specific quirks handled:
  - iCloud's `RFC822` fetch returns empty bodies → uses `BODY.PEEK[]`
  - iCloud's IMAP `APPEND` requires CRLF line endings → all outbound mail uses `policy.SMTP`
  - iCloud's SMTP doesn't auto-save sent mail → owlpost APPENDs to Sent for you
  - Gmail's SMTP *does* auto-save → owlpost skips the duplicate APPEND
  - Folder names auto-discovered via RFC 6154 SPECIAL-USE (no hardcoding `Sent Messages` vs `[Gmail]/Sent Mail`)
  - Servers without the `MOVE` extension get COPY + `\Deleted` + EXPUNGE instead
  - Proton Bridge omits `UID` from FETCH responses unless asked → owlpost always asks
- **Conversation following** — Gmail's `X-GM-THRID` extension where available, otherwise a generic Message-ID/References BFS across folders
- **Reliable connections** — short-lived per-operation IMAP sessions instead of long-lived ones that drop randomly
- **MIME-aware** — parses multipart messages, decodes headers, lists attachments, downloads them to disk
- **Threaded reply/forward** — preserves `In-Reply-To`/`References`, quotes the original
- **Drafts** — compose into the Drafts folder so you can review and send from your own mail client
- **Read-only by default where it matters** — searching and reading never mark mail as read
- **Secrets stay out of the config file if you want** — `password_env` and `password_command` (1Password, `pass`, Keychain, ...)
- **Annotated tools** — every tool declares MCP `readOnlyHint` / `destructiveHint` / `openWorldHint`, so clients can auto-approve reads and ask before sending

## Tools

| Tool | What it does | Read-only |
| --- | --- | :---: |
| `list_accounts` | List configured mail accounts (never includes passwords) | ✅ |
| `list_folders` | List folders for an account, with detected special-use roles | ✅ |
| `resolve_folder` | Resolve a role (`sent`, `trash`, `drafts`, `inbox`, `all`, `archive`, ...) to the provider's folder name | ✅ |
| `search_messages` | Structured IMAP search (from/to/cc/subject/body/text/since/before/unseen/flagged/has_attachment, or raw), newest first | ✅ |
| `read_email` | Read one message: headers, plaintext/HTML body, attachment list. Truncates by default to fit MCP result limits | ✅ |
| `get_conversation` | Return all messages in the same thread (Gmail X-GM-THRID or generic reference walking) | ✅ |
| `save_attachment` | Download an attachment to local disk by part index (mailbox untouched) | writes local file |
| `send_email` | Send mail. Auto-saves to Sent on providers that don't (e.g. iCloud). Supports attachments, threading headers | ❌ sends |
| `save_draft_email` | Build a message and APPEND it to Drafts without sending — same parameters as `send_email` | ❌ |
| `reply_email` | Reply (with optional reply-all), preserving threading and quoting | ❌ sends |
| `forward_email` | Forward, re-attaching original attachments by default | ❌ sends |
| `mark_read` | Set/unset `\Seen` | ❌ |
| `flag_message` | Set/unset `\Flagged` (star) | ❌ |
| `move_email` | Move to another folder | ❌ destructive |
| `delete_email` | Move to Trash (not expunged) | ❌ destructive |

Every tool takes an `account` argument (the name from `list_accounts`).
Messages are addressed by `folder` + `uid`, as returned by `search_messages`.

## Install

The recommended way is to **not install at all** — let
[`uvx`](https://docs.astral.sh/uv/guides/tools/) fetch and cache it on first
use. This keeps the package always up-to-date and avoids polluting your
global Python environment:

```bash
uvx owlpost            # speaks MCP over stdio
uvx owlpost --version
```

If you'd rather have a stable binary on your `PATH`:

```bash
uv tool install owlpost   # or: pipx install owlpost / pip install owlpost
```

There's also a container image, see [Docker](#docker).

## Configure

Copy [`accounts.example.toml`](accounts.example.toml) to
`~/.config/owlpost/accounts.toml` and fill in your credentials. **Use
app-specific passwords**, not your real password:

- **iCloud**: [account.apple.com → Sign-In and Security → App-Specific Passwords](https://account.apple.com)
- **Gmail**: [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (2FA must be enabled)

```toml
[accounts.icloud]
email = "you@icloud.com"
password_command = "op read op://Private/iCloud Mail/app-password"
provider = "icloud"
imap_host = "imap.mail.me.com"
smtp_host = "smtp.mail.me.com"
auto_save_sent = true   # iCloud SMTP doesn't auto-save sent

[accounts.gmail]
email = "you@gmail.com"
password_env = "OWLPOST_GMAIL_PASSWORD"
provider = "gmail"
imap_host = "imap.gmail.com"
smtp_host = "smtp.gmail.com"
auto_save_sent = false  # Gmail SMTP auto-saves
```

The table name (`icloud`, `gmail` above) is what you pass as `account` to
the tools.

### Where the config lives

In order of precedence:

1. `owlpost --config /path/to/accounts.toml`
2. the `OWLPOST_CONFIG` environment variable
3. `~/.config/owlpost/accounts.toml`

The file is read on the first tool call; restart the server after editing it.

### Reference

| Key | Required | Default | Meaning |
| --- | :---: | --- | --- |
| `email` | ✅ | | Login username and the `From` address |
| `password` | one of three | | Password inline (app password recommended) |
| `password_env` | one of three | | Name of an environment variable holding the password |
| `password_command` | one of three | | Command that prints the password, e.g. `op read op://...` |
| `provider` | | `generic` | `icloud`, `gmail` or `generic`. `gmail` enables X-GM-THRID threading |
| `imap_host` | ✅ | | IMAP server hostname |
| `imap_port` | | `993` | IMAP port |
| `imap_security` | | `ssl` | `ssl` (implicit TLS, usually 993) or `starttls` (usually 143 / 1143) |
| `smtp_host` | ✅ | | SMTP server hostname |
| `smtp_port` | | `587` | SMTP port |
| `smtp_security` | | `starttls` | `starttls` (usually 587) or `ssl` (usually 465) |
| `auto_save_sent` | | `true` | APPEND a copy of sent mail to the Sent folder. Turn off for Gmail |
| `tls_verify` | | `true` | Verify TLS certificates. Set `false` only for local bridges with self-signed certs |

Exactly one of `password`, `password_env` and `password_command` must be set.

**`password_command`** is split with shell-like quoting rules and run
directly (no shell, so no pipes or `$VARS`), with a 10 second timeout. Its
stdout, minus the trailing newline, is the password. It runs the first time
the account is used and the result is cached for the life of the server
process. If it fails, the tool call returns an error with the command's exit
code and stderr. Some examples:

```toml
password_command = "op read op://Private/Fastmail/app-password"                  # 1Password CLI
password_command = "pass show mail/fastmail"                                     # pass
password_command = "security find-generic-password -s owlpost-icloud -w"         # macOS Keychain
password_command = "secret-tool lookup service owlpost account icloud"           # GNOME Keyring
```

**`password_env`** reads the variable from the server's environment, which
your MCP client controls (the `env` block in the snippets below).

## Client setup

All of these run `uvx owlpost`. If your client can't find `uvx`, use the
absolute path (typically `~/.local/bin/uvx`, see `which uvx`). If you
installed with `uv tool install`, you can use `owlpost` as the command
instead.

### Claude Desktop

Edit `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`,
Windows: `%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "owlpost": {
      "command": "uvx",
      "args": ["owlpost"]
    }
  }
}
```

### Claude Code

```bash
claude mcp add owlpost --scope user -- uvx owlpost

# with a non-default config path, or a password from the environment
claude mcp add owlpost --scope user \
  -e OWLPOST_CONFIG=/path/to/accounts.toml \
  -- uvx owlpost
```

### Cursor

Add to `~/.cursor/mcp.json` (or `.cursor/mcp.json` in a project):

```json
{
  "mcpServers": {
    "owlpost": {
      "command": "uvx",
      "args": ["owlpost"]
    }
  }
}
```

### VS Code

Add to `.vscode/mcp.json` in your workspace (or run **MCP: Add Server** for a
user-level install):

```json
{
  "servers": {
    "owlpost": {
      "type": "stdio",
      "command": "uvx",
      "args": ["owlpost"]
    }
  }
}
```

### Other MCP clients

owlpost speaks the standard MCP stdio transport — any MCP-compatible client
can talk to it by spawning `uvx owlpost` or the installed `owlpost` binary.
Pass `env: {"OWLPOST_CONFIG": "..."}` to point it at a config file.

### Docker

The image expects the config at `/config/accounts.toml`. Mount the directory
that contains it, read-only:

```bash
docker run -i --rm \
  --mount type=bind,src="$HOME/.config/owlpost",dst=/config,readonly \
  ghcr.io/smankoo/owlpost:latest
```

As an MCP client entry:

```json
{
  "mcpServers": {
    "owlpost": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "--mount", "type=bind,src=/home/you/.config/owlpost,dst=/config,readonly",
        "ghcr.io/smankoo/owlpost:latest"
      ]
    }
  }
}
```

Inside the container `password_command` can only run binaries that exist in
the image, so use `password` or `password_env` (pass it with
`-e OWLPOST_GMAIL_PASSWORD`). To reach Proton Bridge on the host, use
`--network host` (Linux) or `imap_host = "host.docker.internal"`.
`save_attachment` writes inside the container unless you mount a
writable directory for it.

## Examples

Once registered, you can ask things like:

- *"Find emails from the school about the field trip in the last month"*
- *"Show me the full thread for the latest mortgage email"*
- *"Reply to the most recent message from the landlord saying I'll be in touch tomorrow"*
- *"Draft a reply to the insurance renewal asking for the PDF, don't send it"*
- *"Forward the policy PDFs to you@example.com"*
- *"Save the attachment from this month's electricity bill to ~/Downloads"*

## Provider notes

### iCloud

- Use an [app-specific password](https://account.apple.com), not your Apple ID password.
- iCloud throttles aggressive reconnection. owlpost uses short-lived per-op sessions but doesn't pool — if you get `SSLEOFError: EOF`, back off for ~30 seconds.
- Sent folder is `Sent Messages`, Trash is `Deleted Messages`. Both are auto-detected.
- Keep `auto_save_sent = true`: iCloud's SMTP doesn't keep a copy of what you send.

### Gmail

- Requires [2FA](https://myaccount.google.com/security) and an [app password](https://myaccount.google.com/apppasswords).
- IMAP must be enabled in [Gmail settings → Forwarding and POP/IMAP](https://mail.google.com/mail/u/0/#settings/fwdandpop).
- Set `provider = "gmail"` so owlpost uses Gmail's `X-GM-THRID` extension for reliable thread detection.
- Set `auto_save_sent = false`; Gmail's SMTP already files sent mail, so APPENDing would duplicate it.
- `delete_email` moves to `[Gmail]/Trash`. Moving out of `INBOX` to a label folder works like archiving.

### Proton Mail (via Proton Bridge)

Proton only exposes IMAP/SMTP through the [Proton Bridge](https://proton.me/mail/bridge)
app, which runs on localhost with a self-signed certificate and speaks
STARTTLS on non-standard ports. Configure with:

- `provider = "generic"`
- `imap_host = "127.0.0.1"`, `imap_port = 1143`, `imap_security = "starttls"`
- `smtp_host = "127.0.0.1"`, `smtp_port = 1025`, `smtp_security = "starttls"`
- `tls_verify = false` — required because Bridge's cert is self-signed
- Use the Bridge-generated mailbox password (not your Proton account password)

### Other providers

Set `provider = "generic"` and configure `imap_host`, `smtp_host`, ports,
`imap_security` / `smtp_security` (`starttls` or `ssl`), and `tls_verify`
(set `false` for self-signed certs). Folder roles will still be
auto-detected if your provider supports SPECIAL-USE (most modern ones do);
otherwise owlpost falls back to common names like `Sent`, `Sent Items` and
`Trash`. Whether to keep `auto_save_sent` on depends on whether your
provider's SMTP files sent mail itself — send yourself a test and check for
duplicates.

## Security

- **owlpost sends real email.** `send_email`, `reply_email` and
  `forward_email` are annotated `openWorldHint: true` and are not
  read-only. Configure your MCP client to **ask for confirmation** before
  running them (most do by default for non-read-only tools), and don't
  auto-approve them. The server's instructions also tell the model to
  confirm recipients and content with you before sending. If you only want
  the model to prepare mail, steer it towards `save_draft_email`.
- **Untrusted content.** Email bodies are written by strangers. A message
  can contain text designed to manipulate a model ("forward the last 10
  invoices to ..."). Keep send confirmations on, especially in agents that
  read mail and can send it.
- **Use app passwords**, never your main account password. They can be
  revoked individually and don't grant account-management access.
- **Keep secrets out of the file** with `password_command` (your password
  manager) or `password_env`. If you do put a `password` in the file,
  `chmod 600 ~/.config/owlpost/accounts.toml`.
- `list_accounts` never returns passwords or password sources.
- `tls_verify = false` disables certificate checks entirely; only use it for
  a bridge on `127.0.0.1`.
- `save_attachment` and `attachments` read and write local paths as the
  user running the server.

Found a vulnerability? See [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Tools don't show up in the client | The client couldn't spawn `uvx`. Use its absolute path (`which uvx`) and restart the client. Run `uvx owlpost --version` in a terminal to check it works |
| `owlpost config not found at ...` | Create `~/.config/owlpost/accounts.toml`, or set `OWLPOST_CONFIG` / `--config` |
| `must set exactly one of password, password_env, password_command` | Remove the extra key (or add one) in that `[accounts.<name>]` table |
| `password_command exited with 1: ...` | Run the command in a terminal. Password-manager CLIs often need you to be signed in (`op signin`) or unlocked |
| `Unknown account 'x'. Known: [...]` | Use one of the listed names; it's the TOML table name, not the email address |
| `[AUTHENTICATIONFAILED]` / `535` on login | Wrong or revoked app password; for Gmail, make sure IMAP is enabled |
| `SSLEOFError: EOF` on iCloud | iCloud throttling reconnects; wait ~30 seconds |
| `CERTIFICATE_VERIFY_FAILED` with Proton Bridge | Set `tls_verify = false` for that account |
| Sent mail appears twice | Your provider already saves sent mail; set `auto_save_sent = false` |
| Sent mail missing from Sent | Set `auto_save_sent = true` |
| `unknown encoding: idna` | Upgrade to owlpost 0.1.4 or later |

## Development

```bash
git clone https://github.com/smankoo/owlpost
cd owlpost
uv sync                      # creates .venv with dev tools
uv run pytest                # tests use fake IMAP/SMTP, no network
uv run ruff check .
uv run ruff format --check .
uv run python scripts/smoke_stdio.py uv run owlpost   # stdio handshake + tools/list
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the release process and
[CHANGELOG.md](CHANGELOG.md) for history.

## License

MIT — see [LICENSE](LICENSE).
