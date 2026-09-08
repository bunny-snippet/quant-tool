/* Lazy, persistent report panels. Sidebar, theme, export queue and tabs never remount. */
(() => {
  const tabs = document.querySelector('[data-reports-tabs]');
  const initial = document.querySelector('[data-report-panel]');
  if (!tabs || !initial) return;
  const host = initial.parentElement;
  const links = [...tabs.querySelectorAll('a')];
  const kind = url => /\/(?:reports\/term|termination-reasons)\/$/.test(url.pathname) ? 'term' :
    /\/(?:reports\/traffic|traffic-reports|studies)\/$/.test(url.pathname) ? 'traffic' :
    /\/reports\/reconciliation\/$/.test(url.pathname) ? 'reconciliation' : null;
  const allowed = new Set(links.map(link => kind(new URL(link.href))));
  const panels = new Map([[initial.dataset.reportPanel, initial]]);
  const urls = new Map([[initial.dataset.reportPanel, location.href]]);
  const scripts = new Set([initial.dataset.reportPanel]);
  const pending = new Map();
  const status = document.createElement('div');
  status.className = 'reports-load-status'; status.setAttribute('role', 'status');
  tabs.parentElement.after(status);
  let intent = 0;

  function canonical(raw) {
    const url = new URL(raw, location.href);
    const name = kind(url);
    if (url.origin !== location.origin || !allowed.has(name)) throw new Error('Report is not available.');
    url.pathname = `/reports/${name}/`;
    return url;
  }
  async function load(url, name) {
    if (pending.has(url.href)) return pending.get(url.href);
    const work = (async () => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 20000);
      try {
        const response = await fetch(url.href, {credentials:'same-origin', signal:controller.signal});
        if (!response.ok) throw new Error(`Could not load report (${response.status}).`);
        const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
        const incoming = doc.querySelector(`[data-report-panel="${name}"]`);
        const script = doc.querySelector(`script[data-report-script="${name}"]`);
        if (!incoming || (!script && name !== 'reconciliation')) throw new Error('Session expired or report access changed. Reload the page.');
        return {incoming, src:script?.getAttribute('src')};
      } finally { clearTimeout(timer); }
    })();
    pending.set(url.href, work);
    try { return await work; } finally { pending.delete(url.href); }
  }
  async function initialize(name, src) {
    if (name === 'reconciliation') { scripts.add(name); return; }
    if (scripts.has(name)) { if (name === 'term') window.initTermReport?.(); return; }
    const url = new URL(src, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith('/static/surveys/')) throw new Error('Invalid report asset.');
    await new Promise((resolve, reject) => {
      const script = document.createElement('script'); script.src = url.href;
      script.onload = resolve; script.onerror = () => { script.remove(); reject(new Error('Report script could not load. Please retry.')); };
      document.body.append(script);
    });
    scripts.add(name);
  }
  async function activate(raw, {remember=true, reload=false}={}) {
    const token = ++intent;
    let url;
    try {
      url = canonical(raw); const name = kind(url);
      if (reload || !panels.has(name) || !scripts.has(name) || (name === 'term' && urls.get(name) !== url.href)) {
        status.textContent = 'Loading report…'; tabs.setAttribute('aria-busy','true');
        const result = await load(url, name);
        if (token !== intent) return;
        result.incoming.hidden = true;
        const old = panels.get(name);
        if (old) old.replaceWith(result.incoming); else host.append(result.incoming);
        panels.set(name,result.incoming);
        await initialize(name,result.src);
        if (token !== intent) return;
      }
      panels.forEach((panel, key) => { panel.hidden = key !== name; });
      links.forEach(link => {
        const active = kind(new URL(link.href)) === name;
        link.classList.toggle('active',active);
        if(active) link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current');
      });
      urls.set(name,url.href);
      if (remember && location.href !== url.href) history.pushState({},'',url);
      document.title = `${name === 'reconciliation' ? 'Reconciliation' : name === 'term' ? 'Term Reports' : 'Traffic Reports'} · Survey Workspace`;
      window.FilterSelectionChips?.sync();
      status.textContent = '';
    } catch(error) {
      if(token !== intent) return;
      status.textContent = `${error.name === 'AbortError' ? 'Report request timed out.' : error.message} `;
      const retry = document.createElement('a'); retry.href = url?.href || location.href; retry.textContent = 'Retry';
      retry.addEventListener('click', event => {event.preventDefault();activate(retry.href,{reload:true});});
      status.append(retry);
    } finally { if (token === intent) tabs.removeAttribute('aria-busy'); }
  }
  tabs.addEventListener('click', event => {
    const link = event.target.closest('a');
    if (!link || event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    const name = kind(new URL(link.href));
    activate(urls.get(name) || link.href);
  });
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-report-panel="term"] a');
    if (!link || event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const url = new URL(link.href);
    if (url.origin !== location.origin || kind(url) !== 'term') return;
    event.preventDefault(); activate(url.href,{reload:true});
  });
  document.addEventListener('submit', event => {
    if (event.target.id !== 'reasonFilters') return;
    event.preventDefault();
    const url = new URL('/reports/term/',location.origin);
    url.search = new URLSearchParams(new FormData(event.target)).toString();
    activate(url.href,{reload:true});
  });
  window.addEventListener('popstate', () => { if (kind(new URL(location.href))) activate(location.href,{remember:false}); });
})();
