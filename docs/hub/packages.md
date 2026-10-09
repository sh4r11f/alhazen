# Experiment packages (`alhazen.hub.packages`)

An experiment release travels as one ZIP. The SHA-256 of the whole ZIP is the
release's identity: the hub stores it unchanged, a library pins it, a rig
installs only the bytes with that digest. This page is the format and the
rules; the hub API is in [api-contract.md](api-contract.md).

## Trust boundary

Every check here is about the *archive*: what files it holds, where they
would land, and whether their bytes are the declared ones. None of it is a
review of the code. A package that passes can do anything once someone runs
it, as that person, with their files, credentials, data and devices; a
virtual environment is not a sandbox. Nothing in this module imports,
compiles or runs package content, and the hub never does either.

The sensitive-file rules refuse known *classes* of file by name (keys,
credentials, data folders, registries, rig files). They cannot prove that the
chosen source, README or documentation holds no secret or participant data:
the author reviews the exact file list before building, and publishing asks
for a separate confirmation.

## Layout: a small, strict ZIP subset

```
alhazen-package.json      the manifest, at the root
run.py                    the entry point (required)
...                       exactly the files the manifest declares
```

Packages use only the part of ZIP that `build_bundle` writes, and this
module reads them with its own parser (Python's `zipfile` is used only to
write), so every reader and every supported Python sees the same names and
bytes:

- members stored or deflated, nothing else; no encryption, no zip64, ZIP
  version 2.0 at most; flags limited to UTF-8 names (and deflate's level
  bits);
- **no extra fields** in local or central headers (an Info-ZIP Unicode path
  field, for example, can give a member a second name that other tools use);
- no data descriptors: each local header states the same CRC and sizes as
  its central-directory entry, along with the same flags, method and name;
- members laid end to end from byte 0, the central directory straight after
  in the same order, then the end record with no comment: no hidden bytes
  anywhere;
- every deflate stream ends exactly at its member's end, decodes to exactly
  the declared size and matches its CRC; a stored member's two sizes agree;
- no folder entries, links, devices or encrypted members; a non-ASCII name
  carries the UTF-8 flag.

The whole file is read once, in order, by `inspect_bundle`: the SHA-256 it
returns is computed over exactly the bytes it checked.

## Manifest (schema 1)

| Field | Type and rule |
|---|---|
| `schema_version` | `1`. Any other value is refused: a later format is a new schema. |
| `name` | slug: lowercase letters and digits joined by single hyphens, at most 64 (`kde-vergence`) |
| `version` | `MAJOR.MINOR.PATCH`, digits only (`1.2.3`; no pre-release suffix) |
| `title` | text, 1 to 200 characters, single line |
| `description` | text, up to 20,000 characters, may be empty; newlines and tabs allowed |
| `entrypoint` | `"run.py"`, which must be a declared file |
| `python_min` | `"3.N"`, at least `"3.10"` |
| `alhazen_min` | `MAJOR.MINOR.PATCH`, at least `"2.13.0"` |
| `platforms` | non-empty list, no repeats, from `linux`, `darwin`, `win32` |
| `hardware` | exactly `{display, eye_tracker, reward}`, each `true`/`false` (a declaration, not a check) |
| `license` | text, 1 to 200 characters |
| `citations` | list of at most 200 texts, each up to 2,000 characters |
| `documentation` | optional: a declared `.json` file of at most 4 MiB, normally `docs/experiment.json` |
| `files` | non-empty list of `{path, size, sha256}`; sha256 is 64 lowercase hex digits |

Unknown fields are refused. Integers are JSON integers (not `true`, not
`1.0`); `NaN`, `Infinity`, out-of-range numbers and duplicate keys are
refused; the manifest must be UTF-8 without a byte-order mark and at most
8 MiB. Text fields refuse control, unassigned and bidirectional-override
characters. Strings are data: show them as text, never as HTML.

`documentation` is only a pointer here: this module checks that it is a
safe path to a declared, bounded JSON file covered by the package hashes.
What the descriptor means belongs to `alhazen.hub.documentation`. A package
without it is valid.

## Paths

`safe_relative` accepts a relative POSIX path and returns it unchanged, or
raises. Refused, never repaired:

- absolute paths, `.` / `..` / empty components, a trailing `/`;
- backslashes, drive letters, `:` and the other characters Windows forbids;
- control and invisible characters, non-ASCII spaces;
- names that begin or end with a space or end with a dot;
- Windows device names, with or without an extension (`con`, `nul.txt`, `COM1`);
- names over 255 bytes or paths over 1,024 characters;
- text not in Unicode NFC.

- a `~` followed by a digit in a name (it can collide with a Windows 8.3
  short name such as `PROGRA~1`).

Across a whole package, two paths that one common file system would treat as
one name (case, including NTFS upper-case matching such as dotless `ı` and
`I`, or Unicode compatibility form), a folder spelled two ways, and a path
that is both a file and a folder are refused. Package paths are at most 180
characters, so that joined to an install folder they stay within Windows'
260-character limit; on Windows an install whose joined paths would reach
260 characters is refused before anything is written.

## Limits

256 MiB archive, 1 GiB expanded, 10,000 files. These defaults are the
format's maxima: `inspect_bundle`, `stage_bundle`, `install_bundle` and
`extract_bundle` accept lower limits and refuse higher ones (ValueError), so
a hub can never accept a release that a rig cannot install. The member count is read from the end record
and refused before the directory is parsed; expansion is refused from the
declared sizes before anything is decompressed, and decompression never
reads past a member's declared size.

## Functions

```python
from pathlib import Path

from alhazen.hub.packages import (
    InstallInterrupted,
    InstallNotDurable,
    build_bundle,
    compatibility_problems,
    inspect_bundle,
    install_bundle,
    recover_install,
    stage_bundle,
    suggest_files,
)

source = Path("my-experiment")
files = suggest_files(source)  # show this list to the author first
metadata = {
    "name": "my-experiment",
    "version": "1.0.0",
    "title": "My experiment",
    "description": "",
    "hardware": {"display": True, "eye_tracker": True, "reward": False},
    "license": "MIT",
    "documentation": "docs/experiment.json",
}
info = build_bundle(source, Path("my-experiment-1.0.0.zip"), metadata, files)
staged = stage_bundle(Path("upload.part"), Path("staging"), expected_sha256=info.sha256)

destination = Path("installed/my-experiment-1.0.0")
try:
    result = install_bundle(staged.path, destination, expected_sha256=info.sha256)
except InstallInterrupted:
    recover_install(destination)  # clears only that install's own leftovers
    result = install_bundle(staged.path, destination, expected_sha256=info.sha256)
if not result.durable:
    print("installed; not confirmed on disk:", result.durability_note)
problems = compatibility_problems(
    result.info.manifest, python_version=(3, 11), alhazen_version="2.13.0", platform="linux"
)
```

- **`suggest_files(source)`**: in a git work tree, the tracked files (never
  untracked ones); otherwise every file. Leaves out links and anything reached
  through one, non-regular files, unportable paths, every class
  `build_bundle` refuses, files inside an environment, and build/cache/editor
  clutter (`build/`, `dist/`, `node_modules/`, `.pytest_cache/`, `.idea/`,
  `.DS_Store`, `movies/`...). Source, configs, lock files, Markdown,
  documentation JSON and diagram files stay in. A repository git cannot read
  is an error (whose message repeats none of git's paths), not a silent
  switch to listing every file.
- **`build_bundle(source, output, metadata, files)`**: packages exactly
  `files`. `metadata` is the manifest minus `schema_version` and `files`;
  `entrypoint`, `python_min`, `alhazen_min`, `platforms` and `citations`
  default to `run.py`, `3.10`, `2.13.0`, all three platforms and none. Each
  file is opened without following links (on POSIX every folder on the way is
  opened by descriptor too), read once to hash and once to write, and refused
  if it changes in between. The archive is written beside `output`, verified
  with `inspect_bundle`, and only then given its name, never replacing an
  existing file. Output is deterministic for one Python and zlib: fixed
  timestamps and permissions, sorted members, canonical manifest JSON.
- **`inspect_bundle(path, *, max_archive_bytes, max_expanded_bytes,
  max_files) -> PackageInfo`**: one ordered pass, as above. A path that is
  not a regular file (a FIFO, a folder) is refused without blocking.
- **`stage_bundle(source, staging_dir, *, expected_sha256=None, limits...)
  -> VerifiedBundle(path, info)`**: copies `source` once into a new 0600 file
  in `staging_dir`, compares `expected_sha256` against the copied bytes, then
  verifies the copy. The caller keeps the copy (a server files it by digest)
  and never re-reads the mutable original. Refusals remove the copy.
- **`install_bundle(path, destination, *, expected_sha256=None, limits...)
  -> InstallResult(info, destination, durable, durability_note)`**, in its
  parent folder:
  1. refuse if `.<name>.alhazen-install` exists (`InstallInterrupted`) or
     `destination` exists (even an empty folder or a broken link);
  2. create, lock and sync the owner record `.<name>.alhazen-install`;
  3. claim `destination` by creating it empty;
  4. copy the package to `.<name>.<nonce>.package` (0600), compare
     `expected_sha256` (`DigestMismatch`, before anything is extracted),
     verify the copy;
  5. write each file into `.<name>.<nonce>.staging`, decoding and checking it
     again (mode 0644, never executable), syncing files and folders;
  6. remove the copy and rename the staging folder onto the claim. **The
     rename is the commit**: before it nothing is installed and a failure
     raises; after it the function returns, never raises;
  7. sync the parent and remove the record.
  `durable` is True only if every file and folder entry, including the
  rename and the record's removal, was synced and the OS confirmed it. A
  file system that refuses directory syncs (EINVAL/ENOTSUP) and Windows,
  where Python cannot sync a folder, give `durable=False` with a note. On a
  failure before the commit every leftover is removed independently; a
  cleanup failure is attached as a note to the original error (which is the
  one raised) and keeps the record so `recover_install` can finish.
- **`extract_bundle(path, destination, *, expected_sha256=None, limits...)
  -> PackageInfo`**: `install_bundle`, but raises `InstallNotDurable` (whose
  `result` says the files ARE installed) instead of returning an install
  whose durability was not confirmed. On Windows that is every install, so
  rig code should call `install_bundle` and show the durability state.
- **`recover_install(destination) -> RecoveryResult(record_found,
  destination_state, removed)`**: acts only through the owner record, and is
  refused with `InstallInProgress` while the installer still holds the
  record's lock (released by the OS when the process ends). Removes the copy
  and staging folder whose names derive from the record's nonce (never
  names read from it; a link or other non-file at those names is refused and
  left in place), and the destination only if it is an empty folder.
  `destination_state` is `absent`, `claim-removed`, `installed` (the rename
  had committed; the tree is kept, its durability unconfirmed) or `kept`
  (no record, or something else is there; untouched). Safe to repeat.
- **`compatibility_problems(manifest, ...)`**: the declared requirements
  that the chosen interpreter, installed alhazen or platform does not meet,
  as sentences. A development build counts as its release number; an unknown
  alhazen version is reported.

`PackageError` (a `ValueError` and an `AlhazenError`) is the one refusal
type; `DigestMismatch`, `InstallInterrupted`, `InstallInProgress` and
`InstallNotDurable` are subclasses. Messages name archive members (escaped)
and file names, never absolute local paths, so the server may return them.

## Limits of what was verified

Checked on Linux with Python 3.10 to 3.13. Not verified on real Windows
(8.3 short names, NTFS case table, the 260-character limit, reparse-point
tags, the rename retry for briefly held handles, `msvcrt` record locking) or
macOS, or on network/FUSE mounts (directory sync, `flock`). On those the
module fails closed (refuses, or reports `durable=False`) rather than
claiming more.

## Refused file classes

| Class | Matched by |
|---|---|
| Version control | a `.git`, `.hg`, `.svn`, `.bzr` folder; a `.git` file (a worktree or submodule pointer, holding local paths) at any depth |
| Environments | `.venv`, `venv`, `virtualenv`, `.tox`, `.nox`, `.conda`, `conda-meta`, `site-packages` folders; any folder with `pyvenv.cfg` |
| Bytecode | `__pycache__`, `*.pyc`, `*.pyo` |
| Credentials and keys | `.env`, `.env.*`, `*.env` (not `.env.example`/`.sample`/`.template`); `.ssh`, `.gnupg`, `.aws`, `.azure`, `.gcloud`, `.kube`, `.docker` folders; `.netrc`, `.pgpass`, `.pypirc`, `.npmrc`, `.git-credentials`, `.htpasswd`, `known_hosts`, `authorized_keys`, `id_rsa`/`id_dsa`/`id_ecdsa`/`id_ed25519`; `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, `*.kdbx`, `*.ppk`, `*.ovpn`; names with the word secret(s), credential(s) or token(s) |
| Collected data | top-level `data`, `data-*`, `people` folders; any folder or file named `sub-...` anywhere, whatever the subject code (non-ASCII and compatibility forms included); `participants.tsv/.json`, `subjects.csv`, `experimenters.csv`; databases `*.sqlite`, `*.sqlite3`, `*.db` and their `-wal`/`-shm`/`-journal`; eye-tracker recordings `*.edf`, `*.asc` |
| Rig-specific | `rig-*.yaml`, `rig-*.yml`, `rig-*.json` anywhere (rig files, gamma and reward calibrations, measurement reports): each lab runs on its own rig |
| Local state | names starting `.alhazen`; the manifest's own name |

A package's own `src/<package>/data/` folder is code, not a data root, and
stays allowed.
