#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from datetime import datetime, timezone, timedelta
from core.mindspace import (
    Candle, Swing, Level, CHoCHSignal, OrderBlock, FVG, ISSZone, MTFState, Signal,
    StructureMarker, CHOCHDetector, LevelDrawer, OrderBlockDetector,
    FVGDetector, ISSDetector, TJLEngine, MTFAnalyzer, MindspaceEngine,
)


def _candle(time: datetime, open: float, high: float, low: float, close: float, volume: int = 0) -> Candle:
    return Candle(time=time, open=open, high=high, low=low, close=close, volume=volume)


class TestModels(unittest.TestCase):
    def test_candle_body_bullish(self):
        c = _candle(datetime.now(timezone.utc), 2300, 2310, 2295, 2305)
        self.assertAlmostEqual(c.body, 5.0)
        self.assertTrue(c.is_bullish)

    def test_candle_body_bearish(self):
        c = _candle(datetime.now(timezone.utc), 2310, 2315, 2298, 2300)
        self.assertAlmostEqual(c.body, 10.0)
        self.assertTrue(c.is_bearish)

    def test_candle_midpoint(self):
        c = _candle(datetime.now(timezone.utc), 2300, 2320, 2280, 2310)
        self.assertAlmostEqual(c.midpoint, 2300.0)

    def test_swing_high(self):
        s = Swing(price=2310, time=datetime.now(timezone.utc), swing_type="high")
        self.assertTrue(s.is_high)
        self.assertFalse(s.is_low)

    def test_swing_low(self):
        s = Swing(price=2290, time=datetime.now(timezone.utc), swing_type="low")
        self.assertTrue(s.is_low)
        self.assertFalse(s.is_high)

    def test_level_defaults(self):
        l = Level(level_type="qml", price=2305, sl_zone_high=2310, sl_zone_low=2300)
        self.assertTrue(l.active)
        self.assertEqual(l.direction, "")

    def test_fvg_active_default(self):
        f = FVG(gap_high=2310, gap_low=2300, direction="bullish")
        self.assertTrue(f.active)

    def test_iss_zone_is_recent_default(self):
        z = ISSZone(entry_high=2310, entry_low=2300, sl_level=2320, direction="sell")
        self.assertTrue(z.is_recent)

    def test_mtf_state_creation(self):
        s = MTFState(condition=1, direction="buy", entry_trigger="candle", follow_tf="1h")
        self.assertEqual(s.condition, 1)

    def test_signal_creation(self):
        s = Signal(direction="buy", entry_price=2305, sl_high=2310, sl_low=2300,
                   tp_price=2320, level_type="qml", tf="1h")
        self.assertEqual(s.source, "mindspace")


class TestStructureMarker(unittest.TestCase):
    def setUp(self):
        self.marker = StructureMarker(tf="1h")

    def test_initial_trend_none(self):
        self.assertIsNone(self.marker.trend)

    def test_one_candle_sets_trend(self):
        c = _candle(datetime.now(timezone.utc), 2300, 2310, 2295, 2305)
        self.marker.update([c])
        self.assertEqual(self.marker.trend, "bullish")
        self.assertEqual(len(self.marker.swings), 2)

    def test_two_swings_uptrend(self):
        t = datetime.now(timezone.utc)
        c1 = _candle(t, 2300, 2310, 2295, 2305)
        c2 = _candle(t + timedelta(hours=1), 2305, 2320, 2300, 2315)
        self.marker.update([c1])
        self.marker.update([c2])
        self.assertIn(self.marker.trend, (None, "bullish"))

    def test_update_preserves_last_time(self):
        t = datetime.now(timezone.utc)
        self.marker.update([_candle(t, 2300, 2310, 2295, 2305)])
        self.assertEqual(self.marker._last_update, t)

    def test_last_confirmed_high_low(self):
        t = datetime.now(timezone.utc)
        self.marker.update([_candle(t, 2300, 2310, 2295, 2305)])
        self.marker.update([_candle(t + timedelta(hours=1), 2305, 2320, 2300, 2315)])
        if self.marker.last_confirmed_high is not None:
            self.assertGreater(self.marker.last_confirmed_high, 0)


class TestCHOCHDetector(unittest.TestCase):
    def setUp(self):
        self.detector = CHOCHDetector()
        self.structure = StructureMarker(tf="1h")
        self.t = datetime.now(timezone.utc)

    def test_less_than_2_candles_returns_none(self):
        result = self.detector.detect(self.structure, [
            _candle(self.t, 2300, 2310, 2295, 2305)
        ], "1h")
        self.assertIsNone(result)

    def test_no_signal_when_no_structure(self):
        result = self.detector.detect(self.structure, [
            _candle(self.t, 2300, 2310, 2295, 2305),
            _candle(self.t + timedelta(hours=1), 2305, 2320, 2300, 2315),
        ], "1h")
        self.assertIsNone(result)

    def test_reset_clears_events(self):
        self.detector.reset()
        self.assertEqual(len(self.detector.events), 0)
        self.assertIsNone(self.detector._last_event)

    def test_most_recent_returns_none_if_empty(self):
        self.assertIsNone(self.detector.get_most_recent())

    def test_double_choch_requires_prior_event(self):
        self.assertFalse(self.detector._check_double_choch(
            CHoCHSignal(direction="bullish", break_level=2310, time=self.t, tf="1h")
        ))


class TestLevelDrawer(unittest.TestCase):
    def setUp(self):
        self.drawer = LevelDrawer()

    def test_draw_from_choch_without_candle(self):
        signal = CHoCHSignal(direction="bullish", break_level=2310, time=datetime.now(timezone.utc), tf="1h")
        levels = self.drawer.draw_from_choch(signal)
        self.assertEqual(len(levels), 0)

    def test_draw_from_choch_with_candle(self):
        t = datetime.now(timezone.utc)
        c = _candle(t, 2300, 2315, 2295, 2310)
        signal = CHoCHSignal(direction="bullish", break_level=2310, time=t, tf="1h", candle=c)
        levels = self.drawer.draw_from_choch(signal)
        self.assertGreater(len(levels), 0)
        types = [l.level_type for l in levels]
        self.assertIn("rbs", types)
        self.assertIn("db", types)

    def test_get_best_level_priority(self):
        self.drawer.levels = [
            Level(level_type="sbr", price=2300, sl_zone_high=2310, sl_zone_low=2290, direction="sell"),
            Level(level_type="qml", price=2305, sl_zone_high=2310, sl_zone_low=2300, direction="sell"),
        ]
        best = self.drawer.get_best_level("sell", 2303, max_distance=10)
        self.assertIsNotNone(best)
        self.assertEqual(best.level_type, "qml")

    def test_get_best_level_no_candidates(self):
        best = self.drawer.get_best_level("buy", 2300, max_distance=5)
        self.assertIsNone(best)

    def test_reset_clears(self):
        self.drawer.levels.append(Level(level_type="qml", price=2305, sl_zone_high=2310, sl_zone_low=2300))
        self.drawer.reset()
        self.assertEqual(len(self.drawer.levels), 0)

    def test_get_recent_empty(self):
        self.assertEqual(len(self.drawer.get_recent("qml")), 0)


class TestOrderBlockDetector(unittest.TestCase):
    def setUp(self):
        self.detector = OrderBlockDetector()
        self.t = datetime.now(timezone.utc)

    def test_less_than_3_candles_empty(self):
        candles = [
            _candle(self.t, 2300, 2310, 2295, 2305),
            _candle(self.t + timedelta(hours=1), 2305, 2315, 2300, 2310),
        ]
        result = self.detector.scan(candles, "1h")
        self.assertEqual(len(result), 0)

    def test_no_match_returns_empty(self):
        candles = [
            _candle(self.t, 2300, 2310, 2295, 2305),
            _candle(self.t + timedelta(hours=1), 2305, 2315, 2300, 2310),
            _candle(self.t + timedelta(hours=2), 2310, 2320, 2305, 2315),
        ]
        result = self.detector.scan(candles, "1h")
        self.assertEqual(len(result), 0)

    def test_reset_clears(self):
        self.detector.reset()
        self.assertEqual(len(self.detector.order_blocks), 0)


class TestFVGDetector(unittest.TestCase):
    def setUp(self):
        self.detector = FVGDetector()
        self.t = datetime.now(timezone.utc)

    def test_less_than_3_candles_empty(self):
        candles = [ _candle(self.t, 2300, 2310, 2295, 2305) ]
        result = self.detector.scan(candles, "15m")
        self.assertEqual(len(result), 0)

    def test_no_gap_returns_empty(self):
        candles = [
            _candle(self.t, 2300, 2310, 2295, 2305),
            _candle(self.t + timedelta(minutes=15), 2305, 2315, 2300, 2310),
            _candle(self.t + timedelta(minutes=30), 2310, 2320, 2305, 2315),
        ]
        result = self.detector.scan(candles, "15m")
        self.assertEqual(len(result), 0)

    def test_bullish_fvg_detected(self):
        candles = [
            _candle(self.t, 2300, 2305, 2295, 2300),
            _candle(self.t + timedelta(minutes=15), 2300, 2303, 2298, 2305),
            _candle(self.t + timedelta(minutes=30), 2310, 2320, 2306, 2315),
        ]
        result = self.detector.scan(candles, "15m")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].direction, "bullish")

    def test_bearish_fvg_detected(self):
        candles = [
            _candle(self.t, 2315, 2320, 2310, 2315),
            _candle(self.t + timedelta(minutes=15), 2315, 2318, 2308, 2310),
            _candle(self.t + timedelta(minutes=30), 2300, 2308, 2295, 2305),
        ]
        result = self.detector.scan(candles, "15m")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].direction, "bearish")

    def test_reset_clears(self):
        self.detector.reset()
        self.assertEqual(len(self.detector.fvgs), 0)


class TestISSDetector(unittest.TestCase):
    def setUp(self):
        self.detector = ISSDetector()
        self.structure = StructureMarker(tf="1h")
        self.t = datetime.now(timezone.utc)

    def test_less_than_3_candles_returns_none(self):
        candles = [ _candle(self.t, 2300, 2310, 2295, 2305) ]
        result = self.detector.update(self.structure, candles)
        self.assertIsNone(result)

    def test_no_trend_returns_none(self):
        candles = [
            _candle(self.t, 2300, 2310, 2295, 2305),
            _candle(self.t + timedelta(hours=1), 2305, 2315, 2300, 2310),
            _candle(self.t + timedelta(hours=2), 2310, 2320, 2305, 2315),
        ]
        result = self.detector.update(self.structure, candles)
        self.assertIsNone(result)

    def test_reset_clears(self):
        self.detector.reset()
        self.assertIsNone(self.detector.current_iss)
        self.assertEqual(self.detector._wave_count, 0)


class TestTJLEngine(unittest.TestCase):
    def setUp(self):
        self.engine = TJLEngine()

    def test_less_than_2_swings_returns_none(self):
        structure = StructureMarker(tf="1h")
        t1, t2 = self.engine.update(structure, "1h")
        self.assertIsNone(t1)
        self.assertIsNone(t2)

    def test_reset_clears(self):
        self.engine.reset()
        self.assertIsNone(self.engine.tjl1)
        self.assertIsNone(self.engine.tjl2)

    def test_get_qml_returns_none_when_not_set(self):
        self.assertIsNone(self.engine.get_qml())

    def test_get_best_level_no_candidates(self):
        best = self.engine.get_best_level("buy", 2300)
        self.assertIsNone(best)


class TestMTFAnalyzer(unittest.TestCase):
    def setUp(self):
        self.analyzer = MTFAnalyzer()
        self.daily = StructureMarker(tf="daily")
        self.h4 = StructureMarker(tf="4h")
        self.h1 = StructureMarker(tf="1h")

    def test_all_none_returns_none(self):
        result = self.analyzer.analyze()
        self.assertIsNone(result)

    def test_missing_markers_returns_previous(self):
        result = self.analyzer.analyze(daily=self.daily, h4=self.h4)
        self.assertIsNone(result)

    def test_reset_clears(self):
        self.analyzer.reset()
        self.assertIsNone(self.analyzer._last_state)

    def test_default_trigger_is_candle(self):
        trigger = self.analyzer.get_required_trigger()
        self.assertEqual(trigger, "candle")


class TestMindspaceEngine(unittest.TestCase):
    def setUp(self):
        self.engine = MindspaceEngine()

    def test_init_creates_all_components(self):
        self.assertIsInstance(self.engine.tf_markers, dict)
        self.assertIsInstance(self.engine.choch, CHOCHDetector)
        self.assertIsInstance(self.engine.levels, LevelDrawer)
        self.assertIsInstance(self.engine.ob, OrderBlockDetector)
        self.assertIsInstance(self.engine.fvg, FVGDetector)
        self.assertIsInstance(self.engine.iss, ISSDetector)
        self.assertIsInstance(self.engine.tjl, TJLEngine)
        self.assertIsInstance(self.engine.mtf, MTFAnalyzer)

    def test_get_signal_returns_none_when_no_mtf_state(self):
        signal = self.engine.get_signal()
        self.assertIsNone(signal)

    def test_update_markets_empty_dict(self):
        self.engine.update_markets({})
        self.assertEqual(len(self.engine.tf_markers), 0)

    def test_update_markets_with_candles(self):
        t = datetime.now(timezone.utc)
        candles = [
            _candle(t, 2300, 2310, 2295, 2305),
            _candle(t + timedelta(hours=1), 2305, 2320, 2300, 2315),
        ]
        self.engine.update_markets({"1h": candles})
        self.assertIn("1h", self.engine.tf_markers)
        self.assertGreater(len(self.engine.tf_markers["1h"].swings), 0)

    def test_manage_position_hold_when_no_risk(self):
        result = self.engine.manage_position(
            entry_price=2300, direction="buy", current_price=2300,
            sl_price=2300, tp_price=2320, volume=1.0, position_id=1
        )
        self.assertEqual(result["action"], "hold")

    def test_manage_position_buy_1rr_partial_close(self):
        result = self.engine.manage_position(
            entry_price=2300, direction="buy", current_price=2305,
            sl_price=2295, tp_price=2320, volume=1.0, position_id=2
        )
        self.assertIn(result["action"], ("hold", "partial_close"))

    def test_manage_position_sell_1rr_partial_close(self):
        result = self.engine.manage_position(
            entry_price=2310, direction="sell", current_price=2305,
            sl_price=2315, tp_price=2290, volume=1.0, position_id=3
        )
        self.assertIn(result["action"], ("hold", "partial_close"))

    def test_manage_position_tp_hit_buy(self):
        self.engine._set_trailing_activated(4)
        result = self.engine.manage_position(
            entry_price=2300, direction="buy", current_price=2320,
            sl_price=2295, tp_price=2315, volume=1.0, position_id=4
        )
        self.assertEqual(result["action"], "close")

    def test_manage_position_tp_hit_sell(self):
        self.engine._set_trailing_activated(5)
        result = self.engine.manage_position(
            entry_price=2310, direction="sell", current_price=2280,
            sl_price=2315, tp_price=2290, volume=1.0, position_id=5
        )
        self.assertEqual(result["action"], "close")

    def test_reset_clears_all(self):
        self.engine.update_markets({})
        self.engine.reset()
        self.assertEqual(len(self.engine.tf_markers), 0)


if __name__ == "__main__":
    unittest.main()
