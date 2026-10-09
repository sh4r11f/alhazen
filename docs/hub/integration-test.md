# Live round-trip integration test

`tests/hub/test_live_roundtrip.py` runs the Experiment Hub's main cross-component path end to end. It uses the real processes over loopback HTTP. No component is replaced by a stand-in. Interface tests for each module live beside it (`test_server_*.py`, `test_rig_*.py`, `test_packages.py`, `test_documentation.py`). This test checks that those interfaces actually fit together.

## What it starts

| Process | How | Configuration |
|---|---|---|
| Central hub | `python -m alhazen hub serve --config <tmp>/hub.toml --host 127.0.0.1 --port <free>` (real uvicorn, one worker, maintenance thread on) | TOML written under `tmp_path`. The database URL arrives through `[database] url_env`, so it never appears in the file. `admin.init_database` and `admin.create_invite` run in the test process. |
| Clean rig | `python -m alhazen dashboard --hub --no-browser --state-dir <tmp>/rig-state --port 0` | A fresh state folder. The test reads the workspace token from the URL fragment in `server.json` (the holder record), the way the page gets it. The token is never taken from `/hub/bootstrap.json`, which holds none. |

Both children run with this checkout's `src/` first on `PYTHONPATH`. The rig passes that environment on to the interpreter it probes and runs. A precondition check asserts that the children import this checkout. The rig installs the release with `sys.executable`, the test's own interpreter.

## The path it checks

1. **Accounts.** Two synthetic accounts, `author_e2e` and `collector_e2e`, are created from admin invites and registered over HTTP with the configured `Origin`. Bearer tokens come from `POST /auth/token`.
2. **Publish one pinned release.** The author builds the package from `alhazen new fixation_demo` plus the source-checked documentation fixture (`tests/hub/fixtures/documentation/scaffold/docs`), using the real `build_bundle(suggest_files(...))`. The author uploads it, checks that the hub's SHA-256 equals the local ZIP's, and publishes it.
3. **The publication stays pinned.** The author uploads a later private version `0.2.0`. The public catalogue still pins `0.1.0` with the same SHA-256, and public metadata never names the private version. Documentation of the published version is readable.
4. **Library.** The collector pins the published version in their library.
5. **Rig install.** On the clean rig, through the dashboard's `/api/hub/v1` adapter only:
   - status starts as `not_configured`;
   - connect with explicit loopback HTTP;
   - sign in as the collector (status never carries a token);
   - `POST /local/install` with `trust_code: true` and the test interpreter;
   - the release is registered with the exact version id and SHA-256.
6. **Simulate.** The existing Workspace launch API (`POST /api/runs`) runs a headless `simulate` session with 2 trials on `alhazen/laptop`. The test polls `/api/state` until the run is `completed` with return code 0. No hardware is touched.
7. **Upload.** `GET /local/sessions` finds the completed session. Then `POST /local/upload-preview` runs; the test checks recipient hub and account, release and privacy warning. `POST /local/upload` follows, with that `preview_id` and `consent: true`. The test polls `GET /local/jobs/{id}` until the job is `completed` and the receipt says `committed`.
8. **The collector's view, against the rig's own bytes.** The test reads every file in the session folder, which it finds from the dashboard's `/api/data/roots`. It then checks:
   - file count and total bytes;
   - artifact list and per-file hashes;
   - `manifest_sha256`, recomputed independently from the definition in docs/hub/server.md (experiment, version, sorted `{path, sha256, size}`, the four metadata fields);
   - subject code and mode;
   - that the session list holds exactly this session.
9. **Trial index.** The test waits for `index.status == "indexed"`. Columns must equal the CSV header and rows must equal the rig's `*_trials.csv` rows.
10. **Exports.**
    - The CSV export equals the rig's rows after the documented spreadsheet-safe prefix rule, and is an attachment.
    - The JSON export values equal the rig's rows.
    - The CSV relayed through the rig's proxy is byte-identical to the hub's.
11. **Originals.** Every original file downloads byte-identical, with a matching `X-Alhazen-SHA256`.
12. **Isolation.** The author gets `404` on every route of the collector's session (detail, trials, export, file) and an empty `/data/sessions`. An anonymous caller gets `401`.

On success it prints one line of safe state: backend, number of files and bytes, number of indexed trials. Tokens, passwords and process logs are never printed. Logs stay in `tmp_path` (`hub/hub.log`, `rig/dashboard.log`, the run's `console.log`) for a failed run. Teardown sends SIGINT to both children (terminate on Windows), kills them after 15 s, and asserts that neither outlived the test.

## Running it

```sh
# portable default: SQLite, everything under pytest's tmp_path
PYTHONPATH=src python -m pytest tests/hub/test_live_roundtrip.py -q

# PostgreSQL: an EMPTY disposable database; the test creates its own schema
ALHAZEN_HUB_TEST_POSTGRES_URL='postgresql://USER@/DB?host=SOCKET-DIR' \
  PYTHONPATH=src python -m pytest tests/hub/test_live_roundtrip.py -q
```

- **Marker and runtime.** The test is marked `slow`: it starts two services and a real session, about 25 s on the development sandbox. It stays in the default suite; deselect it while iterating with `-m 'not slow'`.
- **Dependencies.** It needs the hub extra (FastAPI, uvicorn, SQLAlchemy, httpx), like the other `tests/hub` service tests; it imports them directly rather than skipping.
- **Network and ports.** It uses only `127.0.0.1`. The hub's port is probed free just before start; the dashboard picks its own (`--port 0`).
- **What it never uses:** real data, real accounts, credentials or institutional paths.

## Pending seams (not yet asserted)

- **Run provenance.** Run records do not yet carry the pinned release (experiment, version, SHA-256). Once the rig's provenance snapshot is integrated, step 6 should assert it in the run's launch/run record and in the uploaded `session.json` or receipt binding.
- **Empty files and closed attempts.** The data review found that a session containing an empty file never completes (BL1), and that an aborted or expired upload cannot be retried (BL2). Owners are adding those regressions. A small end-to-end empty-file case belongs here once the server and rig fixes are in the parent branch; at this commit it would fail.
- **Install API.** The rig still unpacks with `extract_bundle` at this commit. When it moves to `install_bundle(expected_sha256=...)` and reports durability, add an assertion on the reported durability state; the round trip itself needs no change.
- **Rig receipt check.** This test recomputes the receipt digest itself. The rig's own receipt check is still pending in the rig branch.
- **Unfinished uploads.** Listing and aborting unfinished uploads (the new server routes and UI) are not exercised here.

## Limits

- One machine, loopback only.
- Not covered:
  - TLS and certificate verification (the rig talks plain HTTP to a loopback hub under the explicit development flag);
  - a browser or the frontend;
  - timing on real rig hardware;
  - multi-process hubs;
  - Windows and macOS (run on Linux only).
- The session is 11 files of about 200 KB, so large-transfer, timeout and lease behaviour are not exercised.
- Failure injection, races and recovery live in the module tests and the review repros, not here.
