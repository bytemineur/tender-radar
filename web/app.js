/* 招标雷达 — 前端逻辑
   零依赖（无框架、无 CDN），所有渲染手写。部署时只需静态文件。 */

(() => {
  "use strict";

  const API = "/api";
  const state = {
    view: "board",
    page: 1,
    size: 20,
    total: 0,
    q: "",
    province: "",
    notice_type: "",
    minBudget: null,
    maxBudget: null,
    sort: "deadline",
    onlyActive: true,
    includeResult: false,
    loading: false,
  };

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  };

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

  function toast(msg, ms = 2600) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.remove("show"), ms);
  }

  /* 统一走 TRAuth.api：自动带上 Authorization 头，401 时由它负责清令牌。
     这里不再自己 fetch，避免出现「一半请求带鉴权、一半不带」的漏网之鱼。 */
  async function api(path, opts) {
    return window.TRAuth.api(path, opts);
  }

  /*      ── 主题 ──────────────────────────────────────
     三选一：跟随系统 / 浅色 / 深色，**默认是深色**——
     用户没手动点过之前一律用 dark，不跟随系统。
     偏好存 localStorage；选了「跟随系统」就一直监听系统的深浅色变化。
     所有颜色都是 CSS 变量，地图与图表用 class 取色，切主题无需重绘——
     这也是为什么这里只改 <html data-theme>，不碰任何 SVG 属性。 */
  const THEME_KEY = "tr-theme";
  const DEFAULT_PREF = "dark"; // 未手动选择时默认深色，而不是跟随系统
  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");

  const resolveTheme = (pref) =>
    pref === "auto" ? (darkQuery.matches ? "dark" : "light") : pref;

  function applyTheme(pref) {
    const mode = resolveTheme(pref);
    const root = document.documentElement;
    root.setAttribute("data-theme", mode);
    // 让原生控件（select、滚动条、表单）也跟上
    root.style.colorScheme = mode;
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

  $("#themeSwitch").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-theme]");
    if (b) setTheme(b.dataset.theme);
  });

  darkQuery.addEventListener("change", () => {
    if (currentPref() === "auto") applyTheme("auto");
  });

  applyTheme(currentPref());

  const fmtMoney = (v) => {
    if (v == null || v === 0) return "";
    if (v >= 1e8) return (v / 1e8).toFixed(2) + " 亿";
    if (v >= 1e4) return (v / 1e4).toFixed(v >= 1e6 ? 0 : 1) + " 万";
    return Math.round(v) + " 元";
  };

  const fmtDate = (s) => (s ? String(s).slice(0, 10) : "");

  const fmtYuan = (v) => (v == null ? "" : v.toLocaleString("zh-CN") + " 元");

  /* ── 视图切换 ────────────────────────────────── */
  function switchView(v) {
    // 未登录时不给看任何视图：门禁在启动时统一拦，这里只兜个底
    const user = window.TRAuth.user();
    if (!user) {
      window.TRAuth.requireLogin();
      return;
    }
    // 管理视图不是「隐藏就够了」：标签的显隐交给 admin.js，这里再兜一道，
    // 免得哪条路径（键盘、缓存的旧脚本、手改 DOM）把非管理员送进来。
    // 服务端本来也会 403，但与其让人看到一个空面板，不如根本不切。
    if (v === "admin" && !user.is_admin) v = "board";
    state.view = v;
    document.querySelectorAll("#tabs button").forEach((b) =>
      b.classList.toggle("active", b.dataset.view === v)
    );
    document.querySelectorAll(".view").forEach((s) =>
      s.classList.toggle("active", s.id === "view-" + v)
    );
    if (v === "radar") loadList();
    if (v === "board") loadBoard();
    if (v === "sources") loadSources();
    if (v === "admin") window.TRAdmin && window.TRAdmin.load();
  }

  /* ── 雷达列表 ────────────────────────────────── */
  function cardHtml(t) {
    const d = t.days_left;
    // 结果公告（中标/成交/废标）本来就没有截止时间，
    // 显示「未披露」是在暗示数据缺失——其实是它不适用。
    const isResult = !!t.is_result;
    let cls = "", dcls = "", dlabel = "";

    if (isResult) {
      cls = "result";
      dcls = "muted";
      dlabel = "已出结果";
    } else if (t.expired || (d != null && d < 0)) {
      cls = "expired";
      dcls = "muted";
      dlabel = "已截止";
    } else if (d == null) {
      dcls = "muted";
      dlabel = "未披露";
    } else if (d <= 3) {
      cls = "urgent";
      dcls = "danger";
    } else if (d <= 7) {
      cls = "soon";
      dcls = "warn";
    } else {
      dcls = "ok";
    }

    const daysNum = d == null ? "—" : t.expired || d < 0 ? "0" : String(d);
    const unit =
      t.expired || (d != null && d < 0)
        ? dlabel || "已截止"
        : d == null
        ? "未披露"
        : "天后截止";

    const tags = [];
    if (t.is_result) tags.push(`<span class="tag">结果公告</span>`);
    if (t.mirrors > 0) tags.push(`<span class="tag ok">${t.mirrors + 1} 个来源</span>`);
    if (t.deadline && !t.deadline_confident)
      tags.push(`<span class="tag warn">截止待核</span>`);

    // 预算始终是核心信息 —— 结果公告里那就是「中标/成交金额」，同样要露出来
    const budgetLabel = isResult ? "金额" : "预算";
    const budgetCell = t.budget_amount
      ? `<b title="${esc(fmtYuan(t.budget_amount))}">${fmtMoney(t.budget_amount)}</b>`
      : `<b class="muted">未披露</b>`;
    const deadlineCell =
      isResult
        ? `<b class="muted">不适</b>`
        : t.deadline
        ? `<b>${fmtDate(t.deadline)}</b>`
        : t.deadline_raw
        ? `<b class="muted" title="原文：${esc(t.deadline_raw)}">待核</b>`
        : `<b class="muted">未披露</b>`;
    const leftCell = isResult
      ? `<b class="muted">已出结果</b>`
      : d == null
      ? `<b class="muted">未披露</b>`
      : t.expired || d < 0
      ? `<b class="muted">已截止</b>`
      : `<b class="${dcls === "danger" ? "hot" : dcls === "warn" ? "mid" : ""}">${d} 天</b>`;

    return `<div class="card ${cls}" data-id="${t.id}">
      <div class="row1">
        <div class="title">${esc(t.title)}</div>
        <div class="days ${dcls}">
          <span class="num">${daysNum}</span>
          <span class="unit">${unit}</span>
        </div>
      </div>
      <div class="facts">
        <span class="fact"><span class="k">${budgetLabel}</span>${budgetCell}</span>
        <span class="fact"><span class="k">截止</span>${deadlineCell}</span>
        <span class="fact"><span class="k">剩余</span>${leftCell}</span>
        <span class="fact"><span class="k">采购人</span><b>${esc(t.purchaser || "—")}</b></span>
        <span class="fact"><span class="k">地区</span><b>${esc(t.province || "—")}</b></span>
        <span class="fact"><span class="k">来源</span><b>${esc(t.source_name || t.source_id || "—")}</b></span>
        <span class="fact"><span class="k">发布</span><b>${fmtDate(t.publish_time) || "—"}</b></span>
      </div>
      <div class="tags-row">${tags.join("")}</div>
    </div>`;
  }

  function renderList(items, append) {
    const box = $("#list");
    if (!append) box.innerHTML = "";
    if (!items.length && !append) {
      box.innerHTML = `<div class="empty"><div class="big">◌</div>
        没有符合条件的线索。放宽筛选条件，或到「来源」页点「采集全部」。</div>`;
      return;
    }
    box.insertAdjacentHTML("beforeend", items.map(cardHtml).join(""));
    box.querySelectorAll(".card").forEach((c) => {
      c.onclick = () => openDetail(Number(c.dataset.id));
    });
  }

  async function loadList(reset = true) {
    if (state.loading) return;
    state.loading = true;
    if (reset) state.page = 1;

    const box = $("#list");
    if (reset) box.innerHTML = `<div class="empty"><div class="big">◌</div>正在加载…</div>`;

    const p = new URLSearchParams({
      page: state.page,
      size: state.size,
      sort: state.sort,
      only_active: state.onlyActive,
      include_result: state.includeResult,
    });
    if (state.q) p.set("q", state.q);
    if (state.province) p.set("province", state.province);
    if (state.notice_type) p.set("notice_type", state.notice_type);
    // 单位是元，前端用的是元输入框；后端 budget_amount 也存元，直接透传
    if (state.minBudget != null && state.minBudget !== "") p.set("min_budget", state.minBudget);
    if (state.maxBudget != null && state.maxBudget !== "") p.set("max_budget", state.maxBudget);

    try {
      const data = await api("/tenders?" + p.toString());
      state.total = data.total;
      $("#count").textContent = `共 ${data.total} 条`;
      renderList(data.items, !reset);
      if (reset) loadFilterOptions();
    } catch (e) {
      box.innerHTML = `<div class="empty"><div class="big">!</div>加载失败：${esc(e.message)}</div>`;
    } finally {
      state.loading = false;
    }
  }

  /* 筛选下拉从分布接口取全量维度，避免只反映当前页 */
  let filtersLoaded = false;
  async function loadFilterOptions() {
    if (filtersLoaded) return;
    filtersLoaded = true;
    const dims = [
      ["province", "#fProvince"],
      ["notice_type", "#fType"],
    ];
    for (const [dim, sel] of dims) {
      try {
        const d = await api(`/stats/distribution?dim=${dim}&limit=50`);
        const box = $(sel);
        const exist = new Set([...box.options].map((o) => o.value));
        d.labels.forEach((v) => {
          if (!v || exist.has(v)) return;
          const o = document.createElement("option");
          o.value = o.textContent = v;
          box.appendChild(o);
        });
      } catch (_) {
        /* 单项失败不影响其他 */
      }
    }
  }

  /* ── 详情抽屉 ────────────────────────────────── */
  async function openDetail(id) {
    $("#drawer").classList.add("open");
    $("#mask").classList.add("open");
    $("#dTitle").textContent = "加载中…";
    $("#dBody").innerHTML = `<div class="skeleton" style="width:70%;margin-bottom:10px"></div>
      <div class="skeleton" style="width:90%"></div>`;

    try {
      const t = await api("/tenders/" + id);
      $("#dTitle").textContent = t.title;

      const kv = [];
      const add = (k, v) => {
        if (v) kv.push(`<dt>${k}</dt><dd>${v}</dd>`);
      };
      add("公告类型", esc(t.notice_type));
      add("采购人", esc(t.purchaser));
      add("代理机构", esc(t.agency));
      add(
        "预算",
        t.budget_amount
          ? `${fmtMoney(t.budget_amount)} <span class="hint">（${fmtYuan(t.budget_amount)}）</span>`
          : ""
      );
      add("地区", esc([t.province, t.city].filter(Boolean).join(" ")));
      add("发布", fmtDate(t.publish_time));
      add(
        "截止",
        t.deadline
          ? `${fmtDate(t.deadline)} <span class="tag ${t.deadline_confident ? "ok" : "warn"}">${
              t.deadline_confident ? "已校验" : "待核实"
            }</span>`
          : t.deadline_raw
          ? `<span class="tag warn">原文：${esc(t.deadline_raw)}</span>`
          : "未披露"
      );
      add("来源", esc(t.source_id));
      add(
        "原文",
        t.source_url
          ? `<a href="${esc(t.source_url)}" target="_blank" rel="noopener">打开来源页 ↗</a>`
          : ""
      );

      const mirrors =
        t.mirrors_list && t.mirrors_list.length
          ? `<div class="panel" style="margin:16px 0 0;padding:12px 14px">
               <h3 style="margin-bottom:6px">同源镜像 ${t.mirrors_list.length}</h3>
               <p class="hint" style="margin-bottom:8px">同一项目在其他平台也有发布，已折叠为一条</p>
               ${t.mirrors_list
                 .map(
                   (m) =>
                     `<div style="font-size:12.5px;margin-bottom:5px">
                        <span class="tag">${esc(m.source_id)}</span>
                        <a href="${esc(m.url)}" target="_blank" rel="noopener">${esc(
                       m.title.slice(0, 40)
                     )}…</a>
                      </div>`
                 )
                 .join("")}
             </div>`
          : "";

      const meta = t.extract_meta
        ? `<div class="panel" style="margin:16px 0 0;padding:12px 14px">
             <h3 style="margin-bottom:8px">抽取溯源</h3>
             <p class="hint" style="margin-bottom:8px">字段由规则命中还是 LLM 补齐，逐项可查</p>
             <div style="display:flex;flex-wrap:wrap;gap:6px">
               ${Object.entries(t.extract_meta)
                 .filter(([, v]) => v)
                 .map(
                   ([k, v]) =>
                     `<span class="tag ${v === "llm" ? "warn" : "ok"}">${esc(k)}：${
                       v === "llm" ? "LLM" : "规则"
                     }</span>`
                 )
                 .join("")}
             </div>
           </div>`
        : "";

      $("#dBody").innerHTML = `
        <dl class="kv">${kv.join("")}</dl>
        <h3 style="font-size:13px;margin:0 0 8px">公告正文</h3>
        <div class="content-box">${
          t.content_text ? esc(t.content_text) : "（未抓取正文，可在来源页勾选「取详情」重采）"
        }</div>
        ${mirrors}${meta}`;
    } catch (e) {
      $("#dTitle").textContent = "加载失败";
      $("#dBody").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    }
  }

  function closeDetail() {
    $("#drawer").classList.remove("open");
    $("#mask").classList.remove("open");
  }

  /* ── 看板 ────────────────────────────────────── */
  function trendSvg(data) {
    const W = 560, H = 150, padL = 6, padR = 6, padB = 18;
    const n = data.labels.length || 1;
    const maxC = Math.max(1, ...data.counts);
    const bw = (W - padL - padR) / n;

    const bars = data.counts
      .map((c, i) => {
        const h = (c / maxC) * (H - padB - 8);
        const x = padL + i * bw + bw * 0.18;
        const y = H - padB - h;
        // 配色交给 CSS（rect.tbar），切主题时不用重绘
        return `<rect class="tbar" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(
          bw * 0.64
        ).toFixed(1)}" height="${h.toFixed(1)}" rx="1.5" opacity=".85"/>`;
      })
      .join("");

    // 只标注首尾与中间三个日期，避免拥挤
    const marks = [0, Math.floor(n / 2), n - 1]
      .filter((v, i, a) => a.indexOf(v) === i)
      .map((i) => {
        const x = padL + i * bw + bw / 2;
        return `<text class="tlab" x="${x.toFixed(1)}" y="${H - 4}" text-anchor="middle"
          font-size="9.5">${data.labels[i].slice(5)}</text>`;
      })
      .join("");

    return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
      aria-label="每日公告数量趋势">
      <line class="taxis" x1="0" y1="${H - padB}" x2="${W}" y2="${H - padB}"
        stroke-width="1"/>
      ${bars}${marks}
    </svg>`;
  }

  function barsHtml(data, opts = {}) {
    if (!data.labels.length) return `<div class="empty" style="padding:20px">暂无数据</div>`;
    // 后端按需求一次多给一些（地图要用全量），列表只展示前 N 名
    const n = opts.limit ? Math.min(opts.limit, data.labels.length) : data.labels.length;
    const names = data.labels.slice(0, n);
    const values = data.values.slice(0, n);
    const rest = data.labels.length - n;
    const max = Math.max(...values, 1);
    const rows = names
      .map((name, i) => {
        const pct = (values[i] / max) * 100;
        // 展示名可与原始值不同：来源维度传进来的是 csg/ccgp 这类 id
        const label = opts.fmt ? opts.fmt(name) : name;
        return `<div class="bar-row"${opts.clickable ? ` data-name="${esc(name)}"` : ""}>
          <span class="name" title="${esc(label)}">${esc(label)}</span>
          <span class="bar-track"><span class="bar-fill" style="width:${pct.toFixed(
            1
          )}%"></span></span>
          <span class="n">${values[i]}</span>
        </div>`;
      })
      .join("");
    const more =
      rest > 0
        ? `<div class="bar-more">其余 ${rest} 个地区的线索已计入上方地图</div>`
        : "";
    return rows + more;
  }

  /* ── 中国地图 ──────────────────────────────────
     数据来自 web/china-map.js（全国标准行政区划边界，含台湾省与南海诸岛）。
     完全离线渲染，不调用任何第三方地图服务。
     分级色是 CSS 变量（.l1~.l6），按该省在库线索数取一到六级，0 条不分级。 */
  const MAP_LEVELS = 6;

  /** 返回 0（无数据）或 1..6 的色阶编号 */
  function colorLevel(count, max) {
    if (!count) return 0;
    const r = max <= 1 ? 1 : Math.log1p(count) / Math.log1p(max);
    return Math.min(MAP_LEVELS, Math.max(1, Math.round(r * (MAP_LEVELS - 1))));
  }

  // 直辖市/特别行政区面积太小，标签直接盖在邻居头上，给个偏移量挪开
  const LABEL_SHIFT = {
    北京: [-52, -16],
    天津: [40, 10],
    上海: [52, -4],
    香港: [44, 12],
    澳门: [44, 30],
  };

  function renderMap(dist) {
    const box = $("#chinaMap");
    const data = window.CHINA_MAP;
    if (!data) {
      box.innerHTML = `<div class="empty" style="padding:24px">地图数据未加载</div>`;
      return;
    }
    const counts = {};
    dist.labels.forEach((n, i) => (counts[n] = dist.values[i]));
    const max = Math.max(1, ...dist.values);

    const [vx, vy, vw, vh] = data.viewBox;
    // 画布宽 1000，实际渲染约 500px，字号要按这个比例放大才看得清
    const top = data.provinces
      .filter((p) => counts[p.name] && p.cx != null)
      .sort((a, b) => counts[b.name] - counts[a.name])
      .slice(0, 12);
    const labels = top
      .map((p) => {
        const [dx, dy] = LABEL_SHIFT[p.name] || [0, 0];
        const x = p.cx + dx;
        const y = p.cy + dy;
        // 挪开的标签补一条引线，别让人不知道指的是哪块
        const leader =
          dx || dy
            ? `<line class="leader" x1="${p.cx}" y1="${p.cy}" x2="${x}" y2="${y}"/>`
            : "";
        return `${leader}<text class="plabel" x="${x}" y="${y}" text-anchor="middle"
             font-size="21" pointer-events="none">${esc(p.name)} ${counts[p.name]}</text>`;
      })
      .join("");

    const paths = data.provinces
      .map((p) => {
        const c = counts[p.name] || 0;
        const lv = colorLevel(c, max);
        const cls = ["prov"];
        if (lv) cls.push("l" + lv, "hit");
        return `<path d="${p.d}" class="${cls.join(" ")}"
          data-name="${esc(p.name)}" data-n="${c}"></path>`;
      })
      .join("");

    const inset = data.inset
      ? `<g class="inset" pointer-events="none">
           <rect x="${data.inset.frame[0]}" y="${data.inset.frame[1]}"
                 width="${data.inset.frame[2]}" height="${data.inset.frame[3]}"
                 stroke-width="1"/>
           ${data.inset.paths
             .map((d) => `<path d="${d}" stroke-width="1.4"/>`)
             .join("")}
           <text x="${data.inset.frame[0] + data.inset.frame[2] / 2}"
                 y="${data.inset.frame[1] + data.inset.frame[3] - 3}"
                 text-anchor="middle" font-size="9">南海诸岛</text>
         </g>`
      : "";

    box.innerHTML = `<svg viewBox="${vx} ${vy} ${vw} ${vh}" class="map-svg" role="img"
      aria-label="各省线索数量分布图">
      <g class="provs">${paths}</g>${inset}${labels}
    </svg>`;

    const tip = $("#mapTip");
    const total = dist.values.reduce((a, b) => a + b, 0);
    $("#mapTag").textContent = `${dist.labels.length} 个省 / ${total} 条`;

    box.querySelectorAll("path.prov").forEach((el) => {
      el.addEventListener("mousemove", (ev) => {
        const n = Number(el.dataset.n);
        const name = el.dataset.name;
        tip.innerHTML = n
          ? `<b>${esc(name)}</b><span>${n} 条线索 · 点击筛选</span>`
          : `<b>${esc(name)}</b><span>暂无线索</span>`;
        const r = box.getBoundingClientRect();
        tip.style.left = ev.clientX - r.left + 12 + "px";
        tip.style.top = ev.clientY - r.top + 12 + "px";
        tip.classList.add("show");
      });
      el.addEventListener("mouseleave", () => tip.classList.remove("show"));
      el.addEventListener("click", () => {
        if (Number(el.dataset.n) > 0) gotoProvince(el.dataset.name);
      });
    });
  }

  /* 来源 id → 中文名映射。看板要用，来源页也会用，缓存一次就够。 */
  let srcNameMap = null;
  async function sourceNames() {
    if (srcNameMap) return srcNameMap;
    try {
      const rows = await api("/sources");
      srcNameMap = Object.fromEntries(rows.map((s) => [s.id, s.name]));
    } catch {
      srcNameMap = {}; // 拿不到就退回显示 id，不影响其他面板
    }
    return srcNameMap;
  }

  async function loadBoard() {
    try {
      const [ov, tr, dp, ds, up, smap] = await Promise.all([
        api("/stats/overview"),
        api("/stats/trend?days=30"),
        api("/stats/distribution?dim=province&limit=50"),
        api("/stats/distribution?dim=source&limit=20"),
        api("/stats/upcoming?days=7"),
        sourceNames(),
      ]);

      $("#mTotal").textContent = ov.total;
      $("#mActive").textContent = ov.active;
      $("#mUrgent").textContent = ov.urgent;
      $("#mBudget").innerHTML = (ov.budget_sum / 1e4).toFixed(0) + "<small>万</small>";

      renderMap(dp);
      $("#trendChart").innerHTML = trendSvg(tr);
      $("#distProvince").innerHTML = barsHtml(dp, { clickable: true, limit: 10 });
      $("#distSource").innerHTML = barsHtml(ds, { fmt: (id) => smap[id] || id });

      $("#distProvince")
        .querySelectorAll(".bar-row[data-name]")
        .forEach((n) => (n.onclick = () => gotoProvince(n.dataset.name)));

      $("#upcoming").innerHTML = up.items.length
        ? up.items
            .map(
              (it) => `<div class="up-item" data-id="${it.id}">
                <span class="tag ${it.days_left <= 3 ? "danger" : "warn"}">${
                it.days_left
              } 天</span>
                <span class="t">${esc(it.title)}</span>
              </div>`
            )
            .join("")
        : `<div class="empty" style="padding:18px">未来 7 天没有到期项目</div>`;

      $("#upcoming").querySelectorAll(".up-item").forEach((n) => {
        n.onclick = () => openDetail(Number(n.dataset.id));
      });
    } catch (e) {
      toast("看板加载失败：" + e.message);
    }
  }

  /* ── 来源 ────────────────────────────────────── */
  async function loadSources() {
    try {
      const rows = await api("/sources");
      const body = $("#srcBody");
      body.innerHTML = rows
        .map((s) => {
          const healthCls = s.health === "idle" ? "idle" : s.health === "running" ? "running" : "error";
          const healthTxt =
            { idle: "正常", running: "采集中", blocked: "被拦截", error: "异常" }[s.health] ||
            s.health;
          return `<tr>
            <td><strong>${esc(s.name)}</strong><br>
                <span style="font-size:11.5px;color:var(--text-3)">${esc(s.id)}</span></td>
            <td><span class="tag">${esc(s.mode)}</span></td>
            <td><span class="dotled"><i class="${healthCls}"></i>${healthTxt}</span></td>
            <td style="font-size:12.5px;color:var(--text-2)">
              ${s.last_run_at ? esc(String(s.last_run_at).slice(0, 16).replace("T", " ")) : "从未"}
              ${s.last_error ? `<br><span style="color:var(--danger);font-size:11.5px">${esc(
                s.last_error.slice(0, 46)
              )}</span>` : ""}
            </td>
            <td>${s.fail_count > 0 ? `<span class="tag danger">${s.fail_count}</span>` : "—"}</td>
            <td>
              <button class="btn btn-sm" data-act="toggle" data-id="${esc(s.id)}"
                data-on="${s.enabled}">${s.enabled ? "停用" : "启用"}</button>
              <button class="btn btn-sm" data-act="crawl" data-id="${esc(s.id)}"
                title="只抓列表，快">采集</button>
              <button class="btn btn-sm" data-act="crawl" data-id="${esc(s.id)}"
                data-detail="1" title="逐条打开详情页补截止时间/预算，慢但字段全">深采</button>
            </td>
          </tr>`;
        })
        .join("") || `<tr><td colspan="6" class="empty">configs/sites 下没有规则</td></tr>`;

      body.querySelectorAll("button[data-act]").forEach((b) => {
        b.onclick = async () => {
          const id = b.dataset.id;
          if (b.dataset.act === "toggle") {
            const next = b.dataset.on !== "true";
            await api("/sources/" + id, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ enabled: next }),
            });
            toast(next ? "已启用" : "已停用");
            loadSources();
          } else {
            b.disabled = true;
            const label = b.textContent;
            b.textContent = "…";
            try {
              const r = await api("/crawl", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  site_id: id,
                  max_pages: 3,
                  fetch_detail: b.dataset.detail === "1",
                }),
              });
              const s = r.results[id] || {};
              toast(
                `采集完成：新增 ${s.new || 0} 条，去重 ${s.dup || 0} 条` +
                  (s.error ? `（${String(s.error).slice(0, 30)}）` : "")
              );
              loadSources();
              if (state.view === "radar") loadList();
            } catch (e) {
              toast("采集失败：" + e.message);
            } finally {
              b.disabled = false;
              b.textContent = label;
            }
          }
        };
      });
    } catch (e) {
      toast("来源加载失败：" + e.message);
    }
  }

  /* ── 事件绑定 ────────────────────────────────── */
  $("#tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-view]");
    if (b) switchView(b.dataset.view);
  });

  let qTimer;
  $("#q").addEventListener("input", (e) => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => {
      state.q = e.target.value.trim();
      loadList();
    }, 320);
  });

  ["fProvince", "fType", "fSort"].forEach((id) => {
    const key = { fProvince: "province", fType: "notice_type", fSort: "sort" }[id];
    $("#" + id).addEventListener("change", (e) => {
      state[key] = e.target.value;
      loadList();
    });
  });

  /* 预算区间：下拉快速选档，填进两个输入框后仍可手动微调 */
  $("#fBudgetRange").addEventListener("change", (e) => {
    const v = e.target.value;
    const [lo, hi] = v ? v.split(",") : ["", ""];
    $("#fMinBudget").value = lo || "";
    $("#fMaxBudget").value = hi || "";
    applyBudget();
  });

  let bTimer;
  ["fMinBudget", "fMaxBudget"].forEach((id) => {
    $("#" + id).addEventListener("input", () => {
      clearTimeout(bTimer);
      bTimer = setTimeout(applyBudget, 500);
    });
  });

  function applyBudget() {
    const lo = $("#fMinBudget").value.trim();
    const hi = $("#fMaxBudget").value.trim();
    state.minBudget = lo === "" ? null : Number(lo);
    state.maxBudget = hi === "" ? null : Number(hi);
    // 手动改数字时同步下拉的显示状态，避免两处看起来对不上
    const range = $("#fBudgetRange");
    const match = `${lo},${hi}`;
    range.value = [...range.options].some((o) => o.value === match) ? match : "";
    loadList();
  }

  /** 由地图/柱状图点击触发：按省份筛线索并跳回雷达 */
  function gotoProvince(name) {
    state.province = name;
    const sel = $("#fProvince");
    if (![...sel.options].some((o) => o.value === name)) {
      const o = document.createElement("option");
      o.value = o.textContent = name;
      sel.appendChild(o);
    }
    sel.value = name;
    switchView("radar"); // switchView 内部会按省份条件重新拉列表，别再调一次
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  $("#swActive").addEventListener("click", (e) => {
    state.onlyActive = !state.onlyActive;
    e.currentTarget.classList.toggle("on", state.onlyActive);
    loadList();
  });

  $("#swResult").addEventListener("click", (e) => {
    state.includeResult = !state.includeResult;
    e.currentTarget.classList.toggle("on", state.includeResult);
    loadList();
  });

  $("#btnClose").onclick = closeDetail;
  $("#mask").onclick = closeDetail;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDetail();
  });

  $("#btnCrawl").onclick = async (e) => {
    const b = e.currentTarget;
    b.disabled = true;
    b.textContent = "采集中…";
    try {
      const r = await api("/crawl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_pages: 2 }),
      });
      const n = Object.values(r.results).reduce((a, s) => a + (s.new || 0), 0);
      toast(`采集完成：新增 ${n} 条`);
      if (state.view === "radar") loadList();
      if (state.view === "sources") loadSources();
    } catch (err) {
      toast("采集失败：" + err.message);
    } finally {
      b.disabled = false;
      b.textContent = "抓取";
    }
  };

  $("#btnExport").onclick = async () => {
    try {
      // 导出必须带 Authorization：接口需要登录，这里手写一个带头的 fetch
      const res = await fetch(
        API + "/tenders/export?only_active=" + state.onlyActive,
        {
          method: "POST",
          headers: { Authorization: "Bearer " + window.TRAuth.token() },
        }
      );
      if (!res.ok) throw new Error("导出失败");
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "tenders.csv";
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      toast(e.message);
    }
  };

  $("#btnReload").onclick = async () => {
    try {
      const r = await api("/sources/reload", { method: "POST" });
      toast(`已重载规则，共 ${r.total} 个来源（新增 ${r.added}）`);
      loadSources();
    } catch (e) {
      toast(e.message);
    }
  };

  $("#btnCrawlAll").onclick = async (e) => {
    const b = e.currentTarget;
    b.disabled = true;
    b.textContent = "采集中…";
    try {
      const r = await api("/crawl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_pages: 3 }),
      });
      const n = Object.values(r.results).reduce((a, s) => a + (s.new || 0), 0);
      toast(`全部完成：新增 ${n} 条`);
      loadSources();
    } catch (err) {
      toast("采集失败：" + err.message);
    } finally {
      b.disabled = false;
      b.textContent = "采集全部";
    }
  };

  // 滚动到底自动加载下一页
  window.addEventListener("scroll", () => {
    if (state.view !== "radar" || state.loading) return;
    if (window.innerHeight + window.scrollY >= document.body.offsetHeight - 320) {
      if (state.page * state.size < state.total) {
        state.page += 1;
        loadList(false);
      }
    }
  });

  /* ── 启动 ──────────────────────────────────────
     先过门禁，再拉数据。顺序反了会先吃一串 401，
     控制台一片红，实际上只是还没登录而已。 */
  window.TRAuth.requireLogin()
    .then((user) => {
      if (!user) return; // 已弹出登录框，等它完成
      window.TRAdmin && window.TRAdmin.applyUser(user);
      switchView(state.view);
    })
    .catch(() => {});

  api("/health")
    .then((h) => {
      if (!h.total) toast("库里还没有数据，去「来源」页点「采集全部」试试");
    })
    .catch(() => {});
})();
