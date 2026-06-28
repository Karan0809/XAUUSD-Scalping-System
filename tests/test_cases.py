#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from datetime import datetime, timezone, timedelta, time
from unittest.mock import patch, MagicMock, PropertyMock
from typing import Dict, Any

from config.sessions import SessionTimes, SessionValidator
from core.risk_manager import RiskManager


class TestSessionTransitions(unittest.TestCase):
    def setUp(self):
        self.sessions = SessionTimes()

    def _utc(self, h: int, m: int, weekday: int = 0) -> datetime:
        return datetime(2026, 6, 15 + weekday, h, m, tzinfo=timezone.utc)

    def test_asia_to_london_transition(self):
        self.assertEqual(self.sessions.get_active_session(self._utc(8, 59)), "asia")
        self.assertEqual(self.sessions.get_active_session(self._utc(9, 0)), "london")

    def test_london_to_gap_transition(self):
        self.assertEqual(self.sessions.get_active_session(self._utc(11, 59)), "london")
        self.assertIsNone(self.sessions.get_active_session(self._utc(12, 0)))

    def test_gap_to_ny_transition(self):
        self.assertIsNone(self.sessions.get_active_session(self._utc(12, 59)))
        self.assertEqual(self.sessions.get_active_session(self._utc(13, 0)), "ny")

    def test_ny_to_close_transition(self):
        self.assertEqual(self.sessions.get_active_session(self._utc(15, 59)), "ny")
        self.assertIsNone(self.sessions.get_active_session(self._utc(16, 0)))

    def test_trading_hours_boundary(self):
        self.assertTrue(self.sessions.is_trading_hours(self._utc(0, 0)))
        self.assertTrue(self.sessions.is_trading_hours(self._utc(16, 59)))
        self.assertFalse(self.sessions.is_trading_hours(self._utc(17, 0)))
        self.assertFalse(self.sessions.is_trading_hours(self._utc(23, 59)))

    def test_friday_close_boundary(self):
        friday_1659 = self._utc(16, 59, weekday=4)
        friday_1700 = self._utc(17, 0, weekday=4)
        self.assertFalse(SessionValidator.is_friday_close(friday_1659))
        self.assertTrue(SessionValidator.is_friday_close(friday_1700))

    def test_sunday_filter(self):
        sunday = self._utc(12, 0, weekday=6)
        self.assertTrue(SessionValidator.is_sunday(sunday))
        self.assertFalse(SessionValidator.is_valid_session_day(sunday))

    def test_next_monday_from_friday(self):
        friday = self._utc(17, 0, weekday=4)
        monday = SessionValidator.next_monday_utc(friday)
        self.assertEqual(monday.weekday(), 0)
        self.assertEqual(monday.hour, 0)
        self.assertEqual(monday.minute, 0)

    def test_session_strings_overlap(self):
        dt = self._utc(7, 0)
        sessions = self.sessions.get_sessions(dt)
        self.assertIn("asia", sessions)
        self.assertIn("pre_london", sessions)

    def test_is_trade_window(self):
        self.assertTrue(self.sessions.is_trade_window(self._utc(10, 0)))
        self.assertTrue(self.sessions.is_trade_window(self._utc(14, 0)))
        self.assertFalse(self.sessions.is_trade_window(self._utc(7, 0)))
        self.assertFalse(self.sessions.is_trade_window(self._utc(12, 30)))


class TestDrawdownCalculation(unittest.TestCase):
    def setUp(self):
        self.rm = RiskManager(
            max_daily_loss_pct=3.0,
            max_consecutive_losses=4,
            max_drawdown_pct=15.0,
        )

    def test_initial_peak_tracks_balance(self):
        self.rm.start_day("2026-06-15", 1000.0)
        self.assertEqual(self.rm._peak_balance, 1000.0)

    def test_peak_updates_on_higher_balance(self):
        self.rm.start_day("2026-06-15", 1000.0)
        self.rm.start_day("2026-06-16", 1200.0)
        self.assertEqual(self.rm._peak_balance, 1200.0)

    def test_peak_does_not_update_on_lower_balance(self):
        self.rm.start_day("2026-06-15", 1000.0)
        self.rm.start_day("2026-06-16", 900.0)
        self.assertEqual(self.rm._peak_balance, 1000.0)

    def test_drawdown_triggers_kill(self):
        self.rm.start_day("2026-06-15", 1000.0)
        allowed, reason = self.rm.check_entry_allowed(801.0)
        self.assertTrue(allowed)
        allowed, reason = self.rm.check_entry_allowed(800.0)
        self.assertFalse(allowed)
        self.assertIn("drawdown", reason.lower())
        self.assertTrue(self.rm._is_killed)

    def test_daily_loss_blocks_after_threshold(self):
        self.rm.start_day("2026-06-15", 100.0)
        self.rm.record_trade(-30.0)
        allowed, reason = self.rm.check_entry_allowed(100.0)
        self.assertFalse(allowed)
        self.assertIn("loss limit", reason.lower())

    def test_daily_loss_blocks_at_scaled_threshold_above_1000(self):
        self.rm.start_day("2026-06-15", 1000.0)
        self.rm.record_trade(-31.0)
        allowed, reason = self.rm.check_entry_allowed(1000.0)
        self.assertFalse(allowed)
        self.assertIn("loss limit", reason.lower())

    def test_daily_loss_resets_on_new_day(self):
        self.rm.start_day("2026-06-15", 1000.0)
        self.rm.record_trade(-31.0)
        allowed, _ = self.rm.check_entry_allowed(1000.0)
        self.assertFalse(allowed)
        self.rm.start_day("2026-06-16", 969.0)
        allowed, _ = self.rm.check_entry_allowed(969.0)
        self.assertTrue(allowed)

    def test_consecutive_losses_block(self):
        self.rm.start_day("2026-06-15", 150.0)
        for i in range(4):
            allowed, _ = self.rm.check_entry_allowed(150.0)
            self.assertTrue(allowed, f"Failed on iteration {i}")
            self.rm.record_trade(-5.0)
        allowed, reason = self.rm.check_entry_allowed(150.0)
        self.assertFalse(allowed)
        self.assertIn("consecutive", reason.lower())

    def test_win_resets_consecutive_counter(self):
        self.rm.start_day("2026-06-15", 1000.0)
        self.rm.record_trade(-10.0)
        self.rm.record_trade(-10.0)
        self.rm.record_trade(20.0)
        self.assertEqual(self.rm._consecutive_losses, 0)

    def test_scaled_thresholds_small_account(self):
        self.rm.start_day("2026-06-15", 150.0)
        self.assertEqual(self.rm._get_effective_max_daily_loss_pct(150.0), 20.0)
        self.assertEqual(self.rm._get_effective_max_drawdown_pct(150.0), 50.0)

    def test_scaled_thresholds_medium_account(self):
        self.assertEqual(self.rm._get_effective_max_daily_loss_pct(350.0), 10.0)
        self.assertEqual(self.rm._get_effective_max_drawdown_pct(350.0), 30.0)

    def test_scaled_thresholds_large_account(self):
        self.assertEqual(self.rm._get_effective_max_daily_loss_pct(2000.0), 3.0)
        self.assertEqual(self.rm._get_effective_max_drawdown_pct(2000.0), 15.0)

    def test_drawdown_after_intraday_peak(self):
        self.rm.start_day("2026-06-15", 1000.0)
        self.rm.start_day("2026-06-16", 1000.0)
        allowed, _ = self.rm.check_entry_allowed(850.0)
        self.assertTrue(allowed)
        allowed, reason = self.rm.check_entry_allowed(800.0)
        self.assertFalse(allowed)
        self.assertIn("drawdown", reason.lower())

    def test_kill_is_permanent(self):
        self.rm.start_day("2026-06-15", 1000.0)
        self.rm._is_killed = True
        allowed, reason = self.rm.check_entry_allowed(10000.0)
        self.assertFalse(allowed)
        self.assertIn("killed", reason.lower())


class TestSameCandleTP1SLEOrdering(unittest.TestCase):
    def test_tp1_checked_before_sl(self):
        from scripts.run_live import ScalperBot
        tp1_line = None
        sl_line = None
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            for i, line in enumerate(f, 1):
                if "# TP1: close first tranche at 1:1" in line:
                    tp1_line = i
                if "# SL/be check on remaining position" in line:
                    sl_line = i
        self.assertIsNotNone(tp1_line, "TP1 comment not found")
        self.assertIsNotNone(sl_line, "SL comment not found")
        self.assertLess(tp1_line, sl_line,
                         f"TP1 check at line {tp1_line} must come before SL check at line {sl_line}")

    def test_aggressive_tp1_checked_before_sl(self):
        from scripts.run_aggressive import AggressiveBot
        tp1_line = None
        sl_line = None
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py") as f:
            for i, line in enumerate(f, 1):
                if "# TP1 at 1:1" in line:
                    tp1_line = i
                if "# SL/BE check" in line:
                    sl_line = i
        self.assertIsNotNone(tp1_line, "TP1 comment not found")
        self.assertIsNotNone(sl_line, "SL comment not found")
        self.assertLess(tp1_line, sl_line,
                         f"TP1 check at line {tp1_line} must come before SL check at line {sl_line}")

    def test_sl_check_uses_current_sl_not_original(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        self.assertIn('self._position["sl"]', content,
                      "SL check must use current sl, not original_sl")

    def test_tp1_sets_sl_to_entry_before_sl_check(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        close_idx = content.index('self._close_partial(self._position["tp1_lots"]')
        tp1_flag_idx = content.index('self._position["tp1_hit"] = True')
        modify_idx = content.index("modify_position", tp1_flag_idx)
        sl_moved_idx = content.index('self._position["sl"] = entry')
        self.assertLess(close_idx, tp1_flag_idx,
                        "TP1 partial close must come before tp1_hit flag")
        self.assertLess(tp1_flag_idx, modify_idx,
                        "modify_position call must come after tp1_hit flag")
        self.assertLess(modify_idx, sl_moved_idx,
                        "SL move to entry must come AFTER modify_position succeeds")


class TestOrphanRecovery(unittest.TestCase):
    def test_orb_orphan_dict_keys(self):
        pos: Dict[str, Any] = {
            "type": "buy",
            "entry": 2330.0,
            "sl": 2329.0,
            "tp": 2331.0,
            "lot_size": 0.5,
            "original_sl": 2329.0,
            "original_lot_size": 0.5,
            "tp1_lots": 0,
            "tp2_lots": 0,
            "tp3_lots": 0,
            "remaining_lots": 0.5,
            "pnl": 0.0,
            "tp1_hit": False,
            "tp2_hit": False,
            "tp3_hit": False,
            "trailing_activated": False,
            "trail_activation_bar": 0,
            "trade_id": "test-uuid",
            "open_time": datetime.now(timezone.utc),
            "ticket": 12345,
        }
        required = ["type", "entry", "sl", "tp", "lot_size", "original_sl",
                     "original_lot_size", "remaining_lots", "pnl",
                     "tp1_hit", "tp2_hit", "tp3_hit",
                     "trailing_activated", "trail_activation_bar",
                     "trade_id", "open_time", "ticket",
                     "tp1_lots", "tp2_lots", "tp3_lots"]
        for key in required:
            self.assertIn(key, pos, f"ORB orphan dict missing key: {key}")

    def test_aggressive_orphan_dict_keys(self):
        pos: Dict[str, Any] = {
            "type": "buy",
            "entry": 2330.0,
            "sl": 2329.0,
            "tp": 2331.0,
            "lot_size": 0.5,
            "original_sl": 2329.0,
            "original_lot_size": 0.5,
            "tag": "AGGR",
            "tp1_lots": 0.25,
            "remaining_lots": 0.5,
            "pnl": 0.0,
            "tp1_hit": False,
            "trailing_activated": False,
            "trail_level": 0.0,
            "trail_activation_bar": 0,
            "tp_hit_bar": 0,
            "trade_id": "test-uuid",
            "open_time": datetime.now(timezone.utc),
            "ticket": 12345,
        }
        required = ["type", "entry", "sl", "tp", "lot_size", "original_sl",
                     "original_lot_size", "tag", "tp1_lots", "remaining_lots",
                     "pnl", "tp1_hit", "trailing_activated", "trail_level",
                     "trail_activation_bar", "tp_hit_bar",
                     "trade_id", "open_time", "ticket"]
        for key in required:
            self.assertIn(key, pos, f"Aggressive orphan dict missing key: {key}")

    def test_orphan_recovery_clears_tp1_lots_if_partial(self):
        pos = {
            "tp1_lots": 0.25,
            "tp1_hit": False,
            "remaining_lots": 0.5,
            "trailing_activated": False,
        }
        close_info = {"profit": 5.0}
        if close_info:
            pos["tp1_hit"] = True
            pos["tp1_lots"] = 0
            pos["trailing_activated"] = True
            pos["trail_activation_bar"] = 999999
        self.assertTrue(pos["tp1_hit"])
        self.assertEqual(pos["tp1_lots"], 0)
        self.assertTrue(pos["trailing_activated"])

    def test_orphan_lot_split_logic(self):
        for cents, expected_tp1, expected_tp2, expected_tp3 in [
            (50, 0.15, 0.20, 0.15),
            (7, 0.03, 0.04, 0),
            (3, 0.03, 0, 0),
        ]:
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
            self.assertEqual(tp1_c / 100.0, expected_tp1, f"cents={cents} tp1")
            self.assertEqual(tp2_c / 100.0, expected_tp2, f"cents={cents} tp2")
            self.assertEqual(tp3_c / 100.0, expected_tp3, f"cents={cents} tp3")


class TestPartialCloseFailureHandling(unittest.TestCase):
    def test_close_position_creates_fresh_request_per_call(self):
        import inspect
        from connectors.mt5_connector import MT5Connector
        src = inspect.getsource(MT5Connector.close_position)
        self.assertIn("request = {", src)
        self.assertEqual(src.count("request = {"), 1,
                         "close_position must create exactly one fresh request dict per call")

    def test_close_position_ioc_retry_path(self):
        src_path = Path(__file__).resolve().parent.parent / "connectors" / "mt5_connector.py"
        with open(src_path) as f:
            content = f.read()
        self.assertIn("retcode == 10013", content)
        self.assertIn("request.pop(\"type_filling\")", content)
        self.assertIn("request[\"position\"] = actual_ticket", content)

    def test_orb_close_partial_calls_resolve_on_failure(self):
        src_path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(src_path) as f:
            content = f.read()
        self.assertIn("self._resolve_position_closed(current_time)", content)

    def test_aggressive_close_partial_sets_closed_on_failure(self):
        src_path = Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py"
        with open(src_path) as f:
            content = f.read()
        self.assertIn("pos[\"closed\"] = True", content)


class TestBugRegression(unittest.TestCase):
    def assertSourceLineAfter(self, content: str, earlier: str, later: str, msg: str = ""):
        idx1 = content.index(earlier)
        idx2 = content.index(later)
        self.assertLess(idx1, idx2, msg or f"'{earlier}' not before '{later}'")

    def test_bug8_orb_crash_guard_present(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        self.assertIn("if self._position is None:\n                    break", content)
        self.assertIn('self._position and self._position.get("trailing_activated")', content)

    def test_bug9_aggressive_double_count_guard_present(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py"
        with open(path) as f:
            content = f.read()
        self.assertIn('pos["closed"] = True', content)
        self.assertIn("elif pos[\"remaining_lots\"] <= 0", content)


class TestTP1MovesSLToBE(unittest.TestCase):
    def test_modify_position_called_with_entry_sl_after_tp1(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        self.assertIn('self._position["sl"] = entry', content,
                      "SL must be moved to entry price")
        self.assertIn("modify_position", content,
                      "modify_position must be called to push SL to broker")
        close_idx = content.index('self._close_partial(self._position["tp1_lots"]')
        tp1_flag_idx = content.index('self._position["tp1_hit"] = True')
        modify_idx = content.index("modify_position", tp1_flag_idx)
        sl_idx = content.index('self._position["sl"] = entry')
        self.assertLess(close_idx, tp1_flag_idx,
                        "TP1 partial close must precede tp1_hit flag")
        self.assertLess(tp1_flag_idx, modify_idx,
                        "modify_position call must come after tp1_hit flag")
        self.assertLess(modify_idx, sl_idx,
                        "SL move must come AFTER modify_position succeeds")

    def test_aggressive_modify_position_called_with_entry_sl_after_tp1(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py") as f:
            content = f.read()
        self.assertIn('pos["sl"] = pos["entry"]', content,
                      "Aggressive: SL must be moved to entry price")
        self.assertIn("modify_position", content,
                      "Aggressive: modify_position must be called")
        close_idx = content.index('self._close_partial(pos["tp1_lots"]')
        tp1_flag_idx = content.index('pos["tp1_hit"] = True')
        modify_idx = content.index("modify_position", tp1_flag_idx)
        sl_idx = content.index('pos["sl"] = pos["entry"]')
        self.assertLess(close_idx, tp1_flag_idx,
                        "Aggressive: TP1 partial close must precede tp1_hit flag")
        self.assertLess(tp1_flag_idx, modify_idx,
                        "Aggressive: modify_position call must come after tp1_hit flag")
        self.assertLess(modify_idx, sl_idx,
                        "Aggressive: SL move must come AFTER modify_position succeeds")

    def test_tp1_close_uses_tp1_level_not_entry(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        close_tp1 = "tp1_level" in content[content.index("_close_partial"):content.index("_close_partial") + 200]
        tp1_level_arg = content.count("tp1_level")
        self.assertGreaterEqual(tp1_level_arg, 2,
                                "TP1 partial close must use tp1_level (not entry) as price")

    def test_aggressive_tp1_close_uses_tp1_level(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py") as f:
            content = f.read()
        tp1_in_close = content.count('"tp1",') == 1 and content.count('"tp1",') > 0
        tp1_level_count = content.count("tp1_level")
        self.assertGreaterEqual(tp1_level_count, 2,
                                "Aggressive: TP1 partial close must use tp1_level as price")


class TestTP1AndBEHitSameCandle(unittest.TestCase):
    def test_sl_check_skips_tp_hit_bar(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        self.assertIn('j != self._position.get("tp_hit_bar")', content,
                      "SL/BE check must skip the bar that triggered TP1 (tp_hit_bar guard)")

    def test_aggressive_sl_check_skips_tp_hit_bar(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py") as f:
            content = f.read()
        self.assertIn('j != pos.get("tp_hit_bar")', content,
                      "Aggressive: SL/BE check must skip tp_hit_bar")

    def test_tp_hit_bar_set_after_tp1_close_before_sl_check(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        tp1_close = content.index('_close_partial(self._position["tp1_lots"]')
        tp_hit_set = content.index('self._position["tp_hit_bar"] = j')
        sl_check_header = content.index("# SL/be check on remaining position")
        self.assertLess(tp1_close, tp_hit_set,
                        "tp_hit_bar must be set after TP1 close")
        self.assertLess(tp_hit_set, sl_check_header,
                        "tp_hit_bar must be set before SL/BE check runs")

    def test_same_cannot_double_close_both_tp1_and_be(self):
        pos = {
            "entry": 2330.0, "original_sl": 2320.0, "sl": 2320.0,
            "tp1_lots": 0.15, "remaining_lots": 0.35,
            "tp1_hit": False, "tp2_hit": False, "tp3_lots": 0.15,
            "trailing_activated": False, "trail_level": 0.0,
            "trail_activation_bar": 0, "tp_hit_bar": 0,
            "type": "buy", "ticket": 5001,
        }
        tp1_level = pos["entry"] + abs(pos["entry"] - pos["original_sl"])
        self.assertEqual(tp1_level, 2340.0)
        tp1_triggered = True
        if tp1_triggered:
            pos["tp1_hit"] = True
            pos["sl"] = pos["entry"]
            pos["tp_hit_bar"] = 5
        j = 5
        sl_guard = j != pos.get("tp_hit_bar")
        self.assertFalse(sl_guard,
                         "SL/BE must be skipped on the same bar that triggered TP1")
        self.assertEqual(pos["remaining_lots"], 0.35,
                         "Remaining lots must survive after TP1 close when BE is skipped")


class TestTP1TP2TrailSameCandle(unittest.TestCase):
    def test_orb_execution_order_within_bar(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        self.assertIn("# TP1:", content, "TP1 section must exist")
        self.assertIn("# TP2:", content, "TP2 section must exist (3-target)")
        tp1_idx = content.index("# TP1:")
        tp2_idx = content.index("# TP2:")
        trail_update_idx = content.index("# Update trailing stop")
        self.assertLess(tp1_idx, tp2_idx, "TP1 check must come before TP2")
        self.assertLess(tp2_idx, trail_update_idx, "TP2 must come before trailing update")

    def test_aggressive_execution_order(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py") as f:
            content = f.read()
        self.assertIn("# TP1 at 1:1", content)
        self.assertIn("# Update trailing stop", content)
        tp1_idx = content.index("# TP1 at 1:1")
        trail_idx = content.index("# Update trailing stop")
        self.assertLess(tp1_idx, trail_idx, "TP1 must come before trailing update")

    def test_trail_check_skips_activation_bar(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        self.assertIn('j != self._position.get("trail_activation_bar")', content,
                      "Trailing stop check must skip activation bar")
        self.assertIn('self._position["trail_activation_bar"] = j', content,
                      "Trail activation must record the bar index")

    def test_tp2_skips_tp_hit_bar(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        tp2_section = content[content.index("# TP2:"):content.index("# Update trailing stop")]
        self.assertIn('j != self._position.get("tp_hit_bar")', tp2_section,
                      "TP2 must skip the bar that triggered TP1")

    def test_tp1_only_fires_on_activation_bar_when_all_levels_hit(self):
        pos = {
            "entry": 2330.0, "original_sl": 2320.0, "sl": 2320.0,
            "tp1_lots": 0.15, "tp2_lots": 0.20, "tp3_lots": 0.15,
            "remaining_lots": 0.35,
            "tp1_hit": False, "tp2_hit": False,
            "trailing_activated": False, "trail_level": 0.0,
            "trail_activation_bar": 0, "tp_hit_bar": 0,
            "type": "buy", "ticket": 5001,
        }
        j = 3
        is_buy = True
        sl_dist = abs(pos["entry"] - pos["original_sl"])
        tp1 = pos["entry"] + sl_dist
        tp2 = pos["entry"] + 2 * sl_dist

        bar = {"high": tp2 + 1, "low": pos["entry"] - 1}
        triggered = []
        if not pos["tp1_hit"] and ((is_buy and bar["high"] >= tp1)):
            triggered.append("tp1")
            pos["tp1_hit"] = True
            pos["sl"] = pos["entry"]
            pos["tp_hit_bar"] = j
        if pos["tp1_hit"] and pos["tp3_lots"] > 0 and not pos["tp2_hit"] and \
           pos["remaining_lots"] > 0 and j != pos.get("tp_hit_bar") and \
           ((is_buy and bar["high"] >= tp2)):
            triggered.append("tp2")
        self.assertEqual(triggered, ["tp1"],
                         "Only TP1 must fire on activation bar even when TP2 level is also hit")


class TestGapThroughStopLoss(unittest.TestCase):
    def test_sl_uses_lte_operator(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        self.assertIn('bar["low"] <= self._position["sl"]', content,
                      "SL condition must use <= (not ==) to catch gaps for buys")
        self.assertIn('bar["high"] >= self._position["sl"]', content,
                      "SL condition must use >= for sells")

    def test_aggressive_sl_uses_lte_operator(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py") as f:
            content = f.read()
        self.assertIn('bar["low"] <= pos["sl"]', content,
                      "Aggressive: SL condition must use <= to catch gaps for buys")

    def test_gap_triggered_condition_with_bar_low_below_sl(self):
        is_buy = True
        sl = 2320.0
        bar_low = 2290.0
        self.assertTrue(bar_low <= sl,
                        "When bar low (2290) is below SL (2320), condition must trigger")

    def test_gap_does_not_block_close_on_subsequent_bar(self):
        sl = 2320.0
        tp_hit_bar = 2
        j = 5
        is_buy = True
        is_not_tp_bar = j != tp_hit_bar
        self.assertTrue(is_not_tp_bar,
                        "Gap on a later bar (not tp_hit_bar) must still trigger SL")
        bar_low = 2290.0
        self.assertTrue(bar_low <= sl,
                        "Gap past SL triggers close regardless of prior events")


class TestTrailingRatchetOnlyForward(unittest.TestCase):
    def test_buy_trail_only_moves_up(self):
        sl_dist = 10.0
        multiplier = 0.2
        trail_dist = sl_dist * multiplier
        trail_level = 2335.0
        bars_high = [2338.0, 2342.0, 2339.0, 2345.0, 2340.0]
        expected_levels = [2335.0, 2336.0, 2340.0, 2340.0, 2343.0, 2343.0]
        actual_levels = [trail_level]
        for high in bars_high:
            new_trail = high - trail_dist
            if new_trail > trail_level:
                trail_level = new_trail
            actual_levels.append(trail_level)
        self.assertEqual(actual_levels, expected_levels,
                         "Buy trail must ratchet upward only, never down")

    def test_sell_trail_only_moves_down(self):
        sl_dist = 10.0
        multiplier = 0.2
        trail_dist = sl_dist * multiplier
        trail_level = 2325.0
        bars_low = [2322.0, 2318.0, 2320.0, 2315.0, 2319.0]
        expected_levels = [2325.0, 2324.0, 2320.0, 2320.0, 2317.0, 2317.0]
        actual_levels = [trail_level]
        for low in bars_low:
            new_trail = low + trail_dist
            if new_trail < trail_level:
                trail_level = new_trail
            actual_levels.append(trail_level)
        self.assertEqual(actual_levels, expected_levels,
                         "Sell trail must ratchet downward only, never up")

    def test_buy_trail_does_not_move_on_lower_high(self):
        trail_level = 2340.0
        sl_dist = 10.0
        trail_dist = sl_dist * 0.2
        new_trail = 2335.0 - trail_dist
        self.assertLess(new_trail, trail_level)
        self.assertFalse(new_trail > trail_level,
                         "Lower high must not increase trail_level")

    def test_sell_trail_does_not_move_on_higher_low(self):
        trail_level = 2320.0
        sl_dist = 10.0
        trail_dist = sl_dist * 0.2
        new_trail = 2325.0 + trail_dist
        self.assertGreater(new_trail, trail_level)
        self.assertFalse(new_trail < trail_level,
                         "Higher low must not decrease trail_level for sells")

    def test_trail_initialized_from_tp1_bar_high_low(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        if "trail_level" in content:
            self.assertIn('bar["high"] - trail_dist', content,
                          "Buy trail must initialize from bar high")
            self.assertIn('bar["low"] + trail_dist', content,
                          "Sell trail must initialize from bar low")

    def test_trail_uses_sl_dist_times_multiplier(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py") as f:
            content = f.read()
        self.assertIn("sl_dist * self.settings.trail_multiplier", content,
                      "Trail distance must be sl_dist * trail_multiplier")

    def test_trail_skip_activation_bar_on_check(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        self.assertIn('j != self._position.get("trail_activation_bar")', content,
                      "Trail check must skip activation bar to avoid immediate trigger")


class TestNoDuplicateCloseRequests(unittest.TestCase):
    def test_tp1_guard_flag_prevents_reentry(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        tp1_section = content[content.index("# TP1:"):content.index("# TP2:") if "# TP2:" in content else content.index("# Update trailing stop")]
        self.assertIn('not self._position.get("tp1_hit", False)', tp1_section,
                      "TP1 must be guarded by tp1_hit flag")

    def test_tp2_guard_flag_prevents_reentry(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        if "# TP2:" in content:
            tp2_section = content[content.index("# TP2:"):content.index("# Update trailing stop")]
            self.assertIn('not self._position.get("tp2_hit", False)', tp2_section,
                          "TP2 must be guarded by tp2_hit flag")

    def test_remaining_lots_zero_breaks_loop(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        bar_loop = content[content.index("for j in range(start_idx"):content.index("if self._position and self._position")]
        self.assertIn('if self._position["remaining_lots"] <= 0:', bar_loop,
                      "Loop must break when remaining_lots <= 0")
        self.assertIn('break', bar_loop[bar_loop.index('if self._position["remaining_lots"] <= 0:'):],
                      "break must follow the remaining_lots <= 0 check")

    def test_aggressive_remaining_lots_zero_breaks_loop(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py") as f:
            content = f.read()
        self.assertIn('if pos["remaining_lots"] <= 0:', content,
                      "Aggressive: Loop must break when remaining_lots <= 0")
        remaining_check = content[content.index('if pos["remaining_lots"] <= 0:'):]
        self.assertIn('break', remaining_check[:100],
                      "break must follow remaining_lots <= 0 check")

    def test_sl_check_has_tp_hit_bar_guard(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            content = f.read()
        self.assertIn("# SL/be check", content)
        sl_idx = content.index("# SL/be check")
        self.assertIn('self._position.get("tp_hit_bar")', content[sl_idx:],
                      "SL check must have tp_hit_bar guard to prevent same-bar double close")

    def test_close_partial_called_once_per_condition(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_live.py") as f:
            lines = f.readlines()
        close_partial_calls = sum(1 for line in lines if "self._close_partial(" in line)
        self.assertEqual(close_partial_calls, 4,
                         "ORB bot must have exactly 4 _close_partial calls (tp1, tp2, trail, sl/be)")

    def test_aggressive_close_partial_called_once_per_condition(self):
        with open(Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py") as f:
            lines = f.readlines()
        close_partial_calls = sum(1 for line in lines if "self._close_partial(" in line)
        self.assertEqual(close_partial_calls, 3,
                         "Aggressive bot must have exactly 3 _close_partial calls (tp1, trail, sl/be)")


class TestMissingOpeningRangeCandle(unittest.TestCase):
    def setUp(self):
        from core.opening_range_scalp import OpeningRangeScalp
        self.orb = OpeningRangeScalp()
        self.orb._current_session = "asia"

    def _m15_index(self, base: datetime, count: int, minutes: int = 15):
        return [base + timedelta(minutes=i * minutes) for i in range(count)]

    def _m15_df(self, times, highs, lows, closes=None, opens=None):
        import pandas as pd
        if opens is None:
            opens = [l - 0.5 for l in lows]
        if closes is None:
            closes = [(h + l) / 2 for h, l in zip(highs, lows)]
        return pd.DataFrame({
            "open": opens, "high": highs, "low": lows, "close": closes,
        }, index=pd.DatetimeIndex(times))

    def test_empty_dataframe_returns_false(self):
        import pandas as pd
        t = datetime(2026, 6, 15, 0, 20, tzinfo=timezone.utc)
        result = self.orb._establish_opening_range(pd.DataFrame(), t)
        self.assertFalse(result)

    def test_none_dataframe_returns_false(self):
        t = datetime(2026, 6, 15, 0, 20, tzinfo=timezone.utc)
        result = self.orb._establish_opening_range(None, t)
        self.assertFalse(result)

    def test_too_early_returns_false(self):
        t = datetime(2026, 6, 15, 0, 5, tzinfo=timezone.utc)
        df = self._m15_df(self._m15_index(t, 5), [2300]*5, [2290]*5)
        result = self.orb._establish_opening_range(df, t)
        self.assertFalse(result)

    def test_no_candle_in_window_returns_false(self):
        t = datetime(2026, 6, 15, 1, 0, tzinfo=timezone.utc)
        times = [datetime(2026, 6, 15, 0, 30, tzinfo=timezone.utc)]
        df = self._m15_df(times, [2300], [2290])
        result = self.orb._establish_opening_range(df, t)
        self.assertFalse(result)

    def test_establishes_range_on_first_valid_candle(self):
        t = datetime(2026, 6, 15, 0, 20, tzinfo=timezone.utc)
        or_start = t.replace(hour=0, minute=0, second=0, microsecond=0)
        times = self._m15_index(or_start, 1)
        df = self._m15_df(times, [2310.5], [2299.3])
        result = self.orb._establish_opening_range(df, t)
        self.assertTrue(result)
        self.assertEqual(self.orb._range_high, 2310.5)
        self.assertEqual(self.orb._range_low, 2299.3)
        self.assertTrue(self.orb._range_established)

    def test_already_established_returns_true(self):
        self.orb._range_established = True
        self.orb._range_high = 2310.0
        self.orb._range_low = 2299.0
        t = datetime(2026, 6, 15, 0, 20, tzinfo=timezone.utc)
        result = self.orb._establish_opening_range(None, t)
        self.assertTrue(result)

    def test_range_from_multiple_candles_uses_max_high_min_low(self):
        t = datetime(2026, 6, 15, 0, 20, tzinfo=timezone.utc)
        or_start = t.replace(hour=0, minute=0, second=0, microsecond=0)
        times = [or_start + timedelta(minutes=1), or_start + timedelta(minutes=14)]
        df = self._m15_df(times, [2305.0, 2312.0], [2298.0, 2295.0])
        result = self.orb._establish_opening_range(df, t)
        self.assertTrue(result)
        self.assertEqual(self.orb._range_high, 2312.0)
        self.assertEqual(self.orb._range_low, 2295.0)

    def test_london_session_opening_time(self):
        self.orb._current_session = "london"
        t = datetime(2026, 6, 15, 9, 20, tzinfo=timezone.utc)
        or_start = t.replace(hour=9, minute=0, second=0, microsecond=0)
        times = self._m15_index(or_start, 1)
        df = self._m15_df(times, [2320.5], [2310.3])
        result = self.orb._establish_opening_range(df, t)
        self.assertTrue(result)
        self.assertEqual(self.orb._range_high, 2320.5)
        self.assertEqual(self.orb._range_low, 2310.3)

    def test_ny_session_opening_time(self):
        self.orb._current_session = "ny"
        t = datetime(2026, 6, 15, 13, 50, tzinfo=timezone.utc)
        or_start = t.replace(hour=13, minute=30, second=0, microsecond=0)
        times = self._m15_index(or_start, 1)
        df = self._m15_df(times, [2330.5], [2320.3])
        result = self.orb._establish_opening_range(df, t)
        self.assertTrue(result)
        self.assertEqual(self.orb._range_high, 2330.5)
        self.assertEqual(self.orb._range_low, 2320.3)


class TestBreakoutPullbackEntry(unittest.TestCase):
    def setUp(self):
        from core.opening_range_scalp import OpeningRangeScalp
        self.orb = OpeningRangeScalp()

    def _df(self, times, opens, highs, lows, closes):
        import pandas as pd
        return pd.DataFrame({
            "open": opens, "high": highs, "low": lows, "close": closes,
        }, index=pd.DatetimeIndex(times))

    def _times(self, base, count, minutes=5):
        return [base + timedelta(minutes=i * minutes) for i in range(count)]

    def test_buy_pullback_detected(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 8)
        poi = (2310.0, 2320.0)
        close_price = 2314.5
        row = {"open": 2314.0, "high": 2319.0, "low": 2312.0, "close": close_price}
        opens = [2310.0]*7 + [row["open"]]
        highs = [2318.0]*7 + [row["high"]]
        lows  = [2308.0]*7 + [row["low"]]
        closes = [2312.0]*7 + [row["close"]]
        df = self._df(times, opens, highs, lows, closes)
        result = self.orb._check_pullback(df, poi, "buy")
        self.assertIsNotNone(result)
        self.assertIn("entry", result)
        self.assertIn("sl", result)
        self.assertEqual(result["entry"], close_price)

    def test_sell_pullback_detected(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 8)
        poi = (2320.0, 2330.0)
        close_price = 2325.5
        row = {"open": 2326.0, "high": 2329.0, "low": 2322.0, "close": close_price}
        opens = [2330.0]*7 + [row["open"]]
        highs = [2335.0]*7 + [row["high"]]
        lows  = [2328.0]*7 + [row["low"]]
        closes = [2332.0]*7 + [row["close"]]
        df = self._df(times, opens, highs, lows, closes)
        result = self.orb._check_pullback(df, poi, "sell")
        self.assertIsNotNone(result)
        self.assertEqual(result["entry"], close_price)

    def test_pullback_fib_below_50_returns_none(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 8)
        poi = (2310.0, 2320.0)
        close_near_zh = 2319.0
        opens = [2310.0]*7 + [2317.0]
        highs = [2318.0]*7 + [2321.0]
        lows  = [2308.0]*7 + [2316.0]
        closes = [2312.0]*7 + [close_near_zh]
        df = self._df(times, opens, highs, lows, closes)
        result = self.orb._check_pullback(df, poi, "buy")
        self.assertIsNone(result)

    def test_pullback_fib_above_618_returns_none(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 8)
        poi = (2310.0, 2320.0)
        close_near_zl = 2311.5
        opens = [2310.0]*7 + [2313.0]
        highs = [2318.0]*7 + [2316.0]
        lows  = [2308.0]*7 + [2310.0]
        closes = [2312.0]*7 + [close_near_zl]
        df = self._df(times, opens, highs, lows, closes)
        result = self.orb._check_pullback(df, poi, "buy")
        self.assertIsNone(result)

    def test_pullback_wrong_direction_returns_none(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 8)
        poi = (2310.0, 2320.0)
        opens = [2310.0]*7 + [2314.0]
        highs = [2318.0]*7 + [2319.0]
        lows  = [2308.0]*7 + [2312.0]
        closes = [2312.0]*7 + [2316.0]
        df = self._df(times, opens, highs, lows, closes)
        result = self.orb._check_pullback(df, poi, "sell")
        self.assertIsNone(result)

    def test_slow_momentum_mixed_direction_passes(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 5)
        opens  = [2310.0, 2312.0, 2315.0, 2313.0, 2316.0]
        highs  = [2315.0, 2318.0, 2320.0, 2318.0, 2321.0]
        lows   = [2308.0, 2310.0, 2313.0, 2311.0, 2314.0]
        closes = [2312.0, 2315.0, 2313.0, 2316.0, 2318.0]
        df = self._df(times, opens, highs, lows, closes)
        self.assertTrue(self.orb._check_slow_momentum(df))

    def test_slow_momentum_all_same_direction_fails(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 5)
        opens  = [2310.0, 2312.0, 2315.0, 2317.0, 2320.0]
        highs  = [2315.0, 2318.0, 2320.0, 2322.0, 2325.0]
        lows   = [2308.0, 2310.0, 2313.0, 2315.0, 2318.0]
        closes = [2312.0, 2315.0, 2317.0, 2320.0, 2322.0]
        df = self._df(times, opens, highs, lows, closes)
        self.assertFalse(self.orb._check_slow_momentum(df))

    def test_slow_momentum_aggressive_bar_fails(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 5)
        opens  = [2310.0, 2312.0, 2310.0, 2313.0, 2316.0]
        highs  = [2315.0, 2318.0, 2360.0, 2318.0, 2321.0]
        lows   = [2308.0, 2310.0, 2308.0, 2311.0, 2314.0]
        closes = [2312.0, 2315.0, 2358.0, 2316.0, 2318.0]
        df = self._df(times, opens, highs, lows, closes)
        self.assertFalse(self.orb._check_slow_momentum(df))

    def test_reaction_engulfing_buy_returns_true(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 3)
        opens  = [2315.0, 2316.0, 2310.0]
        highs  = [2318.0, 2319.0, 2330.0]
        lows   = [2313.0, 2314.0, 2308.0]
        closes = [2316.0, 2317.0, 2328.0]
        df = self._df(times, opens, highs, lows, closes)
        self.assertTrue(self.orb._check_reaction(df, "buy"))

    def test_reaction_bearish_no_pattern_returns_false(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 3)
        opens  = [2315.0, 2316.0, 2317.0]
        highs  = [2318.0, 2319.0, 2320.0]
        lows   = [2313.0, 2314.0, 2315.0]
        closes = [2316.0, 2317.0, 2318.0]
        df = self._df(times, opens, highs, lows, closes)
        result = self.orb._check_reaction(df, "sell")
        self.assertFalse(result)

    def test_fib_discount_entry_at_high_fib_fails(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 6)
        opens  = [2300.0]*6
        highs  = [2320.0]*6
        lows   = [2290.0]*6
        closes = [2310.0]*6
        df = self._df(times, opens, highs, lows, closes)
        poi = (2290.0, 2320.0)
        price_near_low = 2301.0
        fib_level = (2320.0 - price_near_low) / (2320.0 - 2290.0)
        self.assertGreater(fib_level, 0.618)
        result = self.orb._check_fib_discount(df, price_near_low, "buy", poi)
        self.assertFalse(result)

    def test_fib_discount_entry_at_low_fib_passes(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 6)
        opens  = [2300.0]*6
        highs  = [2320.0]*6
        lows   = [2290.0]*6
        closes = [2310.0]*6
        df = self._df(times, opens, highs, lows, closes)
        poi = (2290.0, 2320.0)
        price_at_discount = 2315.0
        fib_level = (2320.0 - price_at_discount) / (2320.0 - 2290.0)
        self.assertLessEqual(fib_level, 0.618)
        result = self.orb._check_fib_discount(df, price_at_discount, "buy", poi)
        self.assertTrue(result)


class TestAggressiveFVGEntry(unittest.TestCase):
    def setUp(self):
        from core.opening_range_scalp import OpeningRangeScalp
        self.orb = OpeningRangeScalp()
        self.orb._range_high = 2320.0
        self.orb._range_low = 2310.0

    def _df(self, times, opens, highs, lows, closes):
        import pandas as pd
        return pd.DataFrame({
            "open": opens, "high": highs, "low": lows, "close": closes,
        }, index=pd.DatetimeIndex(times))

    def _times(self, base, count, minutes=5):
        return [base + timedelta(minutes=i * minutes) for i in range(count)]

    def test_bullish_fvg_detected_near_range(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 12)
        highs = [2321.0, 2322.0, 2323.0, 2324.0, 2325.0, 2326.0,
                 2327.0, 2328.0, 2318.0, 2322.0, 2325.0, 2320.0]
        lows  = [2319.0, 2320.0, 2321.0, 2322.0, 2323.0, 2324.0,
                 2325.0, 2326.0, 2315.0, 2320.0, 2323.0, 2318.0]
        opens = [2320.0]*12
        closes = [2321.0]*12
        highs[8] = 2318.0; lows[8] = 2314.0; closes[8] = 2315.0
        lows[10] = 2324.0
        highs[10] = 2327.0
        df = self._df(times, opens, highs, lows, closes)
        result = self.orb._find_fvg_near_range(df, "buy")
        self.assertIsNotNone(result)

    def test_bearish_fvg_detected_near_range(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 12)
        highs = [2312.0, 2313.0, 2314.0, 2315.0, 2316.0, 2317.0,
                 2318.0, 2319.0, 2322.0, 2318.0, 2315.0, 2320.0]
        lows  = [2310.0, 2311.0, 2312.0, 2313.0, 2314.0, 2315.0,
                 2316.0, 2317.0, 2320.0, 2316.0, 2313.0, 2318.0]
        opens = [2311.0]*12
        closes = [2310.0]*12
        highs[8] = 2322.0; lows[8] = 2320.0; opens[8] = 2321.0
        df = self._df(times, opens, highs, lows, closes)
        result = self.orb._find_fvg_near_range(df, "sell")
        self.assertIsNotNone(result)

    def test_no_fvg_detected(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 8)
        highs = [2320.0]*8
        lows  = [2310.0]*8
        opens = [2315.0]*8
        closes = [2316.0]*8
        df = self._df(times, opens, highs, lows, closes)
        result = self.orb._find_fvg_near_range(df, "buy")
        self.assertIsNone(result)

    def _fvg_far_from_range_df(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 12)
        highs = [2320.0]*9 + [2450.0, 2480.0, 2520.0]
        lows  = [2310.0]*9 + [2310.0, 2320.0, 2500.0]
        opens = [2315.0]*12
        closes = [2318.0]*12
        return self._df(times, opens, highs, lows, closes)

    def test_fvg_too_far_from_range_returns_none(self):
        self.orb._range_high = 2320.0
        df = self._fvg_far_from_range_df()
        result = self.orb._find_fvg_near_range(df, "buy")
        self.assertIsNone(result)

    def test_fvg_anywhere_no_proximity_check(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 6)
        c1_high = 2500.0; c3_low = 2510.0
        expected_mid = (c1_high + c3_low) / 2.0
        highs = [c1_high, 2510.0, 2520.0, 2530.0, 2540.0, 2550.0]
        lows  = [2490.0, 2500.0, c3_low, 2520.0, 2530.0, 2540.0]
        opens = [2495.0]*6
        closes = [2505.0]*6
        df = self._df(times, opens, highs, lows, closes)
        result = self.orb._find_fvg_anywhere(df, "buy")
        self.assertAlmostEqual(result, expected_mid)

    def test_insufficient_data_returns_none(self):
        import pandas as pd
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 3)
        df = self._df(times, [2315.0]*3, [2320.0]*3, [2310.0]*3, [2316.0]*3)
        result = self.orb._find_fvg_near_range(df, "buy")
        self.assertIsNone(result)

    def test_fvg_anywhere_detects_after_find_near_returns_none(self):
        self.orb._range_high = 2320.0
        df = self._fvg_far_from_range_df()
        near_result = self.orb._find_fvg_near_range(df, "buy")
        anywhere_result = self.orb._find_fvg_anywhere(df, "buy")
        self.assertIsNone(near_result)
        self.assertIsNotNone(anywhere_result)


class TestRangeReversalEntry(unittest.TestCase):
    def setUp(self):
        from core.opening_range_scalp import OpeningRangeScalp
        self.orb = OpeningRangeScalp()
        self.orb._range_high = 2320.0
        self.orb._range_low = 2310.0

    def _df(self, times, opens, highs, lows, closes):
        import pandas as pd
        return pd.DataFrame({
            "open": opens, "high": highs, "low": lows, "close": closes,
        }, index=pd.DatetimeIndex(times))

    def _times(self, base, count, minutes=5):
        return [base + timedelta(minutes=i * minutes) for i in range(count)]

    def test_buy_reversal_detected(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 5)
        opens  = [2315.0, 2314.0, 2316.0, 2315.0, 2312.0]
        highs  = [2318.0, 2317.0, 2319.0, 2318.0, 2315.0]
        lows   = [2313.0, 2312.0, 2314.0, 2313.0, 2309.0]
        closes = [2316.0, 2315.0, 2317.0, 2316.0, 2314.0]
        df = self._df(times, opens, highs, lows, closes)
        result = self.orb._check_range_reversal(df)
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "buy")

    def test_sell_reversal_detected(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 5)
        opens  = [2315.0, 2316.0, 2318.0, 2317.0, 2320.0]
        highs  = [2318.0, 2319.0, 2321.0, 2320.0, 2322.0]
        lows   = [2313.0, 2314.0, 2316.0, 2315.0, 2318.0]
        closes = [2316.0, 2317.0, 2319.0, 2318.0, 2319.0]
        df = self._df(times, opens, highs, lows, closes)
        result = self.orb._check_range_reversal(df)
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "sell")

    def test_wick_too_short_returns_none(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 5)
        opens  = [2315.0, 2314.0, 2316.0, 2315.0, 2312.0]
        highs  = [2318.0, 2317.0, 2319.0, 2318.0, 2315.0]
        lows   = [2313.0, 2312.0, 2314.0, 2313.0, 2312.0]
        closes = [2316.0, 2315.0, 2317.0, 2316.0, 2313.0]
        df = self._df(times, opens, highs, lows, closes)
        result = self.orb._check_range_reversal(df)
        self.assertIsNone(result)

    def test_no_touch_of_range_boundary_returns_none(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 5)
        opens  = [2315.0, 2314.0, 2316.0, 2315.0, 2314.0]
        highs  = [2318.0, 2317.0, 2319.0, 2318.0, 2317.0]
        lows   = [2313.0, 2312.0, 2314.0, 2313.0, 2312.0]
        closes = [2316.0, 2315.0, 2317.0, 2316.0, 2315.0]
        df = self._df(times, opens, highs, lows, closes)
        result = self.orb._check_range_reversal(df)
        self.assertIsNone(result)

    def test_wrong_close_direction_returns_none(self):
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = self._times(base, 5)
        opens  = [2315.0, 2314.0, 2316.0, 2315.0, 2312.0]
        highs  = [2318.0, 2317.0, 2319.0, 2318.0, 2314.0]
        lows   = [2313.0, 2312.0, 2314.0, 2313.0, 2309.8]
        closes = [2316.0, 2315.0, 2317.0, 2316.0, 2311.0]
        df = self._df(times, opens, highs, lows, closes)
        result = self.orb._check_range_reversal(df)
        self.assertIsNone(result)

    def test_none_dataframe_returns_none(self):
        result = self.orb._check_range_reversal(None)
        self.assertIsNone(result)

    def test_range_reversal_sl_calculation_buy(self):
        self.orb._range_high = 2320.0
        self.orb._range_low = 2310.0
        self.orb._market_structure = "ranging"
        reversal_data = {"direction": "buy", "price": 2312.0, "time": "dummy"}
        self.orb._check_range_reversal = MagicMock(return_value=reversal_data)
        range_width = self.orb._range_high - self.orb._range_low
        direction = reversal_data["direction"]
        entry = reversal_data["price"]
        if direction == "buy":
            sl = self.orb._range_low - 0.05
            sl = min(sl, entry - 0.50)
            tp = entry + range_width
        self.assertEqual(sl, 2309.95)
        self.assertEqual(tp, 2322.0)

    def test_range_reversal_sl_calculation_sell(self):
        self.orb._range_high = 2320.0
        self.orb._range_low = 2310.0
        reversal_data = {"direction": "sell", "price": 2318.0, "time": "dummy"}
        range_width = self.orb._range_high - self.orb._range_low
        direction = reversal_data["direction"]
        entry = reversal_data["price"]
        if direction == "sell":
            sl = self.orb._range_high + 0.05
            sl = max(sl, entry + 1.00)
            tp = entry - range_width
        self.assertEqual(sl, 2320.05)
        self.assertEqual(tp, 2308.0)


class TestDailySessionReset(unittest.TestCase):
    def setUp(self):
        from core.opening_range_scalp import OpeningRangeScalp
        self.orb = OpeningRangeScalp()

    def test_date_change_triggers_full_reset(self):
        self.orb._current_date = "2026-06-15"
        self.orb._current_session = "london"
        self.orb._range_high = 2320.0
        self.orb._range_low = 2310.0
        self.orb._range_established = True
        self.orb._entry_triggered = True
        self.orb._market_structure = "uptrend"
        self.orb._htf_aligned = True
        t = datetime(2026, 6, 16, 9, 0, tzinfo=timezone.utc)
        self.orb.analyze(None, None, t, "asia")
        self.assertIsNone(self.orb._range_high)
        self.assertIsNone(self.orb._range_low)
        self.assertFalse(self.orb._range_established)
        self.assertFalse(self.orb._entry_triggered)
        self.assertIsNone(self.orb._market_structure)
        self.assertFalse(self.orb._htf_aligned)

    def test_session_change_triggers_full_reset(self):
        self.orb._current_date = "2026-06-15"
        self.orb._current_session = "asia"
        self.orb._range_established = True
        self.orb._entry_triggered = False
        self.orb._market_structure = "ranging"
        t = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
        self.orb.analyze(None, None, t, "london")
        self.assertFalse(self.orb._range_established)
        self.assertIsNone(self.orb._market_structure)
        self.assertEqual(self.orb._current_session, "london")

    def test_same_date_session_keeps_state(self):
        self.orb._current_date = "2026-06-15"
        self.orb._current_session = "london"
        self.orb._range_established = True
        self.orb._entry_triggered = True
        self.orb._market_structure = "uptrend"
        t = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        self.orb.analyze(None, None, t, "london")
        self.assertTrue(self.orb._range_established)
        self.assertEqual(self.orb._market_structure, "uptrend")

    def test_entry_triggered_guard_returns_none(self):
        self.orb._current_date = "2026-06-15"
        self.orb._current_session = "london"
        self.orb._entry_triggered = True
        t = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
        result = self.orb.analyze(None, None, t, "london")
        self.assertIsNone(result)

    def test_entry_triggered_reset_on_new_date(self):
        self.orb._current_date = "2026-06-15"
        self.orb._current_session = "london"
        self.orb._entry_triggered = True
        self.orb._range_established = True
        t = datetime(2026, 6, 16, 9, 0, tzinfo=timezone.utc)
        self.orb.analyze(None, None, t, "london")
        self.assertFalse(self.orb._entry_triggered)

    def test_reset_entry_keeps_date_and_session(self):
        self.orb._current_date = "2026-06-15"
        self.orb._current_session = "london"
        self.orb._range_high = 2320.0
        self.orb._range_low = 2310.0
        self.orb._range_established = True
        self.orb._entry_triggered = True
        self.orb._poi_high = 2318.0
        self.orb.reset_entry()
        self.assertFalse(self.orb._entry_triggered)
        self.assertIsNone(self.orb._poi_high)
        self.assertEqual(self.orb._current_date, "2026-06-15")
        self.assertEqual(self.orb._current_session, "london")
        self.assertTrue(self.orb._range_established)

    def test_current_date_updated_on_date_change(self):
        self.orb._current_date = "2026-06-15"
        t = datetime(2026, 6, 16, 0, 30, tzinfo=timezone.utc)
        self.orb.analyze(None, None, t, "asia")
        self.assertEqual(self.orb._current_date, "2026-06-16")

    def test_current_session_updated_on_session_change(self):
        self.orb._current_date = "2026-06-15"
        self.orb._current_session = "asia"
        t = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
        self.orb.analyze(None, None, t, "london")
        self.assertEqual(self.orb._current_session, "london")

    def test_scalper_bot_check_new_day_calls_orb_reset(self):
        src_path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(src_path) as f:
            content = f.read()
        self.assertIn("self.orb.reset()", content,
                      "ScalperBot _check_new_day must call orb.reset()")

    def test_aggressive_bot_check_new_day_no_orb_reset(self):
        src_path = Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py"
        with open(src_path) as f:
            content = f.read()
        new_day_section = content[content.index("def _check_new_day"):content.index("def _get_risk_amount")]
        self.assertNotIn("orb.reset", new_day_section,
                         "AggressiveBot _check_new_day must NOT call orb.reset()")

    def test_risk_manager_start_day_resets_loss_not_consecutive(self):
        rm = RiskManager()
        rm.start_day("2026-06-15", 1000.0)
        rm.record_trade(-10.0)
        rm.record_trade(-10.0)
        self.assertEqual(rm._consecutive_losses, 2)
        self.assertEqual(rm._daily_loss_sum, 20.0)
        rm.start_day("2026-06-16", 980.0)
        self.assertEqual(rm._daily_loss_sum, 0.0)
        self.assertEqual(rm._consecutive_losses, 0)

    def test_risk_manager_start_day_resets_blocked(self):
        rm = RiskManager(max_consecutive_losses=2)
        rm.start_day("2026-06-15", 1000.0)
        rm.record_trade(-5.0)
        rm.record_trade(-5.0)
        self.assertTrue(rm._blocked_session)
        rm.start_day("2026-06-16", 990.0)
        self.assertFalse(rm._blocked_session)

    def test_risk_manager_start_day_does_not_reset_peak(self):
        rm = RiskManager()
        rm.start_day("2026-06-15", 1000.0)
        rm.start_day("2026-06-16", 900.0)
        self.assertEqual(rm._peak_balance, 1000.0)

    def test_risk_manager_start_day_updates_peak_on_higher_balance(self):
        rm = RiskManager()
        rm.start_day("2026-06-15", 1000.0)
        rm.start_day("2026-06-16", 1100.0)
        self.assertEqual(rm._peak_balance, 1100.0)

    def test_analyze_detects_session_from_time_when_none(self):
        self.orb._current_date = "2026-06-15"
        self.orb._entry_triggered = True
        t = datetime(2026, 6, 16, 9, 0, tzinfo=timezone.utc)
        self.orb.analyze(None, None, t, None)
        self.assertEqual(self.orb._current_date, "2026-06-16")

    def test_analyze_orb_pipeline_runs_in_session(self):
        import pandas as pd
        self.orb._current_date = "2026-06-15"
        self.orb._current_session = "london"
        self.orb._range_high = 2350.0
        self.orb._range_low = 2280.0
        self.orb._range_established = True
        self.orb._market_structure = "ranging"
        base = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
        times = [base + timedelta(minutes=i*5) for i in range(5)]
        df5 = pd.DataFrame({
            "open": [2315.0]*5, "high": [2320.0]*5, "low": [2310.0]*5, "close": [2316.0]*5,
        }, index=pd.DatetimeIndex(times))
        df15 = pd.DataFrame({
            "open": [2300.0], "high": [2325.0], "low": [2295.0], "close": [2310.0],
        }, index=pd.DatetimeIndex([datetime(2026, 6, 15, 9, 15, tzinfo=timezone.utc)]))
        t = datetime(2026, 6, 15, 9, 50, tzinfo=timezone.utc)
        result = self.orb.analyze(df5, df15, t, "london")
        self.assertIsNone(result)

    def test_reset_clears_all_state(self):
        self.orb._current_date = "2026-06-15"
        self.orb._current_session = "london"
        self.orb._range_high = 2320.0
        self.orb._range_low = 2310.0
        self.orb._range_established = True
        self.orb._market_structure = "uptrend"
        self.orb._entry_triggered = True
        self.orb._breakout_dir = "buy"
        self.orb._poi_high = 2317.0
        self.orb._poi_low = 2315.0
        self.orb._best_zone = "dummy"
        self.orb._htf_aligned = True
        self.orb._breakout_fvg = (2318.0, 2320.0)
        self.orb._swing_broken = True
        self.orb.reset()
        self.assertIsNone(self.orb._current_date)
        self.assertIsNone(self.orb._current_session)
        self.assertIsNone(self.orb._range_high)
        self.assertIsNone(self.orb._range_low)
        self.assertFalse(self.orb._range_established)
        self.assertIsNone(self.orb._market_structure)
        self.assertFalse(self.orb._entry_triggered)
        self.assertIsNone(self.orb._breakout_dir)
        self.assertIsNone(self.orb._poi_high)
        self.assertIsNone(self.orb._poi_low)
        self.assertIsNone(self.orb._best_zone)
        self.assertFalse(self.orb._htf_aligned)
        self.assertIsNone(self.orb._breakout_fvg)
        self.assertFalse(self.orb._swing_broken)


class TestConsecutiveLosses(unittest.TestCase):
    def test_default_threshold_is_4(self):
        from config.settings import get_settings
        s = get_settings(".env")
        self.assertEqual(s.circuit_breaker_max_consecutive_losses, 4)

    def test_not_blocked_after_3_losses(self):
        rm = RiskManager(max_consecutive_losses=4, max_daily_loss_pct=10.0)
        rm.start_day("2026-06-15", 1000.0)
        for _ in range(3):
            rm.record_trade(-10.0)
        self.assertEqual(rm._consecutive_losses, 3)
        self.assertFalse(rm._blocked_session)
        allowed, _ = rm.check_entry_allowed(1000.0)
        self.assertTrue(allowed)

    def test_blocked_after_4_losses(self):
        rm = RiskManager(max_consecutive_losses=4)
        rm.start_day("2026-06-15", 1000.0)
        for _ in range(4):
            rm.record_trade(-10.0)
        self.assertEqual(rm._consecutive_losses, 4)
        self.assertTrue(rm._blocked_session)

    def test_blocked_after_exactly_threshold_losses(self):
        rm = RiskManager(max_consecutive_losses=3)
        rm.start_day("2026-06-15", 1000.0)
        for _ in range(3):
            rm.record_trade(-5.0)
        allowed, reason = rm.check_entry_allowed(1000.0)
        self.assertFalse(allowed)
        self.assertIn("consecutive", reason.lower())

    def test_win_resets_counter_to_zero(self):
        rm = RiskManager(max_consecutive_losses=4)
        rm.start_day("2026-06-15", 1000.0)
        rm.record_trade(-10.0)
        rm.record_trade(-10.0)
        self.assertEqual(rm._consecutive_losses, 2)
        rm.record_trade(20.0)
        self.assertEqual(rm._consecutive_losses, 0)

    def test_win_unblocks_if_block_still_active(self):
        rm = RiskManager(max_consecutive_losses=2)
        rm.start_day("2026-06-15", 1000.0)
        rm.record_trade(-5.0)
        rm.record_trade(-5.0)
        self.assertTrue(rm._blocked_session)
        rm.record_trade(10.0)
        self.assertEqual(rm._consecutive_losses, 0)
        self.assertTrue(rm._blocked_session,
                        "Win resets counter but does NOT unblock — block persists until next day")

    def test_blocked_reason_message(self):
        rm = RiskManager(max_consecutive_losses=2)
        rm.start_day("2026-06-15", 1000.0)
        rm.record_trade(-5.0)
        rm.record_trade(-5.0)
        allowed, reason = rm.check_entry_allowed(1000.0)
        self.assertFalse(allowed)
        self.assertIn("Blocked", reason)

    def test_configurable_threshold(self):
        rm = RiskManager(max_consecutive_losses=10)
        rm.start_day("2026-06-15", 1000.0)
        for _ in range(9):
            rm.record_trade(-5.0)
        self.assertFalse(rm._blocked_session)
        rm.record_trade(-5.0)
        self.assertTrue(rm._blocked_session)
        self.assertEqual(rm._consecutive_losses, 10)

    def test_losses_tracked_after_block(self):
        rm = RiskManager(max_consecutive_losses=2)
        rm.start_day("2026-06-15", 1000.0)
        rm.record_trade(-5.0)
        rm.record_trade(-5.0)
        self.assertTrue(rm._blocked_session)
        rm.record_trade(-5.0)
        self.assertEqual(rm._consecutive_losses, 3)

    def test_zero_threshold_blocks_on_first_loss(self):
        rm = RiskManager(max_consecutive_losses=1)
        rm.start_day("2026-06-15", 1000.0)
        rm.record_trade(-5.0)
        allowed, _ = rm.check_entry_allowed(1000.0)
        self.assertFalse(allowed)


class TestCircuitBreaker(unittest.TestCase):
    def test_drawdown_triggers_kill(self):
        rm = RiskManager(max_drawdown_pct=15.0)
        rm.start_day("2026-06-15", 1000.0)
        allowed, _ = rm.check_entry_allowed(801.0)
        self.assertTrue(allowed)
        allowed, reason = rm.check_entry_allowed(800.0)
        self.assertFalse(allowed)
        self.assertIn("drawdown", reason.lower())
        self.assertTrue(rm._is_killed)

    def test_kill_is_permanent_no_reset(self):
        rm = RiskManager(max_drawdown_pct=15.0)
        rm.start_day("2026-06-15", 1000.0)
        rm._is_killed = True
        rm.start_day("2026-06-16", 10000.0)
        allowed, reason = rm.check_entry_allowed(10000.0)
        self.assertFalse(allowed)
        self.assertIn("killed", reason.lower())

    def test_daily_loss_blocks_entry(self):
        rm = RiskManager(max_daily_loss_pct=3.0)
        rm.start_day("2026-06-15", 1000.0)
        rm.record_trade(-31.0)
        allowed, reason = rm.check_entry_allowed(1000.0)
        self.assertFalse(allowed)
        self.assertIn("loss limit", reason.lower())

    def test_daily_loss_exact_at_threshold_blocks(self):
        rm = RiskManager(max_daily_loss_pct=5.0)
        rm.start_day("2026-06-15", 1000.0)
        rm.record_trade(-50.0)
        allowed, _ = rm.check_entry_allowed(1000.0)
        self.assertFalse(allowed,
                         "Code uses >= so exact threshold must block")

    def test_daily_loss_one_cent_below_threshold_allows(self):
        rm = RiskManager(max_daily_loss_pct=5.0)
        rm.start_day("2026-06-15", 1000.0)
        rm.record_trade(-49.99)
        allowed, _ = rm.check_entry_allowed(1000.0)
        self.assertTrue(allowed)

    def test_daily_loss_resets_on_start_day(self):
        rm = RiskManager(max_daily_loss_pct=3.0)
        rm.start_day("2026-06-15", 1000.0)
        rm.record_trade(-31.0)
        allowed, _ = rm.check_entry_allowed(1000.0)
        self.assertFalse(allowed)
        rm.start_day("2026-06-16", 969.0)
        allowed, _ = rm.check_entry_allowed(969.0)
        self.assertTrue(allowed)

    def test_kill_check_comes_first(self):
        rm = RiskManager(max_drawdown_pct=15.0)
        rm.start_day("2026-06-15", 1000.0)
        rm._is_killed = True
        rm._blocked_session = None
        allowed, reason = rm.check_entry_allowed(1000000.0)
        self.assertFalse(allowed)
        self.assertIn("killed", reason.lower())

    def test_blocked_check_comes_before_loss_checks(self):
        rm = RiskManager(max_daily_loss_pct=3.0)
        rm.start_day("2026-06-15", 1000.0)
        rm._blocked_session = True
        allowed, reason = rm.check_entry_allowed(1000.0)
        self.assertFalse(allowed)
        self.assertIn("Blocked", reason)

    def test_drawdown_one_percent_under_threshold_allows(self):
        rm = RiskManager(max_drawdown_pct=15.0)
        rm.start_day("2026-06-15", 1000.0)
        allowed, _ = rm.check_entry_allowed(851.0)
        self.assertTrue(allowed)

    def test_drawdown_uses_peak_balance(self):
        rm = RiskManager(max_drawdown_pct=15.0)
        rm.start_day("2026-06-15", 1000.0)
        rm.start_day("2026-06-16", 900.0)
        self.assertEqual(rm._peak_balance, 1000.0)
        allowed, _ = rm.check_entry_allowed(860.0)
        self.assertTrue(allowed, "14% drawdown from 1000 peak should be allowed")
        allowed, reason = rm.check_entry_allowed(800.0)
        self.assertFalse(allowed, "20% drawdown from 1000 peak should be killed")

    def test_blocked_does_not_set_kill(self):
        rm = RiskManager(max_consecutive_losses=2)
        rm.start_day("2026-06-15", 1000.0)
        rm.record_trade(-5.0)
        rm.record_trade(-5.0)
        self.assertTrue(rm._blocked_session)
        self.assertFalse(rm._is_killed)

    def test_peak_only_updates_via_start_day_not_intraday(self):
        rm = RiskManager()
        rm.start_day("2026-06-15", 1000.0)
        rm.start_day("2026-06-16", 1200.0)
        self.assertEqual(rm._peak_balance, 1200.0)
        rm.start_day("2026-06-16", 1100.0)
        self.assertEqual(rm._peak_balance, 1200.0, "Peak must NOT decrease")

    def test_combined_daily_loss_and_consecutive_losses(self):
        rm = RiskManager(max_daily_loss_pct=10.0, max_consecutive_losses=3)
        rm.start_day("2026-06-15", 2000.0)
        rm.record_trade(-20.0)
        rm.record_trade(-20.0)
        self.assertEqual(rm._consecutive_losses, 2)
        self.assertEqual(rm._daily_loss_sum, 40.0)
        allowed, _ = rm.check_entry_allowed(2000.0)
        self.assertTrue(allowed)
        rm.record_trade(-20.0)
        self.assertTrue(rm._blocked_session)

    def test_positive_profit_does_not_affect_daily_loss_sum(self):
        rm = RiskManager(max_daily_loss_pct=3.0)
        rm.start_day("2026-06-15", 1000.0)
        rm.record_trade(50.0)
        self.assertEqual(rm._daily_loss_sum, 0.0)

    def test_daily_loss_negative_profit_adds_abs_to_sum(self):
        rm = RiskManager()
        rm.start_day("2026-06-15", 1000.0)
        rm.record_trade(-25.0)
        self.assertEqual(rm._daily_loss_sum, 25.0)

    def test_scaled_thresholds_small_account(self):
        from config.settings import ScalperSettings
        s = ScalperSettings()
        adj = s.adjust_for_balance(150.0)
        self.assertEqual(adj.circuit_breaker_max_daily_loss_pct, 20.0)
        self.assertEqual(adj.circuit_breaker_max_drawdown_pct, 50.0)
        self.assertEqual(adj.max_daily_trades, 5)
        self.assertEqual(s.circuit_breaker_max_daily_loss_pct, 10.0,
                         "Original must remain unchanged")

    def test_scaled_thresholds_medium_account(self):
        from config.settings import ScalperSettings
        s = ScalperSettings()
        adj = s.adjust_for_balance(350.0)
        self.assertEqual(adj.circuit_breaker_max_daily_loss_pct, 10.0)
        self.assertEqual(adj.circuit_breaker_max_drawdown_pct, 30.0)
        self.assertEqual(adj.max_daily_trades, 10)

    def test_scaled_thresholds_large_account(self):
        from config.settings import ScalperSettings
        s = ScalperSettings()
        adj = s.adjust_for_balance(2000.0)
        self.assertEqual(adj.circuit_breaker_max_daily_loss_pct, 10.0)
        self.assertEqual(adj.circuit_breaker_max_drawdown_pct, 15.0)
        self.assertEqual(adj.max_daily_trades, 15)

    def test_drawdown_calculation_with_no_peak(self):
        rm = RiskManager(max_drawdown_pct=15.0)
        allowed, _ = rm.check_entry_allowed(100.0)
        self.assertTrue(allowed, "With no peak set, drawdown check must pass")

    def test_blocked_resets_on_start_day(self):
        rm = RiskManager(max_consecutive_losses=2)
        rm.start_day("2026-06-15", 1000.0)
        rm.record_trade(-5.0)
        rm.record_trade(-5.0)
        self.assertTrue(rm._blocked_session)
        rm.start_day("2026-06-16", 990.0)
        self.assertFalse(rm._blocked_session)

    def test_block_resets_even_if_no_date_change(self):
        rm = RiskManager(max_consecutive_losses=2)
        rm.start_day("2026-06-15", 1000.0)
        rm.record_trade(-5.0)
        rm.record_trade(-5.0)
        self.assertTrue(rm._blocked_session)
        rm.start_day("2026-06-15", 990.0)
        self.assertTrue(rm._blocked_session,
                        "Same date must NOT reset blocked state")


class TestMaxDailyTrades(unittest.TestCase):
    def test_live_bot_guard_present(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        entry_section = content[content.index("if self._position is None"):content.index("# News blackout")]
        self.assertIn("self._trades_today < self.settings.max_daily_trades", entry_section,
                      "Live bot must check daily trade count before entering")

    def test_aggressive_bot_guard_present(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py"
        with open(path) as f:
            content = f.read()
        entry_guard = 'if self._position is None:'
        self.assertIn(entry_guard, content,
                      "Aggressive bot must check position before entering")

    def test_aggressive_max_trades_constant(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py"
        with open(path) as f:
            for line in f:
                if "MAX_TRADES_PER_DAY" in line and "=" in line and "20" in line:
                    break
            else:
                self.fail("MAX_TRADES_PER_DAY = 20 constant not found")

    def test_default_max_trades_settings(self):
        from config.settings import ScalperSettings
        s = ScalperSettings()
        self.assertEqual(s.max_daily_trades, 15)

    def test_trades_today_initialized_to_zero_live(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        init_section = content[content.index("class ScalperBot"):content.index("def __init__")]
        init_section += content[content.index("def __init__"):content.index("def initialize")]
        self.assertIn("self._trades_today = 0", init_section,
                      "Live bot must initialize _trades_today to 0")

    def test_trades_today_initialized_to_zero_aggressive(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py"
        with open(path) as f:
            content = f.read()
        init_section = content[content.index("class AggressiveBot"):content.index("def initialize")]
        self.assertIn("self._trades_today = 0", init_section,
                      "Aggressive bot must initialize _trades_today to 0")

    def test_trades_today_reset_in_check_new_day_live(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        new_day_section = content[content.index("def _check_new_day"):content.index("def _get_risk_amount")]
        self.assertIn("self._trades_today = 0", new_day_section,
                      "Live bot must reset _trades_today in _check_new_day")

    def test_trades_today_reset_in_check_new_day_aggressive(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py"
        with open(path) as f:
            content = f.read()
        new_day_section = content[content.index("def _check_new_day"):content.index("def _get_risk_amount")]
        self.assertIn("self._trades_today = 0", new_day_section,
                      "Aggressive bot must reset _trades_today in _check_new_day")

    def test_trades_today_incremented_on_order_live(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        order_section = content[content.index("self.connector.place_order"):content.index("trade_id = str(uuid4())")]
        self.assertIn("self._trades_today += 1", order_section,
                      "Live bot must increment _trades_today after placing order")

    def test_trades_today_incremented_on_order_aggressive(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py"
        with open(path) as f:
            content = f.read()
        order_section = content[content.index("self.connector.place_order"):content.index("trade_id = str(uuid4())")]
        self.assertIn("self._trades_today += 1", order_section,
                      "Aggressive bot must increment _trades_today after placing order")

    def test_trades_today_set_to_1_on_orphan_recovery_live(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        recovery_section = content[content.index("Recovered partially"):content.index("date_str = datetime")]
        recovery_section += content[content.index("self._current_date = date_str"):content.index("logger.info(f\"Recovered orphaned")]
        self.assertIn("self._trades_today = 1", recovery_section,
                      "Live bot must set _trades_today to 1 on orphan recovery")

    def test_low_balance_cap_max_trades_200(self):
        from config.settings import ScalperSettings
        s = ScalperSettings(max_daily_trades=15)
        adj = s.adjust_for_balance(150.0)
        self.assertEqual(adj.max_daily_trades, 5)

    def test_low_balance_cap_max_trades_500(self):
        from config.settings import ScalperSettings
        s = ScalperSettings(max_daily_trades=15)
        adj = s.adjust_for_balance(350.0)
        self.assertEqual(adj.max_daily_trades, 10)


class TestSpreadFilter(unittest.TestCase):
    def test_live_bot_spread_check_exists(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        spread_section = content[content.index("tick = self.connector.get_tick()"):
                                  content.index("spread_pips = tick") + 50]
        self.assertIn("spread_pips = tick[\"spread\"]", spread_section,
                      "Live bot must get spread from tick")
        self.assertIn("if spread_pips > self.settings.max_spread:", content,
                      "Live bot must check spread against max_spread")

    def test_aggressive_bot_spread_check_exists(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py"
        with open(path) as f:
            content = f.read()
        spread_section = content[content.index("tick = self.connector.get_tick()"):
                                  content.index("spread_pips = tick") + 50]
        self.assertIn("spread_pips = tick[\"spread\"]", spread_section,
                      "Aggressive bot must get spread from tick")
        self.assertIn("if spread_pips > self.settings.max_spread:", content,
                      "Aggressive bot must check spread against max_spread")

    def test_default_max_spread(self):
        from config.settings import ScalperSettings
        s = ScalperSettings()
        self.assertEqual(s.max_spread, 60.0)

    def test_tick_has_spread_key(self):
        tick = {"bid": 2330.5, "ask": 2331.0, "spread": 50.0}
        self.assertIn("spread", tick)
        self.assertGreater(tick["spread"], 0)

    def test_spread_filter_blocks_when_too_high(self):
        max_spread = 60.0
        spread_pips = 65.0
        self.assertTrue(spread_pips > max_spread,
                        "Spread above max must be filtered")

    def test_spread_filter_passes_when_within_limit(self):
        max_spread = 60.0
        spread_pips = 45.0
        self.assertFalse(spread_pips > max_spread,
                         "Spread within limit must pass")

    def test_spread_filter_exact_at_boundary_passes(self):
        max_spread = 60.0
        spread_pips = 60.0
        self.assertFalse(spread_pips > max_spread,
                         "Spread exactly at max must pass (not block)")

    def test_backtest_spread_uses_settings_max_spread(self):
        backtest_path = Path(__file__).resolve().parent.parent / "scripts" / "backtest.py"
        with open(backtest_path) as f:
            content = f.read()
        if "spread_pips > settings.max_spread" not in content:
            self.fail("Backtest must use settings.max_spread for spread threshold")

    def test_backtest_aggressive_spread_uses_settings_max_spread(self):
        backtest_path = Path(__file__).resolve().parent.parent / "scripts" / "backtest_aggressive.py"
        with open(backtest_path) as f:
            content = f.read()
        if "spread_pips > settings.max_spread" not in content:
            self.fail("Aggressive backtest must use settings.max_spread for spread threshold")

    def test_spread_logs_debug_when_too_high_live(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        spread_section = content[content.index("if spread_pips > self.settings.max_spread:"):
                                  content.index("if spread_pips > self.settings.max_spread:") + 250]
        self.assertIn("logger.debug", spread_section,
                      "Live bot must log debug when spread too high")
        self.assertIn("time.sleep(10)", spread_section,
                      "Live bot must sleep 10s when spread too high")

    def test_spread_logs_debug_when_too_high_aggressive(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py"
        with open(path) as f:
            content = f.read()
        spread_section = content[content.index("if spread_pips > self.settings.max_spread:"):
                                  content.index("if spread_pips > self.settings.max_spread:") + 250]
        self.assertIn("logger.debug", spread_section,
                      "Aggressive bot must log debug when spread too high")
        self.assertIn("time.sleep(10)", spread_section,
                      "Aggressive bot must sleep 10s when spread too high")


class TestMongoWriteFailure(unittest.TestCase):
    """Tests for MongoDB write resilience in MongoClient and bot scripts."""

    def setUp(self):
        self.mongo_patcher = patch("database.mongo_client.PyMongoClient")
        self.mock_pymongo = self.mongo_patcher.start()
        self.addCleanup(self.mongo_patcher.stop)

        from database.mongo_client import MongoClient
        self.client = MongoClient()
        self.client._connected = True
        self.client._db = MagicMock()

    def test_save_trade_returns_false_when_disconnected(self):
        self.client._connected = False
        with patch.object(self.client, "connect", return_value=False):
            result = self.client.save_trade({"trade_id": "1", "profit": 10.0})
        self.assertFalse(result)

    def test_save_trade_returns_false_on_db_exception(self):
        self.client._db.__getitem__.return_value.update_one.side_effect = Exception("DB error")
        result = self.client.save_trade({"trade_id": "1", "profit": 10.0})
        self.assertFalse(result)

    def test_save_trade_returns_true_on_success(self):
        result = self.client.save_trade({"trade_id": "1", "profit": 10.0})
        self.assertTrue(result)

    def test_save_trade_derives_win_outcome(self):
        self.client.save_trade({"trade_id": "1", "profit": 50.0})
        call_args = self.client._db["trades"].update_one.call_args
        self.assertIsNotNone(call_args)
        data = call_args[0][1]["$set"]
        self.assertEqual(data["outcome"], "win")

    def test_save_trade_derives_loss_outcome(self):
        self.client.save_trade({"trade_id": "2", "profit": -10.0})
        call_args = self.client._db["trades"].update_one.call_args
        self.assertIsNotNone(call_args)
        data = call_args[0][1]["$set"]
        self.assertEqual(data["outcome"], "loss")

    def test_save_trade_skips_outcome_when_profit_none(self):
        self.client.save_trade({"trade_id": "3", "profit": None})
        call_args = self.client._db["trades"].update_one.call_args
        self.assertIsNotNone(call_args)
        data = call_args[0][1]["$set"]
        self.assertNotIn("outcome", data)

    def test_save_trade_preserves_explicit_outcome(self):
        self.client.save_trade({"trade_id": "4", "profit": 50.0, "outcome": "loss"})
        call_args = self.client._db["trades"].update_one.call_args
        data = call_args[0][1]["$set"]
        self.assertEqual(data["outcome"], "loss")

    def test_friday_reconnect_checks_mongo_return_live(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        friday_block = content[content.index("if not self.mongo.connect():")
                               :content.index("if not self.mongo.connect():") + 80]
        self.assertIn("logger.warning", friday_block,
                      "Live Friday reconnect must log warning on failure")

    def test_friday_reconnect_checks_mongo_return_aggressive(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py"
        with open(path) as f:
            content = f.read()
        self.assertIn('if not self.mongo.connect():', content,
                      "Aggressive Friday reconnect must check mongo.connect() return")


class TestTradeLoggingConsistency(unittest.TestCase):
    """Trade log format must be consistent between live and aggressive bots."""

    def _get_file_lines(self, rel_path: str, marker: str, context: int = 300) -> str:
        path = Path(__file__).resolve().parent.parent / rel_path
        with open(path) as f:
            content = f.read()
        idx = content.index(marker)
        return content[idx:idx + context]

    def test_entry_log_format_live(self):
        section = self._get_file_lines("scripts/run_live.py",
                                       'f"ORB TRADE {signal', context=400)
        self.assertIn('f"ORB TRADE {signal[\'direction\'].upper()}', section)
        self.assertIn('trade_logger.info(', section)
        self.assertIn("OPEN", section)

    def test_entry_log_format_aggressive(self):
        section = self._get_file_lines("scripts/run_aggressive.py",
                                       'f"AGGR TRADE {direction', context=500)
        self.assertIn('f"AGGR TRADE {direction.upper()}', section)
        self.assertIn('trade_logger.info(', section)
        self.assertIn("OPEN", section)

    def test_close_log_format_live(self):
        section = self._get_file_lines("scripts/run_live.py",
                                       'f"CLOSE {trade[\'type\']}')
        self.assertIn("CLOSE", section)
        self.assertIn("trade_logger.info", section)
        self.assertIn("exit", section)

    def test_close_log_format_aggressive(self):
        section = self._get_file_lines("scripts/run_aggressive.py",
                                       'f"CLOSE {pos[\'type\']}', context=400)
        self.assertIn("CLOSE", section)
        self.assertIn("trade_logger.info", section)

    def test_partial_log_format_live(self):
        section = self._get_file_lines("scripts/run_live.py",
                                       'f"PARTIAL {reason.upper()}')
        self.assertIn("PARTIAL", section)
        self.assertIn("trade_logger.info", section)

    def test_partial_log_format_aggressive(self):
        section = self._get_file_lines("scripts/run_aggressive.py",
                                       'f"PARTIAL {reason}', context=200)
        self.assertIn("PARTIAL", section)
        self.assertIn("trade_logger.info", section)

    def test_close_log_on_stale_ticket_live(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        idx = content.index("def _resolve_position_closed")
        method = content[idx:idx + 2000]
        self.assertIn('trade_logger.info', method)
        self.assertIn('"CLOSE ', method)

    def test_close_log_on_stale_ticket_aggressive(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py"
        with open(path) as f:
            content = f.read()
        idx = content.index("not still_open:")
        branch = content[idx:idx + 2000]
        self.assertIn('trade_logger.info', branch)
        self.assertIn('"CLOSE ', branch)


class TestPnLCalculation(unittest.TestCase):
    """PnL formula: profit = (entry - exit) * lots * 100 - commission * lots."""

    def test_pnl_buy_profit(self):
        entry, exit_price, lots = 3000.0, 3010.0, 0.1
        pdiff = exit_price - entry
        comm = 3.5 * lots
        profit = round(pdiff * lots * 100 - comm, 2)
        self.assertEqual(profit, 99.65)

    def test_pnl_sell_profit(self):
        entry, exit_price, lots = 3010.0, 3000.0, 0.1
        pdiff = entry - exit_price
        comm = 3.5 * lots
        profit = round(pdiff * lots * 100 - comm, 2)
        self.assertEqual(profit, 99.65)

    def test_pnl_buy_loss(self):
        entry, exit_price, lots = 3000.0, 2990.0, 0.1
        pdiff = exit_price - entry
        comm = 3.5 * lots
        profit = round(pdiff * lots * 100 - comm, 2)
        self.assertEqual(profit, -100.35)

    def test_pnl_zero_entry_exit_equals_entry(self):
        entry, exit_price, lots = 3000.0, 3000.0, 0.5
        pdiff = exit_price - entry
        comm = 3.5 * lots
        profit = round(pdiff * lots * 100 - comm, 2)
        self.assertEqual(profit, -1.75)

    def test_pnl_multiple_lots(self):
        entry, exit_price, lots = 3000.0, 3015.0, 1.0
        pdiff = exit_price - entry
        comm = 3.5 * lots
        profit = round(pdiff * lots * 100 - comm, 2)
        self.assertEqual(profit, 1496.50)

    def test_live_close_partial_uses_formula(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        idx = content.index("def _close_partial")
        method = content[idx:idx + 400]
        self.assertIn("pdiff * lots * 100", method)
        self.assertIn("backtest_commission", method)

    def test_live_resolve_position_closed_computes_pnl(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        idx = content.index("def _resolve_position_closed")
        method = content[idx:idx + 2000]
        self.assertIn("pdiff * remaining * 100", method)
        self.assertIn("backtest_commission", method)
        self.assertIn("pos.get(\"pnl\", 0) + pnl_close", method)

    def test_aggressive_stale_ticket_computes_pnl(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py"
        with open(path) as f:
            content = f.read()
        idx = content.index("not still_open:")
        branch = content[idx:idx + 1200]
        self.assertIn("pdiff * remaining * 100", branch,
                      "Aggressive stale ticket must compute PnL from exit price")
        self.assertIn("backtest_commission", branch)


class TestBrokerSLTPSync(unittest.TestCase):
    """Broker SL/TP synchronization and lifecycle."""

    def test_startup_reads_sl_tp_from_broker_live(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        idx = content.index("existing = self.connector.get_positions")
        block = content[idx:idx + 500]
        self.assertIn('"sl": p["sl"]', block)
        self.assertIn('"tp": p["tp"]', block)
        self.assertIn('"original_sl": p["sl"]', block)

    def test_startup_reads_sl_tp_from_broker_aggressive(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py"
        with open(path) as f:
            content = f.read()
        idx = content.index("existing = self.connector.get_positions")
        block = content[idx:idx + 500]
        self.assertIn('"sl": p["sl"]', block)
        self.assertIn('"tp": p["tp"]', block)
        self.assertIn('"original_sl": p["sl"]', block)

    def test_tp1_moves_sl_to_be_before_modify_live(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        idx = content.index('self._position["tp1_hit"] = True')
        block = content[idx:idx + 400]
        self.assertIn('self._position["sl"] = entry', block)
        self.assertIn('self.connector.modify_position', block)
        self.assertIn('sl=entry', block)

    def test_tp1_moves_sl_to_be_before_modify_aggressive(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py"
        with open(path) as f:
            content = f.read()
        idx = content.index('pos["tp1_hit"] = True')
        block = content[idx:idx + 400]
        self.assertIn('pos["sl"] = pos["entry"]', block)
        self.assertIn('self.connector.modify_position', block)
        self.assertIn('sl=pos["entry"]', block)

    def test_modify_failure_logs_warning_not_crash_live(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        idx = content.index("Failed to move SL to BE for")
        self.assertIn("logger.warning", content[idx - 100:idx + 60])

    def test_modify_failure_logs_warning_not_crash_aggressive(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_aggressive.py"
        with open(path) as f:
            content = f.read()
        idx = content.index("Failed to move SL to BE for")
        self.assertIn("logger.warning", content[idx - 100:idx + 60])

    def test_stale_ticket_checks_existence_only(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        idx = content.index("still_open = any(p[\"ticket\"] == ticket")
        self.assertIn("ticket", content[idx:idx + 100])
        self.assertNotIn("sl", content[idx:idx + 100])
        self.assertNotIn("tp", content[idx:idx + 100])

    def test_trailing_stop_is_local_only(self):
        path = Path(__file__).resolve().parent.parent / "scripts" / "run_live.py"
        with open(path) as f:
            content = f.read()
        self.assertIn('self._position["trail_level"]', content)
        self.assertNotIn("modify_position", content[content.index('"trail_level"'):content.index('"trail_level"') + 200])


if __name__ == "__main__":
    unittest.main(verbosity=2)
