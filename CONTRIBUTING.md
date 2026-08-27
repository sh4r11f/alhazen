# Contributing to alhazen

## The gates

Every change passes all five, and none of them is ever weakened to get green.
If a gate seems wrong, say so in the pull request rather than adjusting it.

```bash
pip install -e ".[dev]"
pytest                          # must stay green; no display, no hardware needed
ruff check . && ruff format --check .
mypy                            # zero errors, src/ only
lint-imports                    # the layering contract must stay KEPT
```

**Definition of done:** all five green, the new behavior has tests, and
`docs/architecture.md` is updated in the same change.

## The layering contract

Imports point only downward:

```
cli → session | testing | analysis → training → task → paradigms | devices
    → core → stimuli | scenes → display → config | data
```

`errors` and `version` sit outside it — anything may import them. The contract
is enforced by `lint-imports`, not by convention, and a new package joins the
list in the same change that adds it.

## The invariants

These are what the tests pin. Do not "simplify" one away without a discussion:

1. **Flip-locked events.** Visual events queue via `ctx.emit_on_flip` and emit
   only after the flip that showed them.
2. **One clock.** Every timestamp comes from the injected session clock.
   Device clocks are aligned offline, never mixed in online.
3. **Dumb phases.** A phase touches only the `TrialContext` — no hardware, no
   bus, no window, no module state.
4. **The blink rule.** An unverifiable position is outside every region.
   Fixation is never credited when it cannot be verified.
5. **Scheduler re-queue.** Every outcome with `completed=False` re-serves its
   condition, and `record()` is called for every outcome.
6. **Loud failures.** Subscriber errors propagate. Config typos raise
   `ConfigError` naming the file. A missing vendor SDK raises a typed error
   naming the extra — at use time, never at import time.
7. **Lazy vendor imports.** psychopy, pylink and nidaqmx are imported inside
   the method that needs them. `import alhazen` and the whole default test
   suite work with none of them installed.
8. **Seed discipline.** All randomness flows from generators spawned off the
   one session seed. Never module-level `np.random`.
9. **Data safety.** Never overwrite an existing run. Snapshot before trial 1.
   Teardown attempts every step regardless of earlier failures.
10. **Exact-inverse geometry.** `deg2px` and `px2deg` are one linear model in
    both directions. A second model silently mis-measures every eccentric
    position — by up to a third of the effect size.

## Style

- Python ≥3.10, `from __future__ import annotations` everywhere.
- Protocols over inheritance for every seam.
- Config models subclass `alhazen.config.models.Model` (unknown keys are
  errors, values frozen). Units in field names (`_px`, `_cm`, `_ms`, `_dva`);
  durations are `Duration`.
- Comments explain constraints and reasons — why this is done this way, and
  what breaks otherwise — not what the next line does. A reviewer should be
  able to read the reasoning without reconstructing it.
- Tests: pytest classes grouping behaviors, `alhazen.testing`'s fakes,
  deterministic (no sleeps, no wall-clock dependencies). Anything needing a
  real window gets the `display` marker and is excluded by default.
- Line length 100. Run `ruff format` before committing.

## Commits

One commit per coherent unit. An imperative subject line, and a body saying
what changed and why. No model names in commits, PRs or code.
