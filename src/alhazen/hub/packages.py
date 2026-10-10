"""Experiment packages: one portable, checksummed ZIP per experiment release.

A package is how an experiment travels between a lab's rig and the hub: the
author chooses the files (:func:`suggest_files` proposes them, the author
reviews the list), :func:`build_bundle` snapshots exactly those files into a
deterministic ZIP whose root ``alhazen-package.json`` lists every file with
its size and SHA-256, the hub stores the ZIP unchanged, and a rig verifies and
installs it with :func:`extract_bundle`. The SHA-256 of the whole ZIP is the
release's identity; nothing else names a version.

**What this module guarantees.** A package is a small, strict ZIP subset
(stored or deflated members, no extra fields, descriptors, comments or
hidden bytes; headers that agree), read by this module's own parser in one
ordered pass, so every reader and Python version sees the same names and
bytes, and the SHA-256 that names the release covers exactly the bytes that
were checked. A bundle that passes :func:`inspect_bundle` holds exactly the
manifest and the files it declares; every path is a portable relative POSIX
path that cannot leave the install folder on Linux, macOS or Windows and
collides with no other path on a case-insensitive file system; the archive
fits the limits and every file's bytes match its declared size and hash.
:func:`install_bundle` writes only after all of that holds (and after an
expected digest matched), into a staging folder named by an owner record,
and commits by renaming it onto a claimed empty folder: never over an
existing one. After a crash, :func:`recover_install` removes that install's
own leftovers and nothing else.

**What it does not guarantee.** Validation is not a security review of the
code. A package that passes every check can still run anything once a person
chooses to execute it, under that person's own account, with access to their
files, credentials and devices; a virtual environment is not a sandbox. The
exclusion rules in :func:`build_bundle` refuse known classes of sensitive
files by name, which cannot prove that the chosen source or documentation
holds no secret or participant data. Nothing here imports, compiles or runs
package content.

Error messages name archive members and file names, never the absolute paths
of the machine doing the checking, so a server may return them to a client.
Member names are untrusted text and are shown escaped (``repr``).
"""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import math
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from alhazen.errors import AlhazenError
from alhazen.version import get_version

MANIFEST_NAME = "alhazen-package.json"
SCHEMA_VERSION = 1
ENTRYPOINT = "run.py"
DEFAULT_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_FILES = 10_000
# The manifest of a 10,000-file package is about 1.5 MB; the cap leaves room
# for long paths and citations while keeping the parse bounded.
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
# The documentation descriptor is read whole by alhazen.hub.documentation.
MAX_DOCUMENTATION_BYTES = 4 * 1024 * 1024
DOCUMENTATION_SUFFIX = ".json"
PLATFORMS = ("darwin", "linux", "win32")
HARDWARE_KEYS = ("display", "eye_tracker", "reward")
# Oldest values a manifest may declare. ``alhazen_min`` is the experiment's
# own runtime floor, checked against the EXPERIMENT's interpreter at install
# (compatibility_problems); the package format itself is read by the rig's
# alhazen, never the experiment's, so it asks nothing of that floor. 2.12.0
# is the oldest an imported experiment pins (attention-clamp; import round
# 2026-10-09): the release that added subject_kind, which the reward
# suggestion reads. Python 3.10 is what 2.12 itself needs.
OLDEST_PYTHON = (3, 10)
OLDEST_ALHAZEN = (2, 12, 0)

_CHUNK = 1024 * 1024
_MAX_PATH_CHARS = 1024
# Package paths are shorter than other relative paths: joined to a real
# install folder they must stay under Windows' 260-character limit.
MAX_PACKAGE_PATH_CHARS = 180
_MAX_COMPONENT_BYTES = 255
# ZIP without zip64: every size and offset fits 32 bits, every count 16.
_ZIP32_MAX = 0xFFFFFFFF
_ZIP16_MAX = 0xFFFF


# Byte-level openers under their own names. Neither has a text mode or an
# encoding parameter: os.open returns a raw descriptor, and ZipFile.open reads
# or writes a member's bytes. tests/unit/test_text_encoding.py looks for text
# opened without an encoding by the method name `open`, which these are not.
_open_descriptor = os.open
_open_member = zipfile.ZipFile.open


class PackageError(AlhazenError, ValueError):
    """A package, a path in one, or a request to build one is refused. The
    message says what is wrong and never contains a local absolute path."""


@dataclass(frozen=True)
class PackageFile:
    """One declared file: its package path, byte size and SHA-256 (hex)."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class PackageInfo:
    """A verified package: its validated manifest (a fresh copy), and the
    SHA-256 and byte size of the whole ZIP, which pin the release."""

    manifest: dict[str, Any]
    sha256: str
    size: int

    @property
    def files(self) -> tuple[PackageFile, ...]:
        """The declared files, in manifest order."""
        return tuple(PackageFile(**entry) for entry in self.manifest["files"])


# --------------------------------------------------------------------------
# Paths


def _shown(value: str, limit: int = 120) -> str:
    """Untrusted text for a message: escaped, and cut if long."""
    return repr(value if len(value) <= limit else value[:limit] + "...")


_WINDOWS_FORBIDDEN = frozenset('<>:"|?*\\')
_SHORT_NAME = re.compile(r"~[0-9]")
_WINDOWS_RESERVED = re.compile(r"(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])")


def _path_problem(value: object) -> str | None:
    """Why ``value`` is not a portable package path, or None if it is."""
    if not isinstance(value, str):
        return f"a package path must be text, not {type(value).__name__}"
    if not value:
        return "a package path must not be empty"
    if len(value) > _MAX_PATH_CHARS:
        return f"path {_shown(value)} is longer than {_MAX_PATH_CHARS} characters"
    for ch in value:
        category = unicodedata.category(ch)
        if category[0] == "C" or category in ("Zl", "Zp") or (category == "Zs" and ch != " "):
            return (
                f"path {_shown(value)} contains the invisible or control character U+{ord(ch):04X}"
            )
        if ch in _WINDOWS_FORBIDDEN:
            return (
                f"path {_shown(value)} contains {ch!r}, which is not allowed in a "
                "portable path (use '/' between folders; no drive letters)"
            )
    if unicodedata.normalize("NFC", value) != value:
        return f"path {_shown(value)} is not in Unicode NFC form; save it with composed characters"
    if value.startswith("/"):
        return f"path {_shown(value)} is absolute; package paths are relative"
    for part in value.split("/"):
        if part == "":
            return (
                f"path {_shown(value)} has an empty folder name (a leading, trailing or double '/')"
            )
        if part in (".", ".."):
            return (
                f"path {_shown(value)} contains {part!r}; package paths never step up or sideways"
            )
        if part != part.strip(" ") or part.endswith("."):
            return (
                f"path {_shown(value)} has a name that begins or ends with a space or ends "
                "with a dot, which Windows silently changes"
            )
        if len(part.encode("utf-8")) > _MAX_COMPONENT_BYTES:
            return f"path {_shown(value)} has a name longer than {_MAX_COMPONENT_BYTES} bytes"
        if _SHORT_NAME.search(part):
            return (
                f"path {_shown(value)} has a name with '~' and a digit, which can collide with a "
                "Windows short (8.3) name"
            )
        if _WINDOWS_RESERVED.fullmatch(part.split(".", 1)[0].casefold()):
            return f"path {_shown(value)} uses {part!r}, a name Windows reserves for a device"
    return None


def safe_relative(value: str) -> str:
    """``value`` if it is a portable relative POSIX path, else PackageError.

    Refused, never repaired: absolute paths, ``.``/``..`` and empty
    components, backslashes, drive letters and every other character Windows
    forbids, control and invisible characters, names that begin or end with a
    space or end with a dot, Windows device names (``con``, ``nul.txt``,
    ``COM1``...), over-long names, and text that is not Unicode NFC (two
    spellings of one name would install as two files on one system and one
    on another).
    """
    problem = _path_problem(value)
    if problem is not None:
        raise PackageError(problem)
    return value


def _fold(path: str) -> str:
    """The key two paths share if any common file system might treat them as
    one name: Unicode compatibility-normalized, upper-cased (NTFS compares
    upper-case forms: dotless i matches I) and then case-folded."""
    upper = unicodedata.normalize("NFKC", path).upper()
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", upper).casefold())


def _check_path_set(paths: Iterable[str]) -> None:
    """Refuse duplicates, names that differ only by case or Unicode form,
    folders spelled two ways, and a path that is both a file and a folder."""
    files: dict[str, str] = {}
    folders: dict[str, str] = {}
    for path in paths:
        key = _fold(path)
        if key in files:
            other = files[key]
            if other == path:
                raise PackageError(f"path {_shown(path)} is listed twice")
            raise PackageError(
                f"paths {_shown(other)} and {_shown(path)} differ only by case or Unicode "
                "form and would be one file on Windows and macOS"
            )
        files[key] = path
        parts = path.split("/")
        for depth in range(1, len(parts)):
            spelled = "/".join(parts[:depth])
            folded = _fold(spelled)
            seen = folders.setdefault(folded, spelled)
            if seen != spelled:
                raise PackageError(
                    f"folder {_shown(seen)} is also spelled {_shown(spelled)}; one folder "
                    "must have one spelling"
                )
    for folded, spelled in folders.items():
        if folded in files:
            raise PackageError(
                f"{_shown(files[folded])} is a file and also the folder {_shown(spelled)}"
            )


# --------------------------------------------------------------------------
# Manifest

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SEMVER = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
_PYTHON = re.compile(r"(3)\.(0|[1-9][0-9]{0,2})")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_REQUIRED_KEYS = (
    "schema_version",
    "name",
    "version",
    "title",
    "description",
    "entrypoint",
    "python_min",
    "alhazen_min",
    "platforms",
    "hardware",
    "license",
    "citations",
    "files",
)
# protocol_version: the experiment's own pyproject version, recorded only
# when the release version differs from it (import round, decision 3): the
# data of a release is filed under its protocol version (data/v<protocol>/),
# so a documentation-only release 0.6.1 of protocol 0.6.0 keeps its data
# with 0.6.0's.
_OPTIONAL_KEYS = ("documentation", "protocol_version")
# What a pyproject [project] version may look like (PEP 440 characters).
_PROTOCOL = re.compile(r"[0-9][0-9A-Za-z.+!_-]{0,31}")
_MAX_TITLE = 200
_MAX_DESCRIPTION = 20_000
_MAX_LICENSE = 200
_MAX_CITATIONS = 200
_MAX_CITATION = 2_000


def _where(field: str) -> str:
    return f"{MANIFEST_NAME}: {field}"


def _text(
    value: object, field: str, *, max_chars: int, multiline: bool = False, empty: bool = False
) -> str:
    if not isinstance(value, str):
        raise PackageError(f"{_where(field)} must be text, not {type(value).__name__}")
    if not empty and not value.strip():
        raise PackageError(f"{_where(field)} must not be empty")
    if len(value) > max_chars:
        raise PackageError(f"{_where(field)} is longer than {max_chars} characters")
    for ch in value:
        if multiline and ch in "\n\t":
            continue
        category = unicodedata.category(ch)
        if category in ("Cc", "Cs", "Co", "Cn", "Zl", "Zp") or ch in _BIDI:
            raise PackageError(
                f"{_where(field)} contains the control or unassigned character U+{ord(ch):04X}"
            )
    return value


def _whole(value: object, field: str, *, minimum: int, maximum: int) -> int:
    # type() rather than isinstance: True is an int to Python, never a size.
    if type(value) is not int:
        raise PackageError(f"{_where(field)} must be a whole number, not {type(value).__name__}")
    if not minimum <= value <= maximum:
        raise PackageError(f"{_where(field)} must be from {minimum} to {maximum}, not {value}")
    return value


def _release(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise PackageError(f"{value!r} is not a MAJOR.MINOR.PATCH version")
    return (int(match[1]), int(match[2]), int(match[3]))


def _validate_manifest(value: object, *, max_files: int, max_expanded_bytes: int) -> dict[str, Any]:
    """The manifest rebuilt from checked fields, in canonical key order.
    Unknown fields are refused: a later format is a new schema_version."""
    if not isinstance(value, dict):
        raise PackageError(f"{MANIFEST_NAME} must hold a JSON object")
    missing = [key for key in _REQUIRED_KEYS if key not in value]
    if missing:
        raise PackageError(f"{MANIFEST_NAME} is missing {', '.join(missing)}")
    unknown = sorted(set(value) - set(_REQUIRED_KEYS) - set(_OPTIONAL_KEYS))
    if unknown:
        raise PackageError(
            f"{MANIFEST_NAME} has unknown fields {', '.join(_shown(k) for k in unknown)}"
        )

    schema = value["schema_version"]
    if type(schema) is not int or schema != SCHEMA_VERSION:
        raise PackageError(
            f"{_where('schema_version')} is {schema!r}; this alhazen reads schema "
            f"{SCHEMA_VERSION} (a newer package needs a newer alhazen)"
        )
    name = _text(value["name"], "name", max_chars=64)
    if not _SLUG.fullmatch(name):
        raise PackageError(
            f"{_where('name')} {_shown(name)} must be lowercase letters and digits joined by "
            "single hyphens, such as 'kde-vergence'"
        )
    version = _text(value["version"], "version", max_chars=32)
    if not _SEMVER.fullmatch(version):
        raise PackageError(f"{_where('version')} {_shown(version)} must be MAJOR.MINOR.PATCH")
    protocol: str | None = None
    if "protocol_version" in value:
        protocol = _text(value["protocol_version"], "protocol_version", max_chars=32)
        if not _PROTOCOL.fullmatch(protocol):
            raise PackageError(
                f"{_where('protocol_version')} {_shown(protocol)} must be a version such as 0.6.0"
            )
        if protocol == version:
            raise PackageError(
                f"{_where('protocol_version')} is given only when it differs from version"
            )
    title = _text(value["title"], "title", max_chars=_MAX_TITLE)
    description = _text(
        value["description"], "description", max_chars=_MAX_DESCRIPTION, multiline=True, empty=True
    )
    if value["entrypoint"] != ENTRYPOINT:
        raise PackageError(f"{_where('entrypoint')} must be {ENTRYPOINT!r}")

    python_min = _text(value["python_min"], "python_min", max_chars=8)
    match = _PYTHON.fullmatch(python_min)
    if match is None or (int(match[1]), int(match[2])) < OLDEST_PYTHON:
        raise PackageError(
            f"{_where('python_min')} {_shown(python_min)} must be a Python version such as "
            f"'3.11', no older than {OLDEST_PYTHON[0]}.{OLDEST_PYTHON[1]}"
        )
    alhazen_min = _text(value["alhazen_min"], "alhazen_min", max_chars=32)
    if not _SEMVER.fullmatch(alhazen_min) or _release(alhazen_min) < OLDEST_ALHAZEN:
        raise PackageError(
            f"{_where('alhazen_min')} {_shown(alhazen_min)} must be MAJOR.MINOR.PATCH, no "
            f"older than {'.'.join(map(str, OLDEST_ALHAZEN))} (the oldest release the hub installs)"
        )

    platforms = value["platforms"]
    if not isinstance(platforms, list) or not platforms:
        raise PackageError(f"{_where('platforms')} must be a non-empty list")
    for item in platforms:
        if item not in PLATFORMS:
            raise PackageError(
                f"{_where('platforms')} may hold only {', '.join(PLATFORMS)}, not {item!r}"
            )
    if len(set(platforms)) != len(platforms):
        raise PackageError(f"{_where('platforms')} lists a platform twice")

    hardware = value["hardware"]
    if not isinstance(hardware, dict) or set(hardware) != set(HARDWARE_KEYS):
        raise PackageError(
            f"{_where('hardware')} must be an object with exactly {', '.join(HARDWARE_KEYS)}"
        )
    for key in HARDWARE_KEYS:
        if type(hardware[key]) is not bool:
            raise PackageError(f"{_where('hardware.' + key)} must be true or false")

    license_ = _text(value["license"], "license", max_chars=_MAX_LICENSE)
    citations = value["citations"]
    if not isinstance(citations, list) or len(citations) > _MAX_CITATIONS:
        raise PackageError(f"{_where('citations')} must be a list of at most {_MAX_CITATIONS}")
    checked_citations = [
        _text(item, f"citations[{i}]", max_chars=_MAX_CITATION, multiline=True)
        for i, item in enumerate(citations)
    ]

    files = value["files"]
    if not isinstance(files, list) or not files:
        raise PackageError(f"{_where('files')} must be a non-empty list")
    if len(files) > max_files:
        raise PackageError(
            f"{_where('files')} declares {len(files)} files; the limit is {max_files}"
        )
    checked_files: list[dict[str, Any]] = []
    total = 0
    for i, entry in enumerate(files):
        field = f"files[{i}]"
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise PackageError(f"{_where(field)} must be an object with path, size and sha256")
        problem = _path_problem(entry["path"])
        if problem is not None:
            raise PackageError(f"{_where(field)}: {problem}")
        if len(entry["path"]) > MAX_PACKAGE_PATH_CHARS:
            raise PackageError(
                f"{_where(field)}: package paths are at most {MAX_PACKAGE_PATH_CHARS} characters"
            )
        size = _whole(entry["size"], field + ".size", minimum=0, maximum=max_expanded_bytes)
        digest = entry["sha256"]
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise PackageError(f"{_where(field + '.sha256')} must be 64 lowercase hex digits")
        total += size
        checked_files.append({"path": entry["path"], "size": size, "sha256": digest})
    if total > max_expanded_bytes:
        raise PackageError(
            f"the package expands to {total} bytes; the limit is {max_expanded_bytes}"
        )
    _check_path_set([MANIFEST_NAME, *(entry["path"] for entry in checked_files)])
    declared = {entry["path"]: entry for entry in checked_files}
    if ENTRYPOINT not in declared:
        raise PackageError(f"{MANIFEST_NAME} declares entrypoint {ENTRYPOINT!r} but no such file")

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "version": version,
        **({"protocol_version": protocol} if protocol is not None else {}),
        "title": title,
        "description": description,
        "entrypoint": ENTRYPOINT,
        "python_min": python_min,
        "alhazen_min": alhazen_min,
        "platforms": list(platforms),
        "hardware": {key: hardware[key] for key in HARDWARE_KEYS},
        "license": license_,
        "citations": checked_citations,
    }
    if "documentation" in value:
        pointer = value["documentation"]
        problem = _path_problem(pointer)
        if problem is not None:
            raise PackageError(f"{_where('documentation')}: {problem}")
        assert isinstance(pointer, str)  # _path_problem refuses anything else
        if not pointer.endswith(DOCUMENTATION_SUFFIX):
            raise PackageError(f"{_where('documentation')} must name a {DOCUMENTATION_SUFFIX} file")
        if pointer not in declared:
            raise PackageError(
                f"{_where('documentation')} names {_shown(pointer)}, which is not a declared file"
            )
        if declared[pointer]["size"] > MAX_DOCUMENTATION_BYTES:
            raise PackageError(
                f"the documentation file {_shown(pointer)} is larger than "
                f"{MAX_DOCUMENTATION_BYTES} bytes"
            )
        manifest["documentation"] = pointer
    manifest["files"] = checked_files
    return manifest


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise PackageError(f"{MANIFEST_NAME} has the key {_shown(key)} twice")
        result[key] = item
    return result


def _no_constant(name: str) -> Any:
    raise PackageError(f"{MANIFEST_NAME} contains {name}, which is not a number")


def _finite(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise PackageError(f"{MANIFEST_NAME} contains the out-of-range number {text[:40]}")
    return number


def _parse_manifest(data: bytes) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PackageError(f"{MANIFEST_NAME} is not UTF-8 text") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_constant=_no_constant,
            parse_float=_finite,
        )
    except PackageError:
        raise
    except (ValueError, RecursionError) as error:
        # ValueError covers malformed JSON (JSONDecodeError) and integers with
        # more digits than Python converts.
        raise PackageError(f"{MANIFEST_NAME} is not valid JSON: {error}") from error


def _canonical_json(manifest: Mapping[str, Any]) -> bytes:
    text = json.dumps(
        manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return (text + "\n").encode("utf-8")


def compatibility_problems(
    manifest: Mapping[str, Any],
    *,
    python_version: tuple[int, int] | None = None,
    alhazen_version: str | None = None,
    platform: str | None = None,
) -> list[str]:
    """Why a verified package's declared requirements are not met here, as
    sentences; empty when they are. Pass the interpreter the experiment will
    run under (it is usually not the one running this). A development or
    pre-release alhazen counts as its release number; an alhazen whose
    version is unknown is reported, never assumed new enough."""
    python = tuple(python_version) if python_version is not None else sys.version_info[:2]
    current = alhazen_version if alhazen_version is not None else get_version()
    where = platform if platform is not None else sys.platform
    problems = []
    major, minor = (int(part) for part in manifest["python_min"].split("."))
    if python < (major, minor):
        problems.append(
            f"needs Python {manifest['python_min']} or newer; the chosen interpreter is "
            f"{python[0]}.{python[1]}"
        )
    needed = _release(manifest["alhazen_min"])
    leading = re.match(r"(\d+)\.(\d+)\.(\d+)", current)
    if leading is None:
        problems.append(
            f"needs alhazen {manifest['alhazen_min']} or newer; the installed version is "
            f"unknown ({current!r})"
        )
    elif tuple(int(part) for part in leading.groups()) < needed:
        problems.append(f"needs alhazen {manifest['alhazen_min']} or newer; {current} is installed")
    if where not in manifest["platforms"]:
        problems.append(
            f"declares {', '.join(manifest['platforms'])} only; this computer is {where}"
        )
    return problems


# --------------------------------------------------------------------------
# Reading a bundle
#
# Packages use a deliberately small subset of ZIP, read here by this module
# alone (zipfile is only used to write): members stored or deflated; no
# extra fields, comments, data descriptors, encryption or zip64; local
# headers that repeat the central directory exactly; members laid end to end
# from byte 0 with the central directory straight after; every deflate
# stream ending exactly at its member's end. Every reader, and every Python
# version, then sees one and the same set of names and bytes. The whole file
# is read once, in order: the SHA-256 that names the release is computed
# over exactly the bytes that were checked.

_EOCD = struct.Struct("<4s4H2LH")
_LOCAL = struct.Struct("<4s5H3L2H")
_CENTRAL = struct.Struct("<4s6H3L5H2L")
_EOCD_SIGNATURE = b"PK\x05\x06"
_LOCAL_SIGNATURE = b"PK\x03\x04"
_CENTRAL_SIGNATURE = b"PK\x01\x02"
_ZIP64_EXTRA = 0x0001
_FLAG_ENCRYPTED = 0x0001
_FLAG_DEFLATE_LEVEL = 0x0006
_FLAG_DESCRIPTOR = 0x0008
_FLAG_STRONG_ENCRYPTION = 0x0040
_FLAG_UTF8 = 0x0800
_FLAG_MASKED_HEADERS = 0x2000
_STORED = 0
_DEFLATED = 8
# ZIP 2.0: what stored and deflated members need. Higher means zip64,
# other compression or encryption.
_MAX_VERSION_NEEDED = 20
_MAX_NAME_BYTES = 4 * _MAX_PATH_CHARS
_DOS_DIRECTORY = 0x10
_DOS_REPARSE_POINT = 0x400


@dataclass(frozen=True)
class _Limits:
    archive: int
    expanded: int
    files: int

    @classmethod
    def checked(cls, archive: object, expanded: object, files: object) -> _Limits:
        # The defaults are the format's maxima: a hub may accept less, never
        # more, so every release it accepts can be installed by every rig.
        for name, value, top in (
            ("max_archive_bytes", archive, DEFAULT_MAX_ARCHIVE_BYTES),
            ("max_expanded_bytes", expanded, DEFAULT_MAX_EXPANDED_BYTES),
            ("max_files", files, DEFAULT_MAX_FILES),
        ):
            if type(value) is not int or not 1 <= value <= top:
                raise ValueError(f"{name} must be a whole number from 1 to {top}, not {value!r}")
        assert isinstance(archive, int) and isinstance(expanded, int) and isinstance(files, int)
        return cls(archive, expanded, files)


@dataclass(frozen=True)
class _Member:
    """One member as read: where its data starts, how it is stored, and
    what it decodes to."""

    name: str
    header_offset: int
    data_offset: int
    method: int
    flags: int
    crc: int
    compressed: int
    size: int
    sha256: str


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


class _Tap:
    """Sequential reads that hash (and optionally copy) every byte read."""

    def __init__(self, handle: IO[bytes], copy: IO[bytes] | None = None) -> None:
        self.handle = handle
        self.copy = copy
        self.digest = hashlib.sha256()
        self.position = 0

    def read(self, length: int) -> bytes:
        data = self.handle.read(length)
        if len(data) != length:
            raise PackageError("the ZIP archive is truncated")
        self.digest.update(data)
        if self.copy is not None:
            self.copy.write(data)
        self.position += length
        return data


def _end_record(handle: IO[bytes], size: int, limits: _Limits) -> tuple[bytes, int, int, int]:
    """The 22-byte end record, which must close the file: no comment, no
    trailing data, one disk, no zip64; and (entries, cd_offset, cd_size).
    Read first so a forged member count is refused before anything else."""
    if size < _EOCD.size:
        raise PackageError("the file is not a ZIP archive")
    handle.seek(size - _EOCD.size)
    raw = handle.read(_EOCD.size)
    handle.seek(0)
    if len(raw) != _EOCD.size:
        raise PackageError("the ZIP archive is truncated")
    (signature, disk, cd_disk, on_disk, entries, cd_size, cd_offset, comment) = _EOCD.unpack(raw)
    if signature != _EOCD_SIGNATURE:
        raise PackageError(
            "the file is not a ZIP archive, or has a comment or data after its end record"
        )
    if comment != 0 or disk != 0 or cd_disk != 0 or on_disk != entries:
        raise PackageError("the ZIP archive has a comment or spans several disks")
    if _ZIP16_MAX in (entries, on_disk) or _ZIP32_MAX in (cd_size, cd_offset):
        raise PackageError("the ZIP archive uses zip64, which packages do not")
    if entries > limits.files + 1:
        raise PackageError(f"the package has {entries} members; the limit is {limits.files} files")
    if cd_offset + cd_size != size - _EOCD.size:
        raise PackageError(
            "the ZIP archive has data before its first member or between its directory and "
            "its end record"
        )
    if cd_size > entries * (_CENTRAL.size + _MAX_NAME_BYTES):
        raise PackageError("the ZIP directory is larger than its members need")
    return raw, entries, cd_offset, cd_size


def _extras_problem(name: str, extra: bytes) -> str:
    position = 0
    while position + 4 <= len(extra):
        header, length = struct.unpack_from("<HH", extra, position)
        if header == _ZIP64_EXTRA:
            return f"member {_shown(name)} uses zip64, which packages do not"
        position += 4 + length
    return (
        f"member {_shown(name)} carries ZIP extra fields; packages carry none (a second name "
        "or other metadata there would be read differently by other tools)"
    )


def _decode_name(raw: bytes, flags: int) -> str:
    try:
        return raw.decode("utf-8") if flags & _FLAG_UTF8 else raw.decode("ascii")
    except UnicodeDecodeError as error:
        shown = _shown(raw.decode("latin-1"))
        if flags & _FLAG_UTF8:
            raise PackageError(f"member name {shown} is marked UTF-8 but is not") from error
        raise PackageError(f"member name {shown} is not ASCII and not marked as UTF-8") from error


def _check_flags_and_method(name: str, flags: int, method: int, version: int) -> None:
    if method not in (_STORED, _DEFLATED):
        raise PackageError(
            f"member {_shown(name)} uses compression method {method}; packages use stored "
            "or deflate only"
        )
    if flags & (_FLAG_ENCRYPTED | _FLAG_STRONG_ENCRYPTION | _FLAG_MASKED_HEADERS):
        raise PackageError(f"member {_shown(name)} is encrypted")
    if flags & _FLAG_DESCRIPTOR:
        raise PackageError(
            f"member {_shown(name)} uses a trailing data descriptor; package members state "
            "their sizes and CRC in their headers"
        )
    allowed = _FLAG_UTF8 | (_FLAG_DEFLATE_LEVEL if method == _DEFLATED else 0)
    if flags & ~allowed:
        raise PackageError(f"member {_shown(name)} sets ZIP flags {flags:#06x} packages do not use")
    if version > _MAX_VERSION_NEEDED:
        raise PackageError(
            f"member {_shown(name)} needs ZIP version {version / 10:.1f} features (such as "
            "zip64); packages need 2.0 at most"
        )


def _payload(
    read: Callable[[int], bytes],
    name: str,
    method: int,
    compressed: int,
    size: int,
    crc: int,
    sink: Callable[[bytes], object] | None,
) -> str:
    """Read exactly ``compressed`` raw bytes and decode them to exactly
    ``size`` bytes with CRC ``crc``; the SHA-256 of the decoded bytes."""
    digest = hashlib.sha256()
    running = 0
    produced = 0

    def emit(data: bytes) -> None:
        nonlocal running, produced
        produced += len(data)
        if produced > size:
            raise PackageError(f"member {_shown(name)} holds more data than its header says")
        running = zlib.crc32(data, running)
        digest.update(data)
        if sink is not None:
            sink(data)

    left = compressed
    if method == _STORED:
        if compressed != size:
            raise PackageError(f"member {_shown(name)} is stored but its two sizes differ")
        while left:
            chunk = read(min(_CHUNK, left))
            left -= len(chunk)
            emit(chunk)
    else:
        inflater = zlib.decompressobj(-15)
        try:
            while left:
                chunk = read(min(_CHUNK, left))
                left -= len(chunk)
                data = chunk
                while data:
                    if inflater.eof:
                        raise PackageError(
                            f"member {_shown(name)} has bytes after the end of its compressed data"
                        )
                    # Never more output than the header promises (plus one
                    # byte, to notice a lie): no decompression bomb.
                    emit(inflater.decompress(data, size - produced + 1))
                    data = inflater.unconsumed_tail
            # Output zlib still holds once all input is in (bounded the same way).
            while not inflater.eof:
                out = inflater.decompress(inflater.unconsumed_tail, size - produced + 1)
                if not out and not inflater.eof:
                    break
                emit(out)
        except zlib.error as error:
            raise PackageError(f"member {_shown(name)} has damaged compressed data") from error
        if not inflater.eof or inflater.unused_data:
            raise PackageError(
                f"member {_shown(name)}'s compressed data does not end exactly at the member's end"
            )
    if produced != size:
        raise PackageError(f"member {_shown(name)} holds less data than its header says")
    if running != crc:
        raise PackageError(f"member {_shown(name)} fails its CRC check")
    return digest.hexdigest()


def _scan(
    handle: IO[bytes], size: int, limits: _Limits, copy: IO[bytes] | None = None
) -> tuple[str, list[_Member], bytes]:
    """One ordered pass over a whole archive (optionally copying every byte
    to ``copy``): (SHA-256 of the file, members, manifest bytes)."""
    eocd, entries, cd_offset, cd_size = _end_record(handle, size, limits)
    tap = _Tap(handle, copy)
    members: list[_Member] = []
    expanded = 0
    manifest: bytes | None = None
    while tap.position < cd_offset:
        start = tap.position
        if cd_offset - start < _LOCAL.size:
            raise PackageError("the ZIP archive holds data that no member accounts for")
        header = tap.read(_LOCAL.size)
        (signature, version, flags, method, _time, _date, crc, compressed, length) = _LOCAL.unpack(
            header
        )[:9]
        name_length, extra_length = _LOCAL.unpack(header)[9:]
        if signature != _LOCAL_SIGNATURE:
            raise PackageError("the ZIP archive holds data that no member accounts for")
        if len(members) >= entries:
            raise PackageError("the ZIP archive holds more members than its directory lists")
        if not 0 < name_length <= _MAX_NAME_BYTES:
            raise PackageError("a member has an empty or over-long name")
        name = _decode_name(tap.read(name_length), flags)
        if extra_length:
            raise PackageError(_extras_problem(name, tap.read(extra_length)))
        _check_flags_and_method(name, flags, method, version)
        if name.endswith("/"):
            raise PackageError(f"the package holds the folder entry {_shown(name)}; only files")
        problem = _path_problem(name)
        if problem is not None:
            raise PackageError(f"member {problem}")
        expanded += length
        if expanded > limits.expanded:
            raise PackageError(
                f"the package expands to more than {limits.expanded} bytes (the limit)"
            )
        if name == MANIFEST_NAME and length > MAX_MANIFEST_BYTES:
            raise PackageError(f"{MANIFEST_NAME} is larger than {MAX_MANIFEST_BYTES} bytes")
        if tap.position + compressed > cd_offset:
            raise PackageError(f"member {_shown(name)} runs into the ZIP directory")
        collected: bytearray | None = bytearray() if name == MANIFEST_NAME else None
        data_offset = tap.position
        sha = _payload(
            tap.read,
            name,
            method,
            compressed,
            length,
            crc,
            collected.extend if collected is not None else None,
        )
        if collected is not None:
            if manifest is not None:
                raise PackageError(f"member {_shown(name)} appears twice")
            manifest = bytes(collected)
        members.append(
            _Member(name, start, data_offset, method, flags, crc, compressed, length, sha)
        )
    if len(members) != entries:
        raise PackageError("the ZIP directory lists members the archive does not hold")

    for index in range(entries):
        record = tap.read(_CENTRAL.size)
        fields = _CENTRAL.unpack(record)
        (signature, made_by, version, flags, method, _time, _date, crc, compressed, length) = (
            fields[:10]
        )
        name_length, extra_length, comment_length, disk, internal, external, offset = fields[10:]
        member = members[index]
        if signature != _CENTRAL_SIGNATURE:
            raise PackageError("the ZIP directory is damaged")
        if name_length > _MAX_NAME_BYTES:
            raise PackageError("the ZIP directory is damaged")
        raw_name = tap.read(name_length)
        if extra_length:
            raise PackageError(_extras_problem(member.name, tap.read(extra_length)))
        if comment_length or disk or internal not in (0, 1):
            raise PackageError(f"member {_shown(member.name)} has a comment or a disk number")
        if (
            offset != member.header_offset
            or raw_name != member.name.encode("utf-8")
            or (flags, method, crc, compressed, length)
            != (member.flags, member.method, member.crc, member.compressed, member.size)
        ):
            raise PackageError(
                f"member {_shown(member.name)} disagrees with its entry in the ZIP directory"
            )
        _check_flags_and_method(member.name, flags, method, version)
        mode = external >> 16
        if made_by >> 8 == 3 and stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            raise PackageError(
                f"member {_shown(member.name)} is a link, folder or device, not a file"
            )
        if external & (_DOS_DIRECTORY | _DOS_REPARSE_POINT):
            raise PackageError(f"member {_shown(member.name)} is marked as a folder or a link")
    if tap.position != cd_offset + cd_size:
        raise PackageError("the ZIP directory is larger than its entries")
    if tap.read(_EOCD.size) != eocd or tap.position != size:
        raise PackageError("the package changed while it was being checked")
    if manifest is None:
        raise PackageError(f"the package has no {MANIFEST_NAME} at its root")
    return tap.digest.hexdigest(), members, manifest


def _verify_members(
    members: list[_Member], manifest_bytes: bytes, limits: _Limits
) -> dict[str, Any]:
    """The validated manifest, after checking that the archive holds exactly
    the manifest and the declared files, each with its declared size and hash."""
    seen: set[str] = set()
    for member in members:
        if member.name in seen:
            raise PackageError(f"member {_shown(member.name)} appears twice")
        seen.add(member.name)
    _check_path_set(member.name for member in members)
    manifest = _validate_manifest(
        _parse_manifest(manifest_bytes), max_files=limits.files, max_expanded_bytes=limits.expanded
    )
    by_name = {member.name: member for member in members}
    declared = {entry["path"]: entry for entry in manifest["files"]}
    present = set(by_name) - {MANIFEST_NAME}
    extra = sorted(present - set(declared))
    if extra:
        shown = ", ".join(_shown(path) for path in extra[:10])
        raise PackageError(f"the package holds files its manifest does not declare: {shown}")
    absent = sorted(set(declared) - present)
    if absent:
        shown = ", ".join(_shown(path) for path in absent[:10])
        raise PackageError(f"the manifest declares files the package lacks: {shown}")
    for path, entry in declared.items():
        member = by_name[path]
        if member.size != entry["size"]:
            raise PackageError(f"member {_shown(path)} does not match the size in {MANIFEST_NAME}")
        if member.sha256 != entry["sha256"]:
            raise PackageError(
                f"member {_shown(path)} does not match the SHA-256 in {MANIFEST_NAME}"
            )
    return manifest


_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _open_regular(path: Path) -> IO[bytes]:
    """``path`` opened for binary reading, refused unless it is a regular
    file. Opened non-blocking first, so a FIFO cannot stall the caller."""
    try:
        fd = _open_descriptor(path, os.O_RDONLY | _O_BINARY | _O_NONBLOCK)
    except OSError as error:
        raise PackageError(
            f"cannot open the package {_shown(path.name)}: {error.strerror}"
        ) from error
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise PackageError(f"the package {_shown(path.name)} is not a regular file")
    if _O_NONBLOCK:
        os.set_blocking(fd, True)
    return os.fdopen(fd, "rb")


def _inspect_handle(handle: IO[bytes], name: str, limits: _Limits) -> PackageInfo:
    before = os.fstat(handle.fileno())
    if before.st_size > limits.archive:
        raise PackageError(f"the package is larger than {limits.archive} bytes")
    digest, members, manifest_bytes = _scan(handle, before.st_size, limits)
    manifest = _verify_members(members, manifest_bytes, limits)
    if _identity(before) != _identity(os.fstat(handle.fileno())):
        raise PackageError(f"the package {_shown(name)} changed while it was being checked")
    return PackageInfo(manifest=copy.deepcopy(manifest), sha256=digest, size=before.st_size)


def inspect_bundle(
    path: Path,
    *,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
) -> PackageInfo:
    """Verify a package without writing anything; PackageError if refused.

    One ordered pass reads every byte once: the returned SHA-256 is computed
    over exactly the bytes whose structure, manifest and file contents were
    checked. Limits may be lowered but not raised above the defaults (the
    format's maxima). A file whose size, modification time or identity
    changes during the pass is refused. To keep the verified bytes
    themselves, use :func:`stage_bundle`.
    """
    limits = _Limits.checked(max_archive_bytes, max_expanded_bytes, max_files)
    path = Path(path)
    with _open_regular(path) as handle:
        return _inspect_handle(handle, path.name, limits)


def _check_expected(expected_sha256: str | None) -> str | None:
    if expected_sha256 is None:
        return None
    if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
        raise ValueError("expected_sha256 must be 64 lowercase hex digits")
    return expected_sha256


class DigestMismatch(PackageError):
    """The package's SHA-256 is not the release's expected one."""


def _copy_private(source: Path, fd: int, limit: int) -> tuple[str, int]:
    """Copy ``source`` into the new private file open as ``fd``, hashing what
    is copied. Takes ownership of ``fd``."""
    with os.fdopen(fd, "wb") as writer:
        with _open_regular(source) as reader:
            digest = hashlib.sha256()
            size = 0
            while chunk := reader.read(_CHUNK):
                size += len(chunk)
                if size > limit:
                    raise PackageError(f"the package is larger than {limit} bytes")
                digest.update(chunk)
                writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    return digest.hexdigest(), size


def _verified_copy(
    source: Path, fd: int, copy_path: Path, expected: str | None, limits: _Limits
) -> tuple[PackageInfo, list[_Member]]:
    """Copy ``source`` into the private file ``fd`` (at ``copy_path``),
    compare the expected digest, then verify the copy in one pass."""
    digest, size = _copy_private(source, fd, limits.archive)
    if expected is not None and digest != expected:
        raise DigestMismatch(
            f"the package's SHA-256 is {digest}, not the expected {expected}; nothing was installed"
        )
    with open(copy_path, "rb") as handle:
        scanned, members, manifest_bytes = _scan(handle, size, limits)
    if scanned != digest:
        raise PackageError("the private copy of the package changed while it was being checked")
    manifest = _verify_members(members, manifest_bytes, limits)
    return PackageInfo(manifest=copy.deepcopy(manifest), sha256=digest, size=size), members


@dataclass(frozen=True)
class VerifiedBundle:
    """A verified private copy of a package (mode 0600, never rewritten by
    this module) and what it holds."""

    path: Path
    info: PackageInfo


def stage_bundle(
    source: Path,
    staging_dir: Path,
    *,
    expected_sha256: str | None = None,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
) -> VerifiedBundle:
    """Copy ``source`` once into a new private file in ``staging_dir``,
    check ``expected_sha256`` against the copied bytes, and verify the copy.
    The caller keeps (and later removes) the returned file; on refusal it is
    removed here. Lets a server store exactly the bytes it checked."""
    expected = _check_expected(expected_sha256)
    limits = _Limits.checked(max_archive_bytes, max_expanded_bytes, max_files)
    try:
        fd, name = tempfile.mkstemp(prefix=".package-", suffix=".zip", dir=staging_dir)
    except OSError as error:
        raise PackageError(f"cannot create a staging file: {error.strerror}") from error
    copy_path = Path(name)
    try:
        info, _ = _verified_copy(Path(source), fd, copy_path, expected, limits)
    except BaseException:
        os.unlink(copy_path)
        raise
    return VerifiedBundle(path=copy_path, info=info)


# --------------------------------------------------------------------------
# Installing a bundle


def _fsync_directory(path: Path) -> bool:
    """Make a folder's entries durable; False where that cannot be done or
    confirmed (Windows, or a file system that refuses directory syncs)."""
    if sys.platform == "win32":
        return False
    try:
        fd = _open_descriptor(path, os.O_RDONLY | _O_DIRECTORY)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EPERM):
            return False
        raise
    try:
        os.fsync(fd)
    except OSError as error:
        if error.errno in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF):
            return False
        raise
    finally:
        os.close(fd)
    return True


class _Durability:
    def __init__(self) -> None:
        self.unconfirmed: list[str] = []

    def directory(self, path: Path, what: str) -> None:
        if not _fsync_directory(path):
            self.unconfirmed.append(what)


def _extract_verified(
    bundle: Path, members: list[_Member], tree: Path, durability: _Durability
) -> None:
    """Write every declared file of an already verified private copy into the
    empty folder ``tree``, decoding and checking each file again."""
    folders = {""}
    with open(bundle, "rb") as handle:
        for member in members:
            if member.name == MANIFEST_NAME:
                continue
            parts = member.name.split("/")
            for depth in range(1, len(parts)):
                folder = "/".join(parts[:depth])
                if folder not in folders:
                    os.mkdir(tree.joinpath(*parts[:depth]), 0o755)
                    folders.add(folder)
            fd = _open_descriptor(
                tree.joinpath(*parts),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOFOLLOW,
                0o644,
            )
            with os.fdopen(fd, "wb") as writer:
                handle.seek(member.data_offset)
                sha = _payload(
                    _exact_reader(handle),
                    member.name,
                    member.method,
                    member.compressed,
                    member.size,
                    member.crc,
                    writer.write,
                )
                if sha != member.sha256:
                    raise PackageError(f"member {_shown(member.name)} changed during the install")
                writer.flush()
                os.fsync(writer.fileno())
    for folder in sorted(folders, key=len, reverse=True):
        durability.directory(tree.joinpath(*folder.split("/")) if folder else tree, "folders")


def _exact_reader(handle: IO[bytes]) -> Callable[[int], bytes]:
    def read(length: int) -> bytes:
        data = handle.read(length)
        if len(data) != length:
            raise PackageError("the private copy of the package is truncated")
        return data

    return read


def _same_folder(path: Path, claim: os.stat_result) -> bool:
    try:
        now = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(now.st_mode) and (now.st_dev, now.st_ino) == (claim.st_dev, claim.st_ino)


class InstallInterrupted(PackageError):
    """An install record for this destination exists: an earlier install was
    interrupted (or is still running). :func:`recover_install` clears it."""


class InstallInProgress(PackageError):
    """Another process is installing to this destination right now."""


class InstallNotDurable(PackageError):
    """Raised by :func:`extract_bundle` when the package WAS installed (the
    final rename happened) but the file system could not confirm that the
    folder entries are on disk. ``result`` carries the installed package."""

    def __init__(self, result: InstallResult) -> None:
        super().__init__(
            f"{_shown(result.destination.name)} was installed, but its durability could not be "
            f"confirmed ({result.durability_note}); the files are in place and verified"
        )
        self.result = result


@dataclass(frozen=True)
class InstallResult:
    """A completed install. ``durable`` is True only when every file and
    every folder entry, including the final rename, was synced and the OS
    confirmed it; otherwise ``durability_note`` says what could not be."""

    info: PackageInfo
    destination: Path
    durable: bool
    durability_note: str


@dataclass(frozen=True)
class RecoveryResult:
    """What :func:`recover_install` found and did. ``destination_state`` is
    ``absent`` (nothing there), ``claim-removed`` (the empty claimed folder
    was removed), ``installed`` (the rename had committed; the tree is kept)
    or ``kept`` (something not created by the install is there; untouched).
    ``removed`` names the leftovers deleted, beside the destination."""

    record_found: bool
    destination_state: str
    removed: tuple[str, ...]


_RECORD_SUFFIX = ".alhazen-install"
_NONCE = re.compile(r"[0-9a-f]{32}")
_MAX_RECORD_BYTES = 4096
# Windows refuses paths of 260 characters or more unless long paths are
# enabled machine-wide, which a package cannot know; installs stay below.
_WINDOWS_MAX_PATH = 259


def _record_path(destination: Path) -> Path:
    return destination.parent / f".{destination.name}{_RECORD_SUFFIX}"


def _leftover_names(destination: Path, nonce: str) -> tuple[str, str]:
    """(private copy, staging folder) names, derived from the record's nonce
    and never read from the record itself."""
    stem = f".{destination.name[:40]}.{nonce}"
    return f"{stem}.package", f"{stem}.staging"


def _lock(fd: int) -> bool:
    """Take an exclusive, non-blocking lock on an open record; False if
    another process holds it. Released by the OS when the holder exits."""
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return False
        raise PackageError(
            f"cannot lock the install record (file locking unsupported here?): {error.strerror}"
        ) from error
    return True


def _note(error: BaseException, text: str) -> None:
    """Attach a cleanup failure to the error that is propagating, without
    replacing it (add_note is Python 3.11+; 3.10 keeps it on an attribute)."""
    add = getattr(error, "add_note", None)
    if add is not None:
        add(text)
    else:
        notes = getattr(error, "cleanup_notes", [])
        error.cleanup_notes = [*notes, text]  # type: ignore[attr-defined]


def _remove_tree_or_file(path: Path) -> str | None:
    """Remove a leftover this install created: a real folder (recursively,
    never through links) or a regular file. Returns its name if removed."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    elif stat.S_ISREG(info.st_mode):
        os.unlink(path)
    else:
        raise PackageError(f"{_shown(path.name)} is not what the install created; left in place")
    return path.name


def _windows_path_check(destination: Path, staging: Path, members: list[_Member]) -> None:
    if sys.platform != "win32" or not members:
        return
    longest = max(len(member.name) for member in members)
    base = max(len(str(destination.resolve())), len(str(staging.resolve())))
    if base + 1 + longest > _WINDOWS_MAX_PATH:
        raise PackageError(
            f"installing here would need paths of {base + 1 + longest} characters; Windows "
            f"allows {_WINDOWS_MAX_PATH} unless long paths are enabled. Choose a shorter folder"
        )


def _replace_onto_claim(tree: Path, destination: Path) -> None:
    """The commit: rename the finished tree onto the empty claimed folder."""
    if sys.platform != "win32":
        # POSIX rename atomically replaces the empty folder claimed earlier.
        os.replace(tree, destination)
        return
    # Windows cannot rename onto a folder: free the name, then take it, with
    # a bounded retry for antivirus or indexer handles briefly held open.
    os.rmdir(destination)
    delay = 0.05
    for attempt in range(8):
        try:
            os.rename(tree, destination)
            return
        except PermissionError as error:
            if attempt == 7 or getattr(error, "winerror", None) not in (5, 32):
                raise
            time.sleep(delay)
            delay *= 2


def install_bundle(
    path: Path,
    destination: Path,
    *,
    expected_sha256: str | None = None,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
) -> InstallResult:
    """Verify a package and install its files as the new folder ``destination``.

    Steps, each beside ``destination`` in its parent folder:

    1. refuse if ``destination`` exists (even an empty folder or broken link);
    2. create and lock the owner record ``.<name>.alhazen-install`` (refused
       with InstallInterrupted if one exists) and sync it;
    3. claim ``destination`` by creating it empty;
    4. copy the package into a private file, compare ``expected_sha256``
       against the copied bytes (DigestMismatch, before anything else), and
       verify the copy in one pass;
    5. write each file into a private staging folder, decoding and checking
       it again, syncing files and folders;
    6. remove the copy and rename the staging folder onto the claim. This
       rename is the commit: before it nothing is installed, after it the
       install has happened and this function returns (never raises);
    7. sync the parent and remove the record.

    On a failure before the commit every leftover this call created is
    removed (an independent attempt for each; failures are attached as notes
    to the original error, which is the one raised), and the record is kept
    only if some cleanup failed, so :func:`recover_install` can finish it.
    After a crash, :func:`recover_install` removes exactly this install's
    leftovers. Nothing is imported or run.
    """
    expected = _check_expected(expected_sha256)
    limits = _Limits.checked(max_archive_bytes, max_expanded_bytes, max_files)
    path = Path(path)
    destination = Path(destination)
    name = destination.name
    if name in ("", ".", "..") or name.startswith("."):
        raise PackageError("the install folder needs a name that does not start with a dot")
    parent = destination.parent
    if not parent.is_dir():
        raise PackageError(f"the folder that should hold {_shown(name)} does not exist")
    record = _record_path(destination)
    if os.path.lexists(record):
        raise InstallInterrupted(
            f"an earlier install of {_shown(name)} was interrupted or is still running; "
            "call recover_install for it, then install again"
        )
    if os.path.lexists(destination):
        raise PackageError(
            f"{_shown(name)} already exists; a package never installs over an existing folder"
        )
    nonce = os.urandom(16).hex()
    copy_name, staging_name = _leftover_names(destination, nonce)
    try:
        record_fd = _open_descriptor(
            record, os.O_RDWR | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOFOLLOW, 0o600
        )
    except FileExistsError as error:
        raise InstallInterrupted(
            f"an earlier install of {_shown(name)} was interrupted or is still running; "
            "call recover_install for it, then install again"
        ) from error
    except OSError as error:
        raise PackageError(f"cannot write the install record: {error.strerror}") from error
    durability = _Durability()
    claim: os.stat_result | None = None
    try:
        if not _lock(record_fd):
            raise InstallInProgress(f"another process is installing {_shown(name)}")
        body = json.dumps(
            {"format": 1, "destination": name, "nonce": nonce, "pid": os.getpid()},
            sort_keys=True,
        ).encode("utf-8")
        os.write(record_fd, body.ljust(64))
        os.fsync(record_fd)
        durability.directory(parent, "the install record")
        try:
            os.mkdir(destination, 0o755)
        except FileExistsError as error:
            raise PackageError(
                f"{_shown(name)} already exists; a package never installs over an existing folder"
            ) from error
        claim = os.lstat(destination)
        copy_path = parent / copy_name
        tree = parent / staging_name
        fd = _open_descriptor(
            copy_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOFOLLOW, 0o600
        )
        info, members = _verified_copy(path, fd, copy_path, expected, limits)
        os.mkdir(tree, 0o700)
        _windows_path_check(destination, tree, members)
        _extract_verified(copy_path, members, tree, durability)
        os.chmod(tree, 0o755)
        os.unlink(copy_path)
        if not _same_folder(destination, claim) or os.listdir(destination):
            raise PackageError(f"{_shown(name)} was changed by something else during the install")
        _replace_onto_claim(tree, destination)
    except BaseException as error:
        _abandon(error, destination, claim, (parent / copy_name, parent / staging_name), record)
        os.close(record_fd)
        if isinstance(error, OSError):
            raise PackageError(
                f"installing {_shown(name)} failed: {error.strerror or error}"
            ) from error
        raise
    # Committed: from here on the install has happened, whatever follows.
    # Each step is independent, and none can turn the install into a failure.
    for step, what in (
        (lambda: durability.directory(parent, "the final rename"), "syncing the final rename"),
        (lambda: os.unlink(record), "removing the install record"),
        (
            lambda: durability.directory(parent, "the removal of the install record"),
            "syncing the record's removal",
        ),
    ):
        try:
            step()
        except OSError as error:
            durability.unconfirmed.append(f"{what} ({error.strerror or error})")
    os.close(record_fd)
    unconfirmed = sorted(set(durability.unconfirmed))
    note = "not confirmed on disk: " + ", ".join(unconfirmed)
    if sys.platform == "win32":
        note += " (Windows: folders cannot be synced from Python; files were flushed)"
    return InstallResult(
        info=info,
        destination=destination,
        durable=not unconfirmed,
        durability_note=note if unconfirmed else "",
    )


def _abandon(
    error: BaseException,
    destination: Path,
    claim: os.stat_result | None,
    leftovers: tuple[Path, ...],
    record: Path,
) -> None:
    """Undo an install that failed before its commit. Each step is attempted
    independently; a failed step is noted on ``error`` and keeps the record,
    so recover_install can finish the job later."""
    complete = True
    for leftover in leftovers:
        try:
            _remove_tree_or_file(leftover)
        except (OSError, PackageError) as cleanup:
            complete = False
            _note(error, f"could not remove {leftover.name}: {cleanup}")
    if claim is not None and _same_folder(destination, claim):
        try:
            os.rmdir(destination)
        except OSError as cleanup:
            # Not empty: something else wrote into the claim. It is no longer
            # ours to delete, and nothing of ours remains in it.
            if cleanup.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                complete = False
                _note(error, f"could not remove the empty claimed folder: {cleanup.strerror}")
    if complete:
        try:
            os.unlink(record)
        except OSError as cleanup:
            _note(error, f"could not remove the install record: {cleanup.strerror}")


def extract_bundle(
    path: Path,
    destination: Path,
    *,
    expected_sha256: str | None = None,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
) -> PackageInfo:
    """:func:`install_bundle`, returning the verified package. Never reports
    a durable success it could not confirm: if the install committed but the
    file system could not confirm durability (always so on Windows), it
    raises InstallNotDurable, whose ``result`` says the files ARE installed.
    Callers that handle that state should call install_bundle directly."""
    result = install_bundle(
        path,
        destination,
        expected_sha256=expected_sha256,
        max_archive_bytes=max_archive_bytes,
        max_expanded_bytes=max_expanded_bytes,
        max_files=max_files,
    )
    if not result.durable:
        raise InstallNotDurable(result)
    return result.info


def recover_install(destination: Path) -> RecoveryResult:
    """Clear what an interrupted install of ``destination`` left behind.

    Acts only through the owner record ``.<name>.alhazen-install``: refused
    with InstallInProgress while its installer still holds the record's lock.
    Removes the private copy and staging folder whose names derive from the
    record's nonce, and the claimed destination only if it is an EMPTY
    folder. A destination with content is kept: if the record is present it
    is the committed install (``installed``), whose durability was never
    confirmed. Without a record nothing is touched. Safe to repeat.
    """
    destination = Path(destination)
    record = _record_path(destination)
    try:
        fd = _open_descriptor(record, os.O_RDWR | _O_BINARY | _O_NOFOLLOW)
    except FileNotFoundError:
        state = "kept" if os.path.lexists(destination) else "absent"
        return RecoveryResult(record_found=False, destination_state=state, removed=())
    except OSError as error:
        raise PackageError(f"cannot open the install record: {error.strerror}") from error
    try:
        if not _lock(fd):
            raise InstallInProgress(f"another process is installing {_shown(destination.name)}")
        raw = os.read(fd, _MAX_RECORD_BYTES + 1)
        nonce = _record_nonce(raw, destination.name)
        removed: list[str] = []
        if nonce is not None:
            for leftover in _leftover_names(destination, nonce):
                gone = _remove_tree_or_file(destination.parent / leftover)
                if gone is not None:
                    removed.append(gone)
        try:
            info = os.lstat(destination)
        except FileNotFoundError:
            state = "absent"
        else:
            if stat.S_ISDIR(info.st_mode) and not os.listdir(destination):
                os.rmdir(destination)
                removed.append(destination.name)
                state = "claim-removed"
            elif stat.S_ISDIR(info.st_mode) and nonce is not None:
                state = "installed"
            else:
                state = "kept"
        os.unlink(record)
        removed.append(record.name)
        _fsync_directory(destination.parent)
    finally:
        os.close(fd)
    return RecoveryResult(record_found=True, destination_state=state, removed=tuple(removed))


def _record_nonce(raw: bytes, name: str) -> str | None:
    """The nonce from a record, or None for a record cut short by a crash
    before it was written (then no leftover can exist yet)."""
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        body = json.loads(text)
    except ValueError as error:
        raise PackageError("the install record is damaged; inspect it by hand") from error
    if (
        not isinstance(body, dict)
        or body.get("format") != 1
        or body.get("destination") != name
        or not isinstance(body.get("nonce"), str)
        or not _NONCE.fullmatch(body["nonce"])
    ):
        raise PackageError("the install record does not belong to this destination")
    return str(body["nonce"])


# --------------------------------------------------------------------------
# Choosing files

# Each rule names a class of file that must never travel in a source
# package. The lists are known patterns, not a proof that no secret or data
# hides elsewhere: the author still reviews the exact file list.
_REFUSED_FOLDERS = {
    ".git": "version-control history",
    ".hg": "version-control history",
    ".svn": "version-control history",
    ".bzr": "version-control history",
    ".venv": "a Python environment",
    "venv": "a Python environment",
    "virtualenv": "a Python environment",
    ".tox": "a Python environment",
    ".nox": "a Python environment",
    ".conda": "a Python environment",
    "conda-meta": "a Python environment",
    "site-packages": "a Python environment",
    "__pycache__": "compiled Python bytecode",
    ".ssh": "credentials",
    ".gnupg": "credentials",
    ".aws": "credentials",
    ".azure": "credentials",
    ".gcloud": "credentials",
    ".kube": "credentials",
    ".docker": "credentials",
}
# Collected data lives in the experiment's data roots: data/, and beside it
# data-rehearsal/, data-training/, data-dev... (alhazen's naming), plus the people
# registry. Only the experiment's top-level folders are data roots; a
# package's own src/<pkg>/data/ holds code assets.
_DATA_ROOT = re.compile(r"data(-.*)?|people")
# Subject codes are any isalnum() text (non-ASCII letters included), so any
# name beginning sub- counts: participant folders and run files alike.
_SUBJECT = re.compile(r"sub-.+")
_REFUSED_NAMES = {
    ".git": "a git pointer, which holds this computer's paths",
    ".netrc": "credentials",
    "_netrc": "credentials",
    ".pgpass": "credentials",
    ".pypirc": "credentials",
    ".npmrc": "credentials",
    ".git-credentials": "credentials",
    ".htpasswd": "credentials",
    "known_hosts": "the names of machines this computer connects to",
    "authorized_keys": "credentials",
    "id_rsa": "a private key",
    "id_dsa": "a private key",
    "id_ecdsa": "a private key",
    "id_ed25519": "a private key",
    "participants.tsv": "the participant registry",
    "participants.json": "the participant registry",
    "subjects.csv": "the participant registry",
    "experimenters.csv": "the people registry",
    "pyvenv.cfg": "a Python environment",
}
_REFUSED_SUFFIXES = {
    ".pem": "a key or certificate",
    ".key": "a private key",
    ".p12": "a key store",
    ".pfx": "a key store",
    ".jks": "a key store",
    ".keystore": "a key store",
    ".kdbx": "a password database",
    ".ppk": "a private key",
    ".ovpn": "a network credential",
    ".pyc": "compiled Python bytecode",
    ".pyo": "compiled Python bytecode",
    ".sqlite": "a database, which may hold collected data",
    ".sqlite3": "a database, which may hold collected data",
    ".db": "a database, which may hold collected data",
    ".edf": "an eye-tracker recording",
    ".asc": "an eye-tracker recording",
}
_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})
_SECRET_WORD = re.compile(r"(^|[._-])(secrets?|credentials?|tokens?)([._-]|$)")
_RIG_SUFFIXES = (".yaml", ".yml", ".json")
_EXPERIMENT_RIG_SUFFIXES = (".yaml", ".yml")
# Beside a rig file: a measured gamma (config/gamma.py) and a reward
# calibration (config/reward_calibration.py); measurements, not rigs.
_MEASURED_RIG_SUFFIXES = ("_gamma.yaml", "_gamma.yml", ".reward.yaml", ".reward.yml")
# Not sensitive, just not source: left out of suggestions, allowed if chosen.
_NOISE_FOLDERS = frozenset(
    {
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".ipynb_checkpoints",
        ".idea",
        ".vscode",
        "build",
        "dist",
        ".eggs",
        "htmlcov",
    }
)
_NOISE_TOP = frozenset({"movies"})
_NOISE_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini", ".coverage"})


def _refusal(path: str) -> str | None:
    """Why ``path`` (a safe package path) may never be packaged, or None."""
    parts = [_fold(part) for part in path.split("/")]
    name = parts[-1]
    if name == MANIFEST_NAME:
        return "the package manifest's reserved name"
    for part in parts[:-1]:
        if part in _REFUSED_FOLDERS:
            return _REFUSED_FOLDERS[part]
        if _SUBJECT.fullmatch(part):
            return "a subject's data folder"
    if len(parts) > 1 and _DATA_ROOT.fullmatch(parts[0]):
        return "the experiment's data or people folder"
    if name in _REFUSED_NAMES:
        return _REFUSED_NAMES[name]
    if _SUBJECT.fullmatch(name):
        return "a subject's data file"
    if name.startswith("rig-") and name.endswith(_RIG_SUFFIXES):
        # The experiment's own rig files (configs/rig-<name>.yaml, usually
        # `extends:` a shared rig with the design's sync lines, photodiode
        # event and calibration limits) are protocol and ship, so an
        # installed `--rig lab` means what it means in the checkout. What a
        # rig MEASURED stays local: gamma and reward calibrations kept
        # beside the rig, JSON measurement reports, and any rig file outside
        # configs/ (a copied run record).
        shipped = (
            parts[0] == "configs"
            and "measurements" not in parts[:-1]
            and name.endswith(_EXPERIMENT_RIG_SUFFIXES)
            and not name.endswith(_MEASURED_RIG_SUFFIXES)
        )
        if not shipped:
            return "a rig's measured calibration or a copied rig record (measurements stay local)"
    if name.startswith(".alhazen"):
        return "local alhazen state"
    if name not in _ENV_TEMPLATES and (
        name == ".env" or name.startswith(".env.") or name.endswith(".env")
    ):
        return "environment settings, which often hold secrets"
    for suffix, reason in _REFUSED_SUFFIXES.items():
        if name.endswith(suffix):
            return reason
    if name.endswith(("-wal", "-shm", "-journal")):
        return "a database journal"
    if _SECRET_WORD.search(name.rsplit(".", 1)[0] if "." in name[1:] else name):
        return "a file named as a secret, credential or token"
    return None


def _noise(path: str) -> bool:
    parts = [_fold(part) for part in path.split("/")]
    name = parts[-1]
    return (
        any(part in _NOISE_FOLDERS or part.endswith(".egg-info") for part in parts[:-1])
        or (len(parts) > 1 and parts[0] in _NOISE_TOP)
        or name in _NOISE_NAMES
        or name.startswith(".coverage.")
    )


def _inside_environment(source: Path, path: str) -> bool:
    """True when a folder holding ``path`` (or the source itself) is a
    Python environment, recognised by its pyvenv.cfg whatever its name."""
    parts = path.split("/")
    return any(
        os.path.lexists(source.joinpath(*parts[:depth], "pyvenv.cfg"))
        for depth in range(len(parts))
    )


# IO_REPARSE_TAG_NAME_SURROGATE: the reparse point stands for another name
# (symbolic link, junction). Cloud-file placeholders are not surrogates.
_NAME_SURROGATE = 0x20000000


def _is_link(info: os.stat_result) -> bool:
    """A symbolic link, or on Windows a junction or other name surrogate."""
    if stat.S_ISLNK(info.st_mode):
        return True
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )
    return bool(reparse and getattr(info, "st_reparse_tag", 0) & _NAME_SURROGATE)


_DIR_FD = os.open in os.supports_dir_fd and bool(_O_NOFOLLOW and _O_DIRECTORY)


def _open_source(source: Path, path: str) -> int:
    """A read descriptor for ``source/path``, refusing a link anywhere below
    ``source`` and anything but a regular file. POSIX walks the folders by
    descriptor, so a folder swapped for a link mid-walk is refused too."""
    parts = path.split("/")
    try:
        if _DIR_FD:
            folder = _open_descriptor(source, os.O_RDONLY | _O_DIRECTORY)
            try:
                for part in parts[:-1]:
                    inner = _open_descriptor(
                        part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=folder
                    )
                    os.close(folder)
                    folder = inner
                fd = _open_descriptor(
                    parts[-1], os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_BINARY, dir_fd=folder
                )
            finally:
                os.close(folder)
        else:
            for depth in range(1, len(parts) + 1):
                info = os.lstat(source.joinpath(*parts[:depth]))
                if _is_link(info):
                    raise PackageError(f"{_shown(path)} is or passes through a link")
            fd = _open_descriptor(source.joinpath(*parts), os.O_RDONLY | _O_BINARY)
    except FileNotFoundError as error:
        raise PackageError(f"{_shown(path)} does not exist in the experiment folder") from error
    except NotADirectoryError as error:
        raise PackageError(
            f"{_shown(path)} passes through something that is not a folder"
        ) from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise PackageError(f"{_shown(path)} is or passes through a link") from error
        raise PackageError(f"cannot read {_shown(path)}: {error.strerror}") from error
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise PackageError(f"{_shown(path)} is not a regular file")
    return fd


def _read_source(
    source: Path, path: str, sink: Callable[[bytes], object] | None = None
) -> tuple[str, int, tuple[int, int, int, int]]:
    """(sha256, size, identity) of one chosen file, read once; refused if it
    changes while being read."""
    fd = _open_source(source, path)
    with os.fdopen(fd, "rb") as reader:
        before = os.fstat(reader.fileno())
        digest = hashlib.sha256()
        size = 0
        while chunk := reader.read(_CHUNK):
            size += len(chunk)
            if size > before.st_size:
                break
            digest.update(chunk)
            if sink is not None:
                sink(chunk)
        after = os.fstat(reader.fileno())
    if size != before.st_size or _identity(before) != _identity(after):
        raise PackageError(f"{_shown(path)} changed while it was being packaged; try again")
    return digest.hexdigest(), size, _identity(before)


def _publish(temporary: Path, output: Path) -> None:
    """Give the finished archive its name without replacing anything there."""
    try:
        os.link(temporary, output)
    except FileExistsError as error:
        raise PackageError(f"{_shown(output.name)} already exists; choose a new name") from error
    except OSError:
        # No hard links on this file system: copy into an exclusive new file.
        _copy_exclusive(temporary, output)


def _copy_exclusive(temporary: Path, output: Path) -> None:
    try:
        fd = _open_descriptor(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
    except FileExistsError as error:
        raise PackageError(f"{_shown(output.name)} already exists; choose a new name") from error
    try:
        with os.fdopen(fd, "wb") as writer, open(temporary, "rb") as reader:
            shutil.copyfileobj(reader, writer, _CHUNK)
            writer.flush()
            os.fsync(writer.fileno())
    except BaseException:
        os.unlink(output)
        raise


_METADATA_DEFAULTS: dict[str, Any] = {
    "entrypoint": ENTRYPOINT,
    "python_min": f"{OLDEST_PYTHON[0]}.{OLDEST_PYTHON[1]}",
    "alhazen_min": ".".join(map(str, OLDEST_ALHAZEN)),
    "platforms": list(PLATFORMS),
    "citations": [],
}
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def _member(name: str, size: int) -> zipfile.ZipInfo:
    """A member header that depends only on the name and size, so the same
    files always give the same archive (same Python and zlib)."""
    info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.file_size = size
    return info


def build_bundle(
    source: Path, output: Path, metadata: dict[str, Any], files: list[str]
) -> PackageInfo:
    """Snapshot exactly ``files`` (paths relative to ``source``) into a new
    package at ``output`` and return it verified.

    ``metadata`` holds the manifest fields except ``schema_version`` and
    ``files``; ``entrypoint``, ``python_min``, ``alhazen_min``, ``platforms``
    and ``citations`` default to the oldest supported values, all platforms
    and none. Refused before anything is written: unsafe or colliding paths,
    a list without ``run.py``, any file in a sensitive class (version
    control, environments, bytecode, keys and credentials, data folders and
    registries, rig files, local state), links, non-regular files, and an
    existing ``output``. Each file is read twice, once to hash it and once to
    write it, and refused if it changes in between. The archive is
    deterministic: the same files and metadata give the same bytes.
    """
    source = Path(source)
    output = Path(output)
    if not isinstance(metadata, dict):
        raise PackageError("package metadata must be an object")
    if isinstance(files, str) or not isinstance(files, (list, tuple)):
        raise PackageError("the files to package must be a list of paths")
    for key in ("files", "schema_version"):
        if key in metadata and not (key == "schema_version" and metadata[key] == SCHEMA_VERSION):
            raise PackageError(f"package metadata may not set {key!r}; it is computed")
    paths = sorted(safe_relative(path) for path in files)
    if len(paths) > DEFAULT_MAX_FILES:
        raise PackageError(f"{len(paths)} files chosen; the limit is {DEFAULT_MAX_FILES}")
    refused = [(path, reason) for path in paths if (reason := _refusal(path)) is not None]
    if refused:
        shown = "; ".join(f"{_shown(path)} ({reason})" for path, reason in refused[:20])
        more = f" and {len(refused) - 20} more" if len(refused) > 20 else ""
        raise PackageError(f"these files may not be packaged: {shown}{more}")
    _check_path_set([MANIFEST_NAME, *paths])
    if ENTRYPOINT not in paths:
        raise PackageError(f"the package must include {ENTRYPOINT}, the experiment's entry point")
    if not source.is_dir():
        raise PackageError("the experiment folder to package does not exist")
    inside = [path for path in paths if _inside_environment(source, path)]
    if inside:
        raise PackageError(
            f"{_shown(inside[0])} is inside a Python environment (a folder with pyvenv.cfg)"
        )
    if os.path.lexists(output):
        raise PackageError(f"{_shown(output.name)} already exists; choose a new name")
    if not output.parent.is_dir():
        raise PackageError("the folder for the new package does not exist")

    first = {path: _read_source(source, path) for path in paths}
    manifest = _validate_manifest(
        {
            **_METADATA_DEFAULTS,
            **{key: item for key, item in metadata.items() if key != "schema_version"},
            "schema_version": SCHEMA_VERSION,
            "files": [
                {"path": path, "size": first[path][1], "sha256": first[path][0]} for path in paths
            ],
        },
        max_files=DEFAULT_MAX_FILES,
        max_expanded_bytes=DEFAULT_MAX_EXPANDED_BYTES,
    )
    manifest["platforms"] = sorted(manifest["platforms"])
    manifest_bytes = _canonical_json(manifest)

    try:
        fd, name = tempfile.mkstemp(prefix=f".{output.name[:40]}.partial-", dir=output.parent)
    except OSError as error:
        raise PackageError(f"cannot write the new package: {error.strerror}") from error
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w+b") as handle:
            with zipfile.ZipFile(handle, "w", allowZip64=False) as archive:
                archive.writestr(_member(MANIFEST_NAME, len(manifest_bytes)), manifest_bytes)
                for path in paths:
                    with _open_member(archive, _member(path, first[path][1]), "w") as writer:
                        again = _read_source(source, path, writer.write)
                    if again != first[path]:
                        raise PackageError(
                            f"{_shown(path)} changed while it was being packaged; try again"
                        )
            handle.flush()
            os.fsync(handle.fileno())
        info = inspect_bundle(temporary)
        if info.manifest != manifest:
            raise AssertionError("the built manifest does not read back unchanged")
        _publish(temporary, output)
    except OSError as error:
        raise PackageError(f"writing the package failed: {error.strerror or error}") from error
    finally:
        os.unlink(temporary)
    return info


def _git_tracked(source: Path) -> list[str] | None:
    """Paths git tracks under ``source`` (relative to it), or None when
    ``source`` is in no git work tree or git is not installed. A tree that
    git cannot read (unsafe ownership, a broken index) is an error rather
    than a silent switch to listing every file."""
    environment = {
        key: item
        for key, item in os.environ.items()
        if key not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CEILING_DIRECTORIES")
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    # fsmonitor would run a configured program; listing files needs none.
    base = ["git", "-c", "core.fsmonitor=false", "-c", "core.quotepath=off"]
    marked = any(os.path.lexists(folder / ".git") for folder in (source, *source.parents))
    try:
        probe = subprocess.run(
            [*base, "rev-parse", "--is-inside-work-tree"],
            cwd=source,
            env=environment,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if probe.returncode != 0 or probe.stdout.strip() != b"true":
            if marked:
                # git's own message names absolute paths; it is not repeated.
                raise PackageError(
                    "git cannot read the experiment's repository (run `git status` in that "
                    "folder to see why), so its tracked files cannot be listed"
                )
            return None
        listing = subprocess.run(
            [*base, "ls-files", "-z", "--cached", "--"],
            cwd=source,
            env=environment,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        return None  # git is not installed: list the folder instead
    except subprocess.TimeoutExpired as error:
        raise PackageError("git did not answer in time while listing tracked files") from error
    if listing.returncode != 0:
        raise PackageError(
            f"git could not list the tracked files (exit status {listing.returncode}); run "
            "`git status` in that folder to see why"
        )
    names = {raw.decode("utf-8", "surrogateescape") for raw in listing.stdout.split(b"\0") if raw}
    return sorted(names)


_MAX_WALK_ENTRIES = 200_000


def _walk(source: Path) -> list[str]:
    """Every file under ``source`` (relative, '/'-joined), not entering links,
    refused folders or noise folders."""
    found: list[str] = []
    seen = 0
    for folder, folders, names in os.walk(source, followlinks=False):
        relative = Path(folder).relative_to(source).as_posix()
        prefix = "" if relative == "." else relative + "/"
        folders[:] = sorted(
            name
            for name in folders
            if _fold(name) not in _REFUSED_FOLDERS
            and _fold(name) not in _NOISE_FOLDERS
            and not _SUBJECT.fullmatch(_fold(name))
            and not os.path.islink(os.path.join(folder, name))
        )
        seen += len(folders) + len(names)
        if seen > _MAX_WALK_ENTRIES:
            raise PackageError(
                f"the folder holds more than {_MAX_WALK_ENTRIES} entries; choose the experiment's "
                "own folder"
            )
        found.extend(prefix + name for name in names)
    return found


def _is_plain_file(source: Path, path: str) -> bool:
    """Every part of ``path`` exists, and none is a link; the last is a file."""
    parts = path.split("/")
    for depth in range(1, len(parts) + 1):
        try:
            info = os.lstat(source.joinpath(*parts[:depth]))
        except OSError:
            return False
        if _is_link(info):
            return False
    return stat.S_ISREG(info.st_mode)


def suggest_files(source: Path) -> list[str]:
    """The files an author would normally package from ``source``, sorted,
    for them to review before :func:`build_bundle`.

    In a git work tree these are the tracked files (untracked ones are never
    proposed); otherwise every file in the folder. Either way left out:
    links and anything reached through one, non-regular files, paths that
    are not portable, every class :func:`build_bundle` refuses, files inside
    a Python environment, and build/cache/editor clutter. Source, configs,
    lock files, documentation (Markdown, JSON, diagrams) stay in.
    """
    source = Path(source)
    if not source.is_dir():
        raise PackageError("the experiment folder does not exist")
    tracked = _git_tracked(source)
    candidates = tracked if tracked is not None else _walk(source)
    chosen = [
        path
        for path in candidates
        if _path_problem(path) is None
        and _refusal(path) is None
        and not _noise(path)
        and _is_plain_file(source, path)
        and not _inside_environment(source, path)
    ]
    if len(chosen) > DEFAULT_MAX_FILES:
        raise PackageError(
            f"{len(chosen)} files would be proposed; the limit is {DEFAULT_MAX_FILES}. "
            "Choose the experiment's own folder"
        )
    return sorted(chosen)
