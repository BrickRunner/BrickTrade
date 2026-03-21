from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

# UTC+3  (Moscow)
_MSK = timezone(timedelta(hours=3))


class MOEXSchedule:
    """MOEX trading schedule for equities (TQBR).

    Morning session: 06:50 – 09:50 MSK  (blue-chips, since 2024)
    Main session:    10:00 – 18:40 MSK   (continuous) + 18:40-18:50 (closing auction)
    Evening session: 19:05 – 23:50 MSK
    Weekends and holidays are closed.
    """

    MORNING_START = time(6, 50)
    MORNING_END = time(9, 50)
    MAIN_START = time(10, 0)
    MAIN_END = time(18, 50)
    EVENING_START = time(19, 5)
    EVENING_END = time(23, 50)

    def _now_msk(self) -> datetime:
        return datetime.now(tz=_MSK)

    def is_trading_hours(self) -> bool:
        now = self._now_msk()
        if now.weekday() >= 5:
            return False
        t = now.time()
        return (
            (self.MORNING_START <= t <= self.MORNING_END)
            or (self.MAIN_START <= t <= self.MAIN_END)
            or (self.EVENING_START <= t <= self.EVENING_END)
        )

    def session_type(self) -> str:
        """Return 'morning', 'main', 'evening', or 'closed'."""
        now = self._now_msk()
        if now.weekday() >= 5:
            return "closed"
        t = now.time()
        if self.MORNING_START <= t <= self.MORNING_END:
            return "morning"
        if self.MAIN_START <= t <= self.MAIN_END:
            return "main"
        if self.EVENING_START <= t <= self.EVENING_END:
            return "evening"
        return "closed"

    def next_session_open(self) -> datetime:
        """Return the next session open as a timezone-aware MSK datetime."""
        now = self._now_msk()
        t = now.time()

        if now.weekday() < 5:
            # Before morning session today.
            if t < self.MORNING_START:
                return now.replace(hour=6, minute=50, second=0, microsecond=0)
            # Between morning and main.
            if self.MORNING_END < t < self.MAIN_START:
                return now.replace(hour=10, minute=0, second=0, microsecond=0)
            # Between main and evening.
            if self.MAIN_END < t < self.EVENING_START:
                return now.replace(hour=19, minute=5, second=0, microsecond=0)

        # Next business day morning.
        nxt = now + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt.replace(hour=6, minute=50, second=0, microsecond=0)

    def seconds_until_close(self) -> float:
        """Seconds until current session closes. 0 if market is closed."""
        now = self._now_msk()
        t = now.time()
        if now.weekday() >= 5:
            return 0.0
        if self.MORNING_START <= t <= self.MORNING_END:
            close_dt = now.replace(hour=9, minute=50, second=0, microsecond=0)
            return max(0.0, (close_dt - now).total_seconds())
        if self.MAIN_START <= t <= self.MAIN_END:
            close_dt = now.replace(hour=18, minute=50, second=0, microsecond=0)
            return max(0.0, (close_dt - now).total_seconds())
        if self.EVENING_START <= t <= self.EVENING_END:
            close_dt = now.replace(hour=23, minute=50, second=0, microsecond=0)
            return max(0.0, (close_dt - now).total_seconds())
        return 0.0
