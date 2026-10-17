"""alhazen.experiment_platform.guides: reader documents bound to one source version.

The fixture is a neutral fake experiment written here: two tasks, one params file each,
and methods with parameter references. No experiment code is run.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from alhazen.experiment_platform.documentation import (
    MAX_DESCRIPTOR_BYTES,
    DocumentationError,
    check_references,
    format_value,
    parse_json,
    quantity,
    reserved_outcomes,
    resolve_parameter_reference,
    resolve_task,
    same_value,
)
from alhazen.experiment_platform.guides import (
    GUIDE_NOTE,
    UNDOCUMENTED_MEANING,
    build_guide,
    overlay_applies,
    parse_overlay,
    task_key,
)

BUNDLE = "fake-exp-0123456789"
LISTING: dict[str, Any] = {
    "id": "fake-exp",
    "title": "Fake experiment",
    "summary": "A listing summary.",
    "version": "1.2.0",
    "source": {"kind": "repo", "commit": "a" * 40},
    "bundle": {"id": BUNDLE},
}
TASK_YAML = "contrast: 0.5\niti: {ms: 400}\nlabel: easy\n"
HARD_YAML = "contrast: 0.25\niti: {ms: 400}\nlabel: hard\n"


def task(task_id: str = "detect", file: str = "configs/task.yaml", **extra: Any) -> dict:
    record = {
        "id": task_id,
        "title": f"Detect ({file})",
        "parameters_file": file,
        "parameters": [
            {"name": "contrast", "type": "number", "meaning": "Patch contrast."},
            {"name": "iti", "type": "duration", "meaning": "Inter-trial interval."},
            {"name": "label", "type": "string", "meaning": "Set label."},
        ],
        "outcomes": [{"name": "CORRECT", "completed": True, "success": True, "meaning": "Hit."}],
        "events": [{"name": "STIM_ON", "meaning": "The patch appears."}],
    }
    record.update(extra)
    return record


def files(**extra: str) -> dict[str, str]:
    base = {"configs/task.yaml": TASK_YAML, "configs/hard.yaml": HARD_YAML}
    base.update(extra)
    return base


def docs(methods: str = "Contrast is [[param:contrast]].", **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"methods": methods, "references": json.dumps(["A ref."])}
    record.update(extra)
    return record


DIAGRAM = {
    "caption": "The patch.",
    "width": 10,
    "height": 10,
    "elements": [
        {"type": "circle", "cx": 0, "cy": 0, "r": 1, "label": "Patch"},
        {"type": "text", "x": 0, "y": 3, "text": "A"},
    ],
}
TIMELINE = {
    "phases": [
        {
            "id": "stim",
            "label": "Patch",
            "timing": {"kind": "fixed", "ms": 200},
            "start_events": ["STIM_ON"],
        },
    ],
    "end": {"outcome": "CORRECT"},
}


def overlay(bundle: str | None = BUNDLE, **task_extra: Any) -> dict[str, Any]:
    record = {"taskKey": "detect@configs/task.yaml", "diagram": copy.deepcopy(DIAGRAM)}
    record.update(task_extra)
    result: dict[str, Any] = {"tasks": [record]}
    if bundle is not None:
        result["bundleId"] = bundle
    return result


def build(descriptor: dict | None = None, **kw: Any) -> dict[str, Any]:
    kw.setdefault("docs", docs())
    kw.setdefault("files", files())
    return build_guide(
        LISTING, kw.pop("docs"), kw.pop("files"), descriptor=descriptor or {"tasks": [task()]}, **kw
    )


class TestDocument:
    def test_the_schema_the_reader_consumes(self):
        guide = build()
        assert set(guide) == {
            "schema_version",
            "bundleId",
            "title",
            "summary",
            "source",
            "methods",
            "tasks",
            "references",
            "warnings",
            "referenceResolutions",
        }
        assert guide["schema_version"] == 1 and guide["bundleId"] == BUNDLE
        assert guide["source"] == {
            "package": "fake-exp",
            "version": "1.2.0",
            "descriptor": "docs/experiment.json",
            "commit": "a" * 40,
            "note": GUIDE_NOTE,
        }
        assert guide["references"] == ["A ref."] and guide["warnings"] == []
        (resolved,) = guide["tasks"]
        assert resolved["taskKey"] == "detect@configs/task.yaml"
        assert resolved["sourceTaskId"] == "detect"
        assert {p["name"]: p["default"] for p in resolved["parameters"]} == {
            "contrast": 0.5,
            "iti": {"ms": 400},
            "label": "easy",
        }

    def test_a_collection_folder_names_its_repository_commit(self):
        listing = copy.deepcopy(LISTING)
        listing["source"].update(repoCommit="b" * 40, subdir="classics/fake")
        listing["methodSummary"] = "Method summary."
        guide = build_guide(listing, docs(), files(), descriptor={"tasks": [task()]})
        assert guide["source"]["commit"] == "b" * 40
        assert guide["summary"] == "Method summary."

    def test_the_methods_fall_back_to_the_readme(self):
        guide = build(docs=docs(methods="", readme="Read me."))
        assert guide["methods"] == {"path": "docs/methods.md", "markdown": "Read me."}

    def test_the_descriptor_is_read_from_the_files_when_not_given(self):
        descriptor = json.dumps({"tasks": [task()]})
        guide = build_guide(LISTING, docs(), files(**{"docs/experiment.json": descriptor}))
        assert [t["id"] for t in guide["tasks"]] == ["detect"]

    def test_without_a_descriptor_the_records_tasks_are_used(self):
        record = docs(tasks=json.dumps([task()]))
        guide = build_guide(LISTING, record, files())
        assert [t["id"] for t in guide["tasks"]] == ["detect"]

    def test_an_oversized_descriptor_file_is_refused(self):
        big = json.dumps({"tasks": [task()], "pad": "x" * MAX_DESCRIPTOR_BYTES})
        with pytest.raises(DocumentationError, match="the limit is"):
            build_guide(LISTING, docs(), files(**{"docs/experiment.json": big}))

    @pytest.mark.parametrize(
        ("raw", "match"),
        [
            ('{"tasks": [], "tasks": []}', "repeats the key"),
            ('{"tasks": [], "x": NaN}', "not a finite number"),
            ('{"tasks": [], "x": -Infinity}', "not a finite number"),
        ],
    )
    def test_a_hostile_descriptor_file_is_refused(self, raw, match):
        with pytest.raises(DocumentationError, match=match):
            build_guide(LISTING, docs(), files(**{"docs/experiment.json": raw}))

    def test_a_non_finite_number_in_a_given_descriptor_is_refused(self):
        bad = task(diagram={**DIAGRAM, "background": float("nan")})
        with pytest.raises(DocumentationError, match="finite number"):
            build(descriptor={"tasks": [bad]})

    def test_a_default_that_disagrees_with_the_params_file_is_refused(self):
        bad = task()
        bad["parameters"][0]["default"] = 0.9
        with pytest.raises(DocumentationError, match="differs from 'configs/task.yaml'"):
            build(descriptor={"tasks": [bad]})

    def test_a_task_without_an_id_is_refused(self):
        with pytest.raises(DocumentationError, match="with an id"):
            build(descriptor={"tasks": [{"title": "x"}]})

    def test_a_broken_method_reference_is_refused(self):
        with pytest.raises(DocumentationError, match="names no single documented"):
            build(docs=docs(methods="See [[param:nothing]]."))


class TestOverlayBinding:
    def test_an_overlay_for_this_bundle_is_applied(self):
        guide = build(overlay=overlay(notes=["Drawn from configs/task.yaml."]))
        (resolved,) = guide["tasks"]
        assert resolved["diagram"]["caption"] == "The patch."
        assert resolved["guideNotes"] == ["Drawn from configs/task.yaml."]
        assert resolved["guideSources"] == []
        assert guide["warnings"] == []

    def test_a_stale_overlay_is_ignored_with_a_warning(self):
        guide = build(overlay=overlay(bundle="fake-exp-ffffffffff"))
        (resolved,) = guide["tasks"]
        assert resolved["diagram"] is None and "guideNotes" not in resolved
        (warning,) = guide["warnings"]
        assert "fake-exp-ffffffffff" in warning and BUNDLE in warning

    def test_an_overlay_bound_to_no_version_is_ignored(self):
        guide = build(overlay=overlay(bundle=None))
        assert guide["tasks"][0]["diagram"] is None
        assert "names no bundleId" in guide["warnings"][0]

    def test_the_stored_json_form_is_accepted(self):
        stored = {"bundleId": BUNDLE, "json": json.dumps({"tasks": overlay()["tasks"]})}
        assert build(overlay=stored)["tasks"][0]["diagram"] is not None

    @pytest.mark.parametrize(
        ("text", "match"),
        [
            ('{"tasks": [], "tasks": []}', "repeats the key"),
            ('{"tasks": [{"taskKey": "a", "notes": [NaN]}]}', "not a finite number"),
            ("[1]", "must be an object"),
            ('{"tasks": [{"taskKey": "a", "svg": "<svg/>"}]}', "unknown field"),
            ('{"tasks": [{"diagram": {}}]}', "taskKey is required"),
            ('{"tasks": [{"taskKey": "a", "notes": "text"}]}', "list of at most"),
            ('{"tasks": [{"taskKey": "a", "sourceFiles": [1]}]}', "entries must be text"),
            ('{"tasks": [{"taskKey": "a", "description": 3}]}', "Markdown text"),
            ('{"tasks": {"a": 1}}', "tasks must be a list"),
        ],
    )
    def test_a_malformed_overlay_is_refused(self, text, match):
        with pytest.raises(DocumentationError, match=match):
            parse_overlay({"bundleId": BUNDLE, "json": text})

    def test_an_oversized_overlay_is_refused(self):
        text = json.dumps({"tasks": [], "pad": "x" * MAX_DESCRIPTOR_BYTES})
        with pytest.raises(DocumentationError, match="the limit is"):
            parse_overlay({"bundleId": BUNDLE, "json": text})

    def test_overlay_shapes_that_are_not_objects_are_refused(self):
        with pytest.raises(DocumentationError, match="must be an object"):
            parse_overlay(["x"])
        with pytest.raises(DocumentationError, match="JSON text"):
            parse_overlay({"json": 3})
        assert parse_overlay(None) is None

    def test_an_overlay_task_that_matches_nothing_is_reported(self):
        stale = overlay()
        stale["tasks"].append({"taskKey": "removed@configs/old.yaml", "diagram": DIAGRAM})
        guide = build(overlay=stale)
        assert guide["warnings"] == [
            "guide overlay task 'removed@configs/old.yaml' matches no documented task; not shown"
        ]

    def test_an_overlay_drawing_is_validated_like_any_other(self):
        bad = overlay()
        bad["tasks"][0]["diagram"]["elements"][0]["type"] = "svg"
        with pytest.raises(DocumentationError, match="type must be one of"):
            build(overlay=bad)

    def test_an_overlay_timeline_and_description_are_used(self):
        guide = build(overlay=overlay(timeline=TIMELINE, description="Watch the patch."))
        (resolved,) = guide["tasks"]
        assert resolved["timeline"]["phases"][0]["timing"]["ms"] == 200
        assert resolved["description"]["markdown"] == "Watch the patch."
        assert resolved["description"]["path"].startswith("docs/mana-guides/detect-")

    def test_overlay_applies_only_to_the_exact_bundle(self):
        assert overlay_applies({"bundleId": BUNDLE}, BUNDLE)
        assert not overlay_applies({"bundleId": BUNDLE}, BUNDLE + "x")
        assert not overlay_applies({}, BUNDLE)
        assert not overlay_applies(None, BUNDLE)


class TestLegacyShapes:
    def test_a_citation_string_source_is_kept_as_a_citation(self):
        legacy = task()
        legacy["parameters"][0]["source"] = "paper, p. 3"
        (resolved,) = build(descriptor={"tasks": [legacy]})["tasks"]
        contrast = resolved["parameters"][0]
        assert contrast["source"]["citation"] == "paper, p. 3"
        assert contrast["source"]["status"] == "read"

    def test_a_null_default_is_kept_and_marked_nullable(self):
        legacy = task()
        legacy["parameters"].append(
            {"name": "seed", "type": "integer", "default": None, "meaning": "Seed."}
        )
        legacy["parameters_file"] = None
        (resolved,) = build(descriptor={"tasks": [legacy]})["tasks"]
        seed = resolved["parameters"][-1]
        assert (seed["type"], seed["default"], seed["has_default"]) == ("integer", None, True)
        assert seed["nullable"] is True

    def test_a_choice_without_choices_keeps_its_type_and_says_so(self):
        legacy = task()
        legacy["parameters"][2]["type"] = "choice"
        (resolved,) = build(descriptor={"tasks": [legacy]})["tasks"]
        label = resolved["parameters"][2]
        assert (label["type"], label["default"]) == ("choice", "easy")
        assert "not enumerated" in label["constraints"]["note"]

    def test_a_missing_meaning_is_labelled_not_invented(self):
        legacy = task()
        del legacy["parameters"][0]["meaning"]
        (resolved,) = build(descriptor={"tasks": [legacy]})["tasks"]
        assert resolved["parameters"][0]["meaning"] == UNDOCUMENTED_MEANING

    def test_an_inline_description_becomes_a_markdown_file(self):
        (resolved,) = build(descriptor={"tasks": [task(description="Press a key.\nThen wait.")]})[
            "tasks"
        ]
        assert resolved["description"]["markdown"] == "Press a key.\nThen wait."
        assert resolved["description"]["path"].startswith("docs/mana-guides/inline-")

    def test_a_missing_description_file_is_refused(self):
        with pytest.raises(DocumentationError, match="not in this source version"):
            build(descriptor={"tasks": [task(description="docs/tasks/absent.md")]})

    @pytest.mark.parametrize(
        ("timeline", "text"),
        [
            ("fixation 0.5 s | patch 0.2 s", "fixation 0.5 s | patch 0.2 s"),
            (["fixation 0.5 s", "patch until a key"], "fixation 0.5 s | patch until a key"),
        ],
    )
    def test_a_text_timeline_is_kept_as_text_and_not_drawn(self, timeline, text):
        guide = build(descriptor={"tasks": [task(timeline=timeline)]})
        (resolved,) = guide["tasks"]
        assert resolved["timeline"] is None
        assert resolved["timelineText"] == text
        assert "no timeline is drawn" in guide["warnings"][0]

    def test_an_overlay_timeline_replaces_a_text_timeline_silently(self):
        guide = build(
            descriptor={"tasks": [task(timeline="fixation 0.5 s")]},
            overlay=overlay(timeline=TIMELINE),
        )
        assert guide["warnings"] == [] and "timelineText" not in guide["tasks"][0]

    @pytest.mark.parametrize("timeline", ["  ", "a\x01b", "x" * 1201])
    def test_a_malformed_text_timeline_is_refused(self, timeline):
        with pytest.raises(DocumentationError, match="timeline"):
            build(descriptor={"tasks": [task(timeline=timeline)]})

    def test_unknown_replication_fields_are_not_passed_to_the_reader(self):
        (resolved,) = build(descriptor={"tasks": [task(assumed=[{"item": "x"}])]})["tasks"]
        assert "assumed" not in resolved

    def test_tasks_sharing_an_id_get_distinct_ids_and_references_follow(self):
        easy = task(description="docs/tasks/hard.md")
        hard = task(file="configs/hard.yaml", description="docs/tasks/hard.md")
        markdown = "Hard contrast: [[param:detect/contrast]]."
        guide = build(
            descriptor={"tasks": [easy, hard]},
            files=files(**{"docs/tasks/hard.md": markdown}),
            docs=docs(methods="Contrast: [[param:contrast]]."),
        )
        first, second = guide["tasks"]
        assert first["id"] == "detect" and second["id"].startswith("detect-")
        assert second["sourceTaskId"] == "detect"
        assert (
            second["description"]["markdown"]
            == f"Hard contrast: [[param:{second['id']}/contrast]]."
        )
        # 0.5 and 0.25 differ: the methods name both owners rather than pick one.
        assert guide["methods"]["markdown"] == (
            f"Contrast: Detect (configs/task.yaml): [[param:detect/contrast]]; "
            f"Detect (configs/hard.yaml): [[param:{second['id']}/contrast]]."
        )
        assert guide["referenceResolutions"] == [
            {"parameter": "contrast", "tasks": ["detect", second["id"]], "sameValue": False}
        ]

    def test_an_equal_shared_value_resolves_to_the_first_owner(self):
        guide = build(
            descriptor={"tasks": [task(), task(file="configs/hard.yaml")]},
            docs=docs(methods="ITI: [[param:iti]] and [[param:detect/iti]]."),
        )
        assert guide["methods"]["markdown"] == "ITI: [[param:detect/iti]] and [[param:detect/iti]]."
        assert [r["sameValue"] for r in guide["referenceResolutions"]] == [True]

    def test_false_and_zero_do_not_share_a_reference(self):
        tasks, store = [], {}
        for name, kind, value, scalar in [
            ("yesno", "boolean", False, "false"),
            ("number", "integer", 0, "0"),
        ]:
            store[name + ".yaml"] = "value: " + scalar
            tasks.append(
                {
                    "id": name,
                    "title": name,
                    "parameters_file": name + ".yaml",
                    "parameters": [
                        {"name": "value", "type": kind, "default": value, "meaning": "Value."}
                    ],
                }
            )
        guide = build_guide(
            LISTING, docs(methods="Value: [[param:value]]"), store, descriptor={"tasks": tasks}
        )
        assert guide["referenceResolutions"][0]["sameValue"] is False
        assert "[[param:yesno/value]]" in guide["methods"]["markdown"]
        assert "[[param:number/value]]" in guide["methods"]["markdown"]


class TestDrawingExtensions:
    def drawn(self, **element_extra: Any) -> dict[str, Any]:
        extended = overlay()
        extended["tasks"][0]["diagram"]["elements"][1].update(element_extra)
        return build(overlay=extended)["tasks"][0]["diagram"]["elements"][1]

    def test_bounded_extensions_are_added_to_the_validated_geometry(self):
        element = self.drawn(
            color="#aa0000",
            font_size={"param": "contrast", "factor": 2},
            font_family="Arial",
            font_weight=700,
            stroke_width=0.1,
        )
        assert element["color"] == "#aa0000"
        assert element["font_size"] == 1.0
        assert element["refs"]["font_size"] == {"param": "contrast", "factor": 2.0}
        assert (element["font_family"], element["font_weight"]) == ("Arial", 700)
        assert element["stroke_width"] == 0.1

    @pytest.mark.parametrize(
        ("extra", "match"),
        [
            ({"color": "url(https://example.org/a)"}, "six-digit hex"),
            ({"color": "#abc"}, "six-digit hex"),
            ({"stroke_width": 201}, "outside the drawable range"),
            ({"stroke_width": -1}, "greater than zero"),
            ({"font_weight": True}, "font_weight"),
            ({"font_weight": "heavy"}, "font_weight"),
            ({"font_family": "Comic <script>"}, "font_family"),
            ({"font_size": {"param": "label"}}, "must be a number with a default"),
        ],
    )
    def test_unsafe_extensions_are_refused(self, extra, match):
        with pytest.raises(DocumentationError, match=match):
            self.drawn(**extra)

    def test_font_size_applies_only_to_text(self):
        extended = overlay()
        extended["tasks"][0]["diagram"]["elements"][0]["font_size"] = 1
        with pytest.raises(DocumentationError, match="only applies to text"):
            build(overlay=extended)


class TestPublicResolverApi:
    def test_task_key(self):
        assert task_key({"id": "a", "parameters_file": "configs/x.yaml"}) == "a@configs/x.yaml"
        assert task_key({"id": "a", "parameters_file": None}) == "a"

    def test_resolve_task_and_references(self):
        resolved = resolve_task(task(), files(), {"name": "fake-exp", "version": "1"})
        assert resolve_parameter_reference("contrast", resolved, [resolved]) == (
            "detect",
            "contrast",
        )
        assert resolve_parameter_reference("detect/iti", None, [resolved]) == ("detect", "iti")
        assert resolve_parameter_reference("other/iti", None, [resolved]) is None
        markdown = {"path": "m.md", "sha256": "0" * 64, "markdown": "[[task:missing]]"}
        with pytest.raises(DocumentationError, match="unknown task"):
            check_references(markdown, None, [resolved], "methods")
        check_references(None, None, [resolved], "methods")

    def test_values(self):
        assert same_value(2, 2.0) and not same_value(True, 1) and not same_value("2", 2)
        assert same_value({"a": [1, 2]}, {"a": [1.0, 2]})
        assert format_value({"ms": 500}, None) == "500 ms"
        assert format_value(0.3, "dva") == "0.3 dva"
        assert format_value(None, None) == "none"
        assert format_value([True, "x"], None) == "[true, x]"

    def test_quantity(self):
        parameters = {
            p["name"]: p
            for p in resolve_task(task(), files(), {"name": "f", "version": "1"})["parameters"]
        }
        assert quantity(3, "x", parameters) == (3.0, None)
        assert quantity({"param": "contrast", "factor": 4}, "x", parameters) == (
            2.0,
            {"param": "contrast", "factor": 4.0},
        )
        with pytest.raises(DocumentationError, match="not a documented parameter"):
            quantity({"param": "nope"}, "x", parameters)
        with pytest.raises(DocumentationError, match="drawable range"):
            quantity(1e5, "x", parameters)

    def test_reserved_outcomes_are_the_engines(self):
        assert reserved_outcomes() == {"ABORTED", "PAUSED", "DROPPED_FRAMES"}

    def test_parse_json_bounds(self):
        assert parse_json('{"a": 1}', "x.json") == {"a": 1}
        with pytest.raises(DocumentationError, match="the limit is 4"):
            parse_json(b"[1, 2]", "x.json", limit=4)
