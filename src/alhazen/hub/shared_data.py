"""One data root per hub-installed experiment, shared by all its releases.

Secret it hides: how an installed release's data folders reach the
experiment's own folder. A release is unpacked into
``hub/experiments/<name>/<version>-<sha12>/`` and runs there, so a rig whose
``data_root`` is relative (``data``) would write into that release's folder:
every upgrade would start an empty ``participants.tsv`` (restarting
counterbalancing and the subject/initials checks) and deleting an old release
would take its data with it. Instead each data folder name the release's rigs
use (``data``, and beside it ``data-rehearsal``, ``data-training``,
``data-training-rehearsal``; alhazen's naming) is a directory link in the
release folder to ``hub/experiments/<name>/<that name>``. The experiment's own
alhazen, whatever its version, then writes exactly where it always does, and
the release is already recorded per session (launch.json ``hub_release``).

A release folder that already holds a real data folder (an install made
before this rule) is migrated on first use: MOVED to the shared folder, never
copied; refused, with both folders named, when both hold something. Removing
a release folder removes its links, never the shared data (``shutil.rmtree``
and ``rm -r`` do not follow directory links; tests/hub/test_import_round.py
holds that).

Not handled here: a rig whose ``data_root`` is absolute already names one
folder for every release and is left alone.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from alhazen.modes.rehearsal import REHEARSAL_SUFFIX
from alhazen.training.ladder import TRAINING_SUFFIX


class SharedDataConflict(ValueError):
    """The release's own data folder and the shared one both hold data, or a
    name is taken by something that is not a folder; nothing was changed."""


def first_component(data_root: str) -> str | None:
    """The release-folder entry a relative ``data_root`` writes under
    (``data`` for ``data``, ``./data/lab`` and ``data/lab``), or None for an
    absolute one, one leaving the folder (``..``) or an empty one."""
    text = data_root.strip().replace("\\", "/")
    # Rooted ("/srv/data") or with a drive ("D:/data"): outside the release
    # folder on every platform, even where Path calls it relative (Windows).
    if not text or text.startswith(("~", "/")) or Path(text).is_absolute() or ":" in text[:3]:
        return None
    parts = [p for p in PurePosixPath(text).parts if p not in (".", "")]
    if not parts or parts[0] == ".." or ":" in parts[0]:
        return None
    return parts[0]


def link_names(components: Iterable[str]) -> list[str]:
    """Every folder name a data root ``C`` gives rise to: ``C`` itself, its
    rehearsal sibling, and the training roots beside it."""
    names: list[str] = []
    for component in sorted(set(components)):
        for suffix in ("", REHEARSAL_SUFFIX, TRAINING_SUFFIX, TRAINING_SUFFIX + REHEARSAL_SUFFIX):
            name = component + suffix
            if name not in names:
                names.append(name)
    return names


# Windows: a junction is a directory reparse point with this tag; Python
# before 3.12 has no os.path.isjunction, so it is read off lstat.
_REPARSE_POINT = 0x400  # stat.FILE_ATTRIBUTE_REPARSE_POINT
_MOUNT_POINT = 0xA0000003  # stat.IO_REPARSE_TAG_MOUNT_POINT


def _is_link(path: Path) -> bool:
    """A symbolic link, or on Windows a junction."""
    if path.is_symlink():
        return True
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT) and getattr(info, "st_reparse_tag", 0) == _MOUNT_POINT


def _points_at(link: Path, target: Path) -> bool:
    try:
        return link.resolve() == target.resolve()
    except OSError:
        return False


def _make_link(link: Path, target: Path) -> None:
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
        # A junction needs no privilege, unlike a directory symlink.
        import _winapi

        _winapi.CreateJunction(str(target.resolve()), str(link))
        return
    # Relative, so the hub folder can move as a whole.
    os.symlink(os.path.relpath(target, link.parent), link, target_is_directory=True)


def _has_content(folder: Path) -> bool:
    return any(folder.iterdir())


def share(release: Path, home: Path, names: Iterable[str]) -> list[dict[str, str]]:
    """Make each of ``names`` in ``release`` a link to ``home/<name>``.

    Returns what was done, one ``{"name", "action"}`` per name: ``linked``
    (new link, empty shared folder made), ``kept`` (already the link),
    ``moved`` (the release's own folder moved to the shared place, then
    linked) or ``replaced-empty`` (an empty folder in the release removed,
    then linked). Every name is checked before anything changes, so a
    conflict on one leaves all of them as they were."""
    release = Path(release)
    home = Path(home)
    plan: list[tuple[str, str]] = []
    for name in names:
        own = release / name
        shared = home / name
        if _is_link(own):
            if not _points_at(own, shared):
                raise SharedDataConflict(
                    f"{own} links to {os.path.realpath(own)}, not to the experiment's shared "
                    f"data folder {shared}; nothing was changed. Remove that link, or move its "
                    "data to the shared folder, and start again"
                )
            plan.append((name, "kept"))
        elif not os.path.lexists(own):
            plan.append((name, "linked"))
        elif not own.is_dir():
            raise SharedDataConflict(
                f"{own} is a file where the release's data folder {name} belongs; nothing was "
                "changed"
            )
        elif not _has_content(own):
            plan.append((name, "replaced-empty"))
        elif os.path.lexists(shared) and (not shared.is_dir() or _has_content(shared)):
            raise SharedDataConflict(
                f"Both {own} (this release's own data folder) and {shared} (the experiment's "
                "shared data folder, used by every release) hold data. Nothing was moved or "
                "merged: move the sessions you want into one of them by hand, empty the other, "
                "and start again"
            )
        else:
            plan.append((name, "moved"))
    done: list[dict[str, str]] = []
    home.mkdir(parents=True, exist_ok=True)
    for name, action in plan:
        own = release / name
        shared = home / name
        if action == "moved":
            if os.path.lexists(shared):
                shared.rmdir()  # checked empty above
            os.replace(own, shared)
        elif action == "replaced-empty":
            own.rmdir()
        if action != "kept":
            shared.mkdir(exist_ok=True)
            _make_link(own, shared)
        done.append({"name": name, "action": action})
    return done
