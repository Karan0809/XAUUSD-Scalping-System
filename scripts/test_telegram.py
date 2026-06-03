#!/usr/bin/env python3
import sys
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from log_utils.logger_setup import setup_logging
from telegram.alerts import TelegramNotifier

setup_logging()
bot = TelegramNotifier()

# Dummy signal
bot.alert_signal({
    "direction": "buy",
    "entry": 3450.25,
    "sl": 3444.10,
    "tp": 3462.40,
    "setup": "breakout_pullback",
    "session": "London",
})

# Dummy trade open
now = datetime.now(timezone.utc)
bot.alert_trade_open({
    "type": "buy",
    "entry": 3450.25,
    "sl": 3444.10,
    "tp": 3462.40,
    "lot_size": 0.12,
    "tp": 3462.40,
    "tp1_lots": 0.04,
    "tp2_lots": 0.05,
    "tp3_lots": 0.03,
    "balance": 3000.00,
})

# Dummy trade close (full TP1+TP2+TP3)
bot.alert_trade_close({
    "type": "buy",
    "entry": 3450.25,
    "pnl": 145.80,
    "exit_reason": "tp3",
    "tp1_hit": True,
    "tp2_hit": True,
    "tp3_hit": True,
    "open_time": now,
    "close_time": now,
    "original_lot_size": 0.12,
    "original_sl": 3444.10,
    "tp": 3462.40,
    "balance": 3145.80,
})

# Dummy trade close (SL only)
bot.alert_trade_close({
    "type": "sell",
    "entry": 3460.50,
    "pnl": -52.30,
    "exit_reason": "sl",
    "tp1_hit": False,
    "tp2_hit": False,
    "tp3_hit": False,
    "open_time": now,
    "close_time": now,
    "original_lot_size": 0.08,
    "original_sl": 3463.50,
    "tp": 3448.50,
    "balance": 2947.70,
})

# Dummy daily summary
bot.alert_daily_summary({
    "date": "2026-06-02",
    "total_trades": 5,
    "wins": 4,
    "losses": 1,
    "win_rate": 80.0,
    "total_pnl": 387.50,
    "profit_factor": 5.2,
    "max_drawdown": 1.5,
    "balance": 15874.47,
})

# Test alert
ok = bot.send_test()
print(f"Test alerts sent: {'OK' if ok else 'FAILED'}")
print(f"Check your Telegram for {len(bot._chat_ids)} chat(s)")
