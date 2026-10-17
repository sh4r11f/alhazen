"""alhazen.experiment_platform.parameters: parameter-set guides read from source as text.

Characterization tests of the Mana Experiment Hub's extractor (enrich_parameters.py),
ported with the module; a neutral fake experiment is written here. No experiment code is
imported or run.
"""

from __future__ import annotations

import json
import unittest

from alhazen.experiment_platform import parameters as ep

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
