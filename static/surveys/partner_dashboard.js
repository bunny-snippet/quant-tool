/* Permission-scoped, read-only analytics. No sample data or business writes. */
(() => {
  'use strict';
  const root = document.getElementById('partnerDashboard');
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const isClient = root.dataset.section === 'client';
  const state = { range: '7d', partner: '', segment: '', search: '', sort: 'completes', direction: -1, page: 1 };
  let payload = null, controller = null, requestId = 0;
  const number = new Intl.NumberFormat('en-US');
  const escape = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const numeric = (v) => v == null ? '—' : number.format(v);
  const percent = (v) => v == null ? '—' : Number(v).toFixed(1) + '%';
  const ratio = (a, b) => b ? a / b * 100 : 0;
  function money(value, currency) {
    if (value == null) return '—';
    if (!currency) return Number(value) === 0 ? '0.00' : '—';
    try { return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 2 }).format(value); }
    catch (_) { return escape(currency) + ' ' + Number(value).toFixed(2); }
  }
  const html = (id, value) => { if ($(id)) $(id).innerHTML = value; };
  const text = (id, value) => { if ($(id)) $(id).textContent = value; };
  const on = (id, event, fn) => $(id)?.addEventListener(event, fn);
  function delta(value, before) {
    if (value == null || before == null) return 'Comparison unavailable';
    if (!Number(before)) return Number(value) ? 'No previous baseline' : '0.0%';
    const change = (value - before) / before * 100;
    return '<b class="' + (change < 0 ? 'negative' : '') + '">' + (change < 0 ? '↘' : '↗') + ' ' + Math.abs(change).toFixed(1) + '%</b>';
  }
  function renderCards() {
    const s = payload.summary, p = payload.previous, a = payload.access;
    const definitions = isClient ? [['hits','Entrants'],['completes','Completes'],['revenue','Source revenue'],['rpc','RPC']] : [['hits','Entrants'],['completes','Completes'],['conversion','Conversion'],['acceptance','Acceptance']];
    html('pdKpis', definitions.filter(([key]) => a[key === 'acceptance' ? 'completes' : key]).map(([key, label]) => {
      const value = key === 'acceptance' ? percent(ratio(s.accepted, s.completes)) : ['revenue','rpc'].includes(key) ? money(s[key], s.currency) : key === 'conversion' ? percent(s[key]) : numeric(s[key]);
      const meta = key === 'acceptance' ? 'Accepted ÷ all completes' : ['revenue','rpc'].includes(key) && (s.money_note || p.currency !== s.currency) ? escape(s.money_note || 'Currency differs from previous period') : delta(s[key], p[key]);
      return '<article class="pd-kpi"><div class="pd-kpi-label">' + label + '</div><strong>' + value + '</strong><div class="pd-kpi-meta" title="' + escape(payload.comparison_label) + '">' + meta + '</div></article>';
    }).join(''));
    text('pdConversion', percent(s.conversion));
  }
  function renderTrend() {
    if (!$('pdTrend')) return;
    $('pdTrend').closest('.pd-panel').hidden = !payload.access.trend;
    if (!payload.access.trend) { html('pdTrend', ''); return; }
    const timeline = payload.trend, a = payload.access;
    if (!timeline.length) { html('pdTrend', '<p class="pd-empty">No permitted trend data.</p>'); return; }
    const max = Math.max(1, ...timeline.flatMap(p => [p.hits || 0, p.completes || 0]));
    let svg = '<svg viewBox="0 0 680 220" role="img" aria-labelledby="pdChartTitle"><title id="pdChartTitle">Entrants and completes for ' + escape(payload.period) + '</title>';
    for (let i = 0; i <= 3; i++) {
      const y = 14 + 176 * i / 3;
      svg += '<line class="pd-grid-line" x1="43" x2="666" y1="' + y + '" y2="' + y + '"/><text class="pd-axis" x="34" y="' + (y + 4) + '" text-anchor="end">' + numeric(Math.round(max * (1 - i / 3))) + '</text>';
    }
    const group = 623 / timeline.length, bar = Math.min(19, group * .23);
    timeline.forEach((point, i) => {
      const x = 43 + group * (i + .5);
      [['hits', -bar - 2], ['completes', 2]].forEach(([key, offset]) => {
        if (!a[key] || point[key] == null) return;
        const h = point[key] / max * 176;
        svg += '<rect class="pd-chart-' + (key === 'hits' ? 'hit' : 'complete') + '" x="' + (x + offset) + '" y="' + (190 - h) + '" width="' + bar + '" height="' + h + '" rx="2"><title>' + escape(point.label) + ': ' + numeric(point[key]) + ' ' + key + '</title></rect>';
      });
      if (i % Math.ceil(timeline.length / 7) === 0) svg += '<text class="pd-axis" x="' + x + '" y="213" text-anchor="middle">' + escape(point.label) + '</text>';
    });
    html('pdTrend', svg + '</svg>');
  }
  function renderReview() {
    if (!$('pdReview')) return;
    $('pdReview').closest('.pd-panel').hidden = !payload.access.review;
    if (!payload.access.review) { html('pdReview', ''); return; }
    const s = payload.summary, items = [['accepted','Accepted'],['rejected','Rejected'],['pending','Pending']];
    html('pdReview', '<div class="pd-review-number"><strong>' + percent(ratio(s.accepted + s.rejected, s.completes)) + '</strong><span>of completes reviewed</span></div><div class="pd-review-bar" aria-hidden="true">' + items.map(([key]) => '<i class="pd-' + key + '" style="width:' + ratio(s[key],s.completes) + '%"></i>').join('') + '</div>' + items.map(([key,label]) => '<div class="pd-review-row"><i class="pd-' + key + '"></i><span>' + label + '</span><strong>' + numeric(s[key]) + '</strong><small>' + percent(ratio(s[key],s.completes)) + '</small></div>').join(''));
  }
  function renderTable() {
    if (!$('pdRows') || !payload) return;
    const access = payload.access;
    $('pdRows').closest('.pd-panel').hidden = !access.table;
    if (!access.table) { html('pdRows', ''); return; }
    const rows = payload.rows.filter(row => (row.name + ' ' + (row.segment_name || '')).toLowerCase().includes(state.search));
    rows.sort((a,b) => ((Number(a[state.sort]) || 0) - (Number(b[state.sort]) || 0)) * state.direction || a.name.localeCompare(b.name));
    const pages = Math.max(1, Math.ceil(rows.length / 20));
    state.page = Math.max(1, Math.min(pages, state.page));
    const cell = (key, value) => access[key] ? '<td>' + value + '</td>' : '';
    html('pdRows', rows.slice((state.page - 1) * 20, state.page * 20).map(row => {
      const initials = row.name.split(/\s+/).slice(0,2).map(s=>s[0]).join('');
      return '<tr><td><div class="pd-partner-name"><span class="pd-monogram">' + escape(initials) + '</span><strong>' + escape(row.name) + '</strong></div></td><td>' + (isClient ? numeric(row.surveys) : escape(row.segment_name)) + '</td>' + cell('hits',numeric(row.hits)) + cell('completes',numeric(row.completes)) + cell('conversion',percent(row.conversion)) + cell('accepted',numeric(row.accepted)) + cell('rejected',numeric(row.rejected)) + (isClient ? cell('revenue',money(row.revenue,row.currency)) : cell('pending',numeric(row.pending))) + cell('share',percent(row.share)) + '<td>' + (access.partner ? '<button type="button" class="pd-focus" data-focus="' + escape(row.id) + '" aria-label="Focus ' + escape(row.name) + '">↗</button>' : '') + '</td></tr>';
    }).join('') || '<tr><td colspan="10" class="pd-empty">No journeys match these filters.</td></tr>');
    text('pdResultCount', rows.length + ' ' + (isClient ? 'client' : 'supplier / branch') + ' rows · search affects this table only');
    text('pdTableCaption', 'Entry cohort · ' + payload.comparison_label);
    text('pdPage', state.page + ' / ' + pages);
    $('pdPrev').disabled = state.page === 1; $('pdNext').disabled = state.page === pages;
    root.querySelectorAll('[data-metric]').forEach(el => { el.hidden = !access[el.dataset.metric]; });
    root.querySelectorAll('[data-sort-header]').forEach(el => {
      if (el.dataset.sortHeader === state.sort) el.setAttribute('aria-sort', state.direction === -1 ? 'descending' : 'ascending');
      else el.removeAttribute('aria-sort');
      el.querySelector('span').textContent = el.dataset.sortHeader === state.sort ? (state.direction === -1 ? '↓' : '↑') : '↕';
    });
  }
  function options(id, rows, selected, label) {
    if (!$(id)) return;
    html(id, '<option value="">' + label + '</option>' + rows.map(r => '<option value="' + escape(r.id) + '">' + escape(r.name) + '</option>').join(''));
    if (selected && !rows.some(r => r.id === selected)) $(id).insertAdjacentHTML('beforeend', '<option value="' + escape(selected) + '">Selected filter · no activity</option>');
    $(id).value = selected;
  }
  function clearReport() {
    ['pdKpis','pdTrend','pdReview','pdRows'].forEach(id => html(id, ''));
    ['pdConversion','pdResultCount','pdTrendScope','pdTableCaption','pdPage'].forEach(id => text(id,'—'));
  }
  async function load() {
    const thisRequest = ++requestId;
    controller?.abort();
    const activeController = new AbortController();
    controller = activeController;
    payload = null; clearReport(); root.setAttribute('aria-busy','true');
    $('pdError').hidden = true; text('pdFreshness','Loading report…');
    root.querySelectorAll('[data-range]').forEach(el => { const active = el.dataset.range === state.range; el.classList.toggle('active',active); el.setAttribute('aria-pressed',String(active)); });
    const params = new URLSearchParams({format:'json',range:state.range});
    if (state.partner) params.set('partner',state.partner);
    if (state.segment) params.set('segment',state.segment);
    const timeout = setTimeout(() => activeController.abort(), 30000);
    try {
      const response = await fetch(root.dataset.endpoint + '?' + params, {signal:activeController.signal, credentials:'same-origin', headers:{Accept:'application/json'}});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error(response.status === 403 ? 'You no longer have access to this report. Refresh the page.' : 'Report could not be loaded. Check your connection or sign in, then retry.');
      const result = await response.json();
      if (thisRequest !== requestId) return;
      if (!result.summary || !result.access || !Array.isArray(result.rows)) throw new Error('Invalid report response. Please retry.');
      payload = result;
      options('pdPartner',payload.options.partners,state.partner,'All ' + (isClient ? 'clients' : 'suppliers'));
      options('pdSegment',payload.options.segments,state.segment,'All ' + (isClient ? 'countries' : 'branches'));
      text('pdPeriod',payload.period); text('pdDefinition',payload.definition);
      text('pdFreshness','Updated ' + new Date(payload.generated_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}));
      text('pdTrendScope',payload.comparison_label);
      renderCards(); renderTrend(); renderReview(); renderTable();
    } catch(error) {
      if (thisRequest !== requestId) return;
      text('pdFreshness','Report unavailable'); $('pdError').hidden = false;
      text('pdError',error.name === 'AbortError' ? 'Report timed out. Use Reset or choose a filter to retry.' : error.message);
    } finally { clearTimeout(timeout); if (thisRequest === requestId) root.setAttribute('aria-busy','false'); }
  }
  on('pdFilters','submit',e=>e.preventDefault());
  root.querySelectorAll('[data-range]').forEach(el=>el.addEventListener('click',()=>{state.range=el.dataset.range;state.page=1;load();}));
  on('pdPartner','change',e=>{state.partner=e.target.value;state.page=1;load();});
  on('pdSegment','change',e=>{state.segment=e.target.value;state.page=1;load();});
  on('pdSearch','input',e=>{state.search=e.target.value.trim().toLowerCase();state.page=1;renderTable();});
  root.querySelectorAll('[data-sort]').forEach(el=>el.addEventListener('click',()=>{state.direction=state.sort===el.dataset.sort ? -state.direction : -1;state.sort=el.dataset.sort;renderTable();}));
  on('pdPrev','click',()=>{state.page--;renderTable();}); on('pdNext','click',()=>{state.page++;renderTable();});
  on('pdRows','click',e=>{const el=e.target.closest('[data-focus]');if(el){state.partner=el.dataset.focus;state.page=1;load();}});
  on('pdFilters','reset',e=>{e.preventDefault();Object.assign(state,{range:'7d',partner:'',segment:'',search:'',sort:'completes',direction:-1,page:1});if($('pdSearch')) $('pdSearch').value='';load();});
  load();
})();
