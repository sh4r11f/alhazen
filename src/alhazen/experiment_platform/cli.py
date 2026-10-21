"""``alhazen platform ...``: the Experiment Platform from the command line.

    alhazen platform local [--store DIR] [--port N] [--no-browser]
    alhazen platform import REPO [--ref REF] [--folder F ...] [--all-folders] [--store DIR]
    alhazen platform refresh (--config SOURCES.toml | REPO ...) --store DIR [--publisher UID]
    alhazen platform check --store DIR [--config SOURCES.toml]
    alhazen platform access PUBLISHER EXP [--visibility private|people|public]
                            [--people UID,...] (--store DIR | --config FILE)
    alhazen platform checkout FORK DEST [--store DIR]
    alhazen platform save FORK SRC -m MESSAGE [--store DIR]
    alhazen platform server-init --store DIR --config FILE --public-url URL
    alhazen platform serve --config FILE
    alhazen platform users (add|list|passwd|disable|enable) --config FILE ...
    alhazen platform backup --store DIR DEST

The local commands work on the local store (default ``~/.alhazen/platform``) as its single
user. The parser is stdlib-only, so adding it to ``alhazen`` costs nothing.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_STORE_TEXT = "~/.alhazen/platform"


def default_store() -> Path:
    """The store used when --store is not given, looked up when a command needs it: every
    alhazen command builds this parser, and one environment without a home directory
    (a stripped-down subprocess environment on Windows) must not break the others."""
    return Path.home() / ".alhazen" / "platform"


def add_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg,unused-ignore]
    platform = sub.add_parser(
        "platform", help="the Experiment Platform: browse, fork, share experiments"
    )
    commands = platform.add_subparsers(dest="platform_command")

    def store_arg(p: argparse.ArgumentParser, required: bool = False) -> None:
        p.add_argument(
            "--store",
            type=Path,
            default=None,
            required=required,
            help=f"the platform store folder (default: {DEFAULT_STORE_TEXT})"
            if not required
            else "the platform store folder",
        )

    local = commands.add_parser(
        "local", help="open the platform for yourself on this machine (no account)"
    )
    store_arg(local)
    local.add_argument("--port", type=int, default=0, help="loopback port (default: automatic)")
    local.add_argument("--no-browser", action="store_true", help="print the URL without opening it")
    local.add_argument(
        "--name", default=None, help="the name your shared versions carry (default: git user.name)"
    )

    for name, help_text in (
        ("import", "add an experiment from a git repository to your local platform"),
        ("refresh", "rebuild catalogue versions from experiment source at exact commits"),
    ):
        p = commands.add_parser(name, help=help_text)
        store_arg(p, required=name == "refresh")
        p.add_argument(
            "repo", nargs="*", type=Path, help="git repository of the experiment (or collection)"
        )
        p.add_argument("--config", type=Path, default=None, help="a TOML file naming the sources")
        p.add_argument("--ref", default="HEAD", help="the ref to read (default: HEAD)")
        p.add_argument(
            "--folder", action="append", default=[], help="an experiment folder of a collection"
        )
        p.add_argument(
            "--all-folders", action="store_true", help="every classics/ and contributed/ folder"
        )
        p.add_argument(
            "--docs-ref", default=None, help="read the documentation from this ref instead"
        )
        p.add_argument(
            "--id", dest="exp_id", default=None, help="the experiment id (default: folder name)"
        )
        p.add_argument(
            "--overrides", default=None, help="JSON of listing fields the repository does not set"
        )
        p.add_argument("--publisher", default="local", help="publisher id (default: local)")
        p.add_argument("--publisher-name", default=None, help="publisher display name")
        p.add_argument(
            "--alhazen-repo",
            type=Path,
            default=None,
            help="a git clone of alhazen, to list defaults inherited from the pinned alhazen",
        )
        _access_args(
            p,
            "who can see a listing this creates (default: public); "
            "an existing listing keeps its own",
        )

    access = commands.add_parser(
        "access", help="show or change who can see a listing (private, people, public)"
    )
    access.add_argument("publisher", help="the publisher's account id")
    access.add_argument("exp", help="the experiment id")
    store_arg(access)
    access.add_argument(
        "--config", type=Path, default=None, help="a server configuration (its store and accounts)"
    )
    _access_args(access, "the new visibility (without it, the current one is shown)")

    check = commands.add_parser(
        "check", help="fail when documentation is stale or a source moved on"
    )
    store_arg(check, required=True)
    check.add_argument("--config", type=Path, default=None, help="a TOML file naming the sources")
    check.add_argument("--publisher", default=None)

    checkout = commands.add_parser("checkout", help="write a copy's current revision to a folder")
    store_arg(checkout)
    checkout.add_argument("fork", help="the copy's id (see My experiments)")
    checkout.add_argument("dest", type=Path, help="an empty or new folder")

    save = commands.add_parser("save", help="save a folder as a new revision of a copy")
    store_arg(save)
    save.add_argument("fork")
    save.add_argument("src", type=Path)
    save.add_argument("-m", "--message", required=True)

    init = commands.add_parser(
        "server-init", help="create a server store and its configuration file"
    )
    store_arg(init, required=True)
    init.add_argument("--config", type=Path, required=True, help="where to write the configuration")
    init.add_argument("--public-url", required=True, help="the URL people will open")
    init.add_argument("--host", default="127.0.0.1")
    init.add_argument("--port", type=int, default=8770)

    serve = commands.add_parser("serve", help="run the self-hosted multi-user server")
    serve.add_argument("--config", type=Path, required=True)

    users = commands.add_parser("users", help="manage the server's accounts")
    users.add_argument("action", choices=["add", "list", "passwd", "disable", "enable"])
    users.add_argument("username", nargs="?")
    users.add_argument("--config", type=Path, required=True)
    users.add_argument("--name", default=None, help="display name (add)")
    users.add_argument(
        "--password-stdin", action="store_true", help="read the password from standard input"
    )

    backup = commands.add_parser("backup", help="copy a store (and its accounts) to a new folder")
    store_arg(backup, required=True)
    backup.add_argument("dest", type=Path)


def _access_args(p: argparse.ArgumentParser, help_text: str) -> None:
    p.add_argument(
        "--visibility", choices=["private", "people", "public"], default=None, help=help_text
    )
    p.add_argument(
        "--people", default=None, help="account ids, comma-separated (with --visibility people)"
    )


def _access_value(args: argparse.Namespace) -> dict[str, Any] | None:
    """The access the flags ask for, or None when they ask for none."""
    from alhazen.experiment_platform.errors import InvalidRequest

    if args.people is not None and args.visibility != "people":
        raise InvalidRequest("--people goes with --visibility people")
    if args.visibility is None:
        return None
    value: dict[str, Any] = {"visibility": args.visibility}
    if args.visibility == "people":
        if not args.people:
            raise InvalidRequest("--visibility people needs --people uid,...")
        value["people"] = [p.strip() for p in args.people.split(",") if p.strip()]
    return value


def _directory(store: Path) -> Any:
    """The accounts of a server store (people to share with), or None for a local store."""
    from alhazen.experiment_platform.accounts import Accounts
    from alhazen.experiment_platform.server import ACCOUNTS_FILE

    path = Path(store) / ACCOUNTS_FILE
    return Accounts(path) if path.is_file() else None


def _password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password (at least 12 characters): ")
    if getpass.getpass("Again: ") != first:
        raise SystemExit("The passwords differ")
    return first


def run(args: argparse.Namespace) -> int:
    from alhazen.experiment_platform.errors import PlatformError

    try:
        return _run(args)
    except PlatformError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _specs(args: argparse.Namespace) -> list[Any]:
    from alhazen.experiment_platform import refresh, sources
    from alhazen.experiment_platform.errors import InvalidRequest

    specs = refresh.load_config(args.config) if args.config else []
    overrides = json.loads(args.overrides) if getattr(args, "overrides", None) else {}
    for repo in getattr(args, "repo", []) or []:
        folders = list(args.folder)
        if args.all_folders:
            folders += sources.collection_folders(repo, args.ref)
        for folder in folders or [""]:
            specs.append(
                refresh.SourceSpec(
                    repo=repo.resolve(),
                    ref=args.ref,
                    subdir=folder,
                    exp_id=args.exp_id,
                    docs_ref=args.docs_ref,
                    overrides=overrides,
                )
            )
    if not specs and args.platform_command in ("import", "refresh"):
        raise InvalidRequest("Name a repository or --config")
    return specs


def _run(args: argparse.Namespace) -> int:  # noqa: C901 - one branch per subcommand
    from alhazen.experiment_platform.store import FileStore

    command = args.platform_command
    if getattr(args, "store", False) is None:
        args.store = default_store()
    if command is None:
        print(
            "usage: alhazen platform {local,import,refresh,check,access,checkout,save,"
            "server-init,serve,users,backup}"
        )
        return 2
    if command == "local":
        from alhazen.experiment_platform.local import run_local

        return run_local(
            args.store, port=args.port, open_browser=not args.no_browser, name=args.name
        )
    if command in ("import", "refresh"):
        from alhazen.experiment_platform import refresh
        from alhazen.experiment_platform.local import LOCAL_UID, default_name
        from alhazen.experiment_platform.service import PlatformService

        specs = _specs(args)
        wanted = _access_value(args)
        store = FileStore(args.store, create=command == "import" or not args.store.exists())
        name = args.publisher_name or (
            default_name() if args.publisher == LOCAL_UID else args.publisher
        )
        if wanted is not None:  # checked before anything is built: people must exist
            wanted = PlatformService(store, people=_directory(store.root)).check_access(
                wanted, args.publisher
            )
        results = refresh.refresh(
            store,
            specs,
            {"uid": args.publisher, "name": name},
            alhazen_repo=args.alhazen_repo,
            access=wanted,
        )
        for result in results:
            print(result.line())
            for warning in result.warnings:
                print(f"    warning: {warning}")
        return 0
    if command == "check":
        from alhazen.experiment_platform import refresh

        problems = refresh.check(
            FileStore(args.store),
            refresh.load_config(args.config) if args.config else [],
            publisher=args.publisher,
        )
        for problem in problems:
            print(f"{problem.where}: {problem.problem}")
        print("catalogue OK" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    if command == "access":
        from alhazen.experiment_platform import access as access_rules
        from alhazen.experiment_platform.service import PlatformService, Principal

        if args.config is not None:
            from alhazen.experiment_platform.server import load_config

            args.store = load_config(args.config).store
        store = FileStore(args.store)
        service = PlatformService(store, people=_directory(store.root))
        # The operator acts as the publisher: the command changes nothing but this field.
        publisher = Principal(args.publisher, args.publisher)
        wanted = _access_value(args)
        if wanted is None:
            from alhazen.experiment_platform.errors import NotFound

            listing = store.listing(args.publisher, args.exp)
            if listing is None:
                raise NotFound("This experiment is not in the catalogue")
            shown = access_rules.of(listing)
        else:
            shown = service.set_access(publisher, args.publisher, args.exp, wanted)["access"]
        people = ", ".join(
            f"{uid} ({shown.get('names', {}).get(uid, '?')})" if shown.get("names") else uid
            for uid in shown.get("people", [])
        )
        print(
            f"{args.publisher}/{args.exp}: {shown['visibility']}"
            + (f": {people}" if people else "")
        )
        return 0
    if command in ("checkout", "save"):
        from alhazen.experiment_platform import bundles
        from alhazen.experiment_platform.local import LOCAL_UID, default_name
        from alhazen.experiment_platform.service import PlatformService, Principal

        service = PlatformService(FileStore(args.store))
        me = Principal(LOCAL_UID, default_name())
        if command == "checkout":
            if args.dest.exists() and any(args.dest.iterdir()):
                print("error: the destination folder is not empty", file=sys.stderr)
                return 1
            fork, entries = service.checkout(me, args.fork)
            bundles.materialize(entries, args.dest)
            print(f"{fork.get('title')} ({fork['head']['id']}) written to {args.dest}")
            return 0
        blobs = bundles.blobs_from_dir(args.src)
        entries, _ = bundles.split_blobs(blobs)
        archive = bundles.zip_entries(entries, args.fork)
        revision, _ = service.upload_revision(me, args.fork, archive, args.message)
        print(f"Saved revision {revision['bundle']}: {revision['message']}")
        return 0
    if command == "server-init":
        from alhazen.experiment_platform.accounts import Accounts
        from alhazen.experiment_platform.server import ACCOUNTS_FILE, ServerConfig, validate

        config = validate(
            ServerConfig(
                store=args.store.resolve(),
                host=args.host,
                port=args.port,
                public_url=args.public_url,
                tls_terminated_by_proxy=args.public_url.startswith("https://"),
            )
        )
        if args.config.exists():
            print("error: the configuration file exists; choose another name", file=sys.stderr)
            return 1
        FileStore(config.store, create=True)
        Accounts(config.store / ACCOUNTS_FILE)
        args.config.write_text(
            "[server]\n"
            f"store = {json.dumps(str(config.store))}\n"
            f"host = {json.dumps(config.host)}\n"
            f"port = {config.port}\n"
            f"public_url = {json.dumps(config.public_url)}\n"
            f"tls_terminated_by_proxy = {'true' if config.tls_terminated_by_proxy else 'false'}\n"
            "public_catalogue = false\n"
            "session_days = 14\n"
            "max_upload_mb = 64\n"
            "threads = 8\n",
            encoding="utf-8",
        )
        print(
            f"Created the store {config.store} and {args.config}. Add a user: "
            f"alhazen platform users add <name> --config {args.config}"
        )
        return 0
    if command == "serve":
        from alhazen.experiment_platform.server import load_config, serve

        serve(load_config(args.config))
        return 0
    if command == "users":
        from alhazen.experiment_platform.accounts import Accounts
        from alhazen.experiment_platform.server import ACCOUNTS_FILE, load_config

        config = load_config(args.config)
        accounts = Accounts(config.store / ACCOUNTS_FILE)
        if args.action == "list":
            for user in accounts.users():
                print(
                    f"{user['username']:<24} {user['uid']:<14} {user['name']:<30} "
                    f"{'disabled' if user['disabled'] else ''}"
                )
            return 0
        if not args.username:
            print("error: name the user", file=sys.stderr)
            return 2
        if args.action == "add":
            principal = accounts.add_user(
                args.username, args.name or args.username, _password(args)
            )
            print(f"Added {args.username} ({principal.uid})")
        elif args.action == "passwd":
            accounts.set_password(args.username, _password(args))
            print(f"Changed the password of {args.username}; their sessions were ended")
        else:
            accounts.set_disabled(args.username, args.action == "disable")
            print(f"{args.username} {'disabled' if args.action == 'disable' else 'enabled'}")
        return 0
    if command == "backup":
        from alhazen.experiment_platform.accounts import Accounts

        store = FileStore(args.store)
        dest = store.backup(args.dest)
        accounts_file = args.store / "accounts.sqlite3"
        if accounts_file.exists():
            Accounts(accounts_file).backup(dest / "accounts.sqlite3")
        print(f"Backed up to {dest}")
        return 0
    return 2
