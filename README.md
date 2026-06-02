# XAUUSD ORB Scalper

Multi-session opening-range breakout scalper for XAUUSD on MetaTrader 5, combining ICT / supply & demand confluences with a 30%-40%-30% partial-profit exit model.

## Strategy

### Entry — Opening Range Breakout (ORB)

Each session (Asia 00:00–09:00 UTC, London 09:00–12:00 UTC, NY 13:30–16:00 UTC) establishes a fresh opening range from its first 15-minute candle. On the 5-minute chart the bot detects breakouts and looks for a pullback entry into a Point of Interest (POI):

| Filter | Description |
|---|---|
| **HTF alignment** | EMA50/200 cross, BOS, HH/HL structure on M15 |
| **Swing break** | Price broke a recent swing high/low |
| **Institutional zone** | Entry must coincide with a demand/supply zone |
| **FVG** | Fair Value Gap in the pullback for additional confluence |
| **Slow momentum** | Pullback shows loss of momentum (small bodies, long wicks) |
| **Fib discount** | Pullback retraced 50–61.8% (golden retraction zone) |
| **Reaction confluence** | Price reacted at the POI (wicks / rejection) |

A secondary **aggressive FVG** entry fires when price re-enters a FVG that was left after the breakout, without waiting for a full pullback. In ranging markets a **range-reversal** setup looks for liquidity sweeps at the range boundaries.

### Exit — 30% / 40% / 30% Partial Profit

| Target | Level | Size | Contribution |
|---|---|---|---|
| TP1 | 1:1 | 30% of position | 0.30 R |
| TP2 | 1:2 | 40% of position | 0.80 R |
| TP3 | 1:3 | 30% of position | 0.90 R |
| **Total** | | | **2.0 R** |

- SL moves to breakeven after TP1.
- The remaining position after TP2 is fully closed at TP3 (no runner).
- If price reverses before hitting a target, the stop-loss closes whatever portion remains.

### Backtest Results (Sep 2025 – Jun 2026)

```
Total Trades:      216
Win Rate:          90.74%
Return:            5,065.49% ($1,000 → $51,655)
Profit Factor:     18.96
Max Drawdown:      3.17%
Avg Bars Held:     7.4
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
