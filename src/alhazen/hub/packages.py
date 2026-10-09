"""Experiment packages: one portable, checksummed ZIP per experiment release.

A package is how an experiment travels between a lab's rig and the hub: the
author chooses the files (:func:`suggest_files` proposes them, the author
reviews the list), :func:`build_bundle` snapshots exactly those files into a
deterministic ZIP whose root ``alhazen-package.json`` lists every file with
its size and SHA-256, the hub stores the ZIP unchanged, and a rig verifies and
installs it with :func:`extract_bundle`. The SHA-256 of the whole ZIP is the
release's identity; nothing else names a version.

**What this module guarantees.** A bundle that passes :func:`inspect_bundle`
holds exactly the manifest and the files the manifest declares; every path is
a portable relative POSIX path that cannot leave the install folder on Linux,
macOS or Windows, collides with no other path on a case-insensitive file
system, and names a regular file; no member is encrypted, a link, a device or
a directory entry; the archive fits the size and member limits and every
file's bytes match its declared size and hash. :func:`extract_bundle` writes
only after all of that holds, into a staging folder it created, and moves the
finished tree into place without ever writing over an existing folder.

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
import unicodedata
import warnings
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
# Oldest values a manifest may declare: the package format first ships in
# alhazen 2.13.0, which itself needs Python 3.10.
OLDEST_PYTHON = (3, 10)
OLDEST_ALHAZEN = (2, 13, 0)

_CHUNK = 1024 * 1024
_MAX_PATH_CHARS = 1024
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
    """The key two paths share if any common file system would treat them as
    one name: Unicode compatibility-normalized and case-folded."""
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", path).casefold())


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
_OPTIONAL_KEYS = ("documentation",)
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
            f"older than {'.'.join(map(str, OLDEST_ALHAZEN))} (the first with packages)"
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

_EOCD = struct.Struct("<4s4H2LH")
_LOCAL = struct.Struct("<4s5H3L2H")
_EOCD_SIGNATURE = b"PK\x05\x06"
_LOCAL_SIGNATURE = b"PK\x03\x04"
_ZIP64_EXTRA = 0x0001
_FLAG_ENCRYPTED = 0x0001
_FLAG_DESCRIPTOR = 0x0008
_FLAG_STRONG_ENCRYPTION = 0x0040
_FLAG_UTF8 = 0x0800
_FLAG_MASKED_HEADERS = 0x2000
_DOS_DIRECTORY = 0x10
_DOS_REPARSE_POINT = 0x400
_SUPPORTED_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)


@dataclass(frozen=True)
class _Limits:
    archive: int
    expanded: int
    files: int

    @classmethod
    def checked(cls, archive: object, expanded: object, files: object) -> _Limits:
        for name, value, top in (
            ("max_archive_bytes", archive, _ZIP32_MAX - 1),
            ("max_expanded_bytes", expanded, _ZIP32_MAX - 1),
            ("max_files", files, _ZIP16_MAX - 2),
        ):
            if type(value) is not int or not 1 <= value <= top:
                raise ValueError(f"{name} must be a whole number from 1 to {top}, not {value!r}")
        assert isinstance(archive, int) and isinstance(expanded, int) and isinstance(files, int)
        return cls(archive, expanded, files)


_DEFAULT_LIMITS = _Limits(DEFAULT_MAX_ARCHIVE_BYTES, DEFAULT_MAX_EXPANDED_BYTES, DEFAULT_MAX_FILES)


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _hash_handle(handle: IO[bytes], limit: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := handle.read(_CHUNK):
        size += len(chunk)
        if size > limit:
            raise PackageError(f"the package is larger than {limit} bytes")
        digest.update(chunk)
    return digest.hexdigest(), size


def _read_exact(handle: IO[bytes], offset: int, length: int) -> bytes:
    handle.seek(offset)
    data = handle.read(length)
    if len(data) != length:
        raise PackageError("the ZIP archive is truncated")
    return data


def _central_directory(handle: IO[bytes], size: int, limits: _Limits) -> tuple[int, int, int]:
    """(entries, offset, size) of the central directory, from an end record
    that must be the archive's last 22 bytes: no comment, no trailing data,
    no zip64, one disk. Read before zipfile parses anything, so a forged
    member count is refused before millions of entries reach memory."""
    if size < _EOCD.size:
        raise PackageError("the file is not a ZIP archive")
    (signature, disk, cd_disk, on_disk, entries, cd_size, cd_offset, comment) = _EOCD.unpack(
        _read_exact(handle, size - _EOCD.size, _EOCD.size)
    )
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
        raise PackageError("the ZIP archive has data between its directory and its end record")
    return entries, cd_offset, cd_size


def _has_zip64(extra: bytes) -> bool:
    position = 0
    while position + 4 <= len(extra):
        header, length = struct.unpack_from("<HH", extra, position)
        if header == _ZIP64_EXTRA:
            return True
        position += 4 + length
    return False


def _check_member(info: zipfile.ZipInfo) -> None:
    """Refuse a member that is not a plain, unencrypted, portable file."""
    name = info.orig_filename
    if name.endswith("/"):
        raise PackageError(f"the package holds the folder entry {_shown(name)}; only files")
    if info.filename != name:
        raise PackageError(f"the member name {_shown(name)} contains a NUL or a backslash")
    problem = _path_problem(name)
    if problem is not None:
        raise PackageError(f"member {problem}")
    if not name.isascii() and not info.flag_bits & _FLAG_UTF8:
        raise PackageError(f"member {_shown(name)} has a non-ASCII name not marked as UTF-8")
    if info.flag_bits & (_FLAG_ENCRYPTED | _FLAG_STRONG_ENCRYPTION | _FLAG_MASKED_HEADERS):
        raise PackageError(f"member {_shown(name)} is encrypted")
    if info.compress_type not in _SUPPORTED_METHODS:
        raise PackageError(
            f"member {_shown(name)} uses compression method {info.compress_type}; "
            "packages use stored or deflate only"
        )
    if _has_zip64(info.extra) or _ZIP32_MAX in (info.file_size, info.compress_size):
        raise PackageError(f"member {_shown(name)} uses zip64, which packages do not")
    mode = info.external_attr >> 16
    if info.create_system == 3 and stat.S_IFMT(mode) not in (0, stat.S_IFREG):
        raise PackageError(f"member {_shown(name)} is a link, folder or device, not a file")
    if info.external_attr & (_DOS_DIRECTORY | _DOS_REPARSE_POINT):
        raise PackageError(f"member {_shown(name)} is marked as a folder or a link")


def _check_layout(handle: IO[bytes], infos: list[zipfile.ZipInfo], cd_offset: int) -> None:
    """Each member's bytes follow the previous member's with no gap, no
    overlap and nothing before the first: a bomb built from members sharing
    one compressed stream, or bytes hidden between members, is refused."""
    expected = 0
    for info in sorted(infos, key=lambda item: item.header_offset):
        if info.header_offset != expected:
            raise PackageError(
                f"member {_shown(info.orig_filename)} overlaps another or follows hidden data"
            )
        header = _LOCAL.unpack(_read_exact(handle, info.header_offset, _LOCAL.size))
        signature, _version, flags, method = header[0], header[1], header[2], header[3]
        name_length, extra_length = header[9], header[10]
        if signature != _LOCAL_SIGNATURE:
            raise PackageError(f"member {_shown(info.orig_filename)} has no local header")
        raw_name = _read_exact(handle, info.header_offset + _LOCAL.size, name_length)
        encoding = "utf-8" if info.flag_bits & _FLAG_UTF8 else "cp437"
        if raw_name != info.orig_filename.encode(encoding):
            raise PackageError(
                f"member {_shown(info.orig_filename)} has a different name in its local header"
            )
        if method != info.compress_type or (flags ^ info.flag_bits) & (
            _FLAG_ENCRYPTED | _FLAG_STRONG_ENCRYPTION | _FLAG_UTF8 | _FLAG_DESCRIPTOR
        ):
            raise PackageError(
                f"member {_shown(info.orig_filename)} disagrees with its local header"
            )
        local_extra = _read_exact(
            handle, info.header_offset + _LOCAL.size + name_length, extra_length
        )
        if _has_zip64(local_extra):
            raise PackageError(f"member {_shown(info.orig_filename)} uses zip64")
        end = info.header_offset + _LOCAL.size + name_length + extra_length + info.compress_size
        if info.flag_bits & _FLAG_DESCRIPTOR:
            # A data descriptor (written by streaming tools): optional
            # signature, CRC, compressed and uncompressed size.
            end += 16 if _read_exact(handle, end, 4) == b"PK\x07\x08" else 12
        expected = end
    if expected != cd_offset:
        raise PackageError("the ZIP archive holds data that no member accounts for")


def _stream_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    entry: Mapping[str, Any],
    sink: Callable[[bytes], object] | None = None,
) -> None:
    """Decompress one member, never past its declared size, and check its
    size and SHA-256 against the manifest entry."""
    digest = hashlib.sha256()
    size = 0
    try:
        with _open_member(archive, info) as source:
            while chunk := source.read(_CHUNK):
                size += len(chunk)
                if size > entry["size"]:
                    break
                digest.update(chunk)
                if sink is not None:
                    sink(chunk)
    except (zipfile.BadZipFile, EOFError, zlib.error) as error:
        raise PackageError(f"member {_shown(entry['path'])} is damaged: {error}") from error
    if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
        raise PackageError(
            f"member {_shown(entry['path'])} does not match the size and SHA-256 in {MANIFEST_NAME}"
        )


def _verify_open(
    handle: IO[bytes], size: int, limits: _Limits
) -> tuple[dict[str, Any], dict[str, zipfile.ZipInfo]]:
    """Validate the archive in ``handle`` (``size`` bytes): structure,
    manifest, member set and every file's bytes."""
    entries, cd_offset, _cd_size = _central_directory(handle, size, limits)
    handle.seek(0)
    try:
        archive = zipfile.ZipFile(handle)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, ValueError) as error:
        # ValueError includes a UnicodeDecodeError for a name flagged UTF-8.
        raise PackageError(f"the ZIP archive cannot be read: {error}") from error
    with archive:
        infos = archive.infolist()
        if len(infos) != entries or getattr(archive, "start_dir", cd_offset) != cd_offset:
            raise PackageError("the ZIP directory does not match its end record")
        for info in infos:
            _check_member(info)
        _check_layout(handle, infos, cd_offset)
        by_name: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            if info.orig_filename in by_name:
                raise PackageError(f"member {_shown(info.orig_filename)} appears twice")
            by_name[info.orig_filename] = info
        _check_path_set(by_name)

        manifest_info = by_name.get(MANIFEST_NAME)
        if manifest_info is None:
            raise PackageError(f"the package has no {MANIFEST_NAME} at its root")
        if manifest_info.file_size > MAX_MANIFEST_BYTES:
            raise PackageError(f"{MANIFEST_NAME} is larger than {MAX_MANIFEST_BYTES} bytes")
        raw = bytearray()
        try:
            with _open_member(archive, manifest_info) as source:
                while chunk := source.read(_CHUNK):
                    raw += chunk
                    if len(raw) > manifest_info.file_size:
                        raise PackageError(f"{MANIFEST_NAME} is longer than its header says")
        except (zipfile.BadZipFile, EOFError, zlib.error) as error:
            raise PackageError(f"{MANIFEST_NAME} is damaged: {error}") from error
        manifest = _validate_manifest(
            _parse_manifest(bytes(raw)), max_files=limits.files, max_expanded_bytes=limits.expanded
        )

        declared = {entry["path"]: entry for entry in manifest["files"]}
        members = set(by_name) - {MANIFEST_NAME}
        extra = sorted(members - set(declared))
        if extra:
            shown = ", ".join(_shown(path) for path in extra[:10])
            raise PackageError(f"the package holds files its manifest does not declare: {shown}")
        absent = sorted(set(declared) - members)
        if absent:
            shown = ", ".join(_shown(path) for path in absent[:10])
            raise PackageError(f"the manifest declares files the package lacks: {shown}")
        for path, entry in declared.items():
            if by_name[path].file_size != entry["size"]:
                raise PackageError(
                    f"member {_shown(path)} does not match the size in {MANIFEST_NAME}"
                )
        for path, entry in declared.items():
            _stream_member(archive, by_name[path], entry)
    return manifest, by_name


def inspect_bundle(
    path: Path,
    *,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
) -> PackageInfo:
    """Verify a package without writing anything; PackageError if refused.

    Reads the whole file to hash it, then reads its structure and every
    declared file's bytes (streamed, bounded by the declared sizes). The file
    must not change while it is read: a changed size, modification time or
    identity is refused, so the returned SHA-256 describes what was checked.
    """
    limits = _Limits.checked(max_archive_bytes, max_expanded_bytes, max_files)
    path = Path(path)
    try:
        handle = open(path, "rb")  # noqa: SIM115 - closed by the with below
    except IsADirectoryError as error:
        raise PackageError(f"the package {_shown(path.name)} is not a regular file") from error
    except OSError as error:
        raise PackageError(
            f"cannot open the package {_shown(path.name)}: {error.strerror}"
        ) from error
    with handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise PackageError(f"the package {_shown(path.name)} is not a regular file")
        if before.st_size > limits.archive:
            raise PackageError(f"the package is larger than {limits.archive} bytes")
        digest, size = _hash_handle(handle, limits.archive)
        manifest, _ = _verify_open(handle, size, limits)
        after = os.fstat(handle.fileno())
    if _identity(before) != _identity(after) or size != before.st_size:
        raise PackageError(f"the package {_shown(path.name)} changed while it was being checked")
    return PackageInfo(manifest=copy.deepcopy(manifest), sha256=digest, size=size)


# --------------------------------------------------------------------------
# Installing a bundle

_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _fsync_directory(path: Path) -> None:
    """Make a folder's entries durable where the OS allows opening a folder
    (POSIX). Windows has no such call; NTFS journals the rename itself."""
    if sys.platform == "win32":
        return
    fd = _open_descriptor(path, os.O_RDONLY | _O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_private(source: Path, target: Path, limit: int) -> tuple[str, int]:
    """Copy ``source`` into a new private file, hashing what is copied, so
    the checks and the extraction read bytes nobody else can change."""
    try:
        reader = open(source, "rb")  # noqa: SIM115 - closed by the with below
    except IsADirectoryError as error:
        raise PackageError(f"the package {_shown(source.name)} is not a regular file") from error
    except OSError as error:
        raise PackageError(
            f"cannot open the package {_shown(source.name)}: {error.strerror}"
        ) from error
    with reader:
        if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
            raise PackageError(f"the package {_shown(source.name)} is not a regular file")
        fd = _open_descriptor(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
        with os.fdopen(fd, "wb") as writer:
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


def _extract_verified(bundle: Path, manifest: Mapping[str, Any], tree: Path) -> None:
    """Write every declared file of an already verified private copy into the
    empty folder ``tree``, re-checking each file's hash as it is written."""
    folders = {""}
    with open(bundle, "rb") as handle, zipfile.ZipFile(handle) as archive:
        for entry in manifest["files"]:
            parts = entry["path"].split("/")
            for depth in range(1, len(parts)):
                folder = "/".join(parts[:depth])
                if folder not in folders:
                    os.mkdir(tree.joinpath(*parts[:depth]), 0o755)
                    folders.add(folder)
            target = tree.joinpath(*parts)
            fd = _open_descriptor(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOFOLLOW, 0o644
            )
            with os.fdopen(fd, "wb") as writer:
                _stream_member(archive, archive.getinfo(entry["path"]), entry, writer.write)
                writer.flush()
                os.fsync(writer.fileno())
    for folder in sorted(folders, key=len, reverse=True):
        _fsync_directory(tree.joinpath(*folder.split("/")) if folder else tree)


def _same_folder(path: Path, claim: os.stat_result) -> bool:
    try:
        now = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(now.st_mode) and (now.st_dev, now.st_ino) == (claim.st_dev, claim.st_ino)


def _release_claim(destination: Path, claim: os.stat_result) -> None:
    """Remove the empty placeholder this call created, and nothing else."""
    if _same_folder(destination, claim) and not os.listdir(destination):
        os.rmdir(destination)


def extract_bundle(path: Path, destination: Path) -> PackageInfo:
    """Verify a package and install its files as the new folder ``destination``.

    ``destination`` must not exist (not even as an empty folder or a broken
    link) and its parent must. The name is claimed first by creating it, so
    two installs to one place cannot both proceed. The package is copied into
    a private staging folder beside it, verified in full there with the
    default limits, written out file by file (each hash checked again,
    synced), and the finished tree is renamed onto the claimed name. On any
    failure the staging folder and the empty claim are removed; nothing that
    this call did not create is touched. Nothing is imported or run.
    """
    path = Path(path)
    destination = Path(destination)
    name = destination.name
    if name in ("", ".", ".."):
        raise PackageError("the install folder needs a name")
    parent = destination.parent
    if not parent.is_dir():
        raise PackageError(f"the folder that should hold {_shown(name)} does not exist")
    try:
        os.mkdir(destination, 0o755)
    except FileExistsError as error:
        raise PackageError(
            f"{_shown(name)} already exists; a package never installs over an existing folder"
        ) from error
    except OSError as error:
        raise PackageError(
            f"cannot create the install folder {_shown(name)}: {error.strerror}"
        ) from error
    claim = os.lstat(destination)
    installed = False
    staging: Path | None = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=f".{name[:40]}.staging-", dir=parent))
        bundle = staging / "bundle.zip"
        digest, size = _copy_private(path, bundle, DEFAULT_MAX_ARCHIVE_BYTES)
        info = inspect_bundle(bundle)
        if (info.sha256, info.size) != (digest, size):
            raise PackageError("the package changed while it was being installed")
        tree = staging / "tree"
        os.mkdir(tree, 0o755)
        _extract_verified(bundle, info.manifest, tree)
        os.unlink(bundle)
        if not _same_folder(destination, claim) or os.listdir(destination):
            raise PackageError(f"{_shown(name)} was changed by something else during the install")
        try:
            # POSIX rename atomically replaces the empty folder claimed above.
            os.replace(tree, destination)
        except OSError:
            if sys.platform != "win32":
                raise
            # Windows cannot rename onto a folder: free the name, then take it.
            os.rmdir(destination)
            os.rename(tree, destination)
        installed = True
        os.chmod(destination, 0o755)
        _fsync_directory(parent)
    except OSError as error:
        raise PackageError(
            f"installing {_shown(name)} failed: {error.strerror or error}"
        ) from error
    finally:
        if not installed:
            if staging is not None:
                shutil.rmtree(staging)
            _release_claim(destination, claim)
    assert staging is not None
    try:
        os.rmdir(staging)
    except OSError as error:
        # The install is complete; an empty hidden folder left beside it is
        # housekeeping, reported rather than turned into a failed install.
        warnings.warn(
            f"installed {name!r}, but its empty staging folder could not be removed: "
            f"{error.strerror}",
            ResourceWarning,
            stacklevel=2,
        )
    return info


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
# data-rehearsal/, data-training/... (alhazen's naming), plus the people
# registry. Only the experiment's top-level folders are data roots; a
# package's own src/<pkg>/data/ holds code assets.
_DATA_ROOT = re.compile(r"data([-_].*)?|people")
_SUBJECT_FOLDER = re.compile(r"sub-[a-z0-9]+")
_REFUSED_NAMES = {
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
    parts = [part.casefold() for part in path.split("/")]
    name = parts[-1]
    if name == MANIFEST_NAME:
        return "the package manifest's reserved name"
    for part in parts[:-1]:
        if part in _REFUSED_FOLDERS:
            return _REFUSED_FOLDERS[part]
        if _SUBJECT_FOLDER.fullmatch(part):
            return "a subject's data folder"
    if len(parts) > 1 and _DATA_ROOT.fullmatch(parts[0]):
        return "the experiment's data or people folder"
    if name in _REFUSED_NAMES:
        return _REFUSED_NAMES[name]
    if name.startswith("rig-") and name.endswith(_RIG_SUFFIXES):
        return "a rig's own configuration or calibration (each lab uses its own rig)"
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
    parts = [part.casefold() for part in path.split("/")]
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
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
                    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
                ):
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
        return
    except FileExistsError as error:
        raise PackageError(f"{_shown(output.name)} already exists; choose a new name") from error
    except OSError:
        pass  # no hard links on this file system: copy into an exclusive new file below
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

    fd, name = tempfile.mkstemp(prefix=f".{output.name[:40]}.partial-", dir=output.parent)
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
                raise PackageError(
                    "git cannot read the experiment's repository: "
                    + probe.stderr.decode("utf-8", "replace").strip()[-300:]
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
            "git could not list the tracked files: "
            + listing.stderr.decode("utf-8", "replace").strip()[-300:]
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
            if name.casefold() not in _REFUSED_FOLDERS
            and name.casefold() not in _NOISE_FOLDERS
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
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
        ):
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
