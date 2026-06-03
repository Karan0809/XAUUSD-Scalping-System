import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

FOREX_FACTORY_URL = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
)

HIGH_IMPACT_KEYWORDS = [
    "employment", "cpi", "gdp", "fomc", "ppi", "retail sales",
    "non-farm", "unemployment", "ism", "nfp",
    "federal funds rate", "interest rate decision",
    "consumer confidence", "durable goods", "housing starts",
]


class NewsFilter:
    def __init__(self, blackout_minutes: int = 30):
        self._blackout_minutes = blackout_minutes
        self._events: List[dict] = []

    def fetch_events(self) -> None:
        try:
            resp = requests.get(FOREX_FACTORY_URL, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            self._events = []
            for event in data:
                title = event.get("title", "")
                impact = event.get("impact", "").strip()
                country = event.get("country", "")
                date_str = event.get("date", "")
                time_str = event.get("time", "")
                if country == "USD" and "High" in impact:
                    for kw in HIGH_IMPACT_KEYWORDS:
                        if kw in title.lower():
                            try:
                                dt = datetime.strptime(
                                    f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
                                )
                                dt = dt.replace(tzinfo=ZoneInfo("US/Eastern")).astimezone(timezone.utc)
                                self._events.append({
                                    "time": dt,
                                    "title": title,
                                })
                            except (ValueError, TypeError):
                                pass
                            break
            self._events.sort(key=lambda x: x["time"])
            logger.info(
                f"NewsFilter: {len(self._events)} high-impact USD events loaded"
            )
        except Exception as e:
            logger.warning(f"NewsFilter fetch failed: {e}")

    def is_blackout(
        self, current_time: datetime
    ) -> Tuple[bool, Optional[str]]:
        for event in self._events:
            et = event["time"]
            diff_sec = abs((current_time - et).total_seconds())
            if diff_sec < self._blackout_minutes * 60:
                return (
                    True,
                    f"News blackout: {event['title']} "
                    f"at {et.strftime('%H:%M UTC')}",
                )
        return False, None
