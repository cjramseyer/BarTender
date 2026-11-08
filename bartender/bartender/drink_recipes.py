"""Drink specifications and live ingredient availability."""

import json
import math
from pathlib import Path
from urllib.parse import urlparse


DRINK_METHODS = ("build", "stir", "shake", "muddle", "blend")
DRINK_CATEGORIES = ("classic", "house", "nonalcoholic")
INGREDIENT_UNITS = ("ml", "cl", "l", "oz", "tsp", "tbsp", "dash", "drop", "piece")


def classic_recipes(stock: list[dict] | None = None) -> dict[str, dict]:
    catalog = json.loads(Path(__file__).with_name("classic_drinks.json").read_text(encoding="utf-8"))
    recipes = {}
    for key, specification in catalog.items():
        ingredients = []
        for name, amount, unit, *flags in specification["ingredients"]:
            matches = [item for item in (stock or []) if str(item.get("name", "")).strip().casefold() == name.casefold()]
            ingredients.append({
                "name": name, "amount": amount, "unit": unit,
                "optional": bool(flags and flags[0]),
                "stock_id": matches[0]["id"] if len(matches) == 1 else None,
            })
        recipes[key] = validate_recipe({
            **specification, "ingredients": ingredients,
            "category": specification.get("category", "classic"),
            "notes": "Starting bar specification; adjust measures, substitutions and allergens to your house recipe and ingredient labels.",
        })
    return recipes


def validate_recipe(body: dict, existing: dict | None = None) -> dict:
    if not isinstance(body, dict):
        raise ValueError("Recipe must be an object.")
    recipe = {**(existing or {}), **body}
    result = {}
    for field in (
        "name", "description", "base_spirit", "flavor", "glassware", "ice",
        "garnish", "substitutions", "allergens", "source_url", "notes", "price",
    ):
        value = recipe.get(field, "")
        if not isinstance(value, str) or len(value) > 4000:
            raise ValueError(f"{field} must be text of at most 4000 characters.")
        result[field] = value.strip()
    if not result["name"]:
        raise ValueError("Drink name is required.")
    if result["source_url"]:
        source = urlparse(result["source_url"])
        if source.scheme not in ("http", "https") or not source.netloc:
            raise ValueError("Source URL must use http or https.")
    for field, choices, default in (
        ("method", DRINK_METHODS, "build"),
        ("category", DRINK_CATEGORIES, "house"),
    ):
        if recipe.get(field, default) not in choices:
            raise ValueError(f"Invalid {field}.")
        result[field] = recipe.get(field, default)
    visible = recipe.get("menu_visible", False)
    if not isinstance(visible, bool):
        raise ValueError("Menu visibility must be true or false.")
    result["menu_visible"] = visible
    steps = recipe.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 50:
        raise ValueError("Provide 1 to 50 preparation steps.")
    if any(not isinstance(step, str) or not step.strip() or len(step) > 2000 for step in steps):
        raise ValueError("Each preparation step must contain text.")
    result["steps"] = [step.strip() for step in steps]
    ingredients = recipe.get("ingredients")
    if not isinstance(ingredients, list) or not 1 <= len(ingredients) <= 50:
        raise ValueError("Provide 1 to 50 ingredients.")
    result["ingredients"] = []
    for ingredient in ingredients:
        if not isinstance(ingredient, dict):
            raise ValueError("Each ingredient must be an object.")
        name = ingredient.get("name", "")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError("Each ingredient needs a name of at most 200 characters.")
        amount = ingredient.get("amount")
        try:
            valid_amount = not isinstance(amount, bool) and isinstance(amount, (int, float)) and math.isfinite(amount) and amount > 0
        except OverflowError:
            valid_amount = False
        if not valid_amount:
            raise ValueError("Ingredient amounts must be positive finite numbers.")
        unit = ingredient.get("unit")
        if unit not in INGREDIENT_UNITS:
            raise ValueError("Invalid ingredient unit.")
        stock_id = ingredient.get("stock_id")
        if stock_id is not None and (type(stock_id) is not int or stock_id <= 0):
            raise ValueError("Stock links must be positive integer IDs or null.")
        optional = ingredient.get("optional", False)
        if not isinstance(optional, bool):
            raise ValueError("Ingredient optional flag must be true or false.")
        result["ingredients"].append({
            "name": name.strip(), "amount": amount, "unit": unit,
            "stock_id": stock_id, "optional": optional,
        })
    return result


def recipe_availability(recipe: dict, stock: list[dict], stock_enabled: bool = True) -> dict:
    stock_by_id = {item.get("id"): item for item in stock}
    ingredients = []
    for ingredient in recipe["ingredients"]:
        item = stock_by_id.get(ingredient.get("stock_id"))
        status = "not_linked"
        if not stock_enabled:
            status = "unknown"
        elif ingredient.get("stock_id") is not None and item is None:
            status = "missing_stock"
        elif item is not None:
            try:
                quantity = float(item.get("quantity"))
                status = "in_stock" if quantity > 0 else "out_of_stock"
                if not math.isfinite(quantity) or isinstance(item.get("quantity"), bool):
                    status = "unknown"
            except (TypeError, ValueError, OverflowError):
                status = "unknown"
        ingredients.append({
            **ingredient, "stock_name": item.get("name", "") if item else "",
            "availability": status,
        })
    required = [item for item in ingredients if not item.get("optional")]
    missing = [item["name"] for item in required if item["availability"] in ("not_linked", "missing_stock", "out_of_stock")]
    unknown = [item["name"] for item in required if item["availability"] == "unknown"]
    return {
        **recipe, "ingredients": ingredients, "missing_ingredients": missing,
        "unknown_ingredients": unknown,
        "can_make": bool(required) and not missing and not unknown,
    }


def normalize_recipes(raw) -> list[dict]:
    recipes = []
    used_ids = set()
    if not isinstance(raw, list):
        return recipes
    next_id = max((item.get("id", 0) for item in raw if isinstance(item, dict) and type(item.get("id")) is int), default=0) + 1
    for item in raw:
        try:
            recipe = validate_recipe(item)
        except (ValueError, TypeError):
            continue
        recipe_id = item.get("id")
        if type(recipe_id) is not int or recipe_id <= 0 or recipe_id in used_ids:
            recipe_id = max(1, next_id)
            next_id = recipe_id + 1
        used_ids.add(recipe_id)
        recipe["id"] = recipe_id
        revision = item.get("revision", 1)
        recipe["revision"] = revision if type(revision) is int and revision > 0 else 1
        recipe["updated_at"] = str(item.get("updated_at", ""))
        recipes.append(recipe)
    return recipes


def guest_drinks(data: dict) -> list[dict]:
    profile = str(data.get("settings", {}).get("brewery_type", "homebrewer")).strip().lower()
    if profile not in ("pro", "commercial"):
        return []
    drinks = []
    for recipe in normalize_recipes(data.get("drink_recipes", [])):
        if not recipe["menu_visible"]:
            continue
        availability = recipe_availability(recipe, data.get("bar_stock", []), data.get("settings", {}).get("bar_stock_enabled", True))
        drinks.append({
            **{field: recipe[field] for field in ("name", "description", "category", "allergens", "price")},
            "availability": "available" if availability["can_make"] else (
                "unavailable" if availability["missing_ingredients"] else "unknown"
            ),
        })
    return sorted(drinks, key=lambda drink: drink["name"].casefold())


def shopping_list(data: dict) -> list[dict]:
    items = {}
    for recipe in normalize_recipes(data.get("drink_recipes", [])):
        availability = recipe_availability(
            recipe, data.get("bar_stock", []),
            data.get("settings", {}).get("bar_stock_enabled", True),
        )
        for ingredient in availability["ingredients"]:
            status = ingredient["availability"]
            if status not in ("out_of_stock", "not_linked", "missing_stock"):
                continue
            stock_id = ingredient["stock_id"]
            name = ingredient["stock_name"] or ingredient["name"]
            key = ("stock", stock_id) if stock_id is not None else ("name", " ".join(name.split()).casefold())
            if key not in items:
                items[key] = {
                    "name": name, "stock_id": stock_id, "availability": status,
                    "optional": True, "recipes": {},
                }
            item = items[key]
            optional = ingredient["optional"]
            item["optional"] = item["optional"] and optional
            usage = item["recipes"].setdefault(recipe["id"], {
                "id": recipe["id"], "name": recipe["name"], "optional": True,
            })
            usage["optional"] = usage["optional"] and optional
    result = []
    for item in items.values():
        item["recipes"] = sorted(item["recipes"].values(), key=lambda recipe: recipe["name"].casefold())
        result.append(item)
    return sorted(result, key=lambda item: (item["optional"], item["name"].casefold(), item["stock_id"] or 0))


def shopping_list_by_drink(items: list[dict]) -> list[dict]:
    groups = {}
    for item in items:
        if not item["recipes"]:
            continue
        recipe = min(item["recipes"], key=lambda recipe: (recipe["name"].casefold(), recipe["id"]))
        group = groups.setdefault(recipe["id"], {
            "id": recipe["id"], "name": recipe["name"], "items": [],
        })
        group["items"].append(dict(item))
    for group in groups.values():
        group["items"].sort(key=lambda item: (item["optional"], item["name"].casefold(), item["stock_id"] or 0))
    return sorted(groups.values(), key=lambda group: (group["name"].casefold(), group["id"]))