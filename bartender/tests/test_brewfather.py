import os
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


OWNER_HEADERS = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}


def _load_app_module(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    import importlib
    import bartender.app as app_module

    importlib.reload(app_module)
    app_module.DATA_DIR = tmp_path
    app_module.DATA_FILE = tmp_path / "bartender.json"
    app_module.UPLOADS_DIR = tmp_path / "uploads"
    app_module.UPLOADS_DIR.mkdir(exist_ok=True)
    app_module.save_data(app_module.DEFAULT_DATA)
    app_module.app.config["TESTING"] = True
    return app_module


def _configure(app_module):
    client = app_module.app.test_client()
    response = client.post(
        "/api/settings",
        json={
            "brewfather_enabled": True,
            "brewfather_user_id": "brew-user",
            "brewfather_api_key": "secret-key",
        },
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 200
    return client


def test_brewfather_settings_redact_api_key(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = _configure(app_module)

    response = client.get("/api/settings", headers=OWNER_HEADERS)

    assert response.status_code == 200
    payload = response.get_json()
    assert "brewfather_api_key" not in payload
    assert payload["brewfather_credentials"] == {
        "configured": True,
        "user_id_configured": True,
        "api_key_configured": True,
    }
    assert "secret-key" not in response.get_data(as_text=True)


def test_brewfather_api_key_is_not_exported(tmp_path):
    app_module = _load_app_module(tmp_path)
    _configure(app_module)
    payload = app_module._build_export_json_payload(app_module.load_data())
    exported_settings = payload["data"]["settings"]

    assert "brewfather_api_key" not in exported_settings
    assert exported_settings["brewfather_credentials"]["configured"] is True
    assert "secret-key" not in str(payload)


def test_brewfather_sync_is_idempotent_and_persists_ids(tmp_path, monkeypatch):
    app_module = _load_app_module(tmp_path)
    client = _configure(app_module)

    class FakeClient:
        def __init__(self, user_id, api_key):
            assert user_id == "brew-user"
            assert api_key == "secret-key"

        def fetch_recipes(self):
            return [{"brewfather_recipe_id": "recipe-1", "name": "House IPA", "abv": "6.2"}]

        def fetch_batches(self):
            return [{
                "brewfather_recipe_id": "recipe-1",
                "brewfather_batch_id": "batch-1",
                "name": "House IPA Batch 1",
                "packaged_on": "2026-09-01",
            }]

    monkeypatch.setattr(app_module, "BrewfatherClient", FakeClient)
    first = client.post("/api/brewfather/sync", headers=OWNER_HEADERS)
    second = client.post("/api/brewfather/sync", headers=OWNER_HEADERS)

    assert first.status_code == 200
    assert second.status_code == 200
    beers = app_module.load_data()["beers"]
    assert len(beers) == 1
    assert beers[0]["brewfather_recipe_id"] == "recipe-1"
    assert beers[0]["brewfather_batch_id"] == "batch-1"
    assert second.get_json()["counts"]["beers_created"] == 0


def test_brewfather_conflict_requires_explicit_resolution(tmp_path, monkeypatch):
    app_module = _load_app_module(tmp_path)
    client = _configure(app_module)

    class FakeClient:
        def __init__(self, *args):
            pass

        def fetch_recipes(self):
            return [{"brewfather_recipe_id": "recipe-1", "name": "Original IPA"}]

        def fetch_batches(self):
            return []

    monkeypatch.setattr(app_module, "BrewfatherClient", FakeClient)
    assert client.post("/api/brewfather/sync", headers=OWNER_HEADERS).status_code == 200
    beer = app_module.load_data()["beers"][0]
    beer["name"] = "Local IPA"
    app_module.save_data(app_module.load_data())
    data = app_module.load_data()
    data["beers"][0]["name"] = "Local IPA"
    app_module.save_data(data)

    FakeClient.fetch_recipes = lambda self: [{"brewfather_recipe_id": "recipe-1", "name": "Remote IPA"}]
    response = client.post("/api/brewfather/sync", headers=OWNER_HEADERS)
    assert response.status_code == 200
    conflict = response.get_json()["conflicts"][0]

    unresolved = client.post(
        f"/api/brewfather/conflicts/{conflict['id']}/resolve",
        json={"resolution": "brewfather"},
        headers=OWNER_HEADERS,
    )
    assert unresolved.status_code == 200
    assert unresolved.get_json()["beer"]["name"] == "Remote IPA"


def test_brewfather_keg_import_requires_confirmation(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = _configure(app_module)
    data = app_module.load_data()
    data["beers"] = [{
        "id": 1,
        "name": "Batch IPA",
        "packaging": "kegged",
        "brewfather_batch_id": "batch-1",
        "brewfather_recipe_id": "recipe-1",
    }]
    app_module.save_data(data)

    rejected = client.post(
        "/api/brewfather/batches/batch-1/import-keg",
        json={"confirm": False},
        headers=OWNER_HEADERS,
    )
    accepted = client.post(
        "/api/brewfather/batches/batch-1/import-keg",
        json={"confirm": True, "name": "Batch IPA Keg", "size": "Corny (5 gal)"},
        headers=OWNER_HEADERS,
    )

    assert rejected.status_code == 400
    assert accepted.status_code == 201
    assert app_module.load_data()["kegs"][0]["beer_id"] == 1


def test_brewfather_rate_limit_uses_retry_after():
    from bartender.pos_sync.brewfather import BrewfatherClient, BrewfatherError, normalize_recipe

    calls = []
    def opener(request, timeout):
        calls.append(request)
        headers = {"Retry-After": "7"}
        raise HTTPError(request.full_url, 429, "rate limit", headers, None)

    delays = []
    client = BrewfatherClient("user", "key", opener=opener, sleeper=delays.append, max_retries=1)
    with pytest.raises(BrewfatherError) as error:
        client.fetch_recipes()
    assert error.value.status_code == 429
    assert "7 seconds" in error.value.hint
    assert len(calls) == 2
    assert delays == [7]
    assert normalize_recipe({"style": {"name": "American IPA"}})["style_guideline"] == "American IPA"
