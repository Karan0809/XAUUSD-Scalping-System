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

    def alert_signal(self, signal: Dict[str, Any]) -> None:
        direction = signal.get("direction", "").upper()
        entry = signal.get("entry", 0)
        sl = signal.get("sl", 0)
        tp = signal.get("tp", 0)
        setup = signal.get("setup", "")
        session = signal.get("session", "")
        sl_dist = abs(entry - sl)
        msg = (
            f"\U0001f4e1 <b>ORB Scalp Signal</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"<b>{direction}</b> | {session}\n"
            f"Entry: <code>{entry:.2f}</code>  SL: <code>{sl:.2f}</code>\n"
            f"TP: <code>{tp:.2f}</code>  |  R:R 1:2\n"
            f"Risk: {sl_dist:.2f} pts\n"
            f"Setup: {setup}\n"
            f"{fmt_et(fmt='%H:%M')}"
        )
        self._send(msg)

    def alert_trade_open(self, trade: Dict[str, Any]) -> None:
        direction = trade.get("type", "").upper()
        entry = trade.get("entry", 0)
        sl = trade.get("sl", 0)
        lot = trade.get("lot_size", 0)
        sl_dist = abs(entry - sl)
        risk_amt = sl_dist * lot * 100
        tp1 = trade.get("tp1_lots", 0)
        tp2 = trade.get("tp2_lots", 0)
        tp3 = trade.get("tp3_lots", 0)
        msg = (
            f"\U0001f4b0 <b>ORB Scalp  OPEN</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"<b>{direction}</b>  {lot:.2f} lots\n"
            f"Entry: <code>{entry:.2f}</code>  SL: <code>{sl:.2f}</code>\n"
            f"Risk: ${risk_amt:.2f}\n"
            f"TP1 (1:1): {tp1:.2f} lots  |  TP2 (1:2): {tp2:.2f} lots  |  TP3 (1:3): {tp3:.2f} lots\n"
            f"{fmt_et(fmt='%I:%M:%S %p')}"
        )
        self._send(msg)

    def alert_trade_close(self, trade: Dict[str, Any]) -> None:
        direction = trade.get("type", "").upper()
        entry = trade.get("entry", 0)
        pnl = trade.get("pnl", 0)
        reason = trade.get("exit_reason", "")
        tp1_hit = trade.get("tp1_hit", False)
        tp2_hit = trade.get("tp2_hit", False)
        tp3_hit = trade.get("tp3_hit", False)

        open_time = trade.get("open_time")
        close_time = trade.get("close_time")
        duration = ""
        if open_time and close_time:
            mins = int((close_time - open_time).total_seconds() / 60)
            duration = f"{mins}m"

        emoji = "\U0001f4b0" if pnl > 0 else "\U0001f534"

        hits = []
        if tp1_hit: hits.append("TP1")
        if tp2_hit: hits.append("TP2")
        if tp3_hit: hits.append("TP3")
        hits_str = " + ".join(hits) if hits else "\u2014"

        exit_line = f"Exit: <b>{'+' if pnl >= 0 else ''}${pnl:.2f}</b>"
        rr_total = sum([0.30 if tp1_hit else 0, 0.80 if tp2_hit else 0, 0.90 if tp3_hit else 0])
        if rr_total > 0:
            exit_line += f"  |  <b>+{rr_total:.2f}R</b>"

        msg = (
            f"{emoji} <b>ORB Scalp  CLOSE</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"<b>{direction}</b>\n"
            f"{exit_line}\n"
            f"Entry: <code>{entry:.2f}</code>\n"
            f"Targets: {hits_str}\n"
            f"Reason: {reason.upper()}\n"
            f"{duration}  {fmt_et(fmt='%I:%M:%S %p')}"
        )
        self._send(msg)

    def alert_error(self, error_message: str) -> None:
        msg = (
            f"\u26a0\ufe0f <b>ORB Scalp Error</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"{error_message}\n"
            f"Time: {fmt_et(fmt='%I:%M:%S %p')}"
        )
        self._send(msg)

    def alert_heartbeat(self, status: str) -> None:
        msg = (
            f"\u2705 <b>ORB Scalp Heartbeat</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"{status}\n"
            f"Time: {fmt_et(fmt='%I:%M %p')}"
        )
        self._send(msg)

    def alert_daily_summary(self, summary: Dict[str, Any]) -> None:
        pnl = summary.get("total_pnl", 0)
        emoji = "\U0001f44d" if pnl > 0 else "\U0001f44e"
        msg = (
            f"{emoji} <b>ORB Scalp Daily Summary</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"Date: {summary.get('date', 'N/A')}\n"
            f"Trades: {summary.get('total_trades', 0)}\n"
            f"Wins: {summary.get('wins', 0)}  |  Losses: {summary.get('losses', 0)}\n"
            f"Win Rate: {summary.get('win_rate', 0):.1f}%\n"
            f"P&L: <b>{'+' if pnl >= 0 else ''}${pnl:.2f}</b>\n"
            f"Profit Factor: {summary.get('profit_factor', 'N/A')}\n"
            f"Max DD: {summary.get('max_drawdown', 0):.1f}%\n"
            f"Balance: ${summary.get('balance', 0):.2f}\n"
            f"{fmt_et(fmt='%Y-%m-%d %I:%M %p')}"
        )
        self._send(msg)

    def send_test(self) -> bool:
        msg = (
            f"\U0001f514 <b>ORB Scalper  TEST ALERT</b>\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"Bot is live and connected\n"
            f"Telegram notifications working\n"
            f"30/40/30 TP model active\n"
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
