# 🤖 XAU/USD Trading Bot

Bot de trading automático para XAU/USD (Ouro) usando análise técnica, sentimento de notícias e Machine Learning.

## 📋 Visão Geral

O bot opera **100% automaticamente** no MetaTrader 5:
- Abre trades sozinho
- Gerencia posições (TP parcial, trailing stop)
- Fecha trades sozinho
- Envia alertas para Discord

**Você só precisa acompanhar pelo Discord!**

## 🏗️ Arquitetura

```
┌─────────────────────────────────────────────────────────────┐
│                    TRADING BOT XAU/USD                       │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │   TÉCNICO    │  │    NEWS      │  │     ML       │       │
│  │   (45%)      │  │   (40%)      │  │   (15%)      │       │
│  │              │  │              │  │              │       │
│  │ - EMAs       │  │ - Headlines  │  │ - Gradient   │       │
│  │ - RSI        │  │ - DXY        │  │   Boost      │       │
│  │ - MACD       │  │ - Yields     │  │ - 53.56%     │       │
│  │ - Bollinger  │  │ - VIX        │  │   accuracy   │       │
│  │ - Stochastic │  │              │  │              │       │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘       │
│         │                 │                 │                │
│         └────────────┬────┴────────────────┘                │
│                      ▼                                       │
│              ┌───────────────┐                              │
│              │  CONFLUENCE   │                              │
│              │    ENGINE     │                              │
│              │  Score 0-100  │                              │
│              └───────┬───────┘                              │
│                      ▼                                       │
│              ┌───────────────┐                              │
│              │   DECISION    │                              │
│              │    SYSTEM     │                              │
│              │ BUY/SELL/HOLD │                              │
│              └───────┬───────┘                              │
│                      ▼                                       │
│              ┌───────────────┐                              │
│              │    SAFETY     │                              │
│              │    CHECKS     │                              │
│              └───────┬───────┘                              │
│                      ▼                                       │
│              ┌───────────────┐                              │
│              │     RISK      │                              │
│              │   MANAGER     │                              │
│              │ Lot/SL/TP     │                              │
│              └───────┬───────┘                              │
│                      ▼                                       │
│              ┌───────────────┐                              │
│              │   EXECUTOR    │──────────► MT5               │
│              │  (AUTOMÁTICO) │                              │
│              └───────────────┘                              │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

## 📁 Estrutura de Arquivos

```
XAUUSD/
├── main.py              # Loop principal do bot
├── config.py            # Configurações (editar antes de usar!)
├── confluence.py        # Sistema de confluência
├── risk_manager.py      # Gestão de risco
├── executor.py          # Execução de ordens MT5
├── monitor.py           # Monitoramento de posições
├── safety_checks.py     # Validações de segurança
├── technical_analyzer.py # Análise técnica
├── ml_predictor.py      # Predição ML
├── news_sentiment.py    # Score de notícias
├── alerts.py            # Alertas Discord
├── logger.py            # Sistema de logs
├── data/                # Dados históricos
├── models/              # Modelos ML salvos
└── logs/                # Arquivos de log
```

## ⚙️ Configuração

### 1. Editar `config.py`

```python
# Conta MT5
MT5_ACCOUNT = 12345678        # Seu número de conta
MT5_PASSWORD = "sua_senha"    # Sua senha
MT5_SERVER = "ICMarkets-Demo" # Servidor do broker

# Parâmetros de risco
CAPITAL_INICIAL = 1000        # Capital em USD
RISK_PER_TRADE = 2.0          # Risco por trade (%)

# Discord Webhook (já configurado)
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."

# Modo de operação
DRY_RUN = True  # True = teste, False = real
```

### 2. Instalar Dependências

```bash
pip install MetaTrader5 pandas numpy scikit-learn tensorflow requests joblib imbalanced-learn
```

### 3. Configurar MT5

1. Abrir MetaTrader 5
2. Ferramentas → Opções → Expert Advisors
3. Marcar "Permitir negociação algorítmica"
4. Marcar "Permitir importação de DLL"

## 🚀 Como Usar

### Modo Teste (DRY RUN)

```bash
# Rodar bot em modo teste (não executa ordens reais)
python main.py --dry-run

# Executar uma única análise
python main.py --test

# Testar conexão Discord
python main.py --discord-test
```

### Modo LIVE

```bash
# Rodar bot em modo real (CUIDADO!)
python main.py --live
```

## 📊 Sistema de Decisão

### Thresholds

| Score | Decisão | Ação |
|-------|---------|------|
| > 70 | STRONG_BUY | ✅ Abre BUY |
| 65-70 | BUY | ✅ Abre BUY |
| 55-65 | WEAK_BUY | ⏸️ Aguarda |
| 45-55 | HOLD | ⏸️ Aguarda |
| 35-45 | WEAK_SELL | ⏸️ Aguarda |
| 30-35 | SELL | ✅ Abre SELL |
| < 30 | STRONG_SELL | ✅ Abre SELL |

### Pesos de Confluência

- **Técnico**: 45%
- **News**: 40%
- **ML**: 15% (só se probabilidade ≥ 0.55)

Se ML não for confiável:
- **Técnico**: 52.9%
- **News**: 47.1%

## 🛡️ Safety Checks

O bot **NÃO** opera quando:

- ❌ MT5 desconectado
- ❌ Asian session (00-06 GMT)
- ❌ Sexta após 17h GMT
- ❌ 3+ perdas consecutivas (pausa 24h)
- ❌ 3+ posições abertas
- ❌ Perda diária > 6%
- ❌ High-impact news próximas

## 💰 Gestão de Risco

### Position Sizing

```
Risco por trade: 2% do capital
Lot size = (Capital × 2%) / (SL_pips × $10)

Exemplo:
$1000 × 2% = $20 de risco
$20 / (15 pips × $10) = 0.13 lotes
Arredondado: 0.10 lotes (máximo conservador)
```

### Stop Loss / Take Profit

Baseado em ATR (Average True Range):

- **SL**: 1.5 × ATR
- **TP1**: 2.0 × ATR (fecha 50%)
- **TP2**: 3.0 × ATR (fecha resto)

### Gestão Automática

1. **TP1 atingido**: Fecha 50%, move SL para breakeven
2. **Trailing Stop**: Após TP1, SL segue o preço
3. **Timeout**: Fecha após 24h se lucro < 5 pips
4. **Drawdown**: Fecha se perda > 30 pips

## 📱 Alertas Discord

O bot envia alertas para:

- 🤖 Bot iniciado/parado
- 🟢 Sinal BUY detectado
- 🔴 Sinal SELL detectado
- ✅ Ordem executada
- 🎉 TP1 atingido
- 💰 TP2 atingido
- 🔴 Stop Loss atingido
- ⛔ Sinal bloqueado (safety)
- ⚠️ Erros críticos
- 📊 Resumo diário

## 📈 Performance Esperada

Com base nos testes:

| Métrica | Valor |
|---------|-------|
| ML Accuracy | 53.56% |
| ML AUC | 0.5471 |
| Win Rate Esperado | 55-60% |
| Risk/Reward | 1:1.33 (TP1), 1:2.0 (TP2) |

## ⚠️ Avisos Importantes

1. **SEMPRE teste em DRY_RUN primeiro** (24-48h)
2. **Comece com capital pequeno** no modo LIVE
3. **Monitore pelo Discord** nas primeiras semanas
4. **Não modifique trades manualmente** - deixe o bot gerenciar
5. **Mantenha MT5 aberto** no VPS/computador

## 🔧 Troubleshooting

### MT5 não conecta

```python
# Verificar se MT5 está instalado e aberto
import MetaTrader5 as mt5
print(mt5.initialize())
print(mt5.last_error())
```

### Discord não envia

```bash
# Testar webhook
python main.py --discord-test
```

### Bot não opera

1. Verificar horário (evita Asian session)
2. Verificar se há posições abertas (max 3)
3. Verificar logs em `/logs/`

## 📝 Logs

Logs são salvos em `logs/trading_bot_YYYY-MM-DD.log`

Formato:
```
2026-01-26 14:35:22 | INFO     | Análise completa | Tech:65 News:72 ML:58 Final:67
2026-01-26 14:35:23 | INFO     | Decisão: BUY (confiança: medium)
2026-01-26 14:35:24 | SUCCESS  | Ordem executada | Ticket:12345 Lot:0.02
```

## 🎯 Próximos Passos

1. ✅ Configurar `config.py`
2. ✅ Testar conexão MT5
3. ✅ Testar conexão Discord
4. ✅ Rodar em DRY_RUN por 24-48h
5. ✅ Verificar alertas Discord
6. ✅ Analisar logs
7. 🚀 Ativar modo LIVE

---

**Desenvolvido para trading de XAU/USD no MetaTrader 5**

*Use por sua conta e risco. Trading envolve risco de perda de capital.*
