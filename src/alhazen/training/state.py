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
from alhazen.data.atomic import replace_atomically

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
        # The state file `load` could not read, when it could not. The session
        # then starts over at the first stage, and `save` moves this file
        # aside before writing — the only copy of weeks of shaping is never
        # written over by the state of the one session that could not read it.
        self._unreadable: Path | None = None

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
        The unreadable file is never written over: `save` renames it first.
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
        # UTF-8 are what a disk problem leaves, and that is the case this
        # branch exists for.
        except (yaml.YAMLError, KeyError, TypeError, UnicodeDecodeError) as error:
            log.error(
                "training state at %s is unreadable (%s) — starting at stage %r. The file "
                "is kept: when this session saves, it is renamed to %s rather than "
                "written over. Look at it, and put the subject back by hand if it was "
                "further along.",
                path,
                error,
                default_stage,
                _aside_name(path, "<time>").name,
            )
            state = cls(stage=default_stage)
            state._unreadable = path
            return state
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
        cannot read — and would start the subject over from.
        """
        path = self.path_for(data_root, subject)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Only the file `load` failed on is moved, and only once: a later
        # save of the same state writes over this session's own file.
        if self._unreadable == path and path.exists():
            self._set_aside(path)
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

    def _set_aside(self, path: Path) -> None:
        """Rename the file `load` could not read, so saving cannot destroy it.

        Renamed, not copied: the new name says what the file is to whoever
        opens the subject's folder, and the real name is free for this
        session's state. A name that is already taken gets a counter rather
        than being replaced.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        aside = _aside_name(path, stamp)
        counter = 1
        while aside.exists():
            counter += 1
            aside = _aside_name(path, f"{stamp}-{counter}")
        path.rename(aside)
        log.warning(
            "the unreadable training state was moved to %s; this session's state is "
            "written to %s in its place",
            aside,
            path,
        )
        self._unreadable = None


def _aside_name(path: Path, stamp: str) -> Path:
    """Where an unreadable state file is moved: same folder, same name, plus
    ``.unreadable-<stamp>`` before the suffix."""
    return path.with_name(f"{path.stem}.unreadable-{stamp}{path.suffix}")
