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
        self._current_session: Optional[str] = None
        self._blocked_session: Optional[str] = None
        self._is_killed = False
        self._trades_today = 0

    def start_session(self, session_name: str) -> None:
        if self._current_session != session_name:
            self._current_session = session_name
            self._blocked_session = None
            self._consecutive_losses = 0
            self._daily_loss_sum = 0.0
            logger.info(
                f"RiskManager: session {session_name} — "
                f"reset consecutive losses/block/daily loss"
            )

    def start_day(self, date_str: str, balance: float) -> None:
        if self._current_date != date_str:
            self._current_date = date_str
            self._daily_loss_sum = 0.0
            self._trades_today = 0
            self._blocked_session = None
            self._consecutive_losses = 0
            if balance > self._peak_balance:
                self._peak_balance = balance

    def record_trade(self, profit: float) -> None:
        self._trades_today += 1
        if profit < 0:
            self._daily_loss_sum += abs(profit)
            self._consecutive_losses += 1
            if self._consecutive_losses >= self._max_consecutive_losses:
                self._blocked_session = self._current_session or "day"
                session_label = self._blocked_session
                logger.warning(
                    f"RiskManager: {self._consecutive_losses} consecutive losses, "
                    f"blocked for {session_label}"
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
            return False, "Drawdown limit exceeded — killed"

        if self._blocked_session is not None:
            if self._current_session is None or self._current_session == self._blocked_session:
                session_label = self._blocked_session or "day"
                return False, f"Blocked for {session_label} ({self._consecutive_losses} consecutive losses)"

        if self._consecutive_losses >= self._max_consecutive_losses:
            self._blocked_session = self._current_session or "day"
            session_label = self._blocked_session
            logger.warning(
                f"RiskManager: blocked — {self._consecutive_losses} consecutive losses for {session_label}"
            )
            return False, f"Blocked: {self._consecutive_losses} consecutive losses"

        if balance > self._peak_balance:
            self._peak_balance = balance

        daily_loss_pct = (self._daily_loss_sum / balance * 100) if balance > 0 else 0
        max_daily = self._get_effective_max_daily_loss_pct(balance)
        if daily_loss_pct >= max_daily:
            self._blocked_session = self._current_session or "day"
            session_label = self._blocked_session
            reason = f"Daily loss {daily_loss_pct:.1f}% exceeds {max_daily}% loss limit — blocked for {session_label}"
            logger.warning(f"RiskManager: blocked — {reason}")
            return False, reason

        peak = max(self._peak_balance, balance)
        if peak > 0:
            drawdown_pct = (peak - balance) / peak * 100
            max_dd = self._get_effective_max_drawdown_pct(balance)
            if drawdown_pct >= max_dd:
                self._is_killed = True
                logger.warning(
                    f"RiskManager: drawdown {drawdown_pct:.1f}% exceeds {max_dd}% — killed"
                )
                return False, f"Drawdown {drawdown_pct:.1f}% exceeds {max_dd}% limit"

        return True, None

    @property
    def trades_today(self) -> int:
        return self._trades_today

    @property
    def is_blocked(self) -> bool:
        if self._is_killed:
            return True
        if self._blocked_session is not None:
            if self._current_session is None or self._current_session == self._blocked_session:
                return True
        return False
