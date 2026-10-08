# Experiment management in the workspace

Status: implemented locally on `feature/neuro-oct7-experiment-manager-090424`
(not released). The Measure rig integration is a separate gate, below.

## 1. Summary

The workspace (`alhazen dashboard`) gains a home page for experiments and,
per experiment, four pages: **General** (notes, people, rigs), **Run** (the
existing launch form, with a registered subject and experimenter chosen
instead of typed), **Data** (unchanged) and **History** (launches and the
session folders on disk). Subjects and experimenters live in a SQLite
database owned by the workspace, mirrored to CSV; rigs stay YAML. Every launch
writes an immutable `launch.json` snapshot of who it was for and who ran it,
and the session's own `session.json` records the experimenter when its
alhazen can. Top risks: identity mix-ups between records and the data
folders' `participants.tsv` (handled by never merging and checking both), a
CSV copy that silently falls behind its database (made visible and
retryable), and saved HTML reaching the API (kept behind a ticket and a CSP
with no network).

## 2. Requirements (examples)

- Registering a folder already registered is refused, naming it; Project
  settings → save registers it again over the same record and keeps its notes,
  archive flag and registration date.
- Adding `sub-007` with initials `hd` stores code `007` (text) and `HD`;
  `007` in another experiment is a separate record; a second `007` in the
  same experiment, even archived, is refused.
- Editing a record with revision 1 after another tab saved revision 2 is
  refused (409) and changes nothing.
- A Run launch with subject record `s_…` and experimenter `e_…` sends
  `--sub 007 --initials HD --experimenter "Name" --experimenter-id e_…` to a
  project whose alhazen records the experimenter, and only `--sub/--initials`
  to an older one, whose run record then says the experimenter is kept by the
  workspace only. Renaming the experimenter afterwards changes neither the
  past `launch.json` nor `run.json`.
- A test launch for `sub-007` = HD into a data folder whose `participants.tsv`
  records `sub-007` as XY is refused before any run record exists.
- Importing `participants.tsv` previews first; applying writes a backup,
  keeps IDs, initials, every other column (missing ≠ empty) and row order;
  applying again changes nothing; conflicting initials are never merged.
- History lists terminal-launched sessions too; a card without `experimenter`
  reads "not recorded".

Change scenarios: a new people field touches `people.py` (schema migration),
`workspace_manage.js` (form) and the CSV header only; a new page of an
experiment adds one entry to `VIEWS` and one container.

## 3. Assumptions ledger

| Assumption | Why it holds / what if not |
| --- | --- |
| One workspace server per state dir (OS lock) | Existing `workspace_lock`; the registry still serialises writers with SQLite. |
| Tens of experiments, hundreds of subjects, thousands of sessions per data folder | History reads small files per run, as the Data view already does; seconds at worst. |
| Subjects belong to one experiment | A person in two studies is two records with two codes; copying is explicit. |
| Experimenters are shared across experiments | One record, assigned per experiment; same names stay distinct by id. |
| No cloud, no accounts | Local-first lab app; the server is loopback-only with its token. |

## 4. Change map

| File | Change | Contract |
| --- | --- | --- |
| `cli/people.py` (new) | registry, CSV copies, imports, launch snapshot | new on-disk store `people/people.sqlite3` (schema 1) |
| `cli/workspace_manage.py` (new) | General/History/registration API | new `/api/manage/*`, `/data/download` |
| `cli/workspace.py` | `Launch.subject_record/experimenter`, `_identity`, `launch.json`, participants pre-check, probe capabilities, register/meta/archive | additive request fields and run.json keys |
| `cli/dashboard.py` | routes, 409 for conflicts, attachment downloads | additive |
| `cli/main.py`, `session/identity.py`, `session/builder.py`, `session/runner.py` | `--experimenter`, `--experimenter-id` → session.json/session.log | additive card key, no schema bump |
| `cli/capabilities.py` (new) | names what a command line records | probed by the workspace |
| `cli/assets/workspace.{html,js}`, `workspace_manage.{js,css}` | shell, addresses, pages, selectors | page ids (JS tests) |

**What this map decides.** The disposable `experiment.sqlite3` mirror is not
touched; `participants.tsv` is never written by the workspace. The session
side is a small additive flag. The UI rewrite keeps every id the Run page's
tests use, so the existing launch behaviour is held by its tests.

## 5. Data ownership

| Data | System of record | Derived | Lifecycle |
| --- | --- | --- | --- |
| Subject/experimenter records, assignments | `<workspace>/people/people.sqlite3` | `people/csv/…` copies | archived, never deleted; schema refused, never moved aside |
| Scientific subject registry | `<data_root>/participants.tsv` (written by sessions) | registry imports link to it | unchanged lifecycle; counterbalancing order intact |
| Who a launch was for / who ran it | `runs/<id>/launch.json` (create-once) | `run.json` `identity`, History | immutable |
| Who ran a session | `session.json` `experimenter` | History | immutable run folder |
| Rigs | `<project>/configs/rig-<name>.yaml` | rig summaries | written only by an explicit save; shared rigs read only |
| Experiment notes, archive flag | `projects.json` | Experiments page | kept across re-registration |

CSV copies: rewritten whole from one read snapshot after each commit, each
file atomically; `exported_revision` says which revision they show, a failed
export is recorded and shown, and the server retries at start. Cells that
start like a formula get one leading `'`; a missing value is named in
`missing_fields`. Reading back an edited CSV is a preview plus an apply bound
to the file's bytes and the database revision.

## 6. Failure analysis

| Boundary | Failure | Behaviour |
| --- | --- | --- |
| Registry write | two tabs edit one record | optimistic revision → 409, nothing saved |
| Registry write | many writers | `BEGIN IMMEDIATE`, 5 s busy timeout |
| CSV export | disk full / folder unwritable | record saved, status "behind" with the error, retry button |
| Registry file | written by a newer alhazen / foreign | refused untouched; rest of the workspace works |
| Import | file changed since preview | 409 "preview again" |
| Import | same ID, other initials | conflict, left alone |
| Launch | archived/unknown/unassigned record, other experiment's record | refused before anything is written |
| Launch | project alhazen predates `--experimenter` | flag not sent; run says "kept with the launch only"; Run page says so first |
| Launch | registration predates capability probe | treated as not recording; "open Project settings and save" |
| History | missing folder, unreadable card | listed as a problem / "not recorded" |
| Download / text | `..`, absolute, symlink out | `path_inside` refusal |
| Saved monitor page | its own script | served by ticket, CSP `connect-src 'none'`, `form-action 'none'`, opened `noopener`; the API needs a header token it cannot send |
| Rig save | file changed on disk since opened | 409 by sha256 |
| Rig save | invalid YAML / settings | validated as a launch would, nothing written |
| Navigation | unsaved General form | in-page Stay / Discard; `beforeunload` |
| Navigation | active run | leaving a page never stops it; the sidebar shows it everywhere |

## 7. Tests

`test_workspace_people.py` (records, revisions, concurrency, CSV bytes,
export failure and repair, CSV round trip, participants import, backups,
schema refusal), `test_workspace_identity.py` (launch identities, snapshots,
refusals, compatibility, registration), `test_workspace_manage.py` (HTTP:
access rules, people, imports, rigs, history, downloads, saved page CSP),
`test_experimenter_record.py` (flag → card and log), and the JS suites for
the shell, addresses, Back/Forward, the Experiments page and the selectors.

## 8. Evolution

- People schema: `PRAGMA user_version`; a future version migrates forward
  after a backup, and an older alhazen refuses a newer file untouched.
- Experiments on older alhazen keep launching; the experimenter reaches
  their session folders once they move to an alhazen with the capability.
- Measure rig: the rig-measurement branch adds its selection to the Run
  page's Measure mode (`Launch.measurements`); the subject menu resolves to
  `request.subject` before its checks, so its `measurement_subject` reads the
  record's ID. Integration of that branch is a separate, recorded step.

## 9. Rejected alternatives

- Subjects in `experiment.sqlite3`: a rebuildable mirror that is moved aside
  on schema change cannot hold the only copy of typed records.
- Writing `participants.tsv` on registration: would move a subject's
  counterbalancing slot to the day it was registered.
- JSON file as the "database": not a database; no transactions.
- Sandboxing saved monitor pages in an opaque origin: the existing pages read
  `sessionStorage` unguarded and would break; the ticket + CSP boundary is kept.

## 10. Subject age and sex (people schema 2)

Requested 2026-10-08: every subject records age and sex wherever it is
created, edited, listed, selected or written into a session.

- **One convention: age in years**, not a date of birth (nothing in alhazen
  stored either before; BIDS `participants.tsv` uses `age`, and a birthday is
  more identifying than the study needs). 0–120, whole or one decimal (an
  animal of 7.5 years), kept as canonical text (`config.models.normalize_age`)
  and written to `session.json` as a number. Because an age goes stale, the
  record keeps `age_recorded`, the UTC date it was entered here (NULL when
  imported or upgraded: unknown), and the Run page flags one over a year old.
- **Sex** is one of `female`, `male`, `other`, `prefer_not_to_say`
  (`SUBJECT_SEXES`); "prefer not to say" is an answer, NULL is "not
  recorded". The page's labels sit in `workspace.js` (`SUBJECT_SEXES`), held
  to the Python codes by a test.
- **Schema 2** adds three nullable columns. A new file runs schema 1's DDL and
  then every migration step, so new and upgraded files are the same tables.
  An upgrade backs up first, runs in one transaction (columns, promotion of
  extra columns named age/sex, one revision, a `changes` entry naming the
  backup) and rewrites the CSV copies after. A failed backup or step leaves
  the file at schema 1.
- **Sessions** get `--age`/`--sex` (capability `subject-demographics`) and
  record them in `session.json` `subject`, `session.log`, and a new
  `participants.tsv` row. They live in `RunIdentity`, not `SessionInfo`, so
  `config_snapshot.yaml` keeps its shape (`SessionInfo` forbids unknown keys,
  and an older alhazen reading a newer snapshot would refuse it).
- **Not required to launch.** A subject without them launches, and its
  session records nulls; the Run page says so. Making them required for run
  and test is the user's decision (it would block existing subjects until
  someone fills them in).
