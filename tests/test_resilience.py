#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, PropertyMock, call
from typing import Dict, Any
import numpy as np


class FakeMT5Tick:
    bid = 2330.0
    ask = 2330.5
    last = 2330.2
    time = 1234567890
    _point = 0.01

    def __init__(self, bid=2330.0, ask=2330.5):
        self.bid = bid
        self.ask = ask


class FakeMT5Result:
    retcode = 0
    order = 1001
    deal = 1001
    price = 2330.0
    volume = 0.5
    comment = ""


_RATES_DTYPE = np.dtype([
    ('time', 'i8'), ('open', 'f8'), ('high', 'f8'), ('low', 'f8'),
    ('close', 'f8'), ('tick_volume', 'i8'), ('spread', 'i8'), ('real_volume', 'i8'),
])

def _make_rates_data(count: int = 1, base_time: int = 1700000000, open_p: float = 2330.0):
    return np.array(
        [(base_time + i * 60, open_p, open_p + 1.0, open_p - 1.0, open_p + 0.5, 100, 20, 0)
         for i in range(count)],
        dtype=_RATES_DTYPE,
    )


class FakeMT5Position:
    ticket = 5001
    symbol = "XAUUSD"
    type = 0
    volume = 0.5
    price_open = 2330.0
    sl = 2329.0
    tp = 2331.0
    profit = 0.0
    swap = 0.0
    time = int(datetime.now(timezone.utc).timestamp())
    comment = ""
    magic = 202402


class FakeMT5TerminalInfo:
    trade_allowed = True
    name = "MetaTrader 5"


class FakeAccountInfo:
    login = 12345
    balance = 1000.0
    equity = 1000.0
    margin = 0.0
    margin_free = 1000.0
    margin_level = 0.0
    currency = "USD"
    leverage = 500


class FakeSymbolInfo:
    name = "XAUUSD"
    digits = 2
    point = 0.01
    trade_stops_level = 10
    trade_freeze_level = 0
    spread = 20
    spread_float = False
    swap_long = -5.0
    swap_short = -5.0
    margin_initial = 4339.37
    margin_maintenance = 4339.37
    currency_base = "USD"
    currency_profit = "USD"
    trade_tick_value = 0.1
    trade_tick_size = 0.01
    trade_contract_size = 100


class TestMT5DisconnectReconnect(unittest.TestCase):
    def setUp(self):
        self.mt5_patcher = patch("connectors.mt5_connector.mt5")
        self.mock_mt5 = self.mt5_patcher.start()
        self.mock_mt5.terminal_info.return_value = FakeMT5TerminalInfo()
        self.mock_mt5.account_info.return_value = FakeAccountInfo()
        self.mock_mt5.symbol_info_tick.return_value = FakeMT5Tick()
        self.mock_mt5.symbol_info.return_value = FakeSymbolInfo()
        self.mock_mt5.last_error.return_value = "No error"

        # settings
        self.settings_patcher = patch("connectors.mt5_connector.get_settings")
        self.mock_settings = self.settings_patcher.start()
        mock_settings_instance = MagicMock()
        mock_settings_instance.max_slippage = 2
        mock_settings_instance.mt5_login = 12345
        mock_settings_instance.mt5_password = "pass"
        mock_settings_instance.mt5_server = "server"
        self.mock_settings.return_value = mock_settings_instance

        from connectors.mt5_connector import MT5Connector
        self.conn = MT5Connector()

    def tearDown(self):
        self.mt5_patcher.stop()
        self.settings_patcher.stop()

    def test_initial_connect_success(self):
        self.conn._connected = False
        result = self.conn.connect()
        self.assertTrue(result)
        self.assertTrue(self.conn._connected)

    def test_connect_after_disconnect(self):
        self.conn._connected = False
        self.conn.connect()
        self.conn._connected = False
        self.mock_mt5.account_info.return_value = FakeAccountInfo()
        result = self.conn.connect()
        self.assertTrue(result)
        self.assertTrue(self.conn._connected)

    def test_get_rates_auto_reconnects(self):
        self.conn._connected = False
        self.mock_mt5.copy_rates_from_pos.return_value = _make_rates_data()
        df = self.conn.get_rates()
        self.assertIsNotNone(df)
        self.assertTrue(self.conn._connected)

    def test_get_rates_retry_on_none(self):
        self.conn.connect()
        self.mock_mt5.copy_rates_from_pos.side_effect = [None, None, _make_rates_data()]
        df = self.conn.get_rates()
        self.assertIsNotNone(df)

    def test_get_rates_reconnects_on_failure(self):
        self.conn.connect()
        self.conn._connected = True
        self.mock_mt5.copy_rates_from_pos.side_effect = Exception("MT5 disconnected")

        with self.assertRaises(Exception) as ctx:
            self.conn.get_rates()
        self.assertIn("MT5 disconnected", str(ctx.exception) if "MT5 disconnected" in str(ctx.exception) else "No error" or "Failed to get rates for XAUUSD")

    def test_call_with_retry_disconnect_on_failure(self):
        self.conn.connect()
        self.mock_mt5.copy_rates_from_pos.side_effect = [None, None]
        self.mock_mt5.initialize.return_value = True
        with self.assertRaises(Exception):
            self.conn.get_rates()
        self.assertGreaterEqual(self.mock_mt5.shutdown.call_count, 1)

    def test_place_order_after_reconnect(self):
        self.conn.connect()
        self.mock_mt5.order_send.return_value = FakeMT5Result()
        order = self.conn.place_order(
            symbol="XAUUSD",
            order_type=0,
            volume=0.5,
        )
        self.assertIsNotNone(order)
        self.assertIn("ticket", order)


class TestVPSRebootWithOpenTrade(unittest.TestCase):
    def setUp(self):
        self.mt5_patcher = patch("connectors.mt5_connector.mt5")
        self.mock_mt5 = self.mt5_patcher.start()
        self.mock_mt5.terminal_info.return_value = FakeMT5TerminalInfo()
        self.mock_mt5.account_info.return_value = FakeAccountInfo()
        self.mock_mt5.symbol_info_tick.return_value = FakeMT5Tick()
        self.mock_mt5.symbol_info.return_value = FakeSymbolInfo()
        self.mock_mt5.last_error.return_value = "No error"

        self.runlive_mt5_patcher = patch("scripts.run_live.mt5")
        self.mock_runlive_mt5 = self.runlive_mt5_patcher.start()
        self.mock_runlive_mt5.account_info.return_value = FakeAccountInfo()
        self.mock_runlive_mt5.login.return_value = True

        self.settings_patcher = patch("connectors.mt5_connector.get_settings")
        self.mock_settings = self.settings_patcher.start()
        mock_settings_instance = MagicMock()
        mock_settings_instance.max_slippage = 2
        mock_settings_instance.mt5_login = 12345
        mock_settings_instance.mt5_password = "pass"
        mock_settings_instance.mt5_server = "server"
        mock_settings_instance.symbol = "XAUUSD"
        mock_settings_instance.max_daily_trades = 15
        mock_settings_instance.risk_percent = 2.0
        mock_settings_instance.trail_multiplier = 0.2
        mock_settings_instance.max_spread = 30
        mock_settings_instance.backtest_commission = 3.5
        mock_settings_instance.circuit_breaker_max_daily_loss_pct = 3.0
        mock_settings_instance.circuit_breaker_max_consecutive_losses = 4
        mock_settings_instance.circuit_breaker_max_drawdown_pct = 15.0
        mock_settings_instance.news_filter_enabled = False
        mock_settings_instance.adjust_for_balance.return_value = mock_settings_instance
        self.mock_settings.return_value = mock_settings_instance

    def tearDown(self):
        self.mt5_patcher.stop()
        self.runlive_mt5_patcher.stop()
        self.settings_patcher.stop()

    def check_live_bot_orphan_recovery(self):
        from scripts.run_live import ScalperBot
        bot = ScalperBot(env_file=".env")

        self.mock_mt5.positions_get.return_value = [FakeMT5Position()]

        bot.connector._connected = True
        bot.connector._account_info = {"login": 12345, "balance": 1000.0}

        bot.mongo = MagicMock()
        bot.mongo.connect.return_value = True

        bot.zone_detector = MagicMock()
        bot.orb = MagicMock()

        self.mock_mt5.account_info.return_value = FakeAccountInfo()
        self.mock_mt5.copy_rates_from.return_value = [
            (1700000000, 2330.0, 2331.0, 2329.0, 2330.5, 100, 20, 0)
        ]

        bot.telegram = MagicMock()
        bot._load_15min_data = MagicMock()

        result = bot.initialize()
        self.assertTrue(result)
        self.assertIsNotNone(bot._position)
        self.assertEqual(bot._position["ticket"], 5001)
        self.assertEqual(bot._position["type"], "buy")
        self.assertEqual(bot._position["entry"], 2330.0)

    def test_bot_recovers_open_trade_on_start(self):
        self.check_live_bot_orphan_recovery()


class TestMT5CrashRecovery(unittest.TestCase):
    def setUp(self):
        self.mt5_patcher = patch("connectors.mt5_connector.mt5")
        self.mock_mt5 = self.mt5_patcher.start()
        self.mock_mt5.terminal_info.return_value = FakeMT5TerminalInfo()
        self.mock_mt5.account_info.return_value = FakeAccountInfo()
        self.mock_mt5.symbol_info_tick.return_value = FakeMT5Tick()
        self.mock_mt5.symbol_info.return_value = FakeSymbolInfo()
        self.mock_mt5.last_error.return_value = "No error"

        self.settings_patcher = patch("connectors.mt5_connector.get_settings")
        self.mock_settings = self.settings_patcher.start()
        mock_settings_instance = MagicMock()
        mock_settings_instance.max_slippage = 2
        mock_settings_instance.mt5_login = 12345
        mock_settings_instance.mt5_password = "pass"
        mock_settings_instance.mt5_server = "server"
        self.mock_settings.return_value = mock_settings_instance

        from connectors.mt5_connector import MT5Connector
        self.conn = MT5Connector()

    def tearDown(self):
        self.mt5_patcher.stop()
        self.settings_patcher.stop()

    def test_initialize_succeeds_after_terminal_restart(self):
        self.conn._connected = False
        self.mock_mt5.last_error.return_value = "Terminal not ready"
        self.mock_mt5.initialize.side_effect = [False, True]
        self.mock_mt5.login.return_value = True
        self.mock_mt5.account_info.return_value = FakeAccountInfo()

        result = self.conn.connect()
        self.assertTrue(result)

    def test_get_rates_after_crash_and_reconnect(self):
        self.conn.connect()
        self.mock_mt5.copy_rates_from_pos.return_value = _make_rates_data()
        df = self.conn.get_rates()
        self.assertIsNotNone(df)

    def test_manage_position_after_long_downtime(self):
        self.conn._connected = True
        self.mock_mt5.copy_rates_from_pos.return_value = [
            (1700000000 + i * 60, 2330.0, 2331.0, 2329.0, 2330.5, 100, 20, 0)
            for i in range(300)
        ]
        self.mock_mt5.positions_get.return_value = [FakeMT5Position()]
        self.mock_mt5.order_send.return_value = FakeMT5Result()

        rates_data = [
            (1700000000 + i * 60, 2330.0, 2331.0, 2329.0, 2330.5, 100, 20, 0)
            for i in range(300)
        ]

        import pandas as pd
        df = pd.DataFrame(rates_data, columns=["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"])
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)

        self.assertGreater(len(df), 60, "Need enough bars for position management")


class TestAutoTradingEnable(unittest.TestCase):
    def setUp(self):
        self.pywin32_patcher = patch("connectors.mt5_connector._HAS_PYWIN32", False)
        self.pywin32_patcher.start()

        self.mt5_patcher = patch("connectors.mt5_connector.mt5")
        self.mock_mt5 = self.mt5_patcher.start()
        self.mock_mt5.terminal_info.return_value = FakeMT5TerminalInfo()
        self.mock_mt5.account_info.return_value = FakeAccountInfo()
        self.mock_mt5.symbol_info_tick.return_value = FakeMT5Tick()
        self.mock_mt5.symbol_info.return_value = FakeSymbolInfo()

        self.subprocess_patcher = patch("connectors.mt5_connector.subprocess.run")
        self.mock_subprocess = self.subprocess_patcher.start()

        self.settings_patcher = patch("connectors.mt5_connector.get_settings")
        self.mock_settings = self.settings_patcher.start()
        mock_settings_instance = MagicMock()
        mock_settings_instance.max_slippage = 2
        mock_settings_instance.mt5_login = 12345
        mock_settings_instance.mt5_password = "pass"
        mock_settings_instance.mt5_server = "server"
        self.mock_settings.return_value = mock_settings_instance

        from connectors.mt5_connector import MT5Connector
        self.conn = MT5Connector()

    def tearDown(self):
        self.pywin32_patcher.stop()
        self.mt5_patcher.stop()
        self.settings_patcher.stop()
        self.subprocess_patcher.stop()

    def test_autotrading_already_enabled(self):
        self.mock_mt5.terminal_info.return_value = FakeMT5TerminalInfo()
        self.conn._connected = False
        self.conn.connect()
        self.mock_subprocess.assert_not_called()

    def test_autotrading_enabled_via_powershell(self):
        term = FakeMT5TerminalInfo()
        term.trade_allowed = False
        self.mock_mt5.terminal_info.return_value = term

        def terminal_info_side_effect():
            class Info:
                trade_allowed = True
            return Info()

        self.mock_mt5.terminal_info.side_effect = [term, terminal_info_side_effect()]

        self.conn._connected = False
        self.conn.connect()
        self.mock_subprocess.assert_called_once()

    def test_autotrading_powershell_failure_logged(self):
        term = FakeMT5TerminalInfo()
        term.trade_allowed = False

        def terminal_false_then_true():
            class Info:
                trade_allowed = True
            return Info()

        self.mock_mt5.terminal_info.side_effect = [term, terminal_false_then_true()]
        self.mock_subprocess.side_effect = Exception("PowerShell not available")

        self.conn._connected = False
        self.conn.connect()
        self.mock_subprocess.assert_called_once()

    def test_autotrading_skipped_on_second_connect(self):
        self.conn._connected = True
        conn_ok = self.conn.connect()
        self.assertTrue(conn_ok)
        self.mock_mt5.terminal_info.assert_not_called()

    def test_powershell_command_structure(self):
        term = FakeMT5TerminalInfo()
        term.trade_allowed = False
        self.mock_mt5.terminal_info.side_effect = lambda: term

        self.conn._connected = False
        self.conn.connect()
        self.mock_subprocess.assert_called()
        args, kwargs = self.mock_subprocess.call_args
        cmd = args[0]
        self.assertIn("powershell", cmd[0].lower())
        self.assertIn("SendKeys", cmd[2])
        self.assertIn("%t", cmd[2])


class TestMongoDBUnavailable(unittest.TestCase):
    def setUp(self):
        self.mt5_patcher = patch("connectors.mt5_connector.mt5")
        self.mock_mt5 = self.mt5_patcher.start()
        self.mock_mt5.terminal_info.return_value = FakeMT5TerminalInfo()
        self.mock_mt5.account_info.return_value = FakeAccountInfo()
        self.mock_mt5.symbol_info_tick.return_value = FakeMT5Tick()
        self.mock_mt5.symbol_info.return_value = FakeSymbolInfo()
        self.mock_mt5.last_error.return_value = "No error"

        self.runlive_mt5_patcher = patch("scripts.run_live.mt5")
        self.mock_runlive_mt5 = self.runlive_mt5_patcher.start()
        self.mock_runlive_mt5.account_info.return_value = FakeAccountInfo()
        self.mock_runlive_mt5.login.return_value = True

        self.settings_patcher = patch("connectors.mt5_connector.get_settings")
        self.mock_settings = self.settings_patcher.start()
        mock_settings_instance = MagicMock()
        mock_settings_instance.max_slippage = 2
        mock_settings_instance.mt5_login = 12345
        mock_settings_instance.mt5_password = "pass"
        mock_settings_instance.mt5_server = "server"
        mock_settings_instance.symbol = "XAUUSD"
        mock_settings_instance.max_daily_trades = 15
        mock_settings_instance.risk_percent = 2.0
        mock_settings_instance.trail_multiplier = 0.2
        mock_settings_instance.max_spread = 30
        mock_settings_instance.backtest_commission = 3.5
        mock_settings_instance.circuit_breaker_max_daily_loss_pct = 3.0
        mock_settings_instance.circuit_breaker_max_consecutive_losses = 4
        mock_settings_instance.circuit_breaker_max_drawdown_pct = 15.0
        mock_settings_instance.news_filter_enabled = False
        mock_settings_instance.adjust_for_balance.return_value = mock_settings_instance
        self.mock_settings.return_value = mock_settings_instance

    def tearDown(self):
        self.mt5_patcher.stop()
        self.runlive_mt5_patcher.stop()
        self.settings_patcher.stop()

    def test_initialize_continues_when_mongo_unavailable(self):
        from scripts.run_live import ScalperBot
        bot = ScalperBot(env_file=".env")
        bot.connector._connected = True
        bot.connector._account_info = {"login": 12345, "balance": 1000.0}
        bot.mongo = MagicMock()
        bot.mongo.connect.return_value = False
        bot.zone_detector = MagicMock()
        bot.orb = MagicMock()
        bot.telegram = MagicMock()
        bot._load_15min_data = MagicMock()

        result = bot.initialize()
        self.assertTrue(result)
        bot.mongo.connect.assert_called_once()

    def test_save_trade_graceful_failure(self):
        from database.mongo_client import MongoClient as RealMongo
        from pymongo.errors import ConnectionFailure
        with patch("database.mongo_client.PyMongoClient") as mock_pym:
            mock_pym.side_effect = ConnectionFailure("No server")
            with patch("database.mongo_client.get_settings") as mock_gs:
                mock_s = MagicMock()
                mock_s.mongo_uri = "mongodb://invalid:27017"
                mock_s.mongo_db = "test"
                mock_s.mongo_trades_collection = "trades"
                mock_s.mongo_signals_collection = "signals"
                mock_s.mongo_metrics_collection = "metrics"
                mock_gs.return_value = mock_s
                mongo = RealMongo()
                self.assertFalse(mongo._connected)
                result = mongo.save_trade({"test": "data"})
                self.assertFalse(result)

    def test_trade_persists_when_mongo_available(self):
        from scripts.run_live import ScalperBot
        bot = ScalperBot(env_file=".env")
        bot.mongo = MagicMock()
        bot.mongo.connect.return_value = True
        bot.mongo.save_trade.return_value = True
        result = bot.mongo.save_trade({"trade_id": "test123", "profit": 50.0})
        self.assertTrue(result)

    def test_mongo_disconnect_called_on_shutdown(self):
        from scripts.run_live import ScalperBot
        bot = ScalperBot(env_file=".env")
        bot.mongo = MagicMock()
        bot.telegram = MagicMock()
        bot.connector = MagicMock()
        bot.connector.get_positions.return_value = []
        bot.shutdown()
        bot.mongo.disconnect.assert_called_once()
        bot.connector.disconnect.assert_called_once()


class TestTelegramInvalidToken(unittest.TestCase):
    def test_telegram_disabled_with_empty_token(self):
        with patch("telegram.alerts.get_settings") as mock_gs:
            mock_gs.return_value.telegram_token = ""
            mock_gs.return_value.telegram_chat_id = "12345"
            from telegram.alerts import TelegramNotifier
            notifier = TelegramNotifier()
        self.assertFalse(notifier._enabled)
        result = notifier._send("test")
        self.assertFalse(result)

    def test_telegram_disabled_with_empty_chat_id(self):
        with patch("telegram.alerts.get_settings") as mock_gs:
            mock_gs.return_value.telegram_token = "valid:token"
            mock_gs.return_value.telegram_chat_id = ""
            from telegram.alerts import TelegramNotifier
            notifier = TelegramNotifier()
        self.assertFalse(notifier._enabled)
        result = notifier._send("test")
        self.assertFalse(result)

    def test_telegram_send_handles_401_gracefully(self):
        import requests
        with patch("telegram.alerts.get_settings") as mock_gs:
            mock_gs.return_value.telegram_token = "bogus:token"
            mock_gs.return_value.telegram_chat_id = "12345"
            with patch("telegram.alerts.requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("401 Unauthorized")
                mock_post.return_value = mock_resp
                from telegram.alerts import TelegramNotifier
                notifier = TelegramNotifier()
                self.assertTrue(notifier._enabled)
                result = notifier._send("test message")
                self.assertFalse(result)
                mock_post.assert_called_once()

    def test_bot_starts_with_invalid_telegram_token(self):
        self.mt5_patcher = patch("connectors.mt5_connector.mt5")
        self.mock_mt5 = self.mt5_patcher.start()
        self.mock_mt5.terminal_info.return_value = FakeMT5TerminalInfo()
        self.mock_mt5.account_info.return_value = FakeAccountInfo()
        self.mock_mt5.symbol_info_tick.return_value = FakeMT5Tick()
        self.mock_mt5.symbol_info.return_value = FakeSymbolInfo()
        self.mock_mt5.last_error.return_value = "No error"

        self.runlive_mt5_patcher = patch("scripts.run_live.mt5")
        self.mock_runlive_mt5 = self.runlive_mt5_patcher.start()
        self.mock_runlive_mt5.account_info.return_value = FakeAccountInfo()
        self.mock_runlive_mt5.login.return_value = True

        self.settings_patcher = patch("connectors.mt5_connector.get_settings")
        self.mock_settings = self.settings_patcher.start()
        mock_settings_instance = MagicMock()
        mock_settings_instance.max_slippage = 2
        mock_settings_instance.mt5_login = 12345
        mock_settings_instance.mt5_password = "pass"
        mock_settings_instance.mt5_server = "server"
        mock_settings_instance.symbol = "XAUUSD"
        mock_settings_instance.max_daily_trades = 15
        mock_settings_instance.risk_percent = 2.0
        mock_settings_instance.trail_multiplier = 0.2
        mock_settings_instance.max_spread = 30
        mock_settings_instance.backtest_commission = 3.5
        mock_settings_instance.circuit_breaker_max_daily_loss_pct = 3.0
        mock_settings_instance.circuit_breaker_max_consecutive_losses = 4
        mock_settings_instance.circuit_breaker_max_drawdown_pct = 15.0
        mock_settings_instance.news_filter_enabled = False
        mock_settings_instance.adjust_for_balance.return_value = mock_settings_instance
        self.mock_settings.return_value = mock_settings_instance

        try:
            from scripts.run_live import ScalperBot
            bot = ScalperBot(env_file=".env")
            bot.connector._connected = True
            bot.connector._account_info = {"login": 12345, "balance": 1000.0}
            bot.mongo = MagicMock()
            bot.mongo.connect.return_value = True
            bot.zone_detector = MagicMock()
            bot.orb = MagicMock()
            bot.telegram = MagicMock()
            bot._load_15min_data = MagicMock()
            result = bot.initialize()
            self.assertTrue(result)
        finally:
            self.mt5_patcher.stop()
            self.runlive_mt5_patcher.stop()
            self.settings_patcher.stop()


class TestNewsFilterEnableDisable(unittest.TestCase):
    def setUp(self):
        self.mt5_patcher = patch("connectors.mt5_connector.mt5")
        self.mock_mt5 = self.mt5_patcher.start()
        self.mock_mt5.terminal_info.return_value = FakeMT5TerminalInfo()
        self.mock_mt5.account_info.return_value = FakeAccountInfo()
        self.mock_mt5.symbol_info_tick.return_value = FakeMT5Tick()
        self.mock_mt5.symbol_info.return_value = FakeSymbolInfo()
        self.mock_mt5.last_error.return_value = "No error"

    def tearDown(self):
        self.mt5_patcher.stop()

    def test_news_filter_created_when_enabled(self):
        with patch("scripts.run_live.get_settings") as mock_gs:
            mock_settings = MagicMock()
            mock_settings.news_filter_enabled = True
            mock_settings.news_blackout_minutes = 30
            mock_gs.return_value = mock_settings
            with patch("scripts.run_live.MT5Connector"):
                with patch("scripts.run_live.TelegramNotifier"):
                    with patch("scripts.run_live.MongoClient"):
                        with patch("scripts.run_live.NewsFilter") as mock_nf:
                            from scripts.run_live import ScalperBot
                            bot = ScalperBot(env_file=".env")
                            mock_nf.assert_called_once()
                            self.assertIsNotNone(bot.news_filter)

    def test_news_filter_none_when_disabled(self):
        with patch("scripts.run_live.get_settings") as mock_gs:
            mock_settings = MagicMock()
            mock_settings.news_filter_enabled = False
            mock_gs.return_value = mock_settings
            from scripts.run_live import ScalperBot
            bot = ScalperBot(env_file=".env")
            self.assertIsNone(bot.news_filter)

    def test_news_filter_fetch_failure_does_not_crash(self):
        with patch("core.news_filter.requests.get") as mock_get:
            mock_get.side_effect = Exception("Network error")
            from core.news_filter import NewsFilter
            nf = NewsFilter(blackout_minutes=30)
            nf.fetch_events()
            self.assertEqual(len(nf._events), 0)
            blackout, reason = nf.is_blackout(datetime.now(timezone.utc))
            self.assertFalse(blackout)

    def test_news_filter_blocks_during_blackout(self):
        from core.news_filter import NewsFilter
        nf = NewsFilter(blackout_minutes=30)
        event_time = datetime.now(timezone.utc) + timedelta(minutes=5)
        nf._events = [{"time": event_time, "title": "FOMC Press Conference"}]
        blackout, reason = nf.is_blackout(datetime.now(timezone.utc))
        self.assertTrue(blackout)
        self.assertIn("FOMC", reason)


class TestFridayShutdown(unittest.TestCase):
    def setUp(self):
        self.mt5_patcher = patch("connectors.mt5_connector.mt5")
        self.mock_mt5 = self.mt5_patcher.start()
        self.mock_mt5.terminal_info.return_value = FakeMT5TerminalInfo()
        self.mock_mt5.account_info.return_value = FakeAccountInfo()
        self.mock_mt5.symbol_info_tick.return_value = FakeMT5Tick()
        self.mock_mt5.symbol_info.return_value = FakeSymbolInfo()
        self.mock_mt5.last_error.return_value = "No error"

        self.runlive_mt5_patcher = patch("scripts.run_live.mt5")
        self.mock_runlive_mt5 = self.runlive_mt5_patcher.start()
        self.mock_runlive_mt5.account_info.return_value = FakeAccountInfo()
        self.mock_runlive_mt5.login.return_value = True

        self.settings_patcher = patch("connectors.mt5_connector.get_settings")
        self.mock_settings = self.settings_patcher.start()
        mock_settings_instance = MagicMock()
        mock_settings_instance.max_slippage = 2
        mock_settings_instance.mt5_login = 12345
        mock_settings_instance.mt5_password = "pass"
        mock_settings_instance.mt5_server = "server"
        mock_settings_instance.symbol = "XAUUSD"
        mock_settings_instance.max_daily_trades = 15
        mock_settings_instance.risk_percent = 2.0
        mock_settings_instance.trail_multiplier = 0.2
        mock_settings_instance.max_spread = 30
        mock_settings_instance.backtest_commission = 3.5
        mock_settings_instance.circuit_breaker_max_daily_loss_pct = 3.0
        mock_settings_instance.circuit_breaker_max_consecutive_losses = 4
        mock_settings_instance.circuit_breaker_max_drawdown_pct = 15.0
        mock_settings_instance.news_filter_enabled = False
        mock_settings_instance.adjust_for_balance.return_value = mock_settings_instance
        self.mock_settings.return_value = mock_settings_instance

    def tearDown(self):
        self.mt5_patcher.stop()
        self.runlive_mt5_patcher.stop()
        self.settings_patcher.stop()

    def test_orb_friday_shutdown_closes_position(self):
        from scripts.run_live import ScalperBot
        bot = ScalperBot(env_file=".env")
        bot.initialize = MagicMock(return_value=True)
        bot.connector = MagicMock()
        bot.mongo = MagicMock()
        bot.telegram = MagicMock()
        bot._load_15min_data = MagicMock()
        bot.zone_detector = MagicMock()
        bot.orb = MagicMock()
        bot._df_15min = MagicMock()
        bot.session_times = MagicMock()
        bot._position = {"ticket": 5001, "remaining_lots": 0.5, "type": "buy", "symbol": "XAUUSD"}
        bot._current_date = "2025-06-13"
        bot._m15_last_refresh = 1000

        with patch("scripts.run_live.SessionValidator.is_friday_close") as mock_friday:
            with patch("scripts.run_live.SessionValidator.next_monday_utc") as mock_next:
                with patch("scripts.run_live.time.sleep") as mock_sleep:
                    mock_friday.side_effect = [True, False]
                    mock_next.return_value = datetime.now(timezone.utc) + timedelta(hours=1)

                    def stop_after_reconnect(*a, **kw):
                        bot._running = False
                    bot.connector.connect.side_effect = stop_after_reconnect

                    bot.run()

                    bot.connector.close_position.assert_called_once()
                    self.assertEqual(bot.connector.close_position.call_args[0][0]["ticket"], 5001)
                    bot.mongo.disconnect.assert_called()
                    bot.connector.disconnect.assert_called()
                    mock_sleep.assert_called_once()
                    bot.connector.connect.assert_called_once()
                    bot.mongo.connect.assert_called_once()
                    bot._load_15min_data.assert_called_once()
                    self.assertIsNone(bot._current_date)
                    self.assertEqual(bot._m15_last_refresh, 0)

    def test_orb_friday_shutdown_no_position(self):
        from scripts.run_live import ScalperBot
        bot = ScalperBot(env_file=".env")
        bot.initialize = MagicMock(return_value=True)
        bot.connector = MagicMock()
        bot.mongo = MagicMock()
        bot.telegram = MagicMock()
        bot._load_15min_data = MagicMock()
        bot.zone_detector = MagicMock()
        bot.orb = MagicMock()
        bot._df_15min = MagicMock()
        bot.session_times = MagicMock()
        bot._position = None
        bot._current_date = "2025-06-13"
        bot._m15_last_refresh = 1000

        with patch("scripts.run_live.SessionValidator.is_friday_close") as mock_friday:
            with patch("scripts.run_live.SessionValidator.next_monday_utc") as mock_next:
                with patch("scripts.run_live.time.sleep") as mock_sleep:
                    mock_friday.side_effect = [True, False]
                    mock_next.return_value = datetime.now(timezone.utc) + timedelta(hours=1)

                    def stop_after_reconnect(*a, **kw):
                        bot._running = False
                    bot.connector.connect.side_effect = stop_after_reconnect

                    bot.run()

                    bot.connector.close_position.assert_not_called()

    def test_aggressive_friday_shutdown_closes_position(self):
        from scripts.run_aggressive import AggressiveBot
        bot = AggressiveBot(env_file=".env")
        bot.initialize = MagicMock(return_value=True)
        bot.connector = MagicMock()
        bot.mongo = MagicMock()
        bot.telegram = MagicMock()
        bot._load_15min_data = MagicMock()
        bot.zone_detector = MagicMock()
        bot._df_15min = MagicMock()
        bot._position = {"ticket": 6001, "remaining_lots": 0.3, "type": "sell", "symbol": "XAUUSD"}
        bot._current_date = "2025-06-13"
        bot._m15_last_refresh = 1000

        with patch("scripts.run_aggressive.SessionValidator.is_friday_close") as mock_friday:
            with patch("scripts.run_aggressive.SessionValidator.next_monday_utc") as mock_next:
                with patch("scripts.run_aggressive.time.sleep") as mock_sleep:
                    mock_friday.side_effect = [True, False]
                    mock_next.return_value = datetime.now(timezone.utc) + timedelta(hours=1)

                    def stop_after_reconnect(*a, **kw):
                        bot._running = False
                    bot.connector.connect.side_effect = stop_after_reconnect

                    bot.run()

                    bot.connector.close_position.assert_called_once()
                    self.assertEqual(bot.connector.close_position.call_args[0][0]["ticket"], 6001)

    def test_aggressive_friday_shutdown_no_position(self):
        from scripts.run_aggressive import AggressiveBot
        bot = AggressiveBot(env_file=".env")
        bot.initialize = MagicMock(return_value=True)
        bot.connector = MagicMock()
        bot.mongo = MagicMock()
        bot.telegram = MagicMock()
        bot._load_15min_data = MagicMock()
        bot.zone_detector = MagicMock()
        bot._df_15min = MagicMock()
        bot._position = None
        bot._current_date = "2025-06-13"
        bot._m15_last_refresh = 1000

        with patch("scripts.run_aggressive.SessionValidator.is_friday_close") as mock_friday:
            with patch("scripts.run_aggressive.SessionValidator.next_monday_utc") as mock_next:
                with patch("scripts.run_aggressive.time.sleep") as mock_sleep:
                    mock_friday.side_effect = [True, False]
                    mock_next.return_value = datetime.now(timezone.utc) + timedelta(hours=1)

                    def stop_after_reconnect(*a, **kw):
                        bot._running = False
                    bot.connector.connect.side_effect = stop_after_reconnect

                    bot.run()

                    bot.connector.close_position.assert_not_called()


class TestBarScanAfterReconnect(unittest.TestCase):
    def test_scan_from_open_time_not_window(self):
        from scripts.run_live import ScalperBot
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        self.assertIn("df.index.get_loc(open_time)", content,
                      "ORB bot must scan from open_time, not a fixed window")

    def test_aggressive_scan_from_open_time(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py") as f:
            content = f.read()
        self.assertIn("rates.index.get_loc(open_time)", content,
                      "Aggressive bot must scan from open_time, not a fixed window")

    def test_catch_up_handles_bars_before_start_idx(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        self.assertIn("if df.index[j] <= open_time:", content,
                      "ORB bot must skip bars before or at open_time")
        collapsed = "".join(content.split())
        self.assertIn("ifdf.index[j]<=open_time:continue", collapsed,
                      "continue must follow the open_time skip check")

    def test_aggressive_catch_up_skips_old_bars(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py") as f:
            content = f.read()
        self.assertIn("if rates.index[j] <= open_time:", content,
                      "Aggressive bot must skip bars before or at open_time")
        collapsed = "".join(content.split())
        self.assertIn("ifrates.index[j]<=open_time:continue", collapsed,
                      "continue must follow the open_time skip check")


if __name__ == "__main__":
    unittest.main(verbosity=2)
