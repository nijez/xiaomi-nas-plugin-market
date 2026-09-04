/* global qrcode */

const app = document.querySelector('#app');

function pluginAssetBase() {
  const loadedScript = document.currentScript?.src
    || [...document.scripts].map((script) => script.src).find((src) => /\/app\.js(?:$|\?)/.test(src));
  if (loadedScript) return new URL('./', loadedScript).href;

  const microAppRoute = window.__MICRO_APP_BASE_ROUTE__;
  if (typeof microAppRoute === 'string' && microAppRoute) {
    const normalizedRoute = microAppRoute.endsWith('/') ? microAppRoute : `${microAppRoute}/`;
    return new URL(normalizedRoute, window.location.origin).href;
  }
  return new URL('./', window.location.href).href;
}

const assetBase = pluginAssetBase();
const assetUrl = (path) => new URL(path, assetBase).href;
const brandIcon = assetUrl('assets/115-sync-icon.png?v=115life-38.2.0');

const state = {
  status: null,
  screen: 'dashboard',
  modal: null,
  picker: null,
  auth: null,
  toast: null,
  jobDraft: {
    direction: 'upload',
    localPath: '',
    remoteCid: '0',
    remoteLabel: '115 根目录',
    schedule: 'manual',
  },
};

let authPollTimer = null;
let refreshTimer = null;
let layoutObserver = null;
let layoutProbeTimer = null;
let layoutProbeSent = false;

function syncLayoutMode() {
  const rect = app.getBoundingClientRect();
  if (!rect.width) return;

  const layout = rect.width < 700 ? 'narrow' : rect.width < 1040 ? 'compact' : 'wide';
  const visibleHeight = Math.min(rect.height, window.innerHeight || rect.height);
  const height = visibleHeight < 700 ? 'short' : 'regular';

  app.dataset.layout = layout;
  app.dataset.height = height;
}

function observeLayout() {
  syncLayoutMode();
  if (typeof ResizeObserver === 'function') {
    layoutObserver = new ResizeObserver(syncLayoutMode);
    layoutObserver.observe(app);
  }
  window.addEventListener('resize', syncLayoutMode);
  if (/Mac/.test(navigator.platform || '')) {
    layoutProbeTimer = window.setTimeout(reportLayoutMetrics, 900);
  }
}

function reportLayoutMetrics() {
  if (layoutProbeSent) return;
  layoutProbeSent = true;

  const root = app.getBoundingClientRect();
  const visualViewport = window.visualViewport;
  const readWindowSize = (target) => {
    try {
      return `${Math.round(target?.innerWidth || 0)}x${Math.round(target?.innerHeight || 0)}`;
    } catch (_error) {
      return 'unavailable';
    }
  };
  const parameters = new URLSearchParams({
    layout_probe: 'mac',
    root: `${Math.round(root.width)}x${Math.round(root.height)}`,
    inner: readWindowSize(window),
    parent: readWindowSize(window.parent),
    top: readWindowSize(window.top),
    document: `${document.documentElement.clientWidth}x${document.documentElement.clientHeight}`,
    visual: visualViewport ? `${Math.round(visualViewport.width)}x${Math.round(visualViewport.height)}` : 'unavailable',
    dpr: String(window.devicePixelRatio || 1),
  });
  window.fetch(`api/status?${parameters.toString()}`, { cache: 'no-store' }).catch(() => {});
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
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (!Number.isFinite(size) || size <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1);
  return `${(size / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function directionLabel(direction) {
  return direction === 'download' ? '115 下载到 NAS' : 'NAS 上传到 115';
}

function scheduleLabel(schedule) {
  return {
    manual: '仅手动',
    hourly: '每小时',
    every6h: '每 6 小时',
    daily: '每天',
  }[schedule] || '仅手动';
}

async function api(route, options = {}) {
  const response = await fetch(`api/${route}`, {
    method: options.method || 'GET',
    headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
    body: options.body ? JSON.stringify(options.body) : undefined,
    cache: 'no-store',
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || '服务暂时无法响应');
  }
  return payload;
}

function showToast(message, kind = 'error') {
  state.toast = { message, kind };
  render();
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    state.toast = null;
    render();
  }, 4200);
}

function clearAuthPolling() {
  if (authPollTimer) window.clearInterval(authPollTimer);
  authPollTimer = null;
  state.auth = null;
}

async function refreshStatus({ quiet = false } = {}) {
  try {
    state.status = await api('status');
    if (!state.jobDraft.localPath && state.status.localRoot) {
      state.jobDraft.localPath = state.status.localRoot;
    }
    render();
  } catch (error) {
    state.status = { unavailable: true, error: error.message };
    render();
    if (!quiet) showToast(`无法连接到 115 云备份服务：${error.message}`);
  }
}

function topbar() {
  return `
    <header class="topbar">
      <button class="icon-button" type="button" data-action="back" aria-label="返回">
        ${icon('arrow-left')}
      </button>
      <div class="app-ident">
        <img class="brand-icon" src="${brandIcon}" alt="115">
        <div>
          <h1>115 云备份</h1>
          <p>NAS 文件同步</p>
        </div>
      </div>
      <button class="icon-button" type="button" data-action="refresh" aria-label="刷新状态">
        ${icon('refresh-cw')}
      </button>
    </header>`;
}

function unavailableView() {
  return `
    <main class="page page-empty">
      <section class="empty-state">
        <span class="empty-icon danger-icon">${icon('triangle-alert')}</span>
        <h2>云备份服务未连接</h2>
        <p>${escapeHtml(state.status?.error || '请稍后重新刷新。')}</p>
        <button class="primary-button" type="button" data-action="refresh">${icon('refresh-cw')}重新连接</button>
      </section>
    </main>`;
}

function accountPanel(status) {
  if (!status.clientConfigured) {
    return `
      <section class="account-panel account-disconnected">
        <div class="account-brand">
          <img src="${brandIcon}" alt="115" class="account-logo">
          <div>
            <p class="eyebrow">首次设置</p>
            <h2>添加 115 开放平台应用</h2>
            <p>先填写 App ID，再使用 115 App 扫码授权。</p>
          </div>
        </div>
        <button class="primary-button" type="button" data-action="connect">${icon('link-2')}添加 App ID</button>
      </section>`;
  }

  if (!status.authorized) {
    return `
      <section class="account-panel account-pending">
        <div class="account-brand">
          <img src="${brandIcon}" alt="115" class="account-logo">
          <div>
            <p class="eyebrow">115 OPENAPI</p>
            <h2>等待账户授权</h2>
            <p>完成扫码后即可创建备份任务。</p>
          </div>
        </div>
        <button class="primary-button" type="button" data-action="connect">${icon('link-2')}开始授权</button>
      </section>`;
  }

  const account = status.account?.label || '已连接 115 账户';
  return `
    <section class="account-panel account-connected">
      <div class="account-brand">
        <img src="${brandIcon}" alt="115" class="account-logo">
        <div>
          <p class="eyebrow">115 OPENAPI</p>
          <h2>${escapeHtml(account)}</h2>
          <p><span class="status-dot"></span>账户已授权，仅此 NAS 可读取授权令牌</p>
        </div>
      </div>
      <button class="tertiary-button" type="button" data-action="unbind">解除连接</button>
    </section>`;
}

function taskCard(job) {
  const running = job.run;
  const summary = job.lastSummary || {};
  const arrow = job.direction === 'upload' ? 'cloud-upload' : 'download';
  const enabled = Boolean(job.enabled);
  const progress = running?.progress;
  const resultTone = job.lastResult === 'error' ? 'is-error' : job.lastResult === 'success' ? 'is-success' : '';
  const progressText = running
    ? `${progress?.done || 0} / ${progress?.total || 0}${progress?.file ? ` · ${escapeHtml(progress.file)}` : ''}`
    : job.lastSummary
      ? `上次 ${summary.copied || 0} 个已复制，${summary.skipped || 0} 个未变化`
      : '等待首次同步';
  return `
    <article class="task-card">
      <div class="task-card-main">
        <div class="task-type-icon ${job.direction === 'upload' ? 'type-upload' : 'type-download'}">${icon(arrow)}</div>
        <div class="task-copy">
          <div class="task-title-row">
            <h3>${escapeHtml(job.name)}</h3>
            <span class="task-schedule">${escapeHtml(scheduleLabel(job.schedule))}</span>
          </div>
          <p class="task-flow">${escapeHtml(job.localPath)} <span>→</span> ${escapeHtml(job.remoteLabel || '115 文件夹')}</p>
          <p class="task-meta ${resultTone}">${running ? '正在同步' : formatTime(job.lastRunAt)} · ${progressText}</p>
        </div>
      </div>
      <div class="task-actions">
        <button class="run-button" type="button" data-action="run-job" data-id="${job.id}" ${running ? 'disabled' : ''}>
          ${icon(running ? 'loader-circle' : 'play', running ? 'spin' : '')}${running ? '同步中' : '立即同步'}
        </button>
        <button class="switch ${enabled ? 'is-on' : ''}" type="button" role="switch" aria-checked="${enabled}" data-action="toggle-job" data-id="${job.id}" aria-label="${enabled ? '暂停定时同步' : '启用定时同步'}">
          <span></span>
        </button>
        <button class="small-icon-button" type="button" data-action="logs" data-id="${job.id}" aria-label="查看同步记录">${icon('chevron-right')}</button>
      </div>
    </article>`;
}

function dashboardView() {
  const status = state.status;
  const jobs = status.jobs || [];
  return `
    <main class="page dashboard-page">
      <div class="page-heading">
        <div>
          <p class="eyebrow">文件保护</p>
          <h2>备份任务</h2>
        </div>
        <span class="updated-at">${status.updatedAt ? `${formatTime(status.updatedAt)} 更新` : ''}</span>
      </div>
      ${accountPanel(status)}
      <section class="task-section" aria-labelledby="task-heading">
        <div class="section-heading">
          <div>
            <h2 id="task-heading">同步任务</h2>
            <p>同名但内容不同的文件会保留两份，默认不删除任一端文件。</p>
          </div>
          <button class="primary-button compact-button" type="button" data-action="new-job" ${status.authorized ? '' : 'disabled'}>
            ${icon('plus')}新建任务
          </button>
        </div>
        ${jobs.length ? `<div class="task-list">${jobs.map(taskCard).join('')}</div>` : `
          <div class="empty-task-list">
            <span class="empty-icon">${icon('folder-sync')}</span>
            <h3>${status.authorized ? '还没有同步任务' : '完成账户授权后即可新建任务'}</h3>
            <p>${status.authorized ? '选择 NAS 文件夹和 115 文件夹，按需手动或定时同步。' : '此插件只使用 115 官方 OpenAPI 授权。'}</p>
            ${status.authorized ? `<button class="secondary-button" type="button" data-action="new-job">${icon('plus')}新建第一个任务</button>` : ''}
          </div>`}
      </section>
      <section class="storage-note">
        <span>${icon('shield-check')}</span>
        <div><strong>任务数据保存在这台 NAS</strong><p>访问令牌与同步配置仅保存在插件的受限数据目录中。</p></div>
      </section>
    </main>`;
}

function clientIdModal() {
  return `
    <section class="modal-card modal-small" role="dialog" aria-modal="true" aria-labelledby="client-id-title">
      <div class="modal-header">
        <div><p class="eyebrow">115 OPENAPI</p><h2 id="client-id-title">填写应用 App ID</h2></div>
        <button class="icon-button" type="button" data-action="close-modal" aria-label="关闭">${icon('x')}</button>
      </div>
      <form id="client-form" class="stack-form">
        <label>App ID<input name="clientId" autocomplete="off" required maxlength="160" placeholder="粘贴你在 115 开放平台创建的 App ID"></label>
        <div class="app-id-help">
          <p class="field-help">自用 NAS 请选择个人开发者；App ID 只用于发起后续官方扫码授权。</p>
          <a class="official-link" href="https://open.115.com/" target="_blank" rel="noopener noreferrer">打开 115 开放平台</a>
        </div>
        <div class="modal-actions"><button class="primary-button" type="submit">继续授权${icon('chevron-right')}</button></div>
      </form>
    </section>`;
}

function qrCodeMarkup() {
  if (!state.auth?.qrcode) return '<div class="qr-placeholder">正在准备二维码…</div>';
  try {
    const code = qrcode(0, 'M');
    code.addData(state.auth.qrcode);
    code.make();
    return `<img class="qrcode" src="${code.createDataURL(6, 4)}" alt="115 官方授权二维码">`;
  } catch (_error) {
    return '<div class="qr-placeholder">二维码生成失败，请刷新后重试。</div>';
  }
}

function authorizationModal() {
  const stage = state.auth?.stage || 'waiting';
  const stageText = stage === 'scanned' ? '已扫描，正在确认授权' : '请使用 115 App 扫码确认';
  return `
    <section class="modal-card qr-modal" role="dialog" aria-modal="true" aria-labelledby="authorization-title">
      <div class="modal-header">
        <div><p class="eyebrow">安全授权</p><h2 id="authorization-title">连接 115 账户</h2></div>
        <button class="icon-button" type="button" data-action="close-modal" aria-label="关闭">${icon('x')}</button>
      </div>
      <div class="qr-area">${qrCodeMarkup()}</div>
      <p class="qr-status"><span class="loader-dot ${stage === 'scanned' ? 'is-active' : ''}"></span>${stageText}</p>
      <p class="field-help centered">二维码有效期约 10 分钟。授权完成后，115 会把访问令牌直接交给这台 NAS。</p>
      <div class="modal-actions split-actions">
        <button class="secondary-button" type="button" data-action="restart-auth">刷新二维码</button>
        <button class="tertiary-button" type="button" data-action="change-app-id">更换 App ID</button>
      </div>
    </section>`;
}

function jobForm() {
  const draft = state.jobDraft;
  const isUpload = draft.direction === 'upload';
  return `
    <section class="modal-card job-modal" role="dialog" aria-modal="true" aria-labelledby="job-title">
      <div class="modal-header">
        <div><p class="eyebrow">新建任务</p><h2 id="job-title">选择同步方向和目录</h2></div>
        <button class="icon-button" type="button" data-action="close-modal" aria-label="关闭">${icon('x')}</button>
      </div>
      <form id="job-form" class="stack-form">
        <label>任务名称<input name="name" maxlength="80" value="${escapeHtml(isUpload ? 'NAS 上传备份' : '115 下载备份')}" autocomplete="off"></label>
        <fieldset class="segment-field">
          <legend>同步方向</legend>
          <div class="segmented-control">
            <button type="button" class="${isUpload ? 'selected' : ''}" data-action="set-direction" data-direction="upload">${icon('cloud-upload')}上传备份</button>
            <button type="button" class="${!isUpload ? 'selected' : ''}" data-action="set-direction" data-direction="download">${icon('download')}下载备份</button>
          </div>
        </fieldset>
        <label>NAS 文件夹
          <button class="folder-input" type="button" data-action="choose-local">${icon('folder-open')}<span>${escapeHtml(draft.localPath || state.status.localRoot || '/nas/pool0')}</span>${icon('chevron-right')}</button>
        </label>
        <label>115 文件夹
          <button class="folder-input" type="button" data-action="choose-remote"><img class="field-115-icon" src="${brandIcon}" alt=""><span>${escapeHtml(draft.remoteLabel || '115 根目录')}</span>${icon('chevron-right')}</button>
        </label>
        <label>执行方式
          <select name="schedule">
            <option value="manual" ${draft.schedule === 'manual' ? 'selected' : ''}>仅手动</option>
            <option value="hourly" ${draft.schedule === 'hourly' ? 'selected' : ''}>每小时同步</option>
            <option value="every6h" ${draft.schedule === 'every6h' ? 'selected' : ''}>每 6 小时同步</option>
            <option value="daily" ${draft.schedule === 'daily' ? 'selected' : ''}>每天同步</option>
          </select>
        </label>
        <div class="protect-note"><span>${icon('shield-check')}</span><p>同名内容不同会创建带时间标记的副本；此任务不会删除 NAS 或 115 中已有的文件。</p></div>
        <div class="modal-actions"><button class="primary-button" type="submit">创建任务${icon('chevron-right')}</button></div>
      </form>
    </section>`;
}

function folderPicker() {
  const picker = state.picker;
  if (!picker) return '';
  const local = picker.kind === 'local';
  const title = local ? '选择 NAS 文件夹' : '选择 115 文件夹';
  const current = local ? picker.path : picker.label;
  const items = picker.folders || [];
  return `
    <section class="modal-card picker-modal" role="dialog" aria-modal="true" aria-labelledby="picker-title">
      <div class="modal-header">
        <div><p class="eyebrow">${local ? 'NAS 存储池' : '115 云空间'}</p><h2 id="picker-title">${title}</h2></div>
        <button class="icon-button" type="button" data-action="close-picker" aria-label="关闭">${icon('x')}</button>
      </div>
      <div class="picker-current"><span>${local ? icon('folder-open') : `<img src="${brandIcon}" alt="">`}</span><p>${escapeHtml(current || (local ? state.status.localRoot : '115 根目录'))}</p></div>
      <div class="folder-browser">
        ${picker.parent ? `<button class="folder-row parent-row" type="button" data-action="picker-up">${icon('arrow-left')}<span>上一级</span></button>` : ''}
        ${items.length ? items.map((item) => `
          <button class="folder-row" type="button" data-action="picker-open" data-value="${escapeHtml(local ? item.path : item.id)}" data-label="${escapeHtml(item.name)}">
            ${icon('folder')}<span>${escapeHtml(item.name)}</span>${icon('chevron-right')}
          </button>`).join('') : '<p class="folder-empty">当前没有可继续展开的文件夹。</p>'}
      </div>
      <div class="modal-actions picker-actions">
        <button class="secondary-button" type="button" data-action="close-picker">取消</button>
        <button class="primary-button" type="button" data-action="picker-select">选择当前文件夹</button>
      </div>
    </section>`;
}

function logsModal(job) {
  const events = state.logs || [];
  return `
    <section class="modal-card logs-modal" role="dialog" aria-modal="true" aria-labelledby="logs-title">
      <div class="modal-header">
        <div><p class="eyebrow">同步记录</p><h2 id="logs-title">${escapeHtml(job.name)}</h2></div>
        <button class="icon-button" type="button" data-action="close-modal" aria-label="关闭">${icon('x')}</button>
      </div>
      <div class="logs-list">
        ${events.length ? events.slice().reverse().map((event) => `
          <div class="log-row log-${escapeHtml(event.level || 'info')}">
            <span class="log-marker"></span>
            <div><strong>${escapeHtml(event.message)}</strong><p>${formatTime(event.at)}</p></div>
          </div>`).join('') : '<div class="empty-logs">还没有可显示的同步记录。</div>'}
      </div>
      <div class="modal-actions split-actions">
        <button class="tertiary-button danger-text" type="button" data-action="delete-job" data-id="${job.id}">${icon('trash-2')}移除任务</button>
        <button class="secondary-button" type="button" data-action="close-modal">关闭</button>
      </div>
    </section>`;
}

function overlay() {
  let body = '';
  if (state.modal === 'client') body = clientIdModal();
  if (state.modal === 'auth') body = authorizationModal();
  if (state.modal === 'job') body = jobForm();
  if (state.modal === 'logs') {
    const job = (state.status?.jobs || []).find((item) => item.id === state.logJobId);
    if (job) body = logsModal(job);
  }
  if (state.picker) body = folderPicker();
  return body ? `<div class="modal-backdrop">${body}</div>` : '';
}

function render() {
  const content = state.status?.unavailable ? unavailableView() : dashboardView();
  app.innerHTML = `${topbar()}${content}${overlay()}${state.toast ? `<div class="toast toast-${state.toast.kind}">${state.toast.kind === 'error' ? icon('triangle-alert') : icon('shield-check')}<span>${escapeHtml(state.toast.message)}</span></div>` : ''}`;
}

async function openClientConnection() {
  if (state.status?.clientConfigured) {
    await startAuthorization();
    return;
  }
  state.modal = 'client';
  render();
}

async function startAuthorization() {
  clearAuthPolling();
  state.modal = 'auth';
  state.auth = { stage: 'waiting', qrcode: '' };
  render();
  try {
    const payload = await api('auth/start', { method: 'POST', body: {} });
    state.auth = { session: payload.session, qrcode: payload.qrcode, stage: 'waiting' };
    render();
    authPollTimer = window.setInterval(pollAuthorization, 2500);
    window.setTimeout(pollAuthorization, 800);
  } catch (error) {
    clearAuthPolling();
    state.modal = null;
    render();
    showToast(`无法发起 115 授权：${error.message}`);
  }
}

async function pollAuthorization() {
  if (!state.auth?.session) return;
  try {
    const payload = await api(`auth/status?session=${encodeURIComponent(state.auth.session)}`);
    if (payload.status === 'authorized') {
      clearAuthPolling();
      state.modal = null;
      await refreshStatus({ quiet: true });
      showToast('115 账户已连接，可以创建同步任务。', 'success');
      return;
    }
    if (payload.status === 'expired') {
      clearAuthPolling();
      state.modal = null;
      render();
      showToast('二维码已过期，请重新发起授权。');
      return;
    }
    state.auth.stage = payload.status;
    render();
  } catch (error) {
    clearAuthPolling();
    state.modal = null;
    render();
    showToast(`授权状态检查失败：${error.message}`);
  }
}

async function openLocalPicker(parent = null) {
  try {
    const route = parent ? `local/folders?parent=${encodeURIComponent(parent)}` : 'local/folders';
    const payload = await api(route);
    state.picker = { kind: 'local', ...payload };
    render();
  } catch (error) {
    showToast(`无法读取 NAS 文件夹：${error.message}`);
  }
}

async function openRemotePicker(cid = '0', label = '115 根目录', parent = null) {
  try {
    const payload = await api(`remote/folders?cid=${encodeURIComponent(cid)}`);
    state.picker = { kind: 'remote', ...payload, label, parent };
    render();
  } catch (error) {
    showToast(`无法读取 115 文件夹：${error.message}`);
  }
}

async function openLogs(jobId) {
  try {
    const payload = await api(`jobs/${jobId}/logs`);
    state.logs = payload.events || [];
    state.logJobId = jobId;
    state.modal = 'logs';
    render();
  } catch (error) {
    showToast(`无法读取同步记录：${error.message}`);
  }
}

async function runJob(jobId) {
  const job = (state.status?.jobs || []).find((item) => item.id === jobId);
  if (!job || !window.confirm(`现在开始“${job.name}”吗？文件将按该任务的复制规则同步。`)) return;
  try {
    await api(`jobs/${jobId}/run`, { method: 'POST', body: {} });
    await refreshStatus({ quiet: true });
    showToast('同步任务已进入队列。', 'success');
  } catch (error) {
    showToast(`无法启动同步：${error.message}`);
  }
}

async function setJobEnabled(jobId, enabled) {
  try {
    await api(`jobs/${jobId}/enabled`, { method: 'POST', body: { enabled } });
    await refreshStatus({ quiet: true });
  } catch (error) {
    showToast(`无法更新任务：${error.message}`);
  }
}

async function removeJob(jobId) {
  const job = (state.status?.jobs || []).find((item) => item.id === jobId);
  if (!job || !window.confirm(`移除“${job.name}”的同步配置？这不会删除 NAS 或 115 中的文件。`)) return;
  try {
    await api(`jobs/${jobId}/delete`, { method: 'POST', body: {} });
    state.modal = null;
    await refreshStatus({ quiet: true });
    showToast('已移除同步任务，文件保持不变。', 'success');
  } catch (error) {
    showToast(`无法移除任务：${error.message}`);
  }
}

app.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button || button.disabled) return;
  const { action } = button.dataset;
  if (action === 'refresh') await refreshStatus();
  if (action === 'back') window.history.back();
  if (action === 'connect') await openClientConnection();
  if (action === 'close-modal') {
    clearAuthPolling();
    state.modal = null;
    render();
  }
  if (action === 'restart-auth') await startAuthorization();
  if (action === 'change-app-id') {
    clearAuthPolling();
    state.modal = 'client';
    render();
  }
  if (action === 'unbind') {
    if (!window.confirm('解除本机的 115 授权？已有同步文件不会受影响。')) return;
    try {
      await api('auth/unbind', { method: 'POST', body: {} });
      await refreshStatus({ quiet: true });
      showToast('已解除这台 NAS 的 115 授权。', 'success');
    } catch (error) {
      showToast(`无法解除授权：${error.message}`);
    }
  }
  if (action === 'new-job') {
    state.modal = 'job';
    state.jobDraft = {
      direction: 'upload',
      localPath: state.status?.localRoot || '/nas/pool0',
      remoteCid: '0',
      remoteLabel: '115 根目录',
      schedule: 'manual',
    };
    render();
  }
  if (action === 'set-direction') {
    state.jobDraft.direction = button.dataset.direction;
    render();
  }
  if (action === 'choose-local') await openLocalPicker(state.jobDraft.localPath || state.status?.localRoot);
  if (action === 'choose-remote') await openRemotePicker(state.jobDraft.remoteCid, state.jobDraft.remoteLabel);
  if (action === 'close-picker') {
    state.picker = null;
    render();
  }
  if (action === 'picker-up' && state.picker) {
    if (state.picker.kind === 'local') await openLocalPicker(state.picker.parent);
    else if (state.picker.parent) await openRemotePicker(state.picker.parent.cid, state.picker.parent.label, state.picker.parent.parent || null);
  }
  if (action === 'picker-open' && state.picker) {
    const value = button.dataset.value;
    const label = button.dataset.label || '';
    if (state.picker.kind === 'local') await openLocalPicker(value);
    else await openRemotePicker(value, label, { cid: state.picker.cid, label: state.picker.label, parent: state.picker.parent || null });
  }
  if (action === 'picker-select' && state.picker) {
    if (state.picker.kind === 'local') state.jobDraft.localPath = state.picker.path;
    else {
      state.jobDraft.remoteCid = state.picker.cid;
      state.jobDraft.remoteLabel = state.picker.label || '115 文件夹';
    }
    state.picker = null;
    render();
  }
  if (action === 'run-job') await runJob(button.dataset.id);
  if (action === 'toggle-job') {
    const job = (state.status?.jobs || []).find((item) => item.id === button.dataset.id);
    if (job) await setJobEnabled(job.id, !job.enabled);
  }
  if (action === 'logs') await openLogs(button.dataset.id);
  if (action === 'delete-job') await removeJob(button.dataset.id);
});

app.addEventListener('submit', async (event) => {
  if (!(event.target instanceof HTMLFormElement)) return;
  event.preventDefault();
  const form = new FormData(event.target);
  if (event.target.id === 'client-form') {
    try {
      await api('config/client', { method: 'POST', body: { clientId: form.get('clientId') } });
      await refreshStatus({ quiet: true });
      await startAuthorization();
    } catch (error) {
      showToast(`无法保存 App ID：${error.message}`);
    }
  }
  if (event.target.id === 'job-form') {
    try {
      await api('jobs', {
        method: 'POST',
        body: {
          name: form.get('name'),
          direction: state.jobDraft.direction,
          localPath: state.jobDraft.localPath,
          remoteCid: state.jobDraft.remoteCid,
          remoteLabel: state.jobDraft.remoteLabel,
          schedule: form.get('schedule'),
        },
      });
      state.modal = null;
      await refreshStatus({ quiet: true });
      showToast('同步任务已创建。首次同步需要你手动启动。', 'success');
    } catch (error) {
      showToast(`无法创建任务：${error.message}`);
    }
  }
});

refreshTimer = window.setInterval(() => refreshStatus({ quiet: true }), 20000);
observeLayout();
window.addEventListener('beforeunload', () => {
  window.clearInterval(refreshTimer);
  window.clearTimeout(layoutProbeTimer);
  layoutObserver?.disconnect();
  window.removeEventListener('resize', syncLayoutMode);
  clearAuthPolling();
});

refreshStatus({ quiet: true });
