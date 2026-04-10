let lastTimestamp = null;
let lastMetaAgeSeconds = null;
let lastBotStatus = null;

let proactiveCountdownIntervalId = null;
let lastStateForProactiveCountdown = null;

let lastGoodProactiveAnalysis = null;

let lastProactiveDecision = null;
let lastHadPosition = false;
let lastKnownClosedPnl = null;

let proactiveReasoningExpanded = false;

let uiHandlersBound = false;

function decisionLabel(decision) {
  const d = (decision || "").toString();
  return d.replaceAll("_", " ");
}

function bindUIHandlersOnce() {
  if (uiHandlersBound) return;

  const brainToggle = el("brain-toggle");
  if (brainToggle) {
    brainToggle.addEventListener("click", () => {
      try {
        toggleBrainReferencePanel();
      } catch (e) {
        // silent
      }
    });
  }

  const reasoningToggle = el("proactive-reasoning-toggle");
  if (reasoningToggle) {
    reasoningToggle.addEventListener("click", () => {
      try {
        toggleProactiveReasoning();
      } catch (e) {
        // silent
      }
    });
  }

  uiHandlersBound = true;
}

function decisionHexColor(decision) {
  const d = (decision || "").toString().toUpperCase();
  if (d === "OPEN_BUY") return "#4caf50";
  if (d === "OPEN_SELL") return "#e74c3c";
  if (d === "HOLD_TRADE") return "#2ecc71";
  if (d === "CLOSE_TRADE") return "#e67e22";
  if (d === "ADJUST_TRADE") return "#f1c40f";
  if (d === "WAIT") return "#8e8e8e";
  return "#8e8e8e";
}

function toggleBrainReferencePanel() {
  const panel = el("brain-reference-panel");
  const btn = el("brain-toggle");
  if (!panel || !btn) return;

  const isHidden = panel.classList.contains("hidden");
  if (isHidden) {
    panel.classList.remove("hidden");
    btn.textContent = "Hide Market Indicators";
    try {
      ensureIndicatorHistoryRunning(true);
    } catch (e) {
      // silent
    }
  } else {
    panel.classList.add("hidden");
    btn.textContent = "Show Market Indicators";
    try {
      ensureIndicatorHistoryRunning(false);
    } catch (e) {
      // silent
    }
  }
}

let indicatorHistory = null;
let indicatorHistoryLastFetch = 0;
let indicatorHistoryIntervalId = null;

function fetchJson(url) {
  return fetch(url, { cache: "no-store" }).then((r) => {
    if (!r.ok) throw new Error(`http_${r.status}`);
    return r.json();
  });
}

function ensureIndicatorHistoryRunning(shouldRun) {
  if (!shouldRun) {
    if (indicatorHistoryIntervalId != null) {
      clearInterval(indicatorHistoryIntervalId);
      indicatorHistoryIntervalId = null;
    }
    return;
  }

  if (indicatorHistoryIntervalId != null) return;
  // Fetch immediately and then refresh periodically while open
  refreshIndicatorHistory();
  indicatorHistoryIntervalId = setInterval(() => {
    try {
      refreshIndicatorHistory();
    } catch (e) {
      // silent
    }
  }, 60000);
}

function refreshIndicatorHistory() {
  const now = Date.now();
  if (now - indicatorHistoryLastFetch < 10000) return;
  indicatorHistoryLastFetch = now;

  fetchJson("/api/indicator-history?hours=6")
    .then((payload) => {
      indicatorHistory = payload || null;
      try {
        renderMarketIndicatorsPanel(lastStateForProactiveCountdown);
      } catch (e) {
        // silent
      }
    })
    .catch(() => {
      // silent
    });
}

function safeNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function sparklineSvg(series, opts = {}) {
  const w = opts.width || 180;
  const h = opts.height || 28;
  const stroke = opts.stroke || "rgba(148,163,184,0.9)";
  const fill = opts.fill || "none";
  const baseline = opts.baseline || "rgba(148,163,184,0.18)";

  const arr = Array.isArray(series) ? series.map(safeNum).filter((x) => x != null) : [];
  if (arr.length < 2) {
    return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" preserveAspectRatio="none"><line x1="0" y1="${h - 1}" x2="${w}" y2="${h - 1}" stroke="${baseline}" stroke-width="1" /></svg>`;
  }

  let min = Math.min(...arr);
  let max = Math.max(...arr);
  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" preserveAspectRatio="none"></svg>`;
  }
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const pad = (max - min) * 0.08;
  min -= pad;
  max += pad;

  const n = arr.length;
  const stepX = w / (n - 1);
  const pts = arr
    .map((v, i) => {
      const x = i * stepX;
      const t = (v - min) / (max - min);
      const y = h - t * (h - 2) - 1;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");

  return `
    <svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" preserveAspectRatio="none">
      <line x1="0" y1="${h - 1}" x2="${w}" y2="${h - 1}" stroke="${baseline}" stroke-width="1" />
      <polyline points="${pts}" fill="${fill}" stroke="${stroke}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  `.trim();
}

function marketIndicatorRow(label, valueText, sparkHtml, accent) {
  const color = accent || "rgba(226,232,240,0.9)";
  return `
    <div class="flex items-center gap-3 bg-black/20 rounded-lg p-3 border border-white/5">
      <div class="min-w-[78px] text-[10px] text-gray-500 font-semibold tracking-widest uppercase">${label}</div>
      <div class="flex-1">${sparkHtml}</div>
      <div class="font-mono text-xs font-bold" style="color:${color}">${valueText || "—"}</div>
    </div>
  `;
}

function rsiColor(rsi) {
  const v = safeNum(rsi);
  if (v == null) return null;
  if (v < 20 || v > 80) return "#f87171";
  if (v < 30 || v > 70) return "#fb923c";
  return "#34d399";
}

function macdColor(macd) {
  const v = safeNum(macd);
  if (v == null) return null;
  return v >= 0 ? "#34d399" : "#f87171";
}

function renderMarketIndicatorsPanel(state) {
  const grid = el("market-indicators-grid");
  if (!grid) return;

  const la = state?.last_analysis || {};
  const ind = la?.indicators || {};

  const rsi = safeNum(ind.rsi_14);
  const macd = safeNum(ind.macd);
  const adx = safeNum(ind.adx_14);
  const atr = safeNum(ind.atr_14);
  const emaDist = safeNum(ind.price_vs_ema50_pct);
  const volRatio = safeNum(ind.volume_ratio);

  const hist = indicatorHistory || {};

  const rows = [];
  rows.push(
    marketIndicatorRow(
      "RSI",
      rsi != null ? rsi.toFixed(1) : "—",
      sparklineSvg(hist.rsi, { stroke: "rgba(52,211,153,0.9)" }),
      rsiColor(rsi)
    )
  );
  rows.push(
    marketIndicatorRow(
      "MACD",
      macd != null ? macd.toFixed(2) : "—",
      sparklineSvg(hist.macd, { stroke: "rgba(148,163,184,0.9)" }),
      macdColor(macd)
    )
  );
  rows.push(
    marketIndicatorRow(
      "ADX",
      adx != null ? adx.toFixed(1) : "—",
      sparklineSvg(hist.adx, { stroke: "rgba(251,146,60,0.9)" }),
      null
    )
  );
  rows.push(
    marketIndicatorRow(
      "ATR",
      atr != null ? `${atr.toFixed(1)}p` : "—",
      sparklineSvg(hist.atr, { stroke: "rgba(99,102,241,0.9)" }),
      null
    )
  );
  rows.push(
    marketIndicatorRow(
      "EMA DIST",
      emaDist != null ? `${emaDist.toFixed(2)}%` : "—",
      sparklineSvg(hist.ema_distance, { stroke: "rgba(226,232,240,0.85)" }),
      null
    )
  );
  rows.push(
    marketIndicatorRow(
      "VOL RATIO",
      volRatio != null ? volRatio.toFixed(2) : "—",
      sparklineSvg(hist.volume_ratio, { stroke: "rgba(148,163,184,0.85)" }),
      null
    )
  );

  grid.innerHTML = rows.join("");

  // FLO-221: Multi-TF compact grid
  const mtfEl = el("mtf-grid-dashboard");
  if (mtfEl) {
    const mtf = state?.multi_tf_indicators || {};
    const tfs = ["M15", "H1", "H4", "D1"];
    const has = tfs.some((tf) => mtf[tf]);
    if (!has) {
      mtfEl.innerHTML = "";
    } else {
      let h = `<div style="display:grid;grid-template-columns:60px repeat(4,1fr);gap:0;color:#475569;font-weight:700;font-size:9px;letter-spacing:0.08em;margin-bottom:6px"><span></span>`;
      tfs.forEach((tf) => { h += `<span style="text-align:center">${tf}</span>`; });
      h += `</div>`;
      // FLO-222: direction arrow helper
      function dArr(dir) {
        if (dir === "rising" || dir === "bullish_strengthening") return `<span style="color:#4ade80;font-size:8px">↑</span>`;
        if (dir === "falling" || dir === "bearish_strengthening") return `<span style="color:#f87171;font-size:8px">↓</span>`;
        if (dir === "bullish_weakening") return `<span style="color:#facc15;font-size:8px">↑</span>`;
        if (dir === "bearish_weakening") return `<span style="color:#facc15;font-size:8px">↓</span>`;
        return "";
      }
      // RSI
      h += `<div style="display:grid;grid-template-columns:60px repeat(4,1fr);gap:0;padding:3px 0"><span style="color:#546478;font-weight:600">RSI</span>`;
      tfs.forEach((tf) => {
        const td = mtf[tf] || {};
        const v = td.rsi;
        const c = v == null ? "#475569" : v > 70 ? "#f87171" : v < 30 ? "#4ade80" : "#e2e8f0";
        h += `<span style="text-align:center;color:${c};font-weight:700">${v != null ? v.toFixed(0) : "—"}${dArr(td.rsi_direction)}</span>`;
      });
      h += `</div>`;
      // MACD
      h += `<div style="display:grid;grid-template-columns:60px repeat(4,1fr);gap:0;padding:3px 0"><span style="color:#546478;font-weight:600">MACD</span>`;
      tfs.forEach((tf) => {
        const td = mtf[tf] || {};
        const d = td.macd;
        if (!d || d.histogram == null) { h += `<span style="text-align:center;color:#475569">—</span>`; return; }
        const c = d.histogram >= 0 ? "#4ade80" : "#f87171";
        h += `<span style="text-align:center;color:${c};font-weight:700">${d.histogram.toFixed(1)}${dArr(td.macd_direction)}</span>`;
      });
      h += `</div>`;
      // ADX
      h += `<div style="display:grid;grid-template-columns:60px repeat(4,1fr);gap:0;padding:3px 0"><span style="color:#546478;font-weight:600">ADX</span>`;
      tfs.forEach((tf) => {
        const td = mtf[tf] || {};
        const d = td.adx;
        if (!d || d.value == null) { h += `<span style="text-align:center;color:#475569">—</span>`; return; }
        const v = d.value;
        const c = v >= 30 ? "#e2e8f0" : v >= 20 ? "#94a3b8" : "#475569";
        h += `<span style="text-align:center;color:${c};font-weight:700">${v.toFixed(0)}${dArr(td.adx_direction)}</span>`;
      });
      h += `</div>`;
      // EMA alignment + crossover (FLO-224)
      h += `<div style="display:grid;grid-template-columns:60px repeat(4,1fr);gap:0;padding:3px 0"><span style="color:#546478;font-weight:600">EMA</span>`;
      tfs.forEach((tf) => {
        const d = mtf[tf];
        if (!d || (!d.ema_alignment && !d.price_vs_ema50)) { h += `<span style="text-align:center;color:#475569">—</span>`; return; }
        const align = d.ema_alignment;
        const clr = align === "full_bullish" ? "#4ade80" : align === "full_bearish" ? "#f87171" : "#94a3b8";
        const label = align === "full_bullish" ? "BULL" : align === "full_bearish" ? "BEAR" : "MIX";
        let cross = "";
        if (d.ema9_cross_ema21 === "golden_cross") cross = `<span style="color:#4ade80;font-size:7px">✨</span>`;
        else if (d.ema9_cross_ema21 === "death_cross") cross = `<span style="color:#f87171;font-size:7px">☠</span>`;
        if (d.ema50_cross_ema200 === "golden_cross") cross = `<span style="color:#facc15;font-size:7px">⭐</span>`;
        else if (d.ema50_cross_ema200 === "death_cross") cross = `<span style="color:#f87171;font-size:7px">💀</span>`;
        h += `<span style="text-align:center;color:${clr};font-weight:700;font-size:8px">${label}${cross}</span>`;
      });
      h += `</div>`;
      mtfEl.innerHTML = h;
    }
  }

  // FLO-223: 3-Layer Pivot Points
  const ppEl = el("pivot-grid-dashboard");
  if (ppEl) {
    const ppData = state?.pivot_points || {};
    const layers = [
      { key: "daily", label: "DAILY", color: "#22d3ee" },
      { key: "weekly", label: "WEEKLY", color: "#a78bfa" },
      { key: "monthly", label: "MONTHLY", color: "#facc15" },
    ];
    const lvlOrder = ["R3", "R2", "R1", "PP", "S1", "S2", "S3"];
    const lvlColors = { R3: "#f87171", R2: "#f87171", R1: "#fb923c", PP: "#e2e8f0", S1: "#34d399", S2: "#34d399", S3: "#4ade80" };
    let ph = "";
    let hasAny = false;
    for (const ly of layers) {
      const cl = ppData[ly.key]?.classic;
      if (!cl || !cl.PP) continue;
      hasAny = true;
      ph += `<div style="margin-bottom:8px">`;
      ph += `<div style="font-weight:800;color:${ly.color};font-size:9px;letter-spacing:0.1em;margin-bottom:4px">${ly.label} PIVOTS</div>`;
      ph += `<div style="display:flex;flex-wrap:wrap;gap:2px 10px;font-size:10px">`;
      for (const lk of lvlOrder) {
        const lv = cl[lk];
        ph += `<span><span style="color:#546478;font-weight:600">${lk}</span> <span style="color:${lvlColors[lk]};font-weight:700">${lv != null ? Number(lv).toFixed(1) : "—"}</span></span>`;
      }
      ph += `</div></div>`;
    }
    ppEl.innerHTML = hasAny ? `<div style="font-weight:700;color:#475569;font-size:9px;letter-spacing:0.08em;margin-bottom:6px">PIVOT POINTS</div>${ph}` : "";
  }
}

function toggleProactiveReasoning() {
  const p = el("proactive-reasoning");
  const btn = el("proactive-reasoning-toggle");
  if (!p || !btn) return;

  proactiveReasoningExpanded = !proactiveReasoningExpanded;
  if (proactiveReasoningExpanded) {
    p.classList.remove("line-clamp-4");
    btn.textContent = "Collapse";
  } else {
    p.classList.add("line-clamp-4");
    btn.textContent = "Expand";
  }
}

function fastChipStyleFromFastDecision(fd) {
  const action = (fd?.action || "").toString().toUpperCase();
  const execType = (fd?.execution?.type || "").toString().toUpperCase();
  const direction = (fd?.execution?.direction || "").toString().toUpperCase();

  if (action === "ACT") {
    if (execType === "OPEN") {
      if (direction === "BUY") return { color: "#4caf50", border: "rgba(76,175,80,0.35)" };
      if (direction === "SELL") return { color: "#e74c3c", border: "rgba(231,76,60,0.35)" };
    }
    if (execType === "CLOSE") return { color: "#e67e22", border: "rgba(230,126,34,0.35)" };
    if (execType === "ADJUST") return { color: "#f1c40f", border: "rgba(241,196,15,0.35)" };
    return { color: "#f1c40f", border: "rgba(241,196,15,0.35)" };
  }

  if (action === "HOLD") return { color: "#8e8e8e", border: "rgba(142,142,142,0.35)" };
  if (action === "DISMISS") return { color: "#8e8e8e", border: "rgba(142,142,142,0.35)" };
  return { color: "#8e8e8e", border: "rgba(142,142,142,0.35)" };
}

function fmtAgeShort(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s < 0) return "—";
  if (s < 60) return `${Math.round(s)}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h ago`;
}

function renderFastTriggers(fastDecisions) {
  const container = el("fast-triggers-chips");
  if (!container) return;

  if (!Array.isArray(fastDecisions) || fastDecisions.length === 0) {
    container.innerHTML = `<span class="text-gray-600 font-medium">NO TRIGGERS YET</span>`;
    return;
  }

  const now = Date.now();
  const items = fastDecisions.slice(0, 3);
  container.innerHTML = items.map((fd) => {
    const action = (fd?.action || "").toString().toUpperCase() || "HOLD";
    const reason = (fd?.reason || "").toString().trim();
    const exec = fd?.execution || {};
    const execType = (exec?.type || "").toString().toUpperCase();
    const dir = (exec?.direction || "").toString().toUpperCase();
    const entry = exec?.entry ?? exec?.entry_price;

    let suffix = "";
    if (execType === "OPEN") {
      if (entry != null && Number.isFinite(Number(entry))) suffix = `Entry @ ${fmtNum(entry, 1)}`;
      else suffix = dir ? `${dir} executed` : "OPEN executed";
    } else if (execType === "CLOSE") {
      suffix = "CLOSE executed";
    } else if (execType === "ADJUST") {
      suffix = "ADJUST executed";
    }

    let ageText = "—";
    try {
      const ts = fd?.timestamp ? Date.parse(fd.timestamp) : NaN;
      const ageS = Number.isFinite(ts) ? (now - ts) / 1000 : NaN;
      ageText = fmtAgeShort(ageS);
    } catch (e) {
      ageText = "—";
    }

    const style = fastChipStyleFromFastDecision(fd);
    const bodyParts = [];
    if (reason) bodyParts.push(reason);
    if (suffix) bodyParts.push(suffix);

    return `
      <div class="trigger-log-entry">
        <span class="trigger-log-action" style="color:${style.color};">${action}</span>
        <span class="trigger-log-body">${bodyParts.join(' — ') || '—'}</span>
        <span class="trigger-log-age">${ageText}</span>
      </div>
    `;
  }).join("");
}

function el(id) {
  return document.getElementById(id);
}

function setStaleUI(isStale, ageSeconds) {
  const app = el("app");
  const overlay = el("stale-overlay");

  if (!app || !overlay) return;

  if (isStale) {
    app.classList.add("is-stale");
    overlay.className = "block";
    overlay.innerHTML = `<div class="label">OFFLINE / STALE — LAST DATA: ${fmtDuration(ageSeconds)}</div>`;
  } else {
    app.classList.remove("is-stale");
    overlay.className = "hidden";
    overlay.innerHTML = "";
  }
}

function fmtMoney(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return `$${Number(v).toFixed(2)}`;
}

function fmtPct(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}%`;
}

function fmtNum(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return Number(v).toFixed(digits);
}

function fmtDuration(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s < 0) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  const rem = Math.round(s % 60);
  return `${m}m ${rem}s`;
}

function badgeClassByDecision(decision) {
  const d = (decision || "").toUpperCase();
  if (d.includes("BUY")) return { border: "border-green-500", bg: "bg-green-900/20", text: "text-green-400", glow: "glow-green animate-pulse" };
  if (d.includes("SELL")) return { border: "border-red-500", bg: "bg-red-900/20", text: "text-red-400", glow: "glow-red animate-pulse" };
  return { border: "border-yellow-500", bg: "bg-yellow-900/20", text: "text-yellow-400", glow: "glow-yellow" };
}

function pillColor(score) {
  const s = Number(score);
  if (Number.isNaN(s)) return "bg-gray-700";
  if (s < 40) return "bg-gradient-to-r from-red-600 to-red-400 shadow-[0_0_12px_rgba(248,113,113,0.6)]";
  if (s > 60) return "bg-gradient-to-r from-green-600 to-green-400 shadow-[0_0_12px_rgba(74,222,128,0.6)]";
  return "bg-gradient-to-r from-yellow-600 to-yellow-400 shadow-[0_0_12px_rgba(250,204,21,0.6)]";
}

function renderPillar(rowId, score) {
  const bar = el(`${rowId}-bar`);
  const val = el(`${rowId}-val`);
  const s = Number(score);
  const pct = Number.isNaN(s) ? 0 : Math.max(0, Math.min(100, s));
  bar.style.width = `${pct}%`;
  bar.className = `h-2 rounded-full transition-all duration-1000 ease-out ${pillColor(s)}`;
  val.textContent = fmtNum(s, 1);
}

function setStatusDot(isOperational) {
  const dot = el("status-dot");
  const label = el("status-label");

  if (isOperational) {
    dot.className = "status-dot-live";
    label.textContent = "OPERATIONAL";
    label.className = "text-green-400";
  } else {
    dot.className = "status-dot-offline";
    label.textContent = "OFFLINE";
    label.className = "text-red-400";
  }
}

const _lastValues = {};
function flashValue(id, newText) {
  const e = el(id);
  if (!e) return;
  if (_lastValues[id] !== newText) {
    _lastValues[id] = newText;
    e.classList.remove("value-flash");
    void e.offsetWidth;
    e.classList.add("value-flash");
    const cleanup = () => e.classList.remove("value-flash");
    e.addEventListener("animationend", cleanup, { once: true });
  }
}

function renderPositions(positions) {
  const container = el("positions");
  container.innerHTML = "";

  if (!positions || positions.length === 0) {
    container.innerHTML = `<div class="text-xs text-gray-400">NO OPEN POSITIONS</div>`;
    el("positions-count").textContent = "0";
    return;
  }

  el("positions-count").textContent = String(positions.length);

  for (const p of positions) {
    const pnl = Number(p.profit);
    const pnlClass = pnl >= 0 ? "text-green-400" : "text-red-400";
    
    // Phase badge styling
    const phase = p.phase || "OPEN";
    let phaseBadge = "";
    if (phase === "TRAILING") {
      phaseBadge = `<span class="ml-2 px-1.5 py-0.5 rounded text-[10px] font-bold border phase-badge-trailing">TRAILING</span>`;
    } else if (phase === "SL_ADJUSTED") {
      phaseBadge = `<span class="ml-2 px-1.5 py-0.5 rounded text-[10px] font-bold border phase-badge-sl-adjusted">SL ADJ</span>`;
    } else {
      phaseBadge = `<span class="ml-2 px-1.5 py-0.5 rounded text-[10px] font-bold border phase-badge-open">OPEN</span>`;
    }

    // SL status indicator
    let beIndicator = "";
    if (phase === "SL_ADJUSTED" || phase === "TRAILING") {
      beIndicator = `<span class="text-[10px] text-green-500 font-mono font-bold">SL ✓</span>`;
    }

    const dirBorderClass = (p.direction || "").toUpperCase() === "BUY" ? "border-l-4 border-l-green-500" : (p.direction || "").toUpperCase() === "SELL" ? "border-l-4 border-l-red-500" : "";
    container.insertAdjacentHTML(
      "beforeend",
      `
      <div class="bg-gray-800/40 backdrop-blur-sm border border-gray-700/50 rounded-xl p-3 shadow-md hover:bg-gray-800/60 transition-colors duration-200 ${dirBorderClass}">
        <div class="flex items-center justify-between mb-2">
          <div class="text-xs font-mono font-medium text-gray-200">
            <span class="text-gray-500 mr-1">#</span>${p.ticket} 
            <span class="ml-2 px-1.5 py-0.5 rounded text-[10px] font-bold border border-gray-600/50 text-gray-300 bg-gray-800/50">${p.direction}</span> 
            ${phaseBadge}
            <span class="ml-2 text-gray-400">${p.volume} lot</span>
          </div>
          <div class="text-xs font-mono font-bold ${pnlClass}">${fmtMoney(pnl)} <span class="text-[10px] text-gray-500 font-medium ml-1">(${fmtNum(p.profit_pips, 0)} p)</span></div>
        </div>
        <div class="grid grid-cols-2 md:grid-cols-5 gap-3 text-[10px] font-mono font-medium text-gray-500 uppercase tracking-widest bg-black/20 rounded-lg p-2 border border-white/5">
          <div><span class="block mb-0.5">Entry</span><span class="text-gray-200">${fmtNum(p.open_price, 2)}</span></div>
          <div><span class="block mb-0.5">SL</span><span class="text-gray-200">${fmtNum(p.sl, 2)}</span></div>
          <div><span class="block mb-0.5">TP</span><span class="text-gray-200">${fmtNum(p.tp, 2)}</span></div>
          <div><span class="block mb-0.5">Now</span><span class="text-gray-200">${fmtNum(p.current_price, 2)}</span></div>
          <div><span class="block mb-0.5">Protection</span>${beIndicator}</div>
        </div>
      </div>
      `
    );
  }
}

function renderTrades(trades, daily) {
  const container = el("trades");
  container.innerHTML = "";

  const beProfitThreshold = 0.50;

  const wins = Number(daily?.wins || 0);
  const losses = Number(daily?.losses || 0);
  const breakevens = Number(daily?.breakevens || 0);
  const decisive = wins + losses;
  const wr = decisive ? (wins / decisive) * 100 : 0;

  el("trades-w").textContent = String(wins);
  el("trades-l").textContent = String(losses);
  el("trades-be").textContent = String(breakevens);
  el("trades-wr").textContent = `${wr.toFixed(1)}%`;
  const hasDailyPnl = daily && daily.pnl !== null && daily.pnl !== undefined && !Number.isNaN(Number(daily.pnl));
  el("trades-pnl").textContent = hasDailyPnl ? fmtMoney(daily?.pnl) : "—";

  if (!trades || trades.length === 0) {
    container.innerHTML = `<div class="text-xs text-gray-400">NO CLOSED TRADES TODAY</div>`;
    return;
  }

  const sorted = [...trades].sort((a, b) => (b.close_time || "").localeCompare(a.close_time || ""));

  for (const t of sorted.slice(0, 30)) {
    const isPending = t.pending === true || t.profit === null || t.profit === undefined;
    const pnl = isPending ? 0 : Number(t.profit);
    const time = (t.close_time || "").split("T")[1]?.slice(0, 5) || "—";

    // Use close_type if available (from monitor), otherwise heuristic fallback for old data
    let displayReason;
    if (t.close_type) {
      if (t.close_type === "sl") {
        if (pnl >= beProfitThreshold) displayReason = "Trailing Stop";
        else if (Math.abs(pnl) < beProfitThreshold) displayReason = "Breakeven";
        else displayReason = "Stop Loss";
      } else {
        const typeMap = { tp: "Take Profit", trailing: "Trailing Stop", breakeven: "Breakeven", sl: "Stop Loss" };
        displayReason = typeMap[t.close_type] || t.reason || "";
      }
    } else {
      displayReason = t.reason || "";
      if (displayReason.toLowerCase().includes("stop loss")) {
        if (pnl >= beProfitThreshold) displayReason = "Trailing Stop";
        else if (Math.abs(pnl) < beProfitThreshold) displayReason = "Breakeven";
        else displayReason = "Stop Loss";
      }
    }

    let pnlDisplay, pnlClass, icon;
    if (isPending) {
      const outcome = t.outcome || "?";
      pnlDisplay = `<span class="text-amber-400 animate-pulse">Processing...</span>`;
      pnlClass = "text-amber-400";
      icon = outcome === "WIN" ? "WIN" : (outcome === "LOSS" ? "LOSS" : "...");
    } else {
      pnlClass = pnl >= beProfitThreshold ? "text-green-400" : (pnl <= -beProfitThreshold ? "text-red-400" : "text-gray-400");
      const estBadge = t.estimated ? ` <span class="text-amber-400 opacity-75">(est.)</span>` : "";
      pnlDisplay = `${fmtMoney(pnl)}${estBadge}`;
      icon = pnl >= beProfitThreshold ? "WIN" : (pnl <= -beProfitThreshold ? "LOSS" : "BE");
    }

    container.insertAdjacentHTML(
      "beforeend",
      `
      <div class="flex items-center justify-between text-[11px] font-mono border-b border-gray-800/60 py-2.5 px-2 hover:bg-gray-800/30 transition-colors duration-200 rounded-lg group">
        <div class="text-gray-500 font-medium">${time}</div>
        <div class="text-gray-300 font-bold">${t.direction || "—"}</div>
        <div class="text-gray-400 truncate max-w-[14rem] sm:max-w-[10rem] md:max-w-[14rem] font-medium tracking-wide">${displayReason}</div>
        <div class="${pnlClass} font-bold">${pnlDisplay}</div>
        <div class="text-[10px] uppercase tracking-widest font-bold px-1.5 py-0.5 rounded bg-gray-800/50 border border-gray-700/50 text-gray-400">${icon}</div>
      </div>
      `
    );
  }
}

function renderVolBanner(volStatus, volDesc) {
  const banner = el("vol-banner");
  const status = (volStatus || "NORMAL").toUpperCase();

  if (status === "EXTREME") {
    banner.className = "block bg-red-900/60 border-b border-red-500 text-red-200 px-4 py-2 text-xs font-mono animate-pulse";
    banner.textContent = `BREAKING: EXTREME VOLATILITY — TRADING BLOCKED. ${volDesc || ""}`;
  } else if (status === "COOLING_DOWN") {
    banner.className = "block bg-yellow-900/40 border-b border-yellow-500 text-yellow-200 px-4 py-2 text-xs font-mono";
    banner.textContent = `ALERT: COOLING DOWN — ONLY STRONG SIGNALS. ${volDesc || ""}`;
  } else {
    banner.className = "hidden";
    banner.textContent = "";
  }
}

function render(state) {
  const ts = state.timestamp || "—";
  el("last-update").textContent = ts.replace("T", " ").slice(0, 19);

  const metaAge = state._meta?.file_age_seconds;
  const operational = (state.bot?.status || "OFFLINE") === "OPERATIONAL";
  setStatusDot(operational);

  el("last-data-age").textContent = fmtDuration(metaAge);

  setStaleUI(!operational, metaAge);

  el("mode").textContent = state.bot?.mode || "—";

  const marketOpen = state.market?.is_open;
  const marketLabel = el("market");
  if (marketOpen === true) {
    marketLabel.textContent = "OPEN";
    marketLabel.className = "text-green-400";
  } else if (marketOpen === false) {
    marketLabel.textContent = "CLOSED";
    marketLabel.className = "text-gray-300";
  } else {
    marketLabel.textContent = "—";
    marketLabel.className = "text-gray-400";
  }

  // EA Bridge status
  const eaStatus = el("ea-bridge-status");
  const eaSpread = el("ea-spread");
  const eaBridge = state.ea_bridge || {};
  
  if (eaBridge.enabled === true) {
    if (eaBridge.online === true) {
      eaStatus.textContent = "ONLINE";
      eaStatus.className = "text-green-400 font-medium";
    } else {
      eaStatus.textContent = "FALLBACK";
      eaStatus.className = "text-amber-400 font-medium";
    }
  } else {
    eaStatus.textContent = "OFF";
    eaStatus.className = "text-gray-500";
  }
  
  if (eaBridge.spread_pips != null) {
    const spread = Number(eaBridge.spread_pips);
    eaSpread.textContent = `${spread.toFixed(1)}p`;
    eaSpread.className = spread > 5 ? "font-mono text-amber-400" : "font-mono text-gray-300";
  } else {
    eaSpread.textContent = "—";
    eaSpread.className = "font-mono text-gray-500";
  }

  const la = state.last_analysis || {};
  const hasRealAnalysis = la.agent_decision != null && la.final_score != null;
  const marketClosed = state.market?.is_open === false;

  const card = el("goldcon");

  if (marketClosed) {
    card.className = "relative bg-gray-800/40 border-gray-600 border-2 rounded-2xl p-6 scanlines transition-all duration-500 shadow-lg";
    const reason = state.market?.reason || "";
    const isPausa = reason.toLowerCase().includes("pausa") || reason.toLowerCase().includes("daily pause");
    el("goldcon-decision").textContent = isPausa ? "DAILY PAUSE" : "MARKET CLOSED";
    el("goldcon-decision").className = "text-3xl sm:text-4xl md:text-5xl font-bold leading-none text-gray-400 text-shadow-soft";
    el("goldcon-score").textContent = "—";
    el("goldcon-conf").textContent = "—";
    const nextOpen = state.market?.next_open;
    if (nextOpen) {
      const dt = nextOpen.replace("T", " ").slice(0, 16);
      el("goldcon-scenario").textContent = `Reopens: ${dt} UTC`;
    } else {
      el("goldcon-scenario").textContent = state.market?.reason || "—";
    }
    
    // Reset Segmented Bar
    const segmentsContainer = el("signal-segments");
    if (segmentsContainer) {
      const segments = segmentsContainer.children;
      for (let i = 0; i < segments.length; i++) {
        const seg = segments[i];
        if (seg) {
          seg.className = "flex-1 rounded transition-all duration-700 bg-gray-800";
          seg.style.height = "48px";
        }
      }
    }
  } else {
    const decision = la.agent_decision || "HOLD";
    const score = la.final_score;
    const conf = la.agent_confidence;
    const cls = badgeClassByDecision(decision);
    
    // Apply new mockup styling classes to the card
    card.className = `relative ${cls.bg} ${cls.border} backdrop-blur-md border-2 rounded-2xl p-6 shadow-xl overflow-hidden transition-all duration-500 group`;
    
    el("goldcon-decision").textContent = decision;
    el("goldcon-decision").className = `text-4xl sm:text-5xl md:text-6xl font-bold leading-none ${cls.text} text-shadow-soft transition-colors duration-300`;
    el("goldcon-score").textContent = fmtNum(score, 1);
    el("goldcon-conf").textContent = conf != null ? `${fmtNum(conf, 1)}%` : "—";
    const scenarioDisplay = {
      "momentum_forte_confirmado": "Strong confirmed momentum",
      "rsi_extremo_com_momentum": "Extreme RSI with momentum",
      "divergencia_tecnica": "Technical divergence",
      "breakout_confirmado": "Confirmed breakout",
      "lateralizacao": "Sideways / ranging",
      "sinais_conflitantes": "Conflicting signals",
      "ml_vs_tech_conflito": "Tech vs ML conflict (BUY threshold 58)",
      "alinhamento_perfeito": "Perfect alignment",
      "padrao": "Default scenario",
      "post_event_momentum": "Post-event momentum",
    };
    el("goldcon-scenario").textContent = la.scenario_description || scenarioDisplay[la.scenario] || la.scenario || "—";

    const blockedEl = el("goldcon-blocked");
    if (la.hold_forced && la.original_decision) {
      blockedEl.textContent = `${la.original_decision} blocked — ${la.hold_reason || "insufficient confidence"}`;
      blockedEl.className = "text-xs text-amber-400 mt-2 font-medium block";
    } else {
      blockedEl.textContent = "";
      blockedEl.className = "hidden text-xs text-amber-400 mt-2 font-medium";
    }
    
    // Update Segmented Bar
    const segmentsContainer = el("signal-segments");
    if (segmentsContainer && score != null) {
      const clampedScore = Math.max(0, Math.min(100, score));
      const numSegments = 12;
      const activeCount = Math.ceil((clampedScore / 100) * numSegments);
      
      const segments = segmentsContainer.children;
      for (let i = 0; i < numSegments; i++) {
        const seg = segments[i];
        if (!seg) continue;
        
        // Reset classes
        seg.className = "flex-1 rounded transition-all duration-700 bg-gray-800";
        seg.style.height = "48px";
        
        // Determine color zone based on segment index
        // 0-35 SELL (roughly segments 0-3) -> RED
        // 35-65 HOLD (roughly segments 4-7) -> YELLOW
        // 65-100 BUY (roughly segments 8-11) -> GREEN
        let colorClass = "";
        if (i < 4) colorClass = "segment-red";
        else if (i < 8) colorClass = "segment-yellow";
        else colorClass = "segment-green";
        
        if (i < activeCount) {
          seg.classList.add("active");
          seg.classList.add(colorClass);
          seg.classList.remove("bg-gray-800");
          
          // Add pulse to the very last active segment
          if (i === activeCount - 1) {
            seg.classList.add("pulse");
          }
        }
      }
    }
  }

  renderVolBanner(la.volatility_status, la.volatility_description);

  const bal = state.account?.balance;
  const eq = state.account?.equity;
  const balText = fmtMoney(bal);
  const eqText = fmtMoney(eq);
  el("balance").textContent = balText;
  el("equity").textContent = eqText;
  flashValue("balance", balText);
  flashValue("equity", eqText);

  const hasPnl = state.daily_stats && state.daily_stats.pnl !== null && state.daily_stats.pnl !== undefined && !Number.isNaN(Number(state.daily_stats.pnl));
  const hasPnlPct = state.daily_stats && state.daily_stats.pnl_percent !== null && state.daily_stats.pnl_percent !== undefined && !Number.isNaN(Number(state.daily_stats.pnl_percent));

  const noClosedTrades = (Number(state.daily_stats?.wins || 0) + Number(state.daily_stats?.losses || 0) + Number(state.daily_stats?.breakevens || 0)) === 0;
  const pnlIsZero = Number(state.daily_stats?.pnl || 0) === 0;

  if (!operational || bal === null || bal === undefined || eq === null || eq === undefined || !hasPnl || !hasPnlPct || (noClosedTrades && pnlIsZero)) {
    el("pnl").textContent = "—";
    el("pnl").className = "text-xl font-bold text-gray-300";
  } else {
    const pnlText = `${fmtMoney(state.daily_stats?.pnl)}  (${fmtPct(state.daily_stats?.pnl_percent)})`;
    el("pnl").textContent = pnlText;
    const pnlVal = Number(state.daily_stats?.pnl || 0);
    el("pnl").className = pnlVal >= 0 ? "text-xl font-bold text-green-400" : "text-xl font-bold text-red-400";
    flashValue("pnl", pnlText);
  }

  const livePrice = la.current_price;
  const lastKnown = state.last_known_price;
  const priceEl = el("price");
  const priceLabelEl = el("price-label");

  if (livePrice != null) {
    const pt = fmtNum(livePrice, 2);
    priceEl.textContent = pt;
    priceEl.className = "text-2xl font-bold text-white";
    if (priceLabelEl) priceLabelEl.textContent = "";
    flashValue("price", pt);
  } else if (lastKnown != null) {
    const pt = fmtNum(lastKnown, 2);
    priceEl.textContent = pt;
    priceEl.className = "text-2xl font-bold text-gray-400";
    if (priceLabelEl) priceLabelEl.textContent = "LAST";
    flashValue("price", pt);
  } else {
    priceEl.textContent = "—";
    priceEl.className = "text-2xl font-bold text-gray-500";
    if (priceLabelEl) priceLabelEl.textContent = "";
  }

  try {
    renderMarketIndicatorsPanel(state);
  } catch (e) {
    // silent
  }

  renderPositions(state.positions);
  renderTrades(state.trade_history, state.daily_stats);
  renderIntelFeed(la.intel_feed, la.mtf_trend, la.volume_gate, state.market_context);
  renderAgentCard(la.agent_decision, marketClosed);
  renderProactiveAnalysis(la.proactive_analysis, state.positions);
  renderFastTriggers(la.fast_decisions);
  renderAgentMemory(state.agent_memory);

  lastStateForProactiveCountdown = state;
  ensureProactiveCountdownRunning();

  bindUIHandlersOnce();
}

function ensureProactiveCountdownRunning() {
  if (proactiveCountdownIntervalId != null) return;
  proactiveCountdownIntervalId = setInterval(() => {
    try {
      updateProactiveCountdown(lastStateForProactiveCountdown);
    } catch (e) {
      // silent
    }
  }, 1000);
}

function updateProactiveCountdown(state) {
  const countdownEl = el("proactive-countdown");
  if (!countdownEl) return;
  if (!state) return;

  const marketOpen = !!state.market?.is_open;
  if (!marketOpen) {
    countdownEl.textContent = "Pending...";
    return;
  }

  const now = new Date();

  // Compute next M30 close strictly in UTC (next 00 or 30 minute boundary)
  const utcYear = now.getUTCFullYear();
  const utcMonth = now.getUTCMonth();
  const utcDate = now.getUTCDate();
  const utcHour = now.getUTCHours();
  const utcMinute = now.getUTCMinutes();

  const nextMinute = utcMinute < 30 ? 30 : 0;
  const nextHour = utcMinute < 30 ? utcHour : utcHour + 1;

  const nextClose = new Date(Date.UTC(
    utcYear,
    utcMonth,
    utcDate,
    nextHour,
    nextMinute,
    0,
    0,
  ));

  let msLeft = nextClose.getTime() - now.getTime();
  if (!Number.isFinite(msLeft)) {
    countdownEl.textContent = "—";
    return;
  }
  if (msLeft < 0) msLeft = 0;

  const totalSec = Math.floor(msLeft / 1000);
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;

  if (min >= 60) {
    const hr = Math.floor(min / 60);
    const mm = min % 60;
    countdownEl.textContent = `${hr}h ${mm}m`;
  } else if (min >= 1) {
    countdownEl.textContent = `${min} min ${sec} sec`;
  } else {
    countdownEl.textContent = `${sec}s`;
  }
}

/* ================================================================
   INTEL FEED
   ================================================================ */

function intelScoreColor(score) {
  const s = Number(score);
  if (Number.isNaN(s)) return { border: "border-gray-600", bg: "bg-gray-600", text: "text-gray-400", label: "NEUTRAL" };
  if (s >= 65) return { border: "border-green-500", bg: "bg-green-500", text: "text-green-400", label: "BULLISH" };
  if (s >= 55) return { border: "border-green-700", bg: "bg-green-700", text: "text-green-500", label: "LEAN BULL" };
  if (s <= 35) return { border: "border-red-500", bg: "bg-red-500", text: "text-red-400", label: "BEARISH" };
  if (s <= 45) return { border: "border-red-700", bg: "bg-red-700", text: "text-red-500", label: "LEAN BEAR" };
  return { border: "border-gray-600", bg: "bg-gray-600", text: "text-gray-400", label: "NEUTRAL" };
}

function macroImpactText(key, changePct) {
  const c = Number(changePct);
  if (!Number.isFinite(c) || Math.abs(c) < 0.05) return "Neutral for gold";
  if (key === "dxy") return c < 0 ? "Bullish for gold" : "Bearish for gold";
  if (key === "yields") return c < 0 ? "Bullish for gold" : "Bearish for gold";
  if (key === "vix") return c > 0 ? "Bullish for gold" : "Bearish for gold";
  if (key === "oil") return c > 3 ? "Geopolitical risk" : (c < -3 ? "Deflationary signal" : "Neutral");
  if (key === "sp500") return c < 0 ? "Risk off — gold demand" : "Risk on";
  if (key === "gld") return c > 0 ? "Gold demand confirmed" : "Gold selling pressure";
  if (key === "real_yields") return c < 0 ? "Bullish for gold" : "Bearish for gold";
  if (key === "usdcny") return c > 0 ? "Yuan weak — gold demand" : "Yuan strong — less gold demand";
  return "";
}

function macroLabel(key) {
  if (key === "dxy") return "DXY";
  if (key === "yields") return "YIELDS 10Y";
  if (key === "vix") return "VIX";
  if (key === "oil") return "OIL WTI";
  if (key === "sp500") return "S&P 500";
  if (key === "gld") return "GLD VOL";
  if (key === "real_yields") return "REAL YIELDS \u2248";
  if (key === "usdcny") return "USD/CNY";
  return key.toUpperCase();
}

function macroUnit(key) {
  if (key === "yields") return "%";
  if (key === "real_yields") return "%";
  return "";
}

function macroPrefix(key) {
  if (key === "oil") return "$";
  return "";
}

function macroFmtVal(key, val) {
  if (key === "gld") {
    // Format volume as compact number (e.g. 26.1M)
    const n = Number(val);
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(0) + "K";
    return n.toLocaleString();
  }
  return fmtNum(val, 2);
}

function categoryBadge(cat) {
  const map = {
    gold: { label: "GOLD", cls: "bg-yellow-600/30 text-yellow-400" },
    us_monetary: { label: "FED", cls: "bg-blue-600/30 text-blue-400" },
    geopolitics: { label: "GEO", cls: "bg-red-600/30 text-red-400" },
    financial_crisis: { label: "CRISIS", cls: "bg-red-700/30 text-red-300" },
    global_monetary: { label: "CB", cls: "bg-purple-600/30 text-purple-400" },
    inflation_commodities: { label: "INFL", cls: "bg-orange-600/30 text-orange-400" },
    safe_haven: { label: "HAVEN", cls: "bg-yellow-700/30 text-yellow-300" },
    recession: { label: "RECESS", cls: "bg-gray-600/30 text-gray-300" },
    market_risk: { label: "RISK", cls: "bg-pink-600/30 text-pink-400" },
    sanctions: { label: "SANCT", cls: "bg-amber-600/30 text-amber-400" },
    crisis_events: { label: "BLACK SWAN", cls: "bg-red-800/30 text-red-200" },
    echo: { label: "ECHO", cls: "bg-cyan-600/30 text-cyan-400" },
  };
  const m = map[cat] || { label: cat || "?", cls: "bg-gray-600/30 text-gray-400" };
  return `<span class="px-1 py-0.5 rounded text-[9px] font-bold uppercase ${m.cls}">${m.label}</span>`;
}

function echoBadge(classification) {
  if (!classification) return "";
  if (classification === "CRITICAL") return `<span class="px-1.5 py-0.5 rounded text-[9px] font-black uppercase bg-red-500/30 text-red-400 border border-red-500/40 animate-pulse">CRITICAL</span>`;
  if (classification === "IMPORTANT") return `<span class="px-1.5 py-0.5 rounded text-[9px] font-black uppercase bg-amber-500/25 text-amber-400 border border-amber-500/30">IMPORTANT</span>`;
  return "";
}

function renderIntelFeed(feed, mtfTrend, volumeGate, marketContext) {
  const section = el("intel-feed-section");
  const contentEls = section.querySelectorAll(".intel-feed-content");
  const empty = section.querySelector(".intel-feed-empty");
  if (!feed) {
    contentEls.forEach(c => c.classList.add("hidden"));
    if (empty) empty.classList.remove("hidden");
    return;
  }
  contentEls.forEach(c => c.classList.remove("hidden"));
  if (empty) empty.classList.add("hidden");

  // Method badge + cache age
  const method = (feed.analysis_method || "").toUpperCase();
  el("intel-method").textContent = method === "GPT" ? "GPT" : "KEYWORDS";
  const cacheAge = feed.cache_age_minutes;
  el("intel-cache-age").textContent = cacheAge != null ? `News cache: ${Math.round(cacheAge)}m ago` : "";

  // Headlines
  const hlContainer = el("intel-headlines");
  hlContainer.innerHTML = "";
  const headlines = feed.headlines || [];
  if (headlines.length === 0) {
    hlContainer.innerHTML = `<div class="text-xs text-gray-500">No headlines available</div>`;
  } else {
    for (const h of headlines) {
      const sc = intelScoreColor(h.score);
      const age = h.age_hours != null ? `${Number(h.age_hours).toFixed(1)}h` : "?";
      const titleTrunc = h.title.length > 90 ? h.title.slice(0, 90) + "..." : h.title;
      const methodTag = h.method === "gpt" ? "GPT" : "KW";
      hlContainer.insertAdjacentHTML("beforeend", `
        <div class="intel-headline flex items-stretch gap-3 group p-2 rounded-lg hover:bg-white/5 transition-colors duration-200 cursor-default border border-transparent hover:border-white/10">
          <div class="w-1.5 rounded-full flex-shrink-0 ${sc.bg} opacity-80 shadow-[0_0_8px_currentColor]"></div>
          <div class="flex-1 min-w-0 py-0.5">
            <div class="text-xs text-gray-200 leading-snug truncate font-medium" title="${h.title.replace(/"/g, '&quot;')}">${titleTrunc}</div>
            <div class="flex items-center gap-2 mt-1.5 text-[10px] text-gray-500 font-mono">
              ${echoBadge(h.echo_classification)}${categoryBadge(h.category)}
              <span>${age} ago</span>
            </div>
          </div>
          <div class="flex-shrink-0 flex items-center">
            <div class="intel-robot-bubble">
              <img src="/image/flokiwatch.png" alt="Floki" class="floki-intel-mini shadow-md ring-1 ring-white/10">
              <div class="intel-bubble backdrop-blur-md bg-gray-900/90 border-gray-700/50">
                <div class="text-xs font-bold ${sc.text} font-mono">${fmtNum(h.score, 0)}/100</div>
                <div class="text-[10px] font-semibold tracking-wider uppercase text-gray-400 mt-0.5">${sc.label}</div>
                <div class="text-[9px] text-gray-500 mt-1 uppercase tracking-widest">via ${methodTag}</div>
              </div>
            </div>
          </div>
        </div>
      `);
    }
  }

  // FLO-122: 5-section macro panel (MT5 market_context + Yahoo macro)
  const macroContainer = el("intel-macro");
  macroContainer.innerHTML = "";
  const macro = feed.macro || {};
  const mc = marketContext || {};

  // Helper: render a table row for an MT5 instrument
  function mcRow(sym, data, label) {
    if (!data || data.bid == null) return `<tr id="mc-${sym}"><td class="text-gray-500 text-xs py-0.5" colspan="3">${label || sym} —</td></tr>`;
    const v = data.bid;
    const c = data.change_pct;
    const chgHtml = c != null ? `<span class="${c > 0 ? 'text-green-400' : c < 0 ? 'text-red-400' : 'text-gray-400'}">${c > 0 ? '+' : ''}${c.toFixed(2)}%</span>` : '<span class="text-gray-500">—</span>';
    const posHtml = data.position_in_range != null ? `<span class="text-gray-500">${(data.position_in_range * 100).toFixed(0)}%</span>` : '';
    return `<tr id="mc-${sym}"><td class="text-gray-300 text-xs py-0.5 font-medium">${label || sym}</td><td class="text-gray-100 text-xs font-mono text-right">${typeof v === 'number' ? v.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: v < 10 ? 5 : 2}) : v}</td><td class="text-xs font-mono text-right w-16">${chgHtml}</td><td class="text-[10px] font-mono text-right w-8">${posHtml}</td></tr>`;
  }

  // Helper: render a section card
  function mcSection(id, title, tableHtml, extra) {
    return `<div id="${id}" class="bg-gray-800/40 backdrop-blur-sm border border-gray-700/50 rounded-xl p-3 shadow-md"><div class="text-[10px] text-gray-500 font-semibold tracking-widest uppercase mb-2">${title}</div><table class="w-full">${tableHtml}</table>${extra || ''}</div>`;
  }

  // Section 1: METALS
  const metals = mc.metals || {};
  const gsr = metals.gold_silver_ratio;
  let metalsRows = mcRow("XAGUSD", metals.XAGUSD, "Silver") + mcRow("XPTUSD", metals.XPTUSD, "Platinum") + mcRow("XPDUSD", metals.XPDUSD, "Palladium");
  const gsrHtml = gsr ? `<div class="text-[10px] text-gray-400 mt-1 font-mono">Gold/Silver Ratio: <span class="text-gray-200 font-bold">${gsr}</span></div>` : '';
  macroContainer.insertAdjacentHTML("beforeend", mcSection("macro-metals", "METALS", metalsRows, gsrHtml));

  // Section 2: FOREX
  const forex = mc.forex || {};
  const fxLabels = {EURUSD: "EUR/USD", USDJPY: "USD/JPY", USDCHF: "USD/CHF", AUDUSD: "AUD/USD", USDCNH: "USD/CNH", GBPUSD: "GBP/USD"};
  let forexRows = "";
  for (const sym of ["EURUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCNH", "GBPUSD"]) {
    forexRows += mcRow(sym, forex[sym], fxLabels[sym]);
  }
  const ds = forex.dollar_strength;
  const dsHtml = ds ? `<div class="text-[10px] mt-1 font-mono">Dollar: <span class="font-bold ${ds === 'strong' ? 'text-red-400' : ds === 'weak' ? 'text-green-400' : 'text-gray-400'}">${ds.toUpperCase()}</span></div>` : '';
  macroContainer.insertAdjacentHTML("beforeend", mcSection("macro-forex", "FOREX", forexRows, dsHtml));

  // Section 3: FUTURES
  const futures = mc.futures || {};
  const futLabels = {"DXY_M6": "DXY", "VIX_J6": "VIX", "UST10Y_M6": "10Y Bond"};
  let futRows = "";
  for (const sym of ["DXY_M6", "VIX_J6", "UST10Y_M6"]) {
    futRows += mcRow(sym, futures[sym], futLabels[sym]);
  }
  macroContainer.insertAdjacentHTML("beforeend", mcSection("macro-futures", "FUTURES", futRows));

  // Section 4: 2x2 GRID (key metrics from mixed sources)
  const indices = mc.indices || {};
  const energy = mc.energy || {};
  const crypto = mc.crypto || {};
  const yields10y = macro.yields_10y || macro.yields || {};
  function gridBox(id, label, val, chg, unit) {
    const v = val != null ? (unit || '') + (typeof val === 'number' ? val.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}) : val) : '—';
    const chgHtml = chg != null ? `<span class="${chg > 0 ? 'text-green-400' : chg < 0 ? 'text-red-400' : 'text-gray-400'} text-[10px] font-mono">${chg > 0 ? '+' : ''}${Number(chg).toFixed(2)}%</span>` : '';
    return `<div id="${id}" class="bg-gray-800/60 rounded-lg p-2 text-center"><div class="text-[9px] text-gray-500 font-semibold tracking-widest uppercase">${label}</div><div class="text-sm text-gray-100 font-bold font-mono mt-0.5">${v}</div><div class="mt-0.5">${chgHtml}</div></div>`;
  }
  const sp = indices.US500 || {};
  const btc = crypto.BTCUSD || {};
  const oil = energy.XTIUSD || {};
  const yd = yields10y;
  const gridHtml = `<div class="grid grid-cols-2 gap-2">${gridBox("mc-grid-sp500", "S&P 500", sp.bid, sp.change_pct)}${gridBox("mc-grid-btc", "BTC", btc.bid, btc.change_pct, "$")}${gridBox("mc-grid-oil", "OIL WTI", oil.bid, oil.change_pct, "$")}${gridBox("mc-grid-yields", "10Y YIELD", yd.value, null, "")}</div>`;
  macroContainer.insertAdjacentHTML("beforeend", `<div id="macro-grid" class="bg-gray-800/40 backdrop-blur-sm border border-gray-700/50 rounded-xl p-3 shadow-md"><div class="text-[10px] text-gray-500 font-semibold tracking-widest uppercase mb-2">KEY METRICS</div>${gridHtml}</div>`);

  // Section 5: YAHOO DATA (GLD, Real Yields — no MT5 equivalent)
  const gld = macro.gld || {};
  const ry = macro.real_yields || {};
  const gf = macro.gld_flows || {};
  const ryVal = (ry.proxy != null) ? ry.proxy : ry.value;
  let yahooRows = '';
  if (gld.value != null) {
    const gldFmt = Number(gld.value) >= 1e6 ? (Number(gld.value) / 1e6).toFixed(1) + 'M' : (Number(gld.value) >= 1e3 ? (Number(gld.value) / 1e3).toFixed(0) + 'K' : gld.value);
    yahooRows += `<tr id="mc-gld"><td class="text-gray-300 text-xs py-0.5 font-medium">GLD Vol</td><td class="text-gray-100 text-xs font-mono text-right" colspan="3">${gldFmt}</td></tr>`;
  }
  if (gf.direction) {
    const gfColor = gf.direction === 'ACCUMULATION' ? 'text-green-400' : gf.direction === 'DISTRIBUTION' ? 'text-red-400' : 'text-gray-400';
    yahooRows += `<tr id="mc-gld-flows"><td class="text-gray-300 text-xs py-0.5 font-medium">GLD Flows</td><td class="text-xs font-mono text-right ${gfColor}" colspan="3">${gf.direction} ${gf.volume_change_pct != null ? (gf.volume_change_pct > 0 ? '+' : '') + gf.volume_change_pct.toFixed(0) + '%' : ''}</td></tr>`;
  }
  if (ryVal != null) {
    yahooRows += `<tr id="mc-real-yields"><td class="text-gray-300 text-xs py-0.5 font-medium">Real Yields</td><td class="text-gray-100 text-xs font-mono text-right" colspan="3">${Number(ryVal).toFixed(2)}%</td></tr>`;
  }
  if (yahooRows) {
    macroContainer.insertAdjacentHTML("beforeend", mcSection("macro-yahoo", "YAHOO DATA", yahooRows));
  }

  // Anomalies
  const anomalies = feed.anomalies || [];
  if (anomalies.length > 0) {
    macroContainer.insertAdjacentHTML("beforeend", `
      <div class="text-xs text-amber-400 mt-1">${anomalies.map(a => "&#9888; " + a).join("<br>")}</div>
    `);
  }

  // 5d trend arrows (FLO-74)
  fetch("/api/macro-history").then(r => r.json()).then(mh => {
    if (!mh || typeof mh !== "object") return;
    const dates = Object.keys(mh).sort();
    if (dates.length < 2) return;
    const oldest = mh[dates[0]] || {};
    const newest = mh[dates[dates.length - 1]] || {};
    document.querySelectorAll(".macro-trend-arrow").forEach(el => {
      const k = el.dataset.trendKey;
      if (!k || oldest[k] == null || newest[k] == null || oldest[k] === 0) return;
      const pct = ((newest[k] - oldest[k]) / Math.abs(oldest[k])) * 100;
      let arr = Math.abs(pct) < 0.5 ? "\u2594" : (pct > 0 ? "\u25B2" : "\u25BC");
      if (Math.abs(pct) > 5) arr += arr;
      const bullishDir = {dxy: -1, vix: 1, yields: -1, oil: 1, sp500: -1, gold: 1};
      const isBullish = (bullishDir[k] || 0) * pct > 0;
      const color = Math.abs(pct) < 0.5 ? "#6b7280" : (isBullish ? "#4ade80" : "#f87171");
      el.style.color = color;
      el.textContent = arr;
    });
  }).catch(() => {});

  // FLO-75: Correlation break warning
  fetch("/api/luna-brief").then(r => r.json()).then(data => {
    const brief = data && data.brief;
    if (!brief || !brief.correlations || brief.correlations.status !== "ok") return;
    const cr = brief.correlations;
    const broken = ["gold_dxy", "gold_yields", "gold_sp500"].filter(k => cr[k] && cr[k].status === "BROKEN");
    if (broken.length > 0) {
      macroContainer.insertAdjacentHTML("beforeend", `
        <div class="text-xs text-red-400 mt-1 font-bold">&#9888; CORRELATION BREAK: ${broken.map(k => k.replace("gold_", "Gold-").toUpperCase()).join(", ")}</div>
      `);
    }
  }).catch(() => {});

  // Calendar
  const calEl = el("intel-calendar");
  const calClockEl = el("calendar-market-watch");
  const cal = feed.calendar;
  if (cal && cal.phase) {
    const phaseColors = {
      "normal": "text-gray-400",
      "pre_event": "text-yellow-400",
      "during_event": "text-red-400",
      "post_event": "text-green-400",
    };
    const phaseColor = phaseColors[cal.phase] || "text-gray-400";
    const biasIcon = cal.bias === "BULLISH" ? "&#9650;" : (cal.bias === "BEARISH" ? "&#9660;" : "&#9679;");
    const biasColor = cal.bias === "BULLISH" ? "text-green-400" : (cal.bias === "BEARISH" ? "text-red-400" : "text-gray-500");

    try {
      if (calClockEl) {
        let hhmm = cal.market_watch_time || "";
        if (!hhmm) {
          const u0 = (cal.upcoming_events && cal.upcoming_events.length) ? cal.upcoming_events[0] : null;
          const rt = u0 ? u0.reference_time : "";
          if (typeof rt === "string" && rt.length >= 16) hhmm = rt.slice(11, 16);
        }
        calClockEl.textContent = "Market Watch: " + (hhmm || "—");
      }
    } catch (e) {}

    let calHtml = `
      <div class="flex items-center gap-2">
        <span class="${phaseColor} font-bold">${cal.phase.toUpperCase().replace("_", " ")}</span>
        <span class="${biasColor}">${biasIcon} ${cal.bias}</span>
      </div>
      ${cal.closest_event ? `<div class="text-gray-500 mt-0.5">${cal.closest_event}</div>` : ""}
      ${cal.phase_description ? `<div class="text-gray-600 mt-0.5">${cal.phase_description}</div>` : ""}
    `;

    // Upcoming events list
    const upcoming = cal.upcoming_events || [];
    if (upcoming.length > 0) {
      calHtml += `<div class="mt-2 border-t border-gray-800 pt-1.5">`;
      calHtml += `<div class="text-[10px] text-gray-600 tracking-wider mb-1">UPCOMING</div>`;
      for (const ev of upcoming) {
        const impColor = ev.importance === "HIGH" ? "text-red-400 border-red-800" : "text-yellow-500 border-yellow-800";
        const impLabel = ev.importance === "HIGH" ? "H" : "M";
        const timeColor = ev.is_past ? "text-gray-600" : "text-gray-400";
        const nameColor = ev.is_past ? "text-gray-600" : "text-gray-300";
        calHtml += `
          <div class="flex items-center gap-1.5 text-[11px] py-0.5">
            <span class="px-1 border rounded ${impColor}" style="font-size:9px">${impLabel}</span>
            <span class="${timeColor}">${ev.time}</span>
            <span class="${nameColor} truncate">${ev.name}</span>
            <span class="text-gray-600 ml-auto flex-shrink-0">${ev.time_until}</span>
          </div>
        `;
      }
      calHtml += `</div>`;
    } else {
      calHtml += `<div class="mt-1.5 text-[10px] text-gray-600">No upcoming events</div>`;
    }

    calEl.innerHTML = calHtml;
  } else {
    calEl.innerHTML = `<span class="text-gray-600">No calendar data</span>`;
    try {
      if (calClockEl) calClockEl.textContent = "Market Watch: —";
    } catch (e) {}
  }

  // S/R Zones
  const srEl = el("intel-sr");
  const srContainer = el("intel-sr-zones");
  const srLegend = el("intel-sr-legend");
  const srZones = feed.sr_zones || [];
  const tfBadge = (tf) => {
    const colors = { "D1": "bg-amber-700 text-amber-200", "H4": "bg-blue-800 text-blue-300", "H1": "bg-gray-700 text-gray-400" };
    return `<span class="px-1 py-0.5 rounded text-[10px] font-bold ${colors[tf] || colors.H1}">${tf}</span>`;
  };
  const mtfBadge = (confluence) => {
    if (!confluence || confluence.length < 2) return "";
    return `<span class="px-1 py-0.5 rounded text-[10px] font-bold bg-purple-800 text-purple-300 ml-0.5" title="${confluence.join("+")}">MTF</span>`;
  };
  const ztBadge = (zt) => {
    const ztColors = { "FLIP": "bg-yellow-800 text-yellow-300", "SUPPORT": "bg-green-900 text-green-400", "RESISTANCE": "bg-red-900 text-red-400" };
    const label = zt === "SUPPORT" ? "SUP" : zt === "RESISTANCE" ? "RES" : zt;
    return `<span class="px-1 py-0.5 rounded text-[10px] font-bold ${ztColors[zt] || "bg-gray-700 text-gray-400"} ml-0.5">${label}</span>`;
  };
  const zoneRow = (z, color, borderColor) => {
    const isMtf = z.confluence && z.confluence.length >= 2;
    const isStrong = z.strength === "strong" || z.touches >= 4;
    const weight = isStrong ? "font-bold" : "";
    const border = isMtf ? "border-purple-500" : borderColor;
    return `<div class="${color} ${weight} border-l-2 ${border} pl-1.5 py-0.5">${z.price.toFixed(2)} ${tfBadge(z.timeframe)}${mtfBadge(z.confluence)}${ztBadge(z.zone_type)} <span class="text-gray-500">${z.touches}T — ${z.dist_pips.toFixed(0)}p</span></div>`;
  };
  if (srZones.length > 0) {
    srEl.classList.remove("hidden");
    if (srLegend) srLegend.classList.remove("hidden");
    srContainer.innerHTML = "";
    const above = srZones.filter(z => z.position === "above");
    const below = srZones.filter(z => z.position === "below");
    // Resistance (above) — right column
    let aboveHtml = '<div class="w-full md:w-1/2"><div class="text-red-400 font-bold mb-1">&#9650; RESISTANCE</div>';
    if (above.length === 0) { aboveHtml += '<div class="text-gray-600">None nearby</div>'; }
    for (const z of above) { aboveHtml += zoneRow(z, "text-red-300", "border-red-700"); }
    aboveHtml += '</div>';
    // Support (below) — left column
    let belowHtml = '<div class="w-full md:w-1/2 md:pr-3 md:border-r border-gray-700/50"><div class="text-green-400 font-bold mb-1">&#9660; SUPPORT</div>';
    if (below.length === 0) { belowHtml += '<div class="text-gray-600">None nearby</div>'; }
    for (const z of below) { belowHtml += zoneRow(z, "text-green-300", "border-green-700"); }
    belowHtml += '</div>';
    srContainer.innerHTML = belowHtml + aboveHtml;
  } else {
    srEl.classList.add("hidden");
    if (srLegend) srLegend.classList.add("hidden");
    srContainer.innerHTML = "";
  }

  // GPT Validator
  const gptContainer = el("intel-gpt");
  if (feed.gpt_validator) {
    const action = feed.gpt_validator.action || "CONFIRM";
    const text = feed.gpt_validator.reason || "No detail";
    const adj = feed.gpt_validator.adjustment;
    // Sign is determined by action: BOOST = +, REDUCE = -
    const sign = action === "BOOST" ? "+" : (action === "REDUCE" ? "-" : "+");
    const adjStr = (adj != null && adj !== 0) ? ` (${sign}${adj})` : "";
    let bg = "bg-gray-800/50";
    let border = "border-gray-700/50";
    let textCls = "text-gray-300";
    
    // BOOST = green, REDUCE = red, BLOCK = red, CONFIRM = gray
    if (action === "BOOST") { bg = "bg-green-900/20"; border = "border-green-700/50"; textCls = "text-green-400"; }
    else if (action === "REDUCE") { bg = "bg-red-900/20"; border = "border-red-700/50"; textCls = "text-red-400"; }
    else if (action === "BLOCK") { bg = "bg-red-900/20"; border = "border-red-700/50"; textCls = "text-red-400"; }

    gptContainer.innerHTML = `
      <div class="flex items-start gap-2 sm:gap-3 p-3 sm:p-4 rounded-xl border ${border} ${bg} backdrop-blur-sm w-full">
        <div class="mt-0.5 sm:mt-1 flex-shrink-0">
          <img src="/image/flokiwatch.png" alt="Floki GPT" class="w-6 h-6 sm:w-8 sm:h-8 rounded-full shadow-md ring-2 ring-white/10 opacity-90">
        </div>
        <div class="min-w-0 flex-1">
          <div class="text-[10px] font-bold tracking-widest uppercase ${textCls} mb-1 sm:mb-1.5 font-mono">GPT ${action}${adjStr}</div>
          <div class="text-[11px] sm:text-xs text-gray-300 leading-relaxed font-sans break-words whitespace-normal">${text.replace(/"/g, '&quot;')}</div>
        </div>
      </div>
    `;
    gptContainer.classList.remove("hidden");
  } else {
    gptContainer.classList.add("hidden");
  }

  // Candlestick Patterns
  const patternEl = el("intel-patterns");
  const patterns = feed.candlestick_patterns;
  if (patternEl) {
    if (patterns && patterns.primary) {
      patternEl.classList.remove("hidden");
      const p = patterns.primary;
      const dirColor = p.direction === "bullish" ? "text-green-400 border-green-700" : p.direction === "bearish" ? "text-red-400 border-red-700" : "text-gray-400 border-gray-700";
      const scoreColor = p.final_score > 0 ? "text-green-400" : p.final_score < 0 ? "text-red-400" : "text-gray-400";
      const multText = p.sr_multiplier > 1 ? `×${p.sr_multiplier.toFixed(2)}` : "";
      const srText = p.sr_context || "";
      patternEl.innerHTML = `
        <div class="flex items-center gap-2 text-xs flex-wrap">
          <span class="px-1.5 py-0.5 border rounded font-bold ${dirColor}">${p.name}</span>
          <span class="${scoreColor} font-mono">${p.final_score > 0 ? "+" : ""}${p.final_score}</span>
          ${multText ? `<span class="text-amber-400 text-[10px]">${multText} S/R</span>` : ""}
          ${srText ? `<span class="text-gray-500 text-[10px] truncate max-w-xs">${srText}</span>` : ""}
        </div>
      `;
    } else {
      patternEl.classList.add("hidden");
      patternEl.innerHTML = "";
    }
  }

  // Confirmations + Alerts tags
  const tagsEl = el("intel-tags");
  tagsEl.innerHTML = "";
  const confirmations = feed.confirmations || [];
  const alerts = feed.alerts || [];
  for (const c of confirmations) {
    tagsEl.insertAdjacentHTML("beforeend",
      `<span class="inline-block text-xs px-1.5 py-0.5 rounded border border-green-800 text-green-500 bg-green-900/20">${c}</span>`
    );
  }
  for (const a of alerts) {
    tagsEl.insertAdjacentHTML("beforeend",
      `<span class="inline-block text-xs px-1.5 py-0.5 rounded border border-amber-800 text-amber-400 bg-amber-900/20">&#9888; ${a}</span>`
    );
  }

  // MTF Trend
  if (mtfTrend) {
    const d1Dir = mtfTrend.d1_direction;
    const h4Dir = mtfTrend.h4_direction;
    const alignment = mtfTrend.alignment;
    const mtfAdj = mtfTrend.confidence_adjustment || 0;

    const dirColor = (dir) => dir === "bullish" ? "text-green-400" : dir === "bearish" ? "text-red-400" : "text-gray-500";
    const dirLabel = (dir) => dir ? dir.charAt(0).toUpperCase() + dir.slice(1) : "—";
    
    const alignColor = alignment === "aligned" ? "text-green-400" : alignment === "conflict" ? "text-red-400" : alignment === "mixed" ? "text-yellow-400" : "text-gray-500";
    const alignLabel = alignment === "aligned" ? "Aligned ✓" : alignment === "conflict" ? "Conflict ✗" : alignment === "mixed" ? "Mixed" : "N/A";
    
    const adjColor = mtfAdj > 0 ? "text-green-400" : mtfAdj < 0 ? "text-red-400" : "text-gray-500";
    const adjText = mtfAdj > 0 ? `+${mtfAdj}` : mtfAdj < 0 ? `${mtfAdj}` : "0";

    const mtfD1El = document.getElementById("mtf-d1");
    const mtfH4El = document.getElementById("mtf-h4");
    const mtfAlignEl = document.getElementById("mtf-alignment");
    const mtfAdjEl = document.getElementById("mtf-adj");

    if (mtfD1El) { mtfD1El.className = dirColor(d1Dir); mtfD1El.textContent = dirLabel(d1Dir); }
    if (mtfH4El) { mtfH4El.className = dirColor(h4Dir); mtfH4El.textContent = dirLabel(h4Dir); }
    if (mtfAlignEl) { mtfAlignEl.className = alignColor; mtfAlignEl.textContent = alignLabel; }
    if (mtfAdjEl) { mtfAdjEl.className = adjColor; mtfAdjEl.textContent = adjText; }
  }

  // Volume Gate
  if (volumeGate) {
    const volRatio = volumeGate.volume_ratio;
    const volStatus = volumeGate.status;
    const volAdj = volumeGate.confidence_adjustment || 0;

    const statusColor = volStatus === "normal" ? "text-green-400" : volStatus === "low" ? "text-yellow-400" : volStatus === "very_low" ? "text-red-400" : "text-gray-500";
    const statusLabel = volStatus === "normal" ? "Normal ✓" : volStatus === "low" ? "Low ⚠" : volStatus === "very_low" ? "Very Low ✗" : "—";
    
    const adjColor = volAdj > 0 ? "text-green-400" : volAdj < 0 ? "text-red-400" : "text-gray-500";
    const adjText = volAdj > 0 ? `+${volAdj}` : volAdj < 0 ? `${volAdj}` : "0";

    const volRatioEl = document.getElementById("vol-ratio");
    const volStatusEl = document.getElementById("vol-status");
    const volAdjEl = document.getElementById("vol-adj");

    if (volRatioEl) { volRatioEl.className = statusColor; volRatioEl.textContent = volRatio != null ? `${volRatio.toFixed(1)}x avg` : "—"; }
    if (volStatusEl) { volStatusEl.className = statusColor; volStatusEl.textContent = statusLabel; }
    if (volAdjEl) { volAdjEl.className = adjColor; volAdjEl.textContent = adjText; }
  }
}

/* ================================================================
   AI AGENT CARD
   ================================================================ */

function renderAgentCard(agentDecision) {
  const card = el("agent-card");
  if (!card) return;

  if (!agentDecision || !agentDecision.decision) {
    card.classList.add("hidden");
    return;
  }

  card.classList.remove("hidden");

  if (marketClosed === true) {
    const decisionEl = el("agent-decision");
    if (decisionEl) {
      decisionEl.textContent = "SLEEPING";
      decisionEl.className = "text-2xl font-bold text-gray-500";
    }

    const confEl = el("agent-confidence");
    if (confEl) confEl.textContent = "—%";

    const agreeEl = el("agent-agreement");
    if (agreeEl) {
      agreeEl.textContent = "—";
      agreeEl.className = "text-sm font-semibold text-gray-500";
    }

    const execEl = el("agent-executed");
    if (execEl) execEl.textContent = "—";

    return;
  }

  const decision = agentDecision.decision || "—";
  const confidence = agentDecision.confidence;
  const reasoning = agentDecision.reasoning || "—";
  const keyFactors = agentDecision.key_factors || [];
  const concerns = agentDecision.concerns || [];
  const agreement = agentDecision.agreement;
  const executed = agentDecision.executed || "—";
  const latencyMs = agentDecision.latency_ms;

  // Decision styling
  const decisionEl = el("agent-decision");
  if (decisionEl) {
    decisionEl.textContent = decision;
    if (decision.includes("BUY")) {
      decisionEl.className = "text-2xl font-bold text-green-400";
    } else if (decision.includes("SELL")) {
      decisionEl.className = "text-2xl font-bold text-red-400";
    } else if (decision === "REJECT") {
      decisionEl.className = "text-2xl font-bold text-red-500";
    } else if (decision === "WAIT") {
      decisionEl.className = "text-2xl font-bold text-yellow-400";
    } else {
      decisionEl.className = "text-2xl font-bold text-gray-400";
    }
  }

  // Confidence
  const confEl = el("agent-confidence");
  if (confEl) {
    confEl.textContent = confidence != null ? `${confidence}%` : "—%";
  }

  // Agreement
  const agreeEl = el("agent-agreement");
  if (agreeEl) {
    if (agreement === true) {
      agreeEl.textContent = "✅ AGREE";
      agreeEl.className = "text-sm font-semibold text-green-400";
    } else if (agreement === false) {
      agreeEl.textContent = "❌ DISAGREE";
      agreeEl.className = "text-sm font-semibold text-red-400";
    } else {
      agreeEl.textContent = "—";
      agreeEl.className = "text-sm font-semibold text-gray-500";
    }
  }

  // Executed
  const execEl = el("agent-executed");
  if (execEl) {
    execEl.textContent = executed;
  }

  // Latency
  const latEl = el("agent-latency");
  if (latEl) {
    latEl.textContent = latencyMs != null ? latencyMs : "—";
  }

  // Reasoning
  const reasonEl = el("agent-reasoning");
  if (reasonEl) {
    reasonEl.textContent = reasoning;
  }

  // Key factors
  const factorsEl = el("agent-factors");
  if (factorsEl) {
    if (keyFactors.length > 0) {
      factorsEl.innerHTML = keyFactors.map(f => `<li class="text-gray-300">• ${f}</li>`).join("");
    } else {
      factorsEl.innerHTML = `<li class="text-gray-600">—</li>`;
    }
  }

  // Concerns
  const concernsEl = el("agent-concerns");
  if (concernsEl) {
    if (concerns.length > 0) {
      concernsEl.innerHTML = concerns.map(c => `<li class="text-amber-400">• ${c}</li>`).join("");
    } else {
      concernsEl.innerHTML = `<li class="text-gray-600">None</li>`;
    }
  }
}

/* ================================================================
   PROACTIVE ANALYSIS (H1 Snapshot)
   ================================================================ */

function renderProactiveAnalysis(proactive, positions) {
  const section = el("proactive-section");
  if (!section) return;

  const marketClosed = lastStateForProactiveCountdown?.market?.is_open === false;

  const hasValid = !!(proactive && proactive.decision);
  if (hasValid) {
    lastGoodProactiveAnalysis = proactive;
  }

  const toRender = hasValid ? proactive : lastGoodProactiveAnalysis;
  if (!toRender || !toRender.decision) {
    // Only hide if we've never had a valid snapshot yet. If we have cache, keep visible.
    if (!lastGoodProactiveAnalysis) section.classList.add("hidden");
    return;
  }

  section.classList.remove("hidden");

  const h1Close = toRender.h1_close_time || "—";
  const decision = toRender.decision || "—";
  const confidence = toRender.confidence;
  const reasoning = toRender.reasoning || "—";
  const keyFactors = toRender.key_factors || [];
  const concerns = toRender.concerns || [];
  const latencyMs = toRender.latency_ms;
  const tokensUsed = toRender.tokens_used;

  const entryConditionsEl = el("proactive-entry-conditions");

  const sentimentBarEl = el("sentiment-bar");
  const sentimentIndicatorEl = el("sentiment-indicator");
  const sentimentLabelEl = el("sentiment-label");

  const lifecycleBarEl = el("lifecycle-bar");
  const lifecycleIndicatorEl = el("lifecycle-indicator");
  const lifecycleLabelEl = el("lifecycle-label");

  const tpBlockEl = el("proactive-tp-block");
  const tpEntryEl = el("proactive-tp-entry");
  const tpSlEl = el("proactive-tp-sl");
  const tpTpEl = el("proactive-tp-tp");
  const tpRrEl = el("proactive-tp-rr");

  const closeReasonBlockEl = el("proactive-close-reason-block");
  const closeReasonEl = el("proactive-close-reason");

  const adjustBlockEl = el("proactive-adjust-block");
  const adjustSlEl = el("proactive-adjust-sl");
  const adjustTpEl = el("proactive-adjust-tp");
  const adjustReasonEl = el("proactive-adjust-reason");

  const h1El = el("proactive-h1-close");
  if (h1El) {
    try {
      const d = new Date(h1Close);
      if (!Number.isNaN(d.getTime())) {
        const hh = String(d.getUTCHours()).padStart(2, "0");
        const mm = String(d.getUTCMinutes()).padStart(2, "0");
        h1El.textContent = `${hh}:${mm} UTC`;
      } else {
        h1El.textContent = h1Close;
      }
    } catch (e) {
      h1El.textContent = h1Close;
    }
  }

  const decisionEl = el("proactive-decision");
  if (decisionEl) {
    if (marketClosed) {
      decisionEl.textContent = "SLEEPING";
      decisionEl.style.color = "";
      const glowClasses = ["decision-buy", "decision-sell", "decision-hold", "decision-wait", "decision-close", "decision-adjust"];
      glowClasses.forEach(c => decisionEl.classList.remove(c));
      decisionEl.classList.add("decision-wait");
    } else {
      decisionEl.textContent = decisionLabel(decision);
      decisionEl.style.color = "";
      const d = (decision || "").toUpperCase();
      const glowClasses = ["decision-buy", "decision-sell", "decision-hold", "decision-wait", "decision-close", "decision-adjust"];
      glowClasses.forEach(c => decisionEl.classList.remove(c));
      if (d === "OPEN_BUY") decisionEl.classList.add("decision-buy");
      else if (d === "OPEN_SELL") decisionEl.classList.add("decision-sell");
      else if (d === "HOLD_TRADE") decisionEl.classList.add("decision-hold");
      else if (d === "CLOSE_TRADE") decisionEl.classList.add("decision-close");
      else if (d === "ADJUST_TRADE") decisionEl.classList.add("decision-adjust");
      else decisionEl.classList.add("decision-wait");
    }
  }

  const confBarEl = el("proactive-confidence-bar");
  if (confBarEl) {
    if (marketClosed) {
      confBarEl.style.width = "0%";
      confBarEl.classList.remove("conf-low", "conf-mid", "conf-high");
      confBarEl.classList.add("conf-mid");
    } else {
      const confNum = Number(confidence);
      const confSafe = Number.isFinite(confNum) ? Math.max(0, Math.min(100, confNum)) : 0;
      confBarEl.style.width = `${confSafe}%`;
      confBarEl.classList.remove("conf-low", "conf-mid", "conf-high");
      if (confSafe < 55) confBarEl.classList.add("conf-low");
      else if (confSafe < 70) confBarEl.classList.add("conf-mid");
      else confBarEl.classList.add("conf-high");
    }
  }

  const proactivePanelEl = section.querySelector(".glass-panel");
  if (proactivePanelEl) {
    proactivePanelEl.classList.remove("proactive-panel-glow-buy", "proactive-panel-glow-sell", "proactive-panel-glow-hold", "proactive-panel-glow-wait");
    if (marketClosed) {
      proactivePanelEl.classList.add("proactive-panel-glow-wait");
    } else {
      const dUp = (decision || "").toUpperCase();
      if (dUp === "OPEN_BUY") proactivePanelEl.classList.add("proactive-panel-glow-buy");
      else if (dUp === "OPEN_SELL") proactivePanelEl.classList.add("proactive-panel-glow-sell");
      else if (dUp === "HOLD_TRADE" || dUp === "ADJUST_TRADE" || dUp === "CLOSE_TRADE") proactivePanelEl.classList.add("proactive-panel-glow-hold");
      else proactivePanelEl.classList.add("proactive-panel-glow-wait");
    }
  }

  // HOLD display from live positions[]
  const holdBlockEl = el("proactive-hold-block");
  const holdSummaryEl = el("proactive-hold-summary");
  const holdPnlEl = el("proactive-hold-pnl");
  try {
    const isHold = (decision || "").toUpperCase() === "HOLD_TRADE";
    const pos0 = Array.isArray(positions) && positions.length > 0 ? positions[0] : null;

    if (holdBlockEl && holdSummaryEl && holdPnlEl && isHold && pos0) {
      holdBlockEl.classList.remove("hidden");

      const dir = (pos0.direction || "").toString().toUpperCase() || "—";
      const entry = pos0.open_price;
      holdSummaryEl.textContent = `Holding ${dir} from ${fmtNum(entry, 1)}`;

      const pnl = Number(pos0.profit);
      const pnlPips = pos0.profit_pips;
      const pnlClass = pnl > 0 ? "text-green-400" : (pnl < 0 ? "text-red-400" : "text-gray-400");
      const pipsPart = pnlPips != null ? ` (${fmtNum(pnlPips, 0)} p)` : "";
      holdPnlEl.className = `text-xs font-mono mt-1 ${pnlClass}`;
      holdPnlEl.textContent = `P&L: ${fmtMoney(pnl)}${pipsPart}`;
    } else if (holdBlockEl) {
      holdBlockEl.classList.add("hidden");
    }
  } catch (e) {
    if (holdBlockEl) holdBlockEl.classList.add("hidden");
  }

  const confEl = el("proactive-confidence");
  if (confEl) {
    if (marketClosed) confEl.textContent = "—%";
    else confEl.textContent = confidence != null ? `${confidence}%` : "—%";
  }

  try {
    const dUp = (decision || "").toString().toUpperCase();
    const entryConditions = toRender.entry_conditions;
    const hasEntryConditions = !!(entryConditions && typeof entryConditions === "object");

    if (entryConditionsEl && dUp === "WAIT" && hasEntryConditions) {
      const side = (entryConditions.direction || "").toString().toUpperCase();
      const touch = entryConditions.touch_price;
      const rationale = (entryConditions.rationale || entryConditions.reason || "").toString();
      const validHours = entryConditions.valid_hours;

      let msg = "Watching";
      if (side) msg += ` for ${side}`;
      if (touch != null && Number.isFinite(Number(touch))) msg += ` if price touches ${fmtNum(touch, 1)}`;
      if (rationale) msg += ` (${rationale})`;
      if (validHours != null && Number.isFinite(Number(validHours))) msg += `. Valid for ${Number(validHours)}h.`;
      else msg += ".";

      entryConditionsEl.textContent = msg;
      entryConditionsEl.classList.remove("hidden");
    } else if (entryConditionsEl) {
      entryConditionsEl.textContent = "";
      entryConditionsEl.classList.add("hidden");
    }
  } catch (e) {
    if (entryConditionsEl) {
      entryConditionsEl.textContent = "";
      entryConditionsEl.classList.add("hidden");
    }
  }

  try {
    const confNum = Number(confidence);
    const confSafe = Number.isFinite(confNum) ? Math.max(0, Math.min(100, confNum)) : null;
    const pos0 = Array.isArray(positions) && positions.length > 0 ? positions[0] : null;
    const inTrade = Array.isArray(positions) && positions.length > 0;

    const tradeHistory0 = lastStateForProactiveCountdown?.trade_history?.[0] || null;
    const tradeHistoryProfit = tradeHistory0 && tradeHistory0.profit != null ? Number(tradeHistory0.profit) : null;

    if (lastHadPosition === true && inTrade === false && Number.isFinite(tradeHistoryProfit)) {
      lastKnownClosedPnl = tradeHistoryProfit;
    }
    lastHadPosition = inTrade;

    if (sentimentBarEl && sentimentIndicatorEl && sentimentLabelEl) {
      const d = (decision || "").toString().toUpperCase();

      let label = "NEUTRAL";
      let activeIdx = 2;
      const c = confSafe;

      const dirFromPos = (pos0?.direction || "").toString().toUpperCase();
      const holdDir = dirFromPos === "BUY" || dirFromPos === "SELL" ? dirFromPos : null;

      if (d === "OPEN_SELL") {
        if (c != null && c >= 75) { label = "STRONG SELL"; activeIdx = 0; }
        else { label = "SELL"; activeIdx = 1; }
      } else if (d === "OPEN_BUY") {
        if (c != null && c >= 75) { label = "STRONG BUY"; activeIdx = 4; }
        else { label = "BUY"; activeIdx = 3; }
      } else if (d === "WAIT") {
        label = "NEUTRAL";
        activeIdx = 2;
      } else if (d === "CLOSE_TRADE") {
        label = "NEUTRAL";
        activeIdx = 2;
      } else if (d === "HOLD_TRADE" || d === "ADJUST_TRADE") {
        if (holdDir === "SELL") {
          if (c != null && c >= 75) { label = "STRONG SELL"; activeIdx = 0; }
          else { label = "SELL"; activeIdx = 1; }
        } else if (holdDir === "BUY") {
          if (c != null && c >= 75) { label = "STRONG BUY"; activeIdx = 4; }
          else { label = "BUY"; activeIdx = 3; }
        } else {
          label = "NEUTRAL";
          activeIdx = 2;
        }
      }

      sentimentLabelEl.textContent = label;
      const segs = sentimentBarEl.children;
      // 5 segments: 0=STRONG SELL, 1=SELL, 2=NEUTRAL, 3=BUY, 4=STRONG BUY
      for (let i = 0; i < segs.length; i++) {
        const seg = segs[i];
        if (!seg) continue;
        if (i === activeIdx) seg.classList.add("active");
        else seg.classList.remove("active");
      }

      const x = c != null ? (c / 100) : 0.5;
      sentimentIndicatorEl.style.left = `${(Math.max(0, Math.min(1, x)) * 100).toFixed(1)}%`;
    }

    if (lifecycleBarEl && lifecycleIndicatorEl && lifecycleLabelEl) {
      const d = (decision || "").toString().toUpperCase();
      const entryConditionsPresent = !!(toRender.entry_conditions && typeof toRender.entry_conditions === "object");

      const posProfit = pos0 && pos0.profit != null ? Number(pos0.profit) : null;
      const hasPnl = Number.isFinite(posProfit);
      const pnlNonNeg = hasPnl ? posProfit >= 0 : true;

      let step = "WATCHING";
      let stepIdx = 0;

      if (!inTrade) {
        if (entryConditionsPresent) {
          step = "PREPARING";
          stepIdx = 1;
        }
      }

      if (d === "OPEN_BUY" || d === "OPEN_SELL") {
        step = "ENTRY";
        stepIdx = 2;
      } else if (d === "CLOSE_TRADE") {
        step = "CLOSING";
        stepIdx = 4;
      } else if (inTrade && (d === "HOLD_TRADE" || d === "ADJUST_TRADE" || d === "WAIT")) {
        step = "MANAGING";
        stepIdx = 3;
      } else if (!inTrade && d === "ADJUST_TRADE") {
        step = "WATCHING";
        stepIdx = 0;
      }

      if (!inTrade && lastProactiveDecision === "CLOSE_TRADE" && Number.isFinite(lastKnownClosedPnl)) {
        step = "RESULT";
        stepIdx = 5;
      }

      lifecycleLabelEl.textContent = step;

      // 6 segments, colors defined in CSS per nth-child slot
      const segs = lifecycleBarEl.children;
      for (let i = 0; i < segs.length; i++) {
        const seg = segs[i];
        if (!seg) continue;
        if (i <= stepIdx) seg.classList.add("active");
        else seg.classList.remove("active");
      }

      lifecycleIndicatorEl.style.left = `${(((stepIdx + 0.5) / 6) * 100).toFixed(1)}%`;
    }

    lastProactiveDecision = (decision || "").toString().toUpperCase();
  } catch (e) {
    // silent
  }

  // Trade plan block (OPEN decisions only)
  try {
    const isOpen = decision === "OPEN_BUY" || decision === "OPEN_SELL";
    const tp = toRender.trade_plan;

    if (tpBlockEl && tpEntryEl && tpSlEl && tpTpEl && tpRrEl && isOpen && tp) {
      const entryStrategy = (tp.entry_strategy || "").toUpperCase() || "—";
      const entryPrice = tp.entry_price;
      const sl = tp.stop_loss;
      const takeProfit = tp.take_profit;
      const rr = tp.risk_reward_ratio;

      tpEntryEl.textContent = `Entry: ${entryStrategy} @ ${fmtNum(entryPrice, 2)}`;
      tpSlEl.textContent = `SL: ${fmtNum(sl, 2)}`;
      tpTpEl.textContent = `TP: ${fmtNum(takeProfit, 2)}`;
      tpRrEl.textContent = `R:R: ${fmtNum(rr, 1)}`;

      tpBlockEl.classList.remove("hidden");
    } else if (tpBlockEl) {
      tpBlockEl.classList.add("hidden");
      if (tpEntryEl) tpEntryEl.textContent = "—";
      if (tpSlEl) tpSlEl.textContent = "—";
      if (tpTpEl) tpTpEl.textContent = "—";
      if (tpRrEl) tpRrEl.textContent = "—";
    }
  } catch (e) {
    if (tpBlockEl) tpBlockEl.classList.add("hidden");
  }

  // CLOSE reason / ADJUST details blocks
  try {
    const closeReason = toRender.close_reason;
    const adjustment = toRender.adjustment;

    const shouldShowCloseReason = decision === "CLOSE_TRADE" && !!(closeReason && String(closeReason).trim());
    const shouldShowAdjustment = decision === "ADJUST_TRADE" && !!adjustment;

    if (closeReasonBlockEl) {
      if (shouldShowCloseReason) closeReasonBlockEl.classList.remove("hidden");
      else closeReasonBlockEl.classList.add("hidden");
    }

    if (closeReasonEl) {
      closeReasonEl.textContent = shouldShowCloseReason ? String(closeReason) : "—";
    }

    if (adjustBlockEl) {
      if (shouldShowAdjustment) adjustBlockEl.classList.remove("hidden");
      else adjustBlockEl.classList.add("hidden");
    }

    if (adjustSlEl) {
      adjustSlEl.textContent = shouldShowAdjustment ? fmtNum(adjustment?.new_sl, 2) : "—";
    }
    if (adjustTpEl) {
      adjustTpEl.textContent = shouldShowAdjustment ? fmtNum(adjustment?.new_tp, 2) : "—";
    }
    if (adjustReasonEl) {
      adjustReasonEl.textContent = shouldShowAdjustment ? (adjustment?.reason || "—") : "—";
    }
  } catch (e) {
    if (closeReasonBlockEl) closeReasonBlockEl.classList.add("hidden");
    if (adjustBlockEl) adjustBlockEl.classList.add("hidden");
  }

  const reasonEl = el("proactive-reasoning");
  if (reasonEl) {
    reasonEl.textContent = reasoning;
  }

  const factorsEl = el("proactive-factors");
  if (factorsEl) {
    if (keyFactors.length > 0) {
      factorsEl.innerHTML = keyFactors.map(f => `<li class="text-gray-300">• ${f}</li>`).join("");
    } else {
      factorsEl.innerHTML = `<li class="text-gray-600">—</li>`;
    }
  }

  const concernsEl = el("proactive-concerns");
  if (concernsEl) {
    if (concerns.length > 0) {
      concernsEl.innerHTML = concerns.map(c => `<li class="text-amber-400">• ${c}</li>`).join("");
    } else {
      concernsEl.innerHTML = `<li class="text-gray-600">None</li>`;
    }
  }

  const latEl = el("proactive-latency");
  if (latEl) {
    latEl.textContent = latencyMs != null ? latencyMs : "—";
  }

  const tokEl = el("proactive-tokens");
  if (tokEl) {
    tokEl.textContent = tokensUsed != null ? `${tokensUsed}` : "—";
  }
}

/* ================================================================
   AI AGENT MEMORY (v1.3)
   ================================================================ */

function renderAgentMemory(agentMemory) {
  const section = el("agent-memory-section");
  if (!section) return;

  if (!agentMemory || !agentMemory.timestamp) {
    section.classList.add("hidden");
    return;
  }

  section.classList.remove("hidden");

  // Timestamp - how long ago
  const timestampEl = el("agent-memory-timestamp");
  if (timestampEl) {
    try {
      const rejectTime = new Date(agentMemory.timestamp);
      const now = new Date();
      const diffMs = now - rejectTime;
      const diffMin = Math.floor(diffMs / 60000);
      timestampEl.textContent = diffMin < 60 ? `${diffMin} min ago` : `${Math.floor(diffMin / 60)}h ${diffMin % 60}m ago`;
    } catch (e) {
      timestampEl.textContent = "—";
    }
  }

  // Brain signal that was rejected
  const brainSignalEl = el("agent-memory-brain-signal");
  if (brainSignalEl) {
    brainSignalEl.textContent = `${agentMemory.brain_signal || "—"} @ ${fmtNum(agentMemory.brain_score, 1)}`;
  }

  // Market view direction
  const viewDirEl = el("agent-memory-view-direction");
  if (viewDirEl) {
    const dir = agentMemory.market_view?.direction || "—";
    viewDirEl.textContent = dir;
    if (dir === "BUY") {
      viewDirEl.className = "text-lg font-bold text-green-400";
    } else if (dir === "SELL") {
      viewDirEl.className = "text-lg font-bold text-red-400";
    } else {
      viewDirEl.className = "text-lg font-bold text-yellow-400";
    }
  }

  // Market view description
  const viewDescEl = el("agent-memory-view-description");
  if (viewDescEl) {
    viewDescEl.textContent = agentMemory.market_view?.description || "—";
  }

  // Conditions
  const conditionsEl = el("agent-memory-conditions");
  if (conditionsEl) {
    const conditions = agentMemory.conditions || [];
    if (conditions.length === 0) {
      conditionsEl.innerHTML = `<li class="text-gray-500">No conditions set</li>`;
    } else {
      conditionsEl.innerHTML = conditions.map(c => {
        const icon = c.met ? "✅" : "❌";
        const currentVal = c.current_value != null ? ` (now: ${fmtNum(c.current_value, 1)})` : "";
        const textClass = c.met ? "text-green-400" : "text-gray-300";
        return `<li class="${textClass}">${icon} ${c.description}${currentVal}</li>`;
      }).join("");
    }
  }

  // Expiry
  const expiryEl = el("agent-memory-expiry");
  if (expiryEl) {
    const inv = agentMemory.invalidation;
    if (inv) {
      expiryEl.textContent = `${inv.candles_remaining} ${inv.timeframe} candles remaining`;
    } else {
      expiryEl.textContent = "—";
    }
  }

  // Status badge - read from JSON, handle ACTIVE, EXPIRED, conditions_met
  const statusEl = el("agent-memory-status");
  if (statusEl) {
    const status = (agentMemory.status || "ACTIVE").toUpperCase();
    const allMet = agentMemory.all_conditions_met;
    
    if (allMet || status === "CONDITIONS_MET") {
      statusEl.textContent = "ALL CONDITIONS MET";
      statusEl.className = "px-2 py-0.5 rounded text-[10px] font-bold bg-green-500/20 text-green-400 border border-green-500/30 animate-pulse";
    } else if (status === "EXPIRED") {
      statusEl.textContent = "EXPIRED";
      statusEl.className = "px-2 py-0.5 rounded text-[10px] font-bold bg-gray-500/20 text-gray-400 border border-gray-500/30";
    } else {
      statusEl.textContent = "ACTIVE";
      statusEl.className = "px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-400 border border-amber-500/30";
    }
  }
}

/* ================================================================
   RECENT DECISIONS
   ================================================================ */

function decisionColor(d) {
  const s = (d || "").toUpperCase();
  if (s === "OPEN_BUY") return "text-green-400 border-green-700/50 bg-green-900/20 shadow-sm shadow-green-900/10";
  if (s === "OPEN_SELL") return "text-red-400 border-red-700/50 bg-red-900/20 shadow-sm shadow-red-900/10";
  if (s === "HOLD_TRADE") return "text-emerald-400 border-emerald-700/50 bg-emerald-900/20 shadow-sm shadow-emerald-900/10";
  if (s === "CLOSE_TRADE") return "text-red-300 border-red-800/50 bg-red-950/30 shadow-sm shadow-red-900/10";
  if (s === "ADJUST_TRADE") return "text-yellow-300 border-yellow-700/50 bg-yellow-900/20 shadow-sm shadow-yellow-900/10";
  if (s === "WAIT") return "text-yellow-300 border-yellow-700/50 bg-yellow-900/20 shadow-sm shadow-yellow-900/10";

  if (s.includes("BUY")) return "text-green-400 border-green-700/50 bg-green-900/20 shadow-sm shadow-green-900/10";
  if (s.includes("SELL")) return "text-red-400 border-red-700/50 bg-red-900/20 shadow-sm shadow-red-900/10";
  if (s.includes("CLOSE")) return "text-red-300 border-red-800/50 bg-red-950/30 shadow-sm shadow-red-900/10";
  if (s.includes("HOLD")) return "text-yellow-300 border-yellow-700/50 bg-yellow-900/20 shadow-sm shadow-yellow-900/10";
  return "text-gray-300 border-gray-700/50 bg-gray-900/20 shadow-sm";
}

function renderRecentDecisions(decisions) {
  const container = el("recent-decisions");
  if (!container) return;

  if (!decisions || decisions.length === 0) {
    container.innerHTML = `<span class="text-gray-500 font-medium tracking-wide text-[11px] uppercase">NO DECISIONS YET</span>`;
    return;
  }

  container.innerHTML = "";
  for (const d of decisions) {
    const time = (d.timestamp || "").split("T")[1]?.slice(0, 5) || "—";
    const decision = d.decision || "HOLD";
    const score = d.score != null ? `${fmtNum(d.score, 0)}%` : "—";
    const cls = decisionColor(decision);

    container.insertAdjacentHTML("beforeend", `
      <div class="flex items-center gap-1.5 px-2 py-1 rounded-md border ${cls} backdrop-blur-sm transition-transform duration-200 hover:-translate-y-0.5">
        <span class="text-[10px] text-gray-500 font-mono tracking-wider opacity-80">${time}</span>
        <span class="font-bold text-[10px] tracking-wide uppercase">${decision}</span>
        <span class="text-[10px] font-mono opacity-90 font-medium pl-1 border-l border-current/20">${score}</span>
      </div>
    `);
  }
}

async function pollDecisions() {
  try {
    const r = await fetch("/api/recent-decisions", { cache: "no-store" });
    const data = await r.json();
    renderRecentDecisions(data);
  } catch (e) {
    // silent
  }
}

async function poll() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    const data = await r.json();

    const status = data.bot?.status || "OFFLINE";
    const metaAge = data._meta?.file_age_seconds;

    const shouldRender =
      (data.timestamp && data.timestamp !== lastTimestamp) ||
      status !== lastBotStatus ||
      metaAge !== lastMetaAgeSeconds;

    lastTimestamp = data.timestamp || lastTimestamp;
    lastBotStatus = status;
    lastMetaAgeSeconds = metaAge;

    if (!shouldRender) return;
    render(data);
  } catch (e) {
    render({ bot: { status: "OFFLINE" }, timestamp: new Date().toISOString(), _meta: { file_age_seconds: null } });
  }
}

// ── WebSocket: live state push ──────────────────────
let _ws = null;
let _wsRetries = 0;
let _wsPollInterval = null;

function _wsSetStatus(s) {
  const d = document.getElementById("ws-status-dot");
  if (!d) return;
  d.className = "ws-dot ws-" + s;
  d.title = s === "connected" ? "Live (WebSocket)" : s === "polling" ? "Polling (fallback)" : "Reconnecting\u2026";
}

function _wsRenderMsg(data) {
  const status = data.bot?.status || "OFFLINE";
  const metaAge = data._meta?.file_age_seconds;
  const shouldRender =
    (data.timestamp && data.timestamp !== lastTimestamp) ||
    status !== lastBotStatus ||
    metaAge !== lastMetaAgeSeconds;
  lastTimestamp = data.timestamp || lastTimestamp;
  lastBotStatus = status;
  lastMetaAgeSeconds = metaAge;
  if (shouldRender) render(data);
}

function wsConnect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = proto + "//" + location.host + "/ws";
  try { _ws = new WebSocket(url); } catch (e) { _wsFallback(); return; }

  _ws.onopen = function () {
    _wsRetries = 0;
    if (_wsPollInterval) { clearInterval(_wsPollInterval); _wsPollInterval = null; }
    _wsSetStatus("connected");
  };
  _ws.onmessage = function (ev) {
    try { _wsRenderMsg(JSON.parse(ev.data)); } catch (e) {}
  };
  _ws.onclose = function () {
    _ws = null;
    _wsRetries++;
    if (_wsRetries > 3) {
      _wsFallback();
    } else {
      _wsSetStatus("disconnected");
      var delay = Math.min(5000 * Math.pow(2, _wsRetries - 1), 30000);
      setTimeout(wsConnect, delay);
    }
  };
  _ws.onerror = function () {};
}

function _wsFallback() {
  _wsSetStatus("polling");
  if (!_wsPollInterval) _wsPollInterval = setInterval(poll, 3000);
}

// Inject status dot
(function () {
  var dot = document.createElement("span");
  dot.id = "ws-status-dot";
  dot.className = "ws-dot ws-disconnected";
  dot.title = "Connecting\u2026";
  var hdr = document.querySelector(".site-header nav") || document.querySelector(".site-header") || document.body;
  hdr.appendChild(dot);

  var sty = document.createElement("style");
  sty.textContent = ".ws-dot{width:6px;height:6px;border-radius:50%;display:inline-block;margin-left:8px;vertical-align:middle;flex-shrink:0}" +
    ".ws-connected{background:#4ade80;box-shadow:0 0 4px #4ade80}" +
    ".ws-disconnected{background:#f87171}" +
    ".ws-polling{background:#facc15}";
  document.head.appendChild(sty);
})();

wsConnect();
setInterval(pollDecisions, 10000);
poll();
pollDecisions();
