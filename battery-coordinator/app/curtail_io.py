"""Solar-limit writes for the tick loop, with the fail-open restores.

Kept out of main.py so the shutdown path can be tested. The curtailer only
learns a write's outcome once ``set_value`` returns; a cancellation (SIGTERM)
while the call is out leaves it believing the old limit, although HA may
already have applied the new one. ``in_flight`` remembers that doubt, so the
restore cannot skip on a stale ``limit == 100``.
"""

import logging
from collections.abc import Awaitable, Callable

from curtailer import SolarCurtailer

log = logging.getLogger("coordinator")


class CurtailIO:
    def __init__(
        self,
        set_value: Callable[[int], Awaitable[bool]],
        curtailer: SolarCurtailer,
        *,
        dry_run: bool,
    ) -> None:
        self._set_value = set_value
        self._curtailer = curtailer
        self._dry_run = dry_run
        self.in_flight = False

    async def write(self, value: int, reason: str, t: float) -> bool:
        """Write the limit, report the outcome to the curtailer, log it.

        A cancellation mid-call propagates with ``in_flight`` left set.
        """
        old = self._curtailer.limit
        self.in_flight = True
        ok = await self._set_value(value)
        self.in_flight = False
        if ok:
            self._curtailer.mark_written(value, t)
        else:
            self._curtailer.mark_write_failed(t)
        old_s = "?" if old is None else str(old)
        log.info(f"SolarEdge limit {old_s}→{value}% ({reason})" + ("" if ok else " FAILED!"))
        return ok

    async def restore_full(self, reason: str, t: float) -> bool | None:
        """Write 100 unless it is known to be in place. None = nothing written."""
        if self._dry_run:
            return None
        if self._curtailer.limit == 100 and not self.in_flight:
            return None
        return await self.write(100, reason, t)
