/**
 * Smoothly animates numeric text in an element
 */
function animateValue(id, start, end, duration, formatter = (v) => v.toString()) {
  const obj = el(id);
  if (!obj) return;
  
  // If end is not a number, just set it and return
  if (end === null || end === undefined || isNaN(Number(end))) {
    obj.textContent = end || "—";
    return;
  }

  const startNum = parseFloat(start) || 0;
  const endNum = parseFloat(end);
  
  if (startNum === endNum) {
    obj.textContent = formatter(endNum);
    return;
  }

  const range = endNum - startNum;
  let startTime = null;

  function step(timestamp) {
    if (!startTime) startTime = timestamp;
    const progress = Math.min((timestamp - startTime) / duration, 1);
    const current = startNum + (range * progress);
    obj.textContent = formatter(current);
    if (progress < 1) {
      window.requestAnimationFrame(step);
    }
  }
  window.requestAnimationFrame(step);
}

function updateVUMeter(containerId, activeCount) {
  const container = el(containerId);
  if (!container) return;
  const segments = container.children;
  const count = Math.max(0, Math.min(segments.length, activeCount));
  
  for (let i = 0; i < segments.length; i++) {
    const seg = segments[i];
    if (i < count) {
      // Delay each segment activation for a "filling" effect
      setTimeout(() => {
        seg.classList.add("active");
        if (i === count - 1 && count > 0) {
          seg.classList.add("pulse");
        } else {
          seg.classList.remove("pulse");
        }
      }, i * 30);
    } else {
      seg.classList.remove("active", "pulse");
    }
  }
}

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
  if (d === "OPEN_BUY") return "#10b981"; // Emerald
  if (d === "OPEN_SELL") return "#ef4444"; // Red
  if (d === "HOLD_TRADE") return "#10b981"; // Emerald
  if (d === "CLOSE_TRADE") return "#f59e0b"; // Amber
  if (d === "ADJUST_TRADE") return "#3b82f6"; // Blue
  if (d === "WAIT") return "#f59e0b"; // Amber
  return "#9ca3af"; // Gray
}

function toggleBrainReferencePanel() {
  const panel = el("brain-reference-panel");
  const btn = el("brain-toggle");
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
      if (direction === "BUY") return { cls: "bg-emerald-500/10 border-emerald-500/30 text-emerald-400", icon: "▲", glow: "glow-emerald-pulse" };
      if (direction === "SELL") return { cls: "bg-red-500/10 border-red-500/30 text-red-400", icon: "▼", glow: "glow-red-pulse" };
    }
    if (execType === "CLOSE") return { cls: "bg-amber-500/10 border-amber-500/30 text-amber-400", icon: "×", glow: "glow-amber-pulse" };
    if (execType === "ADJUST") return { cls: "bg-blue-500/10 border-blue-500/30 text-blue-400", icon: "±", glow: "glow-blue-pulse" };
    return { cls: "bg-amber-500/10 border-amber-500/30 text-amber-400", icon: "!", glow: "glow-amber-pulse" };
  }

  if (action === "HOLD") return { cls: "bg-gray-500/10 border-white/10 text-gray-400", icon: "●", glow: "" };
  if (action === "DISMISS") return { cls: "bg-gray-500/10 border-white/5 text-gray-500", icon: "○", glow: "" };
  return { cls: "bg-gray-500/10 border-white/10 text-gray-400", icon: "●", glow: "" };
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
    container.innerHTML = `<span class="text-gray-600 font-medium italic">SCANNING FOR SIGNALS...</span>`;
    return;
  }

  const now = Date.now();
  const items = fastDecisions.slice(0, 4);
  container.innerHTML = items.map((fd, idx) => {
    const action = (fd?.action || "").toString().toUpperCase() || "HOLD";
    const reason = (fd?.reason || "").toString().trim();
    const exec = fd?.execution || {};
    const execType = (exec?.type || "").toString().toUpperCase();
    const dir = (exec?.direction || "").toString().toUpperCase();
    const entry = exec?.entry ?? exec?.entry_price;

    let suffix = "";
    if (execType === "OPEN") {
      if (entry != null && Number.isFinite(Number(entry))) suffix = `@ ${fmtNum(entry, 1)}`;
      else suffix = dir ? `${dir}` : "OPEN";
    } else if (execType === "CLOSE") {
      suffix = "CLOSE";
    } else if (execType === "ADJUST") {
      suffix = "ADJ";
    }

    let ageText = "—";
    try {
      const ts = fd?.timestamp ? Date.parse(fd.timestamp) : NaN;
      const ageS = Number.isFinite(ts) ? (now - ts) / 1000 : NaN;
      ageText = fmtAgeShort(ageS);
    } catch (e) {
      ageText = "—";
    }

    const delay = idx * 100;

    return `
      <div class="px-2.5 py-1 rounded-lg border backdrop-blur-md flex items-center gap-2 trigger-entry ${style.cls} ${style.glow}" style="animation-delay: ${delay}ms">
        <span class="text-[10px] font-black opacity-80">${style.icon}</span>
        <span class="font-black tracking-widest-caps uppercase" style="font-size: 8px;">${action}</span>
        <span class="w-px h-2.5 bg-current opacity-20"></span>
        <span class="font-bold tracking-tight opacity-90 truncate max-w-[90px] uppercase" style="font-size: 9px;">${reason || suffix}</span>
        <span class="text-[8px] font-mono font-black opacity-40 ml-1 italic">${ageText}</span>
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
  if (d.includes("BUY")) return { border: "border-emerald-500", bg: "bg-emerald-500/10", text: "text-emerald-400", glow: "glow-emerald-pulse" };
  if (d.includes("SELL")) return { border: "border-red-500", bg: "bg-red-500/10", text: "text-red-400", glow: "glow-red-pulse" };
  return { border: "border-amber-500", bg: "bg-amber-500/10", text: "text-amber-400", glow: "glow-amber-pulse" };
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
    dot.className = "w-2 h-2 rounded-full bg-emerald-400 animate-pulse shadow-[0_0_8px_rgba(16,185,129,0.5)]";
    label.textContent = "OPERATIONAL";
    label.className = "text-emerald-400 font-bold";
  } else {
    dot.className = "w-2 h-2 rounded-full bg-red-500 animate-pulse shadow-[0_0_8px_rgba(239,68,68,0.5)]";
    label.textContent = "OFFLINE";
    label.className = "text-red-400 font-bold";
  }
}

function renderPositions(positions) {
  const container = el("positions");
  const prevContent = container.innerHTML;
  container.innerHTML = "";

  if (!positions || positions.length === 0) {
    container.innerHTML = `<div class="text-xs text-gray-500 italic py-4 text-center border border-dashed border-white/5 rounded-xl">NO ACTIVE POSITIONS</div>`;
    el("positions-count").textContent = "0";
    return;
  }

  el("positions-count").textContent = String(positions.length);

  positions.forEach((p, idx) => {
    const pnl = Number(p.profit);
    const pnlClass = pnl >= 0 ? "text-emerald-400" : "text-red-400";
    const delay = idx * 100;
    
    // Phase badge styling
    const phase = p.phase || "OPEN";
    let phaseBadge = "";
    if (phase === "TRAILING") {
      phaseBadge = `<span class="ml-2 px-1.5 py-0.5 rounded text-[9px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 shadow-[0_0_8px_rgba(16,185,129,0.2)]">TRAILING</span>`;
    } else if (phase === "BREAKEVEN") {
      phaseBadge = `<span class="ml-2 px-1.5 py-0.5 rounded text-[9px] font-bold bg-amber-500/10 text-amber-400 border border-amber-500/20 shadow-[0_0_8px_rgba(245,158,11,0.2)]">BE ACTIVE</span>`;
    } else {
      phaseBadge = `<span class="ml-2 px-1.5 py-0.5 rounded text-[9px] font-bold bg-blue-500/10 text-blue-400 border border-blue-500/20">OPEN</span>`;
    }

    // BE distance indicator
    let beIndicator = "";
    if (phase === "OPEN" && p.be_remaining_pips != null) {
      beIndicator = `<span class="text-[9px] text-gray-500 font-mono">BE IN ${fmtNum(p.be_remaining_pips, 0)}P</span>`;
    } else if (phase === "BREAKEVEN" || phase === "TRAILING") {
      beIndicator = `<span class="text-[9px] text-emerald-500 font-mono font-bold">BE ✓</span>`;
    }

    container.insertAdjacentHTML(
      "beforeend",
      `
      <div class="glass-panel-deep border border-white/5 rounded-xl p-3 hover:bg-white/5 transition-all duration-300 group trigger-entry" style="animation-delay: ${delay}ms">
        <div class="flex items-center justify-between mb-3">
          <div class="flex items-center gap-2">
            <span class="text-gray-600 font-mono text-[10px]">#${p.ticket}</span>
            <span class="px-1.5 py-0.5 rounded text-[9px] font-bold border border-white/10 text-gray-300 bg-black/40">${p.direction}</span> 
            ${phaseBadge}
          </div>
          <div class="text-xs font-mono font-bold ${pnlClass} bg-black/40 px-2 py-1 rounded border border-white/5 shadow-inner">
            ${fmtMoney(pnl)} <span class="text-[9px] text-gray-500 font-medium ml-1">(${fmtNum(p.profit_pips, 0)}P)</span>
          </div>
        </div>
        <div class="grid grid-cols-2 md:grid-cols-5 gap-2 text-[9px] font-mono font-bold text-gray-500 uppercase tracking-widest bg-black/40 rounded-lg p-2 border border-white/5">
          <div class="flex flex-col"><span class="text-gray-600 mb-0.5">ENTRY</span><span class="text-gray-300">${fmtNum(p.open_price, 2)}</span></div>
          <div class="flex flex-col"><span class="text-gray-600 mb-0.5">SL</span><span class="text-gray-300">${fmtNum(p.sl, 2)}</span></div>
          <div class="flex flex-col"><span class="text-gray-600 mb-0.5">TP</span><span class="text-gray-300">${fmtNum(p.tp, 2)}</span></div>
          <div class="flex flex-col"><span class="text-gray-600 mb-0.5">NOW</span><span class="text-gray-100">${fmtNum(p.current_price, 2)}</span></div>
          <div class="flex flex-col"><span class="text-gray-600 mb-0.5">GUARD</span>${beIndicator}</div>
        </div>
      </div>
      `
    );
  });
}

function renderTrades(trades, daily) {
  const container = el("trades");
  container.innerHTML = "";

  const wins = Number(daily?.wins || 0);
  const losses = Number(daily?.losses || 0);
  const breakevens = Number(daily?.breakevens || 0);
  const decisive = wins + losses;
  const wr = decisive ? (wins / decisive) * 100 : 0;

  // Smooth count for daily stats
  const prevW = parseInt(el("trades-w").textContent) || 0;
  const prevL = parseInt(el("trades-l").textContent) || 0;
  const prevBE = parseInt(el("trades-be").textContent) || 0;
  
  animateValue("trades-w", prevW, wins, 800, (v) => Math.round(v).toString());
  animateValue("trades-l", prevL, losses, 800, (v) => Math.round(v).toString());
  animateValue("trades-be", prevBE, breakevens, 800, (v) => Math.round(v).toString());
  el("trades-wr").textContent = `${wr.toFixed(1)}%`;
  
  const hasDailyPnl = daily && daily.pnl !== null && daily.pnl !== undefined && !Number.isNaN(Number(daily.pnl));
  const prevDailyPnl = parseFloat(el("trades-pnl").textContent.replace("$", "")) || 0;
  animateValue("trades-pnl", prevDailyPnl, hasDailyPnl ? daily.pnl : 0, 800, fmtMoney);

  if (!trades || trades.length === 0) {
    container.innerHTML = `<div class="text-[10px] text-gray-600 italic py-4 text-center border border-dashed border-white/5 rounded-xl uppercase tracking-widest">SESSION HISTORY EMPTY</div>`;
    return;
  }

  const sorted = [...trades].sort((a, b) => (b.close_time || "").localeCompare(a.close_time || ""));

  sorted.slice(0, 10).forEach((t, idx) => {
    const isPending = t.pending === true || t.profit === null || t.profit === undefined;
    const pnl = isPending ? 0 : Number(t.profit);
    const time = (t.close_time || "").split("T")[1]?.slice(0, 5) || "—";
    const delay = idx * 50;

    let displayReason;
    if (t.close_type) {
      const typeMap = { tp: "TP", trailing: "TRL", breakeven: "BE", sl: "SL" };
      displayReason = typeMap[t.close_type] || t.reason || "";
    } else {
      displayReason = t.reason || "";
      if (displayReason.toLowerCase().includes("stop loss")) {
        if (pnl > 1.0) displayReason = "TRL";
        else if (pnl >= 0) displayReason = "BE";
        else displayReason = "SL";
      }
    }

    let pnlDisplay, pnlClass, icon;
    if (isPending) {
      pnlDisplay = `<span class="text-amber-400 animate-pulse">PROC...</span>`;
      pnlClass = "text-amber-400";
      icon = "WAIT";
    } else {
      pnlClass = pnl > 0 ? "text-emerald-400" : (pnl < 0 ? "text-red-400" : "text-gray-500");
      pnlDisplay = fmtMoney(pnl);
      icon = pnl > 0 ? "WIN" : (pnl < 0 ? "LOSS" : "BE");
    }

    container.insertAdjacentHTML(
      "beforeend",
      `
      <div class="flex items-center justify-between text-[10px] font-mono border-b border-white/5 py-2 group trigger-entry" style="animation-delay: ${delay}ms">
        <div class="text-gray-600 font-bold w-10">${time}</div>
        <div class="text-gray-400 font-bold w-12">${t.direction || "—"}</div>
        <div class="text-gray-500 truncate flex-1 px-2 font-bold tracking-tighter opacity-70">${displayReason}</div>
        <div class="${pnlClass} font-bold w-20 text-right">${pnlDisplay}</div>
        <div class="text-[8px] uppercase tracking-widest font-black px-1.5 py-0.5 rounded bg-white/5 border border-white/5 text-gray-500 ml-2 w-10 text-center">${icon}</div>
      </div>
      `
    );
  });
}

function renderVolBanner(volStatus, volDesc) {
  const banner = el("vol-banner");
  if (!banner) return;
  const status = (volStatus || "NORMAL").toUpperCase();

  if (status === "EXTREME") {
    banner.className = "block bg-red-600/20 backdrop-blur-md border-b border-red-500/50 text-red-200 px-4 py-2 text-[10px] font-black tracking-[0.2em] text-center animate-pulse uppercase";
    banner.innerHTML = `<span class="bg-red-500 text-white px-1.5 py-0.5 rounded mr-2">CRITICAL</span> EXTREME VOLATILITY DETECTED — TRADING HALTED. ${volDesc || ""}`;
  } else if (status === "COOLING_DOWN") {
    banner.className = "block bg-amber-600/20 backdrop-blur-md border-b border-amber-500/50 text-amber-200 px-4 py-2 text-[10px] font-black tracking-[0.2em] text-center uppercase";
    banner.innerHTML = `<span class="bg-amber-500 text-black px-1.5 py-0.5 rounded mr-2">ADVISORY</span> COOLING DOWN — AGGRESSIVE FILTERS ACTIVE. ${volDesc || ""}`;
  } else {
    banner.className = "hidden";
    banner.innerHTML = "";
  }
}

function render(state) {
  const ts = state.timestamp || "—";
  el("last-update").textContent = ts.replace("T", " ").slice(0, 19);

  const metaAge = state._meta?.file_age_seconds;
  const ageSeconds = Number(metaAge);
  const hasValidAge = Number.isFinite(ageSeconds) && ageSeconds >= 0;
  const isStale = hasValidAge ? ageSeconds > 60 : true;
  const operational = !isStale;

  setStatusDot(operational);
  el("last-data-age").textContent = fmtDuration(metaAge);
  setStaleUI(isStale, metaAge);

  const modeEl = el("mode");
  if (modeEl) modeEl.textContent = state.bot?.mode || "DEMO";

  const marketOpen = state.market?.is_open;
  const marketLabel = el("market");
  if (marketOpen === true) {
    marketLabel.textContent = "OPEN";
    marketLabel.className = "text-emerald-400 font-black tracking-widest-caps";
  } else if (marketOpen === false) {
    marketLabel.textContent = "CLOSED";
    marketLabel.className = "text-white/40 font-black tracking-widest-caps";
  } else {
    marketLabel.textContent = "UNKNOWN";
    marketLabel.className = "text-white/20 font-black tracking-widest-caps";
  }

  // EA Bridge status
  const eaStatus = el("ea-bridge-status");
  const eaSpread = el("ea-spread");
  const eaBridge = state.ea_bridge || {};
  
  if (eaBridge.enabled === true) {
    if (eaBridge.online === true) {
      eaStatus.textContent = "ONLINE";
      eaStatus.className = "text-emerald-400 font-black tracking-widest-caps";
    } else {
      eaStatus.textContent = "FALLBACK";
      eaStatus.className = "text-amber-400 font-black tracking-widest-caps";
    }
  } else {
    eaStatus.textContent = "OFF";
    eaStatus.className = "text-white/20 font-bold uppercase tracking-widest";
  }
  
  if (eaBridge.spread_pips != null) {
    const spread = Number(eaBridge.spread_pips);
    eaSpread.textContent = `${spread.toFixed(1)}P`;
    eaSpread.className = spread > 5 ? "font-mono text-amber-400 font-black" : "font-mono text-white/60 font-black";
  } else {
    eaSpread.textContent = "N/A";
    eaSpread.className = "font-mono text-white/20 font-black";
  }

  const la = state.last_analysis || {};
  const hasRealAnalysis = la.decision != null && la.final_score != null;
  const marketClosed = state.market?.is_open === false;

  const card = el("goldcon");

  if (marketClosed) {
    card.className = "relative glass-panel border-white/10 rounded-2xl p-6 overflow-hidden transition-all duration-700 shadow-xl group";
    const reason = state.market?.reason || "";
    const isPausa = reason.toLowerCase().includes("pausa") || reason.toLowerCase().includes("daily pause");
    el("goldcon-decision").textContent = isPausa ? "DAILY PAUSE" : "MARKET CLOSED";
    el("goldcon-decision").className = "text-3xl sm:text-4xl md:text-5xl font-black leading-none text-white/20 uppercase tracking-tighter transition-all duration-500";
    el("goldcon-score").textContent = "—";
    el("goldcon-conf").textContent = "—";
    const nextOpen = state.market?.next_open;
    if (nextOpen) {
      const dt = nextOpen.replace("T", " ").slice(0, 16);
      el("goldcon-scenario").textContent = `REOPENS: ${dt} UTC`;
    } else {
      el("goldcon-scenario").textContent = (state.market?.reason || "MARKET OFFLINE").toUpperCase();
    }
    
    // Reset Segmented Bar
    const segmentsContainer = el("signal-segments");
    if (segmentsContainer) {
      const segments = segmentsContainer.children;
      for (let i = 0; i < segments.length; i++) {
        const seg = segments[i];
        if (seg) {
          seg.className = "flex-1 rounded-sm transition-all duration-700 bg-white/5";
          seg.style.height = "40px";
        }
      }
    }
  } else {
    const decision = la.decision || "HOLD";
    const score = la.final_score;
    const conf = la.confidence;
    const cls = badgeClassByDecision(decision);
    
    // Apply new mockup styling classes to the card including the glow pulse
    card.className = `relative ${cls.bg} ${cls.border} ${cls.glow} backdrop-blur-md border-2 rounded-2xl p-6 shadow-xl overflow-hidden transition-all duration-500 group`;
    
    el("goldcon-decision").textContent = decision;
    el("goldcon-decision").className = `text-4xl sm:text-5xl md:text-6xl font-bold leading-none ${cls.text} text-shadow-soft transition-colors duration-300`;
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
        seg.className = "flex-1 rounded-sm transition-all duration-700 bg-white/5";
        seg.style.height = "40px";
        
        // Determine color zone based on segment index
        // 0-35 SELL (roughly segments 0-3) -> RED
        // 35-65 HOLD (roughly segments 4-7) -> AMBER
        // 65-100 BUY (roughly segments 8-11) -> EMERALD
        let colorClass = "";
        if (i < 4) colorClass = "segment-red";
        else if (i < 8) colorClass = "segment-amber";
        else colorClass = "segment-emerald";
        
        if (i < activeCount) {
          seg.classList.add("active");
          seg.classList.add(colorClass);
          seg.classList.remove("bg-white/5");
          
          // Add pulse to the very last active segment
          if (i === activeCount - 1) {
            seg.classList.add("pulse");
          }
        }
      }
    }
  }

  renderVolBanner(la.volatility_status, la.volatility_description);

  const prevBal = parseFloat(el("balance").textContent.replace("$", "")) || 0;
  const prevEq = parseFloat(el("equity").textContent.replace("$", "")) || 0;
  const prevPnl = parseFloat(el("pnl").textContent.replace("$", "")) || 0;
  const prevPrice = parseFloat(el("price").textContent) || 0;

  const bal = state.account?.balance;
  const eq = state.account?.equity;
  
  animateValue("balance", prevBal, bal, 800, fmtMoney);
  animateValue("equity", prevEq, eq, 800, fmtMoney);

  const hasPnl = state.daily_stats && state.daily_stats.pnl !== null && state.daily_stats.pnl !== undefined && !Number.isNaN(Number(state.daily_stats.pnl));
  const hasPnlPct = state.daily_stats && state.daily_stats.pnl_percent !== null && state.daily_stats.pnl_percent !== undefined && !Number.isNaN(Number(state.daily_stats.pnl_percent));

  const noClosedTrades = (Number(state.daily_stats?.wins || 0) + Number(state.daily_stats?.losses || 0) + Number(state.daily_stats?.breakevens || 0)) === 0;
  const pnlIsZero = Number(state.daily_stats?.pnl || 0) === 0;

  if (!operational || bal === null || bal === undefined || eq === null || eq === undefined || !hasPnl || !hasPnlPct || (noClosedTrades && pnlIsZero)) {
    el("pnl").textContent = "—";
    el("pnl").className = "text-xl font-bold text-gray-300";
  } else {
    const pnlVal = Number(state.daily_stats?.pnl || 0);
    animateValue("pnl", prevPnl, pnlVal, 800, (v) => `${fmtMoney(v)} (${fmtPct(state.daily_stats?.pnl_percent)})`);
    el("pnl").className = pnlVal >= 0 ? "text-xl font-bold text-emerald-400" : "text-xl font-bold text-red-400";
  }

  const livePrice = la.current_price;
  const lastKnown = state.last_known_price;
  const priceEl = el("price");
  const priceLabelEl = el("price-label");

  if (livePrice != null) {
    animateValue("price", prevPrice, livePrice, 400, (v) => fmtNum(v, 2));
    priceEl.className = "text-2xl sm:text-3xl font-bold text-white transition-colors duration-300";
    if (priceLabelEl) priceLabelEl.textContent = "";
  } else if (lastKnown != null) {
    animateValue("price", prevPrice, lastKnown, 400, (v) => fmtNum(v, 2));
    priceEl.className = "text-2xl sm:text-3xl font-bold text-gray-400";
    if (priceLabelEl) priceLabelEl.textContent = "LAST";
  } else {
    priceEl.textContent = "—";
    priceEl.className = "text-2xl sm:text-3xl font-bold text-gray-500";
    if (priceLabelEl) priceLabelEl.textContent = "";
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
  if (Number.isNaN(s)) return { border: "border-white/10", bg: "bg-white/5", text: "text-gray-500", label: "NEUTRAL" };
  if (s >= 65) return { border: "border-emerald-500/50", bg: "bg-emerald-500", text: "text-emerald-400", label: "BULLISH" };
  if (s >= 55) return { border: "border-emerald-700/50", bg: "bg-emerald-700", text: "text-emerald-500", label: "LEAN BULL" };
  if (s <= 35) return { border: "border-red-500/50", bg: "bg-red-500", text: "text-red-400", label: "BEARISH" };
  if (s <= 45) return { border: "border-red-700/50", bg: "bg-red-700", text: "text-red-500", label: "LEAN BEAR" };
  return { border: "border-white/10", bg: "bg-white/5", text: "text-gray-500", label: "NEUTRAL" };
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
    gold: { label: "GOLD", cls: "bg-amber-500/10 text-amber-400 border border-amber-500/20" },
    us_monetary: { label: "FED", cls: "bg-blue-500/10 text-blue-400 border border-blue-500/20" },
    geopolitics: { label: "GEO", cls: "bg-red-500/10 text-red-400 border border-red-500/20" },
    financial_crisis: { label: "CRISIS", cls: "bg-red-600/10 text-red-300 border border-red-600/20" },
    global_monetary: { label: "CB", cls: "bg-purple-500/10 text-purple-400 border border-purple-500/20" },
    inflation_commodities: { label: "INFL", cls: "bg-orange-500/10 text-orange-400 border border-orange-500/20" },
    safe_haven: { label: "HAVEN", cls: "bg-amber-600/10 text-amber-300 border border-amber-600/20" },
    recession: { label: "RECESS", cls: "bg-white/5 text-gray-400 border border-white/10" },
    market_risk: { label: "RISK", cls: "bg-pink-500/10 text-pink-400 border border-pink-500/20" },
    sanctions: { label: "SANCT", cls: "bg-amber-500/10 text-amber-400 border border-amber-500/20" },
    crisis_events: { label: "BLACK SWAN", cls: "bg-red-700/10 text-red-200 border border-red-700/20" },
  };
  const m = map[cat] || { label: cat || "?", cls: "bg-white/5 text-gray-500 border border-white/5" };
  return `<span class="px-1.5 py-0.5 rounded-[4px] text-[8px] font-black uppercase tracking-widest-caps ${m.cls}">${m.label}</span>`;
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
    hlContainer.innerHTML = `<div class="text-xs text-gray-500 italic">No headlines available</div>`;
  } else {
    headlines.forEach((h, idx) => {
      const sc = intelScoreColor(h.score);
      const age = h.age_hours != null ? `${Number(h.age_hours).toFixed(1)}h` : "?";
      const titleTrunc = h.title.length > 90 ? h.title.slice(0, 90) + "..." : h.title;
      const methodTag = h.method === "gpt" ? "GPT" : "KW";
      
      // Stagger entry animation
      const delay = idx * 50;
      hlContainer.insertAdjacentHTML("beforeend", `
        <div class="intel-headline flex items-stretch gap-3 group p-2 rounded-lg hover:bg-white/5 transition-colors duration-200 cursor-default border border-transparent hover:border-white/10 trigger-entry" style="animation-delay: ${delay}ms">
          <div class="w-1.5 rounded-full flex-shrink-0 ${sc.bg} opacity-80 shadow-[0_0_8px_currentColor]"></div>
          <div class="flex-1 min-w-0 py-0.5">
            <div class="text-xs text-gray-200 leading-snug truncate font-medium" title="${h.title.replace(/"/g, '&quot;')}">${titleTrunc}</div>
            <div class="flex items-center gap-2 mt-1.5 text-[10px] text-gray-500 font-mono">
              ${categoryBadge(h.category)}
              <span class="opacity-60">${age} ago</span>
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
    });
  }

  // Macro cards
  const macroContainer = el("intel-macro");
  macroContainer.innerHTML = "";
  const macro = feed.macro || {};
  const macroKeys = ["dxy", "yields", "vix"];
  macroKeys.forEach((key, idx) => {
    const m = macro[key];
    if (!m) return;
    const val = m.value;
    const chg = m.change_pct;
    const sc = intelScoreColor(m.score);
    const impact = macroImpactText(key, chg);
    const arrow = Number(chg) > 0 ? "&#9650;" : (Number(chg) < 0 ? "&#9660;" : "");
    const chgClass = Number(chg) > 0 ? "text-emerald-400" : (Number(chg) < 0 ? "text-red-400" : "text-white/40");
    const unit = macroUnit(key);

    const delay = idx * 100;
    macroContainer.insertAdjacentHTML("beforeend", `
      <div class="bg-black/20 backdrop-blur-sm border ${sc.border.replace('border-', 'border-')}/30 rounded-xl p-3 shadow-md hover:bg-white/5 transition-all duration-300 intel-macro-card relative overflow-hidden group trigger-entry" style="animation-delay: ${delay}ms">
        <div class="absolute inset-0 bg-gradient-to-br from-white/5 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-300 pointer-events-none"></div>
        <div class="flex items-center justify-between relative z-10">
          <div class="text-[10px] font-bold text-gray-500 tracking-widest-caps">${macroLabel(key)}</div>
          <div class="text-[10px] font-bold font-mono ${sc.text} bg-black/40 px-1.5 py-0.5 rounded border border-white/5">${fmtNum(m.score, 0)}</div>
        </div>
        <div class="flex items-baseline gap-2 mt-3 relative z-10">
          <span class="text-xl text-gray-100 font-bold font-mono tracking-tighter">${val != null ? fmtNum(val, 2) + unit : "—"}</span>
          <span class="text-[10px] font-mono font-bold ${chgClass}">${arrow} ${chg != null ? (Number(chg) > 0 ? "+" : "") + Number(chg).toFixed(2) + "%" : "—"}</span>
        </div>
        <div class="intel-robot-bubble inline-flex mt-3 relative z-10">
          <img src="/image/flokiwatch.png" alt="Floki" class="floki-intel-mini shadow-sm ring-1 ring-white/10 grayscale hover:grayscale-0 transition-all duration-300">
          <div class="intel-bubble backdrop-blur-md bg-gray-900/95 border border-white/10 p-2 shadow-2xl">
            <div class="text-[10px] text-gray-200 font-medium leading-relaxed">${impact}</div>
          </div>
        </div>
      </div>
    `);
  });

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
    const biasColor = cal.bias === "BULLISH" ? "text-emerald-400" : (cal.bias === "BEARISH" ? "text-red-400" : "text-white/40");
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
      calHtml += `<div class="mt-2 border-t border-white/5 pt-2">`;
      calHtml += `<div class="text-[9px] text-gray-600 font-bold tracking-widest-caps mb-2 uppercase">UPCOMING</div>`;
      for (const ev of upcoming) {
        const impColor = ev.importance === "HIGH" ? "text-red-400 border-red-500/30 bg-red-500/5" : "text-amber-500 border-amber-500/30 bg-amber-500/5";
        const impLabel = ev.importance === "HIGH" ? "H" : "M";
        const timeColor = ev.is_past ? "text-gray-600" : "text-gray-400";
        const nameColor = ev.is_past ? "text-gray-600" : "text-gray-300";
        calHtml += `
          <div class="flex items-center gap-2 text-[10px] py-1 border-b border-white/[0.02] last:border-0 font-medium">
            <span class="px-1 border rounded font-black ${impColor}" style="font-size:8px">${impLabel}</span>
            <span class="${timeColor} font-mono">${ev.time}</span>
            <span class="${nameColor} truncate flex-1 tracking-tight">${ev.name}</span>
            <span class="text-gray-600 flex-shrink-0 font-mono" style="font-size:9px">${ev.time_until}</span>
          </div>
        `;
      }
      calHtml += `</div>`;
    } else {
      calHtml += `<div class="mt-2 text-[9px] text-gray-600 font-bold uppercase tracking-widest">No upcoming events</div>`;
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
    const weight = isStrong ? "font-black" : "font-bold";
    const border = isMtf ? "border-purple-500" : borderColor;
    const glow = isMtf ? "shadow-[0_0_8px_rgba(168,85,247,0.2)]" : "";
    
    return `
      <div class="${color} ${weight} border-l-2 ${border} pl-2 py-1.5 mb-1 bg-white/5 rounded-r-md flex items-center justify-between group hover:bg-white/10 transition-all duration-200 ${glow}">
        <div class="flex items-center gap-2">
          <span class="tracking-tighter">${z.price.toFixed(2)}</span>
          ${tfBadge(z.timeframe)}
          ${mtfBadge(z.confluence)}
          ${ztBadge(z.zone_type)}
        </div>
        <div class="text-[9px] font-mono opacity-40 group-hover:opacity-100 transition-opacity">
          ${z.touches}T — ${z.dist_pips.toFixed(0)}P
        </div>
      </div>
    `;
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
    let bg = "bg-white/5";
    let border = "border-white/10";
    let textCls = "text-gray-400";
    let accentCls = "bg-gray-500";
    
    // BOOST = green, REDUCE = red, BLOCK = red, CONFIRM = gray
    if (action === "BOOST") { bg = "bg-emerald-500/5"; border = "border-emerald-500/20"; textCls = "text-emerald-400"; accentCls = "bg-emerald-500"; }
    else if (action === "REDUCE") { bg = "bg-red-500/5"; border = "border-red-500/20"; textCls = "text-red-400"; accentCls = "bg-red-500"; }
    else if (action === "BLOCK") { bg = "bg-red-500/5"; border = "border-red-500/20"; textCls = "text-red-400"; accentCls = "bg-red-500"; }

    gptContainer.innerHTML = `
      <div class="flex items-start gap-4 p-5 rounded-2xl border ${border} ${bg} backdrop-blur-md w-full relative overflow-hidden group">
        <div class="absolute inset-0 bg-gradient-to-br from-white/5 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-700"></div>
        <div class="mt-1 flex-shrink-0 relative z-10">
          <div class="relative">
            <img src="/image/flokiwatch.png" alt="Floki GPT" class="w-10 h-10 rounded-full shadow-2xl ring-2 ring-white/10 grayscale group-hover:grayscale-0 transition-all duration-500">
            <span class="absolute -bottom-1 -right-1 w-4 h-4 rounded-full ${accentCls} border-2 border-black flex items-center justify-center text-[8px] font-black text-white shadow-lg">AI</span>
          </div>
        </div>
        <div class="min-w-0 flex-1 relative z-10">
          <div class="flex items-center justify-between mb-2">
            <div class="text-[9px] font-black tracking-widest uppercase ${textCls} font-mono">GPT ${action}${adjStr}</div>
            <div class="text-[8px] font-bold text-gray-600 uppercase tracking-widest">VALIDATOR ACTIVE</div>
          </div>
          <div class="text-[11px] sm:text-xs text-gray-300 leading-relaxed font-medium font-sans break-words whitespace-normal opacity-90 group-hover:opacity-100 transition-opacity">${text.replace(/"/g, '&quot;')}</div>
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

    const dirColor = (dir) => dir === "bullish" ? "text-emerald-400" : dir === "bearish" ? "text-red-400" : "text-white/40";
    const dirLabel = (dir) => dir ? dir.charAt(0).toUpperCase() + dir.slice(1) : "—";
    
    const alignColor = alignment === "aligned" ? "text-emerald-400" : alignment === "conflict" ? "text-red-400" : alignment === "mixed" ? "text-amber-400" : "text-white/40";
    const alignLabel = alignment === "aligned" ? "Aligned ✓" : alignment === "conflict" ? "Conflict ✗" : alignment === "mixed" ? "Mixed" : "N/A";
    
    const adjColor = mtfAdj > 0 ? "text-emerald-400" : mtfAdj < 0 ? "text-red-400" : "text-white/40";
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

    const statusColor = volStatus === "normal" ? "text-emerald-400" : volStatus === "low" ? "text-amber-400" : volStatus === "very_low" ? "text-red-400" : "text-white/40";
    const statusLabel = volStatus === "normal" ? "Normal ✓" : volStatus === "low" ? "Low ⚠" : volStatus === "very_low" ? "Very Low ✗" : "—";
    
    const adjColor = volAdj > 0 ? "text-emerald-400" : volAdj < 0 ? "text-red-400" : "text-white/40";
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
      agreeEl.textContent = "✓ AGREE";
      agreeEl.className = "text-xs font-black text-emerald-400 bg-emerald-400/10 px-2 py-0.5 rounded border border-emerald-400/20 tracking-widest-caps";
    } else if (agreement === false) {
      agreeEl.textContent = "× DISAGREE";
      agreeEl.className = "text-xs font-black text-red-400 bg-red-400/10 px-2 py-0.5 rounded border border-red-400/20 tracking-widest-caps";
    } else {
      agreeEl.textContent = "—";
      agreeEl.className = "text-xs font-black text-white/20 tracking-widest-caps";
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
    const prevLat = parseInt(latEl.textContent) || 0;
    animateValue("agent-latency", prevLat, latencyMs, 600, (v) => Math.round(v).toString());
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
    decisionEl.textContent = decisionLabel(decision);
    decisionEl.style.color = decisionHexColor(decision);
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
      label = "WAITING";
      activeIdx = 2;
    } else if (d === "CLOSE_TRADE") {
      label = "EXITING";
      activeIdx = 2;
    } else if (d === "HOLD_TRADE" || d === "ADJUST_TRADE") {
      if (holdDir === "SELL") {
        if (c != null && c >= 75) { label = "STRONG SELL"; activeIdx = 0; }
        else { label = "SELL"; activeIdx = 1; }
      } else if (holdDir === "BUY") {
        if (c != null && c >= 75) { label = "STRONG BUY"; activeIdx = 4; }
        else { label = "BUY"; activeIdx = 3; }
      } else {
        label = "MONITORING";
        activeIdx = 2;
      }
    }

      sentimentLabelEl.textContent = label;
      
      // VU Meter logic: 14 segments total
      // activeIdx was 0-4 (5 original steps)
      // We map these 5 logical states to 14 segments
      const segmentMapping = [3, 6, 8, 11, 14]; // Strong Sell(3), Sell(6), Neutral(8), Buy(11), Strong Buy(14)
      updateVUMeter("sentiment-bar", segmentMapping[activeIdx]);
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
      
      // VU Meter logic: 14 segments total
      // stepIdx was 0-5 (6 logical steps)
      // Map: WATCHING(2), PREPARING(4), ENTRY(7), MANAGING(10), CLOSING(12), RESULT(14)
      const lifecycleMapping = [2, 4, 7, 10, 12, 14];
      updateVUMeter("lifecycle-bar", lifecycleMapping[stepIdx]);
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
      const timeStr = diffMin < 60 ? `${diffMin}M AGO` : `${Math.floor(diffMin / 60)}H ${diffMin % 60}M AGO`;
      timestampEl.textContent = `LAST CAPTURE: ${timeStr}`;
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
      viewDirEl.className = "text-xl font-black tracking-tighter text-emerald-400 uppercase";
    } else if (dir === "SELL") {
      viewDirEl.className = "text-xl font-black tracking-tighter text-red-400 uppercase";
    } else {
      viewDirEl.className = "text-xl font-black tracking-tighter text-amber-400 uppercase";
    }
  }

  // Market view description
  const viewDescEl = el("agent-memory-view-description");
  if (viewDescEl) {
    viewDescEl.textContent = agentMemory.market_view?.description || "—";
  }

  const conditionsEl = el("agent-memory-conditions");
  if (conditionsEl) {
    const conditions = agentMemory.conditions || [];
    if (conditions.length === 0) {
      conditionsEl.innerHTML = `<li class="text-gray-600 italic">No conditions set</li>`;
    } else {
      conditionsEl.innerHTML = conditions.map((c, idx) => {
        const icon = c.met ? "✓" : "×";
        const currentVal = c.current_value != null ? `<span class="opacity-50 ml-2 font-mono">(NOW: ${fmtNum(c.current_value, 1)})</span>` : "";
        const textClass = c.met ? "text-emerald-400" : "text-gray-400";
        const bgClass = c.met ? "bg-emerald-500/5 border-emerald-500/10" : "bg-white/5 border-white/5";
        const delay = idx * 50;
        return `
          <li class="flex items-center gap-3 p-2.5 rounded-xl border ${bgClass} ${textClass} transition-all duration-300 trigger-entry" style="animation-delay: ${delay}ms">
            <span class="text-xs font-black">${icon}</span>
            <span class="flex-1 font-bold tracking-widest-caps uppercase text-[9px]">${c.description}</span>
            <span class="text-[9px] font-bold uppercase">${currentVal}</span>
          </li>`;
      }).join("");
    }
  }

  // Expiry
  const expiryEl = el("agent-memory-expiry");
  if (expiryEl) {
    const inv = agentMemory.invalidation;
    if (inv) {
      expiryEl.textContent = `${inv.candles_remaining} ${inv.timeframe} CANDLES REMAINING`;
    } else {
      expiryEl.textContent = "—";
    }
  }

  // Status badge
  const statusEl = el("agent-memory-status");
  if (statusEl) {
    const status = (agentMemory.status || "ACTIVE").toUpperCase();
    const allMet = agentMemory.all_conditions_met;
    
    if (allMet || status === "CONDITIONS_MET") {
      statusEl.textContent = "RE-ENTRY ENABLED";
      statusEl.className = "px-2 py-0.5 rounded text-[9px] font-black bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 animate-pulse tracking-widest";
    } else if (status === "EXPIRED") {
      statusEl.textContent = "MEMORY EXPIRED";
      statusEl.className = "px-2 py-0.5 rounded text-[9px] font-black bg-gray-500/20 text-gray-400 border border-gray-500/30 tracking-widest";
    } else {
      statusEl.textContent = "MEMORY ACTIVE";
      statusEl.className = "px-2 py-0.5 rounded text-[9px] font-black bg-amber-500/20 text-amber-400 border border-amber-500/30 tracking-widest";
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
    container.innerHTML = `<span class="text-gray-600 font-medium italic text-[10px] uppercase tracking-widest">SCANNING HISTORY...</span>`;
    return;
  }

  container.innerHTML = "";
  decisions.forEach((d, idx) => {
    const time = (d.timestamp || "").split("T")[1]?.slice(0, 5) || "—";
    const decision = d.decision || "HOLD";
    const score = d.score != null ? `${fmtNum(d.score, 0)}%` : "—";
    const cls = decisionColor(decision);
    const delay = idx * 50;

    container.insertAdjacentHTML("beforeend", `
      <div class="flex items-center gap-1.5 px-2 py-1 rounded-lg border ${cls} backdrop-blur-md transition-all duration-300 hover:-translate-y-1 trigger-entry" style="animation-delay: ${delay}ms">
        <span class="text-[9px] text-gray-500 font-mono tracking-wider opacity-60">${time}</span>
        <span class="font-black text-[9px] tracking-tighter uppercase">${decision.replace("_", " ")}</span>
        <span class="w-px h-2 bg-current opacity-20"></span>
        <span class="text-[9px] font-mono opacity-90 font-bold">${score}</span>
      </div>
    `);
  });
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
    
    // Add subtle flash effect to the main container on update
    const mainApp = el("app");
    if (mainApp) {
      mainApp.classList.add("data-update-flash");
      setTimeout(() => mainApp.classList.remove("data-update-flash"), 1000);
    }

    render(data);
  } catch (e) {
    render({ bot: { status: "OFFLINE" }, timestamp: new Date().toISOString(), _meta: { file_age_seconds: null } });
  }
}

setInterval(poll, 3000);
setInterval(pollDecisions, 10000);
poll();
pollDecisions();
