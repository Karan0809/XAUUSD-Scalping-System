#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import pandas as pd
import numpy as np

from config.settings import get_settings
from log_utils.logger_setup import setup_logging
from core.mindspace import MindspaceEngine, Candle
from connectors.mt5_connector import MT5Connector, MT5ConnectorError

logger = logging.getLogger(__name__)

SPREAD_COST = 0.20
SLIPPAGE = 0.02


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
    signal_found: int = 0
    signal_skipped: int = 0


def parse_args():
    parser = argparse.ArgumentParser(description="Trader Mindspace Backtest")
    parser.add_argument("--start", type=str, default="2025-09-01")
    parser.add_argument("--end", type=str, default="2026-06-10")
    parser.add_argument("--balance", type=float, default=1000.0)
    parser.add_argument("--risk", type=float, default=2.0)
    parser.add_argument("--output", type=str, default="mindspace_results.json")
    return parser.parse_args()


def _load_tf(tf_name: str, mt5_tf: int, start: datetime, end: datetime) -> pd.DataFrame:
    connector = MT5Connector()
    try:
        connector.connect()
        import MetaTrader5 as mt5
        all_chunks = []
        current_end = end
        while current_end > start:
            chunk = mt5.copy_rates_from("XAUUSD", mt5_tf, current_end, 50000)
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
            raise MT5ConnectorError(f"No {tf_name} data")
        df = pd.concat(all_chunks).sort_index()
        df = df[~df.index.duplicated(keep="last")]
        df = df[(df.index >= start) & (df.index <= end)]
        logger.info(f"Loaded {len(df)} {tf_name} bars")
        return df[["open", "high", "low", "close", "tick_volume"]]
    except Exception as e:
        logger.error(f"Failed to load {tf_name} data: {e}")
        sys.exit(1)


def row_to_candle(idx: datetime, row) -> Candle:
    return Candle(
        time=idx.to_pydatetime(),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=int(row.get("tick_volume", 0)),
    )


def make_position(signal, entry, sl, tp, lot_size, spread_cost, entry_bar, entry_time, date_str, result):
    return {
        "type": signal.direction,
        "entry": entry,
        "sl": sl,
        "original_sl": sl,
        "tp": tp,
        "remaining_lots": lot_size,
        "pnl": round(-spread_cost, 2),
        "entry_bar": entry_bar,
        "entry_time": entry_time,
        "closed": False,
        "level_type": signal.level_type,
        "tf": signal.tf,
        "trailing_activated": False,
        "trail_level": sl,
    }


def compute_entry(signal, balance, settings, result):
    entry = signal.entry_price
    if signal.direction == "buy":
        sl_distance = max(entry - signal.sl_low, 0.30)
        sl = entry - sl_distance
    else:
        sl_distance = max(signal.sl_high - entry, 0.30)
        sl = entry + sl_distance

    risk_per_trade = balance * (settings.risk_percent / 100.0)
    max_sl_distance = risk_per_trade / (0.01 * 100)
    if sl_distance > max_sl_distance:
        result.signal_skipped += 1
        return None

    lot_size = max(0.01, min(round(risk_per_trade / (sl_distance * 100), 2), 0.5))
    if lot_size < 0.01:
        result.signal_skipped += 1
        return None

    tp_raw = signal.tp_price
    if signal.direction == "buy":
        tp_capped = entry + sl_distance * 3
        tp = min(tp_raw, tp_capped) if tp_raw else tp_capped
    else:
        tp_capped = entry - sl_distance * 3
        tp = max(tp_raw, tp_capped) if tp_raw else tp_capped

    return {"entry": entry, "sl": sl, "tp": tp, "lot_size": lot_size, "sl_distance": sl_distance}


def manage_position(position, bar, bar_i, current_time, date_str, engine, bal, result):
    is_buy = position["type"] == "buy"
    sl = position["sl"]
    tp = position.get("tp")
    local_balance = bal

    def book(lots, price, reason):
        nonlocal local_balance
        slip = SLIPPAGE * (-1 if is_buy else 1)
        fill_price = price + slip
        spread_cost = SPREAD_COST * lots
        pdiff = fill_price - position["entry"]
        if not is_buy:
            pdiff = -pdiff
        raw = pdiff * lots * 100
        comm = get_settings().backtest_commission * lots
        net = raw - comm - spread_cost
        local_balance += net
        result.total_commission += comm
        position["pnl"] = round(position["pnl"] + net, 2)
        result.trades.append({
            "type": position["type"], "entry": position["entry"],
            "exit": fill_price, "profit": round(net, 2),
            "commission": round(comm, 2), "spread_cost": round(spread_cost, 2),
            "lot_size": lots,
            "bars_held": bar_i - position["entry_bar"],
            "exit_reason": reason,
            "entry_time": position["entry_time"].isoformat(),
            "exit_time": current_time.isoformat(),
            "date": date_str,
            "level_type": position.get("level_type", ""),
            "tf": position.get("tf", ""),
        })

    mgmt = engine.manage_position(
        entry_price=position["entry"],
        direction=position["type"],
        current_price=bar["close"],
        sl_price=sl,
        tp_price=tp if tp else 0,
        volume=position["remaining_lots"],
        position_id=hash(position["entry_time"]),
    )

    action = mgmt["action"]

    if action == "close":
        book(position["remaining_lots"], tp if tp else bar["close"], "tp")
        position["remaining_lots"] = 0

    elif action == "partial_close":
        close_lots = position["remaining_lots"] * mgmt["close_pct"]
        tp1_price = position["entry"] + abs(position["entry"] - sl) if is_buy else position["entry"] - abs(position["entry"] - sl)

        if (is_buy and bar["high"] >= tp1_price) or (not is_buy and bar["low"] <= tp1_price):
            book(close_lots, tp1_price, "tp1")
            position["remaining_lots"] -= close_lots
            position["sl"] = position["entry"]
            position["trailing_activated"] = True

    if action == "trail" and mgmt.get("new_sl") is not None:
        position["sl"] = mgmt["new_sl"]

    if action == "trail" or position.get("trailing_activated"):
        sl_dist = abs(position["entry"] - position.get("original_sl", sl))
        trail_buffer = sl_dist * 0.3
        if is_buy:
            new_trail = bar["high"] - trail_buffer
            if new_trail > position.get("trail_level", 0):
                position["trail_level"] = max(position["entry"], new_trail)
        else:
            new_trail = bar["low"] + trail_buffer
            if new_trail < position.get("trail_level", 999):
                position["trail_level"] = min(position["entry"], new_trail)

        if position.get("trailing_activated") and position["remaining_lots"] > 0:
            if (is_buy and bar["low"] <= position["trail_level"]) or \
               (not is_buy and bar["high"] >= position["trail_level"]):
                book(position["remaining_lots"], position["trail_level"], "trail")
                position["remaining_lots"] = 0

    if position["remaining_lots"] > 0:
        sl_check = position["sl"]
        if (is_buy and bar["low"] <= sl_check) or (not is_buy and bar["high"] >= sl_check):
            book(position["remaining_lots"], sl_check, "be" if position.get("trailing_activated") else "sl")
            position["remaining_lots"] = 0

    if position["remaining_lots"] <= 0 and not position.get("closed"):
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
        return (None, local_balance)

    return (position, local_balance)


def main():
    args = parse_args()
    setup_logging()
    logging.getLogger().setLevel(logging.WARNING)

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    settings = get_settings()
    settings.risk_percent = args.risk

    import MetaTrader5 as mt5

    print("Loading multi-timeframe data from MT5...")
    df_1h = _load_tf("1h", mt5.TIMEFRAME_H1, start - timedelta(days=30), end)
    df_4h = _load_tf("4h", mt5.TIMEFRAME_H4, start - timedelta(days=60), end)
    df_d1 = _load_tf("daily", mt5.TIMEFRAME_D1, start - timedelta(days=180), end)
    df_15m = _load_tf("15m", mt5.TIMEFRAME_M15, start - timedelta(days=30), end)
    df_1m = _load_tf("1m", mt5.TIMEFRAME_M1, start - timedelta(days=15), end)

    htf_engine = MindspaceEngine()
    scalp_engine = MindspaceEngine()
    result = BacktestResult()
    balance = args.balance
    peak_balance = balance
    htf_pos: Optional[Dict[str, Any]] = None
    scalp_positions: list[Dict[str, Any]] = []
    current_date = None

    warmup_bars = 720
    htf_consumed: set = set()
    scalp_consumed: set = set()

    acc_1h: list[Candle] = []
    acc_4h: list[Candle] = []
    acc_d1: list[Candle] = []

    scalp_acc_1h: list[Candle] = []
    scalp_acc_15m: list[Candle] = []
    scalp_acc_1m: list[Candle] = []
    scalp_bar_counter = 0

    print(f"Running backtest: {len(df_1h)} 1h bars, {len(df_15m)} 15m bars, {len(df_1m)} 1m bars")

    for i in range(len(df_1h)):
        current_time = df_1h.index[i]
        date_str = current_time.strftime("%Y-%m-%d")

        if date_str != current_date:
            current_date = date_str
            htf_consumed.clear()
            scalp_consumed.clear()

        h1_c = row_to_candle(current_time, df_1h.iloc[i])
        h1_c_t = h1_c.time
        acc_1h.append(h1_c)
        scalp_acc_1h.append(h1_c)

        h4_idx = df_4h.index.searchsorted(current_time, side="right") - 1
        if h4_idx >= 0:
            row = df_4h.iloc[h4_idx]
            h4_c = row_to_candle(df_4h.index[h4_idx], row)
            if not acc_4h or acc_4h[-1].time != h4_c.time:
                acc_4h.append(h4_c)

        d1_idx = df_d1.index.searchsorted(current_time, side="right") - 1
        if d1_idx >= 0:
            row = df_d1.iloc[d1_idx]
            d1_c = row_to_candle(df_d1.index[d1_idx], row)
            if not acc_d1 or acc_d1[-1].time != d1_c.time:
                acc_d1.append(d1_c)

        htf_engine.update_markets({
            "1h": acc_1h,
            "4h": acc_4h,
            "daily": acc_d1,
        })

        if i < warmup_bars:
            continue
        if current_time < start:
            continue

        bar_1h = df_1h.iloc[i]

        # --- HTF position management ---
        if htf_pos:
            htf_pos, balance = manage_position(htf_pos, bar_1h, i, current_time, date_str, htf_engine, balance, result)

        # --- HTF signals ---
        if htf_pos is None:
            signal = htf_engine.get_signal()
            if signal is not None:
                sig_key = f"{signal.level_type}_{signal.tf}_{signal.entry_price:.2f}_{signal.direction}"
                if sig_key not in htf_consumed:
                    htf_consumed.add(sig_key)
                    comp = compute_entry(signal, balance, settings, result)
                    if comp is not None:
                        result.signal_found += 1
                        spread_cost = SPREAD_COST * comp["lot_size"]
                        htf_pos = make_position(signal, comp["entry"], comp["sl"], comp["tp"],
                                                comp["lot_size"], spread_cost, i, current_time, date_str, result)
                        balance -= spread_cost
                        result.total_commission += spread_cost

        # --- SCALP within this 1h bar ---
        h1_start = current_time
        h1_end = current_time + timedelta(hours=1)
        m15_in_h1 = df_15m[(df_15m.index >= h1_start) & (df_15m.index < h1_end)]

        for m15_time, m15_row in m15_in_h1.iterrows():
            m15_c = row_to_candle(m15_time, m15_row)
            m15_datetime = m15_c.time
            scalp_acc_15m.append(m15_c)
            scalp_bar_counter += 1

            m1_in_m15 = df_1m[(df_1m.index >= m15_time) & (df_1m.index < m15_time + timedelta(minutes=15))]
            for m1_time, m1_row in m1_in_m15.iterrows():
                m1_c = row_to_candle(m1_time, m1_row)
                scalp_acc_1m.append(m1_c)

            s_candles = {"1h": scalp_acc_1h, "15m": scalp_acc_15m, "1m": scalp_acc_1m}
            scalp_engine.update_markets(s_candles, mtf_hierarchy=["1h", "15m", "1m"])

            bar_15m = m15_row

            # Manage existing scalp positions
            new_scalp = []
            for pos in scalp_positions:
                if pos["remaining_lots"] <= 0 and pos.get("closed"):
                    continue
                managed, balance = manage_position(pos, bar_15m, scalp_bar_counter, m15_time, date_str, scalp_engine, balance, result)
                if managed is not None:
                    new_scalp.append(managed)
            scalp_positions = new_scalp

            # Scalp signals
            sig = scalp_engine.get_signal()
            if sig is not None:
                s_key = f"{sig.level_type}_{sig.tf}_{sig.entry_price:.2f}_{sig.direction}"
                if s_key not in scalp_consumed:
                    scalp_consumed.add(s_key)
                    comp = compute_entry(sig, balance, settings, result)
                    if comp is not None:
                        result.signal_found += 1
                        spread_cost = SPREAD_COST * comp["lot_size"]
                        pos = make_position(sig, comp["entry"], comp["sl"], comp["tp"],
                                            comp["lot_size"], spread_cost, scalp_bar_counter, m15_time, date_str, result)
                        scalp_positions.append(pos)
                        balance -= spread_cost
                        result.total_commission += spread_cost

        # --- equity curve ---
        if balance > peak_balance:
            peak_balance = balance
        current_dd = peak_balance - balance
        current_dd_pct = (current_dd / peak_balance * 100) if peak_balance > 0 else 0
        if current_dd_pct > result.max_drawdown_pct:
            result.max_drawdown_pct = current_dd_pct
            result.max_drawdown = current_dd
        result.equity_curve.append(balance)
        result.equity_timestamps.append(current_time)

    # --- Final calculation ---
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

    print("\n" + "=" * 60)
    print("  TRADER MINDSPACE (HTF + Scalp)")
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
    print(f"  Signals:           Found={result.signal_found} Skipped={result.signal_skipped}")
    print("=" * 60 + "\n")

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
        "signal_found": result.signal_found,
        "signal_skipped": result.signal_skipped,
        "trades": result.trades,
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
    logger.info(f"Results saved to {args.output}")


if __name__ == "__main__":
    main()
