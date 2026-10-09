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

## Layout

```
alhazen-package.json      the manifest, at the root
run.py                    the entry point (required)
...                       exactly the files the manifest declares
```

Nothing else may appear: no folder entries, no undeclared files, nothing
before the first member, between members, or after the end record (so no
archive comment). Members are stored or deflate-compressed, unencrypted,
regular files, without zip64.

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

Across a whole package, two paths that one common file system would treat as
one name (case, Unicode compatibility form), a folder spelled two ways, and a
path that is both a file and a folder are refused. A ZIP name that is not
ASCII must carry the UTF-8 flag.

## Limits

`inspect_bundle` defaults: 256 MiB archive, 1 GiB expanded, 10,000 files
(the server may pass its own). The member count is read from the end record
and refused before the directory is parsed; expansion is refused from the
declared sizes before anything is decompressed, and decompression never
reads past a member's declared size.

## Functions

```python
from pathlib import Path

from alhazen.hub.packages import (
    build_bundle,
    compatibility_problems,
    extract_bundle,
    inspect_bundle,
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
same = inspect_bundle(Path("my-experiment-1.0.0.zip"))
installed = extract_bundle(Path("my-experiment-1.0.0.zip"), Path("installed/my-experiment"))
problems = compatibility_problems(
    installed.manifest, python_version=(3, 11), alhazen_version="2.13.0", platform="linux"
)
```

- **`suggest_files(source)`**: in a git work tree, the tracked files (never
  untracked ones); otherwise every file. Leaves out links and anything reached
  through one, non-regular files, unportable paths, every class
  `build_bundle` refuses, files inside an environment, and build/cache/editor
  clutter (`build/`, `dist/`, `node_modules/`, `.pytest_cache/`, `.idea/`,
  `.DS_Store`, `movies/`...). Source, configs, lock files, Markdown,
  documentation JSON and diagram files stay in. A repository git cannot read
  is an error, not a silent switch to listing every file.
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
- **`inspect_bundle(path, ...)`**: hashes the whole file, then checks the
  structure, the manifest, the member set and every file's bytes. A file that
  changes while being read is refused.
- **`extract_bundle(path, destination)`**: claims `destination` by creating
  it (refused if anything exists there, even an empty folder or a broken
  link), copies the package into a private staging folder beside it,
  verifies the copy, writes each file (hash checked again, synced, mode 0644,
  never executable), and renames the finished tree onto the claimed name. On
  failure it removes only its staging folder and its own empty claim.
- **`compatibility_problems(manifest, ...)`**: the declared requirements
  that the chosen interpreter, installed alhazen or platform does not meet,
  as sentences. A development build counts as its release number; an unknown
  alhazen version is reported.

`PackageError` (a `ValueError` and an `AlhazenError`) is the one refusal
type. Its messages name archive members (escaped) and file names, never
absolute local paths, so the server may return them.

## Refused file classes

| Class | Matched by |
|---|---|
| Version control | a `.git`, `.hg`, `.svn`, `.bzr` folder |
| Environments | `.venv`, `venv`, `virtualenv`, `.tox`, `.nox`, `.conda`, `conda-meta`, `site-packages` folders; any folder with `pyvenv.cfg` |
| Bytecode | `__pycache__`, `*.pyc`, `*.pyo` |
| Credentials and keys | `.env`, `.env.*`, `*.env` (not `.env.example`/`.sample`/`.template`); `.ssh`, `.gnupg`, `.aws`, `.azure`, `.gcloud`, `.kube`, `.docker` folders; `.netrc`, `.pgpass`, `.pypirc`, `.npmrc`, `.git-credentials`, `.htpasswd`, `known_hosts`, `authorized_keys`, `id_rsa`/`id_dsa`/`id_ecdsa`/`id_ed25519`; `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, `*.kdbx`, `*.ppk`, `*.ovpn`; names with the word secret(s), credential(s) or token(s) |
| Collected data | top-level `data`, `data-*`, `data_*`, `people` folders; `sub-<id>` folders anywhere; `participants.tsv/.json`, `subjects.csv`, `experimenters.csv`; databases `*.sqlite`, `*.sqlite3`, `*.db` and their `-wal`/`-shm`/`-journal`; eye-tracker recordings `*.edf`, `*.asc` |
| Rig-specific | `rig-*.yaml`, `rig-*.yml`, `rig-*.json` anywhere (rig files, gamma and reward calibrations, measurement reports): each lab runs on its own rig |
| Local state | names starting `.alhazen`; the manifest's own name |

A package's own `src/<package>/data/` folder is code, not a data root, and
stays allowed.
