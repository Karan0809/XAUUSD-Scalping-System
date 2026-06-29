#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import random

import pandas as pd
import numpy as np

from config.settings import get_settings
from config.sessions import SessionTimes
from log_utils.logger_setup import setup_logging
from core.institutional_zone import InstitutionalZoneDetector
from core.risk_manager import RiskManager
from core.news_filter import NewsFilter
from connectors.mt5_connector import MT5Connector, MT5ConnectorError

logger = logging.getLogger(__name__)

_MARGIN_RATE: Optional[float] = None


@dataclass
class AggBacktestResult:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_profit: float = 0.0
    final_balance: float = 0.0
    return_pct: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    avg_bars_held: float = 0.0
    total_commission: float = 0.0
    trades: List[Dict[str, Any]] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    equity_timestamps: List[datetime] = field(default_factory=list)
    spread_filtered: int = 0
    cb_blocked: int = 0
    news_filtered: int = 0
    zone_filtered: int = 0
    mom_filtered: int = 0
    trend_filtered: int = 0


def parse_args():
    parser = argparse.ArgumentParser(description="Aggressive M1 Scalper Backtest")
    parser.add_argument("--start", type=str, default="2025-09-01")
    parser.add_argument("--end", type=str, default="2026-06-10")
    parser.add_argument("--balance", type=float, default=1000.0)
    parser.add_argument("--risk", type=float, default=1.2, help="Risk percent per trade")
    parser.add_argument("--sl-pips", type=float, default=20.0, help="Fixed SL in pips")
    parser.add_argument("--max-trades", type=int, default=20, help="Max trades per day")
    parser.add_argument("--no-trend-filter", action="store_true", help="Disable M15 trend filter")
    parser.add_argument("--session-filter", action="store_true", help="Only trade during London (9-12) and NY (13-16) sessions")
    parser.add_argument("--sl-mode", type=str, default="min_sl", choices=["fixed", "min_sl", "atr"],
                        help="SL mode: fixed (pips arg), min_sl (zone SL + 20pip min), atr (ATR*1.5)")
    parser.add_argument("--zone-buffer", type=float, default=0.15,
                        help="Buffer below/above zone edge for SL (default: 0.15 = 15 pips)")
    parser.add_argument("--trail-multiplier", type=float, default=None,
                        help="Override trail multiplier (default: from settings = 0.3)")
    parser.add_argument("--max-lots", type=float, default=1.0,
                        help="Max lot size per trade (0 = no cap, margin only)")
    parser.add_argument("--output", type=str, default="aggressive_results.json")
    return parser.parse_args()


def _fetch_margin_rate() -> float:
    connector = MT5Connector()
    try:
        connector.connect()
        rate = connector.get_margin_rate()
        connector.disconnect()
        logger.info(f"Margin rate: ${rate:.2f}/lot")
        return rate
    except Exception as e:
        logger.warning(f"Failed to fetch margin rate: {e}, using default")
        return 1000.0


def load_m1_data(start: datetime, end: datetime) -> pd.DataFrame:
    connector = MT5Connector()
    try:
        connector.connect()
        import MetaTrader5 as mt5
        all_chunks = []
        current_end = end
        while current_end > start:
            chunk = mt5.copy_rates_from("XAUUSD", mt5.TIMEFRAME_M1, current_end, 50000)
            if chunk is None or len(chunk) == 0:
                break
            chunk_df = pd.DataFrame(chunk)
            chunk_df["time"] = pd.to_datetime(chunk_df["time"], unit="s", utc=True)
            chunk_df.set_index("time", inplace=True)
            all_chunks.append(chunk_df)
            current_end = chunk_df.index.min()
            if len(all_chunks) > 1 and (all_chunks[-1].index.min() == all_chunks[-2].index.min()):
                break
        connector.disconnect()
        if not all_chunks:
            raise MT5ConnectorError("No M1 data")
        df = pd.concat(all_chunks).sort_index()
        df = df[~df.index.duplicated(keep="last")]
        df = df[(df.index >= start) & (df.index <= end)]
        logger.info(f"Loaded {len(df)} M1 bars")

        # Precompute ATR(14) for atr sl_mode
        tr = pd.DataFrame({
            "hl": df["high"] - df["low"],
            "hc": (df["high"] - df["close"].shift(1)).abs(),
            "lc": (df["low"] - df["close"].shift(1)).abs(),
        })
        df["atr"] = tr.max(axis=1).rolling(14).mean()

        return df[["open", "high", "low", "close", "tick_volume", "spread", "atr"]]
    except Exception as e:
        logger.error(f"Failed to load M1 data: {e}")
        sys.exit(1)


def load_15min_data(start: datetime, end: datetime) -> pd.DataFrame:
    connector = MT5Connector()
    try:
        connector.connect()
        import MetaTrader5 as mt5
        all_chunks = []
        current_end = end
        while current_end > start:
            chunk = mt5.copy_rates_from("XAUUSD", mt5.TIMEFRAME_M15, current_end, 50000)
            if chunk is None or len(chunk) == 0:
                break
            chunk_df = pd.DataFrame(chunk)
            chunk_df["time"] = pd.to_datetime(chunk_df["time"], unit="s", utc=True)
            chunk_df.set_index("time", inplace=True)
            all_chunks.append(chunk_df)
            current_end = chunk_df.index.min()
            if len(all_chunks) > 1 and (all_chunks[-1].index.min() == all_chunks[-2].index.min()):
                break
        connector.disconnect()
        if not all_chunks:
            raise MT5ConnectorError("No M15 data")
        df = pd.concat(all_chunks).sort_index()
        df = df[~df.index.duplicated(keep="last")]
        df = df[(df.index >= start) & (df.index <= end)]
        logger.info(f"Loaded {len(df)} M15 bars")
        return df[["open", "high", "low", "close", "tick_volume"]]
    except Exception as e:
        logger.error(f"Failed to load M15 data: {e}")
        sys.exit(1)


def get_risk_amount(profit: float, balance: float = 0.0) -> float:
    if profit >= 50000:
        return 50.0
    elif profit >= 10000:
        return 30.0
    elif profit >= 2000:
        return 20.0
    elif profit >= 500:
        return 15.0
    return 10.0


def calc_lot_size(balance: float, risk_pct: float, sl_dist: float, margin_rate: Optional[float] = None, profit: float = 0.0, max_lots: float = 0.5) -> float:
    risk_amount = get_risk_amount(profit)
    risk_lots = round(risk_amount / (sl_dist * 100), 2)
    if margin_rate is not None and margin_rate > 0:
        margin_lots = max(0.01, round((balance * 0.9) / margin_rate, 2))
    else:
        margin_lots = 0.5
    cap = max_lots if max_lots > 0 else 999.0
    return max(0.01, round(min(risk_lots, margin_lots, cap), 2))


def check_momentum(bars: pd.DataFrame, direction: str) -> bool:
    if len(bars) < 3:
        return False
    last = bars.iloc[-1]
    prev = bars.iloc[-2]
    if direction == "buy":
        return last["close"] > last["open"] and last["close"] > prev["close"]
    else:
        return last["close"] < last["open"] and last["close"] < prev["close"]




def check_m1_alignment(m1_bars: pd.DataFrame, direction: str, min_aligned: int = 2) -> bool:
    if len(m1_bars) < 7:
        return True
    closes = m1_bars["close"].values[-6:]
    net_change = closes[-1] - closes[0]
    aligned = sum(1 for i in range(1, len(closes))
                  if (direction == "buy" and closes[i] >= closes[i-1]) or
                     (direction == "sell" and closes[i] <= closes[i-1]))
    if direction == "sell":
        return aligned >= min_aligned and net_change <= 0.50
    return aligned >= min_aligned and net_change >= -0.50


def check_trend(df_15min: pd.DataFrame, m15_idx: int, direction: str) -> bool:
    if "ema50" not in df_15min.columns or m15_idx < 50:
        return False
    ema = df_15min["ema50"]
    if ema.iloc[m15_idx - 1] > ema.iloc[m15_idx - 3]:
        return direction == "buy"
    elif ema.iloc[m15_idx - 1] < ema.iloc[m15_idx - 3]:
        return direction == "sell"
    return True


def print_results(result: AggBacktestResult, label: str = "AGGRESSIVE M1 SCALPER"):
    print("\n" + "=" * 60)
    print(f"  {label}")
    print("=" * 60)
    print(f"  Total Trades:      {result.total_trades}")
    print(f"  Winning Trades:    {result.winning_trades}")
    print(f"  Losing Trades:     {result.losing_trades}")
    print(f"  Win Rate:          {result.win_rate:.2f}%")
    print(f"  Total Profit:      ${result.total_profit:.2f}")
    print(f"  Final Balance:     ${result.final_balance:.2f}")
    print(f"  Return:            {result.return_pct:.2f}%")
    print(f"  Profit Factor:     {result.profit_factor:.2f}")
    print(f"  Max Drawdown:      ${result.max_drawdown:.2f} ({result.max_drawdown_pct:.2f}%)")
    print(f"  Avg Win:           ${result.avg_win:.2f}")
    print(f"  Avg Loss:          ${result.avg_loss:.2f}")
    print(f"  Largest Win:       ${result.largest_win:.2f}")
    print(f"  Largest Loss:      ${result.largest_loss:.2f}")
    print(f"  Avg Bars Held:     {result.avg_bars_held:.1f}")
    print(f"  Total Commission:  ${result.total_commission:.2f}")
    print(f"  Filters:           Zone={result.zone_filtered} Mom={result.mom_filtered} Trend={result.trend_filtered} Spread={result.spread_filtered} CB={result.cb_blocked} News={result.news_filtered}")
    print("=" * 60 + "\n")


def main():
    args = parse_args()
    setup_logging()

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    settings = get_settings()
    if args.trail_multiplier is not None:
        settings.trail_multiplier = args.trail_multiplier

    sl_pips = args.sl_pips
    sl_price = sl_pips / 100.0
    risk_pct = args.risk
    max_trades_per_day = args.max_trades

    df = load_m1_data(start, end)
    df_15min = load_15min_data(start, end)

    margin_rate = _fetch_margin_rate()

    df_15min["ema20"] = df_15min["close"].ewm(span=20, adjust=False).mean()
    df_15min["ema50"] = df_15min["close"].ewm(span=50, adjust=False).mean()

    zone_detector = InstitutionalZoneDetector()
    zone_detector.build_historical(df_15min)

    risk_mgr = RiskManager(
        max_daily_loss_pct=settings.circuit_breaker_max_daily_loss_pct,
        max_consecutive_losses=settings.circuit_breaker_max_consecutive_losses,
        max_drawdown_pct=settings.circuit_breaker_max_drawdown_pct,
    )

    news_filter = NewsFilter(
        blackout_minutes=settings.news_blackout_minutes
    ) if settings.news_filter_enabled else None
    if news_filter is not None:
        news_filter.fetch_events()

    result = AggBacktestResult()
    balance = args.balance
    peak_balance = balance
    position: Optional[Dict[str, Any]] = None
    trades_today = 0
    current_date = None
    m15_idx = 0
    last_entry_bar = -100

    for i in range(120, len(df)):
        current_time = df.index[i]
        date_str = current_time.strftime("%Y-%m-%d")

        if date_str != current_date:
            current_date = date_str
            trades_today = 0
            risk_mgr.start_day(date_str, balance)

        while m15_idx < len(df_15min) and df_15min.index[m15_idx] <= current_time:
            zone_detector.update(df_15min.iloc[m15_idx])
            m15_idx += 1

        zone_detector.update_test_status(df["high"].iloc[i], df["low"].iloc[i])

        if position:
            bar = df.iloc[i]
            is_buy = position["type"] == "buy"
            sl_dist = position["sl_dist"]

            def book(lots, price, reason):
                nonlocal balance
                slip = random.uniform(0.0, 0.01)
                if is_buy:
                    price -= slip
                else:
                    price += slip
                pdiff = price - position["entry"]
                if not is_buy:
                    pdiff = -pdiff
                raw = pdiff * lots * 100
                comm = settings.backtest_commission * lots
                net = raw - comm
                balance += net
                result.total_commission += comm
                position["pnl"] = round(position["pnl"] + net, 2)
                result.trades.append({
                    "type": position["type"], "entry": position["entry"],
                    "exit": price, "profit": round(net, 2),
                    "commission": round(comm, 2), "lot_size": lots,
                    "bars_held": i - position["entry_bar"],
                    "exit_reason": reason,
                    "entry_time": position["entry_time"],
                    "exit_time": current_time, "date": date_str,
                })

            tp1_level = position["entry"] + sl_dist if is_buy else position["entry"] - sl_dist

            # TP1 at 1:1 — close 50%, move SL to BE, activate trail
            if not position.get("tp1_hit") and \
               ((is_buy and bar["high"] >= tp1_level) or (not is_buy and bar["low"] <= tp1_level)):
                book(position["tp1_cents"] / 100.0, tp1_level, "tp1")
                position["remaining_cents"] -= position["tp1_cents"]
                position["sl"] = position["entry"]
                position["tp1_hit"] = True
                position["tp_hit_bar"] = i
                trail_dist = sl_dist * settings.trail_multiplier
                if is_buy:
                    position["trail_level"] = max(position["entry"], bar["high"] - trail_dist)
                else:
                    position["trail_level"] = min(position["entry"], bar["low"] + trail_dist)
                position["trailing_activated"] = True
                position["trail_activation_bar"] = i

            # Update trailing stop
            if position.get("trailing_activated") and position["remaining_cents"] > 0:
                trail_dist = sl_dist * settings.trail_multiplier
                if is_buy:
                    new_trail = bar["high"] - trail_dist
                    if new_trail > position["trail_level"]:
                        position["trail_level"] = max(position["entry"], new_trail)
                else:
                    new_trail = bar["low"] + trail_dist
                    if new_trail < position["trail_level"]:
                        position["trail_level"] = min(position["entry"], new_trail)

            # Check trailing stop — skip activation bar
            if position.get("trailing_activated") and position["remaining_cents"] > 0 and \
               i != position.get("trail_activation_bar") and \
               ((is_buy and bar["low"] <= position["trail_level"]) or (not is_buy and bar["high"] >= position["trail_level"])):
                lots = position["remaining_cents"] / 100.0
                book(lots, position["trail_level"], "trail")
                position["remaining_cents"] = 0

            # SL/BE check — skip the bar that triggered TP1
            if position["remaining_cents"] > 0 and \
               i != position.get("tp_hit_bar") and \
               ((is_buy and bar["low"] <= position["sl"]) or (not is_buy and bar["high"] >= position["sl"])):
                lots = position["remaining_cents"] / 100.0
                book(lots, position["sl"], "be" if position.get("tp1_hit") else "sl")
                position["remaining_cents"] = 0

            if position["remaining_cents"] <= 0 and not position.get("closed"):
                position["closed"] = True
                pnl = position["pnl"]
                result.total_trades += 1
                if pnl > 0:
                    result.winning_trades += 1
                    result.avg_win += pnl
                    result.largest_win = max(result.largest_win, pnl)
                else:
                    result.losing_trades += 1
                    result.avg_loss += pnl
                    result.largest_loss = min(result.largest_loss, pnl)
                risk_mgr.record_trade(pnl)
                position = None

        elif trades_today < max_trades_per_day:
            spread_pips = df["spread"].iloc[i]
            if spread_pips > settings.max_spread:
                result.spread_filtered += 1
                continue

            allowed, cb_reason = risk_mgr.check_entry_allowed(balance)
            if not allowed:
                result.cb_blocked += 1
                continue

            if args.session_filter and not SessionTimes().is_trade_window(current_time):
                continue

            if news_filter is not None:
                in_blackout, _ = news_filter.is_blackout(current_time)
                if in_blackout:
                    result.news_filtered += 1
                    continue

            bar = df.iloc[i]
            price = bar["close"]

            # 1. Zone: find closest unbreached zone in the correct direction
            best_dist = float("inf")
            direction = None
            zone_sl = None
            for z in zone_detector.zones:
                if z.breached:
                    continue
                if z.zone_type == "demand" and z.zone_high < price:
                    d = abs(price - (z.zone_high + z.zone_low) / 2.0)
                    if d < best_dist:
                        best_dist = d
                        direction = "buy"
                        zone_sl = z.zone_low - args.zone_buffer
                elif z.zone_type == "supply" and z.zone_low > price:
                    d = abs(price - (z.zone_high + z.zone_low) / 2.0)
                    if d < best_dist:
                        best_dist = d
                        direction = "sell"
                        zone_sl = z.zone_high + args.zone_buffer

            if direction is None:
                result.zone_filtered += 1
                continue

            # 2. Trend filter: EMA20 slope must align with direction
            if not args.no_trend_filter and not check_trend(df_15min, m15_idx, direction):
                result.trend_filtered += 1
                continue

            # 3. Momentum: confirm M1 direction supports entry
            m1_window = df.iloc[max(0, i - 6):i + 1]
            if not check_m1_alignment(m1_window, direction) or not check_momentum(m1_window, direction):
                result.mom_filtered += 1
                continue

            if (i - last_entry_bar) < 3:
                continue
            entry_price = price
            if direction == "buy":
                entry_price += random.uniform(0.01, 0.02)
            else:
                entry_price -= random.uniform(0.01, 0.02)

            if args.sl_mode == "atr":
                atr_val = bar["atr"]
                sl_dist = max(atr_val * 1.5, sl_price) if pd.notna(atr_val) else sl_price
            elif zone_sl is not None:
                raw_sl_dist = abs(zone_sl - entry_price)
                if raw_sl_dist > 0.80:
                    sl_dist = sl_price
                elif args.sl_mode == "min_sl":
                    sl_dist = max(raw_sl_dist, 0.20)
                else:
                    sl_dist = raw_sl_dist
            else:
                sl_dist = sl_price

            lot_size = calc_lot_size(balance, risk_pct, sl_dist, margin_rate, profit=balance - args.balance, max_lots=args.max_lots)
            if lot_size < 0.01:
                continue

            last_entry_bar = i
            trades_today += 1
            cents = round(lot_size * 100)
            sl_level = entry_price - sl_dist if direction == "buy" else entry_price + sl_dist
            tp1_c = int(cents * 0.5)
            tp2_c = cents - tp1_c

            position = {
                "type": direction,
                "entry": entry_price,
                "sl": sl_level,
                "remaining_cents": cents,
                "tp1_cents": tp1_c,
                "pnl": 0.0,
                "entry_bar": i,
                "entry_time": current_time,
                "tp1_hit": False,
                "closed": False,
                "sl_dist": sl_dist,
                "trailing_activated": False,
                "trail_level": 0.0,
                "trail_activation_bar": 0,
                "tp_hit_bar": 0,
            }

        if balance > peak_balance:
            peak_balance = balance

        current_dd = peak_balance - balance
        current_dd_pct = (current_dd / peak_balance * 100) if peak_balance > 0 else 0
        if current_dd_pct > result.max_drawdown_pct:
            result.max_drawdown_pct = current_dd_pct
            result.max_drawdown = current_dd

        result.equity_curve.append(balance)
        result.equity_timestamps.append(current_time)

    result.total_profit = round(balance - args.balance, 2)
    result.final_balance = round(balance, 2)
    result.return_pct = round((result.total_profit / args.balance) * 100, 2)

    if result.total_trades > 0:
        result.win_rate = round((result.winning_trades / result.total_trades) * 100, 2)
        if result.winning_trades > 0:
            result.avg_win = round(result.avg_win / result.winning_trades, 2)
        if result.losing_trades > 0:
            result.avg_loss = round(result.avg_loss / result.losing_trades, 2)

        gross_profit = sum(t["profit"] for t in result.trades if t["profit"] > 0)
        gross_loss = abs(sum(t["profit"] for t in result.trades if t["profit"] < 0))
        result.profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")
        result.avg_bars_held = round(np.mean([t["bars_held"] for t in result.trades]), 1)

    print_results(result)

    output = {
        "total_trades": result.total_trades,
        "winning_trades": result.winning_trades,
        "losing_trades": result.losing_trades,
        "win_rate": result.win_rate,
        "total_profit": result.total_profit,
        "final_balance": result.final_balance,
        "return_pct": result.return_pct,
        "profit_factor": result.profit_factor,
        "max_drawdown": result.max_drawdown,
        "max_drawdown_pct": result.max_drawdown_pct,
        "avg_win": result.avg_win,
        "avg_loss": result.avg_loss,
        "largest_win": result.largest_win,
        "largest_loss": result.largest_loss,
        "avg_bars_held": result.avg_bars_held,
        "total_commission": result.total_commission,
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
    logger.info(f"Results saved to {args.output}")


if __name__ == "__main__":
    main()
