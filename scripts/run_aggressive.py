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
from core.institutional_zone import InstitutionalZoneDetector
from core.risk_manager import RiskManager
from core.news_filter import NewsFilter
from connectors.mt5_connector import MT5Connector, MT5ConnectorError
from database.mongo_client import MongoClient
from telegram.alerts import TelegramNotifier, fmt_et

logger = logging.getLogger(__name__)
trade_logger = get_logger("trade")

SL_PIPS = 50
SL_PRICE = SL_PIPS / 100.0
RISK_PCT = 1.2
MAX_TRADES_PER_DAY = 20


class AggressiveBot:
    POLL_INTERVAL_SECONDS = 30
    M15_REFRESH_SECONDS = 300
    HEARTBEAT_SECONDS = 21600

    def __init__(self, env_file: str = ".env"):
        self.env_file = env_file
        self.settings = get_settings(env_file)
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
        self._last_heartbeat: float = time.time()
        self._start_time: datetime = datetime.now(timezone.utc)
        self._initial_balance: Optional[float] = None
        self._cb_alerted: bool = False
        self._no_money_cooldown_until: float = 0
        self._last_signal_time: float = 0

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

    def _calc_lot_size(self, balance: float, sl_price: float = SL_PRICE) -> float:
        risk_amount = self._get_risk_amount(balance)
        return max(0.01, min(round(risk_amount / (sl_price * 100), 2), 10.0))

    def _get_zone_signal(self, price: float, dir_filter: Optional[str] = None):
        best_dist = float("inf")
        direction = None
        zone_sl = None
        for z in self.zone_detector.zones:
            if z.breached:
                continue
            if dir_filter == "sell" and z.zone_type != "supply":
                continue
            if dir_filter == "buy" and z.zone_type != "demand":
                continue
            if z.zone_type == "demand" and z.zone_high < price:
                d = abs(price - (z.zone_high + z.zone_low) / 2.0)
                if d < best_dist:
                    best_dist = d
                    direction = "buy"
                    zone_sl = z.zone_low - 0.15
            elif z.zone_type == "supply" and z.zone_low > price:
                d = abs(price - (z.zone_high + z.zone_low) / 2.0)
                if d < best_dist:
                    best_dist = d
                    direction = "sell"
                    zone_sl = z.zone_high + 0.15
        return direction, zone_sl

    def _check_momentum(self, bar: Dict[str, float], prev_close: float, direction: str) -> bool:
        if direction == "buy":
            return bar["close"] > bar["open"] and bar["close"] > prev_close
        else:
            return bar["close"] < bar["open"] and bar["close"] < prev_close

    def _check_trend(self) -> Optional[str]:
        df = self._df_15min
        if df is None or len(df) < 100:
            return None
        close = df["close"]
        ema50 = close.ewm(span=50, adjust=False).mean()
        if len(ema50) < 50 or pd.isna(ema50.iloc[-1]):
            return None
        current_price = close.iloc[-1]
        if pd.isna(current_price):
            return None
        if current_price > ema50.iloc[-1]:
            return "bullish"
        return "bearish"

    def _close_partial(self, lots: float, price: float, reason: str, current_time: datetime) -> bool:
        pos = self._position
        is_buy = pos["type"] == "buy"
        pdiff = price - pos["entry"]
        if not is_buy:
            pdiff = -pdiff
        profit = round(pdiff * lots * 100, 2)

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
                    return False
                actual = self.connector.get_position_close_from_history(ticket, self.settings.symbol, pos.get("entry"))
                if actual:
                    pos["pnl"] = round(actual["profit"], 2)
                    pos["exit"] = actual["price"]
                    pos["exit_reason"] = reason
                    pos["close_time"] = actual["time"]
                else:
                    pos["exit"] = price
                    pos["exit_reason"] = reason
                    pos["close_time"] = current_time
                    pos["pnl"] = round(pos.get("pnl", 0) + profit, 2)
                pos["remaining_lots"] = 0.0
                pos["closed"] = True
                # Record trade immediately since MT5 already closed it
                self.risk_mgr.record_trade(pos["pnl"])
                try:
                    acct = self.connector.get_account_info()
                    pos["balance"] = acct.get("balance", 0)
                except Exception:
                    pos["balance"] = 0
                trade_logger.info(
                    f"CLOSE {pos['type'].upper()} {pos['entry']:.2f} {pos['close_time']} {pos['pnl']:.2f}",
                    extra={"trade": pos},
                )
                self.telegram.alert_trade_close(pos)
                self.mongo.save_trade({
                    "trade_id": pos.get("trade_id", ""),
                    "symbol": self.settings.symbol,
                    "signal_type": pos["type"],
                    "entry_price": pos["entry"],
                    "stop_loss": pos.get("original_sl"),
                    "lot_size": pos["original_lot_size"],
                    "exit_price": pos.get("exit"),
                    "profit": pos["pnl"],
                    "exit_reason": reason,
                    "close_time": pos["close_time"],
                    "session_date": pos["close_time"].strftime("%Y-%m-%d"),
                    "strategy": "aggressive_m1",
                })
                self._position = None
                return True

        pos["pnl"] = round(pos.get("pnl", 0) + profit, 2)
        pos["remaining_lots"] = max(0, round(pos["remaining_lots"] - lots, 2))
        pos["_last_price"] = price
        if pos["remaining_lots"] <= 0:
            pos["exit"] = price

        logger.info(f"PARTIAL {reason}: {lots:.2f} @ {price:.2f} P=${profit:.2f} (cum: ${pos['pnl']:.2f})")
        trade_logger.info(
            f"PARTIAL {pos['type'].upper()} {lots} {pos['entry']:.2f} {price:.2f} {profit:.2f}",
            extra={"trade": pos, "reason": reason},
        )
        self.telegram.alert_partial(pos, reason, lots, price, profit, pos["pnl"])
        return True

    def _manage_position(self, rates: pd.DataFrame, i: int, current_time: datetime) -> None:
        if self._position is None:
            return

        pos = self._position
        is_buy = pos["type"] == "buy"
        sl_dist = abs(pos["entry"] - pos["original_sl"]) if pos.get("original_sl") else SL_PRICE
        tp1_level = pos["entry"] + sl_dist if is_buy else pos["entry"] - sl_dist

        # Stale ticket check — verify position still exists on MT5
        ticket = pos.get("ticket")
        if ticket:
            try:
                positions = self.connector.get_positions(self.settings.symbol)
                still_open = any(p["ticket"] == ticket for p in positions)
                if not still_open:
                    if positions:
                        logger.warning(
                            f"Position {ticket} no longer on MT5. "
                            f"Open tickets: {[p['ticket'] for p in positions]}. "
                            f"Pos entry={pos.get('entry'):.2f} sl={pos.get('sl'):.2f}"
                        )
                    else:
                        logger.warning(
                            f"Position {ticket} no longer on MT5 (no open positions on symbol). "
                            f"Pos entry={pos.get('entry'):.2f} sl={pos.get('sl'):.2f}"
                        )
            except Exception:
                still_open = True
            if not still_open:
                logger.warning(f"Position {ticket} no longer on MT5, resolving...")
                close_info = self.connector.get_position_close_from_history(ticket, self.settings.symbol, pos.get("entry"))
                if close_info is not None:
                    pos["pnl"] = round(close_info["profit"], 2)
                    pos["exit"] = close_info["price"]
                    pos["exit_reason"] = "sl"
                    pos["close_time"] = close_info["time"]
                    logger.info(f"Closed from history: P=${close_info['profit']:.2f}")
                else:
                    exit_price = pos.get("_last_price") or pos.get("sl", pos["entry"])
                    pos["exit"] = exit_price
                    is_buy = pos["type"] == "buy"
                    pdiff = exit_price - pos["entry"]
                    if not is_buy:
                        pdiff = -pdiff
                    remaining = pos.get("remaining_lots", pos["original_lot_size"])
                    pnl_close = round(pdiff * remaining * 100, 2)
                    pos["pnl"] = round(pos.get("pnl", 0) + pnl_close, 2)
                    pos["exit_reason"] = "sl"
                    pos["close_time"] = current_time
                self.risk_mgr.record_trade(pos["pnl"])
                acct = self.connector.get_account_info()
                pos["balance"] = acct.get("balance", 0)
                trade_logger.info(
                    f"CLOSE {pos['type'].upper()} {pos['entry']:.2f} {pos['close_time']} {pos['pnl']:.2f}",
                    extra={"trade": pos},
                )
                self.telegram.alert_trade_close(pos)
                self.mongo.save_trade({
                    "trade_id": pos.get("trade_id", ""),
                    "symbol": self.settings.symbol,
                    "signal_type": pos["type"],
                    "entry_price": pos["entry"],
                    "stop_loss": pos.get("original_sl"),
                    "lot_size": pos["original_lot_size"],
                    "exit_price": pos.get("exit"),
                    "profit": pos["pnl"],
                    "exit_reason": pos["exit_reason"],
                    "close_time": pos["close_time"],
                    "session_date": pos["close_time"].strftime("%Y-%m-%d"),
                    "strategy": "aggressive_m1",
                })
                self._position = None
                return

        open_time = pos["open_time"]
        try:
            start_idx = max(0, rates.index.get_loc(open_time))
        except KeyError:
            start_idx = 0
        for j in range(start_idx, i + 1):
            if pos["remaining_lots"] <= 0:
                break
            bar = rates.iloc[j]
            if rates.index[j] <= open_time:
                continue

            # TP1 — close 50%, move SL to BE, activate trail
            if not pos.get("tp1_hit") and \
               ((is_buy and bar["high"] >= tp1_level) or (not is_buy and bar["low"] <= tp1_level)):
                ok = self._close_partial(pos["tp1_lots"], tp1_level, "tp1", current_time)
                if not ok or self._position is None:
                    if self._position is None:
                        return
                    continue
                pos["tp1_hit"] = True
                pos["tp_hit_bar"] = j
                if pos["remaining_lots"] > 0:
                    ok = self.connector.modify_position(
                        ticket=pos["ticket"],
                        sl=pos["entry"],
                    )
                    if not ok:
                        time.sleep(0.5)
                        ok = self.connector.modify_position(
                            ticket=pos["ticket"],
                            sl=pos["entry"],
                        )
                    if ok:
                        pos["sl"] = pos["entry"]
                    else:
                        logger.warning(f"Failed to move SL to BE for {ticket}")
                trail_dist = sl_dist * self.settings.trail_multiplier
                if is_buy:
                    pos["trail_level"] = max(pos["entry"], bar["high"] - trail_dist)
                else:
                    pos["trail_level"] = min(pos["entry"], bar["low"] + trail_dist)
                pos["trailing_activated"] = True
                pos["trail_activation_bar"] = j

            # Update trailing stop
            if pos.get("trailing_activated") and pos["remaining_lots"] > 0:
                trail_dist = sl_dist * self.settings.trail_multiplier
                if is_buy:
                    new_trail = bar["high"] - trail_dist
                    if new_trail > pos["trail_level"]:
                        pos["trail_level"] = max(pos["entry"], new_trail)
                else:
                    new_trail = bar["low"] + trail_dist
                    if new_trail < pos["trail_level"]:
                        pos["trail_level"] = min(pos["entry"], new_trail)

            # Check trailing stop — skip activation bar
            trail_fired = False
            if pos.get("trailing_activated") and pos["remaining_lots"] > 0 and \
               j != pos.get("trail_activation_bar") and \
               ((is_buy and bar["low"] <= pos["trail_level"]) or (not is_buy and bar["high"] >= pos["trail_level"])):
                if self._close_partial(pos["remaining_lots"], pos["trail_level"], "trail", current_time) and self._position is None:
                    return
                trail_fired = True

            # SL/BE check — skip the bar that triggered TP1
            if not trail_fired and pos["remaining_lots"] > 0 and \
               j != pos.get("tp_hit_bar") and \
               ((is_buy and bar["low"] <= (pos.get("sl") or 0)) or (not is_buy and bar["high"] >= (pos.get("sl") or 99999))):
                if self._close_partial(pos["remaining_lots"], pos["sl"],
                                       "be" if pos.get("tp1_hit") else "sl", current_time) and self._position is None:
                    return

        if pos["remaining_lots"] <= 0.005 and not pos.get("closed"):
            pos["remaining_lots"] = 0.0
            pos["closed"] = True
            pos.setdefault("exit", pos.get("_last_price", None))
            pos["exit_reason"] = "trail" if pos.get("trailing_activated") else "sl/be"
            pos["close_time"] = current_time

            logger.info(f"CLOSE {pos['type']} {pos['entry']:.2f} P=${pos['pnl']:.2f} ({pos['exit_reason']})")
            trade_logger.info(
                f"CLOSE {pos['type']} {pos['entry']:.2f} {pos['close_time']} {pos['pnl']:.2f}",
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
                "exit_price": pos.get("exit"),
                "profit": pos["pnl"],
                "exit_reason": pos["exit_reason"],
                "close_time": current_time,
                "session_date": current_time.strftime("%Y-%m-%d"),
                "strategy": "aggressive_m1",
            })
            self._position = None
        elif pos["remaining_lots"] <= 0:
            self._position = None

    def initialize(self) -> bool:
        logger.info("Initializing aggressive scalper bot...")
        try:
            self.connector.connect()
            logger.info("MT5 connected")
            term = mt5.terminal_info()
            if term is not None and not term.trade_allowed:
                logger.error(
                    "AutoTrading is DISABLED in MetaTrader 5. "
                    "The bot cannot place any orders until this is fixed.\n"
                    "  To enable: Open MT5 → Tools → Options → Expert Advisors\n"
                    "    → Check 'Allow Automated Trading' → OK\n"
                    "  Make sure the terminal window is NOT minimized.\n"
                    "  The bot will attempt to enable it via Alt+T, but this "
                    "requires the MT5 window to be visible and focused."
                )
                self.telegram.alert_error(
                    "AutoTrading DISABLED in MT5. "
                    "Go to Tools → Options → Expert Advisors → "
                    "enable 'Allow Automated Trading'"
                )
        except MT5ConnectorError as e:
            logger.error(f"MT5 connection failed: {e}")
            self.telegram.alert_error(f"MT5 connection failed: {e}")
            return False

        if not self.mongo.connect():
            logger.warning("MongoDB unavailable")

        self._load_15min_data()

        if self.news_filter is not None:
            self.news_filter.fetch_events()

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
        except ValueError as e:
            logger.error(str(e))
            self.telegram.alert_error(str(e))
            return False

        existing = self.connector.get_positions(self.settings.symbol)
        if existing:
            p = existing[0]
            cents = round(p["volume"] * 100)
            tp1_l = int(cents * 0.5) / 100.0
            self._position = {
                "type": p["type"],
                "entry": p["price_open"],
                "sl": p["sl"],
                "tp": p["tp"],
                "lot_size": p["volume"],
                "original_sl": p["sl"],
                "original_lot_size": p["volume"],
                "tag": "AGGR",
                "tp1_lots": tp1_l,
                "remaining_lots": p["volume"],
                "pnl": 0.0,
                "tp1_hit": False,
                "trailing_activated": False,
                "trail_level": 0.0,
                "trail_activation_bar": 0,
                "tp_hit_bar": 0,
                "trade_id": str(uuid4()),
                "open_time": p["time"],
                "ticket": p["ticket"],
            }
            try:
                close_info = self.connector.get_position_close_from_history(p["ticket"], p.get("symbol"), p.get("price_open"))
            except Exception:
                close_info = None
            if close_info:
                self._position["tp1_hit"] = True
                self._position["tp1_lots"] = 0
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
                f"\U0001f916 <b>Aggressive M1 Scalper Bot Started</b>\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"Symbol: {self.settings.symbol}\n"
                f"Balance: ${account['balance']:.2f}\n"
                f"Strategy: Zone + Momentum (M1) 50/50 + Trail\n"
                f"Risk: {RISK_PCT}% | SL: {SL_PIPS}pips | Max/Day: {MAX_TRADES_PER_DAY}\n"
                f"Filters: Spread={self.settings.max_spread}pips "
                f"CB={self.settings.circuit_breaker_max_daily_loss_pct}% "
                f"{'News ' if self.news_filter else ''}\n"
                f"Running 24/5 Mon 00:00 to Fri 17:00 UTC\n"
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
            try:
                positions = self.connector.get_positions(self.settings.symbol)
                for p in positions:
                    if p["ticket"] == self._position.get("ticket"):
                        self.connector.close_position(p)
                        break
            except Exception as e:
                logger.error(f"Failed to close on shutdown: {e}")

        if self.telegram.health_check():
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
                    while secs_until_monday > 0 and self._running:
                        sleep_time = min(60, secs_until_monday)
                        time.sleep(sleep_time)
                        secs_until_monday -= 60
                    if not self._running:
                        return
                    self.connector.connect()
                    if not self.mongo.connect():
                        logger.warning("MongoDB reconnection failed after weekend")
                    self._load_15min_data()
                    self._current_date = None
                    self._m15_last_refresh = 0
                    continue

                self._check_new_day()

                if not SessionValidator.is_valid_session_day(now) or not SessionTimes().is_trade_window(now):
                    time.sleep(60)
                    continue

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

                self._manage_position(rates, i, current_time)

                if self._position is None:  # and self._trades_today < MAX_TRADES_PER_DAY:
                    # Future limits (uncomment when ready):
                    # if self.risk_mgr._daily_loss_sum / max(acct["balance"], 1) * 100 >= self.settings.circuit_breaker_max_daily_loss_pct:
                    #     logger.warning("Daily loss limit reached")
                    #     time.sleep(60)
                    #     continue
                    # if self.risk_mgr._consecutive_losses >= self.settings.circuit_breaker_max_consecutive_losses:
                    #     logger.warning("Consecutive losses limit reached")
                    #     time.sleep(60)
                    #     continue
                    if self.news_filter is not None:
                        in_blackout, reason = self.news_filter.is_blackout(now)
                        if in_blackout:
                            logger.debug(f"News filter blocked: {reason}")
                            time.sleep(60)
                            continue

                    try:
                        acct = self.connector.get_account_info()
                    except Exception:
                        logger.warning("Failed to get account info, retrying...")
                        time.sleep(5)
                        continue
                    allowed, cb_reason = self.risk_mgr.check_entry_allowed(acct["balance"])
                    if not allowed:
                        logger.warning(f"CB blocked: {cb_reason}")
                        if not self._cb_alerted:
                            self.telegram.alert_error(f"Circuit breaker blocked: {cb_reason}")
                            self._cb_alerted = True
                        time.sleep(60)
                        continue

                    tick = self.connector.get_tick()
                    spread_pips = tick["spread"]

                    # Signal frequency throttle (180s min between entries)
                    if time.time() - self._last_signal_time < 180:
                        time.sleep(10)
                        continue

                    if spread_pips > self.settings.max_spread:
                        logger.debug(f"Spread too high: {spread_pips}")
                        time.sleep(10)
                        continue

                    if time.time() < self._no_money_cooldown_until:
                        time.sleep(10)
                        continue

                    direction, zone_sl = self._get_zone_signal(bar["close"])
                    if direction and i >= 2:
                        trend = self._check_trend()
                        if trend is not None and ((direction == "buy" and trend != "bullish") or (direction == "sell" and trend != "bearish")):
                            trend_dir = "sell" if trend == "bearish" else "buy"
                            alt_signal = self._get_zone_signal(bar["close"], trend_dir)
                            if alt_signal[0]:
                                direction, zone_sl = alt_signal
                                logger.info(f"Trend filter: switched to {direction} (M15 trend={trend})")
                            else:
                                logger.info(f"Trend filter blocked {direction} (M15 trend={trend}), no {trend_dir} zone found")
                                time.sleep(60)
                                continue
                        prev_close = rates.iloc[i - 1]["close"]
                        if self._check_momentum(bar, prev_close, direction):
                            balance = acct["balance"]
                            price = tick["ask"] if direction == "buy" else tick["bid"]
                            MIN_SL_DIST = 0.30
                            if zone_sl is not None:
                                raw_dist = abs(zone_sl - price)
                                actual_sl_dist = max(raw_dist, MIN_SL_DIST)
                                if actual_sl_dist > 0.80:
                                    sl = (tick["bid"] - SL_PRICE) if direction == "buy" else (tick["ask"] + SL_PRICE)
                                    actual_sl_dist = SL_PRICE
                                elif direction == "buy":
                                    sl = price - actual_sl_dist
                                else:
                                    sl = price + actual_sl_dist
                            else:
                                logger.info(f"No zone-based SL found (zone_sl=None), skipping trade")
                                continue
                            # Hard clamp: ensure SL is at least MIN_SL_DIST away from price
                            actual_sl_dist = max(actual_sl_dist, MIN_SL_DIST)
                            if direction == "buy":
                                sl = min(sl, price - actual_sl_dist)
                            else:
                                sl = max(sl, price + actual_sl_dist)
                            logger.info(
                                f"SL calc: zone_sl={zone_sl}, price={price:.2f}, "
                                f"raw_dist={'N/A' if zone_sl is None else f'{abs(zone_sl - price):.2f}'}, "
                                f"actual_sl_dist={actual_sl_dist:.2f}, sl={sl:.2f}"
                            )
                            lot_size = self._calc_lot_size(balance, actual_sl_dist)
                            if lot_size >= 0.01:
                                mt5_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
                                tp = (price + actual_sl_dist * 25) if direction == "buy" else (price - actual_sl_dist * 25)

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
                                    if not order.get("ticket"):
                                        logger.error("Order placed but got no ticket")
                                        self.telegram.alert_error("Order placed but got no ticket")
                                        continue
                                    filled_lot = order.get("volume", lot_size)
                                    self._trades_today += 1
                                    trade_id = str(uuid4())
                                    cents = round(filled_lot * 100)
                                    tp1_l = int(cents * 0.5) / 100.0
                                    actual_sl = order.get("sl", sl)
                                    actual_tp = order.get("tp", tp)
                                    self._position = {
                                        "type": direction,
                                        "entry": order["price"],
                                        "sl": actual_sl,
                                        "tp": actual_tp,
                                        "original_sl": actual_sl,
                                        "lot_size": filled_lot,
                                        "original_lot_size": filled_lot,
                                        "tag": "AGGR",
                                        "tp1_lots": tp1_l,
                                        "remaining_lots": filled_lot,
                                        "pnl": 0.0,
                                        "tp1_hit": False,
                                        "trailing_activated": False,
                                        "trail_level": 0.0,
                                        "trail_activation_bar": 0,
                                        "tp_hit_bar": 0,
                                        "trade_id": trade_id,
                                        "open_time": current_time,
                                        "ticket": order["ticket"],
                                    }
                                    self.mongo.save_trade({
                                        "trade_id": trade_id,
                                        "symbol": self.settings.symbol,
                                        "signal_type": direction,
                                        "entry_price": order["price"],
                                        "stop_loss": actual_sl,
                                        "take_profit": actual_tp,
                                        "lot_size": filled_lot,
                                        "session_date": current_time.strftime("%Y-%m-%d"),
                                        "open_time": current_time,
                                        "strategy": "aggressive_m1",
                                    })
                                    logger.info(
                                        f"AGGR TRADE {direction.upper()} "
                                        f"{filled_lot} @ {order['price']:.2f} "
                                        f"SL={actual_sl:.2f} TP={actual_tp:.2f} "
                                        f"ticket={order['ticket']}"
                                    )
                                    trade_logger.info(
                                        f"OPEN {direction.upper()} {filled_lot} "
                                        f"{order['price']:.2f} {actual_sl:.2f} {actual_tp:.2f}",
                                        extra={"trade": self._position},
                                    )
                                    acct = self.connector.get_account_info()
                                    self._position["balance"] = acct.get("balance", 0)
                                    self._last_signal_time = time.time()
                                    self.telegram.alert_trade_open(self._position)
                                except MT5ConnectorError as e:
                                    logger.error(f"Order failed: {e}")
                                    if "10019" in str(e) or "money" in str(e).lower():
                                        self._no_money_cooldown_until = time.time() + 3600
                                        logger.warning("No money — cooling down for 1 hour")
                                    elif "10027" in str(e) or "autotrading" in str(e).lower():
                                        self._no_money_cooldown_until = time.time() + 300
                                        logger.warning("AutoTrading disabled — cooling down for 5 min")
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
    parser = argparse.ArgumentParser(description="Aggressive M1 Scalper Live Bot")
    parser.add_argument("--env", type=str, default=".env", help="Env file to load (default: .env)")
    args = parser.parse_args()
    PROJ_ROOT = Path(__file__).resolve().parent.parent
    env_path = str(PROJ_ROOT / args.env)
    bot = AggressiveBot(env_file=env_path)
    setup_logging()
    bot.run()
