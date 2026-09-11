/* Persistent, server-side report export queue. */
(() => {
  const storageKey = 'exchange-export-jobs-v1';
  const dismissedKey = 'exchange-export-dismissed-status-v1';
  let activeStatusJobId = null;
  const read = () => {
    try { return JSON.parse(localStorage.getItem(storageKey) || '[]'); } catch (_) { return []; }
  };
  const write = (jobs) => localStorage.setItem(storageKey, JSON.stringify(jobs.slice(-12)));
  const csrf = () => document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '';
  const statusToken = (job) => `${job.id}:${job.status}`;
  const statusNode = () => {
    let node = document.getElementById('exportQueueStatus');
    if (node) return node;
    node = document.createElement('div');
    node.id = 'exportQueueStatus';
    node.setAttribute('role', 'status');
    node.setAttribute('aria-live', 'polite');
    Object.assign(node.style, {
      position: 'fixed', right: '22px', bottom: '22px', zIndex: '10000', maxWidth: '390px',
      display: 'none', alignItems: 'center', gap: '12px', padding: '14px 16px', borderRadius: '12px',
      background: '#17233f', color: '#fff', boxShadow: '0 16px 38px rgba(23,35,63,.30)',
      fontFamily: 'DM Sans, system-ui, sans-serif', fontSize: '13px', lineHeight: '1.35',
    });
    document.body.append(node);
    return node;
  };
  const renderStatus = (jobs = read(), focusId = null) => {
    if (focusId) activeStatusJobId = focusId;
    const job = activeStatusJobId
      ? jobs.find((item) => item.id === activeStatusJobId)
      : [...jobs].reverse()[0];
    if (!job || job.downloaded) {
      const existing = document.getElementById('exportQueueStatus');
      if (existing) existing.style.display = 'none';
      return;
    }
    const node = statusNode();
    if (sessionStorage.getItem(dismissedKey) === statusToken(job)) {
      node.style.display = 'none';
      return;
    }
    node.replaceChildren();
    const copy = document.createElement('div'); copy.style.flex = '1';
    const heading = document.createElement('strong'); heading.style.display = 'block';
    const detail = document.createElement('span'); detail.style.opacity = '.8'; detail.style.fontSize = '11px';
    if (job.status === 'completed') {
      heading.textContent = 'Export is ready'; detail.textContent = 'Your file is ready to download.';
      node.style.background = '#17233f';
      const download = document.createElement('a');
      download.href = job.download_url; download.textContent = 'Download';
      Object.assign(download.style, { color: '#fff', background: '#16b9dc', padding: '8px 11px', borderRadius: '8px', textDecoration: 'none', fontWeight: '700', whiteSpace: 'nowrap' });
      download.addEventListener('click', () => {
        job.downloaded = true; write(jobs);
        sessionStorage.setItem(dismissedKey, statusToken(job));
        window.setTimeout(() => { node.style.display = 'none'; }, 0);
      });
      node.append(copy, download);
    } else if (job.status === 'failed') {
      heading.textContent = 'Export could not be created'; detail.textContent = job.error || 'Please try again.';
      node.style.background = '#a83c48'; node.append(copy);
    } else {
      heading.textContent = 'Export queued'; detail.textContent = 'Preparing your file in the background. You can keep working.';
      node.style.background = '#17233f'; node.append(copy);
    }
    copy.append(heading, detail);
    const close = document.createElement('button'); close.type = 'button'; close.textContent = '×';
    close.setAttribute('aria-label', 'Dismiss export status');
    Object.assign(close.style, { border: '0', background: 'transparent', color: '#fff', fontSize: '22px', cursor: 'pointer', padding: '0 0 0 4px', lineHeight: '1' });
    close.addEventListener('click', () => {
      sessionStorage.setItem(dismissedKey, statusToken(job));
      node.style.display = 'none';
    });
    node.append(close); node.style.display = 'flex';
  };
  const notify = (message, kind = 'success', action = null) => {
    const region = document.getElementById('toastRegion');
    if (!region) return;
    const node = document.createElement('div'); node.className = `toast ${kind}`;
    const text = document.createElement('span'); text.textContent = message; node.append(text);
    if (action) {
      const link = document.createElement('a'); link.href = action.url; link.textContent = action.label || 'Download';
      link.className = 'toast-action'; node.append(link);
    }
    region.append(node); window.setTimeout(() => node.remove(), action ? 14000 : 5000);
  };
  async function check(job) {
    const response = await fetch(job.status_url, { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw new Error('Could not check export status.');
    return response.json();
  }
  async function poll() {
    const jobs = read(); let changed = false;
    for (const job of jobs) {
      if (['completed', 'failed'].includes(job.status)) {
        if (!job.notified) {
          job.notified = true; changed = true;
          if (job.status === 'completed') notify('Your export is ready.', 'success', { url: job.download_url, label: 'Download' });
          else notify(job.error || 'Export could not be created.', 'error');
        }
        continue;
      }
      try {
        const data = await check(job);
        job.status = data.status; job.download_url = data.download_url || job.download_url; job.error = data.error || ''; job.downloaded = Boolean(data.downloaded);
        changed = true;
        if (data.status === 'completed' && !job.notified) {
          job.notified = true;
          notify('Your export is ready.', 'success', { url: data.download_url, label: 'Download' });
        } else if (data.status === 'failed' && !job.notified) {
          job.notified = true; notify(data.error || 'Export could not be created.', 'error');
        }
      } catch (_) { /* The next poll retries transient network errors. */ }
    }
    if (changed) write(jobs);
    renderStatus(jobs);
  }
  async function enqueue(kind, query = '') {
    const suffix = query ? `?${String(query).replace(/^\?/, '')}` : '';
    const jobs = read();
    const duplicate = [...jobs].reverse().find((job) => job.kind === kind && job.query === suffix && !job.downloaded && job.status !== 'failed');
    if (duplicate) {
      renderStatus(jobs, duplicate.id);
      notify(duplicate.status === 'completed' ? 'Your earlier export is ready to download.' : 'This export is already being prepared.', 'success');
      return { id: duplicate.id, status: duplicate.status, reused: true };
    }
    const response = await fetch(`/api/v1/export-jobs/${encodeURIComponent(kind)}/${suffix}`, {
      method: 'POST', credentials: 'same-origin', headers: { 'X-CSRFToken': csrf() },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Could not queue export.');
    const matching = jobs.find((job) => job.id === data.id);
    if (matching) {
      Object.assign(matching, { status: data.status, status_url: data.status_url, download_url: data.download_url || matching.download_url, kind, query: suffix, downloaded: false });
    } else {
      jobs.push({ id: data.id, status: data.status, status_url: data.status_url, download_url: data.download_url || '', kind, query: suffix, downloaded: false, notified: false });
    }
    write(jobs); renderStatus(jobs, data.id); notify(data.reused ? 'Existing export is already in progress.' : 'Export queued. You can safely continue working.', 'success'); poll();
    return data;
  }
  window.ExportQueue = { enqueue, poll };
  renderStatus(); poll(); window.setInterval(poll, 5000);
})();
