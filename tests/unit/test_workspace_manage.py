"""The management pages' API (workspace_manage.py) over real HTTP: people,
rig files, registration and history, with the server's own access checks."""

from __future__ import annotations

import json
import sys
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest
from tests.unit import test_workspace as base

from alhazen.cli.dashboard import DashboardServer
from alhazen.config.rigs import shared_rig_files

request_for = base.request_for
finish = base.finish
RIG = base.RIG


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    yield from base.workspace.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def http(workspace):
    server = DashboardServer(workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(path, body=None, headers=None, raw=False):
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        merged = {"X-Alhazen-Token": server.token, **(headers or {})}
        if body is not None:
            merged.setdefault("Content-Type", "application/json")
        conn.request(
            "POST" if body is not None else "GET",
            path,
            json.dumps(body) if body is not None else None,
            merged,
        )
        response = conn.getresponse()
        data = response.read()
        conn.close()
        if raw:
            return response.status, dict(response.getheaders()), data
        return response.status, json.loads(data) if data else None

    yield call, server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def pid(workspace) -> str:
    return workspace.projects[0]["id"]


class TestPeople:
    def test_add_select_edit_and_conflict(self, http, workspace):
        call, _ = http
        key = pid(workspace)
        status, out = call(
            "/api/manage/people/experimenter-add",
            {"project": key, "fields": {"name": "Zoë Lee", "initials": "zl"}},
        )
        assert status == 200
        who = out["record"]
        assert [a["id"] for a in out["people"]["assigned"]] == [who["id"]]  # assigned by default
        status, out = call(
            "/api/manage/people/subject-add",
            {"project": key, "fields": {"code": "007", "initials": "HD"}},
        )
        subject = out["record"]
        assert out["people"]["subjects"][0]["used"] is False
        assert not out["people"]["export"]["pending"]
        assert Path(out["people"]["files"]["subjects"]).is_file()
        ok = call(
            "/api/manage/people/subject-update",
            {"project": key, "id": subject["id"], "revision": 1, "fields": {"notes": "a"}},
        )
        assert ok[0] == 200
        stale = call(
            "/api/manage/people/subject-update",
            {"project": key, "id": subject["id"], "revision": 1, "fields": {"notes": "b"}},
        )
        assert stale[0] == 409 and "changed elsewhere" in stale[1]["error"]
        status, people = call(f"/api/manage/people?project={key}")
        assert people["subjects"][0]["notes"] == "a"

    def test_hostile_text_is_data(self, http, workspace):
        call, _ = http
        key = pid(workspace)
        assert (
            call(
                "/api/manage/people/subject-add", {"project": key, "fields": {"code": "<script>"}}
            )[0]
            == 400
        )
        hostile = '<img src=x onerror=alert(1)>\n=HYPERLINK("http://x")'
        status, out = call(
            "/api/manage/people/subject-add",
            {"project": key, "fields": {"code": "1", "notes": hostile}},
        )
        assert status == 200 and out["record"]["notes"] == hostile
        text = Path(out["people"]["files"]["subjects"]).read_text(encoding="utf-8-sig")
        assert "<img src=x onerror=alert(1)>" in text  # verbatim text, quoted as one cell

    def test_access_rules_hold_for_the_new_routes(self, http, workspace):
        call, server = http
        body = {"project": pid(workspace), "fields": {"name": "X"}}
        assert (
            call("/api/manage/people/experimenter-add", body, headers={"X-Alhazen-Token": "wrong"})[
                0
            ]
            == 403
        )
        assert (
            call(
                "/api/manage/people/experimenter-add",
                body,
                headers={"Origin": "http://evil.example"},
            )[0]
            == 403
        )
        assert (
            call(
                "/api/manage/people/experimenter-add", body, headers={"Content-Type": "text/plain"}
            )[0]
            == 400
        )
        assert call("/api/manage/nothing", {})[0] == 404
        assert call("/api/manage/people/nothing", {"project": pid(workspace)})[0] == 404
        assert call("/api/manage/people?project=unknown")[0] == 400
        assert workspace.people.experimenters() == []

    def test_participants_import_preview_then_apply(self, http, workspace):
        call, _ = http
        key = pid(workspace)
        data = Path(workspace.projects[0]["path"]) / "data"
        data.mkdir()
        (data / "participants.tsv").write_text(
            "participant_id\tinitials\tgroup\nsub-02\tAB\tx\nsub-01\tCD\ty\n", encoding="utf-8"
        )
        status, plan = call(f"/api/manage/participants-plan?project={key}")
        assert status == 200 and plan["counts"] == {"new": 2}
        status, out = call(
            "/api/manage/people/participants-apply", {"project": key, "digest": plan["digest"]}
        )
        assert status == 200 and out["record"]["applied"] == 2
        assert [s["code"] for s in out["people"]["subjects"]] == ["02", "01"]
        status, again = call(f"/api/manage/participants-plan?project={key}")
        assert again["counts"] == {"same": 2}
        stale = call(
            "/api/manage/people/participants-apply", {"project": key, "digest": plan["digest"]}
        )
        assert stale[0] == 409


class TestRigs:
    def test_a_new_rig_is_validated_and_never_overwrites(self, http, workspace):
        call, _ = http
        key = pid(workspace)
        root = Path(workspace.projects[0]["path"])
        text = RIG.read_text(encoding="utf-8")
        status, out = call("/api/manage/rig-save", {"project": key, "name": "bench", "text": text})
        assert status == 200 and out["path"] == "configs/rig-bench.yaml"
        assert (root / "configs/rig-bench.yaml").read_text(encoding="utf-8") == text
        assert "bench" in [r["name"] for r in workspace.describe(key)["rigs"]]
        again = call("/api/manage/rig-save", {"project": key, "name": "bench", "text": text})
        assert again[0] == 400 and "already has a rig" in again[1]["error"]
        for bad, words in [
            ("devices: [", "Not valid YAML"),
            ("- a\n- b\n", "mapping"),
            (text + "\nnot_a_setting: 1\n", "invalid config"),
        ]:
            status, out = call("/api/manage/rig-save", {"project": key, "name": "x", "text": bad})
            assert status == 400 and words in out["error"], out
        assert not (root / "configs/rig-x.yaml").exists()

    def test_editing_needs_the_text_that_was_opened(self, http, workspace):
        call, _ = http
        key = pid(workspace)
        root = Path(workspace.projects[0]["path"])
        status, opened = call(f"/api/manage/rig-file?project={key}&path=configs/rig-sim.yaml")
        assert status == 200 and opened["editable"]
        edited = opened["text"] + "\n# edited\n"
        ok = call(
            "/api/manage/rig-save",
            {
                "project": key,
                "path": "configs/rig-sim.yaml",
                "text": edited,
                "sha256": opened["sha256"],
            },
        )
        assert ok[0] == 200
        stale = call(
            "/api/manage/rig-save",
            {
                "project": key,
                "path": "configs/rig-sim.yaml",
                "text": opened["text"],
                "sha256": opened["sha256"],
            },
        )
        assert stale[0] == 409
        assert (root / "configs/rig-sim.yaml").read_text(encoding="utf-8") == edited

    @pytest.mark.parametrize(
        "path",
        [
            "../outside/rig-a.yaml",
            "run.py",
            "configs/task.yaml",
            "configs/rig-sim_gamma.yaml",
            "/etc/passwd",
        ],
    )
    def test_only_the_experiments_rig_files(self, http, workspace, path):
        call, _ = http
        status, _ = call(f"/api/manage/rig-file?project={pid(workspace)}&path={path}")
        assert status in {400, 404}
        status, _ = call(
            "/api/manage/rig-save",
            {"project": pid(workspace), "path": path, "text": "a: 1", "sha256": "x"},
        )
        assert status in {400, 404}

    def test_a_local_override_of_a_shared_rig(self, http, workspace, monkeypatch):
        call, _ = http
        project = workspace.project(pid(workspace))
        shared = {name: str(path) for name, path in shared_rig_files().items()}
        monkeypatch.setitem(
            project, "shared_rigs", [{"name": n, "path": p} for n, p in shared.items()]
        )
        status, out = call(f"/api/manage/rig-file?project={project['id']}&path=alhazen/laptop")
        assert status == 200 and out["editable"] is False
        status, out = call(
            "/api/manage/rig-save",
            {
                "project": project["id"],
                "name": "laptop",
                "text": "extends: laptop\nmonitor:\n  distance_cm: 61\n",
            },
        )
        assert status == 200, out
        rigs = {(r["name"], r["source"]): r for r in workspace.describe(project["id"])["rigs"]}
        assert rigs[("laptop", "experiment")]["extends"] == "laptop"
        assert rigs[("laptop", "alhazen")]["shadowed"] is True
        assert Path(shared["laptop"]).read_text(encoding="utf-8")  # untouched, still readable


class TestRegistration:
    def test_register_refuses_duplicates_and_bad_folders(self, http, workspace, tmp_path):
        call, _ = http
        project = workspace.projects[0]
        status, out = call(
            "/api/manage/register", {"path": project["path"], "python": sys.executable}
        )
        assert status == 400 and "already registered" in out["error"]
        status, out = call("/api/manage/register", {"path": str(tmp_path / "nothing")})
        assert status == 400 and "No run.py" in out["error"]
        other = tmp_path / "second"
        other.mkdir()
        (other / "run.py").write_text("print('x')\n")
        status, out = call(
            "/api/manage/register", {"path": str(other), "python": str(tmp_path / "no-python")}
        )
        assert status == 400 and "does not exist" in out["error"]
        status, out = call("/api/manage/register", {"path": str(other), "python": sys.executable})
        assert status == 200 and out["name"] == "second"
        assert [p["id"] for p in workspace.projects][0] == project["id"]  # kept, first

    def test_meta_and_archive(self, http, workspace):
        call, _ = http
        key = pid(workspace)
        status, out = call(
            "/api/manage/meta", {"project": key, "fields": {"description": "Saccade study"}}
        )
        assert status == 200 and out["meta"]["description"] == "Saccade study"
        status, out = call("/api/manage/archive", {"project": key, "archived": True})
        assert status == 200 and out["archived"] is True
        state = json.loads(json.dumps(workspace.state()))
        assert state["projects"][0]["archived"] is True


SESSION_RUN_PY = r"""
import json, sys
from pathlib import Path
argv = sys.argv[1:]
print(json.dumps(argv), flush=True)
root = Path.cwd() / "data-rehearsal" / "v0.1.0" / "sub-007" / "ses-001" / "run-01_task-demo"
(root / "figures").mkdir(parents=True)
card = {"schema_version": 1, "task": "demo", "mode": "test", "date": "20261007",
        "subject": {"id": "007", "initials": "HD"},
        "experimenter": {"id": argv[argv.index("--experimenter-id") + 1],
                         "name": argv[argv.index("--experimenter") + 1]}}
(root / "session.json").write_text(json.dumps(card), encoding="utf-8")
(root / "sub-007_ses-001_run-01_task-demo_20261007_session.log").write_text(
    "session start: <b>not markup</b>\n", encoding="utf-8")
(root / "figures" / "live_monitor.html").write_text(
    "<!doctype html><title>saved</title><script>document.title='ran'</script>",
    encoding="utf-8")
# The lines a session prints (cli/main.py, modes/session.py) that name its folder.
print("experiment: demo 0.1.0 — filed under v0.1.0/ (version from pyproject.toml)")
print("running demo: sub-007 ses-001 run-01", flush=True)
print(f"session complete — data under {(Path.cwd() / 'data-rehearsal').resolve()}")
"""


class TestHistory:
    def test_launches_and_session_folders_join(self, http, workspace):
        call, _ = http
        key = pid(workspace)
        project = workspace.project(key)
        project["capabilities"] = ["experimenter"]
        root = Path(project["path"])
        (root / "run.py").write_text(SESSION_RUN_PY, encoding="utf-8")
        # An older session in the same folder, from before session.json named
        # the experimenter, and launched from a terminal (no launch record).
        old = root / "data-rehearsal" / "v0.1.0" / "sub-003" / "ses-001" / "run-01_task-demo"
        old.mkdir(parents=True)
        (old / "session.json").write_text(
            json.dumps({"schema_version": 1, "task": "demo", "subject": {"id": "003"}})
        )
        registry = workspace.people
        subject = registry.add_subject(key, {"code": "007", "initials": "HD"})
        who = registry.add_experimenter({"name": "Zoë Lee"})
        registry.assign(key, who["id"])
        run = finish(
            workspace,
            workspace.start(
                request_for(
                    workspace, mode="test", subject_record=subject["id"], experimenter=who["id"]
                )
            ),
        )
        assert run["status"] == "completed", run["log"]
        status, history = call(f"/api/manage/history?project={key}")
        assert status == 200
        (launch,) = history["launches"]
        assert launch["experimenter"]["name"] == "Zoë Lee"
        assert launch["experimenter_recorded_in"] == "session.json"
        assert "launch.json" in launch["files"] and "console.log" in launch["files"]
        sessions = {s["subject"]: s for s in history["sessions"]}
        assert sessions["007"]["launch"] == run["id"]
        assert launch["session_folder"] == {
            "root": sessions["007"]["root"],
            "run": sessions["007"]["id"],
        }
        assert sessions["007"]["experimenter"] == {
            "recorded": True,
            "id": who["id"],
            "name": "Zoë Lee",
        }
        assert sessions["007"]["page"] == "figures/live_monitor.html"
        assert sessions["007"]["has_log"] is True
        assert sessions["003"]["experimenter"]["recorded"] is False
        assert sessions["003"]["launch"] is None and sessions["003"]["page"] is None

        status, text = call(
            f"/api/manage/launch-text?project={key}&run={run['id']}&name=launch.json"
        )
        assert status == 200 and json.loads(text["text"])["identity"]["experimenter"]
        assert (
            call(f"/api/manage/launch-text?project={key}&run={run['id']}&name=../projects.json")[0]
            == 400
        )
        assert call(f"/api/manage/launch-text?project={key}&run=nope&name=run.json")[0] == 404

        session = sessions["007"]
        query = f"project={key}&root={session['root']}&run={session['id']}"
        status, headers, data = call(
            f"/data/download?{query}&name=figures/live_monitor.html", raw=True
        )
        assert status == 200 and data.startswith(b"<!doctype html>")
        assert headers["Content-Type"] == "application/octet-stream"
        assert headers["Content-Disposition"].startswith("attachment;")
        assert headers["Content-Security-Policy"].startswith("sandbox")
        assert call(f"/data/download?{query}&name=../../../../projects.json", raw=True)[0] == 400
        assert (
            call(f"/data/download?project={key}&root={session['root']}&run=../x&name=a", raw=True)[
                0
            ]
            == 400
        )
        # The saved monitor page opens through the Data view's ticket, under a
        # CSP that allows its own inline script and no requests at all.
        status, ticket = call(f"/api/data/page?{query}&name=figures/live_monitor.html")
        status, headers, page = call(ticket["url"], raw=True)
        assert status == 200 and b"<title>saved</title>" in page
        assert "connect-src 'none'" in headers["Content-Security-Policy"]
        assert "form-action 'none'" in headers["Content-Security-Policy"]

    def test_a_console_without_the_completion_line_is_matched_by_its_kind(self, tmp_path):
        from alhazen.cli.workspace_data import DataRoot
        from alhazen.cli.workspace_manage import _console_run_folder

        real, rehearsal = tmp_path / "data", tmp_path / "data-rehearsal"
        for folder in (real, rehearsal):
            (folder / "v1" / "sub-01" / "ses-001" / "run-02_task-t").mkdir(parents=True)
        roots = [DataRoot("a", real, "real"), DataRoot("b", rehearsal, "rehearsal")]
        console = tmp_path / "console.log"
        console.write_text("filed under v1/ (from pyproject)\nrunning t: sub-01 ses-001 run-02\n")
        found = _console_run_folder(console, roots, "test")
        assert found == str((rehearsal / "v1/sub-01/ses-001/run-02_task-t").resolve())
        assert _console_run_folder(console, roots, "run") == str(
            (real / "v1/sub-01/ses-001/run-02_task-t").resolve()
        )
        console.write_text("running t: sub-09 ses-001 run-02\n")  # no such folder
        assert _console_run_folder(console, roots, "test") is None
        console.write_text("starting\n")  # never got that far
        assert _console_run_folder(console, roots, "test") is None

    def test_history_names_what_it_cannot_read(self, http, workspace):
        call, _ = http
        status, history = call(f"/api/manage/history?project={pid(workspace)}")
        assert status == 200
        assert history["launches"] == [] and history["sessions"] == []
        assert history["missing"]  # the rig's data folder does not exist yet
