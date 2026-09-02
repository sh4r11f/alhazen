"""The pause screen: what the experimenter can do, listed on the display.

A paused session used to show one line of text naming three keys. That line
was written when three keys were all there was; a session now also has a
reward pump, a curriculum whose stage can be moved, and an eye tracker that
can be recalibrated, and none of those appeared on it. An experimenter who
cannot see that a control exists does not use it — so the pause screen is
built from what this session actually has wired, and says so.

Two groups, because they answer two different questions:

- **now** — the keys the pause screen itself accepts. Press one and something
  happens immediately.
- **during a trial** — the live command keys, shown as reference. They do
  nothing while paused, and the screen says so rather than listing them as if
  they were options. They are here because the pause screen is the one moment
  in a session when somebody has time to read, and "what was the key for a
  manual reward again?" is the question they most often have.

Both groups are derived from the real keymap (`core.commands.DEFAULT_KEYMAP`)
rather than written out again here, so a rig that rebinds a key sees its own
binding on screen. A menu that can disagree with the keyboard is worse than no
menu, because it is believed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from alhazen._deprecation import deprecated
from alhazen.core.commands import DEFAULT_KEYMAP, Command

# Orange, in PsychoPy's -1..1 RGB space (0-1 value v maps to 2v-1). Roughly
# #FF9426. The pause screen is the one screen that must never be mistaken for
# a running session at a glance across the room — a session is grey on grey,
# so the pause is warm and saturated. It is also not red: red is for a fault,
# and most pauses are the experimenter choosing to pause.
PAUSE_COLOR = (1.0, 0.16, -0.70)

# The fault case, for a pause nobody asked for (a reward failure). Same screen,
# same keys, different heading and colour, because "the pump did not fire" and
# "I pressed P" want different reactions from whoever looks up.
FAULT_COLOR = (1.0, -0.30, -0.55)


@dataclass(frozen=True)
class MenuItem:
    """One row: the key to press, what it does, and the action it triggers.

    ``action`` is empty for a reference row — a key that is real but does
    nothing from the pause screen.
    """

    key: str
    label: str
    action: str = ""


@dataclass(frozen=True)
class PauseMenu:
    title: str
    subtitle: str
    now: list[MenuItem]
    in_trial: list[MenuItem] = field(default_factory=list)
    color: tuple[float, float, float] = PAUSE_COLOR

    def actions(self) -> dict[str, str]:
        """Key -> action, for the polling loop. Reference rows are excluded,
        so a key that is only listed can never trigger anything."""
        return {item.key: item.action for item in self.now if item.action}

    def render(self) -> str:
        """The menu as the block of text a display draws.

        Keys are padded to a common width so the labels line up in a column;
        an unaligned list of eight keys reads as a paragraph, and the point of
        the screen is that it can be scanned in a second.
        """
        rows = [item for item in (*self.now, *self.in_trial) if item.key]
        width = max((len(item.key) for item in rows), default=0)
        lines = [self.subtitle, ""] if self.subtitle else []
        lines += [f"{item.key:<{width}}   {item.label}" for item in self.now]
        if self.in_trial:
            lines += ["", "during a trial (not now)"]
            lines += [f"{item.key:<{width}}   {item.label}" for item in self.in_trial]
        return "\n".join(lines)


# How a key is spelled on screen. PsychoPy names keys the way pyglet does,
# which is right for code and wrong for a person reading a menu across a room.
KEY_LABELS = {
    "space": "SPACE",
    "escape": "ESC",
    "bracketright": "]",
    "bracketleft": "[",
    "ctrl+c": "CTRL-C",
}


def key_label(key: str) -> str:
    """A key name as it should appear on screen."""
    return KEY_LABELS.get(key, key.upper())


# What each live command does, in the experimenter's words rather than the
# enum's. Keyed by Command so a rebound key still gets the right description.
COMMAND_LABELS = {
    Command.SKIP_TRIAL: "abandon this trial and go to the next",
    Command.PAUSE: "pause",
    Command.CALIBRATE: "pause and recalibrate the eye tracker",
    Command.QUIT: "end the session (data so far is saved)",
    Command.MANUAL_REWARD: "deliver one reward now",
    Command.PROMOTE_STAGE: "move the subject up a training stage",
    Command.DEMOTE_STAGE: "move the subject down a training stage",
    Command.HOLD_STAGE: "hold the training stage where it is",
}

# Which session component each live command needs to do anything. A key whose
# component is absent is not listed at all: an experimenter who presses a
# listed key and gets nothing cannot tell whether the key or the pump is
# broken, and will spend the session's break finding out.
COMMAND_NEEDS: dict[Command, str | None] = {
    Command.SKIP_TRIAL: None,  # always available
    Command.MANUAL_REWARD: "reward",
    Command.PROMOTE_STAGE: "training",
    Command.DEMOTE_STAGE: "training",
    Command.HOLD_STAGE: "training",
}

# The pause-screen action each live command duplicates, where it has one. A
# command already in the "now" group is not listed twice: same key, same
# effect, and two rows would suggest the two presses differ.
COMMAND_ACTIONS = {
    Command.MANUAL_REWARD: "manual_reward",
    Command.PROMOTE_STAGE: "promote_stage",
    Command.DEMOTE_STAGE: "demote_stage",
    Command.HOLD_STAGE: "hold_stage",
}

# Listed in this order when they survive both filters. PAUSE never appears —
# you are reading this because you pressed it — and in a default session the
# two filters leave exactly one row: ESC. That row is the most useful line on
# the screen, because ESC means two different things a second apart (end the
# session from here, abandon one trial from a trial) and nothing else says so.
IN_TRIAL_COMMANDS = (
    Command.SKIP_TRIAL,
    Command.MANUAL_REWARD,
    Command.PROMOTE_STAGE,
    Command.DEMOTE_STAGE,
    Command.HOLD_STAGE,
)


def build_pause_menu(
    *,
    has_tracker: bool = False,
    has_reward: bool = False,
    has_training: bool = False,
    has_dashboard: bool = False,
    fault: str | None = None,
    keymap: dict[str, Command] | None = None,
) -> PauseMenu:
    """The menu for THIS session — only the controls it actually has.

    A rig with no pump does not list a reward key, and a session with no
    curriculum does not list the stage keys, for the reason in COMMAND_NEEDS.
    Same for calibration without a tracker.

    ``fault`` turns this into the involuntary-pause screen: a different colour
    and a heading naming what went wrong, with the same controls underneath —
    whoever looks up needs to know why the session stopped before they need to
    know which key resumes it.
    """
    keymap = DEFAULT_KEYMAP if keymap is None else keymap
    present = {"reward": has_reward, "training": has_training}

    now = [MenuItem("SPACE", "resume", "resume")]
    if has_tracker:
        # The three eye-tracker procedures (session/eyetracker.py). Listed
        # together because they are chosen together: a validation that fails
        # is answered with a recalibration, a small offset with a drift
        # correction, and the experimenter picks between them here.
        now += [
            MenuItem("C", "recalibrate the eye tracker", "calibrate"),
            MenuItem("V", "validate the calibration", "validate"),
            MenuItem("D", "drift-correct the eye tracker", "drift_correct"),
        ]
    if has_reward:
        now.append(MenuItem("R", "deliver one reward", "manual_reward"))
    if has_training:
        now += [
            MenuItem("]", "move up a training stage", "promote_stage"),
            MenuItem("[", "move down a training stage", "demote_stage"),
            MenuItem("H", "hold the training stage", "hold_stage"),
        ]
    now.append(MenuItem("Q or ESC", "end the session (data so far is saved)", "quit"))

    # The live keys, read out of the map so a rebind shows up here, minus the
    # ones this session cannot act on and the ones already offered above.
    offered = {item.action for item in now}
    bindings: dict[Command, list[str]] = {}
    for key, command in keymap.items():
        bindings.setdefault(command, []).append(key_label(key))
    in_trial = []
    for command in IN_TRIAL_COMMANDS:
        needs = COMMAND_NEEDS[command]
        if command not in bindings:
            continue  # this rig's keymap does not bind it at all
        if needs is not None and not present[needs]:
            continue  # the component it acts on is not in this session
        if COMMAND_ACTIONS.get(command) in offered:
            continue  # already a row in the "now" group
        in_trial.append(
            MenuItem(" or ".join(sorted(set(bindings[command]))), COMMAND_LABELS[command])
        )

    subtitle = "the session is paused — nothing is being recorded"
    if has_dashboard:
        subtitle += "\nthe dashboard's buttons are live too"
    return PauseMenu(
        title=fault if fault else "PAUSED",
        subtitle=subtitle,
        now=now,
        in_trial=in_trial,
        color=FAULT_COLOR if fault else PAUSE_COLOR,
    )


def run_pause_menu(
    menu: PauseMenu,
    show: Callable[[PauseMenu], None],
    raw_keys: Callable[[], list[str]],
    wait: Callable[[float], None],
) -> str:
    """Draw the menu and block until the experimenter picks something.

    Returns the chosen action ("resume", "quit", "calibrate", ...). The caller
    decides what each one means; this only reads the keyboard, so the same
    loop serves a rig, a test with a scripted key source, and the dashboard
    path.

    The short wait keeps an idle pause from spinning a core — a session can
    sit here for minutes while somebody fetches the experimenter.
    """
    show(menu)
    actions = menu.actions()
    # Q and ESC share one row on screen, so the row's key text ("Q or ESC") is
    # not a key name. Map the real key names here instead.
    lookup = {key.lower(): action for key, action in actions.items()}
    lookup.update({"q": "quit", "escape": "quit", "space": "resume"})
    while True:
        for key in raw_keys():
            action = lookup.get(key.lower())
            if action is not None:
                return action
        wait(0.01)


@deprecated(since="1.1", removed_in="1.2", instead="build_pause_menu with run_pause_menu")
def pause_menu(
    show_message: Callable[[str], None],
    raw_keys: Callable[[], list[str]],
    wait: Callable[[float], None],
) -> str:
    """The pre-1.1 pause loop: three fixed keys drawn through ``show_message``.

    Kept working because it is public API and an experiment package may call
    it. It builds the default menu and renders it as plain text, so a caller
    that has not moved yet gets the new wording through the old seam — but
    not the colour, and not the controls that depend on what a session has
    wired, because a ``show_message`` callable cannot express either.
    """
    # has_tracker=True because the old three-key menu always offered
    # calibrate, whether or not a tracker was wired. A caller still on this
    # seam must keep getting "calibrate" back for 'c' — silently dropping the
    # key would hang them here, since this loop only returns on a match.
    menu = build_pause_menu(has_tracker=True)
    return run_pause_menu(
        menu,
        lambda m: show_message(f"{m.title}\n\n{m.render()}"),
        raw_keys,
        wait,
    )
