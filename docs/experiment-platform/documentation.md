# Experiment Platform: source-bound documentation

Status: implementation on `feature/portable-experiment-platform` (local branch). Owner
modules: `alhazen.experiment_platform.documentation` (the bounded resolver),
`alhazen.experiment_platform.guides` (the reader document of one version) and
`alhazen.experiment_platform.parameters` (parameter-set guides). The catalogue model and
refresh that use them are in [design.md](design.md).

Every document here is derived from **one source version** of an experiment: the files of
a git commit (`sources.read_tree`) or of a bundle. Nothing is imported, executed or fetched.
A document records the version it was built from (`bundleId`, `sourceCommit`), so a reader
can refuse one that belongs to another version instead of showing it as current.

| Document | Built by | Bound to | Stored as (version record) |
|---|---|---|---|
| Resolved descriptor | `documentation.read_documentation` | the descriptor's SHA-256 | (input to the guide) |
| Reader guide (HubDocs schema) | `guides.build_guide` | `bundleId` | `guide` |
| Parameter guide | `parameters.build_parameter_guide` | `sourceCommit`, `alhazen` | `parameterGuide` |

Provenance: the resolver is alhazen's earlier hub module (`alhazen.hub.documentation`,
PR #159 at a8de721) with its bounds and resolved shape unchanged; the guide builder and the
parameter extractor are the Mana platform's (its method-guide builder and
`enrich_parameters.py`). Their tests are ported as characterization tests, and the port was
checked against every listing of the live Mana catalogue: rebuilt from the same inputs,
both documents are equal to the live ones.

## The descriptor (`docs/experiment.json`)

An experiment version can carry a methods section, and for each task a parameter reference,
a trial timeline and a stimulus schematic, written by the experiment's authors in its own
repository. Neither changes how anything runs.

### Descriptor (`schema: "alhazen-documentation"`, `schema_version: 1`)

Unknown fields anywhere are errors (a typo fails, it does not silently document nothing).

| Field | Type | Notes |
|---|---|---|
| `title`, `summary` | text ≤ 160 / 1000 | title defaults to the manifest's |
| `methods` | `.md` path in the experiment | optional |
| `references` | list of text ≤ 600, at most 100 | plain citations |
| `tasks[]` | at most 32 | at least one task or methods |
| `tasks[].id` | task name (`[A-Za-z][A-Za-z0-9_-]*`) | the registered task name |
| `tasks[].title`, `.summary`, `.description` | text, text, `.md` path | |
| `tasks[].parameters_file` | `.yaml`/`.yml`/`.json` path | defaults are checked against it |
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
platform never imports experiment code. Parameter defaults are never invented: a parameter with
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
`[[param:task-id/name]]`; unknown or ambiguous names refuse the document. In a task
description a parameter reference is a button that scrolls to and focuses its row; the URL
is never changed (the rig page owns its fragment).

### Bounds and refusals

Descriptor ≤ 256 KiB , each
Markdown file ≤ 128 KiB and ≤ 512 KiB in total, params file ≤ 256 KiB, JSON nesting ≤ 12,
numbers finite and within ±10⁶, duplicate JSON keys refused, YAML anchors/aliases refused,
control characters refused. Only files of the source version are read, by exact relative
path (`safety.safe_relative`: no `..`, absolute paths, backslashes, drive letters or `.git`).
Errors are `DocumentationError(PlatformError, ValueError)` with messages naming experiment
paths and descriptor fields only.


## The reader guide

`build_guide(listing, docs, files, *, descriptor=None, overlay=None)` resolves each
descriptor task (above) and assembles what the reader draws:

```text
{schema_version: 1, bundleId, title, summary,
 source: {package, version, descriptor, commit, note},
 methods: {path, markdown}, tasks: [resolved task + taskKey, sourceTaskId,
                                    guideNotes?, guideSources?, timelineText?],
 references, warnings, referenceResolutions}
```

- **Overlays are version-bound.** A guide overlay is `{bundleId, tasks: [{taskKey,
  timeline?, diagram?, description?, notes?, sourceFiles?}]}` (or the stored form
  `{bundleId, json}`): compact drawings authored for one bundle. It is applied only when
  its `bundleId` equals `listing.bundle.id` (`overlay_applies`); otherwise it is ignored
  and `warnings` says so. An overlay task that matches no documented task is reported.
  The drawings it supplies go through the same resolver as any descriptor's.
- **Task identity.** `taskKey` is `id@parameters_file` (`task_key`): two tasks may share an
  id, one per params file; the second gets a distinct id and references follow it.
- **Legacy descriptor shapes** stay readable without losing information: a `source` given
  as a citation string becomes `source.citation`; a `null` default is kept with
  `nullable: true`; a `choice` without enumerated choices keeps its type with a note; an
  inline description becomes a generated Markdown file; a timeline written as text is kept
  as `timelineText` with a warning that no timeline is drawn.
- **Drawing extensions** on diagram elements: `color` (`#rrggbb`), `font_size` (text only)
  and `stroke_width` (numbers or `{param, factor}`, positive, at most 200 diagram units),
  `font_family` (Arial, Helvetica, DejaVu Sans, DejaVu Sans Mono, Courier New, sans-serif,
  serif), `font_weight` (normal, bold, 100 to 900). No new shapes.
- **Methods references.** A bare `[[param:name]]` that several tasks own is rewritten: to the
  first owner when all owners have the same value (2 equals 2.0; false is not 0),
  otherwise to every owner. Each rewrite is listed in `referenceResolutions`. Broken
  references refuse the document.

## Parameter guides

`build_parameter_guide(experiment_id, files, *, source_commit, docs, docs_origin, library,
source_root, pinned, listed_sets, listed_ladders)` describes every named set of `run.py`
`PARAMETERS` with the actual values of its YAML file (what each value means from the docs,
whether a replication marks it ASSUMED, how it differs from another set of the same task),
the training ladders of `LADDERS`, and the rig files apart. `run.py` and task modules are
read with `ast`; YAML through the SafeLoader node graph, keeping comments and line numbers.
Merge keys, anchors with aliases and python tags are refused; non-static paths and
non-literal defaults are left unresolved with a warning.

Defaults an experiment inherits from alhazen (`SubjectParams`, `SchedulerConfig`) are listed
when `library` is alhazen's source at the experiment's pinned tag: `library_files(tree)`
from `sources.read_tree(<alhazen repository>, <tag>, "src")`, or `installed_library()` when
`pin_matches(installed version, tag)`. A development checkout carries its last release's
number with newer code, so the tag's own source is preferred. Without a library the guide
still builds and says what it could not resolve.

## Python interface

```python
from pathlib import Path

from alhazen.experiment_platform import guides, parameters
from alhazen.experiment_platform.documentation import DocumentationError
from alhazen.experiment_platform.sources import read_tree

tree = read_tree(Path("my-experiment"), "main")  # git objects at one commit, nothing run
listing = {"id": "my-experiment", "title": "My experiment", "version": "1.0.0",
           "source": {"commit": tree.commit}, "bundle": {"id": "my-experiment-0123456789"}}
docs = {"methods": tree.text("docs/methods.md"), "references": "[]"}
try:
    guide = guides.build_guide(listing, docs, tree.files)
except DocumentationError as error:
    print(f"refused: {error}")
library = parameters.library_files(read_tree(Path("alhazen"), "v2.16.0", "src"))
guide_of_sets = parameters.build_parameter_guide(
    "my-experiment", tree.files, source_commit=tree.commit, docs=docs, library=library
)
```
