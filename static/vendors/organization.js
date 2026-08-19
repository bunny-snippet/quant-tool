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
  const state = { options: { owners: [], clients: [], client_eligibility: {} }, units: [], access: [], clients: [], providers: [] };
  const edit = { unit: null, access: null, client: null };
  const unitColumns = new Set(JSON.parse($('#organizationUnitColumnAccess')?.textContent || '[]'));
  const accessColumns = new Set(JSON.parse($('#organizationClientColumnAccess')?.textContent || '[]'));

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
  const slug = (value) => value.toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  const label = (type) => ({ branch: 'Branch', sub_branch: 'Sub-branch', shift: 'Shift' }[type] || type);
  const ownerLabel = (owner) => `${owner.name}${owner.type === 'internal_vendor' ? ' · Internal supplier' : ' · Main office'}`;
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
      return `<section class="owner-tree"><header><h3>${escapeHtml(owner.name)}</h3><span>${owner.type === 'internal_vendor' ? 'Internal supplier' : 'Main office'}</span></header>${(byParent.get(null) || []).map((unit) => unitNode(unit, byParent)).join('') || '<div class="organization-empty">No branches in this workspace yet.</div>'}</section>`;
    }).join('') || '<div class="organization-empty">Create the first Branch to start the hierarchy.</div>';
  }

  function renderAccess() {
    const node = $('#organizationClientRows'); if (!node) return;
    const ownerNames = new Map(state.options.owners.map((owner) => [Number(owner.id), owner.name]));
    const unitsById = new Map(state.units.map((unit) => [Number(unit.id), unit]));
    const explicitByUnit = new Map();
    state.access.forEach((row) => { const unitId = Number(row.organization_unit); if (!explicitByUnit.has(unitId)) explicitByUnit.set(unitId, []); explicitByUnit.get(unitId).push(row); });
    const effectiveCache = new Map();
    function effectivePolicies(unit) {
      if (effectiveCache.has(Number(unit.id))) return effectiveCache.get(Number(unit.id));
      const inherited = unit.parent ? new Map(effectivePolicies(unitsById.get(Number(unit.parent)))) : new Map();
      (explicitByUnit.get(Number(unit.id)) || []).forEach((rawRow) => {
        const parentPolicy = inherited.get(Number(rawRow.client)); const row = { ...rawRow }; let cpiSourceUnit = unit;
        if (row.inherit_cpi_range && parentPolicy) { row.min_cpi = parentPolicy.row.min_cpi; row.max_cpi = parentPolicy.row.max_cpi; cpiSourceUnit = parentPolicy.cpiSourceUnit || parentPolicy.sourceUnit; }
        inherited.set(Number(row.client), { row, sourceUnit: unit, cpiSourceUnit });
      });
      effectiveCache.set(Number(unit.id), inherited); return inherited;
    }
    const range = (row) => `${row.min_cpi == null ? 'Any' : `$${row.min_cpi}`} – ${row.max_cpi == null ? 'Any' : `$${row.max_cpi}`}`;
    function policyRows(unit) {
      const directIds = new Set((explicitByUnit.get(Number(unit.id)) || []).map((row) => Number(row.client)));
      const rows = [...effectivePolicies(unit).values()].sort((a, b) => a.row.client_name.localeCompare(b.row.client_name));
      if (!rows.length) return '<div class="access-empty">No client inherited or assigned.</div>';
      return rows.map(({ row, sourceUnit, cpiSourceUnit }) => {
        const direct = directIds.has(Number(row.client)); const inheritedCpi = direct && row.inherit_cpi_range && cpiSourceUnit && Number(cpiSourceUnit.id) !== Number(unit.id); const source = direct ? (inheritedCpi ? `Direct access · CPI inherited from ${cpiSourceUnit.name}` : 'Direct policy') : `Inherited from ${sourceUnit.name}`;
        const editButton = direct ? actionButton('client-access', row.id, canManageUnitClients) : (canManageUnitClients ? `<button class="unit-action" type="button" data-override-client-access="${unit.id}:${row.client}" data-min-cpi="${escapeHtml(row.min_cpi ?? '')}" data-max-cpi="${escapeHtml(row.max_cpi ?? '')}" data-inherit-cpi="true" data-policy-active="${row.is_active ? 'true' : 'false'}">Override</button>` : '');
        return `<div class="access-policy ${row.is_active ? '' : 'blocked'}"><div>${accessColumns.has('client') ? `<strong>${escapeHtml(row.client_name)}</strong>` : ''}${accessColumns.has('unit') ? `<small>${escapeHtml(source)}</small>` : ''}</div>${accessColumns.has('cpi') ? `<span class="access-range">${escapeHtml(range(row))}<small>Source CPI</small></span>` : ''}${accessColumns.has('status') ? stateBadge(row.is_active) : ''}${accessColumns.has('actions') ? `<div class="unit-actions">${editButton}${direct && canRemoveUnitClients ? `<button class="unit-action danger" type="button" data-remove-client-access="${row.id}">Remove</button>` : ''}</div>` : ''}</div>`;
      }).join('');
    }
    const grouped = new Map(); state.units.forEach((unit) => { const ownerId = Number(unit.workspace_owner); if (!grouped.has(ownerId)) grouped.set(ownerId, []); grouped.get(ownerId).push(unit); });
    node.innerHTML = [...grouped.entries()].map(([ownerId, units]) => {
      const byParent = new Map(); units.forEach((unit) => { const key = unit.parent ? Number(unit.parent) : null; if (!byParent.has(key)) byParent.set(key, []); byParent.get(key).push(unit); });
      const unitPanel = (unit) => `<article class="access-unit"><header><div class="unit-identity"><span class="unit-level ${unit.unit_type}">${unit.unit_type === 'sub_branch' ? 'SB' : unit.unit_type === 'shift' ? 'SH' : 'BR'}</span><div><h3>${escapeHtml(unit.name)}</h3><p>${escapeHtml(unit.path)}</p></div></div></header><div class="access-policies">${policyRows(unit)}</div>${(byParent.get(Number(unit.id)) || []).length ? `<div class="access-children">${(byParent.get(Number(unit.id)) || []).map(unitPanel).join('')}</div>` : ''}</article>`;
      return `<section class="access-owner"><header><h3>${escapeHtml(ownerNames.get(ownerId) || `Workspace ${ownerId}`)}</h3><span>Branch → Sub-branch → Shift</span></header>${(byParent.get(null) || []).map(unitPanel).join('') || '<div class="organization-empty">No branches yet.</div>'}</section>`;
    }).join('') || '<div class="organization-empty">No organization units yet.</div>';
  }

  function renderClients() {
    const node = $('#organizationClients'); if (!node) return;
    const providerName = (code) => state.providers.find((item) => item.code === code)?.label || code || 'Custom provider';
    node.innerHTML = state.clients.map((client) => {
      const integrations = canViewIntegrations ? (client.integrations || []).map((item) => {
        const connectionStatus = item.last_test_status || item.last_sync_status || 'draft';
        return `<a class="integration-chip ${escapeHtml(connectionStatus)}" href="/client-integrations/?client=${client.id}"><span>${escapeHtml(item.name)}</span><small>${escapeHtml(providerName(item.provider_code))} · ${escapeHtml(connectionStatus)}</small></a>`;
      }).join('') : '';
      return `<article class="client-card"><header><div><h3>${escapeHtml(client.name)}</h3><p>${escapeHtml(client.code)} · ${escapeHtml(providerName(client.provider_code))}</p></div>${stateBadge(client.is_active)}</header>${canViewIntegrations ? `<div class="client-integrations">${integrations || '<small>No integration configured.</small>'}</div>` : ''}<footer><small>${escapeHtml(client.company_name_match || 'No company match')}</small><div class="client-card-actions">${canManageIntegrations ? `<a class="unit-action" href="/client-integrations/?client=${client.id}">Manage integrations</a>` : ''}${actionButton('client', client.id, canManageClients)}</div></footer></article>`;
    }).join('') || '<div class="organization-empty">No clients yet.</div>';
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
  function updateCpiInheritance() {
    const unit = state.units.find((item) => Number(item.id) === Number(accessForm.elements.organization_unit.value));
    const canInherit = Boolean(unit?.parent); const toggle = $('[data-cpi-inheritance-toggle]', accessForm); toggle.hidden = !canInherit;
    if (!canInherit) accessForm.elements.inherit_cpi_range.checked = false;
    const inherit = canInherit && accessForm.elements.inherit_cpi_range.checked; accessForm.elements.min_cpi.disabled = inherit; accessForm.elements.max_cpi.disabled = inherit;
  }
  function openClientAccess(id = null, preset = null) {
    edit.access = id ? Number(id) : null; accessForm.reset(); accessForm.elements.is_active.checked = true; accessForm.elements.inherit_cpi_range.checked = true;
    accessForm.elements.organization_unit.innerHTML = '<option value="">Select organization unit</option>' + state.units.filter((unit) => unit.is_active).map((unit) => option(unit.id, `${unit.workspace_owner_name} · ${unit.path}`)).join('');
    const record = state.access.find((row) => Number(row.id) === edit.access);
    if (record) { accessForm.elements.organization_unit.value = record.organization_unit; accessForm.elements.organization_unit.disabled = true; refreshClientOptions(); accessForm.elements.client.value = record.client; accessForm.elements.client.disabled = true; accessForm.elements.min_cpi.value = record.min_cpi ?? ''; accessForm.elements.max_cpi.value = record.max_cpi ?? ''; accessForm.elements.inherit_cpi_range.checked = Boolean(record.inherit_cpi_range); accessForm.elements.is_active.checked = record.is_active; }
    else { accessForm.elements.organization_unit.disabled = false; accessForm.elements.client.disabled = false; if (preset) accessForm.elements.organization_unit.value = preset.unit; refreshClientOptions(); if (preset) { accessForm.elements.client.value = preset.client; accessForm.elements.min_cpi.value = preset.min_cpi ?? ''; accessForm.elements.max_cpi.value = preset.max_cpi ?? ''; accessForm.elements.inherit_cpi_range.checked = preset.inherit_cpi_range !== false; accessForm.elements.is_active.checked = preset.is_active; } }
    updateCpiInheritance();
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
  unitForm?.elements.workspace_owner.addEventListener('change', refreshParentOptions); unitForm?.elements.unit_type.addEventListener('change', refreshParentOptions); accessForm?.elements.organization_unit.addEventListener('change', () => { refreshClientOptions(); updateCpiInheritance(); }); accessForm?.elements.inherit_cpi_range.addEventListener('change', updateCpiInheritance);
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
  document.addEventListener('click', (event) => { const removeAccess = event.target.closest('[data-remove-client-access]'); if (removeAccess) { removeClientAccess(removeAccess.dataset.removeClientAccess); return; } const override = event.target.closest('[data-override-client-access]'); if (override) { const [unitId, clientId] = override.dataset.overrideClientAccess.split(':').map(Number); openClientAccess(null, { unit: unitId, client: clientId, min_cpi: override.dataset.minCpi, max_cpi: override.dataset.maxCpi, inherit_cpi_range: override.dataset.inheritCpi !== 'false', is_active: override.dataset.policyActive === 'true' }); return; } const deleteButton = event.target.closest('[data-delete-unit]'); if (deleteButton) { deleteUnit(deleteButton.dataset.deleteUnit); return; } const unit = event.target.closest('[data-edit-unit]'); const access = event.target.closest('[data-edit-client-access]'); const client = event.target.closest('[data-edit-client]'); if (unit) { openUnit(unit.dataset.editUnit); return; } if (access) { openClientAccess(access.dataset.editClientAccess); return; } if (client) openClient(client.dataset.editClient); });
  $('[data-delete-current-unit]')?.addEventListener('click', () => deleteUnit(edit.unit));
  $('[data-remove-current-client-access]')?.addEventListener('click', () => removeClientAccess(edit.access));
  unitForm?.addEventListener('submit', async (event) => { event.preventDefault(); const box = $('[data-unit-error]'); box.hidden = true; const payload = { workspace_owner: Number(unitForm.elements.workspace_owner.value), unit_type: unitForm.elements.unit_type.value, parent: unitForm.elements.parent.value ? Number(unitForm.elements.parent.value) : null, name: unitForm.elements.name.value.trim(), code: unitForm.elements.code.value.trim(), description: unitForm.elements.description.value.trim(), is_active: unitForm.elements.is_active.checked }; try { await api(edit.unit ? `/api/v1/vendors/organization-units/${edit.unit}/` : '/api/v1/vendors/organization-units/', { method: edit.unit ? 'PATCH' : 'POST', body: JSON.stringify(payload) }); closeModals(); toast(edit.unit ? 'Organization unit updated.' : 'Organization unit created.'); await reload(); } catch (error) { box.textContent = error.message; box.hidden = false; } });
  accessForm?.addEventListener('submit', async (event) => { event.preventDefault(); const box = $('[data-client-access-error]'); box.hidden = true; const payload = { organization_unit: Number(accessForm.elements.organization_unit.value), client: Number(accessForm.elements.client.value), min_cpi: accessForm.elements.min_cpi.value || null, max_cpi: accessForm.elements.max_cpi.value || null, inherit_cpi_range: accessForm.elements.inherit_cpi_range.checked, is_active: accessForm.elements.is_active.checked }; try { await api(edit.access ? `/api/v1/vendors/organization-client-access/${edit.access}/` : '/api/v1/vendors/organization-client-access/', { method: edit.access ? 'PATCH' : 'POST', body: JSON.stringify(payload) }); closeModals(); toast(edit.access ? 'Client visibility updated.' : 'Client assigned to organization.'); await reload(); } catch (error) { box.textContent = error.message; box.hidden = false; } });
  clientForm?.addEventListener('submit', async (event) => { event.preventDefault(); const box = $('[data-client-error]'); box.hidden = true; const payload = { name: clientForm.elements.name.value.trim(), code: clientForm.elements.code.value.trim(), provider_code: clientForm.elements.provider_code.value.trim(), company_name_match: clientForm.elements.company_name_match.value.trim(), is_active: clientForm.elements.is_active.checked }; try { await api(edit.client ? `/api/v1/vendors/clients/${edit.client}/` : '/api/v1/vendors/clients/', { method: edit.client ? 'PATCH' : 'POST', body: JSON.stringify(payload) }); closeModals(); toast(edit.client ? 'Client updated.' : 'Client created.'); await reload(); } catch (error) { box.textContent = error.message; box.hidden = false; } });
  $$('[data-close-organization-modal]').forEach((button) => button.addEventListener('click', closeModals)); backdrop?.addEventListener('click', closeModals); document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeModals(); });
  reload().catch((error) => { toast(error.message, true); if ($('#organizationTree')) $('#organizationTree').innerHTML = `<div class="organization-empty">${escapeHtml(error.message)}</div>`; });
})();
