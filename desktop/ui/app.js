const ui = Object.fromEntries(Array.from(document.querySelectorAll('[id]'), (node) => [node.id, node]));
const core = window.__TAURI__?.core;
const preferences = window.finboardDesktopPreferences;
const copy = (zh, en) => ({ zh, en });
const currentCopy = new Map();

function showCopy(node, message) {
  node.removeAttribute('data-l10n');
  currentCopy.set(node, message);
  node.textContent = preferences.text(message);
}

const labels = {
  status: copy('正在检查本机状态', 'Checking local status'),
  install: copy('正在启动研究服务', 'Starting the research service'),
  stop: copy('正在停止研究服务', 'Stopping the research service'),
  import: copy('正在校验并导入合成样例', 'Verifying and importing the synthetic sample'),
  attach: copy('正在配置客户端并验证凭据', 'Configuring the client and verifying its credential'),
  detach: copy('正在撤销所选客户端的接入', 'Disconnecting the selected client'),
  client_workspace: copy('正在检查本机工作区', 'Checking the local workspace'),
  client_issue: copy('正在签发接入配置', 'Issuing connection settings'),
  client_revoke: copy('正在撤销接入凭据', 'Revoking the credential'),
  source_status: copy('正在检查 Tushare 权限', 'Checking Tushare permissions'),
  source_configure: copy('正在检查 Tushare 凭据并应用配置', 'Verifying the Tushare credential and applying settings'),
  uninstall_preserve: copy('正在卸载服务并保留数据', 'Uninstalling services and keeping research data'),
  uninstall_purge: copy('正在删除本实例', 'Deleting this instance'),
};
const done = {
  status: copy('已刷新状态。', 'Status refreshed.'),
  install: copy('研究服务已就绪。', 'The research service is ready.'),
  stop: copy('研究服务已停止。', 'The research service has stopped.'),
  import: copy('合成样例已登记。可以打开研究工作台。', 'The sample was imported. Open the research workspace to use it.'),
  attach: copy('接入已验证。请重开所选客户端，使新配置生效。', 'Connection verified. Reopen the client to use its new settings.'),
  detach: copy('所选客户端的接入已撤销。', 'The selected client was disconnected.'),
  client_revoke: copy('所选接入凭据已撤销。', 'The selected credential was revoked.'),
  uninstall_preserve: copy('服务已卸载，研究数据已保留。可从 Windows「已安装的应用」移除管理程序。', 'Services were removed and research data was kept. You can remove the manager from Windows Installed apps.'),
  uninstall_purge: copy('本实例已清理。可从 Windows「已安装的应用」移除管理程序。', 'This instance was removed. You can remove the manager from Windows Installed apps.'),
};
const serviceLabels = {
  missing: copy('未安装 Docker Desktop', 'Docker Desktop is missing'),
  unavailable: copy('Docker Desktop 不可用', 'Docker Desktop unavailable'),
  wrong_engine: copy('需要 Linux 容器模式', 'Linux containers required'),
};
const serviceHints = {
  missing: copy('请安装并启动 Docker Desktop，然后刷新状态。', 'Install and start Docker Desktop, then refresh status.'),
  unavailable: copy('请启动 Docker Desktop，然后刷新状态。', 'Start Docker Desktop, then refresh status.'),
  wrong_engine: copy('请在 Docker Desktop 中切换到 Linux 容器，然后刷新状态。', 'Switch Docker Desktop to Linux containers, then refresh status.'),
};
const phaseHints = {
  retained: copy('启动后可继续使用保留的研究数据。', 'Start the service to resume with your saved research data.'),
  preserving: copy('可在「安装维护与卸载」继续保留数据卸载。', 'Resume the uninstall under Maintenance to keep your research data.'),
  purging: copy('可在「安装维护与卸载」继续彻底卸载。', 'Resume removing the instance under Maintenance.'),
};
const repairHints = new Map([
  ["Check Docker Desktop and verify this instance's project resources.",
    copy('请检查 Docker Desktop 和此实例的项目资源。', 'Check Docker Desktop and this instance’s project resources.')],
  ['Check the local generic MCP client setup before changing its credentials.',
    copy('请先检查通用 MCP 客户端的本机配置，再更改凭据。', 'Check local generic MCP client settings before changing its credentials.')],
]);
const presets = { readonly: copy('只读', 'Read only'), 'topic-writer': copy('课题与研究运行', 'Topics and research runs') };
const harnesses = { codex: 'Codex CLI', dsh: 'DeepSeek Harness', opencode: 'OpenCode' };
const workbenchPreferenceKey = 'finboard-workbench-target';
const workbenchActions = { in_app: copy('在应用内打开', 'Open in app'), browser: copy('在浏览器中打开', 'Open in browser') };
const workbenchStopped = copy('启动研究服务后可打开工作台。', 'Start the research service to open the workspace.');
const sourceCapabilities = {
  daily: copy('股票日线', 'Daily stock bars'),
  adj_factor: copy('复权因子', 'Adjustment factors'),
  index_daily: copy('指数日线', 'Daily index bars'),
  daily_basic: copy('每日指标', 'Daily indicators'),
  fina_indicator: copy('财务指标', 'Financial indicators'),
  trade_cal: copy('交易日历', 'Trading calendar'),
};
const sourceChecks = {
  available: copy('可用', 'Available'),
  credentials_missing: copy('未配置', 'Not configured'),
  invalid_credentials: copy('凭据无效', 'Invalid credential'),
  permission_denied: copy('权限不足', 'Permission denied'),
  quota_exhausted: copy('已达配额', 'Quota exhausted'),
  connection_failed: copy('网络或服务不可用', 'Network or service unavailable'),
  coverage_insufficient: copy('样本无数据', 'Sample has no data'),
};
let status;
let busy = false;
let lastSourceChecks;

function withEllipsis(message) {
  return copy(`${message.zh}…`, `${message.en}…`);
}

preferences.refreshCopy(() => {
  for (const [node, message] of currentCopy) node.textContent = preferences.text(message);
  renderGenericClients();
  if (lastSourceChecks) renderSourceCapabilities(lastSourceChecks);
});

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
  showCopy(ui['workbench-status'], message);
  ui['workbench-status'].dataset.error = String(error);
}

function renderSourceDiagnostic(diagnostic, saving) {
  const state = ui['source-state'];
  const checks = diagnostic.capabilities;
  let message;
  if (saving) {
    message = diagnostic.saved
      ? diagnostic.restart === 'ready'
        ? copy('配置已保存，研究服务已重启。', 'Settings saved and the research service restarted.')
        : copy('配置已保存，服务重启未确认。请刷新状态后启动服务。', 'Settings saved. Restart was not confirmed. Refresh status and start the service.')
      : copy('配置未保存。请查看接口检查结果后重试。', 'Settings were not saved. Open the endpoint results before retrying.');
    state.dataset.error = String(!diagnostic.saved || diagnostic.restart !== 'ready');
  } else {
    message = checks.daily === 'credentials_missing'
      ? copy('Tushare 尚未配置。', 'Tushare is not configured.')
      : copy('权限检查已完成。展开查看各接口的可用状态。', 'Permissions checked. Expand the results to inspect each endpoint.');
    state.dataset.error = 'false';
  }
  showCopy(state, message);
  lastSourceChecks = checks;
  renderSourceCapabilities(checks);
  ui['source-details'].open = Object.values(checks).some((check) => check !== 'available');
}

function renderSourceCapabilities(checks) {
  const fields = Object.entries(sourceCapabilities).flatMap(([name, label]) => {
    if (!checks[name]) return [];
    const title = document.createElement('dt');
    const value = document.createElement('dd');
    title.textContent = preferences.text(label);
    value.textContent = preferences.text(sourceChecks[checks[name]] ?? copy('未确认', 'Unconfirmed'));
    value.dataset.tone = checks[name] === 'available' ? 'good' : 'warning';
    return [title, value];
  });
  ui['source-capabilities'].replaceChildren(...fields);
  ui['source-details'].hidden = fields.length === 0;
}

function harnessStatus() {
  return status?.clients?.find((entry) => entry.name === ui.harness.value);
}

function renderHarnessStatus() {
  const entry = harnessStatus();
  ui.preset.value = entry?.preset ?? 'readonly';
  const profiles = status?.dsh_profiles ?? [];
  ui['harness-profile'].replaceChildren(...profiles.map((profile) => {
    const option = document.createElement('option');
    option.value = profile;
    option.textContent = profile;
    return option;
  }));
  ui['harness-profile'].value = profiles.includes(entry?.profile)
    ? entry.profile : profiles.includes('desktop') ? 'desktop' : profiles[0] ?? '';
  ui['workspace-address'].hidden = true;
  const name = harnesses[ui.harness.value];
  let message = entry?.pending_revocation
    ? copy('先前的凭据待撤销，请重新接入后复查。', 'The previous credential is pending revocation. Reconnect and check again.')
    : entry?.repair_required || entry?.credential_active === false
      ? copy('无法确认现有凭据，可重新接入。', 'The current credential could not be verified. Reconnect to try again.')
      : entry?.preset
        ? copy(
          `${name} · ${presets[entry.preset]?.zh ?? entry.preset}${entry.credential_active ? ' · 已验连' : ''}`,
          `${name} · ${presets[entry.preset]?.en ?? entry.preset}${entry.credential_active ? ' · Verified' : ''}`,
        )
        : copy(`尚未接入 ${name}。`, `${name} is not connected.`);
  if (ui.harness.value === 'dsh' && !profiles.length) {
    message = copy('请在 DeepSeek Harness 创建一个 profile，再刷新状态。', 'Create a profile in DeepSeek Harness, then refresh status.');
  }
  showCopy(ui['harness-state'], message);
  ui.scopes.textContent = entry?.scopes?.join(' · ') ?? '';
  ui.scopes.hidden = !entry?.scopes?.length;
}

function renderGenericClients() {
  const selected = ui['generic-list'].value;
  const clients = status?.generic_clients ?? [];
  const options = clients.map((entry) => {
    const option = document.createElement('option');
    option.value = entry.name;
    const preset = preferences.text(presets[entry.preset] ?? entry.preset);
    const pending = entry.pending_revocation ? preferences.text(copy(' · 待完成撤销', ' · Revocation pending')) : '';
    option.textContent = `${entry.name} · ${preset}${pending}`;
    return option;
  });
  ui['generic-list'].replaceChildren(...options);
  if (clients.some((entry) => entry.name === selected)) ui['generic-list'].value = selected;
  ui['generic-current'].hidden = clients.length === 0;
}

function clearGenericConnection() {
  ui['generic-config'].value = '';
  ui['generic-connection'].hidden = true;
}

function renderGenericConnection(connection) {
  ui['generic-config'].value = JSON.stringify({
    transport: 'streamable-http',
    url: connection.url,
    headers: { Authorization: `Bearer ${connection.credential}` },
  }, null, 2);
  ui['generic-connection'].hidden = false;
  const scopes = connection.scopes.join(' · ');
  showCopy(ui['generic-state'], copy(
    `${connection.name} 已验连。作用域 ${scopes}。请保存本次配置。`,
    `${connection.name} verified. Scopes: ${scopes}. Save these settings now.`,
  ));
}

function setResult(message, error = false) {
  showCopy(ui['result-status'], message);
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
  ui['service-board'].dataset.serviceReady = String(running);
  ui.refresh.disabled = busy;
  ui.port.disabled = busy || !engine || cleaning;
  ui.start.disabled = busy || !engine || cleaning || (running && !changedPort);
  showCopy(ui.start, running && changedPort ? copy('应用端口', 'Apply port')
    : installed ? copy('启动服务', 'Start service') : copy('安装并启动', 'Install and start'));
  ui.stop.disabled = busy || !engine || !installed || cleaning;
  ui.import.disabled = busy || !running;
  ui['source-token'].disabled = busy || !running;
  ui['source-save'].disabled = busy || !running || !ui['source-token'].value;
  ui['source-probe'].disabled = busy || !running;
  const client = harnessStatus();
  const isDsh = ui.harness.value === 'dsh';
  ui.harness.disabled = busy;
  ui.preset.disabled = busy || !running;
  ui['harness-profile-field'].hidden = !isDsh;
  ui['harness-profile'].disabled = busy || !running;
  ui.attach.disabled = busy || !running || (isDsh && !/^[a-zA-Z0-9_-]{1,64}$/.test(ui['harness-profile'].value));
  showCopy(ui.attach, !client?.preset ? copy('接入并验连', 'Connect and verify')
    : ui.preset.value === client.preset ? copy('重新接入', 'Reconnect') : copy('切换预设', 'Change preset'));
  ui.detach.disabled = busy || !running || !(client?.preset || client?.repair_required);
  ui['workspace-actions'].hidden = ui.harness.value === 'codex' || !client?.preset;
  ui['workspace-open'].disabled = busy;
  ui['workspace-copy'].disabled = busy;
  ui['generic-name'].disabled = busy || !running;
  ui['generic-preset'].disabled = busy || !running;
  ui['generic-issue'].disabled = busy || !running || !/^[a-z0-9][a-z0-9_-]{0,31}$/.test(ui['generic-name'].value);
  ui['generic-list'].disabled = busy;
  ui['generic-revoke'].disabled = busy || !running || !ui['generic-list'].value;
  for (const operation of ['open-workbench', 'open-workbench-alternate', 'copy-workbench-address', 'open-release-bundles']) {
    ui[operation].disabled = busy || !running;
  }
  ui['workbench-target'].disabled = busy;
  showCopy(ui['open-workbench'], workbenchActions[workbenchTarget()]);
  showCopy(ui['open-workbench-alternate'], workbenchActions[alternateWorkbenchTarget()]);
  ui.preserve.disabled = busy || !engine || !hasInstance || purging;
  showCopy(ui.preserve, preserving ? copy('继续保留数据卸载', 'Resume uninstall and keep data')
    : copy('卸载并保留研究数据', 'Uninstall and keep research data'));
  ui['show-purge'].disabled = busy || !engine || !hasInstance || preserving;
  showCopy(ui['show-purge'], purging ? copy('继续彻底卸载…', 'Resume removing everything…')
    : copy('彻底卸载…', 'Remove everything…'));
  ui.confirmation.disabled = ui['show-purge'].disabled;
  ui.purge.disabled = ui.confirmation.disabled || ui.confirmation.value !== 'DELETE-THIS-INSTANCE';
  ui['cancel-purge'].disabled = busy;
  ui['running-progress'].hidden = !busy;
}

function renderStatus(next) {
  if (!lastSourceChecks && (!currentCopy.has(ui['source-state']) || status?.running !== next.running)) {
    showCopy(ui['source-state'], next.running ? copy('凭据仅保存在当前实例。', 'Credentials are stored for this instance.')
      : copy('服务就绪后可检查权限。', 'Permission checks are available once the service is ready.'));
  }
  if (status?.port !== next.port || status?.running !== next.running) {
    ui['workbench-address'].hidden = true;
    setWorkbenchStatus(next.running
      ? copy('可以打开工作台或复制本机地址。', 'Open the workspace or copy its address.') : workbenchStopped);
  }
  status = next;
  const badge = ui['service-state'];
  const phaseLabels = {
    initializing: copy('等待完成安装', 'Installation pending'), retained: copy('研究数据已保留', 'Research data kept'),
    preserving: copy('等待继续卸载', 'Uninstall paused'), purging: copy('等待继续清理', 'Removal paused'),
  };
  showCopy(badge, next.running ? copy('服务就绪', 'Service ready')
    : serviceLabels[next.docker] ?? phaseLabels[next.phase] ?? (next.installed ? copy('已停止', 'Stopped') : copy('待安装', 'Not installed')));
  badge.dataset.tone = next.running ? 'good' : next.docker === 'linux' ? 'neutral' : 'warning';
  const address = `127.0.0.1:${next.port}`;
  const localHint = next.running ? copy(`Docker Desktop 已连接 · ${address}`, `Docker Desktop connected · ${address}`)
    : next.installed ? workbenchStopped : copy('首次启动会加载离线镜像并创建数据库。', 'The first start loads the offline images and creates the database.');
  const repairHint = next.repair
    ? repairHints.get(next.repair) ?? copy('本机配置需要检查。请打开运行诊断查看详情。', 'Local setup needs attention. Open Diagnostics for details.')
    : localHint;
  showCopy(ui['service-detail'], serviceHints[next.docker] ?? phaseHints[next.phase] ?? repairHint);
  if (next.repair) addLog({ stream: 'stderr', line: next.repair });
  ui.port.value = String(next.port);
  renderHarnessStatus();
  renderGenericClients();
  ui['purge-scope'].textContent = [next.project, ...(next.volumes ?? [])].filter(Boolean).join(' · ');
  reconcile();
}

function startRecord() {
  clearGenericConnection();
  ui.log.textContent = '';
  ui.log.hidden = true;
  ui.command.hidden = true;
  ui['diagnostic-details'].hidden = true;
  ui['diagnostic-details'].open = false;
  currentCopy.delete(ui['result-meta']);
  ui['result-meta'].textContent = '';
}

function addLog({ stream, line }) {
  if (!line) return;
  ui['diagnostic-details'].hidden = false;
  ui.log.hidden = false;
  ui.log.append(document.createTextNode(`${stream === 'stderr' ? '[stderr] ' : ''}${line}\n`));
  ui.log.scrollTop = ui.log.scrollHeight;
}

const hostMessages = {
  '已有操作正在运行，请稍后重试。': 'Another operation is running. Please try again shortly.',
  '无法确认管理界面来源。': 'Could not verify the manager window.',
  '无法定位安装资源。': 'Could not locate the installation resources.',
  '本机操作意外终止，可刷新状态后重试。': 'The local operation ended unexpectedly. Refresh status and try again.',
  '研究版安装资源缺失，请重新安装 MSI。': 'Installation resources are missing. Reinstall the MSI.',
  '研究服务尚未就绪，请启动后重试。': 'The research service is not ready. Start it and try again.',
  '请检查默认浏览器，或复制工作台地址。': 'Check the default browser or copy the workspace address.',
  '无法打开系统浏览器，请检查默认浏览器，或复制工作台地址。': 'Could not open the system browser. Check the default browser or copy the workspace address.',
  '浏览器未能打开，可复制地址后重试。': 'Could not open the browser. Copy the workspace address and try again.',
  '无法打开研究工作台，请检查 WebView2。': 'Could not open the research workspace. Check WebView2.',
  '无法重新打开研究工作台。': 'Could not reopen the research workspace.',
  '无法还原研究工作台。': 'Could not restore the research workspace window.',
  '无法切换到研究工作台。': 'Could not switch to the research workspace.',
  '无法关闭旧研究工作台。': 'Could not close the previous research workspace.',
  '无法确定 WebView2 配置目录。': 'Could not locate the WebView2 profile folder.',
  '无法确认研究服务状态。': 'Could not verify the research service status.',
  '端口无效。': 'Invalid port.',
};

function reportHostError(error) {
  const detail = String(error);
  addLog({ stream: 'stderr', line: detail });
  const message = Object.hasOwn(hostMessages, detail) ? hostMessages[detail] : null;
  return copy(detail, message ?? 'The local operation failed. Open Diagnostics for details.');
}

function run(operation, reportLog = true) {
  const onProgress = new core.Channel();
  if (reportLog) onProgress.onmessage = addLog;
  return core.invoke('desktop_operation', { operation, onProgress });
}

async function perform(operation) {
  if (busy) return;
  if (!core) {
    setResult(copy('请在已安装的 FinDashboard 桌面应用内打开此界面。', 'Open this screen in the installed FinDashboard desktop app.'), true);
    return;
  }
  busy = true;
  const genericOperation = operation.kind === 'client_issue' || operation.kind === 'client_revoke';
  startRecord();
  setResult(withEllipsis(labels[operation.kind]));
  if (operation.kind.startsWith('source_')) {
    showCopy(ui['source-state'], withEllipsis(labels[operation.kind]));
    ui['source-state'].dataset.error = 'false';
  }
  if (genericOperation) showCopy(ui['generic-state'], withEllipsis(labels[operation.kind]));
  reconcile();
  try {
    const result = await run(operation);
    ui['command-text'].value = result.command;
    ui.command.hidden = false;
    ui['diagnostic-details'].hidden = false;
    const summary = `${result.exit_code} · ${result.elapsed_seconds.toFixed(1)} s`;
    showCopy(ui['result-meta'], copy(`退出码 ${summary}`, `Exit code ${summary}`));

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

    if (result.connection) {
      renderGenericConnection(result.connection);
      setResult(copy('已签发通用 MCP 接入配置。', 'MCP connection settings were issued.'));
    } else if (result.workspace) {
      if (operation.open) setResult(copy('已打开本机工作区。', 'Opened the local workspace.'));
      else {
        try {
          await navigator.clipboard.writeText(result.workspace);
          setResult(copy('已复制工作区路径。', 'Workspace path copied.'));
        } catch {
          ui['workspace-address'].value = result.workspace;
          ui['workspace-address'].hidden = false;
          ui['workspace-address'].focus();
          ui['workspace-address'].select();
          setResult(copy('剪贴板不可用。请按 Ctrl+C 复制已选中的路径。', 'Clipboard unavailable. Press Ctrl+C to copy the selected path.'), true);
        }
      }
    } else if (result.source) {
      renderSourceDiagnostic(result.source, operation.kind === 'source_configure');
      setResult(currentCopy.get(ui['source-state']), ui['source-state'].dataset.error === 'true');
    } else if (result.exit_code === 0 && operation.kind === 'status' && !result.status) {
      setResult(copy('安装器没有返回可识别的状态，请刷新后重试。', 'The installer returned an unknown status. Refresh status and try again.'), true);
    } else if (result.exit_code === 0) {
      setResult(done[operation.kind]);
      if (operation.kind === 'client_revoke') showCopy(ui['generic-state'], done.client_revoke);
      if (operation.kind.startsWith('uninstall_')) closePurge();
    } else {
      const message = copy('操作未完成。请查看诊断，处理后可以重试。', 'The operation did not finish. Review Diagnostics and try again.');
      setResult(message, true);
      if (genericOperation) showCopy(ui['generic-state'], message);
    }
  } catch (error) {
    if (operation.kind === 'status') setStatusUnknown();
    const message = reportHostError(error);
    if (operation.kind.startsWith('source_')) {
      showCopy(ui['source-state'], message);
      ui['source-state'].dataset.error = 'true';
      ui['source-details'].hidden = true;
      lastSourceChecks = undefined;
    }
    if (genericOperation) showCopy(ui['generic-state'], message);
    setResult(message, true);
  } finally {
    busy = false;
    reconcile();
  }
}

function setStatusUnknown() {
  status = undefined;
  ui['workbench-address'].hidden = true;
  setWorkbenchStatus(copy('请刷新本机状态，确认服务就绪。', 'Refresh local status to check that the service is ready.'));
  showCopy(ui['service-state'], copy('未确认', 'Unconfirmed'));
  ui['service-state'].dataset.tone = 'warning';
  showCopy(ui['service-detail'], copy('请刷新本机状态。', 'Refresh local status.'));
  showCopy(ui['harness-state'], copy('尚未确认客户端状态。请先刷新本机状态。', 'Client status is unknown. Refresh local status first.'));
  ui.scopes.hidden = true;
  ui['generic-current'].hidden = true;
}

function closePurge() {
  ui['purge-form'].hidden = true;
  ui.confirmation.value = '';
  ui['show-purge'].setAttribute('aria-expanded', 'false');
  reconcile();
}

ui.refresh.addEventListener('click', () => perform({ kind: 'status' }));
ui.port.addEventListener('input', reconcile);
ui.preset.addEventListener('change', reconcile);
ui.harness.addEventListener('change', () => {
  renderHarnessStatus();
  reconcile();
});
ui['harness-profile'].addEventListener('change', reconcile);
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
ui.attach.addEventListener('click', () => {
  const client = ui.harness.value;
  perform({
    kind: 'attach', client, preset: ui.preset.value,
    profile: client === 'dsh' ? ui['harness-profile'].value : null,
  });
});
ui.detach.addEventListener('click', () => perform({ kind: 'detach', client: ui.harness.value }));
ui['workspace-open'].addEventListener('click', () => perform({ kind: 'client_workspace', client: ui.harness.value, open: true }));
ui['workspace-copy'].addEventListener('click', () => perform({ kind: 'client_workspace', client: ui.harness.value, open: false }));
ui['generic-name'].addEventListener('input', reconcile);
ui['generic-list'].addEventListener('change', reconcile);
ui['generic-form'].addEventListener('submit', (event) => {
  event.preventDefault();
  if (ui['generic-issue'].disabled) return;
  perform({ kind: 'client_issue', name: ui['generic-name'].value, preset: ui['generic-preset'].value });
});
ui['generic-revoke'].addEventListener('click', () => perform({ kind: 'client_revoke', name: ui['generic-list'].value }));
ui['generic-close'].addEventListener('click', () => {
  clearGenericConnection();
  showCopy(ui['generic-state'], copy('接入配置已收起。需要新配置时可重新签发。', 'Settings hidden. Issue new settings if needed.'));
  ui['generic-name'].focus();
});
ui['generic-copy'].addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(ui['generic-config'].value);
    clearGenericConnection();
    showCopy(ui['generic-state'], copy('配置已复制。请粘贴到所选客户端。', 'Settings copied. Paste them into your client.'));
    ui['generic-name'].focus();
  } catch {
    ui['generic-config'].focus();
    ui['generic-config'].select();
    showCopy(ui['generic-state'], copy('剪贴板不可用。请按 Ctrl+C 复制已选中的配置，随后点击完成。', 'Clipboard unavailable. Press Ctrl+C to copy the selected settings, then select Done.'));
  }
});
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
    setWorkbenchStatus(copy('已保存默认打开方式。', 'Default opening method saved.'));
  } catch {
    setWorkbenchStatus(copy('无法保存偏好。本次选择仍可使用。', 'Could not save the preference. This session will use your selection.'), true);
  }
});

async function performWorkbench(action) {
  if (busy || !core) return;
  busy = true;
  ui['workbench-address'].hidden = true;
  setWorkbenchStatus(action === 'address' ? copy('正在检查工作台地址…', 'Checking the workspace address…')
    : copy('正在打开研究工作台…', 'Opening the research workspace…'));
  reconcile();
  try {
    const appearance = action === 'in_app' || action === 'release_bundles' ? preferences.appearance() : undefined;
    const address = await core.invoke('desktop_open_workbench', { action, appearance });
    if (action !== 'address') {
      setWorkbenchStatus(action === 'browser' ? copy('已在系统浏览器中打开工作台。', 'Opened the workspace in the system browser.')
        : copy('已在应用内打开工作台。', 'Opened the workspace in the app.'));
      return;
    }
    try {
      await navigator.clipboard.writeText(address);
      setWorkbenchStatus(copy('已复制工作台地址。', 'Workspace address copied.'));
    } catch {
      ui['workbench-address'].value = address;
      ui['workbench-address'].hidden = false;
      ui['workbench-address'].focus();
      ui['workbench-address'].select();
      setWorkbenchStatus(copy('剪贴板不可用。请按 Ctrl+C 复制已选中的地址。', 'Clipboard unavailable. Press Ctrl+C to copy the selected address.'), true);
    }
  } catch (error) {
    setWorkbenchStatus(reportHostError(error), true);
  } finally {
    busy = false;
    reconcile();
  }
}

ui['open-workbench'].addEventListener('click', () => performWorkbench(workbenchTarget()));
ui['open-workbench-alternate'].addEventListener('click', () => performWorkbench(alternateWorkbenchTarget()));
ui['copy-workbench-address'].addEventListener('click', () => performWorkbench('address'));
ui['open-release-bundles'].addEventListener('click', () => performWorkbench('release_bundles'));
ui.copy.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(ui['command-text'].value);
    showCopy(ui.copy, copy('已复制', 'Copied'));
    setTimeout(() => showCopy(ui.copy, copy('复制命令', 'Copy command')), 1200);
  } catch {
    ui['command-text'].select();
    setResult(copy('已选中命令。按 Ctrl+C 复制。', 'Command selected. Press Ctrl+C to copy it.'));
  }
});

reconcile();
if (core) perform({ kind: 'status' });
else setStatusUnknown();

window.__TAURI__?.app?.getVersion?.().then((version) => {
  if (!version) return;
  showCopy(ui['desktop-version'], copy(`管理台版本 ${version}`, `Manager version ${version}`));
  ui['desktop-version'].hidden = false;
}).catch(() => {
  // Version display is optional when the host does not expose package metadata.
});
