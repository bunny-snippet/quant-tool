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

  function bindChartTooltip(host, rows, formatter) {
    const tooltip = document.createElement('div');
    tooltip.className = 'bi-chart-tooltip';
    tooltip.setAttribute('role', 'tooltip');
    host.appendChild(tooltip);
    const targets = [...host.querySelectorAll('[data-chart-index]')];
    targets.forEach((target, index) => {
      const show = (event = {}) => {
        const row = rows[index]; if (!row) return;
        tooltip.innerHTML = formatter(row);
        tooltip.classList.add('show');
        const box = host.getBoundingClientRect(), targetBox = target.getBoundingClientRect();
        const px = Number.isFinite(event.clientX) ? event.clientX : targetBox.left + targetBox.width / 2;
        const py = Number.isFinite(event.clientY) ? event.clientY : targetBox.top + 36;
        const tipWidth = tooltip.offsetWidth || 208, tipHeight = tooltip.offsetHeight || 120;
        tooltip.style.left = Math.max(4, Math.min(box.width - tipWidth - 4, px - box.left + 12)) + 'px';
        tooltip.style.top = Math.max(4, Math.min(box.height - tipHeight - 4, py - box.top - tipHeight - 12)) + 'px';
      };
      const hide = () => tooltip.classList.remove('show');
      target.addEventListener('pointerenter', show);
      target.addEventListener('pointermove', show);
      target.addEventListener('pointerleave', hide);
      target.addEventListener('focus', show);
      target.addEventListener('blur', hide);
      target.addEventListener('click', show);
      target.addEventListener('keydown', event => {
        if (event.key === 'Escape') hide();
        if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
          event.preventDefault();
          targets[Math.max(0, Math.min(targets.length - 1, index + (event.key === 'ArrowRight' ? 1 : -1)))].focus();
        }
      });
    });
  }

  function chartScale(maximum, minimumStep = 1) {
    const value = Math.max(minimumStep, maximum);
    const rawStep = value / 4, power = 10 ** Math.floor(Math.log10(rawStep));
    const step = Math.max(minimumStep, [1, 2, 2.5, 5, 10].find(n => n * power >= rawStep) * power);
    const count = Math.ceil(value / step);
    return {maximum: count * step, ticks: Array.from({length: count + 1}, (_, i) => i * step)};
  }

  function renderTimeline(host, rows, options) {
    if (!host) return;
    if (!rows?.length) { host.innerHTML = '<div class="dashboard-empty">No data is available for this range.</div>'; return; }
    if (!rows.some(row => Number(row.hits || 0) || Number(row.completes || 0) || Number(row.revenue || 0))) {
      host.innerHTML = '<div class="dashboard-empty">No activity in this period for the selected client.</div>'; return;
    }
    const width = Math.max(320, Math.round(host.clientWidth || 700)), height = 360;
    const left = options.finance ? 76 : 56, right = 20, plotWidth = width - left - right;
    const keys = options.bars.filter(key => rows.some(row => row[key] != null));
    if (!keys.length && !options.hasLine) {
      host.innerHTML = '<div class="dashboard-empty">Financial metrics are not available for this view.</div>'; return;
    }
    const top = 32, mainHeight = options.hasLine ? 150 : 266;
    const rateTop = keys.length ? 240 : 48, rateHeight = keys.length ? 66 : 258;
    const group = plotWidth / rows.length;
    const x = index => left + group * (index + .5);
    const scale = chartScale(Math.max(0, ...rows.flatMap(row => keys.map(key => Number(row[key] || 0)))), options.finance ? .01 : 1);
    const rateScale = options.finance ? chartScale(Math.max(0, ...rows.map(row => Number(row[options.lineKey] || 0))), .01) : {maximum:100,ticks:[0,50,100]};
    const y = value => top + mainHeight - Number(value || 0) / scale.maximum * mainHeight;
    const rateY = value => rateTop + rateHeight - Math.min(rateScale.maximum, Math.max(0, Number(value || 0))) / rateScale.maximum * rateHeight;
    const title = escapeHtml(options.title + ' · ' + options.rangeLabel);
    let svg = '<svg class="bi-chart-svg bi-clean-chart" viewBox="0 0 '+width+' '+height+'" role="group" aria-label="'+title+'">';
    if (keys.length) {
      svg += '<text class="bi-axis-caption" x="'+left+'" y="16">'+(options.finance ? 'Revenue' : 'Entrants / completes')+'</text><g class="bi-chart-grid">';
      for (const tick of scale.ticks) svg += '<line x1="'+left+'" x2="'+(width-right)+'" y1="'+y(tick)+'" y2="'+y(tick)+'"/><text x="'+(left-10)+'" y="'+(y(tick)+4)+'" text-anchor="end">'+escapeHtml(options.finance ? formatCurrency(tick,options.currency,true) : number(tick))+'</text>';
      svg += '</g>';
      const barWidth = Math.max(.5, Math.min(options.finance ? 24 : 16, group * .65 / keys.length));
      rows.forEach((row,index) => keys.forEach((key,k) => {
        if (row[key] == null) return;
        const cls = key === 'revenue' ? 'bi-finance-bar' : key === 'hits' ? 'bi-volume-hit' : 'bi-volume-complete';
        const barX = x(index) - (keys.length * barWidth + (keys.length-1)*2)/2 + k*(barWidth+2);
        svg += '<rect class="'+cls+'" x="'+barX+'" y="'+y(row[key])+'" width="'+barWidth+'" height="'+Math.max(0,top+mainHeight-y(row[key]))+'" rx="2"/>';
      }));
    }
    if (options.hasLine) {
      svg += '<text class="bi-axis-caption bi-rate-caption" x="'+left+'" y="'+(rateTop-16)+'">'+escapeHtml(options.lineLabel)+(options.finance ? (options.lineKey === 'average_cpi' ? ' · per complete' : ' · per entrant') : ' · completes ÷ entrants')+'</text><g class="bi-chart-grid bi-rate-grid">';
      for (const tick of rateScale.ticks) svg += '<line x1="'+left+'" x2="'+(width-right)+'" y1="'+rateY(tick)+'" y2="'+rateY(tick)+'"/><text x="'+(left-10)+'" y="'+(rateY(tick)+4)+'" text-anchor="end">'+escapeHtml(options.finance ? formatCurrency(tick,options.currency,true) : tick+'%')+'</text>';
      svg += '</g>';
      let points = [];
      const flush = () => {if(points.length) svg += '<path class="bi-chart-line '+(options.finance?'bi-rpc-line':'bi-conversion-line')+'" d="'+svgLine(points)+'"/>'; points=[];};
      rows.forEach((row,index) => {
        // No entrants means no meaningful rate, not an artificial zero/drop.
        if (row[options.lineKey] == null || !Number(row[options.lineKey === 'average_cpi' ? 'completes' : 'hits'])) {flush(); return;}
        const point = {x:x(index),y:rateY(row[options.lineKey])}; points.push(point);
        svg += '<circle class="'+(options.finance?'bi-rpc-dot':'bi-rate-dot')+'" cx="'+point.x+'" cy="'+point.y+'" r="3"/>';
      });
      flush();
    }
    const stride = Math.max(1, Math.ceil(rows.length / Math.max(2, Math.floor(plotWidth / 80))));
    rows.forEach((row,index) => {
      if (index % stride === 0 && (index === rows.length-1 || rows.length-1-index >= stride) || index === rows.length-1) svg += '<text class="bi-x-label" x="'+x(index)+'" y="344" text-anchor="middle">'+escapeHtml(row.short_label)+'</text>';
      const label = options.tooltipText(row);
      svg += '<rect class="bi-chart-hitbox" data-chart-index="'+index+'" tabindex="0" aria-label="'+escapeHtml(label)+'" x="'+(left+group*index)+'" y="'+top+'" width="'+group+'" height="282"><title>'+escapeHtml(label)+'</title></rect>';
    });
    host.innerHTML = svg + '</svg>';
    bindChartTooltip(host, rows, row => '<strong>'+escapeHtml(row.label)+'</strong>'+options.details(row).map(([label,value])=>'<span>'+escapeHtml(label)+'<b>'+escapeHtml(value)+'</b></span>').join(''));
  }

  function renderVolume(rows, rangeLabel = '') {
    const details = row => [['Entrants',number(row.hits)],['Completes',number(row.completes)],['Conversion',row.hits ? Number(row.conversion_rate || 0).toFixed(1)+'%' : '—'],['IR',row.hits ? Number(row.incidence_rate || 0).toFixed(1)+'%' : '—']];
    renderTimeline(byId('volumeChart'), rows, {bars:['hits','completes'],hasLine:true,lineKey:'conversion_rate',lineLabel:'Conversion',title:'Entrants, completes and conversion',rangeLabel,
      details,tooltipText:row=>row.label+' · '+details(row).map(pair=>pair.join(' ')).join(' · ')});
  }

  function renderFinance(rows, currency, rangeLabel = '') {
    const hasRevenue = !!rows?.some(row=>row.revenue != null);
    const lineKey = rows?.some(row=>row.rpc != null) ? 'rpc' : 'average_cpi';
    const lineLabel = lineKey === 'rpc' ? 'RPC' : 'Average CPI';
    const hasLine = !!rows?.some(row=>row[lineKey] != null);
    byId('financeBarLegend')?.toggleAttribute('hidden', !hasRevenue);
    const legend = byId('financeLineLegend'); if(legend){legend.hidden=!hasLine;legend.lastChild.textContent=lineLabel;}
    const details = row => [
      ...(hasRevenue ? [['Revenue',row.revenue == null ? '—' : formatCurrency(row.revenue,currency)]] : []),
      ...(hasLine ? [[lineLabel,row[lineKey] == null || !Number(row[lineKey === 'average_cpi' ? 'completes' : 'hits']) ? '—' : formatCurrency(row[lineKey],currency)]] : []),
      ['Completes',number(row.completes)],['Entrants',number(row.hits)],
    ];
    renderTimeline(byId('financeChart'),rows,{bars:hasRevenue?['revenue']:[],hasLine,lineKey,lineLabel,finance:true,currency,title:'Revenue and '+lineLabel,rangeLabel,
      details,tooltipText:row=>row.label+' · '+details(row).map(pair=>pair.join(' ')).join(' · ')});
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
    if (!rows?.length) { host.innerHTML = '<div class="dashboard-empty">No performer activity matches this range.</div>'; return; }
    const maximum = Math.max(1, ...rows.map((row) => Number(row.completes || 0)));
    host.innerHTML = rows.map((row, index) => `<div class="bi-performer-row" style="--index:${index}"><span class="bi-performer-rank">${String(index + 1).padStart(2, '0')}</span><span class="bi-performer-avatar">${escapeHtml(String(row.name || '?').charAt(0).toUpperCase())}</span><div><b>${escapeHtml(row.name)}</b>${row.branch_name && row.branch_name !== row.name ? `<small>${escapeHtml(row.branch_name)}</small>` : ''}<span><i style="--progress:${Number(row.completes || 0) / maximum * 100}%"></i></span></div><strong>${number(row.completes)}<small>completes</small></strong></div>`).join('');
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
