"""Deprecating something without breaking the experiment that uses it.

alhazen's public API is what an experiment package depends on, and those
packages live in other repositories on other people's schedules. So nothing
public disappears without a release in which it still works and says it is
going: a name is deprecated in a MINOR release, keeps working and warning,
and is removed in the next MAJOR one — removal breaks callers, which is what
a MAJOR bump announces (docs/versioning.md §4).

    @deprecated(since="1.1", removed_in="2.0", instead="Task.build_trial")
    def old_thing(...): ...

The warning names the version it goes away in and what to use instead,
because a DeprecationWarning that says only "deprecated" leaves the reader
exactly where they started. tests/unit/test_versioning.py reads every
``removed_in`` out of the source and fails once pyproject.toml's version
reaches it, so the version named is never one that has already shipped.
"""

from __future__ import annotations

import functools
import warnings
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def deprecation_message(name: str, since: str, removed_in: str, instead: str | None = None) -> str:
    message = f"{name} is deprecated since alhazen {since} and will be removed in {removed_in}"
    return f"{message}; use {instead} instead" if instead else message


def deprecated(since: str, removed_in: str, instead: str | None = None) -> Callable[[F], F]:
    """Mark a function or method as going away.

    ``stacklevel=2`` so the warning points at the caller's line — the place
    that has to change — rather than at this decorator.
    """

    def decorate(function: F) -> F:
        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            warnings.warn(
                deprecation_message(
                    getattr(function, "__qualname__", function.__name__),
                    since,
                    removed_in,
                    instead,
                ),
                DeprecationWarning,
                stacklevel=2,
            )
            return function(*args, **kwargs)

        wrapper.__doc__ = (
            f"{function.__doc__ or ''}\n\n.. deprecated:: {since}\n   "
            f"Removed in {removed_in}." + (f" Use {instead}." if instead else "")
        ).strip()
        return wrapper  # type: ignore[return-value]

    return decorate


def warn_deprecated_argument(
    name: str, since: str, removed_in: str, instead: str | None = None
) -> None:
    """Warn about one argument, from inside a function that still accepts it."""
    warnings.warn(
        deprecation_message(f"the {name!r} argument", since, removed_in, instead),
        DeprecationWarning,
        stacklevel=3,
    )
