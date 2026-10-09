# Experiment Hub: the web interface

Status: development branch, not deployed. Node tests cover the page's logic on a fake document with
a fake hub behind `fetch`; real browser rendering, the real server and the real rig adapter are
verified separately during integration.

One static page serves both hosts:

| Host | Address | Credential the page sends | Extra screens |
|---|---|---|---|
| Central hub service | `/` | HttpOnly session cookie; `X-CSRF-Token` on every non-GET request | none |
| Rig dashboard (`alhazen dashboard --hub`) | `/hub#token=…` | `X-Alhazen-Token` (the dashboard token) on every request; `?token=` on plain download links | This rig |

The page learns which host it is on from `GET /api/hub/v1/config` (`role`). On a rig it reads the
token from the address fragment, keeps it in the tab's `sessionStorage` under the workspace's own key
(so the same tab can open the workspace) and removes the fragment from the address. It never stores a
hub password, bearer token or CSRF token in `localStorage`; on a rig the hub credential stays in the
dashboard's private file and never reaches the browser.

## Files

`src/alhazen/hub/assets/`:

| File | Role |
|---|---|
| `index.html` | The page: masthead (brand, role, navigation, account), banner, `#main`, footer with the theme switch. No inline script or style. |
| `hub_core.js` | Pure helpers, `window.HubCore`: screen addresses, the API client and its error kinds, input checks, formatting. |
| `hub.js` | Views and controller, `window.HubApp`: draws one screen per address. |
| `hub.css` | Styles; light palette first, dark palette for Auto/Dark. |
| `hub_docs.js`, `hub_docs.css` | The documentation viewer (Methods, task guides, Guide), owned by the documentation module; `hub.js` calls its `HubDocs.render*` functions and says plainly when it is missing. |
| `icon.svg`, `fonts/` | The alhazen logo and the Nunito font (SIL OFL, `fonts/OFL.txt`), copies of the workspace's, so the page renders offline. |

Both hosts serve these from a fixed table under `/hub/assets/` and the page loads nothing else, so it
works under the dashboard's `'self'`-only content security policy and on a rig with no internet.

## Screens

Each screen is a real address (`?view=…`), so Back, Forward, reload and shared links restore it.
`HubCore.parseRoute` drops unknown views and any parameter that fails its check; the sign-in `next`
parameter can only name one of the page's own screens.

| Address | Screen | Server routes |
|---|---|---|
| (none) | Landing: what the hub does, the experiment → rig → data path, the latest releases. An empty catalogue says nothing is published yet; no placeholder listings. On a rig, a strip with the hub connection, operator and installs. | `GET /catalog?limit=6` |
| `view=catalog&q=&offset=` | Search the published catalogue. | `GET /catalog` |
| `view=experiment&id=&version=&tab=&task=` | One experiment. Tabs: Overview (description, citations, release sheet with licence, hardware, platforms, Python and alhazen minimums, size, full SHA-256; pin in library; download on central, install on a rig), Methods, Tasks & parameters, Versions. | `GET /experiments/{id}`, `GET …/versions/{vid}/documentation`, `GET|POST /library`, `POST /local/install` |
| `view=guide` | How alhazen runs a session (modes, protections), from the installed alhazen; offline on a rig. | `GET /guide` |
| `view=signin&next=`, `view=register` | Sign in; register with an invite code (password 12 to 1024 characters). On a rig there is no registration form: the rig does not relay registration (409 `register_on_hub`), so the page links to `<configured hub>/?view=register` in a new tab and asks you to come back and sign in. The link is built only from the rig's configured hub address after the same check as the Connect form; without one it says to connect the rig first. | `POST /auth/login`, `POST /auth/register` (central only) |
| `view=library` | Pinned releases; install or open in the workspace (rig), download (central). | `GET /library` |
| `view=mine`, `&new=1`, `&id=` | My experiments: create, edit details, versions, add a version (upload a `.zip` on central; pack a registered project on a rig after reviewing every file and the manifest), publish with two acknowledgements and a confirmation, unpublish with a confirmation. | `GET|POST /experiments`, `PATCH /experiments/{id}`, `POST …/versions`, `POST …/publish|unpublish`, `GET /local/projects`, `POST /local/package-preview|package-upload` |
| `view=data&experiment=&subject=&mode=&offset=` | Uploaded sessions, filtered. The Experiment filter offers your own experiments, your library's (read fresh) and those the listed sessions name (`experiment_title`, the server's authorised label); it never lists anyone else's. | `GET /data/sessions`, `GET /experiments`, `GET /library` |
| `…&session=&toffset=` | One session: metadata, manifest digest, receipt durability, files with downloads, and the trial index from `session.index` (`status`, `rows`, `error`). Indexed: trial rows (20 per page) read from each item's `values` under the server's declared `columns`, with the row number and, when a session has several trial tables, the source file in their own columns; CSV/JSON export. Queued or rebuilding: said so, polled until the server reports a final state. Failed: the reason and **Rebuild trial index**. No trial table: said so. The raw files stay downloadable in every state. | `GET /data/sessions/{id}`, `…/trials`, `…/export`, `…/files`, `POST …/reindex` |
| `view=rig&tab=connection|installed|upload&project=&root=&run=&job=` | Rig only: connect or disconnect a hub; installed releases; choose a finished session, review it and opt in; follow a transfer. | `/local/*` |

Private screens (library, mine, data) send a signed-out reader to sign in and back afterwards.

## Rules the page keeps

- **Server text is text.** Every string from the server is set with `textContent`; the scripts contain
  no `innerHTML`, `eval` or style attributes (a test checks the sources). Citation addresses become a
  separate link only when they are `https://`; a workspace link from the rig must be a same-origin path.
- **No success the server did not confirm.** Every action is a request; its refusal is shown in the
  server's words. Install success is the rig's install record; an upload is "received" only when the
  rig reports the hub's receipt.
- **Stale answers are dropped.** Each draw has an epoch and an `AbortController`; an answer for a
  screen the reader has left is ignored and its request aborted. Polling stops on navigation.
- **Titles.** The tab title is the screen's heading, also when the heading arrives with the data.
- **Focus.** A navigation focuses the new screen's heading once it has loaded; an in-screen step
  (pager, filter, tab) keeps focus on the control that made it. Sign-in errors, refusals and progress
  are announced through `role=alert` / `role=status`.
- **States.** Every region shows loading, then content, a purposeful empty state, or an error with
  Try again. A 401 signs the page out and says so. An unreachable hub shows a banner with Retry; on a
  rig it adds that installed experiments and local data are unaffected.
- **The hub never runs anything.** Central pages say that experiments run on rigs and point to
  `alhazen dashboard --hub`. Running stays in the workspace; the hub page links to it and never adds
  controls to Run or the live monitor.

## Consent and trust (contract gates B2, M6)

- **Installing a release** shows that the code runs as the operator's own OS user with access to
  files, data, saved credentials and devices, and that a virtual environment is not a sandbox. It
  needs an explicit interpreter path and a tick that names the exact release and its SHA-256.
- **Uploading a session** first asks the rig for a preview, then shows the recipient hub and account,
  the release it is recorded against, subject/mode/rig/start, every file with sizes, the session
  manifest digest and what the files can reveal. The upload request names that preview's
  `preview_id` with `consent: true`; if the rig answers `preview_stale`, the page previews again and
  asks for consent again. A transfer paused with `auth_context_changed` says it continues only for
  the account it was approved for.
- **Packaging** shows every file that would be packed (each can be left out), the automatic
  exclusions, and the manifest; it needs a confirmation that the files hold no participant data,
  credentials or rig settings, and never publishes.
- **Publishing** needs a licence, two acknowledgements and a second confirmation; the page says that
  downloads cannot be recalled and that later versions stay private.

## Unfinished uploads

Below the session list, Data shows the signed-in account's uploads the hub has not committed
(`GET /api/hub/v1/sessions?limit=20`: caller-owned staging or sealing uploads), each with what it is,
bytes received of the total and when it last changed. They hold reserved quota until they finish,
expire or are discarded. **Discard…** appears only for a staging upload and asks first: the
confirmation says that only the hub's partial copy is removed, that the session's files on the rig stay
as they are and that received sessions are never removed. Confirming sends
`POST /api/hub/v1/sessions/{id}/abort`; the button shows it is busy, and afterwards the list is always
read again from the hub. A sealing upload's Discard is disabled with the reason. A 409 (it started
sealing or was committed meanwhile) or 410 (already expired or closed) is shown in the hub's words and
the list is refreshed; an offline or other failure keeps the row and lets you try again. The list is
fetched per screen for the current account, so after a sign-out or account change only the new
account's uploads appear. Nothing is discarded automatically. A rig's own transfer controls (Resume,
Cancel) stay on This rig → Upload data.

## Install durability

When the rig reports an install with `durable: false`, the install row and the install panel add that
the files are installed and verified but the computer could not confirm they were written through to
disk, with the rig's `durability_note` (folder sync is unavailable on Windows and some mounts). It says
this is about storage, not the code, and suggests checking the install again after a power loss.
Windows behaviour itself is unverified here.

## Rebuilding a trial index (contract gate B1)

Trial rows are derived from the raw files. When the server reports `session.index.status = "failed"`, the
session page shows the server's reason and a **Rebuild trial index** button. It sends
`POST /api/hub/v1/data/sessions/{id}/reindex` (owner only; the server answers 202 with the session detail):
while waiting the button is disabled; a refusal is shown in the server's words and the button comes back;
on acceptance the page shows the queued state and checks `GET /data/sessions/{id}` about once a second
(backing off to 8 s) until the index is `indexed` (rows and exports appear), `failed` again (reason and
the button again) or `none`. Rows and exports are never shown before the server says the index is ready.

## Look

Light first, with an equally composed dark palette (the workspace's dark theme), switched by
Auto/Light/Dark in the footer (remembered under the workspace's theme key). The visual language is
the workspace's "Instrument": cool paper, white panels, one signal blue for action and selection,
mono for labels, digests and readouts, status as a lamp plus a word. The hub's own signature is the
landing page's signal rail: experiment, rig and data as three stations, with one amber pulse running
along it (none under reduced motion). In the dark theme `hub.css` sets the documentation viewer's
`--hd-*` variables from the page's own tokens, so Methods, task guides and the Guide follow it. Controls are at least 40 px tall; below 640 px the navigation
scrolls sideways and every grid becomes one column.

## Tests

```
node --test tests/js/hub_core.test.mjs tests/js/hub_page.test.mjs
```

`hub_core.test.mjs` covers addresses, the client's credentials per role, error kinds (including the
rig adapter's codes), timeouts and offline, input checks and formatting. `hub_page.test.mjs` mounts
the page on `tests/js/fake_dom.mjs` with a fake hub and covers both roles' bootstrap, hostile text,
sign-in and registration, Back/Forward with stale answers, the experiment page and documentation
tabs, install trust, upload consent and transfer polling, trial index rebuild (refusal, retry, queued, rebuilding, ready, failed again), stale previews, publishing, session expiry,
offline banners, data filters and exports, hub connection checks and focus. Not covered here: real
rendering, layout at 390 px and desktop widths, real keyboard and touch behaviour, and the real
server and rig adapter.
