import os
from dataclasses import dataclass, field
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

    timeframe: int = 5
    swing_lookback: int = 4
    simple_lookback: int = 10
    bos_confirmation_bars: int = 15
    bos_reversal: bool = False

    risk_percent: float = 1.5
    max_daily_trades: int = 3
    max_daily_loss: float = 500.0
    rr_ratio: float = 2.0
    min_sweep_distance_pips: float = 1.0
    min_sl_pips: float = 5.0
    max_spread: float = 2.0
    max_slippage: int = 10

    mongo_uri: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    mongo_db: str = "xauusd_scalper"
    mongo_trades_collection: str = "trades"
    mongo_signals_collection: str = "signals"
    mongo_metrics_collection: str = "metrics"

    telegram_token: str = os.getenv("TELEGRAM_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    log_level: str = "INFO"
    log_dir: str = "logs"
    screenshot_dir: str = "screenshots"
    save_charts: bool = True

    backtest_start: str = "2025-09-01"
    backtest_end: str = "2026-05-30"
    backtest_initial_balance: float = 1000.0
    backtest_commission: float = 3.5
    backtest_slippage_pips: float = 0.5

    h1_trend_filter: bool = False
    h1_trend_period: int = 50

    m1_entry_enabled: bool = True
    m1_entry_lookback_bars: int = 15
    m1_round_number_range_pips: float = 10.0
    m1_entry_rr_ratio: float = 2.0

    fib_strategy_enabled: bool = False
    fib_min_leg_move: float = 2.0
    fib_lookback_minutes: int = 30
    fib_entry_window_minutes: int = 120
    fib_rr_ratio: float = 3.0

    ai_enabled: bool = False
    ai_filter_endpoint: str = os.getenv("AI_FILTER_ENDPOINT", "")
    ai_filter_api_key: str = os.getenv("AI_FILTER_API_KEY", "")
    ai_confidence_threshold: float = 0.7

    ict_strategy_enabled: bool = False
    ict_rr_ratio: float = 2.0
    ict_min_manipulation_pips: float = 5.0
    ict_macro_only: bool = True
    ict_min_sl_pips: float = 3.0

    def validate(self) -> bool:
        errors = []
        if self.risk_percent <= 0 or self.risk_percent > 5:
            errors.append("risk_percent must be between 0 and 5")
        if self.max_daily_trades < 1:
            errors.append("max_daily_trades must be >= 1")
        if self.rr_ratio <= 0:
            errors.append("rr_ratio must be positive")
        if self.min_sl_pips < 0:
            errors.append("min_sl_pips must be >= 0")
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
