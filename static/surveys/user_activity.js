/* Accessible tab switcher for the combined User Activity workspace. */
(() => {
  const switcher = document.querySelector('[data-user-activity-tabs]');
  if (!switcher) return;

  const tabs = [...switcher.querySelectorAll('[data-activity-tab]')];
  const panels = [...document.querySelectorAll('[data-activity-panel]')];
  const available = new Set(tabs.map((tab) => tab.dataset.activityTab));

  function activate(name, { focus = false, updateUrl = false } = {}) {
    const selected = available.has(name) ? name : tabs[0]?.dataset.activityTab;
    if (!selected) return;

    tabs.forEach((tab) => {
      const active = tab.dataset.activityTab === selected;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      if (active && focus) tab.focus();
    });
    panels.forEach((panel) => {
      panel.hidden = panel.dataset.activityPanel !== selected;
    });

    if (updateUrl) {
      const url = new URL(window.location.href);
      if (selected === 'user-hits') url.searchParams.delete('tab');
      else url.searchParams.set('tab', selected);
      window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`);
    }
    document.dispatchEvent(new CustomEvent('user-activity:change', {
      detail: { panel: selected },
    }));
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => activate(tab.dataset.activityTab, { updateUrl: true }));
    tab.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      let nextIndex = index;
      if (event.key === 'ArrowLeft') nextIndex = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length;
      if (event.key === 'Home') nextIndex = 0;
      if (event.key === 'End') nextIndex = tabs.length - 1;
      activate(tabs[nextIndex].dataset.activityTab, { focus: true, updateUrl: true });
    });
  });

  activate(new URLSearchParams(window.location.search).get('tab') || 'user-hits');
})();


