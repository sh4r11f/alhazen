"""The live monitor's server child stops when the session that started it is
killed without stopping it (a dashboard's kill after a hung exit, SIGKILL, Task
Manager). An orphan would keep its port, the session's inherited pipes and the
multiprocessing resource tracker alive indefinitely."""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

SESSION = textwrap.dedent(
    """
    import sys, time
    from alhazen.live_monitor.runtime import LiveMonitorController

    if __name__ == "__main__":
        controller = LiveMonitorController(port=0, auto_open=False)
        controller.start()
        print(controller.url, flush=True)
        time.sleep(120)
    """
)


def _port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def test_server_child_stops_when_the_session_is_killed(tmp_path: Path) -> None:
    script = tmp_path / "session.py"
    script.write_text(SESSION, encoding="utf-8")
    session = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert session.stdout is not None
        url = session.stdout.readline().strip()
        assert url.startswith("http://127.0.0.1:"), url
        port = int(url.split(":")[2].split("/")[0])
        assert _port_open(port)
    finally:
        session.kill()  # no stop(), no atexit: the way a hung session is ended
        session.wait(timeout=10)
    deadline = time.monotonic() + 10
    while _port_open(port) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _port_open(port), "the live monitor outlived its session"
