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
            "current_volume": 5,
            "volume_unit": "gal",
        },
        {
            "id": 2,
            "name": "Keg Two",
            "status": "full",
            "size": "5 gal",
            "percent_full": 100,
            "current_volume": 5,
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


def test_pours_are_recorded_in_analytics_and_audit_trails(tmp_path):
    app_module = _load_app_module(tmp_path)
    _seed_taps_and_kegs(app_module)
    client = app_module.app.test_client()
    staff_headers = {
        "X-BarTender-User-Id": "staff-1",
        "X-BarTender-Role": "staff",
        "X-BarTender-Name": "Staff One",
    }

    keg_pour = client.post(
        "/api/kegs/1/pour",
        json={"amount": 64, "unit": "oz", "preset_name": "Growler"},
        headers=staff_headers,
    )
    assert keg_pour.status_code == 200

    tap_pour = client.post(
        "/api/taps/1/pour",
        json={"amount": 32, "unit": "oz", "preset_name": "Growler"},
        headers=staff_headers,
    )
    assert tap_pour.status_code == 200

    data = app_module.load_data()
    assert len(data["pour_events"]) == 2
    assert [entry["action"] for entry in data["team_audit"]] == [
        "pour_recorded",
        "pour_recorded",
    ]
    assert data["team_audit"][0]["actor_name"] == "Staff One"
    assert data["team_audit"][0]["details"]["source"] == "tap"
    assert data["team_audit"][1]["details"]["source"] == "keg"


def test_keg_extended_fields_persist_and_update(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    create_res = client.post(
        "/api/kegs",
        json={
            "name": "Keg 10",
            "serial_number": "SN-9988",
            "coupler_type": "Sankey D (US)",
            "ownership_type": "Owned",
            "location": "Cold Room A",
            "serving_psi": "12",
            "gas_type": "CO2 (100%)",
            "status": "empty",
            "size": "Corny (5 gal)",
            "cleaned_date": "2026-09-01",
        },
        headers=headers,
    )
    assert create_res.status_code == 201
    keg = create_res.get_json()
    assert keg["serial_number"] == "SN-9988"
    assert keg["coupler_type"] == "Sankey D (US)"
    assert keg["ownership_type"] == "Owned"
    assert keg["location"] == "Cold Room A"
    assert keg["serving_psi"] == "12"
    assert keg["gas_type"] == "CO2 (100%)"
    assert keg["cleaned_date"] == "2026-09-01"

    update_res = client.put(
        f"/api/kegs/{keg['id']}",
        json={
            "location": "Walk-in Cooler 2",
            "serving_psi": "14",
            "kicked_date": "2026-09-07",
        },
        headers=headers,
    )
    assert update_res.status_code == 200
    updated = update_res.get_json()
    assert updated["location"] == "Walk-in Cooler 2"
    assert updated["serving_psi"] == "14"
    assert updated["kicked_date"] == "2026-09-07"
    assert updated["serial_number"] == "SN-9988"


def test_tap_extended_fields_persist_and_update(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    create_res = client.post(
        "/api/taps",
        json={
            "number": 1,
            "label": "Left Nitro",
            "location": "Main Bar Tower",
            "status": "active",
            "tap_handle": "Nitro Handle",
            "faucet_type": "Stout / Nitro",
            "line_length_feet": "12",
            "line_inner_diameter": "3/16\" ID",
            "line_material": "Barrier / EVABarrier",
            "target_pressure_psi": "30",
            "target_temperature": "38°F",
            "clean_interval_days": 14,
            "last_cleaned_date": "2026-09-01",
            "last_serviced_date": "2026-08-01",
        },
        headers=headers,
    )
    assert create_res.status_code == 201
    tap = create_res.get_json()
    assert tap["location"] == "Main Bar Tower"
    assert tap["status"] == "active"
    assert tap["tap_handle"] == "Nitro Handle"
    assert tap["faucet_type"] == "Stout / Nitro"
    assert tap["line_length_feet"] == "12"
    assert tap["line_inner_diameter"] == "3/16\" ID"
    assert tap["line_material"] == "Barrier / EVABarrier"
    assert tap["target_pressure_psi"] == "30"
    assert tap["target_temperature"] == "38°F"
    assert tap["clean_interval_days"] == 14
    assert tap["last_cleaned_date"] == "2026-09-01"
    assert tap["last_serviced_date"] == "2026-08-01"

    update_res = client.put(
        f"/api/taps/{tap['id']}",
        json={
            "status": "cleaning",
            "last_cleaned_date": "2026-09-07",
            "target_pressure_psi": "32",
        },
        headers=headers,
    )
    assert update_res.status_code == 200
    updated = update_res.get_json()
    assert updated["status"] == "cleaning"
    assert updated["last_cleaned_date"] == "2026-09-07"
    assert updated["target_pressure_psi"] == "32"
    assert updated["faucet_type"] == "Stout / Nitro"


def test_taps_page_renders_location_and_line_fields_only_in_pro_mode(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    # Default Homebrewer mode
    homebrewer_res = client.get("/taps")
    assert homebrewer_res.status_code == 200
    homebrewer_html = homebrewer_res.get_data(as_text=True)
    assert 'id="tapLocation"' not in homebrewer_html
    assert 'id="tapLineLengthFeet"' not in homebrewer_html
    assert 'id="tapLineInnerDiameter"' not in homebrewer_html
    assert 'id="tapLineMaterial"' not in homebrewer_html
    assert 'id="tapLastCleanedDate"' not in homebrewer_html

    # Pro mode
    data = app_module.load_data()
    data["settings"]["brewery_type"] = "pro"
    app_module.save_data(data)

    pro_res = client.get("/taps")
    assert pro_res.status_code == 200
    pro_html = pro_res.get_data(as_text=True)
    assert 'id="tapLocation"' in pro_html
    assert 'id="tapLineLengthFeet"' in pro_html
    assert 'id="tapLineInnerDiameter"' in pro_html
    assert 'id="tapLineMaterial"' in pro_html
    assert 'id="tapLastCleanedDate"' in pro_html
