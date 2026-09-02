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
