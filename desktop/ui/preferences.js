(() => {
  const themeKey = 'finboard-desktop-theme';
  const localeKey = 'finboard-desktop-language';
  const root = document.documentElement;
  const english = {
    "appTitle": "FinDashboard Research Manager",
    "managerBrand": "Manager",
    "skip": "Skip to main content",
    "appearance": "Appearance preferences",
    "language": "Interface language",
    "agents": "Agent connections",
    "diagnostics": "Diagnostics",
    "pageTitle": "Research Manager",
    "refresh": "Refresh status",
    "service": "Research service",
    "checking": "Checking",
    "checkDetail": "Checking Docker Desktop and the research service.",
    "port": "Local port",
    "install": "Install and start",
    "stop": "Stop",
    "workbench": "Research workspace",
    "workbenchDetail": "View research topics, strategies, and frozen runs.",
    "openInApp": "Open in app",
    "workbenchOptions": "Opening options and address",
    "openBrowser": "Open in browser",
    "copyAddress": "Copy address",
    "defaultOpen": "Default opening method",
    "inApp": "In app",
    "browser": "System browser",
    "workbenchStopped": "Start the research service to open the workspace.",
    "workbenchAddress": "Research workspace address",
    "dataTitle": "Data and releases",
    "sample": "Synthetic sample",
    "sampleDetail": "Import the bundled offline sample and inspect it under Data.",
    "sampleImport": "Import sample",
    "bundles": "Frozen release bundles",
    "source": "Tushare data source",
    "sourceWaiting": "Permission checks are available once the service is ready.",
    "sourceToken": "Tushare token",
    "sourceSave": "Save and restart service",
    "sourceProbe": "Check permissions",
    "sourceDetails": "Endpoint check results",
    "harnessTitle": "Research clients",
    "harnessWaiting": "Connect a client once the service is ready.",
    "client": "Client",
    "dshProfile": "DSH profile",
    "preset": "Permission preset",
    "readonly": "Read only",
    "topicWriter": "Topics and research runs",
    "attach": "Connect and verify",
    "detach": "Disconnect",
    "workspaceOpen": "Open client workspace",
    "workspaceCopy": "Copy workspace path",
    "workspaceAddress": "Client workspace path",
    "generic": "Generic MCP clients",
    "clientName": "Client name",
    "genericIssue": "Issue connection settings",
    "genericHint": "Copy the settings once. Issuing settings for the same name revokes its old credential.",
    "genericList": "Issued clients",
    "genericRevoke": "Revoke selected credential",
    "genericConfiguration": "New connection settings",
    "genericCopy": "Copy settings",
    "done": "Done",
    "diagnosticHint": "Operation results appear here.",
    "operationProgress": "Operation in progress",
    "diagnosticLog": "Logs and command",
    "diagnosticLogLabel": "Redacted operation log",
    "command": "Equivalent PowerShell command",
    "commandCopy": "Copy command",
    "maintenanceTitle": "Installation and removal",
    "preserveHint": "Keep data removes this instance's services and client connections and keeps its database, research files, and recovery settings.",
    "preserve": "Uninstall and keep research data",
    "purge": "Remove everything…",
    "purgeHint": "This deletes this instance’s database and research volumes, settings, credentials, and logs. Back up anything you need first.",
    "purgeConfirmation": "Type the confirmation phrase",
    "purgeConfirm": "Delete this instance and its research data",
    "cancel": "Cancel",
    "unavailableTitle": "Research workspace unavailable",
    "unavailableHeading": "Research service is unavailable",
    "unavailableDetail": "Restore the service in Research Manager, then open the workspace again."
  };
  const listeners = new Set();

  function load(key) {
    try { return localStorage.getItem(key); } catch { return null; }
  }

  function save(key, value) {
    try { localStorage.setItem(key, value); } catch { /* apply for this session */ }
  }

  const recoveryAppearance = window.finboardDesktopRecoveryAppearance;
  let theme = (recoveryAppearance?.theme ?? load(themeKey)) === 'light' ? 'light' : 'dark';
  let language = (recoveryAppearance?.language ?? load(localeKey)) === 'en' ? 'en' : 'zh';

  function text(message) {
    return typeof message === 'string' ? message : message?.[language] ?? '';
  }

  function applyRoot() {
    root.classList.toggle('light', theme === 'light');
    root.lang = language === 'en' ? 'en' : 'zh-CN';
  }

  const originalText = new WeakMap();
  const originalLabels = new WeakMap();

  function updatePage() {
    for (const node of document.querySelectorAll('[data-l10n]')) {
      if (!originalText.has(node)) originalText.set(node, node.textContent);
      node.textContent = language === 'en' ? english[node.dataset.l10n] : originalText.get(node);
    }
    for (const node of document.querySelectorAll('[data-l10n-label]')) {
      if (!originalLabels.has(node)) originalLabels.set(node, node.getAttribute('aria-label'));
      node.setAttribute('aria-label', language === 'en'
        ? english[node.dataset.l10nLabel] : originalLabels.get(node));
    }
  }

  function updateThemeControl() {
    const toggle = document.getElementById('desktop-theme');
    if (!toggle) return;
    const msg = theme === 'dark'
      ? { zh: '切换到浅色主题', en: 'Switch to light theme' }
      : { zh: '切换到深色主题', en: 'Switch to dark theme' };
    toggle.setAttribute('aria-label', text(msg));
    toggle.title = text(msg);
  }

  applyRoot();
  window.finboardDesktopPreferences = {
    text,
    appearance() { return { theme, language }; },
    refreshCopy(listener) { listeners.add(listener); },
  };
  document.addEventListener('DOMContentLoaded', () => {
    updatePage();
    updateThemeControl();
    const chooser = document.getElementById('desktop-language');
    if (chooser) {
      chooser.value = language;
      chooser.addEventListener('change', () => {
        language = chooser.value === 'en' ? 'en' : 'zh';
        save(localeKey, language);
        applyRoot();
        updatePage();
        for (const update of listeners) update();
        updateThemeControl();
      });
    }
    document.getElementById('desktop-theme')?.addEventListener('click', () => {
      theme = theme === 'dark' ? 'light' : 'dark';
      save(themeKey, theme);
      applyRoot();
      updateThemeControl();
    });
  }, { once: true });
})();
