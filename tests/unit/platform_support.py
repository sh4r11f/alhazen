"""Helpers for the Experiment Platform tests: neutral experiments, stores with versions,
zip archives and WSGI calls without a network."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any
from wsgiref.util import setup_testing_defaults

from alhazen.experiment_platform import bundles
from alhazen.experiment_platform.store import FileStore

EXPERIMENT = {
    "pyproject.toml": b'[project]\nname = "dots"\nversion = "1.2.0"\n',
    "run.py": b'PARAMETERS = {\n    "main": "configs/task.yaml",\n}\n',
    "configs/task.yaml": b"n_trials: 40\ncoherence: 0.25\n",
    "README.md": b"# Dots\n\nA neutral test experiment: random dots, one choice.\n",
}


def experiment(**changes: bytes) -> dict[str, bytes]:
    files = dict(EXPERIMENT)
    files.update(changes)
    return files


def add_version(
    store: FileStore,
    pub: str,
    exp: str,
    files: dict[str, bytes],
    *,
    title: str = "Dots",
    docs: dict[str, Any] | None = None,
    version: str = "1.2.0",
    commit: str = "a" * 40,
    at: int = 1_000,
) -> str:
    """Store a published version of ``files`` and point the listing at it (what a refresh
    does, without reading git). Returns the bundle id."""
    entries, excluded = bundles.split_blobs(sorted((p, b, 0) for p, b in files.items()))
    built = bundles.build_bundle(entries, excluded, exp)
    store.put_bundle(None, built)
    listing = {
        "id": exp,
        "title": title,
        "summary": "Random dots.",
        "art": "dots",
        "category": "motion",
        "tags": ["motion"],
        "subjects": ["human"],
        "hardware": {},
        "publisher": {"uid": pub, "name": "Lab"},
        "source": {"kind": "repo", "repo": "lab/dots", "ref": "main", "commit": commit},
        "version": version,
        "alhazen": "v2.16.0",
        "parameterSets": ["main"],
        "bundle": {
            "id": built.id,
            "fileCount": built.meta["fileCount"],
            "bytes": built.meta["bytes"],
            "binaries": 0,
            "excluded": 0,
        },
        "publishedAt": at,
        "updatedAt": at,
    }
    record_docs = (
        docs
        if docs is not None
        else {
            "methods": "## Question\n\nWhich way do the dots move?",
            "readme": files["README.md"].decode(),
            "tasks": "[]",
            "references": "[]",
            "guide": json.dumps({"schema_version": 1, "bundleId": built.id, "tasks": []}),
            "parameterGuide": json.dumps({"sourceCommit": commit, "sets": []}),
        }
    )
    store.put_version(
        pub,
        exp,
        built.id,
        {
            "key": built.id,
            "bundleId": built.id,
            "listing": listing,
            "docs": record_docs,
            "builtAt": at,
        },
    )
    current = store.listing(pub, exp) or {}
    listing["versions"] = {
        **(current.get("versions") or {}),
        built.id: {"version": version, "commit": commit[:7], "at": at},
    }
    listing["publishedAt"] = current.get("publishedAt", at)
    store.put_listing(pub, exp, listing)
    return built.id


def zip_of(files: dict[str, bytes], top: str = "dots") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for path, data in files.items():
            archive.writestr(f"{top}/{path}" if top else path, data)
    return out.getvalue()


class Response:
    def __init__(self, status: str, headers: list[tuple[str, str]], body: bytes) -> None:
        self.status = int(status.split()[0])
        self.headers = {k.lower(): v for k, v in headers}
        self.all_headers = headers
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body)


def call(
    app: Any,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    query: str = "",
    host: str = "127.0.0.1:8000",
) -> Response:
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "wsgi.input": io.BytesIO(body),
        "CONTENT_LENGTH": str(len(body)),
        "HTTP_HOST": host,
    }
    for key, value in (headers or {}).items():
        if key.lower() == "content-type":
            environ["CONTENT_TYPE"] = value
        else:
            environ["HTTP_" + key.upper().replace("-", "_")] = value
    setup_testing_defaults(environ)
    captured: dict[str, Any] = {}

    def start_response(
        status: str, response_headers: list[tuple[str, str]], exc_info: Any = None
    ) -> None:
        captured["status"], captured["headers"] = status, response_headers

    body_out = b"".join(app(environ, start_response))
    return Response(captured["status"], captured["headers"], body_out)


def web_dir(tmp: Path) -> Path:
    folder = tmp / "web"
    (folder / "fonts").mkdir(parents=True)
    (folder / "index.html").write_text("<!doctype html><title>Alhazen</title>", encoding="utf-8")
    (folder / "platform.js").write_text("console.log(1)", encoding="utf-8")
    (folder / "assets.json").write_text(
        json.dumps({"hero": "<svg/>", "penrose": "<svg/>"}), encoding="utf-8"
    )
    return folder
