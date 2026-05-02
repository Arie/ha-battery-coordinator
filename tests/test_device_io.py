"""device_io read/write resilience.

The brain trusts every Reading as ground truth — there is no concept of
"this read failed". Returning a default zero-status on a network blip
makes the brain see Zen at 0% SOC / 0W and trigger spurious state
transitions (relay clicks, PIB mode flips). These tests pin down the
last-known caching contract so a transient HTTP error doesn't propagate
into a hardware command.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "battery-coordinator" / "app"))

from device_io import ZendureDevice


class _FakeResponse:
    def __init__(self, *, status: int = 200, payload: dict | None = None,
                 raise_on_get: Exception | None = None):
        self.status = status
        self._payload = payload or {}
        self._raise = raise_on_get

    async def __aenter__(self):
        if self._raise:
            raise self._raise
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self, content_type=None):
        return self._payload


class _FakeSession:
    """Returns a queue of (status, payload, exc) tuples — one per get()."""

    def __init__(self, scripted):
        self._queue = list(scripted)

    def get(self, url, timeout=None):
        if not self._queue:
            return _FakeResponse(status=200, payload={})
        item = self._queue.pop(0)
        return _FakeResponse(**item)


_GOOD_REPORT = {
    "sn": "HEC-TEST",
    "properties": {
        "acMode": 1,
        "gridInputPower": 800,
        "outputHomePower": 0,
        "electricLevel": 75,
        "inputLimit": 800,
        "outputLimit": 0,
        "hyperTmp": 2931,  # 20°C
        "packNum": 2,
    },
}


class TestZendureReadResilience:
    """Single failed read must not erase what we knew about the device.

    Without this, a 1-second network blip → ZendureStatus(0,0,0,...) →
    brain sees Zen at 0% SOC and 0W power → triggers SLEEP / mode-flip
    transitions even though the device is happily charging at 800W.
    """

    @pytest.mark.asyncio
    async def test_read_failure_returns_last_known(self):
        zen = ZendureDevice("1.2.3.4")
        session = _FakeSession([
            {"status": 200, "payload": _GOOD_REPORT},
            {"status": 500, "payload": {}},
        ])

        first = await zen.read(session)
        assert first.power == 800
        assert first.soc == 75
        assert first.sn == "HEC-TEST"

        second = await zen.read(session)
        # On HTTP 500, last-known status is preserved instead of zeroed.
        assert second.power == 800
        assert second.soc == 75
        assert second.sn == "HEC-TEST"

    @pytest.mark.asyncio
    async def test_exception_returns_last_known(self):
        zen = ZendureDevice("1.2.3.4")
        session = _FakeSession([
            {"status": 200, "payload": _GOOD_REPORT},
            {"raise_on_get": ConnectionError("network blip")},
        ])

        first = await zen.read(session)
        assert first.power == 800

        second = await zen.read(session)
        assert second.power == 800, (
            "Network blip on read should return last-known status, not "
            "zeros — the brain treats zeros as 'Zen drained' and would "
            "force a state transition."
        )

    @pytest.mark.asyncio
    async def test_first_read_failure_returns_zeros(self):
        # Before any successful read, there is no last-known to return.
        # Zeros are the default; the brain's startup detection has its own
        # holdoff guards to handle this.
        zen = ZendureDevice("1.2.3.4")
        session = _FakeSession([{"raise_on_get": ConnectionError("dead on arrival")}])
        status = await zen.read(session)
        assert status.power == 0
        assert status.soc == 0
        assert status.sn == ""

    @pytest.mark.asyncio
    async def test_sn_persists_across_failed_reads(self):
        # SN was already cached by the previous design; this just confirms
        # the new last-known caching doesn't accidentally discard it.
        zen = ZendureDevice("1.2.3.4")
        session = _FakeSession([
            {"status": 200, "payload": _GOOD_REPORT},
            {"raise_on_get": TimeoutError()},
            {"raise_on_get": TimeoutError()},
        ])
        await zen.read(session)
        await zen.read(session)
        third = await zen.read(session)
        assert third.sn == "HEC-TEST"
