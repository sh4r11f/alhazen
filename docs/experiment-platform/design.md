# Alhazen Experiment Platform: design

Status: implementation on `feature/portable-experiment-platform` (local branch). This file is
the canonical specification. Operations (install, run, backup) are in
[operations.md](operations.md).

## 1. What it is

The Experiment Platform is the catalogue around alhazen experiments: browse experiments,
read their methods (stimulus schematics, duration-aware trial timelines, parameter guides
bound to an exact source version), keep private copies ("forks"), change them, run them on
a rig, and share versions back. It ships **inside the alhazen repository as an optional
module** (`alhazen.experiment_platform`) and in the same release as the toolkit. Experiments
stay in their own repositories.

It runs on three hosts that share one catalogue model, one refresh pipeline and one web
reader:

| Host | Identity | Storage | Change / run |
|---|---|---|---|
| **Local** (`alhazen platform local`) | the single local user; a per-launch token, loopback only | a store directory on this machine | upload or check out a revision; run on this machine with the printed commands |
| **Self-hosted server** (`alhazen platform serve`) | accounts with passwords, server-side sessions | a store directory + an accounts database on the server | upload or download revisions; experiments still run on rigs, never on the server |
| **Mana** (`Apps/alhazen-experiment-platform`) | Mana identity | Firebase, through the Mana adapter | Mana chats change copies; Mana Terminal runs them on paired machines |

Neither standalone host needs Mana, Firebase, an account anywhere, or the network (local
mode works offline). Mana is one integration, built from the same source (section 8).

Non-goals: the platform never executes experiment code in its own process, never controls
devices, never stores session data, and never syncs data from rigs. Timing-critical
presentation stays on the rig, inside the alhazen session.

## 2. Packaging

- Module: `src/alhazen/experiment_platform/`. Layer: its own line directly below
  `alhazen.cli`, above `alhazen.modes`. It imports nothing from modes, session, devices,
  display or stimuli; the catalogue reads experiment source as text (AST and safe YAML).
- Core install (the `alhazen-vision` wheel without extras; on a package index only after a
  release, see operations.md): catalogue build, refresh, the CLI and the
  **local** host work with the core dependencies only (stdlib HTTP server, PyYAML).
- Extra `platform` (`alhazen-vision[platform]`): adds `waitress`, the WSGI
  server used by the self-hosted host. Password hashing is stdlib `hashlib.scrypt`; the
  accounts database is stdlib `sqlite3`. A rig install never pulls a web server.
- Web assets are compiled into the wheel (`experiment_platform/web/`), including React and
  the Manrope font, so nothing is fetched from a CDN at run time.

## 3. Catalogue model (portable, versioned)

The records are the ones the approved Mana platform already uses, minus host paths, so the
reader and both adapters share them.

**Bundle** (format 1, content-addressed). Every file of an experiment at one source
version. Entry `[path, text, executable]` for UTF-8 text, `[path, base64, executable,
"b64"]` for a binary kept (each at most 2 MB, at most 12 MB per bundle); larger binaries
are listed in `excluded` and stay in the source repository. Chunks of about 400 kB.
`bundleId = <slug>-<first 10 hex of sha256 over path\0text exec [\0b64]>` (identical to the
Mana kit, so ids agree across hosts). Every stored bundle also records the full
`sha256` of that digest stream, which readers verify. Paths are relative POSIX paths:
no absolute paths, `..`, backslashes, drive letters, NUL, empty segments, duplicates, or
`.git/` content; at most 4,000 files and 64 MB of text.

**Listing** `catalogue/<publisher>/<experiment>`: title, summary, art (emblem kind),
featured, category, tags, subjects, hardware, platforms, license(s), publisher
`{uid, name}`, source `{kind: repo|fork, repo, ref, commit, repoCommit?, subdir?,
commitDate, subject, pushed?}`, version, alhazen (pinned tag), parameterSets, ladders,
bundle `{id, fileCount, bytes, binaries, excluded}`, docsource, status, statusReason,
collection, original, counts, publishedAt, updatedAt, versions `{bundleId: {version,
commit, at}}`.

**Version** `catalogue/<publisher>/<experiment>/versions/<bundleId>`: immutable. The
listing as of that version plus its **documentation record**: `methods` (Markdown),
`readme`, `tasks` (JSON), `references` (JSON), `assumed`/`deviations` (JSON, for
replications), `parameterGuide` (JSON, the extractor's output bound to `sourceCommit`),
`guide` (JSON, the resolved HubDocs reader document bound to `bundleId`), and
`docsStatus`. A version is written once; a second write with different content is refused.

**Fork** `users/<uid>/forks/<forkId>`: title, summary, art, tags, subjects, hardware,
original, originStatus, parameterSets, alhazen, origin `{publisherUid, publisherName, exp,
title, bundle, version, commit}` or `{scratch: true}`, head `{id}`, revisions
`{<ms>: {bundle, message, by, at, stat?}}`, jobs (Mana only), published, createdAt,
updatedAt. Its bundles live under `users/<uid>/bundles/` (private). Its methods are the
docs snapshot of the version it was forked from (`users/<uid>/docs/<origin bundle>`),
copied at fork time; a fork's documentation never changes by itself.

## 4. Refresh: two axes

1. **Renderer axis.** The reader, the web app and the resolvers ship with each alhazen
   release. Upgrading alhazen changes how documents render, never what they say.
2. **Source axis.** `alhazen platform refresh` rebuilds the catalogue entry of an
   experiment for an exact source commit:
   - reads the git tree at the ref (`git ls-tree` / `git cat-file`), never a working tree,
     never executing anything; a collection folder (`classics/<name>`,
     `contributed/<author>/<name>`) is one experiment;
   - builds the bundle; if the listing's current bundle id is unchanged, nothing is
     written (idempotent);
   - otherwise builds a **new version**: listing fields from `docs/listing.json` (or an
     operator overrides file), documentation from `docs/experiment.json` +
     `docs/methods.md`, the parameter guide from `run.py`/`configs/` (AST + safe YAML)
     and, when available, alhazen's own source at the pinned tag (to list inherited
     defaults; when it is unavailable the guide says so);
   - resolves the reader document through the bounded documentation validator. Optional
     **guide overlays** (compact diagrams/timelines authored for one source version) are
     applied only when their `bundleId` equals the new bundle id; an overlay for another
     version is reported and ignored, never relabelled;
   - writes the version, then the listing pointer, under the store lock (atomic per file;
     the pointer moves last).

   Earlier versions and every fork stay exactly as they were. `docsStatus` is computed,
   not stored as a claim: `current` (guide bound to this bundle and parameters bound to
   this commit), `partial` (parameters current but no reader document or no compact
   drawings for this version), `stale` (a document bound to another version: the reader
   refuses it and says so), `missing`.
   `alhazen platform check` exits non-zero when any listing is `stale` or `missing`, and
   when a source is newer than its listing; it is meant as the release/CI gate for a
    catalogue (no CI workflow runs it yet).

## 5. Service and authorization

`PlatformService` holds every rule; the HTTP layer only translates. A **principal** is
`{uid, name}` or anonymous. Rules:

- Listings and their versions/docs/bundles: readable by any principal permitted to read
  the catalogue (local: the token holder; server: signed-in users, or anyone when the
  operator sets `public_catalogue = true`).
- Forks, private bundles and doc snapshots: readable and writable only by their owner.
  Another user's fork id returns 404, never 403, so ids do not leak. A doc snapshot is
  named by the catalogue version it was taken from (`versionKey`: the bundle id, plus the
  documentation commit when documentation is read from another ref); the fork's origin
  records it as `docsKey`, so a documentation-only refresh gives later copies a new
  snapshot and leaves earlier ones untouched. A listing's `versions` history is keyed the
  same way, each entry naming its `bundle`.
- Accounts: a session is bound to the account's credential generation, raised in the same
  transaction that changes a password or disables the account; issuing a session is a
  conditional insert on that generation, so a sign-in racing a reset cannot survive it.
- Publishing writes `catalogue/<principal.uid>/<forkId>` only; the publisher name comes
  from the account, not the request. A published bundle is copied (verified) into the
  shared bundle store. Unpublishing removes the listing pointer; versions already forked
  by others remain in their forks.
- Removing a copy from the library (My experiments) deletes the copy record (its revisions
  list and job records with it), the private bundles no remaining copy of the principal
  names, and the docs snapshot no remaining copy reads. A listing published from the copy
  keeps its files (publishing copied them into the published store); nothing on a rig's disk
  is touched. The Mana host does the same in one atomic update of the viewer's workspace.
- Uploads (a new revision as a zip): bounded size (`max_upload_mb`, default 64), every
  entry checked as a bundle path, no symlinks, no files outside the root, no `.git/`.

## 6. HTTP surface

One WSGI application (`experiment_platform.http`) serves both standalone hosts:

| Route | Meaning |
|---|---|
| `GET /` and `/assets/*` | the web app (compiled) |
| `GET /api/session` | who is signed in, host kind, capabilities |
| `POST /api/login`, `POST /api/logout` | server mode only |
| `GET /api/catalogue` | all listings `{publisher: {exp: listing}}` |
| `GET /api/catalogue/<pub>/<exp>/docs[?version=<versionKey or bundleId>]` | documentation record + docsStatus (a bundle id means its latest version) |
| `GET /api/bundles/<handle>/meta`, `/chunks/<n>` | bundle files (handle: `L.<pub>.<exp>.<bundleId>` or `F.<forkId>.<bundleId>`) |
| `GET /api/bundles/<handle>/archive` | the bundle as a zip |
| `GET /api/forks`, `POST /api/forks` | the principal's forks; fork a listing |
| `GET /api/forks/<id>/docs` | the docs snapshot |
| `POST /api/forks/<id>/revisions` | upload a zip as a new revision |
| `POST /api/forks/<id>/publish`, `/unpublish`; `DELETE /api/forks/<id>` | |
| `GET /api/art/<path>` | content-hashed listing pictures |

Security: local mode follows `alhazen dashboard` (loopback bind, random token in the URL
fragment, `X-Alhazen-Token` header, exact `Host` check, `Origin` check). Server mode uses an
`HttpOnly`, `SameSite=Strict` session cookie (`Secure` unless the operator runs plain HTTP
on loopback), a CSRF header on every state-changing request, a strict Content-Security-
Policy, login throttling, and refuses to start on a non-loopback address unless the
operator states how TLS is provided (`--tls-terminated-by-proxy`).

## 7. Web app

Canonical source: `platform-web/src/` (React 18, the approved Store/HubDocs experience: Home,
Browse, emblems, Overview / Methods / Tasks & parameters / Versions, private copies with
Changes / Run / Files, source, provenance, assumption and deviation fields, honest stale and
missing documentation). The app talks to a **host** object only:

```
host.kind                      'mana' | 'local' | 'server'
host.viewer                    {uid, name} | null
host.capabilities              {fork, publish, changeWithMana, runOnMachines, newWithMana,
                                uploadRevision, downloadRevision, machines}
host.subscribe{Listings,Forks,Assets,Machines}(cb, onError) -> unsubscribe
host.subscribeDocs(listing, cb, onError), host.subscribeForkDocs(fork, cb, onError)
host.get(handle) helpers: bundleMeta(handle), bundleChunk(handle, i)
host.fork(listing, progress) -> forkId
host.publish(fork, form), host.unpublish(fork), host.removeFork(fork)
host.startJob(fork, kind, request, machine), host.newExperiment(title, request)   (Mana)
host.uploadRevision(fork, file, message), host.archiveUrl(handle)                 (standalone)
host.art {url, encodePath, error}, host.copyText(text), host.useTheme(), host.showChat(id)
```

The implemented hosts add a few members the app uses for presentation and upkeep (`key`,
`downloadArchive`, `links`, `openTerminalApp`, `fontStylesheet`, `inlineStyles`, `extraCss`,
`signOut`, `refresh`, and the `themeToggle` / `signIn` capabilities); `platform-web/README.md`
lists them. The standalone page loads `platform.css` instead of inline `<style>` so it runs
under the strict Content-Security-Policy.

Differences between hosts are explicit in the UI: standalone hosts replace "Change with
Mana" by download / upload of a revision and show the exact commands to run a copy on a rig;
"My machines" and "Fork with AI" appear only where Mana Terminal and Mana chats exist.

`platform-web/build.mjs` produces (1) `src/alhazen/experiment_platform/web/platform.js`
for the standalone hosts and (2) `--mana <file>`: the Mana `user_div` code (the same source
plus the Mana host adapter, no imports), so the repository is the canonical source of the
Mana app; installing an export into a Mana workspace is a separate, reviewed step.
`--check` rebuilds and fails if the committed bundle differs; it runs locally and in the
Python tests when Node is present, not in a CI job.

## 8. Mana integration

Mana keeps its storage layout (`hub/published`, `hub/bundles`, `hub/docs`, viewers'
`alhazen-experiment-platform-mine`). The Mana host adapter in `platform-web/src/host_mana.jsx`
maps the host interface onto it. The Mana catalogue refresh imports
`alhazen.experiment_platform.catalog` from an installed alhazen and writes the same records
to Firebase, as a Mana-side script outside the repository. A guide overlay authored for one
bundle is never applied to another (amodal-averaging and mbri bundles built with the
portable builder keep small binaries, so their ids differ from text-only bundles and their
overlays must be re-bound after review). AI generation ("Change with Mana", "Fork with
AI", "New experiment with Mana") and execution on paired machines exist only in the Mana
host.

## 8a. Rigs: the dashboard's library

A rig's dashboard lists its owner's library and downloads a copy only when it is to be run
(`alhazen.experiment_platform.library` for the protocol and the disk, `alhazen.cli.
workspace_library` for the page and the jobs).

- **Connection.** `<state dir>/library.json` (mode 0600; never in an experiment folder):
  `{schema: 1, url, token, auth: "alhazen-token" | "bearer", label, copies, extras, expires?}`.
  `alhazen library connect` writes it from a local platform's `#token=` URL or a platform
  address plus a token file; the Mana kit writes it on every run (below). Plain `http` is
  accepted only for loopback addresses. The page shows the platform's origin, never the URL's
  path (a Mana ingress path is itself a credential).
- **Client.** Two requests of Appendix A: `GET /api/forks` and `GET /api/bundles/<handle>/
  archive`, the same against `alhazen platform local` and Mana. A server host's cookie
  sessions are not supported yet (it would need per-rig API tokens).
- **Disk.** Every download writes `<copies>/<copy id>/.alhazen-hub/source.json`
  (`{schema: 1, id, bundle, title, source, writtenAt, files, previous}`). A copy's folder is
  `absent`, `current` (marker names the library's revision), `outdated` or `unknown` (no
  readable marker). Data is any run folder (`session.json` or `config_snapshot.yaml`) under the
  folder, environments and hidden folders excluded. Writing overwrites the revision's files and
  deletes nothing. A current folder is not fetched again; an unknown folder holding exactly the
  revision's files is adopted; an outdated or unknown folder with data raises `CopyConflict`
  unless the person answers "update the files, keep the data".
- **Download job.** One background job per copy: checking, fetching, writing, building
  (`uv sync --locked` plus the extras the experiment declares among the connection's
  `extras`), registering; the page polls `GET /api/library`. Copies on disk with data and an
  environment are registered when the library is refreshed.
- **Mana.** A rig cannot read Firebase, so the Mana kit (`hubkit.library_connection`) runs a
  small service in the viewer's own sandbox (daemon `alhazen-library` behind ingress) that
  answers those two requests from `<viewer>/file-system/alhazen-experiment-platform-mine`. It
  serves only that user's copies; each rig gets its own bearer token, stored in the sandbox only
  as a SHA-256 hash with an expiry (7 days, renewed on every run), alongside an ingress link of
  the same lifetime. `hubkit.run_on_machine` applies the same disk rules before copying
  (raising `CopyHasData` instead of overwriting), adds the experiment to a dashboard that is
  already open, and writes the connection file by file transfer (the token never appears in a
  terminal command).

## 9. Decisions and their price

- **The library is pulled by the rig, not pushed by the platform**: the rig decides what to
  download and when, and the same client works against every host. Price: Mana needs a service
  the rig can reach (a daemon and an ingress route per user) and tokens to renew.

- **Same repository, optional module** rather than a separate distribution: one version
  number for toolkit and reader; the price is that the alhazen wheel carries ~0.5 MB of web
  assets. Revisit if the platform needs its own release cadence.
- **File store, not a database, for catalogue records**: the records are documents keyed by
  path, mostly immutable, and the expected scale is hundreds of experiments and users per
  host; atomic file replacement under one lock is enough and is trivial to back up. Price:
  one writer at a time per store. Accounts and sessions use SQLite (WAL).
- **WSGI + waitress** rather than an ASGI framework: no async needs, pure Python on every
  OS, one small dependency. Price: no WebSockets; the web app refreshes by polling and after
  its own writes.
- **Bundle id compatible with the Mana kit** (10 hex of sha256): kept so the same source
  produces the same id on every host; the full digest is stored and verified.


## Appendix A. HTTP API contract (JSON)

Every response is JSON unless stated. Errors are `{"error": {"kind": <class>, "message":
<text>}}` with 400 (InvalidRequest, BundleError), 401 (AuthError: not signed in), 403
(Forbidden), 404 (NotFound), 409 (Conflict), 413 (upload too large), 500 (anything else; the
message is generic and the detail goes to the server log).

Authentication: local host, header `X-Alhazen-Token: <token>` (from the URL fragment
`#token=`); server host, the `alhazen_platform_session` cookie plus header
`X-Alhazen-CSRF: <csrf>` (from `/api/session`) on every POST and DELETE, and
`X-Alhazen-Platform: 1` on every POST and DELETE including `/api/login` (a header a
cross-site form cannot send).

| Request | Response |
|---|---|
| `GET /api/session` | `{kind: "local"\|"server", viewer: {uid, name}\|null, csrf: str\|null, capabilities: {fork, publish, uploadRevision, downloadRevision, changeWithMana: false, runOnMachines: false, newWithMana: false, machines: false, signIn: bool}}` |
| `POST /api/login` `{username, password}` | the session object; sets the cookie |
| `POST /api/logout` | `{}`; clears the cookie |
| `GET /api/assets` | `{hero: <svg>, penrose: <svg>}` |
| `GET /api/catalogue` | `{published: {<publisherUid>: {<exp>: listing}}}`; every `listing.bundle.path` is a bundle handle, and `listing.docsStatus` is computed |
| `GET /api/catalogue/<uid>/<exp>/docs[?version=<bundleId>]` | the documentation record (`guide`, `methods`, `parameterGuide`, `readme`, `references`, `tasks`, `assumed`, `deviations`, all strings as stored) plus `docsStatus`, `bundleId` |
| `GET /api/bundles/<handle>/meta` | bundle meta (`files` and `excluded` are JSON strings, as in Mana) |
| `GET /api/bundles/<handle>/chunks/<n>` | the chunk: a JSON array of entries (the text is returned as is) |
| `GET /api/bundles/<handle>/archive` | `application/zip`, everything under `<exp or fork id>/` |
| `GET /api/forks` | `{forks: {<forkId>: fork}}`; `fork.head.path` is a bundle handle |
| `POST /api/forks` `{publisher, exp}` | `{id, fork}` |
| `GET /api/forks/<id>/docs` | `{docs: record\|null, readme: str\|null, stale: bool}` |
| `POST /api/forks/<id>/revisions?message=<text>` body: zip bytes | `{revision: {bundle, message, by, at}, fork}` |
| `POST /api/forks/<id>/publish` `{title, summary, category, subjects, hardware, version, license}` | `{listing}` |
| `POST /api/forks/<id>/unpublish` | `{}` |
| `DELETE /api/forks/<id>` | `{removed: {bundles: [bundleId], docs: [docsKey]}}` |
| `GET /api/art/<path>` | the image (content-hashed name, cacheable) |

Bundle handles are opaque to the web app: `L~<publisher>~<exp>~<bundleId>` for a published
version, `F~<forkId>~<bundleId>` for a revision of the viewer's own copy. A handle names a
version, so its content never changes.
