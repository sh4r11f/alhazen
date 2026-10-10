# Experiment Hub verification

This is a local development branch. No public deployment, stable release, downstream
experiment repin or real-participant-data transfer is implied by these checks.

## Evidence already obtained

- Existing experiment core: **5,263 passed, 5 skipped, 5 display tests deselected** on an
  immutable integrated checkpoint, including the new launcher provenance path. Existing
  hardware/SDK-dependent skips were not introduced by this feature.
- The complete frontend suite: **437 passed, 0 failed, 0 skipped**. Browser checks used the actual server,
  not an inline mock: sign-in, catalogue, experiment reading views, private data tables,
  trial columns and responsive layouts. Scientific content was checked against the runnable
  scaffold and the current framework APIs.
- Real PostgreSQL and real HTTP: two invited users, immutable publication, private draft
  isolation, library pin, a clean local rig install, an actual headless simulated run,
  11-file upload, derived trial rows, CSV/JSON export, and byte-identical original downloads.
  The source author and anonymous visitors were refused access to collector data.
- A real PostgreSQL dump/restore plus artifact copy to a different root preserved all
  11 artifact hashes, the two trial rows, library pin and committed receipt.
- Archive tests cover Python 3.10 through 3.13, hostile ZIP encodings, checksums, path and
  link attacks, interruption, install recovery and reviewed-digest enforcement.
- Independent reviews drove fixes for auth admission races, revoked-session races,
  request deadlines and owner sharing, cancellation cleanup, empty files, retired upload
  attempts, seal fencing, source provenance, account-bound consent and derived-index recovery.

## Combined verification status

**PASS: all 711 Hub tests pass on SQLite and all 711 pass on PostgreSQL**, with no failures
or skips. Each backend passed alone and both passed concurrently. The verified Python
snapshot is `25221928ecd99a5cd4529f6c0eb89951fcf5c5bc`; subsequent changes were documentation
and frontend only. Sequential times were 423 seconds (SQLite) and 501 seconds (PostgreSQL);
concurrent runs took 555 and 617 seconds. Per-test outcomes, timings and JUnit reports were
retained. PostgreSQL peaked at six connections out of a limit of 100.

Earlier 900-second timeouts were caused by one export-lifetime fixture sending a 21,719-byte
CSV in 7-byte chunks: 3,103 durable writes per setup, measured at 86 seconds. The already
integrated test-only fix `c8e80e8` uses 1 MiB chunks in that unrelated fixture, reducing setup
to 1.6–2.8 seconds. No assertion was removed or loosened. Tiny-chunk protocol tests remain.
The old timed-out runs are not treated as passes; the later complete runs establish the result.

## Design and AI authoring (added after the first verification)

The hub, the operator dashboard and the marketplace were redesigned on this branch from
compared alternatives (Manrope, the Penrose mark, the warm palette). Each merge re-ran the
JavaScript suite, the asset-table tests and the static checks; the full Hub matrix was
re-run after the AI authoring integration: **932 passed on SQLite and 932 on PostgreSQL**, no
failures or skips, with the JavaScript suite at 472 and mypy, ruff and the import-layer
contract clean.

AI-assisted authoring (`docs/hub/ai.md`) was exercised live on the development preview
with GPT-4.1 through an OpenAI-compatible gateway, as a non-privileged user:

- Plan, then source: a fixation-hold task with a peripheral flash. The generated package
  passed all static checks (answer, schema, files, syntax, safety, imports, api, structure,
  defaults, package, documentation), was accepted as a private version, was invisible to
  anonymous and other users and absent from the catalogue, installed on a rig with matching
  provenance, and **ran to completion as a headless simulation** (12 trials, three
  conditions, outcomes and latencies recorded under the rehearsal root).
- Before the `api` check existed, an earlier generation passed the then-current checks and
  failed on the rig (`DisplayBackend` has no `draw_disc`). That failure's run log was then
  sent through `POST /ai/drafts/{id}/repair`: one redaction (a live-monitor URL), a
  repaired package that passed every check, acceptance as version 0.1.1 beside the untouched
  0.1.0, and a completed headless run of the repaired version.

What this does and does not establish: the pipeline produces executable packages and
catches the runtime-API class of error statically; it does not validate the scientific
design of what the model proposes (the author reviews the plan and Methods), and model
behaviour varies between runs and providers (an earlier GPT-4.1 round needed the repair
round twice). Anthropic, Google and OpenRouter clients are tested only against recorded
transports. Nothing generated is ever executed on the hub.

## Integrity acknowledgements

The software-design checker is run against the original main commit. The following new
platform-conditional tests are acknowledged, not hidden failures: POSIX link/permission
checks on Windows, a git-dependent discovery test when git is unavailable, a filesystem
name the host cannot represent, and server-reference tests when the optional server extra
is absent. The verification environment has the server extra and Git.

Three intentional handlers are also acknowledged after inspection:
- `cli/dashboard.py`: the downstream browser connection has gone away, so there is no
  response to deliver. Upstream failures are separately represented as typed hub errors.
- `hub/cli.py`: logout always forgets the local credential and explicitly reports when
  remote revocation could not be confirmed. It does not claim the remote token was revoked.
- `hub/sync.py`: an empty work queue is normal idle control flow, not a failed upload.

New dependencies are registry-verified and locked. Existing tests are retained. The feature
history is scanned for private deployment identifiers, not only its final tree.

## Not established by these tests

- Real rig timing, device behavior or calibration; no hardware was controlled.
- Physical Windows/macOS, network-filesystem locking or directory-sync guarantees. On
  unsupported filesystems an install reports its durability uncertainty rather than lying.
- Multi-process service operation; the pilot uses one API process.
- Production TLS, trusted proxy settings, stable cluster access or availability.
- Durability of the intended archive or an independent production backup. A development
  restore rehearsal is not evidence that an operator's backup policy is configured.
- Scientific correctness of arbitrary author-supplied Methods or diagrams. The renderer
  validates structure and declared default references; authors own the science.

Before a real deployment, approve the host/access path, supported live PostgreSQL storage,
backup/restore and retention policy, participant-data handling and the pilot user set. All
actual hosts, account names, paths and connection strings belong in operator-private
configuration outside Git. The released local workflow remains usable while this branch
is reviewed.
