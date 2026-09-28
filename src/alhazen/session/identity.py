"""The files that say what a run folder is, written together before trial 1.

A run folder has always carried ``config_snapshot.yaml``: the merged config,
the seed, both git trees and the environment. That is the complete record,
and it is long. Since alhazen 2.0 three more files sit beside it, written by
the same call at the same moment, so a run folder says how it was set up to
someone who never opens the snapshot:

- ``session.json`` — a compact identity card (`session_card`): which
  experiment and which version of it, which task and mode, which subject and
  session and run, which rig and params file, which alhazen, and where the
  run's other files are. JSON rather than YAML so any language reads it with
  its standard library; ``schema_version`` so a reader can gate on its shape.
- ``rig.yaml`` — the rig file the session was started with, byte for byte.
- ``params.yaml`` — the params file it was started with, byte for byte, when
  there was one (none when the task ran on its params model's defaults).

The copies are the files as they were, comments and all, not a re-dump of
the parsed config: what a person wrote is what they will look for. They are
not the whole story, and the snapshot stays the authority on what RAN — a
test mode's reduced trial counts, a params hook's changes, a curriculum
stage's overrides and a rig that ``extends`` another all show up in the
snapshot's merged values, not in the files that were started from.

**All or nothing.** The four files are written in one step (`write_run_identity`)
with the snapshot last, and a failure removes whichever were already written.
So a run folder holding a snapshot always holds the rest, and one whose
snapshot could not be written is left as the build left it — which the runner
treats as "not a run" (session/runner.py) and data/paths.py lets the next
attempt reuse.

**Read early, written late.** The rig and params files are read when the
session is BUILT (`source_file`, called by build_session before the run
folder exists), not when it starts: an unreadable file then stops the session
before anything is written, and the bytes copied are the ones read moments
after the config was loaded rather than whatever the file holds once the
window has opened and the tracker connected.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alhazen.config.experiment import Experiment
from alhazen.config.models import SessionConfig
from alhazen.config.snapshot import build_provenance, write_snapshot
from alhazen.data.paths import SessionPaths
from alhazen.errors import ConfigError

log = logging.getLogger(__name__)

# The shape of session.json. It only ever goes up, and tests/unit/
# test_contracts.py pins it against tests/fixtures/contracts.json; a reader
# gates on it the way it gates on the manifest's.
SESSION_JSON_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SourceFile:
    """A config file as the session was started with it: where it was, and
    its bytes, read once when the session was built."""

    path: Path
    content: bytes


def source_file(value: str | Path | None, what: str) -> SourceFile | None:
    """The file ``value`` names, read now; None when it names no file.

    ``value`` is what the session's ``sources`` says supplied a layer, and
    that is not always a path: a rig built in code is ``<inline>``, a task on
    its model's defaults is ``<defaults>``. Those name no file, so there is
    nothing to copy and the run records none. A value that IS a file and
    cannot be read is a ConfigError naming it (``what``: "rig", "params"),
    raised while nothing has been written yet.
    """
    if value is None:
        return None
    path = Path(value)
    if not path.is_file():
        return None
    try:
        return SourceFile(path=path.resolve(), content=path.read_bytes())
    except OSError as exc:
        raise ConfigError(
            f"cannot read the {what} file {path} to copy it into the run folder: {exc}"
        ) from exc


@dataclass(frozen=True)
class RunIdentity:
    """What a run records about how it was set up, beyond its config.

    Built by build_session and handed to the runner, which writes it
    (`write_run_identity`) before trial 1 and stamps ``experiment.version``
    on every trial row, the manifest and the database row.

    - ``experiment`` — the experiment and version the data is filed under
      (`config.experiment.session_experiment`).
    - ``mode`` — the mode that started the session (``run``, ``test``,
      ``simulate``), or None for a session built directly with build_session.
    - ``rig_file`` / ``params_file`` — the files the session was started
      with, read at build time, or None where no file supplied that layer.
    """

    experiment: Experiment
    mode: str | None = None
    rig_file: SourceFile | None = None
    params_file: SourceFile | None = None


def session_card(
    cfg: SessionConfig,
    paths: SessionPaths,
    identity: RunIdentity,
    provenance: dict[str, str],
) -> dict[str, Any]:
    """session.json's contents: the run's identity card.

    ``provenance`` is the snapshot's own (`build_provenance`), so the git
    trees and the creation time here are the snapshot's, read once.

    Paths under ``files`` are relative to the run folder, with forward
    slashes on every platform, so the card still reads right after the
    folder is moved or copied to another machine. ``rig.file`` and
    ``params_file`` are where the originals were, absolute, on the machine
    that ran the session; the copies are what outlive them. The rig's
    ``name`` and ``source`` are what the session's ``sources`` records about
    a rig chosen by name (``rig_name``, ``rig_source``), and null for a rig
    given as a path.
    """
    experiment = identity.experiment
    info = cfg.info
    sources = cfg.sources

    def relative(path: Path) -> str:
        return path.relative_to(paths.run_dir).as_posix()

    return {
        "schema_version": SESSION_JSON_SCHEMA_VERSION,
        "experiment": {
            "name": experiment.name,
            "version": experiment.version,
            "version_source": experiment.version_source,
            # `git describe --always --dirty` of the experiment's tree, the
            # snapshot's `experiment_git_sha` (config/snapshot.py).
            "git": provenance.get("experiment_git_sha"),
        },
        "task": info.task_name,
        "mode": identity.mode,
        # Initials are recorded here and in the registry, never in a path.
        "subject": {"id": info.subject, "initials": info.initials},
        "session": info.session,
        "run": info.run,
        "seed": info.seed,
        # The date the run's file names carry (YYYYMMDD), and the moment the
        # record was made, in UTC.
        "date": paths.base.rsplit("_", 1)[-1],
        "created": provenance.get("created"),
        "rig": {
            "name": sources.get("rig_name"),
            "source": sources.get("rig_source"),
            "file": str(identity.rig_file.path) if identity.rig_file is not None else None,
        },
        "params_file": (
            str(identity.params_file.path) if identity.params_file is not None else None
        ),
        "alhazen": {
            "version": provenance.get("alhazen_version"),
            "git_describe": provenance.get("alhazen_git_describe"),
        },
        "files": {
            "snapshot": relative(paths.snapshot_path),
            "manifest": relative(paths.manifest_path),
            "rig": relative(paths.rig_copy_path) if identity.rig_file is not None else None,
            "params": (
                relative(paths.params_copy_path) if identity.params_file is not None else None
            ),
            "trials": relative(paths.trials_path),
            "events": relative(paths.events_path),
            "frames": relative(paths.frames_path),
            "log": relative(paths.log_path),
        },
    }


def write_run_identity(
    cfg: SessionConfig,
    paths: SessionPaths,
    identity: RunIdentity,
    experiment_dir: Path | None = None,
) -> None:
    """Write rig.yaml, params.yaml, session.json and config_snapshot.yaml —
    all of them, or none.

    The snapshot goes last, so its presence — what the runner and `load_run`
    take to mean "this folder is a run" — implies the other three. A failure
    part-way (a full disk, a folder the antivirus holds) removes what this
    call already wrote and re-raises; a file that cannot be removed either is
    logged with its traceback, so the original error is still the one that
    propagates. ``experiment_dir`` is where the experiment's code lives, for
    its git tree (`build_provenance`).
    """
    written: list[Path] = []

    def ours(path: Path) -> Path:
        # Noted before the write, so a write that fails part-way — leaving a
        # partial file — is cleaned up too. Only a path that did not exist
        # yet: whatever was already there is not this call's to delete.
        if not path.exists():
            written.append(path)
        return path

    try:
        if identity.rig_file is not None:
            ours(paths.rig_copy_path).write_bytes(identity.rig_file.content)
        if identity.params_file is not None:
            ours(paths.params_copy_path).write_bytes(identity.params_file.content)
        # One reading of the provenance for both files, so session.json and
        # the snapshot cannot disagree about a timestamp or a git tree.
        provenance = build_provenance(experiment_dir, identity.experiment)
        card = session_card(cfg, paths, identity, provenance)
        ours(paths.session_json_path).write_text(
            json.dumps(card, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        write_snapshot(cfg, ours(paths.snapshot_path), provenance=provenance)
    except BaseException:
        # BaseException: a Ctrl-C landing here must not leave half a record
        # behind either. Removed newest first; the error is re-raised below.
        for path in reversed(written):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.exception("could not remove %s after the run's record failed to write", path)
        raise
