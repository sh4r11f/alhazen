# Experiment hub service (operator and integrator notes)

The central hub is an optional service a lab runs on one host: accounts, the
experiment catalogue, immutable package versions, per-user libraries and each
collector's private session data. Rigs never need it to run experiments. This
page covers what is implemented on the development branch and how it is meant
to be operated. **Nothing here is deployed**; production hosting, durability
and a restore drill remain unverified until an approved deployment does them.

The wire contract is [api-contract.md](api-contract.md); the design and its
accepted review gates are [design.md](design.md). Package format:
[packages.md](packages.md).

## Install and run (local development)

```
pip install "alhazen-vision[hub]"
```

```python
from pathlib import Path
from alhazen.hub import admin
from alhazen.hub.app import create_app, serve
from alhazen.hub.settings import HubSettings, load_settings

settings = HubSettings.for_development(Path("/tmp/hub-dev"))   # SQLite + artifacts under it
admin.init_database(settings)                                   # once; never automatic
print(admin.create_invite(settings, actor="me").code)           # shown once
app = create_app(settings)                                      # or: serve(Path("hub.toml"))
```

`serve(config_path, host="127.0.0.1", port=8750)` runs uvicorn with **one
worker process**. Public entry points:

| Function | Purpose |
|---|---|
| `alhazen.hub.settings.load_settings(path, environ=None)` | Read and validate a TOML configuration |
| `HubSettings.for_development(root, *, public_origin=...)` | Local SQLite settings |
| `alhazen.hub.app.create_app(settings, *, clock=..., start_maintenance=True)` | The ASGI app; refuses a database not at the current schema |
| `alhazen.hub.app.serve(config_path, host, port)` | Run it (single process) |
| `alhazen.hub.admin.init_database / migrate_database` | Create / upgrade the schema, explicitly |
| `admin.create_invite / list_invites / revoke_invite` | Single-use, expiring, hashed invites |
| `admin.reset_password / disable_user / enable_user` | Recovery; revokes every session; audited |
| `admin.reconcile / reindex_sessions / expire_stale_uploads` | Maintenance on demand |

The same operations from a shell on the hub host (passwords via getpass):

```
python -m alhazen.hub.admin --config PRIVATE.toml init-db
python -m alhazen.hub.admin --config PRIVATE.toml migrate
python -m alhazen.hub.admin --config PRIVATE.toml --actor NAME invite --note "who" --days 7
python -m alhazen.hub.admin --config PRIVATE.toml invites
python -m alhazen.hub.admin --config PRIVATE.toml --actor NAME revoke-invite INVITE_ID
python -m alhazen.hub.admin --config PRIVATE.toml --actor NAME reset-password USERNAME
python -m alhazen.hub.admin --config PRIVATE.toml --actor NAME disable USERNAME   # enable USERNAME
python -m alhazen.hub.admin --config PRIVATE.toml reconcile
python -m alhazen.hub.admin --config PRIVATE.toml reindex [SESSION_ID]
python -m alhazen.hub.admin --config PRIVATE.toml expire-uploads
```

Every admin action takes an `actor` label and is written to the audit table.
There is no email: invite codes and reset passwords are shown once to the
operator, who passes them on through a channel they trust.

## Configuration

One TOML file, kept **outside every repository**. Unknown keys are refused.
Template (placeholders only):

```toml
[server]
public_origin = "https://HUB-HOST"        # the exact browser origin; http only for loopback
extra_origins = []
forwarded_allow_ips = ""                  # trust X-Forwarded-* only from these proxies

[database]
url_env = "ALHAZEN_HUB_DATABASE_URL"      # postgresql://USER@/DB?host=SOCKET-DIR, from the environment

[storage]
artifact_root = "/ABSOLUTE/ARCHIVE/PATH"  # one filesystem; never the database's own storage
min_free_bytes = 1073741824

[limits]                                  # all optional; defaults shown in settings.HubLimits
user_quota_bytes = 53687091200            # 50 GiB reserved + stored per owner
max_staging_sessions = 3

[auth]                                    # all optional; defaults in settings.AuthPolicy
browser_idle_seconds = 3600
```

`HubSettings` hides the database URL from `repr`. Prefer `url_env` so the file
itself carries no secret.

## Process model

**One server process** (`serve` pins `workers=1`). Every correctness rule that
spans requests lives in the database, so it would also hold across processes:
chunk offsets and seals use row locks (`SELECT ... FOR UPDATE` on PostgreSQL),
quota decisions lock the owner's row, invites are consumed by a conditional
update, seals and index jobs are claimed with leases. What is per process:

- admission gates: concurrent transfers (chunks, package uploads, seals;
  default 4), exports (2) and password hashes (2), each refusing with `429`
  and `Retry-After` rather than queueing;
- one maintenance thread: reconciliation, expiry of stale uploads, throttle
  pruning and the trial index queue (so at most one index job at a time).

Running several processes would multiply those bounds; it is not the
supported pilot configuration.

SQLite is for local development with one process on a local disk. It takes
the database write lock for every transaction (`BEGIN IMMEDIATE`), which
serializes everything, including a chunk's disk write and hash. Never put a
live SQLite database on shared or network storage.

## Schema and migrations

`schema.METADATA` always describes the newest schema (`SCHEMA_VERSION`, now 2).
Schema 2 (data review fixes) adds nullable `hub_sessions` columns:
`seal_token`, `problem_code`, `problem_at`, `previous_attempt_id`,
`retired_client_id`, `index_token`. To bring a schema-1 database forward:

```
pg_dump -Fc ... > hub-before-v2.dump          # or copy the SQLite file while stopped
python -m alhazen.hub.admin --config PRIVATE.toml migrate
```
`admin.init_database` creates it in an empty database (idempotent at the same
version, refuses any other existing state). `admin.migrate_database` applies
explicit N→N+1 steps in one transaction; take a backup first. `create_app`
refuses to start on a missing, older or newer schema and never alters it.
Nothing drops a table holding user data.

## Accounts and sessions

- Passwords: Argon2id, 64 MiB, t=3, p=1; 12–1024 characters.
- Sessions are opaque random tokens; only their SHA-256 is stored. Browser
  sessions end 12 h after sign-in or 1 h after last use; bearer tokens (rig,
  CLI) 12 h after issue. Logout, password reset and disabling an account
  revoke at once.
- Browser: `POST /auth/login` (exact `Origin` required, JSON only) sets an
  `HttpOnly; SameSite=Strict; Path=/` cookie, `__Host-` prefixed and `Secure`
  under HTTPS. Every cookie-authenticated write needs the exact `Origin` and
  the session's `X-CSRF-Token` (from login or `GET /auth/me`).
- Rig/CLI: `POST /auth/token` returns a bearer and never sets a cookie; it
  refuses a request carrying a cookie or another token. A request with both a
  cookie and a bearer is refused (`ambiguous_credentials`).
- Admission: 5 failed sign-ins per account and 20 per client address in 15
  minutes, 60 password hashes per minute service-wide, 20 registrations per
  address per hour. All windows slide; nothing locks permanently. Unknown
  usernames cost the same hash and get the same answer.
- Client addresses come from the socket unless `forwarded_allow_ips` names a
  trusted proxy.

## Catalogue, versions and publication

- Versions are immutable: one version string per experiment, one ZIP. An
  identical re-upload returns the stored version (`200`); different bytes are
  `409 version_exists`. Every version of an experiment has the package name
  of its first.
- Uploads stream to a temporary file (capped while reading, not by
  `Content-Length`), pass `packages.inspect_bundle`, and — when the manifest
  points to documentation — `documentation.read_documentation`, before they
  are stored. The file is installed before its row commits, and removed if
  the commit fails.
- Publishing snapshots all public metadata with the chosen version (review
  gate M2): editing the experiment or adding versions changes nothing public
  until the next publish. It needs `license_ack` and `data_excluded_ack`, and
  the experiment's licence must equal the package's declared licence.
- Visitors see only published snapshots; an unpublished or private object is
  the same `404` as a missing one. Download and documentation share one ACL:
  the owner, or anyone for the currently published version.
- A library entry pins one version per experiment. It stays listed after an
  unpublish (`available: false`) with the title the user saw.

## Session upload protocol

`POST /sessions/init` → `PUT /sessions/{id}/files?path=&offset=` (≤ 8 MiB,
`X-Chunk-SHA256`) → `POST /sessions/{id}/complete`. Init and progress
(`GET /sessions/{id}/upload`) return the same shape:
`{id, experiment_id, version_id, client_session_id, status, metadata,
created_at, updated_at, completed_at, manifest_sha256, total_bytes,
file_count, previous_attempt_id, problem, index, files:[{path, size, sha256,
received, verified}], received_bytes}`.

States (review gate B1): `staging → sealing → committed`; `aborted`
(`POST /sessions/{id}/abort`) and `expired` (untouched for 7 days) release the
reservation and delete only the partial bytes.

- **Identity.** `manifest_sha256` is `alhazen.hub.protocol.manifest_sha256`
  (one implementation for hub and rig): SHA-256 of the compact, key-sorted
  UTF-8 JSON of `{experiment_id, version_id, files (sorted by path, each
  {path, sha256, size}), metadata}`, `metadata` holding exactly
  `subject_code, mode, rig_alias, started_at` (absent ones `null`).
- **One open or committed upload per (collector, client_session_id).**
  An identical init returns it (`200`). Different content is
  `409 session_conflict` (`error.session_id`, `error.status`): abort an
  open upload first; a committed session is never replaced.
- **Retrying a closed upload.** After `aborted` or `expired`, an init with the
  same `client_session_id` starts a NEW attempt (`201`, new id,
  `previous_attempt_id` = the closed one), with or without the same content.
  The closed row is kept unchanged as history (its `client_session_id` still
  reads the original; only its unique key is retired). Quota and the
  open-upload limit apply to the new attempt as to any init. A client that
  gets `410 upload_aborted` / `upload_expired` on PUT or complete clears its
  stored session id and calls init again with the same client id. Whether
  new content needs fresh consent is the client's rule (the rig's preview
  binding); the hub only refuses replacing an open or committed upload.
- **Reservation.** The whole session's bytes are reserved against the
  collector's quota (sessions and package versions together) and the disk's
  free space in the transaction that creates it.
- **Empty files.** A `size: 0` file must declare the empty digest
  (`e3b0c442…b855`); it is created (synced) and marked verified at init and
  needs no chunk. An empty PUT at offset 0 is a harmless replay.
- **Chunks.** One session's chunks and its seal claim are serialized by a row
  lock. The server truncates any unacknowledged tail, writes, fsyncs, and only
  then records the chunk `(path, offset, length, sha256)` and the new
  `received`. Retrying a recorded chunk is a replay (`replay: true`); other
  bytes at a covered offset are `409 chunk_conflict`; any other offset is
  `409 offset_mismatch` with `error.received`. A file of at most 64 MiB is
  hashed whole at its last byte (`409 file_hash_mismatch`, resend from 0);
  larger files only at the seal, so no PUT does long work.
- **Seal (fenced).** `complete` requires every file fully received, then
  claims the seal with a random token and a 30-minute lease. The sealer
  renews the lease between files and every 256 MiB it hashes; renewal,
  commit, reset, lease release and problem marks are database updates
  conditional on the token, so a sealer whose claim was taken over can
  change nothing and reports the current state instead. It re-hashes every
  staged file, writes `manifest.json`, fsyncs files and folders, renames the
  tree into `sessions/<experiment>/<session>/` on the same filesystem, fsyncs
  the parents, and only then commits. A staged file that vanished means
  another sealer installed the tree: it is verified, never called bad. An
  existing final that differs is never overwritten: the upload is marked
  `problem.code = "artifact_conflict"` (`409`), stays out of reconciliation
  retries, and its owner may abort it (the disputed copy stays for the
  operator).
- **Answers.** `200` with the receipt once committed; `202 {…, status:
  "sealing", retry_after: 5}` with `Retry-After` while the seal runs (the
  route waits up to 10 s, then the seal continues in the background); call
  complete again or poll `GET /sessions/{id}/upload`. A failed commit
  transaction releases the claim, so a retry may seal at once.
- **Receipt.** `{id, status: "committed", client_session_id, experiment_id,
  version_id, manifest_sha256, file_count, total_bytes, completed_at,
  durability, problem, index}` certifies verified files on the hub's primary
  storage. It is **not** a backup; rigs keep originals. If reconciliation
  later finds the stored files missing, the receipt and data detail say so
  (`problem.code = "artifact_missing"`, `durability` changed) until they are
  restored.
- **Unfinished uploads.** `GET /sessions?limit=&offset=` → `{items,
  next_offset}`: the caller's `staging` and `sealing` uploads (session shape
  plus `received_bytes`, `files_verified`, `sealing_active`, `expires_at`,
  `expired`, `can_abort`). `POST /sessions/{id}/abort` closes a staging upload
  (or one stuck on `artifact_conflict`), never a live seal or committed data.
- **Reconciliation** (maintenance, every 10 minutes and on demand) takes over
  seals whose lease ran out and finishes them; marks committed sessions whose
  files are missing (and clears the mark when restored); removes staging
  leftovers of committed, aborted or expired sessions and interrupted package
  uploads older than a day; reports orphan session folders, unknown staging
  folders, orphan release files and stuck seals. `/readyz` shows counts only;
  `admin.reconcile` and the log carry the ids.

## Data, trial index and exports

Only committed sessions of the caller appear under `/data`. The trial index is
derived: after commit, the maintenance worker parses every `trials.csv` /
`*_trials.csv` within budgets (100,000 rows, 256 columns, 1 MiB per record,
64 KiB per cell, 512 MiB per file; UTF-8). Exceeding one marks the index
`failed` with a message and keeps no rows; the session and its files stay
available. `POST /data/sessions/{id}/reindex` (owner) or
`admin.reindex_sessions` rebuilds it from the raw files.

Trial pages carry at most 4 MiB of row values (always at least one row);
use the returned `next_offset`. Index jobs hold a token: a rebuild requested
while one runs fences it and runs afterwards, and a transient database error
returns the job to `pending` instead of failing it. Exports read the session's
index state, columns and rows in ONE snapshot (REPEATABLE READ on PostgreSQL)
so a concurrent rebuild cannot mix generations; `trials.open_export` reads the
first part before the response starts (a not-ready index is a `409`) and
returns an `ExportStream` whose `close()` releases the snapshot at once,
whether the stream finished, was abandoned or never started.

Exports stream from the index. CSV cells starting with `= + - @`, tab or
carriage return are prefixed with `'` unless they are plain numbers. Original
files download only by a path the session's manifest names, as
`application/octet-stream` attachments with `Content-Security-Policy: sandbox`.

## Errors

`{error: {code, message, ...}}`; messages never contain paths, SQL or library
exceptions. Codes clients branch on: `not_authenticated` 401, `csrf_failed` /
`origin_required` / `origin_not_allowed` 403, `not_found` 404, `session_conflict`
/ `chunk_conflict` / `offset_mismatch` / `file_hash_mismatch` /
`session_incomplete` / `sealing_in_progress` / `version_exists` /
`package_name_mismatch` / `license_mismatch` / `index_not_ready` 409,
`upload_expired` / `upload_aborted` 410, `payload_too_large` / `quota_exceeded` /
`version_limit` / `experiment_limit` 413, `unsupported_media_type` 415,
`invalid_package` / `invalid_documentation` 422, `rate_limited` / `server_busy` /
`upload_limit` 429, `database_unavailable` / `documentation_unavailable` 503,
`insufficient_storage` 507.

## Backup and restore (operator guidance; no drill has been run)

The relational database and the artifact root together are the hub's state.
Neither is a backup of the other, and an upload receipt is not a backup.

1. Dump the database first (`pg_dump -Fc`), then copy the artifact root
   (`rsync -a`). Committed artifacts are made durable before their rows
   commit and are never modified or deleted by the service, so every
   committed row in the dump has its files in the later copy. Extra folders
   in the copy show up as orphans in reconciliation and are harmless.
   Uploads in progress during a backup may need their unfinished files sent
   again after a restore; the protocol detects and asks for that.
2. Restore: create an empty database, `pg_restore`, copy the artifact root to
   the configured path, start the service (the schema check runs), then
   `admin.reconcile(settings)` and expect `missing_artifacts == 0`.
3. SQLite (development only): stop the service, copy the file, copy the root.

Keep backups on storage approved for the data; scratch or node-local storage
on a scheduled cluster is purged and is not an archive.

## Not done here

- No deployment, TLS termination, tunnel, cluster job script or restore drill.
- No email, open registration, organizations or admin web UI.
- Multi-process deployment is not supported (see Process model).
- The web interface's files (`hub/assets/`) come from the UI work; without
  them `/` answers `503` with a development message.
