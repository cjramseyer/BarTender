/* BarTender – shared JS */

function initTheme(serverTheme, ingress) {
  const stored = localStorage.getItem("bartender_theme");
  const theme = stored || serverTheme || "light";
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("bartender_theme", theme);
}

function openModal(id) {
  const modal = document.getElementById(id);
  if (modal) modal.classList.add("is-open");
}

function closeModal(id) {
  const modal = document.getElementById(id);
  if (modal) modal.classList.remove("is-open");
}

async function toggleNavSetting(settingName, checkbox) {
  const ingress = window.BARTENDER_INGRESS || "";
  const enabled = Boolean(checkbox && checkbox.checked);
  const payload = {};
  payload[settingName] = enabled;

  try {
    const response = await fetch(`${ingress}/api/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      throw new Error(`Failed to update ${settingName}: ${response.status}`);
    }

    location.reload();
  } catch (err) {
    console.error(err);
    if (checkbox) {
      checkbox.checked = !enabled;
    }
  }
}

function setNavDropdownState(menu, isOpen) {
  if (!menu) {
    return;
  }

  const button = menu.querySelector(
    "[data-nav-dropdown-toggle], [data-nav-user-toggle]",
  );
  menu.classList.toggle("is-open", isOpen);
  if (button) {
    button.setAttribute("aria-expanded", String(isOpen));
  }
}

function closeNavDropdowns(exceptMenu) {
  document
    .querySelectorAll("[data-nav-dropdown], [data-nav-user-menu]")
    .forEach((menu) => {
      if (menu !== exceptMenu) {
        setNavDropdownState(menu, false);
      }
    });
}

// Close modal when clicking overlay background
document.addEventListener("click", (e) => {
  const dropdownToggle = e.target.closest(
    "[data-nav-dropdown-toggle], [data-nav-user-toggle]",
  );
  if (dropdownToggle) {
    const menu = dropdownToggle.closest(
      "[data-nav-dropdown], [data-nav-user-menu]",
    );
    if (menu) {
      const isOpen = !menu.classList.contains("is-open");
      closeNavDropdowns(menu);
      setNavDropdownState(menu, isOpen);
    }
    return;
  }

  if (!e.target.closest("[data-nav-dropdown], [data-nav-user-menu]")) {
    closeNavDropdowns();
  }

  if (e.target.classList.contains("modal")) {
    if (e.target.dataset.lock === "true") {
      return;
    }
    if (e.target.dataset.overlayClose !== "true") {
      return;
    }
    if (e.target.id === "updateNoticeModal") {
      dismissUpdateNotice();
      return;
    }
    e.target.classList.remove("is-open");
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeNavDropdowns();
  }
});

function initAppPrompts(appVersion) {
  const setupModal = document.getElementById("setupWizardModal");
  const updateModal = document.getElementById("updateNoticeModal");
  const currentVersion = String(appVersion || "").trim();

  if (setupModal) {
    openModal("setupWizardModal");
    return;
  }

  if (!updateModal || !currentVersion) {
    return;
  }

  const seenVersion = localStorage.getItem("bartender_seen_version");
  if (seenVersion === currentVersion) {
    return;
  }
}

async function submitSetupWizard(event) {
  event.preventDefault();
  const ingress = window.BARTENDER_INGRESS || "";
  const appVersion = String(window.BARTENDER_APP_VERSION || "").trim();
  const barName = document.getElementById("setupBarName");
  const measurement = document.querySelector(
    'input[name="setup_measurement"]:checked',
  );
  const theme = document.querySelector('input[name="setup_theme"]:checked');
  const message = document.getElementById("setupWizardMsg");

  const payload = {
    bar_name: barName ? barName.value.trim() : "",
    measurement: measurement ? measurement.value : "us",
    theme: theme ? theme.value : "light",
    setup_completed: true,
  };

  if (!payload.bar_name) {
    if (message) {
      message.textContent = "Bar name is required.";
    }
    return;
  }

  try {
    const response = await fetch(`${ingress}/api/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      throw new Error(`Setup save failed: ${response.status}`);
    }

    const usersResponse = await fetch(`${ingress}/api/team/users`);
    const usersBody = await usersResponse.json();
    const teamUsers = Array.isArray(usersBody.users) ? usersBody.users : [];
    const hasOwner = teamUsers.some(
      (user) =>
        String(user.role || "")
          .trim()
          .toLowerCase() === "owner",
    );

    if (!hasOwner) {
      const ownerResponse = await fetch(`${ingress}/api/team/users`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: "Owner",
          role: "owner",
        }),
      });
      const ownerBody = await ownerResponse.json();
      if (!ownerResponse.ok) {
        throw new Error(ownerBody.error || "Unable to create owner user");
      }
    }

    if (appVersion) {
      localStorage.setItem("bartender_seen_version", appVersion);
    }
    closeModal("setupWizardModal");
    location.reload();
  } catch (err) {
    console.error(err);
    if (message) {
      message.textContent = "Unable to save setup. Please try again.";
    }
  }
}

function dismissUpdateNotice() {
  const appVersion = String(window.BARTENDER_APP_VERSION || "").trim();
  if (appVersion) {
    localStorage.setItem("bartender_seen_version", appVersion);
  }
  closeModal("updateNoticeModal");
}
