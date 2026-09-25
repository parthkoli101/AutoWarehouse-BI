// ==================== STATE ====================
let selectedFiles = [];
let lastConnectPayload = null;
let network = null;
let dashboardEChartInstances = [];
let heatmapChart = null;
let radarChart = null;
let currentWarehouse = 'default';
let lastBlueprint = null;
let allDashboards = [];       // full list from last design/refresh response
let activeDashboardId = null; // which sub-dashboard is currently shown

// refined, single-accent-per-chart palette (not a rainbow per bar)
const CHART_COLORS = ['#E8A659', '#4FB8AE', '#8B8FE8', '#D97EA8', '#5FA8D3', '#6FBF8E'];
const ROLE_COLORS = { fact: '#E8A659', dimension: '#4FB8AE', reference: '#8B8FE8', lookup: '#8B8FE8' };

// ==================== ELEMENTS ====================
const dropzone = document.getElementById('dropzone');
const inputFiles = document.getElementById('input-files');
const inputFolder = document.getElementById('input-folder');
const btnChooseFiles = document.getElementById('btn-choose-files');
const btnChooseFolder = document.getElementById('btn-choose-folder');
const fileListEl = document.getElementById('file-list');
const btnUpload = document.getElementById('btn-upload');

const connectForm = document.getElementById('connect-form');
const btnConnect = document.getElementById('btn-connect');
const schemaResult = document.getElementById('schema-result');
const tableListEl = document.getElementById('table-list');
const btnCreateFiles = document.getElementById('btn-create-files');
const btnCreateDb = document.getElementById('btn-create-db');

const vizEmpty = document.getElementById('viz-empty');
const vizBody = document.getElementById('viz-body');
const graphEl = document.getElementById('graph');
const vizSourceLabel = document.getElementById('viz-source-label');
const summaryTableBody = document.getElementById('summary-table-body');
const btnGenerateBlueprint = document.getElementById('btn-generate-blueprint');

const overviewEmpty = document.getElementById('overview-empty');
const overviewBody = document.getElementById('overview-body');
const blueprintOverview = document.getElementById('blueprint-overview');
const blueprintOverviewText = document.getElementById('blueprint-overview-text');
const blueprintTables = document.getElementById('blueprint-tables');
const pipelineStrip = document.getElementById('pipeline-strip');
const techStackStrip = document.getElementById('tech-stack-strip');
const profileSection = document.getElementById('profile-section');       // overview page: stats row only
const profileSectionFull = document.getElementById('profile-section-full'); // blueprint page: per-column detail
const profileStatsRow = document.getElementById('profile-stats-row');
const profileTables = document.getElementById('profile-tables');
const blueprintGrid = document.getElementById('blueprint-grid');
const insightsList = document.getElementById('insights-list');
const usecasesList = document.getElementById('usecases-list');
const kpiChipRow = document.getElementById('kpi-chip-row');
const mongoDot = document.getElementById('mongo-dot');
const mongoLabel = document.getElementById('mongo-label');

const heatmapCard = document.getElementById('heatmap-card');
const relationshipHeatmapEl = document.getElementById('relationship-heatmap');
const radarCard = document.getElementById('radar-card');
const roleRadarEl = document.getElementById('role-radar');

const logLines = document.getElementById('log-lines');
const logDot = document.getElementById('log-dot');

const warehouseSelect = document.getElementById('warehouse-select');
const btnNewWarehouse = document.getElementById('btn-new-warehouse');
const btnDeleteWarehouse = document.getElementById('btn-delete-warehouse');

const btnDesignDashboard = document.getElementById('btn-design-dashboard');
const btnRefreshDashboard = document.getElementById('btn-refresh-dashboard');
const dashboardSubNav = document.getElementById('dashboard-sub-nav');
const dashboardActiveTitle = document.getElementById('dashboard-active-title');
const dashboardActiveFocus = document.getElementById('dashboard-active-focus');
const dashStatus = document.getElementById('dash-status');
const dashEmpty = document.getElementById('dash-empty');
const dashBody = document.getElementById('dash-body');
const dashSummary = document.getElementById('dash-summary');
const dashSummaryText = document.getElementById('dash-summary-text');
const kpiRow = document.getElementById('kpi-row');
const chartGrid = document.getElementById('chart-grid');

const topbarPageTitle = document.getElementById('topbar-page-title');
const navItems = document.querySelectorAll('.nav-item');
const pages = document.querySelectorAll('.page');
const PAGE_TITLES = { overview: 'Overview', ingestion: 'Data Ingestion', blueprint: 'Blueprint', dashboard: 'Dashboard' };

// ==================== SIDEBAR NAVIGATION ====================
navItems.forEach(btn => {
  btn.addEventListener('click', () => {
    const target = btn.dataset.page;
    navItems.forEach(b => b.classList.toggle('active', b === btn));
    pages.forEach(p => p.classList.toggle('active', p.id === `page-${target}`));
    topbarPageTitle.textContent = PAGE_TITLES[target] || target;
    // lazily resize charts when their page becomes visible again (ECharts
    // needs a real, laid-out container size, which a `display:none` page didn't have)
    setTimeout(() => {
      if (network) network.fit();
      [...dashboardEChartInstances, heatmapChart, radarChart].forEach(c => c && c.resize());
    }, 50);
  });
});

// ==================== LOG HELPER ====================
function log(message, level = 'muted') {
  const line = document.createElement('div');
  line.className = `log-line log-line-${level}`;
  const time = new Date().toLocaleTimeString('en-GB', { hour12: false });
  line.textContent = `> [${time}] ${message}`;
  logLines.prepend(line);

  logDot.className = 'dot ' + (level === 'error' ? 'dot-error' : level === 'success' ? 'dot-success' : 'dot-active');
  if (level !== 'error') {
    setTimeout(() => { logDot.className = 'dot dot-idle'; }, 1200);
  }
}

// ==================== WAREHOUSE SELECTOR ====================
async function loadWarehouseList(selectName) {
  try {
    const res = await fetch('/api/warehouses');
    const data = await res.json();
    const warehouses = data.warehouses || [];

    warehouseSelect.innerHTML = '';
    if (warehouses.length === 0) {
      const opt = document.createElement('option');
      opt.value = 'default';
      opt.textContent = 'default (not yet built)';
      warehouseSelect.appendChild(opt);
      currentWarehouse = 'default';
      btnDeleteWarehouse.disabled = true;
    } else {
      warehouses.forEach(w => {
        const opt = document.createElement('option');
        opt.value = w.name;
        opt.textContent = `${w.name} (${w.table_count} table${w.table_count === 1 ? '' : 's'})`;
        warehouseSelect.appendChild(opt);
      });
      const toSelect = selectName && warehouses.some(w => w.name === selectName) ? selectName : warehouses[0].name;
      warehouseSelect.value = toSelect;
      currentWarehouse = toSelect;
      btnDeleteWarehouse.disabled = false;
    }
  } catch (err) {
    log('Could not load warehouse list.', 'error');
  }
}

function switchToCurrentWarehouse() {
  loadWarehouseSchema();
  loadDashboardForCurrentWarehouse();
}

warehouseSelect.addEventListener('change', () => {
  currentWarehouse = warehouseSelect.value;
  log(`Switched to warehouse '${currentWarehouse}'.`);
  switchToCurrentWarehouse();
});

btnNewWarehouse.addEventListener('click', async () => {
  const name = window.prompt('New warehouse name (letters, numbers, _ and - only):');
  if (!name) return;
  try {
    const res = await fetch('/api/warehouses', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    const data = await res.json();
    if (data.status === 'ok') {
      log(data.message, 'success');
      await loadWarehouseList(name);
      switchToCurrentWarehouse();
    } else {
      log(data.message || 'Could not create warehouse.', 'error');
    }
  } catch (err) {
    log('Could not create warehouse — server unreachable.', 'error');
  }
});

btnDeleteWarehouse.addEventListener('click', async () => {
  const name = currentWarehouse;
  if (!window.confirm(`Delete warehouse '${name}'? This permanently removes its data and cannot be undone.`)) return;
  try {
    const res = await fetch(`/api/warehouses/${encodeURIComponent(name)}`, { method: 'DELETE' });
    const data = await res.json();
    if (data.status === 'ok') {
      log(data.message, 'success');
      await loadWarehouseList();
      switchToCurrentWarehouse();
    } else {
      log(data.message || 'Could not delete warehouse.', 'error');
    }
  } catch (err) {
    log('Could not delete warehouse — server unreachable.', 'error');
  }
});

// ==================== FILE SELECTION ====================
function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function addFiles(fileArray) {
  const validExts = ['.csv', '.xlsx', '.xls'];
  const validFiles = fileArray.filter(f => validExts.some(ext => f.name.toLowerCase().endsWith(ext)));
  const skipped = fileArray.length - validFiles.length;
  selectedFiles = selectedFiles.concat(validFiles);
  renderFileList();
  if (validFiles.length) log(`${validFiles.length} file(s) staged for upload.`);
  if (skipped) log(`${skipped} unsupported file(s) ignored (need .csv/.xlsx/.xls).`, 'error');
}

function renderFileList() {
  fileListEl.innerHTML = '';
  selectedFiles.forEach(f => {
    const li = document.createElement('li');
    li.innerHTML = `<span>${f.webkitRelativePath || f.name}</span><span class="size">${formatSize(f.size)}</span>`;
    fileListEl.appendChild(li);
  });
  btnUpload.disabled = selectedFiles.length === 0;
}

btnChooseFiles.addEventListener('click', () => inputFiles.click());
btnChooseFolder.addEventListener('click', () => inputFolder.click());
inputFiles.addEventListener('change', e => addFiles(Array.from(e.target.files)));
inputFolder.addEventListener('change', e => addFiles(Array.from(e.target.files)));

dropzone.addEventListener('click', (e) => {
  if (e.target === dropzone) inputFiles.click();
});
dropzone.addEventListener('keydown', e => {
  if (e.key === 'Enter' || e.key === ' ') inputFiles.click();
});
['dragenter', 'dragover'].forEach(evt =>
  dropzone.addEventListener(evt, e => { e.preventDefault(); dropzone.classList.add('drag-over'); })
);
['dragleave', 'drop'].forEach(evt =>
  dropzone.addEventListener(evt, e => { e.preventDefault(); dropzone.classList.remove('drag-over'); })
);
dropzone.addEventListener('drop', e => {
  addFiles(Array.from(e.dataTransfer.files));
});

// ==================== UPLOAD ====================
btnUpload.addEventListener('click', async () => {
  if (!selectedFiles.length) return;
  btnUpload.disabled = true;
  log(`Uploading ${selectedFiles.length} file(s) to warehouse '${currentWarehouse}'...`);

  const formData = new FormData();
  formData.append('warehouse', currentWarehouse);
  selectedFiles.forEach(f => formData.append('files', f, f.name));

  try {
    const res = await fetch('/api/upload', { method: 'POST', body: formData });
    const data = await res.json();
    if (data.status === 'ok') {
      log(data.message, 'success');
      selectedFiles = [];
      renderFileList();
    } else {
      log(data.message || 'Upload failed.', 'error');
      btnUpload.disabled = false;
    }
  } catch (err) {
    log('Upload failed — could not reach server.', 'error');
    btnUpload.disabled = false;
  }
});

// ==================== MYSQL CONNECT ====================
connectForm.addEventListener('submit', async e => {
  e.preventDefault();
  btnConnect.disabled = true;
  btnConnect.textContent = 'Connecting...';

  const formData = new FormData(connectForm);
  const payload = Object.fromEntries(formData.entries());
  log(`Connecting to ${payload.database}@${payload.host}:${payload.port}...`);

  try {
    const res = await fetch('/api/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();

    if (data.status === 'ok') {
      log(data.message, 'success');
      renderTables(data.tables);
      lastConnectPayload = payload;
      btnCreateDb.disabled = false;
    } else {
      log(data.message || 'Connection failed.', 'error');
      schemaResult.hidden = true;
      btnCreateDb.disabled = true;
    }
  } catch (err) {
    log('Connection failed — could not reach server.', 'error');
  } finally {
    btnConnect.disabled = false;
    btnConnect.textContent = 'Connect';
  }
});

function renderTables(tables) {
  tableListEl.innerHTML = '';
  tables.forEach(t => {
    const li = document.createElement('li');
    li.innerHTML = `${t.table} <span>${t.columns.length} column(s)</span>`;
    tableListEl.appendChild(li);
  });
  schemaResult.hidden = false;
}

// ==================== CREATE WAREHOUSE: FILES ====================
btnCreateFiles.addEventListener('click', async () => {
  btnCreateFiles.disabled = true;
  btnCreateFiles.textContent = 'Building...';
  log(`Running pipeline on data/raw/ into warehouse '${currentWarehouse}' (extract → clean → transform → load)...`);

  try {
    const res = await fetch('/api/create-warehouse-files', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ warehouse: currentWarehouse }),
    });
    const data = await res.json();
    if (data.status === 'ok') {
      log(data.message, 'success');
      await loadWarehouseList(currentWarehouse);
      loadWarehouseSchema();
    } else {
      log(data.message || 'Warehouse build failed.', 'error');
    }
  } catch (err) {
    log('Warehouse build failed — could not reach server.', 'error');
  } finally {
    btnCreateFiles.disabled = false;
    btnCreateFiles.textContent = 'Create Warehouse from Files';
  }
});

// ==================== CREATE WAREHOUSE: DATABASE ====================
btnCreateDb.addEventListener('click', async () => {
  if (!lastConnectPayload) return;
  btnCreateDb.disabled = true;
  btnCreateDb.textContent = 'Building...';
  log(`Pulling tables from '${lastConnectPayload.database}' into warehouse '${currentWarehouse}'...`);

  try {
    const res = await fetch('/api/create-warehouse-database', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...lastConnectPayload, warehouse: currentWarehouse }),
    });
    const data = await res.json();
    if (data.status === 'ok') {
      log(data.message, 'success');
      await loadWarehouseList(currentWarehouse);
      loadWarehouseSchema();
    } else {
      log(data.message || 'Warehouse build failed.', 'error');
    }
  } catch (err) {
    log('Warehouse build failed — could not reach server.', 'error');
  } finally {
    btnCreateDb.disabled = false;
    btnCreateDb.textContent = 'Create Warehouse from Database';
  }
});

// ==================== WAREHOUSE SCHEMA + GRAPH + HEATMAP ====================
async function loadWarehouseSchema() {
  try {
    const res = await fetch(`/api/warehouse-schema?warehouse=${encodeURIComponent(currentWarehouse)}`);
    const data = await res.json();

    if (!data.exists || !data.tables || data.tables.length === 0) {
      vizEmpty.hidden = false;
      vizBody.hidden = true;
      overviewEmpty.hidden = false;
      overviewBody.hidden = true;
      heatmapCard.hidden = true;
      return;
    }

    vizEmpty.hidden = true;
    vizBody.hidden = false;

    // summary table
    summaryTableBody.innerHTML = '';
    data.tables.forEach(t => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${t.table}</td><td>${t.rows.toLocaleString()}</td><td>${t.columns}</td>`;
      summaryTableBody.appendChild(tr);
    });

    // source label
    const info = data.info || {};
    const sourceType = info.source_type || 'unknown';
    const version = info.source_version;
    vizSourceLabel.textContent = version && version !== 'n/a (file source)'
      ? `Source: ${sourceType} — v${version}`
      : `Source: ${sourceType}`;

    // relationship heatmap -- built from REAL schema data, no AI needed
    renderRelationshipHeatmap(data.tables, data.relationships || []);

    // blueprint (roles/purposes/overview/insights) -- load if it exists, don't auto-generate
    let roleByTable = {};
    try {
      const bpRes = await fetch(`/api/blueprint?warehouse=${encodeURIComponent(currentWarehouse)}`);
      const bp = await bpRes.json();
      if (bp.status === 'ok') {
        lastBlueprint = bp;
        renderBlueprint(bp);
        roleByTable = Object.fromEntries((bp.tables || []).map(t => [t.name, t.role]));
        overviewEmpty.hidden = true;
        overviewBody.hidden = false;
      } else {
        lastBlueprint = null;
        blueprintOverview.hidden = true;
        blueprintTables.hidden = true;
        profileSection.hidden = true;
        profileSectionFull.hidden = true;
        blueprintGrid.hidden = true;
        pipelineStrip.innerHTML = '';
        techStackStrip.innerHTML = '';
        radarCard.hidden = true;
        overviewEmpty.hidden = false;
        overviewBody.hidden = true;
      }
    } catch (e) { /* blueprint optional -- graph still renders without it */ }

    // schema graph
    if (typeof vis === 'undefined') {
      log('Graph library (vis.js) failed to load — check internet connection. Table summary still shown below.', 'error');
      return;
    }

    const nodes = data.tables.map(t => {
      const role = roleByTable[t.table];
      const color = ROLE_COLORS[role] || '#4FB8AE';
      return {
        id: t.table,
        label: `${t.table}${role ? '\n[' + role.toUpperCase() + ']' : ''}\n(${t.rows.toLocaleString()} rows)`,
        color: { background: '#141B21', border: color, highlight: { background: '#1C2B36', border: '#E8A659' } },
      };
    });
    const edges = (data.relationships || []).map(r => ({
      from: r.from_table,
      to: r.to_table,
      label: r.from_column,
      arrows: 'to',
    }));

    const graphData = { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) };
    const options = {
      nodes: {
        shape: 'box',
        font: { color: '#E7ECEF', face: 'IBM Plex Mono', size: 12 },
        borderWidth: 2,
        margin: 12,
      },
      edges: {
        color: { color: '#6B7785', highlight: '#E8A659' },
        font: { color: '#6B7785', size: 10, face: 'IBM Plex Mono', strokeWidth: 0, background: '#0B0F12' },
        smooth: { type: 'continuous' },
      },
      physics: { stabilization: true, barnesHut: { gravitationalConstant: -5000, springLength: 170 } },
      interaction: { hover: true },
    };

    if (network) network.destroy();
    network = new vis.Network(graphEl, graphData, options);
  } catch (err) {
    log(`Could not load warehouse schema: ${err.message}`, 'error');
  }
}

// ---- relationship heatmap (ECharts) -- real FK data, grid of table x table ----
function renderRelationshipHeatmap(tables, relationships) {
  if (!relationships.length || tables.length < 2) {
    heatmapCard.hidden = true;
    return;
  }
  heatmapCard.hidden = false;

  const names = tables.map(t => t.table);
  const linkStrength = {};
  relationships.forEach(r => {
    const key = `${r.from_table}|${r.to_table}`;
    linkStrength[key] = (linkStrength[key] || 0) + 1;
  });

  const cells = [];
  names.forEach((rowName, i) => {
    names.forEach((colName, j) => {
      const strength = (linkStrength[`${rowName}|${colName}`] || 0) + (linkStrength[`${colName}|${rowName}`] || 0);
      cells.push([j, i, strength]);
    });
  });

  if (heatmapChart) heatmapChart.dispose();
  heatmapChart = echarts.init(relationshipHeatmapEl, null, { renderer: 'canvas' });
  heatmapChart.setOption({
    tooltip: { position: 'top', textStyle: { fontFamily: 'IBM Plex Mono' } },
    grid: { top: 20, bottom: 70, left: 110, right: 20 },
    xAxis: { type: 'category', data: names, axisLabel: { color: '#6B7785', fontFamily: 'IBM Plex Mono', fontSize: 10, rotate: 45 }, splitArea: { show: true } },
    yAxis: { type: 'category', data: names, axisLabel: { color: '#6B7785', fontFamily: 'IBM Plex Mono', fontSize: 10 }, splitArea: { show: true } },
    visualMap: {
      min: 0, max: Math.max(...cells.map(c => c[2]), 1),
      calculable: true, orient: 'horizontal', left: 'center', bottom: 0,
      textStyle: { color: '#6B7785', fontFamily: 'IBM Plex Mono', fontSize: 10 },
      inRange: { color: ['#141B21', '#4FB8AE'] },
    },
    series: [{
      type: 'heatmap',
      data: cells,
      label: { show: false },
      emphasis: { itemStyle: { shadowBlur: 8, shadowColor: 'rgba(79,184,174,0.5)' } },
      itemStyle: { borderColor: '#0B0F12', borderWidth: 2 },
    }],
  });
}

// ---- table-role radar (ECharts) -- real role counts from the blueprint ----
function renderRoleRadar(tables) {
  const counts = { fact: 0, dimension: 0, reference: 0, lookup: 0 };
  tables.forEach(t => { counts[t.role] = (counts[t.role] || 0) + 1; });
  const total = Object.values(counts).reduce((a, b) => a + b, 0);

  if (total === 0) {
    radarCard.hidden = true;
    return;
  }
  radarCard.hidden = false;

  if (radarChart) radarChart.dispose();
  radarChart = echarts.init(roleRadarEl, null, { renderer: 'canvas' });
  const indicators = ['fact', 'dimension', 'reference', 'lookup'].map(role => ({
    name: role.charAt(0).toUpperCase() + role.slice(1),
    max: Math.max(total, 4),
  }));
  radarChart.setOption({
    tooltip: { textStyle: { fontFamily: 'IBM Plex Mono' } },
    radar: {
      indicator: indicators,
      axisName: { color: '#6B7785', fontFamily: 'IBM Plex Mono', fontSize: 11 },
      splitLine: { lineStyle: { color: '#232E36' } },
      splitArea: { areaStyle: { color: ['#0F1418', '#141B21'] } },
      axisLine: { lineStyle: { color: '#232E36' } },
    },
    series: [{
      type: 'radar',
      data: [{
        value: ['fact', 'dimension', 'reference', 'lookup'].map(r => counts[r]),
        name: 'Table count',
        areaStyle: { color: 'rgba(232,166,89,0.25)' },
        lineStyle: { color: '#E8A659', width: 2 },
        itemStyle: { color: '#E8A659' },
      }],
    }],
  });
}

// ==================== BLUEPRINT RENDERING ====================
function renderBlueprint(bp) {
  if (bp.overview) {
    blueprintOverview.hidden = false;
    blueprintOverviewText.textContent = bp.overview;
  } else {
    blueprintOverview.hidden = true;
  }

  if (bp.pipeline_steps) renderPipelineStrip(bp.pipeline_steps);
  if (bp.tech_stack) renderTechStack(bp.tech_stack);

  const tables = bp.tables || [];
  if (tables.length > 0) {
    blueprintTables.hidden = false;
    blueprintTables.innerHTML = '';
    tables.forEach(t => {
      const card = document.createElement('div');
      card.className = 'blueprint-table-card';
      const roleClass = `blueprint-role-${t.role || 'dimension'}`;
      card.innerHTML = `
        <p class="blueprint-table-name">${t.name}</p>
        <span class="blueprint-role-badge ${roleClass}">${(t.role || 'dimension')}</span>
        <p class="blueprint-table-purpose">${t.purpose || ''}</p>
      `;
      blueprintTables.appendChild(card);
    });
    renderRoleRadar(tables);
  } else {
    blueprintTables.hidden = true;
    radarCard.hidden = true;
  }

  if (bp.profile) renderDatasetProfile(bp.profile);

  const hasAnalysis = (bp.key_insights || []).length || (bp.suggested_kpis || []).length || (bp.business_use_cases || []).length;
  if (hasAnalysis) {
    blueprintGrid.hidden = false;
    insightsList.innerHTML = (bp.key_insights || []).map(i => `<li>${i}</li>`).join('') || '<li class="blueprint-empty">None generated.</li>';
    usecasesList.innerHTML = (bp.business_use_cases || []).map(u => `<li>${u}</li>`).join('') || '<li class="blueprint-empty">None generated.</li>';
    kpiChipRow.innerHTML = (bp.suggested_kpis || []).map(k => `<span class="kpi-chip">${k}</span>`).join('');
  } else {
    blueprintGrid.hidden = true;
  }
}

function renderPipelineStrip(steps) {
  pipelineStrip.innerHTML = steps.map((s, i) => `
    <div class="pipeline-step">
      <span class="pipeline-step-num">${String(i + 1).padStart(2, '0')}</span>
      <p class="pipeline-step-name">${s.name}</p>
      <p class="pipeline-step-detail">${s.detail}</p>
    </div>
  `).join('');
}

function renderTechStack(stack) {
  techStackStrip.innerHTML = Object.entries(stack).map(([k, v]) => `<span class="tech-chip"><b>${k}:</b> ${v}</span>`).join('');
}

function renderDatasetProfile(profile) {
  profileSection.hidden = false;
  profileSectionFull.hidden = false;

  const sizeMb = (profile.warehouse_size_bytes / (1024 * 1024)).toFixed(2);
  profileStatsRow.innerHTML = `
    <div class="profile-stat"><span class="profile-stat-value">${profile.total_tables}</span><span class="profile-stat-label">Tables</span></div>
    <div class="profile-stat"><span class="profile-stat-value">${profile.total_rows.toLocaleString()}</span><span class="profile-stat-label">Total Rows</span></div>
    <div class="profile-stat"><span class="profile-stat-value">${sizeMb} MB</span><span class="profile-stat-label">Warehouse Size</span></div>
  `;

  profileTables.innerHTML = '';
  (profile.tables || []).forEach(t => {
    const block = document.createElement('div');
    block.className = 'profile-table-block';

    const rowsHtml = (t.columns || []).map(c => {
      const nullPct = c.null_pct ?? 0;
      const range = (c.min !== undefined && c.min !== null) ? `${c.min} → ${c.max}` : '';
      return `
        <div class="profile-column-row">
          <span class="profile-col-name">${c.name}</span>
          <span class="profile-col-type">${c.type}</span>
          <div class="profile-col-null-bar-wrap" title="${nullPct}% null">
            <div class="profile-col-null-bar" style="width:${Math.min(nullPct, 100)}%"></div>
          </div>
          <span class="profile-col-distinct">${c.distinct_count != null ? c.distinct_count.toLocaleString() + ' distinct' : ''}</span>
          <span class="profile-col-range">${range}</span>
        </div>
      `;
    }).join('');

    block.innerHTML = `
      <div class="profile-table-header">${t.name}<span>${t.row_count.toLocaleString()} rows · ${t.column_count} columns</span></div>
      ${rowsHtml}
    `;
    profileTables.appendChild(block);
  });
}

btnGenerateBlueprint.addEventListener('click', async () => {
  btnGenerateBlueprint.disabled = true;
  btnGenerateBlueprint.textContent = 'Generating (can take a few minutes)...';
  log(`Asking Ollama to generate a blueprint for warehouse '${currentWarehouse}'...`);
  try {
    const res = await fetch('/api/blueprint', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ warehouse: currentWarehouse }),
    });
    const data = await res.json();
    if (data.status === 'ok') {
      log('Blueprint generated.', 'success');
      loadWarehouseSchema();
    } else {
      log(data.message || 'Blueprint generation failed.', 'error');
    }
  } catch (err) {
    log('Blueprint generation failed — could not reach server.', 'error');
  } finally {
    btnGenerateBlueprint.disabled = false;
    btnGenerateBlueprint.textContent = 'Generate Blueprint (AI)';
  }
});

// ==================== MONGODB STATUS ====================
async function loadMongoStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    const connected = data.mongo && data.mongo.connected;
    mongoDot.className = connected ? 'dot dot-success' : 'dot dot-idle';
    mongoLabel.title = connected ? 'MongoDB connected' : 'MongoDB not connected — using local disk storage';
  } catch (err) {
    mongoDot.className = 'dot dot-idle';
  }
}
loadMongoStatus();

// ==================== DASHBOARD (ECharts) ====================
function formatKpiValue(value, format) {
  if (value === null || value === undefined) return '—';
  const num = Number(value);
  if (isNaN(num)) return String(value);
  if (format === 'currency') {
    // Indian numbering convention for large amounts -- ₹1,58,43,553
    // is technically correct but unreadable at a glance; Cr/L is how
    // this would actually be reported.
    const abs = Math.abs(num);
    if (abs >= 1e7) return '₹' + (num / 1e7).toLocaleString('en-IN', { maximumFractionDigits: 2 }) + ' Cr';
    if (abs >= 1e5) return '₹' + (num / 1e5).toLocaleString('en-IN', { maximumFractionDigits: 2 }) + ' L';
    return '₹' + num.toLocaleString('en-IN', { maximumFractionDigits: 0 });
  }
  if (format === 'percent') return num.toFixed(1) + '%';
  return num.toLocaleString('en-IN');
}

function renderDashboard(data) {
  // top-level entry point after a design/refresh: store the whole suite,
  // build the sidebar sub-nav, and show whichever dashboard is active
  allDashboards = data.dashboards || [];
  if (allDashboards.length === 0) {
    dashEmpty.hidden = false;
    dashBody.hidden = true;
    return;
  }

  renderDashboardSubNav(allDashboards);

  const stillExists = allDashboards.some(d => d.id === activeDashboardId);
  if (!activeDashboardId || !stillExists) activeDashboardId = allDashboards[0].id; // default: Centralized Overview
  renderActiveDashboard();
}

function renderDashboardSubNav(dashboards) {
  dashboardSubNav.innerHTML = '';
  dashboards.forEach(d => {
    const btn = document.createElement('button');
    btn.className = 'sub-nav-item' + (d.id === 'centralized' ? ' centralized' : '') + (d.id === activeDashboardId ? ' active' : '');
    btn.textContent = d.title;
    btn.addEventListener('click', () => {
      activeDashboardId = d.id;
      // ensure the Dashboard page itself is showing
      navItems.forEach(b => b.classList.toggle('active', b.dataset.page === 'dashboard'));
      pages.forEach(p => p.classList.toggle('active', p.id === 'page-dashboard'));
      topbarPageTitle.textContent = 'Dashboard';
      renderActiveDashboard();
    });
    dashboardSubNav.appendChild(btn);
  });
}

function renderActiveDashboard() {
  const dash = allDashboards.find(d => d.id === activeDashboardId);
  if (!dash) return;

  // reflect selection in the sub-nav
  [...dashboardSubNav.children].forEach(btn => btn.classList.toggle('active', btn.textContent === dash.title));

  dashEmpty.hidden = true;
  dashBody.hidden = false;
  dashboardActiveTitle.textContent = dash.title;
  dashboardActiveFocus.textContent = dash.focus || '';

  if (dash.summary) {
    dashSummary.hidden = false;
    dashSummaryText.textContent = dash.summary;
  } else {
    dashSummary.hidden = true;
  }

  kpiRow.innerHTML = '';
  (dash.kpis || []).forEach(kpi => {
    const card = document.createElement('div');
    card.className = 'kpi-card';
    const insightHtml = kpi.insight ? `<p class="kpi-insight">${kpi.insight}</p>` : '';
    card.innerHTML = `<p class="kpi-title" title="${kpi.title}">${kpi.title}</p><p class="kpi-value">${formatKpiValue(kpi.value, kpi.format)}</p>${insightHtml}`;
    kpiRow.appendChild(card);
  });

  dashboardEChartInstances.forEach(c => c.dispose());
  dashboardEChartInstances = [];
  chartGrid.innerHTML = '';

  // table-role radar only makes sense on the Centralized Overview
  if (dash.id === 'centralized' && lastBlueprint && lastBlueprint.tables) renderRoleRadar(lastBlueprint.tables);
  else radarCard.hidden = true;

  (dash.charts || []).forEach((chart, idx) => {
    const card = document.createElement('div');
    const isHeatmap = chart.chart_type === 'heatmap';
    const isTable = chart.chart_type === 'table';
    card.className = 'chart-card' + (isHeatmap || isTable ? ' chart-card-wide' : '');
    const wrapId = `echart-wrap-${idx}`;
    const insightHtml = chart.insight ? `<p class="chart-insight"><span class="chart-insight-label">AI Insight</span>${chart.insight}</p>` : '';

    if (isTable) {
      card.innerHTML = `<h3>${chart.title}</h3><div id="${wrapId}"></div>${insightHtml}`;
      chartGrid.appendChild(card);
      const rows = chart.data || [];
      const wrapEl = document.getElementById(wrapId);
      if (rows.length === 0) {
        wrapEl.innerHTML = `<div class="chart-empty-state">No data returned for this query.<br><span class="chart-empty-sql">${chart.sql || ''}</span></div>`;
        return;
      }
      wrapEl.innerHTML = buildLeaderboardTable(rows);
      return;
    }

    card.className = 'chart-card' + (isHeatmap ? ' chart-card-wide' : '');
    card.innerHTML = `<h3>${chart.title}</h3><div class="echart-wrap${isHeatmap ? ' echart-wrap-lg' : ''}" id="${wrapId}"></div>${insightHtml}`;
    chartGrid.appendChild(card);

    const rows = chart.data || [];
    const wrapEl = document.getElementById(wrapId);

    if (rows.length === 0) {
      wrapEl.innerHTML = `<div class="chart-empty-state">No data returned for this query.<br><span class="chart-empty-sql">${chart.sql || ''}</span></div>`;
      return;
    }

    try {
      const accent = CHART_COLORS[idx % CHART_COLORS.length];
      const instance = echarts.init(wrapEl, null, { renderer: 'canvas' });

      if (isHeatmap) {
        const values = rows.map(r => r[chart.value_field]);
        const allNullish = values.every(v => v === null || v === undefined);
        if (allNullish) {
          wrapEl.innerHTML = `<div class="chart-empty-state">Query returned rows, but every value was empty/null.<br><span class="chart-empty-sql">${chart.sql || ''}</span></div>`;
          return;
        }
        instance.setOption(buildHeatmapOption(rows, chart.x_field, chart.y_field, chart.value_field));
      } else {
        const labels = rows.map(r => r[chart.x_field]);
        const values = rows.map(r => r[chart.y_field]);
        const allValuesNullish = values.every(v => v === null || v === undefined);
        if (allValuesNullish) {
          wrapEl.innerHTML = `<div class="chart-empty-state">Query returned rows, but every value was empty/null.<br><span class="chart-empty-sql">${chart.sql || ''}</span></div>`;
          return;
        }
        instance.setOption(buildEChartOption(chart.chart_type, labels, values, accent, idx));
      }
      dashboardEChartInstances.push(instance);
    } catch (err) {
      wrapEl.innerHTML = `<div class="chart-empty-state">Chart failed to render: ${err.message}</div>`;
      log(`Chart "${chart.title}" failed to render: ${err.message}`, 'error');
    }
  });

  // CRITICAL FIX: ECharts measures its container's width the instant
  // init() runs. When cards are added one at a time to a CSS Grid with
  // auto-fit columns, the grid's final column widths aren't settled until
  // ALL cards exist -- so early charts get initialized against a stale
  // width and never re-measure, causing them to visually overflow into
  // neighboring cards (exactly the bleed-through/overlap bug). Force a
  // resize pass on every instance once the browser has committed the
  // final layout (double rAF is the reliable way to wait for that, one
  // frame is sometimes not enough for grid re-flow to have settled).
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      dashboardEChartInstances.forEach(c => c.resize());
      if (heatmapChart) heatmapChart.resize();
      if (radarChart) radarChart.resize();
    });
  });
}

function buildHeatmapOption(rows, xField, yField, valueField) {
  const xCats = [...new Set(rows.map(r => String(r[xField])))];
  const yCats = [...new Set(rows.map(r => String(r[yField])))];
  const cells = rows.map(r => [xCats.indexOf(String(r[xField])), yCats.indexOf(String(r[yField])), r[valueField]]);
  const maxVal = Math.max(...cells.map(c => c[2] || 0), 1);

  return {
    tooltip: { position: 'top', textStyle: { fontFamily: 'IBM Plex Mono', fontSize: 12 } },
    grid: { top: 20, bottom: 70, left: 100, right: 20 },
    // splitArea deliberately OFF: its default gray checkerboard bands sit
    // behind the heatmap and, combined with low-value cells rendering
    // near-black (matching the panel background), was visually dominating
    // over the actual data-driven coloring -- exactly the "washed-out
    // gray" look reported. The category axis lines/labels are enough.
    xAxis: { type: 'category', data: xCats, axisLabel: { color: '#6B7785', fontFamily: 'IBM Plex Mono', fontSize: 10, rotate: xCats.length > 5 ? 40 : 0 }, splitArea: { show: false } },
    yAxis: { type: 'category', data: yCats, axisLabel: { color: '#6B7785', fontFamily: 'IBM Plex Mono', fontSize: 10 }, splitArea: { show: false } },
    visualMap: {
      min: 0, max: maxVal, calculable: true, orient: 'horizontal', left: 'center', bottom: 0,
      textStyle: { color: '#6B7785', fontFamily: 'IBM Plex Mono', fontSize: 10 },
      // low end is a visible dark slate, not near-black -- a near-zero
      // value should still read as "a colored cell with low intensity",
      // not disappear against the panel background.
      inRange: { color: ['#1C2B36', '#2D6B63', '#4FB8AE'] },
    },
    series: [{
      type: 'heatmap',
      data: cells,
      label: { show: cells.length <= 30, color: '#E7ECEF', fontFamily: 'IBM Plex Mono', fontSize: 10 },
      emphasis: { itemStyle: { shadowBlur: 8, shadowColor: 'rgba(79,184,174,0.5)' } },
      itemStyle: { borderColor: '#0B0F12', borderWidth: 2 },
    }],
  };
}

function buildLeaderboardTable(rows) {
  const columns = Object.keys(rows[0]);
  const isNumericCol = col => rows.every(r => r[col] === null || r[col] === undefined || !isNaN(Number(r[col])));

  const headerHtml = `<th class="leaderboard-rank">#</th>` + columns.map(c =>
    `<th class="${isNumericCol(c) ? 'leaderboard-num' : ''}">${c.replace(/_/g, ' ')}</th>`
  ).join('');

  const rowsHtml = rows.map((r, i) => {
    const cells = columns.map(c => {
      const val = r[c];
      const numeric = isNumericCol(c);
      const display = numeric && val !== null && val !== undefined && !isNaN(Number(val))
        ? Number(val).toLocaleString('en-IN', { maximumFractionDigits: 2 })
        : (val ?? '—');
      return `<td class="${numeric ? 'leaderboard-num' : ''}">${display}</td>`;
    }).join('');
    return `<tr><td class="leaderboard-rank">${i + 1}</td>${cells}</tr>`;
  }).join('');

  return `<table class="leaderboard-table"><thead><tr>${headerHtml}</tr></thead><tbody>${rowsHtml}</tbody></table>`;
}

function buildEChartOption(chartType, labels, values, accent, seriesIndex) {
  const axisTextStyle = { color: '#6B7785', fontFamily: 'IBM Plex Mono', fontSize: 10 };
  const tooltipStyle = { textStyle: { fontFamily: 'IBM Plex Mono', fontSize: 12 } };

  if (chartType === 'pie') {
    const categoricalPalette = ['#E8A659', '#4FB8AE', '#8B8FE8', '#D97EA8', '#5FA8D3', '#6FBF8E', '#C99A4A', '#3F9187'];
    return {
      tooltip: { trigger: 'item', ...tooltipStyle },
      legend: { bottom: 0, textStyle: { color: '#E7ECEF', fontFamily: 'IBM Plex Mono', fontSize: 10 } },
      color: categoricalPalette,
      series: [{
        type: 'pie',
        radius: ['42%', '72%'],
        center: ['50%', '44%'],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: '#141B21', borderWidth: 2 },
        label: { color: '#E7ECEF', fontFamily: 'IBM Plex Mono', fontSize: 11 },
        data: labels.map((l, i) => ({ name: String(l), value: values[i] })),
      }],
    };
  }

  if (chartType === 'line') {
    return {
      tooltip: { trigger: 'axis', ...tooltipStyle },
      grid: { top: 20, bottom: 50, left: 55, right: 20 },
      xAxis: { type: 'category', data: labels.map(String), axisLabel: axisTextStyle, axisLine: { lineStyle: { color: '#232E36' } } },
      yAxis: { type: 'value', axisLabel: axisTextStyle, splitLine: { lineStyle: { color: '#232E36' } } },
      series: [{
        type: 'line',
        data: values,
        smooth: true,
        symbol: 'circle',
        symbolSize: 6,
        lineStyle: { color: accent, width: 2.5 },
        itemStyle: { color: accent },
        areaStyle: {
          color: {
            type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [{ offset: 0, color: accent + '55' }, { offset: 1, color: accent + '05' }],
          },
        },
      }],
    };
  }

  // default: bar, rounded corners + subtle gradient
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, ...tooltipStyle },
    grid: { top: 20, bottom: 50, left: 55, right: 20 },
    xAxis: { type: 'category', data: labels.map(String), axisLabel: { ...axisTextStyle, rotate: labels.length > 5 ? 30 : 0 }, axisLine: { lineStyle: { color: '#232E36' } } },
    yAxis: { type: 'value', axisLabel: axisTextStyle, splitLine: { lineStyle: { color: '#232E36' } } },
    series: [{
      type: 'bar',
      data: values,
      barMaxWidth: 46,
      itemStyle: {
        borderRadius: [6, 6, 0, 0],
        color: {
          type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: accent }, { offset: 1, color: accent + 'AA' }],
        },
      },
    }],
  };
}

// ==================== DASHBOARD: REFRESH / DESIGN ====================
btnRefreshDashboard.addEventListener('click', async () => {
  btnRefreshDashboard.disabled = true;
  try {
    const res = await fetch(`/api/dashboard/data?warehouse=${encodeURIComponent(currentWarehouse)}`);
    const data = await res.json();
    if (data.status === 'ok') {
      renderDashboard(data);
      dashStatus.textContent = `Last designed: ${new Date(data.generated_at).toLocaleTimeString()} · refreshed just now`;
      log('Dashboard refreshed with current data.', 'success');
    } else {
      log(data.message || 'Refresh failed.', 'error');
    }
  } catch (err) {
    log('Refresh failed — could not reach server.', 'error');
  } finally {
    btnRefreshDashboard.disabled = false;
  }
});

btnDesignDashboard.addEventListener('click', async () => {
  btnDesignDashboard.disabled = true;
  btnDesignDashboard.textContent = 'Designing (several themed dashboards — can take a few minutes)...';
  dashStatus.textContent = '';
  log(`Asking Ollama to propose dashboard themes for '${currentWarehouse}', design each one, then explain the real results...`);

  try {
    const res = await fetch('/api/dashboard/design', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ warehouse: currentWarehouse }),
    });
    const data = await res.json();
    if (data.status === 'ok') {
      const totalKpis = data.dashboards.reduce((n, d) => n + (d.kpis || []).length, 0);
      const totalCharts = data.dashboards.reduce((n, d) => n + (d.charts || []).length, 0);
      log(`Designed ${data.dashboards.length} dashboard(s): ${totalKpis} KPI(s), ${totalCharts} chart(s) total`
        + (data.rejected_count ? `, ${data.rejected_count} query(ies) rejected by safety check` : '')
        + (data.empty_chart_count ? `, ${data.empty_chart_count} chart(s) returned no data (shown on the card itself)` : ''),
        data.empty_chart_count ? 'error' : 'success');
      activeDashboardId = null; // default back to Centralized Overview on a fresh design
      renderDashboard(data);
      dashStatus.textContent = `Last designed: ${new Date(data.generated_at).toLocaleTimeString()}`;
      await loadWarehouseList(currentWarehouse);
    } else {
      log(data.message || 'Dashboard design failed.', 'error');
    }
  } catch (err) {
    log('Dashboard design failed — could not reach server.', 'error');
  } finally {
    btnDesignDashboard.disabled = false;
    btnDesignDashboard.textContent = 'Design Dashboards (AI)';
  }
});

async function loadDashboardForCurrentWarehouse() {
  dashStatus.textContent = '';
  activeDashboardId = null;
  try {
    const res = await fetch(`/api/dashboard/data?warehouse=${encodeURIComponent(currentWarehouse)}`);
    const data = await res.json();
    if (data.status === 'ok') {
      renderDashboard(data);
      dashStatus.textContent = `Last designed: ${new Date(data.generated_at).toLocaleTimeString()}`;
    } else {
      dashEmpty.hidden = false;
      dashBody.hidden = true;
      radarCard.hidden = true;
      dashboardSubNav.innerHTML = '';
    }
  } catch (err) { /* no existing dashboard for this warehouse -- leave empty state */ }
}

// ==================== GLOBAL RESIZE SAFETY NET ====================
window.addEventListener('resize', () => {
  dashboardEChartInstances.forEach(c => c.resize());
  if (heatmapChart) heatmapChart.resize();
  if (radarChart) radarChart.resize();
  if (network) network.fit();
});

// ==================== APP INIT ====================
(async () => {
  await loadWarehouseList();
  switchToCurrentWarehouse();
})();
