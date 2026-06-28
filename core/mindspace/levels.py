import logging
from datetime import datetime
from typing import Optional

from core.mindspace.models import Candle, CHoCHSignal, Level, Swing

logger = logging.getLogger(__name__)


class LevelDrawer:
    def __init__(self):
        self.levels: list[Level] = []

    def draw_from_choch(self, choch: CHoCHSignal) -> list[Level]:
        new_levels = []

        sbr_rbs = self._draw_sbr_rbs(choch)
        if sbr_rbs:
            new_levels.append(sbr_rbs)
            self.levels.append(sbr_rbs)

        qml = self._draw_qml(choch)
        if qml:
            new_levels.append(qml)
            self.levels.append(qml)

        dt_db = self._draw_dt_db(choch)
        if dt_db:
            new_levels.append(dt_db)
            self.levels.append(dt_db)

        if new_levels:
            logger.info(
                f"[{choch.tf}] Drawn {len(new_levels)} levels from {choch.direction} CHOCH: "
                + ", ".join(l.level_type for l in new_levels)
            )

        return new_levels

    def _calc_sl_zone(self, candle: Candle) -> tuple[float, float]:
        body = candle.body
        buffer = body * 0.01
        sl_high = candle.high + buffer
        sl_low = candle.low - buffer
        return sl_high, sl_low

    def _draw_sbr_rbs(self, choch: CHoCHSignal) -> Optional[Level]:
        if choch.candle is None:
            return None

        candle = choch.candle
        sl_high, sl_low = self._calc_sl_zone(candle)

        if choch.direction == "bullish":
            level_type = "rbs"
            price = choch.break_level
        else:
            level_type = "sbr"
            price = choch.break_level

        return Level(
            level_type=level_type,
            price=price,
            sl_zone_high=sl_high,
            sl_zone_low=sl_low,
            source_tf=choch.tf,
            direction="buy" if choch.direction == "bullish" else "sell",
            created_at=choch.time,
        )

    def _draw_qml(self, choch: CHoCHSignal) -> Optional[Level]:
        if choch.candle is None:
            return None

        candle = choch.candle
        body = candle.body
        sl_high, sl_low = self._calc_sl_zone(candle)

        if choch.direction == "bullish":
            price = candle.low + body * 0.25
            direction = "buy"
        else:
            price = candle.high - body * 0.25
            direction = "sell"

        return Level(
            level_type="qml",
            price=price,
            sl_zone_high=sl_high,
            sl_zone_low=sl_low,
            source_tf=choch.tf,
            direction=direction,
            created_at=choch.time,
        )

    def _draw_dt_db(self, choch: CHoCHSignal) -> Optional[Level]:
        if choch.candle is None:
            return None

        candle = choch.candle
        sl_high, sl_low = self._calc_sl_zone(candle)

        if choch.direction == "bullish":
            level_type = "db"
            price = candle.low
            direction = "buy"
        else:
            level_type = "dt"
            price = candle.high
            direction = "sell"

        return Level(
            level_type=level_type,
            price=price,
            sl_zone_high=sl_high,
            sl_zone_low=sl_low,
            source_tf=choch.tf,
            direction=direction,
            created_at=choch.time,
        )

    def get_recent(self, *types: str, max_age_bars: int = 20) -> list[Level]:
        return [
            l for l in self.levels
            if l.active and (not types or l.level_type in types)
        ]

    def get_best_level(self, direction: str, price: float, max_distance: float = 5.0) -> Optional[Level]:
        candidates = [
            l for l in self.levels
            if l.active and l.direction == direction
            and abs(price - l.price) <= max_distance
        ]

        if not candidates:
            return None

        priority = {"qml": 0, "rbs": 1, "sbr": 1, "db": 2, "dt": 2}

        def sort_key(l: Level):
            return (priority.get(l.level_type, 99), abs(price - l.price))

        candidates.sort(key=sort_key)
        return candidates[0]

    def add_level(self, level: Level) -> None:
        self.levels.append(level)

    def expire_old(self, current_candle_count: int) -> None:
        for l in self.levels:
            if l.created_at is not None and False:
                pass
        self.levels = [l for l in self.levels if l.active]

    def reset(self) -> None:
        self.levels.clear()
