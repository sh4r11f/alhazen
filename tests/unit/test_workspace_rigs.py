"""The workspace's Rig menu: an experiment's own rigs and its alhazen's shared ones.

The menu lists rigs by name, the experiment's first and then the shared ones
the PROJECT's alhazen ships — which the registration asks that interpreter
for, since the workspace's own alhazen may be another version. A shared rig
launches as `--rig alhazen/<name>`, an experiment rig as its file; a rig that
extends a shared one is merged over the project's shared file for the
summary and for the run folder's copy, which must say what ran on its own.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from test_workspace import finish, http  # noqa: F401  (http is a fixture)

from alhazen.cli import workspace as workspace_module
from alhazen.cli.workspace import REGISTER_AGAIN, Launch, Workspace
from alhazen.config.rigs import SHARED_RIG_DIR, rig_mapping
from alhazen.errors import ConfigError

RIG = Path(__file__).parents[2] / "examples/minimal_fixation/rig-sim.yaml"


@pytest.fixture
def project_alhazen(tmp_path):
    """The shared rigs of the project's alhazen, as its probe reports them: a
    lab with a monitor no real shared rig has (99 cm wide), so a merge that
    read the workspace's own lab instead would show, and a mac."""
    folder = tmp_path / "project-env" / "alhazen" / "rigs"
    folder.mkdir(parents=True)
    lab = yaml.safe_load(RIG.read_text(encoding="utf-8"))
    lab["monitor"]["width_cm"] = 99.0
    lab["live_monitor"] = {"enabled": True}
    (folder / "rig-lab.yaml").write_text(yaml.safe_dump(lab), encoding="utf-8")
    (folder / "rig-mac.yaml").write_bytes(RIG.read_bytes())
    return {"lab": folder / "rig-lab.yaml", "mac": folder / "rig-mac.yaml"}


@pytest.fixture
def workspace(tmp_path, monkeypatch, project_alhazen):
    """A project with a whole-file rig (sim), a rig extending the shared lab
    that changes only the viewing distance (lab), and the two shared rigs
    above; its run.py prints the argv it was given."""
    monkeypatch.setattr(
        workspace_module,
        "probe_interpreter",
        lambda python, path: {
            "alhazen_version": "2.0.0",
            "python_version": "stub",
            "shared_rigs": [{"name": n, "path": str(p)} for n, p in project_alhazen.items()],
        },
    )
    root = tmp_path / "experiment"
    (root / "configs").mkdir(parents=True)
    (root / "configs/rig-sim.yaml").write_bytes(RIG.read_bytes())
    (root / "configs/rig-lab.yaml").write_text(
        "# The lab, sat closer.\nextends: lab\nmonitor:\n  distance_cm: 45.0\n", encoding="utf-8"
    )
    (root / "run.py").write_text("import json, sys\nprint(json.dumps(sys.argv[1:]), flush=True)\n")
    space = Workspace(tmp_path / "state")
    space.add(str(root), sys.executable)
    yield space
    space.close()


def launch(workspace, rig: str, **overrides) -> Launch:
    return Launch(
        **{"project": workspace.projects[0]["id"], "mode": "movie", "rig": rig, **overrides}
    )


def rig_argument(command: list[str]) -> str:
    return command[command.index("--rig") + 1]


class TestTheMenu:
    def test_it_lists_the_experiments_rigs_then_the_shared_ones_by_name(
        self, workspace, project_alhazen
    ):
        described = workspace.describe(workspace.projects[0]["id"])
        assert described["rigs"] == [
            {
                "name": "lab",
                "source": "experiment",
                "shadowed": False,
                "extends": "lab",
                "path": "configs/rig-lab.yaml",
            },
            {
                "name": "sim",
                "source": "experiment",
                "shadowed": False,
                "extends": None,
                "path": "configs/rig-sim.yaml",
            },
            # The project's alhazen's files, as its probe recorded them: the
            # workspace's own shared rigs are never listed for a project.
            {
                "name": "lab",
                "source": "alhazen",
                "shadowed": True,
                "extends": None,
                "path": str(project_alhazen["lab"]),
            },
            {
                "name": "mac",
                "source": "alhazen",
                "shadowed": False,
                "extends": None,
                "path": str(project_alhazen["mac"]),
            },
        ]
        assert described["rigs_note"] is None

    def test_a_rig_that_cannot_be_read_is_listed_with_the_reason(self, workspace):
        root = Path(workspace.projects[0]["path"])
        (root / "configs/rig-broken.yaml").write_text("extends: [\n", encoding="utf-8")
        entry = next(
            rig
            for rig in workspace.describe(workspace.projects[0]["id"])["rigs"]
            if rig["name"] == "broken"
        )
        assert entry["extends"] is None
        assert "invalid YAML" in entry["error"]

    def test_a_rig_file_that_leads_out_of_the_project_is_not_offered(self, workspace, tmp_path):
        outside = tmp_path / "outside.yaml"
        outside.write_bytes(RIG.read_bytes())
        link = Path(workspace.projects[0]["path"]) / "configs/rig-escape.yaml"
        try:
            link.symlink_to(outside)
        except OSError as exc:  # Windows without the symlink privilege
            pytest.skip(f"cannot create a symlink here: {exc}")
        names = [rig["name"] for rig in workspace.describe(workspace.projects[0]["id"])["rigs"]]
        assert "escape" not in names


class TestLaunchingARig:
    def test_a_shared_rig_launches_by_name_and_is_recorded_as_alhazens(
        self, workspace, project_alhazen
    ):
        request = launch(workspace, "alhazen/mac")
        assert rig_argument(workspace._command(request, workspace.directory)) == "alhazen/mac"
        run = finish(workspace, workspace.start(request))
        assert run["status"] == "completed", run["log"]
        assert (run["rig"], run["rig_name"], run["rig_source"]) == ("alhazen/mac", "mac", "alhazen")
        folder = Path(run["directory"])
        # A shared rig is a whole file: copied as it is, from the project's alhazen.
        assert (folder / "rig.yaml").read_bytes() == project_alhazen["mac"].read_bytes()
        assert not (folder / "rig-source.yaml").exists()

    def test_an_extending_rig_launches_as_its_file_and_its_copy_is_the_merged_rig(
        self, workspace, project_alhazen
    ):
        root = Path(workspace.projects[0]["path"])
        own = root / "configs/rig-lab.yaml"
        request = launch(workspace, "configs/rig-lab.yaml")
        assert rig_argument(workspace._command(request, workspace.directory)) == str(own)
        run = finish(workspace, workspace.start(request))
        assert (run["rig"], run["rig_name"], run["rig_source"]) == (
            "configs/rig-lab.yaml",
            "lab",
            "experiment",
        )
        folder = Path(run["directory"])
        copy = (folder / "rig.yaml").read_text(encoding="utf-8")
        merged = yaml.safe_load(copy)
        # What ran, readable without either file: the project's shared lab
        # (99 cm wide, live monitor on) with the experiment's distance over it.
        assert merged == rig_mapping(own, shared=project_alhazen).values
        assert merged["monitor"]["width_cm"] == 99.0
        assert merged["monitor"]["distance_cm"] == 45.0
        assert merged["live_monitor"] == {"enabled": True}
        assert "extends" not in merged
        assert str(project_alhazen["lab"]) in copy.splitlines()[1]
        # And the experiment's file as written, beside it.
        assert (folder / "rig-source.yaml").read_bytes() == own.read_bytes()

    def test_a_script_gets_the_shared_rigs_file(self, workspace, project_alhazen):
        """A standalone preview/movie script hands --rig to load_rig, which
        takes a file, not a name."""
        package = Path(workspace.projects[0]["path"]) / "src/demo"
        package.mkdir(parents=True)
        (package / "preview.py").write_text(
            "parser.add_argument('--out')\nparser.add_argument('--rig')\n"
            "if __name__ == '__main__': main()\n"
        )
        command = workspace._command(
            launch(workspace, "alhazen/lab", mode="demo.preview"), workspace.directory
        )
        assert rig_argument(command) == str(project_alhazen["lab"])

    @pytest.mark.parametrize(
        "rig, message",
        [
            ("alhazen/vpixx", r"ships no shared rig 'vpixx'; its shared rigs are lab, mac"),
            ("../outside.yaml", "inside"),
            ("configs/rig-missing.yaml", "existing rig"),
        ],
    )
    def test_refusals_name_what_to_choose(self, workspace, rig, message):
        with pytest.raises(ValueError, match=message):
            workspace.start(launch(workspace, rig))
        assert workspace.runs == {}

    def test_a_shared_file_that_has_gone_asks_for_a_new_registration(
        self, workspace, project_alhazen
    ):
        project_alhazen["mac"].unlink()
        with pytest.raises(ValueError, match="no longer exists.*register it again"):
            workspace.start(launch(workspace, "alhazen/mac"))

    def test_an_invalid_merged_rig_is_refused_before_a_run_exists(self, workspace):
        own = Path(workspace.projects[0]["path"]) / "configs/rig-lab.yaml"
        own.write_text("extends: lab\nmonitor:\n  distance_cm: -1\n", encoding="utf-8")
        with pytest.raises(Exception, match="distance_cm must be > 0"):
            workspace.start(launch(workspace, "configs/rig-lab.yaml"))
        assert workspace.runs == {}


class TestARigSayingDashboard:
    """`dashboard:` is the live monitor's rig section as alhazen spelled it
    before 1.9. The workspace's own loader is 2.0's, which refuses it — but
    the child reads the rig with the PROJECT's alhazen, so the launch check
    must read it the way that alhazen does: a project on 1.x launches, one on
    2.0 is refused in 2.0's words. A 1.x rig is a whole file (`extends` is
    2.0's), so the project's sim rig is the one rewritten here."""

    SIM = "configs/rig-sim.yaml"

    @pytest.fixture
    def saying_dashboard(self, workspace):
        own = Path(workspace.projects[0]["path"]) / self.SIM
        text = own.read_text(encoding="utf-8")
        assert "live_monitor:" in text
        own.write_text(text.replace("live_monitor:", "dashboard:"), encoding="utf-8")
        return own

    def on(self, workspace, monkeypatch, version):
        monkeypatch.setitem(workspace.projects[0], "alhazen_version", version)

    def command(self, workspace, **overrides):
        return workspace._command(launch(workspace, self.SIM, **overrides), workspace.directory)

    @pytest.mark.parametrize("version", ["1.5.0", "1.8.0", "1.9.0", "1.10.1"])
    def test_a_project_on_1_x_launches_it(self, workspace, monkeypatch, saying_dashboard, version):
        self.on(workspace, monkeypatch, version)
        # Handed over as the file, which the child reads with its own alhazen.
        assert rig_argument(self.command(workspace)) == str(saying_dashboard)
        assert "dashboard:" in saying_dashboard.read_text(encoding="utf-8")

    def test_the_check_still_catches_a_bad_setting_inside_it(
        self, workspace, monkeypatch, saying_dashboard
    ):
        # Moved to the new name for the check, not waved through.
        text = saying_dashboard.read_text(encoding="utf-8")
        saying_dashboard.write_text(
            text.replace("  enabled: false", "  enabled: maybe"), encoding="utf-8"
        )
        self.on(workspace, monkeypatch, "1.8.0")
        with pytest.raises(ConfigError, match=r"live_monitor\.enabled"):
            self.command(workspace)

    @pytest.mark.parametrize("version", ["2.0.0", "2.1.0", "3.0.0"])
    def test_a_project_on_2_0_is_refused_naming_the_new_key(
        self, workspace, monkeypatch, saying_dashboard, version
    ):
        self.on(workspace, monkeypatch, version)
        with pytest.raises(ConfigError, match=r"renamed to `live_monitor:` in alhazen 1\.9"):
            workspace.start(launch(workspace, self.SIM))
        assert workspace.runs == {}

    def test_both_sections_are_refused_whatever_the_projects_alhazen(self, workspace, monkeypatch):
        # Every alhazen refuses this rig: before 1.9 `live_monitor:` is
        # unknown, 1.9 and 1.10 refuse the pair, 2.0 refuses `dashboard:`.
        own = Path(workspace.projects[0]["path"]) / self.SIM
        own.write_text(
            own.read_text(encoding="utf-8") + "dashboard:\n  enabled: true\n", encoding="utf-8"
        )
        self.on(workspace, monkeypatch, "1.8.0")
        with pytest.raises(ConfigError, match="delete it"):
            self.command(workspace)

    def test_a_project_whose_version_cannot_be_read_is_asked_to_register_again(
        self, workspace, monkeypatch, saying_dashboard
    ):
        # Which reading is right depends on the version; guessing is refused.
        self.on(workspace, monkeypatch, "unknown")
        with pytest.raises(ValueError, match="register it again"):
            self.command(workspace)

    def test_a_rig_without_it_asks_no_version(self, workspace, monkeypatch):
        # A standalone script is launched with no version question at all,
        # and only a rig that says `dashboard:` adds one.
        package = Path(workspace.projects[0]["path"]) / "src/demo"
        package.mkdir(parents=True)
        (package / "preview.py").write_text(
            "parser.add_argument('--out')\nparser.add_argument('--rig')\n"
            "if __name__ == '__main__': main()\n"
        )
        self.on(workspace, monkeypatch, "unknown")
        assert rig_argument(self.command(workspace, mode="demo.preview")).endswith("rig-sim.yaml")


class TestTheSummary:
    def test_an_extending_rig_is_summarised_merged(self, workspace):
        summary = workspace.rig(workspace.projects[0]["id"], "configs/rig-lab.yaml")
        assert (summary["name"], summary["source"], summary["extends"]) == (
            "lab",
            "experiment",
            "lab",
        )
        # The live monitor setting is the shared rig's; the experiment's file
        # says nothing about it.
        assert summary["values"]["live_monitor"] == {"enabled": True}
        assert summary["values"]["monitor"]["distance_cm"] == 45.0

    def test_a_shared_rig_is_summarised_from_the_projects_file(self, workspace):
        summary = workspace.rig(workspace.projects[0]["id"], "alhazen/lab")
        assert (summary["name"], summary["source"], summary["extends"]) == (
            "lab",
            "alhazen",
            None,
        )
        assert summary["values"]["monitor"]["width_cm"] == 99.0

    def test_the_page_asks_for_it_over_http(self, http, workspace):  # noqa: F811
        call, _ = http
        key = workspace.projects[0]["id"]
        status, _, body = call(f"/api/rig?project={key}&rig=alhazen%2Flab")
        assert status == 200
        assert json.loads(body)["values"]["monitor"]["width_cm"] == 99.0
        status, _, body = call(f"/api/rig?project={key}&rig=alhazen%2Fnope")
        assert status == 400 and "ships no shared rig 'nope'" in json.loads(body)["error"]


class TestARegistrationFromBeforeSharedRigs:
    @pytest.fixture
    def old(self, workspace):
        """The project's record as a workspace before shared rigs wrote it:
        no `shared_rigs` at all, which is not the same as an empty list."""
        del workspace.projects[0]["shared_rigs"]
        workspace._save_projects()
        return Workspace(workspace.directory)

    def test_it_lists_no_shared_rigs_and_says_why(self, old):
        described = old.describe(old.projects[0]["id"])
        assert [rig["source"] for rig in described["rigs"]] == ["experiment", "experiment"]
        assert described["rigs_note"] == REGISTER_AGAIN

    @pytest.mark.parametrize("rig", ["alhazen/mac", "configs/rig-lab.yaml"])
    def test_a_shared_or_extending_rig_asks_for_a_new_registration(self, old, rig):
        with pytest.raises(ValueError, match="open Project settings and save"):
            old._command(launch(old, rig), old.directory)

    def test_a_whole_rig_file_still_launches(self, old):
        command = old._command(launch(old, "configs/rig-sim.yaml"), old.directory)
        assert rig_argument(command).endswith("rig-sim.yaml")

    def test_registering_again_fills_them_in(self, old):
        old.add(old.projects[0]["path"], sys.executable)
        assert old.describe(old.projects[0]["id"])["rigs_note"] is None


def test_the_workspaces_own_shared_rigs_are_not_the_projects(workspace):
    """Guard for the fixture above: the project's lab really differs from the
    workspace's, so the merge tests would notice a read of the wrong one."""
    ours = yaml.safe_load((SHARED_RIG_DIR / "rig-lab.yaml").read_text(encoding="utf-8"))
    assert ours["monitor"]["width_cm"] != 99.0
