# FLOKIWATCH — Pending Items (March 19, 2026 — End of Session)

## CONFIRMED ✅ (This Session)

| Feature | Evidence |
|---|---|
| Dashboard shows Agent decision (not Scanner) | Screenshot: "AGENT DECISION: WAIT, 85%" |
| Watch conditions sidebar | Screenshot: "price_above: 4755.0, price_below: 4686.0" |
| Simba WATCHING + CONDITIONS: 2/3 | Screenshot: green WATCHING badge |
| Scanner DECISION/SIGNAL suppressed | Log: zero DECISION/SIGNAL lines after restart |
| Calendar timezone (Market Watch) | Screenshot: "15:30 — 3h51m" correct |
| Market Watch clock on dashboard | Screenshot: "MARKET WATCH: 11:58" |
| Log noise cleanup | Log: RESOLVE_PENDING=0, milestones only |
| Gemini implicit cache 79% hit rate | Log: "cached_tokens=16252" |
| Gemini PARTS diagnostic with finish_reason | Log: "finish_reason=FinishReason.STOP" |
| Rex balanced (37-80% agree) | Log: AGREE/DISAGREE counts |
| Trade Room full text (click-to-expand) | Screenshot: full messages visible |
| Pixel art redesign merged | Commit f3a121d merged to main |
| Trade TP +$54.09 | Log: "Take Profit, P&L=$+54.09" |
| Balance $866.78 | Log: startup balance |

## PENDING CONFIRMATION ⏳

| Feature | What's Needed |
|---|---|
| Simba 30s check frequency | Need a wake trigger to measure reaction time (<1 min) |
| Gemini retry on MALFORMED_RESPONSE | Need a MALFORMED event to trigger retry logic |
| Session memory daily cutoff | Need day rollover to verify stale notes cleared |
| JSON parser brace-matching | Need an "Extra data" event to verify recovery |
| Empty text STOP retry | Need an empty-text-with-STOP event |

## KNOWN ISSUES / FUTURE WORK

| Item | Priority | Notes |
|---|---|---|
| ML ensemble not audited | MEDIUM | 6 models (XGB/LGB/CatBoost x H1/H4) — no validation report exists. Floki uses ML as input. Need accuracy/precision metrics, out-of-sample test, overfitting check. Consider marking as "experimental" in Floki prompt until validated. |
| Scanner computation still runs | LOW | Brain still computes score/decision internally for Agent context. Phase 2: deprecate Scanner decision fields in FIELD_CONTRACT.md |
| Documentation update needed | LOW | README, SYSTEM_DOCUMENTATION, MONDAY_CHECKLIST need Gemini migration info |
| Trading desk multi-agent architecture | FUTURE | Luna (macro), Atlas (technical), Sage (auditor), Echo (news sentinel) — discussed, deferred until system is stable with 30-60 trades |

## KEY COMMITS (This Session — March 18-19)

| SHA | Description |
|---|---|
| `8efb6e7` | Agent-controlled scheduling |
| `190c274` | Timer gate fires agent |
| `5635d97` | Decouple analysis interval (60s) |
| `183e514` | typing.Any import fix |
| `e00dd6d` | Timeout 240s |
| `4f187d9` | Simba wake/watch fire |
| `9bdc65e` | Countdown delta |
| `6075c5e` | set_next_check fallback 5min |
| `ea8b76b` | Legacy triggers disabled |
| `2d4c5ba` | Rex prompt rebalanced |
| `a973845` | Trade Room display fixes |
| `8a6a0e7` | Log noise cleanup |
| `b557bb8` | GEMINI_PARTS diagnostic |
| `fd649fe` | Calendar timezone fix |
| `cfddec0` | Gemini MALFORMED retry |
| `7258f01` | Session memory daily cutoff |
| `898b9ea` | Trade Room click-to-expand |
| `c026132` | Market Watch clock |
| `ce2fcc4` | Watch conditions endpoint fix |
| `6030e5b` | Robust JSON parser + empty STOP retry |
| `f610d21` | Gemini cache usage logging |
| `f3a121d` | Pixel art theme |
| `2839103` | Watch conditions sidebar render |
| `f9b263e` | Dashboard agent decision + scanner suppression |
| `4d5c548` | DECISION logger no-op |
| `aebbb26` | Scanner decision info logs suppressed |
| `cab3eb8` | Simba 30s check frequency |

## SYSTEM ARCHITECTURE (Current)

| Component | Engine | Cost/call |
|---|---|---|
| Floki (Decision) | Gemini 3 Flash Preview | ~$0.04-0.07 |
| Rex (Debate) | GPT-4o | ~$0.03/debate |
| Simba (Monitor) | Python pure | $0 |
| Scanner (Data) | Python pure | $0 |
| EA Bridge | MQL5 | $0 |

Gemini implicit cache: ~79% hit rate → effective cost ~$0.02-0.03/call
