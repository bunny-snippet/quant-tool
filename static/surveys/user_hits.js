(() => {
  const byId = (id) => document.getElementById(id);
  const columns = new Set(JSON.parse(byId('hitColumnAccess')?.textContent || '[]'));
  const columnCount = Math.max(1, columns.size);
  const elements = {
    search: byId('hitSearch'), from: byId('hitFromDateTime'), to: byId('hitToDateTime'), clear: byId('clearHitFilters'),
    pageSize: byId('hitPageSize'), rows: byId('hitRows'), cards: byId('hitCards'), summary: byId('hitSummary'),
    pageStatus: byId('hitPageStatus'), pageInput: byId('hitPageInput'), totalPages: byId('hitTotalPages'),
    first: byId('hitFirstPage'), prev: byId('hitPrevPage'), next: byId('hitNextPage'), last: byId('hitLastPage'),
    totalHits: byId('totalHitCount'), totalCompletes: byId('totalCompleteCount'), conversion: byId('conversionRate'),
    activeUsers: byId('activeUserCount'), dayCount: byId('hitDayCount'),
    branchFilters: document.querySelector('[data-hit-filter="branch"]'),
    subBranchFilters: document.querySelector('[data-hit-filter="sub_branch"]'),
    shiftFilters: document.querySelector('[data-hit-filter="shift"]'),
    userFilters: document.querySelector('[data-hit-filter="user"]'),
  };
  if (!elements.rows) return;
  document.querySelector('.user-hits-table').style.minWidth = `${Math.max(520, columnCount * 145)}px`;

  const state = { page: 1, pages: 1, pageSize: 20, timer: null, controller: null };
  const icons = {
    desktop: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/></svg>',
    mobile: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="2" width="12" height="20" rx="2"/><path d="M10 18h4"/></svg>',
    tablet: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="2" width="14" height="20" rx="2"/><circle cx="12" cy="18" r=".7"/></svg>',
  };

  function escapeHtml(value) { const node = document.createElement('div'); node.textContent = value == null ? '' : String(value); return node.innerHTML; }
  const number = (value) => Number(value || 0).toLocaleString('en-IN');
  const selectedValues = (container) => [...container.querySelectorAll('input:checked')].map((input) => input.value);

  function updateMultiLabel(container) {
    const checked = [...container.querySelectorAll('input:checked')]; const button = container.querySelector('.multi-trigger');
    const fallback = { branch: 'All branches', sub_branch: 'All sub-branches', shift: 'All shifts', user: 'All users' }[container.dataset.hitFilter];
    button.querySelector('span').textContent = checked.length === 0 ? fallback : checked.length === 1 ? checked[0].closest('label').innerText.trim() : `${checked.length} selected`;
    button.classList.toggle('has-value', checked.length > 0);
  }

  function closeMultiSelects(except = null) {
    document.querySelectorAll('.user-hits-filters .multi-select.open').forEach((container) => {
      if (container === except) return;
      container.classList.remove('open'); container.querySelector('.multi-menu').hidden = true;
      container.querySelector('.multi-trigger').setAttribute('aria-expanded', 'false');
    });
  }

  function applyMenuVisibility(container) {
    if (!container) return;
    const needle = container.querySelector('[data-multi-search]')?.value.trim().toLocaleLowerCase() || '';
    let visibleCount = 0;
    container.querySelectorAll('.multi-options label').forEach((option) => {
      const parentMatches = option.dataset.parentHidden !== 'true';
      const searchMatches = !needle || option.innerText.toLocaleLowerCase().includes(needle);
      option.hidden = !(parentMatches && searchMatches);
      if (!option.hidden) visibleCount += 1;
    });
    const noResults = container.querySelector('.multi-no-results');
    if (noResults) noResults.hidden = visibleCount > 0 || Boolean(container.querySelector('.filter-empty'));
  }

  function setParentVisibility(container, predicate) {
    if (!container) return;
    container.querySelectorAll('.multi-options label').forEach((option) => {
      const matches = predicate(option);
      option.dataset.parentHidden = String(!matches);
      const input = option.querySelector('input');
      if (!matches && input?.checked) input.checked = false;
    });
    applyMenuVisibility(container);
    updateMultiLabel(container);
  }

  function updateHierarchyOptions() {
    const branches = new Set(elements.branchFilters ? selectedValues(elements.branchFilters) : []);
    setParentVisibility(elements.subBranchFilters, (option) => !branches.size || branches.has(option.dataset.branchValue || ''));
    const subBranches = new Set(elements.subBranchFilters ? selectedValues(elements.subBranchFilters) : []);
    setParentVisibility(elements.shiftFilters, (option) => (
      (!branches.size || branches.has(option.dataset.branchValue || ''))
      && (!subBranches.size || subBranches.has(option.dataset.subBranchValue || ''))
    ));
    const shifts = new Set(elements.shiftFilters ? selectedValues(elements.shiftFilters) : []);
    setParentVisibility(elements.userFilters, (option) => (
      (!branches.size || branches.has(option.dataset.branchValue || ''))
      && (!subBranches.size || subBranches.has(option.dataset.subBranchValue || ''))
      && (!shifts.size || shifts.has(option.dataset.shiftValue || ''))
    ));
  }

  document.querySelectorAll('.user-hits-filters .multi-select').forEach((container) => {
    const trigger = container.querySelector('.multi-trigger'); const menu = container.querySelector('.multi-menu');
    trigger.addEventListener('click', () => {
      const shouldOpen = !container.classList.contains('open'); closeMultiSelects(container);
      container.classList.toggle('open', shouldOpen); menu.hidden = !shouldOpen; trigger.setAttribute('aria-expanded', String(shouldOpen));
      if (shouldOpen) window.setTimeout(() => menu.querySelector('[data-multi-search]')?.focus(), 0);
    });
    menu.querySelector('[data-multi-search]')?.addEventListener('input', () => applyMenuVisibility(container));
    menu.addEventListener('change', (event) => {
      if (event.target.matches('[data-multi-search]')) return;
      updateMultiLabel(container);
      if ([elements.branchFilters, elements.subBranchFilters, elements.shiftFilters].includes(container)) updateHierarchyOptions();
      scheduleLoad();
    });
    updateMultiLabel(container);
    applyMenuVisibility(container);
  });
  updateHierarchyOptions();

  function filterParams() {
    const params = new URLSearchParams({ page: state.page, page_size: state.pageSize });
    const search = elements.search?.value.trim(); if (search) params.set('search', search);
    document.querySelectorAll('.user-hits-filters [data-hit-filter]').forEach((container) => {
      const values = selectedValues(container); if (values.length) params.set(container.dataset.hitFilter, values.join(','));
    });
    if (elements.from?.value) { const [date, time] = elements.from.value.split('T'); params.set('from_date', date); if (time) params.set('from_time', time); }
    if (elements.to?.value) { const [date, time] = elements.to.value.split('T'); params.set('to_date', date); if (time) params.set('to_time', time); }
    return params;
  }

  function formatDate(value) {
    if (!value) return '—';
    return new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric', weekday: 'short' }).format(new Date(`${value}T12:00:00Z`));
  }

  function deviceBreakdown(counts) {
    const unknown = Number(counts.unclassified || 0);
    return `<div class="device-metric"><strong>Total <b>${number(counts.total)}</b></strong><div class="device-chips"><span class="desktop" title="Desktop"><i>${icons.desktop}</i><b>${number(counts.desktop)}</b><em class="sr-only">Desktop</em></span><span class="mobile" title="Mobile"><i>${icons.mobile}</i><b>${number(counts.mobile)}</b><em class="sr-only">Mobile</em></span><span class="tablet" title="Tablet"><i>${icons.tablet}</i><b>${number(counts.tablet)}</b><em class="sr-only">Tablet</em></span></div>${unknown ? `<small>${number(unknown)} unclassified</small>` : ''}</div>`;
  }

  function userCell(row) { return `<div class="hit-user"><span>${escapeHtml(String(row.user_name || '?').charAt(0).toUpperCase())}</span><div><strong>${escapeHtml(row.user_name)}</strong><small>${escapeHtml(row.user_email || row.username)}</small></div></div>`; }

  function rowTemplate(row) {
    const cells = [];
    if (columns.has('branch')) cells.push(`<td><strong class="hit-branch">${escapeHtml(row.branch || '—')}</strong></td>`);
    if (columns.has('sub_branch')) cells.push(`<td><span class="hit-sub-branch">${escapeHtml(row.sub_branch || '—')}</span></td>`);
    if (columns.has('shift')) cells.push(`<td><span class="hit-sub-branch">${escapeHtml(row.shift || '—')}</span></td>`);
    if (columns.has('user')) cells.push(`<td>${userCell(row)}</td>`);
    if (columns.has('date')) cells.push(`<td><time class="hit-date" datetime="${escapeHtml(row.date)}"><strong>${formatDate(row.date)}</strong><span>IST calendar day</span></time></td>`);
    if (columns.has('hits')) cells.push(`<td>${deviceBreakdown(row.hits)}</td>`);
    if (columns.has('completes')) cells.push(`<td>${deviceBreakdown(row.completes)}</td>`);
    return `<tr>${cells.length ? cells.join('') : '<td><div class="column-denied">No User Hits columns are assigned to your account.</div></td>'}</tr>`;
  }

  function cardTemplate(row) {
    if (!columns.size) return '<article class="survey-card user-hit-card"><div class="column-denied">No User Hits columns are assigned to your account.</div></article>';
    const locationParts = [];
    if (columns.has('branch')) locationParts.push(escapeHtml(row.branch));
    if (columns.has('sub_branch')) locationParts.push(escapeHtml(row.sub_branch));
    if (columns.has('shift')) locationParts.push(escapeHtml(row.shift));
    const location = locationParts.length ? `<div class="hit-location">${locationParts.map((part) => `<span>${part}</span>`).join('<i>→</i>')}</div>` : '';
    const metrics = `${columns.has('hits') ? `<section><label>Hits</label>${deviceBreakdown(row.hits)}</section>` : ''}${columns.has('completes') ? `<section><label>Completes</label>${deviceBreakdown(row.completes)}</section>` : ''}`;
    return `<article class="survey-card user-hit-card"><div class="user-hit-card-head">${columns.has('user') ? userCell(row) : '<div></div>'}${columns.has('date') ? `<time>${formatDate(row.date)}</time>` : ''}</div>${location}${metrics ? `<div class="hit-card-metrics">${metrics}</div>` : ''}</article>`;
  }

  function updateOverview(summary) {
    if (elements.totalHits) elements.totalHits.textContent = number(summary.hits.total);
    if (elements.totalCompletes) elements.totalCompletes.textContent = number(summary.completes.total);
    if (elements.conversion) elements.conversion.textContent = `${Number(summary.conversion_rate || 0).toLocaleString('en-IN')}%`;
    if (elements.activeUsers) elements.activeUsers.textContent = number(summary.active_users);
    if (elements.dayCount) elements.dayCount.textContent = `${number(summary.days)} selected ${Number(summary.days) === 1 ? 'day' : 'days'}`;
  }

  async function loadHits() {
    state.controller?.abort(); state.controller = new AbortController();
    elements.rows.innerHTML = `<tr><td colspan="${columnCount}"><div class="table-loader"><i></i><span>Building device-wise totals…</span></div></td></tr>`;
    try {
      const response = await fetch(`/api/v1/user-hits/?${filterParams()}`, { signal: state.controller.signal });
      const data = await response.json(); if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
      const results = data.results || []; const count = Number(data.count || 0); state.pages = Math.max(1, Math.ceil(count / state.pageSize));
      if (state.page > state.pages) { state.page = state.pages; return loadHits(); }
      updateOverview(data.summary || { hits: {}, completes: {} });
      elements.summary.innerHTML = count ? `<strong>${count.toLocaleString('en-IN')}</strong> user-day ${count === 1 ? 'record' : 'records'} match these filters` : 'No user activity matches these filters';
      elements.rows.innerHTML = results.length ? results.map(rowTemplate).join('') : `<tr><td colspan="${columnCount}"><div class="empty-state"><span>◎</span><strong>No user hits found</strong><small>Try clearing filters or start a new survey journey.</small></div></td></tr>`;
      elements.cards.innerHTML = results.length ? results.map(cardTemplate).join('') : '<div class="empty-state"><span>◎</span><strong>No user hits found</strong><small>Try clearing the filters.</small></div>';
      if (elements.pageInput) { elements.pageInput.value = state.page; elements.pageInput.max = state.pages; }
      if (elements.totalPages) elements.totalPages.textContent = `of ${state.pages.toLocaleString('en-IN')}`;
      elements.pageStatus.textContent = `Page ${state.page.toLocaleString('en-IN')} of ${state.pages.toLocaleString('en-IN')}`;
      if (elements.first && elements.prev) elements.first.disabled = elements.prev.disabled = state.page <= 1;
      if (elements.next && elements.last) elements.next.disabled = elements.last.disabled = state.page >= state.pages;
    } catch (error) {
      if (error.name === 'AbortError') return;
      elements.rows.innerHTML = `<tr><td colspan="${columnCount}"><div class="error-state"><strong>Could not load user hits</strong><span>${escapeHtml(error.message)}</span><button type="button" id="retryUserHits">Try again</button></div></td></tr>`;
      byId('retryUserHits')?.addEventListener('click', loadHits); elements.cards.innerHTML = '';
    }
  }

  function scheduleLoad() { clearTimeout(state.timer); state.timer = setTimeout(() => { state.page = 1; loadHits(); }, 260); }
  function go(page) { state.page = Math.min(state.pages, Math.max(1, Number(page) || 1)); loadHits(); document.querySelector('.user-hits-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }

  elements.search?.addEventListener('input', scheduleLoad); [elements.from, elements.to].filter(Boolean).forEach((input) => input.addEventListener('change', scheduleLoad));
  elements.pageSize?.addEventListener('change', () => { state.pageSize = Number(elements.pageSize.value); state.page = 1; loadHits(); });
  elements.clear?.addEventListener('click', () => {
    if (elements.search) elements.search.value = ''; if (elements.from) elements.from.value = ''; if (elements.to) elements.to.value = '';
    document.querySelectorAll('.user-hits-filters .multi-select').forEach((container) => {
      container.querySelectorAll('input[type="checkbox"]').forEach((input) => { input.checked = false; });
      const searchInput = container.querySelector('[data-multi-search]'); if (searchInput) searchInput.value = '';
      updateMultiLabel(container);
    });
    updateHierarchyOptions();
    closeMultiSelects(); state.page = 1; loadHits();
  });
  elements.first?.addEventListener('click', () => go(1)); elements.prev?.addEventListener('click', () => go(state.page - 1));
  elements.next?.addEventListener('click', () => go(state.page + 1)); elements.last?.addEventListener('click', () => go(state.pages));
  elements.pageInput?.addEventListener('change', () => go(elements.pageInput.value));
  document.addEventListener('click', (event) => { if (!event.target.closest('.user-hits-filters .multi-select')) closeMultiSelects(); });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeMultiSelects(); });
  loadHits();
})();
