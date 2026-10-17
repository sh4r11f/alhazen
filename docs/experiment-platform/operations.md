# Alhazen Experiment Platform: operations

How to install, run, refresh, back up and upgrade the platform. The design is in
[design.md](design.md). The platform ships with alhazen from release 3.1.0, as the
`alhazen platform` command and the optional `platform` extra. "Verification" at the end
lists what was run and where.

## Install

alhazen is not published on PyPI yet (docs/versioning.md says why), so `pip install
alhazen-vision` from the package index does not give you this. Install from a release tag
of the repository instead.

### From a release tag

```
uv tool install "alhazen-vision @ git+https://github.com/sh4r11f/alhazen@v3.1.0"
uv tool install "alhazen-vision[platform] @ git+https://github.com/sh4r11f/alhazen@v3.1.0"
```

The first is the core install: the local host and every catalogue command. The second
adds the `platform` extra (one pure-Python dependency, waitress) for the self-hosted
server; install one or the other. `pip install "alhazen-vision[platform] @
git+https://github.com/sh4r11f/alhazen@v3.1.0"` into a virtual environment works the same
way. A rig keeps its usual install (an experiment pins alhazen by tag) and never needs the
extra.

### From a checkout

From a checkout of the repository (any branch), build a wheel with
[uv](https://docs.astral.sh/uv/) and install that exact file into a fresh environment
outside the checkout:

```
uv build --wheel -o dist/                              # in the checkout
uv venv ~/alhazen-platform-env --python 3.11           # anywhere outside it
VIRTUAL_ENV=~/alhazen-platform-env uv pip install dist/alhazen_vision-*.whl
VIRTUAL_ENV=~/alhazen-platform-env uv pip install "$(ls dist/alhazen_vision-*.whl)[platform]"
```

Then run `~/alhazen-platform-env/bin/alhazen platform ...`. The web app is built and
committed under `src/alhazen/experiment_platform/web/`, so installing needs no Node;
rebuilding it from `platform-web/src` does (see `platform-web/README.md`).

## What the platform does and does not do

- The Mana app is built from this repository's web source (`node platform-web/build.mjs
  --mana`) and installed into a Mana workspace as a separate, reviewed step. Mana keeps its
  own storage, identity, chats and Mana Terminal, and its own catalogue refresh script.
- AI generation and machine execution are Mana-host features: "Change with Mana", "Fork
  with AI", "New experiment with Mana", My machines and running through Mana Terminal.
  The standalone local and server hosts do not offer them and say so in the page.
- On a standalone host, changing a copy is manual (download and upload, or `checkout` /
  `save`), and running one is manual on the rig (the Run tab prints the commands).
- So the standalone hosts are not functionally equivalent to the Mana app: they share its
  catalogue, reader, copies, versions and publishing, not its agent or machine features.

## Local: just me, on this machine

```
alhazen platform import ~/src/my-experiment              # add an experiment (its HEAD)
alhazen platform import ~/src/alhazen-experiments --all-folders --ref main
alhazen platform local                                   # opens the platform in a browser
```

- State lives in `~/.alhazen/platform` (`--store` to choose another folder). It is created
  on first use, readable by you only.
- The server binds `127.0.0.1` on a free port and prints a link with a one-time token in
  its fragment. Nothing is reachable from the network, and nothing needs the network: the
  web app, React and the font ship inside alhazen.
- Your copies: fork from an experiment's page. To change one, download the revision
  (Changes tab) or `alhazen platform checkout <copy-id> <folder>`, edit, then upload the
  folder as a zip in the Changes tab or `alhazen platform save <copy-id> <folder> -m "what changed"`.
- To run a copy on a rig: unpack it there and run `uv sync --locked --extra psychopy`, then
  `uv run alhazen dashboard --project .` (the Run tab shows these commands).

## Self-hosted: a lab server

```
alhazen platform server-init --store /srv/alhazen-platform --config /etc/alhazen-platform.toml \
    --public-url https://lab.example.org/platform --host 127.0.0.1 --port 8770
alhazen platform users add ada --name "Ada Lovelace" --config /etc/alhazen-platform.toml  # prompts for the password
alhazen platform serve --config /etc/alhazen-platform.toml
```

- `server-init` creates the store (catalogue, copies, `accounts.sqlite3`) and a
  configuration file; edit `public_catalogue`, `session_days`, `max_upload_mb`, `threads`
  and `trusted_proxy` there.
- The platform does not terminate TLS. Put it behind a reverse proxy that does, forward
  the path of `public_url` to `host:port`, and set `tls_terminated_by_proxy = true` (and
  `trusted_proxy` to the proxy's address so sign-in throttling sees the client address).
  The server refuses to start on a non-loopback address without that setting, and refuses
  an `http://` public URL that is not loopback.
- With `trusted_proxy` set, the client address used for sign-in throttling is the rightmost
  `X-Forwarded-For` entry, taken only from connections that come from that proxy address
  (waitress drops forwarding headers from every other peer). The proxy must append the
  client address (nginx: `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`).
  Without it, every client behind the proxy shares one throttling bucket.
- `public_catalogue = true` lets visitors browse the catalogue, read methods and download
  published files without an account; a Sign in button opens the sign-in card. Copies,
  uploads, publishing and My experiments still need an account. The default (`false`)
  shows only the sign-in card.
- Accounts: `alhazen platform users add|list|passwd|disable|enable NAME --config FILE`
  (`--password-stdin` for scripts). Passwords have at least 12 characters and are stored as
  scrypt hashes. Changing a password or disabling a user ends their sessions, including a
  sign-in that was being checked at that moment (each session is bound to the account's
  credential generation). There is no self-registration.
- Catalogue: the operator refreshes it on the server with
  `alhazen platform refresh --store /srv/alhazen-platform --config sources.toml --publisher <uid> --publisher-name "Lab"`
  (`alhazen platform users list` shows uids). Users publish their own copies from the web
  app under their own accounts.

## Refresh and the release gate

```
alhazen platform refresh --store DIR --config sources.toml [--alhazen-repo PATH]
alhazen platform check --store DIR --config sources.toml
```

`refresh` reads each experiment from git at the named ref (never a work tree, never
executing it), and writes a new immutable version only when its files changed. `check`
exits 1 when any listing's documentation is stale or missing, or a named source has moved
past its listing; run it in an experiment collection's CI or before a release.
`sources.toml` is described in `refresh.py`'s module documentation. `--alhazen-repo`
(a clone of alhazen with its tags) lets the parameter guide list defaults inherited from
the pinned alhazen; without it, the installed alhazen is used when its version is the pin,
and otherwise the guide says those defaults are not listed.

## Backups, restores and upgrades

- `alhazen platform backup --store DIR DEST` copies the whole store under its lock, and
  the accounts database with SQLite's online backup, to a new folder. Restore by stopping
  the server and pointing `store` at the copy.
- Versions, bundles and documentation snapshots are immutable files; listings and copies
  are replaced atomically. A crash leaves the previous or the new record, never half of one.
- The store carries a format number (`platform-store.json`). An alhazen that does not know
  a store's format refuses to open it rather than guessing. Format 1 is the first.
- Upgrading alhazen upgrades the web app and the documentation renderer (the renderer
  axis); stored documents do not change. Refreshing rebuilds documentation only for
  sources that changed.

## Mana

The Mana app is built from the same web source (`node platform-web/build.mjs --mana
<file>`) and keeps its own storage, identity, chats and Mana Terminal. Installing a new
build into a Mana workspace is a separate, reviewed step; see design.md §8.

## Limitations

- One store per server process; one writer at a time (the store lock). Sized for a lab:
  hundreds of experiments and users, not thousands of concurrent writers.
- No email, password reset by mail, or single sign-on: an operator resets passwords.
- Copies on a standalone host are changed by download/upload or the command line; "Change
  with Mana", "Fork with AI" and running on paired machines need the Mana integration.
- Removing a copy keeps its stored revisions (a published listing may still name them);
  there is no garbage collection yet.

## Verification

Run on Linux (Python 3.11) in a sandbox, from a wheel built from the source installed
outside the checkout, unless noted.

- Commands run from the installed wheel: `import` (a repository; `--folder`,
  `--all-folders`, `--ref`, `--docs-ref`, `--overrides`, `--alhazen-repo`), `refresh`
  (paths, and `--config` with a sources file), `check` (`--store`, `--config`), `local`
  (`--no-browser`, `--port`, `--name`), `server-init` (`--public-url`, `--host`, `--port`),
  `users add --password-stdin`, `users list`, `serve`, `backup`. `checkout`, `save`,
  `users disable` and `users passwd` were run in-process by the test suite. Not run: `local`
  opening a browser by itself, and `users add` prompting interactively.
- Browser workflows: the local host (browse, methods reader, fork, download and upload a
  revision, publish) with no request outside the loopback origin; the server with two
  accounts (sign-in, wrong password, sign-out, no access to the other user's copy by page
  or by API).
- Offline: the local host imported, served and forked with every non-loopback connection
  blocked inside the interpreter. A network namespace was not available.
- Tests: the full suite passed locally on Linux with coverage over the 89% floor (91%),
  including real-socket runs of the local host and of `serve` under waitress.
- CI: the platform tests run in the existing GitHub Actions matrix (Linux, macOS,
  Windows); the results for the change that added the platform are on its pull request.
  The existing Node job runs only `tests/js`. There is no CI job for `platform-web` (its
  Node tests and `build.mjs --check` run locally, and the Python test that compares the
  build skips when Node is absent, as it is in the Python matrix).
- Not covered: a real reverse proxy with TLS, and a container image (none is provided).
