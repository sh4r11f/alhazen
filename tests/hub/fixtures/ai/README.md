# AI authoring fixtures

One real authoring round, recorded on 2026-10-09 so the tests never need the
network. Prompt (`prompt.txt`): "A fixation-hold task with a brief peripheral flash on half the trials; measure whether fixation survives the flash", no start-from.

## live/ — what the model actually answered

`exchanges.json` holds the four requests' answers verbatim, with model, usage,
finish reason, time and a SHA-256 of each request's messages. Model
`gpt-4.1-mini-2025-04-14` through the Mana router (OpenAI chat/completions,
`response_format` json_schema strict, temperature 0.2). About US$0.035 for
these four calls (59560 prompt tokens, 31232 of them cached;
12757 completion tokens); an earlier plan-only round with the first prompt
wording cost about US$0.011 and is not kept.

- Plan, attempt 1: failed the plan rules (duration defaults outside their
  min/max, which the model gave in other units). Attempt 2 (the repair) passed:
  it is `plan.json`.
- Source, attempt 1: failed (params model not a SubjectParams, descriptor keys
  outside the task part, an invalid timing kind). Attempt 2 (the repair) still
  failed: its timeline names an `iti` parameter the plan never had (the model
  copied it from the scaffold example). `source-report.json` is the report as
  recorded by the kit at that time.

**The live source round failed validation twice; this is kept as the failure-mode
fixture.** Replayed through the current kit, attempt 2 also fails two checks added
after the run: its flash was a `NullStimulus` (it would never have been drawn),
and its params model redeclared `subject_kind` as a plain string. The source
prompt now addresses all three (parameters exist only if the params file has
them; draw real stimuli; do not redeclare SubjectParams fields), plus two
scientific points the static checks cannot see (show a stimulus during a gaze
requirement with `HoldFixation(concurrent=...)`; an outcome that is the
measurement is `completed=True`). The revised prompts have not been run
against a live model.

## valid/ — the live answer, corrected by hand

`source-answer.json` is live source attempt 2 with these edits only (a test
holds that only these three fields differ):

1. `task_module`: removed the `NullStimulus` import and the redeclared
   `subject_kind`; the flash is `make_fixation(display, screen, flash_size_dva,
   pos=(deg2px(flash_eccentricity_dva), 0))`; the flash interval (and its
   no-flash counterpart) is a `HoldFixation` with `concurrent=["flash"]`, so gaze
   is checked while the flash is up (the model had used `Feedback`, which does
   not check gaze), recorded as `flash_hold_s`.
2. `task_documentation_json`: removed `timeline.between_trials` (it named `iti`).
3. `task_markdown`: removed the sentence referring to `[[param:iti]]`.
4. `task_module` again (2026-10-09, found by the `api` check added after the gpt-4.1 run):
   `movie_clips` read `setup.refresh_rate_hz`, which a MovieSetup does not have (it has
   `hz`); movie mode would have failed with an AttributeError.

`package.zip` is what `generate_source` builds from `plan.json` and that answer
(provenance names the live model); `report.json` is its validation report, all
checks passing. ZIP bytes depend on the zlib build, so the test compares file
hashes, not bytes.

Known weaknesses of the example, kept as generated: the plan serves a broken
trial again (`FIX_BREAK` is `completed=False`), which removes exactly the trials
the experiment means to count; its fixation point is 2 dva (the stimuli table
says 0.3); the two hold phases each take half of `hold_duration` and both write
`hold_duration_s` (alhazen warns). Run outside the hub with `pytest`, the
generated tests pass 4 of 5 (one builds a trial with `display=None`). The hub
never runs them; a person reviews the code and simulates it on a rig.

Regenerate `valid/package.zip` and `valid/report.json` after an intended change
to assembly or validation:

```python
import json, sys; sys.path.insert(0, ".")
from tests.hub.ai_support import FakeProvider, plan_answer, live_exchanges
from alhazen.hub.ai import author
from alhazen.version import __version__
ctx = author.build_context(__version__)
plan = author.Plan.from_dict(json.loads(plan_answer()))
b = author.generate_source(FakeProvider(model=live_exchanges()[3]["model"]), plan, ctx)
open("tests/hub/fixtures/ai/valid/package.zip", "wb").write(b.archive)
open("tests/hub/fixtures/ai/valid/report.json", "w").write(json.dumps(b.report.to_dict(), indent=1) + "\n")
```
