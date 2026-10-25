"""Solar-limit writes with in-flight tracking, and the fail-open restores."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "battery-coordinator" / "app"))

from curtail_io import CurtailIO
from curtailer import SolarCurtailer


class FakeWriter:
    """Records every value; optionally blocks until released."""

    def __init__(self, ok: bool = True, block: bool = False):
        self.ok = ok
        self.block = block
        self.calls: list[int] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def set_value(self, value: int) -> bool:
        self.calls.append(value)
        self.started.set()
        if self.block:
            await self.release.wait()
        return self.ok


@pytest.mark.asyncio
async def test_restore_after_cancel_mid_write_from_100():
    """HA may have applied the 0 already, so the restore must not trust limit==100."""
    c = SolarCurtailer()
    c.mark_written(100, 0.0)
    w = FakeWriter(block=True)
    cio = CurtailIO(w.set_value, c, dry_run=False)

    task = asyncio.create_task(cio.write(0, "import pays", 1.0))
    await w.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert c.limit == 100  # never confirmed

    w.block = False
    assert await cio.restore_full("shutdown", 2.0) is True
    assert w.calls == [0, 100]
    assert c.limit == 100


@pytest.mark.asyncio
async def test_restore_when_limit_unknown():
    c = SolarCurtailer()
    w = FakeWriter()
    cio = CurtailIO(w.set_value, c, dry_run=False)
    assert await cio.restore_full("startup", 0.0) is True
    assert w.calls == [100]
    assert c.limit == 100


@pytest.mark.asyncio
async def test_restore_after_failed_write():
    c = SolarCurtailer()
    c.mark_written(40, 0.0)
    w = FakeWriter(ok=False)
    cio = CurtailIO(w.set_value, c, dry_run=False)
    assert await cio.write(30, "trim down", 5.0) is False
    assert c.limit is None
    w.ok = True
    assert await cio.restore_full("shutdown", 6.0) is True
    assert w.calls == [30, 100]


@pytest.mark.asyncio
async def test_no_restore_when_already_full_and_nothing_in_flight():
    c = SolarCurtailer()
    w = FakeWriter()
    cio = CurtailIO(w.set_value, c, dry_run=False)
    assert await cio.write(100, "startup", 0.0) is True
    assert await cio.restore_full("shutdown", 10.0) is None
    assert w.calls == [100]


@pytest.mark.asyncio
async def test_no_restore_in_dry_run():
    c = SolarCurtailer()
    w = FakeWriter()
    cio = CurtailIO(w.set_value, c, dry_run=True)
    assert await cio.restore_full("startup", 0.0) is None
    assert await cio.restore_full("shutdown", 1.0) is None
    assert w.calls == []


@pytest.mark.asyncio
async def test_failed_startup_restore_leaves_limit_unknown():
    """The curtailer then retries on its own ("gate off") every min_write_s."""
    c = SolarCurtailer()
    w = FakeWriter(ok=False)
    cio = CurtailIO(w.set_value, c, dry_run=False)
    assert await cio.restore_full("startup", 0.0) is False
    assert c.limit is None
    d = c.decide(p1=0.0, solar_w=0.0, curtail=False, import_pays=False, saturated=False, t=10.0)
    assert d.write == 100


@pytest.mark.asyncio
async def test_successful_startup_restore_is_not_repeated_by_the_curtailer():
    c = SolarCurtailer()
    w = FakeWriter()
    cio = CurtailIO(w.set_value, c, dry_run=False)
    await cio.restore_full("startup", 0.0)
    d = c.decide(p1=0.0, solar_w=0.0, curtail=False, import_pays=False, saturated=False, t=10.0)
    assert d.write is None
