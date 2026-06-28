import logging
from typing import Optional

from core.mindspace.models import Candle, Level, OrderBlock, Signal
from core.mindspace.structures import StructureMarker
from core.mindspace.choch import CHOCHDetector
from core.mindspace.levels import LevelDrawer
from core.mindspace.supply_demand import OrderBlockDetector
from core.mindspace.fvg import FVGDetector
from core.mindspace.iss import ISSDetector
from core.mindspace.tjl import TJLEngine
from core.mindspace.mtf import MTFAnalyzer

logger = logging.getLogger(__name__)


class MindspaceEngine:
    def __init__(self):
        self.tf_markers: dict[str, StructureMarker] = {}
        self.choch = CHOCHDetector()
        self.levels = LevelDrawer()
        self.ob = OrderBlockDetector()
        self.fvg = FVGDetector()
        self.iss = ISSDetector()
        self.tjl = TJLEngine()
        self.mtf = MTFAnalyzer()

        self._instrument: str = "XAUUSD"
        self._last_structure: dict[str, bool] = {}
        self._candle_buffer: dict[str, list[Candle]] = {}

    def update_markets(self, candles_by_tf: dict[str, list[Candle]], mtf_hierarchy: Optional[list[str]] = None) -> None:
        for tf, candles in candles_by_tf.items():
            if not candles:
                continue
            if tf not in self.tf_markers:
                self.tf_markers[tf] = StructureMarker(tf=tf)
            marker = self.tf_markers[tf]

            if tf not in self._candle_buffer:
                self._candle_buffer[tf] = []
            buf = self._candle_buffer[tf]

            new_candles = self._get_new_candles(marker, candles)
            for c in new_candles:
                buf.append(c)
                marker.update([c])

            trim = buf[-100:]
            if len(trim) < len(buf):
                buf[:] = trim

            detect_window = buf[-5:] if len(buf) >= 5 else buf
            if len(detect_window) >= 2:
                self.choch.detect(marker, detect_window, tf)
                self.iss.update(marker, detect_window[-3:])

            self.ob.scan(buf, tf)
            self.fvg.scan(buf, tf)

        if mtf_hierarchy and len(mtf_hierarchy) >= 2:
            self.mtf.analyze(
                high=self.tf_markers.get(mtf_hierarchy[0]),
                mid=self.tf_markers.get(mtf_hierarchy[1]),
                low=self.tf_markers.get(mtf_hierarchy[2]) if len(mtf_hierarchy) > 2 else None,
            )
        else:
            self.mtf.analyze(
                daily=self.tf_markers.get("daily"),
                h4=self.tf_markers.get("4h"),
                h1=self.tf_markers.get("1h"),
            )

    def _get_new_candles(self, marker: StructureMarker, candles: list[Candle]) -> list[Candle]:
        if not marker._last_update:
            return candles[-5:] if len(candles) > 5 else candles
        return [c for c in candles if c.time > marker._last_update]

    def get_signal(self) -> Optional[Signal]:
        mtf_state = self.mtf._last_state
        if mtf_state is None:
            return None

        for tf, marker in sorted(self.tf_markers.items()):
            if marker.trend is None:
                continue

            direction = "buy" if marker.trend == "bullish" else "sell"

            choch_event = self.choch.get_most_recent(direction)
            if not choch_event:
                continue

            levels_drawn = self.levels.draw_from_choch(choch_event)
            if not levels_drawn:
                continue

            level = self.levels.get_best_level(direction, choch_event.candle.close if choch_event.candle else 0)
            if level is None:
                level = self.tjl.get_best_level(direction, choch_event.candle.close if choch_event.candle else 0)
            if level is None:
                continue

            if self._is_inducement_trap(level, direction):
                logger.warning(f"Inducement trap detected at level {level.price:.2f}, skipping")
                continue

            ob_check = self.ob.get_aligned(direction)
            if ob_check:
                big_zones = [oz for oz in ob_check if oz.is_big and oz.source_tf == tf]
                if big_zones:
                    required_trigger = self.mtf.get_required_trigger(tf, is_big=True)
                    if not self._check_trigger_level(required_trigger, level, direction):
                        logger.info(f"Big zone at {level.price:.2f} requires {required_trigger}, not met")
                        continue

            trigger = self.mtf.get_required_trigger()
            if not self._check_trigger_level(trigger, level, direction):
                continue

            tap_go = self.fvg.get_tap_go(level.price, tf, direction)
            if not tap_go:
                continue

            return Signal(
                direction=direction,
                entry_price=level.price,
                sl_high=level.sl_zone_high,
                sl_low=level.sl_zone_low,
                tp_price=self._calc_tp(choch_event, marker),
                level_type=level.level_type,
                tf=tf,
                source="mindspace",
            )

        iss = self.iss.current_iss
        if iss and iss.active and iss.is_recent:
            return Signal(
                direction=iss.direction,
                entry_price=(iss.entry_high + iss.entry_low) / 2.0,
                sl_high=iss.sl_level,
                sl_low=iss.sl_level,
                tp_price=None,
                level_type="iss",
                tf=iss.tf,
                source="mindspace",
            )

        return None

    def _is_inducement_trap(self, level: Level, direction: str) -> bool:
        return False

    def _check_trigger_level(self, trigger: str, level: Level, direction: str) -> bool:
        return True

    def _calc_tp(self, choch_event, marker: StructureMarker) -> Optional[float]:
        if marker.last_confirmed_high is None or marker.last_confirmed_low is None:
            return None
        if choch_event.direction == "bullish":
            return marker.last_confirmed_high
        return marker.last_confirmed_low

    def manage_position(
        self,
        entry_price: float,
        direction: str,
        current_price: float,
        sl_price: float,
        tp_price: float,
        volume: float,
        position_id: int,
    ) -> dict:
        result = {"action": "hold", "close_pct": 0, "new_sl": sl_price}

        if direction == "buy":
            risk = entry_price - sl_price
            if risk <= 0:
                return result
            gain = current_price - entry_price
            rr = gain / risk

            if tp_price and current_price >= tp_price:
                result["action"] = "close"
                result["close_pct"] = 1.0
                logger.info(f"TP hit for pos {position_id}: {current_price:.2f} >= {tp_price:.2f}")
                return result

            if rr >= 1.0 and not self._check_trailing_activated(position_id):
                self._set_trailing_activated(position_id)
                result["action"] = "partial_close"
                result["close_pct"] = 0.5
                result["new_sl"] = entry_price
                logger.info(f"1:1 RR for pos {position_id}: closing 50%, SL to BE")
                return result

            if self._check_trailing_activated(position_id):
                trail_buffer = risk * 0.3
                new_sl = current_price - trail_buffer
                if new_sl > result["new_sl"]:
                    result["new_sl"] = new_sl
                    result["action"] = "trail"

        else:
            risk = sl_price - entry_price
            if risk <= 0:
                return result
            gain = entry_price - current_price
            rr = gain / risk

            if tp_price and current_price <= tp_price:
                result["action"] = "close"
                result["close_pct"] = 1.0
                logger.info(f"TP hit for pos {position_id}: {current_price:.2f} <= {tp_price:.2f}")
                return result

            if rr >= 1.0 and not self._check_trailing_activated(position_id):
                self._set_trailing_activated(position_id)
                result["action"] = "partial_close"
                result["close_pct"] = 0.5
                result["new_sl"] = entry_price
                logger.info(f"1:1 RR for pos {position_id}: closing 50%, SL to BE")
                return result

            if self._check_trailing_activated(position_id):
                trail_buffer = risk * 0.3
                new_sl = current_price + trail_buffer
                if new_sl < result["new_sl"]:
                    result["new_sl"] = new_sl
                    result["action"] = "trail"

        return result

    def _check_trailing_activated(self, position_id: int) -> bool:
        return hasattr(self, f"_trailing_{position_id}")

    def _set_trailing_activated(self, position_id: int) -> None:
        setattr(self, f"_trailing_{position_id}", True)

    def reset(self) -> None:
        self.tf_markers.clear()
        self.choch.reset()
        self.levels.reset()
        self.ob.reset()
        self.fvg.reset()
        self.iss.reset()
        self.tjl.reset()
        self.mtf.reset()
        self._last_structure.clear()
