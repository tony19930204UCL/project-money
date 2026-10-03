/* CIO Market Lab V2 — local-first workstation UI. No broker path. */
(() => {
  "use strict";
  const API_BASE = "";
  const state = {
    view: "command", watchMarket: "TW", selectedSymbol: "2330.TW", selectedQuote: null, timeframe: "1D", layout: "dense",
    watchlists: {TW: [], US: []}, overview: null, portfolios: null, strategies: [], research: [], diagnostics: null, markets: null, experiments: [],
    fills: [], orders: [], positions: [], bars: [], apiErrors: [], killSwitch: false, competition: null, chartSource: "DELAYED DATA", orderPreview: null, replaceOrderId: null
  };
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch]));
  const num = (value, digits = 2) => value != null && value !== "" && Number.isFinite(Number(value)) ? Number(value).toLocaleString("en-US", {minimumFractionDigits: digits, maximumFractionDigits: digits}) : "—";
  const pct = value => value != null && value !== "" && Number.isFinite(Number(value)) ? `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)}%` : "—";
  const money = value => value != null && value !== "" && Number.isFinite(Number(value)) ? `$${num(Number(value), 2)}` : "—";
  const stamp = iso => iso ? new Date(iso).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"}) : "—";

  async function api(path, options = {}) {
    try {
      const response = await fetch(`${API_BASE}${path}`, {headers: {"Content-Type": "application/json", ...(options.headers || {})}, ...options});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } catch (error) {
      state.apiErrors.push({path, error: error.message});
      return null;
    }
  }

  function showToast(message) { const el = $("#toast"); el.textContent = message; el.classList.add("show"); setTimeout(() => el.classList.remove("show"), 3200); }
  function setNotice(message, type = "info") { const el = $("#global-notice"); if (!message) { el.classList.add("hidden"); return; } el.className = `notice notice-${type}`; el.textContent = message; }
  function stateHtml(message, kind = "empty") { return `<div class="state ${kind}-state">${kind === "loading" ? '<span class="spinner"></span>' : ""}${esc(message)}</div>`; }

  function navigate(view) {
    state.view = view;
    $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.view === view));
    $$(".view").forEach(item => item.classList.toggle("active-view", item.id === `view-${view}`));
    const titles = {command:"Market pulse", markets:"Markets", chart:"Chart desk", paper:"Paper trade", portfolio:"Portfolio", replay:"Historical replay", risk:"Paper risk / observer", strategy:"Strategy lab", research:"Research quarantine", diagnostics:"Diagnostics"};
    $("#view-title").textContent = titles[view] || "Market pulse";
    // Markets must refresh when opened rather than waiting for the larger
    // workstation bootstrap Promise to finish every optional endpoint.
    if (view === "markets") loadMarkets();
    if (view === "chart") drawChart($("#desk-chart"), state.bars, true);
    if (view === "paper") renderPaperLedger();
    if (view === "risk") loadRisk();
    if (view === "replay") { const frame = $("#replay-frame"); if (!frame.getAttribute("src")) frame.setAttribute("src", frame.dataset.src); }
  }

  async function loadRisk() {
    const summary = $("#risk-live-summary"), detail = $("#risk-live-json");
    summary.innerHTML = stateHtml("Loading authoritative local PAPER risk state…", "loading");
    detail.textContent = "";
    const [limits, overview, capabilities] = await Promise.all([
      api("/api/paper/risk-limits"), api("/api/overview"), api("/api/paper/capabilities")
    ]);
    if (!limits || !overview || !capabilities) {
      summary.innerHTML = stateHtml("Risk source unavailable; no healthy state or zero exposure inferred.", "error");
      return;
    }
    const rows = [
      ["Max order notional", money(limits.max_order_notional)],
      ["Max position notional", money(limits.max_position_notional)],
      ["Max daily loss", money(limits.max_daily_loss)],
      ["Max strategy loss", money(limits.max_strategy_loss)],
      ["Max open positions", num(limits.max_open_positions, 0)],
      ["Stale data threshold (seconds)", num(limits.stale_data_threshold_seconds, 0)]
    ];
    summary.innerHTML = `<p class="form-note">CONFIGURED PAPER LIMITS · not client balances. DERIVATIVE ACTIVATION is determined by source certification, never by this screen.</p>` +
      rows.map(([label, value]) => `<div class="risk-row"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("");
    detail.textContent = JSON.stringify({observed_at: new Date().toISOString(),
      limits_source:"/api/paper/risk-limits", overview_source:"/api/overview",
      capability_source:"/api/paper/capabilities", limits, overview, capabilities}, null, 2);
  }

  function applyPreset(name) {
    const presets = {"CIO Command": {view:"command", market:"TW", timeframe:"1D", layout:"dense"}, "TW Swing": {view:"markets", market:"TW", timeframe:"1W", layout:"wide"}, "US Growth": {view:"chart", market:"US", timeframe:"1M", layout:"wide"}, "Intraday Lab": {view:"chart", market:"US", timeframe:"1D", layout:"dense"}, "Research Review": {view:"research", market:"TW", timeframe:"Replay", layout:"wide"}};
    const preset = presets[name]; if (!preset) return;
    state.watchMarket = preset.market; state.timeframe = preset.timeframe; state.layout = preset.layout; document.body.dataset.layout = preset.layout;
    $$(".preset").forEach(p => p.classList.toggle("active", p.textContent.trim() === name));
    $$(".seg").forEach(s => s.classList.toggle("active", s.dataset.watch === state.watchMarket));
    $$(".time-tab").forEach(s => s.classList.toggle("active", s.dataset.timeframe === state.timeframe));
    $("#desk-timeframe").value = state.timeframe; navigate(preset.view); renderWatchlist(); loadBars(state.selectedSymbol); showToast(`${name} preset applied`);
  }

  function setupNavigation() {
    $$(".nav-item").forEach(item => item.addEventListener("click", () => navigate(item.dataset.view)));
    $$('[data-view-jump]').forEach(item => item.addEventListener("click", () => navigate(item.dataset.viewJump)));
    $$(".preset").forEach(item => item.addEventListener("click", () => applyPreset(item.textContent.trim())));
    $$(".seg").forEach(item => item.addEventListener("click", () => { $$(".seg").forEach(s => s.classList.remove("active")); item.classList.add("active"); state.watchMarket = item.dataset.watch; renderWatchlist(); }));
    $$(".time-tab").forEach(item => item.addEventListener("click", () => { $$(".time-tab").forEach(s => s.classList.remove("active")); item.classList.add("active"); state.timeframe = item.dataset.timeframe; $("#desk-timeframe").value = state.timeframe; loadBars(state.selectedSymbol); }));
  }

  function renderWatchlist() {
    const items = state.watchlists[state.watchMarket] || [];
    $("#watch-count").textContent = `${items.length} symbols`;
    const list = $("#watchlist");
    if (!items.length) { list.innerHTML = state.apiErrors.length ? stateHtml("Watchlist endpoint unavailable.") : stateHtml("No symbols in this workspace."); return; }
    list.innerHTML = items.map(item => {
      const isUp = Number(item.change_pct) >= 0;
      const stale = item.is_stale || item.quality === "fallback" || item.quality === "missing";
      return `<div class="watch-row" data-symbol="${esc(item.symbol)}"><div><div class="watch-symbol">${esc(item.symbol)}</div><div class="watch-name">${esc(item.name || "—")}</div></div><div><div class="watch-quote">${num(item.last)}</div><div class="watch-change ${isUp ? "up" : "down"}">${pct(item.change_pct)}</div></div><div class="watch-meta"><span>${esc(item.source || "source unavailable")}</span><span>${stale ? "STALE / blocked" : (item.delay_seconds ? `${Math.round(item.delay_seconds)}s age` : "age —")}</span></div></div>`;
    }).join("");
    $$(".watch-row", list).forEach(row => row.addEventListener("click", () => selectSymbol(row.dataset.symbol)));
  }

  function selectSymbol(symbol) {
    state.selectedSymbol = symbol;
    const all = [...(state.watchlists.TW || []), ...(state.watchlists.US || [])];
    state.selectedQuote = all.find(item => item.symbol === symbol) || {symbol};
    $("#order-symbol").value = symbol;
    $("#order-market").value = symbol.endsWith(".TW") ? "TW" : "US";
    $("#chart-title").innerHTML = `${esc(symbol)} <span class="instrument-name">${esc(state.selectedQuote.name || "")}</span>`;
    $("#desk-chart-title").textContent = `${symbol} · delayed bars`;
    loadBars(symbol);
  }

  function renderOverview() {
    const ov = state.overview || {};
    const regimes = ov.market_regimes || {};
    const tw = regimes.TW || {}, us = regimes.US || {};
    $("#tw-session").textContent = tw.status || "CLOSED / unavailable";
    $("#us-session").textContent = us.status || "CLOSED / unavailable";
    $("#taiex-value").textContent = num(tw.last_price);
    $("#taiex-change").textContent = pct(tw.change_pct);
    $("#spx-value").textContent = num(us.last_price);
    $("#spx-change").textContent = pct(us.change_pct);
    [$("#taiex-change"), $("#spx-change")].forEach(el => { if (el) el.className = Number(el.textContent) >= 0 ? "up-text" : "down-text"; });
    // The competition endpoint preserves native ledgers; missing FX is not zero.
    const competition = state.competition;
    const native = Object.entries(competition?.native_totals || {});
    $("#paper-equity").textContent = native.length
      ? native.map(([currency, balances]) => `${currency} ${num(balances.equity)}`).join(" / ") : "—";
    $("#paper-pnl").textContent = !competition ? "Native NAV unavailable"
      : competition.nav_status !== "VALID" ? "Combined NAV unavailable · no assumed FX"
      : `TWD return ${pct(competition.total?.return_pct)}`;
    $("#gross-exposure").textContent = state.positions.length ? `${state.positions.length} positions` : "0%";
    const safety = ov.safety_guards || {};
    state.killSwitch = Boolean(safety.kill_switch);
    updateKillSwitch();
    $("#as-of").textContent = `As of ${stamp(ov.timestamp)}`;
  }

  function updateKillSwitch() {
    const label = $("#kill-state"); label.textContent = state.killSwitch ? "ON / BLOCK" : "ARMED / OFF"; label.className = state.killSwitch ? "" : "safe"; if (state.killSwitch) label.style.color = "var(--red)";
    $("#kill-toggle").classList.toggle("on", state.killSwitch); $("#portfolio-kill").textContent = state.killSwitch ? "ON / block new entries" : "OFF";
  }

  function drawChart(canvas, bars, large = false) {
    if (!canvas) return;
    const wrap = canvas.parentElement; const rect = wrap.getBoundingClientRect(); const dpr = window.devicePixelRatio || 1; const width = Math.max(260, rect.width); const height = Math.max(180, rect.height);
    canvas.width = width * dpr; canvas.height = height * dpr; canvas.style.width = `${width}px`; canvas.style.height = `${height}px`;
    const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr); ctx.clearRect(0, 0, width, height);
    ctx.strokeStyle = "rgba(93,119,145,.18)"; ctx.lineWidth = 1; ctx.font = "9px ui-monospace";
    [0.18, .42, .66, .9].forEach(r => { ctx.beginPath(); ctx.moveTo(34, height * r); ctx.lineTo(width - 12, height * r); ctx.stroke(); });
    if (!bars || bars.length < 2) return;
    const pad = {l:38,r:15,t:18,b:large ? 35 : 42}; const chartH = height - pad.t - pad.b; const volumes = bars.map(b => Number(b.volume) || 0); const maxVol = Math.max(...volumes, 1); const min = Math.min(...bars.map(b => Number(b.low ?? b.l))); const max = Math.max(...bars.map(b => Number(b.high ?? b.h))); const span = Math.max(max - min, 0.01); const step = (width - pad.l - pad.r) / bars.length; const body = Math.max(2, Math.min(12, step * .55));
    const y = value => pad.t + (max - value) / span * (chartH * .72); const volumeTop = pad.t + chartH * .77;
    bars.forEach((bar, i) => { const open = Number(bar.open ?? bar.o), high = Number(bar.high ?? bar.h), low = Number(bar.low ?? bar.l), close = Number(bar.close ?? bar.c); const x = pad.l + i * step + step / 2; const color = close >= open ? "#3ed598" : "#ff6d7a"; ctx.strokeStyle = color; ctx.fillStyle = color; ctx.globalAlpha = .92; ctx.beginPath(); ctx.moveTo(x, y(high)); ctx.lineTo(x, y(low)); ctx.stroke(); const top = y(Math.max(open, close)), bottom = y(Math.min(open, close)); ctx.fillRect(x - body / 2, top, body, Math.max(2, bottom - top)); ctx.globalAlpha = .28; const vh = ((volumes[i] / maxVol) * (chartH * .18)); ctx.fillRect(x - body / 2, height - pad.b + 2 - vh, body, vh); ctx.globalAlpha = 1; });
    ctx.fillStyle = "#64758a"; ctx.fillText(num(max), 5, pad.t + 4); ctx.fillText(num(min), 5, volumeTop - 4); ctx.fillText("VOL", 5, height - 9); ctx.fillStyle = "#9db4c8"; ctx.fillText(`${state.selectedSymbol} · ${state.chartSource}`, pad.l, 11);
  }

  function renderBarsMeta(bars, payload) {
    const last = bars[bars.length - 1], prev = bars[bars.length - 2]; if (!last) return;
    $("#ohlc-open").textContent = num(last.open); $("#ohlc-high").textContent = num(last.high); $("#ohlc-low").textContent = num(last.low); $("#ohlc-close").textContent = num(last.close);
    const stale = last.is_stale || last.quality === "fallback" || payload?.provider_mode === "fallback" || payload?.provider_mode === "offline"; state.chartSource = stale ? "REPLAY / FALLBACK" : "DELAYED DATA"; $("#chart-source").textContent = state.chartSource; $("#chart-freshness").textContent = `bar ${stamp(last.timestamp)} · ${stale ? "stale / execution blocked" : "delayed"}`;
    $("#chart-state").textContent = ""; if ($("#desk-chart-state")) $("#desk-chart-state").textContent = "";
    const range = bars.length > 1 ? `${stamp(bars[0].timestamp)} → ${stamp(last.timestamp)}` : stamp(last.timestamp);
    if ($("#desk-chart-meta")) $("#desk-chart-meta").textContent = `${state.selectedSymbol} · ${state.timeframe} · ${bars.length} bars · ${range} · ${state.chartSource}`;
    if (prev) { const ch = ((Number(last.close) / Number(prev.close)) - 1) * 100; $("#ohlc-close").className = ch >= 0 ? "up-text" : "down-text"; }
  }

  async function loadBars(symbol, timeframe = state.timeframe) {
    state.timeframe = timeframe; $("#chart-state").textContent = "Loading bars…"; if ($("#desk-chart-state")) $("#desk-chart-state").textContent = "Loading bars…";
    const query = new URLSearchParams({limit: timeframe === "1D" ? "64" : timeframe === "1W" ? "80" : "120", timeframe});
    const payload = await api(`/api/market/bars/${encodeURIComponent(symbol)}?${query.toString()}`);
    let bars = payload?.bars || [];
    if (!bars.length) { bars = []; state.chartSource = "NO DATA"; $("#chart-state").textContent = "No bars available. Paper entry remains blocked."; if ($("#desk-chart-state")) $("#desk-chart-state").textContent = "No bars available."; setNotice("Market bars unavailable; chart is in an honest empty state.", "info"); }
    state.bars = bars; renderBarsMeta(bars, payload || {}); drawChart($("#price-chart"), bars); drawChart($("#desk-chart"), bars, true);
    if (bars.length) setNotice(payload?.provider_error ? `Provider reported: ${payload.provider_error}` : (payload.provider_mode === "fallback" ? "Fallback/replay data is visible; new autonomous paper entries remain blocked." : null), "info");
  }

  function renderMarkets(payload) {
    state.markets = payload || {}; const breadth = payload?.breadth || payload || {}; const tw = breadth.TW || breadth.tw || {}; const us = breadth.US || breadth.us || {};
    const value = x => `${x.advances ?? x.up ?? "—"} / ${x.declines ?? x.down ?? "—"}`;
    $("#tw-breadth").textContent = value(tw); $("#us-breadth").textContent = value(us);
    const leaders = payload?.leaders || []; const laggards = payload?.laggards || []; $("#leaders-laggards").textContent = `${leaders.length || payload?.leaders_count || "—"} / ${laggards.length || payload?.laggards_count || "—"}`;
    $("#markets-source").textContent = payload?.source || payload?.provider_mode || "endpoint";
    const rows = [...leaders.slice(0, 5).map(item => ({...item, role:"LEADER"})), ...laggards.slice(0, 5).map(item => ({...item, role:"LAGGARD"}))];
    $("#markets-list").innerHTML = rows.length ? rows.map(item => `<div class="market-row"><span class="badge ${item.role === "LEADER" ? "badge-good" : "badge-bad"}">${item.role}</span><strong>${esc(item.symbol || item.name || "—")}</strong><span>${pct(item.change_pct ?? item.change)}</span><span class="muted">${esc(item.name || item.source || "")}</span></div>`).join("") : stateHtml(payload ? "Breadth endpoint returned no leaders or laggards." : "Markets endpoint unavailable.");
  }

  async function loadMarkets() {
    const [tw, us, movers] = await Promise.all([
      api("/api/markets/breadth?market=TW"),
      api("/api/markets/breadth?market=US"),
      api("/api/markets/leaders")
    ]);
    renderMarkets({
      breadth: {TW: tw || {}, US: us || {}},
      leaders: movers?.leaders || [],
      laggards: movers?.laggards || [],
      source: movers?.freshness?.provider_mode || movers?.freshness?.provider || "unavailable"
    });
  }

  function tableHtml(rows, columns, empty = "No records returned.") { if (!rows?.length) return stateHtml(empty); return `<table class="mini-table"><thead><tr>${columns.map(c => `<th>${esc(c.label)}</th>`).join("")}</tr></thead><tbody>${rows.slice(0, 5).map(row => `<tr>${columns.map(c => `<td>${c.render ? c.render(row) : esc(row[c.key])}</td>`).join("")}</tr>`).join("")}</tbody></table>`; }
  function renderLedgers() {
    const p = state.portfolios || {};
    const rows = value => Array.isArray(value) ? value : Object.values(value || {});
    const all = [...rows(p.swing?.positions), ...rows(p.intraday?.positions)]; state.positions = all;
    if (!state.orders.length) state.orders = [...rows(p.swing?.orders), ...rows(p.intraday?.orders)];
    state.fills = [...rows(p.swing?.fills), ...rows(p.intraday?.fills)];
    const cols = [{label:"SYMBOL",key:"symbol",render:r=>`<strong>${esc(r.symbol)}</strong>`},{label:"QTY",key:"quantity"},{label:"MARK",key:"market_price",render:r=>num(r.market_price ?? r.last_price)},{label:"P&L",key:"unrealized_pnl",render:r=>`<span class="${Number(r.unrealized_pnl)>=0?"up-text":"down-text"}">${money(r.unrealized_pnl)}</span>`}];
    $("#positions-table").innerHTML = tableHtml(all, cols, "No paper positions returned.");
    $("#orders-table").innerHTML = tableHtml(state.orders, [{label:"SYMBOL",key:"symbol"},{label:"SIDE",key:"side"},{label:"QTY",key:"quantity"},{label:"STATUS",key:"status"}], "No working orders returned.");
    $("#fills-table").innerHTML = tableHtml(state.fills, [{label:"SYMBOL",key:"symbol"},{label:"SIDE",key:"side"},{label:"PRICE",key:"price",render:r=>num(r.price)},{label:"QTY",key:"quantity"}], "No paper fills returned.");
    renderPortfolioCards(); renderPaperLedger(); renderOverview();
  }
  function renderPortfolioCards() { const p = state.portfolios || {}; $("#portfolio-cards").innerHTML = ["swing","intraday"].map(key => { const x = p[key]; if (!x) return `<div class="portfolio-card"><div class="label">${key.toUpperCase()}</div><strong>—</strong><small>ledger endpoint unavailable</small></div>`; return `<div class="portfolio-card"><div class="label">${key.toUpperCase()} LEDGER</div><strong>${money(x.equity)}</strong><small>cash ${money(x.cash)} · realized ${money(x.realized_pnl)} · positions ${Array.isArray(x.positions) ? x.positions.length : Object.keys(x.positions || {}).length}</small></div>`; }).join(""); }
  function renderPaperLedger() {
    $("#paper-ledger").innerHTML = tableHtml([...state.orders, ...state.fills], [
      {label:"TYPE",render:r=>r.fill_id?"FILL":"ORDER"},
      {label:"SYMBOL",key:"symbol"},
      {label:"SIDE",key:"side"},
      {label:"PRICE",render:r=>num(r.price ?? r.limit_price)},
      {label:"ORIGIN",render:r=>esc(r.origin || r.order_origin || "—")},
      {label:"VERSION",render:r=>esc(r.strategy_version || "—")},
      {label:"STATUS",render:r=>esc(r.status || "FILLED")},
      {label:"ACTIONS",render:r=>{
        const editable = r.order_id && r.origin === "MANUAL" && ["PENDING","PARTIALLY_FILLED"].includes(r.status);
        return editable ? `<button class="text-btn" data-order-cancel="${esc(r.order_id)}">Cancel</button> <button class="text-btn" data-order-replace="${esc(r.order_id)}">Replace</button>` : "—";
      }}
    ], "No paper orders or fills returned.");
    $("[data-order-cancel]", $("#paper-ledger")).forEach(button => button.addEventListener("click", async () => {
      button.disabled = true;
      const result = await api(`/api/paper/orders/${encodeURIComponent(button.dataset.orderCancel)}/cancel`, {method:"POST"});
      showToast(result ? "Paper order cancelled." : "Cancel failed; order state unchanged.");
      await loadAll();
    }));
    $("[data-order-replace]", $("#paper-ledger")).forEach(button => button.addEventListener("click", () => {
      const order = state.orders.find(item => item.order_id === button.dataset.orderReplace);
      if (!order) return;
      state.replaceOrderId = order.order_id;
      state.orderPreview = null;
      $("#order-symbol").value = order.symbol;
      $("#order-market").value = order.market;
      $("#order-side").value = order.side;
      $("#order-type").value = order.order_type;
      $("#order-qty").value = order.remaining_quantity || order.quantity;
      $("#order-price").value = order.limit_price ?? "";
      $("#order-stop").value = order.stop_price ?? "";
      $("#order-bucket").value = order.bucket;
      $("#order-origin").value = "MANUAL";
      $("#order-reason").value = `Replace ${order.order_id}: ${order.reason || "manual paper order"}`;
      $(".submit-order").innerHTML = "Preview replacement <span>→</span>";
      navigate("command");
      $("#order-message").textContent = `Replacement mode for ${order.order_id}. Preview and confirm to cancel-replace.`;
      $("#order-message").className = "inline-message info";
    }));
  }

  function renderStrategies() { const el = $("#strategy-list"); if (!state.strategies.length) { el.innerHTML = stateHtml("No strategies returned from registry."); return; } el.innerHTML = state.strategies.map(s => `<article class="strategy-card"><div class="strategy-card-head"><div><h3>${esc(s.name || s.id)}</h3><div class="strategy-meta">${esc(s.id)} · v${esc(s.version || "—")} · hash ${esc((s.code_hash || "—").slice(0,10))}</div></div><span class="badge ${s.status === "PAPER_ACTIVE" ? "badge-good" : "badge-neutral"}">${esc(s.status || "UNKNOWN")}</span></div><div class="strategy-params">${Object.entries(s.config || {}).map(([k,v]) => `<span class="param">${esc(k)}=${esc(v)}</span>`).join("") || '<span class="muted">manifest parameters unavailable</span>'}</div></article>`).join(""); }
  function renderResearch() { const el = $("#research-list"); if (!state.research.length) { el.innerHTML = stateHtml("Inbox empty. Sources stay quarantined until verified."); return; } el.innerHTML = state.research.map(r => `<article class="research-card"><div class="research-card-head"><div><h3>${esc(r.title)}</h3><div class="research-meta">${esc(r.id || "item")} · ${esc(r.source_mode || "source")} · ${esc((r.related_symbols || []).join(", ") || "no symbols")}</div></div><span class="badge badge-neutral">${esc((r.status || "UNVERIFIED").toUpperCase())}</span></div><div class="research-meta">${esc(r.url)}</div></article>`).join(""); }
  function renderDiagnostics() { $("#diagnostics-json").textContent = JSON.stringify(state.diagnostics || {status:"unavailable", note:"Diagnostics endpoint did not respond."}, null, 2); $("#endpoint-health").innerHTML = ["/api/health","/api/overview","/api/watchlists","/api/portfolios","/api/paper/orders","/api/paper/kill-switch","/api/market/bars/:symbol"].map(path => `<div class="health-row"><span>${path}</span><span class="badge ${state.apiErrors.some(x => x.path.startsWith(path.replace(":symbol", ""))) ? "badge-bad" : "badge-good"}">${state.apiErrors.some(x => x.path.startsWith(path.replace(":symbol", ""))) ? "UNAVAILABLE" : "PROBED"}</span></div>`).join(""); }

  function updateTopData() { const w = state.watchlists[state.watchMarket] || []; const source = w.find(x => x.source)?.source || state.overview?.market_regimes?.TW?.source || "unknown"; $("#data-provider").textContent = source; const latest = w.find(x => x.market_timestamp || x.observed_at); $("#data-age").textContent = latest?.observed_at ? `obs ${stamp(latest.observed_at)}` : "age —"; }

  async function loadAll() {
    state.apiErrors = []; setNotice(null); $("#watchlist").innerHTML = stateHtml("Loading watchlist", "loading");
    const [overview, watchlists, portfolios, strategies, research, diagnostics, signals, fills, paperOrders, killSwitch, experiments, competition] = await Promise.all([api("/api/overview"), api("/api/watchlists"), api("/api/portfolios"), api("/api/strategies"), api("/api/research/inbox"), api("/api/diagnostics"), api("/api/signals"), api("/api/fills"), api("/api/paper/orders"), api("/api/paper/kill-switch"), api("/api/paper/experiments"), api("/api/paper/competition")]);
    state.competition = competition; state.overview = overview; if (watchlists) state.watchlists = {TW: watchlists.TW || [], US: watchlists.US || []}; state.portfolios = portfolios; state.strategies = Array.isArray(strategies) ? strategies : []; state.research = Array.isArray(research) ? research : []; state.diagnostics = diagnostics; state.experiments = Array.isArray(experiments) ? experiments : []; if (fills) state.fills = fills; state.orders = Array.isArray(paperOrders) ? paperOrders : []; if (killSwitch) state.killSwitch = Boolean(killSwitch.enabled);
    renderOverview(); renderWatchlist(); renderLedgers(); renderStrategies(); renderExperiments(); renderResearch(); renderDiagnostics(); updateTopData();
    const selected = [...state.watchlists.TW, ...state.watchlists.US].find(x => x.symbol === state.selectedSymbol) || state.watchlists.TW[0] || state.watchlists.US[0]; if (selected) selectSymbol(selected.symbol); else { $("#chart-state").textContent = "No selected market data."; }
    // Market breadth can require many delayed Yahoo requests. Keep that optional
    // fan-out off the critical path so Strategy/Paper controls remain usable.
    loadMarkets();
    if (state.apiErrors.length) setNotice("Some optional endpoints are unavailable. The workstation is showing honest empty/stale states rather than synthetic live claims.", "info");
  }

  function renderExperiments() {
    const select = $("#experiment-strategy"); if (!select) return;
    const selectedBefore = select.value;
    const merged = [...state.strategies.map(s => ({id:s.id, name:s.name || s.id})), ...state.experiments.map(x => ({id:x.strategy_id, name:x.strategy_id}))];
    const items = [...new Map(merged.map(item => [item.id, item])).values()];
    if (!items.length) items.push({id:"opening_range_breakout", name:"Opening Range Breakout"});
    select.innerHTML = items.map(s => `<option value="${esc(s.id)}">${esc(s.name || s.id)}</option>`).join("");
    const preferred = selectedBefore || state.experiments.find(x => x.enabled)?.strategy_id || state.experiments[0]?.strategy_id;
    if (preferred && items.some(x => x.id === preferred)) select.value = preferred;
    const current = state.experiments.find(x => x.strategy_id === select.value);
    const enabled = Boolean(current?.enabled); $("#experiment-enabled").checked = enabled; $("#experiment-badge").textContent = enabled ? "PAPER ACTIVE" : "OFF"; $("#experiment-badge").className = `badge ${enabled ? "badge-good" : "badge-neutral"}`;
    if (current) { $("#experiment-universe").value = (current.universe || []).join(", "); $("#experiment-mode").value = current.mode || "swing"; $("#experiment-position").value = current.max_position_notional ?? 50000; $("#experiment-loss").value = current.max_daily_loss ?? 5000; $("#experiment-open").value = current.max_open_positions ?? 5; if (current.expires_at) $("#experiment-expiry").value = new Date(current.expires_at).toISOString().slice(0,16); }
    loadExperimentRuntime(select.value);
  }

  async function loadExperimentRuntime(strategyId) {
    const target = $("#experiment-runtime"); if (!target || !strategyId) return;
    const [status, history] = await Promise.all([api(`/api/paper/experiments/${encodeURIComponent(strategyId)}/status`), api(`/api/paper/experiments/${encodeURIComponent(strategyId)}/history?limit=20`)]);
    if (!status) { target.innerHTML = `<div class="risk-row"><span>Runner</span><strong>unavailable</strong></div>`; return; }
    const last = status.last_run || {}; const runs = history?.runs || []; const decisions = history?.decisions || [];
    const scheduleLabel = status.decision_schedule === "once_after_each_market_close"
      ? "Once after each market close"
      : `Market open · every ${num(status.cadence_seconds,0)} sec`;
    target.innerHTML = `<div class="risk-row"><span>Runner</span><strong>${status.running ? "RUNNING" : "STOPPED"}</strong></div><div class="risk-row"><span>Mode</span><strong>${esc(status.mode || state.experiments.find(x => x.strategy_id === strategyId)?.mode || "—")}</strong></div><div class="risk-row"><span>Schedule</span><strong>${esc(scheduleLabel)}</strong></div><div class="risk-row"><span>Decision engine</span><strong>${status.llm_involved ? "LLM" : "Deterministic rules (no LLM)"}</strong></div><div class="risk-row"><span>Last cycle</span><strong>${esc(last.status || "none")} · ${num(last.fills_count || 0,0)} fills</strong></div><div class="risk-row"><span>Audit trail</span><strong>${runs.length} runs · ${decisions.length} decisions</strong></div>`;
    $("#experiment-badge").textContent = status.running ? "RUNNING" : (status.enabled ? "PAPER ACTIVE" : "OFF");
  }

  async function experimentAction(action) {
    const strategyId = $("#experiment-strategy").value; const message = $("#experiment-message");
    const endpoint = action === "cycle" ? "run-one-cycle" : action;
    const result = await api(`/api/paper/experiments/${encodeURIComponent(strategyId)}/${endpoint}`, {method:"POST"});
    if (!result) { message.textContent = `Runner ${action} failed.`; message.className = "inline-message error"; return; }
    message.textContent = action === "cycle" ? `Cycle complete: ${result.run?.decisions_count || 0} decisions, ${result.run?.fills_count || 0} fills.` : `Runner ${action === "start" ? "started" : "stopped"}.`;
    message.className = "inline-message success"; await loadExperimentRuntime(strategyId); await loadAll();
  }

  async function saveExperiment(event) {
    event.preventDefault(); const strategy_id = $("#experiment-strategy").value; const expiry = $("#experiment-expiry").value;
    const mode = $("#experiment-mode").value;
    const payload = {strategy_id, enabled:$("#experiment-enabled").checked, universe:$("#experiment-universe").value.split(",").map(x=>x.trim().toUpperCase()).filter(Boolean), mode, max_position_notional:Number($("#experiment-position").value), max_daily_loss:Number($("#experiment-loss").value), max_open_positions:Number($("#experiment-open").value), allowed_buckets:[mode], expires_at:expiry ? new Date(expiry).toISOString() : null};
    const result = await api(`/api/paper/experiments/${encodeURIComponent(strategy_id)}`, {method:"PUT", body:JSON.stringify(payload)}); const message = $("#experiment-message");
    if (!result) { message.textContent = "Experiment endpoint unavailable; paper automation was not changed."; message.className = "inline-message error"; return; }
    state.experiments = state.experiments.filter(x => x.strategy_id !== strategy_id).concat(result); message.textContent = result.enabled ? "Bounded paper experiment enabled; no broker route exists." : "Experiment saved OFF."; message.className = "inline-message success"; renderExperiments();
  }

  async function searchSymbols(query) {
    const results = $("#symbol-search-results"); if (!query.trim()) { results.innerHTML = ""; return; }
    const payload = await api(`/api/markets/search?q=${encodeURIComponent(query.trim())}`); let items = payload?.results || payload || [];
    if (!Array.isArray(items)) items = [];
    if (!items.length) items = [...state.watchlists.TW, ...state.watchlists.US].filter(x => `${x.symbol} ${x.name || ""}`.toLowerCase().includes(query.toLowerCase())).slice(0, 8);
    results.innerHTML = items.length ? items.slice(0, 8).map(item => `<button type="button" class="search-result" data-symbol="${esc(item.symbol)}"><strong>${esc(item.symbol)}</strong><span>${esc(item.name || item.exchange || "add to watchlist")}</span></button>`).join("") : `<span class="muted">No matches from /api/markets/search</span>`;
    $$(".search-result", results).forEach(button => button.addEventListener("click", () => { const symbol = button.dataset.symbol; const market = symbol.endsWith(".TW") ? "TW" : "US"; state.selectedSymbol = symbol; state.watchMarket = market; if (!state.watchlists[market].some(x => x.symbol === symbol)) state.watchlists[market].unshift({symbol, name:symbol, source:"search", quality:"unverified"}); renderWatchlist(); $("#chart-symbol-input").value = symbol; results.innerHTML = `<span class="muted">Added ${esc(symbol)} to ${market} watchlist</span>`; loadBars(symbol); }));
  }

  function setupMarketControls() {
    $("#symbol-search-form")?.addEventListener("submit", event => { event.preventDefault(); searchSymbols($("#symbol-search").value); });
    $("#chart-symbol-load")?.addEventListener("click", () => { const symbol = $("#chart-symbol-input").value.trim().toUpperCase(); if (!symbol) return; state.selectedSymbol = symbol; $("#desk-chart-title").textContent = symbol; loadBars(symbol, $("#desk-timeframe").value); });
    $("#desk-timeframe")?.addEventListener("change", event => { state.timeframe = event.target.value; $$(".time-tab").forEach(s => s.classList.toggle("active", s.dataset.timeframe === state.timeframe)); loadBars(state.selectedSymbol, state.timeframe); });
    $("#experiment-form")?.addEventListener("submit", saveExperiment); $("#experiment-strategy")?.addEventListener("change", renderExperiments);
    $("#experiment-start")?.addEventListener("click", () => experimentAction("start")); $("#experiment-cycle")?.addEventListener("click", () => experimentAction("cycle")); $("#experiment-stop")?.addEventListener("click", () => experimentAction("stop"));
    $("#layout-btn")?.addEventListener("click", () => { state.layout = state.layout === "dense" ? "wide" : "dense"; document.body.dataset.layout = state.layout; $("#layout-btn").setAttribute("title", `Layout: ${state.layout}`); showToast(`Layout set to ${state.layout}`); drawChart($("#price-chart"), state.bars); });
  }

  function setupOrderForm() {
    const formEl = $("#order-form"), submit = $(".submit-order");
    const resetPreview = () => { state.orderPreview = null; submit.innerHTML = "Preview paper order <span>→</span>"; };
    formEl.addEventListener("input", resetPreview);
    formEl.addEventListener("submit", async event => {
      event.preventDefault();
      const raw = Object.fromEntries(new FormData(formEl).entries());
      const quote = [...state.watchlists.TW, ...state.watchlists.US].find(item => item.symbol === raw.symbol);
      const lastBar = state.selectedSymbol === raw.symbol ? state.bars.at(-1) : null;
      const observedAt = quote?.observed_at || quote?.market_timestamp || lastBar?.observed_at || lastBar?.timestamp || new Date().toISOString();
      const ageSeconds = Math.max(0, (Date.now() - new Date(observedAt).getTime()) / 1000);
      const dataStale = !quote && !lastBar || Boolean(quote?.is_stale || lastBar?.is_stale);
      const dataFallback = [quote?.quality, lastBar?.quality].includes("fallback") || [quote?.source, lastBar?.source].some(value => String(value || "").toLowerCase().includes("fallback"));
      const payload = {
        symbol: raw.symbol.trim().toUpperCase(), market: raw.market, bucket: raw.bucket, side: raw.side,
        order_type: raw.type, quantity: Number(raw.quantity), limit_price: raw.price ? Number(raw.price) : null,
        stop_price: raw.stop ? Number(raw.stop) : null, origin: raw.origin, reason: raw.reason.trim(),
        explicit_user_instruction: false, audit_metadata: {ui:"cio-market-lab-v2", paper_only:true, two_step_confirmation:true},
        data: {source: quote?.source || lastBar?.source || "ui_no_source", observed_at: observedAt, age_seconds: Number.isFinite(ageSeconds) ? ageSeconds : 999999, last_price: quote?.last || lastBar?.close || null, is_stale: dataStale, is_fallback: dataFallback}
      };
      const message = $("#order-message");
      if (!payload.reason) { message.textContent = "Reason/evidence is required before a paper order can be previewed."; message.className = "inline-message error"; return; }
      if (state.killSwitch) { message.textContent = "Kill switch is ON. New simulated orders are blocked."; message.className = "inline-message error"; return; }
      const formSignature = JSON.stringify(raw);
      if (state.orderPreview?.response?.status === "APPROVED" && state.orderPreview.formSignature === formSignature) {
        const endpoint = state.replaceOrderId
          ? `/api/paper/orders/${encodeURIComponent(state.replaceOrderId)}/cancel-replace`
          : "/api/paper/orders";
        const result = await api(endpoint, {method:"POST", body:JSON.stringify(state.orderPreview.payload)});
        if (!result) { message.textContent = "Simulation engine rejected the order. Nothing was sent to a broker."; message.className = "inline-message error"; return; }
        message.textContent = state.replaceOrderId
          ? `Replacement accepted: ${result.order?.order_id || "local ledger entry"}. No broker route exists.`
          : `Simulated order accepted: ${result.order?.order_id || "local ledger entry"}. No broker route exists.`;
        message.className = "inline-message success"; state.replaceOrderId = null; resetPreview(); await loadAll(); return;
      }
      const preview = await api("/api/paper/orders/preview", {method:"POST", body:JSON.stringify(payload)});
      if (!preview) { message.textContent = "Paper preview service unavailable; nothing was submitted."; message.className = "inline-message error"; return; }
      state.orderPreview = {response: preview, payload, formSignature};
      const risk = preview.risk_decision || {}; const reasons = (risk.reasons || []).join(", ") || "all configured gates passed";
      $("#risk-preview").innerHTML = `<div class="risk-row"><span>Requested notional</span><strong>${money(risk.notional)}</strong></div><div class="risk-row"><span>Fees / tax / slippage</span><strong>${money(risk.estimated_total_cost)}</strong></div><div class="risk-row"><span>Origin / bucket</span><strong>${esc(payload.origin)} · ${esc(payload.bucket)}</strong></div><div class="risk-row"><span>Risk gate</span><span class="badge ${preview.status === "APPROVED" ? "badge-good" : "badge-bad"}">${esc(preview.status)}</span></div>`;
      message.textContent = preview.status === "APPROVED" ? "Preview approved. Review once more, then submit to the local simulation ledger." : `Blocked: ${reasons}`;
      message.className = `inline-message ${preview.status === "APPROVED" ? "info" : "error"}`;
      submit.innerHTML = preview.status === "APPROVED" ? "Submit simulated order <span>→</span>" : "Preview paper order <span>→</span>";
    });
  }
  function setupResearchForm() { $("#research-form").addEventListener("submit", async event => { event.preventDefault(); const payload = {url:$("#research-url").value, title:$("#research-title").value, related_symbols:$("#research-symbols").value.split(",").map(x=>x.trim()).filter(Boolean), hypothesis:$("#research-hypothesis").value, claims:[], source_mode:"manual_intake", status:"unverified"}; const result = await api("/api/research/intake", {method:"POST", body:JSON.stringify(payload)}); if (result) { showToast("Source quarantined in research inbox."); event.currentTarget.reset(); await loadAll(); } else showToast("Research intake endpoint unavailable."); }); }
  function setupCio() { const drawer = $("#cio-drawer"); $("#open-cio-btn").addEventListener("click", () => drawer.classList.add("open")); $("#close-cio-btn").addEventListener("click", () => drawer.classList.remove("open")); $("#chart-ask-btn").addEventListener("click", () => drawer.classList.add("open")); $("#cio-form").addEventListener("submit", async event => { event.preventDefault(); const input = $("#cio-input"); const prompt = input.value.trim(); if (!prompt) return; $("#cio-thread").insertAdjacentHTML("beforeend", `<div class="cio-message cio-user"><span class="message-label">USER / DRAFT</span>${esc(prompt)}</div>`); input.value = ""; const result = await api("/api/integrations/hermes/chat-draft", {method:"POST", body:JSON.stringify({symbol:state.selectedSymbol,user_prompt:prompt,run_id:"ui-v2",visible_metrics:{last:state.selectedQuote?.last || null,source:state.chartSource},evidence_paths:["/api/overview","/api/market/bars"]})}); const text = result?.formatted_message || "Draft bridge unavailable. No message was sent."; $("#cio-thread").insertAdjacentHTML("beforeend", `<div class="cio-message cio-system"><span class="message-label">MAIN CIO DRAFT</span>${esc(text)}</div>`); }); }
  function setupPalette() { const palette = $("#command-palette"), input = $("#palette-input"), results = $("#palette-results"); const options = [{label:"Command Center",view:"command"},{label:"Markets",view:"markets"},{label:"Chart Desk",view:"chart"},{label:"Paper Trade",view:"paper"},{label:"Portfolio",view:"portfolio"},{label:"Replay",view:"replay"},{label:"Risk",view:"risk"},{label:"Strategy Lab",view:"strategy"},{label:"Research",view:"research"},{label:"Diagnostics",view:"diagnostics"}]; function render(q=""){ const filtered=options.filter(x=>x.label.toLowerCase().includes(q.toLowerCase())); results.innerHTML=filtered.map(x=>`<div class="palette-item" data-view="${x.view}">${x.label}</div>`).join(""); $$(".palette-item",results).forEach(x=>x.addEventListener("click",()=>{navigate(x.dataset.view);palette.classList.add("hidden")})); } $("#command-btn").addEventListener("click",()=>{palette.classList.remove("hidden");input.value="";render();input.focus()}); input.addEventListener("input",()=>render(input.value)); palette.addEventListener("click",e=>{if(e.target===palette)palette.classList.add("hidden")}); document.addEventListener("keydown",e=>{if(e.key==="Escape")palette.classList.add("hidden"); if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==="k"){e.preventDefault();palette.classList.remove("hidden");render();input.focus()}}); }
  function setupMisc() { $("#refresh-btn").addEventListener("click",()=>loadAll()); $("#diag-refresh").addEventListener("click",async()=>{state.diagnostics=await api("/api/diagnostics");renderDiagnostics();}); $("#kill-toggle").addEventListener("click",async()=>{const next=!state.killSwitch;const result=await api("/api/paper/kill-switch",{method:"POST",body:JSON.stringify({enabled:next,reason:"CIO Market Lab UI control"})});if(!result){showToast("Kill switch update failed; state unchanged.");return;}state.killSwitch=Boolean(result.enabled);updateKillSwitch();showToast(state.killSwitch?"Kill switch enabled: new paper entries blocked.":"Kill switch off: stale-data and risk gates still apply.")}); $("#prefill-order").addEventListener("click",()=>{if(state.bars.length){$("#order-price").value=Number(state.bars.at(-1).close).toFixed(2);state.orderPreview=null;$(".submit-order").innerHTML="Preview paper order <span>→</span>";showToast("Latest close copied to paper ticket; no order submitted.")}}); window.addEventListener("resize",()=>{drawChart($("#price-chart"),state.bars); if(state.view==="chart")drawChart($("#desk-chart"),state.bars,true)}); }
  document.addEventListener("DOMContentLoaded", () => { setupNavigation(); setupMarketControls(); setupOrderForm(); setupResearchForm(); setupCio(); setupPalette(); setupMisc(); loadAll(); });
})();
