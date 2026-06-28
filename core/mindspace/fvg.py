import logging
from typing import Optional

from core.mindspace.models import Candle, FVG

logger = logging.getLogger(__name__)


class FVGDetector:
    def __init__(self):
        self.fvgs: list[FVG] = []

    def scan(self, candles: list[Candle], tf: str = "") -> list[FVG]:
        if len(candles) < 3:
            return []

        new_fvgs = []

        for i in range(2, len(candles)):
            c1 = candles[i - 2]
            c2 = candles[i - 1]
            c3 = candles[i]

            fvg = self._detect_fvg(c1, c2, c3, tf)
            if fvg and not self._is_duplicate(fvg):
                new_fvgs.append(fvg)
                self.fvgs.append(fvg)

        return new_fvgs

    def _detect_fvg(self, c1: Candle, c2: Candle, c3: Candle, tf: str) -> Optional[FVG]:
        if c1.high < c3.low:
            return FVG(
                gap_high=c3.low,
                gap_low=c1.high,
                tf=tf,
                direction="bullish",
            )

        if c1.low > c3.high:
            return FVG(
                gap_high=c1.low,
                gap_low=c3.high,
                tf=tf,
                direction="bearish",
            )

        return None

    def _is_duplicate(self, fvg: FVG) -> bool:
        for existing in self.fvgs:
            if (
                existing.direction == fvg.direction
                and abs(existing.gap_high - fvg.gap_high) < 0.05
                and abs(existing.gap_low - fvg.gap_low) < 0.05
            ):
                return True
        return False

    def get_tap_go(
        self, level_price: float, level_tf: str, direction: str, max_distance: float = 0.30
    ) -> Optional[FVG]:
        for fvg in self.fvgs:
            if not fvg.active:
                continue
            if fvg.tf != level_tf:
                continue
            if fvg.direction == "bullish" and direction != "buy":
                continue
            if fvg.direction == "bearish" and direction != "sell":
                continue
            if fvg.gap_low <= level_price <= fvg.gap_high:
                return fvg
            gap_mid = (fvg.gap_high + fvg.gap_low) / 2.0
            if abs(level_price - gap_mid) <= max_distance:
                return fvg

        return None

    def reset(self) -> None:
        self.fvgs.clear()
