"""The collector's private data view: filters, the derived trial index and its
budgets, safe exports and original-file downloads. Synthetic sessions."""

from __future__ import annotations

import csv
import io
import json

from tests.hub.server_support import Hub, make_bundle, make_settings, session_files

from alhazen.hub import admin
from alhazen.hub.trials import safe_cell


def collector(hub, tmp_path):
    hub.register("ada")
    ada = hub.browser("ada")
    eid = ada.create_experiment()["id"]
    vid = ada.upload_version(eid, make_bundle(tmp_path)).json()["version"]["id"]
    return ada, eid, vid


class TestQueries:
    def test_list_filters_and_detail(self, hub, tmp_path):
        ada, eid, vid = collector(hub, tmp_path)
        one = ada.upload_session(eid, vid, session_files(), client_id="a")
        meta = {
            "subject_code": "S02",
            "mode": "rehearsal",
            "rig_alias": "bench",
            "started_at": None,
        }
        two = ada.upload_session(eid, vid, {"x.txt": b"x"}, client_id="b", metadata=meta)
        ids = lambda **q: [s["id"] for s in ada.get("/data/sessions", params=q).json()["items"]]  # noqa: E731
        assert set(ids()) == {one["id"], two["id"]}
        assert ids(subject_code="S02") == [two["id"]]
        assert ids(mode="run") == [one["id"]]
        assert ids(experiment_id="0" * 32) == []
        detail = ada.get(f"/data/sessions/{one['id']}").json()
        assert detail["receipt"]["manifest_sha256"] == one["manifest_sha256"]
        assert detail["session"]["experiment_title"] == "Saccade bias"
        assert {a["path"] for a in detail["artifacts"]} == set(session_files())

    def test_trials_are_derived_after_commit(self, hub, tmp_path):
        ada, eid, vid = collector(hub, tmp_path)
        receipt = ada.upload_session(eid, vid, session_files())
        assert receipt["index"]["status"] == "pending"
        before = ada.get(f"/data/sessions/{receipt['id']}/trials").json()
        assert before["items"] == [] and before["index"]["status"] == "pending"
        hub.maintenance.drain_index()
        trials = ada.get(f"/data/sessions/{receipt['id']}/trials", params={"limit": 2}).json()
        assert trials["index"] == {"status": "indexed", "rows": 3, "error": None}
        assert trials["columns"] == ["trial_index", "outcome", "rt", "label"]
        assert [t["values"]["outcome"] for t in trials["items"]] == ["CORRECT", "WRONG"]
        assert trials["next_offset"] == 2

    def test_a_session_without_a_trial_table_is_simply_unindexed(self, hub, tmp_path):
        ada, eid, vid = collector(hub, tmp_path)
        receipt = ada.upload_session(eid, vid, {"session.json": b"{}"})
        assert receipt["index"]["status"] == "none"
        assert ada.get(f"/data/sessions/{receipt['id']}/export").status_code == 409


class TestExports:
    def test_csv_export_defuses_spreadsheet_formulas_but_keeps_numbers(self, hub, tmp_path):
        ada, eid, vid = collector(hub, tmp_path)
        receipt = ada.upload_session(eid, vid, session_files())
        hub.maintenance.drain_index()
        response = ada.get(f"/data/sessions/{receipt['id']}/export", params={"format": "csv"})
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        rows = list(csv.reader(io.StringIO(response.text)))
        assert rows[0] == ["trial_index", "outcome", "rt", "label"]
        assert rows[1][3] == '\'=HYPERLINK("x")' and rows[2][2] == "-0.5" and rows[3][3] == "'+cmd"

    def test_json_export(self, hub, tmp_path):
        ada, eid, vid = collector(hub, tmp_path)
        receipt = ada.upload_session(eid, vid, session_files())
        hub.maintenance.drain_index()
        body = json.loads(
            ada.get(f"/data/sessions/{receipt['id']}/export", params={"format": "json"}).content
        )
        assert body["session_id"] == receipt["id"] and len(body["rows"]) == 3
        assert body["rows"][0]["values"]["label"] == '=HYPERLINK("x")'  # JSON is data, not a sheet
        assert (
            ada.get(f"/data/sessions/{receipt['id']}/export", params={"format": "xlsx"}).status_code
            == 400
        )

    def test_safe_cell(self):
        assert safe_cell("=1+1") == "'=1+1" and safe_cell("@SUM(A1)") == "'@SUM(A1)"
        assert (
            safe_cell("-3.5e2") == "-3.5e2" and safe_cell("+7") == "+7" and safe_cell("-") == "'-"
        )
        assert safe_cell(None) == "" and safe_cell("\tx") == "'\tx"

    def test_original_file_download(self, hub, tmp_path):
        ada, eid, vid = collector(hub, tmp_path)
        receipt = ada.upload_session(eid, vid, session_files())
        response = ada.get(f"/data/sessions/{receipt['id']}/files", params={"path": "session.json"})
        assert response.status_code == 200 and response.content == session_files()["session.json"]
        assert response.headers["content-type"] == "application/octet-stream"
        assert "attachment" in response.headers["content-disposition"]
        assert response.headers["content-security-policy"].startswith("sandbox")
        for bad in ("../hub.sqlite3", "/etc/passwd", "missing.txt", ""):
            assert ada.get(
                f"/data/sessions/{receipt['id']}/files", params={"path": bad}
            ).status_code in (400, 404)


class TestIndexBudgets:
    def test_over_budget_tables_fail_visibly_and_keep_raw_files(self, tmp_path, clock):
        tight = make_settings(tmp_path, max_indexed_columns=3)
        admin.init_database(tight)
        service = Hub(tight, clock)
        ada, eid, vid = collector(service, tmp_path)
        receipt = ada.upload_session(eid, vid, session_files())
        service.maintenance.drain_index()
        detail = ada.get(f"/data/sessions/{receipt['id']}").json()
        assert detail["session"]["index"]["status"] == "failed"
        assert "columns" in detail["session"]["index"]["error"]
        assert ada.get(f"/data/sessions/{receipt['id']}/trials").json()["items"] == []
        export = ada.get(f"/data/sessions/{receipt['id']}/export")
        assert export.status_code == 409 and export.json()["error"]["code"] == "index_not_ready"
        raw = ada.get(
            f"/data/sessions/{receipt['id']}/files",
            params={"path": "sub-01_ses-01_run-01_trials.csv"},
        )
        assert raw.status_code == 200

    def test_row_cap_and_malformed_tables(self, tmp_path, clock):
        tight = make_settings(tmp_path, max_indexed_rows=2, max_csv_cell_bytes=10)
        admin.init_database(tight)
        service = Hub(tight, clock)
        ada, eid, vid = collector(service, tmp_path)
        cases = {
            "too-many-rows": session_files(),
            "ragged": {"trials.csv": b"a,b\n1,2\n3\n"},
            "big-cell": {"trials.csv": b"a\n" + b"x" * 11 + b"\n"},
            "not-utf8": {"trials.csv": b"a\n\xff\xfe\n"},
        }
        for name, files in cases.items():
            receipt = ada.upload_session(eid, vid, files, client_id=name)
            service.maintenance.drain_index()
            index = ada.get(f"/data/sessions/{receipt['id']}").json()["session"]["index"]
            assert index["status"] == "failed" and index["rows"] == 0, (name, index)

    def test_reindex_rebuilds_from_raw_files(self, hub, tmp_path):
        ada, eid, vid = collector(hub, tmp_path)
        receipt = ada.upload_session(eid, vid, session_files())
        hub.maintenance.drain_index()
        again = ada.post(f"/data/sessions/{receipt['id']}/reindex")
        assert again.status_code == 202 and again.json()["session"]["index"]["status"] == "pending"
        hub.maintenance.drain_index()
        assert ada.get(f"/data/sessions/{receipt['id']}/trials").json()["index"]["rows"] == 3
        assert admin.reindex_sessions(hub.settings) == 1
