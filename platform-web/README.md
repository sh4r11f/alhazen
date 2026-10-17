# platform-web: the Experiment Platform web app

The canonical source of the Alhazen Experiment Platform's web app (design.md section 7). One
source, two outputs:

- **Standalone** (`alhazen platform local` / `serve`): `node platform-web/build.mjs` compiles
  `src/alhazen/experiment_platform/web/` (`index.html`, `platform.js`, `platform.css`,
  `assets.json`, `fonts/`, `THIRD_PARTY_LICENSES.txt`). React and the Manrope font are bundled;
  the page fetches nothing from another origin and needs no inline `<style>`.
- **Mana**: `node platform-web/build.mjs --mana out.jsx` writes the `user_div` code of the Mana
  app `Apps/alhazen-experiment-platform/hub`. Installing it into a live Mana app is a separate,
  approved step; this repository never writes to Mana.

## Layout

`src/` holds fragments concatenated in a fixed order into one scope (a Mana `user_div` is one
module with no imports, so the fragments share names instead of importing them):

| File | What it holds |
|---|---|
| `00_guide_css.js` | `GUIDE_CSS`, the HubDocs reader stylesheet |
| `10_constants.js` | palette (`LIGHT`, `DARK`), categories, hardware, status labels |
| `20_styles.js` | `CSS`, the platform stylesheet |
| `30_hub_docs.js` | `HubDocs`, the vanilla reader (schematics, timelines, Markdown subset) |
| `40_reader.jsx` | the experiment page and task/parameter readers (`EHExperimentPage`, ...) |
| `50_logos.jsx` | experiment emblems and glyphs |
| `60_helpers.js` | formatting, routing (`?ehv=&ehid=&ehtab=&ehtask=&ehset=`), Markdown tokens |
| `70_app.jsx` | `PlatformApp({host})`: Home, Browse, experiment pages, My experiments, copies, dialogs |
| `host_mana.jsx` | `UserDiv()`: the Mana host (Firebase paths, Mana chats, prompts, Mana Terminal) |
| `host_http.jsx` | the standalone host over the JSON API (design.md Appendix A), sign-in card, theme |
| `standalone_css.js` | `STANDALONE_CSS`, the standalone-only rules (font face, sign-in, run steps) |
| `entry_standalone.jsx` | imports and mount for the standalone bundle |

Shared fragments may use only the names both targets provide: `React`, `ReactMarkdown`,
`remarkGfm` and `copyText` (Mana injects them; the standalone entry imports the first three and
`host_http.jsx` defines `copyText`). Everything else that differs between hosts goes through
`host`.

## The host object

`PlatformApp` uses only `host`. Members beyond the list in design.md section 7:

| Member | Mana | Standalone |
|---|---|---|
| `key` | stable per app/viewer | new per host instance (subscriptions re-attach) |
| `capabilities.themeToggle`, `capabilities.signIn` | false, false | true, from `/api/session` |
| `downloadArchive(handle, filename)` | (unused) | fetches the zip with the auth header, saves it |
| `links {terminalListing, terminalApp}`, `openTerminalApp()` | Mana Terminal links | empty / no-op |
| `fontStylesheet`, `inlineStyles`, `extraCss` | Google Fonts link, inline `<style>` | none, `platform.css` |
| `signOut()`, `refresh()` | — | server sign-out; re-run every subscription |
| `subscribeForkDocs` callback | `{docs}` or `{stale: true}`; the Mana host backfills old copies | same values from `GET /api/forks/<id>/docs` |

Capabilities gate the interface explicitly: without `changeWithMana`, `newWithMana`,
`runOnMachines` and `machines` there is no Change with Mana, Fork with AI, New experiment or My
machines; Changes offers *Download this revision (.zip)* and *Upload a changed copy (.zip)*,
and Run shows the commands for a rig (`uv sync --locked --extra psychopy`, then
`uv run alhazen dashboard --project .`).

## Building and checking

Dependencies are pinned in `package-lock.json` and installed outside the repository:

```bash
mkdir -p /tmp/apw-node && cp platform-web/package.json platform-web/package-lock.json /tmp/apw-node/
npm ci --prefix /tmp/apw-node --cache /tmp/npm-cache
export ALHAZEN_PLATFORM_NODE_MODULES=/tmp/apw-node/node_modules
node platform-web/build.mjs                      # write src/alhazen/experiment_platform/web/
node platform-web/build.mjs --check              # exit 1 when the committed files differ
node platform-web/build.mjs --mana /tmp/hub.jsx  # the Mana user_div
node --test 'platform-web/test/*.test.mjs'       # the HTTP host against a fake fetch
```

`tests/unit/test_platform_web.py` checks the packaged files without Node (no foreign origins,
the vendored font, no Mana adapter in the standalone bundle) and runs `--check` when Node and
the dependencies are present. Change a fragment, rebuild and commit the web files in the same
commit.
