import logging
from datetime import datetime
from typing import Optional

from core.mindspace.models import Candle, CHoCHSignal, Swing
from core.mindspace.structures import StructureMarker

logger = logging.getLogger(__name__)


class CHOCHDetector:
    def __init__(self):
        self.last_resistance: Optional[float] = None
        self.last_support: Optional[float] = None
        self.events: list[CHoCHSignal] = []
        self._double_choch_pending: Optional[CHoCHSignal] = None
        self._last_event: Optional[CHoCHSignal] = None

    def detect(
        self,
        structure: StructureMarker,
        candles: list[Candle],
        tf: str = "",
    ) -> Optional[CHoCHSignal]:
        if len(candles) < 2:
            return None

        self.last_resistance = structure.last_confirmed_high
        self.last_support = structure.last_confirmed_low

        last_two = candles[-2:]
        c1, c2 = last_two[0], last_two[1]

        signal = None

        if self.last_resistance is not None:
            if c1.close > self.last_resistance and c2.close > self.last_resistance:
                signal = CHoCHSignal(
                    direction="bullish",
                    break_level=self.last_resistance,
                    time=c2.time,
                    tf=tf,
                    candle=c2,
                )
                logger.debug(
                    f"[{tf}] Bullish CHOCH: 2 candles closed above resistance {self.last_resistance:.2f}"
                )

        if signal is None and self.last_support is not None:
            if c1.close < self.last_support and c2.close < self.last_support:
                signal = CHoCHSignal(
                    direction="bearish",
                    break_level=self.last_support,
                    time=c2.time,
                    tf=tf,
                    candle=c2,
                )
                logger.debug(
                    f"[{tf}] Bearish CHOCH: 2 candles closed below support {self.last_support:.2f}"
                )

        if signal is not None:
            self._check_double_choch(signal)
            self.events.append(signal)
            self._last_event = signal

        return signal

    def _check_double_choch(self, signal: CHoCHSignal) -> bool:
        if self._last_event is None:
            return False
        if self._last_event.direction != signal.direction:
            prev = self._last_event
            cur = signal
            if (
                prev.direction == "bearish"
                and cur.direction == "bullish"
                and (cur.time - prev.time).total_seconds() < 7200
            ):
                logger.debug(f"Double CHOCH detected: bearish\u2192bullish within 2h")
                self._double_choch_pending = cur
                return True
            if (
                prev.direction == "bullish"
                and cur.direction == "bearish"
                and (cur.time - prev.time).total_seconds() < 7200
            ):
                logger.debug(f"Double CHOCH detected: bullish\u2192bearish within 2h")
                self._double_choch_pending = cur
                return True
        return False

    @property
    def is_double_choch(self) -> bool:
        return self._double_choch_pending is not None

    def clear_double_choch(self) -> None:
        self._double_choch_pending = None

    def detect_engulfing(self, candles: list[Candle], tf: str = "") -> bool:
        if tf not in ("4h", "daily", "d1"):
            return False
        if len(candles) < 2:
            return False

        c1 = candles[-2]
        c2 = candles[-1]

        if c1.is_bullish and c2.is_bearish:
            if c2.close < c1.open and c2.open > c1.close:
                logger.debug(f"[{tf}] Bearish engulfing detected")
                return True
        elif c1.is_bearish and c2.is_bullish:
            if c2.close > c1.open and c2.open < c1.close:
                logger.debug(f"[{tf}] Bullish engulfing detected")
                return True

        return False

    def get_most_recent(self, direction: Optional[str] = None) -> Optional[CHoCHSignal]:
        for e in reversed(self.events):
            if direction is None or e.direction == direction:
                return e
        return None

    def reset(self) -> None:
        self.events.clear()
        self._double_choch_pending = None
        self._last_event = None
