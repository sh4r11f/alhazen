"""Credential changes and sign-in racing each other: a password verified just before a
reset or a disable must never leave a working session (the credential generation)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from alhazen.experiment_platform import accounts as accounts_module
from alhazen.experiment_platform.accounts import Accounts
from alhazen.experiment_platform.errors import AuthError

OLD = "the original password"
NEW = "the replacement password"


@pytest.fixture()
def accounts(tmp_path: Path, monkeypatch) -> Accounts:
    monkeypatch.setattr(accounts_module, "_SCRYPT", {"n": 2**10, "r": 8, "p": 1})
    db = Accounts(tmp_path / "accounts.sqlite3")
    db.add_user("ada", "Ada", OLD)
    return db


def _login_paused_after_verify(accounts: Accounts, monkeypatch):
    """Start a sign-in with the old password and hold it after the password check, before
    the session is issued. Returns (resume, join) where join() gives the outcome."""
    real_verify = accounts_module.verify_password
    verified, resume = threading.Event(), threading.Event()
    outcome: list[object] = []

    def verify(password: str, stored: str) -> bool:
        correct = real_verify(password, stored)
        verified.set()
        assert resume.wait(10)
        return correct

    monkeypatch.setattr(accounts_module, "verify_password", verify)

    def login() -> None:
        try:
            outcome.append(accounts.login("ada", OLD, "203.0.113.9"))
        except AuthError as error:
            outcome.append(error)

    thread = threading.Thread(target=login)
    thread.start()
    assert verified.wait(10)

    def join() -> object:
        resume.set()
        thread.join(10)
        assert not thread.is_alive()
        monkeypatch.setattr(accounts_module, "verify_password", real_verify)
        return outcome[0]

    return join


def _sessions(accounts: Accounts) -> int:
    return accounts._db().execute("SELECT count(*) FROM sessions").fetchone()[0]


def test_a_reset_during_an_old_password_sign_in_leaves_no_session(accounts, monkeypatch):
    join = _login_paused_after_verify(accounts, monkeypatch)
    accounts.set_password("ada", NEW)  # completes while the old-password sign-in is paused
    outcome = join()
    assert isinstance(outcome, AuthError)
    assert _sessions(accounts) == 0
    token, _, _ = accounts.login("ada", NEW)  # the new password works
    assert accounts.session(token) is not None


def test_a_disable_during_a_sign_in_leaves_no_session(accounts, monkeypatch):
    join = _login_paused_after_verify(accounts, monkeypatch)
    accounts.set_disabled("ada", True)
    outcome = join()
    assert isinstance(outcome, AuthError)
    assert _sessions(accounts) == 0


def test_a_session_from_an_older_generation_is_refused_at_lookup(accounts):
    token, _, _ = accounts.login("ada", OLD)
    # Even a session row that somehow survived a credential change is not accepted.
    db = accounts._db()
    db.execute("UPDATE users SET generation = generation + 1 WHERE username = 'ada'")
    assert accounts.session(token) is None


def test_a_reset_is_one_transaction(accounts, monkeypatch):
    token, _, _ = accounts.login("ada", OLD)
    db = accounts._db()
    # Make the session deletion fail after the password update: nothing must be committed.
    db.execute(
        "CREATE TRIGGER refuse BEFORE DELETE ON sessions BEGIN SELECT RAISE(ABORT, 'no'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        accounts.set_password("ada", NEW)
    db.execute("DROP TRIGGER refuse")
    assert accounts.session(token) is not None
    accounts.login("ada", OLD)  # the old password still stands
    with pytest.raises(AuthError):
        accounts.login("ada", NEW)


def test_an_older_database_gains_generations(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(accounts_module, "_SCRYPT", {"n": 2**10, "r": 8, "p": 1})
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE users (uid TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT"
        " NULL, password TEXT NOT NULL, created INTEGER NOT NULL, disabled INTEGER NOT NULL"
        " DEFAULT 0);"
        "CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, uid TEXT NOT NULL, csrf TEXT NOT"
        " NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL);"
    )
    conn.execute(
        "INSERT INTO users VALUES ('u1', 'ada', 'Ada', ?, 0, 0)",
        (accounts_module.hash_password(OLD),),
    )
    conn.commit()
    conn.close()
    db = Accounts(path)
    token, _, _ = db.login("ada", OLD)
    assert db.session(token) is not None
    db.set_password("ada", NEW)
    assert db.session(token) is None
