# SIMBA ARCHITECTURE — FlokiWatch

## The Team
```
FLOKI (Claude Sonnet) ---- O CHEFE / TRADER
   |                        Analisa, decide, abre/fecha trades
   |                        Caro: $0.16/call
   |                        Só acorda quando necessário
   |
SIMBA (GPT-4o-mini) ------ A SECRETÁRIA
   |                        NÃO analisa mercado
   |                        NÃO toma decisões
   |                        Só verifica checklist do Floki
   |                        Barato: $0.002/call
   |
REX (GPT-4o) ------------- O COLEGA DE DEBATE
                            Só chamado quando Floki quer OPEN/CLOSE
                            Debate com dados
                            Barato: $0.005/turn
```

## Flow — When Floki says WAIT (no position open)

```
┌─────────────────────────────────────────────────────────┐
│ STEP 1: FLOKI WAKES UP (trigger or max sleep expired)   │
│                                                         │
│  Floki calls 15-20 tools, investigates everything,      │
│  debates with Rex if needed, then decides: WAIT         │
│                                                         │
│  BEFORE sleeping, Floki writes a CHECKLIST for Simba:   │
│                                                         │
│  "Simba, here's what I need you to watch:               │
│   ☐ Price breaks above 5022                             │
│   ☐ Price drops below 5002                              │
│   ☐ Volume on H1 candle > 10,000                        │
│   ☐ Scanner detects bearish engulfing pattern            │
│   ☐ Max sleep: 2 hours                                  │
│                                                         │
│   If ANY of these happen, wake me up."                  │
│                                                         │
│  Cost: $0.16 (one-time)                                 │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│ STEP 2: FLOKI SLEEPS — SIMBA WATCHES (every 30 min)    │
│                                                         │
│  Scanner runs every 1 min (FREE, Python)                │
│  Scanner output: price=5014, RSI=50, volume=179,        │
│                  pattern=none, ADX=36...                 │
│                                                         │
│  Every 30 min, Simba receives:                          │
│    - Scanner data (compact summary)                     │
│    - Floki's checklist (the conditions above)           │
│                                                         │
│  Simba checks the list:                                 │
│    ☐ Price > 5022? Current 5014. NO                     │
│    ☐ Price < 5002? Current 5014. NO                     │
│    ☐ H1 volume > 10,000? Current 179. NO               │
│    ☐ Bearish engulfing? Scanner says none. NO           │
│    ☐ 2 hours passed? Only 30 min. NO                    │
│                                                         │
│  Simba says: SLEEP — no conditions met.                 │
│  Trade Room shows: "SIMBA: Monitoring... 0/5 met"       │
│                                                         │
│  Cost: $0.002                                           │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
                  (30 min later)
                       │
┌─────────────────────────────────────────────────────────┐
│ STEP 2 REPEATS: SIMBA CHECKS AGAIN                     │
│                                                         │
│  Scanner data: price=5025, RSI=58, volume=8500,         │
│                pattern=none, ADX=42                      │
│                                                         │
│  Simba checks:                                          │
│    ☑ Price > 5022? Current 5025. YES!                   │
│    ☐ Price < 5002? NO                                   │
│    ☐ H1 volume > 10,000? Current 8500. NO (close)      │
│    ☐ Bearish engulfing? NO                              │
│    ☐ 2 hours passed? NO                                 │
│                                                         │
│  Simba says: WAKE — condition #1 triggered!             │
│  Trade Room shows: "SIMBA: ⚡ ALERT — Price > 5022!"    │
│                                                         │
│  Cost: $0.002                                           │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│ STEP 3: FLOKI WAKES UP (Simba triggered)               │
│                                                         │
│  Floki receives: "Price broke 5022, now at 5025.        │
│  Condition #1 from your checklist was met."             │
│                                                         │
│  Floki investigates (15-20 tools), decides:             │
│    - OPEN BUY? → debates with Rex → executes            │
│    - WAIT again? → writes NEW checklist for Simba       │
│                                                         │
│  Cost: $0.16                                            │
└─────────────────────────────────────────────────────────┘
```

## Flow — When Floki has open position (HOLD_TRADE)

```
┌─────────────────────────────────────────────────────────┐
│ POSITION OPEN — FLOKI STAYS AWAKE (every 30 min)       │
│                                                         │
│  When there's an open trade, the risk justifies         │
│  the cost. Floki is called every H1 close as now.      │
│                                                         │
│  Simba is NOT involved when position is open.           │
│  The existing Monitor + Watch Conditions handle this.   │
│                                                         │
│  When position closes → Floki analyzes result →         │
│  if no new trade → writes checklist for Simba → sleeps  │
└─────────────────────────────────────────────────────────┘
```

## What Simba IS and IS NOT

```
SIMBA IS:                           SIMBA IS NOT:
✅ A checklist verifier              ❌ A market analyst
✅ Cheap ($0.002/call)               ❌ A decision maker
✅ Follows Floki's instructions      ❌ Independent thinker
✅ Compares numbers and flags         ❌ A trader
✅ Reads Scanner output              ❌ Able to override Floki
✅ Says SLEEP or WAKE only           ❌ Able to open/close trades
✅ Shows status in Trade Room        ❌ Able to debate with Rex
```

## Why Simba needs to be AI (not just Python if/else)

Some conditions are simple: "price > 5022" → Python can do this.

But Floki might write:
- "Volume significantly above average" → Simba needs to know what "significant" means
- "Scanner detects reversal pattern" → Simba reads Scanner pattern field
- "If DXY reverses direction" → Simba needs to interpret "reverse"

GPT-4o-mini is smart enough for this interpretation but costs almost nothing.

HOWEVER: to keep Simba reliable, Floki should write conditions as 
SPECIFIC as possible:
- GOOD: "Price > 5022" or "H1 volume > 10000"  
- OK: "Scanner detects bearish engulfing"
- RISKY: "Market sentiment changes" (too vague for Simba)

## Cost Comparison

CURRENT (no Simba):
- 24 Floki calls/day × $0.16 = $3.84/day = $115/month

WITH SIMBA:
- 48 Simba calls/day × $0.002 = $0.10/day
- 6-8 Floki calls/day × $0.16 = $1.12/day  
- 2-3 Rex debates × $0.02 = $0.06/day
- TOTAL: $1.28/day = $38/month

SAVINGS: ~$77/month (67% reduction)
```
