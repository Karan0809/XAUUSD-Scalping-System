# XAUUSD Scalper

Multi-session scalper for XAUUSD on MetaTrader 5 with two bots: an ORB session-based breakout bot and an aggressive M1 zone+momentum scalper.

## Strategy

### Sessions

Each trading day is split into three sessions, each with its own fresh opening range. The bot enters trades in all sessions.

| Session | UTC Hours | Opening Range |
|---|---|---|
| **Asia** | 00:00–09:00 | First 15-min candle at 00:00 |
| **London** | 09:00–12:00 | First 15-min candle at 09:00 |
| **New York** | 13:30–16:00 | First 15-min candle at 13:30 |

The bot runs Mon–Thu 00:00–17:00 UTC, Fri until 17:00 UTC (then disconnects and sleeps until Monday 00:00 UTC).

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

### Entry Types

| Type | Trigger | Condition |
|---|---|---|---|---|
| **Breakout Pullback** | Price breaks the opening range, then pulls back into a POI | 5-min candle shows bullish/bearish reversal within POI |
| **Aggressive FVG** | Price re-enters a FVG left after the breakout | No waiting for a pullback — enters immediately on FVG touch |
| **Range Reversal** | Price sweeps the opening range boundary on the 5-min chart | Reversal candle with wick at the sweep point |

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

On every poll, the bot re-examines all M5 bars since entry (from the position's `open_time` in the rates index to the current bar). This guarantees that if TP1, TP2, or a trail/SL event occurred on a closed bar that is no longer the most recent bar, it is still detected and acted upon. Since all conditions are guarded by flags (`tp1_hit`, `tp2_hit`, `remaining_lots`), re-processing is **idempotent** — safe to repeat endlessly.

## Safety Filters

| Filter | Description | Default |
|---|---|---|
| **Spread filter** | Skips entries when spread exceeds threshold | 60 pips |
| **Circuit breaker** | Blocks new entries on 3% daily loss, 4 consecutive losses, or 15% drawdown from peak; sends Telegram alert on first block | On |
| **News filter** | Optional — blocks entry 30 min before/after high-impact USD events (ForexFactory) | Off |
| **Friday shutdown** | Bot disconnects at 17:00 UTC Friday, sleeps until Monday 00:00 UTC | Auto |

## Backtest Results (Sep 2025 – Jun 2026)

Backtested on live M5/M1 XAUUSD data across all sessions (Asia + London + NY). Commission: $3.50/lot/side. All tests use tiered fixed risk ($10→$15→$20→$30→$50 based on profit milestones), 0.5 lots hard cap, 1-2 pip entry slippage, 0-1 pip exit slippage, `max_spread` from settings (default 60.0), and `trail_multiplier=0.2`.

### ORB Scalper ($1,000 start)

Trades all sessions using ORB pipeline (breakout pullback, aggressive FVG, range reversal). Each session allows at most 1 entry. Lot size determined by tiered fixed risk / SL distance, capped at 0.5 lots.

| Metric | Result |
|---|---|
| **Total Trades** | 1,292 |
| **Win Rate** | 84.06% |
| **Total Profit** | **$67,594** |
| **Profit Factor** | 28.71 |
| **Max Drawdown** | $20.41 (1.00%) |
| **Avg Win / Loss** | +$64.49 / -$11.84 |
| **Largest Win / Loss** | +$644.07 / -$12.25 |
| **Avg Bars Held** | 1.5 |
| **Return** | 6,759% |

### Aggressive M1 ($5,000 start)

Trades M1 bars using zone-based entries with EMA50 trend slope filter, M1 micro-trend alignment + momentum check, and session filter (Asia + London + NY). Zone SL with 15-pip buffer (20-pip min, 50-pip fallback, 80-pip cap). 50/50 + trailing exit model with trail capped at entry.

| Metric | Result |
|---|---|
| **Total Trades** | 1,483 |
| **Win Rate** | 65.81% |
| **Total Profit** | **$63,699** |
| **Profit Factor** | 5.78 |
| **Max Drawdown** | $86.29 (1.00%) |
| **Avg Win / Loss** | +$78.92 / -$26.28 |
| **Largest Win / Loss** | +$2,106.71 / -$27.25 |
| **Avg Bars Held** | 1.5 |
| **Filters** | Zone=0 Mom=3,803 Trend=12,600 Spread=242 CB=0 News=0 |
| **Return** | 1,274% |

### Key Fixes Applied

| Fix | Impact |
|---|---|
| **Tiered fixed risk** ($10→$15→$20→$30→$50 based on profit) | Replaces flat $10 — grows with account without compounding explosion. |
| **0.5 lots hard cap** (was 10.0) | Limits position size regardless of account growth. |
| **Slippage model** (1-2 pip entry, 0-1 pip exit) | More realistic fills, prevents edge-case overperformance. |
| **`elif` in session/date reset** | Stopped double-reset bug that cleared `_entry_triggered`, causing duplicate entries. |
| **3-bar minimum gap** | Safety net preventing re-entry within same session after a close. |
| **Recovery entries** | After a loss, next entry tightens SL using M5 swing level — same risk, larger size. |
| **Spread filter 20 points** (was 60) | Blocks wider spreads — safer for tight SL scalping. |
| **Multi-env with `--env` CLI flag** | Run multiple bots simultaneously on separate MT5 accounts via `.env.orb` / `.env.aggressive`. |
| **Lazy env loading in settings.py** | `field(default_factory=...)` evaluates env vars after `load_dotenv()`, preventing stale values. |
| **Settings cache order** | `setup_logging()` moved after bot init so the correct env file sets the global cache first. |
| **`mt5.login()` after `initialize()`** | Explicitly logs into the account from the env file instead of reusing the terminal's cached session. |
| **Close retcode=10013 fix** | Retries close without `type_filling` if IOC rejected; falls back to finding actual position ticket from MT5 if stored ticket is stale. |
| **AutoTrading auto-enable** | Sends Alt+T keystroke via Win32 API if `terminal_info().trade_allowed` is False after connect. |
| **`place_order` returns `result.deal`** | ICMarkets returns 0 for `result.order` on market execution; uses `result.deal` as position ticket instead. |
| **Bar scan from open_time** | Changed from fixed 30-bar window to scanning from position's `open_time` in the rates DataFrame — no more missed triggers after reconnect. |
| **Partial-close detection in orphan recovery** | After restart, queries MT5 history for exit deals. If TP1 was partially closed before crash, converts remaining position to trail-only. |
| **Removed slippage guard** | Guard compared stale signal entry price vs live market (diff 2-5pts vs max 0.50), blocking every trade. Entry is at market — guard was redundant. |
| **`_check_swing_break()` last 5 bars** | Checks last 5 bars instead of only the most recent — prevents signal death when breakout candle is non-recent. |
| **Zone rebuild without race window** | `build_historical()` saves old zones, rebuilds in-place, restores on error — no gap where zones are cleared. |
| **`trade_stops_level` from symbol info** | Uses broker's actual minimum stop distance instead of hardcoded values — eliminates `Invalid stops (10016)` errors. |
| **SL/TP actual values returned from `place_order`** | Returns the SL/TP values after broker `trade_stops_level` adjustment (including 10016 retry adjustments). Both bots store these actual values in position dicts, so TP1 levels, trail distances, and P&L estimates all match MT5. |
| **Aggressive bot TP set to 500 pips** | Was 20 pips (= TP1 level), causing MT5 to auto-close the full position at 1:1 before the bot could manage partial close + trailing. Now set far away as a safety net. |
| **P&L double-count fix** | Aggressive bot's `_close_partial` exception handler overwrites P&L with total from deal history instead of adding to existing partial P&L. |
| **Log/MongoDB use actual SL/TP** | All log messages and database records use the post-adjustment SL/TP from `place_order` instead of pre-adjustment input values. |
| **`exit` field consistency** | Both bots default `exit` to `None` when no partial close occurred (instead of defaulting to entry price). |
| **`NameError` fix in `_manage_position`** | ORB bot used `rates.index.get_loc()` but the parameter is named `df` — would crash on first poll with an open position. |
| **Crash guard after `_resolve_position_closed`** | When `_close_partial`'s exception handler called `_resolve_position_closed` → `self._position = None`, the loop body crashed on next iteration. Fixed by adding guards and `break`. |
| **Trade double-count on stale-ticket cleanup** | Aggressive bot's handler set `pos["remaining_lots"] = 0` without `pos["closed"] = True` — caused P&L recorded twice. Fixed. |
| **Consecutive losses not reset on new day** | `start_day()` didn't clear `_consecutive_losses`, so a streak persisted across days. Fixed. |
| **Peak balance only updated via `start_day()`** | Intraday balance increases were invisible — bot killed itself on phantom drawdown. Fixed. |
| **Telegram alert on circuit breaker block** | CB blocked silently — no notification. Fixed with sentinel flag and `alert_error()`. |
| **Backtest spread hardcoded to 20.0** | Live bot used `settings.max_spread` (60) but backtests used 20 — inconsistent. Fixed. |
| **Aggressive stale-ticket PnL = 0** | When position disappeared without deal history, PnL was 0. Fixed by computing from SL price. |
| **Missing `trade_logger` close on aggressive stale ticket** | `trades.log` incomplete for stale-ticket path. Fixed. |
| **Friday reconnect ignores mongo return** | `mongo.connect()` return unchecked after weekend. Fixed with warning. |
| **SL updated before broker modify confirms** | TP1 set `pos["sl"] = entry` before modify — premature close on next bar. Fixed. |
| **Telegram heartbeat fires immediately on startup** | First loop iteration sent heartbeat — blocked startup 20s. Fixed. |
| **Telegram 10s timeout blocks bot** | Timeout reduced to 5s with exponential backoff. Fixed. |
| **Ticket mismatch — deal vs position** | Used `result.deal` as position ticket but it's a deal ID — never matched `get_positions()`. Fixed by using `result.order`. |

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
│   ├── risk_manager.py          # Risk controls (daily loss, consecutive losses, drawdown)
│   ├── news_filter.py           # ForexFactory news blackout filter
│   └── session_validator.py     # Session day validation
├── database/
│   └── mongo_client.py          # MongoDB persistence (trades, signals, metrics)
├── log_utils/
│   └── logger_setup.py          # Structured JSON logging (console + file)
├── scripts/
│   ├── backtest.py              # Historical backtester (ORB)
│   ├── backtest_aggressive.py   # Historical backtester (Aggressive M1)
│   ├── run_live.py              # Live trading bot (ORB)
│   └── run_aggressive.py        # Live trading bot (Aggressive M1)
├── telegram/
│   └── alerts.py                # Telegram notifications (open/close/error/heartbeat)
├── tests/
│   ├── test_cases.py            # 210+ unit tests (entry logic, risk mgr, session, spread, CB, logging, PnL, SL/TP)
│   └── test_resilience.py       # 36 resilience tests (MT5 failures, reconnect, timeout, partial data)
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
| `MT5_PORTABLE` | Set `true` to run terminal in portable mode (stores data locally, not in AppData) — required for separate copies of MT5 |
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

# Run both simultaneously (separate terminal windows)
start powershell python scripts/run_live.py --env .env.orb
start powershell python scripts/run_aggressive.py --env .env.aggressive
```

The `--env` path resolves relative to the project root (not the working directory). Each bot connects to its own MT5 terminal via `mt5.login()` after initialization — no shared sessions between bots.

### Key Settings (`config/settings.py`)

| Setting | Default | Description |
|---|---|---|
| `risk_percent` | 2.0 | Risk per trade (% of balance) — backtests use tiered fixed risk |
| `max_daily_trades` | 15 | Max trades per day |
| `max_spread` | 60.0 | Max spread in points before skipping entry |
| `trail_multiplier` | 0.2 | Trailing stop distance = multiplier × SL distance (0.2 outperforms 0.3 across all metrics) |
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
# ORB scalper (default .env)
python scripts/run_live.py

# Aggressive M1 on different account
python scripts/run_aggressive.py --env .env.aggressive
```

Both bots:
1. Connect to MT5, MongoDB, Telegram on startup
2. Load 90 days of M15 data and build institutional zones
3. **Orphan recovery:** Scans for existing MT5 positions on startup — adopts into management (prevents duplicate trades after crash/restart)
4. Poll for new bars every **30 seconds** during trading hours
5. Place market orders with SL and wide TP via `place_order`
6. Manage open positions via bar-by-bar iteration from `open_time` (TP1, trail, SL/BE)
7. Send Telegram alerts for open, close, error, and heartbeat
8. Close open positions at **17:00 UTC Friday**, disconnect, sleep until Monday 00:00 UTC

**ORB bot** (`run_live.py`): Scans sessions (Asia → London → NY) for breakout/pullback/reversal signals on M5.

**Aggressive bot** (`run_aggressive.py`): Scans M1 bars for zone-based entries with HH/HL + EMA50 trend filter and M1 micro-trend alignment.

### Testing

```bash
# Run full test suite (246 tests)
python -m pytest tests/

# Run with verbosity
python -m pytest tests/ -v --tb=short

# Run specific test class
python -m pytest tests/ -k TestMongoWriteFailure -v
```

### Backtesting

```bash
# ORB Scalper
python scripts/backtest.py --start 2025-09-01 --end 2026-06-03 --balance 1000

# Aggressive M1 (min_sl is default now)
python scripts/backtest_aggressive.py --start 2025-09-01 --end 2026-06-19 --balance 5000 --risk 1.2 --sl-mode min_sl --sl-pips 50 --zone-buffer 0.15 --session-filter
```

Both backtests use tiered fixed risk, 0.5 max lots, slippage model, and read `max_spread` from settings. Results saved as JSON with `--output`.

- `--risk <pct>` — risk percent
- `--sl-mode min_sl` — zone-based SL with minimum distance floor
- `--zone-buffer <pips>` — buffer added to zone edge for SL
- `--session-filter` — restrict to Asia/London/NY sessions
- `--output <file>` — save results as JSON

## Risk Management

- **Risk per trade:** %-of-balance (live) or tiered fixed risk (backtest). Aggressive bot uses 1.2% risk = SL distance × lot size.
- **Max position:** Hard-capped at 0.5 lots in backtests; live capped at 10.0 lots
- **Slippage model:** 1-2 pips on entry, 0-1 pip on exit (backtest only — live uses market fills)
- **Max daily trades:** Auto-adjusts (default 20/day, currently relaxed for demo testing)
- **Min balance:** $50 (bot refuses to start below this)
- **Partial profit locking:** SL moves to breakeven after TP1 hit (50% of position closed at 1:1)
- **Trailing stop:** 0.3× SL distance, activates after TP1
- **Spread filter:** Skips entry if spread > 20 points, sleeps 10s
- **Circuit breaker:** Daily loss / consecutive loss / drawdown limits (configurable); currently relaxed for demo
- **News filter:** (Optional) blocks entry during high-impact USD events (ForexFactory)
- **Commission:** $3.50 per lot per side (built into all calculations)

## Telegram Alerts

| Alert | Trigger | Info |
|---|---|---|
| **Signal** | Entry condition met | Direction, entry/SL/TP, pip distance, R:R, setup name (ORB Breakout Pullback / ORB Aggressive FVG / ORB Range Reversal) |
| **Open** | Order filled | Direction, lot size, exit model, entry/SL/TP, risk %, commission, setup name |
| **Close** | Position fully closed | P&L with emoji (green/red), exit reason (TP/trail/BE/SL) with icon, R:R earned, targets hit, duration, balance |
| **Partial** | TP1, TP2, or trail filled | Lots, price, P&L, cumulative P&L |
| **Daily Summary** | End of day | Wins/losses, WR, P&L, PF, DD, balance |
| **Heartbeat** | Every 6 hours | Balance, equity, uptime, position status, daily trades count |
| **Error** | On failure | Error message and timestamp |

**Message volume:** ~4 messages per trade (OPEN → TP1 → TRAIL → CLOSE), plus heartbeats + daily summary.

## Architecture Notes

- **All times in UTC.** MT5 timestamps are Unix epoch → converted with `utc=True`. Session hours are hardcoded as UTC.
- **Bar-by-bar position management.** On each 30s poll, the bot examines every bar since the position's open time, applying TP1/trail/SL checks sequentially. Flags prevent re-triggering.
- **Trail activation bar skip.** The trailing stop check skips the bar where it was just activated, preventing wick noise from stopping out the runner.
- **Bar scan from open_time.** Scans all bars from the position's `open_time` to the current bar — prevents missed triggers if the bot was stopped for many bars.
- **Spread computed live** as `(ask − bid) / point` since `tick.spread` is unavailable on some MT5 builds.
- **Same-tick SL placement.** Spread check, entry price, and SL calculation all use the same `get_tick()` call — prevents mismatch bugs.
- **SL/TP from broker.** `place_order()` returns actual SL/TP values after broker `trade_stops_level` adjustment. All downstream calculations use these actual values.
- **Aggressive bot TP is a far safety net** (500 pips). Prevents MT5 from auto-closing at TP1 level. The bot manages all exits via `order_send`.
- **Partial close failure guard.** Retries without `type_filling` on failure, falls back to finding actual position ticket from MT5.
- **Orphan position recovery.** On startup, scans for existing MT5 positions and adopts them — prevents duplicate opens after crash/restart.
- **Multi-account isolation.** Each bot uses `mt5.login()` after `initialize()` for explicit account connection. Separate MT5 copies with `MT5_PORTABLE=true`.
- **AutoTrading auto-enable.** Sends Alt+T via Win32 API if `trade_allowed` is False after connect.
- **Settings cache ordering.** `setup_logging()` called after bot init so the correct env file populates the cache.
- **Logs are line-buffered** for real-time terminal output.**
