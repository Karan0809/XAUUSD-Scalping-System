import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass
class ScalperSettings:
    symbol: str = "XAUUSD"
    mt5_server: str = os.getenv("MT5_SERVER", "ICMarkets-Demo")
    mt5_login: int = int(os.getenv("MT5_LOGIN", "0"))
    mt5_password: str = os.getenv("MT5_PASSWORD", "")
    mt5_path: str = os.getenv("MT5_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")

    asia_start_hour: int = 0
    asia_end_hour: int = 9
    pre_london_start_hour: int = 6
    pre_london_end_hour: int = 9
    london_start_hour: int = 9
    london_end_hour: int = 12
    ny_start_hour: int = 13
    ny_end_hour: int = 16
    london_close_hour: int = 17

    risk_percent: float = 2.0
    max_daily_trades: int = 15
    max_slippage: int = 10

    max_spread: float = 60.0

    trailing_stop_enabled: bool = True
    trail_multiplier: float = 0.3

    news_filter_enabled: bool = False
    news_blackout_minutes: int = 30

    circuit_breaker_max_daily_loss_pct: float = 3.0
    circuit_breaker_max_consecutive_losses: int = 4
    circuit_breaker_max_drawdown_pct: float = 15.0

    mongo_uri: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    mongo_db: str = "xauusd_scalper"
    mongo_trades_collection: str = "trades"
    mongo_signals_collection: str = "signals"
    mongo_metrics_collection: str = "metrics"

    telegram_token: str = os.getenv("TELEGRAM_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    log_level: str = "INFO"
    log_dir: str = "logs"

    backtest_start: str = "2025-09-01"
    backtest_end: str = "2026-05-30"
    backtest_initial_balance: float = 1000.0
    backtest_commission: float = 3.5

    min_balance: float = 50.0

    def adjust_for_balance(self, balance: float) -> "ScalperSettings":
        import copy
        s = copy.copy(self)
        if balance < s.min_balance:
            raise ValueError(f"Balance ${balance:.2f} below minimum ${s.min_balance:.2f}")
        if balance < 200:
            s.risk_percent = min(s.risk_percent, 1.0)
            s.max_daily_trades = min(s.max_daily_trades, 5)
        elif balance < 500:
            s.risk_percent = min(s.risk_percent, 1.5)
            s.max_daily_trades = min(s.max_daily_trades, 10)
        return s

    def validate(self) -> bool:
        errors = []
        if self.risk_percent <= 0 or self.risk_percent > 5:
            errors.append("risk_percent must be between 0 and 5")
        if self.max_daily_trades < 1:
            errors.append("max_daily_trades must be >= 1")
        if errors:
            raise ValueError(f"Settings validation failed: {', '.join(errors)}")
        return True


_settings: Optional[ScalperSettings] = None


def get_settings() -> ScalperSettings:
    global _settings
    if _settings is None:
        _settings = ScalperSettings()
        _settings.validate()
    return _settings
