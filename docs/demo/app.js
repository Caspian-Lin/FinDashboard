const menuButton = document.querySelector(".menu-button");
const navigation = document.querySelector("#site-nav");
const scrolly = document.querySelector("[data-scrolly]");

menuButton?.addEventListener("click", () => {
  const open = menuButton.getAttribute("aria-expanded") !== "true";
  menuButton.setAttribute("aria-expanded", String(open));
  navigation?.setAttribute("data-open", String(open));
});

navigation?.addEventListener("click", (event) => {
  if (!(event.target instanceof HTMLAnchorElement)) return;
  menuButton?.setAttribute("aria-expanded", "false");
  navigation.removeAttribute("data-open");
});

if (scrolly) {
  const steps = [...scrolly.querySelectorAll("[data-step]")];
  const scenes = [...scrolly.querySelectorAll("[data-scene]")];
  const path = scrolly.querySelector("[data-stage-path]");
  const count = scrolly.querySelector("[data-stage-count]");
  const title = scrolly.querySelector("[data-stage-title]");
  const progress = scrolly.querySelector("[data-stage-progress]");
  let activeIndex = -1;

  function activate(index) {
    if (index === activeIndex || index < 0 || index >= steps.length) return;
    activeIndex = index;
    steps.forEach((step, stepIndex) => step.classList.toggle("is-active", stepIndex === index));
    scenes.forEach((scene, sceneIndex) => scene.classList.toggle("is-active", sceneIndex === index));
    const step = steps[index];
    if (path) path.textContent = step.dataset.path || "";
    if (title) title.textContent = step.dataset.title || "";
    if (count) count.textContent = `${String(index + 1).padStart(2, "0")} / ${String(steps.length).padStart(2, "0")}`;
    if (progress instanceof HTMLElement) progress.style.width = `${((index + 1) / steps.length) * 100}%`;
  }

  let framePending = false;
  function updateFromScroll() {
    framePending = false;
    const viewportCenter = window.innerHeight / 2;
    const closest = steps.reduce(
      (best, step, index) => {
        const rect = step.getBoundingClientRect();
        const distance = Math.abs(rect.top + rect.height / 2 - viewportCenter);
        return distance < best.distance ? { index, distance } : best;
      },
      { index: 0, distance: Number.POSITIVE_INFINITY },
    );
    activate(closest.index);
  }

  function requestUpdate() {
    if (framePending) return;
    framePending = true;
    window.requestAnimationFrame(updateFromScroll);
  }

  window.addEventListener("scroll", requestUpdate, { passive: true });
  window.addEventListener("resize", requestUpdate);
  updateFromScroll();
}
