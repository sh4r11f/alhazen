"""Read a downstream task's parameter schema in its own Python process.

    python -m alhazen.cli.workspace_schema run.py [TASK]

Prints the JSON schema of the params model of the task ``run.py`` runs: the
class it passes to ``run_experiment(task_class=...)`` or, for an experiment
that declares several with ``run_experiment(tasks=...)``, the one named —
without a name, the table's default. The workspace runs this in the
project's own interpreter, where the task's imports resolve, never in its
own; every refusal names what run.py must say for the choices to be read.
"""

from __future__ import annotations

import ast
import contextlib
import json
import runpy
import sys
from pathlib import Path
from typing import Any

SHAPE = (
    "run.py must call run_experiment(task_class=YourTask) or "
    "run_experiment(tasks=TASKS) with TASKS a module-level name to expose parameter choices"
)


def _run_experiment_call(tree: ast.AST) -> ast.Call | None:
    """The `run_experiment(...)` call in run.py, however the name is reached."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "run_experiment":
            return node
    return None


def _value(node: ast.expr, namespace: dict[str, Any], what: str) -> Any:
    """A keyword's value: a module-level name looked up after run.py ran, or a
    literal. Anything else — a local variable, an expression — is refused by
    name, because reading it would mean guessing at code that did not run."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in namespace:
            raise ValueError(f"run.py's {what}={node.id} is not a module-level name")
        return namespace[node.id]
    raise ValueError(f"run.py's {what}= must be a module-level name or a literal; {SHAPE}")


def task_schema(path: Path, task: str | None = None) -> dict[str, Any]:
    # Use the class that run.py actually passes to run_experiment. Running the
    # module under a different name leaves its __main__ launch guard untouched.
    tree = ast.parse(path.read_text(encoding="utf-8"))
    namespace = runpy.run_path(str(path), run_name="_alhazen_schema")
    call = _run_experiment_call(tree)
    if call is None:
        raise ValueError(SHAPE)
    keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
    if "tasks" in keywords:
        table = _value(keywords["tasks"], namespace, "tasks")
        if not isinstance(table, dict) or not table:
            raise ValueError(
                "run.py's tasks= must be a non-empty dict of name -> (TaskClass, params)"
            )
        if task is None:
            default = keywords.get("default_task")
            task = (
                _value(default, namespace, "default_task")
                if default is not None
                else next(iter(table))
            )
        if task not in table:
            raise ValueError(
                f"run.py declares no task {task!r}; it declares {', '.join(map(str, table))}"
            )
        cls = table[task][0]
    elif "task_class" in keywords:
        if task is not None:
            raise ValueError(
                f"run.py declares one task (task_class=); it has no task {task!r} to choose"
            )
        cls = _value(keywords["task_class"], namespace, "task_class")
    else:
        raise ValueError(SHAPE)
    if not hasattr(cls, "params_model"):
        raise ValueError(f"{getattr(cls, '__name__', cls)!r} declares no params_model")
    return cls.params_model.model_json_schema()


def main() -> None:
    with contextlib.redirect_stdout(sys.stderr):
        schema = task_schema(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else None)
    print(json.dumps(schema, allow_nan=False))


if __name__ == "__main__":
    main()
