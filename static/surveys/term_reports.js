/* Searchable, cascading multi-select controls for the server-rendered Term Reports. */
(() => {
  const form = document.getElementById('reasonFilters');
  if (!form) return;

  const containers = [...form.querySelectorAll('.multi-select')];
  const byFilter = (name) => form.querySelector(`[data-multi-filter="${name}"]`);
  const fallbackLabels = {
    branch: 'All branches', sub_branch: 'All sub-branches', shift: 'All shifts',
    user: 'All users', status: 'All unsuccessful', country: 'All countries',
    client: 'All clients', buyer_id: 'All buyer IDs',
  };

  const selectedValues = (container) => new Set(
    container ? [...container.querySelectorAll('.multi-options input:checked')].map((input) => input.value) : []
  );

  function updateLabel(container) {
    const checked = [...container.querySelectorAll('.multi-options input:checked')];
    const label = container.querySelector('.multi-trigger span');
    if (!label) return;
    label.textContent = checked.length === 0
      ? (fallbackLabels[container.dataset.multiFilter] || 'All options')
      : checked.length === 1
        ? checked[0].closest('label')?.innerText.trim() || checked[0].value
        : `${checked.length} selected`;
    container.querySelector('.multi-trigger')?.classList.toggle('has-value', checked.length > 0);
  }

  function applySearch(container) {
    const needle = container.querySelector('[data-multi-search]')?.value.trim().toLocaleLowerCase() || '';
    let visible = 0;
    container.querySelectorAll('.multi-options label').forEach((option) => {
      const matchesParent = option.dataset.parentHidden !== 'true';
      const matchesSearch = !needle || option.innerText.toLocaleLowerCase().includes(needle);
      option.hidden = !(matchesParent && matchesSearch);
      if (!option.hidden) visible += 1;
    });
    const empty = container.querySelector('.multi-no-results');
    if (empty) empty.hidden = visible > 0 || Boolean(container.querySelector('.filter-empty'));
  }

  function limitOptions(container, predicate) {
    if (!container) return;
    container.querySelectorAll('.multi-options label').forEach((option) => {
      const visible = predicate(option);
      option.dataset.parentHidden = String(!visible);
      const input = option.querySelector('input');
      if (!visible && input?.checked) input.checked = false;
    });
    applySearch(container);
    updateLabel(container);
  }

  function updateHierarchy() {
    const branches = selectedValues(byFilter('branch'));
    const subBranches = selectedValues(byFilter('sub_branch'));
    const shifts = selectedValues(byFilter('shift'));
    limitOptions(byFilter('sub_branch'), (option) => !branches.size || branches.has(option.dataset.branchValue || ''));
    limitOptions(byFilter('shift'), (option) => (
      (!branches.size || branches.has(option.dataset.branchValue || ''))
      && (!subBranches.size || subBranches.has(option.dataset.subBranchValue || ''))
    ));
    limitOptions(byFilter('user'), (option) => (
      (!branches.size || branches.has(option.dataset.branchValue || ''))
      && (!subBranches.size || subBranches.has(option.dataset.subBranchValue || ''))
      && (!shifts.size || shifts.has(option.dataset.shiftValue || ''))
    ));
  }

  function updateBuyers() {
    const clients = selectedValues(byFilter('client'));
    limitOptions(byFilter('buyer_id'), (option) => !clients.size || clients.has(option.dataset.clientId || ''));
  }

  function closeMenus(except = null) {
    containers.forEach((container) => {
      if (container === except) return;
      container.classList.remove('open');
      const menu = container.querySelector('.multi-menu');
      const trigger = container.querySelector('.multi-trigger');
      if (menu) menu.hidden = true;
      trigger?.setAttribute('aria-expanded', 'false');
    });
  }

  containers.forEach((container) => {
    const trigger = container.querySelector('.multi-trigger');
    const menu = container.querySelector('.multi-menu');
    trigger?.addEventListener('click', () => {
      const opening = !container.classList.contains('open');
      closeMenus(container);
      container.classList.toggle('open', opening);
      if (menu) menu.hidden = !opening;
      trigger.setAttribute('aria-expanded', String(opening));
      if (opening) window.setTimeout(() => menu?.querySelector('[data-multi-search]')?.focus(), 0);
    });
    container.querySelector('[data-multi-search]')?.addEventListener('input', () => applySearch(container));
    menu?.addEventListener('change', (event) => {
      if (!event.target.matches('input[type="checkbox"]')) return;
      updateLabel(container);
      if (['branch', 'sub_branch', 'shift'].includes(container.dataset.multiFilter)) updateHierarchy();
      if (container.dataset.multiFilter === 'client') updateBuyers();
    });
    updateLabel(container);
    applySearch(container);
  });

  updateHierarchy();
  updateBuyers();
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.reason-list-filters .multi-select')) closeMenus();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeMenus();
  });
})();
