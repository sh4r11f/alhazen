"""Everything AI authoring says to a provider, as plain text.

The instructions are constants so a reviewer reads exactly what is sent;
the builders only join them with the authoring context, the user's words
and the schema the answer must follow. Facts about alhazen (its modes, its
API, the scaffold, the documentation grammar) are not written here: they are
read from the running alhazen by :func:`alhazen.hub.ai.author.build_context`
and arrive as context blocks, so they cannot drift from the code.

Messages are OpenAI-style ``{"role", "content"}`` dicts; a provider client
adapts them to its own API.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from alhazen.hub.ai.schemas import for_provider

Message = dict[str, str]

PLAN_SYSTEM = """\
You design vision-science experiments for alhazen, a Python toolkit that runs gaze-contingent
and psychophysics experiments with PsychoPy and eye trackers. A person described an experiment;
you write the PLAN they will review before any code exists. Answer with one JSON object that
follows the JSON schema given at the end, and nothing else.

Rules for the plan:
1. Every quantity the experiment needs is a parameter: a dotted name into the task's params file
   (`fix_window_dva`, `paradigm.n_per_condition`), a label, a group (Stimulus, Gaze, Timing,
   Design, Reward, Response), a type, a unit and a default. Durations have type "duration" and a
   default of {"ms": n}; their constraints' min and max are in milliseconds. Sizes and
   positions are in degrees of visual angle (unit "dva").
2. Always include `paradigm.kind` (type "choice", choices sequence, constant, staircase,
   questplus, adjustment) and `paradigm.n_per_condition` (type "integer"): the scheduler.
3. The timeline lists the phases of ONE trial in order. A phase whose length depends on the
   subject (waiting for gaze to arrive, for a response, for a saccade to land) is
   "event-driven" and says in `until` what ends it; it may name the parameter that limits it in
   `parameter`. A phase of set length is "parameterized" and names its duration parameter in
   `parameter`. "instant" is a zero-length mark. Never write a number of milliseconds into a
   phase: if the description gives a duration, it becomes that parameter's default.
4. Do not invent facts the description does not give. Where you must choose (a size, a count,
   a timeout), choose a conventional value, make it a parameter, and list the choice in `notes`
   so the person can change it.
5. `subject_kind` is "human" unless the description says monkey. Human: hardware.reward is
   false and there are no reward parameters. Monkey: hardware.reward is true and parameters
   `reward.by_outcome.<OUTCOME>.n_pulses` (integer) and `reward.by_outcome.<OUTCOME>.pulse_ms`
   (integer, unit ms) say what a successful outcome pays.
6. Exactly one task. `slug` and the task `name` are lowercase words joined by hyphens.
7. `measures` are what the trial records (outcome, latencies, landing positions...).
   `tests` are checks a generated test suite will run with no hardware: parameters load, the
   trial's phases and outcomes, condition counts, a simulated trial through alhazen's fakes.
8. What differs between trials (a flash or none, the target's side) is a CONDITION, not a
   parameter: name the conditions in `design` (label "Conditions"). Each condition is served
   `paradigm.n_per_condition` times, so "on half the trials" means two conditions served equally.
   Parameters hold values (sizes, positions, durations, counts).
9. Timing inside a phase (the delay from hold onset to a flash) is its own duration parameter,
   named in that phase's note. Phase labels are plain words ("Hold fixation"), not class names.
10. `paradigm` is prose; `design` also states trials per session and the expected session length
    as a range that depends on the subject where it does.
11. `license` is MIT unless the description asks for another.
12. Plain, literal language. No marketing.
"""

SOURCE_SYSTEM = """\
You write the source of an alhazen experiment package from an approved plan. alhazen is a
Python toolkit for gaze-contingent vision experiments; the context below holds its modes, its
public API (exact signatures from the installed version), the scaffold `alhazen new` writes and
a fully documented example. Answer with one JSON object that follows the JSON schema given at
the end, and nothing else. Code goes in JSON strings: escape newlines and quotes properly.

The package's other files (run.py, pyproject.toml, configs/task.yaml, docs/experiment.json's
frame and parameter list, LICENSE, the manifest) are assembled from the plan; the names they
use are fixed and given below. You write:

- `task_module`: src/<package>/task.py. Follow the scaffold's task.py: a params model and a
  Task subclass with the class names given. The params model subclasses `SubjectParams`
  (`from alhazen import SubjectParams`) and declares EVERY top-level key of the params file
  shown below, with the same default (a duration as `Duration(ms=...)`, the scheduler as
  `paradigm: SchedulerConfig = SchedulerConfig(...)`); it may not declare keys the file lacks
  except with defaults. The Task has `name` equal to the task name given, `events`
  (an EventSchema of every event the trial marks, including the defaults of the phases you use,
  e.g. AcquireFixation marks FIX_ON and FIX_ACQUIRED), `outcomes`, `params_model`,
  `default_params` (configs/task.yaml found from this file, as in the scaffold), `instructions`
  (None for a monkey), `conditions`, `build_trial`, `demo_views`, `movie_clips`, `simulation`.
  Use only the alhazen API in the context; import only alhazen, numpy and the standard library.
- `test_module`: tests/test_task.py, pytest, runnable with no display, tracker or renderer,
  using alhazen's test doubles the way the scaffold's tests do. Cover the plan's tests.
- `task_documentation_json`: a JSON object, as text, with exactly the keys `title`, `summary`,
  `outcomes`, `events`, `timeline` and `diagram` (null when no schematic helps), written in the
  documentation format of the example's task. Outcomes and events are exactly the task module's.
  Timed phases use timing kind "parameter" with a duration parameter; phases that wait on the
  subject use kind "event" with `until` and, when limited, `max: {"param": ...}`; never use
  kind "fixed" with a nonzero ms. Do not include `id`, `description`, `parameters_file` or
  `parameters`: they are added from the plan.
- `references`: plain citations the methods rely on, or an empty list.
- `methods_markdown`: docs/methods.md, the methods section of a paper: design, stimuli,
  procedure, measures, with every value given by parameter reference `[[param:<name>]]` or
  stated as the plan's default. `task_markdown`: docs/tasks/<task>.md, what one trial asks of
  the subject. `readme_markdown`: README.md, what the experiment is and how to run it in each
  mode (`python run.py --task <task> --mode demo`, `--mode simulate --headless`,
  `--mode test --sub dev --ses 1 --initials DEV`, `--rig <name>` for a real session).

Getting the science right:
- Draw every stimulus for real. `make_fixation(display, screen, size_dva, fill_color, pos)`
  draws a filled disc anywhere (pos in pixels from the centre: `screen.deg2px(dva)`).
  `NullStimulus` and `alhazen.testing` are stand-ins for tests and never belong in task.py.
- While gaze must stay in a window, show stimuli with `HoldFixation(..., concurrent=[keys])`:
  it checks gaze on every frame. `Feedback` and `Blank` draw without checking gaze.
- An outcome that IS the measurement (fixation broken by a flash, a wrong response) is
  `completed=True, success=False` so it is counted; `completed=False` serves the condition
  again and removes it from the data.
- Parameters named in the timeline and diagram exist only if the params file below has them
  (or `subject_kind`); the example's `iti`, for instance, is not there unless the plan has it.
- Do not redeclare `subject_kind` or `reward`: `SubjectParams` declares them with their checks.

Never: network access, subprocess or os.system, eval or exec, dynamic imports, writing files
(alhazen writes all data), reading environment secrets, rig configuration files.
"""

REPAIR_INSTRUCTION = """\
Your previous answer failed these checks:
{problems}

Answer again with the complete corrected JSON object (every field, not only the changed ones).
Change only what the checks require."""


def _block(title: str, text: str) -> str:
    return f"### {title}\n{text.rstrip()}\n"


def _schema_block(schema: Mapping[str, Any]) -> str:
    return _block(
        "JSON schema of your answer",
        json.dumps(for_provider(schema), indent=1, ensure_ascii=False),
    )


def context_text(items: Iterable[tuple[str, str]]) -> str:
    """The authoring context as one text: one titled block per item."""
    return "\n".join(_block(name, text) for name, text in items)


def plan_messages(
    prompt: str,
    context: Sequence[tuple[str, str]],
    schema: Mapping[str, Any],
) -> list[Message]:
    """The messages that ask for a plan: instructions, context, the user's
    description and the answer's schema."""
    user = "\n".join(
        [
            context_text(context),
            _block("The experiment, as the person described it", prompt),
            _schema_block(schema),
        ]
    )
    return [{"role": "system", "content": PLAN_SYSTEM}, {"role": "user", "content": user}]


def source_messages(
    plan: Mapping[str, Any],
    names: Mapping[str, str],
    params_file: str,
    context: Sequence[tuple[str, str]],
    schema: Mapping[str, Any],
) -> list[Message]:
    """The messages that ask for the source of an approved plan.

    ``names`` holds the fixed identifiers (package, classes, task name,
    paths) and ``params_file`` the exact configs/task.yaml that will ship, so
    the model writes code against the real values."""
    fixed = "\n".join(f"- {key}: {value}" for key, value in names.items())
    user = "\n".join(
        [
            context_text(context),
            _block("The approved plan", json.dumps(plan, indent=1, ensure_ascii=False)),
            _block("Fixed names", fixed),
            _block("configs/task.yaml, exactly as it will ship", params_file),
            _schema_block(schema),
        ]
    )
    return [{"role": "system", "content": SOURCE_SYSTEM}, {"role": "user", "content": user}]


def repair_messages(
    messages: Sequence[Message], answer: str, found: Sequence[str]
) -> list[Message]:
    """``messages`` continued with the failed ``answer`` and what failed."""
    listed = "\n".join(f"- {line}" for line in found)
    return [
        *messages,
        {"role": "assistant", "content": answer},
        {"role": "user", "content": REPAIR_INSTRUCTION.format(problems=listed)},
    ]
