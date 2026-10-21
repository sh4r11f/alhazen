# Machine rig configs: one machine, several rigs, synced with the platform

Status: design contract for alhazen 3.6.0 (branch `feature/machine-rig-configs`). The user-facing
documentation is docs/rigs.md §10, docs/workspace.md and docs/experiment-platform/design.md §8a /
Appendix A; this file is the contract the dashboard, the platform hosts and the Mana service
implement together.

## 1. What changes

alhazen 3.5.0 kept ONE rig per dashboard state folder (`<state>/machine-rig/rig-<name>.yaml`),
edited only on the dashboard. 3.6.0 keeps **several named rig configs per machine**, one of them
the **default**, editable on the dashboard (General page) and on the platform (Machines /
My machines), synced both ways over the existing library connection. A session runs on one of
this machine's configs, chosen on the Run page (the default preselected). Shared alhazen rigs and
experiment rigs are offered only as a starting point when **creating** a config.

A *machine* is a dashboard state folder (`~/.alhazen/dashboard`, `~/mana-experiments/.dashboard`):
it gets a stable random id the first time 3.6 opens it (`machine_id`, `mc_` + 16 hex). Host names
never enter a repository; a machine's label (library.json `machine`, else the host name) is sent
only to the person's own platform.

## 2. On the rig (dashboard state folder)

```
<state>/machine-rig/
  rig-<first>.yaml (+ rig-<first>_gamma.yaml, rig-<first>.reward.yaml, machine.json, history/)
        3.5.0's machine rig, left exactly where it was: it is the first config ("legacy slot")
  configs.json                       the index (new, 3.6)
  configs/<name>/rig-<name>.yaml     every other config, each with its own companions,
  configs/<name>/config.json         saves (like machine.json) and
  configs/<name>/history/            earlier versions
  trash/<name>.<stamp>/              a deleted config's folder, moved here, never destroyed
```

`configs.json` (written atomically):

```json
{"schema": 1,
 "machine_id": "mc_0123456789abcdef",
 "default": "c1a2b3c4d5e",
 "default_rev": 3,
 "configs": {
   "c1a2b3c4d5e": {"name": "booth", "folder": ".", "created": "<iso>", "origin": {...},
                   "revision": 4,
                   "sync": {"platform_revision": 7, "sha256": "<sha of text at last agreement>",
                            "rejected": {"revision": 8, "sha256": "...", "reason": "..."} | null,
                            "conflict": {"revision": 9, "sha256": "...", "text": "...",
                                         "at": "<iso>", "by": "platform"} | null}}},
 "sync": {"at": "<iso>", "state": "synced|offline|not-connected|unsupported|error",
          "error": null, "platform_default_rev": 2}}
```

* A config has a stable **id** (`c` + 10 hex) and a **name** (the rig's name: its file is
  `rig-<name>.yaml`, PsychoPy's monitor is registered under it, a session copy is named like it).
  Names follow `NAME_PATTERN`, are unique on the machine, and may be renamed (files and
  companions renamed with it; history keeps old names).
* `folder` is `"."` for the legacy slot, else `configs/<name>`.
* `revision` counts this machine's own accepted changes (saves, renames, platform edits applied).
* **Migration** (on the first read by 3.6): a 3.5.0 state folder with a top-level `rig-*.yaml`
  and no `configs.json` gets an index naming that file as the first config and the default,
  nothing moved or rewritten (bytes, `history/`, gamma and reward companions, `machine.json` kept).
  A 3.5.0 dashboard on the same folder afterwards still sees and runs exactly that config (it
  ignores `configs/` and `configs.json`), and its saves to it are picked up by 3.6 as local
  changes. A top-level rig file that a 3.5.0 dashboard sets up after the legacy config was
  deleted is adopted by 3.6 as a new config. An alhazen older than 3.5 never read this folder.
* Two top-level rig files are still refused (as in 3.5.0).
* What runs: each config is validated as a launch validates a rig (`validate_rig`), refuses
  `extends` and event names, and is offered on the Run page only when valid (an invalid one is
  listed with its problem, not selectable).

## 3. Dashboard HTTP API (the page)

All answers JSON; errors as today (`{"error": "..."}` with 400/409).

| Request | Answer |
|---|---|
| `GET /api/machine-rigs` | `Rigs` (below) |
| `GET /api/machine-rigs/templates` | `{templates: [{id, label, kind: "shared"\|"experiment", name, summary, real_data, extends, events, error?}], suggested}` |
| `POST /api/machine-rigs/create` `{name, template}` or `{name, copy_of: <id>}` or `{name, text}` | `Rigs` (201); `created: <id>` |
| `POST /api/machine-rigs/check` `{id?, text}` | `{ok: true, changes: [...]}` or 400 with the reason |
| `POST /api/machine-rigs/save` `{id, text, sha256}` or `{id, values, sha256}` | `Rigs` (+ `saved: [paths]` for values) |
| `POST /api/machine-rigs/rename` `{id, name, sha256}` | `Rigs` |
| `POST /api/machine-rigs/default` `{id}` | `Rigs` |
| `POST /api/machine-rigs/delete` `{id, sha256}` | `Rigs` (the folder moved to `trash/`) |
| `GET /api/machine-rigs/history?id=<id>` | `{saves: [{at, by, sha256, changes, file}], versions: [{file, at, size}]}` |
| `GET /api/machine-rigs/version?id=<id>&file=<history file>` | `{text}` |
| `POST /api/machine-rigs/resolve` `{id, keep: "machine"\|"platform"}` | `Rigs` (a sync conflict settled here) |
| `POST /api/machine-rigs/sync` | `Rigs` after a sync attempt (never raises for an unreachable platform; the reason is in `sync`) |
| `GET /api/session-rig?project=&task=&mode=&config=<id>` | as 3.5.0, plus `config: {id, name}`, `configs: [Choice]`; `machine` describes the chosen config |
| `POST /api/runs` | `Launch` gains `rig_config: <id or name>` (empty: the default) |

`Rigs`:
```json
{"machine": {"id": "mc_…", "label": "RazerBlade"},
 "default": "<id>|null",
 "configs": [Config],
 "sync": {"connected": true, "state": "synced|offline|not-connected|unsupported|error",
          "at": "<iso>|null", "error": "<text>|null", "platform": "<library label>"},
 "migrated": {"from": "3.5.0", "config": "<id>"} | null,
 "error": "<text>|null"}
```
`Config`:
```json
{"id", "name", "default": bool, "file", "folder", "text", "sha256", "values",
 "problem": "<validation error>|null", "revision", "created", "origin",
 "saves": [last 5 saves], "reward_calibration": <summary|null>,
 "gamma": {"file": "...", "at": "<iso>"} | null,
 "legacy": bool,                       // the config a 3.5.0 dashboard sees
 "sync": {"state": "synced|local|pending|rejected|conflict|not-synced",
          "platform_revision": int|null,
          "rejected": {"revision", "reason", "text"} | null,
          "conflict": {"platform_text", "platform_sha256", "platform_revision", "at", "by"} | null}}
```
`Choice` (Run page chooser): `{id, name, default, problem}`.

`describe()` (`GET /api/projects` etc.): `machine_rig` stays the default config's brief
(`{id, name, file, sha256, data_root}`), and a new top-level `machine_rigs:
{default, configs: [Choice]}`.

Run records gain `rig_config: <id>`; `rig_name` is the config's name. Carried event lines come
from the last session of the same experiment and task on **the same config** (by id; a 3.5.0
record without one matches by name).

The page URL `?rig=<name or id>` preselects that config on the Run page; `alhazen dashboard
--rig-config NAME` opens the page with it.

## 4. Sync protocol (rig <-> platform)

The platform keeps, per person and machine, one **machine record**; the rig's local files are
what runs and work offline. Sync rides the library connection (library.json: URL + bearer
machine token, or a local platform's `#token=`).

### Machine record (JSON; the same shape on every host)

```json
{"schema": 1, "id": "mc_…", "label": "RazerBlade", "alhazen": "3.6.0",
 "seenAt": <ms of the last sync from the machine>, "token": "<platform machine-token id>|null",
 "default": "<config id>|null", "defaultRev": <int>, "defaultBy": "machine|platform",
 "rigSchema": "<RigConfig JSON schema, as JSON text>|null", "rigSchemaSha": "<sha>|null",
 "templates": "<JSON text: [{id, label, kind, name, summary, real_data, text}]>|null",
 "configs": {"<id>": PlatformConfig}}
```
`PlatformConfig`:
```json
{"id", "name", "text", "sha256",
 "revision": <int, +1 on every content write from either side>,
 "state": "synced|pending|rejected",
 "deleted": false,
 "updatedAt": <ms>, "updatedBy": "machine|platform",
 "basedOn": "<sha256 of the text a platform edit started from>|null",
 "running": {"name", "text", "sha256", "problem", "at": <ms>} | null,
 "rejection": {"revision", "reason", "at"} | null,
 "conflict": {"text", "sha256", "at": <ms>} | null,
 "calibration": {"gamma": {"at": "<iso>"} | null, "reward": {"at": "<iso>", "summary": "..."} | null}}
```

* `text` is the platform's latest version of the config. When `state` is `synced` it is what the
  machine runs; `pending` is a platform edit the machine has not taken yet; `rejected` is a
  platform edit the machine refused (`rejection.reason`), while `running` keeps what the machine
  still runs.
* `running` is reported by the machine at every sync: the version it runs, its name and its
  validation problem.
* `conflict` is the machine's version, kept when both sides changed the config since they last
  agreed; nothing was overwritten. It is cleared when the conflict is settled on either side.
* `deleted: true` with `state: pending` is a deletion made on the platform, waiting for the
  machine; the machine removes it (or refuses it as a conflict when it changed the config
  locally) and the record is dropped once acknowledged.

### Machine requests (the rig; machine token or local token; `MACHINE_ROUTES`)

| Request | Answer |
|---|---|
| `GET /api/rigs/machine/<mid>` | `{record: MachineRecord\|null}` |
| `POST /api/rigs/machine/<mid>/sync` `Push` | `{record, results: {<id>: {ok: true, revision} \| {ok: false, conflict: true, revision}}}` |

`Push`:
```json
{"label", "alhazen", "rigSchema"?: "<json text>", "rigSchemaSha", "templates"?: "<json text>",
 "default": "<id>|null", "defaultRevSeen": <int>,
 "writes":    [{"id", "name", "text", "sha256", "base": <platform revision it replaces, 0 = new>}],
 "acks":      [{"id", "revision", "ok": true} | {"id", "revision", "ok": false, "reason"}],
 "conflicts": [{"id", "text", "sha256"}],
 "deletes":   [{"id", "base"}],
 "running":   {"<id>": {"name", "text", "sha256", "problem", "calibration"}}}
```
Rules (`alhazen.experiment_platform.rigsync`, pure functions shared by every host):
* A write is applied only when `base` equals the platform's current `revision` (0 for a config
  the platform does not have); then `revision += 1`, `state = synced`, `updatedBy = machine`,
  `rejection = conflict = null`. Otherwise it is not applied (`conflict: true`) and the caller
  learns the current record.
* An ack for the current revision sets `state = synced` (ok) or `rejected` with the reason. An
  ack of a pending deletion drops the config. A stale ack (revision moved on) is ignored.
* A conflict report sets `conflict` (never touches `text`/`revision`).
* `default`: applied when `defaultRevSeen == defaultRev` (`defaultRev += 1`, `defaultBy =
  machine`); otherwise the platform's newer choice stands and the machine adopts it.
* `running`, `label`, `alhazen`, `seenAt`, and `rigSchema`/`templates` (when sent) replace what
  was there. Sizes are bounded (a config text 256 KiB, 64 configs, templates 2 MiB, schema 1 MiB).

### Owner requests (the person, in the web app)

Standalone hosts (session cookie / local token):

| Request | Answer |
|---|---|
| `GET /api/rigs` | `{machines: [MachineSummary]}` (records without `rigSchema`, `templates` or texts) |
| `GET /api/rigs/<mid>` | `{record: MachineRecord}` |
| `PUT /api/rigs/<mid>/configs/<cid>` `{name, text, base, basedOn}` | `{record, config}`; 409 when `base` is not the current revision (0 creates; a new id is `c` + 10 hex chosen by the client) |
| `DELETE /api/rigs/<mid>/configs/<cid>?base=<rev>` | `{record}` (marks a pending deletion; a config the machine never had is dropped at once) |
| `PUT /api/rigs/<mid>/default` `{config, base: <defaultRev>}` | `{record}`; 409 when stale |
| `POST /api/rigs/<mid>/configs/<cid>/resolve` `{keep: "machine"\|"platform"}` | `{record}` |
| `DELETE /api/rigs/<mid>` | `{}` (forget the machine; it reappears at its next sync) |

An owner write sets `revision += 1`, `state = pending`, `updatedBy = platform`, `basedOn` (the
sha of the version the editor started from), `rejection = null`. Names must be unique among the
machine's configs that are not deleted. Resolving: `keep: machine` writes `conflict.text` as a
new pending revision with `basedOn = conflict.sha256`; `keep: platform` writes the current text as
a new pending revision with `basedOn = conflict.sha256`; both clear `conflict`.

Mana host: the same record lives at
`<viewer>/file-system/alhazen-experiment-platform-mine/machines/<mid>` (fields as above; `configs`
stored under `rigs/<cid>`; `machines/metadata` is a hidden link, each machine node an `entry`).
The web app writes owner changes there directly (same rules); the viewer's `alhazen-library`
sandbox service answers the machine requests from it.

### What the machine does at a sync

For each config (L = local text, S = `sync` state, P = platform config):
1. No P (or never synced): write L with `base` 0 (a platform record lost is recreated).
2. L.sha == S.sha and P.revision == S.platform_revision: nothing (report `running`).
3. Local change only (L.sha != S.sha, P.revision == S.platform_revision): write L with `base` =
   S.platform_revision.
4. Platform change only, or P.basedOn == L.sha (a platform edit made knowing this version, e.g. a
   conflict settled on the platform): validate P.text as a launch would; valid -> write it locally
   (history kept), ack ok; invalid -> keep L, ack the refusal (once per revision). A pending
   deletion: move the config to trash and ack; the default config cannot be deleted from the
   platform while it is the default (refused).
5. Both changed: report a conflict (L) and keep L; the page shows both versions and the person
   keeps one (`resolve`), on either side.
6. A platform config with no local one (created on the platform): validated, created locally
   (folder `configs/<name>`), acked; a name taken locally is refused with that reason.
7. A config deleted locally since the last sync: `deletes` with `base` = S.platform_revision.

A sync runs when the dashboard reads the library (the same schedule), after every local change
(in the background), and on demand (`POST /api/machine-rigs/sync`). An unreachable platform,
a platform without these routes (404: `unsupported`) or no connection leaves everything local.
