"""Experiments, immutable versions, publication snapshots and the library
(review gate M2): every private object is invisible to everyone else, and a
public listing changes only at an explicit publish. Synthetic packages."""

from __future__ import annotations

import io
import zipfile

import pytest
from tests.hub.server_support import (
    API,
    Hub,
    make_bundle,
    make_settings,
)


@pytest.fixture
def people(hub):
    hub.register("ada")
    hub.register("bob")
    return hub.browser("ada"), hub.bearer("bob")


def published(hub, tmp_path, owner, **bundle):
    experiment = owner.create_experiment()
    version = owner.upload_version(experiment["id"], make_bundle(tmp_path, **bundle)).json()[
        "version"
    ]
    assert owner.publish(experiment["id"], version["id"]).status_code == 200
    return experiment, version


class TestExperiments:
    def test_create_list_and_edit_own(self, people):
        ada, _bob = people
        created = ada.create_experiment(tags=["vision", "saccade"], citations=["A (2020)."])
        assert created["owner"]["username"] == "ada" and created["published_version_id"] is None
        listed = ada.get("/experiments").json()
        assert [e["id"] for e in listed["items"]] == [created["id"]] and listed[
            "next_offset"
        ] is None
        edited = ada.patch(f"/experiments/{created['id']}", {"summary": "New"}).json()["experiment"]
        assert edited["summary"] == "New" and edited["title"] == created["title"]

    @pytest.mark.parametrize(
        "body",
        [
            {"title": ""},
            {"title": "x" * 121},
            {"title": "ok", "owner_id": "x"},
            {"title": "ok", "tags": ["Bad Tag"]},
            {"title": "ok", "citations": "not a list"},
            {"title": "bell\x07"},
        ],
    )
    def test_metadata_rules(self, people, body):
        ada, _ = people
        assert ada.post("/experiments", body).status_code == 400

    def test_another_user_cannot_see_edit_or_upload(self, people, tmp_path):
        ada, bob = people
        experiment = ada.create_experiment()
        eid = experiment["id"]
        assert bob.get(f"/experiments/{eid}").status_code == 404
        assert bob.patch(f"/experiments/{eid}", {"title": "mine"}).status_code == 404
        assert bob.upload_version(eid, make_bundle(tmp_path)).status_code == 404
        assert bob.get("/experiments").json()["items"] == []
        missing = bob.get(f"/experiments/{'0' * 32}")
        assert (
            missing.status_code == 404 and missing.json() == bob.get(f"/experiments/{eid}").json()
        )

    def test_pagination_bounds(self, people):
        ada, _ = people
        for n in range(3):
            ada.create_experiment(title=f"E{n}")
        first = ada.get("/experiments", params={"limit": 2}).json()
        assert len(first["items"]) == 2 and first["next_offset"] == 2
        assert ada.get("/experiments", params={"limit": 101}).status_code == 400
        assert ada.get("/experiments", params={"offset": -1}).status_code == 400


class TestVersions:
    def test_upload_is_immutable_and_replay_safe(self, people, tmp_path):
        ada, _ = people
        eid = ada.create_experiment()["id"]
        bundle = make_bundle(tmp_path)
        first = ada.upload_version(eid, bundle)
        assert first.status_code == 201
        version = first.json()["version"]
        assert version["manifest"]["name"] == "saccade-bias" and version["experiment_id"] == eid
        replay = ada.upload_version(eid, bundle)
        assert replay.status_code == 200 and replay.json()["version"]["id"] == version["id"]
        changed = make_bundle(tmp_path, extra={"notes.md": b"different"})
        clash = ada.upload_version(eid, changed)
        assert clash.status_code == 409 and clash.json()["error"]["code"] == "version_exists"
        other_name = ada.upload_version(eid, make_bundle(tmp_path, name="other", version="2.0.0"))
        assert other_name.status_code == 409
        assert other_name.json()["error"]["code"] == "package_name_mismatch"

    def test_download_streams_the_exact_bytes(self, people, tmp_path):
        ada, _ = people
        eid = ada.create_experiment()["id"]
        bundle = make_bundle(tmp_path)
        version = ada.upload_version(eid, bundle).json()["version"]
        response = ada.get(f"/experiments/{eid}/versions/{version['id']}/download")
        assert response.status_code == 200 and response.content == bundle.read_bytes()
        assert response.headers["x-alhazen-sha256"] == version["sha256"]
        assert "attachment" in response.headers["content-disposition"]
        assert "sandbox" in response.headers["content-security-policy"]

    def test_bad_packages_are_refused_before_storing(self, people, tmp_path, hub):
        ada, _ = people
        eid = ada.create_experiment()["id"]
        not_zip = ada.post(
            f"/experiments/{eid}/versions",
            content=b"hello",
            headers={"Content-Type": "application/zip"},
        )
        assert not_zip.status_code == 422 and not_zip.json()["error"]["code"] == "invalid_package"
        assert str(hub.settings.artifact_root) not in not_zip.text
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../escape.txt", b"x")
        hostile = ada.post(
            f"/experiments/{eid}/versions",
            content=buffer.getvalue(),
            headers={"Content-Type": "application/zip"},
        )
        assert hostile.status_code == 422
        wrong_type = ada.post(
            f"/experiments/{eid}/versions",
            content=b"x",
            headers={"Content-Type": "application/octet-stream"},
        )
        assert wrong_type.status_code == 415
        assert list((hub.settings.artifact_root / "tmp").iterdir()) == []
        assert ada.get(f"/experiments/{eid}").json()["versions"] == []

    def test_package_size_cap(self, tmp_path, clock):
        from alhazen.hub import admin

        small = make_settings(tmp_path, max_package_bytes=300)
        admin.init_database(small)
        service = Hub(small, clock)
        service.register("ada")
        ada = service.browser("ada")
        eid = ada.create_experiment()["id"]
        response = ada.upload_version(eid, make_bundle(tmp_path))
        assert response.status_code == 413

    def test_version_count_limit(self, tmp_path, clock):
        from alhazen.hub import admin

        small = make_settings(tmp_path, max_versions_per_experiment=1)
        admin.init_database(small)
        service = Hub(small, clock)
        service.register("ada")
        ada = service.browser("ada")
        eid = ada.create_experiment()["id"]
        assert ada.upload_version(eid, make_bundle(tmp_path)).status_code == 201
        second = ada.upload_version(eid, make_bundle(tmp_path, version="1.0.1"))
        assert second.status_code == 413 and second.json()["error"]["code"] == "version_limit"

    def test_unpublished_versions_are_invisible(self, people, tmp_path, hub):
        ada, bob = people
        eid = ada.create_experiment()["id"]
        vid = ada.upload_version(eid, make_bundle(tmp_path)).json()["version"]["id"]
        paths = (
            f"/experiments/{eid}/versions/{vid}/download",
            f"/experiments/{eid}/versions/{vid}/documentation",
            f"/experiments/{eid}",
        )
        for path in paths:
            assert bob.get(path).status_code == 404
            assert hub.client.get(API + path).status_code == 404

    def test_documentation_absent_is_null(self, people, tmp_path):
        ada, _ = people
        eid = ada.create_experiment()["id"]
        vid = ada.upload_version(eid, make_bundle(tmp_path)).json()["version"]["id"]
        assert ada.get(f"/experiments/{eid}/versions/{vid}/documentation").json() == {
            "documentation": None
        }


class TestPublication:
    def test_publish_needs_acknowledgements_and_a_matching_licence(self, people, tmp_path):
        ada, _ = people
        eid = ada.create_experiment(license="")["id"]
        vid = ada.upload_version(eid, make_bundle(tmp_path)).json()["version"]["id"]
        no_ack = ada.post(f"/experiments/{eid}/publish", {"version_id": vid, "license_ack": True})
        assert no_ack.status_code == 400
        assert ada.publish(eid, vid).json()["error"]["code"] == "license_required"
        ada.patch(f"/experiments/{eid}", {"license": "Apache-2.0"})
        assert ada.publish(eid, vid).json()["error"]["code"] == "license_mismatch"
        ada.patch(f"/experiments/{eid}", {"license": "MIT"})
        assert ada.publish(eid, vid).status_code == 200

    def test_public_listing_is_a_frozen_snapshot(self, people, tmp_path, hub):
        ada, bob = people
        experiment, v1 = published(hub, tmp_path, ada)
        eid = experiment["id"]
        ada.patch(
            f"/experiments/{eid}", {"title": "Private draft title", "description": "secret plan"}
        )
        v2 = ada.upload_version(eid, make_bundle(tmp_path, version="2.0.0")).json()["version"]
        for view in (
            bob.get(f"/experiments/{eid}").json(),
            hub.client.get(f"{API}/experiments/{eid}").json(),
        ):
            assert view["experiment"]["title"] == "Saccade bias"
            assert "secret" not in str(view)
            assert [v["id"] for v in view["versions"]] == [v1["id"]]
            assert view["can_edit"] is False
        listing = hub.client.get(f"{API}/catalog").json()["items"]
        assert [(i["experiment"]["title"], i["version"]["id"]) for i in listing] == [
            ("Saccade bias", v1["id"])
        ]
        assert bob.get(f"/experiments/{eid}/versions/{v2['id']}/download").status_code == 404
        assert bob.get(f"/experiments/{eid}/versions/{v1['id']}/download").status_code == 200
        owner_view = ada.get(f"/experiments/{eid}").json()
        assert owner_view["experiment"]["title"] == "Private draft title"
        assert (
            owner_view["publication"]["title"] == "Saccade bias"
            and len(owner_view["versions"]) == 2
        )
        assert ada.publish(eid, v2["id"]).status_code == 200
        assert bob.get(f"/experiments/{eid}").json()["experiment"]["title"] == "Private draft title"
        assert bob.get(f"/experiments/{eid}/versions/{v1['id']}/download").status_code == 404

    def test_swapped_parent_and_child_ids_are_refused(self, people, tmp_path, hub):
        ada, bob = people
        exp_a, _va = published(hub, tmp_path, ada)
        exp_b = ada.create_experiment(title="B")
        vb = ada.upload_version(exp_b["id"], make_bundle(tmp_path, name="other")).json()["version"]
        assert ada.publish(exp_a["id"], vb["id"]).status_code == 404
        assert (
            bob.get(f"/experiments/{exp_a['id']}/versions/{vb['id']}/download").status_code == 404
        )
        assert (
            ada.get(f"/experiments/{exp_a['id']}/versions/{vb['id']}/download").status_code == 404
        )
        assert (
            bob.post("/library", {"experiment_id": exp_a["id"], "version_id": vb["id"]}).status_code
            == 404
        )

    def test_only_the_owner_publishes(self, people, tmp_path, hub):
        ada, bob = people
        eid = ada.create_experiment()["id"]
        vid = ada.upload_version(eid, make_bundle(tmp_path)).json()["version"]["id"]
        assert bob.publish(eid, vid).status_code == 404
        assert bob.post(f"/experiments/{eid}/unpublish").status_code == 404

    def test_unpublish_stops_new_reads(self, people, tmp_path, hub):
        ada, bob = people
        experiment, v1 = published(hub, tmp_path, ada)
        eid = experiment["id"]
        assert (
            bob.post("/library", {"experiment_id": eid, "version_id": v1["id"]}).status_code == 200
        )
        assert ada.post(f"/experiments/{eid}/unpublish").status_code == 200
        assert hub.client.get(f"{API}/catalog").json()["items"] == []
        assert bob.get(f"/experiments/{eid}").status_code == 404
        assert bob.get(f"/experiments/{eid}/versions/{v1['id']}/download").status_code == 404
        item = bob.get("/library").json()["items"][0]
        assert item["available"] is False and item["experiment"]["title"] == "Saccade bias"

    def test_catalogue_search(self, people, tmp_path, hub):
        ada, _ = people
        published(hub, tmp_path, ada)
        other = ada.create_experiment(title="Motion 100%_test", tags=["rdk"])
        vid = ada.upload_version(other["id"], make_bundle(tmp_path, name="motion")).json()[
            "version"
        ]["id"]
        ada.publish(other["id"], vid)

        def search(query):
            response = hub.client.get(f"{API}/catalog", params={"query": query})
            return [item["experiment"]["title"] for item in response.json()["items"]]

        assert search("saccade") == ["Saccade bias"]
        assert search("rdk") == ["Motion 100%_test"]
        assert search("100%") == ["Motion 100%_test"]
        assert search("%") == ["Motion 100%_test"]
        assert len(search("")) == 2


class TestLibrary:
    def test_pin_published_or_own_versions_only(self, people, tmp_path, hub):
        ada, bob = people
        experiment, v1 = published(hub, tmp_path, ada)
        eid = experiment["id"]
        v2 = ada.upload_version(eid, make_bundle(tmp_path, version="2.0.0")).json()["version"]
        assert (
            bob.post("/library", {"experiment_id": eid, "version_id": v2["id"]}).status_code == 404
        )
        item = bob.post("/library", {"experiment_id": eid, "version_id": v1["id"]}).json()
        assert item["version"]["id"] == v1["id"] and item["available"] is True
        assert (
            ada.post("/library", {"experiment_id": eid, "version_id": v2["id"]}).status_code == 200
        )
        bob_items = bob.get("/library").json()["items"]
        assert len(bob_items) == 1 and bob_items[0]["experiment"]["title"] == "Saccade bias"
        assert ada.get("/library").json()["items"][0]["version"]["id"] == v2["id"]

    def test_one_pin_per_experiment(self, people, tmp_path, hub):
        ada, bob = people
        experiment, v1 = published(hub, tmp_path, ada)
        v2 = ada.upload_version(experiment["id"], make_bundle(tmp_path, version="2.0.0")).json()[
            "version"
        ]
        bob.post("/library", {"experiment_id": experiment["id"], "version_id": v1["id"]})
        ada.publish(experiment["id"], v2["id"])
        bob.post("/library", {"experiment_id": experiment["id"], "version_id": v2["id"]})
        items = bob.get("/library").json()["items"]
        assert [i["version"]["id"] for i in items] == [v2["id"]]

    def test_library_needs_sign_in(self, hub):
        assert hub.client.get(f"{API}/library").status_code == 401
