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
from core.mindspace import MindspaceEngine, Candle, Signal
from core.risk_manager import RiskManager
from core.news_filter import NewsFilter
from connectors.mt5_connector import MT5Connector, MT5ConnectorError
from database.mongo_client import MongoClient
from telegram.alerts import TelegramNotifier, fmt_et

logger = logging.getLogger(__name__)
trade_logger = get_logger("trade")

TF_MAP = {
    "1m": mt5.TIMEFRAME_M1,
    "5m": mt5.TIMEFRAME_M5,
    "15m": mt5.TIMEFRAME_M15,
    "30m": mt5.TIMEFRAME_M30,
    "1h": mt5.TIMEFRAME_H1,
    "4h": mt5.TIMEFRAME_H4,
    "daily": mt5.TIMEFRAME_D1,
}


class MindspaceBot:
    POLL_INTERVAL_SECONDS = 30
    TF_REFRESH_SECONDS = {
        "1m": 30,
        "5m": 60,
        "15m": 180,
        "30m": 300,
        "1h": 600,
        "4h": 1800,
        "daily": 3600,
    }
    HEARTBEAT_SECONDS = 21600

    def __init__(self, env_file: str = ".env"):
        self.env_file = env_file
        self.settings = get_settings(env_file)
        self.settings.trail_multiplier = 0.3
        self.connector = MT5Connector(settings=self.settings)
        self.engine = MindspaceEngine()
        self.risk_mgr = RiskManager(
            max_daily_loss_pct=self.settings.circuit_breaker_max_daily_loss_pct,
            max_consecutive_losses=self.settings.circuit_breaker_max_consecutive_losses,
            max_drawdown_pct=self.settings.circuit_breaker_max_drawdown_pct,
        )
        self.news_filter = (
            NewsFilter(blackout_minutes=self.settings.news_blackout_minutes)
            if self.settings.news_filter_enabled
            else None
        )
        self.telegram = TelegramNotifier(settings=self.settings)
        self.mongo = MongoClient(settings=self.settings)
        self._running = False
        self._current_date: Optional[str] = None
        self._trades_today = 0
        self._position: Optional[Dict[str, Any]] = None
        self._cached_tfs: Dict[str, pd.DataFrame] = {}
        self._last_tf_refresh: Dict[str, float] = {}
        self._last_heartbeat: float = time.time()
        self._start_time: datetime = datetime.now(timezone.utc)
        self._initial_balance: Optional[float] = None
        self._cb_alerted: bool = False
        self._current_session: Optional[str] = None
        self._no_money_cooldown_until: float = 0
        self._last_signal_time: float = 0

    def _load_tf_data(self, tf_name: str) -> None:
        try:
            self.connector.connect()
            mt5_tf = TF_MAP[tf_name]
            current_end = datetime.now(timezone.utc) + timedelta(hours=1)
            bars_needed = 500
            chunk = mt5.copy_rates_from("XAUUSD", mt5_tf, current_end, bars_needed)
            if chunk is None or len(chunk) == 0:
                logger.warning(f"No {tf_name} data returned")
                return
            df = pd.DataFrame(chunk)
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df.set_index("time", inplace=True)
            df = df[~df.index.duplicated(keep="last")][["open", "high", "low", "close", "tick_volume"]]
            self._cached_tfs[tf_name] = df
            logger.debug(f"{tf_name} data refreshed: {len(df)} bars")
        except Exception as e:
            logger.warning(f"{tf_name} load failed: {e}", exc_info=True)

    def _refresh_cached_tfs(self) -> None:
        now = time.time()
        for tf in ("1m", "5m", "15m", "30m", "1h", "4h", "daily"):
            last_refresh = self._last_tf_refresh.get(tf, 0)
            if now - last_refresh >= self.TF_REFRESH_SECONDS[tf]:
                self._load_tf_data(tf)
                self._last_tf_refresh[tf] = now
                logger.debug(f"Refreshed {tf} data")

    def _candles_from_df(self, tf: str) -> list[Candle]:
        df = self._cached_tfs.get(tf)
        if df is None or df.empty:
            return []
        candles = []
        for idx, row in df.iterrows():
            candles.append(
                Candle(
                    time=idx.to_pydatetime(),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row.get("tick_volume", 0)),
                )
            )
        return candles

    def _check_new_day(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._current_date != today:
            self._current_date = today
            self._trades_today = 0
            self._cb_alerted = False
            acct = self.connector.get_account_info()
            self.risk_mgr.start_day(today, acct["balance"])
            logger.info(f"New trading day: {today}")

    def _calc_lot_size(self, balance: float, sl_distance: float) -> float:
        if sl_distance <= 0:
            return 0.01
        risk_amount = balance * (self.settings.risk_percent / 100.0)
        pip_value = 100.0
        lot = round(risk_amount / (sl_distance * pip_value), 2)
        return max(0.01, min(lot, 10.0))

    def _close_partial(self, close_price: float, close_lots: float, reason: str, current_time: datetime) -> bool:
        pos = self._position
        is_buy = pos["type"] == "buy"
        pdiff = close_price - pos["entry"]
        if not is_buy:
            pdiff = -pdiff
        profit = round(pdiff * close_lots * 100, 2)

        ticket = pos.get("ticket")
        if ticket:
            try:
                self.connector.close_position({
                    "symbol": self.settings.symbol,
                    "ticket": ticket,
                    "volume": close_lots,
                    "type": pos["type"],
                })
            except Exception as e:
                logger.error(f"Partial close {reason} failed: {e}")
                return False

        pos["pnl"] = round(pos.get("pnl", 0) + profit, 2)
        pos["remaining_lots"] = max(0, round(pos["remaining_lots"] - close_lots, 2))
        pos["_last_price"] = close_price

        if pos["remaining_lots"] < 0.001:
            pos["exit"] = close_price

        logger.info(f"PARTIAL {reason}: {close_lots:.2f} @ {close_price:.2f} P=${profit:.2f} (cum: ${pos['pnl']:.2f})")
        trade_logger.info(
            f"PARTIAL {pos['type'].upper()} {close_lots} {pos['entry']:.2f} {close_price:.2f} {profit:.2f}",
            extra={"trade": pos, "reason": reason},
        )
        self.telegram.alert_partial(pos, reason, close_lots, close_price, profit, pos["pnl"])
        return True

    def _manage_position(self, current_time: datetime, tick: Dict[str, float]) -> None:
        if self._position is None:
            return

        pos = self._position
        is_buy = pos["type"] == "buy"
        current_price = tick["bid"] if is_buy else tick["ask"]

        result = self.engine.manage_position(
            entry_price=pos["entry"],
            direction=pos["type"],
            current_price=current_price,
            sl_price=pos["sl"],
            tp_price=pos.get("tp"),
            volume=pos["remaining_lots"],
            position_id=pos.get("trade_id", 0),
        )

        action = result["action"]
        if action == "close":
            self._close_partial(current_price, pos["remaining_lots"], "tp", current_time)
            pos["exit_reason"] = "tp"
            self._finalize_position(current_time)
        elif action == "partial_close":
            ok = self._close_partial(current_price, pos["remaining_lots"] * result["close_pct"], "tp1", current_time)
            if ok and pos["remaining_lots"] > 0:
                new_sl = result["new_sl"]
                ok = self.connector.modify_position(ticket=pos["ticket"], sl=new_sl)
                if ok:
                    pos["sl"] = new_sl
                pos["trailing_activated"] = True
        elif action == "trail":
            new_sl = result["new_sl"]
            if new_sl and abs(new_sl - pos["sl"]) >= 0.01:
                ok = self.connector.modify_position(ticket=pos["ticket"], sl=new_sl)
                if ok:
                    pos["sl"] = new_sl

        if pos["remaining_lots"] <= 0.005 and not pos.get("closed"):
            pos["remaining_lots"] = 0.0
            pos["closed"] = True
            pos.setdefault("exit", pos.get("_last_price"))
            pos["exit_reason"] = pos.get("exit_reason", "managed")
            pos["close_time"] = current_time
            self._finalize_position(current_time)

    def _finalize_position(self, current_time: datetime) -> None:
        pos = self._position
        logger.info(f"CLOSE {pos['type']} {pos['entry']:.2f} P=${pos['pnl']:.2f} ({pos.get('exit_reason', 'unknown')})")
        trade_logger.info(
            f"CLOSE {pos['type']} {pos['entry']:.2f} {pos.get('close_time')} {pos['pnl']:.2f}",
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
            "exit_reason": pos.get("exit_reason"),
            "close_time": pos.get("close_time", current_time),
            "session_date": pos.get("close_time", current_time).strftime("%Y-%m-%d"),
            "strategy": self.settings.strategy_label,
        })
        self._position = None

    def initialize(self) -> bool:
        logger.info(f"Initializing {self.settings.strategy_label} bot...")
        try:
            self.connector.connect()
            logger.info("MT5 connected")
        except MT5ConnectorError as e:
            logger.error(f"MT5 connection failed: {e}")
            self.telegram.alert_error(f"MT5 connection failed: {e}")
            return False

        if not self.mongo.connect():
            logger.warning("MongoDB unavailable")

        self._refresh_cached_tfs()

        if self.news_filter is not None:
            self.news_filter.fetch_events()

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
            self._position = {
                "type": p["type"],
                "entry": p["price_open"],
                "sl": p["sl"],
                "tp": p["tp"],
                "lot_size": p["volume"],
                "original_sl": p["sl"],
                "original_lot_size": p["volume"],
                "remaining_lots": p["volume"],
                "pnl": 0.0,
                "trade_id": str(uuid4()),
                "open_time": p["time"],
                "ticket": p["ticket"],
                "trailing_activated": False,
            }
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self._current_date = date_str
            self._trades_today = 1
            logger.info(f"Recovered orphaned position: {p['type']} {p['volume']:.2f} @ {p['price_open']:.2f}")
            self.telegram.alert_error(f"Recovered orphaned position: {p['type']} {p['volume']:.2f} @ {p['price_open']:.2f}")

        if self.telegram.health_check():
            logger.info("Telegram connected \u2014 alerts enabled")
            self.telegram._send(
                f"<b>{self.settings.strategy_label} Bot Started</b>\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"Symbol: {self.settings.symbol}\n"
                f"Balance: ${account['balance']:.2f}\n"
                f"Strategy: SMC/ICT \u2014 Mindspace\n"
                f"Max/Day: {self.settings.max_trades_per_day} | Trail: 0.3x\n"
                f"Running 24/5 Mon 00:00 to Fri 17:00 UTC\n"
                f"Time: {fmt_et(fmt='%I:%M %p')}"
            )
        else:
            logger.warning("Telegram unreachable \u2014 alerts will not be delivered.")

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
            self.telegram._send(f"\u26a0\ufe0f <b>{self.settings.strategy_label} Bot Stopped</b>")
        self.mongo.disconnect()
        self.connector.disconnect()
        self._running = False

    def run(self) -> None:
        if not self.initialize():
            logger.error("Initialization failed, exiting")
            return

        self._running = True
        logger.info(f"{self.settings.strategy_label} bot started")

        try:
            while self._running:
                now = datetime.now(timezone.utc)

                if SessionValidator.is_friday_close(now):
                    secs_until_monday = (
                        SessionValidator.next_monday_utc(now) - now
                    ).total_seconds()
                    logger.info(f"Friday close \u2014 sleeping {secs_until_monday/3600:.1f}h")
                    if self._position is not None:
                        try:
                            self.connector.close_position({
                                "symbol": self.settings.symbol,
                                "ticket": self._position["ticket"],
                                "volume": self._position["remaining_lots"],
                                "type": self._position["type"],
                            })
                            logger.info("Closed open position before Friday shutdown")
                        except Exception as e:
                            logger.error(f"Failed to close before Friday shutdown: {e}")
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
                    self._last_tf_refresh.clear()
                    self._current_date = None
                    continue

                self._check_new_day()

                if not SessionValidator.is_valid_session_day(now) or not SessionTimes().is_trade_window(now):
                    time.sleep(60)
                    continue

                current_session = SessionTimes().get_active_session(now)
                if current_session is None:
                    time.sleep(60)
                    continue

                if current_session != self._current_session:
                    self._current_session = current_session
                    self._cb_alerted = False
                    self.risk_mgr.start_session(current_session)

                self._refresh_cached_tfs()

                candles_by_tf = {
                    tf: self._candles_from_df(tf)
                    for tf in ("1m", "5m", "15m", "1h", "4h", "daily")
                }
                self.engine.update_markets(candles_by_tf)

                if self._position is not None:
                    try:
                        tick = self.connector.get_tick()
                    except Exception as e:
                        logger.warning(f"Failed to get tick: {e}")
                        time.sleep(10)
                        continue
                    self._manage_position(now, tick)
                    time.sleep(self.POLL_INTERVAL_SECONDS)
                    continue

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

                if self.settings.mt5_login and acct["login"] != self.settings.mt5_login:
                    logger.warning(f"Account reverted to {acct['login']}, re-logging")
                    mt5.login(
                        login=self.settings.mt5_login,
                        password=self.settings.mt5_password,
                        server=self.settings.mt5_server if self.settings.mt5_server else None,
                    )
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

                if self._trades_today >= self.settings.max_trades_per_day:
                    logger.debug(f"Max trades ({self.settings.max_trades_per_day}) reached for today")
                    time.sleep(60)
                    continue

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

                signal = self.engine.get_signal()
                if signal is not None:
                    price = tick["ask"] if signal.direction == "buy" else tick["bid"]
                    sl_distance = abs(signal.sl_high - signal.sl_low)
                    if sl_distance < 0.30:
                        if signal.direction == "buy":
                            sl = price - 0.30
                        else:
                            sl = price + 0.30
                        sl_distance = 0.30
                    else:
                        sl = signal.sl_low if signal.direction == "buy" else signal.sl_high

                    lot_size = self._calc_lot_size(acct["balance"], sl_distance)
                    if lot_size < 0.01:
                        logger.info(f"Lot size too small ({lot_size}), skipping")
                        time.sleep(10)
                        continue

                    mt5_type = mt5.ORDER_TYPE_BUY if signal.direction == "buy" else mt5.ORDER_TYPE_SELL
                    tp = signal.tp_price

                    try:
                        order = self.connector.place_order(
                            symbol=self.settings.symbol,
                            order_type=mt5_type,
                            volume=lot_size,
                            price=price,
                            sl=sl,
                            tp=tp,
                            comment=self.settings.strategy_label[:4].upper(),
                        )
                        if not order.get("ticket"):
                            logger.error("Order placed but got no ticket")
                            self.telegram.alert_error("Order placed but got no ticket")
                            continue

                        filled_lot = order.get("volume", lot_size)
                        self._trades_today += 1
                        trade_id = str(uuid4())
                        self._position = {
                            "type": signal.direction,
                            "entry": order["price"],
                            "sl": order.get("sl", sl),
                            "tp": order.get("tp", tp),
                            "original_sl": order.get("sl", sl),
                            "lot_size": filled_lot,
                            "original_lot_size": filled_lot,
                            "remaining_lots": filled_lot,
                            "pnl": 0.0,
                            "trade_id": trade_id,
                            "open_time": now,
                            "ticket": order["ticket"],
                            "trailing_activated": False,
                        }
                        self.mongo.save_trade({
                            "trade_id": trade_id,
                            "symbol": self.settings.symbol,
                            "signal_type": signal.direction,
                            "entry_price": order["price"],
                            "stop_loss": order.get("sl", sl),
                            "take_profit": tp,
                            "lot_size": filled_lot,
                            "level_type": signal.level_type,
                            "source_tf": signal.tf,
                            "session_date": now.strftime("%Y-%m-%d"),
                            "open_time": now,
                            "strategy": self.settings.strategy_label,
                        })
                        logger.info(
                            f"{self.settings.strategy_label.upper()} TRADE {signal.direction.upper()} "
                            f"{filled_lot} @ {order['price']:.2f} "
                            f"SL={sl:.2f} TP={tp if tp else 'N/A'} "
                            f"type={signal.level_type} tf={signal.tf}"
                        )
                        trade_logger.info(
                            f"OPEN {signal.direction.upper()} {filled_lot} "
                            f"{order['price']:.2f} {sl:.2f} {tp if tp else 0:.2f}",
                            extra={"trade": self._position},
                        )
                        self._last_signal_time = time.time()
                        self.telegram.alert_trade_open(self._position)
                    except MT5ConnectorError as e:
                        logger.error(f"Order failed: {e}")
                        if "10019" in str(e) or "money" in str(e).lower():
                            self._no_money_cooldown_until = time.time() + 3600
                        self.telegram.alert_error(f"Order failed: {e}")

                if time.time() - self._last_heartbeat > self.HEARTBEAT_SECONDS:
                    self._last_heartbeat = time.time()
                    acct = self.connector.get_account_info()
                    pos_status = "Open" if self._position else "None"
                    self.telegram.alert_heartbeat(
                        f"Balance: ${acct['balance']:.2f}\n"
                        f"Equity: ${acct.get('equity', 0):.2f}\n"
                        f"Running since: {fmt_et(self._start_time, '%Y-%m-%d %I:%M %p')}\n"
                        f"Position: {pos_status} | Today: {self._trades_today}/{self.settings.max_trades_per_day}"
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
    parser = argparse.ArgumentParser(description="Trader Mindspace Live Bot")
    parser.add_argument("--env", type=str, default=".env", help="Env file to load (default: .env)")
    args = parser.parse_args()
    PROJ_ROOT = Path(__file__).resolve().parent.parent
    env_path = str(PROJ_ROOT / args.env)
    bot = MindspaceBot(env_file=env_path)
    setup_logging()
    bot.run()
