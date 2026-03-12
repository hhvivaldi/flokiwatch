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

function decisionHexColor(decision) {
  const d = (decision || "").toString().toUpperCase();
  const map = {
    OPEN_BUY: "#4caf50",
    OPEN_SELL: "#e74c3c",
    HOLD_TRADE: "#2ecc71",
    CLOSE_TRADE: "#e67e22",
    ADJUST_TRADE: "#f1c40f",
    WAIT: "#8e8e8e",
  };
  return map[d] || "#8e8e8e";
}

function bindUIHandlersOnce() {
  if (uiHandlersBound) return;

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

  const refToggle = el("reference-toggle");
  if (refToggle) {
    refToggle.addEventListener("click", () => {
      try {
        toggleReferencePanels();
      } catch (e) {
        // silent
      }
    });
  }

  uiHandlersBound = true;
}

function toggleReferencePanels() {
  const panel = el("reference-panels");
  const btn = el("reference-toggle");
  if (!panel || !btn) return;

  const isHidden = panel.classList.contains("hidden");
  if (isHidden) {
    panel.classList.remove("hidden");
    btn.textContent = "Hide Brain Reference";
  } else {
    panel.classList.add("hidden");
    btn.textContent = "Show Brain Reference";
  }
}

function toggleProactiveReasoning() {
  const p = el("proactive-reasoning");
  const btn = el("proactive-reasoning-toggle");
  const card = el("proactive-reasoning-card");
  if (!p || !btn) return;

  proactiveReasoningExpanded = !proactiveReasoningExpanded;
  if (proactiveReasoningExpanded) {
    p.classList.remove("line-clamp-4");
    btn.textContent = "COLLAPSE";
  } else {
    p.classList.add("line-clamp-4");
    btn.textContent = "EXPAND";
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
    container.innerHTML = `<span class="text-gray-600 font-black uppercase tracking-[0.2em]">NO TRIGGERS YET</span>`;
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
    const textParts = [action];
    if (reason) textParts.push(reason);
    if (suffix) textParts.push(suffix);
    textParts.push(ageText);

    return `
      <span
        class="px-2.5 py-1 rounded-full border bg-black/20 backdrop-blur-sm font-black uppercase tracking-[0.2em]"
        style="color:${style.color};border-color:${style.border};"
      >${textParts.join(" — ")}</span>
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
  if (!bar || !val) return;
  const s = Number(score);
  const pct = Number.isNaN(s) ? 0 : Math.max(0, Math.min(100, s));
  bar.style.width = `${pct}%`;
  bar.className = `h-2 rounded-full transition-all duration-1000 ease-out ${pillColor(s)}`;
  val.textContent = fmtNum(s, 1);
  val.className = "col-span-1 text-xs font-mono font-black text-gray-300 text-right tracking-[0.2em]";
}

function setStatusDot(isOperational) {
  const dot = el("status-dot");
  const label = el("status-label");
  if (!dot || !label) return;

  if (isOperational) {
    dot.className = "w-2 h-2 rounded-full bg-green-400 animate-pulse";
    label.textContent = "OPERATIONAL";
    label.className = "text-green-400 font-black tracking-[0.2em] uppercase";
  } else {
    dot.className = "w-2 h-2 rounded-full bg-red-500 animate-pulse";
    label.textContent = "OFFLINE";
    label.className = "text-red-400 font-black tracking-[0.2em] uppercase";
  }
}

function renderPositions(positions) {
  const container = el("positions");
  const countEl = el("positions-count");
  if (!container) return;
  container.innerHTML = "";

  if (!positions || positions.length === 0) {
    container.innerHTML = `<div class="text-xs text-gray-400">NO OPEN POSITIONS</div>`;
    if (countEl) countEl.textContent = "0";
    return;
  }

  if (countEl) countEl.textContent = String(positions.length);
  // ... rest of function remains same but uses container safely

  for (const p of positions) {
    const pnl = Number(p.profit);
    const pnlClass = pnl >= 0 ? "text-green-400" : "text-red-400";
    
    // Phase badge styling
    const phase = p.phase || "OPEN";
    let phaseBadge = "";
    if (phase === "TRAILING") {
      phaseBadge = `<span class="ml-2 px-1.5 py-0.5 rounded text-[10px] font-black bg-green-500/20 text-green-400 border border-green-500/30 uppercase tracking-[0.2em]">TRAILING</span>`;
    } else if (phase === "BREAKEVEN") {
      phaseBadge = `<span class="ml-2 px-1.5 py-0.5 rounded text-[10px] font-black bg-amber-500/20 text-amber-400 border border-amber-500/30 uppercase tracking-[0.2em]">BE ACTIVE</span>`;
    } else {
      phaseBadge = `<span class="ml-2 px-1.5 py-0.5 rounded text-[10px] font-black bg-red-500/20 text-red-400 border border-red-500/30 uppercase tracking-[0.2em]">OPEN</span>`;
    }

    // BE distance indicator
    let beIndicator = "";
    if (phase === "OPEN" && p.be_remaining_pips != null) {
      beIndicator = `<span class="text-[10px] text-gray-500 font-mono font-black uppercase tracking-[0.2em]">BE in ${fmtNum(p.be_remaining_pips, 0)}p</span>`;
    } else if (phase === "BREAKEVEN" || phase === "TRAILING") {
      beIndicator = `<span class="text-[10px] text-green-500 font-mono font-black uppercase tracking-[0.2em]">BE ✓</span>`;
    }

    container.insertAdjacentHTML(
      "beforeend",
      `
      <div class="glass-panel rounded-xl p-3 shadow-md hover:bg-gray-800/60 transition-colors duration-200">
        <div class="flex items-center justify-between mb-2">
          <div class="text-xs font-mono font-black text-gray-200 uppercase tracking-[0.2em]">
            <span class="text-gray-500 mr-1 font-black tracking-[0.2em]">#</span>${p.ticket} 
            <span class="ml-2 px-1.5 py-0.5 rounded text-[10px] font-black border border-gray-600/50 text-gray-300 bg-gray-800/50 tracking-[0.2em]">${p.direction}</span> 
            ${phaseBadge}
            <span class="ml-2 text-gray-400 uppercase tracking-[0.2em] font-black">${p.volume} lot</span>
          </div>
          <div class="text-xs font-mono font-black ${pnlClass} tracking-[0.2em]">${fmtMoney(pnl)} <span class="text-[10px] text-gray-500 font-black ml-1 tracking-[0.2em]">(${fmtNum(p.profit_pips, 0)} p)</span></div>
        </div>
        <div class="grid grid-cols-2 md:grid-cols-5 gap-3 text-[10px] font-mono font-black text-gray-500 uppercase tracking-[0.2em] bg-black/20 rounded-lg p-2 border border-white/5">
          <div><span class="block mb-0.5 text-gray-600">Entry</span><span class="text-gray-200 tracking-[0.2em]">${fmtNum(p.open_price, 2)}</span></div>
          <div><span class="block mb-0.5 text-gray-600">SL</span><span class="text-gray-200 tracking-[0.2em]">${fmtNum(p.sl, 2)}</span></div>
          <div><span class="block mb-0.5 text-gray-600">TP</span><span class="text-gray-200 tracking-[0.2em]">${fmtNum(p.tp, 2)}</span></div>
          <div><span class="block mb-0.5 text-gray-600">Now</span><span class="text-gray-200 tracking-[0.2em]">${fmtNum(p.current_price, 2)}</span></div>
          <div><span class="block mb-0.5 text-gray-600">Protection</span>${beIndicator}</div>
        </div>
      </div>
      `
    );
  }
}

function renderTrades(trades, daily) {
  const container = el("trades");
  if (!container) return;
  container.innerHTML = "";

  const wins = Number(daily?.wins || 0);
  const losses = Number(daily?.losses || 0);
  const breakevens = Number(daily?.breakevens || 0);
  const decisive = wins + losses;
  const wr = decisive ? (wins / decisive) * 100 : 0;

  const twEl = el("trades-w"); if (twEl) twEl.textContent = String(wins);
  const tlEl = el("trades-l"); if (tlEl) tlEl.textContent = String(losses);
  const tbeEl = el("trades-be"); if (tbeEl) tbeEl.textContent = String(breakevens);
  const twrEl = el("trades-wr"); if (twrEl) twrEl.textContent = `${wr.toFixed(1)}%`;
  const hasDailyPnl = daily && daily.pnl !== null && daily.pnl !== undefined && !Number.isNaN(Number(daily.pnl));
  const tpnlEl = el("trades-pnl"); if (tpnlEl) tpnlEl.textContent = hasDailyPnl ? fmtMoney(daily?.pnl) : "—";

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
      const typeMap = { tp: "Take Profit", trailing: "Trailing Stop", breakeven: "Breakeven", sl: "Stop Loss" };
      displayReason = typeMap[t.close_type] || t.reason || "";
    } else {
      displayReason = t.reason || "";
      if (displayReason.toLowerCase().includes("stop loss")) {
        if (pnl > 1.0) displayReason = "Trailing Stop";
        else if (pnl >= 0) displayReason = "Breakeven";
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
      pnlClass = pnl > 0 ? "text-green-400" : (pnl < 0 ? "text-red-400" : "text-gray-400");
      const estBadge = t.estimated ? ` <span class="text-amber-400 opacity-75">(est.)</span>` : "";
      pnlDisplay = `${fmtMoney(pnl)}${estBadge}`;
      icon = pnl > 0 ? "WIN" : (pnl < 0 ? "LOSS" : "BE");
    }

    container.insertAdjacentHTML(
      "beforeend",
      `
      <div class="flex items-center justify-between text-[11px] font-mono border-b border-gray-800/60 py-2.5 px-2 hover:bg-gray-800/30 transition-colors duration-200 rounded-lg group">
        <div class="text-gray-500 font-black tracking-[0.2em] uppercase">${time}</div>
        <div class="text-gray-300 font-black tracking-[0.2em] uppercase">${t.direction || "—"}</div>
        <div class="text-gray-400 truncate max-w-[14rem] sm:max-w-[10rem] md:max-w-[14rem] font-black tracking-[0.2em] uppercase">${displayReason}</div>
        <div class="${pnlClass} font-black tracking-[0.2em]">${pnlDisplay}</div>
        <div class="text-[10px] uppercase tracking-[0.2em] font-black px-1.5 py-0.5 rounded bg-gray-800/50 border border-gray-700/50 text-gray-400">${icon}</div>
      </div>
      `
    );
  }
}

function renderVolBanner(volStatus, volDesc) {
  const banner = el("vol-banner");
  const status = (volStatus || "NORMAL").toUpperCase();

  if (status === "EXTREME") {
    banner.className = "block bg-red-900/60 border-b border-red-500 text-red-200 px-4 py-2 text-xs font-black tracking-[0.2em] uppercase animate-pulse";
    banner.textContent = `BREAKING: EXTREME VOLATILITY — TRADING BLOCKED. ${volDesc || ""}`;
  } else if (status === "COOLING_DOWN") {
    banner.className = "block bg-yellow-900/40 border-b border-yellow-500 text-yellow-200 px-4 py-2 text-xs font-black tracking-[0.2em] uppercase";
    banner.textContent = `ALERT: COOLING DOWN — ONLY STRONG SIGNALS. ${volDesc || ""}`;
  } else {
    banner.className = "hidden";
    banner.textContent = "";
  }
}

function render(state) {
  const ts = state.timestamp || "—";
  const lastUpdateEl = el("last-update");
  if (lastUpdateEl) {
    lastUpdateEl.textContent = ts.replace("T", " ").slice(0, 19);
    lastUpdateEl.className = "text-gray-200 font-mono font-black tracking-[0.2em]";
  }

  const metaAge = state._meta?.file_age_seconds;
  const operational = (state.bot?.status || "OFFLINE") === "OPERATIONAL";
  setStatusDot(operational);

  const lastDataAgeEl = el("last-data-age");
  if (lastDataAgeEl) {
    lastDataAgeEl.textContent = fmtDuration(metaAge);
    lastDataAgeEl.className = "text-gray-300 font-mono font-black tracking-[0.2em]";
  }

  setStaleUI(!operational, metaAge);

  const modeEl = el("mode");
  if (modeEl) {
    modeEl.textContent = state.bot?.mode || "—";
    modeEl.className = "text-blue-300 font-black tracking-[0.2em] uppercase";
  }

  const marketEl = el("market");
  if (marketEl) {
    const marketOpen = state.market?.is_open;
    if (marketOpen === true) {
      marketEl.textContent = "OPEN";
      marketEl.className = "text-green-400 font-black tracking-[0.2em] uppercase";
    } else if (marketOpen === false) {
      marketEl.textContent = "CLOSED";
      marketEl.className = "text-gray-500 font-black tracking-[0.2em] uppercase";
    } else {
      marketEl.textContent = state.bot?.market_status || "—";
      marketEl.className = "text-gray-400 font-black tracking-[0.2em] uppercase";
    }
  }

  const eaBridgeEl = el("ea-bridge-status");
  const eaSpreadEl = el("ea-spread");
  const eaBridge = state.ea_bridge || {};

  if (eaBridgeEl) {
    if (eaBridge.enabled === true) {
      if (eaBridge.online === true) {
        eaBridgeEl.textContent = "ONLINE";
        eaBridgeEl.className = "text-green-400 font-black tracking-[0.2em] uppercase";
      } else {
        eaBridgeEl.textContent = "FALLBACK";
        eaBridgeEl.className = "text-amber-400 font-black tracking-[0.2em] uppercase";
      }
    } else {
      eaBridgeEl.textContent = "OFF";
      eaBridgeEl.className = "text-gray-500 font-black tracking-[0.2em] uppercase";
    }
  }

  if (eaSpreadEl) {
    if (eaBridge.spread_pips != null) {
      const spread = Number(eaBridge.spread_pips);
      eaSpreadEl.textContent = `${spread.toFixed(1)}p`;
      eaSpreadEl.className = spread > 5 ? "font-mono font-black text-amber-400 tracking-[0.2em]" : "font-mono font-black text-gray-400 tracking-[0.2em]";
    } else {
      eaSpreadEl.textContent = "—";
      eaSpreadEl.className = "font-mono font-black text-gray-500 tracking-[0.2em]";
    }
  }

  const la = state.last_analysis || {};
  const hasRealAnalysis = la.decision != null && la.final_score != null;
  const marketClosed = state.market?.is_open === false;

  const card = el("goldcon");

  if (marketClosed) {
    card.className = "relative glass-panel border-gray-600 border-2 rounded-2xl p-6 scanlines transition-all duration-500 shadow-lg";
    const reason = state.market?.reason || "";
    const isPausa = reason.toLowerCase().includes("pausa") || reason.toLowerCase().includes("daily pause");
    el("goldcon-decision").textContent = isPausa ? "DAILY PAUSE" : "MARKET CLOSED";
    el("goldcon-decision").className = "text-3xl sm:text-4xl md:text-5xl font-black leading-none text-gray-400 text-shadow-soft uppercase tracking-[0.2em]";
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
    const decision = la.decision || "HOLD";
    const score = la.final_score;
    const conf = la.confidence;
    const cls = badgeClassByDecision(decision);
    
    // Apply new mockup styling classes to the card
    card.className = `relative ${cls.bg} ${cls.border} backdrop-blur-md border-2 rounded-2xl p-6 shadow-xl overflow-hidden transition-all duration-500 group`;
    
    el("goldcon-decision").textContent = decision;
    el("goldcon-decision").className = `text-4xl sm:text-5xl md:text-6xl font-black leading-none ${cls.text} text-shadow-soft transition-colors duration-300 tracking-[0.2em]`;
    el("goldcon-score").textContent = fmtNum(score, 1);
    el("goldcon-conf").textContent = `${fmtNum(conf, 1)}%`;
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
      blockedEl.className = "text-xs text-amber-400 mt-2 font-black tracking-[0.2em] block uppercase";
    } else {
      blockedEl.textContent = "";
      blockedEl.className = "hidden text-xs text-amber-400 mt-2 font-black tracking-[0.2em] uppercase";
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
  const balEl = el("balance"); if (balEl) balEl.textContent = fmtMoney(bal);
  const eqEl = el("equity"); if (eqEl) eqEl.textContent = fmtMoney(eq);

  const hasPnl = state.daily_stats && state.daily_stats.pnl !== null && state.daily_stats.pnl !== undefined && !Number.isNaN(Number(state.daily_stats.pnl));
  const hasPnlPct = state.daily_stats && state.daily_stats.pnl_percent !== null && state.daily_stats.pnl_percent !== undefined && !Number.isNaN(Number(state.daily_stats.pnl_percent));

  const noClosedTrades = (Number(state.daily_stats?.wins || 0) + Number(state.daily_stats?.losses || 0) + Number(state.daily_stats?.breakevens || 0)) === 0;
  const pnlIsZero = Number(state.daily_stats?.pnl || 0) === 0;
  const pnlEl = el("pnl");

  if (pnlEl) {
    if (!operational || bal === null || bal === undefined || eq === null || eq === undefined || !hasPnl || !hasPnlPct || (noClosedTrades && pnlIsZero)) {
      pnlEl.textContent = "—";
      pnlEl.className = "text-xl font-black text-gray-300 tracking-[0.2em] font-mono";
    } else {
      pnlEl.textContent = `${fmtMoney(state.daily_stats?.pnl)}  (${fmtPct(state.daily_stats?.pnl_percent)})`;
      const pnlVal = Number(state.daily_stats?.pnl || 0);
      pnlEl.className = pnlVal >= 0 ? "text-xl font-black text-green-400 tracking-[0.2em] font-mono" : "text-xl font-black text-red-400 tracking-[0.2em] font-mono";
    }
  }

  const livePrice = la.current_price;
  const lastKnown = state.last_known_price;
  const priceEl = el("price");
  const priceLabelEl = el("price-label");

  if (priceEl) {
    if (livePrice != null) {
      priceEl.textContent = fmtNum(livePrice, 2);
      priceEl.className = "text-2xl font-black text-white tracking-[0.2em] font-mono";
      if (priceLabelEl) priceLabelEl.textContent = "";
    } else if (lastKnown != null) {
      priceEl.textContent = fmtNum(lastKnown, 2);
      priceEl.className = "text-2xl font-black text-gray-400 tracking-[0.2em] font-mono";
      if (priceLabelEl) priceLabelEl.textContent = "LAST";
    } else {
      priceEl.textContent = "—";
      priceEl.className = "text-2xl font-black text-gray-500 tracking-[0.2em] font-mono";
      if (priceLabelEl) priceLabelEl.textContent = "";
    }
  }

  renderPillar("p-tech", la.tech_score);
  renderPillar("p-ml", la.ml_score);
  renderPillar("p-mom", la.momentum_score);
  renderPillar("p-news", la.news_score);
  renderPillar("p-cal", la.calendar_score);

  renderPositions(state.positions);
  renderTrades(state.trade_history, state.daily_stats);
  renderIntelFeed(la.intel_feed, la.mtf_trend, la.volume_gate);
  renderAgentCard(la.agent_decision);
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
  return "";
}

function macroLabel(key) {
  if (key === "dxy") return "DXY";
  if (key === "yields") return "YIELDS 10Y";
  if (key === "vix") return "VIX";
  return key.toUpperCase();
}

function macroUnit(key) {
  if (key === "yields") return "%";
  return "";
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
  };
  const m = map[cat] || { label: cat || "?", cls: "bg-gray-600/30 text-gray-400" };
  return `<span class="px-1 py-0.5 rounded text-[9px] font-black uppercase tracking-[0.2em] ${m.cls}">${m.label}</span>`;
}

function renderIntelFeed(feed, mtfTrend, volumeGate) {
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
            <div class="text-xs text-gray-200 leading-snug truncate font-black tracking-[0.2em] uppercase" title="${h.title.replace(/"/g, '&quot;')}">${titleTrunc}</div>
            <div class="flex items-center gap-2 mt-1.5 text-[10px] text-gray-500 font-mono">
              ${categoryBadge(h.category)}
              <span>${age} ago</span>
            </div>
          </div>
          <div class="flex-shrink-0 flex items-center">
            <div class="intel-robot-bubble">
              <img src="/image/flokiwatch.png" alt="Floki" class="floki-intel-mini shadow-md ring-1 ring-white/10">
              <div class="intel-bubble backdrop-blur-md bg-gray-900/90 border-gray-700/50">
                <div class="text-xs font-black ${sc.text} font-mono tracking-[0.2em]">${fmtNum(h.score, 0)}/100</div>
                <div class="text-[10px] font-black tracking-[0.2em] uppercase text-gray-400 mt-0.5">${sc.label}</div>
                <div class="text-[9px] text-gray-500 mt-1 uppercase tracking-[0.2em]">via ${methodTag}</div>
              </div>
            </div>
          </div>
        </div>
      `);
    }
  }

  // Macro cards
  const macroContainer = el("intel-macro");
  macroContainer.innerHTML = "";
  const macro = feed.macro || {};
  for (const key of ["dxy", "yields", "vix"]) {
    const m = macro[key];
    if (!m) continue;
    const val = m.value;
    const chg = m.change_pct;
    const sc = intelScoreColor(m.score);
    const impact = macroImpactText(key, chg);
    const arrow = Number(chg) > 0 ? "&#9650;" : (Number(chg) < 0 ? "&#9660;" : "");
    const chgClass = Number(chg) > 0 ? "text-green-400" : (Number(chg) < 0 ? "text-red-400" : "text-gray-400");
    const unit = macroUnit(key);

    macroContainer.insertAdjacentHTML("beforeend", `
      <div class="glass-panel border ${sc.border} rounded-xl p-3 shadow-md hover:bg-gray-800/60 transition-colors duration-300 intel-macro-card relative overflow-hidden group">
        <div class="absolute inset-0 bg-gradient-to-br from-white/5 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-300 pointer-events-none"></div>
        <div class="flex items-center justify-between relative z-10">
          <div class="text-[10px] font-black text-gray-400 tracking-[0.2em] uppercase">${macroLabel(key)}</div>
          <div class="text-xs font-black font-mono ${sc.text} tracking-[0.2em]">${fmtNum(m.score, 0)}</div>
        </div>
        <div class="flex items-baseline gap-2 mt-2 relative z-10">
          <span class="text-lg text-gray-100 font-black font-mono tracking-[0.2em]">${val != null ? fmtNum(val, 2) + unit : "—"}</span>
          <span class="text-xs font-mono font-black ${chgClass} tracking-[0.2em]">${arrow} ${chg != null ? (Number(chg) > 0 ? "+" : "") + Number(chg).toFixed(2) + "%" : "—"}</span>
        </div>
        <div class="intel-robot-bubble inline-flex mt-2 relative z-10">
          <img src="/image/flokiwatch.png" alt="Floki" class="floki-intel-mini shadow-sm ring-1 ring-white/10">
          <div class="intel-bubble backdrop-blur-md bg-gray-900/90 border-gray-700/50 p-2">
            <div class="text-xs text-gray-300 font-black uppercase tracking-[0.2em]">${impact}</div>
          </div>
        </div>
      </div>
    `);
  }

  // Anomalies
  const anomalies = feed.anomalies || [];
  if (anomalies.length > 0) {
    macroContainer.insertAdjacentHTML("beforeend", `
      <div class="text-xs text-amber-400 mt-1">${anomalies.map(a => "&#9888; " + a).join("<br>")}</div>
    `);
  }

  // Calendar
  const calEl = el("intel-calendar");
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
    let calHtml = `
      <div class="flex items-center gap-2">
        <span class="${phaseColor} font-black tracking-[0.2em] uppercase">${cal.phase.toUpperCase().replace("_", " ")}</span>
        <span class="${biasColor} font-black tracking-[0.2em] uppercase">${biasIcon} ${cal.bias}</span>
      </div>
      ${cal.closest_event ? `<div class="text-gray-500 mt-1 font-black tracking-[0.2em] uppercase text-[10px]">${cal.closest_event}</div>` : ""}
      ${cal.phase_description ? `<div class="text-gray-600 mt-1 text-[10px] font-black uppercase tracking-[0.2em] leading-tight">${cal.phase_description}</div>` : ""}
    `;

    // Upcoming events list
    const upcoming = cal.upcoming_events || [];
    if (upcoming.length > 0) {
      calHtml += `<div class="mt-3 border-t border-gray-800 pt-2">`;
      calHtml += `<div class="text-[10px] text-gray-600 font-black tracking-[0.2em] mb-2 uppercase">UPCOMING</div>`;
      for (const ev of upcoming) {
        const impColor = ev.importance === "HIGH" ? "text-red-400 border-red-800/50" : "text-yellow-500 border-yellow-800/50";
        const impLabel = ev.importance === "HIGH" ? "H" : "M";
        const timeColor = ev.is_past ? "text-gray-700" : "text-gray-500";
        const nameColor = ev.is_past ? "text-gray-700" : "text-gray-400";
        calHtml += `
          <div class="flex items-center gap-2 text-[10px] py-0.5 font-black uppercase tracking-[0.2em]">
            <span class="px-1 border rounded ${impColor}" style="font-size:8px">${impLabel}</span>
            <span class="${timeColor} font-mono">${ev.time}</span>
            <span class="${nameColor} truncate flex-1">${ev.name}</span>
            <span class="text-gray-600 ml-auto flex-shrink-0 text-[9px]">${ev.time_until}</span>
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
  }

  // S/R Zones
  const srEl = el("intel-sr");
  const srContainer = el("intel-sr-zones");
  const srLegend = el("intel-sr-legend");
  const srZones = feed.sr_zones || [];
  const tfBadge = (tf) => {
    const colors = { "D1": "bg-amber-700 text-amber-200", "H4": "bg-blue-800 text-blue-300", "H1": "bg-gray-700 text-gray-400" };
    return `<span class="px-1 py-0.5 rounded text-[10px] font-black uppercase tracking-[0.2em] ${colors[tf] || colors.H1}">${tf}</span>`;
  };
  const mtfBadge = (confluence) => {
    if (!confluence || confluence.length < 2) return "";
    return `<span class="px-1 py-0.5 rounded text-[10px] font-black bg-purple-800 text-purple-300 ml-0.5 uppercase tracking-[0.2em]" title="${confluence.join("+")}">MTF</span>`;
  };
  const ztBadge = (zt) => {
    const ztColors = { "FLIP": "bg-yellow-800 text-yellow-300", "SUPPORT": "bg-green-900 text-green-400", "RESISTANCE": "bg-red-900 text-red-400" };
    const label = zt === "SUPPORT" ? "SUP" : zt === "RESISTANCE" ? "RES" : zt;
    return `<span class="px-1 py-0.5 rounded text-[10px] font-black uppercase tracking-[0.2em] ${ztColors[zt] || "bg-gray-700 text-gray-400"} ml-0.5">${label}</span>`;
  };
  const zoneRow = (z, color, borderColor) => {
    const isMtf = z.confluence && z.confluence.length >= 2;
    const isStrong = z.strength === "strong" || z.touches >= 4;
    const weight = isStrong ? "font-black" : "font-black";
    const border = isMtf ? "border-purple-500" : borderColor;
    return `<div class="${color} ${weight} border-l-2 ${border} pl-1.5 py-0.5 uppercase tracking-[0.2em]">${z.price.toFixed(2)} ${tfBadge(z.timeframe)}${mtfBadge(z.confluence)}${ztBadge(z.zone_type)} <span class="text-gray-500 tracking-normal">${z.touches}T — ${z.dist_pips.toFixed(0)}p</span></div>`;
  };
  if (srZones.length > 0) {
    srEl.classList.remove("hidden");
    if (srLegend) srLegend.classList.remove("hidden");
    srContainer.innerHTML = "";
    const above = srZones.filter(z => z.position === "above");
    const below = srZones.filter(z => z.position === "below");
    // Resistance (above) — right column
    let aboveHtml = '<div class="w-full md:w-1/2"><div class="text-red-400 font-black tracking-[0.2em] uppercase mb-1">&#9650; RESISTANCE</div>';
    if (above.length === 0) { aboveHtml += '<div class="text-gray-600">None nearby</div>'; }
    for (const z of above) { aboveHtml += zoneRow(z, "text-red-300", "border-red-700"); }
    aboveHtml += '</div>';
    // Support (below) — left column
    let belowHtml = '<div class="w-full md:w-1/2 md:pr-3 md:border-r border-gray-700/50"><div class="text-green-400 font-black tracking-[0.2em] uppercase mb-1">&#9660; SUPPORT</div>';
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
          <div class="text-[10px] font-black tracking-[0.2em] uppercase ${textCls} mb-1 sm:mb-1.5 font-mono">GPT ${action}${adjStr}</div>
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
          <span class="px-1.5 py-0.5 border rounded font-black uppercase tracking-[0.2em] ${dirColor}">${p.name}</span>
          <span class="${scoreColor} font-mono font-black tracking-[0.2em]">${p.final_score > 0 ? "+" : ""}${p.final_score}</span>
          ${multText ? `<span class="text-amber-400 text-[10px] font-black uppercase tracking-[0.2em]">${multText} S/R</span>` : ""}
          ${srText ? `<span class="text-gray-500 text-[10px] truncate max-w-xs font-black uppercase tracking-[0.2em]">${srText}</span>` : ""}
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
      decisionEl.className = "text-2xl font-black text-green-400 tracking-[0.2em] uppercase";
    } else if (decision.includes("SELL")) {
      decisionEl.className = "text-2xl font-black text-red-400 tracking-[0.2em] uppercase";
    } else if (decision === "REJECT") {
      decisionEl.className = "text-2xl font-black text-red-500 tracking-[0.2em] uppercase";
    } else if (decision === "WAIT") {
      decisionEl.className = "text-2xl font-black text-yellow-400 tracking-[0.2em] uppercase";
    } else {
      decisionEl.className = "text-2xl font-black text-gray-400 tracking-[0.2em] uppercase";
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
      agreeEl.className = "text-sm font-black text-green-400 tracking-[0.2em] uppercase";
    } else if (agreement === false) {
      agreeEl.textContent = "❌ DISAGREE";
      agreeEl.className = "text-sm font-black text-red-400 tracking-[0.2em] uppercase";
    } else {
      agreeEl.textContent = "—";
      agreeEl.className = "text-sm font-black text-gray-500 tracking-[0.2em] uppercase";
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
      factorsEl.innerHTML = keyFactors.map(f => `<li class="text-gray-300 font-black tracking-[0.2em] uppercase text-[10px]">• ${f}</li>`).join("");
    } else {
      factorsEl.innerHTML = `<li class="text-gray-600 font-black tracking-[0.2em] uppercase text-[10px]">—</li>`;
    }
  }

  // Concerns
  const concernsEl = el("agent-concerns");
  if (concernsEl) {
    if (concerns.length > 0) {
      concernsEl.innerHTML = concerns.map(c => `<li class="text-amber-400 font-black tracking-[0.2em] uppercase text-[10px]">• ${c}</li>`).join("");
    } else {
      concernsEl.innerHTML = `<li class="text-gray-600 font-black tracking-[0.2em] uppercase text-[10px]">NONE</li>`;
    }
  }
}

/* ================================================================
   PROACTIVE ANALYSIS (H1 Snapshot)
   ================================================================ */

function renderProactiveAnalysis(proactive, positions) {
  const section = el("proactive-section");
  if (!section) return;

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

  const sentimentBarEl = el("sentiment-bar");
  const sentimentLabelEl = el("sentiment-label");

  const lifecycleBarEl = el("lifecycle-bar");
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
  const decisionCardEl = el("proactive-decision-card");
  if (decisionEl) {
    decisionEl.textContent = decisionLabel(decision);
    decisionEl.style.color = decisionHexColor(decision);
  }
  if (decisionCardEl) {
    const hexColor = decisionHexColor(decision);
    decisionCardEl.style.borderColor = hexColor.replace(")", ", 0.3)").replace("rgb", "rgba").replace("#4caf50","rgba(76,175,80,0.3)").replace("#e74c3c","rgba(231,76,60,0.3)").replace("#2ecc71","rgba(46,204,113,0.3)").replace("#e67e22","rgba(230,126,34,0.3)").replace("#f1c40f","rgba(241,196,15,0.3)").replace("#8e8e8e","rgba(142,142,142,0.15)");
    const glowMap = {
      "#4caf50": "0 0 32px rgba(76,175,80,0.12), 0 8px 32px rgba(0,0,0,0.4)",
      "#e74c3c": "0 0 32px rgba(231,76,60,0.12), 0 8px 32px rgba(0,0,0,0.4)",
      "#2ecc71": "0 0 32px rgba(46,204,113,0.12), 0 8px 32px rgba(0,0,0,0.4)",
      "#e67e22": "0 0 32px rgba(230,126,34,0.12), 0 8px 32px rgba(0,0,0,0.4)",
      "#f1c40f": "0 0 32px rgba(241,196,15,0.12), 0 8px 32px rgba(0,0,0,0.4)",
    };
    decisionCardEl.style.boxShadow = glowMap[decisionHexColor(decision)] || "0 8px 32px rgba(0,0,0,0.4)";
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
    confEl.textContent = confidence != null ? `${confidence}%` : "—%";
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

    if (sentimentBarEl && sentimentLabelEl) {
      const d = (decision || "").toString().toUpperCase();

      let label = "NEUTRAL";
      let activeIdx = 2; // Default to Neutral (middle of 5 zones)
      const c = confSafe;

      if (d === "OPEN_SELL") {
        if (c != null && c >= 75) { label = "STRONG SELL"; activeIdx = 0; }
        else { label = "SELL"; activeIdx = 1; }
      } else if (d === "OPEN_BUY") {
        if (c != null && c >= 75) { label = "STRONG BUY"; activeIdx = 4; }
        else { label = "BUY"; activeIdx = 3; }
      } else if (d === "WAIT" || d === "CLOSE_TRADE") {
        label = "NEUTRAL";
        activeIdx = 2;
      } else if (d === "HOLD_TRADE" || d === "ADJUST_TRADE") {
        const dirFromPos = (pos0?.direction || "").toString().toUpperCase();
        if (dirFromPos === "SELL") {
          if (c != null && c >= 75) { label = "STRONG SELL"; activeIdx = 0; }
          else { label = "SELL"; activeIdx = 1; }
        } else if (dirFromPos === "BUY") {
          if (c != null && c >= 75) { label = "STRONG BUY"; activeIdx = 4; }
          else { label = "BUY"; activeIdx = 3; }
        }
      }

      sentimentLabelEl.textContent = label;
      sentimentLabelEl.className = `text-xs font-black tracking-[0.2em] uppercase min-w-[60px] text-right ${activeIdx <= 1 ? 'text-red-400' : (activeIdx >= 3 ? 'text-emerald-400' : 'text-amber-400')}`;

      const segs = sentimentBarEl.children;
      const numSegs = segs.length; // 16
      
      for (let i = 0; i < numSegs; i++) {
        const seg = segs[i];
        if (!seg) continue;
        
        // Map 5 zones to 16 segments
        // 0: Strong Sell (0-2), 1: Sell (3-5), 2: Neutral (6-9), 3: Buy (10-12), 4: Strong Buy (13-15)
        let isInZone = false;
        if (activeIdx === 0) isInZone = (i <= 2);
        else if (activeIdx === 1) isInZone = (i >= 3 && i <= 5);
        else if (activeIdx === 2) isInZone = (i >= 6 && i <= 9);
        else if (activeIdx === 3) isInZone = (i >= 10 && i <= 12);
        else if (activeIdx === 4) isInZone = (i >= 13);

        if (isInZone) {
          seg.classList.add("is-active");
          // Determine color class based on zone
          seg.classList.remove("zone-bearish", "zone-neutral", "zone-bullish");
          if (activeIdx <= 1) seg.classList.add("zone-bearish");
          else if (activeIdx === 2) seg.classList.add("zone-neutral");
          else seg.classList.add("zone-bullish");

          if ((activeIdx === 0 && i === 2) || (activeIdx === 1 && i === 5) || 
              (activeIdx === 2 && i === 9) || (activeIdx === 3 && i === 12) || 
              (activeIdx === 4 && i === 15)) {
            seg.classList.add("last-active");
          } else {
            seg.classList.remove("last-active");
          }
        } else {
          seg.classList.remove("is-active", "last-active", "zone-bearish", "zone-neutral", "zone-bullish");
        }
      }
    }

    if (lifecycleBarEl && lifecycleLabelEl) {
      const d = (decision || "").toString().toUpperCase();
      const entryConditionsPresent = !!(toRender.entry_conditions && typeof toRender.entry_conditions === "object");

      let step = "WATCHING";
      let stepIdx = 0; // 0: Watching, 1: Preparing, 2: Entry, 3: Managing, 4: Closing, 5: Result

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
      }

      if (!inTrade && lastProactiveDecision === "CLOSE_TRADE" && Number.isFinite(lastKnownClosedPnl)) {
        step = "RESULT";
        stepIdx = 5;
      }

      lifecycleLabelEl.textContent = step;
      lifecycleLabelEl.className = "text-xs font-black tracking-[0.2em] uppercase min-w-[60px] text-right text-purple-400";

      const segs = lifecycleBarEl.children;
      const numSegs = segs.length; // 16
      // Progressive mapping: fill segments up to current step
      // Step 0: 2 segs, Step 1: 5 segs, Step 2: 8 segs, Step 3: 11 segs, Step 4: 14 segs, Step 5: 16 segs
      const mapping = [2, 5, 8, 11, 14, 16];
      const activeSegsCount = mapping[stepIdx];

      for (let i = 0; i < numSegs; i++) {
        const seg = segs[i];
        if (!seg) continue;
        if (i < activeSegsCount) {
          seg.classList.add("is-active");
          if (i === activeSegsCount - 1) {
            seg.classList.add("last-active");
            if (stepIdx === 5) {
               const isLoss = Number.isFinite(lastKnownClosedPnl) && lastKnownClosedPnl < 0;
               seg.classList.add(isLoss ? "step-result-loss" : "step-result-win");
            }
          } else {
            seg.classList.remove("last-active");
          }
        } else {
          seg.classList.remove("is-active", "last-active", "step-result-win", "step-result-loss");
        }
      }
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
      viewDirEl.className = "text-lg font-black text-green-400 tracking-[0.2em] uppercase";
    } else if (dir === "SELL") {
      viewDirEl.className = "text-lg font-black text-red-400 tracking-[0.2em] uppercase";
    } else {
      viewDirEl.className = "text-lg font-black text-yellow-400 tracking-[0.2em] uppercase";
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
      conditionsEl.innerHTML = `<li class="text-gray-500 font-black tracking-[0.2em] uppercase text-[10px]">No conditions set</li>`;
    } else {
      conditionsEl.innerHTML = conditions.map(c => {
        const icon = c.met ? "✅" : "❌";
        const currentVal = c.current_value != null ? ` (now: ${fmtNum(c.current_value, 1)})` : "";
        const textClass = c.met ? "text-green-400" : "text-gray-300";
        return `<li class="${textClass} font-black tracking-[0.2em] uppercase text-[10px] leading-relaxed">${icon} ${c.description}${currentVal}</li>`;
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
      statusEl.className = "px-2 py-0.5 rounded text-[10px] font-black bg-green-500/20 text-green-400 border border-green-500/30 animate-pulse tracking-[0.2em] uppercase";
    } else if (status === "EXPIRED") {
      statusEl.textContent = "EXPIRED";
      statusEl.className = "px-2 py-0.5 rounded text-[10px] font-black bg-gray-500/20 text-gray-400 border border-gray-500/30 tracking-[0.2em] uppercase";
    } else {
      statusEl.textContent = "ACTIVE";
      statusEl.className = "px-2 py-0.5 rounded text-[10px] font-black bg-amber-500/20 text-amber-400 border border-amber-500/30 tracking-[0.2em] uppercase";
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
    container.innerHTML = `<span class="text-gray-500 font-black tracking-[0.2em] text-[11px] uppercase">NO DECISIONS YET</span>`;
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
        <span class="text-[10px] text-gray-500 font-mono tracking-[0.2em] opacity-80 uppercase font-black">${time}</span>
        <span class="font-black text-[10px] tracking-[0.2em] uppercase">${decision}</span>
        <span class="text-[10px] font-mono opacity-90 font-black pl-1 border-l border-current/20 tracking-[0.2em]">${score}</span>
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

setInterval(poll, 3000);
setInterval(pollDecisions, 10000);
poll();
pollDecisions();
