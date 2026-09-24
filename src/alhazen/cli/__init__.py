from alhazen.cli.main import main as main

# Nothing in this package is public except `alhazen.cli.modes.run_experiment`
# (docs/reference.md; a test in tests/unit/test_docs_snippets.py holds
# `__all__` to that page). `main` is the console script's entry point and
# stays importable from here, but is internal, so it is not exported.
__all__: list[str] = []
