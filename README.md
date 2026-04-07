# owlpost

A high-fidelity IMAP/SMTP MCP server for iCloud, Gmail, and other mail
providers. Built because every other email MCP server I tried got tripped up
by real-world IMAP quirks (CRLF line endings, `RFC822` vs `BODY.PEEK[]`,
provider-specific Sent folders, multi-word search values).

owlpost is the one I actually use day-to-day from Claude Code to read,
search, follow conversations, and send mail.

## Features

- **Multi-account** — configure as many mailboxes as you want, switch by name
- **iCloud and Gmail tested end-to-end**, with provider-specific quirks handled:
  - iCloud's `RFC822` fetch returns empty bodies → uses `BODY.PEEK[]`
  - iCloud's IMAP `APPEND` requires CRLF line endings → all outbound mail uses `policy.SMTP`
  - iCloud's SMTP doesn't auto-save sent mail → owlpost APPENDs to Sent for you
  - Gmail's SMTP *does* auto-save → owlpost skips the duplicate APPEND
  - Folder names auto-discovered via RFC 6154 SPECIAL-USE (no hardcoding `Sent Messages` vs `[Gmail]/Sent Mail`)
- **Conversation following** — Gmail's `X-GM-THRID` extension where available, otherwise a generic Message-ID/References BFS across folders
- **Reliable connections** — short-lived per-operation IMAP sessions instead of long-lived ones that drop randomly
- **MIME-aware** — parses multipart messages, decodes headers, lists attachments, downloads them to disk
- **Threaded reply/forward** — preserves `In-Reply-To`/`References`, quotes the original

## Tools

| Tool | What it does |
| --- | --- |
| `list_accounts` | List configured mail accounts |
| `list_folders` | List folders for an account, with detected special-use roles |
| `resolve_folder` | Resolve a role (`sent`, `trash`, `drafts`, `inbox`, `all`, `archive`) to a folder name |
| `search_messages` | Structured IMAP search (from/to/cc/subject/body/since/before/unseen/flagged/has_attachment), newest first |
| `read_email` | Read one message: headers, plaintext/HTML body, attachment list. Truncates by default to fit MCP result limits |
| `save_attachment` | Download an attachment to disk by part index |
| `get_conversation` | Return all messages in the same thread (Gmail X-GM-THRID or generic reference walking) |
| `send_email` | Send mail. Auto-saves to Sent on providers that don't (e.g. iCloud). Supports attachments, threading headers |
| `reply_email` | Reply (with optional reply-all), preserving threading and quoting |
| `forward_email` | Forward, re-attaching original attachments by default |
| `mark_read` | Set/unset `\Seen` |
| `flag_message` | Set/unset `\Flagged` (star) |
| `move_email` | Move to another folder |
| `delete_email` | Move to Trash |

## Install

```bash
pip install owlpost
# or with uv:
uv tool install owlpost
```

## Configure

Copy [`accounts.example.toml`](accounts.example.toml) to
`~/.config/owlpost/accounts.toml` and fill in your credentials. **Use
app-specific passwords**, not your real password:

- **iCloud**: [account.apple.com → Sign-In and Security → App-Specific Passwords](https://account.apple.com)
- **Gmail**: [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (2FA must be enabled)

```toml
[accounts.icloud]
email = "you@icloud.com"
password = "xxxx-xxxx-xxxx-xxxx"
provider = "icloud"
imap_host = "imap.mail.me.com"
imap_port = 993
smtp_host = "smtp.mail.me.com"
smtp_port = 587
auto_save_sent = true   # iCloud SMTP doesn't auto-save sent

[accounts.gmail]
email = "you@gmail.com"
password = "xxxx xxxx xxxx xxxx"
provider = "gmail"
imap_host = "imap.gmail.com"
imap_port = 993
smtp_host = "smtp.gmail.com"
smtp_port = 587
auto_save_sent = false  # Gmail SMTP auto-saves
```

You can override the config path with the `OWLPOST_CONFIG` environment
variable.

## Use with Claude Code

Add to `~/.claude.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "owlpost": {
      "type": "stdio",
      "command": "owlpost",
      "args": [],
      "env": {}
    }
  }
}
```

Or, if you installed with `uv tool install`, the binary will be at
`~/.local/bin/owlpost`.

Restart Claude Code and the tools will appear under the `owlpost` namespace.

## Use with Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "owlpost": {
      "command": "owlpost"
    }
  }
}
```

## Use with other MCP clients

owlpost speaks the standard MCP stdio transport — any MCP-compatible client
can talk to it by spawning the `owlpost` binary.

## Examples

Once registered, you can ask Claude things like:

- *"Find emails from priya about the kids' school in the last month"*
- *"Show me the full thread for the latest mortgage email"*
- *"Reply to the most recent message from the landlord saying I'll be in touch tomorrow"*
- *"Forward the Manulife policy PDFs to Mom and Dad with Priyanka in CC"*
- *"Save the attachment from the Toronto Hydro bill to ~/Downloads"*

## Provider notes

### iCloud

- Use an [app-specific password](https://account.apple.com), not your Apple ID password.
- iCloud throttles aggressive reconnection. owlpost uses short-lived per-op sessions but doesn't pool — if you get `SSLEOFError: EOF`, back off for ~30 seconds.
- Sent folder is `Sent Messages`, Trash is `Deleted Messages`. Both are auto-detected.

### Gmail

- Requires [2FA](https://myaccount.google.com/security) and an [app password](https://myaccount.google.com/apppasswords).
- IMAP must be enabled in [Gmail settings → Forwarding and POP/IMAP](https://mail.google.com/mail/u/0/#settings/fwdandpop).
- owlpost uses Gmail's `X-GM-THRID` extension for reliable thread detection.

### Other providers

Set `provider = "generic"` and configure `imap_host`, `smtp_host`, ports, and
`smtp_security` (`starttls` for 587 or `ssl` for 465). Folder roles will
still be auto-detected if your provider supports SPECIAL-USE (most modern
ones do).

## Development

```bash
git clone https://github.com/smankoo/owlpost
cd owlpost
uv venv
uv pip install -e .
```

Run the server directly to verify it starts:

```bash
.venv/bin/owlpost
# (waits for MCP stdio input)
```

## License

MIT — see [LICENSE](LICENSE).
