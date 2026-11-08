/* 招标雷达 — 首页交互
   只做两件事：主题切换、把真实数据填进 Hero 的三个数字。

   Hero 刻意用 /api/health 这个公开接口：没登录的访客也该看到「库里有多少条、
   接了几个源」，而「7 天内截止」这类明细要登录才给——首页不为了好看去调
   需要鉴权的接口。 */

(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const THEME_KEY = "tr-theme";
  const DEFAULT_PREF = "dark";
  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
  const resolveTheme = (pref) =>
    pref === "auto" ? (darkQuery.matches ? "dark" : "light") : pref;

  function applyTheme(pref) {
    const mode = resolveTheme(pref);
    document.documentElement.setAttribute("data-theme", mode);
    document.documentElement.style.colorScheme = mode;
    document.querySelectorAll("#themeSwitch button").forEach((b) =>
      b.classList.toggle("active", b.dataset.theme === pref)
    );
  }

  const currentPref = () => {
    try {
      return localStorage.getItem(THEME_KEY) || DEFAULT_PREF;
    } catch (_) {
      return DEFAULT_PREF;
    }
  };

  function setTheme(pref) {
    try {
      localStorage.setItem(THEME_KEY, pref);
    } catch (_) {}
    applyTheme(pref);
  }

  const sw = $("#themeSwitch");
  if (sw) {
    sw.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-theme]");
      if (b) setTheme(b.dataset.theme);
    });
  }
  darkQuery.addEventListener("change", () => {
    if (currentPref() === "auto") applyTheme("auto");
  });
  applyTheme(currentPref());

  /* ── 真实数据：来自 /api/health ─────────────────
     拿不到就整块隐藏——首页上放三个「—」比放三个 0 难看，
     但比放三个假数字诚实。 */
  function relTime(iso) {
    if (!iso) return "—";
    const t = new Date(iso);
    if (isNaN(t.getTime())) return "—";
    const mins = Math.floor((Date.now() - t.getTime()) / 60000);
    if (mins < 1) return "刚刚";
    if (mins < 60) return `${mins} 分钟前`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs} 小时前`;
    return `${Math.floor(hrs / 24)} 天前`;
  }

  fetch("/api/health")
    .then((r) => (r.ok ? r.json() : Promise.reject()))
    .then((h) => {
      $("#sTotal").textContent = (h.total || 0).toLocaleString("zh-CN");
      $("#sSources").textContent = h.sources || 0;
      $("#sUpdated").textContent = relTime(h.last_run_at);
    })
    .catch(() => {
      const box = $("#heroStats");
      if (box) box.hidden = true;
    });
})();
