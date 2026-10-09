# Hub v1 cross-module contract

This is the initial implementation contract. If an implementer must change it, report it to the coordinator before diverging. Prefix for both central and rig UI: `/api/hub/v1`. JSON errors: `{error: {code, message}}`, never filesystem paths or raw database exceptions. Bounded `limit` (default 50, max 100) and offset pagination return `{items, next_offset}`.

## Package module (coordinator owned)
`alhazen.hub.packages` exports:
- `PackageError(ValueError)`
- `PackageFile(path: str, size: int, sha256: str)` frozen dataclass
- `PackageInfo(manifest: dict, sha256: str, size: int)` frozen dataclass
- `safe_relative(value: str) -> str`: portable normalized relative POSIX path or error.
- `inspect_bundle(path: Path, *, max_archive_bytes=268435456, max_expanded_bytes=1073741824, max_files=10000) -> PackageInfo`: full streamed checksum validation.
- `extract_bundle(path: Path, destination: Path) -> PackageInfo`: validate then stage/atomically install; refuses existing destination; never imports anything.
- `build_bundle(source: Path, output: Path, metadata: dict, files: list[str]) -> PackageInfo`: explicit chosen file list only, no hidden source-tree dump; excludes known sensitive path classes.
- `suggest_files(source: Path) -> list[str]`: git-tracked regular files if a git tree, otherwise regular files under the folder, excluding data, hidden secrets, environments and rig-specific files; callers show the list before approval.
ZIP root `alhazen-package.json` contains schema_version=1, name (slug), version (simple semver), title, description, entrypoint="run.py", python_min="3.10", alhazen_min="2.13.0", platforms=["linux","darwin","win32"], hardware={display:bool,eye_tracker:bool,reward:bool}, license (string), citations (list of strings), files=[{path,size,sha256}]. Exactly the declared files, plus that manifest, may appear. File list does not include the manifest itself. Manifest strings are data, rendered as text. No uploaded source is imported on the server. All version identity and integrity refers to the whole ZIP sha256.

## Central API (server worker owned)
- GET `/config`: `{role:"server",api_version:1,registration_mode:"invite",limits:{...}}`; no private config.
- POST `/auth/register`: `{username,display_name,password,invite_code}` -> `{user}`. No email dependency; user signs in next.
- POST `/auth/login`: `{username,password}` -> `{user,csrf_token}` plus HttpOnly SameSite cookie. Browser writes send `X-CSRF-Token` and permitted Origin; protect login too.
- POST `/auth/token`: same credentials -> `{user,access_token,expires_at}` for the rig client/CLI. Rate limit as login; token hash stored server-side. Do not return browser login token.
- GET `/auth/me` -> `{user,csrf_token}` (CSRF only relevant to cookie auth). POST `/auth/logout` revokes the current cookie or bearer session.
- GET `/catalog?query=&limit=&offset=` -> published listing items.
- GET/POST `/experiments`: list own / create private metadata `{title,summary,description,license,citations,tags}`.
- GET/PATCH `/experiments/{id}` -> detail/owner metadata edit. Detail includes versions; unauthenticated/nonowner only sees published version.
- POST `/experiments/{id}/versions`: raw application/zip upload, streamed and capped. Version and requirements from manifest. Returns `{version}` including id,version,sha256,size,manifest. Owner only; no replace.
- GET `/experiments/{id}/versions/{version_id}/download`: stream attachment with authorized visibility.
- POST `/experiments/{id}/publish`: `{version_id,license_ack:true,data_excluded_ack:true}`. POST `/experiments/{id}/unpublish`. No other user may publish.
- GET `/library` -> `{items,next_offset}` with experiment and pinned version fields.
- POST `/library`: `{experiment_id,version_id}` -> item; user has a single pinned version per experiment. Download permission still checked against current visibility.
- POST `/sessions/init`: `{experiment_id,version_id,client_session_id,files:[{path,size,sha256}],metadata:{subject_code,mode,rig_alias,started_at},consent:true}` -> session with id,status and file progress. Session owner is collector; public code author gains no data access. Unique `(owner, client_session_id)`; exact manifest replay returns same session, different content conflicts.
- GET `/sessions/{id}/upload`: progress `{id,status,files:[{path,size,sha256,received}]}`.
- PUT `/sessions/{id}/files?path=...&offset=N`: raw chunk max 8 MiB and `X-Chunk-SHA256`. Identical earlier chunk is a safe replay, offset/hash conflict rejected. Only owner of staging session; absolute/traversal/symlink attacks rejected.
- POST `/sessions/{id}/complete`: verifies all files, commits immutable session and indexes supported trials.csv rows. Response is receipt `{id,status,manifest_sha256,...}`. Idempotent. Indexes must be derived/rebuildable and limits/errors explicit.
- GET `/data/sessions?experiment_id=&subject_code=&mode=&limit=&offset=` -> private own completed sessions.
- GET `/data/sessions/{id}` -> metadata, receipt and artifacts.
- GET `/data/sessions/{id}/trials?limit=&offset=` -> derived trial rows.
- GET `/data/sessions/{id}/export?format=csv|json` -> safe download of trial table.
- GET `/data/sessions/{id}/files?path=...` -> forced attachment (sandbox headers for active formats), owner checked.
- GET `/healthz`: readiness without private paths, GET `/readyz` DB readiness.

User public shape `{id,username,display_name}`. Experiment core fields `{id,title,summary,description,owner:{id,username,display_name},license,citations,tags,published_version_id,created_at}`. Version core fields `{id,experiment_id,version,sha256,size,manifest,created_at}`. Catalogue/library items should include `{experiment,version}` rather than incompatible flattened records. Metadata POST/PATCH response wraps `{experiment}`; detail `{experiment,versions}`; lists wrap `items`. Session row has `{id,experiment_id,version_id,client_session_id,status,metadata,created_at,completed_at,manifest_sha256}`.

## Rig integration (rig worker owned)
Keep default dashboard unchanged. Add `alhazen dashboard --hub`, opening `/hub`, and serve shared hub assets on `/hub/assets/...` plus central `/` from the optional service. Add a route adapter under `/api/hub/v1` protected by the dashboard's existing loopback host/origin/token rules. The shared page bootstrap reports role=rig and supplies existing workspace auth without weakening it. Central mode uses cookies/CSRF; rig mode uses existing `X-Workspace-Token` (confirm actual header in dashboard source) plus server-owned, private stored bearer credentials, never tokens in browser localStorage.
- Common routes proxy only the fixed configured hub API, never arbitrary URL/path/headers. Login maps to central auth/token; logout revokes and clears the local stored credential. Configuration/check connection is separate from passwords.
- GET `/local/status`: connection state, configured base URL, signed-in operator (no token), installed records; do not expose remote physical paths.
- POST `/local/connect`: `{url}` saves the fixed HTTPS base (allow explicit loopback for local development); validation, timeout, no redirect credential leaks.
- GET `/local/projects`: registered local experiments for package/upload selection.
- POST `/local/package-preview`: `{project_id}` -> proposed package file list/exclusions and metadata suggestion. POST `/local/package-upload`: explicit `{project_id,experiment_id,metadata,files,confirmed:true}` packs and uploads; no silent publish.
- POST `/local/install`: `{experiment_id,version_id,sha256,trust_code:true,python}` -> verified installed project with workspace URL. Requires explicit interpreter; no automatic dependency installation/import until trust acknowledgment, and no modifying an existing checkout. A missing dependency is an actionable error, never a fake installed success. Record pinned provenance. Consider a separate explicit environment-prepare action only if it can be fully implemented and tested.
- POST `/local/upload-preview`: `{project_id,root_id,run_id}` -> exact completed-session files/bytes plus privacy warning. POST `/local/upload`: same fields plus `experiment_id,version_id,consent:true` -> durable resumable job id, GET `/local/jobs/{id}` -> job status/progress/error/receipt. Never block a frame loop or mark local files deleted. Reuse DataView's exact local-run discovery rather than arbitrary user-supplied absolute paths. Persist idempotency/transfer state and bound workers.
- Optional CLI under `alhazen hub` for serve/register/login/pack/install/sync as useful; server CLI integration can be dispatched from the rig-owned CLI after the server exposes `serve(config_path, host, port)` and `admin` helpers. Actual secrets are outside Git; interactive password input via getpass, never command flags printed in history.

## Frontend (UI worker owned)
One static vanilla JS/CSS frontend shared between central and local modes, no external CDN. Landing, catalogue + detail, sign in/register, library, my experiments/upload/publish, data filters/detail/trials/export, local connection/install/upload controls. Real backend, no fabricated success. A quiet light scientific instrument with a distinctive experiment-to-rig-to-data visual; useful empty/loading/offline/error states, keyboard/touch, 390 px and desktop. No simulated marketplace entries presented as real. Existing local workspace is the run surface, linked from installed items. Server mode says download/use a rig client, never pretends a web page controls hardware. Do not change legacy workspace files; parent/rig worker owns shell wiring. Use safe text rendering, no untrusted innerHTML, URL validation and history navigation. UI can export testable pure functions and Node tests, preserving repository patterns.
