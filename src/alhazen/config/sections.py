"""Which section of the Run page a task parameter is shown in.

The experiment workspace's parameter editor groups a task's parameters under
three headings, in this order: **Stimulus** (what is drawn: sizes, positions,
colours, levels), **Timing** (how long each part of a trial lasts) and
**Session** (how many trials, blocks and repeats, the order they come in, who
the session is for and what pays), then **Other** for anything that is none
of these (a gaze window, a response key, a simulated subject's model).
Nothing is ever hidden.

A params model says where a field belongs in its JSON schema, under
``x-alhazen-section``::

    from pydantic import Field
    from alhazen import Model, section

    class MyParams(Model):
        contrast: float = Field(0.5, json_schema_extra=section("stimulus"))

A nested model can say it once for all its fields, in its ``model_config``
(alhazen's own ``Duration`` does: every duration is Timing)::

    class Fixation(Model):
        model_config = ConfigDict(json_schema_extra=section("session"))

The dashboard takes, for each row, the field's own declaration, else the
nearest declared enclosing field or model, else infers it from the field's
name and schema (the rules are in workspace_parameters.js and
docs/workspace.md), else Other. So a model with no declaration at all still
renders, every row inferred; declaring is how an experiment overrides an
inference it disagrees with, ``section("other")`` included (a reaction-time
bound in ms that is a response criterion, not a trial's timing).

The secret this hides: the schema key and the closed list of names. A typo
in a section name fails when the model's module is imported, not as a row
that quietly lands under Other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pydantic.config import JsonDict

# The sections, in the order the Run page shows them. "other" is also where
# a field goes that nothing declares and no inference rule places.
# workspace_parameters.js lists the same names (tests/unit/test_param_sections.py
# holds the two together).
SECTIONS: Final = ("stimulus", "timing", "session", "other")

# The JSON-schema key a declaration is written under.
SECTION_KEY: Final = "x-alhazen-section"


def section(name: str) -> JsonDict:
    """``{"x-alhazen-section": name}``, for a field's or a model's
    ``json_schema_extra``. Raises ValueError for a name that is not one of
    ``SECTIONS``, naming the ones that are."""
    if name not in SECTIONS:
        raise ValueError(f"unknown parameter section {name!r}: use one of {', '.join(SECTIONS)}")
    return {SECTION_KEY: name}


__all__ = ["SECTIONS", "SECTION_KEY", "section"]
