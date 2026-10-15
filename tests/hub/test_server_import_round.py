"""Import round (2026-10-09), decision 3, on the central hub: a release
whose version differs from its pyproject (protocol) version is accepted and
keeps both. See tests/hub/test_import_round.py for the rest."""

from __future__ import annotations

from tests.hub.test_import_round import META, _source

from alhazen.hub import packages
from alhazen.hub.source import pack


def test_the_hub_accepts_and_shows_both_versions(tmp_path, hub):
    hub.register("sharif")
    owner = hub.browser("sharif")
    checkout = _source(tmp_path / "attention", "attention-clamp", "0.2.0")
    bundle = tmp_path / "ac.zip"
    pack(
        checkout,
        packages,
        bundle,
        {**META, "name": "attention-clamp", "version": "0.2.1"},
        packages.suggest_files(checkout),
    )
    experiment = owner.create_experiment()
    response = owner.upload_version(experiment["id"], bundle)
    assert response.status_code == 201, response.text
    version = response.json()["version"]
    assert version["version"] == "0.2.1"
    assert version["manifest"]["protocol_version"] == "0.2.0"
    listed = owner.get(f"/experiments/{experiment['id']}").json()["versions"]
    assert [v["manifest"].get("protocol_version") for v in listed] == ["0.2.0"]
