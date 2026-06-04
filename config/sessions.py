from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Optional, Tuple


@dataclass
class SessionTimes:
    asia_start: time = time(0, 0)
    asia_end: time = time(9, 0)
    pre_london_start: time = time(6, 0)
    pre_london_end: time = time(9, 0)
    london_open: time = time(9, 0)
    london_trade_end: time = time(12, 0)
    ny_open: time = time(13, 0)
    ny_trade_end: time = time(16, 0)
    london_close: time = time(17, 0)

    def is_asia(self, dt: datetime) -> bool:
        t = dt.time()
        return self.asia_start <= t < self.asia_end

    def is_pre_london(self, dt: datetime) -> bool:
        t = dt.time()
        return self.pre_london_start <= t < self.pre_london_end

    def is_london_trade_window(self, dt: datetime) -> bool:
        t = dt.time()
        return self.london_open <= t < self.london_trade_end

    def is_ny_trade_window(self, dt: datetime) -> bool:
        t = dt.time()
        return self.ny_open <= t < self.ny_trade_end

    def is_trade_window(self, dt: datetime) -> bool:
        return self.is_london_trade_window(dt) or self.is_ny_trade_window(dt)

    def is_trading_hours(self, dt: datetime) -> bool:
        t = dt.time()
        return self.asia_start <= t < self.london_close

    def get_sessions(self, dt: datetime) -> list[str]:
        sessions = []
        if self.is_asia(dt):
            sessions.append("asia")
        if self.is_pre_london(dt):
            sessions.append("pre_london")
        if self.is_london_trade_window(dt):
            sessions.append("london_trade")
        if self.is_ny_trade_window(dt):
            sessions.append("ny_trade")
        return sessions

    def get_active_session(self, dt: datetime) -> Optional[str]:
        if self.is_ny_trade_window(dt):
            return "ny"
        if self.is_london_trade_window(dt):
            return "london"
        if self.is_asia(dt):
            return "asia"
        return None


class SessionValidator:
    MAX_ASIA_RANGE_PIPS: float = 10000.0
    MAX_PRE_LONDON_RANGE_PIPS: float = 6000.0

    @staticmethod
    def is_overextended(
        asia_range_pips: float,
        pre_london_range_pips: float,
    ) -> Tuple[bool, Optional[str]]:
        if asia_range_pips > SessionValidator.MAX_ASIA_RANGE_PIPS:
            return True, (
                f"asia_range={asia_range_pips:.1f} > "
                f"{SessionValidator.MAX_ASIA_RANGE_PIPS}"
            )
        if pre_london_range_pips > SessionValidator.MAX_PRE_LONDON_RANGE_PIPS:
            return True, (
                f"pre_london_range={pre_london_range_pips:.1f} > "
                f"{SessionValidator.MAX_PRE_LONDON_RANGE_PIPS}"
            )
        return False, None

    @staticmethod
    def compute_range_pips(high: float, low: float, digits: int = 2) -> float:
        pip_size = 10.0 ** (-digits)
        return round((high - low) / pip_size, 1)

    @staticmethod
    def is_sunday(dt: datetime) -> bool:
        return dt.weekday() == 6

    @staticmethod
    def is_friday_close(dt: datetime) -> bool:
        return dt.weekday() == 4 and dt.hour >= 17

    @staticmethod
    def next_trading_day(dt: datetime) -> datetime:
        current = dt
        for _ in range(7):
            current = current + timedelta(days=1)
            if current.weekday() < 5:
                return current.replace(hour=0, minute=0, second=0, microsecond=0)
        return current

    @staticmethod
    def is_valid_session_day(dt: datetime) -> bool:
        if SessionValidator.is_sunday(dt):
            return False
        if SessionValidator.is_friday_close(dt):
            return False
        return True
