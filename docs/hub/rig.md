# The rig side of the Experiment Hub

Status: development branch. Nothing here changes the default dashboard: `alhazen dashboard`
still opens the workspace at `/`, and every run, rig, calibration, reward and training rule
stays the launcher's. The hub adds a page (`/hub`), an API on the same loopback server
(`/api/hub/v1`) and an `alhazen hub` command. This page is the rig's design and its exact
local contract; the central API is [api-contract.md](api-contract.md).

## Modules and what each hides

| Module | Secret it hides | Price / not trusted for |
|---|---|---|
| `alhazen.hub.client` | URL canonicalisation, bearer header, redirect refusal, timeouts, typed `HubError` | stdlib only; no retry policy of its own |
| `alhazen.hub.credentials` | where the hub address, bearer and rig id live (`<workspace>/hub/`, 0700/0600) and the `epoch` fence | not protection from code running as the same OS user |
| `alhazen.hub.installation` | download, verification order, versioned unpack, trust record per SHA-256 | proves bytes, not safety of code |
| `alhazen.hub.source` | preview of an experiment's source, metadata suggestion, pack + upload of an approved list | archive rules are all `alhazen.hub.packages` |
| `alhazen.hub.sync` | session upload: preview binding, durable outbox, the background worker | no rig timing guarantee |
| `alhazen.hub.protocol` | the shared upload identity: canonical `manifest_sha256` and receipt checks (stdlib; the hub service uses the same definition) | none beyond the contract |
| `alhazen.hub.cli` | `alhazen hub` subcommands (stdlib argparse) | install registration is injected by `cli/main.py` |
| `alhazen.cli.workspace_hub` | joins the workspace (registration, DataView discovery, active run) with the hub modules; route table | the only module that imports both |

Layering: `alhazen.hub` sits directly below `alhazen.cli`. No `alhazen.hub` module imports
`alhazen.cli`, even lazily; `cli/dashboard.py` imports `cli/workspace_hub.py` only when a hub
route is first used (or with `--hub`); `cli/workspace.py` reads the install records only to
snapshot a hub-installed project's release at launch (cli -> hub), so a dashboard that never opens the hub page loads none
of it.

## Security rules the code enforces

- Browser authentication is unchanged: exact Host, matching Origin when present, and the
  dashboard's token in `X-Alhazen-Token` (or `?token=` on a download link). `/hub`,
  `/hub/assets/*` (a fixed table) and `/hub/bootstrap.json` need no token and hold nothing
  secret.
- One hub: a canonical `https://host[:port][/path]` (no user info, query, fragment, escapes or
  dot segments); `http://` only for a loopback host with an explicit development flag. TLS is
  verified. Redirects are never followed, so a password or bearer is never sent elsewhere.
- The bearer from `POST /auth/token` is stored server-side, owner-only, and never returned to
  the page. A 401 from the hub forgets it. Logout clears it locally even when the hub cannot be
  reached and reports `revoked: false`. A new hub address forgets the sign-in.
- Registration is a browser action on the hub's own page (it requires the hub's exact Origin,
  which the rig must not forge): the rig answers `POST /auth/register` with 409
  `register_on_hub` naming the hub's address. A 401 forgets only the bearer the refused request
  used (compare-and-delete), never a sign-in made since. Connecting to another hub first revokes
  the old bearer at the old hub (`previous_revoked`).
- The proxy is an allowlist of exact methods and route shapes (ids `[A-Za-z0-9_-]{1,128}`),
  each with its allowed query keys; only Accept, Content-Type/Length, the bearer and the upload
  chunk digest go upstream. The hub's status passes through (201, 202). Writes include
  `POST /data/sessions/{id}/reindex` (owner-only on the hub). The upload protocol
  (`/sessions/*`) is not proxied at all. `GET /guide` is answered locally as
  `{guide: global_guide()}`, for offline use.
  Downloads are relayed as attachments with a sandboxing CSP, `nosniff`, an allowlisted type
  and a sanitised file name.

## Installing a release

`POST /local/install {experiment_id, version_id, sha256, trust_code: true, python}`:

1. refused while a session runs (`run_active`) or without `trust_code: true`;
2. the hub's version record must list that exact SHA-256 (`hash_mismatch` otherwise);
3. the ZIP is downloaded (size cap, SHA-256 checked), `packages.inspect_bundle` validates it,
   its name/version must match the hub's record and the platform must be declared;
4. an `installing` record is written, then `packages.install_bundle(expected_sha256=...)` unpacks it into a new folder
   `<workspace>/hub/experiments/<name>/<version>-<sha12>`; an existing folder is never replaced;
   the declared files are made read-only (folders stay writable for data). A durability the
   file system could not confirm is recorded (`durable: false`, its note) rather than treated as
   failure (some network and virtual drives never confirm). An interrupted install is
   cleared only by `POST /local/install-recover {sha256}` through the package module's own
   recovery: its leftovers and an empty claim are removed, a committed tree is completed only if
   it holds exactly the release's declared files, and any other content is left untouched;
5. the trust acknowledgement is recorded with the digest, user and statement;
6. only now the chosen interpreter is probed (this imports code from the folder), checked with
   `packages.compatibility_problems`, and the folder is registered with the workspace. A refusal
   leaves the release `installed` with its error; the same request with another interpreter
   does not download again. The declared files are re-hashed before registration.

The existing experiment registrations, local experiments without a hub account and offline
runs are untouched.

### One data folder per experiment (import round decision 2)

A release runs in its own folder, so a rig whose `data_root` is relative (`data`, alhazen's
shared rigs and most experiments) would write into `<version>-<sha12>/data`: an upgrade would
start an empty `participants.tsv`, restarting counterbalancing and the subject/initials checks,
and removing an old release would take its data. Instead every data folder the release's rigs
write under (the first component of each relative `data_root`, and beside it `-rehearsal`,
`-training`, `-training-rehearsal`) is a directory link in the release folder to
`<workspace>/hub/experiments/<name>/<that name>` (`alhazen.hub.shared_data`; a junction on
Windows, which needs no privilege). The experiment's own alhazen, whatever its version, writes where
it always does; the release of each session is the run's `hub_release`, and `launch.json` also
records `hub_data_folder`. The data stays under the protocol version (`data/v<pyproject
version>/`), so a documentation-only release shares it.

The links are made at registration and checked before every launch. A release folder that
already holds a real data folder (installed before this rule) is moved there on first use,
never copied; when both it and the shared folder hold data the launch is refused, naming both,
and nothing is moved or merged. Removing a release folder removes its links, never the shared
data. An absolute `data_root` already names one folder and is left alone.

### The experiment's own rigs ship (decision 1)

`configs/rig-<name>.yaml` files are protocol and travel in the package, so an installed
`--rig lab` (or `lab-neural`) resolves to the package's file exactly as in a checkout. What a
rig measured stays local: `rig-*_gamma.yaml`, `rig-*.reward.yaml`, `rig-*.json` reports, any
rig file under a `measurements` folder or outside `configs/`.

## Run provenance

`Workspace.start` gives every NEW run of a project under `<workspace>/hub/experiments/` a
`hub_release` in both `run.json` and the write-once `launch.json`: hub base URL (no
credential), experiment and version ids, the source ZIP's SHA-256, name, version, when it was
trusted, and `files_verified` (the declared files re-hashed before spawn: true/false; null for
a release over 64 MiB, not hashed at launch). It comes from the trusted install record, read
from local files only; a hub folder with no usable record refuses the launch. Other projects'
runs get no key and are otherwise unchanged; older records are never backfilled. History
therefore never depends on the mutable `installs.json`.

An upload uses the release recorded with the run that wrote the session (found through the
run's console, as the Run page finds it): `release_source: "run_record"`. Otherwise the
folder's install record (`"install_record"`), otherwise the operator's choice (`"operator"`).
A different release, or a release from another hub, is refused.

`session.json` itself does not carry `hub_release`: it is written by the session engine in the
experiment's own interpreter (whose alhazen may be older) and is covered by the session's
manifest, so adding it would mean an engine change and a rewrite of hashed research files. The
link between a session and its release is the workspace's run record.

### Task parameters on a launch (decision 5)

`POST /api/runs` with a `parameter_set` label whose entry names a params file must carry that
launch's parameter text (`parameters_yaml` or `parameters`) or say `params: "default"`, which
reads the entry's own file and sends it as `--params`. A label alone is refused with a 400
naming the label and its file (it used to run the task's default file under the label's name).
`params: "default"` with text is refused too. The dashboard page always sends the text it
shows; an emptied editor sends no label. `run.json` records `params: {source, file, sha256}`
(`source`: "launch text", "parameter set file" with `file_sha256` of the shipped file, or "task
default" with the task's own file and its SHA-256 when run.py's table names it).

## Uploading a session

`POST /local/upload-preview` lists a completed session (its `manifest.yaml` exists; links
refuse it) found through the exact DataView roots, with its recipient (hub + account), release
and privacy warning, and returns an opaque `preview_id` (30 minutes, in memory) that binds hub,
collector id, experiment/version, the file list (path, size, mtime) and the session's stable
`client_session_id` (this rig's random id + the folder). `POST /local/upload` must name it with
`consent: true`; any difference means `preview_stale` or `auth_context_changed`.

The job is a file in `<workspace>/hub/outbox/`. One worker thread runs jobs one at a time:

- it waits (`waiting`) while a session runs on this workspace, before hashing and between
  chunks;
- before every request the stored sign-in must be the job's hub and account; otherwise the job
  pauses (`signed_out`, `auth_context_changed`) and never runs under another account; signing
  in again as the same account resumes it;
- it hashes each file once in 1 MiB blocks, waiting between blocks while a session runs, and
  refuses a file whose size or mtime changed (`local_changed`); the hashes must match the
  session's own manifest. File names the hub would refuse (not portable, not NFC, differing only
  in case) are found at preview (`unsupported_path`);
- it initialises the session (empty files are sent as one empty chunk), sends chunks of at most
  8 MiB or the hub's `max_chunk_bytes` from the hub's own received offsets with
  `X-Chunk-SHA256`, retries transient failures with the same identity (and
  `sealing_in_progress`), and is `completed` only for a `committed` receipt whose session id,
  client session id, experiment/version, file count, byte total and `manifest_sha256`
  (`alhazen.hub.protocol`) all match the bound upload; any difference fails the job as
  `receipt_mismatch` and the receipt is kept aside, never treated as completion;
- an attempt the hub closed (410 `upload_aborted`/`upload_expired`) is initialised again with
  the same `client_session_id` (the hub starts a new attempt for an identical replay); if the
  hub keeps answering closed, the job fails `upload_closed`, resumable;
- a complete answered `sealing_in_progress` (Retry-After honoured) or timed out is the same
  upload still sealing: it is asked again until the 35-minute bound, then the job pauses,
  resumable, with the same session;
- Cancel discards the hub's unfinished copy with the job's own bound credential, on the worker
  after the job stops (`remote_abort`: `aborted`, `pending` until that account is signed in
  and the hub reachable, `kept` for committed data, which is never aborted). The hub routes
  `GET /sessions` and `POST /sessions/{id}/abort` list and discard the user's own unfinished
  uploads; a discard there also cancels the matching local job;
- local files are never changed, moved or deleted. A restart pauses active jobs as
  `interrupted`; they resume for their own account. `client_session_id` comes from the
  session's own record (session.json experiment, subject/session/run, created) where present,
  so a moved data folder is the same upload.

## `alhazen hub`

`connect URL [--allow-http-loopback]`, `login [--username]` (password via getpass only),
`logout`, `status`, `pack DIR --output F [--license] [--hardware LIST] [--version X.Y.Z]
[--drop-not-for-rig] [--yes]`, `push F --experiment ID`,
`install EXP VER --sha256 S --python PY --trust-code`, `serve --config FILE` (needs the
service from the hub extra). It shares `--state-dir` with the dashboard; `install` takes the
workspace lock, so it refuses while a dashboard holds the workspace, and it starts no upload.

`pack` reads the hardware suggestion from the packed configuration (an eye tracker from the
experiment's own rig files, the reward line from monkey params files; files it could not read
are named), points the manifest at `docs/experiment.json` when it is packed, and marks files
that are probably not for the rig (tests, notebooks, `scripts/`, `.github`, `.githooks`,
`uv.lock`, rehearsal data, stimulus-check images): they are included unless the author drops
them (the prompt, or `--drop-not-for-rig`). `--version` sets a release version different from
the pyproject (protocol) version; the manifest then records `protocol_version` and the data
stays under the protocol's folder.

**Copying a rig state.** A `--state-dir` copied from another workspace carries its hub sign-in
(`hub/credential.json`, a bearer for that account) and its rig identity (`hub/rig.json`). Start
the copy with `alhazen dashboard --state-dir COPY --forget-hub-login`, which removes the
sign-in and keeps the hub address, and delete `hub/rig.json` from the copy so it gets its own
rig id; then sign in as the account the copy is for.

## Windows: how long a session's paths may be

Windows refuses a path of 260 characters or more unless an administrator has switched on long
paths (`LongPathsEnabled`), which most computers have not. An installed experiment writes
through its release folder, so every file of a session starts with

```
<workspace>\hub\experiments\<name>\<version>-<sha12>\<data root>\v<version>\sub-<ID>\ses-001\run-01_task-<task>\
```

and the hub's own part of that (`hub\experiments\` before the name, `\<version>-<sha12>` after
it) is about 35 characters a plain checkout does not have. The path Windows counts is this one,
through the link, not the shared folder the link leads to.

With the default workspace (`C:\Users\<user>\.alhazen\dashboard`) and the longest data root
(`data-training-rehearsal`), a session fits when

```
len(user) + len(name) + 2 * len(version) + 2 * len(subject ID) + len(task) <= 101
```

Typical names come to 60 to 80. When a session would not fit, an experiment on alhazen 2.14 or
newer refuses it before it starts and names the path and its length; nothing is lost. An
experiment that pins an older alhazen has no such check and fails when it writes its tables, at
the end of the session, so on Windows pin 2.14 or newer.

Three ways to make room, any one of which is enough:

- start the dashboard with a short state folder, `alhazen dashboard --hub --state-dir C:\ah`
  (23 characters back, plus the length of the user name);
- have an administrator switch on long paths, after which there is no limit to speak of:
  `reg add HKLM\SYSTEM\CurrentControlSet\Control\FileSystem /v LongPathsEnabled /t REG_DWORD /d 1 /f`;
- use shorter subject IDs or task names.

`tests/hub/test_live_roundtrip.py` installs a release and runs a session from a folder deeper
than the default workspace. It passes on a Windows 11 without long paths; CI's Windows runners
have long paths switched on, so they do not show this limit.

## Not verified here

The real central service and this adapter have not been run together in this branch: the
tests use a stand-in hub that implements the contract as written. Rig timing under concurrent
transfers has not been measured on hardware. Windows file permissions rely on the profile's
ACLs.
