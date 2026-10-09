# Scientific documentation in the Experiment Hub

Status: feature branch, not released. Owner modules: `alhazen.hub.documentation`
(validation, resolution, the guide), `src/alhazen/hub/assets/hub_docs.js` and
`hub_docs.css` (the reading views). Contract summary for the other hub workers:
`/workspace/alhazen-hub-work/documentation-contract.md` during development; this file is canonical.

An experiment version can carry a methods section, and for each task a parameter
reference, a trial timeline and a stimulus schematic. They are written by the
experiment's authors, travel inside the package, are covered by the package's hashes
and are validated when the version is uploaded. Alhazen itself provides a guide to its
modes and protections, generated from its own source. Neither changes how anything runs.

## Where it is shown

| View | Content | Not shown there |
|---|---|---|
| Experiment › Methods | `renderMethods`: methods text, task index, references, provenance | parameters tables, figures |
| Experiment › Tasks & parameters | `renderTaskGuide`: description, Figure 1 stimulus, Figure 2 timeline (+ text version), parameters, outcomes, events | — |
| Guide (top level, central and rig, offline on a rig) | `renderGlobalGuide` | experiment content |
| Run page, live monitor | nothing but, at most, a quiet link to the task guide | never an overlay or panel during a session |

A package without documentation renders `renderMissing`: "No methods for this version",
never a placeholder. No downstream experiment is changed to manufacture coverage.

## The package side

`alhazen-package.json` may hold `"documentation": "docs/experiment.json"`. The packages
module checks only that it is a safe relative path to a declared `.json` file. Everything
inside belongs to this module.

### Descriptor (`schema: "alhazen-documentation"`, `schema_version: 1`)

Unknown fields anywhere are errors (a typo fails, it does not silently document nothing).

| Field | Type | Notes |
|---|---|---|
| `title`, `summary` | text ≤ 160 / 1000 | title defaults to the manifest's |
| `methods` | declared `.md` path | optional |
| `references` | list of text ≤ 600, at most 100 | plain citations |
| `tasks[]` | at most 32 | at least one task or methods |
| `tasks[].id` | task name (`[A-Za-z][A-Za-z0-9_-]*`) | the registered task name |
| `tasks[].title`, `.summary`, `.description` | text, text, declared `.md` | |
| `tasks[].parameters_file` | declared `.yaml`/`.yml`/`.json` | defaults are checked against it |
| `tasks[].parameters[]` | at most 200 | see below |
| `tasks[].outcomes[]` | `{name, completed, success?, meaning}` | names UPPER_CASE |
| `tasks[].events[]` | `{name, meaning}` | |
| `tasks[].timeline`, `.diagram` | objects | optional |

Parameter: `name` (dotted key path into the params file), `label`, `group`, `type`
(`duration | number | integer | boolean | string | choice | object`), `unit`, `default`
(the JSON value exactly as in the params file, e.g. `{"ms": 500}`), `constraints`
(`min`, `max`, `choices`, `note`), `meaning` (required), `interactions`, `source`
(`file`, `key`, `model`). Resolution, per parameter:

| Situation | Result | `source.status` |
|---|---|---|
| file has the key, default given, equal | kept | `matched` |
| file has the key, default given, different | **DocumentationError**: update the documentation with the code | — |
| file has the key, no default given | the file's value is used | `read` |
| file lacks the key | **DocumentationError** | — |
| no file (`source.file: null`, no `parameters_file`) | kept as declared, or no default | `declared` |

Keys the params file sets but the descriptor does not describe are listed in
`parameters_undocumented` and shown as such. `source.model` is provenance text only: the
server never imports package code. Parameter defaults are never invented: a parameter with
no documented and no checkable value has `has_default: false` and reads "no default".

### Timeline

`phases[]` (≤ 24) each `{id, label, timing, start_events?, end_events?, note?}`;
`tracks[]` `{label, role, from, to}` (roles `stimulus, requirement, response, reward,
annotation`); `end {outcome, label?}`; `branches[]` `{from: phase id | "*", kind:
failure | abort, when, outcome, effect?}`; `between_trials {label, timing}`; `caption`.

| `timing.kind` | Fields | Drawn |
|---|---|---|
| `fixed` | `ms` | to scale (`ms: 0` is an instant marker) |
| `parameter` | `param` (type duration, or number with unit ms/s) | to scale if the default is in ms; a `{frames: n}` default depends on the refresh rate and is **not** scaled |
| `event` | `until`, optional `max: {ms} \| {param}` | hatched with break marks, fixed visual width; never stretched by its timeout |
| `conditional` | `when`, inner `timing` (not conditional) | dashed; text says "only if …" |

Outcomes in `end`/`branches` must be the task's documented outcomes or ones the engine
reserves (`ABORTED`, `PAUSED`, `DROPPED_FRAMES`, read from `alhazen.core.trial`). Events
must be documented events.

### Stimulus schematic

Degrees of visual angle (or the declared `unit`), origin at the display centre, y up;
`width`, `height` ≤ 200; optional `background` luminance 0–1; ≤ 200 `elements`. Every
number is a literal or `{"param": name, "factor": k}`: a numeric parameter's resolved
default times a literal. There are no expressions and no uploaded SVG or HTML.

| Type | Numbers | Extra |
|---|---|---|
| `circle` | cx, cy, r | `luminance`, `dashed` |
| `ellipse` | cx, cy, rx, ry | `rotation` |
| `rect` | cx, cy, width, height | `rotation`, `luminance` |
| `screen` | cx, cy, width, height | display outline |
| `line`, `arrow` | x1, y1, x2, y2 | |
| `dot_field` | cx, cy, radius, dot_radius | `count` ≤ 400, `seed` (deterministic layout) |
| `grating` | cx, cy, radius, cycles ≤ 40 | `orientation`, `contrast` (square-wave schematic) |
| `text` | x, y | `text` ≤ 120 |
| `dimension` | x1, y1, x2, y2 | `text`, `value` (shown with the unit) |

Common: `role` (`stimulus, region, apparatus, annotation, target, distractor, cue`),
`label` (becomes a numbered callout with an HTML legend), `dashed`.

### Markdown

Headings, paragraphs, lists, block quotes, fenced code, simple pipe tables, `*em*`,
`**strong**`, `` `code` ``, `[text](https://…)` links. Raw HTML is shown as text; images are
never fetched (they read "[image not shown: alt]"). References: `[[task:id]]`,
`[[param:name]]` (the current task's, or in the methods the one task that has it) and
`[[param:task-id/name]]`; unknown or ambiguous names refuse the upload. In a task
description a parameter reference is a button that scrolls to and focuses its row; the URL
is never changed (the rig page owns its fragment).

### Bounds and refusals

Descriptor ≤ 256 KiB (the packages module allows up to 4 MiB; this is stricter), each
Markdown file ≤ 128 KiB and ≤ 512 KiB in total, params file ≤ 256 KiB, JSON nesting ≤ 12,
numbers finite and within ±10⁶, duplicate JSON keys refused, YAML anchors/aliases refused,
control characters refused. Only files listed in the manifest are read, by exact path, and
each one's size and SHA-256 are checked again. Errors are `DocumentationError(ValueError)`
with messages naming package paths and descriptor fields only.

## Python interface

```python
from pathlib import Path

from alhazen.hub.documentation import DocumentationError, global_guide, read_documentation

manifest = {"name": "fixation-demo", "files": []}  # the package's parsed alhazen-package.json
try:
    documentation = read_documentation(Path("package.zip"), manifest)  # dict, or None if absent
except DocumentationError as error:
    print(f"refused: {error}")  # the server answers 422 invalid_documentation
guide = global_guide()  # dict
```

`read_documentation` returns None when the manifest names no documentation. The resolved
dict:

```text
{schema_version, source: {package, version, descriptor, descriptor_sha256},
 title, summary, references, methods: {path, sha256, markdown} | null,
 tasks: [{id, title, summary, description: {path, sha256, markdown} | null,
          parameters_file, parameters_undocumented,
          parameters: [{name, label, group, type, unit, default, has_default, default_text,
                        constraints, meaning, interactions,
                        source: {file, key, model, status}}],
          outcomes, events,
          timeline: {caption, phases: [{id, label, note, start_events, end_events,
                     timing: {kind, ms, text, scaled, param?, param_label?, until?, max_ms?,
                              max_text?, max_param?, when?, inner?}}],
                     tracks, end, branches, between_trials} | null,
          diagram: {caption, unit, width, height, background,
                    elements: [{type, role, label, luminance, dashed, refs, ...numbers}]} | null}]}
```

`global_guide()` returns `{schema_version, title, alhazen_version, intro, modes, sections}`.
`modes` is derived from `alhazen.modes.Mode`: summary, `runs_trials`, `drives_subject`,
`writes_real_data`, `refuses_development_rig` (from `real_data_refusal`), `accepts`
(`headless`, `mouse`, `calibration_target`, from `flag_refusal`) and the data destination
(from `REHEARSAL_SUFFIX`, `TRAINING_SUFFIX`). `sections` (timing, hardware, reward,
calibration, designs, documentation) hold short notes, each naming its source module;
listed values (frame-QA policies, subject kinds, calibration choices, scheduler kinds) are
read from the code. It imports only core alhazen (`modes`, `config`, `core`, `task`,
`training`, `paradigms`), never `alhazen.cli` and never the hub's server extras.

Server: validate on version upload (`DocumentationError` → 422 `invalid_documentation`),
serve `GET …/versions/{id}/documentation` → `{documentation: dict | null}` with the
download ACL, and `GET /guide` → `{guide: global_guide()}`. Rig: proxy the first, serve
the second locally.

## Browser interface (`hub_docs.js`, classic script, `window.HubDocs`)

```js
HubDocs.renderMethods(doc, options)            // doc: resolved documentation or null
HubDocs.renderTaskGuide(doc, taskId, options)
HubDocs.renderTaskIndex(doc, options)
HubDocs.renderGlobalGuide(guide, options)      // guide: GET /guide → .guide
HubDocs.renderMissing('methods' | 'task' | 'guide', options)
HubDocs.renderMarkdown(text, options, scope?)
HubDocs.timelineSvg(task, options) / HubDocs.diagramSvg(task, options)
HubDocs.parseMarkdown, parseInline, layoutTimeline, layoutDiagram, formatValue,
        resolveParameterReference                // pure, for tests
```

`options = {document, taskHref(taskId) -> "/same-origin/path" | null, headingLevel = 2,
idPrefix = "hd-"}`. Each function returns a detached element for the parent to mount;
nothing fetches, routes or writes the URL. A `taskHref` that is not a same-origin path
(`/…`, not `//…`) renders plain text. Figures are SVG with `role="img"`, a `<title>` and a
`<desc>` stating every phase, branch and shape in words; the timeline also has a
visible text list. No `innerHTML`, no `eval`, no style attributes (the page CSP has none).

`hub_docs.css` is light by default; colours are `--hd-ink, --hd-muted, --hd-faint,
--hd-paper, --hd-soft, --hd-rule, --hd-accent, --hd-accent-soft, --hd-fail, --hd-abort,
--hd-ok` on `.hd-doc`, which a dark parent can override. The stimulus panel uses the
stimulus's own luminance and stays physical in either theme. Below 640 px of view width
the tables stack and the timeline keeps its natural size in a sideways-scrolling,
keyboard-focusable frame, with the same content in the list below it.

## The scaffold example

`tests/hub/fixtures/documentation/scaffold/docs/` documents the package `alhazen new
fixation_demo` writes (methods, task description, descriptor). Tests hold it to the source:
every default equals `FixationDemoTaskParams()`'s; the window radius and point diameter
follow `CircleRegion`/`FixationPoint`; outcomes and events are the task's; the scheduler
choices are `SchedulerConfig.kind`'s; and the package is simulated headless, producing only
documented outcomes and the documented events. It reports no data.

### Fixtures

`tests/hub/fixtures/documentation/resolved/{scaffold,guide}.json` are the resolved outputs
the Node tests draw; a Python test fails when they drift. Regenerate with:

```python
import json, sys, tempfile; from pathlib import Path
sys.path.insert(0, "tests/hub"); import test_documentation as t
from alhazen.hub.documentation import read_documentation, global_guide
with tempfile.TemporaryDirectory() as d:
    doc = read_documentation(*t.with_descriptor(Path(d), t.descriptor()))
out = Path("tests/hub/fixtures/documentation/resolved")
(out / "scaffold.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
(out / "guide.json").write_text(json.dumps(global_guide(), indent=1, ensure_ascii=False) + "\n")
```

## Design notes

| Module | Secret | Interface | Price | Do not rely on |
|---|---|---|---|---|
| `documentation.py` | how untrusted documentation becomes bounded, checked data; which source facts the guide reads | `read_documentation`, `global_guide`, `DocumentationError` | a strict schema authors must follow; the guide imports core alhazen | the resolved dict's key order; `model` being checked |
| `hub_docs.js` | Markdown subset, figure geometry and grammar, accessible DOM | the render functions above | a second formatter (`formatValue`) kept equal to Python's by a test | element structure inside a view beyond the documented classes |

Rejected: uploaded SVG or HTML figures (script and external-fetch risk, cannot be checked
against parameters); expressions in diagram numbers (an evaluator is an attack surface;
`param × factor` covers the scaffold and common sizing); fixed-width timelines (they assert
durations the experiment does not have); importing package code to read defaults (the
server must never execute uploads, so defaults are checked against the params file instead).

Failure handling: every refusal is a `DocumentationError` raised before anything is stored;
ZIP read errors (corrupt, encrypted, unsupported compression) become the same error; no
partial documentation is returned. The renderer draws only validated data; a reference it
cannot resolve renders as plain text, never as a link.

Limitations: the browser figures are verified in Node (structure, geometry, accessibility,
absence of markup/styles) and by rendered previews, not yet in the integrated page at
desktop and 390 px; that inspection belongs to the integrated hub. The grammar covers
schematic shapes, not natural images or movies. Methods are checked for references and
size, not for scientific accuracy; authors stay responsible for their text.

## Future: AI-assisted authoring (planned, not in this build)

There is no AI feature, provider key field or AI button. A later phase may let users with
their own provider keys draft documentation changes in a private fork, as reviewable diffs
validated by this same module and accepted by a person (docs/hub/design.md).
