"""The people registry (alhazen.cli.people): subjects and experimenters,
their CSV copies, participants.tsv import and the launch snapshot."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from alhazen.cli.people import (
    Conflict,
    PeopleError,
    PeopleRegistry,
    csv_cell,
    from_csv_cell,
)

EXP_A = "a" * 16
EXP_B = "b" * 16


@pytest.fixture
def registry(tmp_path: Path) -> PeopleRegistry:
    return PeopleRegistry(tmp_path / "workspace")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf"), "a UTF-8 byte-order mark for spreadsheets"
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
    return list(reader.fieldnames or []), list(reader)


class TestRecords:
    def test_subject_ids_are_text_and_scoped_to_one_experiment(self, registry):
        a = registry.add_subject(EXP_A, {"code": "007", "initials": " hd "})
        b = registry.add_subject(EXP_B, {"code": "007", "initials": "JK"})
        assert a["code"] == "007" and a["initials"] == "HD"
        assert a["id"] != b["id"]
        assert [s["code"] for s in registry.subjects(EXP_A)] == ["007"]
        assert registry.subjects(EXP_B)[0]["initials"] == "JK"
        with pytest.raises(PeopleError, match="already registered"):
            registry.add_subject(EXP_A, {"code": "007", "initials": "XY"})

    def test_same_initials_are_different_people(self, registry):
        one = registry.add_subject(EXP_A, {"code": "1", "initials": "HD"})
        two = registry.add_subject(EXP_A, {"code": "2", "initials": "HD"})
        assert one["id"] != two["id"]
        e1 = registry.add_experimenter({"name": "Sam Lee"})
        e2 = registry.add_experimenter({"name": "Sam Lee"})
        assert e1["id"] != e2["id"]

    @pytest.mark.parametrize("code", ["sub-01", "a b", "0.1", "", "../x", "x" * 33, "a\nb"])
    def test_subject_ids_follow_the_session_rule(self, registry, code):
        with pytest.raises(PeopleError):
            registry.add_subject(EXP_A, {"code": code})

    @pytest.mark.parametrize("initials", ["H1", "ABCDEF", "H.D"])
    def test_initials_follow_the_command_line_rule(self, registry, initials):
        with pytest.raises(PeopleError, match="1 to 5 letters"):
            registry.add_subject(EXP_A, {"code": "1", "initials": initials})

    def test_unknown_fields_and_bad_experiments_are_refused(self, registry):
        with pytest.raises(PeopleError, match="Unknown field"):
            registry.add_subject(EXP_A, {"code": "1", "age": "30"})
        with pytest.raises(PeopleError, match="Unknown experiment"):
            registry.add_subject("../etc", {"code": "1"})

    def test_text_keeps_unicode_and_lines(self, registry):
        notes = 'Ø first line\n  =SUM(A1)\nlast, "quoted"'
        s = registry.add_subject(
            EXP_A,
            {
                "code": "1",
                "initials": "ØY",
                "notes": notes,
                "extra": [["hand", "left"], ["age", None]],
            },
        )
        assert s["notes"] == notes and s["initials"] == "ØY"
        assert s["extra"] == [["hand", "left"], ["age", None]]

    def test_a_stale_edit_is_refused_and_changes_nothing(self, registry):
        e = registry.add_experimenter({"name": "Ana"})
        updated = registry.update_experimenter(e["id"], e["revision"], {"name": "Ana B"})
        assert updated["revision"] == 2
        with pytest.raises(Conflict, match="changed elsewhere"):
            registry.update_experimenter(e["id"], e["revision"], {"name": "Ana C"})
        assert registry.experimenter(e["id"])["name"] == "Ana B"

    def test_archiving_keeps_the_record(self, registry):
        s = registry.add_subject(EXP_A, {"code": "1", "initials": "HD"})
        archived = registry.set_subject_status(EXP_A, s["id"], s["revision"], "archived")
        assert archived["status"] == "archived"
        assert [x["id"] for x in registry.subjects(EXP_A)] == [s["id"]]
        with pytest.raises(PeopleError, match="archived"):
            registry.add_subject(EXP_A, {"code": "1"})

    def test_a_used_subjects_id_and_initials_are_fixed(self, registry):
        s = registry.add_subject(EXP_A, {"code": "1", "initials": "HD"})
        with pytest.raises(PeopleError, match="cannot change"):
            registry.update_subject(EXP_A, s["id"], 1, {"code": "2"}, used=True)
        with pytest.raises(PeopleError, match="cannot change"):
            registry.update_subject(EXP_A, s["id"], 1, {"initials": "JK"}, used=True)
        notes = registry.update_subject(EXP_A, s["id"], 1, {"notes": "ok"}, used=True)
        assert notes["notes"] == "ok"
        unused = registry.add_subject(EXP_A, {"code": "5"})
        renamed = registry.update_subject(EXP_A, unused["id"], 1, {"code": "6", "initials": "AB"})
        assert (renamed["code"], renamed["initials"]) == ("6", "AB")

    def test_a_subject_is_edited_only_through_its_experiment(self, registry):
        s = registry.add_subject(EXP_A, {"code": "1"})
        with pytest.raises(PeopleError, match="another experiment"):
            registry.update_subject(EXP_B, s["id"], 1, {"notes": "x"})

    def test_concurrent_writers_all_land(self, registry):
        errors: list[Exception] = []

        def add(n: int) -> None:
            try:
                registry.add_subject(EXP_A, {"code": f"{n:03d}"})
            except Exception as exc:  # collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=add, args=(n,)) for n in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        subjects = registry.subjects(EXP_A)
        assert len(subjects) == 40
        assert sorted(s["position"] for s in subjects) == list(range(1, 41))
        assert registry.export_status().revision == 40

    def test_two_clients_editing_one_record_one_wins_one_is_told(self, tmp_path):
        first = PeopleRegistry(tmp_path / "w")
        second = PeopleRegistry(tmp_path / "w")
        e = first.add_experimenter({"name": "Ana"})
        first.update_experimenter(e["id"], 1, {"notes": "from tab one"})
        with pytest.raises(Conflict):
            second.update_experimenter(e["id"], 1, {"notes": "from tab two"})
        assert second.experimenter(e["id"])["notes"] == "from tab one"


class TestLaunchIdentity:
    def test_snapshot_copies_the_records(self, registry):
        s = registry.add_subject(EXP_A, {"code": "01", "initials": "HD"})
        e = registry.add_experimenter({"name": "Ana", "initials": "AB"})
        registry.assign(EXP_A, e["id"])
        snap = registry.launch_identity(EXP_A, s["id"], e["id"], need_initials=True)
        assert snap["subject"] == {
            "record_id": s["id"],
            "id": "01",
            "initials": "HD",
            "revision": 1,
        }
        assert snap["experimenter"]["name"] == "Ana"
        registry.update_experimenter(e["id"], 1, {"name": "Ana Renamed"})
        assert snap["experimenter"]["name"] == "Ana"

    def test_refusals(self, registry):
        s = registry.add_subject(EXP_A, {"code": "01"})
        e = registry.add_experimenter({"name": "Ana"})
        with pytest.raises(PeopleError, match="no initials"):
            registry.launch_identity(EXP_A, s["id"], None, need_initials=True)
        assert registry.launch_identity(EXP_A, s["id"], None, need_initials=False)["subject"]
        with pytest.raises(PeopleError, match="another experiment"):
            registry.launch_identity(EXP_B, s["id"], None, need_initials=False)
        with pytest.raises(PeopleError, match="not an experimenter of this experiment"):
            registry.launch_identity(EXP_A, None, e["id"], need_initials=False)
        registry.assign(EXP_A, e["id"])
        registry.unassign(EXP_A, e["id"])
        with pytest.raises(PeopleError, match="not an experimenter of this experiment"):
            registry.launch_identity(EXP_A, None, e["id"], need_initials=False)
        registry.assign(EXP_A, e["id"])
        registry.set_experimenter_status(e["id"], 1, "archived")
        with pytest.raises(PeopleError, match="archived"):
            registry.launch_identity(EXP_A, None, e["id"], need_initials=False)
        with pytest.raises(PeopleError, match="Unknown subject"):
            registry.launch_identity(EXP_A, "s_nothere", None, need_initials=False)


class TestCsvCopies:
    def test_cells_round_trip_and_defuse_formulas(self):
        for value in ["=1+1", "+x", "-2", "@a", "'quoted", "\ttab", "plain", "", "007"]:
            assert from_csv_cell(csv_cell(value)) == value
        assert csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
        assert csv_cell(None) == ""

    def test_files_hold_the_records_exactly(self, registry):
        s = registry.add_subject(
            EXP_A,
            {
                "code": "007",
                "initials": "ØY",
                "notes": 'two\nlines, "q"',
                "extra": [["hand", "=cmd"], ["age", None], ["x", ""]],
            },
        )
        e = registry.add_experimenter({"name": "Zoë @lab"})
        registry.assign(EXP_A, e["id"])
        status = registry.export_status()
        assert not status.as_json()["pending"]
        header, rows = read_csv(registry.csv_dir / EXP_A / "subjects.csv")
        assert header[:4] == ["record_id", "experiment_id", "subject_id", "initials"]
        assert header[-4:] == ["hand", "age", "x", "missing_fields"]
        (row,) = rows
        assert row["record_id"] == s["id"] and row["subject_id"] == "007"
        assert row["initials"] == "ØY" and row["notes"] == 'two\nlines, "q"'
        assert row["hand"] == "'=cmd"
        assert row["age"] == "" and row["x"] == ""
        assert json.loads(row["missing_fields"]) == ["age"]
        _, people = read_csv(registry.csv_dir / "experimenters.csv")
        assert people[0]["name"] == "Zoë @lab"  # only a formula start is defused
        _, assigned = read_csv(registry.csv_dir / EXP_A / "experimenters.csv")
        assert assigned[0]["experimenter_id"] == e["id"]

    def test_unchanged_copy_reads_back_as_unchanged(self, registry):
        registry.add_subject(
            EXP_A,
            {"code": "1", "initials": "HD", "notes": "a\nb", "extra": [["k", "=v"], ["m", None]]},
        )
        plan = registry.plan_csv_import("subjects", EXP_A)
        assert plan["counts"] == {"unchanged": 1}

    def test_a_failed_export_is_saved_pending_and_retryable(self, registry):
        registry.csv_dir.parent.mkdir(parents=True, exist_ok=True)
        registry.csv_dir.mkdir(exist_ok=True)
        blocker = registry.csv_dir / EXP_A
        blocker.write_text("not a folder")
        s = registry.add_subject(EXP_A, {"code": "1"})  # the write itself succeeds
        assert registry.subject(s["id"])["code"] == "1"
        status = registry.export_status().as_json()
        assert status["pending"] and status["error"]
        with pytest.raises(PeopleError, match="records are saved"):
            registry.export_csv()
        blocker.unlink()
        fixed = registry.export_csv().as_json()
        assert not fixed["pending"] and fixed["error"] is None
        assert (registry.csv_dir / EXP_A / "subjects.csv").is_file()

    def test_edited_copy_previews_then_applies(self, registry):
        a = registry.add_subject(EXP_A, {"code": "1", "initials": "HD"})
        b = registry.add_subject(EXP_A, {"code": "2", "initials": "JK"})
        path = registry.csv_path("subjects", EXP_A)
        header, rows = read_csv(path)
        rows[0]["notes"] = "edited in a spreadsheet"
        rows[0]["missing_fields"] = json.dumps(["initials"][:0])
        del rows[1]  # a row deleted in the spreadsheet is not a deletion
        rows.append(
            dict.fromkeys(header, "")
            | {"subject_id": "3", "initials": "LM", "missing_fields": "[]"}
        )
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=header, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
        path.write_text(buffer.getvalue(), encoding="utf-8-sig", newline="")
        plan = registry.plan_csv_import("subjects", EXP_A)
        assert plan["counts"] == {"update": 1, "add": 1}
        assert plan["absent"] == [b["id"]]
        assert registry.subject(a["id"])["notes"] is None  # a preview writes nothing
        result = registry.apply_csv_import("subjects", EXP_A, plan["digest"], used=set())
        assert result["applied"] == 2 and Path(result["backup"]).is_file()
        assert registry.subject(a["id"])["notes"] == "edited in a spreadsheet"
        assert registry.subject(b["id"])["status"] == "active"
        assert [s["code"] for s in registry.subjects(EXP_A)] == ["1", "2", "3"]
        again = registry.plan_csv_import("subjects", EXP_A)
        assert set(again["counts"]) == {"unchanged"}

    def test_an_edit_of_a_stale_copy_is_a_conflict(self, registry):
        a = registry.add_subject(EXP_A, {"code": "1"})
        path = registry.csv_path("subjects", EXP_A)
        stale = path.read_bytes()
        registry.update_subject(EXP_A, a["id"], 1, {"notes": "newer"})
        reader = csv.DictReader(io.StringIO(stale.decode("utf-8-sig"), newline=""))
        old_rows = list(reader)
        old_header = list(reader.fieldnames or [])
        old_rows[0]["notes"] = "older edit"
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=old_header, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(old_rows)
        path.write_text(buffer.getvalue(), encoding="utf-8-sig", newline="")
        plan = registry.plan_csv_import("subjects", EXP_A)
        assert plan["counts"] == {"conflict": 1}
        with pytest.raises(PeopleError, match="cannot be imported"):
            registry.apply_csv_import("subjects", EXP_A, plan["digest"], used=set())
        assert registry.subject(a["id"])["notes"] == "newer"

    def test_apply_is_bound_to_the_preview(self, registry):
        registry.add_subject(EXP_A, {"code": "1"})
        plan = registry.plan_csv_import("subjects", EXP_A)
        registry.add_subject(EXP_A, {"code": "2"})
        with pytest.raises(Conflict, match="preview again"):
            registry.apply_csv_import("subjects", EXP_A, plan["digest"], used=set())


TSV = (
    "participant_id\tinitials\thandedness\tgroup\tnotes\r\n"
    "sub-03\tHD\tright\tB\tfirst\r\n"
    "sub-01\tjk\t\tA\r\n"
    "sub-007\t\tleft\n"
)


def write_tsv(root: Path, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "participants.tsv"
    path.write_text(text, encoding="utf-8", newline="")
    return path


class TestParticipantsImport:
    def test_dry_run_then_apply_preserves_everything(self, registry, tmp_path):
        tsv = write_tsv(tmp_path / "data", TSV)
        before = tsv.read_bytes()
        sources = [(tmp_path / "data", "real")]
        plan = registry.plan_participants_import(EXP_A, sources)
        assert plan["counts"] == {"new": 3}
        assert registry.subjects(EXP_A) == []
        result = registry.apply_participants_import(EXP_A, sources, plan["digest"])
        assert result["applied"] == 3 and Path(result["backup"]).is_file()
        subjects = registry.subjects(EXP_A)
        assert [s["code"] for s in subjects] == ["03", "01", "007"]  # file order kept
        assert [s["initials"] for s in subjects] == ["HD", "JK", None]
        assert subjects[0]["extra"] == [
            ["handedness", "right"],
            ["group", "B"],
            ["notes (participants.tsv)", "first"],
        ]
        # sub-01's row ends after "group": its notes cell is missing, not empty.
        assert subjects[1]["extra"] == [
            ["handedness", ""],
            ["group", "A"],
            ["notes (participants.tsv)", None],
        ]
        assert subjects[2]["extra"][1:] == [["group", None], ["notes (participants.tsv)", None]]
        assert subjects[0]["sources"][0]["line"] == 2
        assert tsv.read_bytes() == before  # never written

    def test_importing_again_changes_nothing(self, registry, tmp_path):
        write_tsv(tmp_path / "data", TSV)
        sources = [(tmp_path / "data", "real")]
        plan = registry.plan_participants_import(EXP_A, sources)
        registry.apply_participants_import(EXP_A, sources, plan["digest"])
        revision = registry.export_status().revision
        again = registry.plan_participants_import(EXP_A, sources)
        assert again["counts"] == {"same": 3} and again["changes"] == 0
        result = registry.apply_participants_import(EXP_A, sources, again["digest"])
        assert result["applied"] == 0 and result["backup"] is None
        assert registry.export_status().revision == revision
        backups = list(registry.backup_dir.glob("*.sqlite3"))
        assert len(backups) == 1

    def test_conflicting_initials_are_never_merged(self, registry, tmp_path):
        write_tsv(tmp_path / "data", "participant_id\tinitials\nsub-01\tHD\n")
        write_tsv(tmp_path / "data-rehearsal", "participant_id\tinitials\nsub-01\tXY\nsub-02\tAB\n")
        sources = [(tmp_path / "data", "real"), (tmp_path / "data-rehearsal", "rehearsal")]
        plan = registry.plan_participants_import(EXP_A, sources)
        actions = [(r["code"], r["action"]) for r in plan["rows"]]
        assert actions == [("01", "new"), ("01", "conflict"), ("02", "new")]
        registry.apply_participants_import(EXP_A, sources, plan["digest"])
        assert [(s["code"], s["initials"]) for s in registry.subjects(EXP_A)] == [
            ("01", "HD"),
            ("02", "AB"),
        ]
        registry.add_subject(EXP_B, {"code": "01", "initials": "QQ"})
        other = registry.plan_participants_import(EXP_B, sources[:1])
        assert other["rows"][0]["action"] == "conflict"

    def test_a_record_without_initials_is_filled_and_linked(self, registry, tmp_path):
        s = registry.add_subject(EXP_A, {"code": "01"})
        write_tsv(tmp_path / "data", "participant_id\tinitials\nsub-01\tHD\n")
        sources = [(tmp_path / "data", "real")]
        plan = registry.plan_participants_import(EXP_A, sources)
        assert plan["rows"][0]["action"] == "fill"
        registry.apply_participants_import(EXP_A, sources, plan["digest"])
        assert registry.subject(s["id"])["initials"] == "HD"
        assert registry.subject(s["id"])["sources"][0]["kind"] == "real"

    def test_backup_restores_the_state_before_the_import(self, registry, tmp_path):
        registry.add_subject(EXP_A, {"code": "99", "initials": "ZZ"})
        write_tsv(tmp_path / "data", TSV)
        sources = [(tmp_path / "data", "real")]
        plan = registry.plan_participants_import(EXP_A, sources)
        result = registry.apply_participants_import(EXP_A, sources, plan["digest"])
        restored = PeopleRegistry(tmp_path / "restored")
        restored.path.unlink()
        restored.path.write_bytes(Path(result["backup"]).read_bytes())
        reopened = PeopleRegistry(tmp_path / "restored")
        assert [s["code"] for s in reopened.subjects(EXP_A)] == ["99"]

    def test_a_damaged_file_is_reported_not_imported(self, registry, tmp_path):
        write_tsv(tmp_path / "data", "participant_id\tinitials\nsub-01\tHD\textra\n")
        plan = registry.plan_participants_import(EXP_A, [(tmp_path / "data", "real")])
        assert "more cells" in plan["files"][0]["error"]
        assert plan["rows"] == []
        missing = registry.plan_participants_import(EXP_A, [(tmp_path / "none", "real")])
        assert missing["files"][0]["error"] == "no participants.tsv"


class TestSchema:
    def test_a_newer_registry_is_refused_untouched(self, tmp_path):
        registry = PeopleRegistry(tmp_path / "w")
        with sqlite3.connect(registry.path) as db:
            db.execute("PRAGMA user_version = 99")
        before = registry.path.read_bytes()
        with pytest.raises(PeopleError, match="newer alhazen"):
            PeopleRegistry(tmp_path / "w")
        assert registry.path.read_bytes() == before

    def test_a_foreign_file_is_refused(self, tmp_path):
        (tmp_path / "w" / "people").mkdir(parents=True)
        with sqlite3.connect(tmp_path / "w" / "people" / "people.sqlite3") as db:
            db.execute("CREATE TABLE something (x)")
        with pytest.raises(PeopleError, match="not a people registry"):
            PeopleRegistry(tmp_path / "w")

    def test_reopening_keeps_the_records(self, tmp_path):
        PeopleRegistry(tmp_path / "w").add_experimenter({"name": "Ana"})
        assert PeopleRegistry(tmp_path / "w").experimenters()[0]["name"] == "Ana"
