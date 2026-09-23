"""Task: the one object an experiment writes.

Everything a session needs from an experiment — its name, its event
vocabulary, its outcomes, its params model, its reward policy, how it builds a
trial, what it schedules and what its subject reads before trial one — arrives
through one subclass instead of loose callables. ``build_session(task=...)``
reads it all from there, and so does every entry point that starts a session
(``alhazen run``, an experiment's ``run.py``), because the subclass is the one
thing all of them are handed.

The class attributes are declarations, checked once when the subclass is
defined rather than at the first trial: a task missing its outcomes is a
programming error the experimenter should meet while writing the file, not
with a subject waiting.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
from pydantic import BaseModel

from alhazen.config.models import Model
from alhazen.core.events import EventSchema
from alhazen.core.trial import OutcomeSet
from alhazen.dashboard.spec import DashboardSpec
from alhazen.errors import ConfigError
from alhazen.paradigms.base import Condition, TrialSource
from alhazen.paradigms.config import SchedulerConfig, make_scheduler
from alhazen.task.plan import TrialPlan, TrialSetup
from alhazen.task.reward_policy import RewardPolicy


class Task:
    """Subclass per experiment task.

    Required class attributes: ``name``, ``events``, ``outcomes``,
    ``params_model``. Optional: ``reward``, ``mid_trial_reward``. Required
    override: ``build_trial``. Everything else has a default that does the
    obvious thing for a single-condition task — except ``instructions``,
    whose default is "not declared", and which a run-mode session warns
    about until the task says what its subject reads (see that method).
    """

    name: ClassVar[str]
    events: ClassVar[EventSchema]
    outcomes: ClassVar[OutcomeSet]
    params_model: ClassVar[type[Model]]
    reward: ClassVar[RewardPolicy | None] = None
    # Whether a phase asks for juice while a trial runs
    # (``ctx.request_reward``), on top of what ``reward`` pays at its end.
    # Declared rather than discovered at the first drop: a session for such a
    # task is refused when it is built on a rig with no dispenser, instead of
    # failing minutes into the session with a subject waiting. And a request
    # from a task that did not declare it is a loud error, not a no-op.
    mid_trial_reward: ClassVar[bool] = False
    dashboard: ClassVar[DashboardSpec | None] = None

    # The params field a default make_source reads its scheduler from. A task
    # that schedules its own trials never needs one.
    paradigm_field: ClassVar[str] = "paradigm"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Checked for every subclass, abstract bases included, because a
        # shared base is exactly where a family of tasks would declare this
        # once (every monkey task in a package returning None, say). A hook
        # written as data rather than as a method — `instructions = "Look at
        # the dot"`, or `= None` — passes every other check here and then
        # fails with "'str' object is not callable" while a session is being
        # built, with the subject already in the chair. Refused now instead,
        # naming the method to write.
        if "instructions" in cls.__dict__ and not callable(cls.instructions):
            raise TypeError(
                f"task {cls.__name__} declares instructions = {cls.__dict__['instructions']!r}; "
                f"it must be a method — def instructions(self) -> str | None — returning "
                f"the text the subject reads, or None to declare that there is none"
            )
        # Abstract intermediate subclasses (a shared base for a family of
        # tasks) declare nothing and are not checked; a task is anything that
        # declares a name.
        if not hasattr(cls, "name"):
            return
        for attribute in ("events", "outcomes", "params_model"):
            if not hasattr(cls, attribute):
                raise TypeError(
                    f"task {cls.__name__} declares 'name' but not '{attribute}'; a task "
                    f"must declare name, events, outcomes and params_model"
                )
        if not isinstance(cls.mid_trial_reward, bool):
            # Only a real bool: `"no"` is truthy, so a string would switch
            # mid-trial reward on while reading as if it switched it off.
            raise TypeError(
                f"task {cls.__name__} declares mid_trial_reward = {cls.mid_trial_reward!r}; "
                f"it must be True or False"
            )
        if not cls.name.islower() or not all(c.isalnum() or c == "-" for c in cls.name):
            raise ValueError(
                f"task name {cls.name!r} must be lowercase alphanumeric/hyphen — it becomes "
                f"a filename segment"
            )

    def __init__(self, params: Model) -> None:
        if not isinstance(params, self.params_model):
            raise TypeError(
                f"{type(self).__name__} takes {self.params_model.__name__} params, got "
                f"{type(params).__name__}"
            )
        self.params = params

    # ------------------------------------------------------------------
    # What the session asks a task for
    # ------------------------------------------------------------------

    def conditions(self, rng: np.random.Generator) -> list[Condition]:
        """The task's condition cells. The default is one nameless condition —
        enough for a task whose trials do not vary."""
        return [Condition({})]

    def make_source(self, params: BaseModel, rng: np.random.Generator) -> TrialSource:
        """The scheduler for this session.

        The default reads a ``SchedulerConfig`` from the params (field name in
        ``paradigm_field``) and builds it over ``conditions()``. Override for
        scheduling a config cannot express.
        """
        paradigm = getattr(params, self.paradigm_field, None)
        if paradigm is None:
            paradigm = SchedulerConfig()
        if not isinstance(paradigm, SchedulerConfig):
            raise TypeError(
                f"{type(self).__name__}.params.{self.paradigm_field} must be a "
                f"SchedulerConfig, got {type(paradigm).__name__}"
            )
        return make_scheduler(
            paradigm,
            self.conditions(rng),
            rng,
            score=self.score_trial,
            # So a config error about the conditions names the task whose
            # conditions they are: an experimenter reading "not a full
            # factorial" needs to know which file to open.
            task_name=self.name,
        )

    def build_trial(self, setup: TrialSetup) -> TrialPlan:
        """The phases, stimuli and regions for one trial. The one method every
        task must write."""
        raise NotImplementedError(f"{type(self).__name__} must implement build_trial")

    # ------------------------------------------------------------------
    # What every session shows before trial one
    # ------------------------------------------------------------------

    def instructions(self) -> str | None:
        """The text the subject reads before trial one, or None if there is none.

        Every way of starting a session shows it — ``alhazen run --task``, an
        experiment's ``run.py`` (``alhazen.cli.modes.run_experiment``),
        ``build_mode_session`` and ``build_session(task=...)`` — because the
        task is the one thing all of them are handed. In ``run`` and ``test``
        modes the screen waits for SPACE (ESC cancels the session); in
        ``simulate`` it is shown and the session starts by itself, since
        nobody is there to press anything.

        Override it one of two ways, and both are answers:

        - return the text. Reading it from a file the experiment keeps under
          review (an ``instructions.md``) is the usual way, so what a subject
          reads cannot drift from what was agreed. The display reflows it:
          a single newline inside a paragraph becomes a space, a blank line
          separates paragraphs, an indented line keeps its break.
        - return None, to declare that this task has none — an animal
          subject, say. Declared on a shared base class, it covers every
          task that inherits it.

        Not overriding it is a third state, "not declared": the session shows
        nothing, exactly as it did before the task could say, and ``--mode
        run`` logs a WARNING naming this method, so a task that forgot is not
        mistaken for one that decided.

        Called once per session, after a curriculum has set the stage's
        params (so the text may depend on ``self.params``) and before the run
        directory exists (so a missing file fails before anything is written).
        An explicit ``instructions=`` passed to ``build_session`` or to
        ``run_experiment`` wins over this.
        """
        return None

    # ------------------------------------------------------------------
    # What the other modes ask a task for. All of these are optional: an
    # experiment that never demos its stimulus, never writes a movie of it,
    # or never rehearses without a subject simply does not answer, and the
    # mode says so plainly rather than improvising something that is not the
    # experiment.
    # ------------------------------------------------------------------

    def demo_views(self, setup: Any) -> list[Any]:
        """The displays ``alhazen run --mode demo`` pages through.

        Takes a ``modes.demo.DemoSetup`` — the display, the screen, the params
        and an rng — and returns a list of ``modes.demo.DemoView``. It gets
        the real display and the real pixel scale because the stimulus is the
        one thing in an experiment no test can check: a test can assert that
        dot k is where the formula says, not that a human sees a transparent
        cylinder, and that judgement is only worth anything if what is on
        screen is the literal stimulus rather than a redrawing of it.
        """
        raise NotImplementedError(
            f"{type(self).__name__} declares no demo views. Implement "
            f"demo_views(setup) returning a list of alhazen.modes.demo.DemoView "
            f"to use --mode demo."
        )

    def demo_controls(self, setup: Any) -> list[Any]:
        """Experiment-specific keys for the demo, as ``modes.demo.DemoControl``.

        The default is none: paging through the views and quitting are the
        viewer's own keys and are always there. This is for the toggles that
        only mean something to one experiment — a new random cloud of dots, a
        faster rotation, showing and hiding the target.
        """
        return []

    def movie_clips(self, setup: Any) -> list[Any]:
        """The files ``alhazen run --mode movie`` writes.

        Takes a ``modes.movie.MovieSetup`` — the screen geometry and refresh
        rate of the rig the movie previews, plus the params and an rng — and
        returns a list of ``modes.movie.MovieClip``, each naming one file and
        yielding its frames as numpy arrays, one per screen flip. The task
        composites the pixels because they are the experiment's own; the
        encoder, the scaling and the contact sheet are the mode's.
        """
        raise NotImplementedError(
            f"{type(self).__name__} declares no movie clips. Implement "
            f"movie_clips(setup) returning a list of alhazen.modes.movie.MovieClip "
            f"to use --mode movie."
        )

    def simulation(self, seed: int) -> Any:
        """The stand-ins for a subject in ``--mode simulate``, or None.

        Returns a ``modes.simulation.Simulation``. Seeded, so a whole
        simulated session replays exactly from the same number — which is
        what makes a rehearsal something you can debug.
        """
        return None

    def live_analysis(self, wiring: Any) -> Any:
        """The task's between-trials live analysis, or None (the default).

        Takes a ``task.live.LiveWiring`` — the spike source the rig config
        built (or None when it configures none), the screen and the session
        clock — and returns a ``task.live.LiveAnalysis``. The builder calls
        this once, after the devices are wired, and the runner then drives
        the returned object between trials: never inside the frame loop, so
        it can afford real computation (a receptive-field map, a PSTH) and
        contribute its own panels to the live dashboard.
        """
        return None

    def score(self, record: dict[str, Any]) -> dict[str, Any]:
        """Derived measures, computed by the experiment after the trial ends.
        The default adds nothing."""
        return record

    def score_trial(self, result: Any) -> bool:
        """Whether an adaptive scheduler should count this trial as a success.
        The default is the outcome's own ``success`` flag; a task titrating
        something else (a bias magnitude, a settling error) overrides it."""
        return bool(result.outcome.success)


# ----------------------------------------------------------------------
# How the framework reads the hooks above. Module functions rather than Task
# methods because an entry point may name a factory instead of a Task
# subclass (cli/tasks.py accepts anything with a params_model), and a factory
# has none of these attributes: it must read as "declares nothing", exactly
# like a task written before the hooks existed.
# ----------------------------------------------------------------------


def declares_instructions(task_class: Any) -> bool:
    """Whether ``task_class`` says what its subject reads — text, or None on purpose.

    True as soon as the class, or any base below ``Task``, overrides
    ``Task.instructions``, whatever the override returns: a task returning
    None has declared that it has no instructions, which is an answer. False
    only for a task that never answered, and that is the one a run-mode
    session warns about (``modes/session.py``). Told apart by identity, the
    way ``movie_clips`` is: the override's return value cannot say whether it
    was deliberate.
    """
    hook = getattr(task_class, "instructions", None)
    return hook is not None and hook is not Task.instructions


def task_instructions(task: Any) -> str | None:
    """The text ``task`` asks to show its subject before trial one, checked.

    None when the task returns None or declares nothing — both mean "no
    instruction screen". Anything else must be text with something in it:

    - a non-string (a ``Path`` returned instead of the file's contents is
      the likely one) is a programming error, raised as ``TypeError``;
    - an empty or whitespace-only string is refused with ``ConfigError``.
      Shown, it would be a blank screen waiting for SPACE with nothing to
      read, and the likeliest cause is an empty section in the file the text
      comes from — which should fail at the build, not in front of a subject.
      A task with nothing to show returns None, and says so.
    """
    hook = getattr(task, "instructions", None)
    if hook is None:
        return None
    text = hook()
    if text is None:
        return None
    name = type(task).__name__
    if not isinstance(text, str):
        raise TypeError(
            f"{name}.instructions() returned {type(text).__name__} {text!r}; it must return "
            f"the text to show the subject (a str), or None to declare that there is none"
        )
    if not text.strip():
        raise ConfigError(
            f"{name}.instructions() returned empty text. Shown, that is a blank screen "
            f"waiting for SPACE with nothing on it. If the text comes from a file, the part "
            f"it reads is empty; if this task has no instructions, return None instead."
        )
    return text
