/* Traffic Report hierarchy filters, KPI animation and responsive attempt rows. */

(() => {
  const byId = (id) => document.getElementById(id);
  const columns = new Set(JSON.parse(byId('studyColumnAccess')?.textContent || '[]'));
  const columnCount = Math.max(1, columns.size);
  const elements = {
    search: byId('studySearch'), branchFilters: document.querySelector('[data-multi-filter="branch"]'),
    subBranchFilters: document.querySelector('[data-multi-filter="sub_branch"]'),
    shiftFilters: document.querySelector('[data-multi-filter="shift"]'),
    userFilters: document.querySelector('[data-multi-filter="user"]'),
    statusFilters: document.querySelector('[data-multi-filter="status"]'),
    countryFilters: document.querySelector('[data-multi-filter="country"]'),
    clientFilters: document.querySelector('[data-multi-filter="client"]'),
    buyerFilters: document.querySelector('[data-multi-filter="buyer_id"]'), dateField: byId('studyDateField'),
    from: byId('studyFromDateTime'), to: byId('studyToDateTime'), clear: byId('clearStudyFilters'),
    export: byId('exportStudies'), pageSize: byId('studyPageSize'), rows: byId('studyRows'),
    cards: byId('studyCards'), summary: byId('studySummary'), pageStatus: byId('studyPageStatus'),
    pageInput: byId('studyPageInput'), totalPages: byId('studyTotalPages'), first: byId('studyFirstPage'),
    prev: byId('studyPrevPage'), next: byId('studyNextPage'), last: byId('studyLastPage'),
    metrics: {
      total: byId('studyMetricTotal'), initiated: byId('studyMetricInitiated'),
      completed: byId('studyMetricCompleted'), terminated: byId('studyMetricTerminated'),
      overQuota: byId('studyMetricQuota'), security: byId('studyMetricSecurity'),
      conversion: byId('studyMetricConversion'), desktop: byId('studyMetricDesktop'),
      mobile: byId('studyMetricMobile'), tablet: byId('studyMetricTablet'),
      revenue: byId('studyMetricRevenue'),
      ir: byId('studyMetricIR'),
    },
  };
  if (!elements.rows) return;
  document.querySelector('.studies-table').style.minWidth = `${Math.max(980, columnCount * 96)}px`;

  const state = {
    page: 1, pages: 1, pageSize: Number(elements.pageSize?.value || 10), timer: null, controller: null,
    projectId: byId('studyProjectScope') ? (new URLSearchParams(window.location.search).get('internal_id') || '') : '',
  };
  const statusTone = { initiated: 'initiate', redirected: 'initiate', '1': 'complete', '2': 'terminate', '3': 'quota', '4': 'quality' };
  const deviceIcons = {
    desktop: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/></svg>',
    mobile: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="2" width="12" height="20" rx="2"/><path d="M10 18h4"/></svg>',
    tablet: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="2" width="14" height="20" rx="2"/><circle cx="12" cy="18" r=".7"/></svg>',
    unknown: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M9.8 9a2.4 2.4 0 1 1 3.5 2.2c-.9.5-1.3 1-1.3 2M12 17h.01"/></svg>',
  };

  function escapeHtml(value) {
    const node = document.createElement('div');
    node.textContent = value == null ? '' : String(value);
    return node.innerHTML;
  }
  const escapeAttr = (value) => escapeHtml(value).replaceAll('"', '&quot;').replaceAll("'", '&#39;');

  const selectedValues = (container) => container ? [...container.querySelectorAll('input:checked')].map((input) => input.value) : [];

  function updateMultiLabel(container) {
    const checked = [...container.querySelectorAll('input:checked')];
    const button = container.querySelector('.multi-trigger');
    const fallback = {
      branch: 'All branches', sub_branch: 'All sub-branches', shift: 'All shifts',
      user: 'All users', status: 'All statuses', country: 'All countries', client: 'All clients', buyer_id: 'All buyer IDs',
    }[container.dataset.multiFilter] || 'All options';
    button.querySelector('span').textContent = checked.length === 0 ? fallback : checked.length === 1 ? checked[0].closest('label').innerText.trim() : `${checked.length} selected`;
    button.classList.toggle('has-value', checked.length > 0);
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

  function updateStudyHierarchyOptions() {
    const branches = new Set(selectedValues(elements.branchFilters));
    setParentVisibility(elements.subBranchFilters, (option) => !branches.size || branches.has(option.dataset.branchValue || ''));
    const subBranches = new Set(selectedValues(elements.subBranchFilters));
    setParentVisibility(elements.shiftFilters, (option) => (
      (!branches.size || branches.has(option.dataset.branchValue || ''))
      && (!subBranches.size || subBranches.has(option.dataset.subBranchValue || ''))
    ));
    const shifts = new Set(selectedValues(elements.shiftFilters));
    setParentVisibility(elements.userFilters, (option) => (
      (!branches.size || branches.has(option.dataset.branchValue || ''))
      && (!subBranches.size || subBranches.has(option.dataset.subBranchValue || ''))
      && (!shifts.size || shifts.has(option.dataset.shiftValue || ''))
    ));
  }

  function closeMultiSelects(except = null) {
    document.querySelectorAll('.studies-filters .multi-select.open').forEach((container) => {
      if (container === except) return;
      container.classList.remove('open');
      container.querySelector('.multi-menu').hidden = true;
      container.querySelector('.multi-trigger').setAttribute('aria-expanded', 'false');
    });
  }

  function updateStudyBuyerOptions() {
    if (!elements.buyerFilters) return;
    const clients = new Set(selectedValues(elements.clientFilters));
    elements.buyerFilters.querySelectorAll('.multi-options label').forEach((option) => {
      const matches = !clients.size || clients.has(option.dataset.clientId || '');
      option.dataset.parentHidden = String(!matches);
      const input = option.querySelector('input');
      if (!matches && input?.checked) input.checked = false;
    });
    applyMenuVisibility(elements.buyerFilters);
    updateMultiLabel(elements.buyerFilters);
  }

  document.querySelectorAll('.studies-filters .multi-select').forEach((container) => {
    const trigger = container.querySelector('.multi-trigger');
    const menu = container.querySelector('.multi-menu');
    trigger.addEventListener('click', () => {
      const shouldOpen = !container.classList.contains('open');
      closeMultiSelects(container);
      container.classList.toggle('open', shouldOpen);
      menu.hidden = !shouldOpen;
      trigger.setAttribute('aria-expanded', String(shouldOpen));
      if (shouldOpen) window.setTimeout(() => menu.querySelector('[data-multi-search]')?.focus(), 0);
    });
    menu.querySelector('[data-multi-search]')?.addEventListener('input', () => applyMenuVisibility(container));
    menu.addEventListener('change', (event) => {
      if (event.target.matches('[data-multi-search]')) return;
      updateMultiLabel(container);
      if ([elements.branchFilters, elements.subBranchFilters, elements.shiftFilters].includes(container)) updateStudyHierarchyOptions();
      if (container === elements.clientFilters) updateStudyBuyerOptions();
      scheduleLoad();
    });
    updateMultiLabel(container);
    applyMenuVisibility(container);
  });
  updateStudyHierarchyOptions();
  updateStudyBuyerOptions();

  function dateBoundary(dateTime, endOfMinute = false) {
    if (!dateTime) return '';
    const [date, selectedTime = '00:00'] = dateTime.split('T');
    return `${date}T${selectedTime}:${endOfMinute ? '59.999' : '00'}+05:30`;
  }

  function filterParams(includePage = true) {
    const params = new URLSearchParams({ ordering: '-initiated_at' });
    const search = elements.search?.value.trim();
    const branches = selectedValues(elements.branchFilters);
    const subBranches = selectedValues(elements.subBranchFilters);
    const shifts = selectedValues(elements.shiftFilters);
    const users = selectedValues(elements.userFilters);
    const statuses = selectedValues(elements.statusFilters);
    const countries = selectedValues(elements.countryFilters);
    const clients = selectedValues(elements.clientFilters);
    const buyers = selectedValues(elements.buyerFilters);
    if (search) params.set('search', search);
    if (branches.length) params.set('branch', branches.join(','));
    if (subBranches.length) params.set('sub_branch', subBranches.join(','));
    if (shifts.length) params.set('shift', shifts.join(','));
    if (users.length) params.set('user', users.join(','));
    if (statuses.length) params.set('status', statuses.join(','));
    if (countries.length) params.set('country', countries.join(','));
    if (clients.length) params.set('client', clients.join(','));
    if (buyers.length) params.set('buyer_id', buyers.join(','));
    if (state.projectId) params.set('internal_id', state.projectId);
    if (elements.dateField && elements.from?.value) params.set(`${elements.dateField.value}_from`, dateBoundary(elements.from.value));
    if (elements.dateField && elements.to?.value) params.set(`${elements.dateField.value}_to`, dateBoundary(elements.to.value, true));
    if (includePage) { params.set('page', state.page); params.set('page_size', state.pageSize); }
    return params;
  }

  function formatIst(value, split = false) {
    if (!value) return split ? { date: '—', time: '' } : '—';
    const parsed = new Date(value);
    const date = new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric' }).format(parsed);
    const time = new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true }).format(parsed);
    return split ? { date, time } : `${date}, ${time}`;
  }

  function formatLoi(seconds) {
    if (seconds == null) return '—';
    const total = Number(seconds); const minutes = Math.floor(total / 60); const remainder = total % 60;
    return minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
  }

  function formatMoney(value, currency = 'USD') {
    const amount = Number(value || 0);
    try {
      return new Intl.NumberFormat('en-IN', {
        style: 'currency', currency: currency || 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2,
      }).format(amount);
    } catch (_) {
      return `${currency || 'USD'} ${amount.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    }
  }

  function animateRevenue(value, currency) {
    if (!elements.metrics.revenue || value == null) return;
    const target = Number(value || 0);
    const start = Number(elements.metrics.revenue.dataset.value || 0);
    const started = performance.now();
    const duration = 780;
    elements.metrics.revenue.dataset.value = String(target);
    const frame = (now) => {
      const progress = Math.min(1, (now - started) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      elements.metrics.revenue.textContent = formatMoney(start + ((target - start) * eased), currency);
      if (progress < 1) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  function animateMetric(element, value, { suffix = '', maximumFractionDigits = 0 } = {}) {
    if (!element) return;
    const target = Number(value || 0);
    const start = Number(element.dataset.value || 0);
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const render = (current) => {
      element.textContent = `${Number(current).toLocaleString('en-IN', { maximumFractionDigits })}${suffix}`;
    };
    element.dataset.value = String(target);
    if (element.metricAnimationFrame) cancelAnimationFrame(element.metricAnimationFrame);
    element.classList.remove('metric-counting');
    void element.offsetWidth;
    element.classList.add('metric-counting');
    if (reducedMotion || start === target) {
      render(target);
      element.classList.remove('metric-counting');
      return;
    }
    const started = performance.now();
    const duration = 680;
    const frame = (now) => {
      const progress = Math.min(1, (now - started) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      render(start + ((target - start) * eased));
      if (progress < 1) {
        element.metricAnimationFrame = requestAnimationFrame(frame);
      } else {
        render(target);
        element.metricAnimationFrame = null;
        window.setTimeout(() => element.classList.remove('metric-counting'), 180);
      }
    };
    element.metricAnimationFrame = requestAnimationFrame(frame);
  }

  function deviceBadge(attempt) {
    const label = attempt.entry_device || 'Unknown'; const normalized = label.toLowerCase();
    const type = normalized.includes('mobile') || normalized.includes('phone') ? 'mobile' : normalized.includes('tablet') || normalized.includes('tab') ? 'tablet' : normalized.includes('desktop') || normalized.includes('computer') || normalized.includes('laptop') ? 'desktop' : 'unknown';
    return `<span class="study-device ${type}" title="${escapeHtml(label)}"><i>${deviceIcons[type]}</i></span>`;
  }

  function ipPair(attempt) {
    const entry = attempt.entry_ip || ''; const exit = attempt.exit_ip || '';
    const stateClass = entry && exit ? (entry === exit ? 'same' : 'changed') : 'pending';
    return `<div class="ip-pair ${stateClass}"><span class="entry-ip"><i>IN</i>${escapeHtml(entry || '—')}</span><span class="exit-ip"><i>OUT</i>${escapeHtml(exit || 'Awaiting')}</span></div>`;
  }

  const endTimestamp = (attempt) => ['initiated', 'redirected'].includes(attempt.status) ? attempt.initiated_at : (attempt.callback_at || attempt.initiated_at);
  function timestampCell(value) { const stamp = formatIst(value, true); return `<div class="study-timestamp"><strong>${stamp.date}</strong><span>${stamp.time} IST</span></div>`; }
  function statusPill(attempt) {
    const label = ['initiated', 'redirected'].includes(attempt.status) ? 'Initiated' : (attempt.status_label || attempt.status);
    const reason = attempt.termination_reason || attempt.termination_category || '';
    return `<div class="attempt-outcome"><span class="attempt-status ${statusTone[attempt.status] || 'neutral'}"><i></i>${escapeHtml(label)}</span>${reason ? `<small class="attempt-reason" title="${escapeAttr(reason)}">${escapeHtml(reason)}</small>` : ''}</div>`;
  }

  function updateOverview(summary = {}) {
    const completedDevices = summary.completed_devices || {};
    const values = {
      total: summary.total, initiated: summary.initiated, completed: summary.completed,
      terminated: summary.terminated, overQuota: summary.over_quota,
      security: summary.security_terminated, desktop: completedDevices.desktop,
      mobile: completedDevices.mobile, tablet: completedDevices.tablet,
    };
    Object.entries(values).forEach(([key, value]) => animateMetric(elements.metrics[key], value));
    animateMetric(elements.metrics.conversion, summary.conversion_rate, { suffix: '%', maximumFractionDigits: 2 });
    animateMetric(elements.metrics.ir, summary.incidence_rate, { suffix: '%', maximumFractionDigits: 2 });
    animateRevenue(summary.total_revenue, summary.revenue_currency || 'USD');
  }

  function rowTemplate(attempt) {
    const cells = [];
    if (columns.has('project_id')) cells.push(`<td class="study-col-project"><div class="study-id-stack"><strong class="study-project-id" title="${escapeAttr(attempt.survey_local_id)}">${escapeHtml(attempt.survey_local_id)}</strong><small class="study-secondary" title="${escapeAttr(attempt.client_name || attempt.company_name || 'Survey client')}">${escapeHtml(attempt.client_name || attempt.company_name || 'Survey client')}</small></div></td>`);
    if (columns.has('survey_id')) cells.push(`<td class="study-col-survey"><div class="study-id-stack"><strong class="study-survey-id" title="${escapeAttr(attempt.survey_source_id)}">${escapeHtml(attempt.survey_source_id)}</strong><small class="study-secondary" title="${escapeAttr(attempt.buyer_id ? `Buyer ${attempt.buyer_id}` : 'Buyer ID unavailable')}">${attempt.buyer_id ? `Buyer ${escapeHtml(attempt.buyer_id)}` : 'Buyer ID unavailable'}</small></div></td>`);
    if (columns.has('country')) cells.push(`<td class="study-col-country"><span class="study-country"><b>${escapeHtml(attempt.country_code || '—')}</b><small>${escapeHtml(attempt.country || attempt.country_code || 'Unknown')}</small></span></td>`);
    if (columns.has('cpi')) cells.push(`<td class="study-col-cpi"><span class="study-cpi"><b>${attempt.source_cpi_snapshot == null ? '—' : formatMoney(attempt.source_cpi_snapshot, attempt.cpi_currency_snapshot || 'USD')}</b><small>${attempt.cpi_snapshot_source === 'legacy_survey' ? 'Legacy recovered' : 'At hit time'}</small></span></td>`);
    if (columns.has('respondent_id')) cells.push(`<td class="study-col-rid"><strong class="respondent-id">${escapeHtml(attempt.rid)}</strong></td>`);
    if (columns.has('user')) cells.push(`<td class="study-col-user"><strong class="study-user-name">${escapeHtml(attempt.user_name)}</strong><small class="study-secondary">${escapeHtml(attempt.user_email || attempt.username || `User #${attempt.user_id}`)}</small></td>`);
    if (columns.has('device')) cells.push(`<td class="study-col-device">${deviceBadge(attempt)}</td>`);
    if (columns.has('ip')) cells.push(`<td class="study-col-ip">${ipPair(attempt)}</td>`);
    if (columns.has('loi')) cells.push(`<td class="study-col-loi"><strong class="study-loi">${formatLoi(attempt.loi_seconds)}</strong><small class="study-secondary">${attempt.loi_seconds == null ? 'Awaiting callback' : 'Actual duration'}</small></td>`);
    if (columns.has('status')) cells.push(`<td class="study-col-status">${statusPill(attempt)}</td>`);
    if (columns.has('start')) cells.push(`<td class="study-col-start">${timestampCell(attempt.initiated_at)}</td>`);
    if (columns.has('end')) cells.push(`<td class="study-col-end">${timestampCell(endTimestamp(attempt))}</td>`);
    return `<tr>${cells.length ? cells.join('') : '<td><div class="column-denied">No Traffic Report columns are assigned to your account.</div></td>'}</tr>`;
  }

  function cardTemplate(attempt) {
    if (!columns.size) return '<article class="survey-card study-card"><div class="column-denied">No Traffic Report columns are assigned to your account.</div></article>';
    const head = `${columns.has('respondent_id') ? `<div><strong>${escapeHtml(attempt.rid)}</strong><span>Respondent ID</span></div>` : '<div></div>'}${columns.has('status') ? statusPill(attempt) : ''}`;
    const survey = columns.has('survey_id') || columns.has('project_id') ? `<div class="study-card-survey">${columns.has('survey_id') ? `<span>Survey ${escapeHtml(attempt.survey_source_id)} · ${attempt.buyer_id ? `Buyer ${escapeHtml(attempt.buyer_id)}` : 'Buyer ID unavailable'}</span>` : ''}${columns.has('project_id') ? `<strong>${escapeHtml(attempt.survey_local_id)}</strong><small>${escapeHtml(attempt.client_name || attempt.company_name || 'Survey client')}</small>` : ''}</div>` : '';
    const metrics = `${columns.has('user') ? `<span><small>User</small><b>${escapeHtml(attempt.user_name)}</b></span>` : ''}${columns.has('country') ? `<span><small>Country</small><b>${escapeHtml(attempt.country || attempt.country_code || '—')}</b></span>` : ''}${columns.has('cpi') ? `<span><small>CPI</small><b>${attempt.source_cpi_snapshot == null ? '—' : formatMoney(attempt.source_cpi_snapshot, attempt.cpi_currency_snapshot || 'USD')}</b></span>` : ''}${columns.has('loi') ? `<span><small>LOI</small><b>${formatLoi(attempt.loi_seconds)}</b></span>` : ''}${columns.has('device') ? `<span><small>Device</small>${deviceBadge(attempt)}</span>` : ''}`;
    const times = columns.has('start') || columns.has('end') ? `<div class="study-card-times">${columns.has('start') ? `<time><small>Start</small><b>${formatIst(attempt.initiated_at)} IST</b></time>` : ''}${columns.has('end') ? `<time><small>End</small><b>${formatIst(endTimestamp(attempt))} IST</b></time>` : ''}</div>` : '';
    return `<article class="survey-card study-card"><div class="study-card-head">${head}</div>${survey}${metrics ? `<div class="study-card-grid">${metrics}</div>` : ''}${columns.has('ip') ? `<div class="study-card-network">${ipPair(attempt)}</div>` : ''}${times}</article>`;
  }

  async function loadAttempts() {
    state.controller?.abort(); state.controller = new AbortController();
    elements.rows.innerHTML = `<tr><td colspan="${columnCount}"><div class="table-loader"><i></i><span>Fetching respondent activity…</span></div></td></tr>`;
    try {
      const response = await fetch(`/api/v1/survey-attempts/?${filterParams()}`, { signal: state.controller.signal });
      const data = await response.json(); if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
      const results = data.results || []; const count = Number(data.count || 0);
      updateOverview(data.summary);
      state.pages = Math.max(1, Math.ceil(count / state.pageSize));
      if (state.page > state.pages) { state.page = state.pages; return loadAttempts(); }
      elements.summary.innerHTML = count ? `<strong>${count.toLocaleString('en-IN')}</strong> filtered respondent ${count === 1 ? 'journey' : 'journeys'}` : 'No attempts match these filters';
      elements.rows.innerHTML = results.length ? results.map(rowTemplate).join('') : `<tr><td colspan="${columnCount}"><div class="empty-state"><span>◎</span><strong>No traffic records found</strong><small>Try clearing the filters or start a survey attempt.</small></div></td></tr>`;
      elements.cards.innerHTML = results.length ? results.map(cardTemplate).join('') : '<div class="empty-state"><span>◎</span><strong>No traffic records found</strong><small>Try clearing the filters.</small></div>';
      if (elements.pageInput) { elements.pageInput.value = state.page; elements.pageInput.max = state.pages; }
      if (elements.totalPages) elements.totalPages.textContent = `of ${state.pages.toLocaleString('en-IN')}`;
      elements.pageStatus.textContent = `Page ${state.page.toLocaleString('en-IN')} of ${state.pages.toLocaleString('en-IN')}`;
      if (elements.first && elements.prev) elements.first.disabled = elements.prev.disabled = state.page <= 1;
      if (elements.next && elements.last) elements.next.disabled = elements.last.disabled = state.page >= state.pages;
    } catch (error) {
      if (error.name === 'AbortError') return;
      elements.rows.innerHTML = `<tr><td colspan="${columnCount}"><div class="error-state"><strong>Could not load traffic reports</strong><span>${escapeHtml(error.message)}</span><button type="button" id="retryStudies">Try again</button></div></td></tr>`;
      byId('retryStudies')?.addEventListener('click', loadAttempts); elements.cards.innerHTML = '';
    }
  }

  function scheduleLoad() { clearTimeout(state.timer); state.timer = setTimeout(() => { state.page = 1; loadAttempts(); }, 280); }
  function go(page) { state.page = Math.min(state.pages, Math.max(1, Number(page) || 1)); loadAttempts(); document.querySelector('.studies-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }

  elements.search?.addEventListener('input', scheduleLoad);
  [elements.from, elements.to].filter(Boolean).forEach((input) => input.addEventListener('input', scheduleLoad));
  elements.dateField?.addEventListener('change', scheduleLoad);
  elements.pageSize?.addEventListener('change', () => { state.pageSize = Number(elements.pageSize.value); state.page = 1; loadAttempts(); });
  elements.clear?.addEventListener('click', () => {
    if (elements.search) elements.search.value = ''; if (elements.dateField) elements.dateField.value = 'initiated';
    if (elements.from) elements.from.value = ''; if (elements.to) elements.to.value = '';
    document.querySelectorAll('.studies-filters .multi-select').forEach((container) => {
      container.querySelectorAll('input[type="checkbox"]').forEach((input) => { input.checked = false; });
      const searchInput = container.querySelector('[data-multi-search]'); if (searchInput) searchInput.value = '';
      updateMultiLabel(container);
    });
    state.projectId = '';
    byId('studyProjectScope')?.remove();
    window.history.replaceState({}, '', window.location.pathname);
    updateStudyHierarchyOptions(); updateStudyBuyerOptions();
    closeMultiSelects(); state.page = 1; loadAttempts();
  });
  byId('clearProjectScope')?.addEventListener('click', () => {
    state.projectId = '';
    byId('studyProjectScope')?.remove();
    window.history.replaceState({}, '', window.location.pathname);
    state.page = 1;
    loadAttempts();
  });
  elements.first?.addEventListener('click', () => go(1)); elements.prev?.addEventListener('click', () => go(state.page - 1));
  elements.next?.addEventListener('click', () => go(state.page + 1)); elements.last?.addEventListener('click', () => go(state.pages));
  elements.pageInput?.addEventListener('change', () => go(elements.pageInput.value));
  elements.export?.addEventListener('click', () => { elements.export.classList.add('exporting'); window.location.assign(`/api/v1/survey-attempts/export/?${filterParams(false)}`); setTimeout(() => elements.export.classList.remove('exporting'), 1000); });
  document.addEventListener('click', (event) => { if (!event.target.closest('.studies-filters .multi-select')) closeMultiSelects(); });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeMultiSelects(); });
  loadAttempts();
})();
