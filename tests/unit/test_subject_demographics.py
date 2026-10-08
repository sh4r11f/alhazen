"""A subject's age and sex: the rule (config.models), the people registry's
fields and its upgrade from schema 1 (cli/people.py), the workspace's launch
(cli/workspace.py), and what a session records (--age, --sex → session.json,
session.log, participants.tsv)."""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from tests.unit import test_builder, test_cli_modes
from tests.unit import test_workspace as base

from alhazen.cli import people as people_module
from alhazen.cli.people import Conflict, PeopleError, PeopleRegistry
from alhazen.config.models import SUBJECT_SEXES, age_number, normalize_age, normalize_sex
from alhazen.session.identity import SubjectDemographics

EXP = "0123456789abcdef"
Built = test_builder.TestTheExperimentVersionFilesTheRun
request_for = base.request_for
finish = base.finish


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    return list(reader.fieldnames or []), list(reader)


# -- the rule ------------------------------------------------------------------


class TestRule:
    @pytest.mark.parametrize(
        ("given", "recorded"),
        [("27", "27"), (" 27.0 ", "27"), (7.5, "7.5"), (0, "0"), ("120", "120"), ("33.40", "33.4")],
    )
    def test_ages_are_recorded_in_one_form(self, given, recorded):
        assert normalize_age(given) == recorded

    @pytest.mark.parametrize("given", ["-1", "121", "7.55", "thirty", "", "nan", "inf", True, None])
    def test_other_ages_are_refused_in_the_rules_words(self, given):
        with pytest.raises(ValueError, match="age must be a number of years from 0 to 120"):
            normalize_age(given)

    def test_session_json_gets_a_number(self):
        assert age_number("27") == 27 and isinstance(age_number("27"), int)
        assert age_number("7.5") == 7.5

    @pytest.mark.parametrize(
        ("given", "code"),
        [
            ("female", "female"),
            ("Male", "male"),
            (" OTHER ", "other"),
            ("Prefer not to say", "prefer_not_to_say"),
            ("prefer_not_to_say", "prefer_not_to_say"),
        ],
    )
    def test_sexes(self, given, code):
        assert normalize_sex(given) == code

    @pytest.mark.parametrize("given", ["F", "m", "woman", "", None, 1])
    def test_other_sexes_are_refused(self, given):
        with pytest.raises(ValueError, match="sex must be one of female, male, other"):
            normalize_sex(given)

    def test_the_page_offers_exactly_the_recorded_codes(self):
        script = (Path(people_module.__file__).parent / "assets" / "workspace.js").read_text(
            encoding="utf-8"
        )
        block = re.search(r"const SUBJECT_SEXES = \[(.*?)\];", script, re.S)
        assert block is not None
        codes = re.findall(r"\['([a-z_]+)', '[^']+'\]", block.group(1))
        assert tuple(codes) == SUBJECT_SEXES


# -- the registry ----------------------------------------------------------------


@pytest.fixture
def registry(tmp_path) -> PeopleRegistry:
    return PeopleRegistry(tmp_path / "ws")


class TestRegistryFields:
    def test_add_records_age_sex_and_the_date_of_the_age(self, registry):
        s = registry.add_subject(
            EXP, {"code": "007", "initials": "HD", "age": "27", "sex": "Female"}
        )
        assert (s["age"], s["sex"], s["age_recorded"]) == ("27", "female", today())

    def test_both_are_optional(self, registry):
        s = registry.add_subject(EXP, {"code": "1", "age": "", "sex": None})
        assert (s["age"], s["sex"], s["age_recorded"]) == (None, None, None)

    @pytest.mark.parametrize(
        ("fields", "words"),
        [
            ({"age": "-2"}, "Age must be"),
            ({"age": "27.25"}, "Age must be"),
            ({"sex": "f"}, "Sex must be one of"),
            ({"sex": "unknown"}, "Sex must be one of"),
        ],
    )
    def test_invalid_values_are_refused_and_nothing_is_written(self, registry, fields, words):
        with pytest.raises(PeopleError, match=words):
            registry.add_subject(EXP, {"code": "1", **fields})
        assert registry.subjects(EXP) == []

    def test_age_and_sex_are_no_longer_extra_column_names(self, registry):
        for name in ("age", "sex", "age_recorded"):
            with pytest.raises(PeopleError, match="one of the record's own fields"):
                registry.add_subject(EXP, {"code": "1", "extra": [[name, "x"]]})

    def test_a_new_age_is_dated_and_a_cleared_one_loses_its_date(self, registry, monkeypatch):
        s = registry.add_subject(EXP, {"code": "1", "age": "27"})
        monkeypatch.setattr(people_module, "_today", lambda: "2027-01-02")
        same = registry.update_subject(EXP, s["id"], s["revision"], {"age": "27.0", "sex": "male"})
        assert same["age_recorded"] == today()  # the same age: its date stays
        older = registry.update_subject(EXP, s["id"], same["revision"], {"age": "28"})
        assert (older["age"], older["age_recorded"]) == ("28", "2027-01-02")
        cleared = registry.update_subject(EXP, s["id"], older["revision"], {"age": None})
        assert (cleared["age"], cleared["age_recorded"]) == (None, None)

    def test_a_used_subject_keeps_its_id_but_age_and_sex_stay_editable(self, registry):
        s = registry.add_subject(EXP, {"code": "1", "initials": "HD", "age": "30", "sex": "male"})
        edited = registry.update_subject(
            EXP, s["id"], s["revision"], {"age": "31", "sex": "prefer_not_to_say"}, used=True
        )
        assert (edited["age"], edited["sex"]) == ("31", "prefer_not_to_say")

    def test_a_stale_edit_of_age_is_refused(self, registry):
        s = registry.add_subject(EXP, {"code": "1"})
        registry.update_subject(EXP, s["id"], s["revision"], {"age": "20"})
        with pytest.raises(Conflict):
            registry.update_subject(EXP, s["id"], s["revision"], {"age": "21"})
        assert registry.subject(s["id"])["age"] == "20"

    def test_the_launch_snapshot_carries_them(self, registry):
        s = registry.add_subject(EXP, {"code": "1", "initials": "HD", "age": "7.5", "sex": "other"})
        snap = registry.launch_identity(EXP, s["id"], None, need_initials=False)
        assert snap["subject"]["age"] == "7.5" and snap["subject"]["sex"] == "other"
        assert snap["subject"]["age_recorded"] == today()

    def test_the_csv_copy_has_the_columns_after_the_initials(self, registry):
        registry.add_subject(EXP, {"code": "1", "initials": "HD", "age": "27", "sex": "female"})
        header, (row,) = read_csv(registry.csv_dir / EXP / "subjects.csv")
        assert header[3:7] == ["initials", "age", "sex", "age_recorded"]
        assert (row["age"], row["sex"], row["age_recorded"]) == ("27", "female", today())


class TestCsvReadBack:
    def rewrite(self, path: Path, change) -> None:
        header, rows = read_csv(path)
        for row in rows:
            change(row)
        out = io.StringIO(newline="")
        writer = csv.DictWriter(out, fieldnames=header, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
        path.write_text("\ufeff" + out.getvalue(), encoding="utf-8", newline="")

    def test_an_edited_age_and_sex_come_back(self, registry):
        s = registry.add_subject(EXP, {"code": "1", "age": "27", "sex": "female"})
        path = registry.csv_dir / EXP / "subjects.csv"

        def edit(row):
            row["age"], row["sex"] = "28", "Prefer not to say"

        self.rewrite(path, edit)
        plan = registry.plan_csv_import("subjects", EXP)
        (change,) = plan["changes"]
        assert change["action"] == "update"
        assert change["fields"] == {"age": "28", "sex": "prefer_not_to_say"}
        registry.apply_csv_import("subjects", EXP, plan["digest"], set())
        after = registry.subject(s["id"])
        assert (after["age"], after["sex"], after["age_recorded"]) == (
            "28",
            "prefer_not_to_say",
            today(),
        )

    def test_an_invalid_age_in_the_file_is_an_error_row(self, registry):
        registry.add_subject(EXP, {"code": "1", "age": "27"})
        path = registry.csv_dir / EXP / "subjects.csv"
        self.rewrite(path, lambda row: row.update(age="old"))
        (change,) = registry.plan_csv_import("subjects", EXP)["changes"]
        assert change["action"] == "error" and "Age must be" in change["reason"]

    def test_a_copy_without_the_columns_does_not_clear_them(self, registry):
        registry.add_subject(EXP, {"code": "1", "age": "27", "sex": "male"})
        path = registry.csv_dir / EXP / "subjects.csv"
        header, rows = read_csv(path)
        old = [c for c in header if c not in ("age", "sex", "age_recorded")]
        out = io.StringIO(newline="")
        writer = csv.DictWriter(out, fieldnames=old, extrasaction="ignore", lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            row["missing_fields"] = "[]"
            writer.writerow(row)
        path.write_text("\ufeff" + out.getvalue(), encoding="utf-8", newline="")
        (change,) = registry.plan_csv_import("subjects", EXP)["changes"]
        assert change["action"] == "unchanged"


class TestParticipantsImport:
    def folder(self, tmp_path: Path, text: str) -> Path:
        root = tmp_path / "data"
        root.mkdir(exist_ok=True)
        (root / "participants.tsv").write_text(text, encoding="utf-8")
        return root

    def test_bids_columns_become_the_fields(self, registry, tmp_path):
        root = self.folder(
            tmp_path,
            "participant_id\tinitials\tAge\tsex\thand\n"
            "sub-01\tHD\t27\tF\tleft\n"
            "sub-02\tXY\tn/a\tprefer not to say\tright\n"
            "sub-03\tAB\tthirty\tq\tright\n",
        )
        plan = registry.plan_participants_import(EXP, [(root, "real")])
        rows = {r["code"]: r for r in plan["rows"]}
        assert (rows["01"]["age"], rows["01"]["sex"]) == ("27", "female")
        assert (rows["02"]["age"], rows["02"]["sex"]) == (None, "prefer_not_to_say")
        assert (rows["03"]["age"], rows["03"]["sex"]) == (None, None)
        assert "not a valid age/sex" in rows["03"]["reason"]
        registry.apply_participants_import(EXP, [(root, "real")], plan["digest"])
        by_code = {s["code"]: s for s in registry.subjects(EXP)}
        assert by_code["01"]["extra"] == [["hand", "left"]]
        assert by_code["01"]["age_recorded"] is None  # when it was taken is not known
        # Nothing typed is lost: the unreadable values stay as columns.
        assert by_code["03"]["extra"] == [
            ["Age", "thirty"],
            ["sex (participants.tsv)", "q"],
            ["hand", "right"],
        ]
        assert [s["code"] for s in registry.subjects(EXP)] == ["01", "02", "03"]

    def test_an_existing_record_is_filled_never_overwritten(self, registry, tmp_path):
        bare = registry.add_subject(EXP, {"code": "01", "initials": "HD"})
        kept = registry.add_subject(
            EXP, {"code": "02", "initials": "XY", "age": "40", "sex": "male"}
        )
        root = self.folder(
            tmp_path,
            "participant_id\tinitials\tage\tsex\nsub-01\tHD\t27\tfemale\nsub-02\tXY\t41\tfemale\n",
        )
        plan = registry.plan_participants_import(EXP, [(root, "real")])
        rows = {r["code"]: r for r in plan["rows"]}
        assert rows["01"]["action"] == "fill" and rows["01"]["fills"] == ["age", "sex"]
        assert rows["02"]["action"] == "link" and rows["02"]["differences"] == ["age", "sex"]
        registry.apply_participants_import(EXP, [(root, "real")], plan["digest"])
        assert (registry.subject(bare["id"])["age"], registry.subject(bare["id"])["sex"]) == (
            "27",
            "female",
        )
        assert (registry.subject(kept["id"])["age"], registry.subject(kept["id"])["sex"]) == (
            "40",
            "male",
        )


# -- the upgrade from schema 1 ------------------------------------------------------


def schema_1_registry(directory: Path) -> Path:
    """A people.sqlite3 as alhazen 2.11 wrote it, with subjects whose extra
    columns include age and sex the way an import from participants.tsv
    left them."""
    directory.mkdir(parents=True)
    path = directory / "people.sqlite3"
    db = sqlite3.connect(path, isolation_level=None)
    db.executescript(people_module._SCHEMA)
    db.execute("INSERT INTO meta(key, value) VALUES ('revision', '7')")
    db.execute("INSERT INTO meta(key, value) VALUES ('exported_revision', '7')")
    stamp = "2026-10-01T10:00:00+00:00"
    subjects = [
        # id, experiment, code, initials, notes, extra, status, position, revision
        (
            "s_aaaaaaaaaaaa",
            EXP,
            "007",
            "HD",
            "first",
            [["age", "27"], ["sex", "F"], ["hand", "L"]],
            "active",
            3,
            2,
        ),
        (
            "s_bbbbbbbbbbbb",
            EXP,
            "01",
            None,
            None,
            [["Age", "thirty"], ["sex", "n/a"]],
            "archived",
            1,
            1,
        ),
        ("s_cccccccccccc", EXP, "2", "AB", None, [["hand", "R"]], "active", 2, 4),
        (
            "s_dddddddddddd",
            EXP,
            "x9",
            "CD",
            None,
            [["age", None], ["sex", "q"], ["age_recorded", "?"]],
            "active",
            4,
            1,
        ),
    ]
    for sid, exp, code, initials, notes, extra, status, position, revision in subjects:
        db.execute(
            "INSERT INTO subjects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                exp,
                code,
                initials,
                notes,
                json.dumps(extra),
                status,
                position,
                revision,
                stamp,
                stamp,
            ),
        )
    db.execute(
        "INSERT INTO subject_sources VALUES (?, '/data/participants.tsv', 'real', 4, ?)",
        ("s_aaaaaaaaaaaa", stamp),
    )
    db.execute("PRAGMA user_version = 1")
    db.close()
    return path


def rows_of(path: Path) -> list[dict]:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute("SELECT * FROM subjects ORDER BY id")]
    finally:
        db.close()


class TestUpgrade:
    def test_every_subject_keeps_its_identity_and_order(self, tmp_path):
        path = schema_1_registry(tmp_path / "ws" / "people")
        before = rows_of(path)
        registry = PeopleRegistry(tmp_path / "ws")
        after = {r["id"]: r for r in rows_of(path)}
        for old in before:
            new = after[old["id"]]
            for column in (
                "experiment_id",
                "code",
                "initials",
                "notes",
                "status",
                "position",
                "created",
            ):
                assert new[column] == old[column], (old["id"], column)
        assert [s["code"] for s in registry.subjects(EXP)] == ["01", "2", "007", "x9"]
        assert registry.subject("s_aaaaaaaaaaaa")["sources"][0]["line"] == 4
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == people_module.SCHEMA_VERSION

    def test_extra_columns_named_age_or_sex_become_the_fields(self, tmp_path):
        schema_1_registry(tmp_path / "ws" / "people")
        registry = PeopleRegistry(tmp_path / "ws")
        a = registry.subject("s_aaaaaaaaaaaa")
        assert (a["age"], a["sex"], a["age_recorded"]) == ("27", "female", None)
        assert a["extra"] == [["hand", "L"]] and a["revision"] == 3
        b = registry.subject("s_bbbbbbbbbbbb")
        # "thirty" is kept (as typed, under its own name); n/a is "not recorded".
        assert (b["age"], b["sex"], b["extra"]) == (None, None, [["Age", "thirty"]])
        c = registry.subject("s_cccccccccccc")
        assert c["extra"] == [["hand", "R"]] and c["revision"] == 4  # untouched
        d = registry.subject("s_dddddddddddd")
        assert (d["age"], d["sex"]) == (None, None)
        assert d["extra"] == [["sex (kept as text)", "q"], ["age_recorded (kept as text)", "?"]]
        # Every record stays editable under the new reserved names.
        registry.update_subject(EXP, d["id"], d["revision"], {"extra": d["extra"], "age": "5"})

    def test_a_backup_comes_first_and_the_upgrade_is_logged_once(self, tmp_path):
        path = schema_1_registry(tmp_path / "ws" / "people")
        before = rows_of(path)
        registry = PeopleRegistry(tmp_path / "ws")
        assert registry.upgraded is not None and registry.upgraded["from"] == 1
        backup = Path(registry.upgraded["backup"])
        assert backup.is_file() and rows_of(backup) == before
        with sqlite3.connect(backup) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert registry.export_status().revision == 8
        # Opening again changes nothing and takes no second backup.
        again = PeopleRegistry(tmp_path / "ws")
        assert again.upgraded is None and again.export_status().revision == 8
        assert len(list(registry.backup_dir.glob("*.sqlite3"))) == 1
        with sqlite3.connect(path) as db:
            actions = [r[0] for r in db.execute("SELECT action FROM changes")]
        assert actions.count("upgrade") == 4  # the registry, and the 3 subjects that changed

    def test_the_csv_copies_are_rewritten_with_the_columns(self, tmp_path):
        schema_1_registry(tmp_path / "ws" / "people")
        registry = PeopleRegistry(tmp_path / "ws")
        assert not registry.export_status().as_json()["pending"]
        header, rows = read_csv(registry.csv_dir / EXP / "subjects.csv")
        assert header[3:7] == ["initials", "age", "sex", "age_recorded"]
        assert [r["subject_id"] for r in rows] == ["01", "2", "007", "x9"]

    def test_a_failed_upgrade_changes_nothing(self, tmp_path, monkeypatch):
        path = schema_1_registry(tmp_path / "ws" / "people")
        before = rows_of(path)

        def broken(db, revision):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(people_module, "_promote_demographics", broken)
        with pytest.raises(sqlite3.OperationalError):
            PeopleRegistry(tmp_path / "ws")
        assert rows_of(path) == before
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 1
            columns = [r[1] for r in db.execute("PRAGMA table_info(subjects)")]
        assert "age" not in columns

    def test_no_backup_no_upgrade(self, tmp_path, monkeypatch):
        path = schema_1_registry(tmp_path / "ws" / "people")

        def refuse(self, reason):
            raise OSError("No space left on device")

        monkeypatch.setattr(PeopleRegistry, "backup", refuse)
        with pytest.raises(PeopleError, match="no backup could be written"):
            PeopleRegistry(tmp_path / "ws")
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 1

    def test_a_new_registry_is_the_same_tables_as_an_upgraded_one(self, tmp_path):
        schema_1_registry(tmp_path / "old" / "people")
        PeopleRegistry(tmp_path / "old")
        PeopleRegistry(tmp_path / "new")

        def schema(where):
            with sqlite3.connect(tmp_path / where / "people" / "people.sqlite3") as db:
                return [r[1:3] for r in db.execute("PRAGMA table_info(subjects)")]

        assert schema("old") == schema("new")

    def test_a_newer_file_is_still_refused_untouched(self, tmp_path):
        path = schema_1_registry(tmp_path / "ws" / "people")
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA user_version = 9")
        with pytest.raises(PeopleError, match="newer alhazen"):
            PeopleRegistry(tmp_path / "ws")
        assert "age" not in rows_of(path)[0]


# -- the workspace's launch ------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    yield from base.workspace.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def people(workspace):
    project = workspace.projects[0]
    project["capabilities"] = ["experimenter", "subject-demographics"]
    registry = workspace.people
    subject = registry.add_subject(
        project["id"], {"code": "007", "initials": "HD", "age": "27", "sex": "female"}
    )
    who = registry.add_experimenter({"name": "Ana"})
    registry.assign(project["id"], who["id"])
    return {"project": project, "subject": subject, "experimenter": who}


def argv_of(run: dict) -> list[str]:
    return json.loads(run["log"].splitlines()[0])


def flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


class TestLaunch:
    def registered(self, workspace, people):
        return finish(
            workspace,
            workspace.start(
                request_for(
                    workspace,
                    mode="test",
                    subject_record=people["subject"]["id"],
                    experimenter=people["experimenter"]["id"],
                )
            ),
        )

    def test_a_registered_subjects_age_and_sex_reach_the_session(self, workspace, people):
        run = self.registered(workspace, people)
        argv = argv_of(run)
        assert flag(argv, "--age") == "27" and flag(argv, "--sex") == "female"
        assert run["demographics_recorded_in"] == "session.json"
        assert run["identity"]["subject"]["age"] == "27"
        launch = json.loads((Path(run["directory"]) / "launch.json").read_text(encoding="utf-8"))
        assert launch["identity"]["subject"]["sex"] == "female"
        assert launch["demographics_recorded_in"] == "session.json"

    def test_an_older_alhazen_is_not_sent_the_flags(self, workspace, people):
        people["project"]["capabilities"] = ["experimenter"]
        run = self.registered(workspace, people)
        argv = argv_of(run)
        assert "--age" not in argv and "--sex" not in argv
        assert run["demographics_recorded_in"] == "workspace"
        assert run["identity"]["subject"]["age"] == "27"  # kept with the launch

    def test_a_later_edit_never_rewrites_a_past_launch(self, workspace, people):
        run = self.registered(workspace, people)
        s = people["subject"]
        workspace.people.update_subject(
            people["project"]["id"], s["id"], s["revision"], {"age": "28"}, used=True
        )
        launch = json.loads((Path(run["directory"]) / "launch.json").read_text(encoding="utf-8"))
        assert launch["identity"]["subject"]["age"] == "27"

    def test_a_subject_without_them_sends_neither(self, workspace, people):
        s = people["subject"]
        workspace.people.update_subject(
            people["project"]["id"], s["id"], s["revision"], {"age": None, "sex": None}
        )
        run = self.registered(workspace, people)
        assert "--age" not in argv_of(run) and run["demographics_recorded_in"] is None

    def test_typed_age_and_sex_are_checked_and_sent(self, workspace, people):
        run = finish(
            workspace,
            workspace.start(
                request_for(
                    workspace, mode="test", subject="s01", initials="hd", age=" 7.50 ", sex="Other"
                )
            ),
        )
        argv = argv_of(run)
        assert flag(argv, "--age") == "7.5" and flag(argv, "--sex") == "other"
        with pytest.raises(ValueError, match="Age must be|age must be"):
            workspace.start(
                request_for(workspace, mode="test", subject="s01", initials="hd", age="old")
            )

    def test_typed_values_beside_a_record_are_refused(self, workspace, people):
        with pytest.raises(ValueError, match="come from its record"):
            workspace.start(
                request_for(
                    workspace,
                    mode="test",
                    subject_record=people["subject"]["id"],
                    experimenter=people["experimenter"]["id"],
                    age="30",
                )
            )
        assert workspace.active is None

    def test_the_project_says_whether_its_alhazen_records_them(self, workspace, people):
        assert workspace.describe(people["project"]["id"])["records_demographics"] is True
        people["project"]["capabilities"] = []
        assert workspace.describe(people["project"]["id"])["records_demographics"] is False
        people["project"].pop("capabilities")
        assert workspace.describe(people["project"]["id"])["records_demographics"] is None


class TestHistory:
    """History reads each session's age and sex from its session.json."""

    def test_the_card_says_or_does_not(self):
        from alhazen.cli.workspace_manage import _session_demographics

        card = {"subject": {"id": "01", "initials": "HD", "age": 27, "sex": "female"}}
        assert _session_demographics(card) == {"recorded": True, "age": 27, "sex": "female"}
        nulls = {"subject": {"id": "01", "initials": "HD", "age": None, "sex": None}}
        assert _session_demographics(nulls) == {"recorded": True, "age": None, "sex": None}
        older = {"subject": {"id": "01", "initials": "HD"}}
        assert _session_demographics(older)["recorded"] is False
        assert _session_demographics(None)["recorded"] is False


# -- what a session records -----------------------------------------------------------------


class TestSession:
    def test_session_json_log_and_participants_record_them(self, tmp_path, monkeypatch):
        repo, task = Built.experiment_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        Built.session(
            tmp_path,
            task,
            rig=repo / "rig-sim.yaml",
            initials="HD",
            demographics=SubjectDemographics.parse("27.0", "Female"),
        ).run()
        (run_dir,) = (p.parent for p in (tmp_path / "data").rglob("session.json"))
        card = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        assert card["subject"] == {"id": "01", "initials": "HD", "age": 27, "sex": "female"}
        log = next(run_dir.glob("*session.log")).read_text(encoding="utf-8")
        assert "subject: age 27, sex female" in log
        with (tmp_path / "data" / "participants.tsv").open(encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            (row,) = list(reader)
        assert reader.fieldnames == ["participant_id", "initials", "age", "sex"]
        assert (row["age"], row["sex"], row["initials"]) == ("27", "female", "HD")

    def test_an_existing_participant_row_is_not_rewritten(self, tmp_path, monkeypatch):
        repo, task = Built.experiment_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        data = tmp_path / "data"
        data.mkdir()
        original = "participant_id\tinitials\tage\nsub-01\tHD\t26\n"
        (data / "participants.tsv").write_text(original, encoding="utf-8")
        Built.session(
            tmp_path,
            task,
            rig=repo / "rig-sim.yaml",
            initials="HD",
            demographics=SubjectDemographics.parse("27", "male"),
        ).run()
        assert (data / "participants.tsv").read_text(encoding="utf-8") == original
        (card,) = data.rglob("session.json")
        assert json.loads(card.read_text(encoding="utf-8"))["subject"]["age"] == 27

    def test_without_them_the_card_says_not_recorded(self, tmp_path, monkeypatch):
        repo, task = Built.experiment_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        Built.session(tmp_path, task, rig=repo / "rig-sim.yaml").run()
        (card,) = (tmp_path / "data").rglob("session.json")
        subject = json.loads(card.read_text(encoding="utf-8"))["subject"]
        assert subject["age"] is None and subject["sex"] is None


class TestCommandLine:
    def run(self, tmp_path, monkeypatch, argv):
        seen: dict = {}
        import alhazen.modes.session as session_module
        from alhazen.cli.modes import run_experiment
        from alhazen.errors import ConfigError

        def stop(mode, **kwargs):
            seen.update(kwargs)
            raise ConfigError("stopped by the test")

        monkeypatch.setattr(session_module, "build_mode_session", stop)
        code = run_experiment(
            task_class=test_cli_modes.TestInitials.task_class(),
            default_rig=test_cli_modes.rig_file(tmp_path),
            argv=argv,
        )
        return code, seen

    def test_the_flags_reach_the_session(self, tmp_path, monkeypatch):
        argv = [
            "--mode",
            "simulate",
            "--task",
            "initials-check",
            "--headless",
            "--age",
            "31",
            "--sex",
            "prefer_not_to_say",
        ]
        code, seen = self.run(tmp_path, monkeypatch, argv)
        assert code == 1  # the test's stop, after the flags were read
        assert seen["demographics"] == SubjectDemographics("31", "prefer_not_to_say")

    def test_without_the_flags_nothing_is_passed(self, tmp_path, monkeypatch):
        code, seen = self.run(
            tmp_path, monkeypatch, ["--mode", "simulate", "--task", "initials-check", "--headless"]
        )
        assert code == 1 and "demographics" not in seen

    @pytest.mark.parametrize(
        ("flag", "value", "words"), [("--age", "200", "age must be"), ("--sex", "f", "sex must be")]
    )
    def test_a_bad_value_is_a_usage_error(self, tmp_path, monkeypatch, capsys, flag, value, words):
        code, seen = self.run(
            tmp_path, monkeypatch, ["--mode", "simulate", "--task", "initials-check", flag, value]
        )
        assert code == 2 and seen == {}
        assert words in capsys.readouterr().err
