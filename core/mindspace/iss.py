import logging
from datetime import datetime
from typing import Optional

from core.mindspace.models import Candle, ISSZone, Swing
from core.mindspace.structures import StructureMarker

logger = logging.getLogger(__name__)


class Wave:
    def __init__(self, direction: str, start_time: datetime):
        self.direction = direction
        self.start_time = start_time
        self.high: Optional[float] = None
        self.low: Optional[float] = None
        self.confirmed: bool = False

    def update(self, candle: Candle) -> None:
        if self.high is None or candle.high > self.high:
            self.high = candle.high
        if self.low is None or candle.low < self.low:
            self.low = candle.low


class ISSDetector:
    def __init__(self):
        self.current_iss: Optional[ISSZone] = None
        self._wave_count: int = 0
        self._waves: list[Wave] = []
        self._in_iss_mode: bool = False
        self._impulse_start: Optional[Wave] = None
        self._last_completed_time: Optional[datetime] = None
        self._retrace_candles: list[Candle] = []

    def update(self, structure: StructureMarker, candles: list[Candle]) -> Optional[ISSZone]:
        if len(candles) < 3:
            return self.current_iss

        trend = structure.trend
        if trend is None:
            return self.current_iss

        if not self._in_iss_mode:
            if structure._looking_for_retracement and trend == "bullish":
                self._start_iss(structure, candles[-1], "sell")
            elif structure._looking_for_retracement and trend == "bearish":
                self._start_iss(structure, candles[-1], "buy")
            return self.current_iss

        for candle in candles:
            if self._last_completed_time is not None and candle.time <= self._last_completed_time:
                continue
            self._process_candle_for_iss(candle, structure)

        return self.current_iss

    def _start_iss(self, structure: StructureMarker, candle: Candle, iss_direction: str) -> None:
        self._in_iss_mode = True
        self._waves = []
        self._wave_count = 0
        wave = Wave(iss_direction, candle.time)
        wave.update(candle)
        self._waves.append(wave)
        self._wave_count = 1
        logger.debug(f"ISS started: wave 1 ({iss_direction}) @ {candle.time}")

    def _process_candle_for_iss(self, candle: Candle, structure: StructureMarker) -> None:
        if self._wave_count == 0:
            return

        current_wave = self._waves[-1]
        current_wave.update(candle)

        if self._wave_count >= 5:
            return

        expected_dir = "sell" if structure.trend == "bullish" else "buy"
        is_counter = (
            (candle.is_bearish if expected_dir == "sell" else candle.is_bullish)
        )

        if self._wave_count >= 2:
            self._retrace_candles.append(candle)
            if len(self._retrace_candles) >= 2:
                c1 = self._retrace_candles[-2]
                c2 = self._retrace_candles[-1]

                if expected_dir == "sell":
                    if c2.close < c1.low:
                        self._start_new_wave(candle)
                        self._retrace_candles = []
                else:
                    if c2.close > c1.high:
                        self._start_new_wave(candle)
                        self._retrace_candles = []
        else:
            if current_wave.direction == "sell" and candle.is_bullish:
                self._start_new_wave(candle)
            elif current_wave.direction == "buy" and candle.is_bearish:
                self._start_new_wave(candle)

        self._check_complete(structure)

    def _start_new_wave(self, candle: Candle) -> None:
        next_dir = "sell" if self._waves[-1].direction == "buy" else "buy"
        wave = Wave(next_dir, candle.time)
        wave.update(candle)
        self._waves.append(wave)
        self._wave_count += 1
        logger.debug(f"ISS wave {self._wave_count} ({next_dir}) started @ {candle.time}")

    def _check_complete(self, structure: StructureMarker) -> None:
        if self._wave_count < 5:
            return

        w3 = self._waves[2]
        w4 = self._waves[3]
        w5 = self._waves[4]

        expected_dir = "sell" if structure.trend == "bullish" else "buy"

        if expected_dir == "sell":
            if w5.high is not None and w3.low is not None and w4.high is not None:
                zone_low = min(w3.low, w4.low)
                zone_high = max(w3.high, w4.high)
                sl_level = w5.high

                self.current_iss = ISSZone(
                    entry_high=zone_high,
                    entry_low=zone_low,
                    sl_level=sl_level,
                    tf=structure.tf,
                    direction="sell",
                    created_at=self._waves[-1].start_time,
                    is_recent=True,
                )
                logger.debug(
                    f"[{structure.tf}] ISS complete: 5-wave sell zone "
                    f"{zone_low:.2f}-{zone_high:.2f}, SL={sl_level:.2f}"
                )
        else:
            if w5.low is not None and w3.high is not None and w4.low is not None:
                zone_low = min(w3.low, w4.low)
                zone_high = max(w3.high, w4.high)
                sl_level = w5.low

                self.current_iss = ISSZone(
                    entry_high=zone_high,
                    entry_low=zone_low,
                    sl_level=sl_level,
                    tf=structure.tf,
                    direction="buy",
                    created_at=self._waves[-1].start_time,
                    is_recent=True,
                )
                logger.debug(
                    f"[{structure.tf}] ISS complete: 5-wave buy zone "
                    f"{zone_low:.2f}-{zone_high:.2f}, SL={sl_level:.2f}"
                )

        self._in_iss_mode = False
        self._last_completed_time = self._waves[-1].start_time

    def reset(self) -> None:
        self.current_iss = None
        self._waves = []
        self._wave_count = 0
        self._in_iss_mode = False
        self._retrace_candles = []
        self._last_completed_time = None
