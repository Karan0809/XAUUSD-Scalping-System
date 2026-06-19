#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

import random

import pandas as pd
import numpy as np

from config.settings import get_settings, ScalperSettings
from log_utils.logger_setup import setup_logging
from core.opening_range_scalp import OpeningRangeScalp
from core.institutional_zone import InstitutionalZoneDetector
from core.risk_manager import RiskManager
from core.news_filter import NewsFilter
from connectors.mt5_connector import MT5Connector, MT5ConnectorError

logger = logging.getLogger(__name__)

_MARGIN_RATE: Optional[float] = None


@dataclass
class SubResult:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_profit: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    trades: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return round((self.winning_trades / self.total_trades * 100), 2) if self.total_trades > 0 else 0.0

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t["pnl"] for t in self.trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in self.trades if t["pnl"] < 0))
        return round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")

    def record(self, trade: dict):
        pnl = trade["pnl"]
        self.total_trades += 1
        self.trades.append(trade)
        if pnl > 0:
            self.winning_trades += 1
            self.avg_win += pnl
            self.largest_win = max(self.largest_win, pnl)
        else:
            self.losing_trades += 1
            self.avg_loss += pnl
            self.largest_loss = min(self.largest_loss, pnl)

    def finalize(self):
        if self.winning_trades > 0:
            self.avg_win = round(self.avg_win / self.winning_trades, 2)
        if self.losing_trades > 0:
            self.avg_loss = round(self.avg_loss / self.losing_trades, 2)
        self.total_profit = round(sum(t["pnl"] for t in self.trades), 2)


@dataclass
class BacktestResult:
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
    recovery_trades: int = 0
    orb: SubResult = field(default_factory=SubResult)


def parse_args():
    parser = argparse.ArgumentParser(description="ORB Scalper Backtest")
    parser.add_argument("--start", type=str, default="2025-09-01")
    parser.add_argument("--end", type=str, default="2026-06-03")
    parser.add_argument("--balance", type=float, default=1000.0)
    parser.add_argument("--risk", type=float, default=2.0)
    parser.add_argument("--output", type=str, default="scalper_results.json")
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


def load_data(start: datetime, end: datetime) -> pd.DataFrame:
    connector = MT5Connector()
    try:
        connector.connect()
        import MetaTrader5 as mt5

        all_chunks = []
        current_end = end
        while current_end > start:
            chunk = mt5.copy_rates_from("XAUUSD", mt5.TIMEFRAME_M5, current_end, 50000)
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
            raise MT5ConnectorError("No M5 data")
        df = pd.concat(all_chunks).sort_index()
        df = df[~df.index.duplicated(keep="last")]
        df = df[(df.index >= start) & (df.index <= end)]
        logger.info(f"Loaded {len(df)} M5 bars")
        return df[["open", "high", "low", "close", "tick_volume", "spread"]]
    except Exception as e:
        logger.error(f"Failed to load M5 data: {e}")
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


SESSION_RANGES: dict[str, tuple[int, int]] = {
    "asia": (0, 9),
    "london": (9, 12),
    "ny": (13, 16),
}


def get_session(hour: int) -> Optional[str]:
    for session, (start, end) in SESSION_RANGES.items():
        if start <= hour < end:
            return session
    return None


def get_risk_amount(profit: float) -> float:
    if profit >= 50000:
        return 50.0
    elif profit >= 10000:
        return 30.0
    elif profit >= 2000:
        return 20.0
    elif profit >= 500:
        return 15.0
    return 10.0


def refine_sl(df: pd.DataFrame, i: int, direction: str, entry: float, original_sl: float, lookback: int = 4) -> float:
    window = df.iloc[max(0, i - lookback):i + 1]
    if direction == "buy":
        swing_low = window["low"].min()
        return max(swing_low - 0.01, original_sl)
    else:
        swing_high = window["high"].max()
        return min(swing_high + 0.01, original_sl)


def calc_lot_size(balance: float, entry: float, sl: float, risk_pct: float, margin_rate: Optional[float] = None, profit: float = 0.0) -> float:
    dist = abs(entry - sl)
    if dist <= 0:
        return 0.01
    risk_amount = get_risk_amount(profit)
    risk_lots = round(risk_amount / (dist * 100), 2)
    if margin_rate is not None and margin_rate > 0:
        margin_lots = max(0.01, round((balance * 0.9) / margin_rate, 2))
    else:
        margin_lots = 0.5
    return max(0.01, round(min(risk_lots, margin_lots, 0.5), 2))


def print_sub_result(label: str, sub: SubResult):
    if sub.total_trades == 0:
        return
    sub.finalize()
    print(f"  --- {label} ---")
    print(f"    Trades:       {sub.total_trades}")
    print(f"    Win Rate:     {sub.win_rate:.2f}%")
    print(f"    Profit:       ${sub.total_profit:.2f}")
    print(f"    Profit Fact:  {sub.profit_factor:.2f}")
    print(f"    Avg Win:      ${sub.avg_win:.2f}")
    print(f"    Avg Loss:     ${sub.avg_loss:.2f}")
    print(f"    Largest Win:  ${sub.largest_win:.2f}")
    print(f"    Largest Loss: ${sub.largest_loss:.2f}")
    print()


def print_results(result: BacktestResult, label: str = ""):
    print("\n" + "=" * 60)
    print(f"  ORB SCALPER BACKTEST RESULTS {label}")
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
    print(f"  Recovery Trades:   {result.recovery_trades}")
    print(f"  Filters:           Spread={result.spread_filtered} CB={result.cb_blocked} News={result.news_filtered}")
    print("=" * 60)
    print_sub_result("ORB Trades", result.orb)


def main():
    args = parse_args()
    setup_logging()

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    settings = get_settings()
    settings.backtest_start = args.start
    settings.backtest_end = args.end
    settings.backtest_initial_balance = args.balance
    settings.risk_percent = args.risk

    df = load_data(start, end)
    df_15min = load_15min_data(start, end)
    margin_rate = _fetch_margin_rate()

    zone_detector = InstitutionalZoneDetector()
    orb = OpeningRangeScalp(
        zone_detector=zone_detector,
    )
    risk_mgr = RiskManager(
        max_daily_loss_pct=settings.circuit_breaker_max_daily_loss_pct,
        max_consecutive_losses=settings.circuit_breaker_max_consecutive_losses,
        max_drawdown_pct=settings.circuit_breaker_max_drawdown_pct,
    )
    result = BacktestResult()

    news_filter = NewsFilter(
        blackout_minutes=settings.news_blackout_minutes
    ) if settings.news_filter_enabled else None
    if news_filter is not None:
        news_filter.fetch_events()

    balance = args.balance
    peak_balance = balance
    position: Optional[Dict[str, Any]] = None
    trades_today = 0
    current_date = None
    m15_idx = 0
    last_entry_bar = -100
    recovery_available = False
    recovery_used_today = False

    for i in range(60, len(df)):
        current_time = df.index[i]
        date_str = current_time.strftime("%Y-%m-%d")

        if date_str != current_date:
            current_date = date_str
            trades_today = 0
            recovery_available = False
            recovery_used_today = False
            risk_mgr.start_day(date_str, balance)

        current_session = get_session(current_time.hour)

        while m15_idx < len(df_15min) and df_15min.index[m15_idx] <= current_time:
            zone_detector.update(df_15min.iloc[m15_idx])
            m15_idx += 1

        zone_detector.update_test_status(df["high"].iloc[i], df["low"].iloc[i])

        window_df = df.iloc[max(0, i - 200):i + 1]

        if position:
            bar = df.iloc[i]
            sl_dist = position["sl_dist"]
            entry = position["entry"]
            is_buy = position["type"] == "buy"
            tp1_level = entry + sl_dist if is_buy else entry - sl_dist
            tp2_level = entry + 2 * sl_dist if is_buy else entry - 2 * sl_dist

            def book(lots, price, reason):
                nonlocal balance
                slip = random.uniform(0.0, 0.01)
                if is_buy:
                    price -= slip
                else:
                    price += slip
                pdiff = price - entry
                if not is_buy:
                    pdiff = -pdiff
                raw = pdiff * lots * 100
                comm = settings.backtest_commission * lots
                net = raw - comm
                balance += net
                result.total_commission += comm
                position["pnl"] = round(position["pnl"] + net, 2)
                result.trades.append({
                    "type": position["type"], "entry": entry,
                    "exit": price, "profit": round(net, 2),
                    "commission": round(comm, 2), "lot_size": lots,
                    "bars_held": i - position["entry_bar"],
                    "exit_reason": reason, "partial": True,
                    "entry_time": position["entry_time"],
                    "exit_time": current_time, "date": date_str,
                })
                logger.info(f"{reason.upper()} {date_str}: {lots:.2f} lots P={net:.2f}")

            was_open = position["remaining_cents"] > 0

            # TP1: close first tranche at 1:1, move SL to BE
            if not position["tp1_hit"] and \
               ((is_buy and bar["high"] >= tp1_level) or (not is_buy and bar["low"] <= tp1_level)):
                book(position["tp1_cents"] / 100.0, tp1_level, "tp1")
                position["remaining_cents"] -= position["tp1_cents"]
                position["sl"] = entry
                position["tp1_hit"] = True
                position["tp_hit_bar"] = i
                # Activate trailing for 50-50 model after TP1
                if position["tp3_cents"] == 0 and position["tp2_cents"] > 0:
                    trail_dist = position["sl_dist"] * settings.trail_multiplier
                    if is_buy:
                        position["trail_level"] = max(position["entry"], bar["high"] - trail_dist)
                    else:
                        position["trail_level"] = min(position["entry"], bar["low"] + trail_dist)
                    position["trailing_activated"] = True
                    position["trail_activation_bar"] = i

            # TP2: close second tranche at 1:2 (3-target model only)

            if position["tp3_cents"] > 0 and position["tp1_hit"] and not position["tp2_hit"] and position["remaining_cents"] > 0 and \
               i != position.get("tp_hit_bar") and \
               ((is_buy and bar["high"] >= tp2_level) or (not is_buy and bar["low"] <= tp2_level)):
                lots = min(position["tp2_cents"], position["remaining_cents"]) / 100.0
                book(lots, tp2_level, "tp2")
                position["remaining_cents"] -= min(position["tp2_cents"], position["remaining_cents"])
                position["tp2_hit"] = True
                position["tp_hit_bar"] = i
                # Activate trailing on remaining 30%
                if position["remaining_cents"] > 0:
                    trail_dist = position["sl_dist"] * settings.trail_multiplier
                    if is_buy:
                        position["trail_level"] = max(position["entry"], bar["high"] - trail_dist)
                    else:
                        position["trail_level"] = min(position["entry"], bar["low"] + trail_dist)
                    position["trailing_activated"] = True
                    position["trail_activation_bar"] = i
            # Update trailing stop
            if position.get("trailing_activated") and position["remaining_cents"] > 0:
                trail_dist = position["sl_dist"] * settings.trail_multiplier
                if is_buy:
                    new_trail = bar["high"] - trail_dist
                    if new_trail > position["trail_level"]:
                        position["trail_level"] = max(position["entry"], new_trail)
                else:
                    new_trail = bar["low"] + trail_dist
                    if new_trail < position["trail_level"]:
                        position["trail_level"] = min(position["entry"], new_trail)

            # Check trailing stop (replaces fixed TP3) — skip activation bar
            if position.get("trailing_activated") and position["remaining_cents"] > 0 and \
               i != position.get("trail_activation_bar") and \
               ((is_buy and bar["low"] <= position["trail_level"]) or (not is_buy and bar["high"] >= position["trail_level"])):
                lots = position["remaining_cents"] / 100.0
                book(lots, position["trail_level"], "trail")
                position["remaining_cents"] = 0

            # SL/be check on remaining position — skip the bar that triggered TP1/TP2
            if position["remaining_cents"] > 0 and \
               i != position.get("tp_hit_bar") and \
               ((is_buy and bar["low"] <= position["sl"]) or (not is_buy and bar["high"] >= position["sl"])):
                lots = position["remaining_cents"] / 100.0
                book(lots, position["sl"],
                     "be" if position["tp1_hit"] else "sl")
                position["remaining_cents"] = 0

            if was_open and position["remaining_cents"] <= 0:
                pnl = position["pnl"]
                setup = position.get("setup", "unknown")
                trade_data = {
                    "type": position["type"], "entry": position["entry"],
                    "pnl": round(pnl, 2), "setup": setup,
                }
                result.total_trades += 1
                if pnl > 0:
                    result.winning_trades += 1
                    result.avg_win += pnl
                    result.largest_win = max(result.largest_win, pnl)
                else:
                    result.losing_trades += 1
                    result.avg_loss += pnl
                    result.largest_loss = min(result.largest_loss, pnl)
                    recovery_available = True
                result.orb.record(trade_data)
                risk_mgr.record_trade(pnl)
                orb.reset_entry()
                position = None

        elif trades_today < settings.max_daily_trades:
            # Spread check
            spread_pips = df["spread"].iloc[i]
            if spread_pips > settings.max_spread:
                result.spread_filtered += 1
                continue

            # Circuit breaker
            allowed, cb_reason = risk_mgr.check_entry_allowed(balance)
            if not allowed:
                result.cb_blocked += 1
                continue

            # News blackout
            if news_filter is not None:
                in_blackout, _ = news_filter.is_blackout(current_time)
                if in_blackout:
                    result.news_filtered += 1
                    continue

            df_15min_window = df_15min[df_15min.index <= current_time]
            signal = orb.analyze(window_df, df_15min_window, current_time, session=current_session)
            if signal is not None and (i - last_entry_bar) >= 3:
                entry_price = signal["entry"]
                if signal["direction"] == "buy":
                    entry_price += random.uniform(0.01, 0.02)
                else:
                    entry_price -= random.uniform(0.01, 0.02)
                sl = signal["sl"]
                is_recovery = recovery_available and not recovery_used_today
                if is_recovery:
                    refined = refine_sl(df, i, signal["direction"], entry_price, sl)
                    if refined != sl:
                        sl = refined
                        recovery_used_today = True
                        result.recovery_trades += 1
                lot_size = calc_lot_size(balance, entry_price, sl, settings.risk_percent, margin_rate, profit=balance - args.balance)

                if lot_size >= 0.01:
                    last_entry_bar = i
                    trades_today += 1
                    sl_dist = abs(entry_price - sl)
                    cents = round(lot_size * 100)
                    if cents >= 10:
                        tp1_c = int(cents * 0.3)
                        tp2_c = int(cents * 0.4)
                        tp3_c = cents - tp1_c - tp2_c
                    elif cents >= 4:
                        tp1_c = int(cents * 0.5)
                        tp2_c = cents - tp1_c
                        tp3_c = 0
                    else:
                        tp1_c = cents
                        tp2_c = 0
                        tp3_c = 0
                    position = {
                        "type": signal["direction"],
                        "entry": entry_price,
                        "sl": sl,
                        "setup": signal.get("setup", ""),
                        "remaining_cents": cents,
                        "tp1_cents": tp1_c,
                        "tp2_cents": tp2_c,
                        "tp3_cents": tp3_c,
                        "pnl": 0.0,
                        "entry_bar": i,
                        "entry_time": current_time,
                        "original_sl": sl,
                        "tp1_hit": False,
                        "tp2_hit": False,
                        "tp3_hit": False,
                        "sl_dist": sl_dist,
                        "trailing_activated": False,
                    }
                    logger.info(
                        f"ORB [{date_str}] {signal['direction']} @ {entry_price:.2f} "
                        f"SL={sl:.2f} lot={lot_size:.2f} session={current_session} "
                        f"({signal.get('setup', '')})"
                    )

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
