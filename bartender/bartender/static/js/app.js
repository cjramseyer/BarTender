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

// Close modal when clicking overlay background
document.addEventListener("click", (e) => {
  if (e.target.classList.contains("modal")) {
    e.target.classList.remove("is-open");
  }
});
