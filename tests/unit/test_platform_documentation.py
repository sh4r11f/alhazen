"""alhazen.experiment_platform.documentation: the documentation contract, characterized.

The resolver is the earlier hub's (alhazen.hub.documentation), ported with its bounds and
resolved shape. These tests are that module's own, run against the port:

1. The scaffold example (tests/fixtures/platform/documentation/scaffold: the documentation
   `alhazen new fixation_demo` was documented with, plus the params file it writes)
   resolves to exactly the resolved fixture the hub produced
   (tests/fixtures/platform/documentation/resolved/scaffold.json, copied unchanged).
2. Untrusted documentation is data: only files of the source version are read, by safe
   relative path and bounded size; nothing is imported; bounds, unknown fields, non-finite
   numbers, aliases and broken references are refused with a DocumentationError.
3. The small public API callers use instead of private names behaves as the resolver does.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from alhazen.errors import AlhazenError
from alhazen.experiment_platform.documentation import (
    MAX_MARKDOWN_BYTES,
    DocumentationError,
    SourceFiles,
    format_value,
    read_documentation,
)
from alhazen.experiment_platform.errors import PlatformError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "platform" / "documentation"
SCAFFOLD = FIXTURES / "scaffold"
RESOLVED = FIXTURES / "resolved"
PACKAGE = {"name": "fixation-demo", "version": "0.1.0", "title": "Fixation demo"}


def fixture_files() -> dict[str, bytes]:
    """The fixture's files with the line endings they were committed with (git on
    Windows may check text out with CRLF, which would change the recorded SHA-256s)."""
    return {
        str(path.relative_to(SCAFFOLD).as_posix()): path.read_bytes().replace(b"\r\n", b"\n")
        for path in sorted(SCAFFOLD.rglob("*"))
        if path.is_file()
    }


def descriptor() -> dict[str, Any]:
    return json.loads((SCAFFOLD / "docs" / "experiment.json").read_text(encoding="utf-8"))


def with_descriptor(value: Any, extra: dict[str, bytes] | None = None) -> dict[str, bytes]:
    files = fixture_files()
    files["docs/experiment.json"] = (
        value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
    )
    files.update(extra or {})
    return files


def resolve(files: dict[str, bytes]) -> dict[str, Any]:
    return read_documentation(files, package=PACKAGE)


def refused(value: Any, match: str, extra: dict[str, bytes] | None = None) -> None:
    with pytest.raises(DocumentationError, match=match):
        resolve(with_descriptor(value, extra))


def edit(change) -> dict[str, Any]:
    value = copy.deepcopy(descriptor())
    change(value)
    return value


def task_of(value: dict[str, Any]) -> dict[str, Any]:
    return value["tasks"][0]


def param_of(value: dict[str, Any], name: str) -> dict[str, Any]:
    return next(p for p in task_of(value)["parameters"] if p["name"] == name)


def the_task(documentation: dict[str, Any]) -> dict[str, Any]:
    (task,) = documentation["tasks"]
    return task


# ---------------------------------------------------------------------------
# 1. The port resolves exactly what the hub resolved
# ---------------------------------------------------------------------------
class TestCharacterization:
    def test_the_scaffold_resolves_to_the_hubs_fixture(self):
        # As the hub's fixture test did: the descriptor is the fixture re-serialized by
        # json.dumps, whose SHA-256 the resolved fixture records.
        expected = json.loads((RESOLVED / "scaffold.json").read_text(encoding="utf-8"))
        assert resolve(with_descriptor(descriptor())) == expected

    def test_the_subject_dependent_phase_stays_unscaled(self):
        timeline = the_task(resolve(fixture_files()))["timeline"]
        acquire, hold = timeline["phases"]
        assert (acquire["timing"]["kind"], acquire["timing"]["scaled"]) == ("event", False)
        assert acquire["timing"]["ms"] is None and acquire["timing"]["max_ms"] == 2000
        assert hold["timing"]["ms"] == 500.0 and hold["timing"]["scaled"] is True
        assert [b["outcome"] for b in timeline["branches"]] == [
            "NO_FIXATION",
            "FIX_BREAK",
            "ABORTED",
        ]

    def test_defaults_were_checked_against_the_params_file(self):
        task = the_task(resolve(fixture_files()))
        statuses = {p["name"]: p["source"]["status"] for p in task["parameters"]}
        assert statuses.pop("paradigm.shuffle") == "declared"  # not in task.yaml
        assert set(statuses.values()) == {"matched"}
        assert task["parameters_undocumented"] == []

    def test_the_error_is_a_platform_error_and_a_value_error(self):
        assert issubclass(DocumentationError, PlatformError)
        assert issubclass(DocumentationError, AlhazenError)
        assert issubclass(DocumentationError, ValueError)


# ---------------------------------------------------------------------------
# 2. Untrusted documentation is data
# ---------------------------------------------------------------------------
class TestFileBoundary:
    @pytest.mark.parametrize(
        "pointer",
        [
            "../experiment.json",
            "/docs/experiment.json",
            "docs\\experiment.json",
            "docs//experiment.json",
            "C:/x.json",
            "docs/./experiment.json",
            ".git/experiment.json",
            "",
        ],
    )
    def test_unsafe_descriptor_paths_are_refused(self, pointer):
        with pytest.raises(DocumentationError):
            read_documentation(fixture_files(), package=PACKAGE, descriptor=pointer)

    def test_a_descriptor_that_is_not_json_by_name_is_refused(self):
        with pytest.raises(DocumentationError, match=".json file"):
            read_documentation(fixture_files(), package=PACKAGE, descriptor="docs/methods.md")

    def test_a_missing_descriptor_is_refused(self):
        files = fixture_files()
        del files["docs/experiment.json"]
        with pytest.raises(DocumentationError, match="not a file of this source version"):
            resolve(files)

    def test_a_traversing_methods_path_is_refused(self):
        refused(edit(lambda v: v.update(methods="../secret.md")), "documentation.methods")

    def test_a_methods_file_missing_from_the_version_is_refused(self):
        refused(edit(lambda v: v.update(methods="docs/absent.md")), "not a file of this")

    def test_an_oversized_markdown_file_is_refused(self):
        big = b"x" * (MAX_MARKDOWN_BYTES + 1)
        refused(descriptor(), "the limit is", extra={"docs/methods.md": big})

    def test_control_characters_in_markdown_are_refused(self):
        refused(descriptor(), "control characters", extra={"docs/methods.md": b"a\x01b"})

    def test_a_non_text_file_value_is_refused(self):
        with pytest.raises(DocumentationError, match="neither text nor bytes"):
            SourceFiles({"a.md": 3})  # type: ignore[dict-item]

    def test_text_and_bytes_are_the_same_file(self):
        text = {k: v.decode("utf-8") for k, v in fixture_files().items()}
        assert resolve(text) == resolve(fixture_files())  # type: ignore[arg-type]

    def test_nothing_in_the_source_is_imported(self, tmp_path):
        sentinel = tmp_path / "imported"
        hostile = f"open({str(sentinel)!r}, 'w').write('x')\nclass Params:\n    pass\n"
        value = descriptor()
        value["tasks"][0]["parameters"][0]["source"] = {"model": "hostile_module:Params"}
        files = with_descriptor(value, extra={"src/hostile_module.py": hostile.encode()})
        assert resolve(files) is not None
        assert not sentinel.exists()
        assert "hostile_module" not in sys.modules

    def test_the_digest_of_every_read_file_is_recorded(self):
        files = SourceFiles(fixture_files())
        documentation = read_documentation(files, package=PACKAGE)
        assert documentation["source"]["descriptor_sha256"] == files.digests["docs/experiment.json"]
        assert set(files.digests) >= {"docs/methods.md", "configs/task.yaml"}


class TestDescriptorValidation:
    def test_the_fixture_alone_resolves(self):
        files = with_descriptor(descriptor())
        assert resolve(files)["title"].startswith("Fixation demo")

    def test_a_default_that_disagrees_with_the_params_file_is_refused(self):
        value = edit(lambda v: param_of(v, "hold_duration").update(default={"ms": 600}))
        refused(value, "differs from 'configs/task.yaml'")

    def test_a_missing_default_is_read_from_the_params_file_not_invented(self):
        value = edit(lambda v: param_of(v, "fix_size_dva").pop("default"))
        files = with_descriptor(value)
        task = the_task(resolve(files))
        size = next(p for p in task["parameters"] if p["name"] == "fix_size_dva")
        assert (size["default"], size["source"]["status"]) == (0.3, "read")

    def test_an_unchecked_parameter_without_a_default_stays_without_one(self):
        value = edit(lambda v: param_of(v, "paradigm.shuffle").pop("default"))
        files = with_descriptor(value)
        task = the_task(resolve(files))
        shuffle = next(p for p in task["parameters"] if p["name"] == "paradigm.shuffle")
        assert (shuffle["has_default"], shuffle["default_text"]) == (False, None)

    def test_a_key_the_params_file_lacks_is_refused(self):
        value = edit(lambda v: param_of(v, "iti").update(name="inter_trial"))
        refused(value, "has no key 'inter_trial'")

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
    def test_malformed_descriptors_are_refused(self, change, match):
        refused(edit(change), match)

    def test_the_engines_reserved_outcomes_may_be_drawn(self):
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
        files = with_descriptor(value)
        assert resolve(files) is not None

    def test_frame_durations_are_not_drawn_to_scale(self):
        files_yaml = fixture_files()["configs/task.yaml"].replace(
            b"hold_duration: {ms: 500}", b"hold_duration: {frames: 30}"
        )
        value = edit(lambda v: param_of(v, "hold_duration").update(default={"frames": 30}))
        files = with_descriptor(value, extra={"configs/task.yaml": files_yaml})
        hold = the_task(resolve(files))["timeline"]["phases"][1]["timing"]
        assert (hold["ms"], hold["scaled"]) == (None, False)
        assert "refresh rate" in hold["text"]

    def test_a_conditional_phase_says_when(self):
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
        files = with_descriptor(value)
        phase = the_task(resolve(files))["timeline"]["phases"][2]["timing"]
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
    def test_hostile_json_is_refused(self, raw, match):
        refused(raw, match)

    def test_overflowing_numbers_are_refused(self):
        value = json.dumps(descriptor()).replace('"background": 0.5', '"background": 1e999')
        refused(value.encode(), "finite number")

    def test_an_oversized_descriptor_is_refused(self):
        value = edit(lambda v: v.update(summary="x" * 300_000))
        refused(value, "the limit is 262144")

    def test_yaml_aliases_in_the_params_file_are_refused(self):
        bomb = b"a: &a [1, 2]\nb: *a\nfix_size_dva: 0.3\n"
        refused(descriptor(), "YAML alias", extra={"configs/task.yaml": bomb})

    @pytest.mark.parametrize(
        ("text", "match"),
        [
            ("See [[param:not_a_parameter]].", "names no single documented parameter"),
            ("See [[task:other]].", "unknown task"),
            ("See [[figure:one]].", "only param and task"),
        ],
    )
    def test_broken_markdown_references_are_refused(self, text, match):
        refused(descriptor(), match, extra={"docs/methods.md": text.encode()})

    def test_qualified_references_resolve_in_the_methods(self):
        text = b"[[param:fixation-demo/iti]] and [[param:iti]]"
        files = with_descriptor(descriptor(), extra={"docs/methods.md": text})
        assert resolve(files)["methods"]["markdown"] == text.decode()

    def test_raw_html_is_kept_as_text_for_the_renderer(self):
        """Markdown is returned verbatim; hub_docs.js renders it as text nodes
        (tests/js/hub_docs.test.mjs holds that half)."""
        text = b"<script>alert(1)</script> <img src=x onerror=alert(1)>"
        files = with_descriptor(descriptor(), extra={"docs/methods.md": text})
        assert resolve(files)["methods"]["markdown"] == text.decode()


def free(name: str, kind: str, **extra: Any) -> dict[str, Any]:
    """A documented parameter not read from the params file (``source.file`` null)."""
    return {
        "name": name,
        "type": kind,
        "meaning": "A test value.",
        "source": {"file": None},
        **extra,
    }


def add_param(value: dict[str, Any], parameter: dict[str, Any]) -> None:
    task_of(value)["parameters"].append(parameter)


class TestMoreBounds:
    """Refusals the hub's own tests did not reach, one rule each."""

    @pytest.mark.parametrize(
        ("change", "match"),
        [
            (lambda v: v.update(methods=3), "must be a relative path"),
            (lambda v: v.update(methods="docs/tasks/../methods.md"), "documentation.methods"),
            (lambda v: v.update(methods="docs/experiment.json"), "must name a Markdown"),
            (lambda v: task_of(v).update(parameters_file="docs/methods.md"), ".yaml, .yml"),
            (lambda v: task_of(v).update(id="1bad"), "must be a task name"),
            (lambda v: task_of(v).update(parameters={}), "must be a list"),
            (lambda v: task_of(v).update(timeline=[1]), "must be an object"),
            (lambda v: v.update(references=["x"] * 101), "the limit is 100"),
            (lambda v: v.update(summary=3), "must be text"),
            (lambda v: v.update(summary="a\x01b"), "control characters"),
            (lambda v: task_of(v)["outcomes"][0].pop("completed"), "completed is required"),
            (lambda v: task_of(v)["outcomes"][0].update(success="yes"), "true or false"),
            (lambda v: param_of(v, "iti").update(unit="x" * 30), "unit must be short"),
            (lambda v: param_of(v, "hold_duration").update(default={"sec": 1}), "must be {'ms'"),
            (
                lambda v: param_of(v, "paradigm.n_per_condition").update(default=10.5),
                "whole number",
            ),
            (lambda v: param_of(v, "paradigm.shuffle").update(default="yes"), "true or false"),
            (
                lambda v: param_of(v, "paradigm.n_per_condition").update(
                    constraints={"min": 5, "max": 1}
                ),
                "min is greater than max",
            ),
            (
                lambda v: param_of(v, "paradigm.n_per_condition").update(constraints={"max": 1}),
                "above the documented maximum",
            ),
            (
                lambda v: param_of(v, "paradigm.kind").update(constraints={"choices": ["fixed"]}),
                "not one of the documented choices",
            ),
            (
                lambda v: param_of(v, "paradigm.kind").update(constraints={"choices": [{"a": 1}]}),
                "must be text, a number",
            ),
            (lambda v: param_of(v, "paradigm.kind").pop("constraints"), "choices"),
            (
                lambda v: param_of(v, "paradigm.kind").update(constraints={"note": "open"}),
                "choices is required",
            ),
            (
                lambda v: add_param(v, free("blob", "object", default={"k": "x" * 5000})),
                "too large to document",
            ),
            (
                lambda v: (
                    add_param(v, free("wait", "duration")),
                    task_of(v)["timeline"]["phases"][1].update(
                        timing={"kind": "parameter", "param": "wait"}
                    ),
                ),
                "has no default to time",
            ),
            (
                lambda v: task_of(v)["timeline"]["phases"][1].update(
                    timing={"kind": "fixed", "ms": -1}
                ),
                "ms must be >= 0",
            ),
            (
                lambda v: task_of(v)["timeline"]["phases"][0]["timing"].update(max={"ms": -5}),
                "max.ms must be >= 0",
            ),
            (lambda v: task_of(v)["timeline"].update(phases=[]), "at least one phase"),
            (lambda v: task_of(v)["timeline"]["tracks"][0].update(role="glow"), "role must be"),
            (lambda v: task_of(v)["timeline"]["branches"][0].update(**{"from": "x"}), "'*'"),
            (lambda v: task_of(v)["timeline"]["branches"][0].update(kind="win"), "kind must be"),
            (lambda v: task_of(v)["diagram"].update(unit="<b>"), "unit must be short text"),
            (lambda v: task_of(v)["diagram"].update(width=500), "at most 200"),
            (lambda v: task_of(v)["diagram"].update(background=2), "between 0 and 1"),
            (lambda v: task_of(v)["diagram"].update(elements=[]), "at least one shape"),
            (lambda v: task_of(v)["diagram"]["elements"][0].update(role="glow"), "role must be"),
        ],
    )
    def test_refused(self, change, match):
        refused(edit(change), match)

    @pytest.mark.parametrize(
        ("element", "match"),
        [
            ({"type": "grating", "cx": 0, "cy": 0, "radius": 1, "cycles": 41}, "at most 40"),
            (
                {"type": "grating", "cx": 0, "cy": 0, "radius": 1, "cycles": 4, "contrast": 2},
                "between 0 and 1",
            ),
            (
                {
                    "type": "dot_field",
                    "cx": 0,
                    "cy": 0,
                    "radius": 1,
                    "dot_radius": 0.1,
                    "count": 5,
                    "seed": -1,
                },
                "seed must be",
            ),
        ],
    )
    def test_refused_shapes(self, element, match):
        refused(edit(lambda v: task_of(v)["diagram"]["elements"].append(element)), match)

    @pytest.mark.parametrize(
        "raw", [b"fix_size_dva: [1\n", b"fix_size_dva: .nan\n", b"a: {b: [1, .inf]}\n"]
    )
    def test_a_bad_params_file_is_refused(self, raw):
        with pytest.raises(DocumentationError, match="params|parameters file|not finite"):
            resolve(with_descriptor(descriptor(), extra={"configs/task.yaml": raw}))


class TestMoreResolution:
    def test_every_shape_resolves(self):
        shapes = [
            {"type": "ellipse", "cx": 1, "cy": 1, "rx": 1, "ry": 0.5, "rotation": 30},
            {"type": "rect", "cx": 0, "cy": 0, "width": 2, "height": 1},
            {"type": "grating", "cx": 0, "cy": 0, "radius": 1, "cycles": 4, "orientation": 45},
            {"type": "dot_field", "cx": 0, "cy": 0, "radius": 1, "dot_radius": 0.1, "count": 5},
            {"type": "text", "x": 0, "y": 2, "text": "Label"},
            {"type": "screen", "cx": 0, "cy": 0, "width": 20, "height": 12},
            {"type": "arrow", "x1": 0, "y1": 0, "x2": 1, "y2": 1},
            {
                "type": "dimension",
                "x1": 0,
                "y1": 0,
                "x2": 2,
                "y2": 0,
                "text": "window",
                "value": {"param": "fix_window_dva"},
            },
        ]
        value = edit(lambda v: task_of(v)["diagram"]["elements"].extend(shapes))
        drawn = the_task(resolve(with_descriptor(value)))["diagram"]["elements"][-len(shapes) :]
        ellipse, rect, grating, dots, text, screen, arrow, dimension = drawn
        assert (ellipse["rotation"], rect["rotation"]) == (30.0, 0.0)
        assert (grating["orientation"], grating["contrast"]) == (45.0, 1.0)
        assert (dots["count"], dots["seed"]) == (5, 1)
        assert text["text"] == "Label" and screen["role"] == "apparatus"
        assert arrow["role"] == "annotation"
        assert dimension["value_text"] == "2 dva"
        assert dimension["refs"]["value"] == {"param": "fix_window_dva", "factor": 1.0}

    def test_a_number_in_seconds_times_a_phase(self):
        def change(v):
            add_param(v, free("wait_s", "number", unit="s", default=0.25))
            task_of(v)["timeline"]["phases"][1].update(
                timing={"kind": "parameter", "param": "wait_s"}
            )
            task_of(v)["timeline"]["phases"][0]["timing"].update(max={"ms": 1500})

        acquire, hold = the_task(resolve(with_descriptor(edit(change))))["timeline"]["phases"]
        assert (hold["timing"]["ms"], hold["timing"]["text"]) == (250.0, "0.25 s")
        assert acquire["timing"]["text"] == "until gaze is inside the window, at most 1500 ms"

    def test_a_json_params_file_and_an_explicit_key(self):
        params = json.dumps({"size": 0.3, "fix_window_dva": 2.0}).encode()

        def change(v):
            task_of(v).update(parameters_file="configs/task.json")
            task_of(v)["parameters"] = [
                {
                    "name": "fix_size_dva",
                    "type": "number",
                    "unit": "dva",
                    "meaning": "Fixation point size.",
                    "source": {"key": "size"},
                },
                param_of(v, "fix_window_dva"),
            ]
            task_of(v).pop("timeline")
            task_of(v).pop("diagram")

        plain = {"docs/methods.md": b"Methods.", "docs/tasks/fixation-demo.md": b"The task."}
        files = with_descriptor(edit(change), extra={"configs/task.json": params, **plain})
        (size, window) = the_task(resolve(files))["parameters"]
        assert (size["default"], size["source"]["key"], size["source"]["status"]) == (
            0.3,
            "size",
            "read",
        )
        assert window["source"]["status"] == "matched"

    def test_value_formatting_of_structures(self):
        assert format_value({"a": 1, "b": "x"}, None) == "a: 1, b: x"
        assert format_value({"frames": 3}, None) == "3 frames"
        assert format_value(1.23456789, "s") == "1.23457 s"
        assert format_value((1, 2), None) == "(1, 2)"

    def test_source_files_membership(self):
        files = SourceFiles({"a.md": "x"})
        assert files.has("a.md") and not files.has("b.md")
