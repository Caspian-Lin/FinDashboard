const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const ui = path.join(__dirname, '..', 'ui');
const source = readFileSync(path.join(ui, 'app.js'), 'utf8');
const page = readFileSync(path.join(ui, 'index.html'), 'utf8');

function element(id) {
  return {
    id, value: '', dataset: {}, handlers: {},
    setAttribute() {}, focus() {},
    select() { this.selected = true; },
    replaceChildren(...children) { this.children = children; },
    addEventListener(type, listener) { this.handlers[type] = listener; },
  };
}

async function loadWorkbench({
  storage = new Map(), running = true, clipboardDenied = false, sourceDiagnostic, connection,
  profiles = [], clients = [],
} = {}) {
  const nodes = new Map(Array.from(page.matchAll(/\bid="([^"]+)"/g), ([, id]) => [id, element(id)]));
  nodes.get('workbench-target').value = 'in_app';
  nodes.get('harness').value = 'codex';
  nodes.get('generic-preset').value = 'readonly';
  const calls = [];
  const copies = [];
  let port = 8376;
  let launchError;
  vm.runInNewContext(source, {
    document: {
      querySelectorAll(selector) { return selector === '[id]' ? Array.from(nodes.values()) : []; },
      createElement: element,
    },
    window: {
      addEventListener() {},
      __TAURI__: { core: {
        Channel: class {},
        invoke: async (command, args) => {
          calls.push([command, args]);
          if (command === 'desktop_open_workbench') {
            if (launchError) throw launchError;
            return args.action === 'address' ? `http://127.0.0.1:${port}/research` : null;
          }
          if (args.operation.kind.startsWith('source_')) {
            return { exit_code: 0, elapsed_seconds: 0, command: 'source-operation', source: sourceDiagnostic };
          }
          if (args.operation.kind === 'client_issue') {
            return { exit_code: 0, elapsed_seconds: 0, command: 'desktop-client-issue', connection };
          }
          return {
            exit_code: 0, elapsed_seconds: 0, command: 'status',
            status: { running, installed: true, docker: 'linux', port, scopes: [], dsh_profiles: profiles, clients },
          };
        },
      } },
    },
    localStorage: {
      getItem(key) { return storage.get(key); },
      setItem(key, value) { storage.set(key, value); },
    },
    navigator: { clipboard: {
      async writeText(address) {
        if (clipboardDenied) throw new Error('clipboard denied');
        copies.push(address);
      },
    } },
    location: { hash: '' },
  });
  await new Promise(setImmediate);
  return {
    calls, copies, nodes,
    setPort(value) { port = value; },
    rejectLaunch(message) { launchError = message; },
    click(id) {
      const element = nodes.get(id);
      if (!element.disabled) return element.handlers.click();
    },
  };
}

test('defaults to in-app opening and keeps the saved browser preference on reload', async () => {
  const storage = new Map();
  const session = await loadWorkbench({ storage });
  await session.click('open-workbench');
  assert.equal(session.calls.at(-1)[1].action, 'in_app');

  session.nodes.get('workbench-target').value = 'browser';
  session.nodes.get('workbench-target').handlers.change();
  const reloaded = await loadWorkbench({ storage });
  await reloaded.click('open-workbench');
  assert.equal(reloaded.calls.at(-1)[1].action, 'browser');
  await reloaded.click('open-workbench-alternate');
  assert.equal(reloaded.calls.at(-1)[1].action, 'in_app');
});

test('copies the fresh native address and offers a selected address on clipboard denial', async () => {
  const session = await loadWorkbench();
  session.setPort(9184);
  await session.click('copy-workbench-address');
  assert.deepEqual(session.copies, ['http://127.0.0.1:9184/research']);
  const denied = await loadWorkbench({ clipboardDenied: true });
  await denied.click('copy-workbench-address');
  assert.equal(denied.nodes.get('workbench-address').value, 'http://127.0.0.1:8376/research');
  assert.equal(denied.nodes.get('workbench-address').selected, true);
});

test('stopped services disable the workbench and native launch errors stay actionable', async () => {
  const stopped = await loadWorkbench({ running: false });
  await stopped.click('open-workbench');
  assert.equal(stopped.calls.length, 1);
  const ready = await loadWorkbench();
  ready.rejectLaunch('请检查默认浏览器，或复制工作台地址。');
  await ready.click('open-workbench-alternate');
  assert.match(ready.nodes.get('workbench-status').textContent, /复制工作台地址/);
  assert.equal(ready.nodes.get('workbench-status').dataset.error, 'true');
});

test('Tushare permission probe displays named outcomes without exposing credentials', async () => {
  const session = await loadWorkbench({ sourceDiagnostic: {
    saved: false, restart: 'not_requested',
    capabilities: {
      daily: 'available', adj_factor: 'permission_denied', index_daily: 'connection_failed',
      daily_basic: 'quota_exhausted', fina_indicator: 'coverage_insufficient',
    },
  } });
  await session.click('source-probe');
  const labels = session.nodes.get('source-capabilities').children.map((node) => node.textContent);
  assert.deepEqual(Array.from(labels), [
    '股票日线', '可用', '复权因子', '权限不足', '指数日线', '网络或服务不可用',
    '每日指标', '已达配额', '财务指标', '样本无数据',
  ]);
  assert.equal(session.nodes.get('source-details').open, true);
});

test('Tushare configuration distinguishes a saved credential from a failed restart', async () => {
  const session = await loadWorkbench({ sourceDiagnostic: {
    saved: true, restart: 'failed', capabilities: { daily: 'available', trade_cal: 'available' },
  } });
  const input = session.nodes.get('source-token');
  input.value = 'fake-tushare-token';
  input.handlers.input();
  session.nodes.get('source-form').handlers.submit({ preventDefault() {} });
  await new Promise(setImmediate);
  assert.equal(input.value, '');
  const command = session.calls.find(([, args]) => args.operation?.kind === 'source_configure');
  assert.equal(command[1].operation.token, 'fake-tushare-token');
  assert.match(session.nodes.get('source-state').textContent, /已保存.*重启未确认/);
  assert.equal(session.nodes.get('source-state').dataset.error, 'true');
  assert.doesNotMatch(session.nodes.get('command-text').value, /fake-tushare-token/);
});

test('each harness restores its own preset and sends the selected DSH profile', async () => {
  const session = await loadWorkbench({
    profiles: ['desktop', 'research-team'],
    clients: [{ name: 'codex', preset: 'topic-writer', scopes: ['research.read', 'research.run'], credential_active: true }],
  });
  assert.equal(session.nodes.get('preset').value, 'topic-writer');
  session.nodes.get('harness').value = 'dsh';
  session.nodes.get('harness').handlers.change();
  assert.equal(session.nodes.get('preset').value, 'readonly');
  session.nodes.get('harness-profile').value = 'research-team';
  await session.click('attach');
  const operation = session.calls.find(([, args]) => args.operation?.kind === 'attach')[1].operation;
  assert.equal(operation.client, 'dsh');
  assert.equal(operation.profile, 'research-team');
});

test('generic contact configuration is removed after the first successful copy', async () => {
  const session = await loadWorkbench({ connection: {
    name: 'reports', url: 'http://127.0.0.1:8376/mcp', credential: 'one-time-fixture-token', scopes: ['research.read'],
  } });
  session.nodes.get('generic-name').value = 'reports';
  session.nodes.get('generic-name').handlers.input();
  session.nodes.get('generic-form').handlers.submit({ preventDefault() {} });
  await new Promise(setImmediate);
  assert.equal(session.nodes.get('generic-connection').hidden, false);
  assert.doesNotMatch(session.nodes.get('command-text').value, /one-time-fixture-token/);
  await session.click('generic-copy');
  assert.match(session.copies[0], /Bearer one-time-fixture-token/);
  assert.equal(session.nodes.get('generic-config').value, '');
  assert.equal(session.nodes.get('generic-connection').hidden, true);
});

test('bundle control opens the native fixed destination only when ready', async () => {
  const running = await loadWorkbench();
  await running.click('open-release-bundles');
  assert.equal(running.calls.at(-1)[0], 'desktop_open_workbench');
  assert.equal(running.calls.at(-1)[1].action, 'release_bundles');
  assert.deepEqual(Object.keys(running.calls.at(-1)[1]), ['action']);

  const stopped = await loadWorkbench({ running: false });
  await stopped.click('open-release-bundles');
  assert.equal(stopped.calls.length, 1);
});
