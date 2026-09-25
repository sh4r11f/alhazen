# Experiment workspace

`alhazen dashboard` opens a local web app for managing downstream Alhazen
experiments. The launcher is separate from the [live session monitor](dashboard.md):
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
present, then its own interpreter. The project's `src/` and root are placed on
`PYTHONPATH`, alongside the running Alhazen installation.

## Configure and run

1. Select an experiment in the sidebar.
2. Choose a mode, rig and parameter preset. Rig files are discovered under
   `configs/rig*.yaml` (including subdirectories and `.yml`); parameter presets
   start with `task` or `params`.
3. Choose text parameters from dropdowns; text lists use dropdowns with
   checkboxes. Choices come from the task model's enums and defaults, keeping
   the current value available. Keyboard bindings offer common keys. Unbounded
   custom strings can still be entered through YAML. Numeric arrays use JSON
   notation in the fields editor. Search filters nested fields.
   **Task defaults** leaves parameter loading to the experiment's entry point.
   Measure rig hides the parameter preset and editor entirely, as do standalone
   scripts without a parameter-file option. Hidden task parameters are not sent
   to those jobs.
4. Set the mode's options and start the run. Run and test require a subject ID;
   simulate can use its own default subject. Only simulate accepts headless,
   and only test accepts mouse gaze.
5. Follow the console or view generated media. Images can be enlarged or saved.
   Movies appear once recording finishes, with native playback and seeking.

The six modes are **simulate**, **demo**, **movie**, **test**, **run** and
**measure**. Demo, test, run and measure still use a physical display and its
usual keyboard controls; the browser is not a replacement renderer. In demo,
use the viewer's screenshot control to send images to the run's gallery.
Movies use the task's `movie_clips` implementation and require the movie extra
in the selected interpreter. A mode a task has not implemented fails visibly
in its console, exactly as it would from `run.py`.

The launcher discovers standalone `src/<package>/preview.py` and `movie.py`
modules when they declare a literal `--out` argparse option and a `__main__`
entry point. **Preview images** runs Amodal's PNG generator; **Movie script**
runs its standalone recorder, which offers additional sheet options. The
launcher passes `--rig` and `--params` or `--task-config` when the script
supports them. Other script arguments can be entered in the UI. Scripts that
are only internal viewer helpers (such as KDE's preview module) are not
presented as runnable image generators; use demo or movie instead.

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

When an experiment prints its live monitor URL to the console, **Live monitor**
opens it. That monitor retains its own token and pause-only controls.

## Storage and local access

By default, state is under `~/.alhazen/dashboard/`:

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
port, or `--no-browser` to print the URL without opening it. The UI requires
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
