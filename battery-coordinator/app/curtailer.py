"""Solar curtailer: decides the inverter's active power limit (0-100 %).

Pure controller, no I/O. The caller reads P1 and solar power, asks
``decide()`` once per tick, performs any write it returns, then reports the
outcome via ``mark_written`` / ``mark_write_failed``.
"""

import math
from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class CurtailDecision:
    write: int | None  # limit % to write this tick, None = no write
    reason: str  # startup | gate off | import pays | trim down | trim up | heartbeat | ""


NO_WRITE = CurtailDecision(None, "")


class SolarCurtailer:
    HEARTBEAT_S: ClassVar[int] = 300
    # Trim up only while the panels produce within this of the limit's cap:
    # below it the sun, not the limit, binds, and raising the limit would
    # only queue up an export burst for when the sun returns.
    HELD_BACK_MARGIN_W: ClassVar[int] = 150
    # Fail open: an import that outlasts this trims up even when the panels
    # look short of the cap (solar_rated_w set too high, sensor off).
    UNHELD_IMPORT_S: ClassVar[int] = 60

    def __init__(
        self,
        *,
        rated_w: int = 6000,
        target_w: int = -30,
        deadband_w: int = 30,
        step_pct: int = 1,
        min_write_s: int = 3,
        settle_down_s: int = 20,
        settle_up_s: int = 5,
    ) -> None:
        self.rated_w = rated_w
        self.target_w = target_w
        self.deadband_w = deadband_w
        self.step_pct = step_pct
        self.min_write_s = min_write_s
        self.settle_down_s = settle_down_s
        self.settle_up_s = settle_up_s

        self._limit: int | None = None
        self._ever_attempted = False
        self._last_attempt_t: float | None = None
        self._last_write_t: float | None = None
        self._down_since: float | None = None
        self._up_since: float | None = None
        # Continuous import, regardless of held-back; writes do not reset it.
        self._import_since: float | None = None

    @property
    def limit(self) -> int | None:
        """Last confirmed write; None = unknown."""
        return self._limit

    def mark_written(self, limit: int, t: float) -> None:
        self._limit = limit
        self._ever_attempted = True
        self._last_attempt_t = t
        self._last_write_t = t
        self._reset_timers()

    def mark_write_failed(self, t: float) -> None:
        self._limit = None
        self._ever_attempted = True
        self._last_attempt_t = t
        self._last_write_t = None
        self._reset_timers()

    def decide(
        self,
        *,
        p1: float,
        solar_w: float,
        curtail: bool,
        import_pays: bool,
        saturated: bool,
        t: float,
    ) -> CurtailDecision:
        if not (curtail or import_pays):
            self._reset_timers()
            self._import_since = None
            if self._limit == 100:
                return NO_WRITE
            reason = "startup" if self._limit is None and not self._ever_attempted else "gate off"
            return self._throttled(100, reason, t)

        if import_pays:
            self._reset_timers()
            self._import_since = None
            if self._limit != 0:
                return self._throttled(0, "import pays", t)
            return self._heartbeat(t)

        decision = self._trim(p1=p1, solar_w=solar_w, saturated=saturated, t=t)
        if decision.write is not None:
            return decision
        return self._heartbeat(t)

    def _reset_timers(self) -> None:
        self._down_since = None
        self._up_since = None

    def _throttled(self, value: int, reason: str, t: float) -> CurtailDecision:
        """Return a write unless one was attempted less than min_write_s ago."""
        if self._last_attempt_t is not None and t - self._last_attempt_t < self.min_write_s:
            return NO_WRITE
        return CurtailDecision(value, reason)

    def _heartbeat(self, t: float) -> CurtailDecision:
        if self._limit is None or self._last_write_t is None:
            return NO_WRITE
        if t - self._last_write_t < self.HEARTBEAT_S:
            return NO_WRITE
        return self._throttled(self._limit, "heartbeat", t)

    def _snap_down(self, pct: float) -> int:
        return math.floor(math.floor(pct) / self.step_pct) * self.step_pct

    def _snap_up(self, pct: float) -> int:
        return math.ceil(math.ceil(pct) / self.step_pct) * self.step_pct

    def _trim(self, *, p1: float, solar_w: float, saturated: bool, t: float) -> CurtailDecision:
        want_down = p1 < self.target_w - self.deadband_w and saturated
        importing = p1 > self.target_w + self.deadband_w
        if not importing:
            self._import_since = None
        elif self._import_since is None:
            self._import_since = t
        held_back = (
            self._limit is None
            or solar_w <= 0  # stale reading: fail open
            or solar_w >= self._limit / 100 * self.rated_w - self.HELD_BACK_MARGIN_W
            or (self._import_since is not None and t - self._import_since >= self.UNHELD_IMPORT_S)
        )
        want_up = importing and held_back
        if want_down:
            if self._down_since is None:
                self._down_since = t
        else:
            self._down_since = None
        if want_up:
            if self._up_since is None:
                self._up_since = t
        else:
            self._up_since = None

        current = 100 if self._limit is None else self._limit

        if self._down_since is not None and t - self._down_since >= self.settle_down_s:
            if solar_w <= 0:
                return NO_WRITE
            pct = (solar_w - (self.target_w - p1)) / self.rated_w * 100
            new = min(self._snap_down(pct), current - self.step_pct)
            new = max(0, min(100, new))
            if new == self._limit:
                return NO_WRITE
            return self._throttled(new, "trim down", t)

        if self._up_since is not None and t - self._up_since >= self.settle_up_s:
            if self._limit == 100:
                return NO_WRITE
            pct = (solar_w + (p1 - self.target_w)) / self.rated_w * 100
            new = min(100, max(self._snap_up(pct), current + self.step_pct))
            if new == self._limit:
                return NO_WRITE
            return self._throttled(new, "trim up", t)

        return NO_WRITE
