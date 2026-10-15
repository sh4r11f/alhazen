"""The shared upload identity (alhazen.hub.protocol): the rig's digest equals
the hub service's own for the same upload (when the service's optional
dependencies are installed), and a receipt certifies exactly one bound upload."""

from __future__ import annotations

import types

import pytest

from alhazen.hub import protocol

FILES = [
    {"path": "z/trials.csv", "size": 12, "sha256": "b" * 64},
    {"path": "session.json", "size": 3, "sha256": "a" * 64},
    # An empty file declares the empty digest (the hub refuses anything else).
    {
        "path": "figures/émotion naïve.png",
        "size": 0,
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    },
]
META = {"subject_code": "01", "mode": "run", "rig_alias": "läb", "started_at": None}


def server_digest(body: dict) -> str:
    """The hub service's digest of an init body, through its own parser."""
    pytest.importorskip("sqlalchemy", reason="the hub service extra is not installed")
    from alhazen.hub import uploads

    limits = types.SimpleNamespace(max_session_files=10_000, max_session_bytes=10**12)
    hub = types.SimpleNamespace(settings=types.SimpleNamespace(limits=limits))
    return uploads.manifest_digest(uploads.parse_init(hub, body))


@pytest.mark.parametrize(
    "metadata",
    [META, {}, {"subject_code": "07"}, {"mode": "simulate", "rig_alias": None}],
)
def test_rig_and_hub_compute_the_same_digest(metadata):
    body = {
        "experiment_id": "e1",
        "version_id": "v1",
        "client_session_id": "rig-abc",
        "files": FILES,
        "metadata": metadata,
        "consent": True,
    }
    ours = protocol.manifest_sha256("e1", "v1", FILES, metadata)
    assert ours == server_digest(body)


def test_digest_ignores_order_and_missing_keys_but_not_content():
    base = protocol.manifest_sha256("e1", "v1", FILES, {"subject_code": "01"})
    assert base == protocol.manifest_sha256(
        "e1", "v1", list(reversed(FILES)), {**protocol.canonical_metadata({"subject_code": "01"})}
    )
    changed = [dict(FILES[0], size=13), *FILES[1:]]
    assert base != protocol.manifest_sha256("e1", "v1", changed, {"subject_code": "01"})
    assert base != protocol.manifest_sha256("e1", "v2", FILES, {"subject_code": "01"})
    assert base != protocol.manifest_sha256("e1", "v1", FILES, {"subject_code": "02"})


def test_unusable_metadata_becomes_null():
    clean = protocol.usable_metadata(
        {"subject_code": "x" * 65, "mode": "run", "rig_alias": "a\nb", "started_at": 5}
    )
    assert clean == {"subject_code": None, "mode": "run", "rig_alias": None, "started_at": None}


def good_receipt() -> dict:
    return {
        "id": "s1",
        "status": "committed",
        "client_session_id": "rig-abc",
        "experiment_id": "e1",
        "version_id": "v1",
        "file_count": 3,
        "total_bytes": 15,
        "manifest_sha256": protocol.manifest_sha256("e1", "v1", FILES, META),
    }


def check(receipt):
    return protocol.receipt_problems(
        receipt,
        session_id="s1",
        client_session_id="rig-abc",
        experiment_id="e1",
        version_id="v1",
        files=FILES,
        metadata=META,
    )


def test_a_matching_receipt_passes():
    assert check(good_receipt()) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "s2"),
        ("status", "sealing"),
        ("client_session_id", "rig-other"),
        ("experiment_id", "e2"),
        ("version_id", "v2"),
        ("file_count", 2),
        ("file_count", True),
        ("total_bytes", 14),
        ("total_bytes", "15"),
        ("manifest_sha256", "0" * 64),
    ],
)
def test_every_identity_field_is_checked(field, value):
    receipt = {**good_receipt(), field: value}
    problems = check(receipt)
    assert problems and problems[0].startswith(field)


def test_a_missing_field_or_non_object_is_refused():
    receipt = good_receipt()
    del receipt["manifest_sha256"]
    assert check(receipt)
    assert check(["not", "a", "receipt"]) == ["the receipt is not an object"]
