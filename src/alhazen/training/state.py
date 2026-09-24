"""What a subject's training remembers between sessions.

Shaping happens over weeks. The stage a subject is on, how much work it has
done there, and every transition it has been through belong to the *subject*,
not to any one session — so they live beside the subject's data, in
``<data_root>/sub-<ID>/training_state.yaml``, and are loaded at session build
and written at teardown.

The file is plain YAML on purpose: an experimenter who needs to put an animal
back a stage on a Monday morning should be able to do it with a text editor.
The price of hand editing is typos, so a file that exists but cannot be read
stops the session at build with a `ConfigError` saying how to fix it — a
trained subject is never quietly given a first-stage session instead.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from alhazen.data import naming
from alhazen.data.atomic import replace_atomically
from alhazen.errors import ConfigError

log = logging.getLogger(__name__)

STATE_FILENAME = "training_state.yaml"
SCHEMA_VERSION = 1


class TrainingState:
    """One subject's place in its curriculum.

    Deliberately not a frozen config model: this is mutable session state
    that is written back, not configuration that must not drift.
    """

    def __init__(
        self,
        stage: str,
        completed_by_stage: dict[str, int] | None = None,
        window: list[dict[str, Any]] | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> None:
        self.stage = stage
        # Completed trials per stage, kept per stage rather than as one
        # counter so a demoted-then-promoted subject resumes its ramps where
        # it left off instead of starting them again.
        self.completed_by_stage = dict(completed_by_stage or {})
        # Recent attempts, for the criteria. Carried ACROSS sessions: a
        # criterion over the last 100 trials means the last 100 trials, not
        # "the last 100 of today", or a subject could be promoted twice on
        # the same good afternoon.
        self.window = list(window or [])
        self.history = list(history or [])

    # -- progress ------------------------------------------------------

    def completed_in(self, stage: str) -> int:
        return self.completed_by_stage.get(stage, 0)

    def note_attempt(self, summary: dict[str, Any], window_size: int) -> None:
        """Record one attempt, keeping the window bounded."""
        self.window.append(summary)
        if summary.get("completed"):
            self.completed_by_stage[self.stage] = self.completed_in(self.stage) + 1
        # Trimmed to a generous multiple of the largest window a criterion
        # might use, so the file stays small without a criterion ever seeing
        # a window that was silently cut short.
        limit = max(window_size, 1) * 4
        if len(self.window) > limit:
            del self.window[:-limit]

    def note_transition(self, from_stage: str, to_stage: str, reason: str, session: str) -> None:
        self.stage = to_stage
        self.history.append(
            {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "from": from_stage,
                "to": to_stage,
                "reason": reason,
                "session": session,
            }
        )
        # A transition invalidates the window: the criteria for the new stage
        # must be judged on trials run AT that stage, not on the trials that
        # earned the move.
        self.window.clear()

    # -- persistence ---------------------------------------------------

    @classmethod
    def path_for(cls, data_root: Path, subject: str) -> Path:
        return Path(data_root) / naming.subject_dirname(subject) / STATE_FILENAME

    @classmethod
    def load(cls, data_root: Path, subject: str, default_stage: str) -> TrainingState:
        """Read a subject's state, or start it at ``default_stage``.

        A missing file is normal — it is a subject's first session. A file
        that exists but cannot be read (a YAML error, a missing ``stage``, a
        wrong type, bytes that are not UTF-8) raises `ConfigError`, and the
        file is not touched. Starting over instead would give a trained
        animal a first-stage session because of one typo in a hand edit —
        weeks of shaping wasted, and read as a behavioural regression. The
        message says how to proceed: fix the file, or rename it to start the
        subject over on purpose.

        Raises:
            ConfigError: the file exists and cannot be read.
        """
        path = cls.path_for(data_root, subject)
        if not path.exists():
            log.info("no training state for %s yet; starting at stage %r", subject, default_stage)
            return cls(stage=default_stage)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            state = cls(
                stage=raw["stage"],
                completed_by_stage=raw.get("completed_by_stage", {}),
                window=raw.get("window", []),
                history=raw.get("history", []),
            )
        # UnicodeDecodeError as well as the parse errors: bytes that are not
        # UTF-8 are what a disk problem leaves, and they get the same
        # actionable refusal as a typo rather than escaping as a bare crash.
        except (yaml.YAMLError, KeyError, TypeError, UnicodeDecodeError) as error:
            # Refused, not started over: this is raised from build_session
            # before a run folder exists or a window opens, so the subject
            # runs no trials at the wrong stage and nothing on disk changes.
            # The rename suggested below is the deliberate way to start
            # over, because a missing file is exactly a first session.
            raise ConfigError(
                f"the training state at {path} cannot be read "
                f"({type(error).__name__}: {error}), so subject {subject!r} was not "
                f"started — a trained subject must not silently get a "
                f"{default_stage!r} session. Either fix the file (it is plain YAML, "
                f"meant for hand editing) and start again; or, to start this subject "
                f"over at the first stage ({default_stage!r}) on purpose, rename it "
                f"in the same folder (for example to "
                f"{path.with_name(f'{path.stem}.unreadable{path.suffix}').name}) and "
                f"start again: with no state file, a session is the subject's first."
            ) from error
        log.info(
            "loaded training state for %s: stage %r, %d completed there",
            subject,
            state.stage,
            state.completed_in(state.stage),
        )
        return state

    def save(self, data_root: Path, subject: str) -> Path:
        """Write the state, replacing the old file in one step.

        The YAML goes to a temporary file beside the real one, which is then
        renamed over it. A crash or a full disk part-way through leaves the
        previous state whole, rather than a truncated file the next session
        cannot read — and would refuse to start from.
        """
        path = self.path_for(data_root, subject)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(
            {
                "schema_version": SCHEMA_VERSION,
                "stage": self.stage,
                "completed_by_stage": self.completed_by_stage,
                "window": self.window,
                "history": self.history,
            },
            sort_keys=False,
        )
        replace_atomically(path, text)
        return path
