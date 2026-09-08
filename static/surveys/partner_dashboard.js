/* Permission-scoped, read-only analytics. No sample data or business writes. */
(() => {
  'use strict';
  const root = document.getElementById('partnerDashboard');
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const isClient = root.dataset.section === 'client';
  const state = { range: 'today', financialYear:'', partner: '', segment: '', search: '', sort: 'completes', direction: -1, page: 1 };
  let mapShapes = null, mapPromise = null, mapCountry = '';
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
      const meta = key === 'acceptance' ? delta(ratio(s.accepted,s.completes),ratio(p.accepted,p.completes)) : ['revenue','rpc'].includes(key) && (s.money_note || p.currency !== s.currency) ? escape(s.money_note || 'Currency differs from previous period') : delta(s[key], p[key]);
      return '<article class="pd-kpi"><div class="pd-kpi-label">' + label + '</div><strong>' + value + '</strong><div class="pd-kpi-meta" title="' + escape(payload.comparison_label) + '">' + meta + '</div></article>';
    }).join(''));
    text('pdConversion', percent(s.conversion));
  }
  function renderTrend() {
    if (!$('pdTrend')) return;
    $('pdTrend').closest('.pd-panel').hidden = !payload.access.trend;
    if (!payload.access.trend) { html('pdTrend', ''); return; }
    const timeline = payload.trend, a = payload.access;
    if (!timeline.length || !timeline.some(p=>p.hits || p.completes)) { html('pdTrend', '<p class="pd-empty">No activity in this period.</p>'); return; }
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
        svg += '<rect tabindex="0" aria-label="'+escape(point.label+': '+numeric(point[key])+' '+key)+'" class="pd-chart-' + (key === 'hits' ? 'hit' : 'complete') + '" x="' + (x + offset) + '" y="' + (190 - h) + '" width="' + bar + '" height="' + h + '" rx="2"><title>' + escape(point.label) + ': ' + numeric(point[key]) + ' ' + key + '</title></rect>';
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
    text('pdResultCount', rows.length + ' rows');
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
    ['pdKpis','pdTrend','pdReview','pdRows','pdWorldMap','pdMapDetail','pdMapCountry'].forEach(id => html(id, ''));
    ['pdConversion','pdResultCount','pdPage'].forEach(id => text(id,'—'));
  }
  function renderCountryDetail() {
    const country=(payload?.countries || []).find(row=>row.code===mapCountry);
    if (!country) { html('pdMapDetail','<p class="pd-empty">No completes for these filters.</p>'); return; }
    html('pdMapDetail','<h3>'+escape(country.name)+'</h3><p><strong>'+numeric(country.completes)+'</strong> completes</p>'+country.clients.map(c=>'<div class="pd-country-client"><span>'+escape(c.name)+'</span><strong>'+numeric(c.completes)+'</strong></div>').join(''));
    if ($('pdMapCountry')) $('pdMapCountry').value=mapCountry;
  }
  async function renderMap() {
    if (!$('pdMapPanel')) return;
    $('pdMapPanel').hidden=!payload?.access.world_map;
    if (!payload?.access.world_map) return;
    const current=payload, rows=current.countries || [];
    if (!rows.some(c=>c.code===mapCountry)) mapCountry=rows[0]?.code || '';
    options('pdMapCountry',rows.map(c=>({id:c.code,name:c.name+' · '+numeric(c.completes)})),mapCountry,'Choose country');
    renderCountryDetail();
    if (!rows.length) { html('pdWorldMap','<p class="pd-empty">No completed journeys in this period.</p>'); return; }
    try {
      if (!mapShapes) {
        if (!mapPromise) mapPromise=fetch(root.dataset.mapUrl,{credentials:'same-origin'}).then(r=>{if(!r.ok)throw new Error('map');return r.json();}).catch(error=>{mapPromise=null;throw error;});
        mapShapes=await mapPromise;
      }
      if (payload!==current || !payload.access.world_map) return;
      const byCode=new Map(rows.map(c=>[c.code==='UK'?'GB':c.code,c])), maximum=Math.max(1,...rows.map(c=>c.completes));
      html('pdWorldMap','<svg viewBox="0 0 720 300" role="group" aria-label="Completes by country">'+mapShapes.map(shape=>{
        const row=byCode.get(shape.code), count=row?.completes || 0;
        const shade=count ? .25+.75*Math.log1p(count)/Math.log1p(maximum) : 0;
        return '<path class="pd-country'+(count?' has-completes':'')+'" d="'+escape(shape.path)+'" data-country="'+escape(row?.code || shape.code)+'" data-country-name="'+escape(shape.name)+'" '+(count?'tabindex="0" role="button" aria-describedby="pdMapTooltip"':'')+' style="--country-shade:'+shade+'" aria-label="'+escape(shape.name+': '+numeric(count)+' completes')+'"></path>';
      }).join('')+'</svg><div id="pdMapTooltip" class="pd-map-tooltip" role="tooltip" tabindex="0" hidden></div>');
    } catch (_) { if(payload===current) html('pdWorldMap','<p class="pd-empty">Map unavailable. Country totals and client details remain available in the list.</p>'); }
  }
  function hideMapTooltip() { if ($('pdMapTooltip')) $('pdMapTooltip').hidden=true; }
  function showMapTooltip(event) {
    if (event.target.closest('.pd-map-tooltip')) return;
    const target=event.target.closest('[data-country]'), tip=$('pdMapTooltip'), host=$('pdWorldMap');
    if (!target || !tip || !payload?.access.world_map) { hideMapTooltip(); return; }
    const row=(payload.countries || []).find(c=>c.code===target.dataset.country) || {name:target.dataset.countryName,completes:0,clients:[]};
    // Pointer movement only repositions; it does not rebuild the client list.
    const key=target.dataset.country+':'+target.dataset.countryName;
    if (tip.dataset.country!==key || tip.hidden) {
      tip.dataset.country=key;
      tip.scrollTop=0;
      tip.innerHTML='<header><strong>'+escape(row.name)+'</strong><span>'+numeric(row.completes)+' <small>completes</small></span></header><div class="pd-map-tooltip-clients">'+row.clients.map(c=>'<div><span>'+escape(c.name)+'</span><b>'+numeric(c.completes)+'</b></div>').join('')+'</div>';
    }
    tip.hidden=false;
    const box=host.getBoundingClientRect(), rect=target.getBoundingClientRect();
    tip.style.maxHeight=Math.max(100,box.height-8)+'px';
    const x=Number.isFinite(event.clientX) ? event.clientX-box.left : rect.left-box.left+rect.width/2;
    const y=Number.isFinite(event.clientY) ? event.clientY-box.top : rect.top-box.top+rect.height/2;
    tip.style.left=Math.max(4,Math.min(box.width-tip.offsetWidth-4,x+14))+'px';
    tip.style.top=Math.max(4,Math.min(box.height-tip.offsetHeight-4,y+14))+'px';
  }
  async function load() {
    const thisRequest = ++requestId;
    controller?.abort();
    const activeController = new AbortController();
    controller = activeController;
    payload = null; clearReport(); root.setAttribute('aria-busy','true');
    $('pdLoading').hidden = false; $('pdEmpty').hidden = true; $('pdErrorBox').hidden = true;
    $('pdError').hidden = true; text('pdFreshness','Loading report…');
    root.querySelectorAll('[data-range]').forEach(el => { const active = el.dataset.range === state.range; el.classList.toggle('active',active); el.setAttribute('aria-pressed',String(active)); });
    const params = new URLSearchParams({format:'json',range:state.range});
    if (state.range === 'fy' && state.financialYear) params.set('financial_year',state.financialYear);
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
      $('pdEmpty').hidden = payload.summary.hits !== 0;
      options('pdPartner',payload.options.partners,state.partner,'All ' + (isClient ? 'clients' : 'suppliers'));
      options('pdSegment',payload.options.segments,state.segment,'All ' + (isClient ? 'countries' : 'branches'));
      if ($('pdDays')) $('pdDays').value = ['7d','15d','21d','28d'].includes(state.range) ? state.range : '';
      if ($('pdFinancialYear')) {
        html('pdFinancialYear','<option value="">Financial year</option>'+(payload.financial_years || []).map(y=>'<option value="'+escape(y.start_year)+'">'+escape(y.label)+'</option>').join(''));
        $('pdFinancialYear').value=state.range === 'fy' ? state.financialYear : '';
      }
      text('pdComparison',payload.comparison_label);
      text('pdPeriod',payload.period);
      text('pdFreshness','Updated ' + new Date(payload.generated_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}));
      renderCards(); renderTrend(); renderReview(); renderTable();
      renderMap();
    } catch(error) {
      if (thisRequest !== requestId) return;
      text('pdFreshness','Report unavailable'); $('pdError').hidden = false;
      $('pdErrorBox').hidden = false;
      text('pdError',error.name === 'AbortError' ? 'Report timed out. Use Reset or choose a filter to retry.' : error.message);
    } finally { clearTimeout(timeout); if (thisRequest === requestId) { root.setAttribute('aria-busy','false'); $('pdLoading').hidden = true; } }
  }
  on('pdFilters','submit',e=>e.preventDefault());
  on('pdRetry','click',load);
  root.querySelectorAll('[data-range]').forEach(el=>el.addEventListener('click',()=>{state.range=el.dataset.range;state.page=1;load();}));
  on('pdPartner','change',e=>{state.partner=e.target.value;state.page=1;load();});
  on('pdMapCountry','change',e=>{mapCountry=e.target.value;renderCountryDetail();});
  on('pdWorldMap','click',e=>{const country=e.target.closest('[data-country]');if(country){mapCountry=country.dataset.country;renderCountryDetail();}});
  on('pdWorldMap','pointermove',showMapTooltip);
  on('pdWorldMap','pointerleave',hideMapTooltip);
  on('pdWorldMap','focusin',showMapTooltip);
  on('pdWorldMap','focusout',e=>{if(!e.relatedTarget || !$('pdWorldMap').contains(e.relatedTarget))hideMapTooltip();});
  on('pdWorldMap','keyup',e=>{if(e.key==='Escape')hideMapTooltip();});
  on('pdWorldMap','keydown',e=>{if(e.key==='Enter'||e.key===' '){const country=e.target.closest('[data-country]');if(country){e.preventDefault();mapCountry=country.dataset.country;renderCountryDetail();}}});
  on('pdDays','change',e=>{if(e.target.value){state.range=e.target.value;state.page=1;load();}});
  on('pdFinancialYear','change',e=>{if(e.target.value){state.range='fy';state.financialYear=e.target.value;state.page=1;load();}});
  on('pdSegment','change',e=>{state.segment=e.target.value;state.page=1;load();});
  on('pdSearch','input',e=>{state.search=e.target.value.trim().toLowerCase();state.page=1;renderTable();});
  root.querySelectorAll('[data-sort]').forEach(el=>el.addEventListener('click',()=>{state.direction=state.sort===el.dataset.sort ? -state.direction : -1;state.sort=el.dataset.sort;renderTable();}));
  on('pdPrev','click',()=>{state.page--;renderTable();}); on('pdNext','click',()=>{state.page++;renderTable();});
  on('pdRows','click',e=>{const el=e.target.closest('[data-focus]');if(el){state.partner=el.dataset.focus;state.page=1;load();}});
  on('pdFilters','reset',e=>{e.preventDefault();Object.assign(state,{range:'today',financialYear:'',partner:'',segment:'',search:'',sort:'completes',direction:-1,page:1});if($('pdSearch')) $('pdSearch').value='';load();});
  load();
})();
