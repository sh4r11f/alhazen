# AI-assisted experiment authoring (hub, schema 3)

A signed-in user describes an experiment; the hub asks **the user's own AI
provider** for a plan, then for source; the user reviews and accepts, and the
accepted source becomes a **private version** of a new experiment through the
same pipeline as an upload. This page is the operator and integrator
reference; the shared contract between the server, the authoring kit and the
interface is `/workspace/alhazen-hub-work/ai-authoring/CONTRACT.md` during
development and is folded into this page and api-contract.md.

## What the hub does and never does

- **Bring your own key.** Each user stores a key per provider (OpenAI,
  Anthropic, Google, OpenRouter). Keys are encrypted at rest with the
  operator's wrapping key and are never returned, logged, exported, packaged
  or sent to the browser. Status shows provider, last four characters and
  when the key was stored and replaced.
- **Scoped disclosure.** A provider receives exactly: the user's prompt;
  alhazen's public authoring context (experiment scaffold, package manifest
  schema, documentation schema, modes/API summary of the hub's alhazen
  version); for a source job, the plan; and only when the user chose *Start
  from*, the text files of that version **if the user may read it now** (own
  experiment, or the currently published version; checked again when the job
  runs). Never collected data, never anyone's private source, never keys.
  Every job records what it disclosed (`disclosed`: files with byte counts,
  skipped files and why, number of calls, bytes sent).
- **No execution.** Generated code is never imported or run on the hub. It is
  packaged with `packages.build_bundle` and checked statically (package
  rules, documentation schema; the authoring kit adds `compile()` and its own
  checks). Simulation happens on a rig after download, as for any version.
- **A person accepts.** Acceptance creates a private version; nothing is
  published, installed, run on a rig or touches a session. The version's
  owner view carries `ai_assisted: true` and `ai_draft_id`; visitors never
  see either.

## Configuration (`[ai]`, all optional; off without `key_secret`)

```toml
[ai]
key_secret_env = "ALHAZEN_HUB_AI_KEY"   # 32 random bytes, base64; or key_secret = "..."
max_active_jobs_per_user = 3            # queued + running
max_jobs_per_day = 20                   # per user, rolling 24 h
workers = 2                             # provider-call threads in the process
job_lease_seconds = 900                 # renewed before every provider call
max_attempts = 2                        # a job whose worker died is retried once
max_prompt_chars = 8000
max_context_bytes = 524288              # start-from source sent at most
max_context_file_bytes = 131072
connect_timeout_seconds = 10
read_timeout_seconds = 120
max_response_bytes = 8388608
max_output_tokens = 16000

[ai.providers.openai]                   # per provider: optional overrides
base_url = "https://api.openai.com/v1"  # e.g. an OpenAI-compatible gateway
models = ["gpt-4.1", "gpt-4.1-mini"]
default_model = "gpt-4.1"
```

Generate a wrapping key with
`python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`
and keep it with the database password (environment, not the file).
**Losing it makes every stored provider key unreadable** (`key_unreadable`;
users enter their keys again). It cannot be rotated in place in this
version. A `base_url` must be https, or http on loopback only.

## Data model (schema 3)

`admin migrate` from schema 2 adds three tables and one nullable column; no
existing data changes meaning (take a backup first, as for every migration).

| Table / column | Holds |
|---|---|
| `hub_ai_keys (user_id, provider)` | Fernet ciphertext whose plaintext also names its user and provider (a copied row does not decrypt elsewhere), `key_hint` (last 4), `created_at`, `rotated_at` |
| `hub_ai_drafts` | owner, prompt, provider/model, optional start version, `plan_json`, current `plan_job_id` / `source_job_id`, `status`, and after acceptance `experiment_id`, `version_id` |
| `hub_ai_jobs` | kind `plan`/`source`, status, request, result, `error_code`/`error_message`, `usage_json`, `disclosed_json`, `attempts`, lease and fencing token |
| `hub_versions.ai_draft_id` | the accepted draft that created a version (NULL for uploads) |

Generated packages awaiting acceptance live in the artifact store under
`ai-drafts/<draft>/<job>.zip`; discarding a draft removes them. Accepted
versions are ordinary releases (`releases/...`) and count against the
owner's quota.

## Draft and job states

```
describing --(plan job)--> planning --done--> planned --(source job)--> generating --done--> generated --accept--> accepted
     ^                        |failed/cancelled            ^                  |failed/cancelled
     +------------------------+                            +------------------+
any state except accepted --discard--> discarded (jobs cancelled, packages removed)
```

`POST /ai/drafts` starts at `planning`. `POST .../plan` plans again from
`describing`, `planned` or `generated` (optionally with a new prompt).
Jobs: `queued -> running -> done | failed | cancelled`. A job is claimed with
a lease and a random token; every write after the claim is conditional on the
token, so a cancelled job or one whose lease was taken over cannot record a
result. The lease is renewed before each provider call and the call is
refused if the job was cancelled meanwhile (a call already in flight is not
interrupted; its answer is discarded). A job whose lease lapses is retried
once, then failed with `lease_lost`.

## Failure codes (job `error.code`, with the HTTP status it corresponds to)

| Code | Status | Meaning |
|---|---|---|
| `provider_quota` | 402 | the provider says the account has no credit/quota |
| `key_required` | 409 | the key was removed before the job ran |
| `key_rejected` | 409 | the provider refused the key (replace it) |
| `key_unreadable` | 409 | the stored key no longer decrypts (wrapping key changed) |
| `start_unavailable` | 404 | the start-from version is no longer readable by the user |
| `generation_invalid` | 422 | the model's answer failed validation after one repair round; `result.report` (and `result.package_error` for packaging) says why |
| `provider_error` | 502 | the provider failed or rejected the request (e.g. unknown model) |
| `provider_timeout` | 504 | the provider did not answer in time |
| `lease_lost` / `internal` | 500 | the hub could not finish the job |

Provider error messages never include the key, request headers or the
provider's response body.

## Seam with the authoring kit

`alhazen.hub.ai.jobs.AuthorKit` adapts `alhazen.hub.ai.author`:
`build_context(alhazen_version, start)`, `plan(client, prompt, ctx)`,
`generate_source(client, plan, ctx)` (returning `files: dict[str, bytes]`,
`manifest`, `report`), `StartFrom(experiment_title, version, files)`,
`PlanInvalid(report)`, `SourceInvalid(report)`, and a plan constructor
(`Plan.from_dict`, else `parse_plan`, else `Plan(**fields)`). Plans and
reports are stored as JSON via `to_dict()` / dataclass fields. The client the
kit receives is a `GuardedClient`: it renews the lease, enforces
`max_output_tokens`, and totals usage and bytes sent.

The bundle's `manifest` supplies the package metadata (`name`, `version`,
`title`, `description`, `hardware`, `license`, `citations`, optional
`documentation`); `files` and `schema_version` are computed. The package
manifest format refuses unknown fields, so "AI-assisted" is recorded on the
hub (`hub_versions.ai_draft_id`), not inside the package.

## Not done here

- No operator-key rotation command; no per-user spend accounting beyond job
  counts and the token usage each job records.
- No provider streaming; a call waits up to `read_timeout_seconds`.
- The browser pass against a running hub with a fake provider is part of
  integration, not of this server work.
