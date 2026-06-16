import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Tuple
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class InstitutionalZone:
    zone_type: str
    zone_high: float
    zone_low: float
    created_at: datetime
    fvg_high: Optional[float] = None
    fvg_low: Optional[float] = None
    tested: bool = False
    breached: bool = False
    strength: int = 0

    def contains(self, price: float) -> bool:
        return self.zone_low <= price <= self.zone_high

    def is_fresh(self) -> bool:
        return not self.tested and not self.breached


class InstitutionalZoneDetector:
    def __init__(self):
        self.zones: List[InstitutionalZone] = []
        self._bars: List[pd.Series] = []

    def reset(self) -> None:
        self.zones.clear()
        self._bars.clear()

    def _find_fvg(self, c0: pd.Series, c1: pd.Series, c2: pd.Series, direction: str) -> Optional[Tuple[float, float]]:
        if direction == "demand":
            if c1["low"] > c0["high"]:
                return (c0["high"], c1["low"])
            if c2["low"] > c1["high"]:
                return (c1["high"], c2["low"])
        else:
            if c1["high"] < c0["low"]:
                return (c1["high"], c0["low"])
            if c2["high"] < c1["low"]:
                return (c2["high"], c1["low"])
        return None

    def _check_displacement(self, c1: pd.Series, c2: pd.Series, c3: pd.Series) -> Tuple[bool, bool]:
        bull = (
            c1["close"] > c1["open"]
            and c2["close"] > c2["open"]
            and c3["close"] > c3["open"]
            and c1["close"] < c2["close"] < c3["close"]
            and abs(c1["close"] - c1["open"]) <= abs(c2["close"] - c2["open"]) <= abs(c3["close"] - c3["open"])
        )
        bear = (
            c1["close"] < c1["open"]
            and c2["close"] < c2["open"]
            and c3["close"] < c3["open"]
            and c1["close"] > c2["close"] > c3["close"]
            and abs(c1["close"] - c1["open"]) <= abs(c2["close"] - c2["open"]) <= abs(c3["close"] - c3["open"])
        )
        return (bull, bear)

    def update(self, bar: pd.Series) -> None:
        self._bars.append(bar)
        if len(self._bars) < 5:
            return

        c0 = self._bars[-4]
        c1 = self._bars[-3]
        c2 = self._bars[-2]
        c3 = self._bars[-1]

        bull, bear = self._check_displacement(c1, c2, c3)

        if bull:
            base_high = max(c0["close"], c0["open"])
            base_low = min(c0["close"], c0["open"])
            zone = InstitutionalZone(
                zone_type="demand",
                zone_high=base_high,
                zone_low=base_low,
                created_at=bar.name,
            )
            fvg = self._find_fvg(c0, c1, c2, "demand")
            if fvg is not None:
                zone.fvg_low, zone.fvg_high = fvg
                zone.strength += 1
            self.zones.append(zone)
            logger.debug(f"Demand zone: {base_low:.2f}-{base_high:.2f} at {bar.name}")

        elif bear:
            base_high = max(c0["close"], c0["open"])
            base_low = min(c0["close"], c0["open"])
            zone = InstitutionalZone(
                zone_type="supply",
                zone_high=base_high,
                zone_low=base_low,
                created_at=bar.name,
            )
            fvg = self._find_fvg(c0, c1, c2, "supply")
            if fvg is not None:
                zone.fvg_low, zone.fvg_high = fvg
                zone.strength += 1
            self.zones.append(zone)
            logger.debug(f"Supply zone: {base_low:.2f}-{base_high:.2f} at {bar.name}")

    def update_test_status(self, high: float, low: float) -> None:
        for zone in self.zones:
            if zone.breached:
                continue
            if low <= zone.zone_high and high >= zone.zone_low:
                zone.tested = True
            if zone.zone_type == "demand" and low < zone.zone_low:
                zone.breached = True
            elif zone.zone_type == "supply" and high > zone.zone_high:
                zone.breached = True

    def get_best_zone(self, direction: str, price: Optional[float] = None) -> Optional[InstitutionalZone]:
        fresh = [z for z in self.zones if z.is_fresh()]
        if direction == "demand":
            demand = [z for z in fresh if z.zone_type == "demand"]
            if not demand:
                return None
            best = max(demand, key=lambda z: z.strength)
            lowest = min(demand, key=lambda z: z.zone_low)
            if price is not None:
                below_price = [z for z in demand if z.zone_high < price]
                if below_price:
                    lowest_below = min(below_price, key=lambda z: z.zone_low)
                    return max([lowest_below, best], key=lambda z: z.strength)
            return best if best.strength >= lowest.strength else lowest
        else:
            supply = [z for z in fresh if z.zone_type == "supply"]
            if not supply:
                return None
            best = max(supply, key=lambda z: z.strength)
            highest = max(supply, key=lambda z: z.zone_high)
            if price is not None:
                above_price = [z for z in supply if z.zone_low > price]
                if above_price:
                    highest_above = max(above_price, key=lambda z: z.zone_high)
                    return max([highest_above, best], key=lambda z: z.strength)
            return best if best.strength >= highest.strength else highest

    def build_historical(self, df_15min: pd.DataFrame) -> None:
        old_zones = self.zones
        old_bars = self._bars
        self.zones = []
        self._bars = []
        try:
            for idx, row in df_15min.iterrows():
                self.update(row)
        except Exception:
            self.zones = old_zones
            self._bars = old_bars
            raise
