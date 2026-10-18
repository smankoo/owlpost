# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them
privately through GitHub's security advisories:
[Report a vulnerability](https://github.com/smankoo/owlpost/security/advisories/new).

Include what you found, how to reproduce it, and the impact you expect. You
should get an acknowledgement within a week. Fixes are released as a new
version on PyPI and ghcr.io, and credited in the advisory unless you'd
rather stay anonymous.

## Supported versions

Only the latest release receives security fixes.

## Scope

owlpost holds mail credentials and can send email on the user's behalf, so
these areas are in scope:

- **Credential handling**: leaking passwords through tool results, logs,
  error messages or `repr()`; `password_env` / `password_command` resolution
  (for example command injection, or running anything other than the
  configured command); TLS verification being weaker than configured.
- **Sending**: mail being sent, replied to or forwarded to recipients other
  than the ones the tool was called with (header injection, Bcc leaking
  into headers, reply-all including unexpected addresses); a tool that's
  declared read-only modifying the mailbox.
- **Local files**: `save_attachment` or `attachments` reading or writing
  outside the path they were given.

Out of scope: an MCP client or model choosing to call a sending tool (that's
what client-side confirmation is for, see the README's Security section),
vulnerabilities in your mail provider, and issues that need an attacker who
can already edit your `accounts.toml`.
