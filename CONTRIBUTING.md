# Contributing

Thanks for helping. Bug reports with a provider name and the exact error are
the most useful thing you can send; please redact addresses and hostnames.

## Development setup

```bash
git clone https://github.com/smankoo/owlpost
cd owlpost
uv sync
uv run pytest -q
uv run ruff check .
uv run ruff format .
uv run python scripts/smoke_stdio.py uv run owlpost
```

Tests run against fake IMAP/SMTP servers (`tests/conftest.py`) and must not
touch the network. When you fix a provider quirk, add a fake that
reproduces it.

## Ground rules

- **Don't rename tools or their arguments, or change defaults.** People
  have clients and prompts wired to them. Add new optional arguments
  instead.
- Keep runtime dependencies to `mcp` only.
- Tool docstrings are what MCP clients and registries show: one paragraph
  on what the tool does, plus an `Args:` entry for every argument.
- Update `CHANGELOG.md` under *Unreleased*.

## Releasing

1. Bump the version in `pyproject.toml`, `src/owlpost/__init__.py` and
   `server.json` (top-level `version`, the PyPI package `version`, and the
   OCI image tag). `tests/test_server.py` checks they agree.
2. Move the *Unreleased* changelog entries under the new version.
3. Commit, then tag and push: `git tag v0.2.0 && git push origin main v0.2.0`.

The `Release` workflow checks the versions match the tag, runs the tests,
publishes to PyPI (trusted publishing, `pypi` environment), pushes
`ghcr.io/smankoo/owlpost:<version>` and `:latest` for amd64/arm64, and
publishes `server.json` to the MCP Registry.

If PyPI trusted publishing isn't set up yet, run the workflow manually on
the tag with `skip_pypi` checked: the registry entry is then published with
only the OCI package.
