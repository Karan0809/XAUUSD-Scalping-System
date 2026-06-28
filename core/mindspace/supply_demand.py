import logging
from typing import Optional

from core.mindspace.models import Candle, OrderBlock

logger = logging.getLogger(__name__)


class OrderBlockDetector:
    def __init__(self):
        self.order_blocks: list[OrderBlock] = []

    def scan(self, candles: list[Candle], tf: str = "") -> list[OrderBlock]:
        if len(candles) < 3:
            return []

        new_blocks = []

        for i in range(2, len(candles)):
            c_prev = candles[i - 1]
            c_curr = candles[i]

            if i >= 2:
                c_before = candles[i - 2]
            else:
                continue

            ob = self._detect_supply(c_before, c_prev, c_curr, tf)
            if ob and not self._is_duplicate(ob):
                new_blocks.append(ob)
                self.order_blocks.append(ob)
                continue

            ob = self._detect_demand(c_before, c_prev, c_curr, tf)
            if ob and not self._is_duplicate(ob):
                new_blocks.append(ob)
                self.order_blocks.append(ob)

        return new_blocks

    def _detect_supply(
        self, c_before: Candle, c_prev: Candle, c_curr: Candle, tf: str
    ) -> Optional[OrderBlock]:
        if not (c_prev.is_bullish and c_curr.is_bearish):
            return None

        bearish_body = c_curr.body
        avg_body = (c_before.body + c_prev.body + c_curr.body) / 3.0

        if bearish_body < avg_body * 1.2:
            return None

        ob = OrderBlock(
            ob_type="supply",
            price_high=c_prev.high,
            price_low=c_prev.open,
            source_tf=tf,
        )

        if bearish_body > avg_body * 2.5:
            ob.is_big = True

        return ob

    def _detect_demand(
        self, c_before: Candle, c_prev: Candle, c_curr: Candle, tf: str
    ) -> Optional[OrderBlock]:
        if not (c_prev.is_bearish and c_curr.is_bullish):
            return None

        bullish_body = c_curr.body
        avg_body = (c_before.body + c_prev.body + c_curr.body) / 3.0

        if bullish_body < avg_body * 1.2:
            return None

        ob = OrderBlock(
            ob_type="demand",
            price_high=c_prev.open,
            price_low=c_prev.low,
            source_tf=tf,
        )

        if bullish_body > avg_body * 2.5:
            ob.is_big = True

        return ob

    def _is_duplicate(self, ob: OrderBlock) -> bool:
        for existing in self.order_blocks:
            if (
                existing.ob_type == ob.ob_type
                and abs(existing.price_high - ob.price_high) < 0.05
                and abs(existing.price_low - ob.price_low) < 0.05
            ):
                return True
        return False

    def get_aligned(self, direction: str) -> list[OrderBlock]:
        ob_type = "demand" if direction == "buy" else "supply"
        return [ob for ob in self.order_blocks if ob.active and ob.ob_type == ob_type]

    def get_big_zones(self, tf: str = "") -> list[OrderBlock]:
        return [ob for ob in self.order_blocks if ob.active and ob.is_big and (not tf or ob.source_tf == tf)]

    def reset(self) -> None:
        self.order_blocks.clear()
