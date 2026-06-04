# XAUUSD ORB Scalper

Multi-session opening-range breakout scalper for XAUUSD on MetaTrader 5, combining ICT / supply & demand confluences with adaptive partial-profit exits and trailing stop.

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

### Exit — Adaptive Partial Profit with Trailing Stop

The exit model adapts automatically based on **lot size** (which derives from account balance):

| Account | Lot Size | Model | Targets |
|---|---|---|---|
| **$100–$150** | 0.01–0.03 | Single Target | 100% at 1:1 |
| **$200–$500** | 0.04–0.09 | 50/50 + Trail | 50% at 1:1 → BE → remaining 50% **trails at 0.3× SL distance** |
| **$600+** | 0.10+ | 30/40/30 + Trail | 30% at 1:1 → BE → 40% at 1:2 → remaining 30% **trails at 0.3× SL distance** |

- SL moves to **breakeven** after TP1 is hit.
- **Trailing stop** activates on the remaining runner after TP1 (50-50 model) or TP2 (3-target model). Trail distance = `0.3 × original SL distance`. Trail level updates only when price extends in the favorable direction.
- If price reverses before hitting a target, the stop-loss closes whatever portion remains.

### Safety Filters

| Filter | Description |
|---|---|
| **Spread filter** | Skips entries when spread exceeds 30 pips (configurable via `max_spread`) |
| **Circuit breaker** | Blocks new entries after 3% daily loss, 4 consecutive losses, or 15% max drawdown from peak |
| **News filter** | (Optional) Blocks entry 30 minutes before/after high-impact USD news events from ForexFactory |

### Backtest Results (Sep 2025 – Jun 2026)

Backtested on M5 XAUUSD data with $3.50/lot commission, 1.5% risk per trade, max 3 trades/day.

| Metric | $100 Account (Single) | $1,000 Account (3-Target + Trail) |
|---|---|---|
| **Total Trades** | 217 | 217 |
| **Win Rate** | 94.47% | 94.47% |
| **Total Profit** | $33,491 | $139,268 |
| **Return** | 33,491% | 13,927% |
| **Profit Factor** | 94.84 | 64.66 |
| **Max Drawdown** | 1.35% | 1.23% |
| **Avg Win** | $163.34 | $680.43 |
| **Avg Loss** | -$19.80 | -$107.80 |
| **Largest Win** | $1,890 | $9,901 |
| **Largest Loss** | -$66.30 | -$382.55 |

Trailing stop was the key driver of the largest wins — captured extended moves that fixed-target models would have missed.

## Project Structure

```
├── config/
│   ├── settings.py              # All configurable parameters (risk, sessions, API keys, safety toggles)
│   └── sessions.py              # Session time definitions & validators
├── connectors/
│   └── mt5_connector.py         # MetaTrader 5 wrapper (rates, orders, positions)
├── core/
│   ├── opening_range_scalp.py   # ORB strategy logic & signal generation
│   ├── institutional_zone.py    # Supply/demand zone detection
│   ├── risk_manager.py          # Circuit breaker (daily loss, consecutive losses, drawdown)
│   └── news_filter.py           # ForexFactory news blackout filter
├── database/
│   └── mongo_client.py          # MongoDB persistence (trades, signals, metrics)
├── log_utils/
│   └── logger_setup.py          # Structured JSON logging (console + file)
├── scripts/
│   ├── backtest.py              # Historical backtester with per-position PnL
│   └── run_live.py              # Live trading bot (polling loop)
├── telegram/
│   └── alerts.py                # Telegram notifications (open/close/error/heartbeat)
├── .env                         # MT5 credentials, MongoDB URI, Telegram tokens
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

Key settings in `config/settings.py`:

| Setting | Default | Description |
|---|---|---|
| `risk_percent` | 1.5 | Risk per trade (% of balance) |
| `max_daily_trades` | 3 | Max trades per day |
| `max_spread` | 30.0 | Max spread in pips |
| `trail_multiplier` | 0.3 | Trailing stop distance as fraction of SL distance |
| `circuit_breaker_max_daily_loss_pct` | 3.0 | Daily loss limit (%) |
| `circuit_breaker_max_consecutive_losses` | 4 | Max consecutive losses before pause |
| `circuit_breaker_max_drawdown_pct` | 15.0 | Max drawdown from peak (%) |
| `news_filter_enabled` | False | Enable ForexFactory news blackout |
| `backtest_commission` | 3.5 | Commission per lot per side ($) |

## Usage

### Live Trading

```bash
python scripts/run_live.py
```

The bot polls MT5 every 30 seconds during trading hours (Mon–Thu 00:00–17:00 UTC, Fri until 17:00 UTC). It loads M15 data and builds zones every 5 minutes. Signal generation runs across all three sessions — Asia (00–09), London (09–12), NY (13–16) UTC — each with its own independent opening range.

### Backtesting

```bash
python scripts/backtest.py --start 2025-09-01 --end 2026-06-03 --balance 1000 --risk 1.5
```

Optional flags:
- `--output <file>` — save results as JSON (default: `scalper_results.json`)

## Risk Management

- **Risk per trade:** 1.5% of current balance (configurable in `settings.py`)
- **Max daily trades:** 3
- **Partial profit locking:** SL moves to breakeven after TP1 hit
- **Trailing stop:** 0.3× SL distance, activates after TP1 (50-50) or TP2 (3-target)
- **Spread filter:** Skips entry if spread > 30 pips
- **Circuit breaker:** Blocks entry after 3% daily loss / 4 consecutive losses / 15% drawdown
- **News filter:** (Optional) blocks entry during high-impact USD events
- **Commission:** $3.50 per lot per side (built into all calculations)
- **Minimum account:** $100 (smaller accounts use single-target exit)

## Telegram Alerts

The bot sends real-time notifications to configured Telegram chats:

| Alert | Trigger | Info |
|---|---|---|
| **Startup** | Bot initialized | Symbol, balance, strategy, active filters |
| **Trade Open** | Order filled | Direction, lots, exit model, entry/SL/TP, risk %, commission |
| **Trade Close** | Position closed | P&L, targets hit, duration, balance, exit reason (trail/sl/tp) |
| **Heartbeat** | Every 4 hours | Balance, equity, uptime, position status, daily trades |
| **Error** | On failure | Error message and timestamp |
