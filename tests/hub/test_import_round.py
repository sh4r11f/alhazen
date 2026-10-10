"""Import round (2026-10-09): what importing the user's four real experiments
into the hub showed, held as tests. See docs/hub/import-round.md.

1. An experiment's own configs/rig-*.yaml ship, so an installed ``--rig lab``
   (or mbri's ``lab-neural``) resolves to the package's file exactly as the
   checkout's does.
2. One data root per experiment, shared by all its installed releases:
   ``hub/experiments/<name>/data`` (and its rehearsal/training siblings),
   migrated from a release's own folder on first use, never deleted with a
   release.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from tests.hub.test_rig_adapter import (  # noqa: F401  (fixtures)
    API,
    connect,
    http,
    hub,
    probes,
    workspace,
)
from tests.unit import test_workspace as base

from alhazen.cli.workspace import Launch, Workspace
from alhazen.config.rigs import resolve_rig, rig_mapping
from alhazen.hub import packages
from alhazen.hub.shared_data import SharedDataConflict, first_component, link_names, share

MBRI_LAB = (
    "extends: lab\n"
    "devices:\n"
    "  eyetracker:\n"
    "    calibration_type: HV5\n"
    "    edf_name: mbri.EDF\n"
    "  photodiode:\n"
    "    event: STIM_ON\n"
)
MBRI_LAB_NEURAL = MBRI_LAB + "monitor:\n  name: rig-lab\n"

META = {
    "title": "Import round demo",
    "description": "",
    "entrypoint": "run.py",
    "python_min": "3.10",
    "alhazen_min": "2.12.0",
    "platforms": ["linux", "darwin", "win32"],
    "hardware": {"display": True, "eye_tracker": True, "reward": False},
    "license": "MIT",
    "citations": [],
}


def _source(root: Path, name: str, version: str, run_py: str = "print('run')\n") -> Path:
    root.mkdir(parents=True)
    (root / "run.py").write_text(run_py, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n', encoding="utf-8"
    )
    (root / "configs").mkdir()
    (root / "configs" / "rig-lab.yaml").write_text(MBRI_LAB, encoding="utf-8")
    (root / "configs" / "rig-lab-neural.yaml").write_text(MBRI_LAB_NEURAL, encoding="utf-8")
    (root / "configs" / "rig-lab.reward.yaml").write_text("pulses: 1\n", encoding="utf-8")
    (root / "configs" / "rig-sim.yaml").write_bytes(base.RIG.read_bytes())
    return root


# -- decision 1: the experiment's own rigs ship --------------------------------------


def test_installed_release_resolves_its_own_rigs_as_the_checkout_does(tmp_path):
    checkout = _source(tmp_path / "mbri", "mbri", "0.4.0")
    files = packages.suggest_files(checkout)
    assert {"configs/rig-lab.yaml", "configs/rig-lab-neural.yaml"} <= set(files)
    assert "configs/rig-lab.reward.yaml" not in files  # a measurement stays local
    bundle = tmp_path / "mbri.zip"
    packages.build_bundle(checkout, bundle, {**META, "name": "mbri", "version": "0.4.0"}, files)
    installed = tmp_path / "rig" / "hub" / "experiments" / "mbri" / "0.4.0-abc"
    installed.parent.mkdir(parents=True)
    packages.install_bundle(bundle, installed)
    for spec in ("lab", "lab-neural", "mbri/lab-neural"):
        here = resolve_rig(spec, checkout)
        there = resolve_rig(spec, installed)
        assert here.source == there.source == "experiment", spec
        assert there.path == installed / "configs" / here.path.name
        assert rig_mapping(there.path).values == rig_mapping(here.path).values
        assert rig_mapping(there.path).extends == "lab"


# -- decision 2: one data root per experiment ----------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("data", "data"),
        ("./data", "data"),
        ("data/lab", "data"),
        ("data-lab", "data-lab"),
        ("/srv/data", None),
        ("~/data", None),
        ("../data", None),
        ("", None),
    ],
)
def test_first_component(raw, expected):
    assert first_component(raw) == expected


def test_link_names_cover_rehearsal_and_training_roots():
    assert link_names(["data"]) == [
        "data",
        "data-rehearsal",
        "data-training",
        "data-training-rehearsal",
    ]


def _release(home: Path, version: str) -> Path:
    folder = home / f"{version}-0123456789ab"
    folder.mkdir(parents=True)
    return folder


def test_share_links_migrates_and_keeps(tmp_path):
    home = tmp_path / "experiments" / "demo"
    old = _release(home, "0.6.0")
    (old / "data" / "v0.6.0").mkdir(parents=True)
    (old / "data" / "participants.tsv").write_text("participant_id\nsub-01\n", encoding="utf-8")
    (old / "data-rehearsal").mkdir()  # empty: replaced by the link
    done = share(old, home, link_names(["data"]))
    assert {d["name"]: d["action"] for d in done} == {
        "data": "moved",
        "data-rehearsal": "replaced-empty",
        "data-training": "linked",
        "data-training-rehearsal": "linked",
    }
    assert (home / "data" / "participants.tsv").read_text(encoding="utf-8").endswith("sub-01\n")
    assert (old / "data").is_symlink() and (old / "data").resolve() == (home / "data").resolve()
    # Again: nothing moves, every link kept.
    assert {d["action"] for d in share(old, home, link_names(["data"]))} == {"kept"}
    new = _release(home, "0.6.1")
    share(new, home, link_names(["data"]))
    assert (new / "data" / "participants.tsv").read_text(encoding="utf-8").endswith("sub-01\n")


def test_share_refuses_when_both_hold_data_and_changes_nothing(tmp_path):
    home = tmp_path / "experiments" / "demo"
    (home / "data").mkdir(parents=True)
    (home / "data" / "participants.tsv").write_text("shared\n", encoding="utf-8")
    old = _release(home, "0.6.0")
    (old / "data").mkdir()
    (old / "data" / "participants.tsv").write_text("own\n", encoding="utf-8")
    with pytest.raises(SharedDataConflict, match="Both .* hold data"):
        share(old, home, link_names(["data"]))
    assert (old / "data" / "participants.tsv").read_text(encoding="utf-8") == "own\n"
    assert (home / "data" / "participants.tsv").read_text(encoding="utf-8") == "shared\n"
    assert not (old / "data-rehearsal").exists()  # checked first: nothing half done


def test_share_refuses_a_foreign_link_or_a_file(tmp_path):
    home = tmp_path / "experiments" / "demo"
    release = _release(home, "0.6.0")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (release / "data").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(SharedDataConflict, match="not to the experiment's shared"):
        share(release, home, ["data"])
    (release / "data").unlink()
    (release / "data").write_text("x", encoding="utf-8")
    with pytest.raises(SharedDataConflict, match="is a file"):
        share(release, home, ["data"])


def test_removing_a_release_never_removes_the_shared_data(tmp_path):
    home = tmp_path / "experiments" / "demo"
    release = _release(home, "0.6.0")
    share(release, home, link_names(["data"]))
    (release / "data" / "participants.tsv").write_text("participant_id\n", encoding="utf-8")
    shutil.rmtree(release)
    assert (home / "data" / "participants.tsv").is_file()


# The release's run.py appends its own version to participants.tsv in the
# rig's data_root, as a session adds a row to the participant registry.
REGISTRY_RUN = """\
from pathlib import Path
version = Path("pyproject.toml").read_text(encoding="utf-8").split('version = "')[1].split('"')[0]
registry = Path("data") / "participants.tsv"
registry.parent.mkdir(parents=True, exist_ok=True)
with registry.open("a", encoding="utf-8") as stream:
    stream.write(f"sub-01\\t{version}\\n")
print("running demo: sub-01 ses-001 run-01", flush=True)
"""


def _install(call, hub, space, tmp_path, version):  # noqa: F811
    source = _source(tmp_path / f"src-{version}", "counterbalanced", version, REGISTRY_RUN)
    bundle = tmp_path / f"counterbalanced-{version}.zip"
    files = packages.suggest_files(source)
    info = packages.build_bundle(
        source, bundle, {**META, "name": "counterbalanced", "version": version}, files
    )
    exp, ver, sha = hub.add_release(bundle.read_bytes(), dict(info.manifest))
    status, out = call(
        f"{API}/local/install",
        {
            "experiment_id": exp,
            "version_id": ver,
            "sha256": sha,
            "python": sys.executable,
            "trust_code": True,
        },
    )
    assert status == 201, out
    install = out["install"]
    install["path"] = next(
        p["path"] for p in space.projects if p["id"] == install["project_id"]
    )
    return install


def _run(space: Workspace, project_id: str) -> dict:
    run = space.start(
        Launch(project=project_id, mode="movie", rig="configs/rig-sim.yaml", extra_args="--task demo")
    )
    space.worker.join(timeout=30)
    assert not space.worker.is_alive()
    return space.detail(run["id"])


def test_an_upgrade_sees_the_same_participants_tsv(http, hub, workspace, tmp_path):  # noqa: F811
    """Install 0.6.0, run, install 0.6.1, run: one participants.tsv for both,
    so counterbalancing and the subject/initials checks continue across
    versions. The run records where the data went."""
    call, _ = http
    connect(call, hub)
    first = _install(call, hub, workspace, tmp_path, "0.6.0")
    assert first["shared_data_error"] is None
    assert first["shared_data"]["links"][0] == {"name": "data", "action": "linked"}
    run = _run(workspace, first["project_id"])
    assert run["status"] == "completed", run
    second = _install(call, hub, workspace, tmp_path, "0.6.1")
    run = _run(workspace, second["project_id"])
    assert run["status"] == "completed", run
    home = Path(first["path"]).parent
    assert Path(second["path"]).parent == home
    registry = home / "data" / "participants.tsv"
    assert registry.read_text(encoding="utf-8") == "sub-01\t0.6.0\nsub-01\t0.6.1\n"
    for install in (first, second):
        assert (Path(install["path"]) / "data" / "participants.tsv").samefile(registry)
    record = json.loads(
        (workspace.directory / "runs" / run["id"] / "launch.json").read_text(encoding="utf-8")
    )
    assert record["hub_data_folder"] == str(home)
    assert record["hub_release"]["version"] == "0.6.1"
    # The Data page reads the shared folder for either release.
    from alhazen.cli.workspace_data import data_roots

    for install in (first, second):
        existing, _, _ = data_roots(workspace.describe(install["project_id"]))
        assert (home / "data").resolve() in [r.path for r in existing]
    # Removing the old release folder leaves the experiment's data.
    shutil.rmtree(first["path"])
    assert registry.is_file()


def test_a_conflict_refuses_the_launch_before_anything_is_written(
    http, hub, workspace, tmp_path  # noqa: F811
):
    call, _ = http
    connect(call, hub)
    install = _install(call, hub, workspace, tmp_path, "0.6.0")
    folder = Path(install["path"])
    home = folder.parent
    # As an install made before the shared root: the release's own data/ ...
    (folder / "data").unlink()
    (folder / "data").mkdir()
    (folder / "data" / "participants.tsv").write_text("own\n", encoding="utf-8")
    # ... while the shared one already holds another release's data.
    (home / "data" / "participants.tsv").write_text("shared\n", encoding="utf-8")
    runs_before = set((workspace.directory / "runs").glob("*"))
    with pytest.raises(ValueError, match="Both .* hold data"):
        workspace.start(Launch(project=install["project_id"], mode="movie", rig="configs/rig-sim.yaml"))
    assert set((workspace.directory / "runs").glob("*")) == runs_before
    assert (folder / "data" / "participants.tsv").read_text(encoding="utf-8") == "own\n"
