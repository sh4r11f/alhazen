"""The platform's rules: who may read and change what, and that versions and copies stay
fixed when the catalogue moves on."""

from __future__ import annotations

import io
import json
import threading
import zipfile
from pathlib import Path

import pytest
from tests.unit.platform_support import add_version, experiment, zip_of

from alhazen.experiment_platform.errors import (
    BundleError,
    Conflict,
    Forbidden,
    InvalidRequest,
    NotFound,
)
from alhazen.experiment_platform.service import PlatformService, Principal
from alhazen.experiment_platform.store import FileStore

ADA = Principal("uada0000001", "Ada")
BEN = Principal("uben0000001", "Ben")
FORM = {
    "title": "Dots, slower",
    "summary": "Half speed.",
    "category": "motion",
    "version": "1.2.0-1",
    "subjects": ["human", "fish"],
    "hardware": {"eye_tracker": True, "teleporter": True},
    "license": "CC0",
}


@pytest.fixture()
def store(tmp_path: Path) -> FileStore:
    return FileStore(tmp_path / "store", create=True)


@pytest.fixture()
def service(store: FileStore) -> PlatformService:
    add_version(store, "lab", "dots", experiment())
    return PlatformService(store)


class TestCatalogue:
    def test_listings_carry_a_handle_and_a_computed_docs_status(self, service: PlatformService):
        listing = service.catalogue(ADA)["lab"]["dots"]
        assert listing["bundle"]["path"] == f"L~lab~dots~{listing['bundle']['id']}"
        assert listing["docsStatus"] == "current"

    def test_a_private_catalogue_needs_a_principal(self, service: PlatformService):
        with pytest.raises(Forbidden):
            service.catalogue(None)
        assert PlatformService(service.store, public_catalogue=True).catalogue(None)["lab"]["dots"]

    def test_docs_bound_to_another_version_are_reported_stale(self, store: FileStore):
        add_version(
            store,
            "lab",
            "stale",
            experiment(),
            docs={
                "methods": "x",
                "guide": json.dumps({"bundleId": "other-0123456789"}),
                "parameterGuide": json.dumps({"sourceCommit": "a" * 40}),
            },
        )
        assert PlatformService(store).docs(ADA, "lab", "stale")["docsStatus"] == "stale"

    def test_docs_of_an_earlier_version_stay_readable(
        self, store: FileStore, service: PlatformService
    ):
        first = store.listing("lab", "dots")["bundle"]["id"]
        add_version(
            store,
            "lab",
            "dots",
            experiment(**{"configs/task.yaml": b"n_trials: 80\n"}),
            commit="b" * 40,
            version="1.3.0",
            docs={"methods": "new methods"},
        )
        assert service.docs(ADA, "lab", "dots")["methods"] == "new methods"
        assert service.docs(ADA, "lab", "dots", first)["methods"].startswith("## Question")
        with pytest.raises(NotFound):
            service.docs(ADA, "lab", "dots", "dots-ffffffffff")


class TestForks:
    def test_a_fork_is_a_private_copy_with_the_methods_of_its_version(
        self, service: PlatformService
    ):
        fork_id, fork = service.fork(ADA, "lab", "dots")
        assert fork["head"]["path"].startswith(f"F~{fork_id}~")
        assert fork["origin"]["bundle"] == fork["head"]["id"]
        assert service.fork_docs(ADA, fork_id)["docs"]["methods"].startswith("## Question")
        assert fork_id in service.forks(ADA) and service.forks(BEN) == {}

    def test_another_users_copy_is_reported_missing_not_forbidden(self, service: PlatformService):
        fork_id, fork = service.fork(ADA, "lab", "dots")
        for attempt in (
            lambda: service.fork_docs(BEN, fork_id),
            lambda: service.bundle_meta(BEN, fork["head"]["path"]),
            lambda: service.upload_revision(BEN, fork_id, zip_of(experiment()), "x"),
            lambda: service.publish(BEN, fork_id, FORM),
            lambda: service.remove_fork(BEN, fork_id),
        ):
            with pytest.raises(NotFound):
                attempt()
        assert service.bundle_meta(ADA, fork["head"]["path"])["fileCount"] == 4

    def test_a_new_catalogue_version_leaves_existing_copies_alone(
        self, store: FileStore, service: PlatformService
    ):
        fork_id, fork = service.fork(ADA, "lab", "dots")
        add_version(
            store,
            "lab",
            "dots",
            experiment(**{"run.py": b"PARAMETERS = {}\n"}),
            commit="c" * 40,
            version="2.0.0",
            docs={"methods": "rewritten"},
        )
        again = service.forks(ADA)[fork_id]
        assert again["head"] == fork["head"]
        assert service.fork_docs(ADA, fork_id)["docs"]["methods"].startswith("## Question")
        entries = store.bundle(ADA.uid, fork["head"]["id"]).entries()
        assert {e[0]: e[1] for e in entries}["run.py"].startswith("PARAMETERS = {\n")

    def test_anonymous_visitors_cannot_fork(self, store: FileStore):
        add_version(store, "lab", "dots", experiment())
        with pytest.raises(Forbidden):
            PlatformService(store, public_catalogue=True).fork(None, "lab", "dots")

    def test_a_revision_from_an_archive_becomes_the_head(self, service: PlatformService):
        fork_id, fork = service.fork(ADA, "lab", "dots")
        changed = experiment(
            **{"configs/task.yaml": b"n_trials: 20\n"}, **{"notes.md": b"slower\n"}
        )
        revision, after = service.upload_revision(ADA, fork_id, zip_of(changed), "Half the trials")
        assert after["head"]["id"] == revision["bundle"] != fork["head"]["id"]
        assert "configs/task.yaml" in revision["stat"] and "notes.md (added)" in revision["stat"]
        assert len(after["revisions"]) == 2
        # the fork's methods still describe the version it came from
        assert service.fork_docs(ADA, fork_id)["docs"] is not None

    @pytest.mark.parametrize(
        "archive, message, error",
        [
            (zip_of({"run.py": b"1", "../x.py": b"2"}, top=""), "m", BundleError),
            (zip_of({"notes.txt": b"just notes"}), "m", InvalidRequest),
            (zip_of(experiment()), "", InvalidRequest),
            (b"not a zip", "m", BundleError),
        ],
    )
    def test_bad_uploads_are_refused_and_change_nothing(
        self, service: PlatformService, archive, message, error
    ):
        fork_id, fork = service.fork(ADA, "lab", "dots")
        with pytest.raises(error):
            service.upload_revision(ADA, fork_id, archive, message)
        assert service.forks(ADA)[fork_id]["head"] == fork["head"]

    def test_a_downloaded_revision_is_a_zip_of_its_files(self, service: PlatformService):
        fork_id, fork = service.fork(ADA, "lab", "dots")
        name, data = service.bundle_archive(ADA, fork["head"]["path"])
        assert name.startswith(fork_id) and name.endswith(".zip")
        assert sorted(zipfile.ZipFile(io.BytesIO(data)).namelist()) == sorted(
            f"{fork_id}/{p}" for p in experiment()
        )

    def test_concurrent_revisions_are_all_kept(self, service: PlatformService):
        fork_id, _ = service.fork(ADA, "lab", "dots")
        errors: list[Exception] = []

        def upload(i: int) -> None:
            try:
                service.upload_revision(
                    ADA, fork_id, zip_of(experiment(**{"n.txt": str(i).encode()})), f"r{i}"
                )
            except Exception as error:  # noqa: BLE001 - collected and asserted below
                errors.append(error)

        threads = [threading.Thread(target=upload, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        messages = {r["message"] for r in service.forks(ADA)[fork_id]["revisions"].values()}
        assert {f"r{i}" for i in range(8)} <= messages

    def test_concurrent_forks_each_get_their_own_copy(self, service: PlatformService):
        ids: list[str] = []
        threads = [
            threading.Thread(target=lambda: ids.append(service.fork(ADA, "lab", "dots")[0]))
            for _ in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(ids)) == 6 and set(ids) <= set(service.forks(ADA))


class TestPublishing:
    def test_a_publication_carries_the_publishers_own_name(self, service: PlatformService):
        fork_id, _ = service.fork(ADA, "lab", "dots")
        listing = service.publish(
            ADA, fork_id, {**FORM, "publisher": {"uid": "lab", "name": "Someone else"}}
        )
        assert listing["publisher"] == {"uid": ADA.uid, "name": "Ada"}
        assert listing["subjects"] == ["human"] and listing["hardware"] == {"eye_tracker": True}
        assert service.catalogue(BEN)[ADA.uid][fork_id]["title"] == "Dots, slower"
        assert service.forks(ADA)[fork_id]["published"]["version"] == "1.2.0-1"
        # an unchanged copy keeps the methods of its version
        assert service.docs(BEN, ADA.uid, fork_id)["methods"].startswith("## Question")

    def test_others_can_fork_a_publication_and_it_stays_theirs_after_unpublishing(
        self, service: PlatformService
    ):
        fork_id, _ = service.fork(ADA, "lab", "dots")
        service.publish(ADA, fork_id, FORM)
        ben_fork, ben = service.fork(BEN, ADA.uid, fork_id)
        service.unpublish(ADA, fork_id)
        assert ADA.uid not in service.catalogue(BEN)
        assert service.bundle_meta(BEN, ben["head"]["path"])["fileCount"] == 4
        assert "published" not in service.forks(ADA)[fork_id]

    @pytest.mark.parametrize(
        "change, message",
        [
            ({"title": ""}, "Title"),
            ({"version": "1 2"}, "Version"),
            ({"category": "../x"}, "Category"),
            ({"title": "x" * 300}, "longer"),
            ({"hardware": ["eye_tracker"]}, "Hardware"),
        ],
    )
    def test_bad_publication_forms_are_refused(self, service: PlatformService, change, message):
        fork_id, _ = service.fork(ADA, "lab", "dots")
        with pytest.raises(InvalidRequest, match=message):
            service.publish(ADA, fork_id, {**FORM, **change})

    def test_a_copy_without_a_revision_cannot_be_published(
        self, store: FileStore, service: PlatformService
    ):
        store.put_fork(
            ADA.uid, "empty-1", {"title": "Empty", "origin": {"scratch": True}, "head": None}
        )
        with pytest.raises(Conflict):
            service.publish(ADA, "empty-1", FORM)

    def test_removing_a_copy_keeps_its_listing(self, service: PlatformService):
        fork_id, _ = service.fork(ADA, "lab", "dots")
        service.publish(ADA, fork_id, FORM)
        service.remove_fork(ADA, fork_id)
        assert fork_id not in service.forks(ADA)
        assert service.catalogue(BEN)[ADA.uid][fork_id]

    def test_removing_a_copy_deletes_its_revisions_and_unshared_docs_snapshot(
        self, store: FileStore, service: PlatformService
    ):
        first, fork = service.fork(ADA, "lab", "dots")
        origin_bundle = fork["head"]["id"]
        revision, _ = service.upload_revision(
            ADA, first, zip_of(experiment(**{"configs/task.yaml": b"n_trials: 12\n"})), "fewer"
        )
        second, _ = service.fork(ADA, "lab", "dots")

        removed = service.remove_fork(ADA, first)

        # Its own revision goes; the forked version and its methods stay: the second copy
        # still names both.
        assert removed == {"bundles": [revision["bundle"]], "docs": []}
        assert store.private_bundles(ADA.uid) == [origin_bundle]
        assert service.fork_docs(ADA, second)["docs"]["methods"].startswith("## Question")
        with pytest.raises(NotFound):
            service.fork_docs(ADA, first)

        removed = service.remove_fork(ADA, second)
        assert removed == {"bundles": [origin_bundle], "docs": [origin_bundle]}
        assert store.private_bundles(ADA.uid) == [] and store.fork_docs_keys(ADA.uid) == []
        # The catalogue version itself is untouched.
        assert service.bundle_meta(ADA, service.catalogue(ADA)["lab"]["dots"]["bundle"]["path"])

    def test_removing_a_published_copy_keeps_the_listings_files(
        self, store: FileStore, service: PlatformService
    ):
        fork_id, _ = service.fork(ADA, "lab", "dots")
        service.upload_revision(
            ADA, fork_id, zip_of(experiment(**{"configs/task.yaml": b"n_trials: 9\n"})), "nine"
        )
        listing = service.publish(ADA, fork_id, FORM)
        service.remove_fork(ADA, fork_id)
        assert store.private_bundles(ADA.uid) == []
        # Ben can still fork the published revision: it lives in the published store.
        ben_id, ben = service.fork(BEN, ADA.uid, fork_id)
        assert ben["head"]["id"] == listing["bundle"]["id"]
        assert service.bundle_meta(BEN, ben["head"]["path"])["fileCount"] == 4

    def test_removing_another_users_copy_leaves_theirs(
        self, store: FileStore, service: PlatformService
    ):
        ada_id, _ = service.fork(ADA, "lab", "dots")
        ben_id, ben = service.fork(BEN, "lab", "dots")
        service.remove_fork(ADA, ada_id)
        assert store.private_bundles(BEN.uid) == [ben["head"]["id"]]
        assert service.fork_docs(BEN, ben_id)["docs"]

    def test_unknown_versions_of_a_listing_are_not_served(self, service: PlatformService):
        with pytest.raises(NotFound):
            service.bundle_meta(ADA, "L~lab~dots~dots-0000000000")
        with pytest.raises(NotFound):
            service.bundle_meta(ADA, "L~lab~../x~dots-0000000000")
        with pytest.raises(NotFound):
            service.bundle_chunk(ADA, service.catalogue(ADA)["lab"]["dots"]["bundle"]["path"], 7)
