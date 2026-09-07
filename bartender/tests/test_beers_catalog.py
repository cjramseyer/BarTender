import json
import io
import os
import sys
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
    return app_module


def test_beer_search_assist_filters_by_name_and_brewery(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    app_module.save_data({
        **app_module.DEFAULT_DATA,
        "beers": [
            {
                "id": 1,
                "name": "Hazy IPA",
                "type": "IPA",
                "packaging": "kegged",
                "brewer": "North Pole",
                "brewery": "Drift House",
                "abv": "6.4",
                "ibu": "42",
                "brewed_on": "2024-01-02",
                "notes": "Citrusy",
            },
            {
                "id": 2,
                "name": "Session Lager",
                "type": "Lager",
                "packaging": "kegged",
                "brewer": "Jack",
                "brewery": "West End",
                "abv": "4.8",
                "ibu": "18",
                "brewed_on": "2024-02-03",
                "notes": "Clean",
            },
        ],
    })

    response = client.get("/api/beers/search?q=drift")
    assert response.status_code == 200
    payload = response.get_json()
    assert any(item["name"] == "Hazy IPA" for item in payload)
    assert not any(item["name"] == "Session Lager" for item in payload)


def test_beer_csv_preview_validates_rows_before_apply(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    csv_payload = (
        "name,type,packaging,brewer,brewery,abv,ibu,brewed_on,notes\n"
        "Hazy IPA,IPA,kegged,North Pole,Drift House,6.4,42,2024-01-02,Citrusy\n"
        ",Lager,kegged,West End,Example,5.2,20,2024-02-03,Missing name\n"
    ).encode("utf-8")

    response = client.post(
        "/api/beers/import/csv/preview",
        data={"file": (io.BytesIO(csv_payload), "beers.csv")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["summary"]["beers"] == 1
    assert payload["errors"]


def test_beer_details_round_trip_through_api_and_csv(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    beer_details = {
        "name": "Hazy IPA",
        "type": "IPA",
        "style_guideline": "BJCP 21C",
        "packaged_on": "2026-09-01",
        "best_by_date": "2026-12-01",
        "availability_status": "seasonal",
        "description": "Citrus-forward hazy.",
        "allergens": ["Lactose", "Wheat"],
        "color_srm": "6",
        "color_ebc": "12",
        "serving_temperature": "38-42 F",
        "glassware": "Pint",
        "supplier": "North Pole Supply",
        "distributor": "Local Distribution",
        "sku": "IPA-001",
        "upc": "012345678901",
        "recipe_url": "https://example.com/recipe",
    }

    create_response = client.post("/api/beers", json=beer_details)
    assert create_response.status_code == 201
    created = create_response.get_json()
    for key, value in beer_details.items():
        assert created[key] == value

    csv_payload = (
        ",".join(app_module.BEER_CSV_HEADER)
        + "\n"
        + "CSV Lager,Lager,BJCP 1A,kegged,CSV Brewer,CSV Brewery,4.8,18,2026-08-01,2026-08-15,2026-11-15,available,Crisp lager,Barley,3,6,36-40 F,Pilsner,CSV Supply,CSV Distribution,LGR-001,098765432109,https://example.com/lager,Notes\n"
    ).encode("utf-8")
    import_response = client.post(
        "/api/beers/import/csv",
        data={"file": (io.BytesIO(csv_payload), "beers.csv")},
        content_type="multipart/form-data",
    )
    assert import_response.status_code == 200

    imported = next(beer for beer in app_module.load_data()["beers"] if beer["name"] == "CSV Lager")
    assert imported["allergens"] == ["Barley"]
    assert imported["recipe_url"] == "https://example.com/lager"


def test_beers_page_renders_supplier_and_distributor_datalists(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    app_module.save_data({
        **app_module.DEFAULT_DATA,
        "beers": [
            {
                "id": 1,
                "name": "Hazy IPA",
                "type": "IPA",
                "supplier": "Acme Hops & Grain",
                "distributor": "Metro Beverage Distributing",
            },
        ],
    })

    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get("/beers")
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert 'id="beerSupplierOptions"' in html
    assert 'id="beerDistributorOptions"' in html
    assert '<option value="Acme Hops &amp; Grain">' in html
    assert '<option value="Metro Beverage Distributing">' in html
    assert 'list="beerSupplierOptions"' in html
    assert 'list="beerDistributorOptions"' in html
    assert 'name="beer_allergen_choice"' in html
    assert 'value="Barley"' in html
    assert 'value="Wheat"' in html
    assert 'value="Lactose"' in html
