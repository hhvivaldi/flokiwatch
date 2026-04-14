/**
 * maintenance.js — FLO-298
 *
 * Cross-page maintenance banner. Displayed when bot_state.json has
 * `maintenance_mode: true` (Floki's primary model Qwen is unavailable).
 *
 * Contract:
 *   window.MaintenanceMode.apply(state)
 *     - Pass the parsed bot_state object; the helper shows/hides the banner
 *       and adds/removes the `.maintenance-on` class on <body>.
 *   window.MaintenanceMode.isActive() -> boolean
 *     - Cheap check for call sites that want to suppress their own rendering.
 *
 * Every host page marks Floki-specific UI regions with `data-floki-hide-on-maint`
 * so they get hidden automatically. Other agents keep rendering normally.
 */
(function () {
  'use strict';

  var BANNER_ID = 'maintenance-banner';
  var state = { active: false };

  function ensureStyles() {
    if (document.getElementById('maintenance-banner-styles')) return;
    var css = document.createElement('style');
    css.id = 'maintenance-banner-styles';
    css.textContent = [
      '#' + BANNER_ID + '{',
      '  position:fixed; top:0; left:0; right:0; z-index:99999;',
      '  background:#1a1a2e; color:#f5f5f7;',
      "  font-family:'JetBrains Mono', ui-monospace, monospace;",
      '  font-size:13px; letter-spacing:0.04em; font-weight:600;',
      '  padding:10px 16px; text-align:center;',
      '  border-bottom:1px solid rgba(255,255,255,0.12);',
      '  box-shadow:0 2px 12px rgba(0,0,0,0.35);',
      '  display:none;',
      '}',
      '#' + BANNER_ID + '.visible{ display:block; }',
      'body.maintenance-on{ padding-top:44px; }',
      // Hide Floki-specific UI when maintenance is on; other agents keep rendering.
      'body.maintenance-on [data-floki-hide-on-maint]{',
      '  opacity:0.25; pointer-events:none; filter:grayscale(0.8);',
      '}'
    ].join('\n');
    document.head.appendChild(css);
  }

  function ensureBanner() {
    var el = document.getElementById(BANNER_ID);
    if (el) return el;
    el = document.createElement('div');
    el.id = BANNER_ID;
    el.textContent = "\uD83D\uDEE0\uFE0F  System under maintenance. We'll be back shortly.";
    document.body.insertBefore(el, document.body.firstChild);
    return el;
  }

  function apply(stateObj) {
    try {
      ensureStyles();
      var banner = ensureBanner();
      var flag = !!(stateObj && stateObj.maintenance_mode === true);
      state.active = flag;
      if (flag) {
        banner.classList.add('visible');
        document.body.classList.add('maintenance-on');
      } else {
        banner.classList.remove('visible');
        document.body.classList.remove('maintenance-on');
      }
    } catch (e) { /* never break the host page */ }
  }

  // For pages that don't have a state-poll loop (e.g. history.html),
  // auto-start a lightweight poller against /api/state.
  function autopoll(intervalMs) {
    intervalMs = intervalMs || 15000;
    function tick() {
      try {
        fetch('/api/state', { cache: 'no-store' })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (s) { if (s) apply(s); })
          .catch(function () {});
      } catch (e) {}
    }
    tick();
    setInterval(tick, intervalMs);
  }

  window.MaintenanceMode = {
    apply: apply,
    isActive: function () { return !!state.active; },
    autopoll: autopoll,
  };
})();
