# Experiment workspace

`alhazen dashboard` opens a local web app for managing downstream Alhazen
experiments. The launcher is separate from the [live session monitor](live_monitor.md):
it configures and starts processes, while the monitor receives trial data and
keeps its existing keyboard-pause policy.

```bash
alhazen dashboard --project ~/projects/amodal-averaging --project ~/projects/kde-vergence
```

The folders are remembered. Next time, `alhazen dashboard` is enough. Add more
with **Add experiment**, using the path to a checkout containing `run.py`.
Nothing is installed by registering a folder. Choose a Python interpreter in
**Project settings** if the experiment needs a particular conda environment or
virtual environment. Otherwise the launcher uses the project's `.venv` when
present, then its own interpreter. The project's `src/` and root are placed
first on the child's `PYTHONPATH`; nothing of the launcher's own installation
is, so the interpreter you choose must have `alhazen-vision` installed (an
experiment's `pyproject.toml` requires it). Registering a folder checks this by
importing alhazen with that interpreter, refuses with the reason when it
cannot, and records the alhazen and Python versions it found, and the
[shared rigs](rigs.md) that alhazen ships.

## Configure and run

1. Select an experiment in the sidebar.
2. Choose a mode, rig and parameter preset. The **Rig** menu lists rigs by
   name — `lab` for `rig-lab.yaml` — in two groups: **This experiment**, its
   `configs/rig-<name>.yaml` files (subdirectories and `.yml` included), and
   **Shared (alhazen)**, the rigs the project's alhazen ships
   ([Rigs](rigs.md)). An experiment rig that extends a shared one says so
   (`lab · extends alhazen/lab`); a shared rig hidden from `--rig lab` by the
   experiment's own is spelled `alhazen/lab (hidden by this experiment's lab)`,
   so the two cannot be confused. The summary under the menu describes the rig
   as it would run — merged, for one that extends — and says whose it is. A
   project registered before shared rigs were listed shows none, and says so:
   save its **Project settings** to register it again. Parameter presets start
   with `task` or `params`.
3. Choose text parameters from dropdowns; text lists use dropdowns with
   checkboxes. Choices come from the task model's enums and defaults, keeping
   the current value available. Keyboard bindings offer common keys. Unbounded
   custom strings can still be entered through the **Text (YAML or JSON)**
   editor; switching to it from Fields shows the current values as JSON,
   which is valid YAML, and either notation may be typed. Numeric arrays use
   JSON notation in the fields editor. Search filters nested fields.
   **Task defaults** leaves parameter loading to the experiment's entry point.
   Measure rig hides the parameter preset and editor entirely, as do standalone
   scripts without a parameter-file option. Hidden task parameters are not sent
   to those jobs.
4. Set the mode's options, add any extra `run.py` arguments (below), and start
   the run. Run and test require a subject ID and the subject's **Initials**
   (1 to 5 letters, sent uppercase as `--initials`); simulate can use its own
   default subject and needs no initials. Initials that break the rule are
   refused on the page in the command line's words ("initials must be 1 to 5
   letters, such as HD"), and again by the launcher, before a run is made.
   They are recorded with the session — never in a file name — and checked
   against the subject's recorded initials: a subject id already recorded with
   other initials is refused ([data on disk](data.md) §2). The history and the
   run summary show who each session was for, `sub-01 · HD`. A project whose
   interpreter runs an alhazen older than 2.0 does not know `--initials`: its
   run and test launches stop at once with that usage error in the console,
   and the fix is to move the project to alhazen 2.0. Only simulate accepts
   headless, and only test accepts mouse gaze.
5. Follow the console or view generated media. Images can be enlarged or saved.
   Movies appear once recording finishes, with native playback and seeking.

The six modes are **simulate**, **demo**, **movie**, **test**, **run** and
**measure**. Demo, test, run and measure still use a physical display and its
usual keyboard controls; the browser is not a replacement renderer. In demo,
use the viewer's screenshot control to send images to the run's gallery.
Movies use the task's `movie_clips` implementation and require the movie extra
in the selected interpreter. A mode a task has not implemented fails visibly
in its console, exactly as it would from `run.py`.

**Several tasks in one experiment.** A `run.py` that declares its tasks as a
table and hands it to alhazen —

```python
TASKS = {
    "mib-search": (MIBSearchTask, HERE / "configs" / "task-search-rdk.yaml"),
    "mt-tuning": (MTTuningTask, HERE / "configs" / "task-tuning.yaml"),
}
run_experiment(tasks=TASKS, default_task="mib-search", default_rig=..., argv=sys.argv[1:])
```

— gets a **Task** menu. The page lists the table's names with the default
selected, reads the chosen task's parameter choices, preselects that task's
parameter file as the preset when the table names one, sends `--task <name>`
right after the mode, and shows the task beside the mode in the history. The
launcher reads the table from `run.py` itself (the way it finds rigs and
scripts), so it must be a module-level dict literal with string keys and, for
the preset, each entry's second element written as `HERE / "configs" / "x.yaml"`
(with `HERE` bound from `__file__`) or as a string path; a `tasks=` written
any other way is reported under the menu, with the shape expected, and a
launch of that project is refused with the same words. A `run.py` that
declares one task (`task_class=`) has no menu and takes no task.

**Extra run.py arguments** go to the experiment's entry point after the
launcher's own flags, in every mode. The field is split like a shell command
line: quote an argument that contains spaces, and write paths with forward
slashes, since a backslash escapes the character after it. It is how a runner
flag the form has no control for is given: `--curriculum configs/shaping.yaml`,
`--run 3`, or measure mode's `--skip` — and, for an experiment that reads its
own `--task` from the command line instead of declaring a table, how its task
is named. An extra argument naming a flag the launcher sets from the form is
refused, naming the flag, before anything is written, so a run's recorded
settings cannot be contradicted from the text field. Those flags are `--mode`,
`--rig`, `--params`, `--seed`, `--no-live-monitor-browser`, `--sub`, `--ses`,
`--initials`, `--trials-per-condition`, `--headless`, `--mouse`, `--windowed`, `--out`,
`--scale`, `--sheet`, `--columns`, `--clip` and `--screenshots`, in either the
`--seed 5` or the `--seed=5` spelling, and `--task` for a project with a Task
menu; every other flag passes through.

The launcher discovers standalone `src/<package>/preview.py` and `movie.py`
modules when they declare a literal `--out` argparse option and a `__main__`
entry point. **Preview images** runs Amodal's PNG generator; **Movie script**
runs its standalone recorder, which offers additional sheet options. The
launcher passes `--rig` and `--params` or `--task-config` when the script
supports them; those and `--out` are the flags reserved for a script, and
anything else it declares goes in the same extra-arguments field, whose help
lists the flags the script offers. Scripts that are only internal viewer
helpers (such as KDE's preview module) are not presented as runnable image
generators; use demo or movie instead.

Parameter choices are read from the class passed to `run_experiment(task_class=...)`
— or, with a Task menu, from the chosen entry of `run_experiment(tasks=...)` —
in a separate process using the project's selected Python interpreter. This
imports the task but does not execute the `run.py` main block or start a session.

Each launch writes an immutable parameter file when parameters were supplied,
a copy of the rig, the actual argument list and working directory, console
output, and its own media folder. Source configuration files are never edited.
The experiment's parameter model still validates values before a session
starts, so unsupported values produce the same errors as the CLI. An
experiment's rig is passed to the process at its original path, to preserve
relative-path semantics; a shared rig is passed by name, `--rig alhazen/lab`,
as it would be typed. The copy, `rig.yaml`, is the file itself for a whole
rig; for a rig that extends a shared one it is the merged rig — the one file
alone would not say what ran — with the experiment's file as written kept
beside it as `rig-source.yaml`. `run.json` records the rig launched (`rig`),
its name (`rig_name`) and whose it is (`rig_source`: `experiment` or
`alhazen`), and the history shows the name. Session data retains the
experiment's normal real/rehearsal paths.

Only one job runs at a time in a workspace. **Stop run** interrupts the run:
SIGINT to its process group on POSIX, a console break (`CTRL_BREAK_EVENT`) on
Windows, which `alhazen run` and every `run.py` built on `run_experiment` turn
into the same `KeyboardInterrupt` as Ctrl+C. The session then tears down as
usual — trials file, manifest, tracker recording — and has thirty seconds to
finish, because an EyeLink EDF transfer plus manifest hashing can take that
long. A run still alive after that is killed: its status becomes **killed**
rather than **cancelled**, its `run.json` records `"stopped": "forced"`, and its
console log ends with a line saying the data may be incomplete. Standalone
preview and movie scripts do not go through `run_experiment`, so a break ends
them at once. Closing the browser does not stop a run; Ctrl+C in the launcher
terminal does. Runs and their output survive restart. A job left marked
running after an unexpected server exit is shown as **interrupted**, not
successful; inspect the OS for surviving processes before restarting hardware
after a crash. On Windows the break is delivered at the run's next Python
statement (a blocking wait delays it), and a forced termination ends the direct
child only; descendants may need to be stopped separately.

## Live monitor

The [live session monitor](live_monitor.md) is a separate loopback server that the
session process starts when the rig has `live_monitor.enabled: true`; the runner
prints its address on the console before trial one (`live monitor: http://127.0.0.1:…`)
and the launcher relays it. The Run output card's
**Live monitor** tab embeds that page while the run is active, so the session's
trials, eye-tracker panels and pause menu are watched from the workspace
without a second browser tab. **Open monitor in new tab ↗** beside the tabs
opens the same page on its own. The monitor keeps its own token and its
pause-only controls; the workspace adds nothing to them, and the launcher
passes `--no-live-monitor-browser` so the session does not open a browser of its
own as well.

For a run started from the workspace, the tab is brought up automatically the
first time its monitor URL appears; a run picked from the history keeps whatever
tab is open. Before the URL appears, the tab says that it is waiting for the
session to open its monitor — or, when the selected rig has
`live_monitor.enabled: false` (the default for a rig without the block), that the
setting must be turned on in the rig YAML. The rig summary under the rig menu
shows the same fact as **live monitor: on/off** — for a rig that extends a
shared one, the setting of the merged rig.

The monitor's server closes with the session. Once the run has finished, the
frame is emptied and replaced by a note: the monitor's final state was saved in
the run's data directory as `figures/live_monitor.html` (and
`figures/live_monitor_state.json`), which opens on its own with no server. A
run recorded by a project on an alhazen before 2.0 has them under their old
names, `figures/dashboard.html` and `figures/dashboard_state.json`, and the
note names both. A browser error page for a server that no longer exists is
never left in the tab.

Embedding needs the monitor to allow being framed, so its responses carry
`frame-ancestors 'self' http://127.0.0.1:* http://localhost:*` in their
Content-Security-Policy: local pages may frame it, nothing else may. The frame
is sandboxed (`allow-scripts allow-same-origin allow-downloads allow-modals`):
the monitor page runs its own script against its own server, can save a figure
and show a dialog, and cannot navigate the workspace or open windows from it.
Running `python run.py` from a terminal is unchanged: the monitor then opens in
its own browser tab as before.

## Data

Each experiment's **Data** view reads the sessions it has saved: pick a data
folder, browse its runs, open one, load its trials and plot them. It only
reads — nothing in a data folder is changed — and it imports none of the
experiment's code.

### How it fits together

```mermaid
flowchart LR
  subgraph Browser
    NAV["workspace.js<br/>(sidebar: Run experiment | Data)"]
    VIEW["workspace_data.js<br/>WorkspaceData.show / hide"]
    PLOT["workspace_plot.js<br/>build → render (SVG)"]
    NAV -- "show(project, {api, token, node, error})" --> VIEW
    VIEW --> PLOT
  end
  subgraph Server["dashboard.py (loopback, token)"]
    API["GET /api/data/roots · runs · run · text · table · page"]
    FILE["GET /data/file (figures, token in URL)"]
    PAGE["GET /data-page/&lt;ticket&gt;"]
    DV["workspace_data.DataView"]
    API --> DV
    FILE --> DV
    PAGE --> DV
  end
  subgraph Disk
    RIGS["project rigs<br/>configs/rig-*.yaml + shared rigs"]
    ROOTS["data_root/ and data_root-rehearsal/<br/>v&lt;version&gt;/sub-*/ses-*/run-*"]
  end
  VIEW -- "JSON (ids, never paths)" --> API
  VIEW -- "&lt;img src&gt;" --> FILE
  VIEW -- "new tab, noopener" --> PAGE
  DV -- "rig_mapping: data_root" --> RIGS
  DV -- "find_runs, csv, session.json" --> ROOTS
```

The page's navigation opens the view with
`window.WorkspaceData.show(projectRecord, {api, token, node, error})` and
leaves it with `WorkspaceData.hide()`; the view fills `<div id="data-view">`
by creating elements, never by writing markup, since everything it shows
comes from files.

### Data folders

A project does not say where its data goes; its rigs do. The folders offered
are, for every rig in the Rig menu (the experiment's own and the shared ones
its alhazen ships), the rig's `data_root` — merged, for a rig that
`extends` a shared one, exactly as a launch merges it — resolved against the
project folder when relative, plus its rehearsal sibling
`<data_root>-rehearsal`, where test and simulate write ([Modes](modes.md)).
Each folder is labelled with the rigs that write there and whether it holds
real or rehearsal data. Only folders that exist are offered; the others are
listed as "not created yet". A rig that cannot be read, or names no
`data_root`, is named at the top rather than skipped.

### Runs

The run table lists every run folder `alhazen.data.find_runs` finds in the
folder, in both layouts: version (`pre-2.0` for a run recorded before
alhazen 2.0, which has no version folder), subject and initials, session,
run, task, mode, date, number of trials, rig. The fields come from the run's
`session.json`, or for a pre-2.0 run from its `config_snapshot.yaml` (which
has no mode). The trial count is `report.yaml`'s when the run wrote one, and
otherwise the trials file's lines, shown as `~N`: listing counts lines rather
than parsing a CSV, so a folder of hundreds of runs lists quickly. Menus
filter by version, subject and task. A run whose records cannot be read is
still listed, flagged ⚠, and its problem is said under the table.

Clicking a run opens it: a summary of `session.json` (experiment and version,
task, mode, subject, session and run, seed, when, rig, params file, alhazen),
the files in the folder, buttons that show its text records
(`session.json`, `config_snapshot.yaml`, `rig.yaml`, `rig-source.yaml`,
`params.yaml`, `report.yaml`, `manifest.yaml`, and the last 64 KiB of
`session.log`; other files are listed, not shown), the images under
`figures/`, and **Open saved live monitor ↗** for
`figures/live_monitor.html` (`figures/dashboard.html` before 2.0).

### Tables

**Load trials table** loads the run's `*_trials.csv`; the menu beside it
picks the events, frames or paradigm table instead. Check several runs and
**Load and pool** to read one table from all of them: the rows are stacked,
the columns are the union of the files' (a run without one gets empty
cells), and three columns are added in front — `run` (the folder's id),
`subject`, `session` — so pooled rows stay distinguishable. A CSV column
with one of those names keeps it, and the added one is called
`subject (folder)`. Headers sort (numerically for a numeric column; empty
cells last), the text box keeps the rows in which any cell contains the text,
and the count says how many rows match. The first 500 matching rows are
drawn; sorting and filtering bring the others up, and plots use them all.

At most 50 000 rows are sent per load, pooled runs together; a table cut
there says so, with the file's full count, and so does every plot drawn from
it. A file that cannot be parsed (a cell over the csv module's size limit, a
file that is not UTF-8, an empty file) fails the load with the run, the file
and the line named. A row with more or fewer cells than the header is kept,
padded or cut to the header, and named.

**Numbers are parsed in the browser.** The server sends each cell as the
CSV's own text: a CSV has no types, and the plot must decide per column
anyway what it holds. A cell is a number when it looks like one, `True` and
`False` (how the trials file writes a boolean) are 1 and 0, an empty cell is
missing, and anything else is text. A column is numeric when every non-empty
cell is.

### Plots

Choose **x**, **y**, an optional **group by**, and a kind:

| Kind | What is drawn |
|---|---|
| Mean ± SEM of y per x | the mean of y at each value of x, with the standard error (sample SD / √n; none for a single value), one series per group; a 0/1 or True/False y is a proportion, on a 0–1 axis |
| Scatter | one point per row (at most 20 000 drawn, said when more) |
| Histogram of y / of x | counts in Sturges' number of bins, rounded to a round width; one group is bars, several are outlines on shared bins |

A text column can be x (one category per value, at most 30) or the group-by
(at most 5 groups, each with its own colour and marker shape), never y; the
y menu lists text columns disabled and says why. A plot that cannot be drawn
says why in words. Series colours are a fixed Okabe-Ito order that reads on
light and dark pages; axes and text take the page's theme colours.
**Save figure (SVG)** downloads the drawing, with the colours in force
written into the file.

### Safety

Every route is read-only, on loopback, and needs the workspace token (an
`<img>` carries it in its URL, like the gallery's media). The browser sends
only ids: a data folder's id (computed by the server from the rigs and
looked up again on every request), a run's path relative to that folder, a
file's name relative to the run. A run id must have a run folder's shape
(`[v<version>/]sub-<ID>/ses-<NNN>/run-<NN>…`), and every path is resolved,
symlinks included, and refused when it leaves its folder. Text is served
only for the records listed above; files only for images under `figures/`,
under a `sandbox` Content-Security-Policy so an SVG's script can never run
as the workspace.

The saved live monitor page is a self-contained file with inline script,
which the workspace's own policy forbids, and it reads `sessionStorage`,
which a sandboxed page cannot. So it is served under its own policy — inline
script and style, and nothing else: no requests, frames or forms — and
reached through a random link valid for two minutes, not the token: an
address opened in a tab is readable by the page and kept in the browser's
history, and the token must be in neither. The tab is opened with
`noopener`, so it gets nothing of the workspace's tab.

### Experiment figures (planned)

The quick plots are generic. An experiment will later be able to declare its
own analysis figures — functions of one or several run folders that return a
figure — and the Data view will show them in a card of their own beside the
quick plots. Because the workspace never imports experiment code, those
functions will run in the project's own interpreter, the way the parameter
schema is read today (`workspace._read_schema`), through a new route in
`workspace_data.py`; the view only lists and shows the images they produce.
The places to extend are marked in both files' header comments.

## Storage and local access

By default, state is under `~/.alhazen/live_monitor/`:

```text
projects.json
runs/<unique-id>/
  run.json          # the command, status — and subject, session, initials for a session
  params.yaml       # when supplied
  rig.yaml          # the rig as it ran (merged, when it extends a shared one)
  rig-source.yaml   # the experiment's file as written, when it extends one
  console.log
  media/
```

Use `--state-dir PATH` for a different workspace, `--port PORT` for a fixed
port, or `--no-browser` to print the URL without opening it. One server holds a
workspace at a time: a second `alhazen dashboard` on the same workspace is refused,
and the refusal names the process that has it and the address of its page, so you
can open that page, stop that process, or start another workspace with
`--state-dir`. The UI requires
no Node server, build step, external fonts or internet resources. Its assets
ship in the Python wheel.

The server binds to `127.0.0.1` only and authenticates API/media requests with
a random per-server token. The opening URL carries it in a fragment, which
the page removes and stores for reloads. Host and origin checks reject remote
web pages; media is limited to supported image/video files inside each run's
output directory, including symlink resolution. Byte-range responses allow
video seeking. Requests are limited to 1 MiB and the visible console is the
last 64 KiB; the complete log remains on disk. The gallery lists up to 500
artifacts per run.

Only add trusted local experiment repositories: launching one executes its
Python code with your account's permissions. This app is a local launcher,
not a remote multi-user service or a sandbox for untrusted experiments.
