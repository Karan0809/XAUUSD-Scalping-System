import os
import subprocess
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

import MetaTrader5 as mt5
import pandas as pd
import numpy as np

try:
    import win32gui
    import win32com.client
    import win32con
    _HAS_PYWIN32 = True
except ImportError:
    _HAS_PYWIN32 = False

from config.settings import get_settings

logger = logging.getLogger(__name__)


class MT5ConnectorError(Exception):
    pass


class MT5Connector:
    def __init__(self, settings: Optional[Any] = None, env_file: Optional[str] = None):
        self.settings = settings if settings is not None else get_settings(env_file)
        self._connected = False
        self._account_info: Optional[Dict[str, Any]] = None

    def connect(self) -> bool:
        if self._connected:
            return True

        MT5Connector._pre_enable_autotrading_config(self.settings.mt5_path)
        # Initialize terminal connection (any account)
        init = MT5Connector._initialize_mt5(
            self.settings.mt5_path,
            self.settings.mt5_portable,
        )
        if not init:
            init = MT5Connector._initialize_mt5()
        if not init:
            error = mt5.last_error()
            logger.error(f"MT5 initialize failed: {error}")
            raise MT5ConnectorError(f"MT5 initialize failed: {error}")

        # Explicitly login with env-file credentials if provided
        if self.settings.mt5_login and self.settings.mt5_password:
            logged_in = mt5.login(
                login=self.settings.mt5_login,
                password=self.settings.mt5_password,
                server=self.settings.mt5_server if self.settings.mt5_server else None,
            )
            if not logged_in:
                error = mt5.last_error()
                mt5.shutdown()
                raise MT5ConnectorError(
                    f"MT5 login failed for {self.settings.mt5_login}: {error}"
                )
            logger.info(f"Logged into account {self.settings.mt5_login}")

        if not MT5Connector._ensure_auto_trading_enabled(self.settings.mt5_path, self.settings):
            logger.warning("AutoTrading still disabled — trying forced config modification...")
            mt5.shutdown()
            MT5Connector._enable_autotrading_via_config(self.settings.mt5_path)
            init = MT5Connector._initialize_mt5(
                self.settings.mt5_path,
                self.settings.mt5_portable,
            )
            if not init:
                init = MT5Connector._initialize_mt5()
            if init and self.settings.mt5_login and self.settings.mt5_password:
                mt5.login(
                    login=self.settings.mt5_login,
                    password=self.settings.mt5_password,
                    server=self.settings.mt5_server if self.settings.mt5_server else None,
                )
            MT5Connector._ensure_auto_trading_enabled(self.settings.mt5_path, self.settings)

        self._connected = True
        info = mt5.account_info()
        if info is not None:
            self._account_info = {
                "login": info.login,
                "balance": info.balance,
                "equity": info.equity,
                "margin": info.margin,
                "margin_free": info.margin_free,
                "currency": info.currency,
            }
            logger.info(
                f"Connected to MT5 - Login: {info.login}, "
                f"Balance: {info.balance:.2f} {info.currency}"
            )
        return True

    @staticmethod
    def _as_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return False

    @staticmethod
    def _initialize_mt5(mt5_path: Optional[str] = None, portable: object = False) -> bool:
        kwargs: Dict[str, Any] = {"timeout": 30000}
        if MT5Connector._as_bool(portable):
            kwargs["portable"] = True

        try:
            if mt5_path:
                return bool(mt5.initialize(path=mt5_path, **kwargs))
            return bool(mt5.initialize(**kwargs))
        except TypeError:
            kwargs.pop("portable", None)
            if mt5_path:
                return bool(mt5.initialize(path=mt5_path, **kwargs))
            return bool(mt5.initialize(**kwargs))

    @staticmethod
    def _ensure_auto_trading_enabled(mt5_path: Optional[str] = None, settings: Optional[Any] = None) -> bool:
        term = mt5.terminal_info()
        if term is not None and term.trade_allowed:
            return True

        for attempt in range(3):
            logger.warning(f"AutoTrading disabled (attempt {attempt + 1}/3), enabling...")

            # Try config modification + restart first (works on headless servers)
            if MT5Connector._ensure_autotrading_via_config_with_restart(mt5_path, settings):
                return True

            # Fallback: window-based methods (requires GUI)
            if _HAS_PYWIN32:
                MT5Connector._ensure_terminal_window(mt5_path, settings)
                MT5Connector._enable_autotrading_pywin32(mt5_path, settings)
                time.sleep(2)
                term = mt5.terminal_info()
                if term is not None and term.trade_allowed:
                    logger.info("AutoTrading enabled via pywin32")
                    return True

            MT5Connector._ensure_terminal_window(mt5_path, settings)
            MT5Connector._enable_autotrading_powershell()
            time.sleep(2)
            term = mt5.terminal_info()
            if term is not None and term.trade_allowed:
                logger.info("AutoTrading enabled via PowerShell")
                return True

            if attempt < 2:
                time.sleep(3 * (attempt + 1))

        logger.warning(
            "Could not enable AutoTrading after 3 attempts. "
            "Enable it manually: MT5 → Tools → Options → Expert Advisors "
            "→ check 'Allow Automated Trading'"
        )
        return False

    @staticmethod
    def _check_autotrading_enabled() -> bool:
        term = mt5.terminal_info()
        return term is not None and term.trade_allowed

    @staticmethod
    def _pre_enable_autotrading_config(mt5_path: Optional[str] = None) -> None:
        MT5Connector._enable_autotrading_via_config(mt5_path)

    @staticmethod
    def _enable_autotrading_via_config(mt5_path: Optional[str] = None) -> bool:
        modified = False
        try:
            for candidate, exact_terminal in MT5Connector._autotrading_config_candidates(mt5_path):
                with open(candidate, 'r', encoding='utf-8-sig') as f:
                    content = f.read()

                if mt5_path and not exact_terminal and mt5_path not in content:
                    continue

                new_content = content.replace('AutoTrading=0', 'AutoTrading=1')
                if new_content != content:
                    with open(candidate, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    logger.info(f"Set AutoTrading=1 in {candidate}")
                    modified = True
        except Exception as e:
            logger.warning(f"Config file modification failed: {e}")

        return modified

    @staticmethod
    def _autotrading_config_candidates(mt5_path: Optional[str] = None) -> List[Tuple[str, bool]]:
        roots: List[Tuple[str, bool]] = []
        seen = set()

        if mt5_path:
            install_dir = os.path.dirname(mt5_path)
            if install_dir and os.path.isdir(install_dir):
                roots.append((install_dir, True))

        appdata = os.environ.get('APPDATA', '')
        terminal_dir = os.path.join(appdata, 'MetaQuotes', 'Terminal') if appdata else ''
        if terminal_dir and os.path.isdir(terminal_dir):
            for entry in os.listdir(terminal_dir):
                entry_path = os.path.join(terminal_dir, entry)
                if os.path.isdir(entry_path):
                    roots.append((entry_path, False))

        for root, exact_terminal in roots:
            for candidate in (
                os.path.join(root, 'config', 'origin.cfg'),
                os.path.join(root, 'origin.cfg'),
                os.path.join(root, 'config', 'terminal.cfg'),
            ):
                if candidate in seen or not os.path.isfile(candidate):
                    continue
                seen.add(candidate)
                yield candidate, exact_terminal

    @staticmethod
    def _enum_mt5_windows(hwnd: int, results: List[int]) -> None:
        title = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        if "MetaTrader" in title or "MT5" in title or cls == "MetaTrader":
            results.append(hwnd)

    @staticmethod
    def _ensure_autotrading_via_config_with_restart(
        mt5_path: Optional[str] = None,
        settings: Optional[Any] = None,
    ) -> bool:
        """Set AutoTrading=1 in config, shutdown MT5, restart. Returns True once trading_allowed."""
        modified = MT5Connector._enable_autotrading_via_config(mt5_path)
        if modified:
            logger.info("Config modified — restarting MT5 to apply...")
            mt5.shutdown()
            time.sleep(2)
            s = settings if settings is not None else get_settings()
            portable = s.mt5_portable
            init = MT5Connector._initialize_mt5(mt5_path, portable)
            if not init:
                init = MT5Connector._initialize_mt5()
            if init:
                if s.mt5_login and s.mt5_password:
                    mt5.login(
                        login=s.mt5_login,
                        password=s.mt5_password,
                        server=s.mt5_server if s.mt5_server else None,
                    )
                time.sleep(1)
                term = mt5.terminal_info()
                if term is not None and term.trade_allowed:
                    logger.info("AutoTrading enabled after config+restart")
                    return True
        return False

    @staticmethod
    def _ensure_terminal_window(mt5_path: Optional[str] = None, settings: Optional[Any] = None) -> None:
        if _HAS_PYWIN32:
            try:
                hwnds: List[int] = []
                win32gui.EnumWindows(MT5Connector._enum_mt5_windows, hwnds)
                if hwnds:
                    for hwnd in hwnds:
                        placement = win32gui.GetWindowPlacement(hwnd)
                        if placement[1] == win32con.SW_SHOWMINIMIZED:
                            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                        win32gui.SetForegroundWindow(hwnd)
                    return
            except Exception:
                pass
        if not mt5_path:
            s = settings if settings is not None else get_settings()
            mt5_path = s.mt5_path
        if isinstance(mt5_path, str) and os.path.isfile(mt5_path):
            subprocess.Popen([mt5_path])
            time.sleep(5)

    @staticmethod
    def _enable_autotrading_pywin32(
        mt5_path: Optional[str] = None,
        settings: Optional[Any] = None,
    ) -> None:
        try:
            hwnds: List[int] = []
            win32gui.EnumWindows(MT5Connector._enum_mt5_windows, hwnds)
            if not hwnds:
                MT5Connector._ensure_terminal_window(mt5_path, settings)
                win32gui.EnumWindows(MT5Connector._enum_mt5_windows, hwnds)
            if not hwnds:
                logger.warning("No MetaTrader window found after launch")
                return

            shell = win32com.client.Dispatch("WScript.Shell")
            for hwnd in hwnds:
                try:
                    placement = win32gui.GetWindowPlacement(hwnd)
                    if placement[1] == win32con.SW_SHOWMINIMIZED:
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                        time.sleep(0.5)

                    win32gui.SetForegroundWindow(hwnd)
                    time.sleep(0.3)
                    shell.SendKeys("%t")
                    time.sleep(1)
                except Exception as e:
                    logger.warning(f"SendKeys failed for hwnd {hwnd}: {e}")
        except Exception as e:
            logger.error(f"pywin32 auto-trading enable failed: {e}")

    @staticmethod
    def _enable_autotrading_powershell() -> None:
        logger.warning("Falling back to PowerShell SendKeys...")
        try:
            subprocess.run([
                "powershell",
                "-Command",
                "$w = New-Object -ComObject wscript.shell; "
                "$titles = @(); "
                "Get-Process terminal64,metatrader* -ErrorAction SilentlyContinue | "
                "Where-Object { $_.MainWindowTitle -match 'MetaTrader|MT5' } | "
                "ForEach-Object { $titles += $_.MainWindowTitle }; "
                "if ($titles.Count -eq 0) { "
                "  Get-Process | Where-Object { $_.MainWindowTitle -match 'MetaTrader|MT5' } | "
                "  ForEach-Object { $titles += $_.MainWindowTitle } "
                "}; "
                "foreach ($t in $titles) { "
                "  try { "
                "    $null = $w.AppActivate($t); "
                "    Start-Sleep -Milliseconds 500; "
                "    $w.SendKeys('%t'); "
                "    Start-Sleep -Seconds 1; "
                "  } catch {} "
                "}"
            ], capture_output=True, timeout=15)
        except Exception as e:
            logger.error(f"PowerShell auto-trading enable failed: {e}")

    def disconnect(self) -> None:
        if self._connected:
            mt5.shutdown()
            self._connected = False
            logger.info("Disconnected from MT5")

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_account_info(self) -> Dict[str, Any]:
        if not self._connected:
            self.connect()
        info = mt5.account_info()
        if info is None:
            raise MT5ConnectorError("Failed to get account info")
        return {
            "login": info.login,
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "margin_free": info.margin_free,
            "margin_level": info.margin_level,
            "currency": info.currency,
            "leverage": info.leverage,
        }

    def _call_with_retry(self, fn, *args, **kwargs):
        for attempt in range(2):
            try:
                result = fn(*args, **kwargs)
                if result is not None:
                    return result
            except Exception as e:
                logger.warning(f"MT5 call failed: {e}")
            if attempt == 0:
                logger.warning("MT5 call failed, reconnecting...")
                self.disconnect()
                self.connect()
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logger.warning(f"Final MT5 call attempt failed: {e}")
            raise

    def get_rates(
        self,
        symbol: str = "XAUUSD",
        timeframe: int = mt5.TIMEFRAME_M5,
        count: int = 500,
        from_date: Optional[datetime] = None,
    ) -> pd.DataFrame:
        if not self._connected:
            self.connect()

        if from_date is not None:
            rates = self._call_with_retry(mt5.copy_rates_from, symbol, timeframe, from_date, count)
        else:
            rates = self._call_with_retry(mt5.copy_rates_from_pos, symbol, timeframe, 0, count)

        if rates is None or len(rates) == 0:
            raise MT5ConnectorError(
                f"Failed to get rates for {symbol}: {mt5.last_error()}"
            )

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        df.rename(
            columns={
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "tick_volume": "volume",
                "spread": "spread",
            },
            inplace=True,
        )
        return df[["open", "high", "low", "close", "volume", "spread", "real_volume"]]

    def get_rates_range(
        self,
        symbol: str,
        timeframe: int,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        if not self._connected:
            self.connect()

        rates = self._call_with_retry(mt5.copy_rates_range, symbol, timeframe, start, end)
        if rates is None or len(rates) == 0:
            raise MT5ConnectorError(
                f"Failed to get rates range for {symbol}: {mt5.last_error()}"
            )

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        return df[["open", "high", "low", "close", "tick_volume", "spread"]]

    def _point(self, symbol: str = "XAUUSD") -> float:
        info = mt5.symbol_info(symbol)
        return info.point if info else 0.01

    def get_tick(self, symbol: str = "XAUUSD") -> Dict[str, float]:
        tick = self._call_with_retry(mt5.symbol_info_tick, symbol)
        if tick is None:
            raise MT5ConnectorError(f"Cannot get tick for {symbol}")
        return {"bid": tick.bid, "ask": tick.ask, "last": tick.last, "spread": round((tick.ask - tick.bid) / self._point(symbol), 1)}

    def get_margin_rate(self, symbol: str = "XAUUSD") -> float:
        info = self._call_with_retry(mt5.symbol_info, symbol)
        if info is None:
            raise MT5ConnectorError(f"Cannot get symbol info for {symbol}")
        margin_per_lot = info.margin_initial
        if margin_per_lot <= 0:
            margin_per_lot = info.margin_maintenance
        if margin_per_lot <= 0:
            tick = self.get_tick(symbol)
            acct = mt5.account_info()
            if acct:
                margin_per_lot = (tick["ask"] * 100) / max(acct.leverage, 1)
            else:
                margin_per_lot = tick["ask"] * 100
        return margin_per_lot

    def get_symbol_info(self, symbol: str = "XAUUSD") -> Dict[str, Any]:
        if not self._connected:
            self.connect()
        info = mt5.symbol_info(symbol)
        if info is None:
            raise MT5ConnectorError(
                f"Symbol {symbol} not found: {mt5.last_error()}"
            )
        return {
            "name": info.name,
            "digits": info.digits,
            "point": info.point,
            "trade_mode": info.trade_mode,
            "trade_stops_level": info.trade_stops_level,
            "trade_freeze_level": info.trade_freeze_level,
            "spread": info.spread,
            "spread_float": info.spread_float,
            "swap_long": info.swap_long,
            "swap_short": info.swap_short,
            "margin_initial": info.margin_initial,
            "margin_maintenance": info.margin_maintenance,
            "currency_base": info.currency_base,
            "currency_profit": info.currency_profit,
            "trade_tick_value": info.trade_tick_value,
            "trade_tick_size": info.trade_tick_size,
            "trade_contract_size": info.trade_contract_size,
        }

    def place_order(
        self,
        symbol: str,
        order_type: int,
        volume: float,
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        comment: str = "",
        magic: int = 202402,
    ) -> Dict[str, Any]:
        if not self._connected:
            self.connect()

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise MT5ConnectorError(f"Cannot get tick for {symbol}")

        order_type_str = "buy" if order_type == mt5.ORDER_TYPE_BUY else "sell"
        info = mt5.symbol_info(symbol)
        if info is None:
            raise MT5ConnectorError(f"Cannot get symbol info for {symbol}")
        point = info.point
        stops_level = info.trade_stops_level * point if info.trade_stops_level > 0 else 0
        min_stop = stops_level if stops_level > 0 else max(point, 0.10)

        if sl is not None:
            if order_type == mt5.ORDER_TYPE_BUY:
                sl = min(sl, tick.bid - min_stop)
            else:
                sl = max(sl, tick.ask + min_stop)

        if tp is not None:
            if order_type == mt5.ORDER_TYPE_BUY:
                tp = max(tp, sl + min_stop)
            else:
                tp = min(tp, sl - min_stop)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price if price else (tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid),
            "sl": sl if sl else 0.0,
            "tp": tp if tp else 0.0,
            "deviation": self.settings.max_slippage,
            "magic": magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        for attempt in range(2):
            result = mt5.order_send(request)
            if result is None:
                error = mt5.last_error()
                logger.error(f"Order send failed: {error}")
                raise MT5ConnectorError(f"Order send failed: {error}")
            if result.retcode in (0, 1, 10008, 10009):
                break
            if attempt == 0 and result.retcode == 10016:
                logger.warning(f"Order rejected (10016), retrying with fresh tick...")
                time.sleep(0.5)
                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    raise MT5ConnectorError("Cannot get tick on retry")
                fresh_price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
                old_price = request["price"]
                price_shift = fresh_price - old_price
                request["price"] = fresh_price
                if order_type == mt5.ORDER_TYPE_BUY:
                    fresh_sl = request.get("sl", fresh_price - 0.20) + price_shift
                    fresh_tp = request.get("tp", fresh_price + 0.20) + price_shift
                    request["sl"] = min(fresh_sl, tick.bid - min_stop)
                    request["tp"] = max(fresh_tp, request["sl"] + min_stop)
                else:
                    fresh_sl = request.get("sl", fresh_price + 0.20) + price_shift
                    fresh_tp = request.get("tp", fresh_price - 0.20) + price_shift
                    request["sl"] = max(fresh_sl, tick.ask + min_stop)
                    request["tp"] = min(fresh_tp, request["sl"] - min_stop)
                continue
            if result.retcode == 10027:
                logger.warning(f"Order rejected (10027 — AutoTrading disabled), re-enabling...")
                re_enabled = MT5Connector._ensure_auto_trading_enabled(
                    self.settings.mt5_path,
                    self.settings,
                )
                time.sleep(1)
                if not re_enabled:
                    logger.error("Could not enable AutoTrading — aborting order")
                    raise MT5ConnectorError("AutoTrading cannot be enabled")
                continue
            logger.error(
                f"Order rejected: retcode={result.retcode}, "
                f"comment={result.comment}"
            )
            raise MT5ConnectorError(
                f"Order rejected: retcode={result.retcode}, "
                f"comment={result.comment}"
            )

        # Validate result — reject ghost orders with zero price or ticket
        if result.price == 0.0 or (result.order == 0 and result.deal == 0):
            logger.error(
                f"Order sent but got zero price/ticket: "
                f"retcode={result.retcode} price={result.price} "
                f"deal={result.deal} order={result.order}"
            )
            raise MT5ConnectorError("Order produced invalid result (zero price/ticket)")

        # Find actual position ticket from MT5 — retry since position may not appear immediately
        ticket = result.order if result.order != 0 else result.deal
        actual_sl = request["sl"]
        for p_attempt in range(3):
            try:
                positions = mt5.positions_get(symbol=symbol)
                if positions:
                    matching = [p for p in positions if p.magic == magic and abs(p.price_open - result.price) < 0.5]
                    if matching:
                        matching.sort(key=lambda p: p.time, reverse=True)
                        ticket = matching[0].ticket
                        actual_sl = matching[0].sl
                        if abs(round(actual_sl, 2) - round(request["sl"], 2)) > 0.05:
                            logger.warning(
                                f"Broker SL mismatch: requested={request['sl']:.2f} actual={actual_sl:.2f} "
                                f"(diff={abs(actual_sl - request['sl']):.2f})"
                            )
                        break
            except Exception:
                pass
            if p_attempt < 2:
                time.sleep(0.5)
        logger.info(
            f"Order placed: {order_type_str} {volume} {symbol} "
            f"@{result.price}, SL={request['sl']:.2f}, actual_SL={actual_sl:.2f}, "
            f"TP={request['tp']:.2f}, "
            f"deal={result.deal} order={result.order} pos_ticket={ticket}"
        )
        return {
            "ticket": ticket,
            "deal": result.deal,
            "order": result.order,
            "price": result.price,
            "volume": result.volume or volume,
            "type": order_type_str,
            "comment": comment,
            "sl": actual_sl,
            "tp": request["tp"],
        }

    def close_position(
        self, position: Dict[str, Any]
    ) -> Dict[str, Any]:
        if not self._connected:
            self.connect()

        symbol = position["symbol"]
        ticket = position["ticket"]
        volume = position["volume"]
        order_type = (
            mt5.ORDER_TYPE_SELL
            if position["type"] == "buy"
            else mt5.ORDER_TYPE_BUY
        )

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise MT5ConnectorError(f"Cannot get tick for {symbol}")

        price = tick.bid if order_type == mt5.ORDER_TYPE_BUY else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "position": ticket,
            "price": price,
            "deviation": self.settings.max_slippage,
            "magic": position.get("magic", 202402),
            "comment": "close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        for close_attempt in range(2):
            result = mt5.order_send(request)
            if result is not None:
                if result.retcode in (0, 10009):
                    logger.info(f"Position closed: {ticket} @ {price} deal={result.deal} retcode={result.retcode}")
                    return {
                        "order": result.order,
                        "deal": result.deal,
                        "price": result.price,
                    }
                if result.retcode == 10027:
                    logger.warning("Close rejected (AutoTrading disabled), re-enabling...")
                    MT5Connector._ensure_auto_trading_enabled(
                        self.settings.mt5_path,
                        self.settings,
                    )
                    time.sleep(1)
                    continue
            break

        # IOC may not be supported for closes; retry without type_filling
        if result and result.retcode == 10013 and request.get("type_filling") is not None:
            logger.warning(f"IOC close rejected ({result.retcode}), retrying without type_filling")
            request.pop("type_filling")
            result = mt5.order_send(request)
            if result is not None and result.retcode in (0, 10009):
                logger.info(f"Position closed (no filling mode): {ticket} @ {price} deal={result.deal}")
                return {
                    "order": result.order,
                    "deal": result.deal,
                    "price": result.price,
                }

        # Stored ticket may be stale — try closing any open position for this symbol
        if result and result.retcode == 10013:
            positions = mt5.positions_get(symbol=symbol)
            if positions:
                for p in positions:
                    actual_ticket = p.ticket
                    if actual_ticket != ticket:
                        logger.warning(f"Retrying close with actual ticket {actual_ticket} (stored was {ticket})")
                        request["position"] = actual_ticket
                        result = mt5.order_send(request)
                        if result is not None and result.retcode in (0, 10009):
                            logger.info(f"Position closed via actual ticket: {actual_ticket} @ {price} deal={result.deal}")
                            return {
                                "order": result.order,
                                "deal": result.deal,
                                "price": result.price,
                            }
                    break  # try first open position for this symbol

        error = mt5.last_error()
        logger.error(f"Close position failed: retcode={result.retcode if result is not None else 'None'}, error={error}")
        raise MT5ConnectorError(f"Close position failed: retcode={result.retcode if result is not None else 'None'}, error={error}")

    def get_positions(self, symbol: str = "XAUUSD") -> List[Dict[str, Any]]:
        if not self._connected:
            self.connect()
        positions = mt5.positions_get(symbol=symbol)
        if positions is None:
            return []
        result = []
        for pos in positions:
            result.append({
                "ticket": pos.ticket,
                "symbol": pos.symbol,
                "type": "buy" if pos.type == 0 else "sell",
                "volume": pos.volume,
                "price_open": pos.price_open,
                "sl": pos.sl,
                "tp": pos.tp,
                "profit": pos.profit,
                "swap": pos.swap,
                "comment": pos.comment,
                "magic": pos.magic,
                "time": datetime.fromtimestamp(pos.time, tz=timezone.utc),
            })
        return result

    def get_position_close_from_history(
        self, ticket: int, symbol: Optional[str] = None, entry_price: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        from_dt = datetime.now() - timedelta(days=7)
        to_dt = datetime.now()
        for attempt in range(3):
            try:
                deals = mt5.history_deals_get(from_dt, to_dt)
            except Exception as e:
                logger.warning(f"history_deals_get failed for ticket {ticket}: {e} (attempt {attempt+1}/3)")
                time.sleep(2)
                continue
            if deals is None or len(deals) == 0:
                logger.warning(f"No deals found in history for ticket {ticket} (attempt {attempt+1}/3)")
                time.sleep(2)
                continue
            exit_deals = [d for d in deals if d.entry == 1]
            matching = [d for d in exit_deals if d.position_id == ticket]
            if matching:
                total_profit = sum(d.profit for d in matching)
                last = matching[-1]
                return {
                    "price": last.price,
                    "profit": total_profit,
                    "volume": sum(d.volume for d in matching),
                    "time": datetime.fromtimestamp(last.time, tz=timezone.utc),
                }
            sample_pos_ids = [d.position_id for d in exit_deals[:5]] if exit_deals else []
            logger.warning(
                f"No closing deal found for ticket {ticket} (attempt {attempt+1}/3). "
                f"Sample position_ids from history: {sample_pos_ids}"
            )
            if attempt < 2:
                time.sleep(2)
        return None

    def get_open_positions_count(self, symbol: str = "XAUUSD") -> int:
        return len(self.get_positions(symbol))

    def modify_position(
        self,
        ticket: int,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> bool:
        if not self._connected:
            self.connect()

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl": sl if sl else 0.0,
            "tp": tp if tp else 0.0,
        }

        result = mt5.order_send(request)
        if result is not None and result.retcode in (0, 10009):
            logger.info(f"Position {ticket} modified: SL={sl}, TP={tp} retcode={result.retcode}")
            return True
        logger.error(
            f"Modify position failed: retcode={result.retcode if result is not None else 'None'}"
        )
        error = mt5.last_error()
        if error:
            logger.error(f"MT5 error: {error}")
        return False
