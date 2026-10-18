# Changelog

All notable changes to owlpost are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-18

Tool names, argument names and defaults are unchanged; existing configs keep
working as-is.

### Added
- `password_env` and `password_command` account keys as alternatives to an
  inline `password` (exactly one of the three must be set).
  `password_command` runs without a shell, with a 10 second timeout, and its
  output is cached for the life of the server process.
- `--config PATH` and `--version` command-line flags. `OWLPOST_CONFIG` is now
  read when the config is loaded rather than at import time.
- MCP tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
  `openWorldHint`) and titles on every tool, plus full descriptions of
  every argument.
- Server instructions telling clients to confirm before sending mail.
- Clearer config errors (invalid TOML, missing keys, bad ports) via
  `ConfigError`.
- Test suite with fake IMAP/SMTP servers, ruff lint/format config, and a
  stdio smoke test.
- `server.json` for the official MCP Registry, `glama.json`, a Dockerfile
  (published to `ghcr.io/smankoo/owlpost`), and CI/release workflows.
- `py.typed` marker.
- SECURITY.md, CONTRIBUTING.md, and issue/PR templates.

### Changed
- Capped the `mcp` dependency at `<2` (now `mcp>=1.10,<2`). mcp 2.x
  removed `mcp.server.fastmcp`, so fresh installs of 0.1.x that resolve
  mcp 2.x fail at startup.
- `AccountConfig.password` may now be `None` when another password source
  is used; use `AccountConfig.resolve_password()`. `repr()` of an
  `AccountConfig` no longer includes the password.
- README rewritten: client setup for Claude Desktop, Claude Code, Cursor,
  VS Code and Docker, config reference, security notes, troubleshooting.

## [0.1.4] - 2026-05-13

### Fixed
- Import `encodings.idna` at startup so IMAP hostname encoding works in
  minimal environments ("unknown encoding: idna").

## [0.1.3] - 2026-05-08

### Fixed
- Normalize IMAP capabilities to `str` before checking for `MOVE`. The bytes
  comparison raised a `TypeError`, breaking `move_email` and `delete_email`
  for every account.

## [0.1.2] - 2026-04-20

### Added
- `save_draft_email` tool: build a message and APPEND it to the Drafts folder
  with the `\Draft` flag, without sending.
- Proton Mail Bridge support: `imap_security = "starttls"` alongside direct
  TLS, and `tls_verify = false` for self-signed local bridges.

### Fixed
- Search results missing UIDs on servers (Proton Bridge) that don't echo
  `UID` on `UID FETCH` unless asked.

## [0.1.1] - 2026-04-07

### Changed
- README: recommend `uvx` for installation and MCP client wiring.

## [0.1.0] - 2026-04-07

### Added
- Initial release: multi-account IMAP/SMTP MCP server with search, read,
  attachments, conversation following (Gmail X-GM-THRID or generic
  References BFS), send/reply/forward with threading headers, flags,
  move and delete. Special-use folder discovery and iCloud/Gmail quirk
  handling.

[Unreleased]: https://github.com/smankoo/owlpost/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/smankoo/owlpost/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/smankoo/owlpost/compare/a50a67b...v0.1.4
[0.1.3]: https://github.com/smankoo/owlpost/compare/v0.1.2...a50a67b
[0.1.2]: https://github.com/smankoo/owlpost/compare/981b841...v0.1.2
[0.1.1]: https://github.com/smankoo/owlpost/compare/d455420...981b841
[0.1.0]: https://github.com/smankoo/owlpost/tree/d455420
