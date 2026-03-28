# XAUUSD Trading Bot — Documentação Completa do Sistema

> **Versão:** Março 2026 | **Modo:** DEMO (Capital Point Demo) | **Símbolo:** XAU/USD | **Timeframe:** H1 | **Balance:** $813.76 (82 trades Pop B)

---

## 1. Visão Geral

Bot de trading algorítmico automatizado para XAU/USD com arquitetura **Agent-first**. O sistema separa claramente:

- **Brain (Python)**: pipeline de dados e cálculo (60s). Busca dados do MT5, calcula indicadores, executa ML, busca news/macro/calendário, calcula zonas de S/R. **Não decide trades** e **não executa ordens**.
- **Floki (GPT-5.4, 28 tools)**: portfolio manager e único decisor. Agenda o próprio ciclo via `set_next_check` (5-120 min). Decide **WAIT / OPEN_BUY / OPEN_SELL / HOLD / CLOSE / ADJUST**. Active thesis persistence (active_thesis.json). 91-line prompt (FLO-128).
- **Rex (GPT-5 mini, 9 tools)**: co-pilot com acesso independente a dados. Verifica dados antes de concordar/discordar. Ajuda refinar planos (SL, entry, timing). Debates em qualquer decisão.
- **Simba (Python, zero AI cost)**: watchdog. Monitoriza 10 tipos de condições (price, RSI, volume, ADX, scanner_pattern, pnl_threshold, indicator) a cada 30s. Acorda o Floki quando condições são atingidas.
- **Luna (MiMo-V2-Flash)**: macro analyst. Analisa DXY, VIX, yields, oil, S&P 500, gold, Echo alerts + calendário a cada 15 min. Produz `luna_brief.json` com environment (SAFE/CAUTION/DANGER), padrões (forced_liquidation, safe_haven_flow, etc.), bias direcional. Quando Luna está ativa, Floki perde acesso a `get_macro` e `get_headlines` (Luna já processou esses dados).
- **Echo (MiMo-V2-Flash)**: news sentinel 24/7. Monitoriza 25 feeds RSS (11 diretos + 14 Google News) a cada 5 min. Classifica headlines como CRITICAL/IMPORTANT/ROUTINE. CRITICAL acorda Floki imediatamente (max 2/hora) + trigger Luna out-of-cycle. Feed health tracking com alerta a 3+ falhas consecutivas.
- **Sage (Gemini)**: auditor diário. Corre às 21:00 UTC com relatório de performance (win rate, profit factor, recomendações).
- **EA Bridge (MQL5, tick-by-tick)**: execução e gestão intra-tick (breakeven, trailing stop).

**Nota:** Agent Fast (gestão de emergência) e legacy triggers (Python Monitor) estão **desabilitados**. O allowlist gate em `agent_proactive_out_of_cycle()` só aceita: SCHEDULED, SIMBA_WAKE, SIMBA_WATCH, ECHO_CRITICAL.

**O que faz:**
- Brain atualiza o pacote de dados (1 min)
- Agent Proactive decide a ação (30 min + triggers)
- Executa ordens via EA Bridge (tick-by-tick) e/ou via Python quando aplicável
- Gere posições via EA Bridge e via Agent Fast quando há risco/alerta
- Notifica via Discord e apresenta tudo num dashboard web (FlokiWatch)

**Princípios:** Nunca bloquear (fallback gracioso), segurança primeiro (múltiplas camadas), transparência (explicação de cada decisão), adaptabilidade (pesos dinâmicos).

---

## 2. Arquitetura

```
main.py (Orquestrador — Trading Office)
  ├── Brain Cycle (60s) → central_brain.py (Pipeline de dados, não decide)
  │     ├── MT5 data fetch (rates, ticks, positions, account)
  │     ├── calculate_indicators.py / calculate_technical_score.py
  │     ├── ml_predictor.py (ML Ensemble — 6 models)
  │     ├── news_score_hybrid.py (25 RSS feeds + DXY/VIX/Yields)
  │     ├── economic_calendar.py (calendário)
  │     └── support_resistance.py (zonas S/R + Fibonacci)
  ├── Floki (GPT-5.4, 28 tools, self-scheduled 5-120 min) → ai_agent.py
  │     ├── 20+ tools (market data, trading, memory, debate)
  │     └── decide: WAIT / OPEN_BUY / OPEN_SELL / HOLD / CLOSE / ADJUST
  ├── Rex (GPT-5 mini, 9 tools, co-pilot) → rex_validator.py
  │     └── AGREE / DISAGREE + reasoning (HOLD/WAIT/ADJUST skipped)
  ├── Luna (MiMo-V2-Flash, every 15 min) → luna_analyst.py
  │     ├── DXY/VIX/yields/oil/S&P 500/gold + Echo alerts + calendar
  │     ├── Environment: SAFE/CAUTION/DANGER + pattern detection
  │     └── luna_brief.json → Floki reads via get_luna_brief
  ├── Echo (MiMo-V2-Flash, every 5 min) → echo_sentinel.py
  │     ├── 25 RSS feeds → keyword pre-filter → MiMo classification
  │     ├── CRITICAL → Simba wake → Floki immediately (max 2/hr) + Luna out-of-cycle
  │     └── IMPORTANT → echo_alerts.json → Luna reads on next cycle
  ├── Simba (Python, every 30s, zero AI cost) → simba_watcher.py
  │     └── 10 condition types → triggers Floki out-of-cycle
  ├── Sage (Gemini, daily 21:00 UTC) → sage_auditor.py
  │     └── performance report + recommendations
  ├── Execution
  │     ├── EA Bridge (tick-by-tick) → ea_bridge.py ↔ mql5/FlokiBridge.mq5
  │     └── executor.py (fallback via MT5 API)
  ├── Deal Resolution → deal_resolver.py (subprocess, MT5 reconnect)
  └── Output
        ├── alerts.py → Discord (10+ webhook channels)
        ├── state_writer.py → bot_state.json (Dashboard)
        ├── db_writer.py → history.db (SQLite)
        └── dashboard/ → FastAPI (port 8080) — Trade Room, History, Intel Feed
```

**Startup:** Conecta MT5 → Reconcilia estado (3-pass) → Loop principal. Dashboard lançado separadamente (uvicorn, porta 8080).

**Loop:** Verifica mercado aberto → Se sim: análise + monitor. Se não: só monitor + sleep diferenciado (weekend=300s, pausa=60s).

---

## 3. O Brain (central_brain.py) — Pipeline de Dados (não decide)

**Ficheiro:** `central_brain.py`

O Brain é responsável por **recolher e preparar dados**, mantendo a consistência do pacote que alimenta os agentes:

- Dados do MT5 (rates multi-TF, preços, spread, posições, conta)
- Indicadores técnicos e features
- Output do ML ensemble
- News/macro/sentimento e calendário
- Zonas de suporte/resistência

O Brain **não faz interpretações finais** (BUY/SELL/HOLD) e **não executa trades**. A decisão é exclusivamente do Agent Proactive.

O output do Brain é consumido por:

- Agent Proactive (decisão principal)
- Python Monitor (deteta condições e chama agentes)
- Dashboard (visibilidade e auditoria)

---

## 4. Floki — Portfolio Manager e Único Decisor

**Modelo:** GPT-5.4 (`gpt-5.4`) | **Temperature:** 1.0 | **API:** OpenAI
**Ficheiro:** `ai_agent.py`, `agent_tools.py`, `agent_prompts.py`

**Quando corre:**

- Auto-agendado via `set_next_check` (5-120 min, default 5 min)
- Fora do ciclo: SIMBA_WAKE (condição atingida), SIMBA_WATCH (posição em risco), ECHO_CRITICAL (breaking news)

**28 ferramentas disponíveis (4 categorias: Technical, Cross-market, Macro, Performance):**

- Dados: get_current_price, get_candles, get_indicators, get_sr_zones, get_fibonacci_levels, get_headlines, get_macro, get_calendar, get_ml_prediction
- Trading: execute_trade, close_trade, adjust_trade
- Gestão: set_wake_conditions, set_watch_conditions, set_next_check
- Memória: read_session_memory, write_session_memory, get_trade_patterns
- Colaboração: debate_with_rex, get_echo_alerts
- Contexto: get_open_positions, get_trade_history, get_account_info, get_position_events

**O que decide:** WAIT / OPEN_BUY / OPEN_SELL / HOLD_TRADE / CLOSE_TRADE / ADJUST_TRADE

---

## 5. Rex — Debate Partner

**Modelo:** GPT-5 mini (`gpt-5-mini`) | **9 tools:** price, candles, indicators, S/R zones, market_context, luna_brief, fibonacci, trade_lessons, open_positions
**Ficheiro:** `rex_validator.py`

Chamado pelo Floki via ferramenta `debate_with_rex`. Recebe a direção, raciocínio, confiança e dados-chave do Floki. Responde com AGREE/DISAGREE e justificação. **Apenas decisões OPEN/CLOSE ativam o Rex** — HOLD/WAIT/ADJUST são skippados (FLO-50, otimização de custo).

---

## 6. Echo — News Sentinel (24/7)

**Modelo:** MiMo-V2-Flash (Xiaomi API, env: ECHO_MODEL, base: ECHO_API_BASE)
**Ficheiro:** `echo_sentinel.py`

Monitoriza 25 feeds RSS (11 diretos + 14 Google News) a cada 5 min. Pipeline:

1. RSS fetch → 2. Dedup (MD5 hash, 24h expiry) → 3. Keyword pre-filter (~70% filtrado, $0) → 4. MiMo classification (CRITICAL/IMPORTANT/ROUTINE)

- **CRITICAL**: acorda Floki imediatamente via `ECHO_CRITICAL` trigger (max 2/hora) + trigger Luna out-of-cycle
- **IMPORTANT**: guardado em `data/echo_alerts.json`, Luna lê no próximo ciclo
- **ROUTINE**: descartado
- **Market closed**: só processa CRITICAL (IMPORTANT suprimido)
- **Feed health**: tracking por feed em `data/echo_feed_health.json`, alerta a 3+ falhas consecutivas
- **Custo**: ~$0.10-0.30/M tokens via MiMo. Daily cap $1.00.
- **API Key**: `LUNA_API_KEY` (partilhada com Luna — mesma conta Xiaomi)

---

## 6a. Luna — Macro Analyst

**Modelo:** MiMo-V2-Flash (Xiaomi API)
**Ficheiro:** `luna_analyst.py`

Analisa o ambiente macro a cada 15 min (market open) ou 30 min (daily pause). **Não corre durante o weekend** — acorda 1h antes do market open (Domingo 21:00 UTC).

**Dados de entrada:** DXY, VIX, Treasury Yields 10Y, Oil (WTI), S&P 500, Gold (preço + change), Echo alerts (CRITICAL/IMPORTANT), calendário económico.

**Output:** `data/luna_brief.json` com:
- `environment`: SAFE / CAUTION / DANGER
- `risk_level`: 1-10 (calibrado por environment)
- `directional_bias`: BULLISH / BEARISH / NEUTRAL com `bias_confidence` 1-10
- `patterns_detected`: forced_liquidation, safe_haven_flow, news_price_divergence, dollar_gold_correlation_break
- `market_regime`: risk_on / risk_off / mixed / crisis
- `data_snapshot`: valores atuais de todos os indicadores macro

**Integração com Floki:**
- Floki chama `get_luna_brief` no início de cada ciclo
- Quando Luna brief é fresh (< 30 min): `get_macro` e `get_headlines` são **removidos** da tool list do Floki (FLO-59)
- Quando Luna brief é stale (> 30 min): fallback — tools macro restaurados automaticamente
- `get_echo_alerts` mantém-se **sempre** (para CRITICAL emergencies via Simba wake)

**Fallback:** Se MiMo API falha, Luna usa análise determinística local (regras if/else).

**Custo:** ~$0.10-0.30/M tokens. Daily cap $1.00.

---

## 6b. Simba — Watchdog (Python, zero AI cost)

**Ficheiro:** `simba_watcher.py`

Corre a cada 30s. Monitoriza condições definidas pelo Floki via 10 tipos de condição:

**Wake conditions** (sem posição aberta): `price_above`, `price_below`, `rsi_above`, `rsi_below`, `volume_above`, `adx_above`, `scanner_pattern`, `indicator_above`, `indicator_below`, `max_sleep_minutes`

**Watch conditions** (com posição aberta): `price_touch`, `pnl_threshold`, `indicator_threshold`

Quando condição é atingida → `agent_proactive_out_of_cycle("SIMBA_WAKE")` ou `SIMBA_WATCH`. Detalhes das condições visíveis no Trade Room card expand panel.

---

## 6c. Sage — Auditor Diário

**Modelo:** Gemini (SAGE_API_KEY)
**Ficheiro:** `sage_auditor.py`

Corre uma vez por dia às 21:00 UTC (Mon-Fri). Analisa trades recentes, calcula win rate e profit factor, gera recomendações. Relatório em `data/sage_report.json`.

---

## 6d. Agent Fast — DESABILITADO

O Agent Fast (gestão de emergência) e os 6 triggers do Python Monitor estão desabilitados. O allowlist gate em `agent_proactive_out_of_cycle()` rejeita todos os triggers legacy. Floki + Simba + Echo cobrem todos os cenários.

---

## 4. Os 5 Pilares

### Pesos Base:
| Pilar | Peso | Ficheiro |
|-------|------|----------|
| Técnico | 30% | `technical_analyzer.py` |
| ML | 25% | `ml_predictor.py` |
| Momentum | 15% | `momentum_detector.py` |
| News | 20% | `news_score_hybrid.py` |
| Calendário | 10% | `economic_calendar.py` |

### 4.1 Técnico (30%)
Score 0-100 combinando RSI(14), MACD, Bollinger Bands, EMAs (9/21/50), ATR(14). Inclui Visual Context Features (velas consecutivas, body trend, engulfing, pin bar) com cap ±8 pontos.

### 4.2 ML Ensemble v3.1 (25%)
6 models: XGBoost + LightGBM + CatBoost × H1 + H4. Blend 40% H1 + 60% H4. ~48 features (technical, session, multi-timeframe H4/M5, macro, cross-asset, sentiment, regime, interactions). Rank-based calibration. Final score 10-90. Training data: Jan 2023 → Feb 2026 (3 years). Walk-forward: 12 folds. WF AUC ~0.79 (H1), ~0.74 (H4).

Ajuste dinâmico: momentum ≥80 + forte → ML perde 5-15% peso, momentum ganha.

### 4.3 Momentum (15%)
Score 0-100 baseado em ADX (+30 max), Volume (+20 max), Velas Consecutivas (+20 max), ATR Trend (+15 max), Breakout (+15 max). Direção por votação multi-sinal. Força: very_strong/strong/moderate/weak/very_weak.

### 4.4 News Híbrido (20%)
Score 0-100 = Headlines 40% (keywords, RSS Google News) + DXY 30% (Yahoo Finance, inverso) + Yields 20% (Yahoo Finance, inverso) + VIX 10% (Yahoo Finance, direto). Cache 30 min. Também recolhe Oil (WTI CL=F) + S&P 500 (^GSPC/ES=F) para Luna macro analyst (não pesam no score).

### 4.5 Calendário (10%)
Score 0-100. Fontes: MQL5 Bridge JSON → FCS API → Hardcoded. Fases: NORMAL(50), PRE_EVENT(20), DURING(0), POST_EVENT(15-85 conforme bias). Eventos: NFP, CPI, FOMC, Jobless Claims, GDP, etc. Nunca bloqueia — score 0 puxa final para HOLD organicamente.

---

## 5. Cenários e Pesos Dinâmicos

| Cenário | Condição | Multiplicador |
|---------|----------|---------------|
| `volatilidade_extrema` | Vol Guard = EXTREME | 0.00 (HOLD forçado) |
| `alinhamento_perfeito` | Todos concordam + RSI guard | 1.15 |
| `janela_pos_evento` | Calendar>70 + Momentum>60 + bias alinhado | 1.10 |
| `lateralizacao` | Momentum weak + ML conf<55% + tech 40-60 | 0.85 |
| `sinais_conflitantes` | Tech vs ML discordam | 0.80 |
| `padrao` | Fallback | 1.00 |

**Filtros:** Parabolic Exhaustion (RSI>80 + momentum>90 → conf ×0.70). Momentum-contra-ML (momentum<30 + abaixo EMAs → bloqueia BUY).

**Pesos dinâmicos (após cenário):** `_adjust_weights()` sempre roda depois da seleção de cenário e pode ajustar pesos mesmo em `padrao`. Ex.: se `ml_confidence > 0.70` (estritamente maior, não `>=`), ML recebe +10% e os outros quatro pilares dividem igualmente -10% (−2.5% cada).

---

## 6. Cálculo de Confiança

```
Base: 50 × scenario_multiplier
+ Confirmações: +3 cada | - Alertas: -3 cada
+ ML: >70%→+15, >60%→+10, >55%→+5, ≥50%→0, <50%→-10
+ Momentum: very_strong→+15, strong→+10, moderate→0, weak/very_weak→-10
+ Volume Gate: <0.3x avg→-25, <0.5x avg→-15 (NEW Feb 2026)
+ MTF Trend: aligned→+10, conflict→-20 (NEW Feb 2026)
+ Fundamentals: positive→+10, negative→-10
+ Calendar: alinhado→+10, contra→-15
- Indecision (score 45-55): até -20
= Clamp [0, 100]
```

### 6.1 Multi-TF Trend Confirmation (NEW Feb 2026)

Verifica se a direção do trade está alinhada com a tendência D1 e H4 usando EMA50:
- **Cálculo:** Price > EMA50 = bullish, Price < EMA50 = bearish
- **Se D1 e H4 concordam:**
  - Trade alinhado com tendência: **+10 confiança**
  - Trade contra tendência: **-20 confiança**
- **Se D1 e H4 discordam:** Sem ajuste (sinais mistos)

**Motivação:** Trades Bot-SELL-34/35 (Feb 20, 2026) venderam contra uptrend claro D1+H4 com volume baixo. MTF teria aplicado -20 conf, Volume Gate -25 conf → ambos bloqueados (conf < 55%).

### 6.2 Volume Gate (NEW Feb 2026)

Penaliza trades com volume baixo (falsos breakouts):
- **Volume < 0.3x média:** -25 confiança (severo)
- **Volume < 0.5x média:** -15 confiança (moderado)

**Config:** `VOLUME_GATE_ENABLED`, `VOLUME_GATE_MODERATE_THRESHOLD`, `VOLUME_GATE_SEVERE_THRESHOLD`

Níveis: very_high(≥80), high(≥65), moderate(≥50), low(≥35), very_low(<35).

---

## 7. GPT Confidence Validator

**Ficheiro:** `gpt_confidence.py` | **Modelo:** gpt-4o-mini (legacy, may be updated)

Pós-Cérebro, ajusta confiança ±15 pontos. Ações: CONFIRM/BOOST/REDUCE.

**Smart Cache:** Só chama GPT se cenário mudou ou pilar variou ≥5 pontos (~30-60 calls/dia vs 288).

**Regras:** Nunca boost durante pre/during_event ou COOLING_DOWN. BOOST em HOLD = 0. Max ±15. "When in doubt, CONFIRM."

**Cycle Memory:** Recebe contexto temporal (HOLDs consecutivos, momentum persistente, missed opportunities).

**Fallback:** Qualquer falha = CONFIRM. Não chamado quando EXTREME.

---

## 8. Volatility Guard

**Ficheiro:** `volatility_guard.py` | **Stateless** (recalcula de M5)

Lógica 2-candle: Vela M5 ≥1.8% → EXTREME. Vela seguinte decide:
- Cancel A: body<0.5% → NORMAL
- Cancel B: oposta + ≥1.0% → NORMAL
- Confirm A: mesma + ≥1.0% → COOLING 90min
- Ambíguo → COOLING 30min

| Status | Trading | Monitor |
|--------|---------|---------|
| NORMAL | Sem restrições | Normal |
| EXTREME | Bloqueio total | Normal |
| COOLING_DOWN | Só conf≥70% | BE agressivo (50 pips), Trailing (80/50 pips) |

---

## 9. M5 Reversal Detection

**Ficheiro:** `momentum_detector.py`

Verifica últimas 6 velas M5 antes de executar trade:
- Move contra ≥0.40% → **Strong** → Bloqueia
- Move contra ≥0.20% → **Moderate** → Confiança -15
- <0.20% → OK

**M5 Score Adjustment** (no Cérebro): Score<45 + M5 bullish >0.15% → +3 a +7. Score>55 + M5 bearish → -3 a -7. Confirma → ±2 bónus.

---

## 10. Safety Checks

**Ficheiro:** `safety_checks.py` — FLO-118: Blocking removido. Apenas 3 checks ativos: MT5 connected, market hours, FLO-85 opposing positions.

1. MT5 conectado
2. Mercado aberto (Dom 22:00→Sex 21:00 UTC, pausa 21-22 UTC)
3. Buffer fecho (5 min antes)
4. Buffer abertura (60 min após)
5. Max perdas consecutivas (3 → pausa 2h)
6. Pausa ativa
7. Max posições (3)
8. Max perda diária (5%)
9. Anti-overtrading (trailing→30min, SL→45min cooldown)
10. Smart Pyramid (2ª posição só se 1ª em lucro ≥0.3%)

---

## 11. Gestão de Risco

**Position Sizing:** 2% do saldo por trade. Volume = (saldo×2%) / (SL_pips × valor_pip). Min 0.01, max 1.0 lotes.

**SL:** ATR × 2.0 (min 150, max 800 pips). **TP:** ATR × 3.0.

**Breakeven:** Dinâmico = 50% dos pips do SL original (changed from 70% after 6-month backtest: +5.7% WR, +0.44 PF). Fallback: fixo 100 pips quando SL não está disponível; em **COOLING_DOWN** usa 50 pips fixos. Move SL para entrada.

**Trailing:** Trigger = 150 pips de lucro. Distância = 100 pips atrás do preço.

**Limites:** Max time 48h, max drawdown 1000 pips (emergency safety net), max daily loss 5%.

**P&L Source of Truth (live):** o P&L primário é apurado por **balance diff** quando o EA fecha posições (tick-by-tick). O Python usa isto como fonte robusta para “o que realmente aconteceu”, mesmo quando a resolução de deals no MT5 API falha temporariamente.

**Deal Resolver (fallback):** um subprocesso (`deal_resolver.py`) faz resolução detalhada de fechos com reconnect ao MT5 quando necessário, para enriquecer histórico (close type, SL/TP/BE/trailing) e reduzir “unknown closures”.

---

## 12. Monitor (Arquitetura Atual)

O sistema tem dois níveis de monitorização:

1. **EA Bridge (tick-by-tick)**: breakeven e trailing no próprio MT5 (menor latência, mais precisão).
2. **Python Monitor (1 min)**: triggers e chamadas ao Agent Fast/Proactive para gestão contextual (eventos, risco, drawdown).

O Monitor também integra:

- Balance diff para P&L real quando posições fecham no EA
- Deal resolver para detalhamento quando a API MT5 não devolve informação imediatamente

---

## 13. Executor MT5

**Ficheiro:** `executor.py`

Interface MT5: connect, disconnect, account info, prices, spread, execute trade, positions, close, modify SL/TP.

**Deal History 3-Level Search:** N1 (position param) → N2 (broad search) → N3 (smart estimation: compara distância SL vs TP com tick price). Retry: [0s, 5s, 15s].

**Dual MT5 Call:** `get_recent_closed_deals()` faz 2 chamadas (long-range + today-only) + merge, contornando bug MT5 API.

### 13.1 Visualização Gráfica (S/R Zones)

**Arquitetura:** Bridge Pattern (Python escreve JSON → MQL5 lê e desenha).
- **Python:** `main.py` escreve `sr_zones.json` em `MQL5\Files\` a cada ciclo de análise (8 zonas: 4 suporte, 4 resistência).
- **MQL5:** `SRZoneDrawer.mq5` (EA) lê o JSON a cada 10s e atualiza objetos no gráfico.

**Visual:**
- **Cores:** Verde (Suporte), Vermelho (Resistência), Dourado (Flip).
- **Espessura:** 
  - 1: H1 (fraco)
  - 2: H4 ou Forte (≥4 toques)
  - 3: D1 ou MTF (Confluência)
- **Labels:** Alinhados à direita, ex: `D1 MTF FLIP 35T` (Timeframe, Tipo, Toques).

---

## 14. Dashboard FlokiWatch

**Ficheiros:** `dashboard/` — FastAPI porta 8050.

- FLOKI SIGNAL card (BUY/SELL/HOLD + HOLD Forçado)
- 5 Pilares com barras de progresso
- Últimas 5 Decisões (SQLite, polling 10s)
- Trades Hoje (P&L, close type, badge estimado)
- Stats: W:X L:Y BE:Z WR:XX%
- INTEL FEED (headlines, macro DXY/Yields/VIX, calendário, GPT validator, confirmações/alertas)
- Floki decorativo (vídeo)

---

## 15. Live Performance Population Split (Pre-Commit vs Current)

**Contexto:** As primeiras trades (tickets #1–7) foram executadas antes do primeiro commit (Feb 20, 2026).
Esse sistema era **pre-commit**, com **4 pilares** (sem Calendário), pesos diferentes e loop de análise a cada **300s**.
As trades #8–22 são do **sistema atual**, com **5 pilares**, `BRAIN_MIN_CONFIDENCE = 55.0` e **monitor a cada 10s**.
Por isso, **as duas populações não são comparáveis** e devem ser analisadas separadamente.

### População A — Trades #1–7 (Pre-Commit, 4 pilares)
- **Sistema:** 4 pilares (Tech/ML/Momentum/News), **sem Calendário**
- **Pesos:** Tech 35% / ML 25% / Momentum 20% / News 20%
- **Loop:** 300s (monitor integrado no ciclo de análise)
- **Confidence floor:** desconhecido (pre-commit)
- **Métricas:** 2W / 5L (WR 28.6%)
  - **Gross Profit:** $32.81
  - **Gross Loss:** $54.10
  - **Net P&L:** -$21.29
  - **PF:** 0.606
- **Nota:** Não comparar com o sistema atual.

### População B — Trades #8–22 (Sistema Atual, 5 pilares)
- **Sistema:** 5 pilares (inclui Calendário)
- **Confidence floor:** `BRAIN_MIN_CONFIDENCE = 55.0`
- **Monitor:** 10s com posições abertas
- **Métricas:** 8W / 7L (WR 53.3%)
  - **Gross Profit:** $197.73
  - **Gross Loss:** $206.65
  - **Net P&L:** -$8.92
  - **PF:** 0.96

**Atenção:** 15 trades (#8–22) **não constituem amostra estatística válida**. Não tirar conclusões de performance até ≥30 trades do sistema atual.

## 15. Alertas Discord

Signal, Trade Opened/Closed, Breakeven, M5 Block, Market Open/Closed, Heartbeat (Full: cenário mudou ou score±8; Short: sem mudanças), Daily Summary, Errors.

Heartbeat: 60 min quando em HOLD sem posições.

---

## 16. Persistência

- **JSON** (`data/bot_state.json`): Estado atual para dashboard (sobrescrito cada ciclo)
- **SQLite** (`data/history.db`): Histórico append-only (analyses, trades, account_snapshots)
  - **Caveat JOIN (analyses ↔ trades):** apenas quando o gap de timestamp é < 5 min; gaps maiores não devem ser unidos.
- Ambos try/except, nunca bloqueiam o bot. SQLite WAL mode.

---

## 17. Cycle Memory

**Ficheiro:** `cycle_memory.py` — Últimos 36 snapshots (~3h).

Detecta: HOLDs consecutivos, momentum persistente, cenário instável, missed opportunity (≥12 HOLDs + ≥4 momentum forte + avg score≥55 + price change>0.3%). Fornece contexto ao GPT Validator.

---

## 18. Pipeline ML

1. `scripts/collect_training_data.py` — Fetch H1/H4/M5 from MT5, compute features, generate labels (Jan 2023 → Feb 2026)
2. `scripts/train_ensemble.py` — Walk-forward CV (12 folds), SHAP feature selection, train 6 models, save percentiles
3. Artifacts in `models/` (ensemble_config.json, 6 model files, ensemble_shap.json)

**Current version:** v3.1 (deployed Feb 18, 2026). WF AUC stable vs v3, M5 SHAP healthy. +$302 P&L vs v3 on same 18-month backtest, PF 2.25 vs 2.14.

Train/live consistency: M5 features use lookback (last N candles), not "candles within the hour".

---

## 19. Backtest

**Ficheiro:** `scripts/run_backtest.py`

```bash
python scripts/run_backtest.py --start 2026-01-27 --end 2026-02-16
```

**Resultados validados:**
| Período | Trades | WR% | P&L | PF | Max DD |
|---------|--------|-----|-----|-----|--------|
| In-sample (Jan 27-Feb 16) | 33 | 81.8% | +$985 | 3.53 | $137 |
| Out-of-sample (Dec 15-Jan 26) | 56 | 67.9% | +$298 | 1.62 | $123 |

OOS PF 1.62 > 1.5 target. Sistema validado, não overfitted.

---

## 20. Modos de Operação

| Modo | Descrição |
|------|-----------|
| `DRY_RUN` | Simulação, sem ordens MT5 |
| `DEMO` | MT5 real, conta demo ($ fake) — **modo atual** |
| `LIVE` | MT5 real, conta real |

---

## 21. Fluxo Completo de um Ciclo

**Brain cycle (1 min):**
1. MT5 data fetch (multi-TF, conta, posições, spread)
2. Cálculo de indicadores/features + ML + news/macro + calendário + S/R
3. Persistência (SQLite + JSON) para dashboard e auditoria

**Agent Proactive (30 min + triggers):**
1. Recebe pacote de dados brutos do Brain + estado do sistema
2. Decide `WAIT / OPEN / HOLD / CLOSE / ADJUST`
3. Se `OPEN`: gera plano (direção + entry + SL/TP) e envia para execução

**Python Monitor (1 min):**
1. Avalia 6 triggers
2. Entry conditions met / Breakout / Session change → chama Agent Proactive
3. Trade at risk / Calendar event / Profit drawdown → chama Agent Fast

**Agent Fast (trigger-only):**
1. Decide `CLOSE / ADJUST / HOLD / DISMISS`
2. Aplica ações de emergência (nunca abre trades)

**Execução:**
1. Preferencial: EA Bridge (tick-by-tick) aplica SL/TP, breakeven e trailing
2. P&L: balance diff quando o EA fecha posições
3. Detalhes: deal resolver para resolver e classificar fechos quando necessário

---

## 22. Estrutura de Ficheiros

```
XAUUSD/
├── main.py                    # Orquestrador
├── config.py                  # Configurações
├── central_brain.py           # Cérebro Central
├── technical_analyzer.py      # Análise técnica
├── calculate_indicators.py    # Indicadores
├── ml_predictor.py            # ML Ensemble
├── momentum_detector.py       # Momentum + M5 reversal
├── news_score_hybrid.py       # News (GPT + macro)
├── economic_calendar.py       # Calendário (5º pilar)
├── volatility_guard.py        # Proteção crashes
├── gpt_confidence.py          # GPT Validator
├── cycle_memory.py            # Memória temporal
├── safety_checks.py           # Verificações segurança
├── risk_manager.py            # Position sizing
├── executor.py                # Interface MT5
├── monitor.py                 # Gestão posições
├── alerts.py                  # Discord
├── state_writer.py            # JSON state
├── db_writer.py               # SQLite history
├── logger.py                  # Logging
├── dashboard/                 # FlokiWatch web UI
├── scripts/                   # Treino ML + Backtest
├── models/                    # Modelos treinados
├── data/                      # Estado + histórico
└── logs/                      # Logs diários
```

---

## 23. Roadmap

## Live Observations

- Feb 24 (trades #21–#22): momentum extremes (85–95) overrode bearish ML/Technical signals and entered against the move; track if this pattern repeats.

### Completed

| Feature | Status | Notes |
|---------|--------|-------|
| Agent-first architecture (Brain=infra, Agent=decisor) | ✅ Done | Brain prepara dados; Agent Proactive decide |
| Proactive Analysis (M30 snapshot + triggers) | ✅ Done | Dashboard mostra decisão, reasoning, factors, concerns |
| Python Monitor triggers (1 min, zero Claude cost) | ✅ Done | 6 triggers chamam Proactive vs Fast |
| Agent Fast emergency manager | ✅ Done | CLOSE/ADJUST/HOLD/DISMISS (no OPEN) |
| EA Bridge tick-by-tick management | ✅ Done | BE/Trailing dentro do MT5 |
| ML Ensemble v3.1 (6 models, 48 features) | ✅ Done | WF AUC ~0.79, 3-year data, 12 WF folds, rank-based calibration |
| Volatility Guard (2-candle logic) | ✅ Done | EXTREME/COOLING/NORMAL |
| Smart Pyramid Rule | ✅ Done | Block 2nd pos unless existing ≥0.3% profit |
| M5 Reversal Detection | ✅ Done | Strong blocks, moderate penalizes |
| Visual Context Features | ✅ Done | 5 H1 patterns, ±8 cap |
| Dynamic Trailing (ATR-based) | ✅ Done | BE=0.5×SL (optimized from 0.7), Trail=0.7×SL |
| Backtest Engine (M5 precision) | ✅ Done | H1 signals + M5 SL/TP/trailing sim |
| FlokiWatch Dashboard | ✅ Done | Real-time + INTEL FEED |
| GPT Confidence Validator | ✅ Done | CONFIRM/BOOST/REDUCE ±15 |
| Economic Calendar (5th pillar) | ✅ Done | MQL5 exporter + phases |
| Early Exit (Pyramid Protection + Extreme Market Exit) | ❌ Abandoned | Tested via 6-month backtest (Aug 2025 → Feb 2026, 275 trades). Thresholds too aggressive: 70% of early exits would have recovered. Baseline PF 2.32 vs EE ON PF 1.77, -$1,022 P&L. Existing trailing/BE already manages exits well. |
| Session / Hour Filter | ❌ Abandoned | Analyzed 662 trades across 18 months. No hour below 50% WR with meaningful sample size. All sessions profitable (PF 1.12-3.15). No action needed. |
| Confidence-Based Position Sizing | ⏸ Deferred | Correlation r=0.70 confirmed (confidence predicts WR: 67.6%→77.6%). But LOT_STEP 0.01 + MAX_LOT 0.02 = only 2 possible lot sizes. Dynamic sizing lost $129 vs fixed in compounding backtest. Deferred until account size >$5,000 or lot constraints change. |
| Dynamic SL/TP by Session & Volatility | ❌ Abandoned | MIN_SL clamping (45.6% of trades) caused by low-ATR markets (<100p), not SL miscalibration. ATR filter at 110p blocked 341 trades with 68.6% WR and +$1,310 P&L — blocks more winners than losers. TP contributes 48.6% of winner P&L — essential, keep as-is. |
| Partial Close (50% at profit target) | ❌ Abandoned | Tested 5 trigger levels (25%-75% of TP). Best result: +$18 at 75% (breakeven). 200 winners would benefit (+99 pips saved) but 93 TP winners get capped (-105 pips lost). Net effect zero. Trailing stop already handles profit protection. |
| Explicit Regime Detection | ❌ Abandoned | All regimes profitable. Trending 72% WR (PF 2.08), Ranging 70% WR (PF 2.49), Normal 74.7% WR. Only Volatile-Trend negative but 12 trades (statistically irrelevant). Central Brain scenarios already provide effective implicit regime detection. |
| Multi-Asset Correlation Filter | ❌ Abandoned | Conflict trades (BUY gold + DXY rising) have HIGHER WR (76.0%) than aligned trades (70.7%). Bot already incorporates macro in scoring — when it overrides macro headwinds, those are high-conviction trades. Filtering would remove $1,578-$2,174 in profit. |
| Weight Optimization | ✅ Completed | 96 five-pillar + 19 three-pillar combinations tested via walk-forward (12M train / 6M test). Best combo improved test PF by only +0.04 (below +0.10 threshold). Current weights confirmed near-optimal. PF range across all combos: 1.70-2.13 (train), 2.13-2.55 (test) — narrow band confirms robustness to weight changes. |
| Multi-TF Trend Confirmation | ✅ Completed | D1+H4 EMA50 trend check. Aligned trades +10 conf, conflicting -20 conf. Prevents counter-trend trades like Bot-SELL-34/35. Config: `MTF_TREND_ENABLED`, `MTF_TREND_ALIGN_BONUS`, `MTF_TREND_CONFLICT_PENALTY`. |
| Volume Gate | ✅ Completed | Penalizes low-volume trades. <0.5x avg: -15 conf, <0.3x avg: -25 conf. Config: `VOLUME_GATE_ENABLED`, thresholds configurable. |
| MACD Divergence Adjustment | ✅ Tuned | Reduced from ±25 to ±15 points. Too aggressive in strong trends. Config: `MACD_DIVERGENCE_ADJUSTMENT`. |
| Spread Monitoring | ✅ Completed | Checks spread before entry, delays if >5.0 pips, retries every 30s for up to 5 min. Prevents bad fills during volatile spreads. |
| S/R Zones on MT5 Chart | ✅ Completed | Bridge pattern (Python writes `sr_zones.json`, MQL5 EA draws lines). 8 nearest zones, line width by strength. See Section 13.1. |
| Trade History Dashboard (/history) | ✅ Completed | Full trade table, equity curve, monthly summary, backtest vs live comparison. FastAPI endpoint + dedicated HTML page. |
| News Feed Expansion (14 RSS feeds) | ✅ Completed | Expanded from 4 to 14 feeds covering geopolitics, crises, monetary policy, safe haven, sanctions. Broader headline coverage for GPT sentiment. |
| GitHub Version Control | ✅ Completed | Private repo at github.com/hhvivaldi/flokiwatch. All code versioned with descriptive commits. |
| FIELD_CONTRACT.md | ✅ Completed | Data interface contract between bot (`state_writer.py`) and dashboard (`app.js`). Prevents ID mismatch bugs. |

### Optimization Roadmap Summary (Feb 2026)

Seven optimization features were rigorously tested via data analysis on 662 backtest trades (Aug 2024 → Feb 2026, 18 months). **All seven reached the same conclusion: the system is already well-optimized.**

Key findings:
- **Every filter tested removed more winners than losers.** The bot is profitable across all hours, sessions, regimes, ATR levels, and macro conditions.
- **The Central Brain's scenario-based approach provides effective implicit regime detection** — `momentum_forte_confirmado` at 89.5% WR in trending markets, `divergencia_tecnica` at 73.0% WR in ranging markets.
- **The trailing stop + breakeven combination handles profit protection effectively** — partial close adds zero net benefit.
- **Confidence genuinely predicts win rate (r=0.70)** but lot size constraints (0.01-0.02) prevent implementation. Deferred to higher account balances.
- **Macro correlation is inverted** — trades against DXY/VIX direction are the bot's highest-conviction, best-performing trades. Filtering them would remove the best results.
- **Weight Optimization:** 96 five-pillar + 19 three-pillar combinations tested via cache-and-replay walk-forward backtest. Best combo improved test PF by only +0.04 (below +0.10 threshold). Current weights (Tech 30% / ML 25% / Mom 15% / News 20% / Cal 10%) confirmed near-optimal. PF range across all combos: 1.70–2.13 (train), 2.13–2.55 (test) — narrow band confirms robustness to weight changes.

Conclusion: No further optimization filters are needed. Focus should shift to monitoring live performance and scaling account size.

### Short-Term Pending

| Feature | Priority | Notes |
|---------|----------|-------|
| Monitor trigger tuning | Medium | Ajustar thresholds de trigger com base em dados live, sem alterar decisões do agente sem visibilidade no dashboard. |
| Deal resolver enrichment | Low | Melhorar classificação/metadata de fechos quando MT5 estiver instável. |
