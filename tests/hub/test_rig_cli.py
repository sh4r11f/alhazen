"""``alhazen hub`` and ``alhazen dashboard --hub`` (alhazen.hub.cli, cli/main.py):
the subcommands share the dashboard's state, take passwords only from
getpass, and refuse an install without an explicit trust flag."""

from __future__ import annotations

import json
import sys

import pytest
from tests.hub.rig_support import FakeHub

from alhazen.cli.main import main as cli_main_function
from alhazen.hub.credentials import RigState


@pytest.fixture
def hub():
    server = FakeHub()
    yield server
    server.close()


def run(*argv):
    return cli_main_function(list(argv))


def test_dashboard_hub_flag_is_parsed(monkeypatch):
    seen = {}

    def fake_serve(args):
        seen["hub"] = args.hub
        return 0

    monkeypatch.setattr("alhazen.cli.dashboard.serve", fake_serve)
    assert run("dashboard", "--hub", "--no-browser") == 0
    assert seen == {"hub": True}
    assert run("dashboard", "--no-browser") == 0
    assert seen == {"hub": False}


def test_no_password_flag_exists(capsys):
    with pytest.raises(SystemExit):
        run("hub", "login", "--password", "x")
    assert "unrecognized arguments" in capsys.readouterr().err


def test_connect_login_status_logout(tmp_path, hub, monkeypatch, capsys):
    state = str(tmp_path / "state")
    assert run("hub", "--state-dir", state, "connect", hub.base) == 1
    assert "local development" in capsys.readouterr().err
    assert run("hub", "--state-dir", state, "connect", hub.base, "--allow-http-loopback") == 0
    passwords = iter(["alice-password-1"])
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(passwords))
    assert run("hub", "--state-dir", state, "login", "--username", "alice") == 0
    capsys.readouterr()
    assert run("hub", "--state-dir", state, "status") == 0
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == "signed_in" and status["user"]["username"] == "alice"
    assert not any(token in json.dumps(status) for token in hub.tokens)
    # The dashboard reads the same sign-in.
    assert RigState(tmp_path / "state" / "hub").credential().user_id == "u-alice"
    assert run("hub", "--state-dir", state, "logout") == 0
    assert hub.tokens == {}
    assert RigState(tmp_path / "state" / "hub").credential() is None


def test_install_needs_the_trust_flag(tmp_path, capsys):
    code = run(
        "hub",
        "--state-dir",
        str(tmp_path),
        "install",
        "e1",
        "v1",
        "--sha256",
        "a" * 64,
        "--python",
        sys.executable,
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "not a sandbox" in captured.out and "--trust-code" in captured.err


def test_serve_without_the_service_says_what_to_install(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "alhazen.hub.app", None)
    assert run("hub", "serve", "--config", str(tmp_path / "hub.toml")) == 1
    assert "alhazen-vision[hub]" in capsys.readouterr().err
