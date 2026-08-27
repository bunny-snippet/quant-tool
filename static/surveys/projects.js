/* Projects inventory filters, pagination, cards/table and survey-detail drawer. */

(() => {
  const $ = (id) => document.getElementById(id);
  if (!$('surveyRows')) return;
  const projectColumns = new Set(JSON.parse($('projectColumnAccess')?.textContent || '[]'));
  const canOpenProjectStudies = JSON.parse($('projectStudyLinkAccess')?.textContent || 'false');
  const canViewProjectClientName = JSON.parse($('projectClientNameAccess')?.textContent || 'false');
  const visibleColumnCount = Math.max(1, projectColumns.size);
  document.querySelector('.survey-table').style.minWidth = `${Math.max(620, visibleColumnCount * 112)}px`;

  const state = {
    page: 1,
    pages: 1,
    pageSize: 20,
    results: [],
    activeSurvey: null,
    activeTab: 'targeting',
    details: { targeting: null, quotas: null },
    detailErrors: { targeting: null, quotas: null },
    timer: null,
    controller: null,
    loading: false,
  };

  const els = {
    rows: $('surveyRows'), cards: $('mobileCards'), summary: $('resultSummary'), pageStatus: $('pageStatus'),
    totalPages: $('totalPages'), pageInput: $('pageInput'), first: $('firstPage'), prev: $('prevPage'),
    next: $('nextPage'), last: $('lastPage'), search: $('searchInput'), dateField: $('dateField'),
    from: $('fromDateTime'), to: $('toDateTime'),
    pageSize: $('pageSize'), clear: $('clearFilters'), sync: $('syncButton'), export: $('exportProjects'),
    drawer: $('detailDrawer'), backdrop: $('drawerBackdrop'), closeDrawer: $('closeDrawer'),
    drawerSurvey: $('drawerSurvey'), drawerContent: $('drawerContent'), tabs: [...document.querySelectorAll('.drawer-tab')],
    multiSelects: [...document.querySelectorAll('[data-multi-filter]')],
    cpiFilter: document.querySelector('[data-cpi-filter]'), cpiTrigger: $('cpiFilterTrigger'), cpiMenu: $('cpiFilterMenu'),
    orderingInputs: [...document.querySelectorAll('input[name="projectOrdering"]')],
    cpiMin: $('cpiMinRange'), cpiMax: $('cpiMaxRange'), cpiValue: $('cpiRangeValue'), cpiFill: $('cpiRangeFill'),
    cpiReset: $('resetCpiFilter'),
  };

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
  function projectIdControl(survey, title = "View this project's traffic reports") {
    const projectId = escapeHtml(survey.local_id);
    if (!canOpenProjectStudies) return `<strong class="id-link project-id-value">${projectId}</strong>`;
    const studyUrl = `/traffic-reports/?internal_id=${encodeURIComponent(survey.local_id)}`;
    return `<span class="id-link project-study-link" role="link" tabindex="0" data-study-url="${escapeHtml(studyUrl)}" title="${escapeHtml(title)}">${projectId}</span>`;
  }
  const formatDate = (value) => value ? new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : '—';
  const money = (value) => value == null ? '—' : `$${Number(value).toFixed(2)}`;
  const entryLinkControl = (survey) => survey.start_link
    ? `<button class="copy-link" data-copy-link="${escapeHtml(survey.start_link)}">Copy link</button>`
    : '<button class="copy-link" type="button" title="This survey is not live" disabled>Unavailable</button>';
  const filterDefaults = {
    country: 'All countries', status: 'All statuses', company: 'All clients', client_name: 'All clients',
    buyer_id: 'All buyer IDs', survey_type: 'All survey types',
  };
  const selectedBuyerValues = new Set();
  els.multiSelects.forEach((filter) => {
    if (filter.dataset.defaultLabel) filterDefaults[filter.dataset.multiFilter] = filter.dataset.defaultLabel;
  });

  function toast(message, kind = 'success') {
    const node = document.createElement('div');
    node.className = `toast ${kind}`;
    node.textContent = message;
    $('toastRegion').append(node);
    requestAnimationFrame(() => node.classList.add('show'));
    setTimeout(() => { node.classList.remove('show'); setTimeout(() => node.remove(), 250); }, 3800);
  }

  function sourceTimestamp(displayValue, fallbackValue) {
    if (!fallbackValue) return '<strong class="source-time">—</strong>';
    const date = new Date(fallbackValue);
    const datePart = new Intl.DateTimeFormat('en-IN', {
      day: '2-digit', month: 'short', year: 'numeric', timeZone: 'Asia/Kolkata',
    }).format(date);
    const timePart = new Intl.DateTimeFormat('en-IN', {
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true, timeZone: 'Asia/Kolkata',
    }).format(date);
    return `<span class="source-date">${escapeHtml(datePart)}</span><strong class="source-time">${escapeHtml(timePart)} <em>IST</em></strong>`;
  }

  function selectedValues(filter) {
    if (!filter) return [];
    if (filter.dataset.multiFilter === 'buyer_id' && filter.dataset.lazyOptionsUrl) {
      return [...selectedBuyerValues];
    }
    return [...filter.querySelectorAll('input:checked')].map((input) => input.value);
  }

  function updateMultiLabel(filter) {
    const values = selectedValues(filter);
    const key = filter.dataset.multiFilter;
    const label = filter.querySelector('.multi-trigger span');
    if (!values.length) label.textContent = filterDefaults[key];
    else if (values.length === 1) label.textContent = values[0];
    else label.textContent = `${values.length} selected`;
    filter.querySelector('.multi-trigger').classList.toggle('has-value', values.length > 0);
  }

  const clientFilter = els.multiSelects.find((filter) => ['company', 'client_name'].includes(filter.dataset.multiFilter));
  const buyerFilter = els.multiSelects.find((filter) => filter.dataset.multiFilter === 'buyer_id');
  let buyerOptionsLoaded = false;
  let buyerOptionsKey = '';
  let buyerOptionsTimer = null;
  let buyerOptionsController = null;

  function resetBuyerOptions() {
    if (!buyerFilter?.dataset.lazyOptionsUrl) return;
    buyerOptionsController?.abort();
    buyerOptionsLoaded = false;
    buyerOptionsKey = '';
    buyerFilter.querySelector('.multi-options').innerHTML = '<span class="filter-empty">Open or search to load buyer IDs</span>';
    const noResults = buyerFilter.querySelector('.multi-no-results');
    if (noResults) noResults.hidden = true;
    updateMultiLabel(buyerFilter);
  }

  async function loadBuyerOptions({ force = false } = {}) {
    if (!buyerFilter?.dataset.lazyOptionsUrl) return;
    const search = buyerFilter.querySelector('[data-multi-search]')?.value.trim() || '';
    const clients = selectedValues(clientFilter);
    const clientParameter = clientFilter?.dataset.multiFilter || '';
    const key = JSON.stringify([search, clientParameter, clients]);
    if (!force && buyerOptionsLoaded && key === buyerOptionsKey) return;

    buyerOptionsController?.abort();
    buyerOptionsController = new AbortController();
    const options = buyerFilter.querySelector('.multi-options');
    options.innerHTML = '<span class="filter-empty">Loading buyer IDs…</span>';
    try {
      const params = new URLSearchParams();
      if (search) params.set('search', search);
      if (clientParameter && clients.length) params.set(clientParameter, clients.join(','));
      const response = await fetch(`${buyerFilter.dataset.lazyOptionsUrl}?${params.toString()}`, {
        signal: buyerOptionsController.signal,
        credentials: 'same-origin',
      });
      if (!response.ok) throw new Error(`Request failed (${response.status})`);
      const data = await response.json();
      const rows = new Map();
      (data.results || []).forEach((row) => {
        const value = String(row.value || '');
        if (value && !rows.has(value)) rows.set(value, row);
      });
      // Keep current selections visible even when a new search page does not
      // contain them, so changing the search text never silently drops a filter.
      selectedBuyerValues.forEach((value) => {
        if (!rows.has(value)) rows.set(value, { value, client_value: '' });
      });
      options.innerHTML = rows.size
        ? [...rows.values()].map((row) => {
          const value = String(row.value);
          const checked = selectedBuyerValues.has(value) ? ' checked' : '';
          return `<label data-client-value="${escapeHtml(row.client_value || '')}"><input type="checkbox" value="${escapeHtml(value)}"${checked}><span>${escapeHtml(value)}</span></label>`;
        }).join('') + (data.has_more ? '<span class="filter-empty">More matches available — refine your search</span>' : '')
        : '<span class="filter-empty">No matching buyer ID</span>';
      buyerOptionsLoaded = true;
      buyerOptionsKey = key;
      const noResults = buyerFilter.querySelector('.multi-no-results');
      if (noResults) noResults.hidden = true;
      updateMultiLabel(buyerFilter);
    } catch (error) {
      if (error.name === 'AbortError') return;
      options.innerHTML = `<span class="filter-empty">Could not load buyer IDs · ${escapeHtml(error.message)}</span>`;
    }
  }

  function updateBuyerOptions() {
    if (!buyerFilter?.dataset.lazyOptionsUrl) return;
    if (buyerFilter.classList.contains('open')) loadBuyerOptions({ force: true });
    else resetBuyerOptions();
  }

  function closeMultiSelects(except = null) {
    els.multiSelects.forEach((filter) => {
      if (filter === except) return;
      filter.classList.remove('open');
      filter.querySelector('.multi-trigger').setAttribute('aria-expanded', 'false');
      filter.querySelector('.multi-menu').hidden = true;
    });
  }

  els.multiSelects.forEach((filter) => {
    const trigger = filter.querySelector('.multi-trigger');
    const menu = filter.querySelector('.multi-menu');
    trigger.addEventListener('click', (event) => {
      event.stopPropagation();
      const willOpen = !filter.classList.contains('open');
      closeCpiFilter();
      closeMultiSelects(filter);
      filter.classList.toggle('open', willOpen);
      trigger.setAttribute('aria-expanded', String(willOpen));
      menu.hidden = !willOpen;
      if (willOpen && filter === buyerFilter) loadBuyerOptions();
    });
    menu.addEventListener('click', (event) => event.stopPropagation());
    menu.addEventListener('change', (event) => {
      if (filter === buyerFilter && event.target.matches('input[type="checkbox"]')) {
        if (event.target.checked) selectedBuyerValues.add(event.target.value);
        else selectedBuyerValues.delete(event.target.value);
      }
      updateMultiLabel(filter);
      if (filter === clientFilter) {
        // Buyer IDs are a child filter. Changing clients invalidates prior
        // buyer choices exactly as the former embedded-option implementation.
        selectedBuyerValues.clear();
        updateBuyerOptions();
      }
      scheduleLoad();
    });
    const search = menu.querySelector('[data-multi-search]');
    search?.addEventListener('input', () => {
      if (filter === buyerFilter && buyerFilter.dataset.lazyOptionsUrl) {
        clearTimeout(buyerOptionsTimer);
        buyerOptionsTimer = setTimeout(() => loadBuyerOptions({ force: true }), 180);
        return;
      }
      const term = search.value.trim().toLocaleLowerCase();
      const options = [...menu.querySelectorAll('.multi-options label')];
      let visible = 0;
      options.forEach((option) => {
        const matches = option.textContent.toLocaleLowerCase().includes(term);
        const selectedClients = filter === buyerFilter ? selectedValues(clientFilter) : [];
        const clientMatches = filter !== buyerFilter || !selectedClients.length || selectedClients.includes(option.dataset.clientValue || '');
        option.hidden = !(matches && clientMatches);
        if (!option.hidden) visible += 1;
      });
      const noResults = menu.querySelector('.multi-no-results');
      if (noResults) noResults.hidden = visible > 0;
    });
    updateMultiLabel(filter);
  });
  updateBuyerOptions();

  function selectedOrdering() {
    return els.orderingInputs.find((input) => input.checked) || null;
  }

  function closeCpiFilter() {
    if (!els.cpiFilter) return;
    els.cpiFilter.querySelector('.cpi-filter-select')?.classList.remove('open');
    els.cpiTrigger.setAttribute('aria-expanded', 'false');
    els.cpiMenu.hidden = true;
  }

  function updateCpiControl(changedInput = null, reload = false) {
    if (!els.cpiFilter) return;
    let minimum = Number(els.cpiMin.value);
    let maximum = Number(els.cpiMax.value);
    if (minimum > maximum) {
      if (changedInput === els.cpiMax) els.cpiMin.value = String(maximum);
      else els.cpiMax.value = String(minimum);
      minimum = Number(els.cpiMin.value);
      maximum = Number(els.cpiMax.value);
    }
    const boundMin = Number(els.cpiMin.min);
    const boundMax = Number(els.cpiMax.max);
    const span = Math.max(0.01, boundMax - boundMin);
    const left = ((minimum - boundMin) / span) * 100;
    const right = ((maximum - boundMin) / span) * 100;
    els.cpiFill.style.left = `${left}%`;
    els.cpiFill.style.width = `${Math.max(0, right - left)}%`;
    els.cpiValue.textContent = `${money(minimum)} – ${money(maximum)}`;
    const ordering = selectedOrdering();
    const rangeActive = minimum > boundMin || maximum < boundMax;
    els.cpiTrigger.querySelector('span').textContent = rangeActive
      ? `${ordering?.dataset.label || 'Recently updated'} · ${money(minimum)}–${money(maximum)}`
      : (ordering?.dataset.label || 'Recently updated');
    els.cpiTrigger.classList.toggle('has-value', rangeActive || ordering?.value !== '-source_modified_at');
    if (reload) scheduleLoad();
  }

  function resetCpiControl(reload = false) {
    if (!els.cpiFilter) return;
    const defaultOrdering = els.orderingInputs.find((input) => input.value === '-source_modified_at');
    if (defaultOrdering) defaultOrdering.checked = true;
    els.cpiMin.value = els.cpiMin.min;
    els.cpiMax.value = els.cpiMax.max;
    updateCpiControl(null, reload);
  }

  els.cpiTrigger?.addEventListener('click', (event) => {
    event.stopPropagation();
    const select = els.cpiFilter.querySelector('.cpi-filter-select');
    const willOpen = !select.classList.contains('open');
    closeMultiSelects();
    select.classList.toggle('open', willOpen);
    els.cpiTrigger.setAttribute('aria-expanded', String(willOpen));
    els.cpiMenu.hidden = !willOpen;
  });
  els.cpiMenu?.addEventListener('click', (event) => event.stopPropagation());
  els.orderingInputs.forEach((input) => input.addEventListener('change', () => {
    updateCpiControl(); state.page = 1; loadSurveys();
  }));
  [els.cpiMin, els.cpiMax].filter(Boolean).forEach((input) => {
    input.addEventListener('input', () => updateCpiControl(input, true));
    input.addEventListener('keydown', (event) => {
      const step = Number(input.step) || 0.01;
      const shortcuts = {
        ArrowLeft: Number(input.value) - step, ArrowDown: Number(input.value) - step,
        ArrowRight: Number(input.value) + step, ArrowUp: Number(input.value) + step,
        PageDown: Number(input.value) - (step * 10), PageUp: Number(input.value) + (step * 10),
        Home: Number(input.min), End: Number(input.max),
      };
      if (!(event.key in shortcuts)) return;
      event.preventDefault();
      input.value = String(Math.min(Number(input.max), Math.max(Number(input.min), shortcuts[event.key])));
      updateCpiControl(input, true);
    });
  });
  els.cpiReset?.addEventListener('click', () => resetCpiControl(true));

  function queryString(includePage = true) {
    const params = new URLSearchParams({ ordering: selectedOrdering()?.value || '-source_modified_at' });
    if (includePage) {
      params.set('page', state.page);
      params.set('page_size', state.pageSize);
    }
    if (els.search?.value.trim()) params.set('search', els.search.value.trim());
    els.multiSelects.forEach((filter) => {
      const values = selectedValues(filter);
      if (values.length) params.set(filter.dataset.multiFilter, values.join(','));
    });
    const prefix = els.dateField?.value;
    if (prefix && els.from?.value) params.set(`${prefix}_from`, dateBoundary(els.from.value));
    if (prefix && els.to?.value) params.set(`${prefix}_to`, dateBoundary(els.to.value, true));
    if (els.cpiMin && Number(els.cpiMin.value) > Number(els.cpiMin.min)) params.set('min_cpi', els.cpiMin.value);
    if (els.cpiMax && Number(els.cpiMax.value) < Number(els.cpiMax.max)) params.set('max_cpi', els.cpiMax.value);
    return params.toString();
  }

  function dateBoundary(dateTime, endOfMinute = false) {
    const [date, selectedTime = '00:00'] = dateTime.split('T');
    const clock = selectedTime || (endOfMinute ? '23:59' : '00:00');
    const seconds = endOfMinute ? '59.999' : '00';
    return `${date}T${clock}:${seconds}+05:30`;
  }

  async function loadSurveys({ silent = false } = {}) {
    // A background refresh must never abort a still-running foreground load.
    // On a saturated database the former 30-second timer could repeatedly
    // cancel and restart the request, leaving the table on its loader forever.
    if (silent && state.loading) return;
    state.controller?.abort();
    const controller = new AbortController();
    state.controller = controller;
    state.loading = true;
    if (!silent) {
      els.rows.innerHTML = `<tr><td colspan="${visibleColumnCount}"><div class="table-loader"><i></i><span>Fetching survey inventory…</span></div></td></tr>`;
      els.cards.innerHTML = '<div class="mobile-loading">Fetching surveys…</div>';
    }
    try {
      const response = await fetch(`/api/v1/surveys/?${queryString()}`, { signal: controller.signal });
      if (!response.ok) throw new Error(`Request failed (${response.status})`);
      const data = await response.json();
      state.results = data.results || [];
      state.pages = Math.max(1, Math.ceil(data.count / state.pageSize));
      if (state.page > state.pages) { state.page = state.pages; return loadSurveys({ silent }); }
      render(data.count);
    } catch (error) {
      if (error.name === 'AbortError') return;
      if (silent) return;
      els.rows.innerHTML = `<tr><td colspan="${visibleColumnCount}"><div class="error-state"><strong>Could not load surveys</strong><span>${escapeHtml(error.message)}</span><button id="retryLoad">Try again</button></div></td></tr>`;
      els.cards.innerHTML = '';
      $('retryLoad')?.addEventListener('click', loadSurveys);
    } finally {
      if (state.controller === controller) state.loading = false;
    }
  }

  function render(count) {
    const start = count ? (state.page - 1) * state.pageSize + 1 : 0;
    const end = Math.min(state.page * state.pageSize, count);
    els.summary.innerHTML = count ? `Showing <strong>${start}–${end}</strong> of <strong>${count.toLocaleString()}</strong> surveys` : 'No surveys match these filters';
    if (!state.results.length) {
      els.rows.innerHTML = `<tr><td colspan="${visibleColumnCount}"><div class="empty-state"><span>⌕</span><strong>No surveys found</strong><small>Try clearing filters or synchronize new inventory.</small></div></td></tr>`;
      els.cards.innerHTML = '<div class="empty-state"><span>⌕</span><strong>No surveys found</strong><small>Try clearing filters.</small></div>';
    } else {
      els.rows.innerHTML = state.results.map(rowTemplate).join('');
      els.cards.innerHTML = state.results.map(cardTemplate).join('');
    }
    if (els.pageInput) { els.pageInput.value = state.page; els.pageInput.max = state.pages; }
    if (els.totalPages) els.totalPages.textContent = `of ${state.pages.toLocaleString()}`;
    els.pageStatus.textContent = `Page ${state.page.toLocaleString()} of ${state.pages.toLocaleString()}`;
    if (els.first && els.prev) els.first.disabled = els.prev.disabled = state.page <= 1;
    if (els.next && els.last) els.next.disabled = els.last.disabled = state.page >= state.pages;
  }

  function rowTemplate(survey) {
    const percent = Math.min(100, Number(survey.progress_percent || 0));
    const cells = [];
    const clientName = survey.client_name || survey.display_company_name || survey.company_name || 'Survey client';
    if (projectColumns.has('project_id')) cells.push(`<td><div class="project-id-stack">${projectIdControl(survey)}${canViewProjectClientName ? `<small>${escapeHtml(clientName)}</small>` : ''}</div></td>`);
    if (projectColumns.has('survey')) cells.push(`<td><div class="survey-name"><strong>${escapeHtml(survey.display_source_id ?? survey.source_id ?? '—')}</strong><span>${survey.buyer_id ? escapeHtml(survey.buyer_id) : 'Buyer ID unavailable'}</span></div></td>`);
    if (projectColumns.has('market')) cells.push(`<td><span class="market-pill">${escapeHtml(survey.country_code || '—')} <i>${escapeHtml(survey.language_code || '')}</i></span><small class="country-name">${escapeHtml(survey.country || '')}</small></td>`);
    if (projectColumns.has('completes')) cells.push(`<td><div class="complete-value"><strong>${survey.completes.toLocaleString()} / ${survey.sample_size.toLocaleString()}</strong><span><i style="width:${percent}%"></i></span></div></td>`);
    if (projectColumns.has('cpi')) cells.push(`<td><strong class="cpi">${money(survey.cpi)}</strong></td>`);
    if (projectColumns.has('loi_ir')) cells.push(`<td><div class="metric-pair"><span><b>${survey.loi ?? '—'}</b> min</span><span><b>${survey.incidence_rate ?? '—'}</b>%</span></div><small class="survey-type-tag">${escapeHtml(survey.survey_type || survey.group_type || 'Type unavailable')}</small></td>`);
    if (projectColumns.has('entry_link')) cells.push(`<td>${entryLinkControl(survey)}</td>`);
    if (projectColumns.has('modified')) cells.push(`<td><div class="source-timestamp">${sourceTimestamp(survey.source_modified_display, survey.source_modified_at)}</div><small class="created-date">Created ${escapeHtml(survey.source_created_display || formatDate(survey.source_created_at))}</small><small class="status ${survey.status}"><i></i>${escapeHtml(survey.status)}</small></td>`);
    if (projectColumns.has('actions')) cells.push(`<td><button class="eye-button" data-action="${escapeHtml(survey.local_id)}" aria-label="View details for ${escapeHtml(survey.name)}">◉</button></td>`);
    return `<tr>${cells.length ? cells.join('') : '<td><div class="column-denied">No project columns are assigned to your account.</div></td>'}</tr>`;
  }

  function cardTemplate(survey) {
    if (!projectColumns.size) return '<article class="survey-card"><div class="column-denied">No project columns are assigned to your account.</div></article>';
    const clientName = survey.client_name || survey.display_company_name || survey.company_name || 'Survey client';
    const top = `${projectColumns.has('project_id') ? `${projectIdControl(survey)}${canViewProjectClientName ? `<small class="project-card-client">${escapeHtml(clientName)}</small>` : ''}` : ''}${projectColumns.has('modified') ? `<span class="status ${survey.status}"><i></i>${escapeHtml(survey.status)}</span>` : ''}`;
    const metrics = [];
    if (projectColumns.has('market')) metrics.push(`<span><small>Market</small><b>${escapeHtml(survey.country_code || '—')} ${escapeHtml(survey.language_code || '')}</b></span>`);
    if (projectColumns.has('completes')) metrics.push(`<span><small>Completes</small><b>${survey.completes} / ${survey.sample_size}</b></span>`);
    if (projectColumns.has('cpi')) metrics.push(`<span><small>CPI</small><b>${money(survey.cpi)}</b></span>`);
    if (projectColumns.has('loi_ir')) metrics.push(`<span><small>LOI / IR · Type</small><b>${survey.loi ?? '—'}m · ${survey.incidence_rate ?? '—'}% · ${escapeHtml(survey.survey_type || survey.group_type || '—')}</b></span>`);
    const bottom = `${projectColumns.has('modified') ? `<div class="source-timestamp"><small>Updated</small>${sourceTimestamp(survey.source_modified_display, survey.source_modified_at)}</div>` : ''}${projectColumns.has('entry_link') ? entryLinkControl(survey) : ''}`;
    return `<article class="survey-card"><div class="card-top"><div>${top}</div>${projectColumns.has('actions') ? `<button class="eye-button" data-action="${escapeHtml(survey.local_id)}" aria-label="View survey details">◉</button>` : ''}</div>${projectColumns.has('survey') ? `<h3>${escapeHtml(survey.display_source_id ?? survey.source_id ?? '—')}</h3><p>${survey.buyer_id ? escapeHtml(survey.buyer_id) : 'Buyer ID unavailable'}</p>` : ''}${metrics.length ? `<div class="card-grid">${metrics.join('')}</div>` : ''}${bottom ? `<div class="card-bottom">${bottom}</div>` : ''}</article>`;
  }

  function scheduleLoad() {
    clearTimeout(state.timer);
    state.timer = setTimeout(() => { state.page = 1; loadSurveys(); }, 280);
  }

  [els.search, els.from, els.to].filter(Boolean).forEach((element) => element.addEventListener('input', scheduleLoad));
  els.dateField?.addEventListener('change', () => { state.page = 1; loadSurveys(); });
  els.pageSize?.addEventListener('change', () => { state.pageSize = Number(els.pageSize.value); state.page = 1; loadSurveys(); });
  els.clear?.addEventListener('click', () => {
    if (els.search) els.search.value = '';
    if (els.dateField) els.dateField.value = 'modified';
    if (els.from) els.from.value = '';
    if (els.to) els.to.value = '';
    selectedBuyerValues.clear();
    els.multiSelects.forEach((filter) => {
      filter.querySelectorAll('input[type="checkbox"]').forEach((input) => { input.checked = false; });
      const search = filter.querySelector('[data-multi-search]');
      if (search) {
        search.value = '';
        if (filter !== buyerFilter) search.dispatchEvent(new Event('input'));
      }
      updateMultiLabel(filter);
    });
    resetBuyerOptions();
    resetCpiControl();
    closeMultiSelects(); closeCpiFilter(); state.page = 1; loadSurveys();
  });
  els.first?.addEventListener('click', () => go(1));
  els.prev?.addEventListener('click', () => go(state.page - 1));
  els.next?.addEventListener('click', () => go(state.page + 1));
  els.last?.addEventListener('click', () => go(state.pages));
  els.pageInput?.addEventListener('change', () => go(Number(els.pageInput.value)));

  function go(page) {
    state.page = Math.min(state.pages, Math.max(1, page || 1));
    loadSurveys();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  document.addEventListener('click', async (event) => {
    if (!event.target.closest('.multi-select')) closeMultiSelects();
    if (!event.target.closest('[data-cpi-filter]')) closeCpiFilter();
    const studyTarget = event.target.closest('[data-study-url]');
    if (studyTarget) {
      const selection = window.getSelection();
      const selectingProjectId = selection && !selection.isCollapsed && selection.containsNode(studyTarget, true);
      if (!selectingProjectId) window.location.assign(studyTarget.dataset.studyUrl);
      return;
    }
    const copy = event.target.closest('[data-copy]');
    if (copy) { await navigator.clipboard.writeText(copy.dataset.copy); toast('Project ID copied'); }
    const copyLink = event.target.closest('[data-copy-link]');
    if (copyLink && copyLink.dataset.copyLink) { await navigator.clipboard.writeText(copyLink.dataset.copyLink); toast('Entry link copied'); }
    const action = event.target.closest('[data-action]');
    if (action) {
      const survey = state.results.find((item) => item.local_id === action.dataset.action);
      if (survey) openDrawer(survey);
    }
  });
  document.addEventListener('keydown', (event) => {
    const studyTarget = event.target.closest('[data-study-url]');
    if (studyTarget && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      window.location.assign(studyTarget.dataset.studyUrl);
    }
  });

  function setActiveTab(tabName) {
    state.activeTab = tabName;
    els.tabs.forEach((tab) => {
      const active = tab.dataset.tab === tabName;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
    });
    renderActiveDetail();
  }

  function openDrawer(survey) {
    state.activeSurvey = survey;
    state.details = { targeting: null, quotas: null };
    state.detailErrors = { targeting: null, quotas: null };
    els.drawer.hidden = els.backdrop.hidden = false;
    document.body.classList.add('drawer-open');
    const clientName = survey.client_name || survey.display_company_name || survey.company_name || 'Survey client';
    els.drawerSurvey.innerHTML = `<div class="drawer-survey-primary">${canViewProjectClientName ? `<strong>${escapeHtml(clientName)}</strong>` : ''}<span>${escapeHtml(survey.country_label || 'Market unavailable')}</span></div><div class="drawer-project-code"><small>Project ID</small><b>${escapeHtml(survey.local_id)}</b></div>`;
    setActiveTab('targeting');
    requestAnimationFrame(() => { els.drawer.classList.add('open'); els.backdrop.classList.add('open'); });
    loadDrawerDetails(survey);
    els.closeDrawer.focus();
  }

  async function loadDrawerDetails(survey) {
    for (const type of ['targeting', 'quotas']) {
      try {
        const response = await fetch(`/api/v1/surveys/${survey.local_id}/${type}/`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
        if (state.activeSurvey?.local_id !== survey.local_id) return;
        state.details[type] = data;
      } catch (error) {
        if (state.activeSurvey?.local_id !== survey.local_id) return;
        state.detailErrors[type] = error.message;
      }
      if (state.activeTab === type) renderActiveDetail();
    }
  }

  function renderActiveDetail() {
    els.drawerContent.classList.remove('content-swap');
    void els.drawerContent.offsetWidth;
    els.drawerContent.classList.add('content-swap');
    const type = state.activeTab;
    if (state.detailErrors[type]) {
      els.drawerContent.innerHTML = `<div class="error-state"><strong>Could not load details</strong><span>${escapeHtml(state.detailErrors[type])}</span></div>`;
    } else if (state.details[type] === null) {
      els.drawerContent.innerHTML = '<div class="drawer-loader"><i></i><span>Loading current details…</span></div>';
    } else {
      els.drawerContent.innerHTML = type === 'quotas' ? renderQuotas(state.details[type]) : renderQuestions(state.details[type]);
    }
  }

  function closeDrawer() {
    els.drawer.classList.remove('open'); els.backdrop.classList.remove('open'); document.body.classList.remove('drawer-open');
    setTimeout(() => { els.drawer.hidden = els.backdrop.hidden = true; state.activeSurvey = null; }, 220);
  }

  els.tabs.forEach((tab) => tab.addEventListener('click', () => setActiveTab(tab.dataset.tab)));
  els.closeDrawer.addEventListener('click', closeDrawer);
  els.backdrop.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') { closeMultiSelects(); closeCpiFilter(); if (!els.drawer.hidden) closeDrawer(); } });

  function renderQuotas(items) {
    if (!items.length) return '<div class="detail-empty"><div class="detail-empty-visual" aria-hidden="true"><span></span><span></span><span></span><i>✓</i></div><strong>No quota data</strong><p>This survey currently has no quota definitions.</p></div>';
    return `<div class="quota-help"><strong>How to read this</strong><span>Overall quota capacity is shown at the top of each card. Provider subquota capacity stays inside its own segment and is never added across layers.</span></div><div class="detail-list">${items.map((quota, index) => {
      const targeting = quota.toluna_layers?.length
        ? tolunaQuotaBreakdown(quota.toluna_layers)
        : quotaTargeting(quota.targeting_details);
      return `<article class="quota-item"><div class="detail-index">${String(index + 1).padStart(2, '0')}</div><div class="detail-main"><div class="detail-title"><div><strong>${escapeHtml(quota.display_name || quota.name || quota.title || 'Survey quota')}</strong><small class="quota-scope">${escapeHtml(quota.scope_label || 'Quota scope')} · limited by ${escapeHtml(quota.limit_type || 'Completes')}</small></div><span class="quota-status quota-status-${escapeHtml(String(quota.status || 'unknown').toLowerCase())}">${escapeHtml(quota.status || 'Unknown')}</span></div><div class="quota-stats"><span><small>Target</small><b>${quota.target_known ? escapeHtml(quota.sample_size) : '<em>Not provided</em>'}</b></span><span><small>Completed</small><b>${quota.completed_known ? escapeHtml(quota.completes) : '<em>Not provided</em>'}</b></span><span><small>Remaining</small><b>${escapeHtml(quota.remaining)}</b></span></div>${targeting}</div></article>`;
    }).join('')}</div>`;
  }

  function quotaTargeting(details) {
    if (!details?.length) return '<div class="quota-targeting quota-targeting-overall"><strong>Overall quota</strong><span>Applies to every respondent entering this survey.</span></div>';
    return `<div class="quota-targeting"><strong>Who this quota applies to</strong>${details.map(detail => `<div><label>${escapeHtml(detail.name)}</label><span>${(detail.values || []).map(escapeHtml).join(', ')}</span></div>`).join('')}</div>`;
  }

  function tolunaSegmentTitle(details, position) {
    const labels = (details || []).flatMap((detail) => detail.values || []).filter(Boolean);
    if (labels.length) return labels.slice(0, 4).join(', ') + (labels.length > 4 ? ` +${labels.length - 4} more` : '');
    return `Segment ${position}`;
  }

  function tolunaSegmentStats(segment) {
    if (!segment.target_known && !segment.completed_known && !segment.remaining_known) {
      return '<p class="toluna-capacity-missing">Subquota capacity was not supplied by Toluna.</p>';
    }
    const metric = (label, known, value) => `<span><small>${label}</small><b>${known ? escapeHtml(value) : '<em>Not provided</em>'}</b></span>`;
    const progress = segment.target_known && segment.completed_known && Number(segment.target) > 0
      ? Math.max(0, Math.min(100, (Number(segment.completed) / Number(segment.target)) * 100))
      : null;
    return `<div class="toluna-segment-stats">${metric('Target', segment.target_known, segment.target)}${metric('Completed', segment.completed_known, segment.completed)}${metric('Remaining', segment.remaining_known, segment.remaining)}</div>${progress === null ? '' : `<div class="toluna-capacity-bar" aria-label="${progress.toFixed(0)} percent complete"><i style="width:${progress.toFixed(2)}%"></i></div>`}`;
  }

  function tolunaQuotaBreakdown(layers) {
    return `<section class="toluna-quota-breakdown"><header class="toluna-logic"><div><strong>Quota composition</strong><span>Every condition inside the selected segment applies together.</span></div><div><b>ALL layers</b><i>+</i><b>ANY one segment per layer</b><i>+</i><b>ALL segment conditions</b></div></header>${layers.map((layer, layerIndex) => {
      const segments = layer.subquotas || [];
      const layerName = layer.name || `Layer ${layer.position || layerIndex + 1}`;
      return `<details class="toluna-layer" ${layerIndex === 0 ? 'open' : ''}><summary><span><small>Layer ${escapeHtml(layer.position || layerIndex + 1)}</small><strong>${escapeHtml(layerName)}</strong></span><b>${segments.length} ${segments.length === 1 ? 'segment' : 'segments'}</b></summary><div class="toluna-layer-body">${segments.map((segment, segmentIndex) => {
        const details = segment.targeting_details || [];
        const statusClass = String(segment.status || 'unknown').toLowerCase();
        return `<article class="toluna-segment"><header><div><small>Eligible segment ${escapeHtml(segment.position || segmentIndex + 1)}</small><strong>${escapeHtml(tolunaSegmentTitle(details, segment.position || segmentIndex + 1))}</strong></div><span class="quota-status quota-status-${escapeHtml(statusClass)}">${escapeHtml(segment.status || 'Unknown')}</span></header>${tolunaSegmentStats(segment)}<div class="toluna-segment-criteria">${details.map((detail) => `<div><label>${escapeHtml(detail.name)}${detail.is_routable ? '<em>Asked by Toluna</em>' : ''}</label><span>${(detail.values || []).map(escapeHtml).join(', ') || 'Provider-defined segment'}</span></div>`).join('') || '<p>Applies to all respondents in this segment.</p>'}</div></article>`;
      }).join('') || '<p class="toluna-capacity-missing">No subquota segments were supplied for this layer.</p>'}</div></details>`;
    }).join('')}</section>`;
  }

  function renderQuestions(items) {
    if (!items.length) return '<div class="detail-empty"><div class="detail-empty-visual" aria-hidden="true"><span></span><span></span><span></span><i>✓</i></div><strong>No pre-screening questions</strong><p>This survey does not require any targeting questions right now.</p></div>';
    const hasQualifyingChoices = items.some(question => (question.options || []).some(option => option.Qualifies === true));
    const legend = hasQualifyingChoices ? '<div class="targeting-legend"><strong>Provider targeting choices</strong><span><i>✓</i> Green answers match the provider-returned qualification.</span><span>Grey answers are outside the returned qualification.</span></div>' : '';
    return `${legend}<div class="detail-list">${items.map((question, index) => `<article class="question-item"><div class="detail-index">${String(index + 1).padStart(2, '0')}</div><div class="detail-main"><div class="question-meta"><span>${escapeHtml(question.category || 'General')}</span><span>${escapeHtml(question.question_type || 'Question')}</span></div><h3>${escapeHtml(question.text || question.key)}</h3><small>${escapeHtml(question.key)}</small>${question.targeting_note ? `<div class="targeting-rule">${escapeHtml(question.targeting_note)}</div>` : ''}<div class="option-list">${(question.options || []).map((option) => { const label=escapeHtml(option.OptionText || (option.ageStart != null ? `${option.ageStart}–${option.ageEnd ?? 99}` : option.OptionId)); const cls=option.Qualifies===true?'qualifying-option':option.Qualifies===false?'non-qualifying-option':''; return `<span class="${cls}">${option.Qualifies===true?'<b>✓</b> ':''}${label}</span>`; }).join('') || '<em>No fixed options</em>'}</div></div></article>`).join('')}</div>`;
  }

  els.export?.addEventListener('click', () => {
    closeMultiSelects(); closeCpiFilter();
    els.export.classList.add('exporting');
    window.location.assign(`/api/v1/surveys/export/?${queryString(false)}`);
    setTimeout(() => els.export.classList.remove('exporting'), 1200);
  });

  els.sync?.addEventListener('click', async () => {
    els.sync.disabled = true; els.sync.classList.add('syncing'); els.sync.lastChild.textContent = ' Syncing…';
    try {
      const response = await fetch('/api/v1/sync/?wait=true', { method: 'POST', headers: { 'X-CSRFToken': document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '' } });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || data.error || `Sync failed (${response.status})`);
      toast(`Sync complete · ${data.created} new, ${data.updated} updated`); state.page = 1; await loadSurveys();
    } catch (error) { toast(error.message, 'error'); }
    finally { els.sync.disabled = false; els.sync.classList.remove('syncing'); els.sync.lastChild.textContent = ' Sync now'; }
  });

  updateCpiControl();
  loadSurveys();
  window.setInterval(() => {
    if (!document.hidden) loadSurveys({ silent: true });
  }, 30000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) loadSurveys({ silent: true });
  });
})();
