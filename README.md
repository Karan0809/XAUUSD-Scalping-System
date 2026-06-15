# XAUUSD Scalper (ORB + Free Trade)

Multi-session scalper for XAUUSD on MetaTrader 5 combining ORB breakouts with an all-day free-trade fallback that uses zone/HTF/momentum confluences. One unified bot replaces the need for separate session and aggressive scalpers.

## Strategy

### Sessions

Each trading day is split into three sessions, each with its own fresh opening range. The bot enters trades in all sessions.

| Session | UTC Hours | Opening Range |
|---|---|---|
| **Asia** | 00:00–09:00 | First 15-min candle at 00:00 |
| **London** | 09:00–12:00 | First 15-min candle at 09:00 |
| **New York** | 13:30–16:00 | First 15-min candle at 13:30 |

The bot runs Mon–Thu 00:00–17:00 UTC, Fri until 17:00 UTC (then disconnects and sleeps until Monday 00:00 UTC).

### Free Trade Fallback

When no ORB range is available (outside session hours, or before the opening candle closes), the bot falls back to a **free trade** mode that uses the same quality filters without the range requirement:

| Filter | Source |
|---|---|
| **Direction** | HTF alignment (EMA50/200, BOS, HH/HL on M15) |
| **Confirmation** | Swing break on M5 |
| **Entry zone** | Institutional zone as POI for pullback |
| **Entry method** | Pullback into zone or FVG anywhere |
| **Validation** | Slow momentum, fib 0.5–0.618 discount, M5 reaction |

Both ORB and free trade share the same tiered fixed risk per trade, same partial-profit exit model (30/40/30 + trailing), and same daily trade limit. Position sizes use tiered fixed risk (not compounding) with a hard cap of 0.5 lots and 1-2 pip slippage applied on entry for realistic fills.

### Entry Filters

All entries share a common set of confluences before a signal is generated:

| Filter | Description |
|---|---|
| **HTF alignment** | EMA 50/200 cross, change of structure (BOS), HH/HL pattern on M15 confirming trend direction |
| **Swing break** | Price must break a recent swing high/low on the 5-min chart |
| **Institutional zone** | Entry must coincide with a supply/demand zone |
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
|---|---|---|---|
| **Breakout Pullback** | Price breaks the opening range, then pulls back into a POI | 5-min candle shows bullish/bearish reversal within POI + fib 0.5–0.618 retrace |
| **Aggressive FVG** | Price re-enters a FVG left after the breakout | No waiting for a pullback — enters immediately on FVG touch with fib discount |
| **Range Reversal** | Price sweeps the opening range boundary on the 5-min chart | Reversal candle with wick at the sweep point, no fib required |

### Free Trade Entry Types

| Type | Trigger | Condition |
|---|---|---|
| **Free Pullback** | Price pulls back into an institutional zone | 5-min candle reversal within zone + fib 0.5–0.618 + slow momentum + reaction |
| **Free FVG** | FVG forms in the direction of the HTF trend | Entry at FVG midpoint, 50-pip fixed SL, no zone proximity required |

## Position Management Lifecycle

Once a trade opens, the bot polls **every 30 seconds** and examines **every M5 bar** since the position's open time (not just the last bar). On each bar it runs these checks in order:

### For any trade:

| Step | Condition | Action |
|---|---|---|
| **1. TP1** | Bar high/low hits entry + 1× SL distance | Close first tranche, move SL to breakeven |
| **2. TP2** (3-target only) | After TP1, bar hits entry + 2× SL distance | Close second tranche, activate trailing |
| **3. Trail update** | Price extends further | Ratchet trail level up (buys) or down (sells) |
| **4. Trail check** | Bar breaches trail level (skips activation bar) | Close remaining lots |
| **5. SL/BE** | Bar breaches current SL | Close remaining (as "be" if TP1 was hit, "sl" otherwise) |

### Exit Model Selection

The model adapts automatically based on **lot size** (derived from account balance and risk %):

**Single Target (≤ 3 cents / < $150 account)**

| Step | Lots | Price | Result |
|---|---|---|---|
| TP1 | 100% (all) | 1:1 | Full close, trade ends |

**50/50 + Trail (4–9 cents / $150–$500 account)**

| Step | Lots | Price | Result |
|---|---|---|---|
| TP1 | 50% | 1:1 | Partial close, SL → BE, trailing activated |
| Trail update | — | — | Trail level ratchets with price |
| Trail hit | Remaining 50% | Trail level | Remaining closes at trail |

**30/40/30 + Trail (10+ cents / $500+ account)**

| Step | Lots | Price | Result |
|---|---|---|---|
| TP1 | 30% | 1:1 | Partial close, SL → BE |
| TP2 | 40% | 1:2 | Partial close, trailing activated |
| Trail update | — | — | Trail level ratchets with price |
| Trail hit | Remaining 30% | Trail level | Remaining closes at trail |

- **Trail distance:** `0.3 × original SL distance` (configurable via `trail_multiplier`)
- **Trail direction:** For buys, trail level = (bar high) − trail distance; for sells, trail level = (bar low) + trail distance
- The trail level **only moves in the favorable direction** — never backwards
- **Activation bar skip:** The trailing stop is **not checked on the bar where it was activated**. This prevents the stop from triggering on the same M5 bar's wick, giving the remaining position a full bar to develop before the trail tightens.
- If price reverses sharply, the SL at breakeven catches the exit before the trail level is hit

### Why iterate all bars?

On every poll, the bot re-examines all M5 bars since entry (up to 30 bars back). This guarantees that if TP1, TP2, or a trail/SL event occurred on a closed bar that is no longer the most recent bar, it is still detected and acted upon. Since all conditions are guarded by flags (`tp1_hit`, `tp2_hit`, `remaining_lots`), re-processing is **idempotent** — safe to repeat endlessly.

## Safety Filters

| Filter | Description | Default |
|---|---|---|
| **Spread filter** | Skips entries when spread exceeds threshold | 30 pips |
| **Circuit breaker** | Blocks new entries on 3% daily loss, 4 consecutive losses, or 15% drawdown from peak | On |
| **News filter** | Optional — blocks entry 30 min before/after high-impact USD events (ForexFactory) | Off |
| **Friday shutdown** | Bot disconnects at 17:00 UTC Friday, sleeps until Monday 00:00 UTC | Auto |

## Backtest Results (Sep 2025 – Jun 2026)

Backtested on live M5/M1 XAUUSD data across all sessions (Asia + London + NY). Commission: $3.50/lot/side. All tests use tiered fixed risk ($10→$15→$20→$30→$50 based on profit milestones), 0.5 lots hard cap, 1-2 pip entry slippage, 0-1 pip exit slippage, and a 20-point spread filter.

### ORB Scalper

Trades all sessions using ORB pipeline (breakout pullback, aggressive FVG, range reversal) with free trade fallback. Each session allows at most 1 entry. Lot size determined by tiered fixed risk / SL distance, capped at 0.5 lots.

| Metric | $1,000 Account |
|---|---|
| **Total Trades** | 518 |
| **Win Rate** | 95.0% |
| **Total Trades** | 518 |
| **Win Rate** | 95.0% |
| **Total Profit** | **$83,266** |
| **Profit Factor** | 103.87 |
| **Max Drawdown** | $15.81 (1.02%) |
| **Avg Win / Loss** | +$170.82 / -$29.95 |
| **Largest Win / Loss** | +$1,489.53 / -$51.56 |
| **Recovery Trades** | 4 |
| **Avg Bars Held** | 2.0 |
| **Filters** | Spread=442 CB=0 |

### Aggressive M1

Trades 24/7 on zone+momentum confluence with fixed 20-pip SL. No session awareness — enters any time zone and momentum align.

| Metric | $1,000 Account |
|---|---|
| **Total Trades** | 1,455 |
| **Win Rate** | 77.7% |
| **Total Profit** | **$68,249** |
| **Profit Factor** | 18.69 |
| **Max Drawdown** | $15.14 (1.46%) |
| **Avg Win / Loss** | +$63.75 / -$11.91 |
| **Largest Win / Loss** | +$712.98 / -$12.25 |
| **Avg Bars Held** | 1.4 |
| **Filters** | Zone=0 Mom=1,591 Spread=425 CB=5,123 |

### Key Fixes Applied

| Fix | Impact |
|---|---|
| **Tiered fixed risk** ($10→$15→$20→$30→$50 based on profit) | Replaces flat $10 — grows with account without compounding explosion. |
| **0.5 lots hard cap** (was 10.0) | Limits position size regardless of account growth. |
| **Slippage model** (1-2 pip entry, 0-1 pip exit) | More realistic fills, prevents edge-case overperformance. |
| **`elif` in session/date reset** | Stopped double-reset bug that cleared `_entry_triggered`, causing duplicate entries. |
| **3-bar minimum gap** | Safety net preventing re-entry within same session after a close. |
| **Recovery entries** (ORB only) | After a loss, next entry tightens SL using M5 swing level — same risk, larger size. +$781 gain. |
| **Spread filter 20 points** (was 60) | Blocks wider spreads — safer for tight 1-pip SL scalping. |

## Project Structure

```
├── config/
│   ├── settings.py              # All configurable parameters (risk, sessions, API keys, safety toggles)
│   └── sessions.py              # Session time definitions & validators
├── connectors/
│   └── mt5_connector.py         # MetaTrader 5 wrapper (rates, orders, positions, modify)
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
│   ├── backtest.py              # Historical backtester (ORB + Free Trade)
│   ├── backtest_aggressive.py   # Historical backtester (Aggressive M1)
│   ├── run_live.py              # Live trading bot (ORB + Free Trade)
│   └── run_aggressive.py        # Live trading bot (Aggressive M1)
├── telegram/
│   └── alerts.py                # Telegram notifications (open/close/error/heartbeat)
├── .env                         # Default MT5 credentials, MongoDB URI, Telegram tokens
├── .env.aggressive              # Env file for second account (aggressive bot)
├── requirements.txt
└── README.md
```

## Setup

### Prerequisites

- Python 3.10+
- MetaTrader 5 terminal installed (IC Markets, MetaQuotes-Demo, or any broker)
- (Optional) MongoDB instance for trade persistence
- (Optional) Telegram bot token for alerts

### Installation

```bash
cd xauusd-scalper
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### Configuration

Create `.env` (or `.env.orb` / `.env.aggressive` for separate accounts):

| Variable | Description |
|---|---|
| `MT5_LOGIN` | MT5 account number |
| `MT5_PASSWORD` | MT5 account password |
| `MT5_SERVER` | Broker server (e.g. `ICMarkets-Demo`, `MetaQuotes-Demo`) |
| `MT5_PATH` | Path to terminal64.exe |
| `MONGO_URI` | MongoDB connection string |
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID (comma-separated for multiple) |

Run multiple bots with different accounts using `--env`:

```bash
# ORB bot (uses .env by default)
python scripts/run_live.py

# ORB bot with explicit env file
python scripts/run_live.py --env .env.orb

# Aggressive bot on a different account
python scripts/run_aggressive.py --env .env.aggressive
```

### Key Settings (`config/settings.py`)

| Setting | Default | Description |
|---|---|---|
| `risk_percent` | 2.0 | Risk per trade (% of balance) — backtests use tiered fixed risk |
| `max_daily_trades` | 15 | Max trades per day |
| `max_spread` | 20.0 | Max spread in points before skipping entry |
| `trail_multiplier` | 0.3 | Trailing stop distance = multiplier × SL distance |
| `trailing_stop_enabled` | True | Master toggle for trailing stop logic |
| `circuit_breaker_max_daily_loss_pct` | 3.0 | Daily loss limit (%) — blocks new entries |
| `circuit_breaker_max_consecutive_losses` | 4 | Max consecutive losses before pause |
| `circuit_breaker_max_drawdown_pct` | 15.0 | Max drawdown from peak (%) — kill switch |
| `news_filter_enabled` | False | Enable ForexFactory news blackout (US Eastern → UTC) |
| `news_blackout_minutes` | 30 | Minutes before/after high-impact event to block entry |
| `backtest_commission` | 3.5 | Commission per lot per side ($) |

## Usage

### Live Trading

```bash
# ORB + Free Trade (default .env)
python scripts/run_live.py

# Aggressive M1 on different account
python scripts/run_aggressive.py --env .env.aggressive
```

The bot:
1. Connects to MT5, MongoDB, Telegram on startup
2. Loads 90 days of M15 data and builds institutional zones
3. **Orphan recovery:** Scans for existing MT5 positions on startup — if one is found, adopts it into management (prevents duplicate trades after crash/restart)
4. **Auto-adjust:** Scales risk % and max trades/day to account balance
5. Polls for new M5 bars every **30 seconds** during trading hours
6. **ORB mode:** Scans all sessions (Asia → London → NY) for breakout/pullback/reversal signals
7. **Free trade mode:** Falls through to HTF + zone + FVG signals when no ORB range is active
8. Places market orders with SL and wide TP (SL recalculated from current tick, never stale signal price; SL also checked to be ≥ 5 pips from bid/ask)
9. Manages every open position via bar-by-bar iteration (TP1, TP2, trail, SL/BE)
10. Sends Telegram alerts for open, close, error, and heartbeat
11. Closes any open position at 17:00 UTC Friday, disconnects, and sleeps until Monday 00:00 UTC (auto-restart)

### Backtesting

```bash
# ORB Scalper (session-based, free trade fallback)
python scripts/backtest.py --start 2025-09-01 --end 2026-06-15 --balance 1000

# Aggressive M1 (zone+momentum, 24/7)
python scripts/backtest_aggressive.py --start 2025-09-01 --end 2026-06-15 --balance 1000
```

Both backtests use tiered fixed risk, 0.5 max lots, slippage model, and 20-point spread filter. Results are saved as JSON with `--output`.

- `--risk <pct>` — risk percent (backtests use tiered fixed risk regardless)
- `--output <file>` — save results as JSON

## Risk Management

- **Risk per trade:** Tiered fixed risk per trade — $10 (profit <$500), $15 ($500+), $20 ($2,000+), $30 ($10,000+), $50 ($50,000+). Live uses %-of-balance auto-adjust.
- **Max position:** Hard-capped at 0.5 lots in backtests; live capped at 10.0 lots
- **Slippage model:** 1-2 pips on entry, 0-1 pip on exit (backtest only — live uses market fills)
- **Max daily trades:** Auto-adjusts: 5 (< $200), 10 ($200–$500), 15 ($500+)
- **Min balance:** $50 (bot refuses to start below this)
- **SL from bid/ask:** SL must be ≥ 5 pips from bid (buys) or ask (sells) — prevents wide-spread entries from placing SL directly at market
- **Partial profit locking:** SL moves to breakeven after TP1 hit
- **Trailing stop:** 0.3× SL distance, activates after TP1 (50-50) or TP2 (3-target); skips activation bar to avoid wick noise
- **Spread filter:** Skips entry if spread > 20 points (backtest) or configured threshold (live)
- **Circuit breaker:** Blocks entry after 3% daily loss / 4 consecutive losses / 15% drawdown
- **News filter:** (Optional) blocks entry during high-impact USD events (ForexFactory)
- **Commission:** $3.50 per lot per side (built into all calculations)

## Telegram Alerts

| Alert | Trigger | Info |
|---|---|---|
| **Signal** | Entry condition met | Direction, entry/SL/TP, pip distance, R:R, setup name (ORB Breakout Pullback / ORB Aggressive FVG / ORB Range Reversal / Free Pullback / Free FVG) |
| **Open** | Order filled | Direction, lot size, exit model, entry/SL/TP, risk %, commission, setup name |
| **Close** | Position fully closed | P&L with emoji (green/red), exit reason (TP/trail/BE/SL) with icon, R:R earned, targets hit, duration, balance |
| **Partial** | TP1, TP2, or trail filled | Lots, price, P&L, cumulative P&L |
| **Daily Summary** | End of day | Trades split by type (ORB vs Free), wins/losses, WR, P&L, PF, DD, balance |
| **Heartbeat** | Every 6 hours | Balance, equity, uptime, position status, daily trades count |
| **Error** | On failure | Error message and timestamp |

All trade alerts are tagged `[ORB]` or `[FREE]` so you can distinguish session-based entries from free-trade fallback entries at a glance.

**Message volume:** ~5 messages per trade (OPEN → TP1 → TP2 → TRAIL → CLOSE) = ~75/day at 15 trades max, plus 4 heartbeats + 1 summary ≈ **80 messages/day total**.

## Architecture Notes

- **All times in UTC.** MT5 timestamps are Unix epoch → converted with `utc=True`. Session hours are hardcoded as UTC. NewsFilter converts ForexFactory Eastern times → UTC via `zoneinfo`.
- **Bar-by-bar position management.** On each 30s poll, the bot examines every M5 bar since the position's open time, applying TP1/TP2/trail/SL checks sequentially. Flags prevent re-triggering.
- **Trail activation bar skip.** The trailing stop check skips the bar where it was just activated, preventing wick noise from stopping out the runner. Trail level is ratcheted on subsequent bars.
- **Spread computed live** as `(ask − bid) / point` since `tick.spread` is unavailable on some MT5 builds.
- **Same-tick SL placement.** Spread check, entry price, and SL calculation all use the same `get_tick()` call. This prevents the bug where a separate earlier tick passes the spread filter but the actual entry tick has wider spread, causing SL to land 1 pip from bid.
- **Partial close failure guard.** MT5 close is attempted before state is updated. If the close fails (network blip, position already gone), `remaining_lots` is unchanged and the next poll retries.
- **Orphan position recovery.** On startup, the bot scans for existing MT5 positions. If found, they're adopted into local management — no orphaned trades run unmanaged and no duplicate opens on top of them.
- **Logs are line-buffered** (`reconfigure(line_buffering=True)`) for real-time terminal output.
- **No external dependencies beyond MT5, pandas, numpy, pymongo, python-dotenv, requests, pydantic, python-json-logger.**
