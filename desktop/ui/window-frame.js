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
        --finboard-titlebar-height: 36px;
        scroll-padding-block-start: var(--finboard-titlebar-height);
      }
      :root[data-finboard-window] body {
        padding-block-start: var(--finboard-titlebar-height);
      }
      :root[data-finboard-window="research"] #root > .h-screen,
      :root[data-finboard-window="research"] body > .fixed.inset-y-0.h-full {
        height: calc(100dvh - var(--finboard-titlebar-height));
      }
      :root[data-finboard-window="research"] body > .fixed.inset-y-0.h-full {
        inset-block-start: var(--finboard-titlebar-height);
      }
      :root[data-finboard-window="research"] .fixed[role="dialog"][class~="top-[50%]"] {
        inset-block-start: calc(50% + var(--finboard-titlebar-height) / 2);
      }
      :root[data-finboard-window="research"] .fixed[role="dialog"][class~="max-h-[90vh]"] {
        max-height: min(90vh, calc(100dvh - var(--finboard-titlebar-height) - 16px));
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
      <div class="drag"><span class="title"></span></div>
      <span class="error" role="status" hidden></span>
      <div class="controls" role="group" aria-label="窗口控制">
        <button type="button" data-action="minimize" aria-label="最小化" title="最小化"></button>
        <button type="button" data-action="toggle_maximize" aria-label="最大化" title="最大化"></button>
        <button type="button" data-action="close" aria-label="关闭" title="关闭"></button>
      </div>`;
    shadow.querySelector(".title").textContent =
      kind === "research" ? "FinDashboard 研究工作台" : "FinDashboard 研究版";
    const maximize = shadow.querySelector('[data-action="toggle_maximize"]');
    const error = shadow.querySelector(".error");

    async function perform(action) {
      try {
        const maximized = await core.invoke("desktop_window_action", { action });
        if (typeof maximized === "boolean") {
          const label = maximized ? "还原" : "最大化";
          maximize.dataset.maximized = String(maximized);
          maximize.setAttribute("aria-label", label);
          maximize.title = label;
        }
        error.hidden = true;
      } catch {
        error.textContent = "窗口操作未完成";
        error.hidden = false;
      }
    }

    for (const button of shadow.querySelectorAll("button")) {
      button.addEventListener("click", () => perform(button.dataset.action));
    }
    // 窗口控制独立于页面弹层。点击窗口按钮时保留内容区焦点。
    frame.addEventListener("pointerdown", (event) => event.stopPropagation());
    shadow.querySelector(".controls").addEventListener("mousedown", (event) => {
      if (event.button === 0) event.preventDefault();
    });
    shadow.querySelector(".drag").addEventListener("mousedown", (event) => {
      if (event.buttons !== 1) return;
      perform(event.detail === 2 ? "toggle_maximize" : "start_dragging");
    });
    let resizeTimer;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => perform("is_maximized"), 90);
    });

    document.body.prepend(frame);
    perform("is_maximized");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount, { once: true });
  } else {
    mount();
  }
}
