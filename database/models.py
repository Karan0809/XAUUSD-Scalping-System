from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field


class TradeRecord(BaseModel):
    trade_id: str = Field(..., description="Unique trade identifier")
    symbol: str = "XAUUSD"
    signal_type: str
    entry_price: float
    exit_price: Optional[float] = None
    stop_loss: float
    take_profit: Optional[float] = None
    lot_size: float
    profit: Optional[float] = None
    commission: Optional[float] = None
    open_time: datetime
    close_time: Optional[datetime] = None
    exit_reason: Optional[str] = None
    session_date: Optional[str] = None
    strategy: str = "orb_scalp"
    setup_notes: Optional[str] = None
    outcome: Optional[str] = None


class SignalRecord(BaseModel):
    signal_id: str
    symbol: str = "XAUUSD"
    signal_type: str
    entry_price: float
    stop_loss: float
    take_profit: Optional[float] = None
    timestamp: datetime
    session_date: Optional[str] = None
    setup_notes: Optional[str] = None
    strategy: str = "orb_scalp"
    executed: bool = False
    trade_id: Optional[str] = None


class SessionMetrics(BaseModel):
    date: str
    symbol: str = "XAUUSD"
    total_signals: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    max_drawdown: float = 0.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
