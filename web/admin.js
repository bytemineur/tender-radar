/* 招标雷达 — 管理后台
   依赖 auth.js（提供带鉴权的 TRAuth.api）。

   职责：
   1. applyUser()   —— 按角色决定「管理」标签是否出现；非管理员即便手动改
      DOM 也进不去，因为所有接口都在服务端校验管理员权限。
   2. load()        —— 拉概览 / 趋势 / 来源 / 页面 / 套餐 / 用户六份数据。

   图表与看板页共用同一套 CSS 变量配色，切主题不用重绘。 */

(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

  let isAdmin = false;
  let days = 30;
  let userOffset = 0;
  const USER_PAGE = 20;

  const KIND_LABEL = {
    direct: "直接访问",
    search: "搜索引擎",
    social: "社交媒体",
    external: "外部链接",
  };

  const fmtDate = (s) => (s ? String(s).slice(0, 10) : "—");
  const fmtTime = (s) => (s ? String(s).slice(0, 16).replace("T", " ") : "—");

  function toast(msg) {
    const t = $("#toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.remove("show"), 2600);
  }

  /** 横条列表。max 不传则按最大值自适应。 */
  function bars(items, max) {
    if (!items || !items.length) {
      return `<div class="empty-sm">暂无数据</div>`;
    }
    const m = max || Math.max(...items.map((i) => i.value)) || 1;
    return items
      .map(
        (i) => `<div class="bar-row">
          <span class="bar-name" title="${esc(i.label)}">${esc(i.label)}</span>
          <span class="bar-track"><i class="bar-fill" style="width:${Math.round(
            (i.value / m) * 100
          )}%"></i></span>
          <span class="bar-val">${i.value.toLocaleString("zh-CN")}</span>
        </div>`
      )
      .join("");
  }

  /* ── 迷你折线图 ──────────────────────────────────
     和看板页一样手写 SVG，不引图表库：三条线共用一套坐标，
     纵轴按 PV 峰值缩放，UV 与新用户按同比例绘制，保证形状可比。 */
  function trendChart(labels, series) {
    const W = 640, H = 200;
    const PL = 40, PR = 12, PT = 14, PB = 26;
    const iw = W - PL - PR;
    const ih = H - PT - PB;

    const rawPeak = Math.max(1, ...series.map((s) => Math.max(...s.data)));
    // 刻度要「整」：直接按峰值四等分会得到 0/4/7/11/14 这种读数，
    // 看着像 bug。先找一个漂亮的步长，再把纵轴顶到步长 × 4。
    const STEPS = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000];
    const stepV =
      STEPS.find((s) => s >= rawPeak / 4) || Math.ceil(rawPeak / 400) * 100;
    const peak = stepV * 4;

    const n = labels.length || 1;
    const x = (i) => PL + (iw * i) / Math.max(1, n - 1);
    const y = (v) => PT + ih - (ih * v) / peak;

    const grid = [0, 0.25, 0.5, 0.75, 1]
      .map((r) => {
        const gy = PT + ih - ih * r;
        const gv = Math.round(peak * r);
        return `<line class="grid" x1="${PL}" y1="${gy}" x2="${W - PR}" y2="${gy}"/>
                <text class="axis" x="${PL - 8}" y="${gy + 4}" text-anchor="end">${gv}</text>`;
      })
      .join("");

    const paths = series
      .map((s) => {
        const d = s.data
          .map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`)
          .join(" ");
        return `<path class="line ${s.cls}" d="${d}" fill="none"/>`;
      })
      .join("");

    // 刻度：等距抽样 + 保证最后一天有标签。
    // 但两者撞在一起时（比如第 28 天和第 29 天）会叠字，所以末尾标签
    // 离前一个太近就把前一个撤掉，而不是两个都留着。
    const step = Math.max(1, Math.ceil(n / 8));
    const shown = [];
    for (let i = 0; i < n; i += step) shown.push(i);
    if (shown[shown.length - 1] !== n - 1) {
      const prev = shown[shown.length - 1];
      if (n - 1 - prev < step * 0.6) shown.pop();
      shown.push(n - 1);
    }
    const ticks = shown
      .map(
        (i) =>
          `<text class="axis" x="${x(i).toFixed(1)}" y="${H - 6}" text-anchor="middle">${labels[i].slice(5)}</text>`
      )
      .join("");

    return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
      aria-label="访问趋势折线图">
      ${grid}${paths}${ticks}</svg>
      <div class="chart-legend">
        ${series
          .map(
            (s) =>
              `<span><i class="sw ${s.cls}"></i>${esc(s.name)}：${s.data
                .reduce((a, b) => a + b, 0)
                .toLocaleString("zh-CN")}</span>`
          )
          .join("")}
      </div>`;
  }

  /* ── 数据加载 ──────────────────────────────────── */
  async function loadOverview() {
    const r = await window.TRAuth.api(`/analytics/overview?days=${days}`);
    $("#aTodayPv").textContent = r.today.pv.toLocaleString("zh-CN");
    $("#aTodayUv").textContent = r.today.uv.toLocaleString("zh-CN");
    $("#aRangeUv").textContent = r.range.uv.toLocaleString("zh-CN");
    $("#aUsers").textContent = r.users.total.toLocaleString("zh-CN");
    $("#admHint").textContent =
      `近 ${days} 天：PV ${r.range.pv.toLocaleString("zh-CN")}，` +
      `人均 ${r.range.pv_per_uv} 页，活跃登录用户 ${r.users.active}，` +
      `今日新增 ${r.today.new_users}`;
  }

  async function loadTraffic() {
    const r = await window.TRAuth.api(`/analytics/traffic?days=${days}`);
    $("#admTrend").innerHTML = trendChart(r.labels, [
      { name: "浏览量 PV", data: r.pv, cls: "s-pv" },
      { name: "访客 UV", data: r.uv, cls: "s-uv" },
      { name: "新注册", data: r.new_users, cls: "s-new" },
    ]);
    $("#admTrendTag").textContent = `近 ${days} 天`;
  }

  async function loadReferrers() {
    const r = await window.TRAuth.api(`/analytics/referrers?days=${days}`);
    $("#admKinds").innerHTML = bars(
      r.kinds.map((k) => ({ label: KIND_LABEL[k.label] || k.label, value: k.value }))
    );
    $("#admHosts").innerHTML = bars(r.hosts);
  }

  async function loadPages() {
    const r = await window.TRAuth.api(`/analytics/pages?days=${days}`);
    $("#admPages").innerHTML = bars(r.items);
  }

  async function loadBilling() {
    const r = await window.TRAuth.api("/billing/summary");
    const PLAN_LABEL = { free: "免费版", pro: "专业版", team: "团队版" };
    $("#admPlans").innerHTML = bars(
      r.distribution.map((d) => ({
        label: PLAN_LABEL[d.label] || d.label,
        value: d.value,
      }))
    );
    $("#admBilling").innerHTML = r.enabled
      ? `本月经常性收入：<b>¥${r.revenue.mrr}</b>`
      : `未接入支付，暂无收入数据。套餐字段已预留，接入后此面板会自动显示。`;
  }

  async function loadUsers() {
    const r = await window.TRAuth.api(
      `/auth/users?limit=${USER_PAGE}&offset=${userOffset}`
    );
    $("#admUserTag").textContent = `共 ${r.total} 人`;
    const body = $("#admUserBody");
    if (!r.items.length) {
      body.innerHTML = `<tr><td colspan="7" class="empty-sm">还没有用户</td></tr>`;
      return;
    }
    const me = window.TRAuth.user();
    body.innerHTML = r.items
      .map((u) => {
        const isMe = me && u.id === me.id;
        return `<tr>
          <td class="u-mail">${esc(u.email)}${isMe ? '<span class="tag ok">我</span>' : ""}</td>
          <td>${esc(u.nickname || "—")}</td>
          <td>${u.is_admin ? '<span class="badge-admin">管理员</span>' : "普通用户"}</td>
          <td>${fmtDate(u.created_at)}</td>
          <td>${u.last_login_at ? fmtTime(u.last_login_at) : "从未登录"}</td>
          <td>${u.login_count || 0}</td>
          <td class="u-ops">
            ${
              isMe
                ? '<span class="muted">—</span>'
                : `<button class="btn btn-xs" data-uid="${u.id}" data-act="${
                    u.is_admin ? "demote" : "promote"
                  }">${u.is_admin ? "取消管理员" : "设为管理员"}</button>
                   <button class="btn btn-xs danger" data-uid="${u.id}" data-act="${
                    u.is_active ? "disable" : "enable"
                  }">${u.is_active ? "停用" : "启用"}</button>`
            }
          </td>
        </tr>`;
      })
      .join("");
  }

  async function load() {
    if (!isAdmin) return;
    const jobs = [
      loadOverview,
      loadTraffic,
      loadReferrers,
      loadPages,
      loadBilling,
      loadUsers,
    ];
    for (const fn of jobs) {
      try {
        await fn();
      } catch (e) {
        toast("后台数据加载失败：" + e.message);
        return; // 第一个失败通常是权限问题，后面也别白跑
      }
    }
  }

  /* ── 权限与入口 ────────────────────────────────── */
  function applyUser(user) {
    isAdmin = !!(user && user.is_admin);
    const tab = $("#tabAdmin");
    if (tab) tab.hidden = !isAdmin;
    // 之前停在管理视图但权限被撤了，就退回看板
    if (!isAdmin) {
      const active = document.querySelector(".view.active");
      if (active && active.id === "view-admin") {
        document.querySelector('#tabs button[data-view="board"]').click();
      }
    }
  }

  document.addEventListener("click", async (e) => {
    const b = e.target.closest("#admUserBody button[data-act]");
    if (!b) return;
    const id = Number(b.dataset.uid);
    const act = b.dataset.act;
    const patch =
      act === "promote"
        ? { role: "admin" }
        : act === "demote"
        ? { role: "user" }
        : { is_active: act === "enable" };
    b.disabled = true;
    try {
      await window.TRAuth.api(`/auth/users/${id}`, { method: "PATCH", json: patch });
      toast("已更新");
      loadUsers();
    } catch (err) {
      toast("更新失败：" + err.message);
      b.disabled = false;
    }
  });

  const rangeSel = $("#admRange");
  if (rangeSel) {
    rangeSel.addEventListener("change", () => {
      days = Number(rangeSel.value);
      load();
    });
  }

  $("#tabs").addEventListener("click", (e) => {
    if (e.target.closest('button[data-view="admin"]')) applyUser(window.TRAuth.user());
  });

  window.TRAdmin = { load, applyUser };

  // 登录/登出后角色可能变化，重新判断一次入口可见性
  window.TRAuth.ready.then(applyUser);
  window.TRAuth.onChange(applyUser);
})();
