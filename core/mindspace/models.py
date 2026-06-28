from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0


@dataclass
class Swing:
    price: float
    time: datetime
    swing_type: str  # "high" or "low"
    confirmed: bool = False
    tf: str = ""

    @property
    def is_high(self) -> bool:
        return self.swing_type == "high"

    @property
    def is_low(self) -> bool:
        return self.swing_type == "low"


@dataclass
class Level:
    level_type: str  # "tjl1" / "tjl2" / "qml" / "sbr" / "rbs" / "dt" / "db" / "iss" / "ob"
    price: float
    sl_zone_high: float
    sl_zone_low: float
    source_tf: str = ""
    direction: str = ""  # "buy" / "sell"
    created_at: Optional[datetime] = None
    active: bool = True


@dataclass
class CHoCHSignal:
    direction: str  # "bullish" or "bearish"
    break_level: float
    time: datetime
    tf: str
    candle: Optional[Candle] = None


@dataclass
class OrderBlock:
    ob_type: str  # "supply" or "demand"
    price_high: float
    price_low: float
    source_tf: str = ""
    is_big: bool = False
    active: bool = True


@dataclass
class FVG:
    gap_high: float
    gap_low: float
    tf: str = ""
    direction: str = ""  # "bullish" (gap up) or "bearish" (gap down)
    active: bool = True


@dataclass
class ISSZone:
    entry_high: float
    entry_low: float
    sl_level: float
    tf: str = ""
    direction: str = ""  # "buy" or "sell"
    created_at: Optional[datetime] = None
    is_recent: bool = True
    active: bool = True


@dataclass
class MTFState:
    condition: int  # 1, 2, or 3
    direction: str  # "buy" or "sell"
    entry_trigger: str  # "candle" / "1m_choch" / "5m_choch" / "1h_choch"
    follow_tf: str  # "1h" / "4h" / "daily"


@dataclass
class Signal:
    direction: str
    entry_price: float
    sl_high: float
    sl_low: float
    tp_price: Optional[float]
    level_type: str
    tf: str
    source: str = "mindspace"
