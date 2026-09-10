"""Every text file alhazen reads or writes names its encoding.

Leave the encoding out and Python uses the machine's locale encoding. On
Linux and macOS that is UTF-8, so nothing looks wrong. On the Windows rig it
is cp1252, and then:

- a config value with a non-ASCII character loads as different characters,
  and nothing raises;
- a trial or event field with one is written to the CSV as cp1252, which
  pandas then reads as UTF-8 and either refuses or silently mis-decodes;
- a character cp1252 has no code for raises UnicodeEncodeError inside the
  recorder, in the middle of a session.

It went unnoticed because the CI that runs these tests is not that machine.
A test that writes and reads a file cannot see it there, for the same reason.
So the guard is mechanical: parse every module and fail on any text read or
write that does not name an encoding, the same shape as the guard on version
lookups in test_distribution_identity.py.

Reported by the kde-vergence session after its instruction screen showed a
subject "â€”" where the file said "—".
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "alhazen"

# Receivers whose .open() is not a file. A display backend opens a window, and
# webbrowser opens a URL. Named here, one by one, so that a new exception is a
# decision someone wrote down rather than something the check quietly allows.
# Matched on the last name in the receiver with leading underscores dropped,
# so `self._display.open()` is the same exception as `display.open()`.
NOT_FILES = {"display", "webbrowser"}


def _literal_mode(call: ast.Call) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == "mode":
            node = keyword.value
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            return None
    position = 1 if isinstance(call.func, ast.Name) else 0
    if len(call.args) > position:
        node = call.args[position]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
    return None


def _names_no_encoding(call: ast.Call) -> bool:
    if any(keyword.arg == "encoding" for keyword in call.keywords):
        return False
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
        return True
    is_open = (isinstance(func, ast.Name) and func.id == "open") or (
        isinstance(func, ast.Attribute) and func.attr == "open"
    )
    if not is_open:
        return False
    if (
        isinstance(func, ast.Attribute)
        and ast.unparse(func.value).split(".")[-1].lstrip("_") in NOT_FILES
    ):
        return False
    mode = _literal_mode(call)
    # A binary mode has no encoding to name. A mode that is not a literal
    # cannot be proven binary, so it is held to the rule.
    return not (mode is not None and "b" in mode)


def _offenders() -> list[str]:
    found = []
    for path in sorted(SOURCE.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and _names_no_encoding(node):
                where = path.relative_to(ROOT).as_posix()
                found.append(f"{where}:{node.lineno}: {lines[node.lineno - 1].strip()}")
    return found


class TestEveryTextFileNamesItsEncoding:
    def test_no_read_or_write_falls_back_to_the_locale(self):
        offenders = _offenders()
        assert not offenders, (
            "these read or write text without naming an encoding, so on the Windows rig "
            "they use cp1252 and garble or refuse anything outside ASCII. Name "
            "encoding='utf-8' (or 'utf-8-sig' for a file a person writes by hand): "
            + "; ".join(offenders)
        )


class TestTheGuardItself:
    """The guard is only worth having if it catches what it claims to and
    leaves alone what it claims to. Checked on source written here, so a
    change to the rules is a change to these tests too."""

    @staticmethod
    def _flags(snippet: str) -> bool:
        call = next(node for node in ast.walk(ast.parse(snippet)) if isinstance(node, ast.Call))
        return _names_no_encoding(call)

    def test_it_catches_the_forms_that_fall_back_to_the_locale(self):
        for snippet in (
            "path.read_text()",
            "path.write_text(text)",
            "open(path)",
            "open(path, 'w', newline='')",
            "path.open()",
            "path.open('w', newline='')",
            "path.open(mode)",
        ):
            assert self._flags(snippet), snippet

    def test_it_leaves_alone_what_is_not_a_text_file(self):
        for snippet in (
            "path.read_text(encoding='utf-8')",
            "path.open('w', newline='', encoding='utf-8')",
            "open(path, 'rb')",
            "path.open('rb')",
            "path.open(mode='wb')",
            "display.open()",
            "self._display.open()",
            "webbrowser.open(url, new=1)",
            "path.read_bytes()",
        ):
            assert not self._flags(snippet), snippet
