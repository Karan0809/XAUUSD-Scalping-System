import logging
from typing import Optional

from core.mindspace.models import Level, Swing
from core.mindspace.structures import StructureMarker

logger = logging.getLogger(__name__)


class TJLEngine:
    def __init__(self):
        self.tjl1: Optional[Level] = None
        self.tjl2: Optional[Level] = None

    def update(self, structure: StructureMarker, tf: str = "") -> tuple[Optional[Level], Optional[Level]]:
        if len(structure.swings) < 2:
            return None, None

        last_two = structure.swings[-2:]

        if last_two[0].swing_type == last_two[1].swing_type:
            return self.tjl1, self.tjl2

        if last_two[0].is_low and last_two[1].is_high:
            direction = "buy"
        elif last_two[0].is_high and last_two[1].is_low:
            direction = "sell"
        else:
            return self.tjl1, self.tjl2

        swing_low = min(s.price for s in last_two)
        swing_high = max(s.price for s in last_two)
        range_dist = swing_high - swing_low

        if range_dist <= 0:
            return self.tjl1, self.tjl2

        if direction == "buy":
            tjl1_price = swing_low + range_dist * 0.382
            tjl2_price = swing_low + range_dist * 0.618
        else:
            tjl1_price = swing_high - range_dist * 0.382
            tjl2_price = swing_high - range_dist * 0.618

        buffer = range_dist * 0.01

        self.tjl1 = Level(
            level_type="tjl1",
            price=tjl1_price,
            sl_zone_high=tjl1_price + buffer,
            sl_zone_low=tjl1_price - buffer,
            source_tf=tf,
            direction=direction,
        )

        self.tjl2 = Level(
            level_type="tjl2",
            price=tjl2_price,
            sl_zone_high=tjl2_price + buffer,
            sl_zone_low=tjl2_price - buffer,
            source_tf=tf,
            direction=direction,
        )

        logger.debug(
            f"[{tf}] TJL drawn: TJL1={tjl1_price:.2f} (QML/A++), "
            f"TJL2={tjl2_price:.2f}, direction={direction}"
        )

        return self.tjl1, self.tjl2

    def get_qml(self) -> Optional[Level]:
        return self.tjl1

    def get_best_level(self, direction: str, price: float, max_distance: float = 5.0) -> Optional[Level]:
        candidates = []

        if self.tjl1 and self.tjl1.active and self.tjl1.direction == direction:
            d1 = abs(price - self.tjl1.price)
            if d1 <= max_distance:
                candidates.append((0, d1, self.tjl1))

        if self.tjl2 and self.tjl2.active and self.tjl2.direction == direction:
            d2 = abs(price - self.tjl2.price)
            if d2 <= max_distance:
                candidates.append((1, d2, self.tjl2))

        if not candidates:
            return None

        candidates.sort(key=lambda x: (x[0], x[1]))
        return candidates[0][2]

    def reset(self) -> None:
        self.tjl1 = None
        self.tjl2 = None
