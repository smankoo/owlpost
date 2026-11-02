from __future__ import annotations

import imaplib
import threading

import pytest
from conftest import icloud_folders, make_account

from owlpost import account as account_mod
from owlpost.account import IMAP_TIMEOUT, SMTP_TIMEOUT


@pytest.fixture
def mailbox(imap_server):
    icloud_folders(imap_server)
    return imap_server


def _ops(server, name):
    return [e for e in server.log if e[0] == name]


def test_connections_are_reused_with_a_health_check(mailbox):
    acc = make_account()
    for _ in range(3):
        with acc.imap() as c:
            c.select('"INBOX"', readonly=True)
    assert mailbox.connections == 1
    assert len(mailbox.logins) == 1
    assert len(_ops(mailbox, "NOOP")) == 2


def test_connection_is_discarded_after_an_error(mailbox):
    acc = make_account()
    with pytest.raises(RuntimeError), acc.imap():
        raise RuntimeError("mid-operation failure")
    assert _ops(mailbox, "SHUTDOWN")
    with acc.imap():
        pass
    assert mailbox.connections == 2


def test_idle_connections_expire(mailbox, monkeypatch):
    acc = make_account()
    with acc.imap():
        pass
    monkeypatch.setattr(account_mod, "POOL_IDLE_SECONDS", -1)
    with acc.imap():
        pass
    # The stale one is closed without a NOOP that could hang on a dead socket.
    assert mailbox.connections == 2
    assert not _ops(mailbox, "NOOP")


def test_dead_pooled_connection_is_replaced(mailbox, monkeypatch):
    acc = make_account()
    with acc.imap():
        pass

    def dead_noop(self):
        raise imaplib.IMAP4.abort("socket error: EOF")

    monkeypatch.setattr("conftest.FakeIMAPConn.noop", dead_noop)
    with acc.imap():
        pass
    assert mailbox.connections == 2


def test_pool_keeps_a_bounded_number_of_idle_connections(mailbox):
    acc = make_account()
    hold = threading.Barrier(4)

    def op():
        with acc.imap():
            hold.wait(timeout=5)

    threads = [threading.Thread(target=op) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert mailbox.connections == 4
    assert len(acc._idle) == account_mod.POOL_MAX_IDLE
    assert len(_ops(mailbox, "LOGOUT")) == 4 - account_mod.POOL_MAX_IDLE
    acc.close()
    assert acc._idle == []


def test_failed_login_closes_the_socket(mailbox):
    mailbox.reject_login = True
    with pytest.raises(imaplib.IMAP4.error), make_account().imap():
        pass
    assert _ops(mailbox, "SHUTDOWN")


def test_sockets_have_timeouts(mailbox, smtp):
    acc = make_account()
    with acc.imap():
        pass
    with acc.smtp():
        pass
    assert mailbox.timeouts == [IMAP_TIMEOUT]
    assert smtp.instances[0].timeout == SMTP_TIMEOUT
