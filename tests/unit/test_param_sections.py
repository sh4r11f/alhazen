"""Parameter sections: the declaration helper, and alhazen's own models' sections.

The Run page groups a task's parameters under Stimulus, Timing, Session and
Other (alhazen.config.sections). These hold the helper's contract (the key,
the closed list, the refusal of a typo) and that a declaration reaches the
JSON schema the workspace reads, on a field, on a nested model, and on
alhazen's own Duration, SchedulerConfig, RewardPolicy and SubjectParams.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ConfigDict, Field

from alhazen import Duration, Model, SchedulerConfig, SubjectParams, section
from alhazen.config.sections import SECTION_KEY, SECTIONS

ASSETS = Path(__file__).resolve().parents[2] / "src" / "alhazen" / "cli" / "assets"


def _def(schema: dict, name: str) -> dict:
    return schema["$defs"][name]


def test_section_is_the_schema_key_and_name():
    assert section("timing") == {"x-alhazen-section": "timing"}
    assert SECTION_KEY == "x-alhazen-section"
    assert SECTIONS == ("stimulus", "timing", "session", "other")
    assert section("other") == {SECTION_KEY: "other"}


@pytest.mark.parametrize("name", ["Timing", "trial", "Other", "", "response"])
def test_a_name_outside_the_list_is_refused_naming_the_list(name):
    with pytest.raises(ValueError, match="stimulus, timing, session, other"):
        section(name)


def test_a_typo_fails_when_the_model_is_defined():
    with pytest.raises(ValueError, match="unknown parameter section 'stimulis'"):

        class _Typo(Model):
            size_dva: float = Field(1.0, json_schema_extra=section("stimulis"))


def test_a_field_declaration_lands_in_the_field_schema():
    class P(Model):
        size_dva: float = Field(1.0, json_schema_extra=section("stimulus"))
        n_dots: int = 10

    props = P.model_json_schema()["properties"]
    assert props["size_dva"][SECTION_KEY] == "stimulus"
    assert SECTION_KEY not in props["n_dots"]


def test_a_nested_model_declares_for_all_its_fields():
    class Window(Model):
        model_config = ConfigDict(json_schema_extra=section("session"))
        radius_dva: float = 2.0

    class P(Model):
        window: Window = Window()

    schema = P.model_json_schema()
    assert _def(schema, "Window")[SECTION_KEY] == "session"
    # The model's own config (extra=forbid, frozen) is kept beside it.
    with pytest.raises(ValueError):
        Window(radius_dva=1.0, typo=1)


def test_a_field_declaration_beats_its_models():
    class P(Model):
        wait: Duration = Field(Duration(ms=100), json_schema_extra=section("session"))

    prop = P.model_json_schema()["properties"]["wait"]
    assert prop[SECTION_KEY] == "session"


def test_alhazens_own_models_declare_their_sections():
    class P(SubjectParams):
        iti: Duration = Duration(ms=500)
        paradigm: SchedulerConfig = SchedulerConfig()

    schema = P.model_json_schema()
    assert _def(schema, "Duration")[SECTION_KEY] == "timing"
    assert _def(schema, "SchedulerConfig")[SECTION_KEY] == "session"
    assert _def(schema, "RewardPolicy")[SECTION_KEY] == "session"
    assert schema["properties"]["subject_kind"][SECTION_KEY] == "session"


def test_duration_still_validates_as_before():
    assert Duration(ms=250).seconds(100.0) == 0.25
    with pytest.raises(ValueError):
        Duration(ms=1, frames=1)


def test_the_page_lists_the_same_sections_in_the_same_order():
    """workspace_parameters.js groups rows by these names; one list in two
    languages, held together here."""
    source = (ASSETS / "workspace_parameters.js").read_text(encoding="utf-8")
    match = re.search(r"const SECTIONS = \[([^\]]*)\]", source)
    assert match, "workspace_parameters.js no longer declares `const SECTIONS = [...]`"
    names = tuple(re.findall(r"'([a-z]+)'", match.group(1)))
    assert names == SECTIONS
