/* Dashboard data loading, scoped graph controls, animated KPIs and SVG charts. */

(() => {
  const byId = (id) => document.getElementById(id);
  const ranges = new Set(['24h', '48h', '7d', 'month', '3m', '6m', 'fy']);
  const initialQuery = new URLSearchParams(location.search);
  const initialMainRange = ranges.has(initialQuery.get('range')) ? initialQuery.get('range') : '24h';
  const state = {
    range: initialMainRange,
    financialYear: initialQuery.get('financial_year') || '',
    trafficClient: initialQuery.get('traffic_client') || '',
    financeClient: initialQuery.get('finance_client') || '',
    controller: null,
    data: null,
    resizeTimer: null,
  };
  const colors = ['#15b8d8', '#4967d8', '#29ad7b', '#e6a43c', '#9165d5', '#e56472', '#57748f', '#1f9d9a'];
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function escapeHtml(value) {
    const node = document.createElement('div');
    node.textContent = value == null ? '' : String(value);
    return node.innerHTML;
  }

  const number = (value) => Number(value || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 });
  const moneyNumber = (value) => Number(value || 0);

  function formatCurrency(value, currency, compact = false) {
    try {
      return new Intl.NumberFormat('en-IN', {
        style: 'currency', currency: currency || 'USD',
        notation: compact ? 'compact' : 'standard', maximumFractionDigits: 2,
      }).format(Number(value || 0));
    } catch (_error) {
      return `${currency || 'USD'} ${Number(value || 0).toFixed(2)}`;
    }
  }

  function formatLoi(seconds) {
    const total = Math.max(0, Math.round(Number(seconds || 0)));
    if (total < 60) return `${total}s`;
    const minutes = Math.floor(total / 60); const remainder = total % 60;
    return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
  }

  function animateNumber(element, target, formatter = number) {
    if (!element || target == null) return;
    const finalValue = Number(target || 0);
    const startValue = Number(element.dataset.value || 0);
    element.dataset.value = String(finalValue);
    if (reducedMotion) { element.textContent = formatter(finalValue); return; }
    const started = performance.now();
    const frame = (now) => {
      const progress = Math.min(1, (now - started) / 760);
      const eased = 1 - Math.pow(1 - progress, 3);
      element.textContent = formatter(startValue + (finalValue - startValue) * eased);
      if (progress < 1) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  function updateSummary(summary, comparison) {
    const currency = summary.revenue_currency || 'USD';
    animateNumber(byId('dashboardRevenue'), summary.revenue, (value) => formatCurrency(value, currency));
    animateNumber(byId('dashboardHits'), summary.hits);
    animateNumber(byId('dashboardCompletes'), summary.completes);
    animateNumber(byId('dashboardConversion'), summary.conversion_rate, (value) => `${value.toFixed(1)}%`);
    animateNumber(byId('dashboardAverageCpi'), summary.average_cpi, (value) => formatCurrency(value, currency));
    animateNumber(byId('dashboardRpc'), summary.rpc, (value) => formatCurrency(value, currency));
    animateNumber(byId('dashboardAverageLoi'), summary.average_loi_seconds, formatLoi);
    animateNumber(byId('dashboardIR'), summary.incidence_rate, (value) => `${value.toFixed(1)}%`);
    document.querySelectorAll('[data-dashboard-trend]').forEach((element) => {
      const delta = comparison?.deltas?.[element.dataset.dashboardTrend];
      if (delta === null || delta === undefined) {
        element.textContent = '—';
        element.className = 'bi-kpi-trend neutral';
        return;
      }
      const numeric = Number(delta);
      element.textContent = `${numeric > 0 ? '↑' : numeric < 0 ? '↓' : '→'} ${Math.abs(numeric).toFixed(1)}%`;
      element.className = `bi-kpi-trend ${numeric > 0 ? 'up' : numeric < 0 ? 'down' : 'neutral'}`;
    });
    document.querySelectorAll('.bi-kpi').forEach((card, index) => {
      card.classList.remove('bi-kpi-ready');
      setTimeout(() => card.classList.add('bi-kpi-ready'), reducedMotion ? 0 : index * 45);
    });
  }

  function svgLine(points) {
    return points.map((point, index) => `${index ? 'L' : 'M'}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ');
  }

  function svgArea(points, bottom) {
    if (!points.length) return '';
    return `${svgLine(points)} L${points[points.length - 1].x.toFixed(1)},${bottom} L${points[0].x.toFixed(1)},${bottom} Z`;
  }

  function bindChartTooltip(host, rows, formatter) {
    const tooltip = document.createElement('div');
    tooltip.className = 'bi-chart-tooltip';
    host.appendChild(tooltip);
    host.querySelectorAll('[data-chart-index]').forEach((target) => {
      const show = (event) => {
        const row = rows[Number(target.dataset.chartIndex)];
        if (!row) return;
        tooltip.innerHTML = formatter(row);
        tooltip.classList.add('show');
        const box = host.getBoundingClientRect();
        const left = Math.max(10, Math.min(box.width - 190, event.clientX - box.left + 12));
        const top = Math.max(8, event.clientY - box.top - 92);
        tooltip.style.left = `${left}px`; tooltip.style.top = `${top}px`;
      };
      target.addEventListener('pointerenter', show);
      target.addEventListener('pointermove', show);
      target.addEventListener('pointerleave', () => tooltip.classList.remove('show'));
    });
  }

  function axisGrid({ width, height, left, right, top, bottom, maximum, formatter = number }) {
    const plotHeight = height - top - bottom;
    return [0, .25, .5, .75, 1].map((ratio) => {
      const y = top + plotHeight - ratio * plotHeight;
      return `<line x1="${left}" y1="${y}" x2="${width - right}" y2="${y}"/><text x="${left - 9}" y="${y + 4}" text-anchor="end">${escapeHtml(formatter(maximum * ratio))}</text>`;
    }).join('');
  }

  function labelStride(rows, host) {
    const target = host.clientWidth < 520 ? 4 : host.clientWidth < 760 ? 6 : 8;
    return Math.max(1, Math.ceil(rows.length / target));
  }

  function animateChart(host) {
    if (reducedMotion) return;
    requestAnimationFrame(() => {
      host.querySelectorAll('.bi-chart-line').forEach((path) => {
        const length = path.getTotalLength();
        path.style.strokeDasharray = length;
        path.style.strokeDashoffset = length;
        requestAnimationFrame(() => { path.style.strokeDashoffset = '0'; });
      });
    });
  }

  function renderVolume(rows, rangeLabel = '') {
    const host = byId('volumeChart'); if (!host) return;
    if (!rows?.length) { host.innerHTML = '<div class="dashboard-empty">No traffic data is available for this range.</div>'; return; }
    const totalHits = rows.reduce((sum, row) => sum + Number(row.hits || 0), 0);
    const totalCompletes = rows.reduce((sum, row) => sum + Number(row.completes || 0), 0);
    const weightedConversion = totalHits ? totalCompletes / totalHits * 100 : 0;
    const width = 860; const height = 300; const left = 52; const right = 48; const top = 24; const bottom = 42;
    const plotWidth = width - left - right; const plotHeight = height - top - bottom;
    const maximum = Math.max(1, ...rows.flatMap((row) => [Number(row.hits), Number(row.completes)]));
    const group = plotWidth / rows.length; const barWidth = Math.max(4, Math.min(18, group * .28));
    const x = (index) => left + group * index + group / 2;
    const y = (value) => top + plotHeight - Number(value || 0) / maximum * plotHeight;
    const rateY = (value) => top + plotHeight - Math.min(100, Number(value || 0)) / 100 * plotHeight;
    const stride = labelStride(rows, host);
    const bars = rows.map((row, index) => {
      const hitY = y(row.hits); const completeY = y(row.completes);
      return `<g class="bi-bar-group" data-chart-index="${index}" style="--delay:${index * 40}ms"><rect class="bi-volume-hit" x="${x(index) - barWidth - 1}" y="${hitY}" width="${barWidth}" height="${top + plotHeight - hitY}"><title>${escapeHtml(row.label)} · Entrants ${number(row.hits)}</title></rect><rect class="bi-volume-complete" x="${x(index) + 1}" y="${completeY}" width="${barWidth}" height="${top + plotHeight - completeY}"><title>${escapeHtml(row.label)} · Completes ${number(row.completes)}</title></rect><rect class="bi-chart-hitbox" x="${left + group * index}" y="${top}" width="${group}" height="${plotHeight}"/></g>`;
    }).join('');
    const ratePoints = rows.map((row, index) => ({ x: x(index), y: rateY(row.conversion_rate), value: row.conversion_rate }));
    const rateDots = ratePoints.map((point, index) => `<circle class="bi-rate-dot" cx="${point.x}" cy="${point.y}" r="3.5"><title>${escapeHtml(rows[index].label)} · Conversion ${Number(point.value).toFixed(1)}%</title></circle>`).join('');
    const labels = rows.map((row, index) => index % stride === 0 || index === rows.length - 1
      ? `<text class="bi-x-label" x="${x(index)}" y="${height - 15}" text-anchor="middle">${escapeHtml(row.short_label)}</text>` : '').join('');
    const rightAxis = [0, 50, 100].map((value) => `<text class="bi-right-axis" x="${width - right + 9}" y="${rateY(value) + 4}">${value}%</text>`).join('');
    const averageY = rateY(weightedConversion);
    host.innerHTML = `<svg class="bi-chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Entrants, completes and conversion over ${escapeHtml(rangeLabel)}"><defs><linearGradient id="trafficArea" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#e7a038" stop-opacity=".22"/><stop offset="1" stop-color="#e7a038" stop-opacity="0"/></linearGradient></defs><g class="bi-chart-grid">${axisGrid({ width, height, left, right, top, bottom, maximum })}</g>${rightAxis}<line class="bi-average-line" x1="${left}" x2="${width - right}" y1="${averageY}" y2="${averageY}"/><path class="bi-chart-area" fill="url(#trafficArea)" d="${svgArea(ratePoints, top + plotHeight)}"/>${bars}<path class="bi-chart-line bi-conversion-line" d="${svgLine(ratePoints)}"/>${rateDots}${labels}</svg>`;
    bindChartTooltip(host, rows, (row) => `<strong>${escapeHtml(row.label)}</strong><span>Entrants <b>${number(row.hits)}</b></span><span>Completes <b>${number(row.completes)}</b></span><span>Conversion <b>${Number(row.conversion_rate || 0).toFixed(1)}%</b></span><span>IR <b>${Number(row.incidence_rate || 0).toFixed(1)}%</b></span>`);
    animateChart(host);
  }

  function renderFinance(rows, currency, rangeLabel = '') {
    const host = byId('financeChart'); if (!host) return;
    if (!rows?.length) { host.innerHTML = '<div class="dashboard-empty">No financial data is available for this range.</div>'; return; }
    const hasRevenue = rows.some((row) => row.revenue != null);
    const lineKey = rows.some((row) => row.rpc != null) ? 'rpc' : 'average_cpi';
    const lineLabel = lineKey === 'rpc' ? 'RPC' : 'Average CPI';
    const hasLine = rows.some((row) => row[lineKey] != null);
    byId('financeBarLegend')?.toggleAttribute('hidden', !hasRevenue);
    const lineLegend = byId('financeLineLegend');
    if (lineLegend) {
      lineLegend.hidden = !hasLine;
      lineLegend.lastChild.textContent = lineLabel;
    }
    const width = 620; const height = 300; const left = 58; const right = 48; const top = 24; const bottom = 42;
    const plotWidth = width - left - right; const plotHeight = height - top - bottom;
    const maxRevenue = Math.max(1, ...rows.map((row) => Number(row.revenue || 0)));
    const maxLine = Math.max(1, ...rows.map((row) => Number(row[lineKey] || 0)));
    const group = plotWidth / rows.length; const barWidth = Math.max(5, Math.min(25, group * .5));
    const x = (index) => left + group * index + group / 2;
    const revenueY = (value) => top + plotHeight - Number(value || 0) / maxRevenue * plotHeight;
    const lineY = (value) => top + plotHeight - Number(value || 0) / maxLine * plotHeight;
    const stride = labelStride(rows, host);
    const bars = hasRevenue ? rows.map((row, index) => {
      const y = revenueY(row.revenue);
      return `<g data-chart-index="${index}"><rect class="bi-finance-bar" style="--delay:${index * 40}ms" x="${x(index) - barWidth / 2}" y="${y}" width="${barWidth}" height="${top + plotHeight - y}"><title>${escapeHtml(row.label)} · Revenue ${escapeHtml(formatCurrency(row.revenue, currency))}</title></rect><rect class="bi-chart-hitbox" x="${left + group * index}" y="${top}" width="${group}" height="${plotHeight}"/></g>`;
    }).join('') : '';
    const linePoints = hasLine
      ? rows.map((row, index) => ({ x: x(index), y: lineY(row[lineKey]), value: row[lineKey] }))
      : [];
    const dots = linePoints.map((point, index) => `<circle class="bi-rpc-dot" cx="${point.x}" cy="${point.y}" r="3.5"><title>${escapeHtml(rows[index].label)} · ${lineLabel} ${escapeHtml(formatCurrency(point.value, currency))}</title></circle>`).join('');
    const labels = rows.map((row, index) => index % stride === 0 || index === rows.length - 1
      ? `<text class="bi-x-label" x="${x(index)}" y="${height - 15}" text-anchor="middle">${escapeHtml(row.short_label)}</text>` : '').join('');
    const rightAxis = hasLine
      ? [0, .5, 1].map((ratio) => `<text class="bi-right-axis" x="${width - right + 8}" y="${lineY(maxLine * ratio) + 4}">${escapeHtml(formatCurrency(maxLine * ratio, currency, true))}</text>`).join('')
      : '';
    const line = hasLine ? `<path class="bi-chart-area" fill="url(#financeArea)" d="${svgArea(linePoints, top + plotHeight)}"/><path class="bi-chart-line bi-rpc-line" d="${svgLine(linePoints)}"/>${dots}` : '';
    const accessibleLabel = hasLine ? `Revenue and ${lineLabel}` : 'Revenue';
    host.innerHTML = `<svg class="bi-chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${accessibleLabel} over ${escapeHtml(rangeLabel)}"><defs><linearGradient id="financeArea" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#e7a038" stop-opacity=".2"/><stop offset="1" stop-color="#e7a038" stop-opacity="0"/></linearGradient></defs><g class="bi-chart-grid">${axisGrid({ width, height, left, right, top, bottom, maximum: maxRevenue, formatter: (value) => formatCurrency(value, currency, true) })}</g>${rightAxis}${bars}${line}${labels}</svg>`;
    bindChartTooltip(host, rows, (row) => `<strong>${escapeHtml(row.label)}</strong><span>Revenue <b>${escapeHtml(formatCurrency(row.revenue, currency))}</b></span><span>${escapeHtml(lineLabel)} <b>${escapeHtml(formatCurrency(row[lineKey], currency))}</b></span><span>Completes <b>${number(row.completes)}</b></span><span>Entrants <b>${number(row.hits)}</b></span>`);
    animateChart(host);
  }

  function renderClients(rows) {
    const host = byId('clientShareChart'); if (!host) return;
    if (!rows?.length) { host.innerHTML = '<div class="dashboard-empty">No completed client activity matches this range.</div>'; return; }
    let cursor = 0;
    const segments = rows.map((row, index) => {
      const start = cursor; cursor += Number(row.share_percent || 0);
      return `${colors[index % colors.length]} ${start}% ${cursor}%`;
    });
    if (cursor < 100) segments.push(`#edf2f6 ${cursor}% 100%`);
    const total = rows.reduce((sum, row) => sum + Number(row.completes || 0), 0);
    host.innerHTML = `<div class="bi-client-donut" style="--segments:${segments.join(',')}"><span><b>${number(total)}</b><small>Completes</small></span></div><ol class="bi-client-list">${rows.map((row, index) => `<li style="--index:${index}"><i style="--series:${colors[index % colors.length]}"></i><span><b>${escapeHtml(row.name)}</b><small>${number(row.completes)} of ${number(row.hits)} · ${Number(row.conversion_rate || 0).toFixed(1)}% conversion</small><em><i style="--progress:${Number(row.share_percent || 0)}%"></i></em></span><strong>${Number(row.share_percent || 0).toFixed(1)}%</strong></li>`).join('')}</ol>`;
  }

  function renderStatus(data) {
    const host = byId('statusBreakdown'); if (!host || !data) return;
    const rows = [
      ['initiated', 'Initiated', data.initiated], ['completed', 'Completed', data.completed],
      ['terminated', 'Terminated', data.terminated], ['quota', 'Quota full', data.quota],
      ['security', 'Quality / security', data.security],
    ];
    const total = Math.max(1, rows.reduce((sum, row) => sum + Number(row[2] || 0), 0));
    const resolved = Math.max(0, total - Number(data.initiated || 0));
    const yieldRate = resolved ? Number(data.completed || 0) / resolved * 100 : 0;
    host.innerHTML = `<div class="bi-status-headline"><span><small>Resolved outcomes</small><strong>${number(resolved)}</strong></span><span><small>Resolved yield</small><strong>${yieldRate.toFixed(1)}%</strong></span></div>${rows.map(([type, label, value], index) => `<div class="bi-status-row ${type}" style="--index:${index}"><span><i></i>${label}</span><div><b style="--progress:${Number(value || 0) / total * 100}%"></b></div><strong>${number(value)}</strong><em>${(Number(value || 0) / total * 100).toFixed(1)}%</em></div>`).join('')}`;
  }

  function renderDevices(data, performance) {
    const host = byId('deviceBreakdown'); if (!host || !data) return;
    const rows = [
      ['desktop', 'Desktop', '#15b8d8'], ['mobile', 'Mobile', '#4967d8'],
      ['tablet', 'Tablet', '#9165d5'], ['unclassified', 'Other', '#d8e0e8'],
    ];
    const total = rows.reduce((sum, [key]) => sum + Number(data[key] || 0), 0);
    let cursor = 0;
    const segments = rows.map(([key, _label, color]) => {
      const start = cursor; cursor += total ? Number(data[key] || 0) / total * 100 : 0;
      return `${color} ${start}% ${cursor}%`;
    });
    if (!total) segments.push('#edf2f6 0 100%');
    host.innerHTML = `<div class="bi-device-ring" style="--segments:${segments.join(',')}"><span><b>${number(total)}</b><small>Completes</small></span></div><div class="bi-device-list">${rows.map(([key, label, color], index) => { const metric = performance?.[key] || {}; return `<div style="--index:${index}"><i style="--series:${color}"></i><span>${label}<small>${number(metric.hits)} entrants</small></span><strong>${number(data[key])}</strong><small>${Number(metric.conversion_rate || 0).toFixed(1)}% CVR</small></div>`; }).join('')}</div>`;
  }

  function renderTopSuppliers(rows) {
    const host = byId('dashboardTopSuppliers'); if (!host) return;
    if (!rows?.length) { host.innerHTML = '<div class="dashboard-empty">No supplier activity matches this range.</div>'; return; }
    const maximum = Math.max(1, ...rows.map((row) => Number(row.completes || 0)));
    host.innerHTML = rows.map((row, index) => `<div class="bi-performer-row" style="--index:${index}"><span class="bi-performer-rank">${String(index + 1).padStart(2, '0')}</span><span class="bi-performer-avatar">${escapeHtml(String(row.name || '?').charAt(0).toUpperCase())}</span><div><b>${escapeHtml(row.name)}</b><small>${escapeHtml(row.branch_name || 'Unassigned branch')}</small><span><i style="--progress:${Number(row.completes || 0) / maximum * 100}%"></i></span></div><strong>${number(row.completes)}<small>completes</small></strong></div>`).join('');
  }

  function populateFinancialYears(data) {
    const years = data.financial_years || [];
    const fallback = String(years[0]?.start_year || '');
    if (!state.financialYear || !years.some((year) => String(year.start_year) === String(state.financialYear))) state.financialYear = fallback;
    const select = byId('dashboardFinancialYear'); if (!select) return;
    select.innerHTML = `<option value="">Financial year</option>${years.map((year) => `<option value="${year.start_year}">${escapeHtml(year.label)}</option>`).join('')}`;
    select.value = String(state.financialYear || '');
    select.closest('label')?.classList.toggle('active', state.range === 'fy');
  }

  function renderOperationalInsights(data) {
    const host = byId('dashboardInsightStrip'); if (!host) return;
    const summary = data.summary || {};
    const points = data.traffic_chart?.points || [];
    const durationHours = Math.max(1, (new Date(data.range.end) - new Date(data.range.start)) / 3600000);
    const hourlyCompletes = Number(summary.completes || 0) / durationHours;
    const lastHourCompletes = Number(summary.last_hour_completes || 0);
    const peak = points.length ? points.reduce((best, row) => Number(row.completes || 0) > Number(best.completes || 0) ? row : best, points[0]) : null;
    const cards = [
      ['Average completes', `${hourlyCompletes < 1 ? hourlyCompletes.toFixed(2) : hourlyCompletes.toFixed(1)} / hr`, `Last hour · ${number(lastHourCompletes)} completes`],
      ['Peak completion window', peak ? peak.short_label : 'No activity', peak ? `${number(peak.completes)} completes · ${Number(peak.conversion_rate || 0).toFixed(1)}% CVR` : 'No selected-range traffic'],
    ];
    host.innerHTML = cards.map(([label, value, detail], index) => `<article style="--index:${index}"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong><span>${escapeHtml(detail)}</span></article>`).join('');
  }

  function updateGraphControls(data) {
    populateFinancialYears(data);
    const clients = data.graph_clients || [];
    [['traffic', 'trafficGraphClient'], ['finance', 'financeGraphClient']].forEach(([graph, id]) => {
      const select = byId(id); if (!select) return;
      const selected = String(state[`${graph}Client`] || '');
      select.innerHTML = `<option value="">All clients</option>${clients.map((client) => `<option value="${escapeHtml(client.id)}">${escapeHtml(client.name)}</option>`).join('')}`;
      select.value = selected;
    });
  }

  const dashboardUpdatedFormatter = new Intl.DateTimeFormat('en-IN', {
    timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit',
  });

  function render(data) {
    state.data = data;
    if (data.range?.financial_year) state.financialYear = String(data.range.financial_year);
    updateSummary(data.summary || {}, data.comparison);
    const caption = byId('dashboardRangeCaption'); if (caption) caption.textContent = data.range.label;
    if (byId('trafficBucketLabel') && data.traffic_chart) byId('trafficBucketLabel').textContent = data.traffic_chart.range.bucket_label;
    if (byId('financeBucketLabel') && data.finance_chart) byId('financeBucketLabel').textContent = data.finance_chart.range.bucket_label;
    updateGraphControls(data);
    renderOperationalInsights(data);
    renderVolume(data.traffic_chart?.points, data.traffic_chart?.range?.label || data.range.label);
    renderFinance(data.finance_chart?.points, data.summary?.revenue_currency || 'USD', data.finance_chart?.range?.label || data.range.label);
    renderClients(data.client_distribution);
    renderStatus(data.status_breakdown);
    renderDevices(data.device_breakdown, data.device_performance);
    renderTopSuppliers(data.top_suppliers);
    const updated = byId('dashboardUpdatedAt');
    if (updated) updated.textContent = `${dashboardUpdatedFormatter.format(new Date(data.generated_at))} IST`;
  }

  function showError(message) {
    document.querySelectorAll('.bi-chart-stage,.bi-client-body,.bi-status-list,.bi-device-body,.bi-performer-list').forEach((host) => {
      host.innerHTML = `<div class="dashboard-error"><strong>Could not load analytics</strong><span>${escapeHtml(message)}</span><button type="button" data-dashboard-retry>Try again</button></div>`;
    });
    document.querySelectorAll('[data-dashboard-retry]').forEach((button) => button.addEventListener('click', loadDashboard));
  }

  async function loadDashboard() {
    state.controller?.abort(); state.controller = new AbortController();
    document.body.classList.add('dashboard-refreshing');
    document.querySelectorAll('[data-dashboard-range]').forEach((button) => { button.disabled = true; });
    try {
      const query = new URLSearchParams({ range: state.range });
      if (state.range === 'fy' && state.financialYear) query.set('financial_year', state.financialYear);
      if (document.querySelector('[data-graph-toolbar="traffic"]')) {
        if (state.trafficClient) query.set('traffic_client', state.trafficClient);
      }
      if (document.querySelector('[data-graph-toolbar="finance"]')) {
        if (state.financeClient) query.set('finance_client', state.financeClient);
      }
      const response = await fetch(`/api/v1/dashboard/?${query.toString()}`, {
        signal: state.controller.signal, credentials: 'same-origin',
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
      render(data);
    } catch (error) {
      if (error.name !== 'AbortError') showError(error.message);
    } finally {
      document.body.classList.remove('dashboard-refreshing');
      document.querySelectorAll('[data-dashboard-range]').forEach((button) => { button.disabled = false; });
    }
  }

  document.querySelectorAll('[data-dashboard-range]').forEach((button) => {
    const selected = button.dataset.dashboardRange === state.range;
    button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected));
    button.addEventListener('click', () => {
      if (button.dataset.dashboardRange === state.range) return;
      state.range = button.dataset.dashboardRange;
      document.querySelectorAll('[data-dashboard-range]').forEach((item) => {
        const active = item === button;
        item.classList.toggle('active', active); item.setAttribute('aria-pressed', String(active));
      });
      const url = new URL(location.href);
      url.searchParams.set('range', state.range);
      url.searchParams.delete('traffic_range');
      url.searchParams.delete('finance_range');
      url.searchParams.delete('traffic_financial_year');
      url.searchParams.delete('finance_financial_year');
      if (state.range !== 'fy') {
        url.searchParams.delete('financial_year');
      }
      history.replaceState({}, '', url);
      loadDashboard();
    });
  });

  byId('dashboardFinancialYear')?.addEventListener('change', (event) => {
    if (!event.target.value) return;
    state.range = 'fy';
    state.financialYear = event.target.value;
    const url = new URL(location.href);
    url.searchParams.set('range', 'fy'); url.searchParams.set('financial_year', event.target.value);
    url.searchParams.delete('traffic_range'); url.searchParams.delete('traffic_financial_year');
    url.searchParams.delete('finance_range'); url.searchParams.delete('finance_financial_year');
    history.replaceState({}, '', url);
    loadDashboard();
  });

  [['traffic', 'trafficGraphClient'], ['finance', 'financeGraphClient']].forEach(([graph, id]) => {
    byId(id)?.addEventListener('change', (event) => {
      state[`${graph}Client`] = event.target.value;
      const url = new URL(location.href);
      if (event.target.value) url.searchParams.set(`${graph}_client`, event.target.value);
      else url.searchParams.delete(`${graph}_client`);
      history.replaceState({}, '', url);
      loadDashboard();
    });
  });

  const resizeObserver = new ResizeObserver(() => {
    clearTimeout(state.resizeTimer);
    state.resizeTimer = setTimeout(() => {
      if (!state.data) return;
      renderVolume(
        state.data.traffic_chart?.points,
        state.data.traffic_chart?.range?.label || state.data.range.label
      );
      renderFinance(
        state.data.finance_chart?.points,
        state.data.summary?.revenue_currency || 'USD',
        state.data.finance_chart?.range?.label || state.data.range.label
      );
    }, 120);
  });
  document.querySelectorAll('.bi-chart-stage').forEach((host) => resizeObserver.observe(host));
  loadDashboard();
})();
