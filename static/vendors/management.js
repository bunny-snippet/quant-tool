(() => {
  const workspace = document.querySelector('#vendorWorkspace');
  if (!workspace) return;

  const backdrop = document.querySelector('#vendorModalBackdrop');
  const modalConfigs = {
    policy: { form: document.querySelector('#vendorPolicyForm'), modal: document.querySelector('#vendorPolicyModal') },
    client: { form: document.querySelector('#clientAllocationForm'), modal: document.querySelector('#clientAllocationModal') },
    survey: { form: document.querySelector('#surveyAllocationForm'), modal: document.querySelector('#surveyAllocationModal') },
    api_key: { form: document.querySelector('#vendorApiKeyForm'), modal: document.querySelector('#vendorApiKeyModal') },
  };
  let activeMode = null;
  let form = null;
  let modal = null;
  let errorBox = null;
  const readColumns = (id) => new Set(JSON.parse(document.getElementById(id)?.textContent || '[]'));
  const vendorColumns = readColumns('vendorColumnAccess');
  const clientColumns = readColumns('clientAllocationColumnAccess');
  const projectColumns = readColumns('projectAllocationColumnAccess');
  const apiColumns = readColumns('apiKeyColumnAccess');
  const canViewVendors = workspace.dataset.viewVendors === 'true';
  const canViewClientAllocations = workspace.dataset.viewClientAllocations === 'true';
  const canViewProjectAllocations = workspace.dataset.viewProjectAllocations === 'true';
  const canViewApiKeys = workspace.dataset.viewApiKeys === 'true';
  const canEditPolicy = workspace.dataset.canEditPolicy === 'true';
  const canAllocateClient = workspace.dataset.allocateClient === 'true';
  const canAllocateProject = workspace.dataset.allocateProject === 'true';
  const canCreateApiKey = workspace.dataset.createApiKey === 'true';
  const canRevokeApiKey = workspace.dataset.revokeApiKey === 'true';
  const state = {
    vendors: [], profiles: [], clients: [], clientAllocations: [], surveyAllocations: [], apiKeys: [],
    selectedSurvey: null, searchTimer: null,
  };
  const clientPicker = document.querySelector('#vendorClientPicker');

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const field = (name, mode = activeMode) => modalConfigs[mode]?.form?.elements[name];

  function csrfToken() {
    return document.cookie.split('; ').find((item) => item.startsWith('csrftoken='))?.split('=').slice(1).join('=') ||
      document.querySelector('input[name=csrfmiddlewaretoken]')?.value || '';
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, (char) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
    })[char]);
  }

  function flattenError(value, prefix = '') {
    if (Array.isArray(value)) return value.map((item) => flattenError(item, prefix)).join(' ');
    if (value && typeof value === 'object') {
      return Object.entries(value).map(([key, item]) => flattenError(item, key === 'non_field_errors' ? prefix : key)).join(' ');
    }
    return `${prefix ? `${prefix}: ` : ''}${value || 'Request could not be completed.'}`;
  }

  async function api(url, options = {}) {
    const response = await fetch(url, {
      credentials: 'same-origin',
      ...options,
      headers: {
        Accept: 'application/json',
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        'X-CSRFToken': csrfToken(),
        ...(options.headers || {}),
      },
    });
    const data = response.status === 204 ? null : await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(flattenError(data));
    return data;
  }

  async function fetchAll(url) {
    let next = `${url}${url.includes('?') ? '&' : '?'}page_size=100`;
    const rows = [];
    while (next) {
      const data = await api(next);
      if (Array.isArray(data)) return data;
      rows.push(...(data.results || []));
      next = data.next;
    }
    return rows;
  }

  function initials(name) {
    return String(name || 'V').trim().split(/\s+/).slice(0, 2).map((part) => part[0]).join('').toUpperCase();
  }

  function accountLabel(type) {
    return type === 'internal_vendor' ? 'Internal' : type === 'external_vendor' ? 'External' : 'Supplier';
  }

  function deliveryLabel(mode) {
    return mode === 'api' ? 'API only' : mode === 'both' ? 'Panel + API' : 'Panel only';
  }

  function number(value) {
    return new Intl.NumberFormat('en-IN').format(Number(value || 0));
  }

  function dateTime(value) {
    if (!value) return 'No limit';
    return new Intl.DateTimeFormat('en-IN', {
      dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Kolkata',
    }).format(new Date(value));
  }

  function toInputDateTime(value) {
    if (!value) return '';
    const date = new Date(value);
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
  }

  function toApiDateTime(value) {
    return value ? new Date(value).toISOString() : null;
  }

  function nullableNumber(value) {
    return value === '' ? null : value;
  }

  function toast(message, isError = false) {
    const region = document.querySelector('#toastRegion');
    if (!region) return;
    const item = document.createElement('div');
    item.className = `toast${isError ? ' error' : ''}`;
    item.textContent = message;
    region.appendChild(item);
    requestAnimationFrame(() => item.classList.add('show'));
    setTimeout(() => { item.classList.remove('show'); setTimeout(() => item.remove(), 220); }, 3200);
  }

  function vendorIdentity(vendor) {
    return `<div class="vendor-identity"><span>${escapeHtml(initials(vendor.full_name))}</span><div><strong>${escapeHtml(vendor.full_name)}</strong><small>${escapeHtml(vendor.email || vendor.username)}</small></div></div>`;
  }

  function typeBadge(type) {
    return `<span class="vendor-type ${escapeHtml(type)}">${escapeHtml(accountLabel(type))}</span>`;
  }

  function stateBadge(active) {
    return `<span class="vendor-state${active ? '' : ' inactive'}">${active ? 'Active' : 'Inactive'}</span>`;
  }

  function quantityMarkup(record) {
    const limit = Number(record.quantity_limit || 0);
    const used = Number(record.consumed_quantity || 0);
    const reserved = Number(record.reserved_quantity || 0);
    const percent = limit ? Math.min(100, ((used + reserved) / limit) * 100) : 0;
    return `<div class="quantity-cell"><div class="quantity-line"><strong>${number(record.remaining_quantity)} left</strong><span>${number(used)} used · ${number(reserved)} held / ${number(limit)}</span></div><div class="quantity-bar"><i style="width:${percent}%"></i></div></div>`;
  }

  function cutMarkup(record, inheritedLabel = 'effective') {
    const own = record.cpi_cut_override_percent;
    return `<div class="vendor-money"><strong>${escapeHtml(record.effective_cpi_cut_percent ?? 0)}%</strong><small>${own === null || own === undefined ? inheritedLabel : 'override'}</small></div>`;
  }

  function emptyRow(columns, message) {
    return `<tr><td colspan="${columns}"><div class="vendor-empty">${escapeHtml(message)}</div></td></tr>`;
  }

  function actionButton(kind, id, allowed, label = 'Edit') {
    return allowed ? `<button class="vendor-action" type="button" data-edit-${kind}="${id}">${escapeHtml(label)}</button>` : '';
  }

  function renderOverview() {
    if ($('#vendorCount')) $('#vendorCount').textContent = number(state.vendors.length);
    if ($('#allocationCount')) $('#allocationCount').textContent = number(state.clientAllocations.filter((row) => row.is_active).length);
    if ($('#remainingQuantity')) $('#remainingQuantity').textContent = number(state.clientAllocations.reduce((total, row) => total + Number(row.remaining_quantity || 0), 0));
    if ($('#surveyRuleCount')) $('#surveyRuleCount').textContent = number(state.surveyAllocations.length);
  }

  function renderVendors() {
    if (!$('#vendorRows')) return;
    const profiles = new Map(state.profiles.map((item) => [Number(item.vendor), item]));
    const rows = state.vendors.map((vendor) => {
      const profile = profiles.get(Number(vendor.id));
      const cut = vendor.average_client_cpi_cut_percent ?? '0.00';
      const allocationCount = vendor.active_client_allocation_count ?? vendor.allocation_count ?? 0;
      const cells = [];
      if (vendorColumns.has('name')) cells.push(`<td>${vendorIdentity(vendor)}</td>`);
      if (vendorColumns.has('type')) cells.push(`<td>${typeBadge(vendor.account_type)}</td>`);
      if (vendorColumns.has('cpi')) cells.push(`<td><div class="vendor-money"><strong>${escapeHtml(cut)}%</strong><small>${allocationCount ? `average across ${number(allocationCount)} client${allocationCount === 1 ? '' : 's'}` : 'supplier default'}</small></div></td>`);
      if (vendorColumns.has('clients')) cells.push(`<td>${number(allocationCount)}</td>`);
      if (vendorColumns.has('status')) cells.push(`<td>${stateBadge(vendor.is_active && (profile?.is_active ?? true))}<small class="delivery-label">${escapeHtml(deliveryLabel(profile?.delivery_mode || vendor.delivery_mode))}</small></td>`);
      if (vendorColumns.has('actions')) cells.push(`<td>${actionButton('policy', vendor.id, canEditPolicy)}</td>`);
      return `<tr>${cells.join('') || '<td><div class="vendor-empty">No supplier columns assigned.</div></td>'}</tr>`;
    }).join('') || emptyRow(Math.max(1, vendorColumns.size), 'No internal or external suppliers have been created yet.');
    $('#vendorRows').innerHTML = rows;
    $('#vendorCards').innerHTML = state.vendors.map((vendor) => {
      const profile = profiles.get(Number(vendor.id));
      const cut = vendor.average_client_cpi_cut_percent ?? '0.00';
      const allocationCount = vendor.active_client_allocation_count ?? vendor.allocation_count ?? 0;
      const head = `${vendorColumns.has('name') ? vendorIdentity(vendor) : ''}${vendorColumns.has('type') ? typeBadge(vendor.account_type) : ''}`;
      const details = `${vendorColumns.has('cpi') ? `<span>Average CPI cut<strong>${escapeHtml(cut)}%</strong></span>` : ''}${vendorColumns.has('clients') ? `<span>Client grants<strong>${number(allocationCount)}</strong></span>` : ''}${vendorColumns.has('status') ? `<span>Delivery<strong>${escapeHtml(deliveryLabel(profile?.delivery_mode || vendor.delivery_mode))}</strong></span>` : ''}`;
      return `<article class="vendor-card">${head ? `<div class="vendor-card-head">${head}</div>` : ''}${details ? `<div class="vendor-card-grid">${details}</div>` : ''}${vendorColumns.has('actions') ? actionButton('policy', vendor.id, canEditPolicy) : ''}</article>`;
    }).join('');
  }

  function renderClientAllocations() {
    if (!$('#clientAllocationRows')) return;
    $('#clientAllocationRows').innerHTML = state.clientAllocations.map((row) => {
      const cells = [];
      if (clientColumns.has('vendor')) cells.push(`<td><strong>${escapeHtml(row.vendor_name)}</strong><br>${typeBadge(row.account_type)}</td>`);
      if (clientColumns.has('client')) cells.push(`<td><strong>${escapeHtml(row.client_name)}</strong><br><small>${stateBadge(row.is_active)}</small></td>`);
      if (clientColumns.has('quantity')) cells.push(`<td>${quantityMarkup(row)}</td>`);
      if (clientColumns.has('cpi')) cells.push(`<td>${cutMarkup(row, 'supplier default')}</td>`);
      if (clientColumns.has('window')) cells.push(`<td><div class="vendor-window"><span>${dateTime(row.starts_at)}</span><span>to ${dateTime(row.ends_at)}</span></div></td>`);
      if (clientColumns.has('actions')) cells.push(`<td>${actionButton('client', row.id, canAllocateClient, 'View')}</td>`);
      return `<tr>${cells.join('') || '<td><div class="vendor-empty">No client-allocation columns assigned.</div></td>'}</tr>`;
    }).join('') || emptyRow(Math.max(1, clientColumns.size), 'No client allocations yet.');
    $('#clientAllocationCards').innerHTML = state.clientAllocations.map((row) => {
      const head = `${clientColumns.has('vendor') ? `<strong>${escapeHtml(row.vendor_name)}</strong>` : ''}${clientColumns.has('client') ? `<small>${escapeHtml(row.client_name)}</small>` : ''}`;
      const details = `${clientColumns.has('quantity') ? `<span>Available<strong>${number(row.remaining_quantity)}</strong></span><span>Limit<strong>${number(row.quantity_limit)}</strong></span>` : ''}${clientColumns.has('cpi') ? `<span>CPI cut<strong>${escapeHtml(row.effective_cpi_cut_percent)}%</strong></span>` : ''}${clientColumns.has('vendor') ? `<span>Type<strong>${escapeHtml(accountLabel(row.account_type))}</strong></span>` : ''}${clientColumns.has('window') ? `<span>Window<strong>${dateTime(row.starts_at)} to ${dateTime(row.ends_at)}</strong></span>` : ''}`;
      return `<article class="vendor-card">${head ? `<div class="vendor-card-head"><div>${head}</div>${clientColumns.has('client') ? stateBadge(row.is_active) : ''}</div>` : ''}${details ? `<div class="vendor-card-grid">${details}</div>` : ''}${clientColumns.has('quantity') ? quantityMarkup(row) : ''}${clientColumns.has('actions') ? actionButton('client', row.id, canAllocateClient, 'View') : ''}</article>`;
    }).join('');
  }

  function renderSurveyAllocations() {
    if (!$('#surveyAllocationRows')) return;
    $('#surveyAllocationRows').innerHTML = state.surveyAllocations.map((row) => {
      const cells = [];
      if (projectColumns.has('vendor')) cells.push(`<td><strong>${escapeHtml(row.vendor_name)}</strong></td>`);
      if (projectColumns.has('survey')) cells.push(`<td><strong>${escapeHtml(row.survey_local_id)}</strong><br><small>#${escapeHtml(row.survey_source_id)} · ${escapeHtml(row.survey_name || 'Survey')}</small></td>`);
      if (projectColumns.has('client')) cells.push(`<td>${escapeHtml(row.client_name)}</td>`);
      if (projectColumns.has('quantity')) cells.push(`<td>${quantityMarkup(row)}</td>`);
      if (projectColumns.has('cpi')) cells.push(`<td>${cutMarkup(row, 'client policy')}</td>`);
      if (projectColumns.has('actions')) cells.push(`<td>${actionButton('survey', row.id, canAllocateProject)}</td>`);
      return `<tr>${cells.join('') || '<td><div class="vendor-empty">No project-allocation columns assigned.</div></td>'}</tr>`;
    }).join('') || emptyRow(Math.max(1, projectColumns.size), 'No projects allocated. This supplier cannot see or start any client project yet.');
    $('#surveyAllocationCards').innerHTML = state.surveyAllocations.map((row) => {
      const head = `${projectColumns.has('survey') ? `<strong>${escapeHtml(row.survey_local_id)}</strong>` : ''}${projectColumns.has('vendor') || projectColumns.has('client') ? `<small>${projectColumns.has('vendor') ? escapeHtml(row.vendor_name) : ''}${projectColumns.has('vendor') && projectColumns.has('client') ? ' · ' : ''}${projectColumns.has('client') ? escapeHtml(row.client_name) : ''}</small>` : ''}`;
      const details = `${projectColumns.has('survey') ? `<span>Survey ID<strong>${escapeHtml(row.survey_source_id)}</strong></span>` : ''}${projectColumns.has('cpi') ? `<span>CPI cut<strong>${escapeHtml(row.effective_cpi_cut_percent)}%</strong></span>` : ''}${projectColumns.has('quantity') ? `<span>Available<strong>${number(row.remaining_quantity)}</strong></span><span>Limit<strong>${number(row.quantity_limit)}</strong></span>` : ''}`;
      return `<article class="vendor-card">${head ? `<div class="vendor-card-head"><div>${head}</div>${stateBadge(row.is_active)}</div>` : ''}${details ? `<div class="vendor-card-grid">${details}</div>` : ''}${projectColumns.has('actions') ? actionButton('survey', row.id, canAllocateProject) : ''}</article>`;
    }).join('');
  }

  function renderApiKeys() {
    if (!$('#apiKeyRows')) return;
    $('#apiKeyRows').innerHTML = state.apiKeys.map((key) => {
      const cells = [];
      if (apiColumns.has('vendor')) cells.push(`<td><strong>${escapeHtml(key.vendor_name)}</strong><br>${typeBadge(key.account_type)}</td>`);
      if (apiColumns.has('key')) cells.push(`<td><div class="vendor-money"><strong>${escapeHtml(key.name)}</strong><small>${escapeHtml(key.masked_key)} · ${escapeHtml((key.client_names || []).join(', ') || 'No clients')}</small></div></td>`);
      if (apiColumns.has('created')) cells.push(`<td>${dateTime(key.created_at)}</td>`);
      if (apiColumns.has('last_used')) cells.push(`<td>${key.last_used_at ? dateTime(key.last_used_at) : 'Never'}</td>`);
      if (apiColumns.has('expires')) cells.push(`<td>${key.expires_at ? dateTime(key.expires_at) : 'No expiry'}</td>`);
      if (apiColumns.has('actions')) cells.push(`<td>${key.is_active && canRevokeApiKey ? `<button class="vendor-action danger" type="button" data-revoke-api-key="${key.id}">Revoke</button>` : stateBadge(false)}</td>`);
      return `<tr>${cells.join('') || '<td><div class="vendor-empty">No API-key columns assigned.</div></td>'}</tr>`;
    }).join('') || emptyRow(Math.max(1, apiColumns.size), 'No API keys issued yet.');
    $('#apiKeyCards').innerHTML = state.apiKeys.map((key) => {
      const head = `${apiColumns.has('key') ? `<strong>${escapeHtml(key.name)}</strong>` : ''}${apiColumns.has('vendor') ? `<small>${escapeHtml(key.vendor_name)}</small>` : ''}`;
      const details = `${apiColumns.has('key') ? `<span>Key<strong>${escapeHtml(key.masked_key)}</strong></span><span>Clients<strong>${escapeHtml((key.client_names || []).join(', ') || 'None')}</strong></span>` : ''}${apiColumns.has('last_used') ? `<span>Last used<strong>${key.last_used_at ? dateTime(key.last_used_at) : 'Never'}</strong></span>` : ''}${apiColumns.has('created') ? `<span>Created<strong>${dateTime(key.created_at)}</strong></span>` : ''}${apiColumns.has('expires') ? `<span>Expires<strong>${key.expires_at ? dateTime(key.expires_at) : 'Never'}</strong></span>` : ''}`;
      return `<article class="vendor-card">${head ? `<div class="vendor-card-head"><div>${head}</div>${key.is_active ? stateBadge(true) : stateBadge(false)}</div>` : ''}${details ? `<div class="vendor-card-grid">${details}</div>` : ''}${apiColumns.has('actions') && key.is_active && canRevokeApiKey ? `<button class="vendor-action danger" type="button" data-revoke-api-key="${key.id}">Revoke key</button>` : ''}</article>`;
    }).join('');
  }

  function render() {
    renderOverview(); renderVendors(); renderClientAllocations(); renderSurveyAllocations(); renderApiKeys();
  }

  function option(value, label, selected = false) {
    return `<option value="${escapeHtml(value)}"${selected ? ' selected' : ''}>${escapeHtml(label)}</option>`;
  }

  function selectedClientIds() {
    return [...field('client', 'client').selectedOptions].map((item) => Number(item.value));
  }

  function selectedApiAllocationIds() {
    return $$('#apiClientAllocationChoices input[type="checkbox"]:checked').map((item) => Number(item.value));
  }

  function renderApiAllocationChoices() {
    const container = $('#apiClientAllocationChoices');
    if (!container) return;
    const vendorId = Number(field('api_vendor', 'api_key')?.value || 0);
    const allocations = state.clientAllocations.filter((row) => (
      Number(row.vendor) === vendorId && row.is_active
    ));
    container.innerHTML = allocations.length ? allocations.map((row) => (
      `<label><input type="checkbox" value="${row.id}"><span><strong>${escapeHtml(row.client_name)}</strong><small>${escapeHtml(row.effective_cpi_cut_percent)}% cut · ${number(row.remaining_quantity)} completes left</small></span></label>`
    )).join('') : '<div class="vendor-choice-empty">Select an external supplier with an active client allocation.</div>';
  }

  function updateClientPickerLabel() {
    const selected = selectedClientIds();
    const label = clientPicker.querySelector('.vendor-client-picker-trigger span');
    if (!selected.length) label.textContent = 'Select one or more clients';
    else if (selected.length === 1) label.textContent = state.clients.find((item) => Number(item.id) === selected[0])?.name || '1 client selected';
    else label.textContent = `${selected.length} clients selected`;
  }

  function renderClientPickerOptions(needle = '') {
    const selected = new Set(selectedClientIds());
    const normalized = needle.trim().toLowerCase();
    const visible = state.clients.filter((client) => !normalized || client.name.toLowerCase().includes(normalized));
    clientPicker.querySelector('.vendor-client-picker-options').innerHTML = visible.length ? visible.map((client) => (
      `<label><input type="checkbox" value="${client.id}"${selected.has(Number(client.id)) ? ' checked' : ''}><span>${escapeHtml(client.name)}</span></label>`
    )).join('') : '<div class="vendor-empty">No matching clients</div>';
  }

  function setClientSelection(ids, disabled = false) {
    const selected = new Set((ids || []).map(Number));
    [...field('client', 'client').options].forEach((item) => { item.selected = selected.has(Number(item.value)); });
    clientPicker.classList.toggle('disabled', disabled);
    clientPicker.querySelector('.vendor-client-picker-trigger').disabled = disabled;
    renderClientPickerOptions();
    updateClientPickerLabel();
  }

  function hydrateSelects() {
    const vendorOptions = state.vendors.map((vendor) => option(vendor.id, `${vendor.full_name} — ${accountLabel(vendor.account_type)}`)).join('');
    field('policy_vendor', 'policy').innerHTML = vendorOptions;
    field('client_vendor', 'client').innerHTML = `<option value="">Select supplier</option>${vendorOptions}`;
    field('client', 'client').innerHTML = state.clients.map((client) => option(client.id, client.name)).join('');
    renderClientPickerOptions(); updateClientPickerLabel();
    field('client_allocation', 'survey').innerHTML = `<option value="">Select supplier and client</option>${state.clientAllocations.map((row) => option(row.id, `${row.vendor_name} — ${row.client_name}`)).join('')}`;
    field('api_vendor', 'api_key').innerHTML = `<option value="">Select API-enabled external supplier</option>${state.vendors.filter((vendor) => {
      const profile = state.profiles.find((item) => Number(item.vendor) === Number(vendor.id));
      return vendor.account_type === 'external_vendor' && ['api', 'both'].includes(profile?.delivery_mode || vendor.delivery_mode);
    }).map((vendor) => option(vendor.id, vendor.full_name)).join('')}`;
    renderApiAllocationChoices();
  }

  function updatePolicyRule() {
    const vendor = state.vendors.find((item) => String(item.id) === field('policy_vendor', 'policy').value);
    const internal = vendor?.account_type === 'internal_vendor';
    field('default_cpi_cut_percent', 'policy').disabled = internal;
    if (internal) field('default_cpi_cut_percent', 'policy').value = '0.00';
    field('delivery_mode', 'policy').disabled = internal;
    if (internal) field('delivery_mode', 'policy').value = 'panel';
    $('#policyRuleNote').textContent = internal ? 'Internal suppliers always receive the full source CPI.' : 'External supplier payable CPI = source CPI minus this percentage.';
  }

  function updateClientRule() {
    const vendor = state.vendors.find((item) => String(item.id) === field('client_vendor', 'client').value);
    const internal = vendor?.account_type === 'internal_vendor';
    field('client_cpi_cut', 'client').disabled = internal;
    if (internal) field('client_cpi_cut', 'client').value = '';
  }

  function updateSurveyRule() {
    const parent = state.clientAllocations.find((item) => String(item.id) === field('client_allocation', 'survey').value);
    const internal = parent?.account_type === 'internal_vendor';
    field('survey_cpi_cut', 'survey').disabled = internal;
    if (internal) field('survey_cpi_cut', 'survey').value = '';
  }

  function resetForm(mode) {
    activeMode = mode;
    ({ form, modal } = modalConfigs[mode]);
    errorBox = $('[data-vendor-form-error]', form);
    form.reset();
    $$('input,select', form).forEach((control) => { control.disabled = false; });
    field('record_id').value = '';
    if (field('survey')) field('survey').value = '';
    if (field('is_active')) field('is_active').checked = true;
    state.selectedSurvey = null;
    if (mode === 'client') setClientSelection([], false);
    if (mode === 'client') {
      const summary = $('#clientAllocationSummary');
      summary.hidden = true;
      summary.innerHTML = '';
    }
    if (mode === 'api_key') renderApiAllocationChoices();
    errorBox.hidden = true;
    const results = $('#surveySearchResults');
    if (results) results.hidden = true;
    const issuedPanel = $('#issuedKeyPanel');
    if (issuedPanel) issuedPanel.hidden = true;
    const issuedValue = $('#issuedKeyValue');
    if (issuedValue) issuedValue.value = '';
    const submit = $('[data-vendor-submit]', form);
    submit.hidden = false;
    submit.disabled = false;
  }

  function showModal() {
    backdrop.hidden = false; modal.hidden = false;
    requestAnimationFrame(() => { backdrop.classList.add('open'); modal.classList.add('open'); });
    document.body.classList.add('vendor-modal-open');
    setTimeout(() => form.querySelector('input:not([type=hidden]):not([disabled]),select:not([disabled])')?.focus(), 140);
  }

  function closeModal() {
    const closingModal = modal;
    backdrop.classList.remove('open'); closingModal?.classList.remove('open');
    document.body.classList.remove('vendor-modal-open');
    setTimeout(() => { backdrop.hidden = true; if (closingModal) closingModal.hidden = true; }, 210);
  }

  function openPolicy(vendorId) {
    resetForm('policy');
    const vendor = state.vendors.find((item) => Number(item.id) === Number(vendorId));
    const profile = state.profiles.find((item) => Number(item.vendor) === Number(vendorId));
    field('record_id').value = profile?.id || '';
    field('policy_vendor').value = String(vendorId);
    field('policy_vendor').disabled = Boolean(profile);
    field('default_cpi_cut_percent').value = profile?.default_cpi_cut_percent || '0.00';
    field('currency').value = profile?.currency || 'USD';
    field('delivery_mode').value = profile?.delivery_mode || vendor?.delivery_mode || 'panel';
    field('is_active').checked = profile?.is_active ?? true;
    $('[data-modal-eyebrow]', modal).textContent = accountLabel(vendor?.account_type);
    $('[data-modal-title]', modal).textContent = profile ? 'Edit commercial policy' : 'Create commercial policy';
    $('[data-vendor-submit]', form).textContent = profile ? 'Save policy' : 'Create policy';
    updatePolicyRule(); showModal();
  }

  function openApiKey() {
    resetForm('api_key');
    $('[data-vendor-submit]', form).textContent = 'Generate secure key';
    showModal();
  }

  function openClientAllocation(recordId = null) {
    resetForm('client');
    const record = state.clientAllocations.find((item) => Number(item.id) === Number(recordId));
    if (record) {
      field('record_id').value = record.id;
      field('client_vendor').value = record.vendor;
      setClientSelection([record.client], true);
      field('client_vendor').disabled = true;
      field('client_quantity_limit').value = record.quantity_limit;
      field('client_cpi_cut').value = record.cpi_cut_override_percent ?? '';
      field('client_starts_at').value = toInputDateTime(record.starts_at);
      field('client_ends_at').value = toInputDateTime(record.ends_at);
      field('is_active').checked = record.is_active;
      const apiScopes = (record.api_key_scopes || []).filter((item) => item.is_active);
      const summary = $('#clientAllocationSummary');
      summary.innerHTML = `
        <article><span>Supplier</span><strong>${escapeHtml(record.vendor_name)}</strong></article>
        <article><span>Client</span><strong>${escapeHtml(record.client_name)}</strong></article>
        <article><span>Effective CPI cut</span><strong>${escapeHtml(record.effective_cpi_cut_percent)}%</strong></article>
        <article><span>API access</span><strong>${escapeHtml(apiScopes.map((item) => item.name).join(', ') || 'No API key')}</strong></article>`;
      summary.hidden = false;
    }
    $('[data-modal-title]', modal).textContent = record ? 'Client allocation details' : 'Allocate a client';
    $('[data-vendor-submit]', form).textContent = record ? 'Update allocation' : 'Create allocation';
    updateClientRule(); showModal();
  }

  function openSurveyAllocation(recordId = null) {
    resetForm('survey');
    const record = state.surveyAllocations.find((item) => Number(item.id) === Number(recordId));
    if (record) {
      field('record_id').value = record.id;
      field('client_allocation').value = record.client_allocation;
      field('client_allocation').disabled = true;
      field('survey').value = record.survey;
      field('survey_search').value = `${record.survey_local_id} · #${record.survey_source_id} · ${record.survey_name || 'Survey'}`;
      field('survey_search').disabled = true;
      state.selectedSurvey = { id: record.survey };
      field('survey_quantity_limit').value = record.quantity_limit;
      field('survey_cpi_cut').value = record.cpi_cut_override_percent ?? '';
      field('survey_starts_at').value = toInputDateTime(record.starts_at);
      field('survey_ends_at').value = toInputDateTime(record.ends_at);
      field('is_active').checked = record.is_active;
    }
    $('[data-modal-title]', modal).textContent = record ? 'Edit project allocation' : 'Allocate a project';
    $('[data-vendor-submit]', form).textContent = record ? 'Save project allocation' : 'Create project allocation';
    updateSurveyRule(); showModal();
  }

  function surveyResultMarkup(survey) {
    return `<button type="button" data-select-survey="${survey.id}"><span><strong>${escapeHtml(survey.local_id)} · #${escapeHtml(survey.source_id)}</strong><small>${escapeHtml(survey.name || 'Survey')} · ${escapeHtml(survey.country_label || '')}</small></span><b>${escapeHtml(survey.cpi ?? '—')}</b></button>`;
  }

  async function searchSurveys() {
    const query = field('survey_search').value.trim();
    const parent = state.clientAllocations.find((item) => String(item.id) === field('client_allocation').value);
    const results = $('#surveySearchResults');
    if (!parent || query.length < 2) { results.hidden = true; return; }
    try {
      const data = await api(`/api/v1/surveys/?page_size=10&client=${encodeURIComponent(parent.client)}&search=${encodeURIComponent(query)}`);
      const surveys = data.results || data;
      results.innerHTML = surveys.length ? surveys.map(surveyResultMarkup).join('') : '<div class="vendor-empty">No matching survey</div>';
      results.hidden = false;
    } catch (error) { toast(error.message, true); }
  }

  async function reloadData() {
    const needsOptions = canViewVendors || canViewClientAllocations || canViewProjectAllocations || canViewApiKeys ||
      canEditPolicy || canAllocateClient || canAllocateProject || canCreateApiKey || canRevokeApiKey;
    const [options, vendors, profiles, clientAllocations, surveyAllocations, apiKeys] = await Promise.all([
      needsOptions ? api('/api/v1/vendors/management-options/') : Promise.resolve({ vendors: [], clients: [] }),
      canViewVendors ? fetchAll('/api/v1/vendors/directory/') : Promise.resolve(null),
      canViewVendors ? fetchAll('/api/v1/vendors/commercial-profiles/') : Promise.resolve([]),
      (canViewClientAllocations || canCreateApiKey) ? fetchAll('/api/v1/vendors/client-allocations/') : Promise.resolve([]),
      canViewProjectAllocations ? fetchAll('/api/v1/vendors/survey-allocations/') : Promise.resolve([]),
      canViewApiKeys ? fetchAll('/api/v1/vendors/api-keys/') : Promise.resolve([]),
    ]);
    Object.assign(state, {
      vendors: vendors || options.vendors || [], profiles, clients: options.clients || [],
      clientAllocations, surveyAllocations, apiKeys,
    });
    hydrateSelects(); render();
  }

  $$('.vendor-tabs [data-vendor-tab]').forEach((button) => button.addEventListener('click', () => {
    $$('.vendor-tabs [data-vendor-tab]').forEach((item) => item.classList.toggle('active', item === button));
    $$('.vendor-tabs [data-vendor-tab]').forEach((item) => item.setAttribute('aria-selected', String(item === button)));
    $$('[data-vendor-panel]').forEach((panel) => {
      const active = panel.dataset.vendorPanel === button.dataset.vendorTab;
      panel.hidden = !active; panel.classList.toggle('active', active);
    });
  }));

  workspace.addEventListener('click', (event) => {
    const createClient = event.target.closest('button[data-create-allocation="client"]');
    const createSurvey = event.target.closest('button[data-create-allocation="survey"]');
    const createApiKey = event.target.closest('button[data-create-api-key]');
    if (createClient && canAllocateClient) {
      event.preventDefault(); event.stopPropagation(); openClientAllocation(); return;
    }
    if (createSurvey && canAllocateProject) {
      event.preventDefault(); event.stopPropagation(); openSurveyAllocation(); return;
    }
    if (createApiKey && canCreateApiKey) {
      event.preventDefault(); event.stopPropagation(); openApiKey(); return;
    }
    const policy = event.target.closest('button[data-edit-policy]');
    const client = event.target.closest('[data-edit-client]');
    const survey = event.target.closest('[data-edit-survey]');
    if (policy && canEditPolicy) { event.preventDefault(); openPolicy(policy.dataset.editPolicy); return; }
    if (client && canAllocateClient) { event.preventDefault(); openClientAllocation(client.dataset.editClient); return; }
    if (survey && canAllocateProject) { event.preventDefault(); openSurveyAllocation(survey.dataset.editSurvey); return; }
    const revokeKey = event.target.closest('button[data-revoke-api-key]');
    if (revokeKey && canRevokeApiKey && confirm('Revoke this API key permanently?')) {
      api(`/api/v1/vendors/api-keys/${revokeKey.dataset.revokeApiKey}/`, { method: 'DELETE' })
        .then(() => { toast('API key revoked.'); return reloadData(); })
        .catch((error) => toast(error.message, true));
    }
  }, true);
  $$('[data-close-vendor-modal]').forEach((button) => button.addEventListener('click', closeModal));
  backdrop.addEventListener('click', closeModal);
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && modal && !modal.hidden) closeModal(); });
  field('policy_vendor', 'policy').addEventListener('change', updatePolicyRule);
  field('client_vendor', 'client').addEventListener('change', updateClientRule);
  field('api_vendor', 'api_key').addEventListener('change', renderApiAllocationChoices);
  field('client_allocation', 'survey').addEventListener('change', () => {
    field('survey', 'survey').value = ''; field('survey_search', 'survey').value = ''; state.selectedSurvey = null;
    updateSurveyRule();
  });
  field('survey_search', 'survey').addEventListener('input', () => {
    field('survey', 'survey').value = ''; state.selectedSurvey = null;
    clearTimeout(state.searchTimer); state.searchTimer = setTimeout(searchSurveys, 260);
  });
  $('#surveySearchResults').addEventListener('click', (event) => {
    const button = event.target.closest('[data-select-survey]');
    if (!button) return;
    const label = button.querySelector('strong').textContent;
    const subtitle = button.querySelector('small').textContent.split(' · ')[0];
    field('survey').value = button.dataset.selectSurvey;
    field('survey_search').value = `${label} · ${subtitle}`;
    state.selectedSurvey = { id: Number(button.dataset.selectSurvey) };
    $('#surveySearchResults').hidden = true;
  });
  clientPicker.querySelector('.vendor-client-picker-trigger').addEventListener('click', () => {
    const open = !clientPicker.classList.contains('open');
    clientPicker.classList.toggle('open', open);
    clientPicker.querySelector('.vendor-client-picker-menu').hidden = !open;
    clientPicker.querySelector('.vendor-client-picker-trigger').setAttribute('aria-expanded', String(open));
    if (open) clientPicker.querySelector('input[type="search"]').focus();
  });
  clientPicker.querySelector('input[type="search"]').addEventListener('input', (event) => renderClientPickerOptions(event.target.value));
  clientPicker.querySelector('.vendor-client-picker-options').addEventListener('change', (event) => {
    if (!event.target.matches('input[type="checkbox"]')) return;
    const target = [...field('client', 'client').options].find((item) => item.value === event.target.value);
    if (target) target.selected = event.target.checked;
    updateClientPickerLabel();
  });
  document.addEventListener('click', (event) => {
    if (clientPicker.contains(event.target)) return;
    clientPicker.classList.remove('open');
    clientPicker.querySelector('.vendor-client-picker-menu').hidden = true;
    clientPicker.querySelector('.vendor-client-picker-trigger').setAttribute('aria-expanded', 'false');
  });
  $('#copyIssuedKey').addEventListener('click', async () => {
    await navigator.clipboard.writeText($('#issuedKeyValue').value);
    toast('API key copied. Store it securely.');
  });

  async function submitVendorForm(event) {
    event.preventDefault();
    activeMode = event.currentTarget.dataset.vendorForm;
    ({ form, modal } = modalConfigs[activeMode]);
    errorBox = $('[data-vendor-form-error]', form);
    errorBox.hidden = true;
    const mode = activeMode;
    const id = field('record_id').value;
    let url; let payload;
    if (mode === 'policy') {
      url = `/api/v1/vendors/commercial-profiles/${id ? `${id}/` : ''}`;
      payload = {
        vendor: Number(field('policy_vendor').value),
        default_cpi_cut_percent: field('default_cpi_cut_percent').disabled ? '0.00' : field('default_cpi_cut_percent').value,
        currency: field('currency').value, delivery_mode: field('delivery_mode').value,
        is_active: field('is_active').checked,
      };
    } else if (mode === 'client') {
      url = `/api/v1/vendors/client-allocations/${id ? `${id}/` : ''}`;
      const clientIds = selectedClientIds();
      if (!clientIds.length) { errorBox.textContent = 'Select at least one client.'; errorBox.hidden = false; return; }
      payload = {
        vendor: Number(field('client_vendor').value), client: clientIds[0],
        quantity_limit: Number(field('client_quantity_limit').value),
        cpi_cut_override_percent: field('client_cpi_cut').disabled ? null : nullableNumber(field('client_cpi_cut').value),
        starts_at: toApiDateTime(field('client_starts_at').value), ends_at: toApiDateTime(field('client_ends_at').value),
        is_active: field('is_active').checked,
      };
    } else if (mode === 'survey') {
      if (!field('survey').value) { errorBox.textContent = 'Select a survey from the search results.'; errorBox.hidden = false; return; }
      url = `/api/v1/vendors/survey-allocations/${id ? `${id}/` : ''}`;
      payload = {
        client_allocation: Number(field('client_allocation').value), survey: Number(field('survey').value),
        quantity_limit: Number(field('survey_quantity_limit').value),
        cpi_cut_override_percent: field('survey_cpi_cut').disabled ? null : nullableNumber(field('survey_cpi_cut').value),
        starts_at: toApiDateTime(field('survey_starts_at').value), ends_at: toApiDateTime(field('survey_ends_at').value),
        is_active: field('is_active').checked,
      };
    } else {
      url = '/api/v1/vendors/api-keys/';
      const allocationIds = selectedApiAllocationIds();
      if (!allocationIds.length) {
        errorBox.textContent = 'Select at least one client for this API key.'; errorBox.hidden = false; return;
      }
      payload = {
        vendor: Number(field('api_vendor').value), name: field('api_key_name').value.trim(),
        client_allocations: allocationIds,
        expires_at: toApiDateTime(field('api_key_expires_at').value),
      };
    }
    try {
      const submit = $('[data-vendor-submit]', form);
      submit.disabled = true;
      let result;
      if (mode === 'client' && !id) {
        for (const clientId of selectedClientIds()) {
          result = await api(url, { method: 'POST', body: JSON.stringify({ ...payload, client: clientId }) });
        }
      } else result = await api(url, { method: id ? 'PATCH' : 'POST', body: JSON.stringify(payload) });
      if (mode === 'api_key') {
        $('#issuedKeyValue').value = result.api_key;
        $('#issuedKeyPanel').hidden = false;
        $$('input,select', form).forEach((control) => { control.disabled = true; });
        submit.hidden = true;
        toast('API key generated. Copy it now.');
        await reloadData();
      } else {
        const count = mode === 'client' && !id ? selectedClientIds().length : 1;
        closeModal(); toast(id ? 'Changes saved.' : (count > 1 ? `${count} client allocations created.` : 'Configuration created.')); await reloadData();
      }
    } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
    finally { const submit = $('[data-vendor-submit]', form); if (submit) submit.disabled = false; }
  }
  Object.values(modalConfigs).forEach((config) => config.form.addEventListener('submit', submitVendorForm));

  reloadData().catch((error) => {
    ['vendorRows', 'clientAllocationRows', 'surveyAllocationRows'].forEach((id) => {
      const node = document.getElementById(id); if (node) node.innerHTML = emptyRow(6, error.message);
    });
    toast(error.message, true);
  });
})();
