let lastTimestamp = null;
let lastMetaAgeSeconds = null;
let lastBotStatus = null;

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
  if (s < 40) return "bg-red-500";
  if (s > 60) return "bg-green-500";
  return "bg-yellow-500";
}

function renderPillar(rowId, score) {
  const bar = el(`${rowId}-bar`);
  const val = el(`${rowId}-val`);
  const s = Number(score);
  const pct = Number.isNaN(s) ? 0 : Math.max(0, Math.min(100, s));
  bar.style.width = `${pct}%`;
  bar.className = `h-2 rounded ${pillColor(s)}`;
  val.textContent = fmtNum(s, 1);
}

function setStatusDot(isOperational) {
  const dot = el("status-dot");
  const label = el("status-label");

  if (isOperational) {
    dot.className = "w-2 h-2 rounded-full bg-green-400 animate-pulse";
    label.textContent = "OPERATIONAL";
    label.className = "text-green-400";
  } else {
    dot.className = "w-2 h-2 rounded-full bg-red-500 animate-pulse";
    label.textContent = "OFFLINE";
    label.className = "text-red-400";
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

    container.insertAdjacentHTML(
      "beforeend",
      `
      <div class="bg-gray-900/60 border border-gray-700 rounded-lg p-3">
        <div class="flex items-center justify-between">
          <div class="text-xs text-gray-200">#${p.ticket} <span class="ml-2 px-2 py-0.5 rounded border border-gray-600 text-gray-300">${p.direction}</span> <span class="ml-2 text-gray-400">${p.volume} lot</span></div>
          <div class="text-xs ${pnlClass}">${fmtMoney(pnl)} (${fmtNum(p.profit_pips, 0)} pips)</div>
        </div>
        <div class="mt-2 grid grid-cols-2 md:grid-cols-4 gap-2 text-xs text-gray-400">
          <div>Entry: <span class="text-gray-200">${fmtNum(p.open_price, 2)}</span></div>
          <div>SL: <span class="text-gray-200">${fmtNum(p.sl, 2)}</span></div>
          <div>TP: <span class="text-gray-200">${fmtNum(p.tp, 2)}</span></div>
          <div>Now: <span class="text-gray-200">${fmtNum(p.current_price, 2)}</span></div>
        </div>
      </div>
      `
    );
  }
}

function renderTrades(trades, daily) {
  const container = el("trades");
  container.innerHTML = "";

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
      <div class="flex items-center justify-between text-xs border-b border-gray-800 py-2">
        <div class="text-gray-400">${time}</div>
        <div class="text-gray-200">${t.direction || "—"}</div>
        <div class="text-gray-400 truncate max-w-[16rem]">${displayReason}</div>
        <div class="${pnlClass}">${pnlDisplay}</div>
        <div class="text-gray-500">${icon}</div>
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

  const la = state.last_analysis || {};
  const hasRealAnalysis = la.decision != null && la.final_score != null;
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
    
    // Reset gauge
    const gaugeArc = el("gauge-arc");
    const gaugeNeedle = el("gauge-needle");
    if (gaugeArc) gaugeArc.style.strokeDashoffset = 251.2;
    if (gaugeNeedle) gaugeNeedle.style.transform = "rotate(-90deg)";
  } else {
    const decision = la.decision || "HOLD";
    const score = la.final_score;
    const conf = la.confidence;
    const cls = badgeClassByDecision(decision);
    
    // Apply new mockup styling classes to the card
    card.className = `relative ${cls.bg} ${cls.border} backdrop-blur-md border-2 rounded-2xl p-6 shadow-xl overflow-hidden transition-all duration-500 group`;
    
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
    
    // Update gauge
    const gaugeArc = el("gauge-arc");
    const gaugeNeedle = el("gauge-needle");
    if (gaugeArc && gaugeNeedle && score != null) {
      const clampedScore = Math.max(0, Math.min(100, score));
      // Arc length is 251.2, so offset goes from 251.2 (0%) to 0 (100%)
      const dashOffset = 251.2 - (251.2 * (clampedScore / 100));
      gaugeArc.style.strokeDashoffset = dashOffset;
      
      // Needle rotation goes from -90deg (0%) to 90deg (100%)
      const rotation = -90 + (180 * (clampedScore / 100));
      gaugeNeedle.style.transform = `rotate(${rotation}deg)`;
    }
  }

  renderVolBanner(la.volatility_status, la.volatility_description);

  const bal = state.account?.balance;
  const eq = state.account?.equity;
  el("balance").textContent = fmtMoney(bal);
  el("equity").textContent = fmtMoney(eq);

  const hasPnl = state.daily_stats && state.daily_stats.pnl !== null && state.daily_stats.pnl !== undefined && !Number.isNaN(Number(state.daily_stats.pnl));
  const hasPnlPct = state.daily_stats && state.daily_stats.pnl_percent !== null && state.daily_stats.pnl_percent !== undefined && !Number.isNaN(Number(state.daily_stats.pnl_percent));

  const noClosedTrades = (Number(state.daily_stats?.wins || 0) + Number(state.daily_stats?.losses || 0) + Number(state.daily_stats?.breakevens || 0)) === 0;
  const pnlIsZero = Number(state.daily_stats?.pnl || 0) === 0;

  if (!operational || bal === null || bal === undefined || eq === null || eq === undefined || !hasPnl || !hasPnlPct || (noClosedTrades && pnlIsZero)) {
    el("pnl").textContent = "—";
    el("pnl").className = "text-xl font-bold text-gray-300";
  } else {
    el("pnl").textContent = `${fmtMoney(state.daily_stats?.pnl)}  (${fmtPct(state.daily_stats?.pnl_percent)})`;
    const pnlVal = Number(state.daily_stats?.pnl || 0);
    el("pnl").className = pnlVal >= 0 ? "text-xl font-bold text-green-400" : "text-xl font-bold text-red-400";
  }

  const livePrice = la.current_price;
  const lastKnown = state.last_known_price;
  const priceEl = el("price");
  const priceLabelEl = el("price-label");

  if (livePrice != null) {
    priceEl.textContent = fmtNum(livePrice, 2);
    priceEl.className = "text-2xl font-bold text-white";
    if (priceLabelEl) priceLabelEl.textContent = "";
  } else if (lastKnown != null) {
    priceEl.textContent = fmtNum(lastKnown, 2);
    priceEl.className = "text-2xl font-bold text-gray-400";
    if (priceLabelEl) priceLabelEl.textContent = "LAST";
  } else {
    priceEl.textContent = "—";
    priceEl.className = "text-2xl font-bold text-gray-500";
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
  return `<span class="px-1 py-0.5 rounded text-[9px] font-bold uppercase ${m.cls}">${m.label}</span>`;
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
        <div class="intel-headline flex items-stretch gap-2 group">
          <div class="w-1 rounded-full flex-shrink-0 ${sc.bg} opacity-60"></div>
          <div class="flex-1 min-w-0 py-1">
            <div class="text-xs text-gray-200 leading-snug truncate" title="${h.title.replace(/"/g, '&quot;')}">${titleTrunc}</div>
            <div class="flex items-center gap-2 mt-0.5 text-xs text-gray-500">
              ${categoryBadge(h.category)}
              <span>${age} ago</span>
            </div>
          </div>
          <div class="flex-shrink-0 flex items-center">
            <div class="intel-robot-bubble">
              <img src="/image/flokiwatch.png" alt="Floki" class="floki-intel-mini">
              <div class="intel-bubble">
                <div class="text-xs font-bold ${sc.text}">${fmtNum(h.score, 0)}/100</div>
                <div class="text-xs text-gray-400">${sc.label}</div>
                <div class="text-xs text-gray-500 mt-0.5">via ${methodTag}</div>
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
      <div class="bg-gray-800/60 border ${sc.border} rounded p-2 intel-macro-card">
        <div class="flex items-center justify-between">
          <div class="text-xs text-gray-400 tracking-wider">${macroLabel(key)}</div>
          <div class="text-xs ${sc.text}">${fmtNum(m.score, 0)}</div>
        </div>
        <div class="flex items-baseline gap-2 mt-1">
          <span class="text-sm text-gray-100 font-bold">${val != null ? fmtNum(val, 2) + unit : "—"}</span>
          <span class="text-xs ${chgClass}">${arrow} ${chg != null ? (Number(chg) > 0 ? "+" : "") + Number(chg).toFixed(2) + "%" : "—"}</span>
        </div>
        <div class="intel-robot-bubble inline-flex mt-1">
          <img src="/image/flokiwatch.png" alt="Floki" class="floki-intel-mini">
          <div class="intel-bubble">
            <div class="text-xs text-gray-300">${impact}</div>
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
    let aboveHtml = '<div><div class="text-red-400 font-bold mb-1">&#9650; RESISTANCE</div>';
    if (above.length === 0) { aboveHtml += '<div class="text-gray-600">None nearby</div>'; }
    for (const z of above) { aboveHtml += zoneRow(z, "text-red-300", "border-red-700"); }
    aboveHtml += '</div>';
    // Support (below) — left column
    let belowHtml = '<div><div class="text-green-400 font-bold mb-1">&#9660; SUPPORT</div>';
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
  const gptEl = el("intel-gpt");
  const gpt = feed.gpt_validator;
  if (gpt && gpt.action) {
    gptEl.classList.remove("hidden");
    const actionColors = {
      "CONFIRM": "border-gray-600 text-gray-400",
      "BOOST": "border-green-600 text-green-400",
      "REDUCE": "border-red-600 text-red-400",
    };
    const ac = actionColors[gpt.action] || "border-gray-600 text-gray-400";
    const sign = gpt.action === "BOOST" ? "+" : (gpt.action === "REDUCE" ? "-" : "");
    const adjText = gpt.adjustment > 0 ? ` (${sign}${gpt.adjustment})` : "";
    gptEl.innerHTML = `
      <div class="flex items-center gap-2 text-xs">
        <img src="/image/flokiwatch.png" alt="Floki" class="floki-intel-mini">
        <span class="px-1.5 py-0.5 border rounded ${ac} font-bold">${gpt.action}${adjText}</span>
        <span class="text-gray-500 truncate max-w-md">${gpt.reason || ""}</span>
      </div>
    `;
  } else {
    gptEl.classList.add("hidden");
    gptEl.innerHTML = "";
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
   RECENT DECISIONS
   ================================================================ */

function decisionColor(d) {
  const s = (d || "").toUpperCase();
  if (s.includes("BUY")) return "text-green-400 border-green-700 bg-green-900/20";
  if (s.includes("SELL")) return "text-red-400 border-red-700 bg-red-900/20";
  return "text-yellow-300 border-yellow-700 bg-yellow-900/20";
}

function renderRecentDecisions(decisions) {
  const container = el("recent-decisions");
  if (!container) return;

  if (!decisions || decisions.length === 0) {
    container.innerHTML = `<span class="text-gray-500">NO DECISIONS YET</span>`;
    return;
  }

  container.innerHTML = "";
  for (const d of decisions) {
    const time = (d.timestamp || "").split("T")[1]?.slice(0, 5) || "—";
    const decision = d.decision || "HOLD";
    const score = d.score != null ? Number(d.score).toFixed(1) : "—";
    const cls = decisionColor(decision);
    container.insertAdjacentHTML("beforeend",
      `<span class="inline-flex items-center gap-1.5 px-2 py-1 rounded border ${cls}">` +
        `<span class="text-gray-500">${time}</span> ` +
        `<span class="font-bold">${decision}</span> ` +
        `<span class="text-gray-400">${score}</span>` +
      `</span>`
    );
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
