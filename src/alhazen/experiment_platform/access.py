"""Who can see a listing: ``access: {visibility, people}`` (design.md section 5a).

The decision this module hides is the visibility rule itself, so the service, a refresh and
the operator's command apply one rule: ``private`` (the publisher), ``people`` (the
publisher and the account ids in ``people``) or ``public`` (anyone who may read the
catalogue). A listing with no ``access`` was written before 3.7 and is public.

``normalize`` checks an access value's shape only (ids, duplicates, the publisher, the
limits); whether each person is an existing, enabled account is the caller's question,
since only a host with accounts can answer it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from alhazen.experiment_platform.errors import InvalidRequest
from alhazen.experiment_platform.safety import is_id

PRIVATE = "private"
PEOPLE = "people"
PUBLIC = "public"
VISIBILITIES = (PRIVATE, PEOPLE, PUBLIC)
MAX_PEOPLE = 200
# The raw list a request may name before duplicates are removed: a bound on the work a
# request can ask for, well above any honest list.
MAX_RAW_PEOPLE = 1000

PUBLIC_ACCESS: dict[str, Any] = {"visibility": PUBLIC}


def of(listing: Mapping[str, Any]) -> dict[str, Any]:
    """The listing's stored access, ``public`` when it has none (written before 3.7).
    A stored value this version does not understand is treated as private: an unknown
    rule must never widen who sees a listing."""
    raw = listing.get("access")
    if raw is None:
        return dict(PUBLIC_ACCESS)
    if not isinstance(raw, Mapping) or raw.get("visibility") not in VISIBILITIES:
        return {"visibility": PRIVATE}
    if raw["visibility"] == PEOPLE:
        people = raw.get("people")
        return {
            "visibility": PEOPLE,
            "people": [p for p in people if isinstance(p, str)] if isinstance(people, list) else [],
        }
    return {"visibility": raw["visibility"]}


def can_see(listing: Mapping[str, Any], publisher: str, uid: str | None) -> bool:
    """True when the principal ``uid`` (None: an anonymous visitor) may see the listing,
    given that they may read the catalogue at all."""
    access = of(listing)
    if access["visibility"] == PUBLIC:
        return True
    if uid is None:
        return False
    if uid == publisher:
        return True
    return access["visibility"] == PEOPLE and uid in access["people"]


def normalize(raw: Any, publisher: str) -> dict[str, Any]:
    """A clean access value from a request or the command line, or InvalidRequest saying
    what to change. ``people`` is de-duplicated (order kept), the publisher is removed, and
    it is kept only when the visibility is ``people``."""
    if not isinstance(raw, Mapping):
        raise InvalidRequest("Say who can see this: {visibility, people}")
    visibility = raw.get("visibility")
    if visibility not in VISIBILITIES:
        raise InvalidRequest("Visibility is one of private, people or public")
    if visibility != PEOPLE:
        return {"visibility": visibility}
    given = raw.get("people")
    if given is None:
        given = []
    if not isinstance(given, list) or not all(isinstance(p, str) for p in given):
        raise InvalidRequest("People is a list of account ids")
    if len(given) > MAX_RAW_PEOPLE:
        raise InvalidRequest(f"Share with at most {MAX_PEOPLE} people")
    people: list[str] = []
    for uid in (p.strip() for p in given):
        if not is_id(uid):
            raise InvalidRequest(f"{uid[:80]!r} is not an account id")
        if uid != publisher and uid not in people:
            people.append(uid)
    if not people:
        raise InvalidRequest("Add at least one person, or choose Only you")
    if len(people) > MAX_PEOPLE:
        raise InvalidRequest(f"Share with at most {MAX_PEOPLE} people")
    return {"visibility": PEOPLE, "people": people}


def present(
    listing: Mapping[str, Any],
    publisher: str,
    uid: str | None,
    names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The ``access`` field (and the flags beside it) as the principal ``uid`` sees it:
    the publisher sees it in full with ``names`` and ``canManage``; anyone else sees the
    visibility only, and ``sharedWithYou`` when they are one of the people."""
    access = of(listing)
    if uid is not None and uid == publisher:
        people = access.get("people", [])
        return {
            "access": {
                "visibility": access["visibility"],
                "people": list(people),
                "names": {p: (names or {})[p] for p in people if p in (names or {})},
            },
            "canManage": True,
        }
    shown: dict[str, Any] = {"access": {"visibility": access["visibility"]}, "canManage": False}
    if access["visibility"] == PEOPLE and uid is not None and uid in access["people"]:
        shown["sharedWithYou"] = True
    return shown
