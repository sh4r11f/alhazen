# Hub auth and admission review: fixes

Source review: `/workspace/alhazen-hub-work/auth-review.md`, done on 9f0bd02. These fixes are on
the branch `fix/experiment-hub-auth-review`, starting from e0fdc0f. Each fix has a regression
test in `tests/hub/test_auth_review_regressions.py`. Every test there failed on e0fdc0f, apart
from the ones that only pin existing behaviour (noted in the status file). The tests run on SQLite
by default and on PostgreSQL when `ALHAZEN_HUB_TEST_POSTGRES_URL` is set. The body-budget,
disconnect and export tests run the app under a real uvicorn server on a private loopback port.

| # | Finding | Fix | Where |
|---|---|---|---|
| 1 | A concurrent burst of sign-ins bypassed the per-account and per-address failure limits. | Admission is one serialised transaction: SQLite `BEGIN IMMEDIATE`, PostgreSQL `pg_advisory_xact_lock`. The account and address attempts are inserted as failures before the hash runs and marked successful only when the password matched, so guesses still in flight count. An attempt that never got a hashing slot (429 `server_busy`) is withdrawn and does not count against the person. | `auth._admit_sign_in`, `_settle`, `_withdraw`, `_serialize_admission` (also used by the global hash budget and registration throttle) |
| 2 | One uploader could hold every transfer slot by sending bodies slowly, and body reads had no deadline. | A per-owner upload gate (`limits.max_transfers_per_owner`, default 2) is taken before the shared transfer gate; refusal is 429 `owner_transfer_limit`. Every body is read through `app._parts`: route byte limit, an idle budget (`body_idle_seconds`, default 30 s between parts) and an overall budget of `body_base_seconds + expected bytes / body_min_bytes_per_second` (defaults 60 s and 64 KiB/s: an 8 MiB chunk 188 s, a 256 MiB package about 69 min, a 1 MiB JSON body 76 s). The expected size is the declared Content-Length, else the route limit. A stall gets 408 `body_timeout` with `Connection: close`, and the slots are released. | `app._parts`, `_body`, `_receive_package`; `context.OwnerGate`; `settings.HubLimits` |
| 3 | The export permit was released only in the body generator's `finally`. A client that had already disconnected left it taken until a cyclic GC. | `PermitStreamingResponse` releases the permit in its ASGI `__call__` `finally` (idempotent) and closes a started generator at once. | `app.PermitStreamingResponse`, export route |
| 4 | On PostgreSQL, an old-password sign-in overlapping an admin reset or disable survived it. | The final sign-in transaction re-reads the account `FOR UPDATE`, so it waits for an uncommitted reset or disable, then sees it and refuses. | `auth.sign_in` |
| 5 | `limit=²` and similar values, and deeply nested JSON, gave a 500 with a traceback (anonymously on login). | Numbers in queries and Content-Length are ASCII digits only (`app._digits`). JSON: `RecursionError` is mapped to 400 `invalid_json`; nesting is capped at 32 levels and repeated keys are refused. | `app._int`, `_declared_length`, `_json` |
| 6 | A client hanging up mid-body was logged as an unhandled 500 with two tracebacks. | A `ClientDisconnect` handler logs one INFO line (method and path, no headers or query) and ends the request. Nothing is acknowledged and the slots are released. | `app._client_gone` |
| 7 | Public metadata accepted bidi and other invisible characters that display names refused. | One rule, `auth.has_hidden_characters`, applies to display names and every human-readable experiment field. It refuses categories Cc, Cf, Zl, Zp and Cs (lone surrogates, which could not be stored); `\n` and `\t` are allowed in the description only. | `auth`, `catalog._bad_text` |
| 8 | `hub_auth_sessions` rows were never deleted. | `auth.purge_sessions` deletes sessions that ended (expired or revoked) more than `auth.session_retention_seconds` ago (default 7 days). It works in batches of 1,000, at most 100 batches per hourly housekeeping pass, through `auth.housekeeping`. | `auth`, `maintenance` |

## New settings

All settings are positive integers and can be set in the TOML `[limits]` and `[auth]` tables:

- `limits.max_transfers_per_owner` = 2
- `limits.body_idle_seconds` = 30
- `limits.body_base_seconds` = 60
- `limits.body_min_bytes_per_second` = 65536
- `auth.session_retention_seconds` = 604800

`GET /config` is unchanged.

## Behaviour changes a client can see

- Sign-in under a burst: at most the account limit of failed checks, then 429 with Retry-After.
  The right password still succeeds whenever admission lets it through. A success frees its own
  pending slot, because the window counts failures only.
- A third concurrent chunk or package upload from one account gets 429
  `owner_transfer_limit` (Retry-After 5). Rig clients already retry 429.
- A body that stalls or overruns its budget gets 408 `body_timeout`. The rig resumes the chunk
  from the server's offset.
- Query numbers in other scripts (fullwidth `５`, Arabic-Indic `٣`) are now 400. They used to be
  accepted or to give a 500.
- JSON with repeated keys, or nested deeper than 32 levels, gets 400 `invalid_json`.
- Titles, summaries, descriptions, licences, citations and display names containing format
  characters get 400. Format characters include zero-width joiners, so emoji ZWJ sequences
  (family or profession emoji) are refused. Single emoji, accents and CJK text are accepted.

## Remaining limits (not fixed here)

- The gates are per process. The supported pilot runs one uvicorn process (`serve()` pins one
  worker); several processes would need the slot limits enforced in the database.
- The body budgets bound time and bytes per request. They do not stop an attacker with many
  accounts or connections from using everyone's share: the global gate is still the last bound,
  and uvicorn `limit_concurrency=256` caps connections. Only the per-owner gate is per account.
- The seal path (`uploads.seal`, owned by the server worker) still takes only the shared gate.
  It does disk work, not client I/O, so it was not in scope.
- Admission serialises every sign-in decision on one advisory lock (PostgreSQL). Each decision is
  a few small statements, which is fine at pilot scale. A busy multi-tenant service would want
  per-subject locks.
- Package manifest strings (title, description inside the ZIP) and upload metadata
  (`subject_code` and similar) are validated by the package and upload modules, which other
  workers own. They are not yet on the shared hidden-character rule.
- Session purge filters on `expires_at`/`revoked_at`, which have no index (a schema change owned
  by the server worker). At pilot scale the hourly scan is small.
- Without a trusted proxy, client addresses are the socket peer. Behind a tunnel, every client
  shares one address, so the per-address limit acts as a service-wide limit. That is a deployment
  decision (`server.forwarded_allow_ips`).
