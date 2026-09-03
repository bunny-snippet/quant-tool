/* Monthly employee completion and Final ID performance dashboard. */
(() => {
  const panel = document.querySelector('.user-performance-panel');
  if (!panel) return;

  const $ = (selector) => document.querySelector(selector);
  const filters = [...document.querySelectorAll('[data-user-dashboard-filter]')];
  const filterByName = (name) => filters.find((item) => item.dataset.userDashboardFilter === name);
  const elements = {
    search: $('#userDashboardSearch'), month: $('#userDashboardMonth'), year: $('#userDashboardYear'),
    pageSize: $('#userDashboardPageSize'), rows: $('#userDashboardRows'), cards: $('#userDashboardCards'),
    summary: $('#userDashboardSummary'), pageStatus: $('#userDashboardPageStatus'),
    pageInput: $('#userDashboardPageInput'), totalPages: $('#userDashboardTotalPages'),
    first: $('#userDashboardFirstPage'), prev: $('#userDashboardPrevPage'),
    next: $('#userDashboardNextPage'), last: $('#userDashboardLastPage'), clear: $('#clearUserDashboardFilters'),
  };
  const defaults = { month: elements.month.value, year: elements.year.value };
  let currentPage = 1;
  let totalPages = 1;
  let requestController = null;
  let searchTimer = null;

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  }[char]));
  const number = (value) => Number(value || 0).toLocaleString('en-IN');
  const selectedValues = (container) => container
    ? [...container.querySelectorAll('input[type="checkbox"]:checked')].map((input) => input.value)
    : [];

  function updateTrigger(container) {
    const values = selectedValues(container);
    const fallback = {
      branch: 'All branches', sub_branch: 'All sub-branches', shift: 'All shifts', user: 'All users',
    }[container.dataset.userDashboardFilter];
    const label = container.querySelector('.multi-trigger span');
    if (!values.length) label.textContent = fallback;
    else if (values.length === 1) {
      const input = container.querySelector(`input[value="${CSS.escape(values[0])}"]`);
      label.textContent = input?.closest('label')?.querySelector('span')?.textContent || '1 selected';
    } else label.textContent = `${values.length} selected`;
  }

  function applyMenuSearch(container) {
    const needle = (container.querySelector('[data-multi-search]')?.value || '').trim().toLowerCase();
    let visible = 0;
    container.querySelectorAll('.multi-options > label').forEach((label) => {
      const matches = !needle || label.textContent.toLowerCase().includes(needle);
      const show = label.dataset.hierarchyHidden !== 'true' && matches;
      label.hidden = !show;
      if (show) visible += 1;
    });
    const empty = container.querySelector('.multi-no-results');
    if (empty) empty.hidden = visible > 0;
  }

  function setHierarchyVisibility(container, predicate) {
    if (!container) return;
    container.querySelectorAll('.multi-options > label').forEach((label) => {
      const allowed = predicate(label);
      label.dataset.hierarchyHidden = allowed ? 'false' : 'true';
      const checkbox = label.querySelector('input[type="checkbox"]');
      if (!allowed && checkbox?.checked) checkbox.checked = false;
    });
    updateTrigger(container);
    applyMenuSearch(container);
  }

  function updateHierarchyOptions() {
    const branches = new Set(selectedValues(filterByName('branch')));
    setHierarchyVisibility(filterByName('sub_branch'), (option) => (
      !branches.size || branches.has(option.dataset.branchValue || '')
    ));
    const subBranches = new Set(selectedValues(filterByName('sub_branch')));
    setHierarchyVisibility(filterByName('shift'), (option) => (
      (!branches.size || branches.has(option.dataset.branchValue || ''))
      && (!subBranches.size || subBranches.has(option.dataset.subBranchValue || ''))
    ));
    const shifts = new Set(selectedValues(filterByName('shift')));
    setHierarchyVisibility(filterByName('user'), (option) => (
      (!branches.size || branches.has(option.dataset.branchValue || ''))
      && (!subBranches.size || subBranches.has(option.dataset.subBranchValue || ''))
      && (!shifts.size || shifts.has(option.dataset.shiftValue || ''))
    ));
  }

  function closeMenus(except = null) {
    filters.forEach((container) => {
      if (container === except) return;
      container.querySelector('.multi-menu').hidden = true;
      container.querySelector('.multi-trigger').setAttribute('aria-expanded', 'false');
    });
  }

  filters.forEach((container) => {
    const trigger = container.querySelector('.multi-trigger');
    const menu = container.querySelector('.multi-menu');
    trigger.addEventListener('click', (event) => {
      event.stopPropagation();
      const willOpen = menu.hidden;
      closeMenus(container);
      menu.hidden = !willOpen;
      trigger.setAttribute('aria-expanded', String(willOpen));
      if (willOpen) container.querySelector('[data-multi-search]')?.focus();
    });
    menu.addEventListener('click', (event) => event.stopPropagation());
    container.querySelector('[data-multi-search]')?.addEventListener('input', () => applyMenuSearch(container));
    container.addEventListener('change', (event) => {
      if (!event.target.matches('input[type="checkbox"]')) return;
      updateTrigger(container);
      if (['branch', 'sub_branch', 'shift'].includes(container.dataset.userDashboardFilter)) {
        updateHierarchyOptions();
      }
      load(1);
    });
    updateTrigger(container);
  });
  document.addEventListener('click', () => closeMenus());

  function query(page) {
    const params = new URLSearchParams({
      page: String(page), page_size: elements.pageSize.value,
      month: elements.month.value, year: elements.year.value,
    });
    if (elements.search.value.trim()) params.set('search', elements.search.value.trim());
    filters.forEach((container) => {
      const values = selectedValues(container);
      if (values.length) params.set(container.dataset.userDashboardFilter, values.join(','));
    });
    return params;
  }

  function rate(value) {
    return value === null || value === undefined ? 'Awaiting review' : `${Number(value).toFixed(1)}%`;
  }

  function performanceCell(row) {
    const acceptance = row.acceptance_rate === null ? 0 : Number(row.acceptance_rate);
    return `<div class="user-performance-rate"><strong>${escapeHtml(rate(row.acceptance_rate))}</strong><span><i style="width:${Math.max(0, Math.min(100, acceptance))}%"></i></span><small>${Number(row.reviewed_rate || 0).toFixed(1)}% reviewed</small></div>`;
  }

  function renderRows(rows) {
    if (!rows.length) {
      elements.rows.innerHTML = '<tr><td colspan="9"><div class="empty-state">No employees match these filters.</div></td></tr>';
      elements.cards.innerHTML = '<div class="empty-state">No employees match these filters.</div>';
      return;
    }
    elements.rows.innerHTML = rows.map((row) => `<tr>
      <td><div class="user-performance-person"><strong>${escapeHtml(row.user_name)}</strong><small>${escapeHtml(row.user_email || row.username || '—')}</small></div></td>
      <td>${escapeHtml(row.branch || '—')}</td><td>${escapeHtml(row.sub_branch || '—')}</td><td>${escapeHtml(row.shift || '—')}</td>
      <td><strong class="user-performance-count">${number(row.completes)}</strong></td>
      <td><span class="user-performance-status accepted">${number(row.accepted)}</span></td>
      <td><span class="user-performance-status rejected">${number(row.rejected)}</span></td>
      <td><span class="user-performance-status pending">${number(row.pending)}</span></td>
      <td>${performanceCell(row)}</td>
    </tr>`).join('');
    elements.cards.innerHTML = rows.map((row) => `<article class="survey-card user-performance-card">
      <header><div><small>${escapeHtml([row.branch, row.sub_branch, row.shift].filter(Boolean).join(' / ') || 'No hierarchy')}</small><h3>${escapeHtml(row.user_name)}</h3><p>${escapeHtml(row.user_email || row.username || '')}</p></div>${performanceCell(row)}</header>
      <div class="user-performance-card-counts"><span><small>Completes</small><strong>${number(row.completes)}</strong></span><span class="accepted"><small>Accepted</small><strong>${number(row.accepted)}</strong></span><span class="rejected"><small>Rejected</small><strong>${number(row.rejected)}</strong></span><span class="pending"><small>Pending</small><strong>${number(row.pending)}</strong></span></div>
    </article>`).join('');
  }

  function renderSummary(summary, period) {
    $('#userDashboardCompletes').textContent = number(summary.completes);
    $('#userDashboardAccepted').textContent = number(summary.accepted);
    $('#userDashboardRejected').textContent = number(summary.rejected);
    $('#userDashboardPending').textContent = number(summary.pending);
    $('#userDashboardAcceptance').textContent = rate(summary.acceptance_rate);
    $('#userDashboardActiveUsers').textContent = number(summary.active_users);
    $('#userDashboardPeriod').textContent = period.label;
    $('#userDashboardUserTotal').textContent = `${number(summary.users)} filtered employees`;
    elements.summary.textContent = `${number(summary.users)} employees · ${number(summary.completes)} completes · ${Number(summary.reviewed_rate || 0).toFixed(1)}% reviewed for ${period.label}`;
  }

  function updatePagination(count) {
    totalPages = Math.max(1, Math.ceil(Number(count || 0) / Number(elements.pageSize.value)));
    currentPage = Math.min(currentPage, totalPages);
    elements.pageInput.value = String(currentPage);
    elements.pageInput.max = String(totalPages);
    elements.totalPages.textContent = `of ${totalPages}`;
    elements.pageStatus.textContent = `Page ${currentPage} of ${totalPages}`;
    elements.first.disabled = elements.prev.disabled = currentPage <= 1;
    elements.next.disabled = elements.last.disabled = currentPage >= totalPages;
  }

  async function load(page = currentPage) {
    currentPage = Math.max(1, page);
    if (requestController) requestController.abort();
    requestController = new AbortController();
    elements.rows.innerHTML = '<tr><td colspan="9"><div class="table-loader"><i></i><span>Building user performance…</span></div></td></tr>';
    try {
      const response = await fetch(`${panel.dataset.apiUrl}?${query(currentPage)}`, {
        credentials: 'same-origin', signal: requestController.signal,
        headers: { Accept: 'application/json' },
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || 'User performance could not be loaded.');
      renderRows(payload.results || []);
      renderSummary(payload.summary || {}, payload.period || { label: 'Selected month' });
      updatePagination(payload.count || 0);
    } catch (error) {
      if (error.name === 'AbortError') return;
      elements.rows.innerHTML = `<tr><td colspan="9"><div class="empty-state error">${escapeHtml(error.message)}</div></td></tr>`;
      elements.cards.innerHTML = '';
      elements.summary.textContent = error.message;
    }
  }

  elements.search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => load(1), 280);
  });
  elements.month.addEventListener('change', () => load(1));
  elements.year.addEventListener('change', () => load(1));
  elements.pageSize.addEventListener('change', () => load(1));
  elements.first.addEventListener('click', () => load(1));
  elements.prev.addEventListener('click', () => load(currentPage - 1));
  elements.next.addEventListener('click', () => load(currentPage + 1));
  elements.last.addEventListener('click', () => load(totalPages));
  elements.pageInput.addEventListener('change', () => load(Math.min(totalPages, Math.max(1, Number(elements.pageInput.value) || 1))));
  elements.clear.addEventListener('click', () => {
    elements.search.value = '';
    elements.month.value = defaults.month;
    elements.year.value = defaults.year;
    filters.forEach((container) => {
      container.querySelectorAll('input[type="checkbox"]:checked').forEach((input) => { input.checked = false; });
      const search = container.querySelector('[data-multi-search]');
      if (search) search.value = '';
      updateTrigger(container);
    });
    updateHierarchyOptions();
    load(1);
  });

  updateHierarchyOptions();
  load(1);
})();
