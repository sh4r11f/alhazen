"""Immutable run provenance for hub-installed experiments (cli/workspace.py
``hub_release``, alhazen.hub.installation.InstallStore.provenance) and the
upload's release bound to it (cli/workspace_hub.py). A new run of an installed
release records the hub base, experiment/version ids and source ZIP SHA-256
in run.json and launch.json before it starts; other launches are unchanged;
history is never re-derived from the mutable install registry."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from tests.hub.rig_support import release_zip
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

# The release's run.py writes one session as a session would (files, then
# its manifest) into the rehearsal data folder and prints the lines the
# workspace reads to find it.
RUN_PY = """\
import json
from pathlib import Path
from alhazen.data.manifest import write_manifest
folder = Path("data-rehearsal/v0.1.0/sub-01/ses-001/run-01_task-demo")
folder.mkdir(parents=True, exist_ok=True)
(folder / "session.json").write_text(json.dumps({"task": "demo", "mode": "movie"}),
                                     encoding="utf-8")
(folder / "sub-01_ses-001_run-01_trials.csv").write_text("trial,rt\\n1,0.2\\n", encoding="utf-8")
if not (folder / "manifest.yaml").exists():
    write_manifest(folder, folder / "manifest.yaml", experiment_version="0.1.0")
print("filed under v0.1.0/", flush=True)
print("running demo: sub-01 ses-001 run-01", flush=True)
"""


def finish(space: Workspace, run: dict) -> dict:
    space.worker.join(timeout=30)
    assert not space.worker.is_alive()
    return space.detail(run["id"])


def records(space: Workspace, run_id: str) -> tuple[dict, dict]:
    folder = space.directory / "runs" / run_id
    return (
        json.loads((folder / "run.json").read_text(encoding="utf-8")),
        json.loads((folder / "launch.json").read_text(encoding="utf-8")),
    )


@pytest.fixture
def installed(http, hub, workspace, tmp_path):  # noqa: F811
    call, _ = http
    connect(call, hub)
    data, manifest = release_zip(tmp_path, run_py=RUN_PY)
    exp, ver, sha = hub.add_release(data, manifest)
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
    folder = Path(next(p["path"] for p in workspace.projects if p["id"] == install["project_id"]))
    # The operator's own rig beside the release (packages never carry rigs).
    (folder / "configs").mkdir(exist_ok=True)
    (folder / "configs" / "rig-sim.yaml").write_bytes(base.RIG.read_bytes())
    return {"experiment_id": exp, "version_id": ver, "sha256": sha, "folder": folder, **install}


def launch(space: Workspace, project_id: str) -> dict:
    run = space.start(
        Launch(
            project=project_id, mode="movie", rig="configs/rig-sim.yaml", extra_args="--task demo"
        )
    )
    return finish(space, run)


class TestRunProvenance:
    def test_a_new_run_of_an_installed_release_records_it(self, installed, workspace, hub):  # noqa: F811
        run = launch(workspace, installed["project_id"])
        assert run["status"] == "completed", run
        expected = {
            "base_url": hub.base,
            "experiment_id": installed["experiment_id"],
            "version_id": installed["version_id"],
            "sha256": installed["sha256"],
            "name": "demo-task",
            "version": "1.0.0",
            "files_verified": True,
        }
        record, launched = records(workspace, run["id"])
        for snapshot in (record["hub_release"], launched["hub_release"]):
            assert {k: snapshot[k] for k in expected} == expected
            assert snapshot["trusted_at"]
        text = json.dumps([record, launched])
        assert not any(token in text for token in hub.tokens)
        assert "Bearer" not in text

    def test_other_launches_are_unchanged(self, http, workspace):  # noqa: F811
        original = workspace.projects[0]["id"]
        root = Path(workspace.projects[0]["path"])
        (root / "configs" / "rig-sim.yaml").write_bytes(base.RIG.read_bytes())
        run = launch(workspace, original)
        record, launched = records(workspace, run["id"])
        assert "hub_release" not in record and "hub_release" not in launched

    def test_history_survives_restart_registry_edits_and_account_changes(
        self,
        installed,
        workspace,  # noqa: F811
        http,  # noqa: F811
        hub,  # noqa: F811
    ):
        call, _ = http
        run = launch(workspace, installed["project_id"])
        before, launched_before = records(workspace, run["id"])
        # The registry is edited (another version pinned on the same record)
        # and another account signs in: history does not move.
        registry = workspace.directory / "hub" / "installs.json"
        entries = json.loads(registry.read_text(encoding="utf-8"))
        for entry in entries:
            entry["version_id"] = "v-edited-later"
        registry.write_text(json.dumps(entries), encoding="utf-8")
        connect(call, hub, "bob")
        again = Workspace(workspace.directory)
        try:
            assert again.runs[run["id"]]["hub_release"] == before["hub_release"]
        finally:
            again.close()
        assert records(workspace, run["id"]) == (before, launched_before)

    def test_changed_code_is_recorded_as_unverified(self, installed, workspace):  # noqa: F811
        target = installed["folder"] / "pyproject.toml"
        os.chmod(target, stat.S_IMODE(target.stat().st_mode) | stat.S_IWUSR)
        target.write_text(target.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
        run = launch(workspace, installed["project_id"])
        record, launched = records(workspace, run["id"])
        assert record["hub_release"]["files_verified"] is False
        assert launched["hub_release"]["files_verified"] is False

    def test_a_hub_folder_without_its_record_does_not_start(self, installed, workspace):  # noqa: F811
        (workspace.directory / "hub" / "installs.json").write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError, match="no install record"):
            workspace.start(
                Launch(
                    project=installed["project_id"],
                    mode="movie",
                    rig="configs/rig-sim.yaml",
                    extra_args="--task demo",
                )
            )
        assert workspace.active is None


class TestUploadBinding:
    def rehearsal_root(self, call, project_id):
        status, out = call(f"/api/data/roots?project={project_id}")
        assert status == 200, out
        return next(r["id"] for r in out["roots"] if r["kind"] == "rehearsal")

    def test_the_recorded_release_is_the_upload_release(self, installed, workspace, http, hub):  # noqa: F811
        call, _ = http
        launch(workspace, installed["project_id"])
        # The registry is edited afterwards: the run's own record still decides.
        registry = workspace.directory / "hub" / "installs.json"
        entries = json.loads(registry.read_text(encoding="utf-8"))
        for entry in entries:
            entry["version_id"] = "v-edited-later"
        registry.write_text(json.dumps(entries), encoding="utf-8")
        body = {
            "project_id": installed["project_id"],
            "root_id": self.rehearsal_root(call, installed["project_id"]),
            "run_id": "v0.1.0/sub-01/ses-001/run-01_task-demo",
        }
        status, out = call(f"{API}/local/upload-preview", body)
        assert status == 200, out
        assert out["release_source"] == "run_record"
        assert (out["experiment_id"], out["version_id"]) == (
            installed["experiment_id"],
            installed["version_id"],
        )
        status, refused = call(
            f"{API}/local/upload-preview",
            {**body, "experiment_id": installed["experiment_id"], "version_id": "v-other"},
        )
        assert status == 409 and refused["error"]["code"] == "conflict"
        assert "recorded with this run" in refused["error"]["message"]


def test_ma4_an_upload_never_names_a_modified_release(installed, workspace, http):  # noqa: F811
    """A session with no run record (an older run) is uploaded under the
    folder's install record only while the installed files still match it."""
    call, _ = http
    folder = installed["folder"] / "data" / "v0.1.0/sub-02/ses-001/run-01_task-demo"
    folder.mkdir(parents=True)
    (folder / "session.json").write_text('{"task": "demo"}', encoding="utf-8")
    from alhazen.data.manifest import write_manifest

    write_manifest(folder, folder / "manifest.yaml", experiment_version="1.0.0")
    status, out = call(f"/api/data/roots?project={installed['project_id']}")
    root = next(r["id"] for r in out["roots"] if r["kind"] == "real")
    body = {
        "project_id": installed["project_id"],
        "root_id": root,
        "run_id": "v0.1.0/sub-02/ses-001/run-01_task-demo",
    }
    status, out = call(f"{API}/local/upload-preview", body)
    assert status == 200, out
    assert out["release_source"] == "install_record" and out["release_verified"] is True
    target = installed["folder"] / "run.py"
    os.chmod(target, stat.S_IMODE(target.stat().st_mode) | stat.S_IWUSR)
    target.write_text("print('edited')\n", encoding="utf-8")
    status, out = call(f"{API}/local/upload-preview", body)
    assert status == 409 and out["error"]["code"] == "release_modified"
