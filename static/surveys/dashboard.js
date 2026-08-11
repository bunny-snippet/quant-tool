(() => {
  const byId = (id) => document.getElementById(id);
  const filterPanel = document.querySelector('.dashboard-filters');
  const state = { controller: null, timer: null, period: 'daily', performance: null };
  const palette = ['#13b9da', '#3457d5', '#27b780', '#efad35', '#9a6de3', '#ee6572', '#55718f', '#22a5a1'];
  const icon = {
    desktop: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/></svg>',
    mobile: '<svg viewBox="0 0 24 24"><rect x="6" y="2" width="12" height="20" rx="2"/><path d="M10 18h4"/></svg>',
    tablet: '<svg viewBox="0 0 24 24"><rect x="5" y="2" width="14" height="20" rx="2"/><circle cx="12" cy="18" r=".7"/></svg>',
    unknown: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M9.7 9a2.4 2.4 0 0 1 4.6 1c0 1.8-2.3 2-2.3 4m0 3h.01"/></svg>',
  };

  function escapeHtml(value) {
    const node = document.createElement('div');
    node.textContent = value == null ? '' : String(value);
    return node.innerHTML;
  }

  const number = (value) => Number(value || 0).toLocaleString('en-IN');
  const selectedValues = (container) => [...container.querySelectorAll('input:checked')].map((input) => input.value);

  function animateNumber(element, target, formatter = number) {
    if (!element) return;
    const finalValue = Number(target || 0);
    const duration = 720;
    const startAt = performance.now();
    const startValue = Number(element.dataset.value || 0);
    element.dataset.value = String(finalValue);
    function frame(now) {
      const progress = Math.min(1, (now - startAt) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      element.textContent = formatter(startValue + (finalValue - startValue) * eased);
      if (progress < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function formatLoi(seconds) {
    const total = Math.max(0, Math.round(Number(seconds || 0)));
    if (total < 60) return `${total}s`;
    const minutes = Math.floor(total / 60); const remainder = total % 60;
    return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
  }

  function formatCurrency(value, currency) {
    try {
      return new Intl.NumberFormat('en-IN', { style: 'currency', currency: currency || 'USD', maximumFractionDigits: 2 }).format(value);
    } catch (_error) {
      return `${currency || 'USD'} ${Number(value || 0).toFixed(2)}`;
    }
  }

  function updateMultiLabel(container) {
    const checked = [...container.querySelectorAll('input:checked')];
    const type = container.dataset.dashboardFilter;
    const fallback = {
      client: 'All clients', country: 'All countries', branch: 'All branches',
      sub_branch: 'All sub-branches', shift: 'All shifts', user: 'All users',
    }[type] || 'All';
    const label = checked.length === 0 ? fallback : checked.length === 1
      ? checked[0].closest('label').innerText.trim() : `${checked.length} selected`;
    container.querySelector('.multi-trigger span').textContent = label;
    container.querySelector('.multi-trigger').classList.toggle('has-value', checked.length > 0);
  }

  function applyMenuVisibility(container) {
    const needle = container.querySelector('[data-multi-search]')?.value.trim().toLocaleLowerCase() || '';
    let visible = 0;
    container.querySelectorAll('.multi-options label').forEach((option) => {
      const show = option.dataset.parentHidden !== 'true' && (!needle || option.innerText.toLocaleLowerCase().includes(needle));
      option.hidden = !show;
      if (show) visible += 1;
    });
    const empty = container.querySelector('.multi-no-results');
    if (empty) empty.hidden = visible > 0 || Boolean(container.querySelector('.filter-empty'));
  }

  function closeMenus(except = null) {
    document.querySelectorAll('.dashboard-filters .multi-select.open').forEach((container) => {
      if (container === except) return;
      container.classList.remove('open');
      container.querySelector('.multi-menu').hidden = true;
      container.querySelector('.multi-trigger').setAttribute('aria-expanded', 'false');
    });
  }

  function setParentVisibility(container, predicate) {
    if (!container) return;
    container.querySelectorAll('.multi-options label').forEach((option) => {
      const show = predicate(option);
      option.dataset.parentHidden = String(!show);
      const input = option.querySelector('input');
      if (!show && input.checked) input.checked = false;
    });
    applyMenuVisibility(container); updateMultiLabel(container);
  }

  function filterContainer(type) { return document.querySelector(`[data-dashboard-filter="${type}"]`); }

  function updateHierarchy() {
    const branches = new Set(filterContainer('branch') ? selectedValues(filterContainer('branch')) : []);
    setParentVisibility(filterContainer('sub_branch'), (option) => !branches.size || branches.has(option.dataset.branchValue || ''));
    const subBranches = new Set(filterContainer('sub_branch') ? selectedValues(filterContainer('sub_branch')) : []);
    setParentVisibility(filterContainer('shift'), (option) => (
      (!branches.size || branches.has(option.dataset.branchValue || ''))
      && (!subBranches.size || subBranches.has(option.dataset.subBranchValue || ''))
    ));
    const shifts = new Set(filterContainer('shift') ? selectedValues(filterContainer('shift')) : []);
    setParentVisibility(filterContainer('user'), (option) => (
      (!branches.size || branches.has(option.dataset.branchValue || ''))
      && (!subBranches.size || subBranches.has(option.dataset.subBranchValue || ''))
      && (!shifts.size || shifts.has(option.dataset.shiftValue || ''))
    ));
  }

  document.querySelectorAll('.dashboard-filters .multi-select').forEach((container) => {
    const trigger = container.querySelector('.multi-trigger'); const menu = container.querySelector('.multi-menu');
    trigger.addEventListener('click', () => {
      const open = !container.classList.contains('open'); closeMenus(container);
      container.classList.toggle('open', open); menu.hidden = !open; trigger.setAttribute('aria-expanded', String(open));
      if (open) setTimeout(() => menu.querySelector('[data-multi-search]')?.focus(), 0);
    });
    menu.querySelector('[data-multi-search]')?.addEventListener('input', () => applyMenuVisibility(container));
    menu.addEventListener('change', () => {
      updateMultiLabel(container);
      if (['branch', 'sub_branch', 'shift'].includes(container.dataset.dashboardFilter)) updateHierarchy();
      scheduleLoad();
    });
    updateMultiLabel(container); applyMenuVisibility(container);
  });
  updateHierarchy();

  function queryParams() {
    const params = new URLSearchParams();
    document.querySelectorAll('[data-dashboard-filter]').forEach((container) => {
      const values = selectedValues(container);
      if (values.length) params.set(container.dataset.dashboardFilter, values.join(','));
    });
    if (byId('dashboardFrom')?.value) params.set('initiated_from', byId('dashboardFrom').value);
    if (byId('dashboardTo')?.value) params.set('initiated_to', byId('dashboardTo').value);
    return params;
  }

  function updateSummary(summary) {
    animateNumber(byId('dashboardHits'), summary.hits);
    animateNumber(byId('dashboardCompletes'), summary.completes);
    animateNumber(byId('dashboardConversion'), summary.conversion_rate, (value) => `${value.toFixed(1)}%`);
    animateNumber(byId('dashboardIR'), summary.incidence_rate, (value) => `${value.toFixed(1)}%`);
    animateNumber(byId('dashboardActiveUsers'), summary.active_users);
    animateNumber(byId('dashboardAverageLoi'), summary.average_loi_seconds, (value) => formatLoi(value));
    animateNumber(byId('dashboardRevenue'), summary.revenue, (value) => formatCurrency(value, summary.revenue_currency));
    document.querySelectorAll('.dashboard-kpi').forEach((card, index) => {
      card.classList.remove('metric-ready');
      setTimeout(() => card.classList.add('metric-ready'), index * 70);
    });
  }

  function linePath(points) {
    if (!points.length) return '';
    return points.map((point, index) => `${index ? 'L' : 'M'} ${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(' ');
  }

  function renderPerformance() {
    const host = byId('performanceChart'); if (!host || !state.performance) return;
    const rows = state.performance[state.period] || [];
    if (!rows.length) { host.innerHTML = '<div class="dashboard-empty">No performance data available.</div>'; return; }
    const width = 720; const height = 250; const left = 42; const right = 18; const top = 18; const bottom = 42;
    const chartWidth = width - left - right; const chartHeight = height - top - bottom;
    const maximum = Math.max(1, ...rows.flatMap((row) => [Number(row.hits), Number(row.completes)]));
    const x = (index) => left + (rows.length === 1 ? chartWidth / 2 : index * chartWidth / (rows.length - 1));
    const y = (value) => top + chartHeight - (Number(value) / maximum * chartHeight);
    const hitPoints = rows.map((row, index) => ({ x: x(index), y: y(row.hits), value: row.hits }));
    const completePoints = rows.map((row, index) => ({ x: x(index), y: y(row.completes), value: row.completes }));
    const grid = [0, .25, .5, .75, 1].map((ratio) => {
      const gridY = top + chartHeight - ratio * chartHeight;
      return `<line x1="${left}" y1="${gridY}" x2="${width - right}" y2="${gridY}"/><text x="${left - 10}" y="${gridY + 4}" text-anchor="end">${number(maximum * ratio)}</text>`;
    }).join('');
    const labels = rows.map((row, index) => `<text class="x-label" x="${x(index)}" y="${height - 14}" text-anchor="middle">${escapeHtml(row.label)}</text>`).join('');
    const dots = (points, rowsForDots, className) => points.map((point, index) => `<g class="chart-point ${className}" style="--delay:${index * 70}ms"><circle cx="${point.x}" cy="${point.y}" r="4"><title>${escapeHtml(rowsForDots[index].label)}: ${number(point.value)}</title></circle><text x="${point.x}" y="${point.y - 10}" text-anchor="middle">${number(point.value)}</text></g>`).join('');
    host.innerHTML = `<svg class="performance-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Hits and completes performance graph"><g class="chart-grid">${grid}</g>${labels}<path class="chart-line hits-line" d="${linePath(hitPoints)}"/><path class="chart-line completes-line" d="${linePath(completePoints)}"/>${dots(hitPoints, rows, 'hit-point')}${dots(completePoints, rows, 'complete-point')}</svg>`;
    requestAnimationFrame(() => host.querySelectorAll('.chart-line').forEach((path) => {
      const length = path.getTotalLength(); path.style.strokeDasharray = length; path.style.strokeDashoffset = length;
      requestAnimationFrame(() => { path.style.strokeDashoffset = '0'; });
    }));
  }

  function renderClients(rows) {
    const host = byId('clientShareChart'); if (!host) return;
    if (!rows?.length) { host.innerHTML = '<div class="dashboard-empty">No completed client activity matches these filters.</div>'; return; }
    let cursor = 0;
    const segments = rows.map((row, index) => {
      const start = cursor; cursor += Number(row.share_percent || 0);
      return `${palette[index % palette.length]} ${start}% ${cursor}%`;
    });
    if (cursor < 100) segments.push(`#edf2f6 ${cursor}% 100%`);
    const total = rows.reduce((sum, row) => sum + Number(row.completes || 0), 0);
    const legend = rows.map((row, index) => `<li style="--index:${index}"><i style="background:${palette[index % palette.length]}"></i><span><b>${escapeHtml(row.name)}</b><small>${number(row.completes)} completes</small></span><strong>${Number(row.share_percent).toFixed(1)}%</strong></li>`).join('');
    host.innerHTML = `<div class="client-donut" style="--donut:${segments.join(',')}"><span><b>${number(total)}</b><small>Completes</small></span></div><ol class="client-share-list">${legend}</ol>`;
  }

  function renderStatus(data) {
    const host = byId('statusBreakdown'); if (!host || !data) return;
    const rows = [
      ['initiated', 'Initiated', data.initiated], ['completed', 'Completed', data.completed],
      ['terminated', 'Terminated', data.terminated], ['quota', 'Quota full', data.quota],
      ['security', 'Quality / security', data.security],
    ];
    const maximum = Math.max(1, ...rows.map((row) => Number(row[2])));
    host.innerHTML = rows.map(([type, label, value], index) => `<div class="horizontal-metric ${type}" style="--index:${index}"><span><i></i>${label}</span><div><b style="--width:${Number(value) / maximum * 100}%"></b></div><strong>${number(value)}</strong></div>`).join('');
  }

  function renderDevices(data) {
    const host = byId('deviceBreakdown'); if (!host || !data) return;
    const rows = [['desktop', 'Desktop'], ['mobile', 'Mobile'], ['tablet', 'Tablet'], ['unclassified', 'Other']];
    const total = rows.reduce((sum, [key]) => sum + Number(data[key] || 0), 0);
    host.innerHTML = rows.map(([key, label], index) => {
      const value = Number(data[key] || 0); const percent = total ? value / total * 100 : 0;
      return `<div class="device-dashboard-card" style="--index:${index}"><span>${icon[key === 'unclassified' ? 'unknown' : key]}</span><div><small>${label}</small><strong>${number(value)}</strong><em>${percent.toFixed(1)}% of completes</em></div><i style="--progress:${percent}%"></i></div>`;
    }).join('');
  }

  function renderTopUsers(rows) {
    const host = byId('dashboardTopUsers'); if (!host) return;
    if (!rows?.length) { host.innerHTML = '<div class="dashboard-empty">No user activity matches these filters.</div>'; return; }
    const maximum = Math.max(1, ...rows.map((row) => Number(row.completes)));
    host.innerHTML = rows.map((row, index) => `<div class="performer-row" style="--index:${index}"><span class="performer-rank">${index + 1}</span><span class="performer-avatar">${escapeHtml(String(row.name || '?').charAt(0).toUpperCase())}</span><div class="performer-copy"><b>${escapeHtml(row.name)}</b><span><i style="--progress:${Number(row.completes) / maximum * 100}%"></i></span><small>${number(row.hits)} hits · ${Number(row.conversion_rate).toFixed(1)}% conversion</small></div><strong>${number(row.completes)}</strong></div>`).join('');
  }

  function renderRecent(rows) {
    const host = byId('dashboardRecentActivity'); if (!host) return;
    if (!rows?.length) { host.innerHTML = '<div class="dashboard-empty">No respondent journeys match these filters.</div>'; return; }
    const formatter = new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' });
    host.innerHTML = rows.map((row, index) => `<article class="activity-row" style="--index:${index}"><i class="activity-status status-${escapeHtml(row.status)}"></i><div><strong>${escapeHtml(row.user_name)}</strong><span>${escapeHtml(row.client_name)} · ${escapeHtml(row.project_id)}</span></div><code>${escapeHtml(row.rid)}</code><span class="activity-result status-${escapeHtml(row.status)}">${escapeHtml(row.status_label)}</span><time>${formatter.format(new Date(row.initiated_at))}<small>IST</small></time></article>`).join('');
  }

  function render(data) {
    updateSummary(data.summary || {});
    state.performance = data.performance; renderPerformance();
    renderClients(data.client_distribution); renderStatus(data.status_breakdown);
    renderDevices(data.device_breakdown); renderTopUsers(data.top_users);
    const updated = byId('dashboardUpdatedAt');
    if (updated) updated.textContent = new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(data.generated_at)) + ' IST';
  }

  function showError(message) {
    document.querySelectorAll('.dashboard-chart-stage,.client-share-body,.horizontal-metrics,.device-dashboard-grid,.performer-list,.dashboard-activity-list').forEach((host) => {
      host.innerHTML = `<div class="dashboard-error"><strong>Could not load analytics</strong><span>${escapeHtml(message)}</span><button type="button" data-dashboard-retry>Try again</button></div>`;
    });
    document.querySelectorAll('[data-dashboard-retry]').forEach((button) => button.addEventListener('click', loadDashboard));
  }

  async function loadDashboard() {
    state.controller?.abort(); state.controller = new AbortController();
    document.body.classList.add('dashboard-loading');
    try {
      const response = await fetch(`/api/v1/dashboard/?${queryParams()}`, { signal: state.controller.signal });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
      render(data);
    } catch (error) {
      if (error.name !== 'AbortError') showError(error.message);
    } finally {
      document.body.classList.remove('dashboard-loading');
    }
  }

  function scheduleLoad() { clearTimeout(state.timer); state.timer = setTimeout(loadDashboard, 280); }
  [byId('dashboardFrom'), byId('dashboardTo')].filter(Boolean).forEach((input) => input.addEventListener('change', scheduleLoad));
  byId('clearDashboardFilters')?.addEventListener('click', () => {
    document.querySelectorAll('.dashboard-filters input[type="checkbox"]').forEach((input) => { input.checked = false; });
    document.querySelectorAll('.dashboard-filters [data-multi-search]').forEach((input) => { input.value = ''; });
    if (byId('dashboardFrom')) byId('dashboardFrom').value = '';
    if (byId('dashboardTo')) byId('dashboardTo').value = '';
    document.querySelectorAll('.dashboard-filters .multi-select').forEach((container) => { updateMultiLabel(container); applyMenuVisibility(container); });
    updateHierarchy(); closeMenus(); loadDashboard();
  });
  document.querySelectorAll('[data-dashboard-period]').forEach((button) => button.addEventListener('click', () => {
    state.period = button.dataset.dashboardPeriod;
    document.querySelectorAll('[data-dashboard-period]').forEach((item) => item.classList.toggle('active', item === button));
    renderPerformance();
  }));
  document.addEventListener('click', (event) => { if (!event.target.closest('.dashboard-filters .multi-select')) closeMenus(); });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeMenus(); });
  loadDashboard();
})();
