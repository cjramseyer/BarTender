import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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
    app_module.app.config["SECRET_KEY"] = "test-secret"
    return app_module


def _seed_taps_and_kegs(app_module):
    now = datetime.now(timezone.utc).isoformat()
    data = app_module.load_data()
    data["kegs"] = [
        {
            "id": 1,
            "name": "Keg One",
            "status": "full",
            "size": "5 gal",
            "percent_full": 100,
            "current_volume": None,
            "volume_unit": "gal",
        },
        {
            "id": 2,
            "name": "Keg Two",
            "status": "full",
            "size": "5 gal",
            "percent_full": 100,
            "current_volume": None,
            "volume_unit": "gal",
        },
    ]
    data["taps"] = [
        {
            "id": 1,
            "number": 1,
            "label": "Tap 1",
            "keg_id": 1,
            "ever_assigned_keg": True,
            "notes": "",
            "updated_at": now,
        },
        {
            "id": 2,
            "number": 2,
            "label": "Tap 2",
            "keg_id": None,
            "ever_assigned_keg": False,
            "notes": "",
            "updated_at": now,
        },
    ]
    app_module.save_data(data)


def test_taps_page_copy_uses_un_used_wording(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get("/taps", follow_redirects=False)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Only full / un-used kegs shown." in body


def test_api_add_tap_rejects_keg_already_connected(tmp_path):
    app_module = _load_app_module(tmp_path)
    _seed_taps_and_kegs(app_module)
    client = app_module.app.test_client()

    response = client.post(
        "/api/taps",
        json={"number": 3, "label": "Tap 3", "keg_id": 1, "notes": ""},
    )

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["code"] == "KEG_ALREADY_CONNECTED"
    assert payload["tap_id"] == 1


def test_api_update_tap_rejects_keg_already_connected_to_other_tap(tmp_path):
    app_module = _load_app_module(tmp_path)
    _seed_taps_and_kegs(app_module)
    client = app_module.app.test_client()

    response = client.put(
        "/api/taps/2",
        json={"keg_id": 1},
    )

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["code"] == "KEG_ALREADY_CONNECTED"
    assert payload["tap_number"] == 1


def test_api_update_tap_allows_keeping_same_connected_keg(tmp_path):
    app_module = _load_app_module(tmp_path)
    _seed_taps_and_kegs(app_module)
    client = app_module.app.test_client()

    response = client.put(
        "/api/taps/1",
        json={"keg_id": 1},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["keg_id"] == 1
