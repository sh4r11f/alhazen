"""Replacing a file so a reader sees the old one or the new one, never half.

Here, at the bottom of the stack, because the files it protects are written
from more than one layer: the participants registry (this package) and a
subject's training state (``training/``). Both are records a later session
reads back, and both are rewritten whole, so a crash or a full disk part-way
through an in-place rewrite would destroy the only copy.
"""

from __future__ import annotations

import os
from pathlib import Path


def replace_atomically(path: Path, text: str, *, newline: str | None = None) -> None:
    """Replace ``path`` with ``text`` so a reader sees the old file or the new
    one, never half of either.

    Written and flushed to disk under a temporary name in the same folder (a
    rename is only atomic within one filesystem), then renamed over the
    target with `os.replace`, which replaces an existing file on Windows as
    well as on POSIX.

    ``newline`` is passed to `open` as it is: None translates ``\\n`` to the
    platform's line ending, and ``""`` writes ``text`` byte for byte — what
    csv output needs, since it carries its own line endings already.
    """
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline=newline) as f:
            f.write(text)
            f.flush()
            # Onto the disk before the rename: otherwise a power cut can
            # leave the real name pointing at data that was never written.
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        # Still there only if something above failed. Removed so a stray,
        # half-written file is never mistaken for the real one; the error
        # that got us here propagates unchanged.
        temporary.unlink(missing_ok=True)
