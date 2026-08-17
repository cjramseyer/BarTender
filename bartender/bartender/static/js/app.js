/* BarTender – shared JS */

function initTheme(serverTheme, ingress) {
  const stored = localStorage.getItem('bartender_theme');
  const theme = stored || serverTheme || 'light';
  document.documentElement.dataset.theme = theme;
  updateThemeIcon(theme);

  document.getElementById('themeToggle').addEventListener('click', () => {
    const current = document.documentElement.dataset.theme;
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('bartender_theme', next);
    updateThemeIcon(next);
    // Persist to server
    fetch(`${ingress}/api/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: next }),
    });
  });
}

function updateThemeIcon(theme) {
  const btn = document.getElementById('themeToggle');
  if (btn) btn.textContent = theme === 'dark' ? '☀' : '🌙';
}

function openModal(id) {
  const modal = document.getElementById(id);
  if (modal) modal.classList.add('is-open');
}

function closeModal(id) {
  const modal = document.getElementById(id);
  if (modal) modal.classList.remove('is-open');
}

// Close modal when clicking overlay background
document.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal')) {
    e.target.classList.remove('is-open');
  }
});
