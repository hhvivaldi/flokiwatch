/**
 * tz.js — FLO-286 / CLAUDE.md Rule 22
 *
 * Single helper for displaying timestamps to the user.
 *
 * The contract:
 *   - Backend ALWAYS sends UTC ISO with explicit "Z" suffix (e.g. "2026-04-13T12:34:56Z").
 *   - Frontend converts to user's local time for display via these helpers.
 *
 * Usage:
 *   window.displayTime("2026-04-13T12:34:56Z")
 *     → "4/13/2026, 2:34:56 PM"  (uses browser locale + tz)
 *
 *   window.displayTimeShort("2026-04-13T12:34:56Z")
 *     → "14:34:56"  (24-hour HH:MM:SS, browser local)
 *
 *   window.displayDate("2026-04-13T12:34:56Z")
 *     → "4/13/2026"
 *
 *   window.displayHHMM("2026-04-13T12:34:56Z")
 *     → "14:34"
 *
 *   window.displayAge("2026-04-13T11:00:00Z")
 *     → "1h 35m ago"  (relative, computed from now)
 *
 * All helpers are tolerant: null/empty/garbage input returns "—".
 */
(function () {
  'use strict';

  function _parse(utcStr) {
    if (utcStr == null || utcStr === '') return null;
    try {
      // Defensive: if backend forgot the Z, treat as UTC anyway (matches Rule 22).
      var s = String(utcStr);
      if (!/[Zz]|[+\-]\d{2}:?\d{2}$/.test(s)) {
        s = s + 'Z';
      }
      var d = new Date(s);
      if (isNaN(d.getTime())) return null;
      return d;
    } catch (e) {
      return null;
    }
  }

  function _pad(n) { return n < 10 ? '0' + n : '' + n; }

  window.displayTime = function (utcStr) {
    var d = _parse(utcStr);
    if (!d) return '—';
    return d.toLocaleString();
  };

  window.displayTimeShort = function (utcStr) {
    var d = _parse(utcStr);
    if (!d) return '—';
    return _pad(d.getHours()) + ':' + _pad(d.getMinutes()) + ':' + _pad(d.getSeconds());
  };

  window.displayDate = function (utcStr) {
    var d = _parse(utcStr);
    if (!d) return '—';
    return d.toLocaleDateString();
  };

  window.displayHHMM = function (utcStr) {
    var d = _parse(utcStr);
    if (!d) return '—';
    return _pad(d.getHours()) + ':' + _pad(d.getMinutes());
  };

  window.displayAge = function (utcStr) {
    var d = _parse(utcStr);
    if (!d) return '—';
    var nowMs = Date.now();
    var diffSec = Math.floor((nowMs - d.getTime()) / 1000);
    if (diffSec < 0) return 'in future';
    if (diffSec < 60) return diffSec + 's ago';
    var mins = Math.floor(diffSec / 60);
    if (mins < 60) return mins + 'm ago';
    var hours = Math.floor(mins / 60);
    var remMins = mins % 60;
    if (hours < 24) return hours + 'h ' + remMins + 'm ago';
    var days = Math.floor(hours / 24);
    var remHours = hours % 24;
    return days + 'd ' + remHours + 'h ago';
  };

  // Back-compat alias for trade_room.html's existing fmtTime() helper
  window.fmtTime = window.displayTimeShort;
})();
