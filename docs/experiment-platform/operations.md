# Alhazen Experiment Platform: operations

How to install, run, refresh, back up and upgrade the platform. The design is in
[design.md](design.md). Every command below was run against this branch (see
"Verification" at the end for what was and was not exercised).

## Install

The platform is part of alhazen. Its local host and every catalogue command need nothing
beyond a normal install:

```
pip install alhazen-vision            # or: uv tool install alhazen-vision
```

The self-hosted server needs the `platform` extra (one pure-Python dependency, waitress):

```
pip install "alhazen-vision[platform]"
```

A rig keeps its usual install; it never needs the extra.

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
alhazen platform users add ada --name "Ada Lovelace" --config /etc/alhazen-platform.toml
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
- Accounts: `alhazen platform users add|list|passwd|disable|enable NAME --config FILE`
  (`--password-stdin` for scripts). Passwords have at least 12 characters and are stored as
  scrypt hashes. Changing a password or disabling a user ends their sessions. There is no
  self-registration.
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
