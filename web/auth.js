/* 招标雷达 — 账号与登录（首页与应用页共用）
   零依赖。对外暴露 window.TRAuth：

     TRAuth.token()            当前令牌（没有则空串）
     TRAuth.user()             当前用户信息（没有则 null）
     TRAuth.ready()            Promise<user|null>，页面启动后先等它
     TRAuth.api(path, opts)    带 Authorization 的请求，401 自动登出
     TRAuth.openLogin(opts)    打开登录弹窗
     TRAuth.logout()           登出
     TRAuth.onChange(fn)       登录态变化回调

   两个刻意的设计：
   1. 登录做成弹窗而不是独立页面——首页要能「看完介绍顺手登录」，
      应用页要能「会话过期当场续上」，两处复用同一套。
   2. 应用页调用 requireLogin()，登录前整页遮罩，不给半登录状态，
      避免出现「页面在但数据 401」的半成品界面。 */

(() => {
  "use strict";

  const TOKEN_KEY = "tr-token";
  const VID_KEY = "tr-vid";
  const API = "/api";

  let currentUser = null;
  const listeners = [];

  /* ── 存储：localStorage 可能被隐私模式禁用，全部包一层 try ── */
  const store = {
    get(k) {
      try {
        return localStorage.getItem(k) || "";
      } catch (_) {
        return "";
      }
    },
    set(k, v) {
      try {
        localStorage.setItem(k, v);
      } catch (_) {}
    },
    del(k) {
      try {
        localStorage.removeItem(k);
      } catch (_) {}
    },
  };

  const token = () => store.get(TOKEN_KEY);
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

  /* ── 匿名访客 id：算 UV 用。取不到 cookie 就退化成一次性随机值，
        最差只是 UV 偏高，不影响 PV 与功能。 ── */
  function visitorId() {
    let v = store.get(VID_KEY);
    if (!v) {
      v =
        Date.now().toString(36) +
        Math.random().toString(36).slice(2, 10) +
        Math.random().toString(36).slice(2, 10);
      store.set(VID_KEY, v);
    }
    return v;
  }

  function emit() {
    // 遍历副本：回调里会把监听器摘掉（requireLogin 就是这么用的），
    // 直接遍历原数组会导致后一个监听器被跳过。
    listeners.slice().forEach((fn) => {
      try {
        fn(currentUser);
      } catch (_) {}
    });
  }

  /* ── 带鉴权的请求 ────────────────────────────────
     401 的统一处理放在这里：清令牌 + 通知页面，而不是让每个调用点
     各写一遍「如果没登录就跳首页」。 */
  async function api(path, opts = {}) {
    const headers = Object.assign({}, opts.headers || {});
    const t = token();
    if (t) headers["Authorization"] = "Bearer " + t;
    if (opts.json !== undefined) headers["Content-Type"] = "application/json";

    const body = opts.json !== undefined ? JSON.stringify(opts.json) : opts.body;
    const res = await fetch(API + path, {
      method: opts.method || (body !== undefined ? "POST" : "GET"),
      headers,
      body,
    });

    if (res.status === 401) {
      store.del(TOKEN_KEY);
      currentUser = null;
      emit();
      throw new Error("登录已失效，请重新登录");
    }
    if (!res.ok) {
      let d = res.statusText;
      try {
        const j = await res.json();
        d = j.detail || d;
      } catch (_) {}
      throw new Error(d);
    }
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res.text();
  }

  /* ── 登录弹窗 ──────────────────────────────────── */
  let modal = null;

  function buildModal() {
    const wrap = document.createElement("div");
    wrap.className = "auth-mask";
    wrap.innerHTML = `
      <div class="auth-card" role="dialog" aria-modal="true" aria-label="登录">
        <button class="auth-x" type="button" aria-label="关闭">×</button>
        <div class="auth-head">
          <div class="auth-mark" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
                 stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="2.2" fill="currentColor" stroke="none"/>
              <path d="M12 10V4.5M13.7 12.9 18 15.4M10.3 12.9 6 15.4"/>
              <path d="M3.4 9.2a9.5 9.5 0 0 0-1.4 4.9 10 10 0 1 0 13.4-9.4" opacity=".55"/>
            </svg>
          </div>
          <h3>登录招标雷达</h3>
          <p>输入邮箱，收一个 6 位验证码即可登录。<br>不用注册，也不设密码。</p>
        </div>

        <div class="auth-dev" id="authDev" hidden></div>

        <label class="auth-label" for="authEmail">邮箱</label>
        <input class="auth-input" id="authEmail" type="email" autocomplete="email"
               placeholder="you@company.com" spellcheck="false">

        <label class="auth-label" for="authCode">验证码</label>
        <div class="auth-code-row">
          <input class="auth-input" id="authCode" type="text" inputmode="numeric"
                 autocomplete="one-time-code" maxlength="6" placeholder="6 位数字"
                 spellcheck="false">
          <button class="btn btn-sm" id="authSend" type="button">获取验证码</button>
        </div>

        <div class="auth-err" id="authErr" hidden></div>

        <button class="btn btn-primary auth-submit" id="authSubmit" type="button">登录</button>

        <p class="auth-foot">
          登录即表示同意<a href="legal/user-agreement.html">用户协议</a>与<a href="legal/privacy-policy.html">隐私政策</a>。
        </p>
      </div>`;
    document.body.appendChild(wrap);
    return wrap;
  }

  function closeLogin() {
    if (!modal) return;
    modal.classList.remove("show");
  }

  function err(msg) {
    const box = modal.querySelector("#authErr");
    if (!msg) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    box.classList.remove("is-ok");
    box.textContent = msg;
  }

  /** 复用错误框显示成功提示（「验证码已发送」），省一套 DOM。 */
  function ok(msg) {
    const box = modal.querySelector("#authErr");
    if (!msg) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    box.classList.add("is-ok");
    box.textContent = msg;
  }

  /** 打开登录弹窗。mandatory=true 时不允许点遮罩关闭（应用页门禁用）。 */
  function openLogin(opts = {}) {
    if (!modal) {
      modal = buildModal();
      bindModal();
    }
    modal.dataset.mandatory = opts.mandatory ? "1" : "";
    modal.querySelector(".auth-x").hidden = !!opts.mandatory;
    modal.classList.add("show");
    err("");
    const target = opts.redirect || "";
    modal.dataset.redirect = target;
    setTimeout(() => modal.querySelector("#authEmail").focus(), 60);
  }

  function afterLogin(user) {
    currentUser = user;
    emit();
    closeLogin();
    const to = modal && modal.dataset.redirect;
    if (to) {
      window.location.href = to;
    } else if (window.location.pathname.endsWith("index.html") || window.location.pathname === "/") {
      // 首页登录后直接进控制台，别把人留在介绍页
      window.location.href = "app.html";
    } else {
      // 应用页：原地刷新一次，让各视图按登录态重新拉数据
      window.location.reload();
    }
  }

  function bindModal() {
    const email = modal.querySelector("#authEmail");
    const code = modal.querySelector("#authCode");
    const send = modal.querySelector("#authSend");
    const submit = modal.querySelector("#authSubmit");
    const dev = modal.querySelector("#authDev");
    let tick = null;

    modal.querySelector(".auth-x").onclick = closeLogin;
    modal.addEventListener("mousedown", (e) => {
      if (e.target === modal && !modal.dataset.mandatory) closeLogin();
    });

    // 邮箱框回车 → 要验证码（顺手就把码要了，少点一次）
    email.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        if (!send.disabled) send.click();
        else code.focus();
      }
    });
    // 验证码框回车 → 直接登录
    code.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        submit.click();
      }
    });
    code.addEventListener("input", () => {
      code.value = code.value.replace(/\D/g, "").slice(0, 6);
    });

    function countdown(sec) {
      let left = sec;
      send.disabled = true;
      const step = () => {
        if (left <= 0) {
          clearInterval(tick);
          tick = null;
          send.disabled = false;
          send.textContent = "重新获取";
          return;
        }
        send.textContent = `${left}s`;
        left -= 1;
      };
      step();
      tick = setInterval(step, 1000);
    }

    send.onclick = async () => {
      const v = email.value.trim();
      if (!v) return err("请先填写邮箱");
      send.disabled = true;
      send.textContent = "发送中…";
      try {
        const r = await api("/auth/code", { json: { email: v } });
        // SMTP 没配时后端会把验证码回显出来，本地联调用得上
        if (r.channel === "dev" && r.dev_code) {
          dev.hidden = false;
          dev.textContent =
            `开发模式：SMTP 未配置，邮件没真发出去。你的验证码是 ${r.dev_code}`;
        } else {
          dev.hidden = true;
          dev.textContent = "";
        }
        ok(`验证码已发送至 ${v}`);
        countdown(r.interval || 60);
        code.focus();
      } catch (e) {
        send.disabled = false;
        send.textContent = "获取验证码";
        err(e.message);
      }
    };

    submit.onclick = async () => {
      const v = email.value.trim();
      const c = code.value.trim();
      if (!v) return err("请先填写邮箱");
      if (!c) return err("请先获取验证码并填写");
      submit.disabled = true;
      submit.textContent = "登录中…";
      try {
        const r = await api("/auth/login", { json: { email: v, code: c } });
        store.set(TOKEN_KEY, r.token);
        afterLogin(r.user);
      } catch (e) {
        submit.disabled = false;
        submit.textContent = "登录";
        err(e.message);
      }
    };
  }

  async function logout() {
    try {
      await api("/auth/logout", { method: "POST" });
    } catch (_) {}
    store.del(TOKEN_KEY);
    currentUser = null;
    emit();
    // 应用页登出后回首页；首页登出就留在原地。
    // 用 "./" 而不是 "index.html"：反代到域名根路径时，地址栏保持干净的
    // https://your.domain/，也不会和静态目录里的文件名绑死。
    if (document.body.dataset.page === "app") window.location.href = "./";
    else window.location.reload();
  }

  /* ── 应用页门禁 ──────────────────────────────────
     刻意「先给页面、再给弹窗」：直接甩一个登录框在纯黑背景上，
     访客不知道自己在登录什么。先看到一句人话的解释，再点登录。 */
  function showGate() {
    let g = document.getElementById("gate");
    if (!g) {
      g = document.createElement("div");
      g.id = "gate";
      g.innerHTML = `
        <div class="gate-box">
          <div class="auth-mark" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
                 stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="2.2" fill="currentColor" stroke="none"/>
              <path d="M12 10V4.5M13.7 12.9 18 15.4M10.3 12.9 6 15.4"/>
              <path d="M3.4 9.2a9.5 9.5 0 0 0-1.4 4.9 10 10 0 1 0 13.4-9.4" opacity=".55"/>
            </svg>
          </div>
          <h2>登录后查看招标线索</h2>
          <p>看板、雷达与来源管理需要登录后访问。<br>邮箱验证码登录，不用注册密码。</p>
          <ul class="gate-points">
            <li>多源公告去重后按截止日排序</li>
            <li>预算、截止时间自动抽取</li>
            <li>公测期间免费，不限注册</li>
          </ul>
          <button class="btn btn-primary" id="gateBtn">登录 / 注册</button>
          <a class="gate-back" href="index.html">← 回首页看产品介绍</a>
        </div>`;
      document.body.appendChild(g);
      g.querySelector("#gateBtn").onclick = () => openLogin();
    }
    g.classList.add("show");
  }

  function hideGate() {
    const g = document.getElementById("gate");
    if (g) g.classList.remove("show");
  }

  /* ── 顶栏账号区 ────────────────────────────────── */
  function renderAccount() {
    const slot = document.getElementById("accountSlot");
    if (!slot) return;
    // 首页的登录按钮带上 redirect，登录完直接进控制台；
    // 控制台自己不需要，登录后原地刷新即可。
    const to = slot.dataset.redirect || "";
    const toAttr = to ? ` data-redirect="${to}"` : "";
    if (!currentUser) {
      slot.innerHTML = `<button class="btn ${to ? "btn-primary " : ""}btn-sm"
        data-act="login"${toAttr}>${to ? "登录 / 注册" : "登录"}</button>`;
      return;
    }
    const u = currentUser;
    const name = u.nickname || u.email.split("@")[0];
    const initial = (name[0] || "?").toUpperCase();
    slot.innerHTML = `
      ${to ? '<a class="btn btn-primary btn-sm" href="app.html">进入控制台</a>' : ""}
      <div class="acct" id="acct">
        <button class="acct-btn" type="button" aria-haspopup="true" aria-expanded="false">
          <span class="avatar">${esc(initial)}</span>
          <span class="acct-name">${esc(name)}</span>
          ${u.is_admin ? '<span class="badge-admin">管理员</span>' : ""}
        </button>
        <div class="acct-menu" hidden>
          <div class="acct-mail">${esc(u.email)}</div>
          <button type="button" data-act="logout">退出登录</button>
        </div>
      </div>`;

    const btn = slot.querySelector(".acct-btn");
    const menu = slot.querySelector(".acct-menu");
    btn.onclick = (e) => {
      e.stopPropagation();
      const open = menu.hidden;
      menu.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
    };
    slot.querySelector('[data-act="logout"]').onclick = (e) => {
      e.stopPropagation();
      logout();
    };
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-act='login']");
    if (b) openLogin({ redirect: b.dataset.redirect || "" });
  });

  /* ── 账号菜单：点空白处收起。只注册一次，避免 renderAccount
        反复调用时叠加出多个监听器。 ── */
  document.addEventListener("click", (e) => {
    if (e.target.closest(".acct")) return;
    document.querySelectorAll(".acct-menu").forEach((m) => (m.hidden = true));
    document.querySelectorAll(".acct-btn").forEach((b) =>
      b.setAttribute("aria-expanded", "false")
    );
  });

  /* ── 埋点 ──────────────────────────────────────── */
  function track() {
    const params = new URLSearchParams(location.search);
    api("/analytics/track", {
      json: {
        visitor_id: visitorId(),
        path: location.pathname || "/",
        title: document.title.slice(0, 300),
        referrer: document.referrer || "",
        utm_source: params.get("utm_source") || "",
      },
    }).catch(() => {}); // 统计失败不该影响页面
  }

  /* ── 启动 ──────────────────────────────────────── */
  async function boot() {
    if (token()) {
      try {
        const r = await api("/auth/me");
        currentUser = r.user;
      } catch (_) {
        currentUser = null;
      }
    }
    renderAccount();
    emit();
    track();
    return currentUser;
  }

  // 只 boot 一次：脚本可能在 DOMContentLoaded 之前或之后执行，
  // 两条路径都指向同一个 Promise，避免重复上报埋点。
  let bootPromise = null;
  function bootOnce() {
    if (!bootPromise) bootPromise = boot();
    return bootPromise;
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootOnce, { once: true });
  } else {
    bootOnce();
  }

  /** 应用页：没登录就挡住，登录后 resolve。 */
  async function requireLogin() {
    // 先等 boot 把 /auth/me 查完，否则会在令牌其实有效时闪一下门禁页
    await bootOnce();
    let u = currentUser;
    if (!u) {
      showGate();
      // 等登录态变化。弹窗由用户点「登录 / 注册」触发，不自动弹。
      await new Promise((resolve) => {
        const off = () => {
          const i = listeners.indexOf(off);
          if (i >= 0) listeners.splice(i, 1);
          resolve();
        };
        listeners.push(off);
      });
      u = currentUser;
    }
    hideGate();
    return u;
  }

  window.TRAuth = {
    token,
    user: () => currentUser,
    get ready() {
      return bootOnce();
    },
    api,
    openLogin,
    closeLogin,
    logout,
    requireLogin,
    track,
    onChange(fn) {
      listeners.push(fn);
    },
    esc,
  };
})();
