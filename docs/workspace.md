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
cannot, and records the alhazen and Python versions it found.

## Configure and run

1. Select an experiment in the sidebar.
2. Choose a mode, rig and parameter preset. Rig files are discovered under
   `configs/rig*.yaml` (including subdirectories and `.yml`); parameter presets
   start with `task` or `params`.
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
   the run. Run and test require a subject ID; simulate can use its own default
   subject. Only simulate accepts headless, and only test accepts mouse gaze.
5. Follow the console or view generated media. Images can be enlarged or saved.
   Movies appear once recording finishes, with native playback and seeking.

The six modes are **simulate**, **demo**, **movie**, **test**, **run** and
**measure**. Demo, test, run and measure still use a physical display and its
usual keyboard controls; the browser is not a replacement renderer. In demo,
use the viewer's screenshot control to send images to the run's gallery.
Movies use the task's `movie_clips` implementation and require the movie extra
in the selected interpreter. A mode a task has not implemented fails visibly
in its console, exactly as it would from `run.py`.

**Extra run.py arguments** go to the experiment's entry point after the
launcher's own flags, in every mode. The field is split like a shell command
line: quote an argument that contains spaces, and write paths with forward
slashes, since a backslash escapes the character after it. It is how an
experiment that ships several tasks is launched — such a `run.py` reads its
own `--task <name>` from the command line and hands the rest to
`run_experiment`, so `--task mib-detect` selects the task — and how a runner
flag the form has no control for is given: `--curriculum configs/shaping.yaml`,
`--run 3`, or measure mode's `--skip`. An extra argument naming a flag the
launcher sets from the form is refused, naming the flag, before anything is
written, so a run's recorded settings cannot be contradicted from the text
field. Those flags are `--mode`, `--rig`, `--params`, `--seed`,
`--no-live-monitor-browser`, `--sub`, `--ses`, `--trials-per-condition`,
`--headless`, `--mouse`, `--windowed`, `--out`, `--scale`, `--sheet`,
`--columns`, `--clip` and `--screenshots`, in either the `--seed 5` or the
`--seed=5` spelling; every other flag passes through.

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
in a separate process using the project's selected Python interpreter. This
imports the task but does not execute the `run.py` main block or start a session.

Each launch writes an immutable parameter file when parameters were supplied,
a copy of the rig, the actual argument list and working directory, console
output, and its own media folder. Source configuration files are never edited.
The experiment's parameter model still validates values before a session
starts, so unsupported values produce the same errors as the CLI. The rig
passed to the process stays at its original path to preserve relative-path
semantics. Session data retains the experiment's normal real/rehearsal paths.

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
shows the same fact as **live monitor: on/off**.

The monitor's server closes with the session. Once the run has finished, the
frame is emptied and replaced by a note: the monitor's final state was saved in
the run's data directory as `figures/dashboard.html` (and
`figures/dashboard_state.json`), which opens on its own with no server. A
browser error page for a server that no longer exists is never left in the tab.

Embedding needs the monitor to allow being framed, so its responses carry
`frame-ancestors 'self' http://127.0.0.1:* http://localhost:*` in their
Content-Security-Policy: local pages may frame it, nothing else may. The frame
is sandboxed (`allow-scripts allow-same-origin allow-downloads allow-modals`):
the monitor page runs its own script against its own server, can save a figure
and show a dialog, and cannot navigate the workspace or open windows from it.
Running `python run.py` from a terminal is unchanged: the monitor then opens in
its own browser tab as before.

## Storage and local access

By default, state is under `~/.alhazen/live_monitor/`:

```text
projects.json
runs/<unique-id>/
  run.json
  params.yaml       # when supplied
  rig.yaml
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
