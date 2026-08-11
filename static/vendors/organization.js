(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const workspace = $('.organization-workspace');
  if (!workspace) return;
  const csrf = $('.csrf-source input')?.value || '';
  const canManageUnits = workspace.dataset.manageUnits === 'true';
  const canCreateUnits = workspace.dataset.createUnits === 'true';
  const canEditUnits = workspace.dataset.editUnits === 'true';
  const canDeleteUnits = workspace.dataset.deleteUnits === 'true';
  const canManageUnitClients = workspace.dataset.manageUnitClients === 'true';
  const canRemoveUnitClients = workspace.dataset.removeUnitClients === 'true';
  const canViewClients = workspace.dataset.viewClients === 'true';
  const canManageClients = workspace.dataset.manageClients === 'true';
  const canViewIntegrations = workspace.dataset.viewIntegrations === 'true';
  const canManageIntegrations = workspace.dataset.manageIntegrations === 'true';
  const canTestIntegrations = workspace.dataset.testIntegrations === 'true';
  const canPreviewIntegrations = workspace.dataset.previewIntegrations === 'true';
  const canSyncIntegrations = workspace.dataset.syncIntegrations === 'true';
  const state = { options: { owners: [], clients: [], client_eligibility: {} }, units: [], access: [], clients: [], providers: [] };
  const edit = { unit: null, access: null, client: null, integration: null, integrationClient: null };
  const unitColumns = new Set(JSON.parse($('#organizationUnitColumnAccess')?.textContent || '[]'));
  const accessColumns = new Set(JSON.parse($('#organizationClientColumnAccess')?.textContent || '[]'));

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
  const slug = (value) => value.toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  const label = (type) => ({ branch: 'Branch', sub_branch: 'Sub-branch', shift: 'Shift' }[type] || type);
  const ownerLabel = (owner) => `${owner.name}${owner.type === 'internal_vendor' ? ' · Internal vendor' : ' · Main office'}`;
  const option = (value, text, selected = false) => `<option value="${escapeHtml(value)}"${selected ? ' selected' : ''}>${escapeHtml(text)}</option>`;

  function toast(message, error = false) {
    const node = $('[data-organization-toast]');
    node.textContent = message; node.classList.toggle('error', error); node.classList.add('show');
    setTimeout(() => node.classList.remove('show'), 3200);
  }

  async function api(url, options = {}) {
    const response = await fetch(url, { credentials: 'same-origin', ...options, headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf, ...(options.headers || {}) } });
    const data = response.status === 204 ? null : await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = Object.entries(data || {}).map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(', ') : value}`).join(' · ') || 'Request could not be completed.';
      throw new Error(message);
    }
    return data;
  }

  async function fetchAll(url) {
    let next = url; const rows = [];
    while (next) { const data = await api(next); rows.push(...(Array.isArray(data) ? data : data.results || [])); next = Array.isArray(data) ? null : data.next; }
    return rows;
  }

  function stateBadge(active) { return `<span class="unit-state${active ? '' : ' inactive'}">${active ? 'Active' : 'Inactive'}</span>`; }
  function actionButton(type, id, allowed) { return allowed ? `<button class="unit-action" type="button" data-edit-${type}="${id}">Edit</button>` : ''; }

  function unitActions(unit) {
    if (!unitColumns.has('actions')) return '';
    return `<div class="unit-actions">${actionButton('unit', unit.id, canEditUnits)}${canDeleteUnits ? `<button class="unit-action danger" type="button" data-delete-unit="${unit.id}">Delete</button>` : ''}</div>`;
  }

  function unitLine(unit) {
    const identity = unitColumns.has('path') || unitColumns.has('type') ? `<div class="unit-identity">${unitColumns.has('type') ? `<span class="unit-level ${unit.unit_type}">${unit.unit_type === 'sub_branch' ? 'SB' : unit.unit_type === 'shift' ? 'SH' : 'BR'}</span>` : ''}${unitColumns.has('path') ? `<div><strong>${escapeHtml(unit.name)}</strong><small>${escapeHtml(unit.path)} · ${escapeHtml(unit.code)}</small></div>` : ''}</div>` : '<span></span>';
    return `<div class="unit-line">${identity}${unitColumns.has('members') ? `<span class="unit-stat"><span>Members</span><b>${Number(unit.member_count || 0)}</b></span>` : ''}${unitColumns.has('clients') ? `<span class="unit-stat clients"><span>Clients</span><b>${Number(unit.client_count || 0)}</b></span>` : ''}${unitColumns.has('status') ? stateBadge(unit.is_active) : ''}${unitActions(unit)}</div>`;
  }

  function unitNode(unit, byParent) {
    const children = byParent.get(unit.id) || [];
    return `<div class="${unit.unit_type === 'branch' ? 'unit-branch' : 'unit-node'}">${unitLine(unit)}${children.length ? `<div class="unit-children">${children.map((child) => unitNode(child, byParent)).join('')}</div>` : ''}</div>`;
  }

  function renderStructure() {
    const root = $('#organizationTree');
    const owners = new Map(state.options.owners.map((owner) => [Number(owner.id), owner]));
    const grouped = new Map();
    state.units.forEach((unit) => { const id = Number(unit.workspace_owner); if (!grouped.has(id)) grouped.set(id, []); grouped.get(id).push(unit); });
    root.innerHTML = [...grouped.entries()].map(([ownerId, units]) => {
      const byParent = new Map();
      units.forEach((unit) => { const key = unit.parent ? Number(unit.parent) : null; if (!byParent.has(key)) byParent.set(key, []); byParent.get(key).push(unit); });
      const owner = owners.get(ownerId) || { name: `Workspace ${ownerId}`, type: 'owner' };
      return `<section class="owner-tree"><header><h3>${escapeHtml(owner.name)}</h3><span>${owner.type === 'internal_vendor' ? 'Internal vendor' : 'Main office'}</span></header>${(byParent.get(null) || []).map((unit) => unitNode(unit, byParent)).join('') || '<div class="organization-empty">No branches in this workspace yet.</div>'}</section>`;
    }).join('') || '<div class="organization-empty">Create the first Branch to start the hierarchy.</div>';
  }

  function renderAccess() {
    const node = $('#organizationClientRows'); if (!node) return;
    node.innerHTML = state.access.map((row) => `<article class="access-card"><header><div>${accessColumns.has('client') ? `<h3>${escapeHtml(row.client_name)}</h3>` : ''}${accessColumns.has('unit') ? `<p>${escapeHtml(row.unit_path)}</p>` : ''}</div>${accessColumns.has('status') ? stateBadge(row.is_active) : ''}</header>${accessColumns.has('unit') || accessColumns.has('actions') ? `<footer>${accessColumns.has('unit') ? `<span class="unit-level ${row.unit_type}">${row.unit_type === 'sub_branch' ? 'SB' : row.unit_type === 'shift' ? 'SH' : 'BR'}</span>` : ''}${accessColumns.has('actions') ? `<div class="unit-actions">${actionButton('client-access', row.id, canManageUnitClients)}${canRemoveUnitClients ? `<button class="unit-action danger" type="button" data-remove-client-access="${row.id}">Remove access</button>` : ''}</div>` : ''}</footer>` : ''}</article>`).join('') || '<div class="organization-empty">No client visibility rules yet.</div>';
  }

  function renderClients() {
    const node = $('#organizationClients'); if (!node) return;
    node.innerHTML = state.clients.map((client) => { const integrations = canViewIntegrations ? (client.integrations || []).filter((item) => item.provider_code === 'rfg').map((item) => { const connectionStatus = item.last_test_status || 'draft'; return `<button class="integration-chip ${escapeHtml(connectionStatus)}" type="button" data-edit-integration="${item.id}" data-client-id="${client.id}"><span>${escapeHtml(item.name)}</span><small>${escapeHtml(connectionStatus)}</small></button>`; }).join('') : ''; return `<article class="client-card"><header><div><h3>${escapeHtml(client.name)}</h3><p>${escapeHtml(client.code)} · ${escapeHtml(client.provider_code)}</p></div>${stateBadge(client.is_active)}</header>${canViewIntegrations ? `<div class="client-integrations">${integrations || '<small>No RFG integration configured.</small>'}</div>` : ''}<footer><small>${escapeHtml(client.company_name_match || 'No company match')}</small><div class="client-card-actions">${canManageIntegrations ? `<button class="unit-action" type="button" data-add-integration="${client.id}">Add RFG integration</button>` : ''}${actionButton('client', client.id, canManageClients)}</div></footer></article>`; }).join('') || '<div class="organization-empty">No clients yet.</div>';
  }

  function renderSummary() {
    if ($('#branchCount')) $('#branchCount').textContent = state.units.filter((unit) => unit.unit_type === 'branch' && unit.is_active).length;
    if ($('#shiftCount')) $('#shiftCount').textContent = state.units.filter((unit) => unit.unit_type === 'shift' && unit.is_active).length;
    if ($('#memberCount')) $('#memberCount').textContent = state.units.reduce((sum, unit) => sum + Number(unit.direct_member_count || 0), 0);
    if ($('#unitClientCount')) $('#unitClientCount').textContent = state.access.filter((row) => row.is_active).length;
  }

  const backdrop = $('[data-organization-backdrop]');
  function openModal(modal) { backdrop.hidden = false; modal.hidden = false; requestAnimationFrame(() => { backdrop.classList.add('open'); modal.classList.add('open'); }); document.body.classList.add('organization-modal-open'); setTimeout(() => $('input,select', modal)?.focus(), 120); }
  function closeModals() { backdrop.classList.remove('open'); $$('.organization-modal.open').forEach((modal) => modal.classList.remove('open')); document.body.classList.remove('organization-modal-open'); setTimeout(() => { backdrop.hidden = true; $$('.organization-modal').forEach((modal) => { modal.hidden = true; }); }, 180); }

  const unitForm = $('#unitForm');
  function refreshParentOptions() {
    const ownerId = Number(unitForm.elements.workspace_owner.value || 0); const type = unitForm.elements.unit_type.value;
    const requiredType = type === 'shift' ? 'sub_branch' : type === 'sub_branch' ? 'branch' : null;
    $('[data-parent-field]').hidden = !requiredType;
    unitForm.elements.parent.required = Boolean(requiredType);
    const candidates = state.units.filter((unit) => Number(unit.workspace_owner) === ownerId && unit.unit_type === requiredType && unit.is_active && Number(unit.id) !== Number(edit.unit || 0));
    unitForm.elements.parent.innerHTML = `<option value="">${requiredType ? `Select ${label(requiredType)}` : 'No parent'}</option>${candidates.map((unit) => option(unit.id, unit.path)).join('')}`;
  }

  function openUnit(id = null) {
    edit.unit = id ? Number(id) : null; unitForm.reset(); unitForm.elements.is_active.checked = true;
    unitForm.elements.workspace_owner.innerHTML = state.options.owners.map((owner) => option(owner.id, ownerLabel(owner))).join('');
    const record = state.units.find((unit) => Number(unit.id) === edit.unit);
    if (record) { unitForm.elements.workspace_owner.value = record.workspace_owner; unitForm.elements.workspace_owner.disabled = true; unitForm.elements.unit_type.value = record.unit_type; unitForm.elements.name.value = record.name; unitForm.elements.code.value = record.code; unitForm.elements.description.value = record.description || ''; unitForm.elements.is_active.checked = record.is_active; }
    else unitForm.elements.workspace_owner.disabled = false;
    refreshParentOptions(); if (record?.parent) unitForm.elements.parent.value = record.parent;
    const deleteButton = $('[data-delete-current-unit]'); if (deleteButton) deleteButton.hidden = !record || !canDeleteUnits;
    $('#unitModalTitle').textContent = record ? 'Edit organization unit' : 'Create organization unit'; $('[data-unit-submit]').textContent = record ? 'Save changes' : 'Create unit'; $('[data-unit-error]').hidden = true; openModal($('#unitModal'));
  }

  const accessForm = $('#clientAccessForm');
  function refreshClientOptions() {
    const unit = state.units.find((item) => Number(item.id) === Number(accessForm.elements.organization_unit.value));
    const eligible = new Set(state.options.client_eligibility[String(unit?.workspace_owner)] || []);
    accessForm.elements.client.innerHTML = '<option value="">Select client</option>' + state.options.clients.filter((client) => eligible.has(Number(client.id))).map((client) => option(client.id, `${client.name} · ${client.code}`)).join('');
  }
  function openClientAccess(id = null) {
    edit.access = id ? Number(id) : null; accessForm.reset(); accessForm.elements.is_active.checked = true;
    accessForm.elements.organization_unit.innerHTML = '<option value="">Select organization unit</option>' + state.units.filter((unit) => unit.is_active).map((unit) => option(unit.id, `${unit.workspace_owner_name} · ${unit.path}`)).join('');
    const record = state.access.find((row) => Number(row.id) === edit.access);
    if (record) { accessForm.elements.organization_unit.value = record.organization_unit; accessForm.elements.organization_unit.disabled = true; refreshClientOptions(); accessForm.elements.client.value = record.client; accessForm.elements.client.disabled = true; accessForm.elements.is_active.checked = record.is_active; }
    else { accessForm.elements.organization_unit.disabled = false; accessForm.elements.client.disabled = false; refreshClientOptions(); }
    const removeButton = $('[data-remove-current-client-access]'); if (removeButton) removeButton.hidden = !record || !canRemoveUnitClients;
    $('#clientAccessModalTitle').textContent = record ? 'Edit client visibility' : 'Assign client'; $('[data-client-access-submit]').textContent = record ? 'Save changes' : 'Assign client'; $('[data-client-access-error]').hidden = true; openModal($('#clientAccessModal'));
  }

  const clientForm = $('#clientForm');
  function openClient(id = null) {
    if (!clientForm) return; edit.client = id ? Number(id) : null; clientForm.reset(); clientForm.elements.provider_code.value = 'innovatemr'; clientForm.elements.is_active.checked = true;
    const record = state.clients.find((client) => Number(client.id) === edit.client);
    if (record) { ['name', 'code', 'provider_code', 'company_name_match'].forEach((field) => { clientForm.elements[field].value = record[field] || ''; }); clientForm.elements.is_active.checked = record.is_active; }
    $('#clientModalTitle').textContent = record ? 'Edit client' : 'Add client'; $('[data-client-submit]').textContent = record ? 'Save changes' : 'Create client'; $('[data-client-error]').hidden = true; openModal($('#clientModal'));
  }

  const integrationForm = $('#integrationForm');
  function allIntegrations() { return state.clients.flatMap((client) => (client.integrations || []).map((item) => ({ ...item, clientRecord: client }))); }
  function populateProviders(selected = 'rfg') {
    integrationForm.elements.provider_code.innerHTML = state.providers.map((provider) => option(provider.code, provider.label, provider.code === selected)).join('');
    const provider = state.providers.find((item) => item.code === integrationForm.elements.provider_code.value) || state.providers[0];
    if (provider && !integrationForm.elements.base_url.value) integrationForm.elements.base_url.value = provider.default_base_url;
    if (provider) integrationForm.elements.sync_interval_seconds.min = provider.minimum_sync_interval_seconds;
  }
  function openIntegration(clientId, integrationId = null) {
    if (!integrationForm) return;
    edit.integrationClient = Number(clientId); edit.integration = integrationId ? Number(integrationId) : null; integrationForm.reset(); integrationForm.elements.is_active.checked = true; integrationForm.elements.enforce_local_targeting.checked = true; integrationForm.elements.sync_interval_seconds.value = 60;
    const client = state.clients.find((item) => Number(item.id) === edit.integrationClient); const record = allIntegrations().find((item) => Number(item.id) === edit.integration);
    $('[data-integration-client]').textContent = client?.name || 'Unknown client'; populateProviders(record?.provider_code || 'rfg');
    if (record) { integrationForm.elements.name.value = record.name || ''; integrationForm.elements.provider_code.value = record.provider_code; integrationForm.elements.base_url.value = record.base_url || ''; integrationForm.elements.apid_env.value = record.credential_env_keys?.apid || ''; integrationForm.elements.secret_env.value = record.credential_env_keys?.secret || ''; integrationForm.elements.sync_interval_seconds.value = record.sync_interval_seconds || 600; integrationForm.elements.country.value = record.config?.country || ''; integrationForm.elements.is_active.checked = record.is_active; integrationForm.elements.enforce_local_targeting.checked = record.config?.enforce_local_targeting !== false; integrationForm.elements.scheduled_sync_enabled.checked = record.scheduled_sync_enabled; integrationForm.elements.scheduled_sync_enabled.disabled = record.last_test_status !== 'success'; }
    else { integrationForm.elements.apid_env.value = 'RFG_APID'; integrationForm.elements.secret_env.value = 'RFG_SECRET'; integrationForm.elements.scheduled_sync_enabled.disabled = true; }
    $$('input,select', integrationForm).forEach((field) => { if (!canManageIntegrations) field.disabled = true; });
    $('[data-test-integration]').hidden = !canTestIntegrations; $('[data-preview-integration]').hidden = !canPreviewIntegrations; $('[data-sync-integration]').hidden = !canSyncIntegrations;
    const connectionStatus = record?.last_test_status || 'draft'; const statusNode = $('[data-integration-status]'); statusNode.textContent = connectionStatus; statusNode.className = connectionStatus; $('[data-integration-actions]').hidden = !record || !(canTestIntegrations || canPreviewIntegrations || canSyncIntegrations); $('[data-integration-preview]').hidden = true; $('[data-integration-error]').hidden = true; $('#integrationModalTitle').textContent = record ? 'Manage integration' : 'Add integration'; if ($('[data-integration-submit]')) $('[data-integration-submit]').textContent = record ? 'Save settings' : 'Create integration'; openModal($('#integrationModal'));
  }

  async function reload() {
    const requests = [api('/api/v1/vendors/organization-options/'), fetchAll('/api/v1/vendors/organization-units/'), fetchAll('/api/v1/vendors/organization-client-access/')];
    if (canViewClients || canManageClients || canViewIntegrations) requests.push(fetchAll('/api/v1/vendors/clients/'));
    if (canViewIntegrations) requests.push(api('/api/v1/vendors/integrations/providers/'));
    const [options, units, access, clients = [], providers = []] = await Promise.all(requests);
    Object.assign(state, { options, units, access, clients, providers }); renderSummary(); renderStructure(); renderAccess(); renderClients();
  }

  $$('[data-organization-tab]').forEach((button) => button.addEventListener('click', () => { $$('[data-organization-tab]').forEach((item) => item.classList.toggle('active', item === button)); $$('[data-organization-panel]').forEach((panel) => { const active = panel.dataset.organizationPanel === button.dataset.organizationTab; panel.hidden = !active; panel.classList.toggle('active', active); }); }));
  if (canCreateUnits) $$('[data-create-unit]').forEach((button) => button.addEventListener('click', () => openUnit()));
  $('[data-create-client-access]')?.addEventListener('click', () => openClientAccess()); $('[data-create-client]')?.addEventListener('click', () => openClient());
  unitForm?.elements.workspace_owner.addEventListener('change', refreshParentOptions); unitForm?.elements.unit_type.addEventListener('change', refreshParentOptions); accessForm?.elements.organization_unit.addEventListener('change', refreshClientOptions);
  unitForm?.elements.name.addEventListener('input', () => { if (!edit.unit) unitForm.elements.code.value = slug(unitForm.elements.name.value); });
  async function deleteUnit(id) {
    const record = state.units.find((unit) => Number(unit.id) === Number(id));
    if (!record || !canDeleteUnits || !confirm(`Delete ${record.name} permanently?`)) return;
    try { await api(`/api/v1/vendors/organization-units/${record.id}/`, { method: 'DELETE' }); closeModals(); toast('Organization unit deleted.'); await reload(); }
    catch (error) { const box = $('[data-unit-error]'); if (!$('#unitModal').hidden) { box.textContent = error.message; box.hidden = false; } else toast(error.message, true); }
  }
  async function removeClientAccess(id) {
    const record = state.access.find((row) => Number(row.id) === Number(id));
    if (!record || !canRemoveUnitClients || !confirm(`Remove ${record.client_name} access from ${record.unit_path}?`)) return;
    try { await api(`/api/v1/vendors/organization-client-access/${record.id}/`, { method: 'DELETE' }); closeModals(); toast('Client access removed.'); await reload(); }
    catch (error) { const box = $('[data-client-access-error]'); if (!$('#clientAccessModal').hidden) { box.textContent = error.message; box.hidden = false; } else toast(error.message, true); }
  }
  document.addEventListener('click', (event) => { const removeAccess = event.target.closest('[data-remove-client-access]'); if (removeAccess) { removeClientAccess(removeAccess.dataset.removeClientAccess); return; } const deleteButton = event.target.closest('[data-delete-unit]'); if (deleteButton) { deleteUnit(deleteButton.dataset.deleteUnit); return; } const unit = event.target.closest('[data-edit-unit]'); const access = event.target.closest('[data-edit-client-access]'); const client = event.target.closest('[data-edit-client]'); const addIntegration = event.target.closest('[data-add-integration]'); const integration = event.target.closest('[data-edit-integration]'); if (unit) { openUnit(unit.dataset.editUnit); return; } if (access) { openClientAccess(access.dataset.editClientAccess); return; } if (client) { openClient(client.dataset.editClient); return; } if (addIntegration) { openIntegration(addIntegration.dataset.addIntegration); return; } if (integration) openIntegration(integration.dataset.clientId, integration.dataset.editIntegration); });
  $('[data-delete-current-unit]')?.addEventListener('click', () => deleteUnit(edit.unit));
  $('[data-remove-current-client-access]')?.addEventListener('click', () => removeClientAccess(edit.access));
  unitForm?.addEventListener('submit', async (event) => { event.preventDefault(); const box = $('[data-unit-error]'); box.hidden = true; const payload = { workspace_owner: Number(unitForm.elements.workspace_owner.value), unit_type: unitForm.elements.unit_type.value, parent: unitForm.elements.parent.value ? Number(unitForm.elements.parent.value) : null, name: unitForm.elements.name.value.trim(), code: unitForm.elements.code.value.trim(), description: unitForm.elements.description.value.trim(), is_active: unitForm.elements.is_active.checked }; try { await api(edit.unit ? `/api/v1/vendors/organization-units/${edit.unit}/` : '/api/v1/vendors/organization-units/', { method: edit.unit ? 'PATCH' : 'POST', body: JSON.stringify(payload) }); closeModals(); toast(edit.unit ? 'Organization unit updated.' : 'Organization unit created.'); await reload(); } catch (error) { box.textContent = error.message; box.hidden = false; } });
  accessForm?.addEventListener('submit', async (event) => { event.preventDefault(); const box = $('[data-client-access-error]'); box.hidden = true; const payload = { organization_unit: Number(accessForm.elements.organization_unit.value), client: Number(accessForm.elements.client.value), is_active: accessForm.elements.is_active.checked }; try { await api(edit.access ? `/api/v1/vendors/organization-client-access/${edit.access}/` : '/api/v1/vendors/organization-client-access/', { method: edit.access ? 'PATCH' : 'POST', body: JSON.stringify(payload) }); closeModals(); toast(edit.access ? 'Client visibility updated.' : 'Client assigned to organization.'); await reload(); } catch (error) { box.textContent = error.message; box.hidden = false; } });
  clientForm?.addEventListener('submit', async (event) => { event.preventDefault(); const box = $('[data-client-error]'); box.hidden = true; const payload = { name: clientForm.elements.name.value.trim(), code: clientForm.elements.code.value.trim(), provider_code: clientForm.elements.provider_code.value.trim(), company_name_match: clientForm.elements.company_name_match.value.trim(), is_active: clientForm.elements.is_active.checked }; try { await api(edit.client ? `/api/v1/vendors/clients/${edit.client}/` : '/api/v1/vendors/clients/', { method: edit.client ? 'PATCH' : 'POST', body: JSON.stringify(payload) }); closeModals(); toast(edit.client ? 'Client updated.' : 'Client created.'); await reload(); } catch (error) { box.textContent = error.message; box.hidden = false; } });
  integrationForm?.elements.provider_code.addEventListener('change', () => { integrationForm.elements.base_url.value = ''; populateProviders(integrationForm.elements.provider_code.value); });
  integrationForm?.addEventListener('submit', async (event) => { event.preventDefault(); const box = $('[data-integration-error]'); box.hidden = true; const current = allIntegrations().find((item) => Number(item.id) === Number(edit.integration)); const payload = { client: edit.integrationClient, name: integrationForm.elements.name.value.trim(), provider_code: integrationForm.elements.provider_code.value, base_url: integrationForm.elements.base_url.value.trim(), credential_env_keys: { apid: integrationForm.elements.apid_env.value.trim(), secret: integrationForm.elements.secret_env.value.trim() }, config: { ...(current?.config || {}), country: integrationForm.elements.country.value.trim().toUpperCase(), enforce_local_targeting: integrationForm.elements.enforce_local_targeting.checked, callback_security_mode: 'ip' }, sync_interval_seconds: Number(integrationForm.elements.sync_interval_seconds.value), scheduled_sync_enabled: integrationForm.elements.scheduled_sync_enabled.checked, is_active: integrationForm.elements.is_active.checked }; try { const saved = await api(edit.integration ? `/api/v1/vendors/integrations/${edit.integration}/` : '/api/v1/vendors/integrations/', { method: edit.integration ? 'PATCH' : 'POST', body: JSON.stringify(payload) }); edit.integration = saved.id; toast('Integration saved. Test the connection before scheduling sync.'); await reload(); openIntegration(edit.integrationClient, edit.integration); } catch (error) { box.textContent = error.message; box.hidden = false; } });
  async function integrationAction(path, method = 'POST', reopen = true) { const box = $('[data-integration-error]'); box.hidden = true; try { const data = await api(`/api/v1/vendors/integrations/${edit.integration}/${path}/`, { method }); toast(data.detail || 'Request completed.'); if (reopen) { await reload(); openIntegration(edit.integrationClient, edit.integration); } return data; } catch (error) { box.textContent = error.message; box.hidden = false; return null; } }
  $('[data-test-integration]')?.addEventListener('click', () => integrationAction('test-connection')); $('[data-sync-integration]')?.addEventListener('click', () => integrationAction('sync-now'));
  $('[data-preview-integration]')?.addEventListener('click', async () => { const data = await integrationAction('preview', 'GET', false); if (!data) return; const node = $('[data-integration-preview]'); node.hidden = false; node.innerHTML = `<strong>${Number(data.total_received || 0)} upstream surveys</strong>${(data.results || []).map((row) => `<span><b>${escapeHtml(row.source_id)}</b>${escapeHtml(row.name || 'Untitled')} · ${escapeHtml(row.country || '—')} · CPI ${escapeHtml(row.cpi ?? '—')}</span>`).join('') || '<span>No surveys returned.</span>'}`; });
  $$('[data-close-organization-modal]').forEach((button) => button.addEventListener('click', closeModals)); backdrop?.addEventListener('click', closeModals); document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeModals(); });
  reload().catch((error) => { toast(error.message, true); if ($('#organizationTree')) $('#organizationTree').innerHTML = `<div class="organization-empty">${escapeHtml(error.message)}</div>`; });
})();
