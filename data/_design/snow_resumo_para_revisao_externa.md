# Snow — Resumo para Revisão Externa

**Audiência:** CEO + amigo trader profissional (revisão informada).
**Objetivo:** dar contexto suficiente para uma conversa produtiva
sobre se as primitivas do Snow conseguem expressar os padrões
operacionais que o trader realmente usa.
**Tempo de leitura:** ~12 minutos.

> Para a discussão técnica detalhada com o trader, ver
> `snow_for_external_review.md` (versão inglesa, ~3-4× mais longa).

---

## 1. O que é o Snow

O **Snow** é o motor que executa **planos contingentes** que o
**Floki** (o agente decisor LLM) escreve a cada ciclo. A divisão é
deliberada:

- **Floki** decide o que fazer. Roda em ciclos de 5–30 minutos.
  Vê tudo (charts, notícias, macro, posições). Tem agência.
- **Snow** executa o que o Floki já decidiu. Roda em ticks de 5
  segundos. Vê apenas o plano + o preço atual. Não decide nada.

Em termos de paradigma: o Floki é **projetivo** (escreve o futuro),
o Snow é **reativo** (cumpre o que está escrito). Quando uma
condição do plano se torna verdadeira, o Snow dispara a ação
correspondente — tipicamente abrir, mover SL/TP para BE, fazer
trailing, ou fechar parcial/total.

**Por que separar?** Um LLM raciocinando a cada 5 segundos é caro,
lento, e propenso a oscilação. Um LLM raciocinando a cada 5 minutos
e deixando 600 ticks de execução para uma máquina determinística
combina o melhor dos dois mundos: julgamento humano-like onde
importa, latência de máquina onde precisa.

---

## 2. A handshake Floki ↔ Snow

A cada ciclo do Floki:

1. Floki recebe um pacote de dados (charts, indicadores, regime,
   notícias, posições abertas, planos ativos).
2. Floki forma uma tese (direcional, ambígua, ou bidirecional).
3. Floki **submete um plano** ao Snow via a tool
   `submit_plan_to_snow(plan)`. O plano descreve:
   - **Análise** — tese + níveis-chave + regime assumido.
   - **Entrada** — direção, volume, condições de gatilho, SL/TP iniciais.
   - **Gestão** — regras para mover SL para break-even, trailing, etc.
   - **Saída** — condições de invalidação que fecham a posição.
   - **Emergência** — perda máxima, duração máxima, fallback de erro.
   - **Validade** — `expires_at` (o plano se autodestrói após).
4. O Snow valida o plano (Pydantic + regras de negócio), gera um
   `plan_id`, persiste em SQLite. **Não executa nada ainda.**
5. A cada tick (5 s), o Snow reavalia todas as condições do plano.
   Quando a entrada é satisfeita → executa a ordem de mercado.
   Depois disso, gestão e saída são monitoradas até a posição
   fechar.

**Princípio crítico:** o Floki não pode interferir num plano em
andamento sem cancelá-lo explicitamente. Decidiu uma vez, deixou
o Snow cumprir. Isso evita o anti-padrão "manager que muda de ideia
a cada vela".

---

## 3. As 21 primitivas — visão de conjunto

O Snow oferece **21 condições primitivas** que o Floki pode combinar
com **AND** dentro de cada bloco do plano. O `OR` é expresso através
de planos paralelos (PAIRED PLANS).

| Categoria | Primitivas | Memória |
|---|---|---|
| Preço (point-in-time) | `price_above`, `price_below` | sem |
| Indicador (point-in-time) | `rsi`, `macd_histogram`, `ema_relation`, `atr`, `stochastic`, `bollinger_position`, `indicator_divergence` | sem |
| Estrutura (point-in-time) | `price_at_sr_zone`, `price_at_fibonacci`, `price_at_pivot` | sem |
| Posição (requer plano ACTIVE) | `profit_pips`, `mfe_reached`, `mae_reached`, `profit_retraced_from_peak` | tracker em memória |
| Tempo / relógio | `duration_exceeds`, `time_between` | sem |
| **Stateful (Phase 8b)** | `indicator_crossover`, `indicator_was`, `price_crossed_level` | per-condition row |

**Total:** 18 stateless + 3 stateful = 21.

### As 3 stateful (Phase 8b — adições mais recentes)

São as únicas com memória entre ticks:

- **`indicator_crossover`** — dispara no PRIMEIRO tick em que
  o indicador (RSI / MACD-hist / Stochastic) cruza um threshold
  numa direção. Continuação não dispara de novo.
  Exemplo: "fire quando RSI H1 cruzar abaixo de 30" (gatilho de
  oversold, NÃO "RSI está abaixo de 30 agora").

- **`indicator_was`** — janela deslizante de `within_bars` (1–20)
  velas fechadas no `tf`. Verdadeiro se o indicador satisfez
  `op threshold` em qualquer uma das velas recentes, mesmo que
  já não satisfaça agora.
  Exemplo: "RSI esteve abaixo de 30 nas últimas 4 velas H1"
  (setup de recuperação de oversold).

- **`price_crossed_level`** — latch de tiro único. Quando o preço
  cruza o nível na direção pedida, fica True para sempre durante
  a vida do plano (sem reset mid-plan).
  Exemplo: "preço tocou 4720 vindo de cima" (combina com
  `price_above(4725)` para expressar "tagged-then-bounced").

---

## 4. Ciclo de vida de um plano

```
                                    Floki cancela
                  ┌──────────────────────────────────────┐
                  │                                       ▼
   submit_plan ─→ PENDING ─[entrada disparou]─→ TRIGGERED → CANCELLED
                  │                                ↓ ordem MT5 OK
                  │                              ACTIVE ←─── (gestão/saída
                  │                                ↓         disparam aqui)
                  │  expires_at passou           CLOSING
                  ↓                                ↓ ordem de fecho OK
                EXPIRED                          CLOSED
                                                 (terminal — outcome
                                                  registado, plano feito)

 Em qualquer ponto: erro de broker irrecuperável → FAILED.
```

- **Ticks** do Snow: 5 s (10× mais rápido que o ciclo do Floki).
- **Flush** do estado para disco: a cada 60 ticks (5 min) +
  atomicamente em transições terminais.
- **Recovery** ao iniciar o bot: o Snow lê todos os planos vivos,
  confronta com o MT5, e reconcilia (planos fantasma, posições
  fechadas externamente, etc.).

---

## 5. O que perguntar ao trader

Para a conversa ficar produtiva, sugerir estes ângulos (do mais
estrutural ao mais específico):

1. **Cobertura de setups.** Os 5 setups que ele mais usa no XAU/USD
   ou similar — consegue exprimir com as 21 primitivas? Onde fica
   sem expressividade?
2. **Multi-timeframe.** Os planos podem misturar condições em
   M5/M15/H1/H4/D1 livremente (cada condição declara o seu `tf`).
   Isso é suficiente, ou faltam padrões de "alinhamento HTF antes
   de entrar"?
3. **Crossover stateful.** As 3 primitivas com memória (Phase 8b)
   cobrem os principais padrões de transição? Há padrões que
   precisam de memória mais profunda (ex.: "MACD cruzou positivo
   há 3 velas, RSI está a recuperar agora" — dois eventos
   sequenciais)?
4. **Gestão de posição.** O bloco de `management` (BE, trailing
   condicional, fechamento parcial) cobre o estilo dele? Ou ele
   espera coisas como "scale in", "pyramiding", "averaging down"?
5. **Limitações estruturais.** O Snow só pode expressar **AND
   dentro de um bloco** + `OR` via planos paralelos. Isto é
   suficiente, ou perde padrões que dependem de lógica condicional
   mais rica?

---

## 6. O que o Snow **não** consegue expressar (limitações honestas)

Entender o teto é metade da conversa.

- **Crossover sustained.** "MACD cruzou positivo E ficou positivo
  por 3 velas" — pode-se aproximar com `indicator_crossover`
  (commit 8b/3) + `indicator_was`, mas não há primitiva única.
- **Padrões multi-evento sequenciais.** "Fez X, depois Y, depois
  voltou a fazer X" — exige máquina de estados explícita; a
  v2 schema permite expressar via planos encadeados (um plano
  cria o próximo na sua saída), mas é mais verboso.
- **Padrões geométricos.** Triângulos, bandeiras, ombro-cabeça-
  ombro — não há primitivas de pattern recognition. O Floki pode
  detectar e descrever em texto, mas o Snow não monitora forma.
- **Order-flow / volume profile / VWAP.** Cat B do RFC; pipeline
  de dados separado, ainda não construído.
- **Liquidity sweep semântico.** O `price_crossed_level` é o
  building block, mas falta a primitiva sweep formal (Cat D, RFC
  futuro).
- **Cross-plan coordination.** Cada plano é privado. Não há
  "regime mudou para bullish" partilhado. Floki recompõe o
  contexto a cada ciclo.

---

## 7. Estado atual + próximo passo

- **Phase 8b** (commits 1–6, FLO-359) terminada esta semana. Adiciona
  as 3 primitivas stateful.
- **FLO-354 + FLO-353** (recovery + outcome backfill) shipped hoje.
  Resolvem os dois bloqueadores que o CTO identificou antes do
  modo LIVE.
- **Próximo passo operacional:** flip de `SNOW_DRY_RUN=false` no
  `.env`, restart do bot. O Snow passa a executar ordens reais na
  conta DEMO. Os primeiros dias são janela de observação:
  - Floki adopta as primitivas stateful?
  - Recovery funciona em casos reais?
  - Backfill captura outcomes corretamente?
  - Latência de tick fica < 200 ms?

A revisão externa que tu queres fazer é **antes ou depois deste
flip** — ambas funcionam. Se for antes, o trader pode questionar
algo que adia o flip. Se for depois, o trader vê comportamento
real (mas em DEMO).

---

## Anexos rápidos (para referência durante a conversa)

### A. Estrutura mínima de um plano

```json
{
  "schema_version": 2,
  "id": "PLAN-20260426-001",
  "created_at": "2026-04-26T14:00:00Z",
  "expires_at": "2026-04-26T18:00:00Z",
  "analysis": { "thesis": "...", "key_levels": [...], "confidence": 72,
                "regime_assumed": "TRENDING_BULLISH" },
  "entry":   { "direction": "BUY", "volume": 0.02,
               "conditions": [ ... AND ... ],
               "initial_sl": 4715.0, "initial_tp": 4750.0 },
  "management": [ { "name": "lock_be_at_10", "priority": 7,
                    "conditions": [...], "action": {...},
                    "fires": "once" } ],
  "exit":      [ { "name": "rsi_exit", "priority": 9,
                   "conditions": [...], "action": {"type": "close_full"},
                   "fires": "once" } ],
  "emergency": { "max_loss_pips": 150, "max_duration_minutes": 480,
                 "on_broker_error": "alert_floki" }
}
```

### B. Tipos de ação disponíveis

`execute_market` (apenas em entry), `adjust_sl`, `adjust_tp`,
`move_sl_to_breakeven`, `move_sl_to_price`, `trail_sl`, `close_full`,
`close_partial`.

### C. Métricas de invariantes (do RFC)

- I1: cada plano = 1 broker ticket no máximo
- I2: TRIGGERED ≤ 60 s (não fica preso)
- I3: CLOSING ≤ 60 s
- I4: emergency avaliada todo tick
- I5: priority ∈ [7, 228]
- I6: update DB + audit row é atómico
- I7: UNIQUE constraint em (live, trade_ticket)

---

**Pronto para conversa.** Se o trader quiser ver o JSON completo
de planos reais ou a lista exaustiva de parâmetros de cada
primitiva, o documento técnico inglês
(`snow_for_external_review.md`) tem tudo isso + 3 exemplos
caminhados passo a passo.
