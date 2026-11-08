import copy
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bartender.drink_recipes import classic_recipes, guest_drinks, normalize_recipes, recipe_availability, shopping_list, shopping_list_by_drink, validate_recipe
from bartender.bar_stock import public_bar_stock, stock_is_public


class _RenderedHTML(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self._active = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag in ("a", "option", "span"):
            self._active.append({"tag": tag, "attrs": dict(attrs), "chunks": []})

    def handle_data(self, data):
        for element in self._active:
            element["chunks"].append(data)

    def handle_endtag(self, tag):
        for index in range(len(self._active) - 1, -1, -1):
            if self._active[index]["tag"] == tag:
                element = self._active.pop(index)
                self.elements.append({
                    "tag": tag,
                    "attrs": element["attrs"],
                    "text": " ".join("".join(element["chunks"]).split()),
                })
                break


def _recipe():
    return {
        "name": "Negroni", "method": "stir", "category": "classic",
        "steps": ["Stir with ice.", "Strain over fresh ice."],
        "ingredients": [
            {"name": "Gin", "amount": 30, "unit": "ml", "stock_id": 1},
            {"name": "Campari", "amount": 30, "unit": "ml", "stock_id": 2},
            {"name": "Sweet vermouth", "amount": 30, "unit": "ml", "stock_id": 3},
        ],
    }


def test_live_stock_availability_and_optional_garnish():
    recipe = validate_recipe(_recipe())
    recipe["ingredients"].append({"name": "Orange peel", "amount": 1, "unit": "piece", "stock_id": None, "optional": True})
    original = copy.deepcopy(recipe)
    stock = [{"id": 1, "name": "Gin", "quantity": 1}, {"id": 2, "quantity": 0}]
    result = recipe_availability(recipe, stock)
    assert not result["can_make"]
    assert result["missing_ingredients"] == ["Campari", "Sweet vermouth"]
    assert [item["availability"] for item in result["ingredients"]] == ["in_stock", "out_of_stock", "missing_stock", "not_linked"]
    stock[1]["quantity"] = 1
    stock.append({"id": 3, "quantity": 1})
    assert recipe_availability(recipe, stock)["can_make"]
    assert recipe == original


@pytest.mark.parametrize("quantity", [None, "invalid", float("nan"), float("inf"), True])
def test_unknown_stock_quantity_does_not_claim_available(quantity):
    result = recipe_availability(validate_recipe(_recipe()), [{"id": 1, "quantity": quantity}])
    assert result["ingredients"][0]["availability"] == "unknown"
    assert not result["can_make"]


def test_disabled_stock_is_unknown():
    result = recipe_availability(validate_recipe(_recipe()), [], False)
    assert result["unknown_ingredients"] == ["Gin", "Campari", "Sweet vermouth"]
    assert not result["can_make"]


@pytest.mark.parametrize("field,value", [("name", ""), ("method", "invalid"), ("steps", []), ("ingredients", []), ("source_url", "javascript:alert(1)"), ("menu_visible", "yes")])
def test_invalid_recipe_fields(field, value):
    recipe = _recipe()
    recipe[field] = value
    with pytest.raises(ValueError):
        validate_recipe(recipe)


@pytest.mark.parametrize("field,value", [("amount", 0), ("amount", True), ("amount", float("inf")), ("amount", 10 ** 400), ("unit", "bottle"), ("stock_id", "1"), ("stock_id", True), ("optional", "false")])
def test_invalid_ingredient_fields(field, value):
    recipe = _recipe()
    recipe["ingredients"][0][field] = value
    with pytest.raises(ValueError):
        validate_recipe(recipe)


def test_recipe_preserves_traditional_units_and_partial_updates():
    recipe = _recipe()
    recipe["ingredients"][0].update(amount=2, unit="dash")
    validated = validate_recipe(recipe)
    assert validate_recipe({"name": "House Negroni"}, validated)["ingredients"][0]["unit"] == "dash"


def _load_app_module(tmp_path):
    import importlib
    import os

    os.environ["DATA_DIR"] = str(tmp_path)
    import bartender.app as app_module

    importlib.reload(app_module)
    app_module.DATA_DIR = tmp_path
    app_module.DATA_FILE = tmp_path / "bartender.json"
    app_module.UPLOADS_DIR = tmp_path / "uploads"
    app_module.UPLOADS_DIR.mkdir(exist_ok=True)
    app_module.save_data({
        **app_module.DEFAULT_DATA,
        "settings": {**app_module.DEFAULT_DATA["settings"], "brewery_type": "pro"},
    })
    app_module.app.config["TESTING"] = True
    return app_module


def _seed_stock(client):
    for name in ("Gin", "Campari", "Sweet vermouth"):
        assert client.post("/api/stock", json={"name": name, "quantity": 1}).status_code == 201


def test_recipe_api_crud_stock_links_filters_and_audit(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    _seed_stock(client)
    created = client.post("/api/drinks", json=_recipe())
    assert created.status_code == 201
    recipe = created.get_json()
    assert recipe["can_make"]
    assert recipe["revision"] == 1
    assert client.get("/api/drinks?can_make=true&q=gin&category=classic&method=stir").get_json()[0]["id"] == recipe["id"]
    assert client.get("/api/drinks?q=vodka").get_json() == []
    assert client.put("/api/stock/2", json={"quantity": 0}).status_code == 200
    assert client.get("/api/drinks?can_make=true").get_json() == []
    assert client.get(f"/api/drinks/{recipe['id']}").get_json()["missing_ingredients"] == ["Campari"]
    updated = client.put(f"/api/drinks/{recipe['id']}", json={"description": "Bitter and balanced"})
    assert updated.get_json()["revision"] == 2
    assert client.delete(f"/api/drinks/{recipe['id']}").status_code == 200
    assert client.get(f"/api/drinks/{recipe['id']}").status_code == 404
    assert [event["action"] for event in app_module.load_data()["team_audit"] if event["action"].startswith("drink_")] == ["drink_deleted", "drink_updated", "drink_created"]


def test_deleted_stock_does_not_relink_when_id_is_reused(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    _seed_stock(client)
    client.post("/api/drinks", json=_recipe())
    client.delete("/api/stock/3")
    client.post("/api/stock", json={"name": "Vodka", "quantity": 1})
    result = client.get("/api/drinks/1").get_json()
    assert result["ingredients"][2]["stock_id"] is None
    assert result["ingredients"][2]["availability"] == "not_linked"
    assert not result["can_make"]


def test_recipe_api_permissions_and_invalid_links(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    _seed_stock(client)
    assert client.post("/api/drinks", json=_recipe()).status_code == 201
    staff = {"X-BarTender-Role": "staff", "X-BarTender-User-Id": "staff-1"}
    assert client.get("/api/drinks", headers=staff).status_code == 200
    for method, path in (("post", "/api/drinks"), ("put", "/api/drinks/1"), ("delete", "/api/drinks/1")):
        assert getattr(client, method)(path, json=_recipe(), headers=staff).status_code == 403
    assert client.put("/api/drinks/1", json={"name": "House Negroni"}, headers={"X-BarTender-Role": "manager"}).status_code == 200
    invalid = _recipe()
    invalid["ingredients"][0]["stock_id"] = 999
    assert client.post("/api/drinks", json=invalid).status_code == 400
    assert client.post("/api/drinks", json=[]).status_code == 400
    assert client.put("/api/drinks/999", json=_recipe()).status_code == 404


@pytest.mark.parametrize("archive", [False, True])
def test_recipes_backup_replace_merge_and_preview(tmp_path, archive):
    import io
    import zipfile

    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    _seed_stock(client)
    client.post("/api/drinks", json=_recipe())
    if archive:
        exported = client.get("/api/export/archive").data
        with zipfile.ZipFile(io.BytesIO(exported)) as backup:
            assert "drink_recipes.json" in backup.namelist()
        def restore(mode, preview=False):
            return client.post("/api/import/archive" + ("/preview" if preview else ""), data={"mode": mode, "file": (io.BytesIO(exported), "backup.zip")})
    else:
        exported = client.get("/api/export/json").get_json()
        def restore(mode, preview=False):
            return client.post("/api/import/json" + ("/preview" if preview else "") + f"?mode={mode}", json=exported)
    client.put("/api/drinks/1", json={"name": "Changed"})
    assert restore("merge", True).get_json()["summary"]["drink_recipes"] == 1
    assert client.get("/api/drinks/1").get_json()["name"] == "Changed"
    assert restore("merge").status_code == 200
    assert len(client.get("/api/drinks").get_json()) == 1
    assert client.get("/api/drinks/1").get_json()["name"] == "Negroni"
    assert restore("replace").status_code == 200
    assert client.get("/api/drinks/1").get_json()["can_make"]
    assert client.post("/api/import/json", json={"bar_stock": []}).status_code == 200
    assert app_module.load_data()["drink_recipes"] == []
    assert client.get("/api/drinks").status_code == 403


def test_bartender_page_and_guest_menus_hide_private_fields(tmp_path, monkeypatch):
    import bartender.display as display_module

    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    _seed_stock(client)
    recipe = {**_recipe(), "menu_visible": True, "notes": "PRIVATE STAFF NOTE", "description": "Bitter aperitif", "price": "$12", "allergens": "Sulphites"}
    assert client.post("/api/drinks", json=recipe).status_code == 201
    client.post("/api/drinks", json={**_recipe(), "name": "Secret House Drink"})
    client.post("/api/drinks", json={**_recipe(), "name": "Lime Soda", "category": "nonalcoholic", "menu_visible": True})
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    page = client.get("/drinks")
    assert page.status_code == 200
    assert "PRIVATE STAFF NOTE" in page.get_data(as_text=True)
    assert "In stock" in page.get_data(as_text=True)
    assert 'id="drinkForm"' in page.get_data(as_text=True)
    monkeypatch.setattr(display_module, "DATA_FILE", app_module.DATA_FILE)
    for menu_client in (client, display_module.display_app.test_client()):
        html = menu_client.get("/menu").get_data(as_text=True)
        assert "Cocktails" in html and "Nonalcoholic Drinks" in html
        assert "Negroni" in html and "$12" in html and "Sulphites" in html
        assert "Secret House Drink" not in html and "PRIVATE STAFF NOTE" not in html
        assert "Stir with ice." not in html
        assert html.index("Cocktails") < html.index('class="actions"' if menu_client is not client else 'class="menu-screen-actions no-print"')
    with client.session_transaction() as session:
        session["user_role"] = "staff"
    staff_page = client.get("/drinks").get_data(as_text=True)
    assert 'id="drinkForm"' not in staff_page
    assert "PRIVATE STAFF NOTE" in staff_page


def test_import_normalization_and_guest_availability():
    raw = [{**_recipe(), "id": 1, "revision": "invalid"}, {**_recipe(), "id": 1}, {"name": "Invalid recipe"}, None]
    recipes = normalize_recipes(raw)
    assert len(recipes) == 2
    assert len({recipe["id"] for recipe in recipes}) == 2
    assert recipes[0]["revision"] == 1
    recipes[0]["menu_visible"] = True
    data = {"drink_recipes": recipes, "bar_stock": [], "settings": {"bar_stock_enabled": False, "brewery_type": "pro"}}
    assert guest_drinks(data)[0]["availability"] == "unknown"
    data["settings"]["bar_stock_enabled"] = True
    assert guest_drinks(data)[0]["availability"] == "unavailable"
    assert "ingredients" not in guest_drinks(data)[0]


def test_recipe_api_requires_login_outside_test_mode(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    _seed_stock(client)
    client.post("/api/drinks", json=_recipe())
    app_module.app.config["TESTING"] = False
    assert client.get("/api/drinks").status_code == 401
    with client.session_transaction() as session:
        session["user_id"] = "staff-1"
        session["user_role"] = "staff"
        session["user_name"] = "Staff"
    assert client.get("/api/drinks").status_code == 200
    assert client.put("/api/drinks/1", json={"name": "Spoofed"}, headers={"X-BarTender-Role": "owner"}).status_code == 403


def test_dashboard_omits_stock_card_without_disabling_stock(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    _seed_stock(client)
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Manage Stock" not in html
    assert "Bar Stock (" not in html
    assert 'href="/stock">Bar Stock</a>' in html
    assert client.get("/stock").status_code == 200
    assert len(client.get("/api/stock").get_json()) == 3


def test_shopping_list_combines_saved_recipes_and_updates_with_stock():
    first = {**_recipe(), "id": 1, "menu_visible": False}
    second = {**_recipe(), "id": 2, "name": "House Negroni", "menu_visible": True}
    second["ingredients"].append({"name": "Orange peel", "amount": 1, "unit": "piece", "optional": True})
    data = {
        "drink_recipes": [first, second],
        "bar_stock": [{"id": 1, "name": "Gin", "quantity": 1}, {"id": 2, "name": "Campari", "quantity": 0}],
        "settings": {},
    }
    original = copy.deepcopy(data)
    result = shopping_list(data)
    assert [item["name"] for item in result] == ["Campari", "Sweet vermouth", "Orange peel"]
    assert [item["availability"] for item in result] == ["out_of_stock", "missing_stock", "not_linked"]
    assert len(result[0]["recipes"]) == 2
    assert result[2]["optional"]
    assert data == original
    data["bar_stock"][1]["quantity"] = 1
    assert "Campari" not in [item["name"] for item in shopping_list(data)]
    data["drink_recipes"] = []
    assert shopping_list(data) == []


def test_shopping_list_deduplicates_names_and_recipe_uses():
    recipe = {**_recipe(), "id": 1}
    recipe["ingredients"] = [
        {"name": "Lime juice", "amount": 20, "unit": "ml", "optional": True},
        {"name": " lime  JUICE ", "amount": 10, "unit": "ml"},
    ]
    result = shopping_list({"drink_recipes": [recipe]})
    assert len(result) == 1
    assert not result[0]["optional"]
    assert result[0]["recipes"] == [{"id": 1, "name": "Negroni", "optional": False}]


def test_shopping_list_does_not_guess_unknown_or_disabled_stock():
    data = {"drink_recipes": [{**_recipe(), "id": 1}], "bar_stock": [{"id": stock_id, "quantity": None} for stock_id in (1, 2, 3)]}
    assert shopping_list(data) == []
    data["bar_stock"][0]["quantity"] = 0
    assert len(shopping_list(data)) == 1
    data["settings"] = {"bar_stock_enabled": False}
    assert shopping_list(data) == []


def test_shopping_list_api_and_staff_page_track_inventory_and_recipe_changes(tmp_path):
    app_module = _load_app_module(tmp_path)
    data = app_module.load_data()
    data["settings"]["brewery_type"] = "pro"
    app_module.save_data(data)
    client = app_module.app.test_client()
    _seed_stock(client)
    assert client.post("/api/drinks", json=_recipe()).status_code == 201
    client.put("/api/stock/2", json={"quantity": 0})
    staff = {"X-BarTender-Role": "staff", "X-BarTender-User-Id": "staff-1"}
    items = client.get("/api/shopping-list", headers=staff).get_json()
    assert len(items) == 1
    assert items[0]["name"] == "Campari"
    assert items[0]["recipes"] == [{"id": 1, "name": "Negroni", "optional": False}]
    with client.session_transaction() as session:
        session["user_id"] = "staff-1"
        session["user_role"] = "staff"
        session["user_name"] = "Staff"
    page = client.get("/shopping-list")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Campari" in html and "Out of stock" in html
    assert 'href="/drinks#recipe-1"' in html
    assert "Print List" in html
    client.put("/api/stock/2", json={"quantity": 1})
    assert client.get("/api/shopping-list").get_json() == []
    assert "No missing or out-of-stock recipe ingredients." in client.get("/shopping-list").get_data(as_text=True)
    client.put("/api/stock/2", json={"quantity": 0})
    with client.session_transaction() as session:
        session["user_role"] = "owner"
    client.delete("/api/drinks/1")
    assert client.get("/api/shopping-list").get_json() == []


def test_shopping_list_requires_authentication_and_handles_disabled_stock(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    app_module.app.config["TESTING"] = False
    assert client.get("/api/shopping-list").status_code == 401
    assert client.get("/shopping-list").status_code == 302
    app_module.app.config["TESTING"] = True
    data = app_module.load_data()
    data["settings"]["brewery_type"] = "pro"
    data["settings"]["bar_stock_enabled"] = False
    app_module.save_data(data)
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    assert "Bar stock is disabled" in client.get("/shopping-list").get_data(as_text=True)


def test_shopping_list_link_is_only_in_menu_dropdown(tmp_path):
    app_module = _load_app_module(tmp_path)
    data = app_module.load_data()
    data["settings"]["brewery_type"] = "pro"
    app_module.save_data(data)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    response = client.get("/drinks")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert html.count('href="/shopping-list"') == 1
    links = [element for element in _RenderedHTML(html).elements
             if element["tag"] == "a" and element["attrs"].get("href") == "/shopping-list"]
    assert len(links) == 1
    assert "nav-menu-item" in links[0]["attrs"].get("class", "").split()
    assert links[0]["attrs"].get("role") == "menuitem"
    assert links[0]["text"] == "Shopping List"


@pytest.mark.parametrize("profile,allowed", [("homebrewer", False), ("pro", True), ("commercial", True), ("invalid", False)])
def test_shopping_list_is_pro_only_for_menu_page_and_api(tmp_path, profile, allowed):
    app_module = _load_app_module(tmp_path)
    data = app_module.load_data()
    data["settings"]["brewery_type"] = profile
    app_module.save_data(data)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "staff-1"
        session["user_role"] = "staff"
        session["user_name"] = "Staff"
    drinks_page = client.get("/")
    assert drinks_page.status_code == 200
    assert ('href="/shopping-list"' in drinks_page.get_data(as_text=True)) is allowed
    for path in ("/shopping-list", "/api/shopping-list"):
        response = client.get(path)
        assert response.status_code == (200 if allowed else 403)
        if not allowed:
            assert response.get_json()["code"] == "SHOPPING_LIST_PRO_REQUIRED"
    assert client.get("/api/drinks").status_code == (200 if allowed else 403)
    assert client.get("/drinks").status_code == (200 if allowed else 403)
    assert ('href="/drinks"' in drinks_page.get_data(as_text=True)) is allowed


def test_classic_catalog_contains_valid_common_drinks_and_non_alcoholic_options():
    catalog = classic_recipes()
    assert len(catalog) >= 30
    assert len({recipe["name"] for recipe in catalog.values()}) == len(catalog)
    assert {"Old Fashioned", "Dry Martini", "Margarita", "Whiskey Sour", "Cosmopolitan", "Gin and Tonic", "Bloody Mary", "Mai Tai", "Long Island Iced Tea", "Espresso Martini"} <= {recipe["name"] for recipe in catalog.values()}
    assert sum(recipe["category"] == "nonalcoholic" for recipe in catalog.values()) >= 3
    for recipe in catalog.values():
        assert validate_recipe(recipe) == recipe
        assert recipe["glassware"] and recipe["ice"] and recipe["steps"]
        assert not recipe["menu_visible"]
    assert "Egg" in catalog["whiskey-sour"]["allergens"]
    assert "Milk" in catalog["white-russian"]["allergens"]


def test_classic_catalog_links_only_unique_exact_stock_matches():
    stock = [{"id": 1, "name": "GIN"}, {"id": 2, "name": "Sweet vermouth"}, {"id": 3, "name": "Sweet vermouth"}]
    recipe = classic_recipes(stock)["negroni"]
    assert recipe["ingredients"][0]["stock_id"] == 1
    assert recipe["ingredients"][1]["stock_id"] is None
    assert recipe["ingredients"][2]["stock_id"] is None
    recipe["ingredients"][0]["amount"] = 1
    assert classic_recipes()["negroni"]["ingredients"][0]["amount"] == 30


def test_add_classics_preserves_existing_recipes_and_stock_and_is_idempotent(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    _seed_stock(client)
    client.post("/api/drinks", json={**_recipe(), "name": "negroni", "notes": "My house recipe", "menu_visible": True})
    before = app_module.load_data()
    response = client.post("/api/drinks/classics")
    assert response.status_code == 201
    assert response.get_json() == {"added": len(classic_recipes()) - 1, "skipped": 1}
    after = app_module.load_data()
    assert after["drink_recipes"][0] == before["drink_recipes"][0]
    assert after["bar_stock"] == before["bar_stock"]
    assert len(after["drink_recipes"]) == len(classic_recipes())
    assert len({recipe["id"] for recipe in after["drink_recipes"]}) == len(after["drink_recipes"])
    assert all(not recipe["menu_visible"] for recipe in after["drink_recipes"][1:])
    assert len(shopping_list(after)) > 0
    repeated = client.post("/api/drinks/classics")
    assert repeated.status_code == 200
    assert repeated.get_json()["added"] == 0
    assert app_module.load_data()["drink_recipes"] == after["drink_recipes"]


def test_add_classics_permissions_and_starter_editor(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    assert client.post("/api/drinks/classics", headers={"X-BarTender-Role": "staff"}).status_code == 403
    assert client.post("/api/drinks/classics", headers={"X-BarTender-Role": "manager"}).status_code == 201
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    page = client.get("/drinks")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'id="addClassicsBtn"' in html
    options = {element["attrs"].get("value"): element["text"]
               for element in _RenderedHTML(html).elements if element["tag"] == "option"}
    assert options["old-fashioned"] == "Old Fashioned"
    assert options["shirley-temple"] == "Shirley Temple (nonalcoholic)"
    with client.session_transaction() as session:
        session["user_role"] = "staff"
    assert 'id="addClassicsBtn"' not in client.get("/drinks").get_data(as_text=True)


@pytest.mark.parametrize("method,path", [
    ("get", "/drinks"), ("get", "/api/drinks"), ("get", "/api/drinks/1"),
    ("post", "/api/drinks"), ("put", "/api/drinks/1"),
    ("delete", "/api/drinks/1"), ("post", "/api/drinks/classics"),
])
def test_non_pro_cannot_access_or_mutate_saved_drinks(tmp_path, method, path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    _seed_stock(client)
    client.post("/api/drinks", json=_recipe())
    data = app_module.load_data()
    data["settings"]["brewery_type"] = "homebrewer"
    app_module.save_data(data)
    before = copy.deepcopy(data["drink_recipes"])
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    response = getattr(client, method)(path, json=_recipe())
    assert response.status_code == 403
    assert response.get_json()["code"] == "DRINKS_PRO_REQUIRED"
    assert app_module.load_data()["drink_recipes"] == before
    assert client.get("/api/export/json").get_json()["data"]["drink_recipes"] == before


def test_base_guest_menus_hide_saved_drinks(tmp_path, monkeypatch):
    import bartender.display as display_module

    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    _seed_stock(client)
    client.post("/api/drinks", json={**_recipe(), "menu_visible": True})
    data = app_module.load_data()
    data["settings"]["brewery_type"] = "homebrewer"
    app_module.save_data(data)
    assert guest_drinks(data) == []
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    monkeypatch.setattr(display_module, "DATA_FILE", app_module.DATA_FILE)
    for menu_client in (client, display_module.display_app.test_client()):
        response = menu_client.get("/menu")
        assert response.status_code == 200
        assert "Negroni" not in response.get_data(as_text=True)


def test_unstocked_ingredient_label_uses_inventory_wording(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    _seed_stock(client)
    recipe = _recipe()
    recipe["ingredients"][0].update(name="Lime juice", stock_id=None)
    assert client.post("/api/drinks", json=recipe).status_code == 201
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    for path in ("/drinks", "/shopping-list"):
        response = client.get(path)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        labels = [element["text"] for element in _RenderedHTML(html).elements
              if element["tag"] == "span" and "drink-status" in element["attrs"].get("class", "").split()]
        assert "Not in bar stock" in labels
        assert "Missing stock link" not in labels
    assert client.get("/api/shopping-list").get_json()[0]["availability"] == "not_linked"


def test_shopping_stock_prefill_and_create_link_all_affected_recipes(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    _seed_stock(client)
    recipe = _recipe()
    recipe["ingredients"][0].update(name="Lime juice", stock_id=None)
    client.post("/api/drinks", json=recipe)
    client.post("/api/drinks", json={**recipe, "name": "House Drink"})
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    shopping_html = client.get("/shopping-list").get_data(as_text=True)
    assert "Add to Bar Stock" in shopping_html
    assert "shopping_item=Lime%20juice" in shopping_html
    page = client.get("/stock?shopping_item=Lime%20juice")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert '"name": "Lime juice"' in html
    assert '"category": "Other"' in html
    assert '"quantity": 1' in html
    created = client.post("/api/stock", json={
        "name": "Fresh lime juice", "category": "Juice", "quantity": 1,
        "shopping_item": "Lime juice", "shopping_stock_id": None,
    })
    assert created.status_code == 201
    for saved in client.get("/api/drinks").get_json():
        assert saved["ingredients"][0]["stock_id"] == created.get_json()["id"]
        assert saved["revision"] == 2
        assert saved["can_make"]
    assert client.get("/api/shopping-list").get_json() == []


def test_shopping_stock_existing_prefill_and_replenishment_preserve_record(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    _seed_stock(client)
    client.post("/api/drinks", json=_recipe())
    client.put("/api/stock/2", json={"quantity": 0, "category": "Liqueur", "size_label": "750 ml", "notes": "Existing note"})
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    html = client.get("/stock?shopping_item=Campari&shopping_stock_id=2").get_data(as_text=True)
    assert '"size_label": "750 ml"' in html
    assert '"notes": "Existing note"' in html
    assert "Restock Item" in html
    assert client.post("/api/stock", json={"name": "Campari", "quantity": 1, "shopping_item": "Campari", "shopping_stock_id": 2}).status_code == 400
    client.put("/api/stock/2", json={"quantity": 1})
    assert len(client.get("/api/stock").get_json()) == 3
    assert client.get("/api/shopping-list").get_json() == []


def test_shopping_stock_context_rejects_stale_items_and_non_pro_linking(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    assert client.post("/api/stock", json={"name": "Invalid", "shopping_item": "Not on list"}).status_code == 409
    assert client.get("/api/stock").get_json() == []
    data = app_module.load_data()
    data["settings"]["brewery_type"] = "homebrewer"
    app_module.save_data(data)
    assert client.post("/api/stock", json={"name": "Gin", "quantity": 1}).status_code == 201
    assert client.post("/api/stock", json={"name": "Lime juice", "shopping_item": "Lime juice"}).status_code == 409


def test_shopping_list_groups_by_drink_with_shared_and_optional_ingredients():
    items = [
        {"name": "Campari", "stock_id": 2, "availability": "out_of_stock", "optional": False,
         "recipes": [{"id": 1, "name": "Negroni", "optional": False}, {"id": 2, "name": "House Aperitif", "optional": True}]},
        {"name": "Orange peel", "stock_id": None, "availability": "not_linked", "optional": True,
         "recipes": [{"id": 1, "name": "Negroni", "optional": True}]},
        {"name": "Sweet vermouth", "stock_id": 3, "availability": "out_of_stock", "optional": False,
         "recipes": [{"id": 1, "name": "Negroni", "optional": False}]},
    ]
    before = copy.deepcopy(items)
    groups = shopping_list_by_drink(items)
    assert [group["name"] for group in groups] == ["House Aperitif", "Negroni"]
    assert groups[0]["items"][0]["name"] == "Campari"
    assert not groups[0]["items"][0]["optional"]
    assert [item["name"] for item in groups[1]["items"]] == ["Sweet vermouth", "Orange peel"]
    assert [item["name"] for group in groups for item in group["items"]].count("Campari") == 1
    assert sum(len(group["items"]) for group in groups) == len(items)
    assert len(groups[0]["items"][0]["recipes"]) == 2
    assert items == before
    assert shopping_list_by_drink([]) == []


def test_shopping_list_page_orders_drink_sections_and_preserves_combined_api(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    _seed_stock(client)
    client.post("/api/drinks", json={**_recipe(), "name": "Zulu Drink"})
    client.post("/api/drinks", json={**_recipe(), "name": "Alpha Drink"})
    client.put("/api/stock/2", json={"quantity": 0})
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    response = client.get("/shopping-list")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="shopping-drink-2"' in html
    assert 'id="shopping-drink-1"' not in html
    assert html.count('<h3>Campari</h3>') == 1
    assert "1 distinct ingredient / 2 drinks" in html
    assert "Also needed for" in html
    assert html.count("Add to Bar Stock") == 1
    assert len(client.get("/api/shopping-list").get_json()) == 1


def test_public_stock_defaults_hide_extra_ingredients_without_mutating_inventory():
    stock = [
        {"id": 1, "name": "Gin", "category": "Gin"},
        {"id": 2, "name": "Orange slice", "category": "Garnish"},
        {"id": 3, "name": "Simple syrup", "category": "Syrups"},
        {"id": 4, "name": "Salt", "category": "Other"},
        {"id": 5, "name": "Campari", "category": "Liqueur"},
    ]
    before = copy.deepcopy(stock)
    assert [item["name"] for item in public_bar_stock(stock)] == ["Gin", "Campari"]
    assert stock == before
    assert stock_is_public({"category": " gin "})
    assert not stock_is_public({"category": "Bitters"})
    assert not stock_is_public({})


def test_public_stock_category_exclusions_can_be_configured():
    assert stock_is_public({"category": "Other"}, [])
    assert not stock_is_public({"category": "Gin"}, [" gin "])
    assert stock_is_public({"category": "Custom Spirit"})
    assert not stock_is_public({"category": "Garnish", "public_visible": True})
    assert stock_is_public({"category": "Gin", "public_visible": False})


def test_public_stock_categories_filter_both_displays_and_menus_but_not_app(tmp_path, monkeypatch):
    import bartender.display as display_module

    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    for name, category in (("Public Gin", "Gin"), ("Private Orange", "Garnish"), ("Private Syrup", "Syrups"), ("Private Salt", "Other")):
        assert client.post("/api/stock", json={"name": name, "category": category, "quantity": 1, "notes": "PRIVATE INVENTORY NOTE"}).status_code == 201
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    app_html = client.get("/stock").get_data(as_text=True)
    assert "Private Orange" in app_html and "Private Syrup" in app_html and "Private Salt" in app_html
    assert "stockPublicVisible" not in app_html
    assert len(client.get("/api/stock").get_json()) == 4
    monkeypatch.setattr(display_module, "DATA_FILE", app_module.DATA_FILE)
    paths = ((client, "/display?display=2"), (client, "/menu"),
             (display_module.display_app.test_client(), "/?display=2"), (display_module.display_app.test_client(), "/menu"))
    for menu_client, path in paths:
        response = menu_client.get(path)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "Public Gin" in html
        assert "Private Orange" not in html and "Private Syrup" not in html and "Private Salt" not in html
        assert "PRIVATE INVENTORY NOTE" not in html
    assert client.post("/api/settings", json={"public_stock_hidden_categories": []}).status_code == 200
    for menu_client, path in paths:
        assert "Private Orange" in menu_client.get(path).get_data(as_text=True)


def test_hidden_stock_categories_settings_round_trip_permissions_and_recipe_availability(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    _seed_stock(client)
    client.post("/api/drinks", json=_recipe())
    original_stock = client.get("/api/stock").get_json()
    response = client.post("/api/settings", json={"public_stock_hidden_categories": [" Gin ", "gin", "Garnish"]})
    assert response.status_code == 200
    assert response.get_json()["public_stock_hidden_categories"] == ["Gin", "Garnish"]
    assert client.get("/api/stock").get_json() == original_stock
    assert client.get("/api/drinks/1").get_json()["can_make"]
    backup = client.get("/api/export/json").get_json()
    assert backup["data"]["settings"]["public_stock_hidden_categories"] == ["Gin", "Garnish"]
    assert client.post("/api/import/json", json=backup).status_code == 200
    assert app_module.load_data()["settings"]["public_stock_hidden_categories"] == ["Gin", "Garnish"]
    assert client.post("/api/settings", json={"public_stock_hidden_categories": "Garnish"}).status_code == 400
    assert client.post("/api/settings", json={"public_stock_hidden_categories": [1]}).status_code == 400
    assert client.post("/api/settings", json={"public_stock_hidden_categories": []}, headers={"X-BarTender-Role": "staff"}).status_code == 403
    assert client.post("/api/settings", json={"public_stock_hidden_categories": []}, headers={"X-BarTender-Role": "manager"}).status_code == 200
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    settings_html = client.get("/settings").get_data(as_text=True)
    assert "App-Only Stock Categories" in settings_html
    assert 'name="public_stock_hidden_category"' in settings_html