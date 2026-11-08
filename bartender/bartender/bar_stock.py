"""Public visibility of beverage inventory."""


STOCK_CATEGORY_CHOICES = (
    "Whiskey", "Bourbon", "Scotch", "Rye", "Irish Whiskey",
    "Gin", "Vodka", "Rum", "Tequila", "Mezcal", "Brandy", "Cognac",
    "Liqueur", "Vermouth", "Bitters", "Amaro", "Beer", "Wine", "Sparkling Wine",
    "Cider", "Sake", "Non-Alcoholic Spirits", "Ready-to-Drink Cocktails",
    "Mixers", "Tonic / Soda", "Juice", "Syrups", "Garnish", "Other",
)


DEFAULT_HIDDEN_STOCK_CATEGORIES = (
    "Garnish", "Garnishes", "Syrups", "Bitters", "Mixers", "Tonic / Soda",
    "Juice", "Other", "Ingredients", "Fruit", "Sweeteners", "Spices", "Ice",
)


def normalize_hidden_stock_categories(value) -> list[str]:
    if not isinstance(value, list):
        return list(DEFAULT_HIDDEN_STOCK_CATEGORIES)
    categories = []
    seen = set()
    for category in value:
        if not isinstance(category, str):
            continue
        category = category.strip()
        if category and category.casefold() not in seen:
            categories.append(category)
            seen.add(category.casefold())
    return categories


def stock_is_public(item: dict, hidden_categories: list[str] | None = None) -> bool:
    category = str(item.get("category", "") or "Other").strip().casefold() or "other"
    excluded = {value.casefold() for value in normalize_hidden_stock_categories(hidden_categories)}
    return category not in excluded


def public_bar_stock(stock: list[dict], hidden_categories: list[str] | None = None) -> list[dict]:
    return [item for item in stock if isinstance(item, dict) and stock_is_public(item, hidden_categories)]