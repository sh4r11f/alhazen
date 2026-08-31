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
        self._t0 = ctx.clock.now()

    def on_frame(self, ctx):
        if self._key in ctx.inputs.keys:
            return self._on_press
        if ctx.clock.now() - self._t0 >= self._timeout_s:
            return self._on_timeout
        return PhaseAction.CONTINUE
```

Two rules that are not optional:

- **Touch nothing but `ctx`.** No hardware, no bus, no window, no module
  state. That is what lets every phase be tested against a fake clock.
- **Check gaze before checking completion.** If the phase requires fixation,
  test it first — otherwise a blink on the final frame passes as success.

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
returns a number; name it in a stage's `promote_when` or `demote_when`.

## Add a display backend

Satisfy `DisplayBackend`: `kind`, `window`, `open`, `close`, `flip`,
`measure_refresh_rate`, `show_message`, `set_gamma`. `flip()` blocks until
the buffer swap and returns nothing — the engine stamps the clock immediately
after, so there is exactly one clock and one stamping site.
