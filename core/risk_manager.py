import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(
        self,
        max_daily_loss_pct: float = 3.0,
        max_consecutive_losses: int = 4,
        max_drawdown_pct: float = 15.0,
    ):
        self._max_daily_loss_pct = max_daily_loss_pct
        self._max_consecutive_losses = max_consecutive_losses
        self._max_drawdown_pct = max_drawdown_pct
        self._daily_loss_sum = 0.0
        self._peak_balance = 0.0
        self._consecutive_losses = 0
        self._current_date: Optional[str] = None
        self._blocked_today = False
        self._is_killed = False
        self._trades_today = 0

    def start_day(self, date_str: str, balance: float) -> None:
        if self._current_date != date_str:
            self._current_date = date_str
            self._daily_loss_sum = 0.0
            self._trades_today = 0
            self._blocked_today = False
            self._consecutive_losses = 0
            if balance > self._peak_balance:
                self._peak_balance = balance

    def record_trade(self, profit: float) -> None:
        self._trades_today += 1
        if profit < 0:
            self._daily_loss_sum += abs(profit)
            self._consecutive_losses += 1
            if self._consecutive_losses >= self._max_consecutive_losses:
                self._blocked_today = True
                logger.warning(
                    f"RiskManager: {self._consecutive_losses} consecutive losses, "
                    f"blocked for rest of day"
                )
        else:
            self._consecutive_losses = 0

    def _get_effective_max_daily_loss_pct(self, balance: float) -> float:
        if balance < 200:
            return 20.0
        elif balance < 500:
            return 10.0
        elif balance < 1000:
            return 5.0
        return self._max_daily_loss_pct

    def _get_effective_max_drawdown_pct(self, balance: float) -> float:
        if balance < 200:
            return 50.0
        elif balance < 500:
            return 30.0
        elif balance < 1000:
            return 20.0
        return self._max_drawdown_pct

    def check_entry_allowed(self, balance: float) -> Tuple[bool, Optional[str]]:
        if self._is_killed:
            return False, "Bot killed by max drawdown circuit breaker"

        if self._blocked_today:
            return False, "Blocked for day (consecutive losses or daily loss limit)"

        if balance > self._peak_balance:
            self._peak_balance = balance

        effective_daily_loss_pct = self._get_effective_max_daily_loss_pct(balance)
        daily_loss_pct = (
            (self._daily_loss_sum / balance * 100) if balance > 0 else 0
        )
        if daily_loss_pct >= effective_daily_loss_pct:
            self._blocked_today = True
            return (
                False,
                f"Daily loss limit reached "
                f"({daily_loss_pct:.1f}% >= {effective_daily_loss_pct:.0f}%)",
            )

        effective_drawdown_pct = self._get_effective_max_drawdown_pct(balance)
        drawdown_pct = (
            ((self._peak_balance - balance) / self._peak_balance * 100)
            if self._peak_balance > 0
            else 0
        )
        if drawdown_pct >= effective_drawdown_pct:
            self._is_killed = True
            return (
                False,
                f"Max drawdown reached "
                f"({drawdown_pct:.1f}% >= {effective_drawdown_pct:.0f}%)",
            )

        return True, None

    @property
    def trades_today(self) -> int:
        return self._trades_today

    @property
    def is_blocked(self) -> bool:
        return self._blocked_today or self._is_killed
