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
| `alhazen.hub.cli` | `alhazen hub` subcommands (stdlib argparse) | install registration is injected by `cli/main.py` |
| `alhazen.cli.workspace_hub` | joins the workspace (registration, DataView discovery, active run) with the hub modules; route table | the only module that imports both |

Layering: `alhazen.hub` sits directly below `alhazen.cli`. No `alhazen.hub` module imports
`alhazen.cli`, even lazily; `cli/dashboard.py` imports `cli/workspace_hub.py` only when a hub
route is first used (or with `--hub`), so a dashboard that never opens the hub page loads none
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
- The proxy is an allowlist of exact methods and route shapes (ids `[A-Za-z0-9_-]{1,128}`),
  each with its allowed query keys; only Accept, Content-Type/Length, the bearer and the upload
  chunk digest go upstream. The upload protocol (`/sessions/*`) is not proxied at all.
  Downloads are relayed as attachments with a sandboxing CSP, `nosniff`, an allowlisted type
  and a sanitised file name.

## Installing a release

`POST /local/install {experiment_id, version_id, sha256, trust_code: true, python}`:

1. refused while a session runs (`run_active`) or without `trust_code: true`;
2. the hub's version record must list that exact SHA-256 (`hash_mismatch` otherwise);
3. the ZIP is downloaded (size cap, SHA-256 checked), `packages.inspect_bundle` validates it,
   its name/version must match the hub's record and the platform must be declared;
4. `packages.extract_bundle` unpacks it into a new folder
   `<workspace>/hub/experiments/<name>/<version>-<sha12>`; an existing folder is never replaced;
   the declared files are made read-only (folders stay writable for data);
5. the trust acknowledgement is recorded with the digest, user and statement;
6. only now the chosen interpreter is probed (this imports code from the folder), checked with
   `packages.compatibility_problems`, and the folder is registered with the workspace. A refusal
   leaves the release `installed` with its error; the same request with another interpreter
   does not download again. The declared files are re-hashed before registration.

The existing experiment registrations, local experiments without a hub account and offline
runs are untouched. A run of an installed release is tied to its immutable folder, so the
release is known from `installs.json` (`path` -> `sha256`, `version_id`).

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
- it hashes each file once, refusing a file whose size or mtime changed since the preview
  (`local_changed`), and checks the hashes against the session's own manifest;
- it initialises the session, sends 8 MiB chunks from the hub's own received offsets with
  `X-Chunk-SHA256`, retries transient failures with the same identity, and is `completed` only
  when `/complete` answers `committed` (staging/sealing are retried, anything else fails);
- local files are never changed, moved or deleted. A restart pauses active jobs as
  `interrupted`; they resume for their own account.

## `alhazen hub`

`connect URL [--allow-http-loopback]`, `login [--username]` (password via getpass only),
`logout`, `status`, `pack DIR --output F [--license] [--yes]`, `push F --experiment ID`,
`install EXP VER --sha256 S --python PY --trust-code`, `serve --config FILE` (needs the
service from the hub extra). It shares `--state-dir` with the dashboard; `install` takes the
workspace lock, so it refuses while a dashboard holds the workspace.

## Not verified here

The real central service and this adapter have not been run together in this branch: the
tests use a stand-in hub that implements the contract as written. Rig timing under concurrent
transfers has not been measured on hardware. Windows file permissions rely on the profile's
ACLs.
