import logging
from datetime import datetime
from typing import Optional

from core.mindspace.models import Candle, Swing

logger = logging.getLogger(__name__)


class StructureMarker:
    def __init__(self, tf: str = ""):
        self.tf = tf
        self.swings: list[Swing] = []
        self.last_confirmed_high: Optional[float] = None
        self.last_confirmed_low: Optional[float] = None
        self.trend: Optional[str] = None  # "bullish" or "bearish"

        self._pending_high: Optional[float] = None
        self._pending_low: Optional[float] = None
        self._retracement_candles: list[Candle] = []
        self._looking_for_retracement: bool = False
        self._high_confirmed_after_retrace: bool = False
        self._low_confirmed_after_break: bool = False
        self._last_swing_time: Optional[datetime] = None
        self._last_update: Optional[datetime] = None

    def update(self, candles: list[Candle]) -> list[Swing]:
        if not candles:
            return []

        new_swings = []
        for candle in candles:
            if self._last_swing_time is not None and candle.time <= self._last_swing_time:
                continue
            swings = self._process_candle(candle)
            new_swings.extend(swings)
            self._last_swing_time = candle.time

        self.swings.extend(new_swings)

        if candles:
            self._last_update = candles[-1].time

        return new_swings

    def _process_candle(self, candle: Candle) -> list[Swing]:
        new_swings = []

        if self.last_confirmed_low is None and self.last_confirmed_high is None:
            self.last_confirmed_low = candle.low
            self.last_confirmed_high = candle.high
            self.trend = "bullish" if candle.is_bullish else "bearish"
            new_swings.append(Swing(candle.low, candle.time, "low", True, self.tf))
            new_swings.append(Swing(candle.high, candle.time, "high", True, self.tf))
            return new_swings

        if self.trend == "bullish":
            swings = self._process_bullish(candle)
        else:
            swings = self._process_bearish(candle)

        new_swings.extend(swings)
        return new_swings

    def _process_bullish(self, candle: Candle) -> list[Swing]:
        new_swings = []

        if not self._looking_for_retracement:
            if candle.close < self.last_confirmed_high:
                if not self._low_confirmed_after_break:
                    self.last_confirmed_low = candle.low
                    self._low_confirmed_after_break = True
                    logger.debug(f"[{self.tf}] Bullish Low confirmed @ {candle.low:.2f}")
                self._retracement_candles = [candle]
                self._looking_for_retracement = True
                logger.debug(f"[{self.tf}] Bullish: looking for 2-candle retracement")
            return new_swings

        self._retracement_candles.append(candle)

        if len(self._retracement_candles) >= 2:
            c1 = self._retracement_candles[-2]
            c2 = self._retracement_candles[-1]

            if c2.close < c1.low:
                self.last_confirmed_high = max(c1.high, c2.high)
                self._high_confirmed_after_retrace = True
                self._retracement_candles = []
                self._looking_for_retracement = False

                swing = Swing(self.last_confirmed_high, c2.time, "high", True, self.tf)
                new_swings.append(swing)
                logger.debug(
                    f"[{self.tf}] Bullish High confirmed @ {self.last_confirmed_high:.2f} "
                    f"(2-candle retracement: c2.close {c2.close:.2f} < c1.low {c1.low:.2f})"
                )

                if self.last_confirmed_low is None:
                    self.last_confirmed_low = min(c1.low, c2.low)
                    self._low_confirmed_after_break = True
                    new_swings.append(Swing(self.last_confirmed_low, c2.time, "low", True, self.tf))
            else:
                if c2.close > self.last_confirmed_high:
                    self._retracement_candles = []
                    self._looking_for_retracement = False
                    self.last_confirmed_high = candle.high
                    logger.debug(f"[{self.tf}] Bullish: retracement failed, new high @ {candle.high:.2f}")

        return new_swings

    def _process_bearish(self, candle: Candle) -> list[Swing]:
        new_swings = []

        if not self._looking_for_retracement:
            if candle.close > self.last_confirmed_low:
                if not self._high_confirmed_after_retrace:
                    self.last_confirmed_high = candle.high
                    self._high_confirmed_after_retrace = True
                    logger.debug(f"[{self.tf}] Bearish High confirmed @ {candle.high:.2f}")
                self._retracement_candles = [candle]
                self._looking_for_retracement = True
                logger.debug(f"[{self.tf}] Bearish: looking for 2-candle retracement")
            return new_swings

        self._retracement_candles.append(candle)

        if len(self._retracement_candles) >= 2:
            c1 = self._retracement_candles[-2]
            c2 = self._retracement_candles[-1]

            if c2.close > c1.high:
                self.last_confirmed_low = min(c1.low, c2.low)
                self._retracement_candles = []
                self._looking_for_retracement = False

                swing = Swing(self.last_confirmed_low, c2.time, "low", True, self.tf)
                new_swings.append(swing)
                logger.debug(
                    f"[{self.tf}] Bearish Low confirmed @ {self.last_confirmed_low:.2f} "
                    f"(2-candle retracement: c2.close {c2.close:.2f} > c1.high {c1.high:.2f})"
                )

                if self.last_confirmed_high is None:
                    self.last_confirmed_high = max(c1.high, c2.high)
                    self._high_confirmed_after_retrace = True
                    new_swings.append(Swing(self.last_confirmed_high, c2.time, "high", True, self.tf))
            else:
                if c2.close < self.last_confirmed_low:
                    self._retracement_candles = []
                    self._looking_for_retracement = False
                    self.last_confirmed_low = candle.low
                    logger.debug(f"[{self.tf}] Bearish: retracement failed, new low @ {candle.low:.2f}")

        return new_swings

    def get_last_swing(self, swing_type: Optional[str] = None) -> Optional[Swing]:
        for s in reversed(self.swings):
            if swing_type is None or s.swing_type == swing_type:
                return s
        return None

    def is_impulse_direction(self, price: float) -> Optional[str]:
        if self.last_confirmed_high is None or self.last_confirmed_low is None:
            return None
        if price > self.last_confirmed_high:
            return "buy"
        if price < self.last_confirmed_low:
            return "sell"
        return None

    def reset(self) -> None:
        self.swings.clear()
        self.last_confirmed_high = None
        self.last_confirmed_low = None
        self.trend = None
        self._retracement_candles = []
        self._looking_for_retracement = False
        self._high_confirmed_after_retrace = False
        self._low_confirmed_after_break = False
        self._last_swing_time = None
