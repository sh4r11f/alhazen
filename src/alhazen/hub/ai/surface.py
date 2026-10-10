"""The runtime API a generated task may use: listed for the provider, and
checked in the code it writes.

One module owns this knowledge both ways, so the prompt and the check cannot
disagree. Everything is read from the running alhazen by introspection
(signatures, type hints, dataclass and model fields, docstrings, and the
``self.<name> =`` assignments in alhazen's own source); nothing here imports
or runs generated code.

The check follows types through a generated module the way a reader would:
from what alhazen hands a task (``build_trial(setup)`` gets a TrialSetup,
``demo_views(setup)`` a DemoSetup, a phase callback's ``ctx`` a
TrialContext), through attributes and return annotations, assignments,
annotated variables, and the constructors of classes the module defines
itself (``Flash(setup.display)`` makes ``self.display`` in ``Flash`` a
DisplayBackend). Every attribute read on an alhazen type, and every keyword
passed to an alhazen callable, must exist on the real class or function.
Where a type cannot be followed, nothing is claimed. PsychoPy is not
checked: the hub does not install it.
"""

from __future__ import annotations

import ast
import builtins
import dataclasses
import difflib
import functools
import importlib
import inspect
import textwrap
import types
import typing
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

# What alhazen passes to each Task hook, by hook and parameter name: the
# documented calls in alhazen.task.task / alhazen.modes (test_ai_surface holds
# these to the real call sites' classes).
HOOK_ARGUMENTS: dict[str, dict[str, str]] = {
    "build_trial": {"setup": "alhazen.task.plan:TrialSetup"},
    "demo_views": {"setup": "alhazen.modes.demo:DemoSetup"},
    "demo_controls": {"setup": "alhazen.modes.demo:DemoSetup"},
    "demo_stimulus_extent": {"setup": "alhazen.modes.demo:DemoSetup"},
    "movie_clips": {"setup": "alhazen.modes.movie:MovieSetup"},
    "trial_timing": {"condition": "alhazen.paradigms.base:Condition"},
}
# A callback parameter by this name is what phases hand their callbacks.
CONVENTIONAL_NAMES: dict[str, str] = {"ctx": "alhazen.core.trial:TrialContext"}

# The listing, in reading order: (title, [(module, names or () for __all__)]).
_LISTING: tuple[tuple[str, tuple[tuple[str, tuple[str, ...]], ...]], ...] = (
    (
        "Objects alhazen hands a task",
        (
            ("alhazen.task.plan", ("TrialSetup",)),
            ("alhazen.modes.demo", ("DemoSetup",)),
            ("alhazen.modes.movie", ("MovieSetup",)),
            ("alhazen.core.trial", ("TrialContext", "InputFrame", "CircleRegion")),
            ("alhazen.display.backend", ("DisplayBackend",)),
            ("alhazen.display.screen", ("Screen",)),
            ("alhazen.paradigms.base", ("Condition",)),
            ("alhazen.config.models", ("Duration",)),
        ),
    ),
    (
        "Stimuli (draw only through these; the display has no drawing methods)",
        (
            ("alhazen.stimuli.base", ("Stimulus", "NullStimulus")),
            ("alhazen.stimuli.fixation", ("make_fixation", "FixationPoint")),
        ),
    ),
    ("Trial phases (alhazen.task.phases)", (("alhazen.task.phases", ()),)),
    (
        "Declaring a task",
        (
            ("alhazen", ("Task", "TrialPlan", "outcomes", "SubjectParams", "Model")),
            ("alhazen.core.events", ("EventSchema",)),
            ("alhazen.paradigms.config", ("SchedulerConfig",)),
            ("alhazen", ("RewardPolicy", "RewardPulses")),
        ),
    ),
    (
        "Other modes",
        (
            ("alhazen.modes.demo", ("DemoView",)),
            ("alhazen.modes.movie", ("MovieClip",)),
            ("alhazen.modes.simulation", ("Simulation",)),
            ("alhazen.devices.automated", ("AutomatedGazeTracker",)),
        ),
    ),
)
_TASK_HOOKS = (
    "default_params",
    "instructions",
    "conditions",
    "build_trial",
    "demo_views",
    "movie_clips",
    "simulation",
)
_MAX_LISTED_MEMBERS = 12


def resolve(reference: str) -> Any:
    """``module:Name`` (or ``module``) to the object."""
    module_name, _, name = reference.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, name) if name else module


def _alhazen(obj: Any) -> bool:
    return (getattr(obj, "__module__", "") or "").split(".")[0] == "alhazen"


def _summary(obj: Any) -> str:
    doc = inspect.getdoc(obj) or ""
    return doc.split("\n\n")[0].replace("\n", " ").strip()[:240]


def _signature(obj: Any) -> str:
    try:
        return str(inspect.signature(obj))
    except (TypeError, ValueError):
        return "(...)"


# ---------------------------------------------------------------------------
# Members of a real class
# ---------------------------------------------------------------------------


@functools.cache
def _self_assigned(cls: type) -> frozenset[str]:
    """Names alhazen's own methods assign on ``self`` in ``cls`` (read from
    its source; it is trusted code)."""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
    except (OSError, TypeError, SyntaxError):
        return frozenset()
    names = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            names.add(node.attr)
    return frozenset(names)


@functools.cache
def members(cls: type) -> frozenset[str]:
    """Every attribute an instance of ``cls`` has: what every class in its
    MRO defines (an Enum member's ``value``, a model's ``model_dump``), the
    annotations, the fields, and the ``self.<name> =`` assignments in
    alhazen's own methods."""
    names: set[str] = set()
    for klass in cls.__mro__:
        if klass is object:
            continue
        names.update(vars(klass))
        names.update(getattr(klass, "__annotations__", {}))
        if _alhazen(klass):
            names.update(_self_assigned(klass))
    if issubclass(cls, BaseModel):
        names.update(getattr(cls, "model_fields", {}))
    if dataclasses.is_dataclass(cls):
        names.update(f.name for f in dataclasses.fields(cls))
    names.discard("__annotations__")
    return frozenset(names)


def public_members(cls: type) -> list[str]:
    return sorted(
        name
        for name in members(cls)
        if not name.startswith("_") and (name in vars(cls) or _alhazen(cls))
    )


@functools.cache
def _hints(obj: Any) -> dict[str, Any]:
    try:
        return typing.get_type_hints(obj)
    except Exception:  # noqa: BLE001 - unresolvable hints mean "type unknown", nothing more
        return {}


def _as_type(hint: Any) -> Any:
    """The alhazen class a type hint names, or a ('dict', value type) for
    a mapping; None when it names nothing checkable."""
    if isinstance(hint, type):
        return hint if _alhazen(hint) else None
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)
    if origin is typing.ClassVar and args:
        return _as_type(args[0])
    if origin in (typing.Union, types.UnionType):
        found = [t for t in (_as_type(a) for a in args if a is not type(None)) if t is not None]
        return found[0] if len(found) == 1 else None
    if origin is dict and len(args) == 2:
        inner = _as_type(args[1])
        return ("dict", inner) if inner is not None else None
    return None


def member_type(cls: type, name: str) -> Any:
    """What reading ``name`` on an instance of ``cls`` gives, as far as
    alhazen's annotations say."""
    for klass in cls.__mro__:
        if not _alhazen(klass):
            continue
        if name in getattr(klass, "__annotations__", {}):
            return _as_type(_hints(klass).get(name))
        value = vars(klass).get(name)
        if isinstance(value, property) and value.fget is not None:
            return _as_type(_hints(value.fget).get("return"))
        if callable(value) or isinstance(value, (staticmethod, classmethod)):
            return ("method", cls, name)
    if issubclass(cls, BaseModel) and name in getattr(cls, "model_fields", {}):
        return _as_type(cls.model_fields[name].annotation)
    return None


def _return_type(obj: Any) -> Any:
    if inspect.isclass(obj):
        return obj if _alhazen(obj) else None
    return _as_type(_hints(obj).get("return"))


# ---------------------------------------------------------------------------
# The listing for the provider
# ---------------------------------------------------------------------------


def _listed_members(cls: type) -> list[str]:
    """Public names alhazen itself defines on ``cls`` (pydantic's own API is
    accepted by the check but not listed)."""
    names: set[str] = set()
    for klass in cls.__mro__:
        if _alhazen(klass):
            names.update(vars(klass))
            names.update(getattr(klass, "__annotations__", {}))
            names.update(_self_assigned(klass))
    return sorted(name for name in names if not name.startswith("_"))


def _plain(text: str) -> str:
    return text.replace("'", "")


def _describe_class(cls: type) -> list[str]:
    phase = cls.__module__.startswith("alhazen.task.phases")
    if phase or issubclass(cls, BaseModel):
        return [f"- class {cls.__name__}{_plain(_signature(cls))}", f"  {_summary(cls)}"]
    if dataclasses.is_dataclass(cls):
        lines = [f"- class {cls.__name__}  {_summary(cls)}"]
    else:
        lines = [f"- class {cls.__name__}{_plain(_signature(cls))}", f"  {_summary(cls)}"]
    hints: dict[str, Any] = {}
    for klass in reversed(cls.__mro__):
        if _alhazen(klass):
            hints.update(_hints(klass))
    for name in _listed_members(cls):
        value = inspect.getattr_static(cls, name, None)
        if isinstance(value, (staticmethod, classmethod)):
            value = value.__func__
        if inspect.isfunction(value):
            lines.append(f"  .{name}{_plain(_signature(value))}  {_summary(value)[:160]}")
        elif isinstance(value, property):
            ret = _hints(value.fget).get("return", "") if value.fget else ""
            lines.append(f"  .{name}: {getattr(ret, '__name__', ret)}  {_summary(value)[:160]}")
        elif name in hints:
            hint = hints[name]
            shown = getattr(hint, "__name__", None) or str(hint).replace("typing.", "")
            lines.append(f"  .{name}: {shown.replace('alhazen.core.trial.', '')}")
        else:
            lines.append(f"  .{name}")
    return lines


def api_text() -> str:
    """The API a task may use, read from the running alhazen. Anything not
    listed does not exist for the task."""
    out = [
        "Everything below is read from the installed alhazen. Use only these names, "
        "attributes and keyword arguments: anything else does not exist and fails at run time.",
    ]
    for title, sources in _LISTING:
        out.append(f"\n## {title}")
        for module_name, wanted in sources:
            try:
                module = importlib.import_module(module_name)
            except ImportError as error:
                out.append(f"- {module_name}: not importable here ({error.name})")
                continue
            names = wanted or tuple(getattr(module, "__all__", ()))
            for name in names:
                obj = getattr(module, name)
                if inspect.isclass(obj) and name == "Task":
                    out.append(f"- class Task  {_summary(obj)}")
                    out.append(
                        "  class attributes: name, events, outcomes, params_model; "
                        "instance attributes: params, reward"
                    )
                    for hook in _TASK_HOOKS:
                        member = getattr(obj, hook)
                        out.append(f"  .{hook}{_signature(member)}  {_summary(member)}")
                    for hook, arguments in HOOK_ARGUMENTS.items():
                        given = ", ".join(f"{k}: {v.split(':')[1]}" for k, v in arguments.items())
                        out.append(f"  {hook} receives {given}")
                elif inspect.isclass(obj):
                    out.extend(_describe_class(obj))
                else:
                    out.append(f"- {name}{_plain(_signature(obj))}  {_summary(obj)}")
    fixation = importlib.import_module("alhazen.stimuli.fixation")
    out.append(
        "\n## How alhazen draws a stimulus (alhazen.stimuli.fixation, verbatim)\n"
        "A stimulus builds its PsychoPy visual on `display.window` in its constructor, "
        "importing psychopy there; a make_ factory returns a NullStimulus on the "
        "simulated display. Write any new shape the same way.\n```python\n"
        + inspect.getsource(fixation).split('"""', 2)[2].strip()
        + "\n```"
    )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

_ROOT_BUILTINS = frozenset(dir(builtins))


@dataclass
class _UserClass:
    name: str
    node: ast.ClassDef
    bases: list[Any] = field(default_factory=list)  # real classes or user names
    open: bool = False  # a base we cannot see into: claim nothing
    names: set[str] = field(default_factory=set)
    attr_types: dict[str, Any] = field(default_factory=dict)
    init_args: dict[str, Any] = field(default_factory=dict)


def _scope_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Descendants of ``node`` in its own scope: nested functions, lambdas
    and classes are yielded but not entered."""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            yield from _scope_nodes(child)


def _closest(name: str, options: list[str]) -> str:
    close = difflib.get_close_matches(name, options, n=3, cutoff=0.5)
    if close:
        return "; closest: " + ", ".join(close)
    shown = options[:_MAX_LISTED_MEMBERS]
    more = ", ..." if len(options) > len(shown) else ""
    return "; it has: " + ", ".join(shown) + more


class _Checker:
    """One generated module, typed as far as its alhazen values can be
    followed."""

    def __init__(self, path: str, tree: ast.Module, source: str) -> None:
        self.path = path
        self.tree = tree
        self.lines = source.splitlines()
        self.is_test = path.startswith("tests/") or "/tests/" in path
        self.problems: list[str] = []
        self.globals: dict[str, Any] = {}
        self.classes: dict[str, _UserClass] = {}
        self.parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent

    # -- names ----------------------------------------------------------

    def _imports(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and not node.level and node.module:
                if node.module.split(".")[0] != "alhazen" or node.module.startswith("alhazen.cli"):
                    continue
                try:
                    module = importlib.import_module(node.module)
                except ImportError:
                    continue
                for alias in node.names:
                    value = getattr(module, alias.name, None)
                    if value is None:
                        try:
                            value = importlib.import_module(f"{node.module}.{alias.name}")
                        except ImportError:
                            continue
                    self.globals[alias.asname or alias.name] = ("value", value)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "alhazen" and alias.asname:
                        try:
                            self.globals[alias.asname] = (
                                "value",
                                importlib.import_module(alias.name),
                            )
                        except ImportError:
                            continue

    def _collect_classes(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ClassDef):
                self.classes.setdefault(node.name, _UserClass(node.name, node))

    def _resolve_bases(self) -> None:
        for user in self.classes.values():
            user.bases.clear()
            user.open = False
            for base in user.node.bases:
                kind = self.type_of(base, self.globals)
                if kind is not None and kind[0] == "value" and inspect.isclass(kind[1]):
                    if _alhazen(kind[1]):
                        user.bases.append(kind[1])
                    elif kind[1] is object:
                        continue
                    else:
                        user.open = True
                elif kind is not None and kind[0] == "class":
                    user.bases.append(kind[1])
                else:
                    user.open = True
            for item in user.node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    user.names.add(item.name)
                elif isinstance(item, ast.Assign):
                    user.names.update(t.id for t in item.targets if isinstance(t, ast.Name))
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    user.names.add(item.target.id)
                    annotated = self._annotation(item.annotation)
                    if annotated is not None:
                        user.attr_types[item.target.id] = annotated
            for node in ast.walk(user.node):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.ctx, ast.Store)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "self"
                ):
                    user.names.add(node.attr)
                if isinstance(node, ast.FunctionDef) and node.name in (
                    "__getattr__",
                    "__getattribute__",
                ):
                    user.open = True

    # -- types ----------------------------------------------------------

    def _annotation(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                node = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return None
        kind = self.type_of(node, self.globals)
        if kind is None:
            return None
        if kind[0] == "value" and inspect.isclass(kind[1]) and _alhazen(kind[1]):
            return ("instance", kind[1])
        if kind[0] == "class":
            return ("user", kind[1])
        return None

    def _instance_member(self, kind: Any, name: str) -> Any:
        """Type of ``<instance>.name``, or None."""
        if kind[0] == "instance":
            found = member_type(kind[1], name)
            return self._wrap(found)
        if kind[0] == "user":
            user = self.classes.get(kind[1])
            if user is None:
                return None
            if name in user.attr_types:
                return user.attr_types[name]
            if name in user.names:
                return ("method", kind, name) if self._is_method(user, name) else None
            for base in user.bases:
                if isinstance(base, str):
                    found = self._instance_member(("user", base), name)
                else:
                    found = self._wrap(member_type(base, name))
                if found is not None:
                    return found
        return None

    def _is_method(self, user: _UserClass, name: str) -> bool:
        return any(
            isinstance(item, ast.FunctionDef) and item.name == name for item in user.node.body
        )

    @staticmethod
    def _wrap(found: Any) -> Any:
        if found is None:
            return None
        if isinstance(found, tuple):
            if found[0] == "dict":
                inner = found[1]
                return ("dict", ("instance", inner) if isinstance(inner, type) else inner)
            if found[0] == "method":
                return ("method", ("instance", found[1]), found[2])
            return found
        return ("instance", found)

    def type_of(self, node: ast.AST, env: dict[str, Any]) -> Any:
        """('value', obj) | ('instance', cls) | ('user', name) | ('class', name)
        | ('method', owner, name) | ('dict', value type) | None."""
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            if node.id in self.classes:
                return ("class", node.id)
            return self.globals.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self.type_of(node.value, env)
            if base is None:
                return None
            if base[0] == "value":
                value = getattr(base[1], node.attr, None)
                return ("value", value) if value is not None else None
            if base[0] in ("instance", "user"):
                return self._instance_member(base, node.attr)
            return None
        if isinstance(node, ast.Subscript):
            base = self.type_of(node.value, env)
            if base is not None and base[0] == "dict":
                return base[1]
            return None
        if isinstance(node, ast.Call):
            func = self.type_of(node.func, env)
            if func is None:
                return None
            if func[0] == "value":
                found = _return_type(func[1])
                return self._wrap(found)
            if func[0] == "class":
                return ("user", func[1])
            if func[0] == "method":
                owner, name = func[1], func[2]
                if owner[0] == "instance":
                    method = inspect.getattr_static(owner[1], name, None)
                    if isinstance(method, (staticmethod, classmethod)):
                        method = method.__func__
                    return self._wrap(_return_type(method)) if method is not None else None
            return None
        return None

    # -- scopes ---------------------------------------------------------

    def _function_env(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
        outer: dict[str, Any],
        owner: _UserClass | None,
    ) -> dict[str, Any]:
        env = dict(outer)
        args = node.args
        positional = [*args.posonlyargs, *args.args]
        is_method = owner is not None and not isinstance(node, ast.Lambda)
        decorators = {d.id for d in getattr(node, "decorator_list", []) if isinstance(d, ast.Name)}
        hook = (
            HOOK_ARGUMENTS.get(node.name, {})
            if is_method
            and owner is not None
            and self._is_task(owner)
            and not isinstance(node, ast.Lambda)
            else {}
        )
        for index, arg in enumerate([*positional, *args.kwonlyargs]):
            if is_method and index == 0 and "staticmethod" not in decorators:
                if "classmethod" in decorators:
                    env[arg.arg] = ("class", owner.name)  # type: ignore[union-attr]
                else:
                    env[arg.arg] = ("user", owner.name)  # type: ignore[union-attr]
                continue
            known = None
            if arg.annotation is not None:
                known = self._annotation(arg.annotation)
            if known is None and arg.arg in hook:
                known = ("instance", resolve(hook[arg.arg]))
            if known is None and owner is not None and getattr(node, "name", "") == "__init__":
                known = owner.init_args.get(arg.arg)
            if (
                known is None
                and arg.annotation is None
                and not self.is_test
                and arg.arg in CONVENTIONAL_NAMES
            ):
                known = ("instance", resolve(CONVENTIONAL_NAMES[arg.arg]))
            if known is not None:
                env[arg.arg] = known
            else:
                env.pop(arg.arg, None)
        return env

    def _is_task(self, user: _UserClass) -> bool:
        task = resolve("alhazen.task.task:Task")
        return any(
            (isinstance(base, type) and issubclass(base, task))
            or (
                isinstance(base, str) and base in self.classes and self._is_task(self.classes[base])
            )
            for base in user.bases
        )

    def _walk_scope(
        self, scope: ast.AST, env: dict[str, Any], owner: _UserClass | None, check: bool
    ) -> None:
        nodes = list(_scope_nodes(scope))
        for node in nodes:  # assignments first, in order: the env the scope ends with
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                kind = self.type_of(node.value, env)
                if isinstance(target, ast.Name):
                    if kind is not None:
                        env[target.id] = kind
                    else:
                        env.pop(target.id, None)
                elif (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in env
                    and env[target.value.id][0] == "user"
                    and kind is not None
                ):
                    user = self.classes.get(env[target.value.id][1])
                    if user is not None:
                        user.attr_types.setdefault(target.attr, kind)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                kind = self._annotation(node.annotation)
                if kind is None and node.value is not None:
                    kind = self.type_of(node.value, env)
                if kind is not None:
                    env[node.target.id] = kind
            elif isinstance(node, ast.Call):
                self._bind_constructor(node, env)
        if check:
            for node in nodes:
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                    self._check_attribute(node, env)
                elif isinstance(node, ast.Call):
                    self._check_keywords(node, env)
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                inner = self._function_env(
                    node, env, owner if not isinstance(node, ast.Lambda) else None
                )
                if isinstance(node, ast.Lambda):
                    inner = self._function_env(node, env, None)
                self._walk_scope(node, inner, None, check)
            elif isinstance(node, ast.ClassDef):
                user = self.classes[node.name]
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        inner = self._function_env(item, env, user)
                        self._walk_scope(item, inner, None, check)
                    elif check:
                        for sub in ast.walk(item):
                            if isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Load):
                                self._check_attribute(sub, env)

    def _bind_constructor(self, call: ast.Call, env: dict[str, Any]) -> None:
        func = self.type_of(call.func, env)
        if func is None or func[0] != "class":
            return
        user = self.classes[func[1]]
        init = next(
            (i for i in user.node.body if isinstance(i, ast.FunctionDef) and i.name == "__init__"),
            None,
        )
        if init is None:
            return
        params = [a.arg for a in [*init.args.posonlyargs, *init.args.args]][1:]
        for name, value in zip(params, call.args, strict=False):
            kind = self.type_of(value, env)
            if kind is not None:
                user.init_args.setdefault(name, kind)
        for keyword in call.keywords:
            if keyword.arg is not None:
                kind = self.type_of(keyword.value, env)
                if kind is not None:
                    user.init_args.setdefault(keyword.arg, kind)

    # -- findings -------------------------------------------------------

    def _where(self, node: ast.AST) -> str:
        return f"{self.path}:{getattr(node, 'lineno', 0)}"

    def _guarded(self, node: ast.Attribute) -> bool:
        """Whether the author checked for the attribute first
        (``hasattr(x, "name")`` in an enclosing condition) or marked the
        line ``# type: ignore[attr-defined]``: a capability not every
        implementation has, used knowingly."""
        line = getattr(node, "lineno", 0)
        if 0 < line <= len(self.lines) and "attr-defined" in self.lines[line - 1]:
            return True
        target = ast.unparse(node.value)
        parent = self.parents.get(node)
        while parent is not None:
            tests = []
            if isinstance(parent, (ast.If, ast.While, ast.IfExp, ast.Assert)):
                tests.append(parent.test)
            if isinstance(parent, ast.BoolOp):
                tests.append(parent)
            for test in tests:
                for call in ast.walk(test):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id == "hasattr"
                        and len(call.args) == 2
                        and ast.unparse(call.args[0]) == target
                        and isinstance(call.args[1], ast.Constant)
                        and call.args[1].value == node.attr
                    ):
                        return True
            parent = self.parents.get(parent)
        return False

    def _check_attribute(self, node: ast.Attribute, env: dict[str, Any]) -> None:
        base = self.type_of(node.value, env)
        if base is None or self._guarded(node):
            return
        shown = ast.unparse(node.value)[:60]
        if node.attr.startswith("__") and node.attr.endswith("__"):
            return
        if base[0] == "instance":
            cls = base[1]
            names = members(cls)
            if "__getattr__" in names:
                return
            if node.attr not in names:
                self.problems.append(
                    f"{self._where(node)}: {shown} is a {cls.__name__}, which has no "
                    f"{node.attr!r}{_closest(node.attr, public_members(cls))}"
                )
            elif node.attr.startswith("_") and not self.is_test:
                self.problems.append(
                    f"{self._where(node)}: {shown}.{node.attr} is private to alhazen; "
                    "use the public API"
                )
        elif base[0] == "user":
            user = self.classes.get(base[1])
            if user is None or self._open(user):
                return
            known = self._user_members(user)
            public = sorted(n for n in known if not n.startswith("_"))
            if node.attr not in known:
                self.problems.append(
                    f"{self._where(node)}: {shown} is a {user.name}, which has no "
                    f"{node.attr!r}{_closest(node.attr, public)}"
                )

    def _open(self, user: _UserClass, seen: frozenset[str] = frozenset()) -> bool:
        if user.open or user.name in seen:
            return True
        return any(
            isinstance(base, str)
            and (base not in self.classes or self._open(self.classes[base], seen | {user.name}))
            for base in user.bases
        )

    def _user_members(self, user: _UserClass) -> set[str]:
        known = set(user.names) | set(dir(object))
        for base in user.bases:
            if isinstance(base, str):
                known |= self._user_members(self.classes[base])
            else:
                known |= members(base)
        return known

    def _check_keywords(self, call: ast.Call, env: dict[str, Any]) -> None:
        func = self.type_of(call.func, env)
        if func is None:
            return
        target: Any = None
        if func[0] == "value" and callable(func[1]) and _alhazen(func[1]):
            target = func[1]
        elif func[0] == "method" and func[1][0] == "instance":
            target = getattr(func[1][1], func[2], None)
        if target is None or not call.keywords:
            return
        try:
            signature = inspect.signature(target)
        except (TypeError, ValueError):
            return
        accepted = signature.parameters
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in accepted.values()):
            return
        name = getattr(target, "__qualname__", getattr(target, "__name__", "it"))
        for keyword in call.keywords:
            if keyword.arg is not None and keyword.arg not in accepted:
                self.problems.append(
                    f"{self._where(call)}: {name}() takes no argument {keyword.arg!r}"
                    f"{_closest(keyword.arg, [p for p in accepted if p != 'self'])}"
                )

    def run(self) -> list[str]:
        self._imports()
        self._collect_classes()
        for _ in range(3):  # bases, constructor arguments and attributes settle
            self._resolve_bases()
            self._walk_scope(self.tree, dict(self.globals), None, check=False)
        self._resolve_bases()
        self._walk_scope(self.tree, dict(self.globals), None, check=True)
        return list(dict.fromkeys(self.problems))


def check(path: str, tree: ast.Module, source: str = "") -> list[str]:
    """Attributes read on alhazen values, and keywords passed to alhazen
    callables, that the running alhazen does not have, as ``path:line:
    message`` lines naming the closest real names. ``source`` (the text
    ``tree`` was parsed from) lets a line's ``# type: ignore[attr-defined]``
    be honoured."""
    return _Checker(path, tree, source).run()
