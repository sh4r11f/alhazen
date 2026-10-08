# How to extend alhazen

Each of these is a seam the framework was built around: something an
experiment will need that the core deliberately does not decide.

## Add a stimulus

Implement `update(dt)` and `draw()`. Convert degrees to pixels **once**, at
construction, through `Screen`, and import the renderer lazily so importing
your module stays safe on a machine with no display.

```python
class Bar:
    def __init__(self, display, screen, length_dva, pos):
        from psychopy import visual          # inside __init__, never at module top
        self._stim = visual.Rect(display.window, width=screen.deg2px(length_dva))

    def update(self, dt): ...                # advance any time-varying state
    def draw(self): self._stim.draw()
```

Give it a simulated twin through a factory, so the same task code runs
headless:

```python
def make_bar(display, screen, length_dva, pos):
    if display.kind == "simulated":
        return NullStimulus("bar")           # records draw counts; draws nothing
    return Bar(display, screen, length_dva, pos)
```

## Preview an experiment's stimuli

Declare every stimulus the experiment shows once, for all its tasks, by
naming the function that draws them in its `pyproject.toml`:

```toml
[tool.alhazen]
stimuli = "my_experiment.stimulus_set:stimulus_images"
```

The function takes the rig's `Screen` and returns one `StimulusImage` per
stimulus: its name, which becomes the file's name; the picture at the rig's
pixel scale, `(height, width)` luminance or `(height, width, 3)` RGB, as
floats in [0, 1] or uint8; and a one-line caption saying what to look for.

```python
import numpy as np

from alhazen.stimuli import StimulusImage


def stimulus_images(screen):
    size = int(round(screen.deg2px(2.0)))
    return [
        StimulusImage("square-grey", np.full((size, size), 0.5), "a grey square, 2 deg across"),
        StimulusImage("square-white", np.ones((size, size)), "the same square at full white"),
    ]
```

Then draw them all, in the experiment's folder:

```bash
alhazen preview --rig lab --out docs/stimulus-check
```

That writes one PNG per stimulus and an index, `README.md`, listing them
with their sizes and captions, at the scale of the rig named. It takes no
task and no parameter file: the stimuli are the experiment's, and a shorter
configuration runs fewer of them, not different ones. The workspace's
**Preview images** runs the same command
([Experiment workspace](workspace.md)).

Each stimulus is declared once. The command refuses the same picture under
two names, two names one filesystem would take for one file, and an output
folder that holds an image the declaration no longer has, so the folder
always shows exactly the declared set. Everything is drawn and checked
before anything is written.

A test worth keeping beside the declaration: draw every condition each of
the experiment's parameter files can run, and check that each one is among
`alhazen.stimuli.preview.declared_stimuli(root, screen)`. That is what makes
the set the experiment's own rather than one file's.

## Add a phase

An object with `name`, `on_enter(ctx)` and `on_frame(ctx) -> PhaseAction |
Outcome`. Take **plain values** in the constructor — seconds, region names,
stimulus keys, Outcomes — never config models: resolving a `Duration` against
the measured refresh rate is the task's job, done once in `build_trial`.

```python
class WaitForKey:
    name = "wait_for_key"

    def __init__(self, key: str, timeout_s: float, on_press, on_timeout):
        self._key, self._timeout_s = key, timeout_s
        self._on_press, self._on_timeout = on_press, on_timeout

    def on_enter(self, ctx):
        # Read right after the flip before this phase's first frame: the
        # moment the timeout starts.
        self._t0 = ctx.clock.now()

    def on_frame(self, ctx):
        if self._key in ctx.inputs.keys:
            return self._on_press
        # Asked before anything is drawn. On the frame the time runs out the
        # phase draws nothing and ends undrawn, so it is on screen for
        # timeout_s, to the nearest frame — not one frame longer.
        if ctx.time_up(self._t0, self._timeout_s):
            return ctx.end_undrawn(self._on_timeout)
        return PhaseAction.CONTINUE
```

Three rules that are not optional:

- **Touch nothing but `ctx`.** No hardware, no bus, no window, no module
  state. That is what lets every phase be tested against a fake clock.
- **Check gaze before checking completion.** If the phase requires fixation,
  test it first — otherwise a blink on the final frame passes as success.
- **Time a duration with `ctx.time_up`, before drawing, and end with
  `ctx.end_undrawn`.** The engine flips every frame a phase draws, so a
  phase that compares `ctx.clock.now() - self._t0` with its duration after
  drawing shows the frame its time ran out on as well: one frame too long.
  `end_undrawn(then)` returns `then` unchanged and tells the engine not to
  flip; the next phase draws that frame instead. Check what the phase
  measures (a key, gaze) first, so the inputs read on that frame still
  count. The rule, the rounding and what happens to a dropped frame are in
  [architecture §2.3](architecture.md#23-how-long-a-phase-lasts).

## Add a paradigm

Satisfy `TrialSource`: `next()`, `record(condition, result)`, `summary()`.

```python
class MyScheduler:
    def next(self):                          # None means the session is done
        ...
    def record(self, condition, result):
        if not result.outcome.completed:     # no measurement: serve it again
            self._queue.append(condition)
    def summary(self):                       # a DataFrame, or None
        ...
```

- Draw randomness **only** from the injected generator.
- Read `TrialResult.outcome`, never the record. A scheduler that reaches into
  measurements is how a scheduler and an analysis end up disagreeing about
  what "correct" meant.
- An adaptive scheduler asks a `score(result) -> bool` whether a *completed*
  trial was a success, defaulting to `outcome.success`, as the built-in
  staircases and QUEST+ do. Pass it the task's `score_trial` from
  `make_source`, or a task that titrates something else gets accuracy. The
  built-in ones refuse (`TypeError`) an answer that is not a `bool`.
- A queue-based scheduler may define `remaining()` (planned trials still
  queued); `BlockPlan` then refuses a `trials_per_block` that would cut it.
- A scheduler that leaves breaks between blocks (`take_block_break()`, as
  `BlockPlan` does) may carry `validate_after_break = True` to end each
  break with a validation of the eye tracker
  ([eye tracker](eye-tracker.md#validation-at-block-breaks)). A task that
  builds its own `BlockPlan` passes `breaks=` and `validate_after_break=` on
  from its params' `BlockConfig`: a session whose params ask for the
  validation and whose scheduler does not carry it is refused.
- `record()` is called for *every* outcome, including PAUSED and ABORTED.

## Add a device backend

Satisfy its protocol in `devices/`, import the vendor SDK **inside the method
that needs it**, and raise a typed alhazen error naming what to install:

```python
class MyTracker:
    def connect(self):
        try:
            import theirsdk
        except ImportError as e:
            raise TrackerError(
                "theirsdk is not installed — pip install alhazen-vision[theirs], or use "
                "backend 'mouse_sim' for development"
            ) from e
```

Then add it to that device's `make_*` factory — the one both `build_session`
and `check-rig` call, so a clean check exercises the real constructor — and
ship a `Simulated*` sibling in the same change.

## Add a training stage or metric

A curriculum is config, so a stage needs no code. A **metric** does:

```python
from alhazen.training import register_metric

register_metric("mean_saccade_error_dva", lambda window: ...)
```

The function receives the sliding window of recent trial summaries and
returns a number; name it in a stage's `promote_when` or `demote_when`. A
summary carries `outcome`, `completed`, `success`, `rt_ms` and `stage`; list
any other record field the metric reads in the curriculum's `record_fields`
(here, `record_fields: [saccade_error_dva]`). If the task's phases write the
RT under another name, set the curriculum's `rt_key` to it.

## Add a display backend

Satisfy `DisplayBackend`: `kind`, `window`, `open`, `close`, `flip`,
`measure_refresh_rate`, `show_message`, `set_gamma`. `flip()` blocks until
the buffer swap and returns nothing — the engine stamps the clock immediately
after, so there is exactly one clock and one stamping site.

`show_message(text, *, reflow=True)` takes `reflow` keyword-only, defaulting
to `True`: when it is set, pass the text through `alhazen.display.reflow`
before laying it out; with `reflow=False`, draw every line break as given.
A backend with no screen records the flag rather than dropping it. A backend
that predates the argument still works — alhazen never passes `reflow` to a
`show_message` that does not take it — but its messages are drawn unreflowed.

## Run a task for humans and for monkeys

The lab rig has a juice line whoever is in the chair, so who the subject is
is declared where a session is chosen: in its params file. Mix
`alhazen.SubjectParams` into the task's params model and give every file a
`subject_kind`:

```python
from alhazen import SubjectParams


class MyParams(SubjectParams):
    eccentricity_dva: float = 10.0
```

```yaml
# configs/task.yaml: a human session. The reward line is never opened.
subject_kind: human
```

```yaml
# configs/task-monkey.yaml: the same session for a monkey.
subject_kind: monkey
reward:
  by_outcome:
    CORRECT: {n_pulses: 2, pulse_ms: 200, inter_pulse_ms: 200}
  on_fault: {n_pulses: 1, pulse_ms: 200, inter_pulse_ms: 200}   # optional
```

The rig file says which line the juice goes out on (`devices.reward`:
`device`, `channel`, `voltage`); the params file says what pays and how much
(pulse width sets the volume per pulse). A human file with a `reward` block,
a monkey file without one, or a block naming an outcome the task does not
declare are refused when the session is built. A monkey session shows no
instruction screen and is refused in run mode on a rig with no reward line.
Name the Task parameters entries so the kind is visible (`"Main (human)"`,
`"Main (monkey)"`); session.json, session.log and every trial row record it.

## Say what the subject reads before trial one

Override `Task.instructions`. Every way of starting a session shows what it
returns — `alhazen run --task`, the experiment's `run.py`,
`build_session(task=...)` — so this is the one place the wording lives:

```python
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]   # src/<package>/task.py -> the repository


class MyTask(Task):
    def instructions(self) -> str | None:
        # A file under review, so what the subject reads cannot drift from
        # what was agreed. Read as UTF-8 by name: on a Windows rig the
        # default code page turns every em-dash into three wrong characters.
        return (REPO / "instructions.md").read_text(encoding="utf-8")
```

- **Return `None` for a task with nothing to show** — an animal subject. That
  is a declaration, not an omission: leave the method out altogether and
  `--mode run` logs a WARNING naming it, because a task that forgot looks
  exactly like one that decided.
- **Declare it once on a shared base** when a family of tasks answers the
  same way; every task under the base inherits the answer.
- **Hard-wrapped text is fine.** The display reflows prose: a single newline
  inside a paragraph becomes a space, a blank line separates paragraphs, and
  an indented line or a list item keeps its break.
- **It may quote the params.** It is called once per session, after a
  curriculum has set the stage's values, so `self.params` is what the session
  runs at.
- **Fail loudly.** A missing file raises its own error before the run
  directory is created; empty text is refused, since shown it would be a
  blank screen waiting for SPACE.

## Say which params a task runs with

Override `Task.default_params` — a classmethod, because it decides the params
the task is built with — to name the file a session loads when nobody passes
`--params`. `alhazen run --task` and the experiment's `run.py` both use it,
in every mode:

```python
class MyTask(Task):
    @classmethod
    def default_params(cls):
        return REPO / "configs" / "task.yaml"   # REPO as in the recipe above
```

- **Absolute, or relative to this file.** A relative path is resolved
  against the file the method is written in — never the working directory,
  which is wherever `alhazen run` was started — so a config kept inside the
  package can be named as `"configs/task.yaml"`.
- **A missing file stops the session by name.** The params model's defaults
  are not the experiment, so they never run in place of a file the task
  declared. `REPO` found from `__file__` reaches the repository only while
  the package is installed editable (`pip install -e .`).
- **`--params` still wins**, for a pilot or a one-off; so does
  `run_experiment(default_params=...)` for the sessions `run.py` starts.

When the params depend on *who* and *which session* — a scheduler that
carries state across sessions — override `Task.params_hook` as well:

```python
class MySearchTask(Task):
    @classmethod
    def params_hook(cls, params, args):
        return params.model_copy(update={"session": args.ses})
```

It runs after the subject and session are settled and before the task is
built; what it returns is re-validated through `params_model`, and an
exception it raises is not caught. [docs/modes.md](modes.md#parameters-derived-from-the-invocation)
has the whole contract, including why state derived from the data root must
follow a rehearsal to the rehearsal root.
