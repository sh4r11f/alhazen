"""What a subject's training remembers between sessions.

Shaping happens over weeks. The stage a subject is on, how much work it has
done there, and every transition it has been through belong to the *subject*,
not to any one session — so they live beside the subject's data, in
``<data_root>/sub-<ID>/training_state.yaml``, and are loaded at session build
and written at teardown.

The file is plain YAML on purpose: an experimenter who needs to put an animal
back a stage on a Monday morning should be able to do it with a text editor.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from alhazen.data import naming

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
        that cannot be read is NOT normal, and says so loudly before starting
        over: silently restarting an animal at stage 0 after a disk problem
        would waste weeks of shaping and look like a behavioural regression.
        """
        path = cls.path_for(data_root, subject)
        if not path.exists():
            log.info("no training state for %s yet; starting at stage %r", subject, default_stage)
            return cls(stage=default_stage)
        try:
            raw = yaml.safe_load(path.read_text()) or {}
            state = cls(
                stage=raw["stage"],
                completed_by_stage=raw.get("completed_by_stage", {}),
                window=raw.get("window", []),
                history=raw.get("history", []),
            )
        except (yaml.YAMLError, KeyError, TypeError) as error:
            log.error(
                "training state at %s is unreadable (%s) — starting at stage %r. The old "
                "file is left in place; move it aside once you have looked at it.",
                path,
                error,
                default_stage,
            )
            return cls(stage=default_stage)
        log.info(
            "loaded training state for %s: stage %r, %d completed there",
            subject,
            state.stage,
            state.completed_in(state.stage),
        )
        return state

    def save(self, data_root: Path, subject: str) -> Path:
        path = self.path_for(data_root, subject)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": SCHEMA_VERSION,
                    "stage": self.stage,
                    "completed_by_stage": self.completed_by_stage,
                    "window": self.window,
                    "history": self.history,
                },
                sort_keys=False,
            )
        )
        return path
