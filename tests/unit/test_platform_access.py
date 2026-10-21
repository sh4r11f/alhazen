"""Who can see an experiment (design.md section 5a): private, specific people, everyone.

A principal who may not see a listing finds nothing (404, never 403) through any route that
reaches it; only the publisher changes who sees it; people are existing, enabled accounts;
a refresh never changes an existing listing's access; copies made earlier stay theirs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.unit.platform_support import add_version, call, experiment, zip_of

from alhazen.cli.main import main as alhazen_main
from alhazen.experiment_platform import access as access_rules
from alhazen.experiment_platform import accounts as accounts_module
from alhazen.experiment_platform.accounts import Accounts
from alhazen.experiment_platform.errors import AuthError, Forbidden, InvalidRequest, NotFound
from alhazen.experiment_platform.local import LocalGate
from alhazen.experiment_platform.server import ServerConfig, build_app, validate
from alhazen.experiment_platform.service import PlatformService, Principal
from alhazen.experiment_platform.store import FileStore
from alhazen.experiment_platform.webapp import PlatformApp

PASSWORD = "a long enough password"
FORM = {
    "title": "Dots, slower",
    "summary": "Half speed.",
    "category": "motion",
    "version": "1.0.0",
    "license": "CC0",
}


@pytest.fixture()
def store(tmp_path: Path) -> FileStore:
    return FileStore(tmp_path / "store", create=True)


@pytest.fixture()
def accounts(store: FileStore, monkeypatch) -> Accounts:
    monkeypatch.setattr(accounts_module, "_SCRYPT", {"n": 2**10, "r": 8, "p": 1})
    return Accounts(store.root / "accounts.sqlite3")


@pytest.fixture()
def people(accounts: Accounts) -> dict[str, Principal]:
    return {
        name: accounts.add_user(name, display, PASSWORD)
        for name, display in [
            ("ada", "Ada Lovelace"),
            ("ben", "Ben Franklin"),
            ("cy", "Cy Twombly"),
            ("dee", "Dee Dee"),
        ]
    }


@pytest.fixture()
def service(store: FileStore, accounts: Accounts, people) -> PlatformService:
    ada = people["ada"].uid
    add_version(store, ada, "dots", experiment())
    return PlatformService(store, people=accounts)


def set_access(store: FileStore, pub: str, exp: str, value: dict) -> None:  # type: ignore[type-arg]
    store.put_listing(pub, exp, dict(store.listing(pub, exp), access=value))


def handle(store: FileStore, pub: str, exp: str) -> str:
    listing = store.listing(pub, exp)
    assert listing is not None
    return f"L~{pub}~{exp}~{listing['bundle']['id']}"


class TestRule:
    def test_a_listing_without_access_is_public(self):
        assert access_rules.of({}) == {"visibility": "public"}
        assert access_rules.can_see({}, "ada", None)

    def test_an_access_value_this_version_does_not_know_is_private(self):
        for odd in ({"visibility": "friends"}, "everyone", {"people": ["x"]}):
            assert access_rules.of({"access": odd}) == {"visibility": "private"}
            assert not access_rules.can_see({"access": odd}, "ada", "ben")
            assert access_rules.can_see({"access": odd}, "ada", "ada")

    def test_normalize_dedupes_drops_the_publisher_and_keeps_people_only_for_people(self):
        assert access_rules.normalize(
            {"visibility": "people", "people": ["b", " c", "b", "ada", "c"]}, "ada"
        ) == {"visibility": "people", "people": ["b", "c"]}
        assert access_rules.normalize({"visibility": "public", "people": ["b"]}, "ada") == {
            "visibility": "public"
        }
        with pytest.raises(InvalidRequest, match="at least one person"):
            access_rules.normalize({"visibility": "people", "people": ["ada"]}, "ada")
        with pytest.raises(InvalidRequest, match="at most 200"):
            access_rules.normalize(
                {"visibility": "people", "people": [f"u{i}" for i in range(201)]}, "ada"
            )
        for bad in ({"visibility": "everyone"}, None, {"visibility": "people", "people": "b"}):
            with pytest.raises(InvalidRequest):
                access_rules.normalize(bad, "ada")
        with pytest.raises(InvalidRequest, match="not an account id"):
            access_rules.normalize({"visibility": "people", "people": ["../x"]}, "ada")


class TestVisibility:
    def test_the_publisher_sees_it_in_full_others_see_the_visibility_only(
        self, service: PlatformService, store: FileStore, people
    ):
        ada, ben, cy = people["ada"], people["ben"], people["cy"]
        set_access(store, ada.uid, "dots", {"visibility": "people", "people": [ben.uid]})
        mine = service.catalogue(ada)[ada.uid]["dots"]
        assert mine["access"] == {
            "visibility": "people",
            "people": [ben.uid],
            "names": {ben.uid: "Ben Franklin"},
        }
        assert mine["canManage"] is True
        shared = service.catalogue(ben)[ada.uid]["dots"]
        assert shared["access"] == {"visibility": "people"} and shared["sharedWithYou"] is True
        assert shared["canManage"] is False
        assert ada.uid not in service.catalogue(cy)

    def test_a_legacy_listing_is_public_to_everyone(self, service: PlatformService, people):
        listing = service.catalogue(people["cy"])[people["ada"].uid]["dots"]
        assert listing["access"] == {"visibility": "public"} and "sharedWithYou" not in listing

    def test_every_route_to_an_unseen_listing_is_not_found(
        self, service: PlatformService, store: FileStore, people
    ):
        ada, ben = people["ada"], people["ben"]
        h = handle(store, ada.uid, "dots")
        bid = store.listing(ada.uid, "dots")["bundle"]["id"]
        set_access(store, ada.uid, "dots", {"visibility": "private"})
        anyone = PlatformService(store, public_catalogue=True, people=service.people_directory)
        for who, svc in ((ben, service), (None, anyone)):
            calls = [
                ("docs", (who, ada.uid, "dots")),
                ("docs", (who, ada.uid, "dots", bid)),
                ("bundle_meta", (who, h)),
                ("bundle_chunk", (who, h, 0)),
                ("bundle_archive", (who, h)),
            ]
            for method, args in calls:
                with pytest.raises(NotFound):
                    getattr(svc, method)(*args)
            # A copy needs an account (Forbidden for a visitor); a signed-in principal
            # finds nothing, so the listing does not leak.
            with pytest.raises(Forbidden if who is None else NotFound):
                svc.fork(who, ada.uid, "dots")
        assert service.docs(ada, ada.uid, "dots")["bundleId"] == bid
        assert service.bundle_meta(ada, h)["fileCount"] == 4

    def test_people_see_a_shared_listing_and_can_copy_it(
        self, service: PlatformService, store: FileStore, people
    ):
        ada, ben, cy = people["ada"], people["ben"], people["cy"]
        set_access(store, ada.uid, "dots", {"visibility": "people", "people": [ben.uid]})
        fork_id, fork = service.fork(ben, ada.uid, "dots")
        assert fork["origin"]["publisherUid"] == ada.uid
        with pytest.raises(NotFound):
            service.fork(cy, ada.uid, "dots")

    def test_narrowing_leaves_copies_people_already_made(
        self, service: PlatformService, store: FileStore, people
    ):
        ada, ben = people["ada"], people["ben"]
        fork_id, fork = service.fork(ben, ada.uid, "dots")
        service.set_access(ada, ada.uid, "dots", {"visibility": "private"})
        assert service.forks(ben)[fork_id]["head"]["id"] == fork["head"]["id"]
        assert service.bundle_meta(ben, fork["head"]["path"])["fileCount"] == 4
        assert service.fork_docs(ben, fork_id)["docs"] is not None
        with pytest.raises(NotFound):
            service.docs(ben, ada.uid, "dots")


class TestChangingAccess:
    def test_only_the_publisher_changes_it(
        self, service: PlatformService, store: FileStore, people
    ):
        ada, ben = people["ada"], people["ben"]
        with pytest.raises(Forbidden, match="Only the publisher"):
            service.set_access(ben, ada.uid, "dots", {"visibility": "private"})
        anyone = PlatformService(store, public_catalogue=True, people=service.people_directory)
        with pytest.raises(Forbidden):
            anyone.set_access(None, ada.uid, "dots", {"visibility": "private"})
        listing = service.set_access(ada, ada.uid, "dots", {"visibility": "private"})
        assert listing["access"]["visibility"] == "private"
        assert store.listing(ada.uid, "dots")["access"] == {"visibility": "private"}
        with pytest.raises(NotFound):
            service.set_access(ben, ada.uid, "dots", {"visibility": "public"})
        with pytest.raises(NotFound):
            service.set_access(ada, ada.uid, "nothing", {"visibility": "public"})

    def test_people_must_be_existing_enabled_accounts(
        self, service: PlatformService, accounts: Accounts, people
    ):
        ada, ben, cy = people["ada"], people["ben"], people["cy"]
        with pytest.raises(InvalidRequest, match="unobody"):
            service.set_access(
                ada, ada.uid, "dots", {"visibility": "people", "people": [ben.uid, "unobody"]}
            )
        accounts.set_disabled("cy", True)
        with pytest.raises(InvalidRequest, match=cy.uid):
            service.set_access(ada, ada.uid, "dots", {"visibility": "people", "people": [cy.uid]})
        with pytest.raises(InvalidRequest, match="Only you"):
            service.set_access(ada, ada.uid, "dots", {"visibility": "people", "people": []})
        listing = service.set_access(
            ada, ada.uid, "dots", {"visibility": "people", "people": [ben.uid, ada.uid, ben.uid]}
        )
        assert listing["access"]["people"] == [ben.uid]

    def test_a_host_without_accounts_cannot_share_with_people(self, store: FileStore):
        add_version(store, "local", "dots", experiment())
        local = PlatformService(store)
        me = Principal("local", "Me")
        with pytest.raises(InvalidRequest, match="no other accounts"):
            local.set_access(me, "local", "dots", {"visibility": "people", "people": ["ux"]})
        assert local.set_access(me, "local", "dots", {"visibility": "private"})["canManage"]
        assert local.people(me, "ad") == []


class TestPublishing:
    def _copy(self, service: PlatformService, who: Principal) -> str:
        fork_id, _ = service.fork(who, *self._source(service))
        service.upload_revision(
            who, fork_id, zip_of(experiment(**{"configs/task.yaml": b"n_trials: 10\n"})), "fewer"
        )
        return fork_id

    def _source(self, service: PlatformService) -> tuple[str, str]:
        [pub] = service.store.listings()
        return pub, "dots"

    def test_without_a_choice_a_new_listing_is_public_and_an_existing_one_keeps_its_access(
        self, service: PlatformService, store: FileStore, people
    ):
        ben, cy = people["ben"], people["cy"]
        fork_id = self._copy(service, ben)
        listing = service.publish(ben, fork_id, FORM)
        assert store.listing(ben.uid, fork_id)["access"] == {"visibility": "public"}
        assert listing["canManage"] is True
        service.set_access(ben, ben.uid, fork_id, {"visibility": "people", "people": [cy.uid]})
        service.publish(ben, fork_id, dict(FORM, version="1.0.1"))
        assert store.listing(ben.uid, fork_id)["access"] == {
            "visibility": "people",
            "people": [cy.uid],
        }

    def test_the_form_can_choose_who_sees_it(
        self, service: PlatformService, store: FileStore, people
    ):
        ben, cy = people["ben"], people["cy"]
        fork_id = self._copy(service, ben)
        service.publish(ben, fork_id, dict(FORM, access={"visibility": "private"}))
        assert fork_id not in service.catalogue(cy).get(ben.uid, {})
        assert service.catalogue(ben)[ben.uid][fork_id]["access"]["visibility"] == "private"
        with pytest.raises(InvalidRequest):
            service.publish(
                ben, fork_id, dict(FORM, access={"visibility": "people", "people": ["nobody"]})
            )
        assert store.listing(ben.uid, fork_id)["access"] == {"visibility": "private"}


class TestFindingPeople:
    def test_search_matches_user_names_and_names_by_prefix(
        self, service: PlatformService, accounts: Accounts, people
    ):
        ada = people["ada"]
        assert [p["username"] for p in service.people(ada, "be")] == ["ben"]
        assert [p["username"] for p in service.people(ada, "franK")] == ["ben"]  # a word
        assert service.people(ada, "Cy")[0] == {
            "uid": people["cy"].uid,
            "name": "Cy Twombly",
            "username": "cy",
        }
        assert service.people(ada, "b") == []  # a 1-character prefix finds no one
        assert service.people(ada, "ada") == []  # never yourself
        assert service.people(ada, "%") == [] and service.people(ada, "_e") == []
        accounts.set_disabled("ben", True)
        assert service.people(ada, "ben") == []

    def test_at_most_ten_results_and_only_when_signed_in(
        self, service: PlatformService, accounts: Accounts, people
    ):
        for i in range(14):
            accounts.add_user(f"lab{i:02d}", f"Lab Member {i}", PASSWORD)
        found = service.people(people["ada"], "lab")
        assert len(found) == 10 and found[0]["username"] == "lab00"
        with pytest.raises(AuthError):
            service.people(None, "lab")

    def test_describing_people_by_id(self, service: PlatformService, accounts: Accounts, people):
        ada, ben, cy = people["ada"], people["ben"], people["cy"]
        accounts.set_disabled("cy", True)
        described = service.describe_people(ada, [ben.uid, "unknown", cy.uid, ben.uid])
        assert described == [{"uid": ben.uid, "name": "Ben Franklin", "username": "ben"}]
        with pytest.raises(InvalidRequest):
            service.describe_people(ada, [f"u{i}" for i in range(201)])


class TestHttp:
    @pytest.fixture()
    def app(self, store: FileStore, accounts: Accounts, people) -> PlatformApp:
        add_version(store, people["ada"].uid, "dots", experiment())
        return build_app(validate(ServerConfig(store=store.root, host="127.0.0.1", port=8000)))

    @staticmethod
    def login(app: PlatformApp, user: str) -> dict[str, str]:
        response = call(
            app,
            "POST",
            "/api/login",
            headers={
                "Content-Type": "application/json",
                "X-Alhazen-Platform": "1",
                "Origin": "http://127.0.0.1:8000",
            },
            body=json.dumps({"username": user, "password": PASSWORD}).encode(),
        )
        cookie = response.headers["set-cookie"].split(";")[0]
        return {"Cookie": cookie, "X-Alhazen-CSRF": response.json()["csrf"]}

    def put_access(self, app, who, pub, value, csrf=True):  # type: ignore[no-untyped-def]
        headers = {**who, "Content-Type": "application/json"}
        if not csrf:
            headers.pop("X-Alhazen-CSRF")
        return call(
            app,
            "PUT",
            f"/api/catalogue/{pub}/dots/access",
            headers=headers,
            body=json.dumps(value).encode(),
        )

    def test_changing_access_over_http(self, app: PlatformApp, people):
        ada_uid, ben_uid = people["ada"].uid, people["ben"].uid
        ada, ben, cy = self.login(app, "ada"), self.login(app, "ben"), self.login(app, "cy")
        refused = self.put_access(app, ada, ada_uid, {"visibility": "private"}, csrf=False)
        assert refused.status == 403
        assert self.put_access(app, ben, ada_uid, {"visibility": "private"}).status == 403
        changed = self.put_access(app, ada, ada_uid, {"visibility": "people", "people": [ben_uid]})
        assert changed.status == 200
        assert changed.json()["listing"]["access"]["names"] == {ben_uid: "Ben Franklin"}
        assert self.put_access(app, cy, ada_uid, {"visibility": "public"}).status == 404
        catalogue = call(app, "GET", "/api/catalogue", headers=cy).json()["published"]
        assert ada_uid not in catalogue
        assert call(app, "GET", f"/api/catalogue/{ada_uid}/dots/docs", headers=cy).status == 404
        shared = call(app, "GET", "/api/catalogue", headers=ben).json()["published"]
        assert shared[ada_uid]["dots"]["sharedWithYou"] is True
        fork = call(
            app,
            "POST",
            "/api/forks",
            headers={**cy, "Content-Type": "application/json"},
            body=json.dumps({"publisher": ada_uid, "exp": "dots"}).encode(),
        )
        assert fork.status == 404
        bad = self.put_access(app, ada, ada_uid, {"visibility": "people", "people": ["nobody"]})
        assert bad.status == 400 and "nobody" in bad.json()["error"]["message"]

    def test_finding_people_over_http(self, app: PlatformApp, people):
        ada = self.login(app, "ada")
        found = call(app, "GET", "/api/people", headers=ada, query="q=be").json()
        assert [p["username"] for p in found["people"]] == ["ben"]
        ben_uid = people["ben"].uid
        named = call(app, "GET", "/api/people", headers=ada, query=f"uids={ben_uid},zz").json()
        assert named == {"people": [{"uid": ben_uid, "name": "Ben Franklin", "username": "ben"}]}
        assert call(app, "GET", "/api/people", query="q=be").status == 401
        session = call(app, "GET", "/api/session", headers=ada).json()
        assert session["capabilities"]["shareWithPeople"] is True
        assert call(app, "GET", "/api/session").json()["capabilities"]["shareWithPeople"] is False

    def test_the_local_host_cannot_share_with_people(self, store: FileStore):
        add_version(store, "local", "dots", experiment())
        app = PlatformApp(PlatformService(store), LocalGate(8000, "t" * 43, "Me"))
        token = {"X-Alhazen-Token": "t" * 43, "Content-Type": "application/json"}
        session = call(app, "GET", "/api/session", headers=token).json()
        assert session["capabilities"]["shareWithPeople"] is False
        response = call(
            app,
            "PUT",
            "/api/catalogue/local/dots/access",
            headers=token,
            body=b'{"visibility": "private"}',
        )
        assert response.status == 200 and response.json()["listing"]["canManage"] is True
        assert call(app, "GET", "/api/people", headers=token, query="q=ab").json() == {"people": []}


class TestCommandLine:
    def test_the_operator_shows_and_changes_access(
        self, service: PlatformService, store: FileStore, people, capsys
    ):
        ada, ben = people["ada"].uid, people["ben"].uid
        base = ["platform", "access", ada, "dots", "--store", str(store.root)]
        assert alhazen_main(base) == 0
        assert capsys.readouterr().out.strip() == f"{ada}/dots: public"
        assert alhazen_main([*base, "--visibility", "people", "--people", ben]) == 0
        assert f"{ben} (Ben Franklin)" in capsys.readouterr().out
        assert store.listing(ada, "dots")["access"] == {"visibility": "people", "people": [ben]}
        assert alhazen_main([*base, "--visibility", "private", "--people", ben]) == 1
        assert "goes with --visibility people" in capsys.readouterr().err
        assert alhazen_main([*base, "--visibility", "people"]) == 1
        assert alhazen_main([*base, "--visibility", "people", "--people", "nobody"]) == 1
        assert store.listing(ada, "dots")["access"]["people"] == [ben]
