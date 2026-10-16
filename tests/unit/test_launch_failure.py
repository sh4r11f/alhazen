"""Why a failed run failed, for the Run page beside Start
(``alhazen.cli.launch_failure``): the rule that picks the most informative
line(s) from a run's console, and the run detail that carries it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_workspace import request_for, workspace

from alhazen.cli.launch_failure import MAX_CHARS, MAX_LINES, launch_failure

__all__ = ["request_for", "workspace"]

FAILED = {"status": "failed", "returncode": 1, "error": None}

DARK = (
    "INVALID: a session cannot show this background: configs/task-dark-background.yaml "
    '(Task parameters "Dark background (candidate)") sets background_value 0.2, but alhazen '
    "opens the subject's window at mid-grey (0.5)."
)


def failure(log: str, **run) -> dict | None:
    return launch_failure(log, {**FAILED, **run})


class TestTheRule:
    def test_a_refusal_is_its_reason_without_the_prefix(self):
        found = failure("[harness] paced\n" + DARK + "\n")
        assert found == {
            "kind": "refused",
            "heading": "Run refused",
            "reason": DARK.removeprefix("INVALID: "),
        }

    @pytest.mark.parametrize("prefix", ["CANNOT RUN", "CANNOT DEMO", "CANNOT READ RUN"])
    def test_every_cannot_line_is_a_refusal(self, prefix):
        found = failure(f"live monitor: off\n{prefix}: run mode needs a rig that collects\n")
        assert found["kind"] == "refused"
        assert found["reason"] == "run mode needs a rig that collects"

    def test_a_usage_error_is_its_message_without_the_usage_lines(self):
        """argparse prints the usage, wrapped over several lines, then
        ``<prog>: error: <message>``; the message is what to fix."""
        log = (
            "usage: run.py (amodal-averaging) [-h] [--mode {run,test,simulate}]\n"
            "                                 [--task {amodal-averaging}]\n"
            "run.py (amodal-averaging): error: unrecognized arguments: --bogus-flag\n"
        )
        assert failure(log) == {
            "kind": "refused",
            "heading": "Run refused",
            "reason": "unrecognized arguments: --bogus-flag",
        }

    def test_a_refusal_keeps_the_lines_that_continue_it(self):
        log = (
            "INVALID: configs/task.yaml: 2 validation errors for Params\n"
            "speed\n  Input should be a valid number [type=float_parsing]\n"
            "\nsomething printed after a blank line\n"
        )
        assert failure(log)["reason"] == (
            "configs/task.yaml: 2 validation errors for Params\n"
            "speed\n  Input should be a valid number [type=float_parsing]"
        )

    def test_an_uncaught_exception_is_its_last_line_with_a_short_type(self):
        log = (
            "params: configs/task.yaml\n"
            "Traceback (most recent call last):\n"
            '  File "run.py", line 9, in <module>\n'
            "    main()\n"
            "    ^^^^^^\n"
            '  File "x.py", line 3, in main\n'
            "    raise ConfigError('no tracker answered')\n"
            "alhazen.errors.ConfigError: no tracker answered in 10 s\n"
        )
        assert failure(log) == {
            "kind": "error",
            "heading": "Run stopped with an error",
            "reason": "ConfigError: no tracker answered in 10 s",
        }

    def test_a_chained_exception_reads_the_last_block(self):
        log = (
            "Traceback (most recent call last):\n  File \"a\", line 1\nKeyError: 'x'\n\n"
            "During handling of the above exception, another exception occurred:\n\n"
            'Traceback (most recent call last):\n  File "b", line 2\n'
            "RuntimeError: the rig file names no monitor\n"
        )
        assert failure(log)["reason"] == "RuntimeError: the rig file names no monitor"

    def test_an_exception_without_a_message_is_its_type(self):
        log = 'Traceback (most recent call last):\n  File "a", line 1\nKeyboardInterrupt\n'
        assert failure(log)["reason"] == "KeyboardInterrupt"

    def test_the_diagnostic_printed_last_wins(self):
        """A prompt answered badly is asked again (an INVALID line) and the
        session may still die later; and a refusal printed after a caught
        traceback is the verdict."""
        refused_then_crashed = (
            "INVALID: session number must be a whole number\n"
            'Traceback (most recent call last):\n  File "a", line 1\n'
            "OSError: the display is gone\n"
        )
        assert failure(refused_then_crashed)["kind"] == "error"
        crashed_then_refused = (
            'Traceback (most recent call last):\n  File "a", line 1\nOSError: x\n' + DARK
        )
        assert failure(crashed_then_refused)["kind"] == "refused"

    def test_otherwise_the_last_lines_it_printed(self):
        log = "one\ntwo\n\nthree\nfour\n[workspace] Run killed after 10 s without exiting.\n\n"
        found = failure(log)
        assert found["kind"] == "exit" and found["heading"] == "Run failed"
        assert found["reason"] == "three\nfour\n[workspace] Run killed after 10 s without exiting."

    def test_silence_is_the_exit_code(self):
        assert failure("", returncode=3)["reason"] == "It exited with code 3 and printed nothing."
        assert failure("\n\n", returncode=None)["reason"] == "It printed nothing."

    def test_a_run_that_never_started_is_the_workspaces_own_error(self):
        found = failure("", error="[Errno 2] No such file or directory: 'python9'")
        assert found["reason"] == "[Errno 2] No such file or directory: 'python9'"

    @pytest.mark.parametrize("status", ["completed", "running", "cancelled", "killed"])
    def test_only_a_failed_run_has_one(self, status):
        assert launch_failure(DARK, {**FAILED, "status": status}) is None

    def test_colour_codes_and_rewritten_lines_are_read_as_shown(self):
        log = "progress 10%\rprogress 90%\r\n\x1b[31mINVALID: \x1b[0mno such rig\r\n"
        assert failure(log)["reason"] == "no such rig"
        assert failure("progress 10%\rprogress 90%\n")["reason"] == "progress 90%"

    def test_a_long_reason_is_capped_and_says_so(self):
        many = "INVALID: first\n" + "".join(f"field {i}\n" for i in range(40))
        reason = failure(many)["reason"]
        assert reason.count("\n") == MAX_LINES - 1 and reason.endswith(" …")
        wide = failure("INVALID: " + "x" * 5000)["reason"]
        assert len(wide) <= MAX_CHARS + 2 and wide.endswith(" …")


class TestTheRunDetail:
    """/api/runs/<id> carries it, read from the run's console."""

    def finished(self, workspace, run_py: str) -> dict:
        project = workspace.project(workspace.projects[0]["id"])
        (Path(project["path"]) / "run.py").write_text(run_py, encoding="utf-8")
        run = workspace.start(request_for(workspace, mode="simulate", subject="s01"))
        workspace.worker.join(timeout=30)
        return workspace.detail(run["id"])

    def test_a_refused_session(self, workspace):
        detail = self.finished(
            workspace,
            "import sys\nprint('INVALID: configs/task-dark.yaml sets background_value 0.2', "
            "file=sys.stderr)\nsys.exit(1)\n",
        )
        assert detail["status"] == "failed"
        assert detail["failure"] == {
            "kind": "refused",
            "heading": "Run refused",
            "reason": "configs/task-dark.yaml sets background_value 0.2",
        }
        # The run record on disk is not changed by it: the reason is read
        # from the console each time.
        record = json.loads((Path(detail["directory"]) / "run.json").read_text(encoding="utf-8"))
        assert "failure" not in record

    def test_a_completed_run_has_none(self, workspace):
        assert self.finished(workspace, "print('done')\n")["failure"] is None
