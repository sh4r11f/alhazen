"""alhazen.experiment_platform.parameters: parameter-set guides read from source as text.

Characterization tests of the Mana Experiment Hub's extractor (enrich_parameters.py),
ported with the module; a neutral fake experiment is written here. No experiment code is
imported or run.
"""

from __future__ import annotations

import json
import subprocess
import unittest

import pytest

from alhazen.experiment_platform import parameters as ep
from alhazen.experiment_platform.sources import read_tree

RUN_PY = """
from pathlib import Path
from demo.task import DemoTask, OtherTask
HERE = Path(__file__).parent
TASKS = {
    "demo": (DemoTask, HERE / "configs" / "task.yaml"),
    "other": (OtherTask, None),
}
PARAMETERS = {
    "Main (human)": ("demo", HERE / "configs" / "task.yaml"),
    "Main (monkey)": ("demo", HERE / "configs" / "task-monkey.yaml"),
    "Fast (human)": ("demo", HERE / "configs" / "task-fast.yaml"),
    "Other (human)": ("other", None),
}
LADDERS = {"Demo (monkey)": HERE / "configs" / "training-demo.yaml"}
"""

TASK_PY = """
from enum import Enum
from alhazen.task.subject_kind import SubjectParams
from demo.base import Scheduler

class Mode(str, Enum):
    KEYBOARD = "keyboard"
    SACCADE = "saccade"

class DemoParams(SubjectParams):
    response_mode: Mode = Mode.KEYBOARD
    contrast: float = 0.5
    iti: Duration = Duration(ms=500)
    paradigm: Scheduler = Scheduler(n_per_condition=10)
    sizes: list[float] = Field(default_factory=list)

class OtherParams(SubjectParams):
    'Other params.'
    level: int = 3

class DemoTask:
    'A demo task: one key per trial.'
    params_model = DemoParams

class OtherTask:
    'The other task, run on its own defaults.'
    params_model = OtherParams
"""

BASE_PY = """
class Scheduler:
    kind: str = "sequence"
    n_per_condition: int = 1
    shuffle: bool = True
"""

ALHAZEN_SK = """
class SubjectParams(Model):
    subject_kind: SubjectKind | None = None
    reward: RewardPolicy | None = None
"""

TASK_YAML = """# Demo HUMAN set.
# Second header line.

subject_kind: human
response_mode: keyboard   # deviation: keys instead of saccades
# --- stimulus (p. 3) ----------------
# The patch contrast.
contrast: 0.5             # paper: p. 3
iti: {ms: 500}            # ASSUMED (not in the paper): a guess
paradigm:
  n_per_condition: 10     # ASSUMED (not in the paper)
"""

MONKEY_YAML = """# Demo MONKEY set.
subject_kind: monkey
response_mode: saccade
contrast: 0.5
iti: {ms: 800}
matrices: {4: [2, 2], 16: [4, 4]}
paradigm:
  n_per_condition: 20     # paper: p. 4
reward:
  by_outcome:
    CORRECT: {n_pulses: 1, pulse_ms: 100}
"""

FAST_YAML = """# Fast set: shorter ITI.
subject_kind: human
iti: {ms: 100}
"""

LADDER_YAML = """# Demo ladder header.
name: demo
title: Demo (monkey)
task: demo
params: task-monkey.yaml
stages:
  - id: fixate
    title: Fixate
    overrides:
      contrast: 1.0          # ASSUMED (not in the paper): easy
    success: CORRECT
  - id: full
    title: Full task
    success: CORRECT
"""

DOCS = {
    "tasks": json.dumps(
        [
            {
                "id": "demo",
                "title": "Demo, human set",
                "summary": "Human summary.",
                "parameters_file": "configs/task.yaml",
                "parameters": [
                    {
                        "name": "contrast",
                        "label": "Contrast",
                        "unit": "",
                        "group": "Stimulus",
                        "default": 0.4,
                        "meaning": "Michelson contrast.",
                    },
                    {
                        "name": "paradigm.shuffle",
                        "label": "Shuffle",
                        "default": True,
                        "meaning": "Random order.",
                    },
                ],
            },
            {
                "id": "demo",
                "title": "Demo, monkey set",
                "summary": "Monkey summary.",
                "parameters_file": "configs/task-monkey.yaml",
                "parameters": [
                    {
                        "name": "iti",
                        "label": "ITI",
                        "group": "Timing",
                        "meaning": "Inter-trial interval.",
                    }
                ],
            },
            {
                "id": "ghost",
                "title": "Not in run.py",
                "parameters_file": "configs/ghost.yaml",
                "parameters": [],
            },
        ]
    ),
    "assumed": json.dumps([{"item": "contrast", "key": "contrast", "reason": "docs say assumed"}]),
}


def project():
    return {
        "run.py": RUN_PY,
        "src/demo/__init__.py": "",
        "src/demo/task.py": TASK_PY,
        "src/demo/base.py": BASE_PY,
        "configs/task.yaml": TASK_YAML,
        "configs/task-monkey.yaml": MONKEY_YAML.encode(),
        "configs/task-fast.yaml": FAST_YAML,
        "configs/training-demo.yaml": LADDER_YAML,
        "configs/rig-booth.yaml": "extends: lab\n",
    }


LIB = {
    "alhazen/__init__.py": "",
    "alhazen/task/__init__.py": "",
    "alhazen/task/subject_kind.py": ALHAZEN_SK,
}


def build(**kw):
    args = dict(source_commit="c0ffee", docs=DOCS, docs_origin="test docs", library_files=LIB)
    args.update(kw)
    return ep.extract_experiment("demo-exp", project(), **args)


class ParseYamlTests(unittest.TestCase):
    def test_comments_sections_header(self):
        py = ep.parse_yaml(TASK_YAML, "t.yaml")
        notes = {ep.path_str(n.path): n for n in py.notes}
        self.assertEqual(py.header, "Demo HUMAN set.\nSecond header line.")
        self.assertEqual(notes["contrast"].inline, "paper: p. 3")
        self.assertEqual(notes["contrast"].lead, "The patch contrast.")
        self.assertEqual(notes["contrast"].section, "stimulus (p. 3)")
        self.assertIsNone(notes["subject_kind"].lead)  # header is not a lead comment
        self.assertEqual(notes["iti"].inline, "ASSUMED (not in the paper): a guess")
        self.assertEqual(notes["paradigm.n_per_condition"].inline, "ASSUMED (not in the paper)")
        self.assertEqual(notes["paradigm.n_per_condition"].line, 11)

    def test_header_directly_followed_by_key(self):
        py = ep.parse_yaml("# header\n# more\na: 1  # c\n", "x.yaml")
        self.assertEqual(py.header, "header\nmore")
        self.assertIsNone(py.notes[0].lead)
        self.assertEqual(py.notes[0].inline, "c")

    def test_flow_mapping_children_inherit_comment(self):
        py = ep.parse_yaml("r: {a: 1, b: 2}   # both\n", "x.yaml")
        notes = {ep.path_str(n.path): n for n in py.notes}
        self.assertEqual(notes["r.a"].inline, "both")
        self.assertEqual(notes["r.b"].inline, "both")

    def test_hash_inside_quoted_value_is_not_a_comment(self):
        py = ep.parse_yaml('k: "a # b"   # real\n', "x.yaml")
        self.assertEqual(py.data, {"k": "a # b"})
        self.assertEqual(py.notes[0].inline, "real")

    def test_non_string_keys_kept_as_one_value(self):
        py = ep.parse_yaml("m: {4: [2, 2], 16: [4, 4]}\n", "x.yaml")
        self.assertEqual([ep.path_str(n.path) for n in py.notes], ["m"])
        flat = ep._flatten(py.data, set(), [], "x.yaml")
        self.assertEqual(flat, [(("m",), {4: [2, 2], 16: [4, 4]})])

    def test_merge_keys_rejected(self):
        with self.assertRaises(ep.SourceError):
            ep.parse_yaml("base: &b {a: 1}\nother:\n  <<: *b\n  c: 2\n", "x.yaml")

    def test_python_tags_rejected(self):
        with self.assertRaises(ep.SourceError):
            ep.parse_yaml("a: !!python/object:os.system {}\n", "x.yaml")

    def test_duplicate_key_warns_last_wins(self):
        py = ep.parse_yaml("a: 1\na: 2\n", "x.yaml")
        self.assertEqual(py.data, {"a": 2})
        self.assertTrue(any("repeated" in w for w in py.warnings))


class RegistryTests(unittest.TestCase):
    def test_reads_tasks_sets_ladders(self):
        r = ep.read_registry(RUN_PY)
        self.assertEqual(r.tasks["demo"], {"class": "DemoTask", "config": "configs/task.yaml"})
        self.assertEqual(r.tasks["other"]["config"], None)
        self.assertEqual(
            [s["label"] for s in r.sets],
            ["Main (human)", "Main (monkey)", "Fast (human)", "Other (human)"],
        )
        self.assertEqual(
            r.ladders, [{"label": "Demo (monkey)", "file": "configs/training-demo.yaml"}]
        )

    def test_dynamic_path_rejected_not_guessed(self):
        src = RUN_PY.replace(
            'HERE / "configs" / "task-fast.yaml"', 'HERE / ("configs" + "/x.yaml")'
        )
        r = ep.read_registry(src)
        self.assertNotIn("Fast (human)", [s["label"] for s in r.sets])
        self.assertTrue(any("not a static path" in w for w in r.warnings))

    def test_path_root_must_come_from_file(self):
        src = RUN_PY.replace("HERE = Path(__file__).parent", "HERE = Path.home()")
        r = ep.read_registry(src)
        self.assertEqual([s["label"] for s in r.sets], ["Other (human)"])

    def test_unknown_task_skipped(self):
        src = RUN_PY.replace('"Fast (human)": ("demo"', '"Fast (human)": ("nope"')
        r = ep.read_registry(src)
        self.assertTrue(any("not in TASKS" in w for w in r.warnings))


class DefaultsTests(unittest.TestCase):
    def test_static_defaults(self):
        idx = ep.ModuleIndex(project(), LIB)
        r = ep.read_registry(RUN_PY)
        m = ep.task_model(idx, r, "DemoTask")
        self.assertEqual(m.fields["response_mode"]["default"], "keyboard")  # enum member value
        self.assertEqual(m.fields["iti"]["default"], {"ms": 500})
        self.assertEqual(
            m.fields["paradigm"]["default"],
            {"kind": "sequence", "n_per_condition": 10, "shuffle": True},
        )
        self.assertEqual(m.fields["sizes"]["default"], [])  # default_factory=list
        self.assertIn("subject_kind", m.fields)  # from the library base
        self.assertEqual(m.default_of("paradigm.shuffle"), (True, True))
        self.assertEqual(m.default_of("reward.by_outcome"), (True, None))
        self.assertEqual(m.default_of("nothing"), (False, None))

    def test_unresolvable_expression_is_not_invented(self):
        idx = ep.ModuleIndex({}, {})
        node = ep.ast.parse("compute(3) + 1", mode="eval").body
        self.assertIs(idx.default_value(node, "x.py"), ep.UNRESOLVED)

    def test_call_with_unknown_kwarg_is_unresolved(self):
        idx = ep.ModuleIndex(project(), LIB)
        node = ep.ast.parse("Scheduler(bogus=1)", mode="eval").body
        self.assertIs(idx.default_value(node, "src/demo/task.py"), ep.UNRESOLVED)


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.e = build()
        self.sets = {s["id"]: s for s in self.e["sets"]}

    def test_sets_tasks_and_subjects(self):
        self.assertEqual(
            list(self.sets), ["main-human", "main-monkey", "fast-human", "other-human"]
        )
        self.assertEqual(self.sets["main-monkey"]["taskIds"], ["demo"])
        self.assertEqual(self.sets["main-monkey"]["subject"], "monkey")
        self.assertEqual(self.sets["other-human"]["subject"], "human")
        self.assertEqual(self.sets["other-human"]["provenance"]["subjectSource"], "set label")
        self.assertEqual(self.sets["main-human"]["taskImplementation"]["paramsModel"], "DemoParams")
        self.assertEqual(self.sets["main-human"]["purpose"], "Human summary.")
        self.assertEqual(self.sets["fast-human"]["purpose"], "Fast set: shorter ITI.")
        self.assertEqual(
            self.sets["fast-human"]["provenance"]["purposeSource"], "params file header comment"
        )

    def test_values_are_actual_and_structured(self):
        v = {x["key"]: x for x in self.sets["main-monkey"]["values"]}
        self.assertEqual(v["iti"]["value"], {"ms": 800})
        self.assertEqual(v["iti"]["display"], "800 ms")
        self.assertEqual(v["matrices"]["value"], {"4": [2, 2], "16": [4, 4]})
        self.assertTrue(v["matrices"]["keysStringified"])
        self.assertEqual(v["paradigm.n_per_condition"]["value"], 20)
        self.assertNotIn("[object Object]", json.dumps(self.e))

    def test_run_metadata_separated(self):
        keys = [x["key"] for x in self.sets["main-monkey"]["runValues"]]
        self.assertIn("subject_kind", keys)
        self.assertIn("reward.by_outcome.CORRECT.n_pulses", keys)
        self.assertNotIn("subject_kind", [x["key"] for x in self.sets["main-monkey"]["values"]])

    def test_meanings_and_undocumented(self):
        v = {x["key"]: x for x in self.sets["main-human"]["values"]}
        self.assertEqual(v["contrast"]["meaning"], "Michelson contrast.")
        self.assertTrue(v["contrast"]["documented"])
        self.assertFalse(v["response_mode"]["documented"])
        self.assertIsNone(v["response_mode"]["meaning"])
        self.assertEqual(
            v["iti"]["meaningFrom"], "configs/task-monkey.yaml"
        )  # meaning from the other entry
        self.assertEqual(
            v["paradigm.shuffle"]["source"], "model default (documented, not set in this file)"
        )
        self.assertEqual(v["paradigm.shuffle"]["value"], True)

    def test_assumed_from_config_comment_beats_docs_table(self):
        v = {x["key"]: x for x in self.sets["main-human"]["values"]}
        self.assertIs(v["contrast"]["assumed"], False)  # "paper: p. 3" on this set's line
        self.assertIs(v["iti"]["assumed"], True)
        self.assertTrue(v["response_mode"]["deviation"])
        m = {x["key"]: x for x in self.sets["main-monkey"]["values"]}
        self.assertIs(m["contrast"]["assumed"], True)  # no comment: the docs table
        self.assertEqual(m["contrast"]["assumedSource"], "docs assumed table (not set-specific)")
        self.assertIsNone(m["response_mode"]["assumed"])  # nothing says either way

    def test_assumed_not_applicable_without_assumed_table(self):
        e = build(docs={"tasks": DOCS["tasks"]})
        self.assertFalse(e["assumedApplies"])
        self.assertTrue(all(v["assumed"] is None for s in e["sets"] for v in s["values"]))

    def test_differences_same_task_only(self):
        mk = self.sets["main-monkey"]
        self.assertEqual(mk["comparison"]["comparedTo"], "main-human")  # task default set
        d = {x["key"]: x for x in mk["differences"]}
        self.assertEqual((d["iti"]["value"], d["iti"]["baseValue"]), ({"ms": 800}, {"ms": 500}))
        self.assertEqual(d["subject_kind"]["scope"], "run")
        self.assertNotIn("contrast", d)
        fast = {x["key"]: x for x in self.sets["fast-human"]["differences"]}
        self.assertEqual(set(fast), {"iti"})  # the rest equal through model defaults
        self.assertEqual(
            self.sets["main-human"]["comparison"]["comparedTo"], "fast-human"
        )  # closest other set
        self.assertIsNone(self.sets["other-human"]["comparison"]["comparedTo"])
        for s in self.e["sets"]:
            for x in s["differences"]:
                self.assertEqual(self.sets[x["comparedTo"]]["taskIds"], s["taskIds"])

    def test_config_less_set_uses_model_defaults(self):
        o = self.sets["other-human"]
        self.assertIsNone(o["configPath"])
        self.assertEqual({x["key"]: x["value"] for x in o["values"]}, {"level": 3})
        self.assertEqual(o["values"][0]["source"], "model default")

    def test_task_definitions(self):
        t = {x["id"]: x for x in self.e["taskDefinitions"]}
        self.assertEqual(t["demo"]["title"], "Demo")
        self.assertEqual(t["demo"]["setIds"], ["main-human", "main-monkey", "fast-human"])
        self.assertEqual(t["other"]["summary"], "The other task, run on its own defaults.")
        self.assertEqual(t["other"]["summarySource"], "task class docstring")

    def test_warnings_for_docs_drift(self):
        w = "\n".join(self.e["warnings"])
        self.assertIn("docs give contrast = 0.4 for configs/task.yaml, the source has 0.5", w)
        self.assertIn("'ghost'", w)
        self.assertIn("task other: no docs entry", w)

    def test_ladder_guide(self):
        (lad,) = self.e["ladders"]
        self.assertEqual(lad["params"], "configs/task-monkey.yaml")
        self.assertEqual(lad["paramsSetId"], "main-monkey")
        self.assertEqual(
            lad["stages"][0]["overrides"],
            [
                {
                    "key": "contrast",
                    "value": 1.0,
                    "display": "1.0",
                    "comment": "ASSUMED (not in the paper): easy",
                    "assumed": True,
                }
            ],
        )
        self.assertEqual(lad["stages"][1]["task"], "demo")

    def test_hardware_configs_separate(self):
        self.assertEqual(
            self.e["otherConfigs"],
            [
                {
                    "configPath": "configs/rig-booth.yaml",
                    "kind": "rig (hardware; alhazen rig file)",
                    "extends": "lab",
                }
            ],
        )
        self.assertNotIn("configs/rig-booth.yaml", [s["configPath"] for s in self.e["sets"]])

    def test_missing_config_warns(self):
        files = project()
        del files["configs/task-fast.yaml"]
        e = ep.extract_experiment("x", files, source_commit="c", docs=DOCS, library_files=LIB)
        self.assertTrue(any("task-fast.yaml is not in the source" in w for w in e["warnings"]))

    def test_unknown_run_fields_warn(self):
        e = build(library_files=None)
        self.assertTrue(any("run metadata fields" in w for w in e["warnings"]))

    def test_deterministic(self):
        self.assertEqual(json.dumps(build(), sort_keys=True), json.dumps(build(), sort_keys=True))


# ---------------------------------------------------------------------------
# What the port adds or changes (the hub tests above are unchanged)
# ---------------------------------------------------------------------------
def test_yaml_aliases_are_refused_not_expanded():
    with pytest.raises(ep.SourceError, match="YAML alias at line 2"):
        ep.parse_yaml("a: &x [1, 2]\nb: *x\n", "t.yaml")


def test_an_anchor_without_an_alias_is_harmless():
    assert ep.parse_yaml("a: &x 1\nb: 2\n", "t.yaml").data == {"a": 1, "b": 2}


def test_a_set_whose_file_uses_an_alias_is_reported_not_read():
    files = project()
    files["configs/task-fast.yaml"] = "subject_kind: human\nbase: &b {ms: 1}\niti: *b\n"
    entry = ep.extract_experiment("x", files, source_commit="c", docs=DOCS, library_files=LIB)
    fast = next(s for s in entry["sets"] if s["id"] == "fast-human")
    assert fast["values"] == [] and fast["runValues"] == []
    assert any("fast-human" in w and "YAML alias" in w for w in entry["warnings"])


def test_an_alias_in_another_config_is_reported():
    files = project()
    files["configs/rig-booth.yaml"] = "a: &x 1\nb: *x\n"
    entry = ep.extract_experiment("x", files, source_commit="c", docs=DOCS, library_files=LIB)
    assert any("rig-booth.yaml: YAML alias" in w for w in entry["warnings"])
    (rig,) = entry["otherConfigs"]
    assert "extends" not in rig


def test_invalid_yaml_in_another_config_is_reported():
    files = project()
    files["configs/rig-booth.yaml"] = "a: [1\n"
    entry = ep.extract_experiment("x", files, source_commit="c", docs=DOCS, library_files=LIB)
    assert "configs/rig-booth.yaml: invalid YAML" in entry["warnings"]


def test_no_run_py_is_an_error():
    with pytest.raises(ep.SourceError, match="no run.py"):
        ep.extract_experiment("x", {"configs/task.yaml": "a: 1\n"}, source_commit="c0ffee")


def test_experiment_code_is_never_executed(tmp_path):
    sentinel = tmp_path / "ran"
    files = project()
    hostile = f"open({str(sentinel)!r}, 'w').write('x')\n"
    files["run.py"] = hostile + RUN_PY
    files["src/demo/task.py"] = hostile + TASK_PY
    ep.extract_experiment("x", files, source_commit="c", docs=DOCS, library_files=LIB)
    assert not sentinel.exists()


class TestInheritedDefaults:
    def test_listed_when_the_library_is_given(self):
        # OtherParams inherits SubjectParams from alhazen; its set has no params file, so
        # every value is a model default, the inherited ones read from alhazen's source.
        human = next(s for s in build()["sets"] if s["id"] == "other-human")
        inherited = {v["key"]: v for v in human["runValues"]}
        assert sorted(inherited) == ["reward", "subject_kind"]
        assert all(v["source"] == "model default" for v in inherited.values())
        assert inherited["subject_kind"]["definedIn"].startswith("lib:alhazen/task/subject_kind.py")

    def test_not_listed_without_the_library(self):
        human = next(s for s in build(library_files=None)["sets"] if s["id"] == "other-human")
        assert [v["key"] for v in human["values"] + human["runValues"]] == ["level"]

    def test_warned_when_the_library_is_absent(self):
        entry = build(library_files=None)
        warnings = "\n".join(entry["warnings"])
        assert "params model bases not readable" in warnings
        assert "run metadata fields (alhazen SubjectParams) unknown" in warnings

    def test_build_parameter_guide_says_when_alhazen_is_not_readable(self):
        entry = ep.build_parameter_guide(
            "demo-exp", project(), source_commit="c0ffee", docs=DOCS, library=None, pinned="v9.9.9"
        )
        assert entry["alhazen"] == "v9.9.9"
        assert "runMetadataFields" not in entry
        assert (
            "alhazen v9.9.9 source not readable: defaults inherited from alhazen are not listed"
            in entry["warnings"]
        )

    def test_build_parameter_guide_binds_the_alhazen_it_read(self):
        files = project()
        files["pyproject.toml"] = (
            "[tool.uv.sources]\n"
            'alhazen-vision = { git = "https://example.org/a", tag = "v2.1.0" }\n'
        )
        entry = ep.build_parameter_guide(
            "demo-exp",
            files,
            source_commit="c0ffee",
            docs=DOCS,
            library=LIB,
            pinned="v0",
            listed_sets=["Main (human)"],
            listed_ladders=["Other ladder"],
        )
        assert entry["alhazen"] == "v2.1.0"
        assert entry["runMetadataFields"] == ["reward", "subject_kind"]
        warnings = "\n".join(entry["warnings"])
        assert "listing parameterSets ['Main (human)'] differ from run.py at c0ffee" in warnings
        assert "listing ladders ['Other ladder'] differ from run.py" in warnings

    def test_matching_listing_names_add_no_warning(self):
        plain = ep.build_parameter_guide(
            "demo-exp", project(), source_commit="c", docs=DOCS, library=LIB
        )
        named = ep.build_parameter_guide(
            "demo-exp",
            project(),
            source_commit="c",
            docs=DOCS,
            library=LIB,
            listed_sets=[s["name"] for s in plain["sets"]],
            listed_ladders=[lad["name"] for lad in plain["ladders"]],
        )
        assert named["warnings"] == plain["warnings"]


class TestLibrarySources:
    def test_alhazen_pin(self):
        pyproject = 'alhazen-vision = { git = "https://example.org/x.git", tag = "v2.16.0" }\n'
        assert ep.alhazen_pin({"pyproject.toml": pyproject}) == "v2.16.0"
        assert ep.alhazen_pin({"pyproject.toml": b"[project]\n"}) is None
        assert ep.alhazen_pin({}) is None

    def test_run_fields_from_library(self):
        assert ep.run_fields_from_library(LIB) == {"subject_kind", "reward"}
        assert ep.run_fields_from_library(None) is None
        assert ep.run_fields_from_library({"alhazen/task/subject_kind.py": "x = 1\n"}) is None
        assert ep.run_fields_from_library({"alhazen/task/subject_kind.py": "def (:\n"}) is None

    def test_library_files_from_a_source_tree(self, tmp_path):
        repo = tmp_path / "alhazen"
        (repo / "src" / "alhazen" / "task").mkdir(parents=True)
        (repo / "src" / "alhazen" / "__init__.py").write_text("")
        (repo / "src" / "alhazen" / "task" / "subject_kind.py").write_text(ALHAZEN_SK)
        (repo / "src" / "alhazen" / "task" / "data.yaml").write_text("a: 1\n")
        (repo / "README.md").write_text("x\n")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.org"]
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run([*git, "-C", str(repo), "add", "."], check=True)
        subprocess.run([*git, "-C", str(repo), "commit", "-qm", "x"], check=True)
        whole = ep.library_files(read_tree(repo, "HEAD"))
        under_src = ep.library_files(read_tree(repo, "HEAD", "src"))
        assert whole == under_src
        assert sorted(whole) == ["alhazen/__init__.py", "alhazen/task/subject_kind.py"]
        assert ep.run_fields_from_library(whole) == {"subject_kind", "reward"}

    def test_library_files_refuses_a_tree_that_is_not_alhazen(self, tmp_path):
        repo = tmp_path / "other"
        repo.mkdir()
        (repo / "x.py").write_text("")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.org"]
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run([*git, "-C", str(repo), "add", "."], check=True)
        subprocess.run([*git, "-C", str(repo), "commit", "-qm", "x"], check=True)
        with pytest.raises(ep.SourceError, match="not alhazen's source"):
            ep.library_files(read_tree(repo, "HEAD"))

    def test_installed_library_is_this_alhazen(self):
        import alhazen

        files, version = ep.installed_library()
        assert version == alhazen.__version__
        assert "alhazen/task/subject_kind.py" in files
        assert "alhazen/experiment_platform/parameters.py" in files
        assert all(path.startswith("alhazen/") and path.endswith(".py") for path in files)
        assert ep.run_fields_from_library(files) >= {"subject_kind"}

    def test_pin_matches(self):
        assert ep.pin_matches("2.16.0", "v2.16.0")
        assert ep.pin_matches("v2.16.0", "2.16.0")
        assert not ep.pin_matches("2.16.1", "v2.16.0")
        assert not ep.pin_matches("2.16.0", None)


MESSY_RUN_PY = """
from pathlib import Path
from demo.task import DemoTask, OtherTask
import demo
HERE = Path(__file__).parent
KEY = "computed"
TASKS = {
    "demo": (DemoTask, HERE / "configs" / "task.yaml"),
    "other": (OtherTask, None),
    "dotted": (demo.DemoTask, None),
    "bare": DemoTask,
    KEY: (DemoTask, None),
}
PARAMETERS = {
    "Main (human)": ("demo", HERE / "configs" / "task.yaml"),
    "Main (human)": ("demo", HERE / "configs" / "task-fast.yaml"),
    "Main  (human)": ("demo", HERE / "configs" / "task-list.yaml"),
    "Odd": "demo",
    KEY: ("demo", None),
}
LADDERS = {
    "Missing (monkey)": HERE / "configs" / "training-missing.yaml",
    "Broken (monkey)": HERE / "configs" / "training-broken.yaml",
    "Bare (monkey)": HERE / "configs" / "training-bare.yaml",
    "Bad params (monkey)": HERE / "configs" / "training-badparams.yaml",
    KEY: HERE / "configs" / "x.yaml",
}
"""


def messy_project() -> dict:
    files = project()
    files["run.py"] = MESSY_RUN_PY
    files["configs/task.yaml"] = (
        "subject_kind: human\nweird: .nan\nlookup: configs/table.yaml\ndotted:\n  a.b: 1\n"
    )
    files["configs/table.yaml"] = "rows: []\n"
    files["configs/task-list.yaml"] = "- 1\n- 2\n"
    files["configs/training-broken.yaml"] = "stages: [\n"
    files["configs/training-bare.yaml"] = "name: bare\n"
    files["configs/training-badparams.yaml"] = (
        "name: b\ntitle: B\ntask: nope\nparams: ../elsewhere/task.yaml\n"
        "stages:\n  - id: s\n    title: S\n    task: ghost\n    params: absent.yaml\n"
    )
    return files


class TestMessySource:
    """Constructs the extractor reports instead of guessing (no experiment code runs)."""

    def entry(self) -> dict:
        return ep.extract_experiment(
            "messy", messy_project(), source_commit="c0ffee", docs=DOCS, library_files=LIB
        )

    def test_registry_warnings(self):
        warnings = "\n".join(self.entry()["warnings"])
        for expected in [
            "TASKS has a non-literal key",
            "task class is not a plain name",
            "is not a (class, params) tuple",
            "PARAMETERS has a non-literal label",
            "names 'Main (human)' twice",
            "PARAMETERS['Odd'] is not (task, params)",
            "LADDERS has a non-literal label",
        ]:
            assert expected in warnings, expected

    def test_values_that_need_care(self):
        entry = self.entry()
        main = next(s for s in entry["sets"] if s["name"] == "Main (human)")
        values = {v["key"]: v for v in main["values"]}
        assert values["weird"]["value"] == "nan"
        assert values["lookup"]["file"] is True
        assert main["referencedFiles"] == [{"key": "lookup", "path": "configs/table.yaml"}]
        assert values["dotted"]["value"] == {"a.b": 1}
        warnings = "\n".join(entry["warnings"])
        assert "non-finite number" in warnings
        assert "has keys containing '.'" in warnings
        table = next(c for c in entry["otherConfigs"] if c["configPath"] == "configs/table.yaml")
        assert table["kind"] == "data file named by a parameter value"
        assert table["referencedBy"] == ["lookup"]

    def test_set_ids_stay_unique_and_bad_files_are_reported(self):
        entry = self.entry()
        ids = [s["id"] for s in entry["sets"]]
        assert len(ids) == len(set(ids)) and "main-human-2" in ids
        warnings = "\n".join(entry["warnings"])
        assert "task-list.yaml is not a mapping at the top level" in warnings

    def test_ladder_warnings(self):
        warnings = "\n".join(self.entry()["warnings"])
        assert "training-missing.yaml not in the source" in warnings
        assert "'Broken (monkey)'" in warnings
        assert "training-bare.yaml lacks name, title or a stages list" in warnings
        assert "runs 'ghost', not one of TASKS" in warnings

    def test_docs_input_errors(self):
        with pytest.raises(ep.SourceError, match="not JSON"):
            ep.extract_experiment("x", project(), source_commit="c", docs={"tasks": "{bad"})
        with pytest.raises(ep.SourceError, match="must be lists"):
            ep.extract_experiment("x", project(), source_commit="c", docs={"tasks": {"a": 1}})

    def test_no_sets_is_reported(self):
        files = project()
        files["run.py"] = "TASKS = {}\nPARAMETERS = {}\n"
        entry = ep.extract_experiment("x", files, source_commit="c")
        assert "run.py declares no PARAMETERS sets" in entry["warnings"]
