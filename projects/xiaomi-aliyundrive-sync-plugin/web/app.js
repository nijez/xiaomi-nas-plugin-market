/* global qrcode */

const app = document.querySelector('#app');

function pluginAssetBase() {
  const loadedScript = document.currentScript?.src
    || [...document.scripts].map((script) => script.src).find((src) => /\/app\.js(?:$|\?)/.test(src));
  if (loadedScript) return new URL('./', loadedScript).href;
  const microAppRoute = window.__MICRO_APP_BASE_ROUTE__;
  if (typeof microAppRoute === 'string' && microAppRoute) {
    return new URL(microAppRoute.endsWith('/') ? microAppRoute : `${microAppRoute}/`, window.location.origin).href;
  }
  return new URL('./', window.location.href).href;
}

const assetBase = pluginAssetBase();
const assetUrl = (path) => new URL(path, assetBase).href;
const apiUrl = (route) => new URL(`api/${route}`, assetBase).href;
const brandIcon = assetUrl('assets/aliyundrive-icon.png?v=official-20260902');

const state = {
  status: null,
  modal: null,
  picker: null,
  auth: null,
  toast: null,
  draft: { direction: 'upload', localPath: '', remoteFolderId: 'root', remoteLabel: '阿里云盘根目录', schedule: 'manual' },
};

let authPollTimer = null;
let refreshTimer = null;
let layoutObserver = null;

function syncLayoutMode() {
  const rect = app.getBoundingClientRect();
  if (!rect.width) return;
  app.dataset.layout = rect.width < 700 ? 'narrow' : rect.width < 1040 ? 'compact' : 'wide';
  app.dataset.height = Math.min(rect.height, window.innerHeight || rect.height) < 700 ? 'short' : 'regular';
}

function observeLayout() {
  syncLayoutMode();
  if (typeof ResizeObserver === 'function') {
    layoutObserver = new ResizeObserver(syncLayoutMode);
    layoutObserver.observe(app);
  }
  window.addEventListener('resize', syncLayoutMode);
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function icon(name, extraClass = '') {
  return `<img class="ui-icon ${extraClass}" src="${assetUrl(`assets/icons/${name}.svg`)}" alt="" aria-hidden="true">`;
}

function formatTime(value) {
  if (!value) return '尚未运行';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
}

function scheduleLabel(value) {
  return { manual: '仅手动', hourly: '每小时', every6h: '每 6 小时', daily: '每天' }[value] || '仅手动';
}

async function api(route, options = {}) {
  const response = await fetch(apiUrl(route), {
    method: options.method || 'GET',
    headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
    body: options.body ? JSON.stringify(options.body) : undefined,
    cache: 'no-store',
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.ok === false) throw new Error(payload.error || '服务暂时无法响应');
  return payload;
}

function showToast(message, kind = 'error') {
  state.toast = { message, kind };
  render();
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { state.toast = null; render(); }, 4200);
}

function clearAuthPolling() {
  window.clearInterval(authPollTimer);
  authPollTimer = null;
  state.auth = null;
}

async function refreshStatus({ quiet = false } = {}) {
  try {
    state.status = await api('status');
    if (!state.draft.localPath && state.status.localRoot) state.draft.localPath = state.status.localRoot;
    render();
  } catch (error) {
    state.status = { unavailable: true, error: error.message };
    render();
    if (!quiet) showToast(`无法连接阿里云盘备份服务：${error.message}`);
  }
}

function topbar() {
  return `<header class="topbar">
    <button class="icon-button" type="button" data-action="back" aria-label="返回">${icon('arrow-left')}</button>
    <div class="app-ident"><img class="brand-icon" src="${brandIcon}" alt="阿里云盘"><div><h1>阿里云盘备份</h1><p>NAS 文件同步</p></div></div>
    <button class="icon-button" type="button" data-action="refresh" aria-label="刷新状态">${icon('refresh-cw')}</button>
  </header>`;
}

function unavailableView() {
  return `<main class="page page-empty"><section class="empty-state"><span class="empty-icon danger-icon">${icon('triangle-alert')}</span><h2>云备份服务未连接</h2><p>${escapeHtml(state.status?.error || '请稍后重新刷新。')}</p><button class="primary-button" type="button" data-action="refresh">${icon('refresh-cw')}重新连接</button></section></main>`;
}

function accountPanel(status) {
  if (!status.clientConfigured) {
    return `<section class="account-panel account-disconnected">
      <div class="account-brand"><img src="${brandIcon}" alt="阿里云盘" class="account-logo"><div><p class="eyebrow">首次设置</p><h2>添加阿里云盘开放应用</h2><p>填写你自己的 App ID，再用阿里云盘扫码授权。</p></div></div>
      <button class="primary-button" type="button" data-action="connect">${icon('link-2')}添加 App ID</button>
    </section>`;
  }
  if (!status.authorized) {
    return `<section class="account-panel account-pending">
      <div class="account-brand"><img src="${brandIcon}" alt="阿里云盘" class="account-logo"><div><p class="eyebrow">阿里云盘 OPENAPI</p><h2>等待账户授权</h2><p>完成官方二维码授权后即可创建备份任务。</p></div></div>
      <button class="primary-button" type="button" data-action="connect">${icon('link-2')}开始授权</button>
    </section>`;
  }
  return `<section class="account-panel account-connected">
    <div class="account-brand"><img src="${brandIcon}" alt="阿里云盘" class="account-logo"><div><p class="eyebrow">阿里云盘 OPENAPI</p><h2>${escapeHtml(status.account?.label || '已连接阿里云盘账户')}</h2><p><span class="status-dot"></span>账户已授权，仅此 NAS 可读取授权令牌</p></div></div>
    <button class="tertiary-button" type="button" data-action="unbind">解除连接</button>
  </section>`;
}

function taskCard(job) {
  const running = job.run;
  const resultTone = job.lastResult === 'error' ? 'is-error' : job.lastResult === 'success' ? 'is-success' : '';
  const summary = job.lastSummary || {};
  const detail = running
    ? `${running.progress?.done || 0} / ${running.progress?.total || 0}${running.progress?.file ? ` · ${escapeHtml(running.progress.file)}` : ''}`
    : job.lastSummary ? `上次 ${summary.copied || 0} 个已复制，${summary.skipped || 0} 个未变化` : '等待首次同步';
  const enabled = Boolean(job.enabled);
  return `<article class="task-card">
    <div class="task-card-main"><div class="task-type-icon ${job.direction === 'upload' ? 'type-upload' : 'type-download'}">${icon(job.direction === 'upload' ? 'cloud-upload' : 'download')}</div><div class="task-copy"><div class="task-title-row"><h3>${escapeHtml(job.name)}</h3><span class="task-schedule">${escapeHtml(scheduleLabel(job.schedule))}</span></div><p class="task-flow">${escapeHtml(job.localPath)} <span>→</span> ${escapeHtml(job.remoteLabel || '阿里云盘文件夹')}</p><p class="task-meta ${resultTone}">${running ? '正在同步' : formatTime(job.lastRunAt)} · ${detail}</p></div></div>
    <div class="task-actions"><button class="run-button" type="button" data-action="run-job" data-id="${job.id}" ${running ? 'disabled' : ''}>${icon(running ? 'loader-circle' : 'play', running ? 'spin' : '')}${running ? '同步中' : '立即同步'}</button><button class="switch ${enabled ? 'is-on' : ''}" type="button" role="switch" aria-checked="${enabled}" data-action="toggle-job" data-id="${job.id}" aria-label="${enabled ? '暂停定时同步' : '启用定时同步'}"><span></span></button><button class="small-icon-button" type="button" data-action="logs" data-id="${job.id}" aria-label="查看同步记录">${icon('chevron-right')}</button></div>
  </article>`;
}

function dashboardView() {
  const status = state.status;
  const jobs = status.jobs || [];
  return `<main class="page dashboard-page">
    <div class="page-heading"><div><p class="eyebrow">文件保护</p><h2>备份任务</h2></div><span class="updated-at">${status.updatedAt ? `${formatTime(status.updatedAt)} 更新` : ''}</span></div>
    ${accountPanel(status)}
    <section class="task-section"><div class="section-heading"><div><h2>同步任务</h2><p>同名但内容不同的文件会保留两份，默认不删除 NAS 或阿里云盘中的文件。</p></div><button class="primary-button compact-button" type="button" data-action="new-job" ${status.authorized ? '' : 'disabled'}>${icon('plus')}新建任务</button></div>
      ${jobs.length ? `<div class="task-list">${jobs.map(taskCard).join('')}</div>` : `<div class="empty-task-list"><span class="empty-icon">${icon('folder-sync')}</span><h3>${status.authorized ? '还没有同步任务' : '完成账户授权后即可新建任务'}</h3><p>${status.authorized ? '选择 NAS 文件夹和阿里云盘文件夹，按需手动或定时同步。' : '此插件只使用阿里云盘官方 OAuth 授权。'}</p>${status.authorized ? `<button class="secondary-button" type="button" data-action="new-job">${icon('plus')}新建第一个任务</button>` : ''}</div>`}
    </section>
    <section class="storage-note"><span>${icon('shield-check')}</span><div><strong>任务数据保存在这台 NAS</strong><p>访问令牌与同步配置仅保存在插件的受限数据目录中。</p></div></section>
  </main>`;
}

function clientIdModal() {
  return `<section class="modal-card modal-small" role="dialog" aria-modal="true" aria-labelledby="client-id-title"><div class="modal-header"><div><p class="eyebrow">阿里云盘 OPENAPI</p><h2 id="client-id-title">填写应用 App ID</h2></div><button class="icon-button" type="button" data-action="close-modal" aria-label="关闭">${icon('x')}</button></div>
    <form id="client-form" class="stack-form"><label>App ID<input name="clientId" autocomplete="off" required maxlength="160" placeholder="粘贴你在阿里云盘开放平台创建的 App ID"></label><div class="app-id-help"><p class="field-help">自用 NAS 请创建自己的个人应用；创建时将 Bundle ID 设为 <code>${escapeHtml(state.status?.bundleId || '')}</code>。</p><a class="official-link" href="https://www.alibabacloud.com/help/zh/pds/drive-and-photo-service-dev/user-guide/application-access-details/" target="_blank" rel="noopener noreferrer">打开官方应用接入说明</a></div><div class="modal-actions"><button class="primary-button" type="submit">继续授权${icon('chevron-right')}</button></div></form>
  </section>`;
}

function qrCodeMarkup() {
  if (!state.auth?.qrcode) return '<div class="qr-placeholder">正在准备二维码…</div>';
  try {
    const code = qrcode(0, 'M');
    code.addData(state.auth.qrcode);
    code.make();
    return `<img class="qrcode" src="${code.createDataURL(6, 4)}" alt="阿里云盘官方授权二维码">`;
  } catch (_error) {
    return '<div class="qr-placeholder">二维码生成失败，请刷新后重试。</div>';
  }
}

function authorizationModal() {
  const scanned = state.auth?.stage === 'scanned';
  return `<section class="modal-card qr-modal" role="dialog" aria-modal="true" aria-labelledby="authorization-title"><div class="modal-header"><div><p class="eyebrow">安全授权</p><h2 id="authorization-title">连接阿里云盘账户</h2></div><button class="icon-button" type="button" data-action="close-modal" aria-label="关闭">${icon('x')}</button></div><div class="qr-area">${qrCodeMarkup()}</div><p class="qr-status"><span class="loader-dot ${scanned ? 'is-active' : ''}"></span>${scanned ? '已扫描，正在确认授权' : '请使用阿里云盘 App 扫码确认'}</p><p class="field-help centered">授权仅用于读取和写入你在任务中选择的云端文件夹；令牌只保存在这台 NAS。</p><div class="modal-actions split-actions"><button class="secondary-button" type="button" data-action="restart-auth">刷新二维码</button><button class="tertiary-button" type="button" data-action="change-app-id">更换 App ID</button></div></section>`;
}

function jobForm() {
  const draft = state.draft;
  const uploading = draft.direction === 'upload';
  return `<section class="modal-card job-modal" role="dialog" aria-modal="true" aria-labelledby="job-title"><div class="modal-header"><div><p class="eyebrow">新建任务</p><h2 id="job-title">选择同步方向和目录</h2></div><button class="icon-button" type="button" data-action="close-modal" aria-label="关闭">${icon('x')}</button></div>
    <form id="job-form" class="stack-form"><label>任务名称<input name="name" maxlength="80" value="${escapeHtml(uploading ? 'NAS 上传备份' : '阿里云盘下载备份')}" autocomplete="off"></label><fieldset class="segment-field"><legend>同步方向</legend><div class="segmented-control"><button type="button" class="${uploading ? 'selected' : ''}" data-action="set-direction" data-direction="upload">${icon('cloud-upload')}上传备份</button><button type="button" class="${!uploading ? 'selected' : ''}" data-action="set-direction" data-direction="download">${icon('download')}下载备份</button></div></fieldset><label>NAS 文件夹<button class="folder-input" type="button" data-action="choose-local">${icon('folder-open')}<span>${escapeHtml(draft.localPath || state.status.localRoot || '/nas/pool0')}</span>${icon('chevron-right')}</button></label><label>阿里云盘文件夹<button class="folder-input" type="button" data-action="choose-remote"><img class="field-115-icon" src="${brandIcon}" alt=""><span>${escapeHtml(draft.remoteLabel || '阿里云盘根目录')}</span>${icon('chevron-right')}</button></label><label>执行方式<select name="schedule"><option value="manual" ${draft.schedule === 'manual' ? 'selected' : ''}>仅手动</option><option value="hourly" ${draft.schedule === 'hourly' ? 'selected' : ''}>每小时同步</option><option value="every6h" ${draft.schedule === 'every6h' ? 'selected' : ''}>每 6 小时同步</option><option value="daily" ${draft.schedule === 'daily' ? 'selected' : ''}>每天同步</option></select></label><div class="protect-note"><span>${icon('shield-check')}</span><p>同步只会新增或保留副本，不会删除 NAS 或阿里云盘中已有的文件。</p></div><div class="modal-actions"><button class="primary-button" type="submit">创建任务${icon('chevron-right')}</button></div></form>
  </section>`;
}

function folderPicker() {
  const picker = state.picker;
  if (!picker) return '';
  const local = picker.kind === 'local';
  const current = local ? picker.path : picker.label;
  const rows = (picker.folders || []).map((folder) => `<button class="folder-row" type="button" data-action="open-folder" data-id="${escapeHtml(folder.id || '')}" data-path="${escapeHtml(folder.path || '')}" data-name="${escapeHtml(folder.name)}">${icon('folder')}<span>${escapeHtml(folder.name)}</span>${icon('chevron-right')}</button>`).join('') || '<div class="folder-empty">此文件夹内还没有子文件夹</div>';
  const up = local && picker.parent ? `<button class="folder-row parent-row" type="button" data-action="up-folder">${icon('arrow-left')}<span>上一级</span>${icon('chevron-right')}</button>` : (!local && picker.trail?.length > 1 ? `<button class="folder-row parent-row" type="button" data-action="up-folder">${icon('arrow-left')}<span>上一级</span>${icon('chevron-right')}</button>` : '');
  return `<section class="modal-card picker-modal" role="dialog" aria-modal="true" aria-labelledby="picker-title"><div class="modal-header"><div><p class="eyebrow">${local ? 'NAS 存储池' : '阿里云盘'}</p><h2 id="picker-title">选择${local ? ' NAS ' : '阿里云盘'}文件夹</h2></div><button class="icon-button" type="button" data-action="close-picker" aria-label="关闭">${icon('x')}</button></div><div class="picker-current"><span>${local ? icon('folder-open') : `<img src="${brandIcon}" alt="">`}</span><p>${escapeHtml(current || '')}</p></div><div class="folder-browser">${up}${rows}</div><div class="picker-actions"><button class="primary-button" type="button" data-action="select-folder">选择当前文件夹</button></div></section>`;
}

function logsModal() {
  const events = state.modal?.events || [];
  return `<section class="modal-card logs-modal" role="dialog" aria-modal="true" aria-labelledby="logs-title"><div class="modal-header"><div><p class="eyebrow">同步记录</p><h2 id="logs-title">最近事件</h2></div><button class="icon-button" type="button" data-action="close-modal" aria-label="关闭">${icon('x')}</button></div><div class="logs-list">${events.length ? events.map((event) => `<div class="log-row log-${escapeHtml(event.level)}"><span class="log-marker"></span><div><strong>${escapeHtml(event.message)}</strong><p>${formatTime(event.at)}</p></div></div>`).join('') : '<div class="empty-logs">暂无同步记录</div>'}</div><div class="modal-actions"><button class="tertiary-button danger-text" type="button" data-action="delete-job" data-id="${escapeHtml(state.modal?.jobId || '')}">${icon('trash-2')}删除任务</button></div></section>`;
}

function renderModal() {
  let contents = '';
  if (state.modal === 'client') contents = clientIdModal();
  if (state.modal === 'auth') contents = authorizationModal();
  if (state.modal === 'job') contents = jobForm();
  if (state.modal?.type === 'logs') contents = logsModal();
  if (state.picker) contents = folderPicker();
  return contents ? `<div class="modal-backdrop">${contents}</div>` : '';
}

function render() {
  const content = state.status?.unavailable ? unavailableView() : dashboardView();
  const toast = state.toast ? `<div class="toast toast-${state.toast.kind}">${icon(state.toast.kind === 'success' ? 'shield-check' : 'triangle-alert')}<span>${escapeHtml(state.toast.message)}</span></div>` : '';
  app.innerHTML = `${topbar()}${content}${renderModal()}${toast}`;
  syncLayoutMode();
}

async function startAuthorization() {
  const payload = await api('auth/start', { method: 'POST', body: {} });
  clearAuthPolling();
  state.auth = { sid: payload.sid, qrcode: payload.qrcode, stage: 'waiting' };
  state.modal = 'auth';
  render();
  authPollTimer = window.setInterval(async () => {
    try {
      const status = await api(`auth/status?sid=${encodeURIComponent(state.auth.sid)}`);
      if (status.stage === 'authorized') {
        clearAuthPolling();
        state.modal = null;
        await refreshStatus({ quiet: true });
        showToast('阿里云盘账户已连接', 'success');
      } else if (state.auth) {
        state.auth.stage = status.stage;
        render();
      }
    } catch (error) {
      clearAuthPolling();
      showToast(error.message);
    }
  }, 2200);
}

async function openLocalPicker(path = null) {
  const payload = await api(`local/folders${path ? `?path=${encodeURIComponent(path)}` : ''}`);
  state.picker = { kind: 'local', path: payload.path, parent: payload.parent, folders: payload.folders || [] };
  render();
}

async function openRemotePicker(folderId = 'root', trail = null) {
  const payload = await api(`remote/folders?parent_id=${encodeURIComponent(folderId)}`);
  const currentTrail = trail || [{ id: 'root', label: '阿里云盘根目录' }];
  state.picker = { kind: 'remote', id: folderId, label: currentTrail.at(-1)?.label || '阿里云盘根目录', folders: payload.folders || [], trail: currentTrail };
  render();
}

async function handleClick(event) {
  const target = event.target.closest('[data-action]');
  if (!target || target.disabled) return;
  const { action, id, direction, path, name } = target.dataset;
  try {
    if (action === 'refresh') return refreshStatus();
    if (action === 'back') return window.history.length > 1 ? window.history.back() : undefined;
    if (action === 'connect') return state.status.clientConfigured ? startAuthorization() : (state.modal = 'client', render());
    if (action === 'close-modal') { clearAuthPolling(); state.modal = null; return render(); }
    if (action === 'change-app-id') { clearAuthPolling(); state.modal = 'client'; return render(); }
    if (action === 'restart-auth') return startAuthorization();
    if (action === 'unbind') {
      if (!window.confirm('解除连接会清除这台 NAS 保存的授权令牌，并暂停定时任务。云盘和 NAS 中的文件不会删除。')) return;
      await api('auth/unbind', { method: 'POST', body: {} });
      await refreshStatus({ quiet: true });
      return showToast('已清除本机授权并暂停定时任务', 'success');
    }
    if (action === 'new-job') { state.modal = 'job'; return render(); }
    if (action === 'set-direction') { state.draft.direction = direction; return render(); }
    if (action === 'choose-local') return openLocalPicker(state.draft.localPath || state.status.localRoot);
    if (action === 'choose-remote') return openRemotePicker(state.draft.remoteFolderId || 'root');
    if (action === 'close-picker') { state.picker = null; return render(); }
    if (action === 'open-folder') {
      if (state.picker.kind === 'local') return openLocalPicker(path);
      const trail = [...state.picker.trail, { id, label: name }];
      return openRemotePicker(id, trail);
    }
    if (action === 'up-folder') {
      if (state.picker.kind === 'local') return openLocalPicker(state.picker.parent);
      const trail = state.picker.trail.slice(0, -1);
      return openRemotePicker(trail.at(-1)?.id || 'root', trail);
    }
    if (action === 'select-folder') {
      if (state.picker.kind === 'local') state.draft.localPath = state.picker.path;
      else { state.draft.remoteFolderId = state.picker.id; state.draft.remoteLabel = state.picker.label; }
      state.picker = null;
      return render();
    }
    if (action === 'run-job') { await api(`jobs/${encodeURIComponent(id)}/run`, { method: 'POST', body: {} }); await refreshStatus({ quiet: true }); return showToast('已开始同步', 'success'); }
    if (action === 'toggle-job') { const job = state.status.jobs.find((item) => item.id === id); await api(`jobs/${encodeURIComponent(id)}/toggle`, { method: 'POST', body: { enabled: !job.enabled } }); return refreshStatus({ quiet: true }); }
    if (action === 'logs') { const payload = await api(`events?job_id=${encodeURIComponent(id)}`); state.modal = { type: 'logs', jobId: id, events: payload.events || [] }; return render(); }
    if (action === 'delete-job') {
      if (!window.confirm('删除任务只会移除本机配置，不会删除 NAS 或阿里云盘中的文件。确认删除？')) return;
      await api(`jobs/${encodeURIComponent(id)}`, { method: 'DELETE' });
      state.modal = null;
      await refreshStatus({ quiet: true });
      return showToast('已删除任务，文件保持不变', 'success');
    }
  } catch (error) {
    showToast(error.message);
  }
}

async function handleSubmit(event) {
  if (event.target.id === 'client-form') {
    event.preventDefault();
    try {
      const clientId = new FormData(event.target).get('clientId');
      await api('client', { method: 'POST', body: { clientId } });
      await refreshStatus({ quiet: true });
      await startAuthorization();
    } catch (error) { showToast(error.message); }
  }
  if (event.target.id === 'job-form') {
    event.preventDefault();
    try {
      const form = new FormData(event.target);
      await api('jobs', { method: 'POST', body: { name: form.get('name'), direction: state.draft.direction, localPath: state.draft.localPath || state.status.localRoot, remoteFolderId: state.draft.remoteFolderId, remoteLabel: state.draft.remoteLabel, schedule: form.get('schedule') } });
      state.modal = null;
      await refreshStatus({ quiet: true });
      showToast('同步任务已创建', 'success');
    } catch (error) { showToast(error.message); }
  }
}

app.addEventListener('click', handleClick);
app.addEventListener('submit', handleSubmit);
observeLayout();
refreshStatus();
refreshTimer = window.setInterval(() => refreshStatus({ quiet: true }), 5000);
window.addEventListener('beforeunload', () => { clearAuthPolling(); window.clearInterval(refreshTimer); layoutObserver?.disconnect(); });
