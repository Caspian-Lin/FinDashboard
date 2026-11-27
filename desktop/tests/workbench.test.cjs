const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const ui = path.join(__dirname, '..', 'ui');
const source = readFileSync(path.join(ui, 'app.js'), 'utf8');
const page = readFileSync(path.join(ui, 'index.html'), 'utf8');

async function loadWorkbench({ storage = new Map(), running = true, clipboardDenied = false } = {}) {
  const nodes = new Map(Array.from(page.matchAll(/\bid="([^"]+)"/g), ([, id]) => [id, {
    id, value: '', dataset: {}, handlers: {},
    setAttribute() {}, focus() {},
    select() { this.selected = true; },
    addEventListener(type, listener) { this.handlers[type] = listener; },
  }]));
  nodes.get('workbench-target').value = 'in_app';
  const calls = [];
  const copies = [];
  let port = 8376;
  let launchError;
  vm.runInNewContext(source, {
    document: {
      querySelectorAll(selector) { return selector === '[id]' ? Array.from(nodes.values()) : []; },
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
          return {
            exit_code: 0, elapsed_seconds: 0, command: 'status',
            status: { running, installed: true, docker: 'linux', port, scopes: [] },
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
