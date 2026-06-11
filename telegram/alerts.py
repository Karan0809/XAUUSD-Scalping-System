import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from config.settings import get_settings

logger = logging.getLogger(__name__)
ET_TZ = ZoneInfo("America/New_York")


def fmt_et(dt: Optional[datetime] = None, fmt: str = "%I:%M %p") -> str:
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET_TZ).strftime(f"{fmt} %Z")


class TelegramNotifier:
    def __init__(self):
        self.settings = get_settings()
        raw = (self.settings.telegram_chat_id or "").strip()
        self._chat_ids = [c.strip() for c in raw.split(",") if c.strip()]
        self._enabled = bool(self.settings.telegram_token and self._chat_ids)
        self._base_url = (
            f"https://api.telegram.org/bot{self.settings.telegram_token}"
        )
        if self._enabled:
            logger.info(f"Telegram alerts enabled for {len(self._chat_ids)} chat(s)")
        else:
            logger.warning("Telegram alerts disabled (missing token or chat_id)")

    def _send(self, text: str, parse_mode: str = "HTML") -> bool:
        if not self._enabled:
            logger.debug(f"Telegram disabled, would send: {text[:50]}...")
            return False

        success = False
        for chat_id in self._chat_ids:
            try:
                url = f"{self._base_url}/sendMessage"
                payload = {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                }
                resp = requests.post(url, json=payload, timeout=10)
                resp.raise_for_status()
                success = True
            except requests.RequestException as e:
                logger.error(f"Telegram send to {chat_id} failed: {e}")
        return success

    def send_photo(self, photo_path: str, caption: str = "") -> bool:
        if not self._enabled:
            return False
        try:
            path = Path(photo_path)
            if not path.exists():
                logger.error(f"Photo not found: {photo_path}")
                return False
            url = f"{self._base_url}/sendPhoto"
            with open(path, "rb") as f:
                files = {"photo": f}
                data = {
                    "chat_id": self._chat_ids[0],
                    "caption": caption,
                    "parse_mode": "HTML",
                }
                resp = requests.post(url, files=files, data=data, timeout=30)
                resp.raise_for_status()
            logger.debug(f"Telegram photo sent: {photo_path}")
            return True
        except Exception as e:
            logger.error(f"Telegram photo send failed: {e}")
            return False

    def _exit_model_name(self, trade: Dict[str, Any]) -> str:
        tp3 = trade.get("tp3_lots", 0)
        tp2 = trade.get("tp2_lots", 0)
        tp1 = trade.get("tp1_lots", 0)
        if tp3 > 0:
            return "3-Target 30/40/30"
        elif tp2 > 0:
            return "2-Target 50/50"
        else:
            return "Single Target"

    def _setup_label(self, setup: str) -> str:
        labels = {
            "breakout_pullback": "ORB Breakout Pullback",
            "aggressive_fvg": "ORB Aggressive FVG",
            "range_reversal": "ORB Range Reversal",
            "free_pullback": "Free Pullback",
            "free_fvg": "Free FVG",
        }
        return labels.get(setup, setup)

    def _is_free_setup(self, setup: str) -> bool:
        return setup.startswith("free_")

    def _dir_emoji(self, direction: str) -> str:
        return "\U0001f7e2" if direction == "buy" else "\U0001f534"

    def _dir_arrow(self, direction: str) -> str:
        return "\U0001f847" if direction == "buy" else "\U0001f841"

    def alert_signal(self, signal: Dict[str, Any]) -> None:
        direction = signal.get("direction", "")
        entry = signal.get("entry", 0)
        sl = signal.get("sl", 0)
        tp = signal.get("tp", 0)
        setup = signal.get("setup", "")
        sl_dist = abs(entry - sl)
        rr = abs(entry - tp) / sl_dist if sl_dist > 0 else 0
        label = self._setup_label(setup)
        is_free = self._is_free_setup(setup)
        tag = "FREE" if is_free else "ORB"
        session_info = f" | {signal.get('session', '').upper()}" if not is_free else ""
        msg = (
            f"\U0001f4e1 <b>SIGNAL [{tag}]</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"{self._dir_emoji(direction)} {direction.upper()} {self._dir_arrow(direction)}{session_info}\n"
            f"Entry: <code>{entry:.2f}</code>\n"
            f"SL: <code>{sl:.2f}</code> ({sl_dist * 100:.0f} pips)\n"
            f"TP: <code>{tp:.2f}</code> (1:{rr:.1f})\n"
            f"{label}\n"
            f"{fmt_et(fmt='%I:%M:%S %p')}"
        )
        self._send(msg)

    def alert_trade_open(self, trade: Dict[str, Any]) -> None:
        direction = trade.get("type", "").lower()
        entry = trade.get("entry", 0)
        sl = trade.get("sl", 0)
        lot = trade.get("lot_size", 0)
        sl_dist = abs(entry - sl)
        risk_amt = sl_dist * lot * 100
        comm = round(self.settings.backtest_commission * lot, 2)
        balance = trade.get("balance", 0)
        risk_pct = (risk_amt / balance * 100) if balance > 0 else 0
        model = self._exit_model_name(trade)
        tp = trade.get("tp", 0)
        setup = trade.get("setup", "")
        label = self._setup_label(setup)
        is_free = self._is_free_setup(setup)
        tag = "FREE" if is_free else "ORB"
        session_info = f" | {trade.get('session', '').upper()}" if not is_free else ""

        tp1 = trade.get("tp1_lots", 0)
        tp2 = trade.get("tp2_lots", 0)
        tp3 = trade.get("tp3_lots", 0)
        pairs = []
        if tp3 > 0:
            pairs = [f"TP1 1:1 {tp1:.2f}", f"TP2 1:2 {tp2:.2f}", f"TP3 1:3 {tp3:.2f}"]
        elif tp2 > 0:
            pairs = [f"TP1 1:1 {tp1:.2f}", f"TP2 1:2 {tp2:.2f}"]
        else:
            pairs = [f"TP1 1:1 {tp1:.2f}"]
        targets = " | ".join(pairs)
        msg = (
            f"\U0001f4b0 <b>OPEN [{tag}]</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"{self._dir_emoji(direction)} {direction.upper()} {self._dir_arrow(direction)}{session_info}\n"
            f"Lot: {lot:.2f} | {model}\n"
            f"Entry: <code>{entry:.2f}</code> | SL: <code>{sl:.2f}</code>\n"
            f"Risk: ${risk_amt:.2f} ({risk_pct:.1f}%) | Comm: ${comm:.2f}\n"
            f"{label}\n"
            f"{targets}\n"
            f"{fmt_et(fmt='%I:%M:%S %p')}"
        )
        self._send(msg)

    def alert_trade_close(self, trade: Dict[str, Any]) -> None:
        direction = trade.get("type", "").lower()
        entry = trade.get("entry", 0)
        pnl = trade.get("pnl", 0)
        reason = trade.get("exit_reason", "")
        tp1_hit = trade.get("tp1_hit", False)
        tp2_hit = trade.get("tp2_hit", False)
        tp3_hit = trade.get("tp3_hit", False)
        lot = trade.get("original_lot_size", 0)
        balance = trade.get("balance", 0)
        setup = trade.get("setup", "")
        label = self._setup_label(setup)
        is_free = self._is_free_setup(setup)
        tag = "FREE" if is_free else "ORB"
        session_info = f" | {trade.get('session', '').upper()}" if not is_free else ""

        open_time = trade.get("open_time")
        close_time = trade.get("close_time")
        duration = ""
        if open_time and close_time:
            mins = int((close_time - open_time).total_seconds() / 60)
            duration = f"{mins}m"

        emoji = "\U0001f7e2" if pnl > 0 else "\U0001f534"

        hits = []
        if tp1_hit: hits.append("TP1")
        if tp2_hit: hits.append("TP2")
        if tp3_hit: hits.append("TP3")
        hits_str = " + ".join(hits) if hits else "\u2014"

        exit_line = f"P&L: <b>{'+' if pnl >= 0 else ''}${pnl:.2f}</b>"
        rr_total = sum([0.30 if tp1_hit else 0, 0.80 if tp2_hit else 0, 0.90 if tp3_hit else 0])
        if rr_total > 0:
            exit_line += f" | +{rr_total:.2f}R"

        reason_emoji = {"tp1": "\U0001f3c6", "tp2": "\U0001f3c6", "tp3": "\U0001f3c6", "trail": "\U0001f4c8", "be": "\U0001f504", "sl": "\U0001f6ab"}.get(reason, "\u2753")
        reason_label = reason.upper()
        if reason == "trail" and trade.get("tp3_lots", 0) > 0 and tp2_hit:
            reason_label = "TRAIL (runner)"

        msg = (
            f"{emoji} <b>CLOSE [{tag}]</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"{self._dir_emoji(direction)} {direction.upper()}{session_info}\n"
            f"Entry: <code>{entry:.2f}</code> | Lot: {lot:.2f}\n"
            f"{exit_line}\n"
            f"{reason_emoji} {reason_label} | {hits_str}\n"
            f"Duration: {duration}\n"
            f"\U0001f4b5 ${balance:.2f}\n"
            f"{label}\n"
            f"{fmt_et(fmt='%I:%M:%S %p')}"
        )
        self._send(msg)

    def alert_partial(self, trade: Dict[str, Any], reason: str, lots: float, price: float, profit: float, cumulative: float) -> None:
        direction = trade.get("type", "").lower()
        entry = trade.get("entry", 0)
        setup = trade.get("setup", "")
        label = self._setup_label(setup)
        is_free = self._is_free_setup(setup)
        tag = "FREE" if is_free else "ORB"

        emoji = "\U0001f7e2" if profit >= 0 else "\U0001f534"
        reason_upper = reason.upper()
        reason_emoji = {"TP1": "\U0001f3c6", "TP2": "\U0001f3c6", "TP3": "\U0001f3c6", "TRAIL": "\U0001f4c8", "BE": "\U0001f504", "SL": "\U0001f6ab"}.get(reason_upper, "\u2795")

        msg = (
            f"{emoji} <b>PARTIAL [{tag}]</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"{self._dir_emoji(direction)} {direction.upper()}\n"
            f"{reason_emoji} {reason_upper}\n"
            f"Lots: {lots:.2f} @ <code>{price:.2f}</code>\n"
            f"P&L: <b>{'+' if profit >= 0 else ''}${profit:.2f}</b> (cum: ${cumulative:.2f})\n"
            f"Entry: <code>{entry:.2f}</code>\n"
            f"{label}\n"
            f"{fmt_et(fmt='%I:%M:%S %p')}"
        )
        self._send(msg)

    def alert_error(self, error_message: str) -> None:
        msg = (
            f"\u26a0\ufe0f <b>ERROR</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"{error_message}\n"
            f"{fmt_et(fmt='%I:%M:%S %p')}"
        )
        self._send(msg)

    def alert_heartbeat(self, status: str) -> None:
        msg = (
            f"\u2705 <b>RUNNING</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"{status}\n"
            f"{fmt_et(fmt='%I:%M %p')}"
        )
        self._send(msg)

    def alert_daily_summary(self, summary: Dict[str, Any]) -> None:
        pnl = summary.get("total_pnl", 0)
        emoji = "\U0001f44d" if pnl > 0 else "\U0001f44e"
        orbs = summary.get("orb_trades", 0)
        frees = summary.get("free_trades", 0)
        msg = (
            f"{emoji} <b>Daily Summary</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"Date: {summary.get('date', 'N/A')}\n"
            f"Trades: {summary.get('total_trades', 0)} (ORB {orbs} | Free {frees})\n"
            f"Wins: {summary.get('wins', 0)} | Losses: {summary.get('losses', 0)}\n"
            f"WR: {summary.get('win_rate', 0):.1f}%\n"
            f"P&L: <b>{'+' if pnl >= 0 else ''}${pnl:.2f}</b>\n"
            f"PF: {summary.get('profit_factor', 'N/A')}\n"
            f"DD: {summary.get('max_drawdown', 0):.1f}%\n"
            f"\U0001f4b5 ${summary.get('balance', 0):.2f}\n"
            f"{fmt_et(fmt='%Y-%m-%d %I:%M %p')}"
        )
        self._send(msg)

    def send_test(self) -> bool:
        msg = (
            f"\U0001f514 <b>TEST ALERT</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"Bot is live and connected\n"
            f"ORB + Free Trade combined strategy\n"
            f"Telegram notifications working\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"{fmt_et(fmt='%Y-%m-%d %I:%M:%S %p')}"
        )
        return self._send(msg)

    def health_check(self) -> bool:
        if not self._enabled:
            return False
        try:
            url = f"{self._base_url}/getMe"
            resp = requests.get(url, timeout=10)
            return resp.status_code == 200
        except requests.RequestException:
            return False
