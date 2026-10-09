"""Operating the service: configuration, schema checks, readiness, the static
interface, database outages and the documentation seam."""

from __future__ import annotations

import sys
import types
import zipfile
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, text
from tests.hub.server_support import API, ORIGIN, Hub, make_bundle, make_settings

from alhazen.hub import admin
from alhazen.hub import app as hub_app
from alhazen.hub.app import create_app
from alhazen.hub.schema import SCHEMA_VERSION, SchemaError
from alhazen.hub.settings import HubSettings, SettingsError, load_settings


class TestSettings:
    def write(self, tmp_path, body):
        path = tmp_path / "hub.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_load_with_url_from_environment_and_relative_paths(self, tmp_path):
        path = self.write(
            tmp_path,
            """
[server]
public_origin = "https://hub.example.org"
[database]
url_env = "HUB_DB"
[storage]
artifact_root = "archive"
[limits]
user_quota_bytes = 1000
[auth]
failures_per_account = 3
""",
        )
        settings = load_settings(path, {"HUB_DB": "postgresql://hub:s3cret@db.example.org/hub"})
        assert settings.artifact_root == (tmp_path / "archive").resolve()
        assert settings.limits.user_quota_bytes == 1000 and settings.auth.failures_per_account == 3
        assert settings.secure_cookies
        assert "s3cret" not in repr(settings) and "s3cret" not in settings.redacted_database_url()

    @pytest.mark.parametrize(
        "body, message",
        [
            (
                "[server]\npublic_origin='http://hub.example.org'\n[database]\nurl='sqlite:///x.db'\n"
                "[storage]\nartifact_root='a'\n",
                "https",
            ),
            (
                "[server]\npublic_origin='https://h.org'\n[database]\nurl='mysql://x'\n"
                "[storage]\nartifact_root='a'\n",
                "not supported",
            ),
            (
                "[server]\npublic_origin='https://h.org'\n[database]\nurl='sqlite:///x.db'\n"
                "[storage]\nartifact_root='a'\n[limits]\nmax_chunk_bytes=99999999\n",
                "max_chunk_bytes",
            ),
            (
                "[server]\npublic_origin='https://h.org'\n[database]\nurl='sqlite:///x.db'\n"
                "[storage]\nartifact_root='a'\n[limits]\nquota=1\n",
                "unknown",
            ),
            (
                "[server]\npublic_origin='https://h.org'\n[database]\nurl_env='MISSING'\n"
                "[storage]\nartifact_root='a'\n",
                "MISSING",
            ),
        ],
    )
    def test_refusals(self, tmp_path, body, message):
        with pytest.raises(SettingsError, match=message):
            load_settings(self.write(tmp_path, body), {})


class TestSchema:
    def test_the_service_never_creates_a_schema(self, tmp_path):
        settings = make_settings(tmp_path)
        with pytest.raises(SchemaError, match="init_database"):
            create_app(settings)
        assert admin.init_database(settings) == SCHEMA_VERSION
        assert admin.init_database(settings) == SCHEMA_VERSION  # idempotent at the version
        assert admin.migrate_database(settings) == SCHEMA_VERSION
        create_app(settings, start_maintenance=False).state.hub.db.dispose()

    def test_a_newer_schema_is_refused_untouched(self, tmp_path):
        settings = make_settings(tmp_path)
        admin.init_database(settings)
        engine = create_engine(
            settings.database_url.replace("postgresql://", "postgresql+psycopg://")
        )
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE hub_schema SET value = :v WHERE key = 'schema_version'"),
                {"v": str(SCHEMA_VERSION + 1)},
            )
        engine.dispose()
        with pytest.raises(SchemaError, match="newer"):
            create_app(settings)
        with pytest.raises(admin.AdminError):
            admin.init_database(settings)


class TestReadiness:
    def test_ready_after_reconciliation(self, hub):
        assert hub.client.get(f"{API}/readyz").status_code == 503
        hub.maintenance.run_once(force_reconcile=True)
        body = hub.client.get(f"{API}/readyz").json()
        assert body["status"] == "ready" and body["database"] == "ok"

    def test_database_outage_is_a_typed_503(self, hub, tmp_path):
        # Any unreachable database: here an engine whose file cannot be opened.
        unreachable = tmp_path / "gone"
        unreachable.mkdir()
        database = hub.app.state.hub.db
        database.engine.dispose()
        database.engine = create_engine(f"sqlite:///{unreachable.as_posix()}")
        response = hub.client.get(f"{API}/catalog")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "database_unavailable"
        assert response.headers["retry-after"]
        assert str(tmp_path) not in response.text
        assert hub.client.get(f"{API}/readyz").status_code == 503


class TestInterface:
    def test_missing_assets_are_a_plain_development_state(self, hub, tmp_path, monkeypatch):
        monkeypatch.setattr(hub_app, "ASSET_DIR", tmp_path / "no-assets")
        response = hub.client.get("/")
        assert response.status_code == 503 and "development state" in response.text

    def test_page_and_fixed_asset_table(self, hub, tmp_path, monkeypatch):
        assets = tmp_path / "assets"
        (assets / "fonts").mkdir(parents=True)
        (assets / "index.html").write_text("<!doctype html><title>hub</title>", encoding="utf-8")
        (assets / "hub.js").write_text("export {};", encoding="utf-8")
        (assets / "secret.txt").write_text("no", encoding="utf-8")
        monkeypatch.setattr(hub_app, "ASSET_DIR", assets)
        for path in ("/", "/hub", "/hub/"):
            page = hub.client.get(path)
            assert (
                page.status_code == 200
                and "script-src 'self'" in page.headers["content-security-policy"]
            )
        script = hub.client.get("/hub/assets/hub.js")
        assert script.status_code == 200 and script.headers["content-type"].startswith(
            "text/javascript"
        )
        for path in ("/hub/assets/secret.txt", "/hub/assets/../app.py", "/hub/assets/hub.css"):
            assert hub.client.get(path).status_code == 404


class TestDocumentationSeam:
    """The server's wiring to alhazen.hub.documentation (owned by another
    worker), exercised with a stand-in module that follows its contract."""

    @pytest.fixture
    def documentation(self, monkeypatch):
        module = types.ModuleType("alhazen.hub.documentation")

        class DocumentationError(ValueError):
            pass

        def read_documentation(bundle_path, manifest):
            if not manifest.get("documentation"):
                return None
            with zipfile.ZipFile(bundle_path) as archive:
                descriptor = archive.read(manifest["documentation"])
            if b"BROKEN" in descriptor:
                raise DocumentationError("descriptor is not valid")
            return {"title": manifest["title"], "tasks": []}

        module.DocumentationError = DocumentationError
        module.read_documentation = read_documentation
        module.global_guide = lambda: {"modes": ["run"]}
        monkeypatch.setitem(sys.modules, "alhazen.hub.documentation", module)
        return module

    def documented(self, tmp_path, body=b"{}", version="1.0.0"):
        from alhazen.hub.packages import build_bundle

        source = tmp_path / f"doc-src-{version}"
        (source / "docs").mkdir(parents=True)
        (source / "run.py").write_bytes(b"print(1)\n")
        (source / "docs" / "experiment.json").write_bytes(body)
        out = tmp_path / f"doc-{version}.zip"
        meta = {
            "name": "documented",
            "version": version,
            "title": "Documented",
            "description": "",
            "hardware": {"display": True, "eye_tracker": False, "reward": False},
            "license": "MIT",
            "citations": [],
            "documentation": "docs/experiment.json",
        }
        build_bundle(source, out, meta, ["docs/experiment.json", "run.py"])
        return out

    def test_documentation_is_validated_on_upload_and_read_under_the_download_acl(
        self, hub, tmp_path, documentation
    ):
        hub.register("ada")
        hub.register("bob")
        ada, bob = hub.browser("ada"), hub.bearer("bob")
        eid = ada.create_experiment()["id"]
        broken = ada.upload_version(eid, self.documented(tmp_path, b'{"x": "BROKEN"}', "0.9.0"))
        assert (
            broken.status_code == 422 and broken.json()["error"]["code"] == "invalid_documentation"
        )
        vid = ada.upload_version(eid, self.documented(tmp_path)).json()["version"]["id"]
        path = f"/experiments/{eid}/versions/{vid}/documentation"
        assert ada.get(path).json() == {"documentation": {"title": "Documented", "tasks": []}}
        assert bob.get(path).status_code == 404
        assert hub.client.get(API + path).status_code == 404
        ada.publish(eid, vid)
        assert hub.client.get(API + path).json()["documentation"]["title"] == "Documented"
        assert hub.client.get(f"{API}/guide").json() == {"guide": {"modes": ["run"]}}

    def test_without_the_module_documented_uploads_are_refused(self, hub, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "alhazen.hub.documentation", None)
        hub.register("ada")
        ada = hub.browser("ada")
        eid = ada.create_experiment()["id"]
        response = ada.upload_version(eid, self.documented(tmp_path))
        assert response.status_code == 503
        assert hub.client.get(f"{API}/guide").status_code == 503


def test_settings_for_development_is_local_sqlite(tmp_path):
    settings = HubSettings.for_development(tmp_path)
    assert settings.is_sqlite and settings.public_origin == ORIGIN
    assert replace(settings, public_origin="https://x.org").secure_cookies


class TestSchemaTwo:
    def test_a_schema_one_database_migrates_without_losing_rows(self, tmp_path, clock):
        from alhazen.hub.schema import data_sessions

        settings = make_settings(tmp_path)
        admin.init_database(settings)
        service = Hub(settings, clock)
        service.register("ada")
        ada = service.browser("ada")
        eid = ada.create_experiment()["id"]
        vid = ada.upload_version(eid, make_bundle(tmp_path)).json()["version"]["id"]
        receipt = ada.upload_session(eid, vid, {"a": b"abcd"})
        service.app.state.hub.db.dispose()
        engine = create_engine(
            settings.database_url.replace("postgresql://", "postgresql+psycopg://")
        )
        with engine.begin() as conn:  # make it a schema-1 database again
            for column in (
                "seal_token",
                "problem_code",
                "problem_at",
                "previous_attempt_id",
                "retired_client_id",
                "index_token",
            ):
                conn.exec_driver_sql(f"ALTER TABLE hub_sessions DROP COLUMN {column}")
            conn.execute(text("UPDATE hub_schema SET value = '1' WHERE key = 'schema_version'"))
        engine.dispose()
        with pytest.raises(SchemaError, match="migrate"):
            create_app(settings)
        assert admin.migrate_database(settings) == SCHEMA_VERSION == 2
        service = Hub(settings, clock)
        with service.app.state.hub.db.transaction() as conn:
            row = conn.execute(data_sessions.select()).one()
        assert row.id == receipt["id"] and row.status == "committed" and row.seal_token is None
        ada = service.browser("ada")
        assert ada.post(f"/sessions/{receipt['id']}/complete").json()["status"] == "committed"


class TestAdminCommandLine:
    def test_operator_commands(self, tmp_path, capsys, monkeypatch):
        import json as json_module

        config = tmp_path / "hub.toml"
        config.write_text(
            '[server]\npublic_origin = "http://127.0.0.1:8750"\n'
            '[database]\nurl = "sqlite:///hub.sqlite3"\n[storage]\nartifact_root = "art"\n',
            encoding="utf-8",
        )
        run = lambda *args: admin.main(["--config", str(config), "--actor", "op", *args])  # noqa: E731
        assert run("init-db") == 0
        assert json_module.loads(capsys.readouterr().out)["schema_version"] == SCHEMA_VERSION
        assert run("invite", "--note", "pilot", "--days", "3") == 0
        invite = json_module.loads(capsys.readouterr().out)
        assert invite["code"].startswith("inv-")
        assert run("invites") == 0
        listed = json_module.loads(capsys.readouterr().out)
        assert listed[0]["id"] == invite["id"] and "code" not in listed[0]
        assert run("reconcile") == 0
        assert json_module.loads(capsys.readouterr().out)["missing_artifacts"] == 0
        answers = iter(["a long new passphrase", "a different passphrase"])
        monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
        assert run("reset-password", "nobody") == 2
        assert "differ" in capsys.readouterr().err
        assert run("disable", "nobody") == 2
        assert "no user" in capsys.readouterr().err


def test_package_limits_above_the_format_maxima_fail_at_start(tmp_path):
    from alhazen.hub.packages import DEFAULT_MAX_ARCHIVE_BYTES

    with pytest.raises(SettingsError, match="max_package_bytes"):
        make_settings(tmp_path, max_package_bytes=DEFAULT_MAX_ARCHIVE_BYTES + 1)
