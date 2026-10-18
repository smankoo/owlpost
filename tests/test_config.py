from __future__ import annotations

import json
import shlex
import sys

import pytest

from owlpost import config as config_mod
from owlpost.config import ConfigError, load_config

VALID = """
[accounts.icloud]
email = "you@icloud.com"
password = "xxxx-xxxx-xxxx-xxxx"
provider = "icloud"
imap_host = "imap.mail.me.com"
smtp_host = "smtp.mail.me.com"

[accounts.proton]
email = "you@example.com"
password = "bridge"
imap_host = "127.0.0.1"
imap_port = 1143
imap_security = "starttls"
smtp_host = "127.0.0.1"
smtp_port = 1025
tls_verify = false
auto_save_sent = false
"""


def test_valid_config_with_defaults(write_config):
    cfg = load_config(write_config(VALID))
    assert sorted(cfg.accounts) == ["icloud", "proton"]

    icloud = cfg.get("icloud")
    assert icloud.email == "you@icloud.com"
    assert icloud.provider == "icloud"
    assert (icloud.imap_port, icloud.smtp_port) == (993, 587)
    assert icloud.imap_security == "ssl"
    assert icloud.smtp_security == "starttls"
    assert icloud.tls_verify is True
    assert icloud.auto_save_sent is True
    assert icloud.resolve_password() == "xxxx-xxxx-xxxx-xxxx"
    assert not icloud.is_gmail

    proton = cfg.get("proton")
    assert proton.provider == "generic"
    assert (proton.imap_port, proton.smtp_port) == (1143, 1025)
    assert proton.imap_security == "starttls"
    assert proton.tls_verify is False
    assert proton.auto_save_sent is False


def test_unknown_account_lists_known_names(write_config):
    cfg = load_config(write_config(VALID))
    with pytest.raises(KeyError, match=r"Unknown account 'nope'.*icloud"):
        cfg.get("nope")


def test_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="owlpost config not found"):
        load_config(tmp_path / "absent.toml")


def test_invalid_toml(write_config):
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(write_config("[accounts.x\nemail = "))


def test_missing_required_key(write_config):
    path = write_config(
        """
        [accounts.a]
        email = "you@example.com"
        password = "p"
        imap_host = "imap.example.com"
        """
    )
    with pytest.raises(ConfigError, match=r"\[accounts.a\] is missing required key\(s\): smtp_host"):
        load_config(path)


def test_bad_port(write_config):
    path = write_config(
        """
        [accounts.a]
        email = "you@example.com"
        password = "p"
        imap_host = "imap.example.com"
        imap_port = "nine-nine-three"
        smtp_host = "smtp.example.com"
        """
    )
    with pytest.raises(ConfigError, match="ports must be integers"):
        load_config(path)


def test_env_override_is_read_at_call_time(write_config, monkeypatch):
    path = write_config(VALID, name="elsewhere.toml")
    monkeypatch.setenv("OWLPOST_CONFIG", str(path))
    assert config_mod.config_path() == path
    assert "icloud" in load_config().accounts


def test_default_path_without_env(monkeypatch):
    monkeypatch.delenv("OWLPOST_CONFIG", raising=False)
    assert config_mod.config_path() == config_mod.DEFAULT_CONFIG_PATH
    assert config_mod.DEFAULT_CONFIG_PATH.parts[-3:] == (".config", "owlpost", "accounts.toml")


def test_repr_never_contains_password(write_config):
    acc = load_config(write_config(VALID)).get("icloud")
    assert "xxxx" not in repr(acc)


# ----- password sources ------------------------------------------------------

ACCOUNT_TEMPLATE = """
[accounts.a]
email = "you@example.com"
imap_host = "imap.example.com"
smtp_host = "smtp.example.com"
{extra}
"""


def _account(write_config, extra: str):
    return load_config(write_config(ACCOUNT_TEMPLATE.format(extra=extra))).get("a")


@pytest.mark.parametrize(
    "extra, found",
    [
        ("", "none"),
        ('password = "p"\npassword_env = "X"', "password, password_env"),
        ('password_env = "X"\npassword_command = "echo hi"', "password_env, password_command"),
    ],
)
def test_exactly_one_password_source(write_config, extra, found):
    with pytest.raises(ConfigError, match=rf"exactly one of .*\(found: {found}\)"):
        _account(write_config, extra)


def test_password_env(write_config, monkeypatch):
    acc = _account(write_config, 'password_env = "OWLPOST_TEST_PW"')
    monkeypatch.setenv("OWLPOST_TEST_PW", "from-env")
    assert acc.password is None
    assert acc.resolve_password() == "from-env"


def test_password_env_unset(write_config, monkeypatch):
    acc = _account(write_config, 'password_env = "OWLPOST_TEST_PW"')
    monkeypatch.delenv("OWLPOST_TEST_PW", raising=False)
    with pytest.raises(ConfigError, match="OWLPOST_TEST_PW is not set"):
        acc.resolve_password()


def _py_command(code: str) -> str:
    # A shlex-quoted python one-liner as a TOML basic string (JSON escaping is TOML-compatible).
    return "password_command = " + json.dumps(shlex.join([sys.executable, "-c", code]))


def test_password_command_strips_trailing_newline(write_config):
    acc = _account(write_config, _py_command("print('s3cret  ')"))
    assert acc.resolve_password() == "s3cret  "


def test_password_command_runs_without_shell(write_config, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))

        class P:
            returncode = 0
            stdout = "pw\n"
            stderr = ""

        return P()

    monkeypatch.setattr(config_mod.subprocess, "run", fake_run)
    acc = _account(write_config, "password_command = 'op read \"op://Private/Mail Account/password\"'")
    assert acc.resolve_password() == "pw"
    assert acc.resolve_password() == "pw"  # cached: command runs once
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["op", "read", "op://Private/Mail Account/password"]
    assert kwargs.get("shell", False) is False
    assert kwargs["timeout"] == 10


def test_password_command_failure_is_clear(write_config):
    acc = _account(write_config, _py_command("import sys; sys.stderr.write('vault locked'); sys.exit(3)"))
    with pytest.raises(ConfigError, match="exited with 3: vault locked"):
        acc.resolve_password()


def test_password_command_not_found(write_config):
    acc = _account(write_config, "password_command = 'owlpost-no-such-binary-xyz read'")
    with pytest.raises(ConfigError, match="password_command not found: owlpost-no-such-binary-xyz"):
        acc.resolve_password()


def test_password_command_timeout(write_config, monkeypatch):
    monkeypatch.setattr(config_mod, "PASSWORD_COMMAND_TIMEOUT", 0.2)
    acc = _account(write_config, _py_command("import time; time.sleep(5)"))
    with pytest.raises(ConfigError, match="timed out"):
        acc.resolve_password()


def test_password_command_empty_output(write_config):
    acc = _account(write_config, _py_command("pass"))
    with pytest.raises(ConfigError, match="no output"):
        acc.resolve_password()


def test_account_login_uses_resolved_password(write_config, monkeypatch, imap_server):
    from owlpost.account import Account

    monkeypatch.setenv("OWLPOST_TEST_PW", "env-pw")
    imap_server.add_folder("INBOX")
    acc = Account(_account(write_config, 'password_env = "OWLPOST_TEST_PW"'))
    with acc.imap():
        pass
    assert imap_server.logins == [("you@example.com", "env-pw")]


def test_example_config_parses():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "accounts.example.toml"
    cfg = load_config(example)
    assert set(cfg.accounts) == {"icloud", "gmail"}
    assert cfg.get("gmail").password_env == "OWLPOST_GMAIL_PASSWORD"
    assert cfg.get("gmail").auto_save_sent is False
