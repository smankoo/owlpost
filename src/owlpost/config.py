"""Account configuration loading."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


CONFIG_PATH = Path(
    os.environ.get("OWLPOST_CONFIG")
    or (Path.home() / ".config" / "owlpost" / "accounts.toml")
)


@dataclass(frozen=True)
class AccountConfig:
    """Static configuration for a single mail account."""

    name: str
    email: str
    password: str
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

    @property
    def is_gmail(self) -> bool:
        return self.provider == "gmail"


@dataclass
class Config:
    accounts: dict[str, AccountConfig] = field(default_factory=dict)

    def get(self, name: str) -> AccountConfig:
        if name not in self.accounts:
            raise KeyError(
                f"Unknown account '{name}'. Known: {sorted(self.accounts)}"
            )
        return self.accounts[name]


def load_config(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        raise FileNotFoundError(
            f"owlpost config not found at {path}. "
            "Create it with [accounts.<name>] sections."
        )
    with path.open("rb") as f:
        data = tomllib.load(f)

    cfg = Config()
    for name, raw in (data.get("accounts") or {}).items():
        cfg.accounts[name] = AccountConfig(
            name=name,
            email=raw["email"],
            password=raw["password"],
            provider=raw.get("provider", "generic"),
            imap_host=raw["imap_host"],
            imap_port=int(raw.get("imap_port", 993)),
            smtp_host=raw["smtp_host"],
            smtp_port=int(raw.get("smtp_port", 587)),
            auto_save_sent=bool(raw.get("auto_save_sent", True)),
            smtp_security=raw.get("smtp_security", "starttls"),
            imap_security=raw.get("imap_security", "ssl"),
            tls_verify=bool(raw.get("tls_verify", True)),
        )
    return cfg
