#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import logging
import time
from uuid import uuid4
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

import pandas as pd
import MetaTrader5 as mt5

from config.settings import get_settings
from config.sessions import SessionTimes, SessionValidator
from log_utils.logger_setup import setup_logging, get_logger
from core.opening_range_scalp import OpeningRangeScalp
from core.institutional_zone import InstitutionalZoneDetector
from core.risk_manager import RiskManager
from core.news_filter import NewsFilter
from connectors.mt5_connector import MT5Connector, MT5ConnectorError
from database.mongo_client import MongoClient
from telegram.alerts import TelegramNotifier, fmt_et

logger = logging.getLogger(__name__)
trade_logger = get_logger("trade")


class ScalperBot:
    POLL_INTERVAL_SECONDS = 30
    M15_REFRESH_SECONDS = 300
    HEARTBEAT_SECONDS = 21600

    def __init__(self, env_file: str = ".env"):
        self.env_file = env_file
        self.settings = get_settings(env_file)
        self.session_times = SessionTimes()
        self.connector = MT5Connector()
        self.zone_detector = InstitutionalZoneDetector()
        self.orb = OpeningRangeScalp(
            zone_detector=self.zone_detector,
        )
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
        self._last_heartbeat: float = time.time()
        self._start_time: datetime = datetime.now(timezone.utc)
        self._last_signal_time: Optional[datetime] = None
        self._no_money_cooldown_until: float = 0
        self._cb_alerted: bool = False
        self._initial_balance: Optional[float] = None

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
            self._cb_alerted = False
            self.orb.reset()
            acct = self.connector.get_account_info()
            self.risk_mgr.start_day(today, acct["balance"])
            logger.info(f"New trading day: {today}")

    def _get_risk_amount(self, balance: float) -> float:
        if self._initial_balance is None:
            return 10.0
        profit = balance - self._initial_balance
        if profit >= 50000:
            return 50.0
        elif profit >= 10000:
            return 30.0
        elif profit >= 2000:
            return 20.0
        elif profit >= 500:
            return 15.0
        return 10.0

    def _calc_lot_size(self, entry: float, sl: float, balance: float) -> float:
        dist = abs(entry - sl)
        if dist <= 0:
            return 0.01
        risk_amount = self._get_risk_amount(balance)
        gold_oz_per_lot = 100.0
        risk_lots = round(risk_amount / (dist * gold_oz_per_lot), 2)

        try:
            margin_rate = self.connector.get_margin_rate()
            margin_lots = max(0.01, round((balance * 0.9) / margin_rate, 2))
        except Exception:
            margin_lots = 10.0

        return max(0.01, round(min(risk_lots, margin_lots, 10.0), 2))

    def _close_partial(self, lots: float, price: float, reason: str, current_time: datetime) -> None:
        pos = self._position
        is_buy = pos["type"] == "buy"
        pdiff = price - pos["entry"]
        if not is_buy:
            pdiff = -pdiff
        comm = self.settings.backtest_commission * lots
        profit = round(pdiff * lots * 100 - comm, 2)

        ticket = pos.get("ticket")
        if ticket:
            try:
                self.connector.close_position({
                    "symbol": self.settings.symbol,
                    "ticket": ticket,
                    "volume": lots,
                    "type": pos["type"],
                })
            except Exception as e:
                logger.error(f"Partial close {reason} failed: {e}")
                try:
                    positions = self.connector.get_positions(self.settings.symbol)
                    still_open = any(p["ticket"] == ticket for p in positions)
                except Exception:
                    still_open = True
                if still_open:
                    return
                self._resolve_position_closed(current_time)
                return

        pos["_last_price"] = price
        pos["pnl"] = round(pos.get("pnl", 0) + profit, 2)
        pos["remaining_lots"] = round(pos["remaining_lots"] - lots, 2)

        logger.info(
            f"PARTIAL {reason.upper()}: {lots:.2f} lots @ {price:.2f} "
            f"P=${profit:.2f} (cumulative: ${pos['pnl']:.2f})"
        )
        trade_logger.info(
            f"PARTIAL {pos['type'].upper()} {lots:.2f} {pos['entry']:.2f} {price:.2f} {profit:.2f}",
            extra={"trade": pos, "partial_reason": reason, "partial_lots": lots},
        )
        self.telegram.alert_partial(pos, reason, lots, price, profit, pos["pnl"])

    def _resolve_position_closed(self, current_time: datetime) -> None:
        pos = self._position
        ticket = pos.get("ticket")
        close_info = None
        if ticket:
            try:
                close_info = self.connector.get_position_close_from_history(ticket)
            except Exception:
                pass
        if close_info is not None:
            pos["_last_price"] = close_info["price"]
            pos["pnl"] = round(close_info["profit"], 2)
            pos["exit_reason"] = "sl"
            pos["close_time"] = close_info["time"]
            logger.info(
                f"Position {ticket} already closed by MT5 "
                f"@ {close_info['price']:.2f} P=${close_info['profit']:.2f}"
            )
        else:
            exit_price = pos.get("_last_price") or pos.get("sl", pos["entry"])
            pos["_last_price"] = exit_price
            is_buy = pos["type"] == "buy"
            pdiff = exit_price - pos["entry"]
            if not is_buy:
                pdiff = -pdiff
            remaining = pos.get("remaining_lots", pos["original_lot_size"])
            pnl_close = round(pdiff * remaining * 100, 2)
            pos["pnl"] = round(pos.get("pnl", 0) + pnl_close, 2)
            pos["exit_reason"] = "sl"
            pos["close_time"] = current_time
            logger.info(
                f"Position {ticket} no longer on MT5 (computed P&L=${pos['pnl']:.2f})"
            )
        pos["remaining_lots"] = 0.0
        trade_logger.info(
            f"CLOSE {pos['type'].upper()} {pos['entry']:.2f} {pos['close_time']} {pos['pnl']:.2f}",
            extra={"trade": pos},
        )
        self.risk_mgr.record_trade(pos["pnl"])
        acct = self.connector.get_account_info()
        pos["balance"] = acct.get("balance", 0)
        self.telegram.alert_trade_close(pos)
        self.mongo.save_trade({
            "trade_id": pos.get("trade_id", ""),
            "symbol": self.settings.symbol,
            "signal_type": pos["type"],
            "entry_price": pos["entry"],
            "stop_loss": pos.get("original_sl"),
            "lot_size": pos["original_lot_size"],
            "exit_price": pos.get("_last_price"),
            "profit": pos["pnl"],
            "exit_reason": pos["exit_reason"],
            "close_time": pos["close_time"],
            "session_date": pos["close_time"].strftime("%Y-%m-%d"),
            "strategy": "orb_scalp",
            "tp1_hit": pos.get("tp1_hit", False),
            "tp2_hit": pos.get("tp2_hit", False),
            "tp3_hit": pos.get("tp3_hit", False),
        })
        self.orb.reset_entry()
        self._position = None

    def _manage_position(self, df: pd.DataFrame, i: int, current_time: datetime) -> bool:
        if self._position is None:
            return False

        ticket = self._position.get("ticket")
        if ticket:
            try:
                positions = self.connector.get_positions(self.settings.symbol)
                still_open = any(p["ticket"] == ticket for p in positions)
            except Exception:
                still_open = True
            if not still_open:
                self._resolve_position_closed(current_time)
                return True

        entry = self._position["entry"]
        sl_dist = abs(entry - self._position["original_sl"])
        is_buy = self._position["type"] == "buy"

        tp1_level = entry + sl_dist if is_buy else entry - sl_dist
        tp2_level = entry + 2 * sl_dist if is_buy else entry - 2 * sl_dist

        # Check ALL bars since position open, not just the last bar.
        # Conditions are guarded by flags (tp1_hit, tp2_hit, remaining_lots)
        # so re-processing bars is idempotent and safe.
        open_time = self._position["open_time"]
        try:
            start_idx = max(0, df.index.get_loc(open_time))
        except KeyError:
            start_idx = 0
        for j in range(start_idx, i + 1):
            if self._position["remaining_lots"] <= 0:
                break
            bar = df.iloc[j]
            if df.index[j] <= open_time:
                continue

            # TP1: close first tranche at 1:1, move SL to BE
            if not self._position.get("tp1_hit", False) and \
               ((is_buy and bar["high"] >= tp1_level) or (not is_buy and bar["low"] <= tp1_level)):
                self._close_partial(self._position["tp1_lots"], tp1_level, "tp1", current_time)
                self._position["tp1_hit"] = True
                self._position["tp_hit_bar"] = j
                if self._position["remaining_lots"] > 0:
                    ok = self.connector.modify_position(
                        ticket=self._position["ticket"],
                        sl=entry,
                    )
                    if ok:
                        self._position["sl"] = entry
                    else:
                        logger.warning(
                            f"Failed to move SL to BE for {self._position['ticket']}"
                        )
                # Activate trailing for 50-50 model after TP1
                if self._position["tp3_lots"] == 0 and self._position["tp2_lots"] > 0 and \
                   self._position["remaining_lots"] > 0:
                    trail_dist = sl_dist * self.settings.trail_multiplier
                    if is_buy:
                        self._position["trail_level"] = bar["high"] - trail_dist
                    else:
                        self._position["trail_level"] = bar["low"] + trail_dist
                    self._position["trailing_activated"] = True
                    self._position["trail_activation_bar"] = j

            # TP2: close second tranche at 1:2 (3-target model only)
            if self._position.get("tp3_lots", 0) > 0 and \
               self._position.get("tp1_hit", False) and not self._position.get("tp2_hit", False) and \
               self._position["remaining_lots"] > 0 and \
               j != self._position.get("tp_hit_bar") and \
               ((is_buy and bar["high"] >= tp2_level) or (not is_buy and bar["low"] <= tp2_level)):
                lots = min(self._position["tp2_lots"], self._position["remaining_lots"])
                if lots > 0:
                    self._close_partial(lots, tp2_level, "tp2", current_time)
                    self._position["tp2_hit"] = True
                    self._position["tp_hit_bar"] = j
                    if self._position["remaining_lots"] > 0:
                        trail_dist = sl_dist * self.settings.trail_multiplier
                        if is_buy:
                            self._position["trail_level"] = bar["high"] - trail_dist
                        else:
                            self._position["trail_level"] = bar["low"] + trail_dist
                        self._position["trailing_activated"] = True
                        self._position["trail_activation_bar"] = j

            # Update trailing stop
            if self._position.get("trailing_activated") and self._position["remaining_lots"] > 0:
                trail_dist = sl_dist * self.settings.trail_multiplier
                if is_buy:
                    new_trail = bar["high"] - trail_dist
                    if new_trail > self._position["trail_level"]:
                        self._position["trail_level"] = new_trail
                else:
                    new_trail = bar["low"] + trail_dist
                    if new_trail < self._position["trail_level"]:
                        self._position["trail_level"] = new_trail

            # Check trailing stop — skip activation bar
            if self._position and self._position.get("trailing_activated") and self._position["remaining_lots"] > 0 and \
               j != self._position.get("trail_activation_bar") and \
               ((is_buy and bar["low"] <= self._position["trail_level"]) or (not is_buy and bar["high"] >= self._position["trail_level"])):
                self._close_partial(self._position["remaining_lots"], self._position["trail_level"], "trail", current_time)
                if self._position is None:
                    break

            # SL/be check on remaining position — skip the bar that triggered TP1/TP2
            if self._position and self._position["remaining_lots"] > 0 and \
               j != self._position.get("tp_hit_bar") and \
               ((is_buy and bar["low"] <= self._position["sl"]) or (not is_buy and bar["high"] >= self._position["sl"])):
                self._close_partial(self._position["remaining_lots"], self._position["sl"],
                                    "be" if self._position.get("tp1_hit") else "sl", current_time)
                if self._position is None:
                    break

        if self._position and self._position["remaining_lots"] <= 0:
            trade = self._position
            trade["exit"] = trade.get("_last_price", None)
            trade["exit_reason"] = "trail" if trade.get("trailing_activated") else \
                                   ("tp2" if trade.get("tp2_hit") else "sl/be")
            trade["close_time"] = current_time

            logger.info(
                f"CLOSE {trade['type']} {trade['entry']:.2f} "
                f"P=${trade['pnl']:.2f} ({trade['exit_reason']})"
            )
            trade_logger.info(
                f"CLOSE {trade['type']} {trade['entry']:.2f} {trade['close_time']} {trade['pnl']:.2f}",
                extra={"trade": trade},
            )
            self.risk_mgr.record_trade(trade["pnl"])
            acct = self.connector.get_account_info()
            trade["balance"] = acct.get("balance", 0)
            self.telegram.alert_trade_close(trade)
            self.mongo.save_trade({
                "trade_id": trade.get("trade_id", ""),
                "symbol": self.settings.symbol,
                "signal_type": trade["type"],
                "entry_price": trade["entry"],
                "stop_loss": trade.get("original_sl"),
                "lot_size": trade["original_lot_size"],
                "exit_price": trade.get("exit"),
                "profit": trade["pnl"],
                "exit_reason": trade["exit_reason"],
                "close_time": current_time,
                "session_date": current_time.strftime("%Y-%m-%d"),
                "strategy": "orb_scalp",
                "tp1_hit": trade.get("tp1_hit", False),
                "tp2_hit": trade.get("tp2_hit", False),
                "tp3_hit": trade.get("tp3_hit", False),
            })
            self.orb.reset_entry()
            self._position = None
            return True

        return False

    def initialize(self) -> bool:
        logger.info("Initializing scalper bot...")

        try:
            self.connector.connect()
            logger.info("MT5 connected")
        except MT5ConnectorError as e:
            logger.error(f"MT5 connection failed: {e}")
            self.telegram.alert_error(f"MT5 connection failed: {e}")
            return False

        if not self.mongo.connect():
            logger.warning("MongoDB unavailable — trades will not be persisted to database")

        self._load_15min_data()

        if self.news_filter is not None:
            self.news_filter.fetch_events()
            logger.info("News filter initialized")

        # Re-verify account — M15 data loading can cause MT5 to revert account
        if self.settings.mt5_login and self.settings.mt5_password:
            info = mt5.account_info()
            if info is not None and info.login != self.settings.mt5_login:
                logger.warning(f"Account reverted to {info.login}, re-logging as {self.settings.mt5_login}")
                mt5.login(
                    login=self.settings.mt5_login,
                    password=self.settings.mt5_password,
                    server=self.settings.mt5_server if self.settings.mt5_server else None,
                )

        account = self.connector.get_account_info()
        logger.info(f"Account: {account['login']}, Balance: ${account['balance']:.2f}")
        self._initial_balance = account["balance"]

        try:
            self.settings = self.settings.adjust_for_balance(account["balance"])
            risk_pct = self.settings.risk_percent
            max_trd = self.settings.max_daily_trades
        except ValueError as e:
            logger.error(str(e))
            self.telegram.alert_error(str(e))
            return False

        existing = self.connector.get_positions(self.settings.symbol)
        if existing:
            p = existing[0]
            self._position = {
                "type": p["type"],
                "entry": p["price_open"],
                "sl": p["sl"],
                "tp": p["tp"],
                "lot_size": p["volume"],
                "original_sl": p["sl"],
                "original_lot_size": p["volume"],
                "tp1_lots": 0,
                "tp2_lots": 0,
                "tp3_lots": 0,
                "remaining_lots": p["volume"],
                "pnl": 0.0,
                "tp1_hit": False,
                "tp2_hit": False,
                "tp3_hit": False,
                "trailing_activated": False,
                "trail_activation_bar": 0,
                "trade_id": str(uuid4()),
                "open_time": p["time"],
                "ticket": p["ticket"],
            }
            cents = round(p["volume"] * 100)
            if cents >= 10:
                tp1_c = int(cents * 0.3)
                tp2_c = int(cents * 0.4)
                self._position["tp1_lots"] = tp1_c / 100.0
                self._position["tp2_lots"] = tp2_c / 100.0
                self._position["tp3_lots"] = (cents - tp1_c - tp2_c) / 100.0
            elif cents >= 4:
                tp1_c = int(cents * 0.5)
                self._position["tp1_lots"] = tp1_c / 100.0
                self._position["tp2_lots"] = (cents - tp1_c) / 100.0
            else:
                self._position["tp1_lots"] = p["volume"]
            # Check if position was partially closed before restart
            try:
                close_info = self.connector.get_position_close_from_history(p["ticket"])
            except Exception:
                close_info = None
            if close_info:
                self._position["tp1_hit"] = True
                self._position["tp1_lots"] = 0
                self._position["tp2_lots"] = 0
                self._position["tp3_lots"] = 0
                self._position["trailing_activated"] = True
                sl_dist = max(abs(p["price_open"] - (p["sl"] or p["price_open"])), 0.15)
                trail_dist = sl_dist * self.settings.trail_multiplier
                if p["type"] == "buy":
                    self._position["trail_level"] = p["price_open"] - trail_dist
                else:
                    self._position["trail_level"] = p["price_open"] + trail_dist
                self._position["trail_activation_bar"] = 999999
                logger.info("Recovered partially closed position — converted to trail-only")

            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self._current_date = date_str
            self._trades_today = 1
            logger.info(f"Recovered orphaned position: {p['type']} {p['volume']:.2f} @ {p['price_open']:.2f} ticket={p['ticket']}")
            self.telegram.alert_error(f"Recovered orphaned position: {p['type']} {p['volume']:.2f} @ {p['price_open']:.2f}")

        if self.telegram.health_check():
            logger.info("Telegram connected — alerts enabled")
            self.telegram._send(
                f"\U0001f916 <b>ORB Scalper Bot Started</b>\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"Symbol: {self.settings.symbol}\n"
                f"Balance: ${account['balance']:.2f}\n"
                f"Strategy: ORB + Safety Filters\n"
                f"Risk: {risk_pct}% | Max/Day: {max_trd}\n"
                f"Filters: Spread={self.settings.max_spread}pips "
                f"Trail={self.settings.trail_multiplier}x "
                f"CB={self.settings.circuit_breaker_max_daily_loss_pct}% "
                f"{'News ' if self.news_filter else ''}\n"
                f"Sessions: Asia (00-09) London (09-12) NY (13-16) UTC\n"
                f"Time: {fmt_et(fmt='%I:%M %p')}"
            )
        else:
            logger.warning(
                "Telegram unreachable — alerts will not be delivered. "
                "Check network/firewall: api.telegram.org:443 must be reachable."
            )

        return True

    def shutdown(self) -> None:
        logger.info("Shutting down...")

        if self._position is not None:
            logger.info("Closing open position...")
            try:
                positions = self.connector.get_positions(self.settings.symbol)
                for p in positions:
                    if p["ticket"] == self._position.get("ticket"):
                        self.connector.close_position(p)
                        break
            except Exception as e:
                logger.error(f"Failed to close position on shutdown: {e}")

        if self.telegram.health_check():
            self.telegram._send(
                f"\U0001f6ab <b>ORB Scalper Bot Stopped</b>"
            )
        self.mongo.disconnect()
        self.connector.disconnect()
        self._running = False

    def run(self) -> None:
        if not self.initialize():
            logger.error("Initialization failed, exiting")
            return

        self._running = True
        logger.info("Scalper bot started")

        try:
            while self._running:
                now = datetime.now(timezone.utc)

                if SessionValidator.is_friday_close(now):
                    secs_until_monday = (
                        SessionValidator.next_monday_utc(now) - now
                    ).total_seconds()
                    logger.info(
                        f"Friday close — sleeping {secs_until_monday / 3600:.1f}h until Monday"
                    )
                    position_closed = False
                    if self._position is not None:
                        try:
                            self.connector.close_position({
                                "symbol": self.settings.symbol,
                                "ticket": self._position["ticket"],
                                "volume": self._position["remaining_lots"],
                                "type": self._position["type"],
                            })
                            logger.info("Closed open position before Friday shutdown")
                            position_closed = True
                        except Exception as e:
                            logger.error(f"Failed to close position before Friday shutdown: {e}")
                    if position_closed:
                        self._position = None
                    self.mongo.disconnect()
                    self.connector.disconnect()
                    time.sleep(secs_until_monday)
                    self.connector.connect()
                    if not self.mongo.connect():
                        logger.warning("MongoDB reconnection failed after weekend")
                    self._load_15min_data()
                    self._current_date = None
                    self._m15_last_refresh = 0
                    continue

                if not self.session_times.is_trading_hours(now):
                    time.sleep(60)
                    continue

                self._check_new_day()

                current_session = self.session_times.get_active_session(now)
                if current_session is None:
                    time.sleep(60)
                    continue

                if time.time() - self._m15_last_refresh > self.M15_REFRESH_SECONDS:
                    self._load_15min_data()
                    self._m15_last_refresh = time.time()

                try:
                    rates = self.connector.get_rates("XAUUSD", mt5.TIMEFRAME_M5, 300)
                except MT5ConnectorError as e:
                    logger.error(f"Failed to get rates: {e}")
                    time.sleep(10)
                    continue

                if rates.empty or len(rates) < 60:
                    time.sleep(10)
                    continue

                i = len(rates) - 1
                current_time = rates.index[i]

                bar = rates.iloc[i]
                if self._df_15min is not None:
                    self.zone_detector.update_test_status(bar["high"], bar["low"])

                self._manage_position(rates, i, current_time)

                if self._position is None and self._trades_today < self.settings.max_daily_trades:
                    throttled = self._last_signal_time and (current_time - self._last_signal_time).total_seconds() < 180
                    if not throttled:
                        # News blackout
                        if self.news_filter is not None:
                            in_blackout, reason = self.news_filter.is_blackout(now)
                            if in_blackout:
                                logger.debug(f"News filter blocked: {reason}")
                                time.sleep(60)
                                continue

                        # No-money cooldown — stop spamming retcode=10019
                        if time.time() < self._no_money_cooldown_until:
                            time.sleep(10)
                            continue

                        # Circuit breaker
                        acct = self.connector.get_account_info()
                        allowed, cb_reason = self.risk_mgr.check_entry_allowed(acct["balance"])
                        if not allowed:
                            logger.debug(f"Circuit breaker blocked: {cb_reason}")
                            if not self._cb_alerted:
                                self.telegram.alert_error(f"Circuit breaker blocked: {cb_reason}")
                                self._cb_alerted = True
                            time.sleep(60)
                            continue

                        window_df = rates.iloc[max(0, i - 200):i + 1]
                        df_15min_window = self._df_15min[self._df_15min.index <= current_time] if self._df_15min is not None else pd.DataFrame()
                        signal = self.orb.analyze(window_df, df_15min_window, current_time, session=current_session)

                        if signal is not None:
                            balance = self.connector.get_account_info()["balance"]
                            lot_size = self._calc_lot_size(
                                signal["entry"], signal["sl"], balance
                            )

                            if lot_size >= 0.01:
                                mt5_type = mt5.ORDER_TYPE_BUY if signal["direction"] == "buy" else mt5.ORDER_TYPE_SELL

                                tick = self.connector.get_tick()
                                spread_pips = tick["spread"]
                                if spread_pips > self.settings.max_spread:
                                    logger.debug(f"Spread too high: {spread_pips} > {self.settings.max_spread}")
                                    time.sleep(10)
                                    continue

                                sl_dist = abs(signal["entry"] - signal["sl"])
                                tp_dist = abs(signal["entry"] - signal["tp"])

                                if signal["direction"] == "buy":
                                    entry_price = tick["ask"]
                                    new_sl = tick["bid"] - sl_dist
                                    new_tp = entry_price + tp_dist
                                else:
                                    entry_price = tick["bid"]
                                    new_sl = tick["ask"] + sl_dist
                                    new_tp = entry_price - tp_dist

                                try:
                                    order = self.connector.place_order(
                                        symbol=self.settings.symbol,
                                        order_type=mt5_type,
                                        volume=lot_size,
                                        price=entry_price,
                                        sl=new_sl,
                                        tp=new_tp,
                                        comment=f"ORB {signal.get('setup', '')}",
                                    )
                                    self._last_signal_time = current_time
                                    self._trades_today += 1
                                    trade_id = str(uuid4())
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
                                    actual_sl = order.get("sl", new_sl)
                                    actual_tp = order.get("tp", new_tp)
                                    self._position = {
                                        "type": signal["direction"],
                                        "entry": order["price"],
                                        "sl": actual_sl,
                                        "tp": actual_tp,
                                        "lot_size": lot_size,
                                        "original_sl": actual_sl,
                                        "original_lot_size": lot_size,
                                        "tp1_lots": tp1_c / 100.0,
                                        "tp2_lots": tp2_c / 100.0,
                                        "tp3_lots": tp3_c / 100.0,
                                        "remaining_lots": lot_size,
                                        "pnl": 0.0,
                                        "tp1_hit": False,
                                        "tp2_hit": False,
                                        "tp3_hit": False,
                                        "trailing_activated": False,
                                        "trail_activation_bar": 0,
                                        "trade_id": trade_id,
                                        "open_time": current_time,
                                        "ticket": order["ticket"],
                                        "session": current_session,
                                    }
                                    self.mongo.save_trade({
                                        "trade_id": trade_id,
                                        "symbol": self.settings.symbol,
                                        "signal_type": signal["direction"],
                                        "entry_price": order["price"],
                                        "stop_loss": actual_sl,
                                        "take_profit": actual_tp,
                                        "lot_size": lot_size,
                                        "session_date": current_time.strftime("%Y-%m-%d"),
                                        "open_time": current_time,
                                        "strategy": "orb_scalp",
                                        "setup_notes": signal.get("setup", ""),
                                    })
                                    logger.info(
                                        f"ORB TRADE {signal['direction'].upper()} "
                                        f"{lot_size} @ {order['price']:.2f} "
                                        f"SL={actual_sl:.2f} TP={actual_tp:.2f}"
                                    )
                                    trade_logger.info(
                                        f"OPEN {signal['direction'].upper()} {lot_size} "
                                        f"{order['price']:.2f} {actual_sl:.2f} {actual_tp:.2f}",
                                        extra={"trade": self._position},
                                    )
                                    acct = self.connector.get_account_info()
                                    self._position["balance"] = acct.get("balance", 0)
                                    self.telegram.alert_trade_open(self._position)
                                except MT5ConnectorError as e:
                                    logger.error(f"Order failed: {e}")
                                    if "No money" in str(e) or "10019" in str(e):
                                        self._no_money_cooldown_until = time.time() + 300
                                    self.telegram.alert_error(f"Order failed: {e}")

                if time.time() - self._last_heartbeat > self.HEARTBEAT_SECONDS:
                    self._last_heartbeat = time.time()
                    acct = self.connector.get_account_info()
                    pos_status = "Open" if self._position else "None"
                    self.telegram.alert_heartbeat(
                        f"Balance: ${acct['balance']:.2f}\n"
                        f"Equity: ${acct.get('equity', 0):.2f}\n"
                        f"Running since: {fmt_et(self._start_time, '%Y-%m-%d %I:%M %p')}\n"
                        f"Position: {pos_status} | Today: {self._trades_today}/{self.settings.max_daily_trades}"
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
    parser = argparse.ArgumentParser(description="ORB Scalper Live Bot")
    parser.add_argument("--env", type=str, default=".env", help="Env file to load (default: .env)")
    args = parser.parse_args()
    PROJ_ROOT = Path(__file__).resolve().parent.parent
    env_path = str(PROJ_ROOT / args.env)
    bot = ScalperBot(env_file=env_path)
    setup_logging()
    bot.run()
