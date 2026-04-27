/* FLO-377 — Trade Room Snow page.
 *
 * Self-contained page module loaded by trade_room.html. Provides:
 *   - Active view: 5s polling of /api/snow/plans?status=active.
 *     Each plan card is expandable (click to fetch /api/snow/plan/{id}
 *     and render full structure + audit log).
 *   - History view: terminal plans table with direction / setup_type /
 *     free-text filters, lazy-loaded on first tab switch and on refresh.
 *
 * No build step, no framework. Vanilla DOM. Mobile-responsive at
 * 375px by construction (flex-wrap, single-column grid, max-width
 * containers, overflow-x:auto on the history table).
 */
(function () {
  "use strict";

  // ── Constants ─────────────────────────────────────────────────────
  var ACTIVE_POLL_MS = 5000;
  var HISTORY_LIMIT = 100;
  var SETUP_TYPES = [
    "breakout_range", "pullback_trend", "mean_reversion_extreme",
    "liquidity_sweep", "continuation_momentum", "news_reaction",
    "divergence_play", "paired_hedge", "structural_bounce",
    "session_open_break",
  ];

  // ── State ─────────────────────────────────────────────────────────
  var pollHandle = null;
  var currentView = "active";
  var historyLoaded = false;
  var historyRows = [];
  var expandedPlanIds = new Set();   // ids whose detail is expanded
  var planDetailCache = {};          // id → fetched detail (TTL: until next poll)

  // ── Helpers ───────────────────────────────────────────────────────
  function el(id) { return document.getElementById(id); }
  function esc(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function fmtTime(iso) {
    if (!iso) return "—";
    if (window.displayTime) return window.displayTime(iso);
    return String(iso).replace("T", " ").slice(0, 19);
  }
  function timeUntil(iso) {
    if (!iso) return "";
    try {
      var ms = new Date(iso).getTime() - Date.now();
      if (ms <= 0) return "expired";
      var min = Math.round(ms / 60000);
      if (min < 60) return min + "m left";
      var hr = Math.floor(min / 60);
      var rem = min % 60;
      return hr + "h" + (rem ? rem + "m" : "") + " left";
    } catch (e) { return ""; }
  }
  function dirStyle(dir) {
    if (dir === "BUY") return "color:#34d399;background:rgba(52,211,153,0.12)";
    if (dir === "SELL") return "color:#f87171;background:rgba(248,113,113,0.12)";
    return "color:#94a3b8;background:rgba(148,163,184,0.10)";
  }
  function statusLabel(s) {
    if (!s) return "—";
    switch (s) {
      case "pending": return { lbl: "PENDING", color: "#fbbf24" };
      case "triggered": return { lbl: "TRIGGERED", color: "#f97316" };
      case "active": return { lbl: "ACTIVE", color: "#38bdf8" };
      case "closing": return { lbl: "CLOSING", color: "#a78bfa" };
      case "closed": return { lbl: "CLOSED", color: "#94a3b8" };
      case "expired": return { lbl: "EXPIRED", color: "#64748b" };
      case "cancelled": return { lbl: "CANCELLED", color: "#64748b" };
      case "failed": return { lbl: "FAILED", color: "#f87171" };
      default: return { lbl: s.toUpperCase(), color: "#94a3b8" };
    }
  }

  function setError(msg) {
    var box = el("snow-error");
    if (!box) return;
    if (!msg) { box.hidden = true; box.textContent = ""; return; }
    box.hidden = false;
    box.textContent = String(msg);
  }

  // ── API ───────────────────────────────────────────────────────────
  function fetchPlans(status) {
    var url = "/api/snow/plans?status=" + encodeURIComponent(status || "active");
    if (status === "terminal") url += "&limit=" + HISTORY_LIMIT;
    return fetch(url, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
  }
  function fetchPlanDetail(id) {
    return fetch("/api/snow/plan/" + encodeURIComponent(id), { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
  }

  // ── Render: plan summary (used by active list + history detail) ──
  function renderTagsLine(ct) {
    if (!ct) return "";
    var tags = [];
    if (ct.trend) tags.push(ct.trend);
    if (ct.volatility) tags.push(ct.volatility);
    if (ct.htf) tags.push(ct.htf);
    if (Array.isArray(ct.news_session)) {
      ct.news_session.forEach(function (n) { tags.push(n); });
    }
    if (!tags.length) return "";
    return tags.map(function (t) {
      return '<span style="font-family:\'JetBrains Mono\',monospace;font-size:9px;color:#94a3b8;background:rgba(255,255,255,0.04);padding:1px 6px;border-radius:3px">' + esc(t) + "</span>";
    }).join(" ");
  }

  function renderPlanHeader(p) {
    var st = statusLabel(p.status);
    var setupBadge = p.setup_type
      ? '<span style="font-family:\'JetBrains Mono\',monospace;font-size:9px;color:#38bdf8;background:rgba(56,189,248,0.08);border:1px solid rgba(56,189,248,0.2);padding:1px 6px;border-radius:3px">' + esc(p.setup_type) + "</span>"
      : "";
    return ''
      + '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
      + '<span style="font-family:\'JetBrains Mono\',monospace;font-size:11px;color:#e2e8f0;font-weight:700">' + esc(p.id) + "</span>"
      + '<span style="font-family:\'JetBrains Mono\',monospace;font-size:9px;font-weight:700;color:' + st.color + ';background:rgba(0,0,0,0.3);padding:2px 6px;border-radius:3px">' + st.lbl + "</span>"
      + '<span style="font-family:\'JetBrains Mono\',monospace;font-size:10px;font-weight:700;' + dirStyle(p.direction) + ';padding:2px 6px;border-radius:3px">' + esc(p.direction || "—") + "</span>"
      + setupBadge
      + '<span style="font-family:\'JetBrains Mono\',monospace;font-size:9px;color:#64748b">v' + esc(p.schema_version || "—") + "</span>"
      + "</div>";
  }

  function renderPlanMetaRow(p) {
    return ''
      + '<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;font-family:\'JetBrains Mono\',monospace;font-size:10px;color:#64748b;margin-top:6px">'
      + "<span>SL <span style=\"color:#94a3b8\">" + (p.initial_sl != null ? p.initial_sl : "—") + "</span></span>"
      + "<span>TP <span style=\"color:#94a3b8\">" + (p.initial_tp != null ? p.initial_tp : "—") + "</span></span>"
      + "<span>vol <span style=\"color:#94a3b8\">" + (p.volume != null ? p.volume : "—") + "</span></span>"
      + "<span>conf <span style=\"color:#94a3b8\">" + (p.confidence != null ? p.confidence : "—") + "</span></span>"
      + "<span>mgmt <span style=\"color:#94a3b8\">" + (p.n_management != null ? p.n_management : "—") + "</span></span>"
      + "<span>exit <span style=\"color:#94a3b8\">" + (p.n_exit != null ? p.n_exit : "—") + "</span></span>"
      + (p.expires_at ? "<span>" + esc(timeUntil(p.expires_at)) + "</span>" : "")
      + "</div>";
  }

  function renderPlanCard(p) {
    var tagsLine = renderTagsLine(p.context_tags);
    var expanded = expandedPlanIds.has(p.id);
    var detail = planDetailCache[p.id];
    return ''
      + '<div data-plan-id="' + esc(p.id) + '" class="snow-plan-card" '
      + 'style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:12px;cursor:pointer">'
      + renderPlanHeader(p)
      + '<p style="font-size:11px;color:#94a3b8;line-height:1.5;margin:8px 0 0 0">' + esc(p.thesis_short || "—") + "</p>"
      + renderPlanMetaRow(p)
      + (tagsLine ? '<div style="margin-top:6px">' + tagsLine + "</div>" : "")
      + (expanded
          ? renderPlanDetailBlock(detail || { _loading: true })
          : '<div style="margin-top:8px;font-family:\'JetBrains Mono\',monospace;font-size:9px;color:#475569">click to expand →</div>')
      + "</div>";
  }

  // ── Detail block (full plan structure + audit log) ───────────────
  function renderConditionList(conds) {
    if (!Array.isArray(conds) || conds.length === 0) return "<em style=\"color:#475569\">none</em>";
    return conds.map(function (c) {
      var summary = c.type;
      // Best-effort: surface the most useful prop for the type.
      Object.keys(c).forEach(function (k) {
        if (k === "type") return;
        var v = c[k];
        if (typeof v === "object") return;
        summary += " " + k + "=" + v;
      });
      return '<li style="font-family:\'JetBrains Mono\',monospace;font-size:10px;color:#94a3b8;padding:2px 0">' + esc(summary) + "</li>";
    }).join("");
  }

  function renderContingencyBlock(label, list) {
    if (!Array.isArray(list) || list.length === 0) {
      return '<div><div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;color:#64748b;letter-spacing:0.5px">' + esc(label) + "</div>"
        + '<div style="font-size:10px;color:#475569;font-style:italic;margin-top:4px">none</div></div>';
    }
    var items = list.map(function (c) {
      var st = statusLabel(c.state || "armed");
      return ''
        + '<div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.04);border-radius:6px;padding:8px;margin-top:6px">'
        + '<div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px">'
        + '<span style="font-family:\'JetBrains Mono\',monospace;font-size:10px;font-weight:700;color:#e2e8f0">' + esc(c.name) + "</span>"
        + '<span style="font-family:\'JetBrains Mono\',monospace;font-size:9px;color:' + st.color + '">' + st.lbl + "</span>"
        + "</div>"
        + '<div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;color:#64748b;margin-top:4px">'
        + "action: <span style=\"color:#94a3b8\">" + esc((c.action && c.action.type) || "—") + "</span>"
        + "&nbsp;·&nbsp; priority: " + esc(c.priority || "—")
        + "&nbsp;·&nbsp; fires: " + esc(c.fires || "once")
        + (c.fired_at ? '&nbsp;·&nbsp; fired: ' + esc(fmtTime(c.fired_at)) : "")
        + "</div>"
        + '<ul style="margin:6px 0 0 18px;padding:0">' + renderConditionList(c.conditions) + "</ul>"
        + "</div>";
    }).join("");
    return '<div><div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;color:#64748b;letter-spacing:0.5px">' + esc(label) + "</div>"
      + items + "</div>";
  }

  function renderAuditList(triggers) {
    if (!Array.isArray(triggers) || triggers.length === 0) {
      return "<em style=\"color:#475569;font-size:10px\">no audit rows</em>";
    }
    var rows = triggers.slice(0, 8).map(function (t) {
      var status = t.execution_status || "—";
      var color = status === "success" ? "#34d399" : (status === "skipped_guard" ? "#fbbf24" : "#f87171");
      return ''
        + '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-family:\'JetBrains Mono\',monospace;font-size:9px;padding:2px 0;color:#64748b">'
        + "<span>" + esc(fmtTime(t.fired_at)) + "</span>"
        + '<span style="color:#94a3b8">' + esc(t.contingency_name || "—") + "</span>"
        + '<span style="color:#94a3b8">' + esc(t.action_type || "—") + "</span>"
        + '<span style="color:' + color + ';font-weight:700">' + esc(status) + "</span>"
        + "</div>";
    }).join("");
    var more = triggers.length > 8
      ? '<div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;color:#475569;margin-top:4px">… and ' + (triggers.length - 8) + " more</div>"
      : "";
    return rows + more;
  }

  function renderPlanDetailBlock(detail) {
    if (detail && detail._loading) {
      return '<div style="margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.05);font-family:\'JetBrains Mono\',monospace;font-size:10px;color:#475569">loading detail…</div>';
    }
    if (!detail || !detail.plan) {
      return '<div style="margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.05);font-family:\'JetBrains Mono\',monospace;font-size:10px;color:#f87171">detail unavailable</div>';
    }
    var plan = detail.plan;
    var analysis = plan.analysis || {};
    var entry = plan.entry || {};
    return ''
      + '<div style="margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.05);display:grid;grid-template-columns:1fr;gap:10px">'
      + (analysis.confidence_reason
          ? '<div><div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;color:#64748b;letter-spacing:0.5px">CONFIDENCE REASON</div>'
            + '<div style="font-size:10px;color:#94a3b8;line-height:1.5;margin-top:4px">' + esc(analysis.confidence_reason) + "</div></div>"
          : "")
      + '<div><div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;color:#64748b;letter-spacing:0.5px">FULL THESIS</div>'
      + '<div style="font-size:10px;color:#94a3b8;line-height:1.5;margin-top:4px;white-space:pre-wrap">' + esc(analysis.thesis || "—") + "</div></div>"
      + '<div><div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;color:#64748b;letter-spacing:0.5px">ENTRY CONDITIONS</div>'
      + '<ul style="margin:4px 0 0 18px;padding:0">' + renderConditionList(entry.conditions) + "</ul></div>"
      + renderContingencyBlock("MANAGEMENT", plan.management)
      + renderContingencyBlock("EXIT", plan.exit)
      + '<div><div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;color:#64748b;letter-spacing:0.5px">RECENT AUDIT</div>'
      + '<div style="margin-top:4px">' + renderAuditList(detail.triggers) + "</div></div>"
      + "</div>";
  }

  // ── Active view loop ──────────────────────────────────────────────
  function renderActiveList(plans) {
    var listBox = el("snow-active-list");
    var emptyBox = el("snow-active-empty");
    if (!Array.isArray(plans) || plans.length === 0) {
      if (listBox) listBox.innerHTML = "";
      if (emptyBox) emptyBox.hidden = false;
      return;
    }
    if (emptyBox) emptyBox.hidden = true;
    if (listBox) listBox.innerHTML = plans.map(renderPlanCard).join("");
  }

  function refreshActive() {
    fetchPlans("active")
      .then(function (j) {
        setError(null);
        var plans = j.plans || [];
        var meta = el("snow-tab-meta");
        if (meta) meta.textContent = plans.length + " active";
        renderActiveList(plans);
        // Refresh expanded details opportunistically.
        expandedPlanIds.forEach(function (id) {
          if (!plans.find(function (p) { return p.id === id; })) return;
          fetchPlanDetail(id).then(function (d) {
            if (!d || !d.success) return;
            planDetailCache[id] = d;
            renderActiveList(plans);
          }).catch(function () { /* swallow per-detail errors */ });
        });
      })
      .catch(function (e) { setError("active fetch failed: " + e.message); });
  }

  function startPolling() {
    stopPolling();
    refreshActive();
    pollHandle = setInterval(refreshActive, ACTIVE_POLL_MS);
  }
  function stopPolling() {
    if (pollHandle != null) { clearInterval(pollHandle); pollHandle = null; }
  }

  // ── History view ──────────────────────────────────────────────────
  function refreshHistory() {
    fetchPlans("terminal")
      .then(function (j) {
        setError(null);
        historyRows = j.plans || [];
        renderHistory();
      })
      .catch(function (e) { setError("history fetch failed: " + e.message); });
  }

  function applyFilters(rows) {
    var dirVal = (el("snow-filter-direction") && el("snow-filter-direction").value) || "";
    var setupVal = (el("snow-filter-setup") && el("snow-filter-setup").value) || "";
    var search = ((el("snow-filter-search") && el("snow-filter-search").value) || "").toLowerCase();
    return rows.filter(function (p) {
      if (dirVal && p.direction !== dirVal) return false;
      if (setupVal && p.setup_type !== setupVal) return false;
      if (search) {
        var hay = ((p.id || "") + " " + (p.thesis_short || "")).toLowerCase();
        if (hay.indexOf(search) === -1) return false;
      }
      return true;
    });
  }

  function renderHistory() {
    var tbody = el("snow-history-tbody");
    var empty = el("snow-history-empty");
    if (!tbody) return;
    var rows = applyFilters(historyRows);
    if (!rows.length) {
      tbody.innerHTML = "";
      if (empty) empty.hidden = false;
      var meta = el("snow-tab-meta");
      if (meta) meta.textContent = "0 / " + historyRows.length + " (filtered)";
      return;
    }
    if (empty) empty.hidden = true;
    tbody.innerHTML = rows.map(function (p) {
      var pips = p.outcome_pips != null ? Number(p.outcome_pips).toFixed(1) : "—";
      var usd = p.outcome_usd != null ? "$" + Number(p.outcome_usd).toFixed(2) : "—";
      var pipsColor = (p.outcome_pips == null) ? "#64748b" : (p.outcome_pips >= 0 ? "#34d399" : "#f87171");
      var st = statusLabel(p.status);
      return ''
        + '<tr data-plan-id="' + esc(p.id) + '" class="snow-history-row" style="border-bottom:1px solid rgba(255,255,255,0.04);cursor:pointer">'
        + '<td style="padding:6px 8px;color:#e2e8f0">' + esc(p.id) + "</td>"
        + '<td style="padding:6px 8px;' + dirStyle(p.direction) + ';">' + esc(p.direction || "—") + "</td>"
        + '<td style="padding:6px 8px;color:' + st.color + '">' + st.lbl + "</td>"
        + '<td style="padding:6px 8px;color:#94a3b8">' + esc(p.setup_type || "—") + "</td>"
        + '<td style="padding:6px 8px;text-align:right;color:' + pipsColor + ';font-weight:700">' + pips + "</td>"
        + '<td style="padding:6px 8px;text-align:right;color:' + pipsColor + '">' + usd + "</td>"
        + '<td style="padding:6px 8px;color:#64748b">' + esc(fmtTime(p.closed_at || p.created_at)) + "</td>"
        + "</tr>";
    }).join("");
    var meta2 = el("snow-tab-meta");
    if (meta2) meta2.textContent = rows.length + " / " + historyRows.length + " (filtered)";
  }

  // ── View switching ────────────────────────────────────────────────
  function switchView(view) {
    currentView = view;
    var aV = el("snow-active-view"), hV = el("snow-history-view");
    var aT = el("snow-tab-active"), hT = el("snow-tab-history");
    if (view === "active") {
      if (aV) aV.hidden = false;
      if (hV) hV.hidden = true;
      if (aT) { aT.setAttribute("aria-selected", "true"); aT.style.background = "rgba(56,189,248,0.12)"; aT.style.borderColor = "rgba(56,189,248,0.3)"; aT.style.color = "#38bdf8"; }
      if (hT) { hT.setAttribute("aria-selected", "false"); hT.style.background = "transparent"; hT.style.borderColor = "rgba(255,255,255,0.06)"; hT.style.color = "#94a3b8"; }
      startPolling();
    } else {
      if (aV) aV.hidden = true;
      if (hV) hV.hidden = false;
      if (hT) { hT.setAttribute("aria-selected", "true"); hT.style.background = "rgba(56,189,248,0.12)"; hT.style.borderColor = "rgba(56,189,248,0.3)"; hT.style.color = "#38bdf8"; }
      if (aT) { aT.setAttribute("aria-selected", "false"); aT.style.background = "transparent"; aT.style.borderColor = "rgba(255,255,255,0.06)"; aT.style.color = "#94a3b8"; }
      stopPolling();
      if (!historyLoaded) { historyLoaded = true; refreshHistory(); }
      else { renderHistory(); }
    }
  }

  // ── Wire-up ───────────────────────────────────────────────────────
  function populateSetupFilter() {
    var sel = el("snow-filter-setup");
    if (!sel) return;
    SETUP_TYPES.forEach(function (st) {
      var opt = document.createElement("option");
      opt.value = st;
      opt.textContent = st;
      sel.appendChild(opt);
    });
  }

  function bindEvents() {
    var aT = el("snow-tab-active"), hT = el("snow-tab-history");
    if (aT) aT.addEventListener("click", function () { switchView("active"); });
    if (hT) hT.addEventListener("click", function () { switchView("history"); });
    var refresh = el("snow-history-refresh");
    if (refresh) refresh.addEventListener("click", refreshHistory);
    ["snow-filter-direction", "snow-filter-setup", "snow-filter-search"].forEach(function (id) {
      var n = el(id);
      if (n) n.addEventListener("input", renderHistory);
    });

    // Active list: click a plan card → toggle expand + lazy-fetch detail.
    var listBox = el("snow-active-list");
    if (listBox) {
      listBox.addEventListener("click", function (ev) {
        var card = ev.target.closest && ev.target.closest(".snow-plan-card");
        if (!card) return;
        var id = card.getAttribute("data-plan-id");
        if (!id) return;
        if (expandedPlanIds.has(id)) {
          expandedPlanIds.delete(id);
          delete planDetailCache[id];
          refreshActive();
        } else {
          expandedPlanIds.add(id);
          refreshActive();  // re-render with loading placeholder
          fetchPlanDetail(id).then(function (d) {
            if (!d || !d.success) {
              setError("plan " + id + ": " + (d && d.error || "load failed"));
              return;
            }
            planDetailCache[id] = d;
            refreshActive();
          }).catch(function (e) { setError("detail fetch failed: " + e.message); });
        }
      });
    }

    // History row: click → switch to active view briefly OR open detail in a
    // future expansion. For v1 we simply log the id; detail-on-click for
    // history is a follow-up if CEO wants it.
    var tbody = el("snow-history-tbody");
    if (tbody) {
      tbody.addEventListener("click", function (ev) {
        var row = ev.target.closest && ev.target.closest(".snow-history-row");
        if (!row) return;
        var id = row.getAttribute("data-plan-id");
        if (!id) return;
        // Open the same detail panel inline below the row.
        var existing = row.nextSibling;
        if (existing && existing.classList && existing.classList.contains("snow-history-detail")) {
          existing.parentNode.removeChild(existing);
          return;
        }
        fetchPlanDetail(id).then(function (d) {
          if (!d || !d.success) { setError("plan " + id + ": " + (d && d.error || "load failed")); return; }
          var tr = document.createElement("tr");
          tr.className = "snow-history-detail";
          var td = document.createElement("td");
          td.colSpan = 7;
          td.style.padding = "10px";
          td.style.background = "rgba(255,255,255,0.02)";
          td.innerHTML = renderPlanDetailBlock(d);
          tr.appendChild(td);
          row.parentNode.insertBefore(tr, row.nextSibling);
        }).catch(function (e) { setError("detail fetch failed: " + e.message); });
      });
    }
  }

  function init() {
    if (!el("snow-section")) return;  // page doesn't host the section
    populateSetupFilter();
    bindEvents();
    switchView("active");
    // If the URL already targets #snow, scroll into view.
    if (window.location.hash === "#snow") {
      try { el("snow-section").scrollIntoView({ behavior: "smooth", block: "start" }); }
      catch (e) { /* old browsers */ }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
