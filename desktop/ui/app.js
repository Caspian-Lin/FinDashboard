const ui = Object.fromEntries(Array.from(document.querySelectorAll('[id]'), (node) => [node.id, node]));
const core = window.__TAURI__?.core;
const labels = {
  status: '正在检查本机状态',
  install: '正在启动研究服务',
  stop: '正在停止研究服务',
  import: '正在校验并导入合成样例',
  attach: '正在接入 Codex 并验证凭据',
  source_status: '正在检查 Tushare 权限',
  source_configure: '正在检查 Tushare 凭据并应用配置',
  uninstall_preserve: '正在卸载服务并保留数据',
  uninstall_purge: '正在删除本实例',
};
const done = {
  status: '已刷新状态。',
  install: '研究服务已就绪。',
  stop: '研究服务已停止。',
  import: '合成样例已登记。可以打开研究工作台。',
  attach: 'Codex 接入已验证。请重开 Codex，使新配置生效。',
  uninstall_preserve: '服务已卸载，研究数据已保留。可从 Windows「已安装的应用」移除管理程序。',
  uninstall_purge: '本实例已清理。可从 Windows「已安装的应用」移除管理程序。',
};
const serviceLabels = {
  missing: '未安装 Docker Desktop',
  unavailable: 'Docker Desktop 不可用',
  wrong_engine: '需要 Linux 容器模式',
};
const presets = { readonly: '只读', 'topic-writer': '课题与研究运行' };
const workbenchPreferenceKey = 'finboard-workbench-target';
const workbenchActions = { in_app: '在应用内打开', browser: '在浏览器中打开' };
const sourceCapabilities = {
  daily: '股票日线', adj_factor: '复权因子', index_daily: '指数日线',
  daily_basic: '每日指标', fina_indicator: '财务指标', trade_cal: '交易日历',
};
const sourceChecks = {
  available: '可用', credentials_missing: '未配置', invalid_credentials: '凭据无效',
  permission_denied: '权限不足', quota_exhausted: '已达配额', connection_failed: '网络或服务不可用',
  coverage_insufficient: '样本无数据',
};
let status;
let busy = false;

try {
  if (localStorage.getItem(workbenchPreferenceKey) === 'browser') ui['workbench-target'].value = 'browser';
} catch {
  // 持久化不可用时沿用应用内打开。
}

function workbenchTarget() {
  return ui['workbench-target'].value === 'browser' ? 'browser' : 'in_app';
}

function alternateWorkbenchTarget() {
  return workbenchTarget() === 'browser' ? 'in_app' : 'browser';
}

function setWorkbenchStatus(message, error = false) {
  ui['workbench-status'].textContent = message;
  ui['workbench-status'].dataset.error = String(error);
}

function renderSourceDiagnostic(diagnostic, saving) {
  const state = ui['source-state'];
  const checks = diagnostic.capabilities;
  if (saving) {
    state.textContent = diagnostic.saved
      ? diagnostic.restart === 'ready'
        ? '配置已保存，研究服务已重启。'
        : '配置已保存，服务重启未确认。请刷新状态后启动服务。'
      : '配置未保存。请查看接口检查结果后重试。';
    state.dataset.error = String(!diagnostic.saved || diagnostic.restart !== 'ready');
  } else {
    state.textContent = checks.daily === 'credentials_missing'
      ? 'Tushare 尚未配置。'
      : '权限检查已完成。展开查看各接口的可用状态。';
    state.dataset.error = 'false';
  }
  const fields = Object.entries(sourceCapabilities).flatMap(([name, label]) => {
    if (!checks[name]) return [];
    const title = document.createElement('dt');
    const value = document.createElement('dd');
    title.textContent = label;
    value.textContent = sourceChecks[checks[name]] ?? '未确认';
    value.dataset.tone = checks[name] === 'available' ? 'good' : 'warning';
    return [title, value];
  });
  ui['source-capabilities'].replaceChildren(...fields);
  ui['source-details'].hidden = fields.length === 0;
  ui['source-details'].open = Object.values(checks).some((check) => check !== 'available');
}

function setResult(message, error = false) {
  ui['result-status'].textContent = message;
  ui['result-status'].dataset.error = String(error);
  if (error && !ui['diagnostic-details'].hidden) ui['diagnostic-details'].open = true;
}

function reconcile() {
  const engine = status?.docker === 'linux';
  const installed = status?.installed === true;
  const running = status?.running === true;
  const preserving = status?.phase === 'preserving';
  const purging = status?.phase === 'purging';
  const cleaning = preserving || purging;
  const hasInstance = status?.has_instance === true;
  const changedPort = Number(ui.port.value) !== status?.port;
  ui.refresh.disabled = busy;
  ui.port.disabled = busy || !engine || cleaning;
  ui.start.disabled = busy || !engine || cleaning || (running && !changedPort);
  ui.start.textContent = running && changedPort ? '应用端口' : installed ? '启动服务' : '安装并启动';
  ui.stop.disabled = busy || !engine || !installed || cleaning;
  ui.import.disabled = busy || !running;
  ui['source-token'].disabled = busy || !running;
  ui['source-save'].disabled = busy || !running || !ui['source-token'].value;
  ui['source-probe'].disabled = busy || !running;
  ui.preset.disabled = busy || !running;
  ui.attach.disabled = busy || !running;
  ui.attach.textContent = !status?.preset ? '接入 Codex' : ui.preset.value === status.preset ? '重新接入' : '切换预设';
  for (const operation of ['open-workbench', 'open-workbench-alternate', 'copy-workbench-address']) {
    ui[operation].disabled = busy || !running;
  }
  ui['workbench-target'].disabled = busy;
  ui['open-workbench'].textContent = workbenchActions[workbenchTarget()];
  ui['open-workbench-alternate'].textContent = workbenchActions[alternateWorkbenchTarget()];
  ui.preserve.disabled = busy || !engine || !hasInstance || purging;
  ui.preserve.textContent = preserving ? '继续保留数据卸载' : '卸载并保留研究数据';
  ui['show-purge'].disabled = busy || !engine || !hasInstance || preserving;
  ui['show-purge'].textContent = purging ? '继续彻底卸载…' : '彻底卸载…';
  ui.confirmation.disabled = ui['show-purge'].disabled;
  ui.purge.disabled = ui.confirmation.disabled || ui.confirmation.value !== 'DELETE-THIS-INSTANCE';
  ui['cancel-purge'].disabled = busy;
  ui['running-progress'].hidden = !busy;
}

function renderStatus(next) {
  if (status?.port !== next.port || status?.running !== next.running) {
    ui['workbench-address'].hidden = true;
    setWorkbenchStatus(next.running ? '可以打开工作台或复制本机地址。' : '启动研究服务后可打开工作台。');
  }
  status = next;
  const badge = ui['service-state'];
  const phaseLabels = { initializing: '等待完成安装', retained: '研究数据已保留', preserving: '等待继续卸载', purging: '等待继续清理' };
  badge.textContent = next.running ? '服务就绪' : (serviceLabels[next.docker] ?? phaseLabels[next.phase] ?? (next.installed ? '已停止' : '待安装'));
  badge.dataset.tone = next.running ? 'good' : next.docker === 'linux' ? 'neutral' : 'warning';
  const installHint = next.phase === 'retained' ? '启动后可继续使用保留的研究数据。'
    : next.installed ? '启动后可打开工作台。' : '首次启动会加载离线镜像并创建数据库。';
  ui['service-detail'].textContent = next.repair || (next.running
    ? `Docker Desktop 已连接 · 127.0.0.1:${next.port}` : installHint);
  ui.port.value = String(next.port);
  if (next.preset in presets) ui.preset.value = next.preset;
  ui['codex-state'].textContent = next.pending_revocation
    ? '上一次切换仍有旧凭据待撤销。请重新接入所选预设。'
    : next.credential_active === false
      ? 'Codex 凭据已失效，请重新接入。'
      : presets[next.preset] ? `当前预设为「${presets[next.preset]}」。` : '尚未接入 Codex。';
  ui.scopes.textContent = next.scopes.join(' · ');
  ui.scopes.hidden = next.scopes.length === 0;
  ui['purge-scope'].textContent = [next.project, ...(next.volumes ?? [])].filter(Boolean).join(' · ');
  reconcile();
}

function startRecord() {
  ui.log.textContent = '';
  ui.log.hidden = true;
  ui.command.hidden = true;
  ui['diagnostic-details'].hidden = true;
  ui['diagnostic-details'].open = false;
  ui['result-meta'].textContent = '';
}

function addLog({ stream, line }) {
  if (!line) return;
  ui['diagnostic-details'].hidden = false;
  ui.log.hidden = false;
  ui.log.append(document.createTextNode(`${stream === 'stderr' ? '[stderr] ' : ''}${line}\n`));
  ui.log.scrollTop = ui.log.scrollHeight;
}

function run(operation, reportLog = true) {
  const onProgress = new core.Channel();
  if (reportLog) onProgress.onmessage = addLog;
  return core.invoke('desktop_operation', { operation, onProgress });
}

async function perform(operation) {
  if (busy) return;
  if (!core) {
    setResult('请在已安装的 FinDashboard 桌面应用内打开此界面。', true);
    return;
  }
  busy = true;
  startRecord();
  setResult(`${labels[operation.kind]}…`);
  if (operation.kind.startsWith('source_')) {
    ui['source-state'].textContent = `${labels[operation.kind]}…`;
    ui['source-state'].dataset.error = 'false';
  }
  reconcile();
  try {
    const result = await run(operation);
    ui['command-text'].value = result.command;
    ui.command.hidden = false;
    ui['diagnostic-details'].hidden = false;
    ui['result-meta'].textContent = `退出码 ${result.exit_code} · ${result.elapsed_seconds.toFixed(1)} s`;

    if (operation.kind === 'status') {
      if (result.exit_code === 0 && result.status) renderStatus(result.status);
      else setStatusUnknown();
    } else {
      try {
        const refreshed = await run({ kind: 'status' }, false);
        if (refreshed.exit_code === 0 && refreshed.status) renderStatus(refreshed.status);
        else setStatusUnknown();
      } catch {
        setStatusUnknown();
      }
    }

    if (result.source) {
      renderSourceDiagnostic(result.source, operation.kind === 'source_configure');
      setResult(ui['source-state'].textContent, ui['source-state'].dataset.error === 'true');
    } else if (result.exit_code === 0 && operation.kind === 'status' && !result.status) {
      setResult('安装器没有返回可识别的状态，请刷新后重试。', true);
    } else if (result.exit_code === 0) {
      setResult(done[operation.kind]);
      if (operation.kind.startsWith('uninstall_')) closePurge();
    } else {
      setResult('操作未完成。请查看诊断，处理后可以重试。', true);
    }
  } catch (error) {
    if (operation.kind === 'status') setStatusUnknown();
    if (operation.kind.startsWith('source_')) {
      ui['source-state'].textContent = String(error);
      ui['source-state'].dataset.error = 'true';
      ui['source-details'].hidden = true;
    }
    setResult(String(error), true);
  } finally {
    busy = false;
    reconcile();
  }
}

function setStatusUnknown() {
  status = undefined;
  ui['workbench-address'].hidden = true;
  setWorkbenchStatus('请刷新本机状态，确认服务就绪。');
  ui['service-state'].textContent = '未确认';
  ui['service-state'].dataset.tone = 'warning';
  ui['service-detail'].textContent = '请刷新本机状态。';
  ui['codex-state'].textContent = '尚未确认 Codex 接入。';
  ui.scopes.hidden = true;
}

function closePurge() {
  ui['purge-form'].hidden = true;
  ui.confirmation.value = '';
  ui['show-purge'].setAttribute('aria-expanded', 'false');
  reconcile();
}

function updateNavigation() {
  const section = location.hash || '#overview';
  for (const link of document.querySelectorAll('.rail-links a')) {
    if (link.hash === section) link.setAttribute('aria-current', 'location');
    else link.removeAttribute('aria-current');
  }
}

ui.refresh.addEventListener('click', () => perform({ kind: 'status' }));
ui.port.addEventListener('input', reconcile);
ui.preset.addEventListener('change', reconcile);
ui['service-form'].addEventListener('submit', (event) => {
  event.preventDefault();
  perform({ kind: 'install', port: Number(ui.port.value) });
});
ui.stop.addEventListener('click', () => perform({ kind: 'stop' }));
ui.import.addEventListener('click', () => perform({ kind: 'import' }));
ui['source-token'].addEventListener('input', reconcile);
ui['source-probe'].addEventListener('click', () => perform({ kind: 'source_status' }));
ui['source-form'].addEventListener('submit', (event) => {
  event.preventDefault();
  if (ui['source-save'].disabled) return;
  const token = ui['source-token'].value.trim();
  ui['source-token'].value = '';
  perform({ kind: 'source_configure', token });
});
ui.attach.addEventListener('click', () => perform({ kind: 'attach', preset: ui.preset.value }));
ui.preserve.addEventListener('click', () => perform({ kind: 'uninstall_preserve' }));
ui['show-purge'].addEventListener('click', () => {
  ui['purge-form'].hidden = false;
  ui['show-purge'].setAttribute('aria-expanded', 'true');
  ui.confirmation.focus();
});
ui['cancel-purge'].addEventListener('click', () => {
  closePurge();
  ui['show-purge'].focus();
});
ui.confirmation.addEventListener('input', reconcile);
ui['purge-form'].addEventListener('submit', (event) => {
  event.preventDefault();
  if (!ui.purge.disabled) perform({ kind: 'uninstall_purge', confirmation: ui.confirmation.value });
});
ui['workbench-target'].addEventListener('change', () => {
  reconcile();
  try {
    localStorage.setItem(workbenchPreferenceKey, workbenchTarget());
    setWorkbenchStatus('已保存默认打开方式。');
  } catch {
    setWorkbenchStatus('无法保存偏好。本次选择仍可使用。', true);
  }
});

async function performWorkbench(action) {
  if (busy || !core) return;
  busy = true;
  ui['workbench-address'].hidden = true;
  setWorkbenchStatus(action === 'address' ? '正在检查工作台地址…' : '正在打开研究工作台…');
  reconcile();
  try {
    const address = await core.invoke('desktop_open_workbench', { action });
    if (action !== 'address') {
      setWorkbenchStatus(action === 'browser' ? '已在系统浏览器中打开工作台。' : '已在应用内打开工作台。');
      return;
    }
    try {
      await navigator.clipboard.writeText(address);
      setWorkbenchStatus('已复制工作台地址。');
    } catch {
      ui['workbench-address'].value = address;
      ui['workbench-address'].hidden = false;
      ui['workbench-address'].focus();
      ui['workbench-address'].select();
      setWorkbenchStatus('剪贴板不可用。请按 Ctrl+C 复制已选中的地址。', true);
    }
  } catch (error) {
    setWorkbenchStatus(String(error), true);
  } finally {
    busy = false;
    reconcile();
  }
}

ui['open-workbench'].addEventListener('click', () => performWorkbench(workbenchTarget()));
ui['open-workbench-alternate'].addEventListener('click', () => performWorkbench(alternateWorkbenchTarget()));
ui['copy-workbench-address'].addEventListener('click', () => performWorkbench('address'));
ui.copy.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(ui['command-text'].value);
    ui.copy.textContent = '已复制';
    setTimeout(() => { ui.copy.textContent = '复制命令'; }, 1200);
  } catch {
    ui['command-text'].select();
    setResult('已选中命令。按 Ctrl+C 复制。');
  }
});

ui['maintenance-link'].addEventListener('click', () => { ui.maintenance.open = true; });
window.addEventListener('hashchange', updateNavigation);
updateNavigation();
reconcile();
if (core) perform({ kind: 'status' });
else setStatusUnknown();
