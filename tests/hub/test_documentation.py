"""alhazen.hub.documentation: the documentation contract, checked against source.

Three claims are held here:

1. The scaffold example (tests/hub/fixtures/documentation/scaffold) documents
   the experiment `alhazen new` actually writes: every documented default is
   the scaffolded params model's own, the outcomes and events are the task's,
   and the package runs (simulate, headless) producing only documented
   outcomes and the documented events.
2. Untrusted documentation is data: only declared, hash-checked files are
   read; nothing is imported; bounds, unknown fields, non-finite numbers,
   aliases and broken references are refused with a DocumentationError.
3. The global guide is alhazen's own: its modes and flag rules are the Mode
   enum's and flag_refusal's, and every source it names exists.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, get_args

import pytest

from alhazen._scaffold import scaffold
from alhazen.hub.documentation import (
    DocumentationError,
    global_guide,
    read_documentation,
)

FIXTURE = Path(__file__).parent / "fixtures" / "documentation" / "scaffold"
EXPERIMENT = "fixation_demo"
TASK_ID = "fixation-demo"
RESERVED = {"ABORTED", "PAUSED", "DROPPED_FRAMES"}


# ---------------------------------------------------------------------------
# Packages: a ZIP with the contract's manifest (docs/hub/api-contract.md)
# ---------------------------------------------------------------------------
def make_package(
    tmp_path: Path,
    files: dict[str, bytes],
    *,
    documentation: str | None = "docs/experiment.json",
    manifest_override: dict[str, Any] | None = None,
    archive_override: dict[str, bytes] | None = None,
) -> tuple[Path, dict[str, Any]]:
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "name": "fixation-demo",
        "version": "0.1.0",
        "title": "Fixation demo",
        "description": "test package",
        "entrypoint": "run.py",
        "python_min": "3.10",
        "alhazen_min": "2.13.0",
        "platforms": ["linux", "darwin", "win32"],
        "hardware": {"display": True, "eye_tracker": True, "reward": False},
        "license": "MIT",
        "citations": [],
        "files": [
            {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for path, data in sorted(files.items())
        ],
    }
    if documentation is not None:
        manifest["documentation"] = documentation
    manifest.update(manifest_override or {})
    bundle = tmp_path / "package.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("alhazen-package.json", json.dumps(manifest))
        for path, data in {**files, **(archive_override or {})}.items():
            archive.writestr(path, data)
    return bundle, manifest


def fixture_files() -> dict[str, bytes]:
    """The fixture's files (all text) with the line endings they were
    committed with. Git on Windows checks text out with CRLF by default
    (core.autocrlf), which changes each file's bytes and so the SHA-256 the
    resolved fixture records for it: the same checkout passed on Linux and
    failed on Windows."""
    return {
        str(path.relative_to(FIXTURE).as_posix()): path.read_bytes().replace(b"\r\n", b"\n")
        for path in sorted(FIXTURE.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def scaffolded(tmp_path: Path) -> Path:
    """`alhazen new fixation_demo` with the example documentation copied in."""
    root = scaffold(EXPERIMENT, tmp_path / "src-tree")
    shutil.copytree(FIXTURE / "docs", root / "docs")
    return root


def package_files(root: Path) -> dict[str, bytes]:
    wanted = [
        "run.py",
        "pyproject.toml",
        "README.md",
        "configs/task.yaml",
        f"src/{EXPERIMENT}/__init__.py",
        f"src/{EXPERIMENT}/task.py",
        "docs/experiment.json",
        "docs/methods.md",
        f"docs/tasks/{TASK_ID}.md",
    ]
    return {path: (root / path).read_bytes() for path in wanted}


def descriptor() -> dict[str, Any]:
    return json.loads((FIXTURE / "docs" / "experiment.json").read_text(encoding="utf-8"))


def with_descriptor(tmp_path: Path, value: Any, extra: dict[str, bytes] | None = None):
    files = fixture_files()
    files["configs/task.yaml"] = scaffold_task_yaml(tmp_path)
    files["docs/experiment.json"] = (
        value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
    )
    files.update(extra or {})
    return make_package(tmp_path, files)


_YAML_CACHE: dict[str, bytes] = {}


def scaffold_task_yaml(tmp_path: Path) -> bytes:
    if "yaml" not in _YAML_CACHE:
        root = scaffold(EXPERIMENT, tmp_path / "yaml-source")
        _YAML_CACHE["yaml"] = (root / "configs" / "task.yaml").read_bytes()
    return _YAML_CACHE["yaml"]


def the_task(documentation: dict[str, Any]) -> dict[str, Any]:
    (task,) = documentation["tasks"]
    return task


# ---------------------------------------------------------------------------
# 1. The example is the scaffold's truth
# ---------------------------------------------------------------------------
class TestScaffoldExample:
    def resolved(self, scaffolded: Path, tmp_path: Path) -> dict[str, Any]:
        bundle, manifest = make_package(tmp_path, package_files(scaffolded))
        documentation = read_documentation(bundle, manifest)
        assert documentation is not None
        return documentation

    def scaffold_module(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        """The rendered package's own task module (our scaffold, in this test
        only: the hub itself never imports a package)."""
        monkeypatch.syspath_prepend(str(root / "src"))
        for name in [m for m in sys.modules if m == EXPERIMENT or m.startswith(EXPERIMENT + ".")]:
            monkeypatch.delitem(sys.modules, name)
        return importlib.import_module(f"{EXPERIMENT}.task")

    def test_every_default_is_the_params_models_default(self, scaffolded, tmp_path, monkeypatch):
        task = the_task(self.resolved(scaffolded, tmp_path))
        module = self.scaffold_module(scaffolded, monkeypatch)
        params = module.FixationDemoTaskParams()
        for parameter in task["parameters"]:
            value: Any = params
            for part in parameter["name"].split("."):
                value = getattr(value, part)
            if parameter["type"] == "duration":
                value = {key: v for key, v in value.model_dump().items() if v is not None}
            assert parameter["default"] == value, parameter["name"]
            assert parameter["has_default"]

    def test_defaults_were_checked_against_the_params_file(self, scaffolded, tmp_path):
        task = the_task(self.resolved(scaffolded, tmp_path))
        statuses = {p["name"]: p["source"]["status"] for p in task["parameters"]}
        assert statuses.pop("paradigm.shuffle") == "declared"  # not in task.yaml
        assert set(statuses.values()) == {"matched"}
        # Every key the params file sets is documented.
        assert task["parameters_undocumented"] == []

    def test_scheduler_choices_are_the_schedulers(self, scaffolded, tmp_path):
        from alhazen.paradigms.config import SchedulerConfig

        task = the_task(self.resolved(scaffolded, tmp_path))
        (kind,) = [p for p in task["parameters"] if p["name"] == "paradigm.kind"]
        assert kind["constraints"]["choices"] == list(
            get_args(SchedulerConfig.model_fields["kind"].annotation)
        )

    def test_outcomes_and_events_are_the_tasks(self, scaffolded, tmp_path, monkeypatch):
        task = the_task(self.resolved(scaffolded, tmp_path))
        module = self.scaffold_module(scaffolded, monkeypatch)
        declared = {}
        for entry in module.FixationDemoTask.outcomes:
            outcome = (
                entry if hasattr(entry, "completed") else module.FixationDemoTask.outcomes[entry]
            )
            if outcome.name not in RESERVED:
                declared[outcome.name] = (outcome.completed, outcome.success)
        documented = {o["name"]: (o["completed"], o["success"]) for o in task["outcomes"]}
        assert documented == declared
        assert {e["name"] for e in task["events"]} == set(module.FixationDemoTask.events.declared)
        assert module.FixationDemoTask.name == task["id"]

    def test_timeline_keeps_the_subject_dependent_phase_unscaled(self, scaffolded, tmp_path):
        timeline = the_task(self.resolved(scaffolded, tmp_path))["timeline"]
        acquire, hold = timeline["phases"]
        assert acquire["timing"]["kind"] == "event"
        assert acquire["timing"]["scaled"] is False
        assert acquire["timing"]["ms"] is None
        assert acquire["timing"]["max_ms"] == 2000
        assert acquire["start_events"] == ["FIX_ON"]
        assert acquire["end_events"] == ["FIX_ACQUIRED"]
        assert hold["timing"] == {
            "kind": "parameter",
            "param": "hold_duration",
            "param_label": "Hold duration",
            "ms": 500.0,
            "text": "500 ms",
            "scaled": True,
        }
        assert timeline["between_trials"]["timing"]["ms"] == 500.0
        assert [b["outcome"] for b in timeline["branches"]] == [
            "NO_FIXATION",
            "FIX_BREAK",
            "ABORTED",
        ]

    def test_diagram_draws_the_source_geometry(self, scaffolded, tmp_path):
        diagram = the_task(self.resolved(scaffolded, tmp_path))["diagram"]
        window, point, radius, diameter = diagram["elements"]
        # CircleRegion's radius is fix_window_dva; FixationPoint's radius is size / 2.
        assert (window["r"], window["refs"]["r"]) == (
            2.0,
            {"param": "fix_window_dva", "factor": 1.0},
        )
        assert point["r"] == pytest.approx(0.15)
        assert point["luminance"] == 1.0
        assert diagram["background"] == 0.5  # PsychoPy window colour (0, 0, 0): mid-grey
        assert radius["value_text"] == "2 dva"
        assert diameter["value_text"] == "0.3 dva"

    def test_provenance_names_the_version_and_files(self, scaffolded, tmp_path):
        documentation = self.resolved(scaffolded, tmp_path)
        source = documentation["source"]
        assert source["package"] == "fixation-demo"
        assert source["version"] == "0.1.0"
        assert source["descriptor"] == "docs/experiment.json"
        assert documentation["methods"]["path"] == "docs/methods.md"
        assert "[[param:fix_window_dva]]" in documentation["methods"]["markdown"]
        assert str(tmp_path) not in json.dumps(documentation)

    def test_a_package_built_by_the_package_module_resolves(self, scaffolded, tmp_path):
        """The seam with alhazen.hub.packages: its builder and inspector carry
        the documentation pointer, and the hashed files it declares are the
        ones read here."""
        from alhazen.hub.packages import build_bundle, inspect_bundle

        metadata = {
            "name": "fixation-demo",
            "version": "0.1.0",
            "title": "Fixation demo",
            "description": "The scaffold, documented.",
            "hardware": {"display": True, "eye_tracker": True, "reward": False},
            "license": "MIT",
            "documentation": "docs/experiment.json",
        }
        built = build_bundle(
            scaffolded, tmp_path / "built.zip", metadata, sorted(package_files(scaffolded))
        )
        inspected = inspect_bundle(tmp_path / "built.zip")
        assert inspected.manifest["documentation"] == "docs/experiment.json"
        documentation = read_documentation(tmp_path / "built.zip", inspected.manifest)
        assert documentation is not None
        assert documentation["source"]["descriptor_sha256"] == next(
            f["sha256"] for f in built.manifest["files"] if f["path"] == "docs/experiment.json"
        )

    def test_the_example_package_runs_with_only_documented_outcomes(self, scaffolded, tmp_path):
        """Runnable, not just plausible: simulate the scaffolded package
        headless, as the scaffold's own acceptance test does."""
        documentation = self.resolved(scaffolded, tmp_path)
        task = the_task(documentation)
        alhazen_src = Path(importlib.import_module("alhazen").__file__).parents[1]
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": os.pathsep.join([str(scaffolded / "src"), str(alhazen_src)]),
            "HOME": str(tmp_path),
        }
        for name in ("SystemRoot", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP"):
            if name in os.environ:
                environment[name] = os.environ[name]
        session = subprocess.run(
            [
                sys.executable,
                str(scaffolded / "run.py"),
                "--task",
                TASK_ID,
                "--mode",
                "simulate",
                "--rig",
                str(scaffolded / "configs" / "rig-lab.yaml"),
                "--headless",
            ],
            capture_output=True,
            text=True,
            cwd=scaffolded,
            env=environment,
            timeout=600,
        )
        assert session.returncode == 0, session.stdout + session.stderr
        run_dir = next((scaffolded / "data-rehearsal").glob("v*/sub-*/ses-*/run-*"))
        with next(run_dir.glob("*_trials.csv")).open() as handle:
            rows = list(csv.DictReader(handle))
        assert rows
        documented = {o["name"] for o in task["outcomes"]} | RESERVED
        assert {row["outcome"] for row in rows} <= documented
        with next(run_dir.glob("*_events.csv")).open() as handle:
            events = {row.get("event") or row.get("name") for row in csv.DictReader(handle)}
        assert {e["name"] for e in task["events"]} <= events


# ---------------------------------------------------------------------------
# 2. Untrusted documentation is data
# ---------------------------------------------------------------------------
class TestLegacyAndPackageIntegrity:
    def test_a_package_without_documentation_has_none(self, tmp_path):
        bundle, manifest = make_package(tmp_path, {"run.py": b"print(1)\n"}, documentation=None)
        assert read_documentation(bundle, manifest) is None

    @pytest.mark.parametrize(
        "pointer",
        [
            "../experiment.json",
            "/docs/experiment.json",
            "docs\\experiment.json",
            "docs//experiment.json",
            "C:/x.json",
            "docs/./experiment.json",
            "",
        ],
    )
    def test_unsafe_pointers_are_refused(self, tmp_path, pointer):
        bundle, manifest = make_package(tmp_path, fixture_files(), documentation=pointer)
        with pytest.raises(DocumentationError):
            read_documentation(bundle, manifest)

    def test_an_undeclared_descriptor_is_refused(self, tmp_path):
        bundle, manifest = make_package(
            tmp_path,
            {"run.py": b""},
            documentation="docs/experiment.json",
            archive_override={"docs/experiment.json": json.dumps(descriptor()).encode()},
        )
        with pytest.raises(DocumentationError, match="not a file this package declares"):
            read_documentation(bundle, manifest)

    def test_a_file_that_does_not_match_its_hash_is_refused(self, tmp_path):
        files = fixture_files()
        files["configs/task.yaml"] = scaffold_task_yaml(tmp_path)
        tampered = files["docs/methods.md"].replace(b"Purpose", b"Purpoze")
        bundle, manifest = make_package(
            tmp_path, files, archive_override={"docs/methods.md": tampered}
        )
        with pytest.raises(DocumentationError, match="SHA-256"):
            read_documentation(bundle, manifest)

    def test_a_declared_file_missing_from_the_archive_is_refused(self, tmp_path):
        value = descriptor()
        value["tasks"][0]["description"] = "docs/extra.md"
        bundle, manifest = with_descriptor(tmp_path, value)
        manifest["files"].append({"path": "docs/extra.md", "size": 3, "sha256": "0" * 64})
        with pytest.raises(DocumentationError, match="missing from the archive"):
            read_documentation(bundle, manifest)

    def test_a_non_zip_is_refused(self, tmp_path):
        bundle = tmp_path / "not.zip"
        bundle.write_bytes(b"not a zip")
        manifest = {"documentation": "docs/experiment.json", "files": []}
        with pytest.raises(DocumentationError, match="could not be opened"):
            read_documentation(bundle, manifest)

    def test_nothing_in_the_package_is_imported(self, tmp_path):
        sentinel = tmp_path / "imported"
        hostile = f"open({str(sentinel)!r}, 'w').write('x')\nclass Params:\n    pass\n"
        value = descriptor()
        value["tasks"][0]["parameters"][0]["source"] = {"model": "hostile_module:Params"}
        bundle, manifest = with_descriptor(
            tmp_path, value, extra={"src/hostile_module.py": hostile.encode()}
        )
        assert read_documentation(bundle, manifest) is not None
        assert not sentinel.exists()
        assert "hostile_module" not in sys.modules


def refused(tmp_path: Path, value: Any, match: str, extra: dict[str, bytes] | None = None) -> None:
    bundle, manifest = with_descriptor(tmp_path, value, extra)
    with pytest.raises(DocumentationError, match=match):
        read_documentation(bundle, manifest)


def edit(change) -> dict[str, Any]:
    value = copy.deepcopy(descriptor())
    change(value)
    return value


def task_of(value: dict[str, Any]) -> dict[str, Any]:
    return value["tasks"][0]


def param_of(value: dict[str, Any], name: str) -> dict[str, Any]:
    return next(p for p in task_of(value)["parameters"] if p["name"] == name)


class TestDescriptorValidation:
    def test_the_fixture_alone_resolves(self, tmp_path):
        bundle, manifest = with_descriptor(tmp_path, descriptor())
        assert read_documentation(bundle, manifest)["title"].startswith("Fixation demo")

    def test_a_default_that_disagrees_with_the_params_file_is_refused(self, tmp_path):
        value = edit(lambda v: param_of(v, "hold_duration").update(default={"ms": 600}))
        refused(tmp_path, value, "differs from 'configs/task.yaml'")

    def test_a_missing_default_is_read_from_the_params_file_not_invented(self, tmp_path):
        value = edit(lambda v: param_of(v, "fix_size_dva").pop("default"))
        bundle, manifest = with_descriptor(tmp_path, value)
        task = the_task(read_documentation(bundle, manifest))
        size = next(p for p in task["parameters"] if p["name"] == "fix_size_dva")
        assert (size["default"], size["source"]["status"]) == (0.3, "read")

    def test_an_unchecked_parameter_without_a_default_stays_without_one(self, tmp_path):
        value = edit(lambda v: param_of(v, "paradigm.shuffle").pop("default"))
        bundle, manifest = with_descriptor(tmp_path, value)
        task = the_task(read_documentation(bundle, manifest))
        shuffle = next(p for p in task["parameters"] if p["name"] == "paradigm.shuffle")
        assert (shuffle["has_default"], shuffle["default_text"]) == (False, None)

    def test_a_key_the_params_file_lacks_is_refused(self, tmp_path):
        value = edit(lambda v: param_of(v, "iti").update(name="inter_trial"))
        refused(tmp_path, value, "has no key 'inter_trial'")

    @pytest.mark.parametrize(
        ("change", "match"),
        [
            (lambda v: v.update(colour="red"), "unknown field"),
            (lambda v: task_of(v).update(timeline_typo=1), "unknown field"),
            (lambda v: v.update(schema_version=2), "not supported"),
            (lambda v: v.update(schema="something"), "schema must be"),
            (lambda v: param_of(v, "iti").update(type="time"), "type must be"),
            (lambda v: param_of(v, "iti").update(meaning=""), "must not be empty"),
            (lambda v: param_of(v, "iti").update(meaning="x" * 2001), "limit is 2000"),
            (
                lambda v: param_of(v, "paradigm.n_per_condition").update(constraints={"min": 11}),
                "below the documented minimum",
            ),
            (
                lambda v: task_of(v)["timeline"]["phases"][1]["timing"].update(param="nope"),
                "not a documented parameter",
            ),
            (
                lambda v: task_of(v)["timeline"]["phases"][1]["timing"].update(
                    param="fix_size_dva"
                ),
                "is not a duration",
            ),
            (
                lambda v: task_of(v)["timeline"]["phases"][0]["timing"].pop("until"),
                "until is required",
            ),
            (
                lambda v: task_of(v)["timeline"]["phases"][0].update(start_events=["FIX_OFF"]),
                "event 'FIX_OFF'",
            ),
            (
                lambda v: task_of(v)["timeline"]["branches"][0].update(outcome="MISSED"),
                "outcome 'MISSED'",
            ),
            (
                lambda v: task_of(v)["timeline"]["tracks"][0].update(
                    **{"from": "hold", "to": "acquire"}
                ),
                "in order",
            ),
            (
                lambda v: task_of(v)["timeline"]["phases"][1].update(
                    timing={
                        "kind": "conditional",
                        "when": "x",
                        "timing": {
                            "kind": "conditional",
                            "when": "y",
                            "timing": {"kind": "fixed", "ms": 1},
                        },
                    }
                ),
                "kind must be one of fixed, parameter, event",
            ),
            (
                lambda v: task_of(v)["diagram"]["elements"][0].update(type="svg"),
                "type must be one of",
            ),
            (
                lambda v: task_of(v)["diagram"]["elements"][0].update(r={"param": "iti"}),
                "must be a number with a default",
            ),
            (
                lambda v: task_of(v)["diagram"]["elements"][0].update(
                    r={"param": "fix_window_dva", "factor": -1}
                ),
                "greater than zero",
            ),
            (lambda v: task_of(v)["diagram"]["elements"][0].update(cx=500), "outside the drawable"),
            (
                lambda v: task_of(v)["diagram"]["elements"].append(
                    {
                        "type": "dot_field",
                        "cx": 0,
                        "cy": 0,
                        "radius": 1,
                        "dot_radius": 0.05,
                        "count": 401,
                    }
                ),
                "count must be a whole number",
            ),
            (
                lambda v: task_of(v)["diagram"]["elements"][0].update(
                    r={"param": "fix_window_dva", "factor": "2*3"}
                ),
                "must be a number",
            ),
            (lambda v: task_of(v)["diagram"]["elements"][0].update(luminance=2), "luminance"),
            (
                lambda v: v["tasks"].append(copy.deepcopy(v["tasks"][0])),
                "names 'fixation-demo' twice",
            ),
            (lambda v: v.update(tasks=[], methods=None), "methods or at least one task"),
        ],
    )
    def test_malformed_descriptors_are_refused(self, tmp_path, change, match):
        refused(tmp_path, edit(change), match)

    def test_the_engines_reserved_outcomes_may_be_drawn(self, tmp_path):
        value = edit(
            lambda v: task_of(v)["timeline"]["branches"].append(
                {
                    "from": "hold",
                    "kind": "failure",
                    "when": "too many frames dropped",
                    "outcome": "DROPPED_FRAMES",
                }
            )
        )
        bundle, manifest = with_descriptor(tmp_path, value)
        assert read_documentation(bundle, manifest) is not None

    def test_frame_durations_are_not_drawn_to_scale(self, tmp_path):
        files_yaml = scaffold_task_yaml(tmp_path).replace(
            b"hold_duration: {ms: 500}", b"hold_duration: {frames: 30}"
        )
        value = edit(lambda v: param_of(v, "hold_duration").update(default={"frames": 30}))
        bundle, manifest = with_descriptor(tmp_path, value, extra={"configs/task.yaml": files_yaml})
        hold = the_task(read_documentation(bundle, manifest))["timeline"]["phases"][1]["timing"]
        assert (hold["ms"], hold["scaled"]) == (None, False)
        assert "refresh rate" in hold["text"]

    def test_a_conditional_phase_says_when(self, tmp_path):
        value = edit(
            lambda v: task_of(v)["timeline"]["phases"].append(
                {
                    "id": "feedback",
                    "label": "Feedback",
                    "timing": {
                        "kind": "conditional",
                        "when": "the trial was correct",
                        "timing": {"kind": "fixed", "ms": 250},
                    },
                }
            )
        )
        bundle, manifest = with_descriptor(tmp_path, value)
        phase = the_task(read_documentation(bundle, manifest))["timeline"]["phases"][2]["timing"]
        assert phase["kind"] == "conditional"
        assert phase["inner"]["ms"] == 250
        assert phase["text"] == "only if the trial was correct: 250 ms"

    @pytest.mark.parametrize(
        ("raw", "match"),
        [
            (b'{"schema": "a", "schema": "b"}', "repeats the key"),
            (b'{"schema_version": NaN}', "not a finite number"),
            (b'{"schema_version": Infinity}', "not a finite number"),
            (b"[" * 40 + b"]" * 40, "nested more than"),
            (b"{not json", "not valid JSON"),
            (b"\xff\xfe", "not UTF-8"),
        ],
    )
    def test_hostile_json_is_refused(self, tmp_path, raw, match):
        refused(tmp_path, raw, match)

    def test_overflowing_numbers_are_refused(self, tmp_path):
        value = json.dumps(descriptor()).replace('"background": 0.5', '"background": 1e999')
        refused(tmp_path, value.encode(), "finite number")

    def test_an_oversized_descriptor_is_refused(self, tmp_path):
        value = edit(lambda v: v.update(summary="x" * 300_000))
        refused(tmp_path, value, "the limit is 262144")

    def test_yaml_aliases_in_the_params_file_are_refused(self, tmp_path):
        bomb = b"a: &a [1, 2]\nb: *a\nfix_size_dva: 0.3\n"
        refused(tmp_path, descriptor(), "YAML alias", extra={"configs/task.yaml": bomb})

    @pytest.mark.parametrize(
        ("text", "match"),
        [
            ("See [[param:not_a_parameter]].", "names no single documented parameter"),
            ("See [[task:other]].", "unknown task"),
            ("See [[figure:one]].", "only param and task"),
        ],
    )
    def test_broken_markdown_references_are_refused(self, tmp_path, text, match):
        refused(tmp_path, descriptor(), match, extra={"docs/methods.md": text.encode()})

    def test_qualified_references_resolve_in_the_methods(self, tmp_path):
        text = b"[[param:fixation-demo/iti]] and [[param:iti]]"
        bundle, manifest = with_descriptor(tmp_path, descriptor(), extra={"docs/methods.md": text})
        assert read_documentation(bundle, manifest)["methods"]["markdown"] == text.decode()

    def test_raw_html_is_kept_as_text_for_the_renderer(self, tmp_path):
        """Markdown is returned verbatim; hub_docs.js renders it as text nodes
        (tests/js/hub_docs.test.mjs holds that half)."""
        text = b"<script>alert(1)</script> <img src=x onerror=alert(1)>"
        bundle, manifest = with_descriptor(tmp_path, descriptor(), extra={"docs/methods.md": text})
        assert read_documentation(bundle, manifest)["methods"]["markdown"] == text.decode()


# ---------------------------------------------------------------------------
# 3. The global guide is alhazen's own
# ---------------------------------------------------------------------------
class TestGlobalGuide:
    def test_modes_are_the_mode_enum_in_order(self):
        from alhazen.modes import MODE_SUMMARIES, Mode

        modes = global_guide()["modes"]
        assert [m["id"] for m in modes] == [mode.value for mode in Mode]
        for record in modes:
            mode = Mode(record["id"])
            assert record["summary"] == MODE_SUMMARIES[mode]
            assert record["runs_trials"] == mode.runs_trials
            assert record["drives_subject"] == mode.drives_subject
            assert record["writes_real_data"] == mode.writes_real_data

    def test_flags_follow_flag_refusal(self):
        guide = {m["id"]: m for m in global_guide()["modes"]}
        assert [m for m, r in guide.items() if r["accepts"]["headless"]] == ["simulate"]
        assert [m for m, r in guide.items() if r["accepts"]["mouse"]] == ["test"]
        assert {m for m, r in guide.items() if r["accepts"]["calibration_target"]} == {
            "test",
            "run",
            "training",
        }
        assert {m for m, r in guide.items() if r["refuses_development_rig"]} == {"run", "training"}

    def test_data_destinations_use_the_real_suffixes(self):
        from alhazen.modes.rehearsal import REHEARSAL_SUFFIX
        from alhazen.training.ladder import TRAINING_SUFFIX

        guide = {m["id"]: m for m in global_guide()["modes"]}
        assert REHEARSAL_SUFFIX in guide["simulate"]["data"]
        assert REHEARSAL_SUFFIX in guide["test"]["data"]
        assert TRAINING_SUFFIX in guide["training"]["data"]

    def test_every_named_source_exists(self):
        guide = global_guide()
        sources = [m["source"] for m in guide["modes"]]
        sources += [
            s for section in guide["sections"] for i in section["items"] for s in i["sources"]
        ]
        assert sources
        for source in sources:
            module_name, _, attribute = source.partition(":")
            module = importlib.import_module(module_name)
            if attribute:
                assert hasattr(module, attribute), source

    def test_listed_values_are_read_from_source(self):
        from alhazen.config.models import (
            CALIBRATION_APPEARANCES,
            CALIBRATION_MOTIONS,
            FrameQAConfig,
        )
        from alhazen.task.subject_kind import SubjectKind

        items = {i["id"]: i for s in global_guide()["sections"] for i in s["items"]}
        assert items["frame-qa"]["values"] == list(
            get_args(FrameQAConfig.model_fields["policy"].annotation)
        )
        assert items["subject-kind"]["values"] == [kind.value for kind in SubjectKind]
        assert items["targets"]["values"] == [*CALIBRATION_APPEARANCES, *CALIBRATION_MOTIONS]

    def test_it_is_deterministic_json_and_needs_no_hub_extras(self):
        assert json.dumps(global_guide()) == json.dumps(global_guide())
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, sys; from alhazen.hub.documentation import global_guide; "
                "json.dumps(global_guide()); "
                "print(sorted(m for m in ('fastapi', 'sqlalchemy', 'uvicorn', 'psycopg', 'argon2')"
                " if m in sys.modules))",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout.strip() == "[]"


# ---------------------------------------------------------------------------
# 4. The renderer's fixtures are this module's real output
# ---------------------------------------------------------------------------
RESOLVED = Path(__file__).parent / "fixtures" / "documentation" / "resolved"
REGENERATE = (
    "tests/js/hub_docs.test.mjs draws this file; regenerate it from read_documentation / "
    "global_guide (see docs/hub/documentation.md, 'Fixtures')"
)


class TestRendererFixtures:
    def test_the_scaffold_fixture_is_what_the_server_returns(self, tmp_path):
        bundle, manifest = with_descriptor(tmp_path, descriptor())
        expected = json.loads((RESOLVED / "scaffold.json").read_text(encoding="utf-8"))
        assert read_documentation(bundle, manifest) == expected, REGENERATE

    def test_the_guide_fixture_is_what_the_server_returns(self):
        expected = json.loads((RESOLVED / "guide.json").read_text(encoding="utf-8"))
        actual = json.loads(json.dumps(global_guide()))
        # The version is the installed alhazen's; everything else is fixed.
        expected.pop("alhazen_version")
        actual.pop("alhazen_version")
        assert actual == expected, REGENERATE
