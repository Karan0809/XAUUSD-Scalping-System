import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


@dataclass
class ScalperSettings:
    symbol: str = "XAUUSD"
    mt5_server: str = field(default_factory=lambda: _env("MT5_SERVER", "ICMarkets-Demo"))
    mt5_login: int = field(default_factory=lambda: int(_env("MT5_LOGIN", "0")))
    mt5_password: str = field(default_factory=lambda: _env("MT5_PASSWORD", ""))
    mt5_path: str = field(default_factory=lambda: _env("MT5_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe"))
    mt5_portable: bool = field(default_factory=lambda: _env("MT5_PORTABLE", "false").lower() in ("1", "true", "yes"))

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
    max_slippage_points: float = 0.50

    trailing_stop_enabled: bool = True
    trail_multiplier: float = 0.3

    max_trades_per_day: int = 5
    strategy_label: str = "Mindspace"

    news_filter_enabled: bool = False
    news_blackout_minutes: int = 30

    circuit_breaker_max_daily_loss_pct: float = 10.0
    circuit_breaker_max_consecutive_losses: int = 4
    circuit_breaker_max_drawdown_pct: float = 15.0

    mongo_uri: str = field(default_factory=lambda: _env("MONGO_URI", "mongodb://localhost:27017"))
    mongo_db: str = "xauusd_scalper"
    mongo_trades_collection: str = "trades"
    mongo_signals_collection: str = "signals"
    mongo_metrics_collection: str = "metrics"

    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID", ""))

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
            s.circuit_breaker_max_daily_loss_pct = 20.0
            s.circuit_breaker_max_drawdown_pct = 50.0
        elif balance < 500:
            s.risk_percent = min(s.risk_percent, 1.5)
            s.max_daily_trades = min(s.max_daily_trades, 10)
            s.circuit_breaker_max_daily_loss_pct = 10.0
            s.circuit_breaker_max_drawdown_pct = 30.0
        elif balance < 1000:
            s.circuit_breaker_max_daily_loss_pct = 5.0
            s.circuit_breaker_max_drawdown_pct = 20.0
        return s

    def validate(self) -> bool:
        errors = []
        if self.risk_percent <= 0 or self.risk_percent > 5:
            errors.append("risk_percent must be between 0 and 5")
        if self.max_daily_trades < 1:
            errors.append("max_daily_trades must be >= 1")
        if self.max_trades_per_day < 1:
            errors.append("max_trades_per_day must be >= 1")
        if errors:
            raise ValueError(f"Settings validation failed: {', '.join(errors)}")
        return True


def get_settings(env_file: Optional[str] = None, _cache: dict = {}) -> ScalperSettings:
    resolved = env_file or ".env"
    if resolved not in _cache:
        load_dotenv(resolved, override=True)
        s = ScalperSettings()
        s.validate()
        _cache[resolved] = s
    return _cache[resolved]
