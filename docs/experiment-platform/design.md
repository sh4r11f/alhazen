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

## 5a. Who can see an experiment (access, 3.7)

Requested 2026-10-21: people keep experiments they made in their own account, see them under My
experiments and run them on their rigs without putting them on the marketplace; and they can
publish an experiment to specific people instead of everyone.

**Listing field.** `access: {visibility, people}`:

| `visibility` | UI label | Who can see the listing |
|---|---|---|
| `private` | Only you | the publisher |
| `people` | Specific people | the publisher and every uid in `people` |
| `public` | Everyone | anyone allowed to read the catalogue (section 5) |

A listing with no `access` (written before 3.7) is `public`. Every write from 3.7 on stores
`access` explicitly. `people` is a list of account ids (never emails or names); it is kept only
when `visibility` is `people`. A stored `access` this version does not understand (an unknown
`visibility`, not an object) is read as `private`: an unknown rule never widens who sees a
listing. The rule lives in `experiment_platform/access.py`.

**What it covers.** Visibility applies to the listing and everything reached through it: its
versions, documentation records, bundles and archives (`L~` handles), and forking. A principal
who cannot see a listing gets **404** (`This experiment is not in the catalogue`), never 403,
so names and ids do not leak. Copies people made while they could see a listing stay theirs
when access is narrowed (as with unpublishing); narrowing never reaches into other libraries.

**Presentation** (per principal). The publisher sees `access` in full,
`{visibility, people (always a list, empty unless visibility is people), names: {uid: name}}`
(`names` holds the enabled accounts only), and `canManage: true`. Anyone else sees
`access: {visibility}` only, `canManage: false`, plus `sharedWithYou: true` when they are in
`people`. `GET /api/catalogue` leaves out a publisher none of whose listings the principal
may see.

**Changing it.** Only the publisher: `PlatformService.set_access(principal, publisher, exp,
access)`. Another principal who can see the listing gets 403 (`Only the publisher can change
who sees this`); one who cannot gets 404. `people` is de-duplicated, the publisher is removed
from it, at most 200 entries, each must be an existing, enabled account (`InvalidRequest`
naming the id otherwise); `people` with no one left is refused (`Add at least one person, or
choose Only you`).

- **Publishing a copy** takes an optional `access` in the form; without it, an existing
  listing keeps its access and a new one is `public`.
- **Refresh** never changes the access of an existing listing; a new listing takes
  `alhazen platform refresh ... --visibility private|people|public` (default `public`;
  `people` needs `--people uid,...`).
- **Operators:** `alhazen platform access <publisher> <exp> --visibility ... [--people ...]`.
- **Operators:** `--store DIR` or `--config FILE` (a server's); without `--visibility` the
  command prints the current access. People are checked against the store's accounts, so a
  local store (no accounts) cannot share with people.
- **Finding people** (signed in only, else 401): `people(principal, query)` matches an exact
  username, or a username, a name or a word of a name that starts with the query when it has
  at least 2 characters (case-insensitive for ASCII; `%` and `_` are literal); exact usernames
  first, then by username; at most 10 results; never the asking principal; disabled accounts
  are never returned; result `[{uid, name, username}]`. `describe_people(principal, uids)`
  returns the same for the known, enabled ids among at most 200, in the order asked. The
  service gets these from a `PeopleDirectory` the host hands it (the server's `Accounts`).
  The local host has one user and no people search (`shareWithPeople: false`; `GET
  /api/people` answers `{people: []}` and a `people` visibility is refused).

**HTTP** (Appendix A): `GET /api/catalogue` is filtered for the principal;
`PUT /api/catalogue/<pub>/<exp>/access {visibility, people}` returns `{listing}` (CSRF like
every write); `GET /api/people?q=<text>` returns `{people: [{uid, name, username}]}`;
`GET /api/people?uids=<a,b>` returns the same for known ids (publisher's view of a listing's
people); session capability `shareWithPeople` (server true, local false). Forking, docs and
bundle reads of a listing the principal cannot see are 404.

**Mana host.** Public listings stay in the hub (`hub/published/<pub>/<exp>`, `hub/bundles`,
`hub/docs`). The hub is readable by everyone it is shared with and Mana grants inherit
downward, so a non-public listing cannot live there: it lives in the publisher's own
workspace, `<pub>/file-system/alhazen-experiment-platform-mine/listings/<exp>/` with
`listing` (the record; its `bundle.path` and `versions` point inside this node), `docs` (same
record as `hub/docs/<pub>/<exp>`), `bundles/<bundleId>` (`{meta}`; files in Storage, below), and
`metadata: {type: link, hidden: true, permissions: {read: {<uid>: true}}}` mirroring
`access.people`; the field and the grants are written in one update.

- *Your own non-public listings* are indexed at `mine/listed/<exp> = {visibility, at}` (derived,
  written with every write of `listings/<exp>`, rebuildable from it). The app subscribes to each
  listing's `listing` record separately; subscribing to `listings/` itself would download every
  bundle.
- *Finding what is shared with you:* (1) the hub owner's share index
  `<hubOwner>/file-system/alhazen-experiment-platform-shares` (hidden link with
  `per_user_read`, so each person reads only `<their uid>/`), entries
  `<recipient>/<pub>~<exp> = {publisher, exp, at}`, written when the publisher is the hub owner;
  (2) each person's own pointers `mine/shared/<pub>~<exp>`, saved when they open a share link
  (`?ehv=exp&ehid=<pub>~<exp>`) to a listing they can read. A pointer whose listing cannot be
  read is ignored.
- *Changing visibility* moves the records between the hub and the publisher's workspace: copy
  bundle files to the destination's Storage (below), then docs, then the listing last; read back; then
  delete the old location. A hub bundle that another of the publisher's hub listings names is not
  deleted. Copies in libraries are untouched. Publishing a copy with a different visibility moves
  the existing listing first, so its earlier versions go with it.
- *Bundle files are in Firebase Storage, not the database (since 3.7.2).* Every device that
  opens Mana loads the owner's whole `file-system` tree, so bundle chunks there (source plus
  base64 stimulus images, one copy per release) made the owner's workspace too large to open on
  a phone (488 MB in October 2026). A bundle node keeps only `meta`, with
  `meta.store = {v: 1, owner, path}`; chunk *i* is the Storage object `<owner>/<path>/<i>.json`
  holding the same JSON text `chunks/<i>` used to hold. Public bundles are under the hub's
  `bundles` storage grant, `app-storage/<hub app path>/bundles/<pub>/<bundleId>` (readable by
  everyone who can read the hub; written by the hub owner's page and by hubkit as the owner).
  Private bundles (copies, non-public listings) are under
  `alhazen-experiment-platform-mine/bundles/<bundleId>` in their owner's own Storage;
  bundle ids are content hashes, so a copy and its private listing share the files. Forking
  copies the files into the viewer's own Storage (so a copy outlives the listing), publishing
  and changing visibility copy them where the destination keeps them, and a bundle whose files
  already sit there is named, not copied. Bundles written before 3.7.2 (database `chunks/<i>`)
  are still read and are moved to Storage when copied; a one-time migration moved the hub
  owner's. Known limit: a person a non-public listing is shared with sees its record and methods
  but cannot open its files (they are in the publisher's own Storage). Removing a copy leaves
  its Storage files in place.
- *Docs text is in Firebase Storage too (since 3.8.8).* A docs record's texts (methods, readme,
  tasks, the task guide, the parameter guide, ...) were the next largest thing in the tree, copied
  again into every private listing and every copy's snapshot. A text longer than 1024 characters is
  stored once under its sha256 and the record keeps `{sha, bytes, store: {v: 1, owner, path}}` in
  its place: public listings in the hub's `bundles` grant at `_docs/<sha>.txt`, private ones in the
  owner's own Storage at `alhazen-experiment-platform-mine/docs/<sha>.txt`. Readers fetch on demand
  and check the sha256. A copy's snapshot names the listing's objects when it can read them (the
  hub's, or its own), so identical text is never stored twice. Listings shared with specific
  people keep their texts inline, because those people cannot read the publisher's own Storage.
  When a listing leaves the hub, the hub owner's page removes public doc objects no hub record and
  none of the owner's copies still name; another person's copy of an old version may then read as
  stale (its README stands in). Records with inline texts are still read.
- *People* are found by email (Mana `/share/lookup-user`) and shown by email to the publisher
  (`/share/lookup-users`); people added can be sent Mana's share email (`/share/notify`).
- *hubkit* (`run_listing`, forks made by chats) reads a listing from the hub, else from the
  publisher's `mine/listings/<exp>`.

**Web app.** Cards and listing headers carry a badge for non-public listings (`Only you`,
`Shared`, or `Shared with you`). The publisher's listing sheet says who can see it with a
Change button (Access dialog: Only you / Specific people / Everyone, a people picker through
`host.findPeople`, a list with Remove, on Mana an "Email them a link" box). The publish dialog
of a copy asks who can see it first (default Everyone). My experiments shows, above the copies,
*Not on the marketplace* (your private and shared listings) and *Shared with you*, and a
collapsed *On the marketplace* list of your public listings. Browse is the marketplace: it
shows public listings only, for every viewer including the publisher (`marketplaceListings`);
private listings and listings shared with specific people are reached from My experiments, never
Browse. Home's sections show public listings only, plus a *Shared with you* section when there
is one.
A listing that is not public offers **Publish to the marketplace** directly: a Publish button on
its *Not on the marketplace* row and a link beside Change on its listing sheet. Both open a
confirmation that says what it means (in Browse for everyone; methods, source files and every
recorded version readable and forkable; copies made meanwhile stay their owners') and then make the
same access change as Change > Everyone. On Mana that move also carries the catalogue builder's
records for the listing (`method-figures/<exp>` and `kit/method-guides/descriptors/<exp>` in the
hub, `methodFigures` and `guideDescriptor` beside a non-public listing) in both directions, when the
mover may write them (the hub owner); before 3.8.6 a move dropped them.

**Actions on your own listing (3.9).** A listing the viewer publishes is not offered *Add to
library*: without a copy its sheet shows **Run on a machine** (makes the copy, then the Run dialog's
run) and **Change with Mana** (makes the copy, then a change request), with **Make a copy** as a
quiet link; with a copy, **Open my copy**, **Duplicate** and **Duplicate with Mana**. Someone else's
listing keeps *Add to library* / *Fork with AI* (*Open my copy* / *Fork again* once copied), and a
host without runs on machines keeps the old labels. In My experiments a copy sits under the
listing it came from when that listing is shown there (*Not on the marketplace*, *Shared with
you*); the remaining copies are listed below as *Other copies* (or *Your copies* when none are
grouped). (Until 3.8.5 Browse showed everything the viewer could see, with a "Visible to" filter;
that put a publisher's private research experiments on their marketplace page.)

Host interface additions: `host.setAccess(listing, {visibility, people, notify})` resolving to
`{access, notified: {sent, failed}}`, `host.findPeople(query) -> [{uid, label}]`,
`host.describePeople(uids) -> {uid: label}`, `host.shareLink(listing)` (the URL that opens the
listing page), capabilities `shareWithPeople` and `notifyPeople` (Mana only: it sends Mana's share
email; the server host sends none). The Mana host also has `resolveListing(pub, exp) -> bool` for
a share link to a listing it has not listed yet. `publish(fork, form)` takes `form.access` and
`form.notify`. A listing's `canManage` is `listing.canManage === true` (server) or
`listing.uid === host.viewer.uid`.

## 6. HTTP surface

One WSGI application (`experiment_platform.http`) serves both standalone hosts:

| Route | Meaning |
|---|---|
| `GET /` and `/assets/*` | the web app (compiled) |
| `GET /api/session` | who is signed in, host kind, capabilities |
| `POST /api/login`, `POST /api/logout` | server mode only |
| `GET /api/catalogue` | all listings `{publisher: {exp: listing}}` |
| `GET /api/catalogue/<pub>/<exp>/docs[?version=<versionKey or bundleId>]` | documentation record + docsStatus (a bundle id means its latest version) |
| `PUT /api/catalogue/<pub>/<exp>/access` | who can see a listing (publisher only, section 5a) |
| `GET /api/people?q=<text>` or `?uids=<a,b>` | people to share with (signed in, server only) |
| `GET /api/bundles/<handle>/meta`, `/chunks/<n>` | bundle files (handle: `L.<pub>.<exp>.<bundleId>` or `F.<forkId>.<bundleId>`) |
| `GET /api/bundles/<handle>/archive` | the bundle as a zip |
| `GET /api/forks`, `POST /api/forks` | the principal's forks; fork a listing |
| `GET /api/forks/<id>/docs` | the docs snapshot |
| `POST /api/forks/<id>/revisions` | upload a zip as a new revision |
| `POST /api/forks/<id>/publish`, `/unpublish`; `DELETE /api/forks/<id>` | |
| `GET /api/art/<path>` | content-hashed listing pictures |
| `GET /api/machines`, `POST /api/machines`, `DELETE /api/machines/<id>` | the person's machine tokens and the machines' recent reports |
| `POST /api/machines/events` | a rig's report (machine token only) |

Security: local mode follows `alhazen dashboard` (loopback bind, random token in the URL
fragment, `X-Alhazen-Token` header, exact `Host` check, `Origin` check). Server mode uses an
`HttpOnly`, `SameSite=Strict` session cookie (`Secure` unless the operator runs plain HTTP
on loopback), a CSRF header on every state-changing request, a strict Content-Security-
Policy, login throttling, and refuses to start on a non-loopback address unless the
operator states how TLS is provided (`--tls-terminated-by-proxy`). Both hosts also accept a
**machine token** (`Authorization: Bearer alm_...`), made per rig on the Machines page and
stored as a SHA-256 in `<store>/machines.sqlite3` (`experiment_platform.machines`). A token
reaches only `GET /api/forks`, `GET /api/bundles/F~.../archive` (its owner's own revisions) and
`POST /api/machines/events`; everything else is 403. It needs no CSRF header because a browser
never sends it on its own; revoking it, or disabling the account, refuses it at once (401).

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
host.openDashboard(window, label), host.subscribeDashboards(cb)                  (Mana)
host.shutdownDashboard(window, label)                                           (Mana)
host.subscribeRigMachines(cb), host.subscribeRigMachine(mid, cb), host.putRigConfig(...),
host.deleteRigConfig(...), host.setRigDefault(...), host.resolveRigConfig(...),
host.forgetRigMachine(mid), host.loadYaml()                                      (rigConfigs)
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

**Machines and their rig configs (3.8).** The Machines page (My machines in Mana) is one list of
machines: the computers paired with Mana Terminal, or the machine tokens on a standalone host,
plus any machine whose dashboard syncs rig configs but matches no row. A row is matched to the
rig machines its dashboard synced by label (library.json `machine`, else the host name; trimmed,
case-blind), the same rule as the Run dialog's config choice; one machine can match more than
one record when dashboards with different state folders sync from it. Each row shows one line
about its configs and has **Configure**, which opens the machine's Configure window: an in-page
modal (keyboard and touch, Escape or the scrim closes it, it scrolls inside itself) listing the
configs with the default and their state, and, in the same window, the editor (settings form and
YAML, save, refusals, conflicts, make default, delete) and the new-config form. A row with no
synced record opens a window that says so and offers Open dashboard (Mana), which connects the
machine; no machine record is ever made in the browser, ids come from the dashboard. The window
is in the address so Back closes it and a link reopens it:
`?ehv=machines&ehdev=<row>[&ehid=<machine>&ehtab=<config|new>]` for a paired machine,
`?ehv=machines&ehid=<machine>[&ehtab=<config|new>]` for a synced machine (the address of the
full-screen editor before 3.8, so old links still open). Each step into the window is a history
entry; Close goes back past all of them. The rig configs belong to the machine and every
experiment run on it uses them; nothing about them is set inside an experiment's page.

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

**Open dashboard (3.7.1).** My machines has an *Open dashboard* button on each paired machine with
an open terminal window: Mana's dashboard (`<home>/mana-experiments/.dashboard`) opens there with
no experiment chosen. It lists the experiments already set up on that machine and, through the
library connection, every copy in the person's library, which it downloads and sets up on
request; sessions run in each experiment's own environment, so the dashboard needs only alhazen.
hubkit's `open_dashboard(machine)` opens an already running dashboard in the machine's browser;
otherwise it installs the newest alhazen release into `<home>/mana-experiments/.alhazen-dashboard`
(kept between runs, upgraded when a newer release is asked for), connects the library and starts
`python -m alhazen dashboard --state-dir ...`. The job is `mine/dashboards/<jobId>` (`kind:
dashboard`); the library service closes it on the dashboard's closing report like a run job.
Standalone hosts have no remote execution: a rig opens its dashboard with `alhazen dashboard` and
reaches the library through a machine token (section 8a).

**Dashboard state and Shut down (3.8).** Each machine row says what its dashboard last reported, from
the newest run or dashboard job on that machine (a job is on the machine its `machineLabel` names,
else the machine of the terminal window `machine` names, else `machine` taken as a label): *Starting
the dashboard* (running), *Dashboard open since …* (done with a url, no `closedAt`: the last known
state, since a dashboard that stopped without reporting still reads open), *Dashboard closed …*
(`closedAt`). Runs rows and a copy's Run tab and Last run show such jobs as *Open* or *Closed*
instead of *Done*, and the dashboard's address only while open. A row that reads open has **Shut
down** (it needs an open terminal window there), confirmed in a small dialog; the Mana host's
`shutdownDashboard(window, label)` writes `mine/dashboards/<jobId>` (`kind: shutdown`) and asks a
chat to run hubkit's `shutdown_dashboard(machine, job_id)`. That refuses while a session runs
(`DashboardBusy`, the job is left waiting with that message), otherwise stops the dashboard (its
Shut down request, or ending the process of an older one) and marks every run and dashboard job
still open on that machine closed (`closedAt`, `closedBy`), then finishes its own job. A refusal in
the terminal is final (`stopped`). Standalone hosts have no Shut down: the dashboard is closed on
the rig.

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
- **Client.** Three requests of Appendix A: `GET /api/forks`, `GET /api/bundles/<handle>/
  archive` and, when the dashboard closes, `POST /api/machines/events`; the same against
  `alhazen platform local`, `alhazen platform serve` and Mana. On a standalone host the rig uses
  a **machine token**: the person makes one on the Machines page (shown once) and runs
  `alhazen library connect <address> --token <token>` on the rig, or pastes both into the
  dashboard's Library section (`POST /api/library/connect`, which lists the copies before it
  saves anything). The connection may also name the machine (`machine`) and the platform's run
  jobs it serves (`runJobs`, written by Mana's kit).
- **Reading the list.** The dashboard reads the list in the background the first time the page
  asks after a connection appears or changes (a `library.json` written while it runs, as Mana's
  kit does when it joins an open dashboard); `GET /api/library` then says `refreshing: true`, and
  any failure is kept as `error`. The page asks every few seconds while nothing is connected, so
  a connection made meanwhile shows without a reload. A changed connection drops the old list.
- **Rig configs (3.6).** The same connection syncs the machine's rig configs
  (docs/rigs.md §10; protocol in docs/design/machine-rig-configs.md). Per person and machine
  (a dashboard state folder, random id `mc_…`) the platform keeps one machine record: each
  config's text, a revision that moves on every content write from either side, its state
  (`synced`, `pending` for an edit made on the platform that the machine has not taken,
  `rejected` with the machine's reason), what the machine reported it runs, a conflict copy, the
  default, the machine's rig schema and the templates it can start a config from. The rules are
  one pure module, `alhazen.experiment_platform.rigsync`: every write is compare-and-set on the
  revision, the machine validates every platform edit as a launch would before taking it, and a
  conflict keeps both versions until the person keeps one. The standalone hosts store records in
  `<store>/rigs.sqlite3` (`rigstore.py`); Mana keeps them at
  `<viewer>/file-system/alhazen-experiment-platform-mine/machines/<id>` (configs under
  `rigs/<id>`), written by the web app for the person's edits and by the viewer's
  `alhazen-library` service (which vendors `rigsync.py`) for the machine's syncs. The machine
  syncs when it reads the library, every five minutes while the dashboard runs, after each
  change made on it and on demand; a platform without these routes (404) leaves its configs
  local. On the Machines page (My machines in Mana) each machine's Configure window (section 7)
  shows its configs with the same sections as the dashboard's editor and a YAML view; Mana's Run
  dialog names the config, and the dashboard opens with it chosen (`?rig=`, `alhazen dashboard
  --rig-config`).
- **Closing.** When the dashboard stops (Shut down on the page, Ctrl+C), after its active run
  has ended, it sends `dashboard-closed` to its library: machine, alhazen version, start and
  close times, why it closed, the run jobs named in its connections, and every session launched
  since it started (experiment, library copy and revision, task, mode, start and end, how it
  ended and the outcome counts of the console's last line). It waits at most 5 s and never
  stops the shutdown; the result is printed and appended to `<state>/library/reports.log`. A
  standalone host keeps the report for the Machines page; Mana's service marks the matching run
  jobs finished and notifies the person.
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
  (raising `CopyHasData` instead of overwriting), writes the connection file by file transfer
  (the token never appears in a terminal command) before it starts or joins the dashboard, so
  the page lists the library from its first read, names the run job in it, and adds the
  experiment to a dashboard that is already open. The service answers `POST
  /api/machines/events` too: it marks the reported run jobs (and, for an older connection that
  names none, the open run jobs of that machine) finished with a plain summary.

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
cross-site form cannot send). A rig, on either host: `Authorization: Bearer <machine token>`,
for the requests marked *machine* below and no others (403); an unknown, revoked or
disabled-account token is 401.

| Request | Response |
|---|---|
| `GET /api/session` | `{kind: "local"\|"server", viewer: {uid, name}\|null, csrf: str\|null, capabilities: {fork, publish, uploadRevision, downloadRevision, changeWithMana: false, runOnMachines: false, newWithMana: false, machines: false, machineTokens: bool, rigConfigs: bool, signIn: bool, shareWithPeople: bool}}` (`shareWithPeople`: true for a signed-in server viewer, false locally and for a visitor) |
| `POST /api/login` `{username, password}` | the session object; sets the cookie |
| `POST /api/logout` | `{}`; clears the cookie |
| `GET /api/assets` | `{hero: <svg>, penrose: <svg>}` |
| `GET /api/catalogue` | `{published: {<publisherUid>: {<exp>: listing}}}`, only the listings the viewer may see (section 5a); every `listing.bundle.path` is a bundle handle, `listing.docsStatus` is computed, and `listing.access`, `canManage`, `sharedWithYou?` are presented for the viewer |
| `GET /api/catalogue/<uid>/<exp>/docs[?version=<bundleId>]` | the documentation record (`guide`, `methods`, `parameterGuide`, `readme`, `references`, `tasks`, `assumed`, `deviations`, all strings as stored) plus `docsStatus`, `bundleId`; 404 for a listing the viewer may not see |
| `PUT /api/catalogue/<uid>/<exp>/access` `{visibility: "private"\|"people"\|"public", people?: [uid]}` | `{listing}` as the publisher sees it; 403 for another viewer who can see the listing, 404 for one who cannot, 400 for an unknown or disabled person, an empty `people`, or more than 200 |
| `GET /api/people?q=<text>` (signed in) | `{people: [{uid, name, username}]}`, at most 10 (section 5a); 401 when not signed in |
| `GET /api/people?uids=<uid,uid>` (signed in) | `{people: [{uid, name, username}]}` for the known, enabled ids (at most 200), in the order asked |
| `GET /api/bundles/<handle>/meta` | bundle meta (`files` and `excluded` are JSON strings, as in Mana) |
| `GET /api/bundles/<handle>/chunks/<n>` | the chunk: a JSON array of entries (the text is returned as is) |
| `GET /api/bundles/<handle>/archive` (also *machine*, `F~` handles only) | `application/zip`, everything under `<exp or fork id>/` |
| `GET /api/forks` (also *machine*) | `{forks: {<forkId>: fork}}`; `fork.head.path` is a bundle handle |
| `POST /api/forks` `{publisher, exp}` | `{id, fork}`; 404 for a listing the viewer may not see |
| `GET /api/forks/<id>/docs` | `{docs: record\|null, readme: str\|null, stale: bool}` |
| `POST /api/forks/<id>/revisions?message=<text>` body: zip bytes | `{revision: {bundle, message, by, at}, fork}` |
| `POST /api/forks/<id>/publish` `{title, summary, category, subjects, hardware, version, license, access?}` | `{listing}`; without `access`, an existing listing keeps its own and a new one is public |
| `POST /api/forks/<id>/unpublish` | `{}` |
| `DELETE /api/forks/<id>` | `{removed: {bundles: [bundleId], docs: [docsKey]}}` |
| `GET /api/art/<path>` | the image (content-hashed name, cacheable) |
| `GET /api/machines` | `{machines: [{id, name, created, lastUsed}], events: [event]}` (newest 20 reports) |
| `POST /api/machines` `{name}` | `{machine: {id, name, created, lastUsed: null}, token}`; the token is returned this once |
| `DELETE /api/machines/<id>` | `{}`; the token is refused from now on |
| `POST /api/machines/events` (*machine* only) `{event: "dashboard-closed", machine, alhazen, startedAt, closedAt, reason, jobs: [jobId], sessions: [{experiment, copy, revision, task, mode, startedAt, endedAt, status, session, outcomes: {<outcome>: n}}]}` | `{ok: true, event}`: what was kept (strings bounded, unknown fields dropped, `machine` is the token's name and `reportedName` the one sent; 200 reports kept per person) |
| `GET /api/rigs/machine/<mid>` (*machine*; also the local token) | `{record: MachineRecord\|null}` (docs/design/machine-rig-configs.md §4) |
| `POST /api/rigs/machine/<mid>/sync` (*machine*; also the local token) `{label, alhazen, rigSchema?, rigSchemaSha, templates?, default, defaultRevSeen, writes, acks, conflicts, deletes, running}` | `{record, results: {<configId>: {ok, revision, conflict?}}}`; a write applies only against the current revision |
| `GET /api/rigs` | `{machines: [record without schema, templates or texts]}` |
| `GET /api/rigs/<mid>` | `{record}` |
| `PUT /api/rigs/<mid>/configs/<cid>` `{name, text, base, basedOn}` | `{record, config}`: a new revision, `pending` until the machine takes it; 409 when `base` is not the current revision |
| `DELETE /api/rigs/<mid>/configs/<cid>?base=<revision>` | `{record}`: a deletion pending until the machine removes it (the default is refused) |
| `PUT /api/rigs/<mid>/default` `{config, base}` | `{record}`; 409 when stale |
| `POST /api/rigs/<mid>/configs/<cid>/resolve` `{keep: "machine"\|"platform"}` | `{record}` |
| `DELETE /api/rigs/<mid>` | `{}`: the machine is forgotten until its next sync |

Bundle handles are opaque to the web app: `L~<publisher>~<exp>~<bundleId>` for a published
version, `F~<forkId>~<bundleId>` for a revision of the viewer's own copy. A handle names a
version, so its content never changes. An `L~` handle of a listing the viewer may not see is
404 (meta, chunks and archive); a viewer's `F~` handles keep working after the listing they
copied is narrowed. `PUT` requests carry the same CSRF header as POST and DELETE.
