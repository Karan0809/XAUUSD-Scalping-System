import logging
from typing import Optional

from core.mindspace.models import MTFState
from core.mindspace.structures import StructureMarker

logger = logging.getLogger(__name__)


class MTFAnalyzer:
    def __init__(self):
        self._last_state: Optional[MTFState] = None

    def analyze(
        self,
        daily: Optional[StructureMarker] = None,
        h4: Optional[StructureMarker] = None,
        h1: Optional[StructureMarker] = None,
        high: Optional[StructureMarker] = None,
        mid: Optional[StructureMarker] = None,
        low: Optional[StructureMarker] = None,
    ) -> Optional[MTFState]:
        if high is not None and mid is not None:
            a = high.trend
            b = mid.trend
            c = low.trend if low else mid.trend
            if a is None or b is None or c is None:
                return self._last_state
            labels = (a, b, c)
            high_label, mid_label, low_label = (getattr(m, "tf", "?") for m in (high, mid, low))
        elif daily is not None and h4 is not None and h1 is not None:
            a = daily.trend
            b = h4.trend
            c = h1.trend
            if a is None or b is None or c is None:
                return self._last_state
            labels = (a, b, c)
            high_label, mid_label, low_label = "daily", "4h", "1h"
        else:
            return self._last_state

        if a == b == c:
            cond = 1
            follow_tf = mid_label
            entry_trigger = "1m_choch" if low_label in ("1m",) else "candle"
            direction = a
        elif a == b and c != a:
            cond = 2
            follow_tf = mid_label
            entry_trigger = "1m_choch"
            direction = a
        elif a != b:
            cond = 3
            follow_tf = high_label
            entry_trigger = f"{mid_label}_choch"
            direction = a
        else:
            cond = 2
            follow_tf = mid_label
            entry_trigger = "1m_choch"
            direction = a

        logger.debug(f"MTF Cond {cond}: {labels} -> {direction} ({entry_trigger})")

        state = MTFState(
            condition=cond,
            direction=("buy" if direction == "bullish" else "sell"),
            entry_trigger=entry_trigger,
            follow_tf=follow_tf,
        )

        self._last_state = state
        return state

    def get_required_trigger(self, ob_tf: str = "", ob_is_big: bool = False) -> str:
        if ob_is_big:
            zone_trigger_map = {
                "daily": "1h_choch",
                "d1": "1h_choch",
                "4h": "5m_choch",
                "1h": "1m_choch",
                "15m": "1m_candle",
            }
            for key, trigger in zone_trigger_map.items():
                if key in ob_tf:
                    return trigger

        state = self._last_state
        if state is None:
            return "candle"

        return state.entry_trigger

    def is_trigger_met(self, trigger: str, latest_candles: dict) -> bool:
        if trigger == "candle":
            return self._check_candle_confirmation(latest_candles.get("1m", []))

        if trigger == "1m_choch":
            return self._check_1m_choch(latest_candles.get("1m", []))

        known_tfs = {"5m": "5m", "15m": "15m", "1h": "1h"}
        for tf_key, tf_label in known_tfs.items():
            if trigger == f"{tf_key}_choch" or tf_key in trigger:
                return self._check_tf_choch(latest_candles.get(tf_label, []))

        return False

    def _check_candle_confirmation(self, candles: list) -> bool:
        if len(candles) < 2:
            return False
        last = candles[-1]
        prev = candles[-2]
        if last.close > prev.close and last.close > last.open:
            return True
        if last.close < prev.close and last.close < last.open:
            return True
        return False

    def _check_1m_choch(self, candles: list) -> bool:
        if len(candles) < 2:
            return False
        return self._check_tf_choch(candles)

    def _check_tf_choch(self, candles: list) -> bool:
        if len(candles) < 2:
            return False
        c1 = candles[-2]
        c2 = candles[-1]

        support = min(c1.low, c2.low)
        resistance = max(c1.high, c2.high)

        if c1.close > c1.open and c2.close > c2.open:
            return c1.close > resistance and c2.close > resistance

        if c1.close < c1.open and c2.close < c2.open:
            return c1.close < support and c2.close < support

        return False

    def reset(self) -> None:
        self._last_state = None
