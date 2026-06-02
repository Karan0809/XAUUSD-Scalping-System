#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import pandas as pd
import numpy as np

from config.settings import get_settings, ScalperSettings
from log_utils.logger_setup import setup_logging
from core.opening_range_scalp import OpeningRangeScalp
from core.institutional_zone import InstitutionalZoneDetector
from connectors.mt5_connector import MT5Connector, MT5ConnectorError

logger = logging.getLogger(__name__)


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


def parse_args():
    parser = argparse.ArgumentParser(description="ORB Scalper Backtest")
    parser.add_argument("--start", type=str, default="2025-09-01")
    parser.add_argument("--end", type=str, default="2026-05-30")
    parser.add_argument("--balance", type=float, default=1000.0)
    parser.add_argument("--risk", type=float, default=1.0)
    parser.add_argument("--output", type=str, default="scalper_results.json")
    return parser.parse_args()


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
            chunk_df["time"] = pd.to_datetime(chunk_df["time"], unit="s")
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
        return df[["open", "high", "low", "close", "tick_volume"]]
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
            chunk_df["time"] = pd.to_datetime(chunk_df["time"], unit="s")
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


def calc_lot_size(balance: float, entry: float, sl: float, risk_pct: float) -> float:
    dist = abs(entry - sl)
    if dist <= 0:
        return 0.01
    risk_amount = balance * (risk_pct / 100.0)
    return max(0.01, min(round(risk_amount / (dist * 100), 2), 10.0))


def print_results(result: BacktestResult):
    print("\n" + "=" * 60)
    print("  ORB SCALPER BACKTEST RESULTS")
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
    print("=" * 60 + "\n")


def main():
    args = parse_args()
    setup_logging()

    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")

    settings = get_settings()
    settings.backtest_start = args.start
    settings.backtest_end = args.end
    settings.backtest_initial_balance = args.balance
    settings.risk_percent = args.risk

    df = load_data(start, end)
    df_15min = load_15min_data(start, end)

    zone_detector = InstitutionalZoneDetector()
    orb = OpeningRangeScalp(zone_detector=zone_detector)
    result = BacktestResult()
    balance = args.balance
    peak_balance = balance
    position: Optional[Dict[str, Any]] = None
    trades_today = 0
    current_date = None
    last_session: Optional[str] = None
    m15_idx = 0
    pending_exit: Optional[Dict[str, Any]] = None

    for i in range(60, len(df)):
        current_time = df.index[i]
        date_str = current_time.strftime("%Y-%m-%d")

        if date_str != current_date:
            current_date = date_str
            trades_today = 0

        current_session = get_session(current_time.hour)
        if current_session is not None and current_session != last_session:
            orb.reset()
            last_session = current_session

        while m15_idx < len(df_15min) and df_15min.index[m15_idx] <= current_time:
            zone_detector.update(df_15min.iloc[m15_idx])
            m15_idx += 1

        zone_detector.update_test_status(df["high"].iloc[i], df["low"].iloc[i])

        window_df = df.iloc[max(0, i - 200):i + 1]

        if position:
            bar = df.iloc[i]
            sl_dist = abs(position["entry"] - position["original_sl"])
            tp1_level = position["entry"] + sl_dist if position["type"] == "buy" else position["entry"] - sl_dist
            tp2_level = position["entry"] + 2 * sl_dist if position["type"] == "buy" else position["entry"] - 2 * sl_dist

            exit_triggered = False
            exit_price = None
            exit_reason = None

            if not position.get("tp1_hit", False):
                if (position["type"] == "buy" and bar["high"] >= tp1_level) or \
                   (position["type"] == "sell" and bar["low"] <= tp1_level):
                    position["sl"] = position["entry"]
                    position["tp1_hit"] = True
                    logger.info(f"TP1 {date_str}: SL moved to entry (BE)")

            if not exit_triggered and position.get("tp1_hit", False):
                if (position["type"] == "buy" and bar["high"] >= tp2_level) or \
                   (position["type"] == "sell" and bar["low"] <= tp2_level):
                    exit_price = position["entry"] + 2 * sl_dist if position["type"] == "buy" else position["entry"] - 2 * sl_dist
                    exit_reason = "tp2"
                    exit_triggered = True

            if not exit_triggered:
                if position["type"] == "buy" and bar["low"] <= position["sl"]:
                    exit_price = position["sl"]
                    exit_reason = "be" if position.get("tp1_hit", False) else "sl"
                    exit_triggered = True
                elif position["type"] == "sell" and bar["high"] >= position["sl"]:
                    exit_price = position["sl"]
                    exit_reason = "be" if position.get("tp1_hit", False) else "sl"
                    exit_triggered = True

            if exit_triggered:
                price_diff = exit_price - position["entry"]
                if position["type"] == "sell":
                    price_diff = -price_diff

                raw_profit = price_diff * position["lot_size"] * 100
                commission = settings.backtest_commission * position["lot_size"]
                profit = raw_profit - commission

                balance += profit

                trade_record = {
                    "type": position["type"],
                    "entry": position["entry"],
                    "exit": exit_price,
                    "profit": round(profit, 2),
                    "commission": round(commission, 2),
                    "lot_size": position["original_lot_size"],
                    "bars_held": i - position["entry_bar"],
                    "exit_reason": exit_reason,
                    "entry_time": position["entry_time"],
                    "exit_time": current_time,
                    "date": date_str,
                    "tp1_hit": position.get("tp1_hit", False),
                    "sl_dist": round(sl_dist, 2),
                }
                result.trades.append(trade_record)
                result.total_trades += 1
                result.total_commission += commission

                if profit > 0:
                    result.winning_trades += 1
                    result.avg_win += profit
                    result.largest_win = max(result.largest_win, profit)
                else:
                    result.losing_trades += 1
                    result.avg_loss += profit
                    result.largest_loss = min(result.largest_loss, profit)

                logger.info(f"CLOSE {date_str} {position['type']} @ {exit_price:.2f} P={profit:.2f} ({exit_reason})")
                position = None

        elif trades_today < settings.max_daily_trades:
            df_15min_window = df_15min[df_15min.index <= current_time]
            signal = orb.analyze(window_df, df_15min_window, current_time, session=current_session)
            if signal is not None:
                entry_price = signal["entry"]
                sl = signal["sl"]
                lot_size = calc_lot_size(balance, entry_price, sl, settings.risk_percent)

                if lot_size >= 0.01:
                    trades_today += 1
                    sl_dist = abs(entry_price - sl)
                    position = {
                        "type": signal["direction"],
                        "entry": entry_price,
                        "sl": sl,
                        "lot_size": lot_size,
                        "original_lot_size": lot_size,
                        "entry_bar": i,
                        "entry_time": current_time,
                        "original_sl": sl,
                        "tp1_hit": False,
                        "sl_dist": sl_dist,
                    }
                    logger.info(f"ORB [{date_str}] {signal['direction']} @ {entry_price:.2f} SL={sl:.2f} lot={lot_size:.2f} session={current_session} ({signal.get('setup', '')})")

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
