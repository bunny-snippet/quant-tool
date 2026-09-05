/* Shared authenticated-shell behavior: responsive sidebar and mobile dismissal. */

/* Persistent light/dark appearance for every page using the shared shell. */
(() => {
  const root = document.documentElement;
  const toggle = document.getElementById('themeToggle');
  if (!toggle) return;

  const syncToggle = () => {
    const isDark = root.dataset.theme === 'dark';
    toggle.setAttribute('aria-pressed', String(isDark));
    toggle.setAttribute('aria-label', isDark ? 'Switch to light theme' : 'Switch to dark theme');
    toggle.title = isDark ? 'Light theme' : 'Dark theme';
  };

  toggle.addEventListener('click', () => {
    const nextTheme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    root.dataset.theme = nextTheme;
    try {
      window.localStorage.setItem('exchange-hub-theme', nextTheme);
    } catch (_error) {
      // The theme still applies for this page if browser storage is unavailable.
    }
    syncToggle();
  });

  syncToggle();
})();

(() => {
  const shell = document.querySelector('.app-shell');
  const menu = document.getElementById('menuButton');
  const scrim = document.getElementById('scrim');
  if (!shell || !menu) return;
  const isMobile = () => window.matchMedia('(max-width: 900px)').matches;
  const setSidebar = (open) => {
    shell.dataset.sidebar = open ? 'open' : 'closed';
    menu.setAttribute('aria-expanded', String(open));
  };
  menu.addEventListener('click', () => setSidebar(shell.dataset.sidebar !== 'open'));
  scrim?.addEventListener('click', () => setSidebar(false));
  window.addEventListener('resize', () => { if (!isMobile() && shell.dataset.sidebar === 'closed') setSidebar(true); });
  if (isMobile()) setSidebar(false);
})();

/* Show checked multi-select values above their filter panel. Removing a chip
   updates the original checkbox so page-specific fetch and hierarchy logic
   remains the single source of truth. */
(() => {
  const panels = [...document.querySelectorAll('.filter-panel')]
    .filter((panel) => panel.querySelector('.multi-select input[type="checkbox"]'));
  if (!panels.length) return;
  // A dropdown search hides labels only; it does not change the selected
  // filters. Remember selection by the actual checkbox node so chips cannot
  // disappear merely because their option is outside the current search.
  const selectedByPanel = new WeakMap();

  function fieldName(input) {
    const field = input.closest('.field');
    return field?.querySelector(':scope > label')?.textContent.trim() || 'Filter';
  }

  function optionName(input) {
    return input.closest('label')?.querySelector('span')?.textContent.trim()
      || input.value
      || 'Selected';
  }

  function stripFor(panel) {
    let strip = panel.querySelector(':scope > .active-filter-strip');
    if (strip) return strip;
    strip = document.createElement('div');
    strip.className = 'active-filter-strip';
    strip.hidden = true;
    strip.setAttribute('aria-live', 'polite');
    const title = document.createElement('span');
    title.className = 'active-filter-title';
    title.textContent = 'Selected filters';
    const chips = document.createElement('div');
    chips.className = 'active-filter-chips';
    strip.append(title, chips);
    panel.prepend(strip);
    return strip;
  }

  function syncPanel(panel) {
    const strip = stripFor(panel);
    const chips = strip.querySelector('.active-filter-chips');
    let selected = selectedByPanel.get(panel);
    if (!selected) {
      selected = new Set();
      selectedByPanel.set(panel, selected);
    }
    // Capture initial/programmatic checked values, but remove values only in
    // the checkbox change handler below. Search-filtered labels remain in the
    // set and therefore stay removable from the chip strip.
    panel.querySelectorAll('.multi-select input[type="checkbox"]:checked').forEach(
      (input) => selected.add(input),
    );
    selected = [...selected].filter((input) => input.isConnected);
    chips.replaceChildren();
    selected.forEach((input) => {
      const button = document.createElement('button');
      button.className = 'active-filter-chip';
      button.type = 'button';
      button.title = `Remove ${optionName(input)} from ${fieldName(input)}`;
      button.setAttribute('aria-label', button.title);
      const name = document.createElement('b');
      name.textContent = fieldName(input);
      const value = document.createElement('span');
      value.textContent = optionName(input);
      const close = document.createElement('i');
      close.setAttribute('aria-hidden', 'true');
      close.textContent = '×';
      button.append(name, value, close);
      button.addEventListener('click', () => {
        input.checked = false;
        input.dispatchEvent(new Event('change', { bubbles: true }));
      });
      chips.append(button);
    });
    strip.hidden = selected.length === 0;
  }

  function syncAll() {
    panels.forEach(syncPanel);
  }

  document.addEventListener('change', (event) => {
    const panel = event.target.closest?.('.filter-panel');
    const input = event.target.matches?.('.multi-select input[type="checkbox"]')
      ? event.target
      : null;
    if (panel && input) {
      const selected = selectedByPanel.get(panel) || new Set();
      if (input.checked) selected.add(input);
      else selected.delete(input);
      selectedByPanel.set(panel, selected);
      queueMicrotask(() => syncPanel(panel));
    }
  });
  document.addEventListener('click', (event) => {
    const clear = event.target.closest?.('.filter-panel .clear-button');
    if (clear) {
      const panel = clear.closest('.filter-panel');
      selectedByPanel.delete(panel);
      window.setTimeout(() => syncPanel(panel), 0);
    }
  });
  window.FilterSelectionChips = { sync: syncAll, syncPanel };
  window.setTimeout(syncAll, 0);
})();
