"""The workspace's Data view, server side: data folders, runs, records, tables.

Built on small data folders written here with the real layout (session.json,
`<base>_trials.csv`, report.yaml, figures/…), in both the 2.0 layout
(``v<version>/sub-…``) and the one before it, so nothing depends on a data
folder outside the repository. The project's rigs are real rig files — one
whole, one that extends a shared rig — so the folders are found the way the
view finds them for a real experiment.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from http.client import HTTPConnection
from pathlib import Path

import pytest
import yaml
from test_workspace import http  # noqa: F401  (a fixture)

from alhazen.cli import workspace as workspace_module
from alhazen.cli import workspace_data as data_module
from alhazen.cli.dashboard import DATA_FILE_CSP, SAVED_PAGE_CSP
from alhazen.cli.workspace import Workspace
from alhazen.cli.workspace_data import SAVED_PAGES, DataView, data_roots

RIG = Path(__file__).parents[2] / "examples/minimal_fixation/rig-sim.yaml"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


def rig_text(data_root: str | None) -> str:
    values = yaml.safe_load(RIG.read_text(encoding="utf-8"))
    values.pop("data_root", None)
    if data_root is not None:
        values["data_root"] = data_root
    return yaml.safe_dump(values)


def make_run(
    folder: Path,
    *,
    card: dict | None = None,
    trials: str | None = "trial_index,outcome,success,rt_ms\n1,HIT,True,300\n2,MISS,False,\n",
    report_rows: int | None = None,
    snapshot: dict | None = None,
    base: str = "sub-01_ses-001_run-01_task-demo_20260929",
) -> Path:
    """One run folder, with the files a real run writes (what the test asks for)."""
    (folder / "figures").mkdir(parents=True)
    if card is not None:
        (folder / "session.json").write_text(json.dumps(card), encoding="utf-8")
    if snapshot is not None:
        (folder / "config_snapshot.yaml").write_text(yaml.safe_dump(snapshot), encoding="utf-8")
    if trials is not None:
        (folder / f"{base}_trials.csv").write_text(trials, encoding="utf-8", newline="")
    if report_rows is not None:
        (folder / "report.yaml").write_text(
            yaml.safe_dump({"trials": {"n_rows": report_rows}}), encoding="utf-8"
        )
    # Bytes, so the line ends are "\n" on Windows too (write_text translates).
    (folder / "session.log").write_bytes(b"started\nfinished\n")
    return folder


def card_for(subject: str, run: int, **extra) -> dict:
    return {
        "schema_version": 1,
        "experiment": {"name": "demo", "version": "1.2.0", "git": "abc"},
        "task": "demo",
        "mode": "run",
        "subject": {"id": subject, "initials": "HD"},
        "session": 1,
        "run": run,
        "seed": 7,
        "date": "20260929",
        "created": "2026-09-29T10:00:00+00:00",
        "rig": {"name": "sim", "source": "experiment", "file": "C:/x/configs/rig-sim.yaml"},
        "params_file": None,
        "alhazen": {"version": "2.0.1"},
        "files": {"trials": f"sub-{subject}_ses-001_run-0{run}_task-demo_20260929_trials.csv"},
        **extra,
    }


@pytest.fixture
def shared(tmp_path):
    """The project's alhazen's shared rigs: a lab writing to data/, a mac
    writing to shared-data/ (a folder nobody has created)."""
    folder = tmp_path / "project-env" / "rigs"
    folder.mkdir(parents=True)
    (folder / "rig-lab.yaml").write_text(rig_text("data"), encoding="utf-8")
    (folder / "rig-mac.yaml").write_text(rig_text("shared-data"), encoding="utf-8")
    return {"lab": folder / "rig-lab.yaml", "mac": folder / "rig-mac.yaml"}


@pytest.fixture
def project(tmp_path):
    """An experiment with four rigs — whole (data/), extending the shared mac
    with its own data_root (bench-data/, missing), one that cannot be read,
    and one with no data_root — and data in data/ (both layouts) and
    data-rehearsal/."""
    root = tmp_path / "experiment"
    (root / "configs").mkdir(parents=True)
    (root / "configs/rig-sim.yaml").write_text(rig_text("data"), encoding="utf-8")
    (root / "configs/rig-bench.yaml").write_text(
        "extends: mac\ndata_root: bench-data\n", encoding="utf-8"
    )
    (root / "configs/rig-broken.yaml").write_text("extends: nothing-shared\n", encoding="utf-8")
    (root / "configs/rig-noroot.yaml").write_text(rig_text(None), encoding="utf-8")
    (root / "run.py").write_text("print('hi')\n")
    data = root / "data"
    make_run(
        data / "v1.2.0/sub-01/ses-001/run-01_task-demo",
        card=card_for("01", 1),
        report_rows=2,
        base="sub-01_ses-001_run-01_task-demo_20260929",
    )
    make_run(
        data / "v1.2.0/sub-02/ses-001/run-02_task-demo",
        card=card_for("02", 2),
        trials="trial_index,outcome,success,rt_ms,block\n1,HIT,True,280,1\n",
        base="sub-02_ses-001_run-02_task-demo_20260929",
    )
    make_run(
        data / "sub-old/ses-001/run-01_task-demo",
        snapshot={
            # As config/snapshot.py writes it: sources inside config.
            "config": {"sources": {"rig": "configs/rig-laptop.yaml"}},
            "provenance": {"created": "2026-01-02T03:04:05+00:00"},
        },
        base="sub-old_ses-001_run-01_task-demo_20260102",
    )
    make_run(
        root / "data-rehearsal/v1.2.0/sub-sim/ses-001/run-01_task-demo",
        card={**card_for("sim", 1), "mode": "simulate", "files": {}},
        base="sub-sim_ses-001_run-01_task-demo_20260929",
    )
    # Not runs: the registry, a notes folder.
    (data / "participants.tsv").write_text("participant_id\n", encoding="utf-8")
    (data / "notes").mkdir()
    return root


@pytest.fixture
def workspace(tmp_path, monkeypatch, shared, project):
    monkeypatch.setattr(
        workspace_module,
        "probe_interpreter",
        lambda python, path: {
            "alhazen_version": "2.0.1",
            "python_version": "stub",
            "shared_rigs": [{"name": n, "path": str(p)} for n, p in shared.items()],
        },
    )
    space = Workspace(tmp_path / "state")
    space.add(str(project), sys.executable)
    yield space
    space.close()


@pytest.fixture
def view(workspace):
    return DataView(workspace)


def ids(view: DataView, workspace: Workspace) -> tuple[str, dict[str, str]]:
    """The project id and the data folders' ids by (folder name, kind)."""
    key = workspace.projects[0]["id"]
    roots = view.roots(key)["roots"]
    return key, {r["name"]: r["id"] for r in roots}


class TestDataFolders:
    def test_folders_come_from_every_rig_with_their_rehearsal_siblings(self, view, workspace):
        key = workspace.projects[0]["id"]
        answer = view.roots(key)
        existing = {(r["name"], r["kind"]): r["rigs"] for r in answer["roots"]}
        # data/ is written by the experiment's sim rig and the shared lab;
        # its rehearsal sibling by the same rigs, in test and simulate.
        assert existing == {
            ("data", "real"): ["sim", "alhazen/lab"],
            ("data-rehearsal", "rehearsal"): ["sim", "alhazen/lab"],
        }
        missing = {(r["name"], r["kind"]): r["rigs"] for r in answer["missing"]}
        # The rig that extends the shared mac overrides its data_root; the
        # shared mac itself keeps its own.
        assert missing == {
            ("bench-data", "real"): ["bench"],
            ("bench-data-rehearsal", "rehearsal"): ["bench"],
            ("shared-data", "real"): ["alhazen/mac"],
            ("shared-data-rehearsal", "rehearsal"): ["alhazen/mac"],
        }
        problems = "\n".join(answer["problems"])
        assert "Rig broken cannot be read" in problems and "nothing-shared" in problems
        assert "Rig noroot names no data_root" in problems

    def test_a_relative_data_root_is_the_project_folders(self, view, workspace, project):
        key = workspace.projects[0]["id"]
        paths = {r["name"]: r["path"] for r in view.roots(key)["roots"]}
        assert paths["data"] == str((project / "data").resolve())

    def test_an_absolute_data_root_is_kept_as_it_is(self, workspace, tmp_path, project):
        elsewhere = tmp_path / "big-disk" / "exp"
        elsewhere.mkdir(parents=True)
        (project / "configs/rig-sim.yaml").write_text(rig_text(str(elsewhere)), encoding="utf-8")
        existing, _, _ = data_roots(workspace.describe(workspace.projects[0]["id"]))
        assert str(elsewhere.resolve()) in {str(r.path) for r in existing}

    def test_a_project_registered_before_shared_rigs_says_so(self, workspace):
        record = workspace.describe(workspace.projects[0]["id"])
        record["rigs_note"] = workspace_module.REGISTER_AGAIN
        _, _, problems = data_roots(record)
        assert problems[0] == workspace_module.REGISTER_AGAIN

    def test_an_unknown_folder_id_is_refused(self, view, workspace):
        key = workspace.projects[0]["id"]
        with pytest.raises(FileNotFoundError, match="Unknown data folder"):
            view.runs(key, "not-an-id")

    def test_a_folder_that_vanished_says_so(self, view, workspace, project):
        key, roots = ids(view, workspace)
        # Removed since the folder was listed, as someone tidying up would.
        shutil.rmtree(project / "data-rehearsal")
        with pytest.raises(FileNotFoundError, match="no longer exists"):
            view.runs(key, roots["data-rehearsal"])

    def test_an_unknown_project_is_refused(self, view):
        with pytest.raises(ValueError, match="not registered"):
            view.roots("nope")


class TestRunListing:
    def test_both_layouts_are_listed_with_what_their_records_say(self, view, workspace):
        key, roots = ids(view, workspace)
        answer = view.runs(key, roots["data"])
        rows = {row["id"]: row for row in answer["runs"]}
        assert sorted(rows) == [
            "sub-old/ses-001/run-01_task-demo",
            "v1.2.0/sub-01/ses-001/run-01_task-demo",
            "v1.2.0/sub-02/ses-001/run-02_task-demo",
        ]
        new = rows["v1.2.0/sub-01/ses-001/run-01_task-demo"]
        assert new["version"] == "1.2.0" and new["layout"] == "2.0"
        assert (new["subject"], new["initials"], new["session"], new["run"]) == ("01", "HD", 1, 1)
        assert (new["task"], new["mode"], new["date"], new["rig"]) == (
            "demo",
            "run",
            "2026-09-29",
            "sim",
        )
        # From report.yaml: exact.
        assert (new["trials"], new["trials_counted"]) == (2, "report")
        # No report: the trials file's lines, marked as a count of lines.
        second = rows["v1.2.0/sub-02/ses-001/run-02_task-demo"]
        assert (second["trials"], second["trials_counted"]) == (1, "lines")
        old = rows["sub-old/ses-001/run-01_task-demo"]
        assert old["version"] is None and old["layout"] == "pre-2.0"
        assert (old["rig"], old["date"], old["mode"]) == ("laptop", "2026-01-02", None)
        assert old["trials"] == 2 and old["problems"] == []

    def test_a_broken_card_is_named_and_the_run_still_listed(self, view, workspace, project):
        key, roots = ids(view, workspace)
        card = project / "data/v1.2.0/sub-01/ses-001/run-01_task-demo/session.json"
        card.write_text("{not json", encoding="utf-8")
        rows = {r["id"]: r for r in view.runs(key, roots["data"])["runs"]}
        row = rows["v1.2.0/sub-01/ses-001/run-01_task-demo"]
        assert any("session.json cannot be read" in p for p in row["problems"])

    def test_a_run_that_never_started_says_so(self, view, workspace, project):
        key, roots = ids(view, workspace)
        (project / "data/v1.2.0/sub-03/ses-001/run-01_task-demo").mkdir(parents=True)
        rows = {r["id"]: r for r in view.runs(key, roots["data"])["runs"]}
        assert "did not start" in rows["v1.2.0/sub-03/ses-001/run-01_task-demo"]["problems"][0]

    def test_counting_lines_handles_a_last_line_without_an_end(self, tmp_path):
        path = tmp_path / "t.csv"
        path.write_bytes(b"a,b\n1,2\n3,4")
        assert data_module._count_lines(path) == 2
        path.write_bytes(b"a,b\n")
        assert data_module._count_lines(path) == 0


class TestRunDetail:
    def test_the_detail_has_the_card_files_records_tables_and_figures(
        self, view, workspace, project
    ):
        key, roots = ids(view, workspace)
        run_id = "v1.2.0/sub-01/ses-001/run-01_task-demo"
        folder = project / "data" / run_id
        (folder / "figures/live_monitor.html").write_text("<html></html>", encoding="utf-8")
        (folder / "figures/psychometric.png").write_bytes(PNG)
        (folder / "rig.yaml").write_text("x: 1\n", encoding="utf-8")
        detail = view.run(key, roots["data"], run_id)
        assert detail["card"]["subject"] == {"id": "01", "initials": "HD"}
        assert detail["card_error"] is None
        names = [f["name"] for f in detail["files"]]
        assert "figures/psychometric.png" in names and "session.json" in names
        assert detail["texts"] == ["session.json", "rig.yaml", "report.yaml", "session.log"]
        assert detail["tables"] == [
            {"kind": "trials", "name": "sub-01_ses-001_run-01_task-demo_20260929_trials.csv"}
        ]
        assert detail["images"] == ["figures/psychometric.png"]
        assert detail["page"] == "figures/live_monitor.html"

    def test_a_pre_2_0_run_offers_its_old_monitor_page(self, view, workspace, project):
        key, roots = ids(view, workspace)
        run_id = "sub-old/ses-001/run-01_task-demo"
        (project / "data" / run_id / "figures/dashboard.html").write_text("<html>", "utf-8")
        detail = view.run(key, roots["data"], run_id)
        assert detail["page"] == "figures/dashboard.html"
        assert detail["card"] is None and detail["texts"] == [
            "config_snapshot.yaml",
            "session.log",
        ]

    def test_text_records_whole_and_the_log_as_its_tail(
        self, view, workspace, project, monkeypatch
    ):
        key, roots = ids(view, workspace)
        run_id = "v1.2.0/sub-01/ses-001/run-01_task-demo"
        answer = view.text(key, roots["data"], run_id, "session.json")
        assert json.loads(answer["text"])["run"] == 1 and not answer["truncated"]
        log = project / "data" / run_id / "session.log"
        log.write_bytes(b"x" * 100 + b"THE END\n")
        monkeypatch.setattr(data_module, "LOG_TAIL_BYTES", 10)
        tail = view.text(key, roots["data"], run_id, "session.log")
        assert tail["truncated"] and tail["tail"] and tail["text"].endswith("THE END\n")
        assert tail["size"] == 108

    def test_only_known_records_are_read_as_text(self, view, workspace):
        key, roots = ids(view, workspace)
        run_id = "v1.2.0/sub-01/ses-001/run-01_task-demo"
        for name in ["../../../../run.py", "figures/x.png", "other.txt"]:
            with pytest.raises(ValueError, match="not a record"):
                view.text(key, roots["data"], run_id, name)
        with pytest.raises(FileNotFoundError):
            view.text(key, roots["data"], run_id, "params.yaml")  # not written by this run


class TestTables:
    def test_one_runs_trials_as_text_cells(self, view, workspace):
        key, roots = ids(view, workspace)
        table = view.table(key, roots["data"], ["v1.2.0/sub-01/ses-001/run-01_task-demo"], "trials")
        assert table["columns"] == ["trial_index", "outcome", "success", "rt_ms"]
        # The file's own text, empty cell included: numbers are the client's.
        assert table["rows"] == [["1", "HIT", "True", "300"], ["2", "MISS", "False", ""]]
        assert (table["total"], table["capped"], table["added"]) == (2, False, [])

    def test_pooled_runs_get_run_subject_and_session_columns(self, view, workspace):
        key, roots = ids(view, workspace)
        table = view.table(
            key,
            roots["data"],
            ["v1.2.0/sub-01/ses-001/run-01_task-demo", "v1.2.0/sub-02/ses-001/run-02_task-demo"],
            "trials",
        )
        assert table["added"] == ["run", "subject", "session"]
        # The union of the files' columns; the first file has no `block`.
        assert table["columns"][3:] == ["trial_index", "outcome", "success", "rt_ms", "block"]
        assert table["rows"][0][:3] == ["v1.2.0/sub-01/ses-001/run-01_task-demo", "01", "1"]
        assert table["rows"][0][-1] == ""
        assert table["rows"][2][1:3] == ["02", "1"] and table["rows"][2][-1] == "1"
        assert [(f["rows"], f["loaded"]) for f in table["files"]] == [(2, 2), (1, 1)]

    def test_an_added_column_never_hides_a_csv_column_of_that_name(self, view, workspace, project):
        key, roots = ids(view, workspace)
        for run_id in ["v1.2.0/sub-01/ses-001/run-01_task-demo"]:
            folder = project / "data" / run_id
            (next(folder.glob("*_trials.csv"))).write_text("subject,x\nS,1\n", encoding="utf-8")
        table = view.table(
            key,
            roots["data"],
            ["v1.2.0/sub-01/ses-001/run-01_task-demo", "sub-old/ses-001/run-01_task-demo"],
            "trials",
        )
        assert table["added"] == ["run", "subject (folder)", "session"]
        assert "subject" in table["columns"][3:]

    def test_the_row_cap_is_said(self, view, workspace, monkeypatch):
        monkeypatch.setattr(data_module, "MAX_TABLE_ROWS", 2)
        key, roots = ids(view, workspace)
        table = view.table(
            key,
            roots["data"],
            ["v1.2.0/sub-01/ses-001/run-01_task-demo", "v1.2.0/sub-02/ses-001/run-02_task-demo"],
            "trials",
        )
        assert len(table["rows"]) == 2 and table["total"] == 3 and table["capped"]
        assert table["limit"] == 2
        # The second run came after the cap: said per file.
        assert [(f["rows"], f["loaded"]) for f in table["files"]] == [(2, 2), (1, 0)]

    def test_a_damaged_row_is_named_and_kept(self, view, workspace, project):
        key, roots = ids(view, workspace)
        run_id = "v1.2.0/sub-01/ses-001/run-01_task-demo"
        path = next((project / "data" / run_id).glob("*_trials.csv"))
        path.write_text("a,b\n1,2\n3\n4,5,6\n", encoding="utf-8")
        table = view.table(key, roots["data"], [run_id], "trials")
        assert table["rows"] == [["1", "2"], ["3", ""], ["4", "5"]]
        assert len(table["problems"]) == 2 and "line 3 has 1 cells" in table["problems"][0]

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            (b"a,b\n" + b'"' + b"x" * 200_000 + b'",1\n', "cannot be parsed"),
            (b"a,b\n\xff\xfe,1\n", "not UTF-8"),
            (b"", "empty"),
        ],
        ids=["a cell over the csv field limit", "not utf-8", "empty"],
    )
    def test_a_file_that_cannot_be_read_fails_loudly(
        self, view, workspace, project, content, message
    ):
        key, roots = ids(view, workspace)
        run_id = "v1.2.0/sub-01/ses-001/run-01_task-demo"
        next((project / "data" / run_id).glob("*_trials.csv")).write_bytes(content)
        with pytest.raises(ValueError, match=message):
            view.table(key, roots["data"], [run_id], "trials")

    def test_a_run_without_the_table_fails_the_pool(self, view, workspace):
        key, roots = ids(view, workspace)
        with pytest.raises(FileNotFoundError, match="has no events table"):
            view.table(key, roots["data"], ["v1.2.0/sub-01/ses-001/run-01_task-demo"], "events")

    def test_bad_requests_are_refused(self, view, workspace):
        key, roots = ids(view, workspace)
        run_id = "v1.2.0/sub-01/ses-001/run-01_task-demo"
        with pytest.raises(ValueError, match="Unknown table"):
            view.table(key, roots["data"], [run_id], "secrets")
        with pytest.raises(ValueError, match="at least one"):
            view.table(key, roots["data"], [], "trials")
        with pytest.raises(ValueError, match="twice"):
            view.table(key, roots["data"], [run_id, run_id], "trials")


class TestConfinement:
    @pytest.mark.parametrize(
        "run_id",
        [
            "..",
            "../experiment/data/v1.2.0/sub-01/ses-001/run-01_task-demo",
            "v1.2.0/sub-01/ses-001/../../../../run.py",
            "v1.2.0/sub-01/ses-001",
            "C:/Windows/sub-01/ses-001/run-01",
            "/etc/sub-01/ses-001/run-01",
            "v1.2.0\\sub-01\\ses-001\\run-01_task-demo",
            "notes/sub-01/ses-001/run-01",
            "v1.2.0/sub-/ses-001/run-01",
            "v1.2.0/sub-01/ses-1a/run-01",
        ],
    )
    def test_a_run_id_must_have_a_run_folders_shape(self, view, workspace, run_id):
        key, roots = ids(view, workspace)
        with pytest.raises(ValueError, match="Not a run folder name"):
            view.run(key, roots["data"], run_id)

    def test_a_run_id_that_is_not_there(self, view, workspace):
        key, roots = ids(view, workspace)
        with pytest.raises(FileNotFoundError, match="no longer in"):
            view.run(key, roots["data"], "v9/sub-01/ses-001/run-01_task-demo")

    def test_files_are_images_under_figures_inside_the_run(self, view, workspace, project):
        key, roots = ids(view, workspace)
        run_id = "v1.2.0/sub-01/ses-001/run-01_task-demo"
        folder = project / "data" / run_id
        (folder / "figures/a.png").write_bytes(PNG)
        (folder / "figures/b.svg").write_text("<svg/>", encoding="utf-8")
        assert view.file(key, roots["data"], run_id, "figures/a.png")[1] == "image/png"
        assert view.file(key, roots["data"], run_id, "figures/b.svg")[1] == "image/svg+xml"
        with pytest.raises(ValueError, match="inside"):
            view.file(key, roots["data"], run_id, "../../../../../configs/x.png")
        with pytest.raises(ValueError, match="Only images"):
            view.file(key, roots["data"], run_id, "session.json")
        (folder / "top.png").write_bytes(PNG)
        with pytest.raises(ValueError, match="Only images"):
            view.file(key, roots["data"], run_id, "top.png")
        # An absolute path, even one inside the run, is not a figures/ name.
        with pytest.raises(ValueError, match="Only images"):
            view.file(key, roots["data"], run_id, str(folder / "figures/a.png"))

    def test_a_symlink_out_of_the_folder_is_refused(self, view, workspace, project, tmp_path):
        key, roots = ids(view, workspace)
        outside = tmp_path / "outside" / "run"
        make_run(outside, card=card_for("99", 1))
        link = project / "data/v1.2.0/sub-99/ses-001/run-01_task-demo"
        link.parent.mkdir(parents=True)
        secret = tmp_path / "secret.png"
        secret.write_bytes(PNG)
        run_id = "v1.2.0/sub-01/ses-001/run-01_task-demo"
        try:
            link.symlink_to(outside, target_is_directory=True)
            (project / "data" / run_id / "figures/escape.png").symlink_to(secret)
        except OSError as exc:
            # Windows refuses symlinks to an account without the privilege
            # (ERROR_PRIVILEGE_NOT_HELD, 1314), as in test_workspace; CI has it.
            if getattr(exc, "winerror", None) != 1314:
                raise
            pytest.skip("symlink creation needs a privilege this account lacks")
        listing = view.runs(key, roots["data"])
        assert "v1.2.0/sub-99/ses-001/run-01_task-demo" not in [r["id"] for r in listing["runs"]]
        assert any("outside this data folder" in p for p in listing["problems"])
        with pytest.raises(ValueError, match="inside"):
            view.run(key, roots["data"], "v1.2.0/sub-99/ses-001/run-01_task-demo")
        with pytest.raises(ValueError, match="inside"):
            view.file(key, roots["data"], run_id, "figures/escape.png")
        assert "figures/escape.png" not in view.run(key, roots["data"], run_id)["images"]


class TestSavedPages:
    def test_the_page_names_match_the_monitors(self):
        from alhazen.live_monitor.runtime import SAVED_PAGE

        assert SAVED_PAGES[0] == f"figures/{SAVED_PAGE}"

    def test_a_ticket_opens_one_page_until_it_lapses(self, view, workspace, project, monkeypatch):
        key, roots = ids(view, workspace)
        run_id = "v1.2.0/sub-01/ses-001/run-01_task-demo"
        page = project / "data" / run_id / "figures/live_monitor.html"
        page.write_text("<html>monitor</html>", encoding="utf-8")
        url = view.page_ticket(key, roots["data"], run_id, "figures/live_monitor.html")["url"]
        ticket = url.removeprefix("/data-page/")
        assert view.page(ticket) == page.resolve()
        later = time.monotonic() + data_module.TICKET_TTL_S + 1
        monkeypatch.setattr(data_module.time, "monotonic", lambda: later)
        with pytest.raises(FileNotFoundError, match="expired"):
            view.page(ticket)
        with pytest.raises(FileNotFoundError, match="expired"):
            view.page("made-up")

    def test_only_saved_monitor_pages_get_tickets(self, view, workspace):
        key, roots = ids(view, workspace)
        run_id = "v1.2.0/sub-01/ses-001/run-01_task-demo"
        with pytest.raises(ValueError, match="not a saved monitor page"):
            view.page_ticket(key, roots["data"], run_id, "session.json")
        with pytest.raises(FileNotFoundError):
            view.page_ticket(key, roots["data"], run_id, "figures/dashboard.html")

    def test_old_tickets_make_room(self, view, workspace, project, monkeypatch):
        monkeypatch.setattr(data_module, "MAX_TICKETS", 2)
        key, roots = ids(view, workspace)
        run_id = "v1.2.0/sub-01/ses-001/run-01_task-demo"
        (project / "data" / run_id / "figures/live_monitor.html").write_text("x", "utf-8")
        urls = [
            view.page_ticket(key, roots["data"], run_id, "figures/live_monitor.html")["url"]
            for _ in range(3)
        ]
        assert len(view._tickets) == 2
        view.page(urls[-1].removeprefix("/data-page/"))


class TestRoutes:
    @pytest.fixture
    def routes(self, http, workspace):  # noqa: F811  (the imported fixture)
        call, server = http
        key = workspace.projects[0]["id"]
        roots = {r["name"]: r["id"] for r in server.data.roots(key)["roots"]}
        return call, server, key, roots["data"]

    def test_the_views_assets_are_served_with_their_types(self, routes):
        call, *_ = routes
        for path, kind in [
            ("/workspace_data.js", "text/javascript"),
            ("/workspace_plot.js", "text/javascript"),
            ("/workspace_data.css", "text/css"),
        ]:
            status, headers, _ = call(path)
            assert status == 200 and headers["Content-Type"].startswith(kind)
        page = call("/")[2].decode()
        for asset in ("/workspace_data.js", "/workspace_plot.js", "/workspace_data.css"):
            assert asset in page
        assert '<div id="data-view" hidden></div>' in page

    def test_json_routes_answer_and_refuse(self, routes):
        call, _, key, root = routes
        status, headers, body = call(f"/api/data/roots?project={key}")
        assert status == 200 and headers["Content-Type"].startswith("application/json")
        assert {r["name"] for r in json.loads(body)["roots"]} == {"data", "data-rehearsal"}
        runs = json.loads(call(f"/api/data/runs?project={key}&root={root}")[2])["runs"]
        assert len(runs) == 3
        run_id = "v1.2.0/sub-01/ses-001/run-01_task-demo"
        assert call(f"/api/data/run?project={key}&root={root}&run={run_id}")[0] == 200
        both = f"{run_id},v1.2.0/sub-02/ses-001/run-02_task-demo"
        table = json.loads(
            call(f"/api/data/table?project={key}&root={root}&runs={both}&kind=trials")[2]
        )
        assert table["added"] == ["run", "subject", "session"]
        text = call(f"/api/data/text?project={key}&root={root}&run={run_id}&name=session.log")
        assert json.loads(text[2])["text"] == "started\nfinished\n"
        # Unknown folder, run or route: 404; a path-shaped trick: 400.
        assert call(f"/api/data/runs?project={key}&root=nope")[0] == 404
        gone = "v9/sub-1/ses-001/run-01"
        assert call(f"/api/data/run?project={key}&root={root}&run={gone}")[0] == 404
        assert call(f"/api/data/run?project={key}&root={root}&run=../../x")[0] == 400
        assert call(f"/api/data/nothing?project={key}")[0] == 404
        assert call("/api/data/roots?project=nope")[0] == 400

    def test_every_data_route_needs_the_token(self, routes):
        call, _, key, root = routes
        wrong = {"X-Alhazen-Token": "wrong"}
        assert call(f"/api/data/roots?project={key}", headers=wrong)[0] == 403
        run_id = "v1.2.0/sub-01/ses-001/run-01_task-demo"
        assert (
            call(
                f"/data/file?project={key}&root={root}&run={run_id}&name=figures/a.png",
                headers=wrong,
            )[0]
            == 403
        )
        # Spelled with an escape, as the other routes' test does.
        assert call(f"/%64ata/file?project={key}", headers=wrong)[0] == 403

    def test_figures_are_served_sandboxed(self, routes, project):
        call, server, key, root = routes
        run_id = "v1.2.0/sub-01/ses-001/run-01_task-demo"
        (project / "data" / run_id / "figures/a.png").write_bytes(PNG)
        (project / "data" / run_id / "figures/b.svg").write_text("<svg/>", encoding="utf-8")
        base = f"/data/file?project={key}&root={root}&run={run_id}&token={server.token}"
        # As an <img> asks for it: the token in the URL, no header.
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", f"{base}&name=figures/a.png")
        response = connection.getresponse()
        status, headers, data = response.status, dict(response.getheaders()), response.read()
        connection.close()
        assert status == 200 and data == PNG and headers["Content-Type"] == "image/png"
        assert headers["Content-Security-Policy"] == DATA_FILE_CSP
        status, headers, _ = call(f"{base}&name=figures/b.svg")
        assert headers["Content-Type"] == "image/svg+xml"
        assert headers["Content-Security-Policy"].startswith("sandbox;")
        assert call(f"{base}&name=session.json")[0] == 400
        assert call(f"{base}&name=figures/missing.png")[0] == 404

    def test_a_saved_page_opens_through_its_ticket_with_its_own_policy(self, routes, project):
        call, _, key, root = routes
        run_id = "v1.2.0/sub-01/ses-001/run-01_task-demo"
        page = project / "data" / run_id / "figures/live_monitor.html"
        page.write_text("<!doctype html><script>1</script>", encoding="utf-8")
        answer = call(
            f"/api/data/page?project={key}&root={root}&run={run_id}&name=figures/live_monitor.html"
        )
        url = json.loads(answer[2])["url"]
        # No token needed — or accepted as a substitute: the ticket is it.
        status, headers, body = call(url, headers={"X-Alhazen-Token": ""})
        assert status == 200 and body.startswith(b"<!doctype html>")
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert headers["Content-Security-Policy"] == SAVED_PAGE_CSP
        assert "connect-src 'none'" in SAVED_PAGE_CSP
        assert call("/data-page/forged", headers={"X-Alhazen-Token": ""})[0] == 404
        # The workspace's own page keeps its strict policy.
        assert "script-src 'self'" in call("/")[1]["Content-Security-Policy"]
