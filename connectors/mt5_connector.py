import os
import subprocess
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

import MetaTrader5 as mt5
import pandas as pd
import numpy as np

from config.settings import get_settings

logger = logging.getLogger(__name__)


class MT5ConnectorError(Exception):
    pass


class MT5Connector:
    def __init__(self):
        self.settings = get_settings()
        self._connected = False
        self._account_info: Optional[Dict[str, Any]] = None

    def connect(self) -> bool:
        if self._connected:
            return True

        # Initialize terminal connection (any account)
        init = mt5.initialize(path=self.settings.mt5_path, timeout=30000)
        if not init:
            init = mt5.initialize()
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
                raise MT5ConnectorError(
                    f"MT5 login failed for {self.settings.mt5_login}: {error}"
                )
            logger.info(f"Logged into account {self.settings.mt5_login}")

        self._ensure_auto_trading_enabled()

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
    def _ensure_auto_trading_enabled() -> None:
        term = mt5.terminal_info()
        if term is not None and term.trade_allowed:
            return

        logger.warning("AutoTrading disabled, attempting to enable via PowerShell...")
        try:
            subprocess.run([
                "powershell",
                "-Command",
                "$w = New-Object -ComObject wscript.shell; "
                "try { $w.AppActivate((Get-Process terminal64 | Where-Object { $_.MainWindowTitle -match 'MetaTrader' } | Select-Object -First 1).MainWindowTitle); "
                "Start-Sleep -Milliseconds 800; $w.SendKeys('%t'); Start-Sleep -Seconds 2 } catch {}"
            ], capture_output=True, timeout=10)
            time.sleep(2)
            term = mt5.terminal_info()
            if term is not None and term.trade_allowed:
                logger.info("AutoTrading enabled successfully")
            else:
                logger.warning("PowerShell SendKeys did not enable AutoTrading")
        except Exception as e:
            logger.error(f"Failed to enable AutoTrading: {e}")

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
        return fn(*args, **kwargs)

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
                margin_per_lot = (tick["ask"] * 100) / acct.leverage
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
            logger.error(
                f"Order rejected: retcode={result.retcode}, "
                f"comment={result.comment}"
            )
            raise MT5ConnectorError(
                f"Order rejected: retcode={result.retcode}, "
                f"comment={result.comment}"
            )

        # Find actual position ticket from MT5 for downstream operations
        ticket = result.order if result.order != 0 else result.deal
        try:
            positions = mt5.positions_get(symbol=symbol)
            if positions:
                matching = [p for p in positions if p.magic == magic and abs(p.price_open - result.price) < 1.0]
                if matching:
                    matching.sort(key=lambda p: p.time, reverse=True)
                    ticket = matching[0].ticket
        except Exception:
            pass
        logger.info(
            f"Order placed: {order_type_str} {volume} {symbol} "
            f"@{result.price}, SL={request['sl']:.2f}, TP={request['tp']:.2f}, "
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
            "sl": request["sl"],
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

        result = mt5.order_send(request)
        if result is not None and result.retcode in (0, 10009):
            logger.info(f"Position closed: {ticket} @ {price} deal={result.deal} retcode={result.retcode}")
            return {
                "order": result.order,
                "deal": result.deal,
                "price": result.price,
            }

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

    def get_position_close_from_history(self, ticket: int) -> Optional[Dict[str, Any]]:
        from_dt = datetime.now(timezone.utc) - timedelta(days=7)
        to_dt = datetime.now(timezone.utc)
        deals = mt5.history_deals_get(from_dt, to_dt)
        if deals is None or len(deals) == 0:
            logger.warning(f"No deals found in history for ticket {ticket}")
            return None
        exit_deals = []
        for d in deals:
            if d.entry == 1:
                exit_deals.append(d)
        matching = [d for d in exit_deals if d.position_id == ticket]
        if not matching:
            sample_pos_ids = [d.position_id for d in exit_deals[:5]] if exit_deals else []
            logger.warning(
                f"No closing deal found for ticket {ticket}. "
                f"Sample position_ids from history: {sample_pos_ids}"
            )
            # Fallback: try matching by any deal with this ticket or position_id
            for d in deals:
                if d.ticket == ticket or d.position_id == ticket:
                    logger.info(f"Found deal by ticket/position fallback: deal={d.ticket} pos_id={d.position_id} entry={d.entry} profit={d.profit}")
                    break
            return None
        exit_deals = matching
        total_profit = sum(d.profit for d in exit_deals)
        last = exit_deals[-1]
        return {
            "price": last.price,
            "profit": total_profit,
            "volume": sum(d.volume for d in exit_deals),
            "time": datetime.fromtimestamp(last.time, tz=timezone.utc),
        }

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
