"use strict";
const $ = id => document.getElementById(id);
let csrf = "", overview = null, pendingConfirm = null, page = "dashboard", busy = false;
let accountMode = "browser", oauthFlow = null, oauthTimer = null, oauthGeneration = 0;
const labels = {
  dashboard: ["让每个账号，各尽其用", "WORKBUDDY WORKSPACE", "在这里查看服务运行、账号积分和请求表现。"],
  accounts: ["账号池", "ACCOUNT POOL", "集中管理登录凭据、积分与可用状态，让请求自动分配到可用账号。"],
  keys: ["API 密钥", "CLIENT ACCESS", "为每个客户端分配独立密钥，让连接清晰可控。"],
  models: ["模型与倍率", "MODEL RATES", "查看每个模型的官方倍率、限时免费促销与本机实测扣费。"],
  taskcenter: ["任务中心", "TASK CENTER", "扫描全部账号的待办任务，并按队列执行。"],
  usage: ["用量", "USAGE", "按账号、模型与域查看 token 消耗与请求表现。"],
  test: ["连接测试", "CONNECTION LAB", "从当前账号发起请求，确认模型能否正常响应。"],
  guide: ["接入指南", "GET CONNECTED", "从导入凭据到客户端接入，只需几步。"]
};
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const stamp = ts => ts ? new Date(ts).toLocaleString("zh-CN", {hour12:false}) : "未提供";
function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $("toast").hidden = true, 4500); }
function showLogin() {
  csrf = ""; overview = null; $("workspace").hidden = true; $("login").hidden = false; $("boot").hidden = true;
  document.querySelectorAll("dialog[open]").forEach(d => d.close()); $("admin-key").value = "";
}
async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = {"Content-Type":"application/json", ...(method !== "GET" ? {"X-CSRF-Token":csrf} : {})};
  const response = await fetch("/admin/api/" + path, {method, headers, credentials:"same-origin", body:options.body === undefined ? undefined : JSON.stringify(options.body)});
  let data; try { data = await response.json(); } catch { throw new Error("服务器暂时不可用，请稍后重试"); }
  if (!response.ok) {
    if (response.status === 401 && path !== "login") showLogin();
    throw new Error(typeof data.detail === "string" ? data.detail : "操作失败，请检查输入后重试");
  }
  return data;
}
function goPage(next) {
  if (!(next in labels)) next = "dashboard";
  page = next;
  document.querySelectorAll(".page-panel").forEach(el => el.hidden = el.id !== "page-" + next);
  document.querySelectorAll("[data-page]").forEach(el => { el.classList.toggle("selected", el.dataset.page === next); el.setAttribute("aria-current", el.dataset.page === next ? "page" : "false"); });
  const [title, kicker, desc] = labels[next];
  $("page-title").replaceChildren(document.createTextNode(title));
  const dot = document.createElement("span"); dot.className = "title-dot"; dot.textContent = "."; $("page-title").append(dot);
  $("breadcrumb").textContent = next === "dashboard" ? "概览" : title; $("page-kicker").textContent = kicker; $("page-desc").textContent = desc;
  history.replaceState(null, "", "#" + next);
  if (next === "models") loadModels().catch(e => toast(e.message));
  if (next === "usage") loadUsage().catch(e => toast(e.message));
  if (next === "taskcenter") loadTaskCenter().catch(e => toast(e.message));
}
const fmtRate = v => v === null || v === undefined ? "—" : "x" + Number(v).toFixed(2);
const fmtNum = v => v === null || v === undefined || v === "" ? "—" : Number(v).toLocaleString("zh-CN");
async function loadModels() {
  $("models-body").innerHTML = '<tr><td colspan="9" class="history-empty">正在加载模型目录…</td></tr>';
  renderModels(await api("models"));
  loadAccountModels().catch(e => { $("acct-models-body").textContent = e.message; });
}
async function loadAccountModels() {
  const d = await api("accounts/models");
  const rows = d.accounts || [];
  if (!rows.length) { $("acct-models-body").innerHTML = '<p class="history-empty">还没有账号。</p>'; return; }
  const rotating = overview?.pool?.routing === "round_robin";
  // 明确告诉用户 /v1/models 现在返回什么
  const activeRow = rows.find(a => a.active && a.enabled);
  const banner = rotating
    ? `<div class="banner warning acct-model-banner">当前是<b>账号池轮转</b>模式，请求会轮流分到 ${rows.filter(a=>a.enabled).length} 个账号，因此 <code>/v1/models</code> 返回它们的<b>并集</b>（共 ${rows.filter(a=>a.enabled).reduce((s,a)=>s+a.available.length,0)} 个）。<br>想让 <code>/v1/models</code> 只返回某一个账号的模型，请点该账号的「切换为此账号」，或在上方「请求分配」选「手动指定账号」。</div>`
    : `<div class="banner acct-model-banner">当前是<b>手动指定账号</b>模式，<code>/v1/models</code> 只返回 <b>${esc(activeRow?.name || "（未选择）")}</b> 的模型。</div>`;
  $("acct-models-body").innerHTML = banner + rows.map(a => `
    <div class="acct-model-row">
      <div class="acct-model-head">
        <strong>${esc(a.name)}</strong>
        <span class="pill ${a.region === "intl" ? "amber" : ""}">${a.region === "intl" ? "国际" : "国内"}</span>
        ${a.active ? '<span class="mini-active">当前账号</span>' : ""}
        ${a.enabled ? "" : '<span class="pill red">已暂停</span>'}
        <span class="muted mono acct-model-count">可用 ${a.available.length} 个${a.unknown_count ? " · 待探测 " + a.unknown_count : ""}</span>
        ${a.enabled && !a.active ? `<button class="switch acct-use" data-use="${a.id}">切换为此账号</button>` : ""}
        <button class="secondary acct-probe" data-probe="${a.id}">探测未确认模型</button>
      </div>
      <div class="acct-model-list">${a.available.map(m => `<code class="model-chip">${esc(m)}</code>`).join("") || '<span class="muted">尚未确认任何模型，点「探测」或先发一次请求。</span>'}</div>
      ${a.confirmed_no.length ? `<div class="muted acct-model-no">已确认不可用：${a.confirmed_no.map(esc).join("、")}</div>` : ""}
    </div>`).join("");
}
$("acct-models-body").addEventListener("click", async e => {
  const u = e.target.closest("[data-use]");
  if (u) {
    u.disabled = true;
    try {
      await api("pool/settings", {method:"PATCH", body:{routing:"manual"}});
      await api("accounts/" + u.dataset.use, {method:"PATCH", body:{active:true}});
      toast("已切换为手动模式并使用该账号");
      await refresh(); await loadModels();
    } catch (err) { toast(err.message); u.disabled = false; }
    return;
  }
  const b = e.target.closest("[data-probe]");
  if (!b) return;
  b.disabled = true; b.textContent = "探测中…";
  try {
    const r = await api("accounts/" + b.dataset.probe + "/probe", {method:"POST", body:{limit:8}});
    const n = Object.values(r.probed || {}).filter(v => v === true).length;
    const no = Object.values(r.probed || {}).filter(v => v === false).length;
    toast(r.message || `探测完成：${n} 个可用，${no} 个不可用`);
    await loadAccountModels();
  } catch (err) { toast(err.message); b.disabled = false; b.textContent = "探测未确认模型"; }
});
function renderModels(data) {
  const rows = data.models || [];
  const regions = data.regions || {};
  const fmtCtx = m => {
    const top = fmtNum(m.context_length);
    if (!m.context_length) return top;
    const opts = (m.context_lengths || []).filter(Boolean);
    const def = m.context_default_length;
    let note = "";
    if (opts.length > 1) {
      note = opts.map(o => Math.round(o / 1000) + "K").join(" / ");
    }
    if (def && def !== m.context_length) {
      note = note ? `${Math.round(def / 1000)}K 默认 · ${note}` : `${Math.round(def / 1000)}K 默认`;
    }
    return note ? `${top} <small class="cell-note mono">${note}</small>` : top;
  };
  // 默认档：上游 reasoning.effort；缺失时标自适应。
  const fmtEffort = m => {
    if (m.supports_reasoning === null || m.supports_reasoning === undefined) return "—";
    if (!m.supports_reasoning) return '<span class="muted">不支持</span>';
    const def = m.default_reasoning_effort || "自适应";
    return `<span class="pill green">${esc(def)}</span>`;
  };
  // 支持的档位：上游不给列表，用 models.dev 档位众数；可关思考时补一个 off。
  const fmtOptions = m => {
    if (!m.supports_reasoning) return "—";
    const opts = (m.effort_options || []).filter(Boolean);
    const chips = opts.map(v => `<span class="effort-chip">${esc(v)}</span>`);
    if (m.can_disable_thinking) chips.push('<span class="effort-chip off">off（可关）</span>');
    if (!chips.length) return '<span class="muted">常开</span>';
    return chips.join("");
  };
  $("models-regions").innerHTML = ["cn", "intl"].map(r => {
    const info = regions[r];
    const label = r === "cn" ? "国内版" : "国际版";
    if (!info || !info.count) {
      return `<span class="pill red">${label}：无数据${info && info.error ? "（" + esc(info.error) + "）" : ""}</span>`;
    }
    return `<span class="pill green">${label}：${info.count} 个模型${info.promotions ? " · " + info.promotions + " 项促销" : ""}</span>`;
  }).join("");
  if (!rows.length) {
    $("models-body").innerHTML = '<tr><td colspan="10" class="history-empty">没有取到模型目录。请确认账号可用后点「刷新上游目录」。</td></tr>';
    return;
  }
  $("models-body").innerHTML = rows.map(m => {
    const free = m.effective === 0;
    const eff = m.uncatalogued
      ? '<span class="pill amber" title="可调用，但不在上游目录中">目录外</span>'
      : free
        ? '<span class="pill green">免费</span>'
        : `<span class="mono">${fmtRate(m.effective)}</span>`;
    const promo = m.promo ? `<span class="pill green" title="${esc(m.promo_note || "")}">${esc(m.promo)}</span>` : "—";
    return `<tr>
      <td class="mono muted">${m.region === "intl" ? "国际" : "国内"}</td>
      <td><strong>${esc(m.name)}</strong><small class="cell-note mono">${esc(m.id)}</small></td>
      <td class="mono">${m.uncatalogued ? "—" : fmtRate(m.official)}</td>
      <td>${eff}</td>
      <td>${promo}</td>
      <td class="mono">${m.measured === null || m.measured === undefined ? "—" : fmtRate(m.measured)}</td>
      <td class="mono">${m.tokens ? fmtNum(m.tokens) : "—"}</td>
      <td class="mono">${fmtCtx(m)}</td>
      <td>${fmtEffort(m)}</td>
      <td>${fmtOptions(m)}</td>
      <td class="mono">${fmtNum(m.max_output_tokens)}</td>
    </tr>`;
  }).join("");
}
$("models-refresh").addEventListener("click", async () => {
  $("models-refresh").disabled = true;
  try { renderModels(await api("models/refresh", {method:"POST"})); toast("已刷新上游模型目录"); }
  catch (e) { toast(e.message); }
  finally { $("models-refresh").disabled = false; }
});
$("models-reset").addEventListener("click", async () => {
  try { await api("models/usage/reset", {method:"POST"}); await loadModels(); toast("实测统计已清零"); }
  catch (e) { toast(e.message); }
});
function render() {
  const {accounts, keys, models, uptime, events} = overview;
  const active = accounts.find(a => a.active && a.enabled);
  renderDashboard();
  $("account-count").textContent = accounts.length; $("account-badge").textContent = accounts.length;
  const rotating = overview.pool?.routing === "round_robin";
  $("active-name").textContent = rotating ? "账号池轮转" : active?.name || "未选择账号";
  $("active-state").textContent = rotating ? "新请求轮流分配，跳过暂停与冷却账号" : "新请求使用手动指定账号";
  $("test-account").textContent = active?.name || "尚未选择";
  $("uptime").textContent = uptime < 60 ? "已启动不到 1 分钟" : `持续运行 ${Math.floor(uptime / 3600)} 小时 ${Math.floor(uptime % 3600 / 60)} 分钟`;
  $("accounts-empty").hidden = accounts.length > 0;
  renderAccounts();
  const count = status => accounts.filter(a => a.pool_state === status).length;
  $("pool-counts").textContent = `全部 ${accounts.length}  ·  可用 ${count("available")}  ·  冷却 ${count("cooling")}  ·  暂停 ${count("paused")}  ·  耗尽 ${count("exhausted")}`;
  const known = accounts.filter(a => a.remaining !== null && a.remaining !== undefined);
  $("total-credits").textContent = known.length ? known.reduce((s,a) => s + a.remaining, 0).toLocaleString("zh-CN", {maximumFractionDigits:2}) + (known.length < accounts.length ? "（部分）" : "") : "待查询";
  if (!$("pool-settings").contains(document.activeElement)) {
    $("pool-routing").value = overview.pool?.routing || "manual"; $("auto-checkin").checked = overview.pool?.auto_checkin || false; $("checkin-time").value = overview.pool?.checkin_time || "09:00";
  }
  $("keys-body").innerHTML = keys.map(k => {
    const secret = k.recoverable
      ? `<div class="key-reveal"><code class="secret-value">${esc(k.key)}</code><button class="copy-button" data-copykey="${esc(k.key)}">复制</button></div>`
      : `<code>${esc(k.hint)}</code><small class="cell-note">完整密钥仅在创建时显示一次，无法再次查看</small>`;
    return `<tr><td><strong>${esc(k.name)}</strong></td><td>${secret}</td><td class="muted mono">${esc(stamp(k.created * 1000))}</td><td class="align-right"><div class="actions"><button class="danger" data-revoke="${k.id}">撤销</button></div></td></tr>`;
  }).join("");
  // 接入指南里的 API Key：只有 .env 初始 Key 可还原
  const guideKey = keys.find(k => k.recoverable);
  $("guide-key").textContent = guideKey ? guideKey.key : "（见「API 密钥」页新建）";
  $("guide-key-note").textContent = guideKey
    ? "此 Key 来自服务启动配置，可直接填入客户端。也可在「API 密钥」页新建独立 Key（新 Key 仅创建时显示一次）。"
    : "现有密钥均只保存摘要，无法再次显示。请在「API 密钥」页新建一个 Key 并立即保存。";
  const selected = $("model").value || "deepseek-v4-flash";
  $("model").replaceChildren(...models.map(model => { const o = document.createElement("option"); o.value = o.textContent = model; return o; }));
  if (models.includes(selected)) $("model").value = selected;
  $("test-submit").disabled = !active || busy;
  $("test-history").innerHTML = events.length ? events.map(e => `<div class="history-row"><span class="mono muted">${esc(stamp(e.time * 1000))}</span><strong>${esc(e.model)}</strong><span class="pill ${e.ok ? "green" : "red"}">${e.ok ? "成功" : "失败"}</span><span class="mono">${e.seconds}s</span></div>`).join("") : '<p class="history-empty">暂无测试记录，发送第一条测试消息。</p>';
}
function renderDashboard() {
  const {accounts, metrics:m = {}, pool} = overview;
  const fmt = value => Number(value).toLocaleString("zh-CN",{maximumFractionDigits:2});
  const rate = value => value === null || value === undefined ? "—" : value.toFixed(1) + "%";
  $("dash-available").textContent = accounts.filter(a=>a.pool_state === "available").length + " / " + accounts.length;
  const known = accounts.filter(a=>typeof a.remaining === "number");
  $("dash-credits").textContent = known.length ? fmt(known.reduce((s,a)=>s+a.remaining,0)) : "—";
  $("dash-credit-note").textContent = known.length < accounts.length ? `已查询 ${known.length}/${accounts.length} 个账号，余额可能不完整` : accounts.some(a=>a.credits_stale) ? "包含待刷新余额，以最近一次查询为准" : "以最近一次上游查询为准";
  $("dash-requests").textContent = fmt(m.completed || 0);
  $("dash-request-note").textContent = `API ${m.api_count || 0} · 后台测试 ${m.test_count || 0}`;
  $("dash-success").textContent = rate(m.success_rate); $("dash-http").textContent = rate(m.http_success_rate);
  $("dash-failed").textContent = m.failed || 0; $("dash-inflight").textContent = m.in_flight || 0;
  $("dash-latency").textContent = m.avg_duration_ms === null || m.avg_duration_ms === undefined ? "—" : fmt(m.avg_duration_ms) + " ms";
  $("dash-since").textContent = "统计开始于 " + stamp((m.started_at || 0)*1000);
  $("dash-routing").textContent = pool?.routing === "round_robin" ? "轮流分配请求，自动跳过不可用账号" : "手动指定账号模式";
  const states={available:"可用",paused:"已暂停",cooling:"冷却中",exhausted:"积分耗尽",invalid:"凭据异常"};
  $("dash-account-list").innerHTML = accounts.slice(0,8).map(a=>`<div class="dashboard-account"><span class="account-icon">${esc(a.name.slice(0,1))}</span><div><strong>${esc(a.name)}</strong><small class="cell-note">${esc(a.uid || a.nickname)}</small></div><div class="dashboard-account-credit"><strong>${a.remaining === null || a.remaining === undefined ? "待查询" : fmt(a.remaining)}</strong><small class="cell-note">积分</small></div><span class="pill ${a.pool_state === 'available' ? 'green' : 'amber'}">${states[a.pool_state] || '待查询'}</span></div>`).join("") || '<p class="history-empty">尚未添加账号，点击“添加账号”开始。</p>';
  if(accounts.length > 8) $("dash-account-list").insertAdjacentHTML("beforeend",'<p class="muted">更多账号请前往账号池查看。</p>');
  const outcomes={success:"完成",stream_error:"流式错误",interrupted:"未完整结束",http_error:"请求失败"};
  $("dash-recent-body").innerHTML=(m.recent || []).map(r=>`<tr><td class="mono muted">${esc(stamp(r.time*1000))}</td><td>${r.source === 'test' ? '后台测试' : 'API'}<small class="cell-note mono">${esc(r.path)}</small></td><td><span class="pill ${r.ok ? 'green' : 'red'}">${r.status ?? '—'}</span><small class="cell-note">${outcomes[r.outcome] || '请求失败'}</small></td><td class="align-right mono">${fmt(r.duration_ms)} ms</td></tr>`).join("") || '<tr><td colspan="4" class="history-empty">尚无请求记录。发起 API 调用或后台测试后，这里会自动更新。</td></tr>';
}
document.querySelectorAll("[data-dashboard-page]").forEach(b=>b.addEventListener("click",()=>goPage(b.dataset.dashboardPage)));
$("dash-add").addEventListener("click",()=>openAccount());
$("dash-base").textContent=location.origin+"/v1";
$("dash-copy").addEventListener("click",()=>copy(location.origin+"/v1"));
function renderAccounts() {
  const query = $("account-search").value.toLowerCase(), filter = $("account-filter").value;
  const rows = overview.accounts.filter(a => (!query || [a.name,a.nickname,a.uid].join(" ").toLowerCase().includes(query)) && (filter === "all" || a.pool_state === filter));
  const labels = {available:["可用","green"],cooling:["冷却中","amber"],paused:["已暂停",""],exhausted:["积分耗尽","amber"],invalid:["凭据异常","red"]};
  $("accounts-body").innerHTML = rows.map(a => {
    const status = labels[a.pool_state] || ["待查询", ""];
    const credits = a.remaining === null || a.remaining === undefined ? "—" : Number(a.remaining).toLocaleString("zh-CN",{maximumFractionDigits:2});
    return `<tr><td><div class="account-cell"><span class="account-icon">${esc(a.name.slice(0,1))}</span><div><strong>${esc(a.name)}${a.active ? '<span class="mini-active">手动 / 测试账号</span>' : ""}</strong><small>${esc(a.uid || a.nickname)}</small></div></div></td><td><span class="pill ${status[1]}">${status[0]}</span><small class="cell-note">${a.today_checked_in ? "今日已签到" : "今日未确认签到"}</small>${a.cooldown_until > Date.now()/1000 ? `<small class="cell-note">至 ${esc(stamp(a.cooldown_until*1000))}</small>` : ""}</td><td><strong class="credit-number">${credits}</strong><small class="cell-note">${a.credits_updated ? esc(stamp(a.credits_updated*1000)) : "点击查询积分"}${a.credits_stale && a.credits_updated ? " · 待刷新" : ""}</small>${a.last_error ? `<small class="cell-note field-error">${esc(a.last_error)}</small>` : ""}</td><td class="mono">${esc(stamp(a.expires_at))}<small class="cell-note">${a.expired ? "已到期 · 调用时尝试刷新" : "支持自动刷新"}</small></td><td><div class="actions pool-actions">${a.enabled ? `<button data-action="status" data-id="${a.id}" title="查询积分与签到状态">查询积分</button><button data-action="checkin" data-id="${a.id}">签到</button><button data-action="refresh" data-id="${a.id}">刷新凭据</button>` : ""}${a.enabled && !a.active ? `<button class="switch" data-action="activate" data-id="${a.id}">设为手动 / 测试</button>` : ""}<button data-action="tasks" data-id="${a.id}">任务</button><button data-action="rename" data-id="${a.id}">备注</button><button data-action="toggle" data-id="${a.id}">${a.enabled ? "暂停" : "恢复"}</button><button class="danger" data-action="delete" data-id="${a.id}">删除</button></div></td></tr>`;
  }).join("") || (overview.accounts.length ? '<tr><td colspan="5" class="muted">没有符合筛选条件的账号。</td></tr>' : "");
}
$("account-search").addEventListener("input", () => { if(overview) renderAccounts(); });
$("account-filter").addEventListener("change", () => { if(overview) renderAccounts(); });
async function refresh() {
  $("refresh").disabled = true;
  try { overview = await api("overview"); render(); $("load-error").hidden = true; }
  catch (e) { $("load-error").textContent = e.message; $("load-error").hidden = false; throw e; }
  finally { $("refresh").disabled = false; }
}
async function enter() { $("boot").hidden = true; $("login").hidden = true; $("workspace").hidden = false; goPage(location.hash.slice(1)); await refresh(); }
$("login-form").addEventListener("submit", async e => {
  e.preventDefault(); const button = e.submitter; button.disabled = true; $("login-error").textContent = "";
  try { const data = await api("login", {method:"POST",body:{key:$("admin-key").value.trim()}}); csrf = data.csrf; $("admin-key").value = ""; await enter(); }
  catch (error) { $("login-error").textContent = error.message; }
  finally { button.disabled = false; }
});
$("logout").addEventListener("click", async () => { try { await api("logout", {method:"POST"}); showLogin(); } catch (e) { toast(e.message); } });
async function batchAction(action) {
  $("refresh").disabled = $("batch-checkin").disabled = true;
  try {
    const result = await api("pool/actions/"+action,{method:"POST"});
    $("pool-result").hidden = false;
    $("pool-result").textContent = result.results.map(r => `${overview.accounts.find(a=>a.id===r.id)?.name || "账号"}：${r.message}`).join("；") || "没有启用的账号";
    await refresh();
  } catch(e) { toast(e.message); } finally { $("refresh").disabled = $("batch-checkin").disabled = false; }
}
$("refresh").addEventListener("click", () => page === "accounts" ? batchAction("status") : refresh().catch(e=>toast(e.message)));
$("batch-checkin").addEventListener("click", () => batchAction("checkin"));
$("pool-settings").addEventListener("submit", async e => { e.preventDefault();e.submitter.disabled=true;try {await api("pool/settings",{method:"PATCH",body:{routing:$("pool-routing").value,auto_checkin:$("auto-checkin").checked,checkin_time:$("checkin-time").value}});await refresh();toast("账号池设置已保存");}catch(err){toast(err.message);}finally{e.submitter.disabled=false;} });
setInterval(() => { if(csrf && overview && !document.hidden && !document.querySelector("dialog[open]") && !$("refresh").disabled) refresh().catch(()=>{}); },30000);
document.querySelectorAll("[data-page]").forEach(b => b.addEventListener("click", () => goPage(b.dataset.page)));
$("go-guide").addEventListener("click", () => goPage("guide"));
document.querySelectorAll(".close-dialog").forEach(b => b.addEventListener("click", () => b.closest("dialog").close()));
function clearOAuth() {
  oauthGeneration++; clearTimeout(oauthTimer);
  const previous = oauthFlow; oauthFlow = null;
  if (previous && csrf) api("oauth/" + previous.id, {method:"DELETE"}).catch(() => {});
  $("oauth-link-box").hidden = true; $("oauth-open").removeAttribute("href");
  $("oauth-start").disabled = false; $("oauth-start").textContent = "生成登录链接 ↗";
  $("oauth-retry").hidden = true; $("account-name").disabled = false;
  $("oauth-status").textContent = "登录链接 5 分钟内有效。同一账号重新登录会更新凭据。";
  $("oauth-status").className = "banner oauth-status";
}
function setAccountMode(mode) {
  clearOAuth(); accountMode = mode;
  $("browser-login-panel").hidden = mode !== "browser";
  $("file-import-panel").hidden = $("account-import").hidden = mode !== "file";
  $("mode-browser").setAttribute("aria-pressed", String(mode === "browser"));
  $("mode-file").setAttribute("aria-pressed", String(mode === "file"));
  $("account-error").textContent = "";
}
$("mode-browser").addEventListener("click", () => setAccountMode("browser"));
$("mode-file").addEventListener("click", () => setAccountMode("file"));
$("account-dialog").addEventListener("close", () => { clearOAuth(); $("account-form").reset(); $("file-label").textContent = "选择或拖入 .info / .json 文件"; $("account-error").textContent = ""; });
function openAccount() { setAccountMode("browser"); $("account-dialog").showModal(); }
async function pollOAuth(generation) {
  if (generation !== oauthGeneration || !oauthFlow) return;
  const flow = oauthFlow;
  if (Date.now() >= flow.expires_at) { clearOAuth(); $("oauth-status").textContent = "登录链接已过期，请重新生成。"; return; }
  try {
    const result = await api("oauth/" + flow.id + "/poll", {method:"POST"});
    if (generation !== oauthGeneration) return;
    if (result.status === "success") {
      $("account-dialog").close(); await refresh();
      toast(result.updated ? "授权成功，账号凭据已更新" : "授权成功，账号已添加；可在列表中切换使用"); return;
    }
    if (result.status === "expired") { clearOAuth(); $("oauth-status").textContent = "登录链接已过期，请重新生成。"; return; }
    $("oauth-status").className = "banner oauth-status";
    $("oauth-status").textContent = `等待你在官方页面完成登录… 链接剩余 ${Math.max(1, Math.ceil((flow.expires_at - Date.now()) / 1000))} 秒。`;
    oauthTimer = setTimeout(() => pollOAuth(generation), 3000);
  } catch (e) {
    if (generation !== oauthGeneration) return;
    $("oauth-status").className = "banner warning oauth-status"; $("oauth-status").textContent = e.message;
    $("oauth-retry").hidden = false;
  }
}
$("oauth-start").addEventListener("click", async () => {
  clearOAuth(); const generation = oauthGeneration;
  $("oauth-start").disabled = true; $("oauth-start").textContent = "正在生成…";
  $("account-error").textContent = "";
  try {
    const flow = await api("oauth/start", {method:"POST",body:{name:$("account-name").value.trim() || undefined, region:$("oauth-region").value}});
    if (generation !== oauthGeneration) { api("oauth/" + flow.id, {method:"DELETE"}).catch(() => {}); return; }
    oauthFlow = flow; $("oauth-open").href = flow.url; $("oauth-link-box").hidden = false;
    $("oauth-start").textContent = "重新生成链接"; $("account-name").disabled = true;
    $("oauth-status").textContent = "登录链接已就绪，请打开官方页面完成登录。";
    oauthTimer = setTimeout(() => pollOAuth(generation), 3000);
  } catch (e) { if (generation === oauthGeneration) $("account-error").textContent = e.message; }
  finally { if (generation === oauthGeneration) $("oauth-start").disabled = false; }
});
$("oauth-copy").addEventListener("click", () => { if (oauthFlow) copy(oauthFlow.url); });
$("oauth-retry").addEventListener("click", () => { $("oauth-retry").hidden = true; pollOAuth(oauthGeneration); });
$("add-account").addEventListener("click", openAccount); $("empty-add").addEventListener("click", openAccount);
async function readFile(file) {
  if (!file) return;
  if (file.size > 1024 * 1024) throw new Error("文件超过 1 MB，请选择登录凭据文件");
  $("credential-json").value = (await file.text()).replace(/^\uFEFF/, "");
  $("file-label").textContent = file.name;
}
$("credential-file").addEventListener("change", e => { readFile(e.target.files[0]).catch(err => $("account-error").textContent = err.message); });
$("file-drop").addEventListener("dragover", e => { e.preventDefault(); $("file-drop").classList.add("drag-over"); });
$("file-drop").addEventListener("dragleave", () => $("file-drop").classList.remove("drag-over"));
$("file-drop").addEventListener("drop", e => { e.preventDefault(); $("file-drop").classList.remove("drag-over"); readFile(e.dataTransfer.files[0]).catch(err => $("account-error").textContent = err.message); });
$("account-form").addEventListener("submit", async e => {
  if (accountMode !== "file") { e.preventDefault(); return; }
  e.preventDefault(); e.submitter.disabled = true; $("account-error").textContent = "";
  try {
    let credential; try { credential = JSON.parse($("credential-json").value); } catch { throw new Error("请选择文件或粘贴有效的 JSON 内容"); }
    await api("accounts", {method:"POST", body:{name:$("account-name").value.trim() || undefined, credential}});
    $("account-dialog").close(); await refresh(); toast("账号已导入");
  } catch (err) { $("account-error").textContent = err.message; } finally { e.submitter.disabled = false; }
});
function confirmAction(title, desc, callback, rename = null) {
  $("confirm-title").textContent = title; $("confirm-desc").textContent = desc; $("confirm-error").textContent = "";
  $("rename-label").hidden = $("rename-value").hidden = rename === null; $("rename-value").value = rename || "";
  pendingConfirm = callback; $("confirm-dialog").showModal();
}
$("confirm-form").addEventListener("submit", async e => { e.preventDefault(); e.submitter.disabled = true; try { await pendingConfirm(); $("confirm-dialog").close(); await refresh(); toast("操作已保存"); } catch (err) { $("confirm-error").textContent = err.message; } finally { e.submitter.disabled = false; } });
$("accounts-body").addEventListener("click", async e => {
  const b = e.target.closest("[data-action]"); if (!b) return;
  const a = overview.accounts.find(x => x.id === b.dataset.id); if (!a) return;
  if (b.dataset.action === "tasks") {
    $("task-dialog").showModal();
    $("task-title").textContent = "积分任务";
    loadTasks(a.id);
    return;
  }
  if (["status","checkin","refresh"].includes(b.dataset.action)) {
    b.disabled = true;
    try { const result=await api(`accounts/${a.id}/actions/${b.dataset.action}`,{method:"POST"});toast(result.message);await refresh(); }
    catch(err){toast(err.message);} finally {b.disabled=false;} return;
  }
  const update = body => api("accounts/" + a.id, {method:"PATCH", body});
  if (b.dataset.action === "activate") confirmAction("切换为「手动指定账号」模式？", `将请求分配切到手动模式并设为「${a.name}」。此后 /v1/models 只返回该账号的模型。`, async () => {
    await api("pool/settings", {method:"PATCH", body:{routing:"manual"}});
    await update({active:true});
  });
  if (b.dataset.action === "rename") confirmAction("编辑账号备注", "备注仅用于在管理后台识别账号。", () => update({name:$("rename-value").value}), a.name);
  if (b.dataset.action === "toggle") confirmAction(a.enabled ? "暂停这个账号？" : "恢复这个账号？", "暂停后不参与新请求分配和自动签到，已开始的请求继续完成。恢复后重新参与轮转。", () => update({enabled:!a.enabled}));
  if (b.dataset.action === "delete") confirmAction("删除这个账号？", `「${a.name}」将从账号列表移除。服务器保留恢复副本。${a.active ? "当前调用账号将被清空。" : ""}`, () => api("accounts/" + a.id, {method:"DELETE"}));
});
$("keys-body").addEventListener("click", e => {
  const c = e.target.closest("[data-copykey]");
  if (c) { copy(c.dataset.copykey); return; }
  const b = e.target.closest("[data-revoke]"); if (!b) return; const k = overview.keys.find(x => x.id === b.dataset.revoke); confirmAction("撤销客户端密钥？", `使用「${k.name}」的客户端将无法发起新请求。此操作不可撤销。`, () => api("keys/" + k.id, {method:"DELETE"})); });
$("add-key").addEventListener("click", () => { $("key-form").hidden = false; $("created-key").hidden = true; $("key-dialog").showModal(); });
$("key-dialog").addEventListener("close", () => { $("key-form").reset(); $("new-key-value").textContent = ""; $("key-error").textContent = ""; });
$("key-form").addEventListener("submit", async e => { e.preventDefault(); e.submitter.disabled = true; try { const d = await api("keys", {method:"POST",body:{name:$("key-name").value}}); $("key-form").hidden = true; $("created-key").hidden = false; $("new-key-value").textContent = d.key; await refresh(); } catch (err) { $("key-error").textContent = err.message; } finally { e.submitter.disabled = false; } });
async function copy(value) { try { await navigator.clipboard.writeText(value); toast("已复制"); } catch { toast("无法自动复制，请手动选择文本复制"); } }
$("copy-key").addEventListener("click", () => copy($("new-key-value").textContent));
document.querySelectorAll("[data-copy]").forEach(b => b.addEventListener("click", () => copy(b.dataset.copy)));
$("base-url").textContent = location.origin + "/v1"; $("anthropic-url").textContent = location.origin;
$("copy-base").addEventListener("click", () => copy(location.origin + "/v1")); $("copy-anthropic").addEventListener("click", () => copy(location.origin));
$("copy-guide-key").addEventListener("click", () => {
  const k = (overview?.keys || []).find(x => x.recoverable);
  if (k) copy(k.key); else toast("没有可显示的完整密钥，请在「API 密钥」页新建");
});

$("usage-refresh").addEventListener("click", () => loadUsage().then(() => toast("\u7528\u91cf\u5df2\u5237\u65b0")).catch(e => toast(e.message)));
$("usage-reset").addEventListener("click", () => confirmAction("\u6e05\u96f6\u7528\u91cf\u7edf\u8ba1\uff1f", "\u5c06\u6e05\u7a7a\u6309\u8d26\u53f7\u3001\u6a21\u578b\u3001\u57df\u7684\u7d2f\u8ba1\u8ba1\u6570\u4e0e\u65f6\u5e8f\u6570\u636e\uff0c\u6b64\u64cd\u4f5c\u4e0d\u53ef\u64a4\u9500\u3002", async () => { await api("usage/reset", {method:"POST"}); await loadUsage(); }));

// ---------------- 积分任务 ----------------
let taskAccount = null;
// 支持一键完成的任务（与后端 autotask.ACTIONS 对应）
const AUTO_TASKS = new Set([
  "chat_5", "first_buddy", "Model_chat_GLM5.2", "RichMeow_Chat",
  "Buddy_App", "Buddy_App_QQ", "automation_1", "Library_read",
  "template_5", "playbook_prompt", "create_canvas", "Hp_Appearance", "black_cat",
]);

function renderTasks(data) {
  const rows = (data && data.tasks) || [];
  if (!data || !data.ok) {
    $("task-body").innerHTML = `<p class="history-empty">${esc((data && data.message) || "查询失败")}</p>`;
    return;
  }
  if (!rows.length) {
    $("task-body").innerHTML = '<p class="history-empty">该账号没有可用的任务</p>';
    return;
  }
  const claimable = rows.filter(t => t.claimable).length;
  const claimed = rows.filter(t => t.claimed).length;
  $("task-title").textContent = `积分任务 · ${claimable ? claimable + " 项可领取" : "共 " + rows.length + " 项"}`;
  $("task-body").innerHTML = rows.map(t => {
    const prog = t.target ? `${t.current} / ${t.target}` : "—";
    const reward = [
      t.credit ? `+${t.credit} 分` : "",
      t.energy ? `+${t.energy} 能` : "",
      t.reward_buddy ? "Buddy" : "",
    ].filter(Boolean).join(" ") || "—";
    let status, cls;
    if (t.claimed) { status = "已领取"; cls = "pill"; }
    else if (t.claimable) { status = "可领取"; cls = "pill green"; }
    else if (t.accept_status === "accepted") { status = "进行中"; cls = "pill amber"; }
    else { status = "未接受"; cls = "pill"; }
    const auto = AUTO_TASKS.has(t.task_code);
    const buttons = [];
    if (auto && !t.claimed) {
      buttons.push(`<button class="secondary" data-task-auto="${esc(t.task_code)}">一键完成</button>`);
    }
    if (t.claimable) {
      buttons.push(`<button class="primary" data-task-claim="${esc(t.task_code)}">领取</button>`);
    }
    const action = buttons.join("");
    return `<div class="task-row">
      <div class="task-main">
        <strong>${esc(t.title || t.task_code)}</strong>
        <small class="cell-note mono">${esc(t.task_code)}${t.tag ? " · " + esc(t.tag) : ""}${t.level_name ? " · " + esc(t.level_name) : ""}</small>
        ${t.task_desc ? `<small class="cell-note">${esc(t.task_desc)}</small>` : ""}
      </div>
      <div class="task-progress mono">${prog}</div>
      <div class="task-reward mono">${reward}</div>
      <div class="task-status"><span class="${cls}">${status}</span></div>
      <div class="task-action">${action}</div>
    </div>`;
  }).join("");
}

async function loadTasks(aid) {
  taskAccount = aid;
  $("task-body").innerHTML = "正在加载任务列表…";
  try {
    renderTasks(await api(`accounts/${aid}/tasks`));
  } catch (e) {
    $("task-body").innerHTML = `<p class="history-empty">${esc(e.message)}</p>`;
  }
}

$("task-refresh").addEventListener("click", () => taskAccount && loadTasks(taskAccount));
$("task-accept-all").addEventListener("click", async () => {
  if (!taskAccount) return;
  $("task-accept-all").disabled = true;
  try {
    const r = await api(`accounts/${taskAccount}/tasks/accept`, {method:"POST", body:{}});
    toast(r.message || "已接受任务");
    await loadTasks(taskAccount);
  } catch (e) { toast(e.message); }
  finally { $("task-accept-all").disabled = false; }
});
$("task-auto-all").addEventListener("click", async () => {
  if (!taskAccount) return;
  const btn = $("task-auto-all");
  btn.disabled = true;
  btn.textContent = "执行中，请稍候…";
  try {
    const r = await api(`accounts/${taskAccount}/tasks/auto_all`, {method:"POST", body:{}});
    toast(r.message || "执行完成");
    await loadTasks(taskAccount);
  } catch (e) { toast(e.message); }
  finally { btn.disabled = false; btn.textContent = "一键完成可自动任务"; }
});
$("task-claim-all").addEventListener("click", async () => {
  if (!taskAccount) return;
  $("task-claim-all").disabled = true;
  $("task-claim-all").textContent = "领取中…";
  try {
    const r = await api(`accounts/${taskAccount}/tasks/claim_all`, {method:"POST", body:{}});
    toast(r.message || "领取完成");
    await loadTasks(taskAccount);
  } catch (e) { toast(e.message); }
  finally { $("task-claim-all").disabled = false; $("task-claim-all").textContent = "领取可领奖励"; }
});
$("task-body").addEventListener("click", async e => {
  const autoBtn = e.target.closest("[data-task-auto]");
  if (autoBtn && taskAccount) {
    autoBtn.disabled = true;
    autoBtn.textContent = "执行中…";
    try {
      const r = await api(`accounts/${taskAccount}/tasks/auto`, {method:"POST", body:{code:autoBtn.dataset.taskAuto}});
      toast(r.message || "已执行");
      await loadTasks(taskAccount);
    } catch (err) { toast(err.message); autoBtn.disabled = false; autoBtn.textContent = "一键完成"; }
    return;
  }
  const b = e.target.closest("[data-task-claim]");
  if (!b || !taskAccount) return;
  b.disabled = true;
  try {
    const r = await api(`accounts/${taskAccount}/tasks/claim`, {method:"POST", body:{code:b.dataset.taskClaim}});
    toast(r.message || "已领取");
    await loadTasks(taskAccount);
  } catch (err) { toast(err.message); b.disabled = false; }
});

// ---------------- 任务中心 ----------------
let tcTimer = null;

async function loadTaskCenter() {
  try { renderTaskQueue(await api("tasks/queue")); } catch (e) { /* 未启动 */ }
}

async function scanTasks() {
  const btn = $("tc-scan");
  btn.disabled = true;
  btn.textContent = "扫描中…";
  $("tc-accounts").innerHTML = "正在并发扫描各账号，请稍候…";
  try {
    const d = await api("tasks/scan");
    renderTaskScan(d);
  } catch (e) { toast(e.message); $("tc-accounts").textContent = e.message; }
  finally { btn.disabled = false; btn.textContent = "扫描待办"; }
}

function renderTaskScan(d) {
  const accounts = d.accounts || [];
  $("tc-summary").innerHTML = [
    ["账号", accounts.length],
    ["待办项", d.pending_count || 0],
    ["可领奖", accounts.reduce((s, a) => s + (a.claimable || 0), 0)],
  ].map(([k, v]) => `<div class="tc-card"><strong>${esc(v)}</strong><span>${esc(k)}</span></div>`).join("");
  if (!accounts.length) { $("tc-accounts").innerHTML = '<p class="history-empty">没有启用中的账号</p>'; return; }
  $("tc-accounts").innerHTML = accounts.map(a => {
    const pending = (a.pending || []).map(p => {
      const prog = p.target ? `${p.current}/${p.target}` : "—";
      return `<span class="model-chip" title="${esc(p.title)}">${esc(p.code)} ${esc(prog)}${p.credit ? " +" + p.credit : ""}</span>`;
    }).join("") || '<span class="muted">无待办</span>';
    const err = a.error ? `<small class="cell-note field-error">${esc(a.error)}</small>` : "";
    return `<div class="tc-row">
      <div class="tc-row-head"><strong>${esc(a.name)}</strong>
        <small class="cell-note">已领 ${a.claimed}/${a.total}${a.claimable ? " · 可领 " + a.claimable : ""}</small></div>
      <div class="tc-chips">${pending}</div>${err}</div>`;
  }).join("");
}

async function runTaskQueue() {
  const btn = $("tc-run");
  const conc = parseInt($("tc-concurrency").value, 10) || 1;
  btn.disabled = true;
  btn.textContent = "启动中…";
  try {
    const r = await api("tasks/queue", {method:"POST", body:{concurrency:conc}});
    toast(r.message || "队列已启动");
    if (r.total) startQueuePolling();
  } catch (e) { toast(e.message); }
  finally { btn.disabled = false; btn.textContent = "执行队列"; }
}

function renderTaskQueue(q) {
  if (!q || !q.total) {
    $("tc-queue").innerHTML = '<p class="history-empty">尚未启动队列。</p>';
    $("tc-progress").textContent = "";
    return;
  }
  $("tc-progress").textContent = `${q.done + q.failed} / ${q.total}${q.running ? " · 执行中" : " · 已结束"}`;
  const label = {pending:"等待", running:"执行中", done:"完成", error:"失败", skipped:"跳过"};
  const cls = {pending:"", running:"amber", done:"green", error:"red", skipped:""};
  $("tc-queue").innerHTML = (q.items || []).map(it => `<div class="tc-item">
    <div class="tc-item-main"><strong>${esc(it.code)}</strong>
      <small class="cell-note">${esc(it.nickname)}</small></div>
    <span class="pill ${cls[it.status] || ""}">${label[it.status] || it.status}</span>
    <small class="tc-item-msg">${esc(it.message || "")}</small>
  </div>`).join("");
}

function startQueuePolling() {
  clearInterval(tcTimer);
  tcTimer = setInterval(async () => {
    try {
      const q = await api("tasks/queue");
      renderTaskQueue(q);
      if (!q.running) { clearInterval(tcTimer); tcTimer = null; await scanTasks(); }
    } catch (e) { clearInterval(tcTimer); tcTimer = null; }
  }, 2000);
}

$("tc-scan").addEventListener("click", () => scanTasks());
$("tc-run").addEventListener("click", () => runTaskQueue());
$("host-label").textContent = location.host;
$("test-form").addEventListener("submit", async e => {
  e.preventDefault(); busy = true; $("test-submit").disabled = true; $("test-submit").textContent = "正在调用…"; $("test-status").className = "pill amber"; $("test-status").textContent = "请求中"; $("test-output").textContent = "正在等待上游响应，最长约 90 秒…"; $("test-meta").textContent = "";
  try { const r = await api("test", {method:"POST",body:{model:$("model").value,prompt:$("test-prompt").value}}); $("test-status").className = "pill " + (r.ok ? "green" : "red"); $("test-status").textContent = r.ok ? "连接成功" : "调用失败"; $("test-output").textContent = r.ok ? r.answer : r.error; $("test-meta").textContent = `${r.seconds}s${r.status ? " · HTTP " + r.status : ""}${r.usage?.total_tokens !== undefined ? " · " + r.usage.total_tokens + " tokens" : ""}`; }
  catch (err) { $("test-status").className = "pill red"; $("test-status").textContent = "请求失败"; $("test-output").textContent = err.message; }
  finally { busy = false; $("test-submit").textContent = "发送测试 ↗"; await refresh().catch(() => {}); }
});

const fmtTokens = v => {
  const n = Number(v) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
};
const fmtDuration = ms => ms === null || ms === undefined ? "\u2014" : (ms / 1000).toFixed(2) + "s";
let usageData = null;

async function loadUsage() {
  $("usage-accounts-body").innerHTML = '<tr><td colspan="9" class="history-empty">\u6b63\u5728\u52a0\u8f7d\u2026</td></tr>';
  usageData = await api("usage");
  renderUsage();
}

function usageRow(row, cells) {
  return "<tr>" + cells.map(c => "<td>" + c + "</td>").join("") + "</tr>";
}

function renderUsage() {
  const d = usageData || {};
  const t = d.totals || {};
  const legacy = d.legacy || {};
  const accounts = d.accounts || [], models = d.models || [], realms = d.realms || [];
  const cards = [
    ["\u8bf7\u6c42\u6570", fmtNum(t.requests || 0)],
    ["\u603b token", fmtTokens(t.tokens)],
    ["prompt", fmtTokens(t.prompt)],
    ["completion", fmtTokens(t.completion)],
    ["\u5931\u8d25\u5c1d\u8bd5", fmtNum(t.failed || 0)],
    ["\u5e73\u5747\u5ef6\u8fdf", fmtDuration(t.avg_duration_ms)],
  ];
  $("usage-cards").innerHTML = cards.map(([label, value]) =>
    '<div class="usage-card"><strong>' + esc(value) + '</strong><span>' + esc(label) + "</span></div>").join("");

  // \u5408\u8ba1\u5217\uff1a\u672c\u6a21\u5757\u5b9e\u65f6\u7edf\u8ba1\u5df2\u542b\u5386\u53f2\uff0c\u5386\u53f2\u65e0 prompt \u62c6\u5206\u65f6\u6807\u6ce8\u3002
  const legacyCell = row => {
    const n = row.legacy_tokens || 0;
    if (!n) return "";
    return ` <small class="cell-note mono">\u542b\u5386\u53f2 ${fmtTokens(n)}</small>`;
  };
  const promptCell = row => row.prompt ? fmtTokens(row.prompt) : (row.legacy_tokens ? "\u2014" : "\u2014");
  const totalCell = row => `<strong>${fmtTokens(row.tokens)}</strong>` + legacyCell(row);

  $("usage-accounts-body").innerHTML = accounts.length ? accounts.map(a => {
    const avg = a.timed ? (a.tokens / a.timed) / ((a.duration_ms / a.timed) / 1000) : null;
    const initial = (a.name || "?").slice(0, 1);
    return usageRow(a, [
      '<div class="account-cell"><span class="account-icon">' + esc(initial) + "</span>"
        + '<div><strong class="usage-name">' + esc(a.name) + "</strong>"
        + (a.detail ? '<small class="cell-note mono">' + esc(a.detail) + "</small>" : "")
        + "</div></div>",
      esc(a.region || "\u2014"),
      fmtNum(a.requests), a.failed ? '<span class="warn-text">' + fmtNum(a.failed) + "</span>" : "\u2014",
      promptCell(a), a.completion ? fmtTokens(a.completion) : "\u2014",
      totalCell(a),
      fmtDuration(a.timed ? a.duration_ms / a.timed : null),
      avg ? avg.toFixed(1) + " tok/s" : "\u2014",
    ]);
  }).join("") : '<tr><td colspan="9" class="history-empty">\u6682\u65e0\u7528\u91cf\u6570\u636e</td></tr>';

  $("usage-models-body").innerHTML = models.length ? models.map(m => usageRow(m, [
    '<div class="usage-name">' + esc(m.name) + "</div>",
    fmtNum(m.requests), m.failed ? '<span class="warn-text">' + fmtNum(m.failed) + "</span>" : "\u2014",
    promptCell(m), m.completion ? fmtTokens(m.completion) : "\u2014",
    totalCell(m),
    m.credit ? m.credit.toFixed(2) : "\u2014",
  ])).join("") : '<tr><td colspan="7" class="history-empty">\u6682\u65e0\u7528\u91cf\u6570\u636e</td></tr>';

  $("usage-realms-body").innerHTML = realms.length ? realms.map(r => usageRow(r, [
    esc(r.realm || r.name), fmtNum(r.requests),
    r.failed ? '<span class="warn-text">' + fmtNum(r.failed) + "</span>" : "\u2014",
    promptCell(r), r.completion ? fmtTokens(r.completion) : "\u2014",
    totalCell(r),
  ])).join("") : '<tr><td colspan="6" class="history-empty">\u6682\u65e0\u7528\u91cf\u6570\u636e</td></tr>';

  renderUsageSeries(d.series || []);
}

function renderUsageSeries(series) {
  const body = $("usage-chart-body");
  if (!series.length) { body.innerHTML = '<p class="history-empty">\u6682\u65e0\u65f6\u5e8f\u6570\u636e</p>'; $("usage-window").textContent = ""; return; }
  const W = 900, H = 160, PAD = 4;
  const peak = Math.max(1, ...series.map(p => (p.prompt || 0) + (p.completion || 0)));
  const step = series.length > 1 ? (W - PAD * 2) / (series.length - 1) : 0;
  const y = v => H - PAD - (v / peak) * (H - PAD * 2);
  let promptPts = [], compPts = [], areaPts = [];
  series.forEach((p, i) => {
    const x = PAD + step * i;
    promptPts.push(x + "," + y(p.prompt || 0));
    const total = (p.prompt || 0) + (p.completion || 0);
    compPts.push(x + "," + y(total));
    areaPts.push(x + "," + y(total));
  });
  const base = H - PAD;
  const area = "M" + PAD + "," + base + " L" + areaPts.join(" L") + " L" + (PAD + step * (series.length - 1)) + "," + base + " Z";
  body.innerHTML =
    '<svg class="usage-svg" viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" aria-hidden="true">' +
    '<path d="' + area + '" class="usage-area"></path>' +
    '<polyline points="' + compPts.join(" ") + '" class="usage-line completion"></polyline>' +
    '<polyline points="' + promptPts.join(" ") + '" class="usage-line prompt"></polyline>' +
    "</svg>";
  const first = new Date(series[0].t * 1000), last = new Date(series[series.length - 1].t * 1000);
  const hm = d => d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  $("usage-window").textContent = hm(first) + " \u2013 " + hm(last) + " \u00b7 \u5cf0\u503c " + fmtTokens(peak) + "/min";
}

(async () => { try { const s = await api("session"); csrf = s.csrf; await enter(); } catch (e) { if (!csrf) showLogin(); } })();
