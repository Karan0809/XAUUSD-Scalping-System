# XAUUSD ORB Scalper

Multi-session opening-range breakout scalper for XAUUSD on MetaTrader 5, combining ICT / supply & demand confluences with a 30%-40%-30% partial-profit exit model.

## Strategy

### Sessions

Each trading day is split into three sessions, each with its own fresh opening range:

| Session | UTC Hours | Opening Range |
|---|---|---|
| **Asia** | 00:00–09:00 | First 15-min candle at 00:00 |
| **London** | 09:00–12:00 | First 15-min candle at 09:00 |
| **New York** | 13:30–16:00 | First 15-min candle at 13:30 |

### Entry Filters

All entries share a common set of confluences before a signal is generated:

| Filter | Description |
|---|---|
| **HTF alignment** | EMA 50/200 cross, change of structure (BOS), HH/HL pattern on M15 confirming trend direction |
| **Swing break** | Price must break a recent swing high/low on the 5-min chart |
| **Institutional zone** | Entry must coincide with a supply/demand zone from `institutional_zone.py` |
| **FVG** | A Fair Value Gap must exist in the pullback for additional confluence |
| **Slow momentum** | Pullback shows loss of momentum (small-bodied candles, long upper/lower wicks) |
| **Reaction** | Price reacted at the POI with wicks or rejection, confirming the level holds |
| **Fib discount** | Entry must be in the 0.5–0.618 golden retraction zone of the swing |

#### Fibonacci Convention

Fibonacci is measured from the **origin of the move** (where the retracement pulls back toward):

| Direction | Fib drawn | 0.0 = | 1.0 = | 0.5–0.618 zone = |
|---|---|---|---|---|
| **Buy** (uptrend) | Low → High | Swing high | Swing low | Price retraced 50–61.8% back toward the swing low |
| **Sell** (downtrend) | High → Low | Swing low | Swing high | Price retraced 50–61.8% back toward the swing high |

Standard levels: **1.0, 0.786, 0.618, 0.5, 0.382, 0.236, 0.0**. Only the 0.5–0.618 golden zone is used for entry.

### Entry Types

| Type | Trigger | Condition |
|---|---|---|
| **Breakout Pullback** | Price breaks the opening range, then pulls back into a POI | 5-min candle shows bullish/bearish reversal within POI + fib 0.5–0.618 retrace |
| **Aggressive FVG** | Price re-enters a FVG left after the breakout | No waiting for a pullback — enters immediately on FVG touch with fib discount |
| **Range Reversal** | Price sweeps the opening range boundary on the 5-min chart | Reversal candle with wick at the sweep point, no fib required |

### Exit — 30% / 40% / 30% Partial Profit

| Target | R:R Level | Position % | R Contribution |
|---|---|---|---|
| TP1 | 1:1 | 30% | 0.30 R |
| TP2 | 1:2 | 40% | 0.80 R |
| TP3 | 1:3 | 30% | 0.90 R |
| **Total** | **2.0 R** | **100%** | **2.00 R** |

- SL moves to **breakeven** after TP1 is hit.
- The remaining position after TP2 is fully closed at TP3 (no trailing runner).
- If price reverses before hitting a target, the stop-loss closes whatever portion remains.

### Backtest Results (Sep 2025 – Jun 2026)

```
Total Trades:      216
Win Rate:          94.44%
Return:            6,003.35% ($1,000 → $61,033)
Profit Factor:     36.35
Max Drawdown:      1.67%
Avg Bars Held:     6.5
```

## Project Structure

```
├── config/
│   ├── settings.py         # All configurable parameters (risk, sessions, API keys)
│   └── sessions.py         # Session time definitions & validators
├── connectors/
│   └── mt5_connector.py    # MetaTrader 5 wrapper (rates, orders, positions)
├── core/
│   ├── opening_range_scalp.py  # ORB strategy logic & signal generation
│   └── institutional_zone.py   # Supply/demand zone detection
├── database/
│   └── mongo_client.py     # MongoDB persistence (trades, signals, metrics)
├── log_utils/
│   └── logger_setup.py     # Structured JSON logging (console + file)
├── scripts/
│   ├── backtest.py         # Historical backtester with per-position PnL
│   └── run_live.py         # Live trading bot (polling loop)
├── telegram/
│   └── alerts.py           # Telegram notifications (open/close/error/heartbeat)
├── .env                    # MT5 credentials, MongoDB URI, Telegram tokens
├── requirements.txt
└── README.md
```

## Setup

### Prerequisites

- Python 3.10+
- MetaTrader 5 terminal installed (IC Markets or any broker)
- (Optional) MongoDB instance for trade persistence
- (Optional) Telegram bot token for alerts

### Installation

```bash
# Clone and enter the directory
cd xauusd-scalper

# Create a virtual environment
python -m venv venv
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration

Copy the template into `.env` and fill in your credentials:

| Variable | Description |
|---|---|
| `MT5_LOGIN` | MT5 account number |
| `MT5_PASSWORD` | MT5 account password |
| `MT5_SERVER` | Broker server (default: `ICMarkets-Demo`) |
| `MT5_PATH` | Path to terminal64.exe |
| `MONGO_URI` | MongoDB connection string |
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID |

All other parameters live in `config/settings.py` — risk per trade, max daily trades, session hours, commission, etc.

## Usage

### Live Trading

```bash
python scripts/run_live.py
```

The bot polls MT5 every 30 seconds during trading hours (Mon–Thu 00:00–17:00 UTC, Fri until 17:00 UTC). It loads M15 data and building zones once every 5 minutes.

### Backtesting

```bash
python scripts/backtest.py --start 2025-09-01 --end 2026-06-03 --balance 1000 --risk 1.5
```

Optional flags:
- `--output <file>` — save results as JSON (default: `scalper_results.json`)

## Risk Management

- **Risk per trade:** 1.5% of current balance (configurable)
- **Max daily trades:** 3 (configurable)
- **Max daily loss:** $500 hard stop (configurable)
- **Partial profit locking:** 30% at 1:1 moves SL to breakeven
- **Commission:** $3.50 per lot per side (built into all calculations)
