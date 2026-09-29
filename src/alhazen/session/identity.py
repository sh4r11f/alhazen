"""The files that say what a run folder is, written together before trial 1.

A run folder has always carried ``config_snapshot.yaml``: the merged config,
the seed, both git trees and the environment. That is the complete record,
and it is long. Since alhazen 2.0 three more files sit beside it, written by
the same call at the same moment, so a run folder says how it was set up to
someone who never opens the snapshot:

- ``session.json`` — a compact identity card (`session_card`): which
  experiment and which version of it, which task and mode, which subject and
  session and run, which rig and params file, the command the session was
  started with, which alhazen, and where the run's other files are. JSON
  rather than YAML so any language reads it with its standard library;
  ``schema_version`` so a reader can gate on its shape.
- ``rig.yaml`` — the rig file the session was started with, byte for byte.
- ``rig-merged.yaml`` — for a rig file that ``extends`` one of alhazen's
  shared rigs, the whole rig: the file merged over the shared one
  (`merged_rig`). rig.yaml alone is then only half a rig, and the other half
  lives in whichever alhazen was installed that day; with this beside it the
  run folder says what the machine was on its own. Not written for a rig
  that extends nothing, whose rig.yaml is already the whole rig.
- ``params.yaml`` — the params file it was started with, byte for byte, when
  there was one (none when the task ran on its params model's defaults).

The copies are the files as they were, comments and all, not a re-dump of
the parsed config: what a person wrote is what they will look for. They are
not the whole story, and the snapshot stays the authority on what RAN — a
test mode's reduced trial counts, a params hook's changes, a curriculum
stage's overrides and a mode standing a device down all show up in the
snapshot's merged values, not in the files that were started from.

**All or nothing.** The files are written in one step (`write_run_identity`)
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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from alhazen.config.experiment import Experiment
from alhazen.config.models import SessionConfig
from alhazen.config.rigs import rig_mapping
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
    - ``rig_merged`` — rig-merged.yaml's bytes (`merged_rig`), made at build
      time from ``rig_file``; None when the rig file extends nothing or there
      is no rig file.
    - ``command`` — the command line the session was started with
      (`recorded_command`), or None for a session built in code, where no
      command line was parsed.
    """

    experiment: Experiment
    mode: str | None = None
    rig_file: SourceFile | None = None
    params_file: SourceFile | None = None
    rig_merged: bytes | None = None
    command: tuple[str, ...] | None = None


# How the alhazen console command is recorded: by its name, which is what a
# person types, rather than wherever pip put its launcher on this machine.
ALHAZEN_PROGRAM = "alhazen"


def recorded_command(invocation: Sequence[str], experiment_root: Path | None) -> tuple[str, ...]:
    """The command line to record: ``invocation`` (the program, then the
    arguments exactly as the command line parser received them) with the
    program made readable.

    The program is how the session would be started again: a ``run.py``
    inside the experiment's folder is recorded relative to that folder, with
    forward slashes (``run.py``, ``scripts/run.py``), so the record means the
    same after the checkout moves and names no one's home folder. A program
    outside the experiment (a task installed from a wheel, a test runner)
    is kept as it was started, and so is the ``alhazen`` command. The
    arguments are never rewritten — a path among them is what was typed, and
    changing it would record a command nobody ran. None of alhazen's flags
    carries a secret, so the arguments are recorded whole.

    An empty ``invocation`` is a caller's bug (there is always a program), so
    it is refused rather than recorded as an empty command.
    """
    if not invocation:
        raise ValueError("an invocation has at least its program; this one has no program")
    program, *arguments = (str(part) for part in invocation)
    if program != ALHAZEN_PROGRAM and experiment_root is not None:
        started = Path(program).resolve()
        root = experiment_root.resolve()
        # Outside the experiment's folder, the program is kept exactly as
        # it was started.
        if started.is_relative_to(root):
            program = started.relative_to(root).as_posix()
    return (program, *arguments)


def merged_rig(rig_file: SourceFile | None) -> bytes | None:
    """rig-merged.yaml's contents for a rig file that ``extends`` a shared
    rig: the file's settings merged over the shared rig's, as the session
    loaded them (`config.rigs.rig_mapping`), headed by what was merged. None
    when there is no rig file or it extends nothing — its rig.yaml copy is
    then the whole rig already.

    Called when the session is BUILT, like `source_file`, so the shared rig
    read is the one the session just loaded. A rig file that cannot be read
    or merged raises the ConfigError `rig_mapping` raises, naming the file —
    the session loaded the same file moments earlier, so that means it
    changed in between, and the session stops before anything is written.

    ``monitor.name`` is written in when the files leave it out: the session
    named the panel after the rig file (`config.loader.load_rig`), and a
    file called rig-merged.yaml, loaded on its own, would otherwise name it
    ``rig-merged`` — a different, uncalibrated monitor to PsychoPy.
    """
    if rig_file is None:
        return None
    merged = rig_mapping(rig_file.path)
    if merged.extends is None or merged.base is None:
        return None
    values = dict(merged.values)
    monitor = values.get("monitor")
    if monitor is None:
        values["monitor"] = {"name": rig_file.path.stem}
    elif isinstance(monitor, dict) and "name" not in monitor:
        # The name first, where a person reading the file looks for it.
        values["monitor"] = {"name": rig_file.path.stem, **monitor}
    # File names only: where the files were on this machine is session.json's
    # to say (`rig.file`), and a full path here would carry a home folder
    # into every copy of the data.
    header = (
        f"# The rig this run was started with, whole: {rig_file.path.name} (copied as\n"
        "# it was written to rig.yaml) merged over alhazen's shared rig\n"
        f"# '{merged.extends}' ({merged.base.name}), as the session loaded it. Written\n"
        "# by alhazen from the settings: the comments are in rig.yaml and in the\n"
        "# shared rig.\n"
    )
    body = yaml.safe_dump(values, sort_keys=False, allow_unicode=True)
    return (header + body).encode("utf-8")


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
        # A list, as the process received it: no shell's quoting rules to
        # guess at, and the same on every platform. Null for a session built
        # in code. Added in 2.1.0 without a schema bump: it is a new key,
        # and a reader of schema 1 ignores keys it does not know.
        "command": list(identity.command) if identity.command is not None else None,
        "alhazen": {
            "version": provenance.get("alhazen_version"),
            "git_describe": provenance.get("alhazen_git_describe"),
        },
        "files": {
            "snapshot": relative(paths.snapshot_path),
            "manifest": relative(paths.manifest_path),
            "rig": relative(paths.rig_copy_path) if identity.rig_file is not None else None,
            "rig_merged": (
                relative(paths.rig_merged_path) if identity.rig_merged is not None else None
            ),
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
    """Write rig.yaml, rig-merged.yaml, params.yaml, session.json and
    config_snapshot.yaml — all of those the session has, or none.

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
        if identity.rig_merged is not None:
            ours(paths.rig_merged_path).write_bytes(identity.rig_merged)
        if identity.params_file is not None:
            ours(paths.params_copy_path).write_bytes(identity.params_file.content)
        # One reading of the provenance for both files, so session.json and
        # the snapshot cannot disagree about a timestamp or a git tree.
        provenance = build_provenance(experiment_dir, identity.experiment)
        card = session_card(cfg, paths, identity, provenance)
        ours(paths.session_json_path).write_text(
            json.dumps(card, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        write_snapshot(
            cfg, ours(paths.snapshot_path), provenance=provenance, command=identity.command
        )
    except BaseException:
        # BaseException: a Ctrl-C landing here must not leave half a record
        # behind either. Removed newest first; the error is re-raised below.
        for path in reversed(written):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.exception("could not remove %s after the run's record failed to write", path)
        raise
