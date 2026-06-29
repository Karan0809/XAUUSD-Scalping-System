# XAUUSD Scalper — Aggressive M1 + Mindspace Engine

Multi-session scalper for XAUUSD on MetaTrader 5. Runs two bots:
- **Aggressive M1**: Zone + momentum scalper on M1 bars (live-tested, 78% WR)
- **Mindspace**: SMC/ICT strategy (CHOCH, FVG, ISS, TJL) on dual HTF+scalp engines (backtested, 53.69% WR, 1327% return)

## Strategy (Aggressive M1)

The aggressive bot trades a **zone + momentum** strategy on M1 bars. It scans supply/demand zones from 90-day M15 data, waits for a pullback into a zone, and enters on momentum confirmation. The circuit breaker blocks per-session (Asia/London/NY) rather than full-day.

### Sessions

Each trading day is split into three sessions. The circuit breaker resets on each session change.

| Session | UTC Hours |
|---|---|
| **Asia** | 00:00–09:00 |
| **London** | 09:00–12:00 |
| **New York** | 13:00–16:00 |

The bot runs Mon–Thu 00:00–17:00 UTC, Fri until 17:00 UTC (then disconnects and sleeps until Monday 00:00 UTC).

### Entry Logic

| Step | Check | Action |
|---|---|---|
| 1. **Zone** | Find nearest unbreached demand (buy) or supply (sell) zone below/above current price | Direction + zone SL = zone edge ± 0.30 buffer |
| 2. **Pullback** | Price within 3.0 of zone edge | Skip if too far from zone |
| 3. **Trend** | EMA50 on M15 | If trend opposes direction, try opposite direction; skip if no alt zone |
| 4. **Momentum** | Current M1 bar close > open AND close > prev close (buy) or opposite (sell) | Skip if no momentum |
| 5. **SL calc** | Zone distance capped between 0.30–5.0 | SL = price ± zone distance |
| 6. **Entry** | Market order with calculated SL, TP = SL × 25 | Tracks +1 trade counter |

## Position Management Lifecycle

Once a trade opens, the bot polls **every 30 seconds** and examines **every M1 bar** since the position's open time (not just the last bar). On each bar it runs these checks in order:

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

On every poll, the bot re-examines all M1 bars since entry (from the position's `open_time` in the rates index to the current bar). This guarantees that if TP1, trail, or SL event occurred on a closed bar that is no longer the most recent bar, it is still detected and acted upon. Since all conditions are guarded by flags (`tp1_hit`, `remaining_lots`), re-processing is **idempotent** — safe to repeat endlessly.

## Safety Filters

| Filter | Description | Default |
|---|---|---|
| **Spread filter** | Skips entries when spread exceeds threshold | 60 pips |
| **Circuit breaker** | Blocks new entries on 3% daily loss, 4 consecutive losses, or 15% drawdown from peak; blocks per-session (resets on Asia/London/NY change); sends Telegram alert on first block | On |
| **News filter** | Optional — blocks entry 30 min before/after high-impact USD events (ForexFactory) | Off |
| **Friday shutdown** | Bot disconnects at 17:00 UTC Friday, sleeps until Monday 00:00 UTC | Auto |

## Backtest Results (Sep 2025 – Jun 2026)

Backtested on live M1 XAUUSD data across all sessions (Asia + London + NY). Commission: $3.50/lot/side, 1-2 pip entry slippage, 0-1 pip exit slippage, `max_spread` from settings (default 60.0), `trail_multiplier=0.3`.

### Aggressive M1 ($1,000 start)

Trades M1 bars using zone-based entries with EMA50 trend slope filter, M1 micro-trend alignment + momentum check, and session filter (Asia + London + NY). Zone SL with 0.30 buffer, clamped between 0.30–5.0. Pullback filter (3.0 max dist from zone edge). 50/50 + trailing exit model. Tiered fixed risk ($10→$15→$20→$30→$50 based on profit milestones), 1.0 lots hard cap.

| Metric | Result |
|---|---|
| **Total Trades** | 1,240 |
| **Win Rate** | 78.06% |
| **Total Profit** | **$115,239** |
| **Profit Factor** | 18.96 |
| **Max Drawdown** | $26.19 (1.61%) |
| **Avg Win / Loss** | +$125.68 / -$23.59 |
| **Largest Win / Loss** | +$1,425.49 / -$24.49 |
| **Avg Bars Held** | 1.4 |
| **Filters** | Zone=0 Mom=3,173 Trend=10,685 Spread=201 CB=0 News=0 |
| **Return** | 11,523% |

> **Backtest vs live discrepancy:** Backtest uses bar-resolution SL (M1 high/low), which misses intra-bar spikes. Live tick-level volatility can hit SL ~4-8 points earlier than backtest suggests. The widened SL (up to 5.0) and pullback filter compensate for this gap.

### Key Fixes Applied

| Fix | Impact |
|---|---|---|
| **Tiered fixed risk** ($10→$15→$20→$30→$50 based on profit) | Replaces flat $10 — grows with account without compounding explosion. |
| **1.0 lots hard cap** (was 10.0, later tightened from 0.5 backtest cap) | Limits position size while allowing room to scale. At $2,300 balance, margin naturally limits to ~0.51 lots; cap only binds once account exceeds ~$4,500. Backtested: 1.0 cap yields +92% profit over 0.5 cap with same 1.6% max DD. |
| **Trail multiplier 0.3** (was 0.2) | Wider trail reduces premature exits; backtest confirmed 0.3 is optimal for the 78% WR strategy. |
| **Slippage model** (1-2 pip entry, 0-1 pip exit) | More realistic fills, prevents edge-case overperformance. |
| **`elif` in session/date reset** | Stopped double-reset bug that cleared `_entry_triggered`, causing duplicate entries. |
| **3-bar minimum gap** | Safety net preventing re-entry within same session after a close. |
| **Recovery entries** | After a loss, next entry tightens SL using M5 swing level — same risk, larger size. |
| **Spread filter 20 points** (was 60) | Blocks wider spreads — safer for tight SL scalping. |
| **Multi-env with `--env` CLI flag** | Run multiple bots simultaneously on separate MT5 accounts via `.env.aggressive` / `.env.mindspace`. |
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
| **Removed zone-SL proximity filter** | `_get_zone_signal()` had a 0.80 filter rejecting zones within 0.80 of price — set `zone_sl=None` on ALL signals. Removed. |
| **AutoTrading 3-layer fallback** | pywin32 `SetForegroundWindow`+`SendKeys` → PowerShell `AppActivate`+`SendKeys` → direct `origin.cfg` config modification + terminal restart. Handles headless VPS where no MT5 window is visible. |
| **Per-trade-loop account re-verification** | After `get_account_info()`, checks `acct["login"]` matches env setting. M15 data loads reverting MT5 login are detected and corrected mid-loop. |
| **Zone-SL cap removed** | Zone distance was capped at 0.80, forcing SL to 80 points regardless of market structure. Now uses `min(raw_dist, 5.0)` — SL matches zone depth (30–500 points). Fixes root cause of 30-60s stopouts. |
| **Circuit breaker re-enabled** | Was completely commented out returning `(True, None)`. Logs showed 4-7 consecutive losses reached but bot kept trading. Now checks consecutive losses, daily loss %, drawdown. |
| **Circuit breaker per-session** | Changed from full-day `_blocked_today` to per-session `_blocked_session`. Block auto-clears at session change; `_daily_loss_sum` also resets so a new session isn't blocked by the prior session's losses. |
| **Max trades per day disabled** | Line 640 check was commented out — bot traded unlimited daily. Uncommented for now (removed during testing). |
| **Pullback filter added** | Enters only when price is within 3.0 of zone edge. Calibrated from backtesting: baseline 65.7% WR (1,263 trades) vs pullback 3.0 at 69.6% WR (161 trades). |
| **Zone buffer 0.15 → 0.30** | Increased buffer added to zone edge for SL calculation — gives trades more breathing room from zone boundary. |

## Project Structure

```
├── config/
│   ├── settings.py              # All configurable parameters (risk, sessions, API keys, safety toggles)
│   └── sessions.py              # Session time definitions & validators
├── connectors/
│   └── mt5_connector.py         # MetaTrader 5 wrapper (rates, orders, positions, modify)
├── core/
│   ├── institutional_zone.py    # Supply/demand zone detection
│   ├── risk_manager.py          # Risk controls (daily loss, consecutive losses, drawdown, per-session CB)
│   ├── news_filter.py           # ForexFactory news blackout filter
│   ├── session_validator.py     # Session day validation
│   └── mindspace/               # SMC/ICT engine modules
│       ├── models.py            # Signal, ISSZone data classes
│       ├── structures.py        # Structure marker (swing points)
│       ├── choch.py             # Change of character detector
│       ├── levels.py            # SBR/RBS/QML/DB/DT levels
│       ├── supply_demand.py     # Order block detector
│       ├── fvg.py               # Fair value gap detector
│       ├── iss.py               # 5-wave ISS detector
│       ├── tjl.py               # TJL1/QML and TJL2 engine
│       ├── mtf.py               # Multi-timeframe analyzer (Cond 1/2/3)
│       └── engine.py            # Mindspace orchestrator
├── database/
│   └── mongo_client.py          # MongoDB persistence (trades, signals, metrics)
├── log_utils/
│   └── logger_setup.py          # Structured JSON logging (console + file)
├── scripts/
│   ├── backtest_aggressive.py   # Historical backtester (zone + momentum)
│   ├── backtest_mindspace.py    # Historical backtester (SMC/ICT dual engine)
│   ├── run_aggressive.py        # Live trading bot (aggressive M1)
│   ├── run_mindspace.py         # Live trading bot (mindspace SMC/ICT)
│   └── run_both.ps1             # Launcher — starts both bots in separate windows
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

Create `.env` (or `.env.aggressive`):

| Variable | Description |
|---|---|
| `MT5_LOGIN` | MT5 account number |
| `MT5_PASSWORD` | MT5 account password |
| `MT5_SERVER` | Broker server (e.g. `MetaQuotes-Demo`) |
| `MT5_PATH` | Path to terminal64.exe |
| `MT5_PORTABLE` | Set `true` to run terminal in portable mode (stores data locally, not in AppData) — required for separate copies of MT5 |
| `MONGO_URI` | MongoDB connection string |
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID (comma-separated for multiple) |

Run with the env file:

```bash
python scripts/run_aggressive.py --env .env.aggressive
```

The `--env` path resolves relative to the project root (not the working directory).

### Key Settings (`config/settings.py`)

| Setting | Default | Description |
|---|---|---|---|
| `risk_percent` | 2.0 | Risk per trade (% of balance) — aggressive bot uses tiered fixed risk |
| `max_daily_trades` | 15 | Max trades per day |
| `max_spread` | 60.0 | Max spread in points before skipping entry |
| `trail_multiplier` | 0.3 | Trailing stop distance = multiplier × SL distance (0.3 optimal for 78% WR aggressive bot) |
| `trailing_stop_enabled` | True | Master toggle for trailing stop logic |
| `circuit_breaker_max_daily_loss_pct` | 10.0 | Daily loss limit (%) — blocks current session only (resets on session change) |
| `circuit_breaker_max_consecutive_losses` | 4 | Max consecutive losses before pause |
| `circuit_breaker_max_drawdown_pct` | 15.0 | Max drawdown from peak (%) — kill switch |
| `news_filter_enabled` | False | Enable ForexFactory news blackout (US Eastern → UTC) |
| `news_blackout_minutes` | 30 | Minutes before/after high-impact event to block entry |
| `backtest_commission` | 3.5 | Commission per lot per side ($) |
| `max_trades_per_day` | 5 | Max trades per day (mindspace bot) |
| `strategy_label` | "Mindspace" | Strategy tag for logs and DB |
## Usage

### Live Trading

```bash
python scripts/run_aggressive.py --env .env.aggressive
```

The bot:

1. Connect to MT5, MongoDB, Telegram on startup
2. Load 90 days of M15 data and build institutional zones
3. **Orphan recovery:** Scans for existing MT5 positions on startup — adopts into management (prevents duplicate trades after crash/restart)
4. Poll for new bars every **30 seconds** during trading hours
5. Place market orders with SL and wide TP via `place_order`
6. Manage open positions via bar-by-bar iteration from `open_time` (TP1, trail, SL/BE)
7. Send Telegram alerts for open, close, error, and heartbeat
8. Close open positions at **17:00 UTC Friday**, disconnect, sleep until Monday 00:00 UTC

**Aggressive bot** (`run_aggressive.py`): Scans M1 bars for zone-based entries with EMA50 trend filter, M1 momentum check, and pullback filter (3.0 from zone edge). SL uses zone distance clamped between 0.30–5.0 (no fixed cap). Circuit breaker resets per-session.

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
python scripts/backtest_aggressive.py --start 2025-09-01 --end 2026-06-22 --balance 1000 --sl-mode min_sl --zone-buffer 0.30 --max-lots 1.0
```

Backtest uses tiered fixed risk ($10–$50), 1.0 max lots, slippage model, and reads `max_spread` from settings. Results saved as JSON with `--output`.

| Arg | Default | Description |
|---|---|---|
| `--risk <pct>` | 1.2 | Risk percent (used for legacy; aggressive bot uses fixed tiers) |
| `--sl-mode` | `min_sl` | SL mode: `fixed` (pips), `min_sl` (zone + 20 pip min), `atr` (ATR×1.5) |
| `--zone-buffer <pips>` | 0.15 | Buffer added to zone edge for SL |
| `--session-filter` | off | Restrict to London (9-12) + NY (13-16) sessions |
| `--trail-multiplier` | from settings (0.3) | Override trail distance multiplier |
| `--max-lots` | 1.0 | Max lot size per trade (0 = no cap, margin only) |
| `--output <file>` | `aggressive_results.json` | Save results as JSON |

## Risk Management

- **Risk per trade:** Tiered fixed risk ($10→$15→$20→$30→$50 based on profit milestones). Aggressive bot uses fixed dollar amounts (not %-of-balance) to avoid compounding explosion at high WR.
- **Max position:** 1.0 lots hard cap (both backtest and live). Margin auto-limits to ~0.51 lots at $2,300 balance; the cap only binds when account exceeds ~$4,500.
- **Slippage model:** 1-2 pips on entry, 0-1 pip on exit (backtest only — live uses market fills)
- **Max daily trades:** 15/day (aggressive), 5/day (mindspace)
- **Min balance:** $50 (bot refuses to start below this)
- **Partial profit locking:** SL moves to breakeven after TP1 hit (50% of position closed at 1:1)
- **Trailing stop:** 0.3× SL distance, activates after TP1
- **Spread filter:** Skips entry if spread > 60 points (default), sleeps 10s
- **Circuit breaker:** Per-session (not per-day). Daily loss / consecutive loss / drawdown limits block only the current session; block and daily loss sum auto-reset on Asia/London/NY session change.
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
- **Bar-by-bar position management (M1).** On each 30s poll, the bot examines every bar since the position's open time, applying TP1/trail/SL checks sequentially. Flags prevent re-triggering.
- **Trail activation bar skip.** The trailing stop check skips the bar where it was just activated, preventing wick noise from stopping out the runner.
- **Bar scan from open_time.** Scans all bars from the position's `open_time` to the current bar — prevents missed triggers if the bot was stopped for many bars.
- **Spread computed live** as `(ask − bid) / point` since `tick.spread` is unavailable on some MT5 builds.
- **Same-tick SL placement.** Spread check, entry price, and SL calculation all use the same `get_tick()` call — prevents mismatch bugs.
- **SL/TP from broker.** `place_order()` returns actual SL/TP values after broker `trade_stops_level` adjustment. All downstream calculations use these actual values.
- **Aggressive bot TP is a far safety net** (500 pips). Prevents MT5 from auto-closing at TP1 level. The bot manages all exits via `order_send`.
- **Partial close failure guard.** Retries without `type_filling` on failure, falls back to finding actual position ticket from MT5.
- **Orphan position recovery.** On startup, scans for existing MT5 positions and adopts them — prevents duplicate opens after crash/restart.
- **Multi-account isolation.** Each bot uses `mt5.login()` after `initialize()` for explicit account connection.
- **AutoTrading auto-enable.** 3-layer fallback: pywin32 `SetForegroundWindow`+`SendKeys` → PowerShell `AppActivate`+`SendKeys` → direct `origin.cfg` config modification + terminal restart. Handles headless VPS where no MT5 window is visible.
- **Settings cache ordering.** `setup_logging()` called after bot init so the correct env file populates the cache.
- **Logs are line-buffered** for real-time terminal output.**
