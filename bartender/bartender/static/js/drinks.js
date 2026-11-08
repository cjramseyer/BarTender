(() => {
  const data = JSON.parse(document.getElementById("drinkData").textContent);
  const recipes = new Map(
    data.recipes.map((recipe) => [String(recipe.id), recipe]),
  );
  const byId = (id) => document.getElementById(id);
  const fields = {
    name: "drinkName",
    description: "drinkDescription",
    base_spirit: "drinkBaseSpirit",
    flavor: "drinkFlavor",
    category: "drinkCategory",
    method: "drinkMethod",
    glassware: "drinkGlassware",
    ice: "drinkIce",
    garnish: "drinkGarnish",
    substitutions: "drinkSubstitutions",
    allergens: "drinkAllergens",
    source_url: "drinkSourceUrl",
    notes: "drinkNotes",
    price: "drinkPrice",
  };
  const starters = data.starters;

  function filterRecipes() {
    const query = byId("drinkSearch").value.trim().toLocaleLowerCase();
    let count = 0;
    document.querySelectorAll("[data-recipe-id]").forEach((element) => {
      const recipe = recipes.get(element.dataset.recipeId);
      const search = [
        recipe.name,
        recipe.base_spirit,
        recipe.flavor,
        recipe.method,
        recipe.description,
        ...recipe.ingredients.map((item) => item.name),
      ]
        .join(" ")
        .toLocaleLowerCase();
      element.hidden =
        !search.includes(query) ||
        (byId("drinkCategoryFilter").value &&
          recipe.category !== byId("drinkCategoryFilter").value) ||
        (byId("drinkMethodFilter").value &&
          recipe.method !== byId("drinkMethodFilter").value) ||
        (byId("drinkReadyFilter").checked && !recipe.can_make);
      if (!element.hidden) count += 1;
    });
    byId("drinkResultCount").textContent =
      `${count} ${count === 1 ? "drink" : "drinks"}`;
    byId("drinkEmpty").hidden = count > 0;
  }
  [
    "drinkSearch",
    "drinkCategoryFilter",
    "drinkMethodFilter",
    "drinkReadyFilter",
  ].forEach((id) => byId(id).addEventListener("input", filterRecipes));
  filterRecipes();
  function openLinkedRecipe() {
    const recipe = byId(window.location.hash.slice(1));
    if (recipe?.classList.contains("drink-recipe")) {
      recipe.open = true;
      recipe.scrollIntoView();
    }
  }
  openLinkedRecipe();
  window.addEventListener("hashchange", openLinkedRecipe);
  if (!byId("drinkForm")) return;

  function addIngredient(ingredient = {}) {
    const row = document.createElement("div");
    row.className = "drink-ingredient-editor";
    row.innerHTML =
      '<div><label>Ingredient *</label><input class="form-control ingredient-name" aria-label="Ingredient name" required maxlength="200"></div><div><label>Amount *</label><input class="form-control ingredient-amount" aria-label="Ingredient amount" type="number" min="0.001" step="any" required></div><div><label>Unit</label><select class="form-control ingredient-unit" aria-label="Ingredient unit"></select></div><div><label>Bar stock</label><select class="form-control ingredient-stock" aria-label="Linked bar stock"></select><span class="ingredient-status muted" role="status"></span></div><label class="drink-check"><input class="ingredient-optional" type="checkbox"> Optional</label><button class="btn btn-icon" type="button" title="Remove ingredient" aria-label="Remove ingredient">&times;</button>';
    row.querySelector(".ingredient-name").value = ingredient.name || "";
    row.querySelector(".ingredient-amount").value = ingredient.amount || 1;
    const unit = row.querySelector(".ingredient-unit");
    data.units.forEach((value) => unit.add(new Option(value, value)));
    unit.value =
      ingredient.unit || (data.measurement === "metric" ? "ml" : "oz");
    const stock = row.querySelector(".ingredient-stock");
    stock.add(new Option("Not linked", ""));
    data.stock.forEach((item) =>
      stock.add(
        new Option(
          `${item.name} (${item.quantity ?? "?"} on hand)`,
          String(item.id),
        ),
      ),
    );
    if (
      ingredient.stock_id &&
      !data.stock.some((item) => item.id === ingredient.stock_id)
    ) {
      stock.add(
        new Option(
          `Missing stock item #${ingredient.stock_id}`,
          String(ingredient.stock_id),
        ),
      );
    }
    stock.value =
      ingredient.stock_id == null ? "" : String(ingredient.stock_id);
    function updateStatus() {
      const item = data.stock.find((item) => String(item.id) === stock.value);
      let label = stock.value ? "Stock item missing" : "Not in bar stock";
      if (item) {
        const quantity =
          item.quantity == null ||
          typeof item.quantity === "boolean" ||
          String(item.quantity).trim() === ""
            ? NaN
            : Number(item.quantity);
        label = Number.isFinite(quantity)
          ? quantity > 0
            ? "In stock"
            : "Out of stock"
          : "Availability unknown";
      }
      row.querySelector(".ingredient-status").textContent = label;
    }
    stock.addEventListener("change", updateStatus);
    updateStatus();
    row.querySelector(".ingredient-optional").checked = !!ingredient.optional;
    row.querySelector("button").addEventListener("click", () => row.remove());
    byId("drinkIngredientRows").append(row);
  }

  function fillForm(recipe = {}) {
    Object.entries(fields).forEach(([field, id]) => {
      byId(id).value = recipe[field] || "";
    });
    byId("drinkCategory").value = recipe.category || "house";
    byId("drinkMethod").value = recipe.method || "build";
    byId("drinkId").value = recipe.id || "";
    byId("drinkSteps").value = (recipe.steps || []).join("\n");
    byId("drinkMenuVisible").checked = !!recipe.menu_visible;
    byId("drinkIngredientRows").replaceChildren();
    (recipe.ingredients?.length ? recipe.ingredients : [{}]).forEach(
      addIngredient,
    );
    byId("drinkFormError").hidden = true;
  }

  function openEditor(recipe) {
    fillForm(recipe);
    byId("drinkStarter").value = "";
    byId("drinkStarterGroup").hidden = !!recipe?.id;
    byId("drinkModalTitle").textContent = recipe?.id
      ? "Edit Drink"
      : "Add Drink";
    openModal("drinkModal");
    byId("drinkName").focus();
  }
  byId("addDrinkBtn").addEventListener("click", () => openEditor());
  byId("addIngredientBtn").addEventListener("click", () => addIngredient());
  byId("drinkStarter").addEventListener("change", (event) => {
    const starter = starters[event.target.value];
    if (!starter) {
      fillForm();
      return;
    }
    fillForm(starter);
  });
  document
    .querySelectorAll("[data-edit-drink]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        openEditor(recipes.get(button.dataset.editDrink)),
      ),
    );

  async function sendRecipe(path, method, body) {
    const response = await fetch(`${data.ingress}${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Unable to save drink.");
    return result;
  }
  byId("addClassicsBtn").addEventListener("click", async () => {
    if (
      !confirm("Add classic bar recipes that are not already in your catalog?")
    )
      return;
    byId("addClassicsBtn").disabled = true;
    byId("drinkPageError").hidden = true;
    try {
      await sendRecipe("/api/drinks/classics", "POST");
      window.location.reload();
    } catch (error) {
      byId("drinkPageError").textContent = error.message;
      byId("drinkPageError").hidden = false;
    } finally {
      byId("addClassicsBtn").disabled = false;
    }
  });
  byId("drinkForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = Object.fromEntries(
      Object.entries(fields).map(([field, id]) => [
        field,
        byId(id).value.trim(),
      ]),
    );
    body.steps = byId("drinkSteps")
      .value.split("\n")
      .map((step) => step.trim())
      .filter(Boolean);
    body.menu_visible = byId("drinkMenuVisible").checked;
    body.ingredients = Array.from(byId("drinkIngredientRows").children).map(
      (row) => ({
        name: row.querySelector(".ingredient-name").value.trim(),
        amount: Number(row.querySelector(".ingredient-amount").value),
        unit: row.querySelector(".ingredient-unit").value,
        stock_id: row.querySelector(".ingredient-stock").value
          ? Number(row.querySelector(".ingredient-stock").value)
          : null,
        optional: row.querySelector(".ingredient-optional").checked,
      }),
    );
    byId("saveDrinkBtn").disabled = true;
    byId("drinkFormError").hidden = true;
    try {
      const id = byId("drinkId").value;
      await sendRecipe(
        id ? `/api/drinks/${id}` : "/api/drinks",
        id ? "PUT" : "POST",
        body,
      );
      window.location.reload();
    } catch (error) {
      byId("drinkFormError").textContent = error.message;
      byId("drinkFormError").hidden = false;
    } finally {
      byId("saveDrinkBtn").disabled = false;
    }
  });
  document.querySelectorAll("[data-delete-drink]").forEach((button) =>
    button.addEventListener("click", async () => {
      const recipe = recipes.get(button.dataset.deleteDrink);
      if (!confirm(`Delete "${recipe.name}"?`)) return;
      button.disabled = true;
      try {
        await sendRecipe(`/api/drinks/${recipe.id}`, "DELETE");
        window.location.reload();
      } catch (error) {
        byId("drinkPageError").textContent = error.message;
        byId("drinkPageError").hidden = false;
        button.disabled = false;
      }
    }),
  );
})();
