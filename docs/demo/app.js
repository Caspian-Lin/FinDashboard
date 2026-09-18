const menuButton = document.querySelector(".menu-button");
const navigation = document.querySelector("#site-nav");
const video = document.querySelector("[data-overview-video]");
const videoToggle = document.querySelector("[data-video-toggle]");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

function setVideoState(playing) {
  if (!video || !videoToggle) return;
  videoToggle.textContent = playing ? "暂停总览" : "播放总览";
  videoToggle.setAttribute("aria-pressed", String(playing));
}

if (reducedMotion.matches && video) {
  video.pause();
  setVideoState(false);
}

videoToggle?.addEventListener("click", async () => {
  if (!video) return;
  if (video.paused) {
    await video.play();
    setVideoState(true);
  } else {
    video.pause();
    setVideoState(false);
  }
});

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
