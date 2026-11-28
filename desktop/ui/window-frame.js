function mountWindowFrame({ window: kind, origin, recovery, stylesheet }) {
  if (window.top !== window) return;
  if (recovery && location.href === "chrome-error://chromewebdata/") {
    location.replace(recovery);
    return;
  }
  if (location.origin !== origin && location.href !== recovery) return;

  function mount() {
    const core = window.__TAURI__?.core;
    if (!core || document.querySelector("finboard-window-frame")) return;

    const layout = new CSSStyleSheet();
    layout.replaceSync(`
      :root[data-finboard-window] {
        --finboard-window-button-width: 44px;
        --finboard-window-controls-width: calc(3 * var(--finboard-window-button-width));
        --finboard-window-controls-height: 34px;
        --finboard-window-controls-inset: 8px;
        --finboard-window-controls-bottom: calc(var(--finboard-window-controls-inset) + var(--finboard-window-controls-height));
      }
      :root[data-finboard-window="research"] #root > .h-screen > div > header {
        padding-inline-end: calc(1rem + var(--finboard-window-controls-width) + var(--finboard-window-controls-inset));
      }
      @media (max-width: 520px) {
        :root[data-finboard-window="research"] #root > .h-screen > div > header {
          height: auto;
          flex-wrap: wrap;
          gap: .25rem;
          padding-block: .25rem;
          padding-inline-end: 1rem;
        }
        :root[data-finboard-window="research"] #root > .h-screen > div > header > :first-child {
          min-width: 0;
          max-width: calc(100% - var(--finboard-window-controls-width) - var(--finboard-window-controls-inset));
        }
        :root[data-finboard-window="research"] #root > .h-screen > div > header > :first-child > div {
          min-width: 0;
          overflow: hidden;
          white-space: nowrap;
        }
        :root[data-finboard-window="research"] #root > .h-screen > div > header > :last-child {
          width: 100%;
          justify-content: flex-end;
        }
      }
      :root[data-finboard-window="research"] body > .fixed.inset-y-0.h-full {
        height: calc(100dvh - var(--finboard-window-controls-bottom));
        inset-block-start: var(--finboard-window-controls-bottom);
      }
      :root[data-finboard-window="research"] .fixed[role="dialog"][class~="top-[50%]"] {
        inset-block-start: calc(50% + var(--finboard-window-controls-bottom) / 2);
      }
      :root[data-finboard-window="research"] .fixed[role="dialog"][class~="max-h-[90vh]"] {
        max-height: min(90vh, calc(100dvh - var(--finboard-window-controls-bottom) - 16px));
      }
    `);
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, layout];
    document.documentElement.dataset.finboardWindow = kind;

    const frame = document.createElement("finboard-window-frame");
    const shadow = frame.attachShadow({ mode: "open" });
    const chrome = new CSSStyleSheet();
    chrome.replaceSync(stylesheet);
    shadow.adoptedStyleSheets = [chrome];
    shadow.innerHTML = `
      <span class="error" role="status" hidden></span>
      <div class="controls" role="group">
        <button type="button" data-action="minimize"></button>
        <button type="button" data-action="toggle_maximize"></button>
        <button type="button" data-action="close"></button>
      </div>`;
    const controls = shadow.querySelector(".controls");
    const maximize = shadow.querySelector('[data-action="toggle_maximize"]');
    const error = shadow.querySelector(".error");
    const buttons = [...shadow.querySelectorAll("button")];
    const copy = {
      zh: { group: "窗口控制", minimize: "最小化", toggle_maximize: "最大化", restore: "还原", close: "关闭", error: "窗口操作未完成" },
      en: { group: "Window controls", minimize: "Minimize", toggle_maximize: "Maximize", restore: "Restore", close: "Close", error: "Window action failed" },
    };

    function localize() {
      const labels = document.documentElement.lang.toLowerCase().startsWith("en") ? copy.en : copy.zh;
      controls.setAttribute("aria-label", labels.group);
      for (const button of buttons) {
        const action = button === maximize && button.dataset.maximized === "true"
          ? "restore" : button.dataset.action;
        button.setAttribute("aria-label", labels[action]);
        button.title = labels[action];
      }
      error.textContent = labels.error;
    }

    async function perform(action) {
      try {
        const maximized = await core.invoke("desktop_window_action", { action });
        if (typeof maximized === "boolean") {
          maximize.dataset.maximized = String(maximized);
          localize();
        }
        error.hidden = true;
      } catch {
        error.hidden = false;
      }
    }

    for (const button of buttons) {
      button.addEventListener("click", () => perform(button.dataset.action));
    }
    // 窗口控制独立于页面弹层。点击窗口按钮时保留内容区焦点。
    frame.addEventListener("pointerdown", (event) => event.stopPropagation());
    controls.addEventListener("mousedown", (event) => {
      if (event.button === 0) event.preventDefault();
    });
    const header = kind === "research" ? "#root > .h-screen > div > header" : ".topbar";
    document.addEventListener("mousedown", (event) => {
      const target = event.target;
      if (event.buttons !== 1 || !(target instanceof Element) || !target.matches(header)) return;
      perform(event.detail === 2 ? "toggle_maximize" : "start_dragging");
    });
    let resizeTimer;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => perform("is_maximized"), 90);
    });

    localize();
    new MutationObserver(localize).observe(document.documentElement, { attributeFilter: ["lang"] });
    const skipLink = document.querySelector(".skip-link");
    if (skipLink) skipLink.after(frame);
    else document.body.prepend(frame);
    perform("is_maximized");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount, { once: true });
  } else {
    mount();
  }
}
