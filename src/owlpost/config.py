"""Account configuration loading."""

from __future__ import annotations

import os
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "owlpost" / "accounts.toml"

# Kept for backwards compatibility: the path resolved at import time.
CONFIG_PATH = Path(os.environ.get("OWLPOST_CONFIG") or DEFAULT_CONFIG_PATH)

# How long a password_command may run before we give up.
PASSWORD_COMMAND_TIMEOUT = 10

_REQUIRED_KEYS = ("email", "imap_host", "smtp_host")
_PASSWORD_KEYS = ("password", "password_env", "password_command")

# password_command results, cached per process so that a reply (which opens
# several IMAP/SMTP sessions) doesn't re-run the secret manager each time.
_command_cache: dict[str, str] = {}


class ConfigError(ValueError):
    """The config file exists but is malformed or incomplete."""


def config_path() -> Path:
    """Return the config path, honouring OWLPOST_CONFIG at call time."""
    return Path(os.environ.get("OWLPOST_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()


def _run_password_command(account: str, command: str) -> str:
    if command in _command_cache:
        return _command_cache[command]
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise ConfigError(f"account '{account}': password_command could not be parsed: {e}") from e
    if not argv:
        raise ConfigError(f"account '{account}': password_command is empty")
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PASSWORD_COMMAND_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as e:
        raise ConfigError(f"account '{account}': password_command not found: {argv[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise ConfigError(
            f"account '{account}': password_command timed out after {PASSWORD_COMMAND_TIMEOUT}s"
        ) from e
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise ConfigError(f"account '{account}': password_command exited with {proc.returncode}{detail}")
    secret = proc.stdout.rstrip("\r\n")
    if not secret:
        raise ConfigError(f"account '{account}': password_command produced no output")
    _command_cache[command] = secret
    return secret


@dataclass(frozen=True)
class AccountConfig:
    """Static configuration for a single mail account."""

    name: str
    email: str
    password: str | None
    provider: str  # "icloud" | "gmail" | "generic"
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    auto_save_sent: bool = True
    # SMTP socket type: "starttls" (587) or "ssl" (465)
    smtp_security: str = "starttls"
    # IMAP socket type: "ssl" (993, direct TLS) or "starttls" (143/1143, upgrade)
    imap_security: str = "ssl"
    # Set False for local bridges with self-signed certs (e.g. Proton Bridge).
    tls_verify: bool = True
    # Alternatives to an inline password; exactly one of the three is set.
    password_env: str | None = field(default=None, repr=False)
    password_command: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:  # never leak the password into logs/tracebacks
        return f"AccountConfig(name={self.name!r}, email={self.email!r}, provider={self.provider!r})"

    @property
    def is_gmail(self) -> bool:
        return self.provider == "gmail"

    def resolve_password(self) -> str:
        """Return the account password from whichever source is configured."""
        if self.password is not None:
            return self.password
        if self.password_env is not None:
            value = os.environ.get(self.password_env)
            if not value:
                raise ConfigError(
                    f"account '{self.name}': environment variable {self.password_env} is not set or empty"
                )
            return value
        if self.password_command is not None:
            return _run_password_command(self.name, self.password_command)
        raise ConfigError(f"account '{self.name}': no password configured")


@dataclass
class Config:
    accounts: dict[str, AccountConfig] = field(default_factory=dict)

    def get(self, name: str) -> AccountConfig:
        if name not in self.accounts:
            raise KeyError(f"Unknown account '{name}'. Known: {sorted(self.accounts)}")
        return self.accounts[name]


def _parse_account(name: str, raw: object) -> AccountConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"[accounts.{name}] must be a table")
    missing = [k for k in _REQUIRED_KEYS if k not in raw]
    if missing:
        raise ConfigError(f"[accounts.{name}] is missing required key(s): {', '.join(missing)}")
    sources = [k for k in _PASSWORD_KEYS if raw.get(k) is not None]
    if len(sources) != 1:
        found = ", ".join(sources) if sources else "none"
        raise ConfigError(
            f"[accounts.{name}] must set exactly one of password, password_env, "
            f"password_command (found: {found})"
        )
    for key in sources:
        if not isinstance(raw[key], str):
            raise ConfigError(f"[accounts.{name}] {key} must be a string")
    try:
        imap_port = int(raw.get("imap_port", 993))
        smtp_port = int(raw.get("smtp_port", 587))
    except (TypeError, ValueError) as e:
        raise ConfigError(f"[accounts.{name}] ports must be integers") from e
    return AccountConfig(
        name=name,
        email=raw["email"],
        password=raw.get("password"),
        password_env=raw.get("password_env"),
        password_command=raw.get("password_command"),
        provider=raw.get("provider", "generic"),
        imap_host=raw["imap_host"],
        imap_port=imap_port,
        smtp_host=raw["smtp_host"],
        smtp_port=smtp_port,
        auto_save_sent=bool(raw.get("auto_save_sent", True)),
        smtp_security=raw.get("smtp_security", "starttls"),
        imap_security=raw.get("imap_security", "ssl"),
        tls_verify=bool(raw.get("tls_verify", True)),
    )


def load_config(path: Path | str | None = None) -> Config:
    """Load accounts from `path`, or from OWLPOST_CONFIG / the default path."""
    path = Path(path).expanduser() if path is not None else config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"owlpost config not found at {path}. Create it with [accounts.<name>] sections."
        )
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"owlpost config at {path} is not valid TOML: {e}") from e

    accounts = data.get("accounts") or {}
    if not isinstance(accounts, dict):
        raise ConfigError("'accounts' must be a table of [accounts.<name>] sections")
    cfg = Config()
    for name, raw in accounts.items():
        cfg.accounts[name] = _parse_account(name, raw)
    return cfg
