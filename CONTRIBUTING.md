# Contributing to alhazen

## The environment

alhazen is developed with [uv](https://docs.astral.sh/uv/). One command
builds the whole development environment, in `.venv` inside the clone:

```bash
uv sync                         # Python 3.11 (.python-version), exactly as uv.lock pins it
```

uv downloads the Python itself if the machine has none. `uv sync` installs
alhazen editable with the `dev` dependency group, which is the `dev` extra
in `pyproject.toml` plus pip and setuptools; an extra is added with
`--extra`, e.g. `uv sync --extra psychopy` for a real window. Nothing needs
activating: `uv run <command>` runs a command in that environment, and
brings the environment up to date with `uv.lock` first.

- **Adding or changing a dependency:** edit `pyproject.toml` (development
  tools go in the `dev` extra, which pip users install too), run `uv lock`,
  and commit `uv.lock` in the same commit. CI fails when the two disagree.
- **Without uv:** `pip install -e ".[dev]"` in any Python 3.10 or newer
  still works, unlocked; it is what CI's test matrix runs.

## The gates

Every change passes all six, and none of them is ever weakened to get green.
If a gate seems wrong, say so in the pull request rather than adjusting it.

```bash
uv run pytest                   # must stay green; no display, no hardware needed
uv run ruff check . && uv run ruff format --check .
uv run mypy                     # zero errors, src/ only
uv run lint-imports             # the layering contract must stay KEPT
node --test "tests/js/*.test.mjs"  # the live monitor renderer; Node 22+, nothing to install
```

A change to `docs/` also builds the site the way `.github/workflows/docs.yml`
does: `uv run --extra docs mkdocs build --strict`.

The last gate in the block runs the live monitor's browser script,
`src/alhazen/live_monitor/assets/live_monitor.js`, which pytest cannot execute. It
needs only Node 22 or newer (<https://nodejs.org>) — no npm install and no
`package.json`; keep the quotes, Node expands the pattern itself. The tests in
`tests/js/` load the real script, unmodified, into a fake page
(`fake_dom.mjs`, `load_live_monitor.mjs`) and check what it draws: tick values,
number formats, colours, legends, figure-export sizes, the camera stream. Run
one file with `node --test tests/js/charts.test.mjs`. When live_monitor.js starts
using a DOM feature the fake lacks, the fake throws naming it: extend the
fake, do not loosen it.

Two of those live inside `pytest` and are worth knowing about before one
fails on you:

- `tests/unit/test_contracts.py` pins the on-disk compatibility contracts
  (RNG streams, reserved events, the run-directory layout, the schema version
  numbers) against `tests/fixtures/contracts.json`. Adding to any of them is
  normal — update the baseline in the same commit. A failure on a *removal* is
  the contract working.
- `tests/unit/test_versioning.py` checks that `pyproject.toml` and
  `CHANGELOG.md` name the same version, and that every deprecation's
  `removed_in` is a MAJOR release the declared version has not reached — so
  the bump to a MAJOR fails until the names it removes are gone.

**Definition of done:** all six green, the new behavior has tests, and
`docs/architecture.md` is updated in the same change.

## The layering contract

Imports point only downward:

```
cli → modes → session | testing | analysis → training → task → live_monitor
    → paradigms | devices → core | neural → stimuli | scenes → display
    → config | data | _scaffold → _deprecation
```

`errors` and `version` sit outside it — anything may import them.
`_deprecation` (the `@deprecated` decorator) is on it, alone on the bottom
line: every layer may import it, and it may import nothing else from alhazen.
The contract is enforced by `lint-imports`, not by convention, and a new
package joins the
list in the same change that adds it: `lint-imports` cannot see a package that
is not on the list, so `tests/unit/test_layering.py` fails until it is. The
same test checks this drawing, and the one in `docs/architecture.md`, against
the config.

## The invariants

These are what the tests pin. Do not "simplify" one away without a discussion:

1. **Flip-locked events.** Visual events queue via `ctx.emit_on_flip` and emit
   only after the flip that showed them, stamped with that flip's time.
2. **One clock.** Every timestamp comes from the injected session clock
   (`build_session(clock=...)`; a tracker or stand-in you build yourself
   gets that same clock). Device clocks are aligned offline, never mixed in
   online.
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
11. **Durations to the frame.** A phase of d seconds is on screen for d, to
    the nearest frame: a timed phase asks `ctx.time_up` before drawing and
    ends through `ctx.end_undrawn`, and the engine does not flip a frame a
    phase ended undrawn. A check made after drawing shows one frame too many
    (docs/architecture.md §2.3).

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

Anything user-visible gets an entry under `## Unreleased` in `CHANGELOG.md`, in
the same commit. That section is what becomes the next release's notes.

## Releasing

The version number lives in exactly one place — `version` in `pyproject.toml` —
and `CHANGELOG.md` and the git tag must agree with it. A published number is
spent forever, so `scripts/release_check.py` refuses to let CI build anything
until all three match. [docs/versioning.md](docs/versioning.md) has the full
policy; the steps are:

```bash
# 1. In one commit: rename `## Unreleased` to `## X.Y.Z - YYYY-MM-DD`,
#    set version = "X.Y.Z" in pyproject.toml, and run `uv lock` (uv.lock
#    records the project's own version, so it changes too; CI fails without it).
uv lock
# 2. Run the same gate CI will run. A mismatch here costs an edit;
#    the same mismatch after tagging costs a deleted tag.
uv run python scripts/release_check.py --tag vX.Y.Z

# 3. Land that commit on main, then tag it.
git tag vX.Y.Z
git push origin vX.Y.Z
```

The tag triggers `.github/workflows/release.yml`: the version gate, then a
build of the wheel and sdist that twine checks. Nothing is published yet;
docs/versioning.md says why the publishing jobs are not there and how they
come back.
