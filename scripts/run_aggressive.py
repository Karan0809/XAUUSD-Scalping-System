#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
import time
from uuid import uuid4
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

import pandas as pd
import MetaTrader5 as mt5

from config.settings import get_settings
from config.sessions import SessionValidator
from log_utils.logger_setup import setup_logging, get_logger
from core.institutional_zone import InstitutionalZoneDetector
from core.risk_manager import RiskManager
from core.news_filter import NewsFilter
from connectors.mt5_connector import MT5Connector, MT5ConnectorError
from database.mongo_client import MongoClient
from telegram.alerts import TelegramNotifier, fmt_et

logger = logging.getLogger(__name__)
trade_logger = get_logger("trade")

SL_PIPS = 20
SL_PRICE = SL_PIPS / 100.0
RISK_PCT = 1.2
MAX_TRADES_PER_DAY = 20


class AggressiveBot:
    POLL_INTERVAL_SECONDS = 30
    M15_REFRESH_SECONDS = 300
    HEARTBEAT_SECONDS = 21600

    def __init__(self):
        self.settings = get_settings()
        self.connector = MT5Connector()
        self.zone_detector = InstitutionalZoneDetector()
        self.risk_mgr = RiskManager(
            max_daily_loss_pct=self.settings.circuit_breaker_max_daily_loss_pct,
            max_consecutive_losses=self.settings.circuit_breaker_max_consecutive_losses,
            max_drawdown_pct=self.settings.circuit_breaker_max_drawdown_pct,
        )
        self.news_filter = NewsFilter(
            blackout_minutes=self.settings.news_blackout_minutes
        ) if self.settings.news_filter_enabled else None
        self.telegram = TelegramNotifier()
        self.mongo = MongoClient()
        self._running = False
        self._current_date: Optional[str] = None
        self._trades_today = 0
        self._position: Optional[Dict[str, Any]] = None
        self._df_15min: Optional[pd.DataFrame] = None
        self._m15_last_refresh: float = 0
        self._last_heartbeat: float = 0
        self._start_time: datetime = datetime.now(timezone.utc)
        self._last_bar_idx: Optional[int] = None

    def _load_15min_data(self) -> None:
        try:
            self.connector.connect()
            all_chunks = []
            current_end = datetime.now(timezone.utc) + timedelta(hours=1)
            while current_end > datetime.now(timezone.utc) - timedelta(days=90):
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
            if not all_chunks:
                logger.error("No M15 data loaded")
                return
            df = pd.concat(all_chunks).sort_index()
            df = df[~df.index.duplicated(keep="last")][["open", "high", "low", "close", "tick_volume"]]
            self._df_15min = df
            self.zone_detector.build_historical(df)
            logger.info(f"M15 data refreshed: {len(df)} bars, {len(self.zone_detector.zones)} zones built")
        except Exception as e:
            logger.warning(f"M15 load failed: {e}", exc_info=True)

    def _check_new_day(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._current_date != today:
            self._current_date = today
            self._trades_today = 0
            acct = self.connector.get_account_info()
            self.risk_mgr.start_day(today, acct["balance"])
            logger.info(f"New trading day: {today}")

    def _calc_lot_size(self, balance: float) -> float:
        risk_amount = balance * (RISK_PCT / 100.0)
        return max(0.01, min(round(risk_amount / (SL_PRICE * 100), 2), 10.0))

    def _is_within_zone(self, price: float):
        best_dist = float("inf")
        direction = None
        for z in self.zone_detector.zones:
            if z.breached:
                continue
            if z.zone_type == "demand" and z.zone_high < price:
                d = abs(price - (z.zone_high + z.zone_low) / 2.0)
                if d < best_dist:
                    best_dist = d
                    direction = "buy"
            elif z.zone_type == "supply" and z.zone_low > price:
                d = abs(price - (z.zone_high + z.zone_low) / 2.0)
                if d < best_dist:
                    best_dist = d
                    direction = "sell"
        return direction

    def _check_momentum(self, bar: Dict[str, float], prev_close: float, direction: str) -> bool:
        if direction == "buy":
            return bar["close"] > bar["open"] and bar["close"] > prev_close
        else:
            return bar["close"] < bar["open"] and bar["close"] < prev_close

    def _close_trade(self, price: float, reason: str, current_time: datetime) -> None:
        pos = self._position
        is_buy = pos["type"] == "buy"
        pdiff = price - pos["entry"]
        if not is_buy:
            pdiff = -pdiff
        comm = self.settings.backtest_commission * pos["lot_size"]
        profit = round(pdiff * pos["lot_size"] * 100 - comm, 2)

        ticket = pos.get("ticket")
        if ticket:
            try:
                self.connector.close_position({
                    "symbol": self.settings.symbol,
                    "ticket": ticket,
                    "volume": pos["lot_size"],
                    "type": pos["type"],
                })
            except Exception as e:
                logger.error(f"Close failed: {e}")

        pos["pnl"] = profit
        pos["exit"] = price
        pos["exit_reason"] = reason
        pos["close_time"] = current_time

        logger.info(f"CLOSE {pos['type']} {pos['entry']:.2f} P=${profit:.2f} ({reason})")
        trade_logger.info(
            f"CLOSE {pos['type'].upper()} {pos['lot_size']} {pos['entry']:.2f} {price:.2f} {profit:.2f}",
            extra={"trade": pos},
        )

        self.risk_mgr.record_trade(profit)
        acct = self.connector.get_account_info()
        pos["balance"] = acct.get("balance", 0)
        self.telegram.alert_trade_close(pos)
        self.mongo.save_trade({
            "trade_id": pos.get("trade_id", ""),
            "symbol": self.settings.symbol,
            "signal_type": pos["type"],
            "entry_price": pos["entry"],
            "stop_loss": pos["sl"],
            "lot_size": pos["lot_size"],
            "exit_price": price,
            "profit": profit,
            "exit_reason": reason,
            "close_time": current_time,
            "session_date": current_time.strftime("%Y-%m-%d"),
            "strategy": "aggressive_m1",
        })
        self._position = None

    def _manage_position(self, bar: Dict[str, float], current_time: datetime) -> None:
        if self._position is None:
            return
        is_buy = self._position["type"] == "buy"
        if not self._position.get("tp_hit") and \
           ((is_buy and bar["high"] >= self._position["tp"]) or (not is_buy and bar["low"] <= self._position["tp"])):
            self._close_trade(self._position["tp"], "tp", current_time)
        elif self._position["remaining_lots"] > 0 and \
             ((is_buy and bar["low"] <= self._position["sl"]) or (not is_buy and bar["high"] >= self._position["sl"])):
            self._close_trade(self._position["sl"], "sl", current_time)

    def initialize(self) -> bool:
        logger.info("Initializing aggressive scalper bot...")
        try:
            self.connector.connect()
            logger.info("MT5 connected")
        except MT5ConnectorError as e:
            logger.error(f"MT5 connection failed: {e}")
            self.telegram.alert_error(f"MT5 connection failed: {e}")
            return False

        if not self.mongo.connect():
            logger.warning("MongoDB unavailable")

        self._load_15min_data()

        if self.news_filter is not None:
            self.news_filter.fetch_events()

        account = self.connector.get_account_info()
        logger.info(f"Account: {account['login']}, Balance: ${account['balance']:.2f}")

        self.telegram._send(
            f"\U0001f916 <b>Aggressive M1 Scalper Bot Started</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"Symbol: {self.settings.symbol}\n"
            f"Balance: ${account['balance']:.2f}\n"
            f"Strategy: Zone + Momentum (M1)\n"
            f"Risk: {RISK_PCT}% | SL: {SL_PIPS}pips | Max/Day: {MAX_TRADES_PER_DAY}\n"
            f"Filters: Spread={self.settings.max_spread}pips "
            f"CB={self.settings.circuit_breaker_max_daily_loss_pct}% "
            f"{'News ' if self.news_filter else ''}\n"
            f"Running 24/5 Mon 00:00 UTC to Fri 17:00 UTC\n"
            f"Time: {fmt_et(fmt='%I:%M %p')}"
        )
        return True

    def shutdown(self) -> None:
        logger.info("Shutting down...")
        if self._position is not None:
            try:
                positions = self.connector.get_positions(self.settings.symbol)
                for p in positions:
                    if p["ticket"] == self._position.get("ticket"):
                        self.connector.close_position(p)
                        break
            except Exception as e:
                logger.error(f"Failed to close on shutdown: {e}")

        self.telegram._send("\U0001f6ab <b>Aggressive M1 Scalper Bot Stopped</b>")
        self.mongo.disconnect()
        self.connector.disconnect()
        self._running = False

    def run(self) -> None:
        if not self.initialize():
            logger.error("Initialization failed, exiting")
            return

        self._running = True
        logger.info("Aggressive scalper bot started")

        try:
            while self._running:
                now = datetime.now(timezone.utc)

                if SessionValidator.is_friday_close(now):
                    secs_until_monday = (
                        SessionValidator.next_monday_utc(now) - now
                    ).total_seconds()
                    logger.info(f"Friday close — sleeping {secs_until_monday/3600:.1f}h")
                    self._position = None
                    self.mongo.disconnect()
                    self.connector.disconnect()
                    time.sleep(secs_until_monday)
                    self.connector.connect()
                    self.mongo.connect()
                    self._load_15min_data()
                    self._current_date = None
                    self._m15_last_refresh = 0
                    continue

                self._check_new_day()

                if time.time() - self._m15_last_refresh > self.M15_REFRESH_SECONDS:
                    self._load_15min_data()
                    self._m15_last_refresh = time.time()

                try:
                    rates = self.connector.get_rates("XAUUSD", mt5.TIMEFRAME_M1, 100)
                except MT5ConnectorError as e:
                    logger.error(f"Failed to get rates: {e}")
                    time.sleep(10)
                    continue

                if rates.empty or len(rates) < 10:
                    time.sleep(10)
                    continue

                i = len(rates) - 1
                current_time = rates.index[i]
                bar = rates.iloc[i]

                if self._df_15min is not None:
                    self.zone_detector.update_test_status(bar["high"], bar["low"])

                self._manage_position(bar, current_time)

                if self._position is None and self._trades_today < MAX_TRADES_PER_DAY:
                    if self.news_filter is not None:
                        in_blackout, reason = self.news_filter.is_blackout(now)
                        if in_blackout:
                            logger.debug(f"News filter blocked: {reason}")
                            time.sleep(60)
                            continue

                    acct = self.connector.get_account_info()
                    allowed, cb_reason = self.risk_mgr.check_entry_allowed(acct["balance"])
                    if not allowed:
                        logger.debug(f"CB blocked: {cb_reason}")
                        time.sleep(60)
                        continue

                    tick = self.connector.get_tick()
                    spread_pips = tick["spread"]
                    if spread_pips > self.settings.max_spread:
                        logger.debug(f"Spread too high: {spread_pips}")
                        time.sleep(10)
                        continue

                    direction = self._is_within_zone(bar["close"])
                    if direction and i >= 2:
                        prev_close = rates.iloc[i - 1]["close"]
                        if self._check_momentum(bar, prev_close, direction):
                            balance = acct["balance"]
                            lot_size = self._calc_lot_size(balance)
                            if lot_size >= 0.01:
                                mt5_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
                                price = bar["close"]
                                sl = price - SL_PRICE if direction == "buy" else price + SL_PRICE
                                tp = price + SL_PRICE if direction == "buy" else price - SL_PRICE

                                try:
                                    order = self.connector.place_order(
                                        symbol=self.settings.symbol,
                                        order_type=mt5_type,
                                        volume=lot_size,
                                        price=price,
                                        sl=sl,
                                        tp=tp,
                                        comment="AGGR",
                                    )
                                    self._trades_today += 1
                                    trade_id = str(uuid4())
                                    self._position = {
                                        "type": direction,
                                        "entry": order["price"],
                                        "sl": sl,
                                        "tp": tp,
                                        "lot_size": lot_size,
                                        "remaining_lots": lot_size,
                                        "pnl": 0.0,
                                        "tp_hit": False,
                                        "trade_id": trade_id,
                                        "open_time": current_time,
                                        "ticket": order.get("order", 0),
                                    }
                                    self.mongo.save_trade({
                                        "trade_id": trade_id,
                                        "symbol": self.settings.symbol,
                                        "signal_type": direction,
                                        "entry_price": order["price"],
                                        "stop_loss": sl,
                                        "take_profit": tp,
                                        "lot_size": lot_size,
                                        "session_date": current_time.strftime("%Y-%m-%d"),
                                        "open_time": current_time,
                                        "strategy": "aggressive_m1",
                                    })
                                    logger.info(
                                        f"AGGR TRADE {direction.upper()} "
                                        f"{lot_size} @ {order['price']:.2f} "
                                        f"SL={sl:.2f} TP={tp:.2f}"
                                    )
                                    trade_logger.info(
                                        f"OPEN {direction.upper()} {lot_size} "
                                        f"{order['price']:.2f} {sl:.2f} {tp:.2f}",
                                        extra={"trade": self._position},
                                    )
                                    acct = self.connector.get_account_info()
                                    self._position["balance"] = acct.get("balance", 0)
                                    self.telegram.alert_trade_open(self._position)
                                except MT5ConnectorError as e:
                                    logger.error(f"Order failed: {e}")
                                    self.telegram.alert_error(f"Order failed: {e}")

                if time.time() - self._last_heartbeat > self.HEARTBEAT_SECONDS:
                    self._last_heartbeat = time.time()
                    acct = self.connector.get_account_info()
                    pos_status = "Open" if self._position else "None"
                    self.telegram.alert_heartbeat(
                        f"Balance: ${acct['balance']:.2f}\n"
                        f"Equity: ${acct.get('equity', 0):.2f}\n"
                        f"Running since: {fmt_et(self._start_time, '%Y-%m-%d %I:%M %p')}\n"
                        f"Position: {pos_status} | Today: {self._trades_today}/{MAX_TRADES_PER_DAY}"
                    )

                time.sleep(self.POLL_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logger.info("Shutdown requested")
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            self.telegram.alert_error(f"Fatal error: {e}")
        finally:
            self.shutdown()


if __name__ == "__main__":
    setup_logging()
    bot = AggressiveBot()
    bot.run()
