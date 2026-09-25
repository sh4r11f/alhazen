"""Read a downstream task's parameter schema in its own Python process."""

from __future__ import annotations

import ast
import contextlib
import json
import runpy
import sys
from pathlib import Path
from typing import Any


def task_schema(path: Path) -> dict[str, Any]:
    # Use the class that run.py actually passes to run_experiment. Running the
    # module under a different name leaves its __main__ launch guard untouched.
    tree = ast.parse(path.read_text(encoding="utf-8"))
    namespace = runpy.run_path(str(path), run_name="_alhazen_schema")
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        for keyword in call.keywords:
            if keyword.arg == "task_class" and isinstance(keyword.value, ast.Name):
                task = namespace.get(keyword.value.id)
                if task is not None and hasattr(task, "params_model"):
                    return task.params_model.model_json_schema()
    raise ValueError("run.py must declare run_experiment(task_class=YourTask) to expose choices")


def main() -> None:
    with contextlib.redirect_stdout(sys.stderr):
        schema = task_schema(Path(sys.argv[1]))
    print(json.dumps(schema, allow_nan=False))


if __name__ == "__main__":
    main()
