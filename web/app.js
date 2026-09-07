/* N字战法 交易系统 Web 前端 —— 无外部依赖, 全部使用同源相对路径 /api/* */
"use strict";

/* 子路径部署支持: 以当前脚本地址推导站点前缀(如部署在 /N/ 下则为 /N, 根目录部署则为空),
   之后所有 /api/* 请求自动带上前缀, 由 nginx location ^~ /N/ 转发到后端。 */
const _scriptSrc = (document.currentScript && document.currentScript.src) || "";
const APP_BASE = _scriptSrc ? _scriptSrc.replace(/\/app\.js(\?.*)?(#.*)?$/, "") : "";
const apiUrl = (path) => APP_BASE + path;

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const cfg = { headers: { "Content-Type": "application/json" }, ...opts };
  if (cfg.body && typeof cfg.body !== "string") cfg.body = JSON.stringify(cfg.body);
  const r = await fetch(apiUrl(path), cfg);
  const text = await r.text();              // 兼容各种响应体, 失败时能给出真实状态与内容
  let j = null;
  try { j = text ? JSON.parse(text) : null; } catch (e) { j = null; }
  if (!r.ok || !j || j.ok === false) {
    const why = (j && j.error) ? j.error
      : `HTTP ${r.status} ${r.statusText || ""}${text ? " → " + text.slice(0, 200) : ""}`;
    throw new Error(why);
  }
  return j;
}

/* ---------------- 涨跌颜色 ---------------- */
function updown(v) { return v > 0 ? "up" : v < 0 ? "down" : "flat"; }
function pn(v, d = 2, sign = true) {
  const s = (sign && v > 0 ? "+" : "") + Number(v).toFixed(d);
  return `<span class="${updown(v)}">${s}</span>`;
}
function nm(v, d = 2) { return `<span class="${updown(v)}">${Number(v).toFixed(d)}</span>`; }
const fmtMoney = (v) => Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const PHASE = { pre: "集合竞价", am: "上午交易中", lunch: "午间休市", pm: "下午交易中", post: "已收盘(复盘窗口)", closed: "休市" };
const KIND_TAG = { b1: '<span class="tag b1">B1回踩</span>', b2: '<span class="tag b2">B2突破</span>',
                   watch: '<span class="tag watch">观察</span>', manual: '<span class="tag manual">人工</span>' };

/* ================= 顶部状态 ================= */
let META = null;
async function refreshMeta() {
  try {
    const j = await api("/api/meta");
    META = j.data;
    const d = META.data = META.data || {};
    const c = META.clock, eng = META.engine_enabled;
    const v = META.version ? `v${META.version.no} ${META.version.name}` : "未初始化";
    const env = META.env || {};
    const open = !!(c.weekday < 5 && (c.phase === "am" || c.phase === "pm"));
    const h = document.createElement("span");
    h.innerHTML =
      `<span class="status-dot" style="background:${open ? "#4ade80" : "#fbbf24"}"></span>` +
      `${c.date} ${PHASE[c.phase] || c.phase} · ` +
      `<b>${eng ? "引擎:运行中" : "引擎:已暂停"}</b> · 版本<b>${esc(v)}</b> · ` +
      `环境<b class="${env.mode === "full" ? "up" : env.mode === "off" ? "down" : ""}">${env.mode === "full" ? "正常" : env.mode === "half" ? "震荡(半仓)" : "退潮(暂停)"}</b> · ` +
      `池${META.pool_count} 持仓${META.open_positions}`;
    $("#hstatus").replaceChildren(h);
  } catch (e) { console.warn("meta", e); }
}
setInterval(refreshMeta, 4000);

/* ================= 通用表格工具 ================= */
function table(head, rows) {
  if (!rows.length) return `<p class="muted pad">暂无数据</p>`;
  const th = rows[0].map ? "" : "";
  const hd = head.map((h) => `<th class="${h[1] || ""}">${h[0]}</th>`).join("");
  const bd = rows.map((r) => "<tr>" + head.map((h, i) => `<td class="${h[1] || ""}">${r[i]}</td>`).join("") + "</tr>").join("");
  return `<div class="table-wrap"><table class="grid"><thead><tr>${hd}</tr></thead><tbody>${bd}</tbody></table></div>`;
}
function toast(msg, bad) {
  const d = document.createElement("div");
  d.textContent = msg;
  Object.assign(d.style, { position: "fixed", top: "70px", right: "20px", zIndex: 99, background: bad ? "#dc2626" : "#15803d",
    color: "#fff", padding: "10px 16px", borderRadius: "8px", boxShadow: "0 4px 12px #0003" });
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 3200);
}
async function confirmDlg(title, text, okLabel) {
  return new Promise((res) => {
    openModal(title, `<p class="pad">${esc(text)}</p>
      <div class="pad" style="display:flex;gap:10px">
        <button class="btn primary" id="dlg-ok">${esc(okLabel || "确定")}</button>
        <button class="btn" id="dlg-no">取消</button></div>`);
    $("#dlg-ok").onclick = () => { closeModal(); res(true); };
    $("#dlg-no").onclick = () => { closeModal(); res(false); };
  });
}

/* ================= Modal ================= */
function openModal(title, html) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  $("#modal-mask").style.display = "flex";
}
function closeModal() { $("#modal-mask").style.display = "none"; }
$("#modal-close").onclick = closeModal;
$("#modal-mask").addEventListener("click", (e) => { if (e.target.id === "modal-mask") closeModal(); });

/* ================= 标签页切换 ================= */
let curTab = "dash";
$$("#nav button").forEach((b) => b.onclick = () => switchTab(b.dataset.tab));
function switchTab(t) {
  curTab = t;
  $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
  $$(".tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + t));
  loadTab(t);
}
function loadTab(t) {
  if (t === "dash") loadDash();
  if (t === "pool") loadPool();
  if (t === "trades") loadTrades();
  if (t === "strategy") loadStrategy();
  if (t === "stats") loadStats();
  if (t === "backtest") loadBacktests();
  if (t === "settings") loadSettings();
}
setInterval(() => { if (curTab === "pool") loadPool(); if (curTab === "trades") loadTrades(); }, 8000);
setInterval(() => { if (curTab === "dash") loadDash(); if (curTab === "stats") loadStats(); }, 6000);
setInterval(() => { if (curTab === "backtest") loadBacktests(); }, 3500);

/* ================= 仪表盘 ================= */
async function loadDash() {
  try {
    const [meta, stats] = await Promise.all([api("/api/meta"), api("/api/stats")]);
    const m = meta.data, st = stats.data || {};
    const s = st.stats_all || {}, sync = m.sync || {};
    const pct = sync.total ? Math.round((sync.done || 0) / sync.total * 100) : (m.bootstrap_done ? 100 : 0);
    const kpis = [
      ["账户权益", fmtMoney(st.equity ?? 0), `${st.open_positions || 0}持仓 / 已实现${fmtMoney(st.realized ?? 0)}`, updown(st.equity - (st.base || 0)) === "up" ? "up" : updown(st.equity - (st.base || 0))],
      ["成功率(已平仓)", s.winrate != null ? s.winrate + "%" : "—", `盈利${s.wins ?? 0}/${s.n ?? 0}笔`, updown((s.winrate || 0) - 50)],
      ["盈亏比", s.profit_loss_ratio ?? "—", `均盈${s.avg_win_pct ?? 0}% / 均亏${Math.abs(s.avg_loss_pct || 0)}%`, updown((s.profit_loss_ratio || 0) - 1)],
      ["总已实现盈亏", fmtMoney(st.realized ?? 0), `含浮盈权益 ${fmtMoney(st.equity ?? 0)}`, updown(st.realized)],
      ["最大回撤", (st.max_drawdown_pct ?? 0) + "%", "", "down"],
      ["交易笔数", s.n ?? 0, `最近扫描信号 ${m.last_scan_count || 0} 个`, "flat"],
      ["推荐池/持仓", `${m.pool_count} / ${m.open_positions}`, `全市场 ${m.universe_count} 只`, "flat"],
      ["数据同步", m.bootstrap_done ? "已就绪" : "同步中…", `K线缓存 ${m.bar_codes} 只`, pct >= 100 ? "up" : "flat"],
    ];
    const uniWarn = (m.universe_count || 0) === 0
      ? `<div class="kpi" style="grid-column:1/-1;border-color:#e6a3a3;background:#fff6f6">
           <div class="k">⚠️ 股票列表未就绪</div>
           <div class="v" style="font-size:15px">全市场股票列表(约5500只)尚未抓取成功</div>
           <div class="s">点击下方【同步历史K线】手动重试, 系统也会每25秒自动重试; 若持续失败请检查服务器到数据源的连通性(/api/diag)</div></div>`
      : "";
    $("#dash-cards").innerHTML = uniWarn + kpis.map(([k, v, s2, cl]) =>
      `<div class="kpi"><div class="k">${esc(k)}</div><div class="v ${esc(cl)}">${v}</div><div class="s">${esc(s2)}</div></div>`).join("");

    const env = m.env || {};
    $("#dash-env").innerHTML =
      `<p><b>环境模式:</b> <span class="badge ${esc(env.mode)}">${env.mode === "full" ? "正常·满仓运行" : env.mode === "half" ? "震荡·半仓" : "退潮·暂停开仓"}</span></p>
       <p class="muted">${esc(env.reason || "—")}</p>
       <hr style="margin:8px 0;border:none;border-top:1px solid #eee">
       <p><b>最近扫描:</b> ${esc(m.last_scan_ts || "尚未")} · 信号 ${m.last_scan_count || 0} 个</p>
       <p><b>最近快照:</b> ${esc(m.last_fast_ts || "—")}</p>
       <p style="margin-top:8px"><b>数据同步:</b> ${pct}%
         <span class="progress" style="display:inline-block;vertical-align:middle"><i style="width:${pct}%"></i></span></p>
       <p style="margin-top:8px">
         <button class="btn primary" id="btn-scan">立即扫描</button>
         <button class="btn" id="btn-close">收盘复盘</button>
         <button class="btn" id="btn-sync">同步历史K线</button></p>`;
    $("#btn-scan").onclick = async () => { try { await api("/api/engine/scan", { method: "POST", body: {} }); toast("扫描任务已提交(异步), 稍候查看仪表盘/推荐池"); setTimeout(loadDash, 3000); } catch (e) { toast(e.message, 1); } };
    $("#btn-close").onclick = async () => { try { await api("/api/engine/close", { method: "POST", body: {} }); toast("收盘复盘任务已提交(异步)"); setTimeout(loadDash, 3000); } catch (e) { toast(e.message, 1); } };
    $("#btn-sync").onclick = async () => { try { await api("/api/engine/sync", { method: "POST", body: {} }); toast("同步任务已提交(异步), 列表+K线将在后台进行"); setTimeout(loadDash, 3000); } catch (e) { toast(e.message, 1); } };
    const logs = (await api("/api/logs?limit=60")).data;
    $("#dash-logs").innerHTML = logs.map((l) =>
      `<div>${esc(l.ts)} [${esc(l.level)}] ${esc(l.msg)}</div>`).join("") || '<div class="muted">暂无</div>';
  } catch (e) { $("#dash-cards").innerHTML = `<div class="card pad">加载失败: ${esc(e.message)}</div>`; }
}

/* ================= 推荐池 ================= */
let poolData = [];
async function loadPool() {
  try {
    const j = await api("/api/pool");
    poolData = j.data;
    renderPool();
  } catch (e) { $("#pool-table").outerHTML = `<p class="pad">加载失败 ${esc(e.message)}</p>`; }
}
function renderPool() {
  const f = $("#pool-filter").value;
  let rows = poolData;
  if (f) rows = rows.filter((r) => (f === "manual" ? r.source === "manual" : r.signal === f));
  $("#pool-tip").textContent = `当前 ${rows.length} 只(信号类为系统实时自动筛选; 点击代码查看K线与信号明细)`;
  const head = [["代码", "l"], ["名称", "l"], ["板块"], ["现价"], ["涨跌额"], ["涨跌幅"], ["来源"], ["信号"], ["阶段", "l"], ["信号日"], ["点火日"], ["回调(天)"], ["参考买价"], ["状态"], ["操作"]];
  const body = rows.map((r) => [
    `<a class="lnk" data-code="${esc(r.code)}" href="javascript:void(0)">${esc(r.code)}</a>`,
    `<span class="${updown(r.pct_chg)}">${esc(r.name)}</span>`,
    esc(r.board || ""),
    nm(r.price ?? 0, 3),
    r.change != null ? pn(r.change, 3, false) : "—",
    pn(r.pct_chg ?? 0, 2),
    r.source === "manual" ? '<span class="tag manual">人工</span>' : '<span class="tag auto">自动</span>',
    KIND_TAG[r.signal] || "—",
    esc(r.stage || ""),
    esc(r.signal_date || ""),
    esc(r.ignite_date || ""),
    r.pull_days != null ? r.pull_days : "—",
    nm(r.ref_price ?? 0, 3),
    r.open_pos ? '<span class="tag pos">已持仓</span>' : "",
    `<button class="btn danger rm" data-code="${esc(r.code)}">移除</button>`,
  ]);
  const el = $("#pool-table");
  el.innerHTML = buildTableHTML(head, body);
  $$("a.lnk", el).forEach((a) => a.onclick = () => showStockDetail(a.dataset.code));
  $$("button.rm", el).forEach((b) => b.onclick = async () => {
    if (await confirmDlg("移出推荐池", `确定将 ${b.dataset.code} 移出推荐买入池?`, "移除"))
      try { await api("/api/pool/remove", { method: "POST", body: { code: b.dataset.code } }); toast("已移除"); loadPool(); } catch (e) { toast(e.message, 1); }
  });
}
function buildTableHTML(head, rows) {
  const hd = head.map((h) => `<th class="${h[1] || ""}">${h[0]}</th>`).join("");
  const bd = rows.map((r) => "<tr>" + r.map((c, i) => `<td class="${head[i][1] || ""}">${c}</td>`).join("") + "</tr>").join("");
  return `<thead><tr>${hd}</tr></thead><tbody>${bd}</tbody>`;
}
$("#pool-filter").onchange = renderPool;

/* 手动加入 */
$("#pool-add").onclick = async () => {
  const kw = $("#pool-search").value.trim();
  if (!kw) return toast("请输入代码或名称", 1);
  const j = await api("/api/search?kw=" + encodeURIComponent(kw));
  const list = j.data;
  if (!list.length) return toast("未找到该股票", 1);
  const exact = list.find((x) => x.code === kw);
  if (exact) {
    try { await api("/api/pool/manual", { method: "POST", body: { code: exact.code } }); toast(`已加入: ${exact.name}`); loadPool(); }
    catch (e) { toast(e.message, 1); }
  } else {
    openModal("选择要加入的股票", '<div class="pad">' + list.slice(0, 20).map((x) =>
      `<button class="btn pk" data-code="${esc(x.code)}" style="margin:3px">${esc(x.code)} ${esc(x.name)} [${esc(x.board)}]</button>`).join("") + "</div>");
    $$("#modal .pk").forEach((b) => b.onclick = async () => {
      try { await api("/api/pool/manual", { method: "POST", body: { code: b.dataset.code } });
        toast("已加入推荐池"); closeModal(); loadPool(); } catch (e) { toast(e.message, 1); }
    });
  }
};

/* 个股详情(含K线) */
async function showStockDetail(code) {
  try {
    const [k, exe] = await Promise.all([
      api("/api/kline/" + code + "?days=180"),
      api("/api/executions?code=" + code + "&limit=30")]);
    const s = k.data.series, name = k.data.name || code;
    const poolRow = poolData.find((r) => r.code === code);
    const matched = (poolRow && poolRow.matched) || [];
    const reason = (poolRow && poolRow.reason) || "";
    openModal(`${code} ${name} — K线 / 信号 / 交易记录`, `
      <canvas id="kline-canvas" class="chart"></canvas>
      ${poolRow ? `<h4>推荐池信息(${esc(poolRow.stage || "")})</h4>
        <p class="muted" style="font-size:12px">${esc(reason)}</p>
        <div class="table-wrap"><table class="grid"><tbody>
        ${matched.map((m) => `<tr><td>${m.ok ? "✅" : "❌"} ${esc(m.name)}</td><td class="l">${esc(m.detail || "")}</td></tr>`).join("") || '<tr><td>无明细</td></tr>'}
        </tbody></table></div>` : ""}
      <h4>系统交易记录</h4>
      <div class="table-wrap"><table class="grid">
        ${buildTableHTML([["时间", "l"], ["方向"], ["价格"], ["股数"], ["版本"], ["理由", "l"]],
          exe.data.map((e) => [esc(e.dt), e.side === "buy" ? '<span class="up">买入</span>' : '<span class="down">卖出</span>',
            e.price, e.shares, e.version_no ? "v" + e.version_no : "—", esc((e.reason || "").slice(0, 120))]))}
      </table></div>`);
    setTimeout(() => drawKline("kline-canvas", s, k.data.markers || []), 30);
  } catch (e) { toast(e.message, 1); }
}

/* ================= 交易系统买卖池 ================= */
let subTrade = "open";
$$("#tab-trades .subbtn").forEach((b) => b.onclick = () => {
  subTrade = b.dataset.sub;
  $$("#tab-trades .subbtn").forEach((x) => x.classList.toggle("active", x === b));
  $$("#tab-trades .subview").forEach((x) => x.style.display = "none");
  $("#sub-" + subTrade).style.display = "";
  loadTrades();
});
async function loadTrades() {
  const [open, closed, exec] = await Promise.all([api("/api/positions?status=open"), api("/api/positions?status=closed"), api("/api/executions?limit=200")]);
  const oh = [["代码", "l"], ["名称", "l"], ["开仓时间"], ["买入价"], ["股数"], ["金额"], ["现价"], ["浮盈%"], ["止损"], ["目标"], ["已实现"], ["版本"], ["信号"], ["开仓理由", "l"], ["操作"]];
  $("#sub-open").innerHTML = table(oh, open.data.map((p) => [
    esc(p.code), esc(p.name), esc(p.entry_dt), p.entry_price, p.entry_shares, fmtMoney(p.entry_amount),
    nm(p.price ?? 0, 3), pn(p.float_pct ?? 0, 2), p.stop_price, p.target_price,
    nm(p.realized_pnl || 0, 2), p.version_no ? "v" + p.version_no : "—",
    KIND_TAG[p.signal] || "—", esc((p.entry_reason || "").slice(0, 110)),
    `<button class="btn danger" data-id="${p.id}">平仓</button>`]));
  $$("#sub-open button[data-id]").forEach((b) => b.onclick = async () => {
    const note = prompt("平仓备注(可选):");
    try { await api("/api/positions/close", { method: "POST", body: { id: Number(b.dataset.id), note: note || "" } }); toast("已执行平仓"); loadTrades(); loadStats(); } catch (e) { toast(e.message, 1); }
  });
  const ch = [["代码", "l"], ["名称", "l"], ["开仓", "l"], ["平仓", "l"], ["买入价"], ["卖出价"], ["持股(天)"], ["盈亏额"], ["盈亏%"], ["版本"], ["信号"], ["平仓原因", "l"], ["失败备注", "l"]];
  $("#sub-closed").innerHTML = table(ch, closed.data.map((p) => [
    esc(p.code), esc(p.name), esc(p.entry_dt), esc(p.closed_dt || ""), p.entry_price,
    p.exit_price ? nm(p.exit_price, 3) : "—", p.holding_days ?? "—",
    pn(p.realized_pnl ?? 0, 2), pn(p.realized_pnl_pct ?? 0, 2),
    p.version_no ? "v" + p.version_no : "—", KIND_TAG[p.signal] || "—",
    esc((p.exit_reason || "").slice(0, 80)), esc((p.failure_note || p.note || "").slice(0, 140))]));
  const eh = [["时间", "l"], ["代码", "l"], ["名称", "l"], ["方向"], ["成交价"], ["股数"], ["金额"], ["手续费"], ["模式"], ["版本"], ["理由", "l"]];
  $("#sub-exec").innerHTML = table(eh, exec.data.map((e) => [
    esc(e.dt), esc(e.code), esc(e.name), e.side === "buy" ? '<span class="up">买入</span>' : '<span class="down">卖出</span>',
    e.price, e.shares, fmtMoney(e.amount), fmtMoney(e.fee), e.mode === "manual" ? "人工" : "系统",
    e.version_no ? "v" + e.version_no : "—", esc((e.reason || "").slice(0, 120))]));
}

/* ================= 交易系统 ================= */
async function loadStrategy() {
  const [cur, vs, opts, r5] = await Promise.all([
    api("/api/strategy/current"), api("/api/strategy/versions"),
    api("/api/optimizations"), api("/api/stats/rule5")]);
  const c = cur.data.version, cp = cur.data.params;
  const fields = (obj) => Object.entries(obj || {}).map(([k, v]) =>
    `<tr><td class="l">${esc(k)}</td><td>${esc(JSON.stringify(v))}</td></tr>`).join("");
  $("#str-current").innerHTML = `
    <p><b>v${c.version_no} ${esc(c.name)}</b> <span class="badge full">当前执行</span>
       <span class="muted">来源:${esc(c.source)} · 创建:${esc(c.created_at)}</span></p>
    <p class="muted" style="margin:6px 0">${esc(c.reason || "")}</p>
    <pre class="text" style="max-height:300px">${esc(c.readable || "")}</pre>
    <details><summary>完整参数(JSON)</summary>
      <table class="grid"><tbody>${fields(cp)}</tbody></table></details>`;
  $("#str-rule5").innerHTML = (() => {
    const r = r5.data;
    const rows = (r.evaluated || []).map((x) =>
      `<tr><td>${esc(x.window_end)}</td><td>${x.n}</td><td class="${updown(x.winrate - 40)}">${x.winrate}%</td>
       <td class="${updown(x.rrr - 0.6)}">${x.rrr}</td><td>${x.bad ? '<span class="up">不达标</span>' : '<span class="down">达标</span>'}</td></tr>`).join("");
    return `<p>${esc(r.reason || "")}</p>
      <table class="grid"><thead><tr><th class="l">窗口末日</th><th>样本</th><th>成功率</th><th>盈亏比</th><th>判定</th></tr></thead>
      <tbody>${rows || '<tr><td colspan=5 class="muted l">暂无足够样本</td></tr>'}</tbody></table>
      <p class="hint">触发条件: 连续5个交易日, 每个近5日窗口均 盈亏比&lt;0.6 且 成功率&lt;40%(窗口平仓≥3笔)。达标判定随持仓浮盈回调即时更新。</p>
      <button class="btn primary" id="btn-opt-manual" style="margin-top:8px">手动发起系统优化</button>`;
  })();
  const o = $("#btn-opt-manual");
  if (o) o.onclick = async () => {
    const reason = prompt("优化原因(可选):", "用户手动发起优化");
    if (reason === null) return;
    try { const r = await api("/api/optimize/manual", { method: "POST", body: { reason } });
      toast("优化完成, 已切换到新版本"); loadStrategy(); } catch (e) { toast(e.message, 1); }
  };
  $("#str-versions").innerHTML = table(
    [["版本", "l"], ["名称", "l"], ["来源"], ["触发"], ["创建时间", "l"], ["状态"], ["说明", "l"], ["操作"]],
    vs.data.map((v) => [
      "v" + v.version_no, esc(v.name), esc(v.source),
      v.trigger === "initial" ? "初始" : v.trigger === "rule5_auto" ? "规则5自动" : v.trigger === "monthly" ? "月度自动" : "手动",
      esc(v.created_at), v.is_active ? '<span class="badge full">执行中</span>' : "",
      esc((v.reason || "").slice(0, 80)),
      v.is_active ? "" : `<button class="btn act" data-id="${v.id}">切换执行</button>`]));
  $$("#str-versions button.act").forEach((b) => b.onclick = async () => {
    if (await confirmDlg("切换交易系统版本", "将使用该历史版本执行后续买卖操作(存量持仓仍按原版本记录)。确认切换?", "切换"))
      try { await api("/api/strategy/activate", { method: "POST", body: { id: Number(b.dataset.id) } }); toast("已切换"); loadStrategy(); } catch (e) { toast(e.message, 1); }
  });
  $("#str-opts").innerHTML = table(
    [["时间", "l"], ["触发"], ["旧→新"], ["统计背景", "l"], ["变更", "l"]],
    opts.data.map((x) => [esc(x.created_at),
      x.trigger === "rule5_auto" ? "规则5自动" : x.trigger === "monthly" ? "月度" : "手动",
      esc(`v${x.old_version_id ?? "?"}→v${x.new_version_id ?? "?"}`),
      esc((x.stats_before || "").slice(0, 120)), esc((x.changed || "").slice(0, 160))]));
}

/* ================= 复盘统计 ================= */
async function loadStats() {
  const [st, monthly, reviews] = await Promise.all([api("/api/stats"), api("/api/stats/monthly"), api("/api/reviews")]);
  const a = st.data, s = a.stats_all || {};
  const cards = [
    ["账户权益(含浮盈)", fmtMoney(a.equity ?? 0), `初始 ${fmtMoney(a.base)}`],
    ["已实现盈亏", fmtMoney(a.realized ?? 0), `浮盈 ${fmtMoney(a.unrealized ?? 0)}`],
    ["成功率", s.n ? s.winrate + "%" : "—", `盈利 ${s.wins}/${s.n} 笔`],
    ["盈亏比", s.n ? s.profit_loss_ratio : "—", `均盈${s.avg_win_pct}% vs 均亏${Math.abs(s.avg_loss_pct || 0)}%`],
    ["最大回撤", a.max_drawdown_pct + "%", "基于已实现权益"],
    ["平仓总笔数", s.n ?? 0, `持仓中 ${a.open_positions}`],
  ];
  $("#stats-cards").innerHTML = cards.map(([k, v, sub]) =>
    `<div class="kpi"><div class="k">${esc(k)}</div><div class="v">${v}</div><div class="s">${esc(sub)}</div></div>`).join("");
  drawEquity("eq-canvas", a.curve || [], a.base || 0);
  $("#monthly-table").innerHTML = table([["月份", "l"], ["平仓笔数"], ["盈利"], ["成功率"], ["盈亏比"], ["月盈亏"]],
    monthly.data.map((r) => [esc(r.ym), r.n, r.wins, r.winrate + "%", r.profit_loss_ratio, pn(r.pnl, 2)]));
  $("#reviews-list").innerHTML = reviews.data.map((r) =>
    `<div class="review-item" data-id="${r.id}"><b>[${r.rtype === "daily" ? "每日" : "月度"}] ${esc(r.date)}</b>
     <span class="muted">${esc(r.title)}</span>
     <div class="muted" style="font-size:12px">备注: ${esc((r.notes || "—").slice(0, 160))}</div></div>`).join("");
  $$("#reviews-list .review-item").forEach((el) => el.onclick = () => {
    const r = reviews.data.find((x) => x.id === Number(el.dataset.id));
    openModal(`${r.rtype === "daily" ? "每日" : "月度"}复盘 · ${r.date}`,
      `<pre class="text">${esc(r.content || "")}</pre>`);
  });
}

/* ================= 回测 ================= */
let btList = [];
async function loadBacktests() {
  const j = await api("/api/backtests");
  btList = j.data;
  const vs = await api("/api/strategy/versions");
  const sel = $("#bt-ver");
  if (sel && !sel.options.length) vs.data.forEach((v) => {
    const o = document.createElement("option");
    o.value = v.id; o.text = `v${v.version_no} ${v.name}` + (v.is_active ? " (当前)" : "");
    sel.appendChild(o);
  });
  renderBtList();
}
$("#bt-run").onclick = async () => {
  const body = { start: $("#bt-start").value, end: $("#bt-end").value,
    version_id: Number($("#bt-ver").value) || 0, capital: Number($("#bt-cap").value) || 1e6 };
  if (!body.start || !body.end) return toast("请选择回测区间", 1);
  try { await api("/api/backtests", { method: "POST", body }); toast("回测任务已创建"); loadBacktests(); }
  catch (e) { toast(e.message, 1); }
};
function renderBtList() {
  const box = $("#bt-list");
  if (!btList.length) { box.innerHTML = '<p class="muted pad">暂无回测任务</p>'; return; }
  box.innerHTML = btList.map((b) => {
    let p = {}; try { p = JSON.parse(b.params || "{}"); } catch (e) {}
    let sm = {}; try { sm = JSON.parse(b.summary || "{}"); } catch (e) {}
    const st = b.status;
    return `<div class="pad" style="border-bottom:1px solid #eef">
      <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
        <b>#${b.id}</b> ${esc(p.start || "")} ~ ${esc(p.end || "")}
        ${p.version_no ? `<span class="tag auto">v${p.version_no}</span>` : ""}
        <span class="badge ${st === "done" ? "full" : st === "failed" || st === "cancelled" ? "off" : "half"}">${esc({ running: "运行中…", done: "完成", failed: "失败", cancelled: "已取消", cancelling: "取消中" }[st] || st)}</span>
        <span class="progress" style="flex:1;max-width:220px"><i style="width:${b.progress || 0}%"></i></span>
        <span class="muted">${(b.progress || 0).toFixed(0)}%</span>
        ${st === "running" ? `<button class="btn danger" data-cancel="${b.id}">取消</button>` : ""}
        ${st === "done" ? `<button class="btn" data-view="${b.id}">查看结果</button>` : ""}
      </div>
      ${sm.n_trades != null ? `<div class="muted" style="font-size:12px;margin-top:4px">平仓${sm.n_trades}笔 · 成功率${sm.winrate}% · 盈亏比${sm.profit_loss_ratio} · 总收益${fmtMoney(sm.total_pnl)}(${sm.total_pnl_pct}%) · 最大回撤${sm.max_drawdown_pct}% · 最终权益${fmtMoney(sm.final_equity)}</div>` : ""}
      ${b.error ? `<div class="down" style="font-size:12px">${esc(b.error)}</div>` : ""}
    </div>`;
  }).join("");
  $$("#bt-list [data-view]").forEach((b) => b.onclick = () => showBacktest(Number(b.dataset.view)));
  $$("#bt-list [data-cancel]").forEach((b) => b.onclick = async () => {
    try { await api("/api/backtests/" + b.dataset.cancel + "/cancel", { method: "POST", body: {} }); toast("正在取消"); } catch (e) { toast(e.message, 1); }
  });
}
async function showBacktest(id) {
  const j = await api("/api/backtests/" + id);
  const r = j.data, s = r.summary || {};
  openModal(`回测 #${id} 结果(v${s.version_no || ""})`,
    `<div class="cards">
      ${[["平仓笔数", s.n_trades ?? 0], ["成功率", s.winrate != null ? s.winrate + "%" : "—"],
         ["盈亏比", s.profit_loss_ratio ?? "—"], ["总收益", fmtMoney(s.total_pnl) + " (" + s.total_pnl_pct + "%)"],
         ["最大回撤", (s.max_drawdown_pct ?? 0) + "%"], ["最终权益", fmtMoney(s.final_equity)]]
        .map(([k, v]) => `<div class="kpi"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`).join("")}
    </div>
    <canvas id="bt-canvas" class="chart"></canvas>
    <h4>交易明细(可复盘)</h4>
    <div class="table-wrap"><table class="grid">${btTable(r.trades || [])}</table></div>`);
  drawEquity("bt-canvas", r.equity || [], s.capital || 1e6);
}
function btTable(trades) {
  if (!trades.length) return "<tr><td>无平仓交易</td></tr>";
  const h = "<thead><tr><th class='l'>代码</th><th class='l'>名称</th><th>开仓</th><th>平仓</th><th>买价</th><th>卖价</th><th>股数</th><th>盈亏额</th><th>盈亏%</th><th class='l'>平仓原因</th></tr></thead>";
  const b = trades.map((t) => `<tr><td class='l'>${esc(t.code)}</td><td class='l'>${esc(t.name)}</td>
    <td>${esc((t.entry_dt || "").slice(0, 10))}</td><td>${esc((t.exit_dt || "").slice(0, 10))}</td>
    <td>${t.entry_price}</td><td>${t.exit_price}</td><td>${t.shares}</td>
    <td class="${updown(t.pnl)}">${pn(t.pnl, 2)}</td><td class="${updown(t.pnl_pct)}">${pn(t.pnl_pct, 2)}</td>
    <td class='l' style='white-space:normal'>${esc(String(t.exit_reason || "").slice(0, 80))}</td></tr>`).join("");
  return h + "<tbody>" + b + "</tbody>";
}

/* ================= 设置 ================= */
async function loadSettings() {
  const [cfg, logs] = await Promise.all([api("/api/config"), api("/api/logs?limit=400")]);
  const c = cfg.data, e = c.engine || {};
  $("#set-panel").innerHTML = `
    <div style="display:flex;gap:30px;flex-wrap:wrap;align-items:center">
      <div><label class="muted" style="display:block">交易引擎自动执行</label>
        <label class="switch"><input type="checkbox" id="eng-sw" ${e.enabled ? "checked" : ""}><span class="slider"></span></label>
        <span class="hint">暂停后系统仍实时筛选/入池, 但不再自行买卖</span></div>
      <div><label class="muted">行情刷新间隔(秒)</label><input id="cfg-refresh" type="number" value="${c.scheduler.data_refresh_sec}"></div>
      <div><label class="muted">全市场扫描间隔(秒)</label><input id="cfg-scan" type="number" value="${c.scheduler.scan_interval_sec}"></div>
      <div><button class="btn primary" id="cfg-save">保存设置</button></div>
      <div><span class="muted">初始虚拟资金: ${fmtMoney(c.base_capital)} 元(纸面交易)</span></div>
    </div>
    <h4 style="margin-top:12px">当前交易系统规则(可读版)</h4>
    <pre class="text">${esc(c.strategy_readable || "")}</pre>`;
  $("#eng-sw").onchange = async () => {
    try { await api("/api/engine/toggle", { method: "POST", body: { enabled: $("#eng-sw").checked } });
      toast("引擎" + ($("#eng-sw").checked ? "已开启" : "已暂停")); } catch (e) { toast(e.message, 1); }
  };
  $("#cfg-save").onclick = async () => {
    try { await api("/api/config", { method: "PUT", body: { data_refresh_sec: Number($("#cfg-refresh").value), scan_interval_sec: Number($("#cfg-scan").value) } });
      toast("设置已保存(实时生效)"); } catch (e) { toast(e.message, 1); }
  };
  $("#full-logs").textContent = logs.data.map((l) => `${l.ts} [${l.level}] ${l.msg}`).join("\n");
}

/* ================= 图表 ================= */
function setupCanvas(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || cv.parentElement.clientWidth || 800;
  const h = cv.clientHeight || 300;
  cv.width = w * dpr; cv.height = h * dpr;
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}
function drawEquity(id, curve, base) {
  const cv = document.getElementById(id);
  if (!cv) return;
  const { ctx, w, h } = setupCanvas(cv);
  if (!curve.length) { ctx.fillStyle = "#889"; ctx.fillText("暂无权益数据", 20, 30); return; }
  const min = Math.min(base, ...curve.map((c) => c.equity));
  const max = Math.max(base, ...curve.map((c) => c.equity));
  const pad = 16;
  const X = (i) => pad + (w - pad * 2) * (i / Math.max(1, curve.length - 1));
  const Y = (v) => h - pad - (h - pad * 2) * ((v - min) / Math.max(1e-9, max - min));
  // 基线
  ctx.strokeStyle = "#e5eaf1"; ctx.beginPath(); ctx.moveTo(pad, Y(base)); ctx.lineTo(w - pad, Y(base)); ctx.stroke();
  ctx.fillStyle = "#889"; ctx.font = "11px sans-serif";
  ctx.fillText("初始 " + fmtMoney(base), pad + 4, Y(base) - 4);
  const pts = [{ i: 0, v: base }, ...curve.map((c, i) => ({ i: i + 1, v: c.equity }))];
  const grad = ctx.createLinearGradient(0, pad, 0, h - pad);
  grad.addColorStop(0, "rgba(47,107,216,.18)"); grad.addColorStop(1, "rgba(47,107,216,.01)");
  ctx.beginPath();
  pts.forEach((p, k) => { const x = X(p.i), y = Y(p.v); k ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.strokeStyle = "#2f6bd8"; ctx.lineWidth = 1.8; ctx.stroke();
  // 填充
  ctx.lineTo(X(pts[pts.length - 1].i), h - pad); ctx.lineTo(X(0), h - pad); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  const last = curve[curve.length - 1];
  ctx.fillStyle = "#1f3a68"; ctx.font = "12px sans-serif";
  ctx.fillText(`${last.date}  权益 ${fmtMoney(last.equity)}`, pad + 4, 14);
}
function drawKline(id, s, markers) {
  const cv = document.getElementById(id);
  if (!cv) return;
  const { ctx, w, h } = setupCanvas(cv);
  const n = s.dates.length;
  if (!n) { ctx.fillText("暂无K线", 20, 30); return; }
  const cw = Math.max(2, Math.floor((w - 12) / n) - 1);
  const mainH = h * 0.72, volH = h * 0.16, gap = 6;
  const closes = s.close, highs = s.high, lows = s.low;
  const lo = Math.min(...lows), hi = Math.max(...highs);
  const X = (i) => 8 + i * (cw + 1) + cw / 2;
  const Y = (v) => 6 + (mainH - 10) * (1 - (v - lo) / Math.max(1e-9, hi - lo));
  const volMax = Math.max(1, ...s.vol);
  function ma(arr, k, i) { if (i < k - 1) return null; let s2 = 0; for (let j = i - k + 1; j <= i; j++) s2 += arr[j]; return s2 / k; }
  ctx.font = "10px sans-serif";
  for (let i = 0; i < n; i++) {
    const up = closes[i] >= (i ? closes[i - 1] : closes[i]);
    const col = up ? "#e02f2f" : "#00a854";
    ctx.strokeStyle = col; ctx.fillStyle = col;
    const o = Y((i ? closes[i - 1] : s.open[i])), c = Y(closes[i]);
    const x = X(i);
    ctx.beginPath(); ctx.moveTo(x, Y(highs[i])); ctx.lineTo(x, Y(lows[i])); ctx.stroke();
    ctx.fillRect(x - cw / 2, Math.min(o, c), cw, Math.max(1, Math.abs(c - o)));
    // 量
    const vh = (volH - 8) * (s.vol[i] / volMax);
    ctx.fillStyle = up ? "rgba(224,47,47,.5)" : "rgba(0,168,84,.5)";
    ctx.fillRect(x - cw / 2, 6 + mainH + gap + (volH - vh), cw, vh);
  }
  // MA5/10/20
  [[5, "#e6a23c"], [10, "#2f6bd8"], [20, "#9b59b6"]].forEach(([k, col]) => {
    ctx.strokeStyle = col; ctx.lineWidth = 1; ctx.beginPath(); let st = false;
    for (let i = k - 1; i < n; i++) { const v = ma(closes, k, i); if (v == null) continue;
      const x = X(i), y = Y(v); st ? ctx.lineTo(x, y) : ctx.moveTo(x, y); st = true; }
    ctx.stroke();
  });
  // 买卖标记
  (markers || []).forEach((mk2) => {
    const i = s.dates.indexOf(mk2.d);
    if (i < 0 || !mk2.p) return;
    const x = X(i), y = Y(Number(mk2.p));
    ctx.beginPath();
    if (mk2.t === "买") { ctx.fillStyle = "#e02f2f"; ctx.moveTo(x, y - 8); ctx.lineTo(x - 5, y); ctx.lineTo(x + 5, y); ctx.closePath(); ctx.fill(); }
    else { ctx.fillStyle = "#00a854"; ctx.moveTo(x, y + 8); ctx.lineTo(x - 5, y); ctx.lineTo(x + 5, y); ctx.closePath(); ctx.fill(); }
    ctx.fillStyle = "#fff"; ctx.font = "8px sans-serif";
    ctx.fillText(mk2.t, x - 3, mk2.t === "买" ? y - 1 : y + 5);
  });
  // 轴标签
  ctx.fillStyle = "#889"; ctx.font = "10px sans-serif";
  ctx.fillText(`${s.dates[0]}`, 8, h - 2);
  ctx.fillText(`${s.dates[n - 1]}`, w - 70, h - 2);
  ctx.fillText(`${lo.toFixed(2)}`, 2, mainH - 4);
  ctx.fillText(`${hi.toFixed(2)}`, 2, 12);
}

/* 默认日期: 近一年 */
(function initDates() {
  const now = new Date();
  const fmt = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const end = new Date(now); const start = new Date(now); start.setFullYear(now.getFullYear() - 1);
  if ($("#bt-end")) $("#bt-end").value = fmt(end);
  if ($("#bt-start")) $("#bt-start").value = fmt(start);
})();
refreshMeta();
loadDash();
