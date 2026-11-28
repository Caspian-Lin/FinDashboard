const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const root = path.resolve(__dirname, '../..');
const desktop = readFileSync(path.join(root, 'desktop/ui/styles.css'), 'utf8');
const web = readFileSync(path.join(root, 'web/src/index.css'), 'utf8');
const sharedTokens = [
  'background', 'foreground', 'card', 'primary', 'primary-foreground', 'muted', 'muted-foreground',
  'accent', 'destructive', 'success', 'warning', 'border', 'input', 'ring', 'radius', 'radius-compact',
];

function tokens(stylesheet, selector, inherited = {}) {
  const block = stylesheet.match(selector)?.[1];
  assert.ok(block, 'The theme selector must exist');
  return { ...inherited, ...Object.fromEntries(
    [...block.matchAll(/--([\w-]+):\s*([^;]+);/g)].map(([, name, value]) => [name, value.trim()]),
  ) };
}

test('the native manager uses the Web palette and corner radii in both themes', () => {
  const desktopDark = tokens(desktop, /^:root\s*\{([^}]+)}/m);
  const webDark = tokens(web, /^\s*:root\s*\{([^}]+)}/m);
  const desktopLight = tokens(desktop, /^:root\.light\s*\{([^}]+)}/m, desktopDark);
  const webLight = tokens(web, /^\s*\.light\s*\{([^}]+)}/m, webDark);
  for (const [name, native, browser] of [['dark', desktopDark, webDark], ['light', desktopLight, webLight]]) {
    for (const token of sharedTokens) {
      assert.ok(browser[token], `Web must define ${name} --${token}`);
      assert.equal(native[token], browser[token], `Shared ${name} --${token}`);
    }
  }
});
