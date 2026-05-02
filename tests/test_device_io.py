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

from device_io import HWP1Meter, OptionalHASensor, ZendureDevice


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


class TestZendureFetchSN:
    """fetch_sn polls until the device returns a usable SN.

    Production bug 2026-04-25 09:59 in coordinator_cli: after a host
    reboot the integration took ~80s to populate SN; the coordinator
    fetched once, got empty, and stayed observe-only for the rest of
    the day. The retry pattern was previously only in the CLI; the
    add-on path read once at startup and missed the retry."""

    @pytest.mark.asyncio
    async def test_returns_sn_immediately(self):
        zen = ZendureDevice("1.2.3.4")
        session = _FakeSession([{"status": 200, "payload": _GOOD_REPORT}])
        sn = await zen.fetch_sn(session, max_attempts=3, delay_s=0)
        assert sn == "HEC-TEST"

    @pytest.mark.asyncio
    async def test_retries_past_empty(self):
        empty = {"sn": "", "properties": {}}
        zen = ZendureDevice("1.2.3.4")
        session = _FakeSession([
            {"status": 200, "payload": empty},
            {"status": 200, "payload": empty},
            {"status": 200, "payload": _GOOD_REPORT},
        ])
        sn = await zen.fetch_sn(session, max_attempts=10, delay_s=0)
        assert sn == "HEC-TEST"

    @pytest.mark.asyncio
    async def test_retries_past_exceptions(self):
        zen = ZendureDevice("1.2.3.4")
        session = _FakeSession([
            {"raise_on_get": ConnectionError("dead")},
            {"raise_on_get": TimeoutError()},
            {"status": 200, "payload": _GOOD_REPORT},
        ])
        sn = await zen.fetch_sn(session, max_attempts=10, delay_s=0)
        assert sn == "HEC-TEST"

    @pytest.mark.asyncio
    async def test_returns_empty_after_max_attempts(self):
        zen = ZendureDevice("1.2.3.4")
        session = _FakeSession([
            {"raise_on_get": ConnectionError("nope")},
            {"raise_on_get": ConnectionError("nope")},
        ])
        sn = await zen.fetch_sn(session, max_attempts=2, delay_s=0)
        assert sn == ""


class _FakeStateSession:
    """Returns scripted HA /api/states/<entity> responses."""

    def __init__(self, scripted):
        self._queue = list(scripted)

    def get(self, url, headers=None, timeout=None):
        item = self._queue.pop(0) if self._queue else {"status": 200, "payload": {"state": "unavailable"}}
        return _FakeResponse(**item)


class _RecordingSession:
    """Captures every write payload so tests can assert what was sent."""

    def __init__(self):
        self.writes: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.writes.append({"url": url, "json": json})
        return _FakeResponse(status=200, payload={})


class TestZendureStandbyFlashWear:
    """standby() persists smartMode=0 to flash; the brain re-asserts target=0
    every 30s in SLEEP / PIB_DISCHARGE. Flashing every 30s wears the device.

    Heartbeat re-assertions for an already-zero target should use a
    RAM-only command, not a flash write. The first transition into
    target=0 should still flash so the device deep-sleeps across reboots.
    """

    @pytest.mark.asyncio
    async def test_standby_writes_flash_smartmode_zero(self):
        zen = ZendureDevice("1.2.3.4")
        zen._sn = "TEST"
        session = _RecordingSession()
        await zen.standby(session)
        assert session.writes[0]["json"]["properties"]["smartMode"] == 0

    @pytest.mark.asyncio
    async def test_hold_zero_writes_ram_smartmode_one(self):
        zen = ZendureDevice("1.2.3.4")
        zen._sn = "TEST"
        session = _RecordingSession()
        await zen.hold_zero(session)
        props = session.writes[0]["json"]["properties"]
        assert props["smartMode"] == 1, (
            "Heartbeat re-assertion of target=0 must NOT flash (smartMode=0). "
            "Use RAM-only smartMode=1 with outputLimit=inputLimit=0."
        )
        assert props["outputLimit"] == 0
        assert props["inputLimit"] == 0


class _FakeMultiURLSession:
    """Routes get() calls by URL substring to a scripted response queue.

    HWP1Meter hits two endpoints (/api/measurement and /api/batteries) per
    read, so a flat queue would couple ordering to the implementation.
    Keying by URL keeps the tests readable and order-independent.
    """

    def __init__(self, scripts: dict[str, list[dict]]):
        self._scripts = {k: list(v) for k, v in scripts.items()}

    def get(self, url, headers=None, timeout=None, ssl=None):
        for key, queue in self._scripts.items():
            if key in url:
                if not queue:
                    return _FakeResponse(status=200, payload={})
                return _FakeResponse(**queue.pop(0))
        return _FakeResponse(status=404, payload={})


_GOOD_MEASUREMENT = {"power_w": 850.0}
_GOOD_BATTERIES = {
    "power_w": 600.0,
    "mode": "zero",
    "permissions": ["charge_allowed"],
    "battery_count": 2,
}


class TestHWP1ReadResilience:
    """Same contract as ZendureDevice: a transient read failure must not
    erase what we knew. The brain treats every Reading as ground truth,
    and P1Status drives p1.grid_power into the Reading. Zeroing on a blip
    propagates a false 'grid balanced' signal that resets transition
    holdoff timers and (with bad-status responses) hides auth failures.
    """

    @pytest.mark.asyncio
    async def test_measurement_failure_returns_last_known_grid_power(self):
        meter = HWP1Meter("1.2.3.4", "tok")
        session = _FakeMultiURLSession({
            "/api/measurement": [
                {"status": 200, "payload": _GOOD_MEASUREMENT},
                {"raise_on_get": ConnectionError("blip")},
            ],
            "/api/batteries": [
                {"status": 200, "payload": _GOOD_BATTERIES},
                {"status": 200, "payload": _GOOD_BATTERIES},
            ],
        })
        first = await meter.read(session)
        assert first.grid_power == 850.0

        second = await meter.read(session)
        assert second.grid_power == 850.0, (
            "Network blip on /api/measurement returned 0 instead of "
            "last-known 850W. The brain reads this as 'grid balanced' and "
            "resets all P1-driven transition holdoffs on every blip."
        )

    @pytest.mark.asyncio
    async def test_batteries_failure_returns_last_known_pib_state(self):
        meter = HWP1Meter("1.2.3.4", "tok")
        session = _FakeMultiURLSession({
            "/api/measurement": [
                {"status": 200, "payload": _GOOD_MEASUREMENT},
                {"status": 200, "payload": _GOOD_MEASUREMENT},
            ],
            "/api/batteries": [
                {"status": 200, "payload": _GOOD_BATTERIES},
                {"raise_on_get": TimeoutError()},
            ],
        })
        first = await meter.read(session)
        assert first.pib_power == 600.0
        assert first.pib_count == 2
        assert first.pib_mode == "zero"

        second = await meter.read(session)
        assert second.pib_power == 600.0
        assert second.pib_count == 2
        assert second.pib_mode == "zero"
        assert second.pib_permissions == ["charge_allowed"]

    @pytest.mark.asyncio
    async def test_non_200_status_uses_last_known(self):
        # 401 (bad token) or 500 with a JSON body would currently silently
        # return 0 from data.get("power_w", 0). Status check + last-known
        # should suppress that.
        meter = HWP1Meter("1.2.3.4", "tok")
        session = _FakeMultiURLSession({
            "/api/measurement": [
                {"status": 200, "payload": _GOOD_MEASUREMENT},
                {"status": 401, "payload": {"error": "unauthorized"}},
            ],
            "/api/batteries": [
                {"status": 200, "payload": _GOOD_BATTERIES},
                {"status": 200, "payload": _GOOD_BATTERIES},
            ],
        })
        await meter.read(session)
        second = await meter.read(session)
        assert second.grid_power == 850.0, (
            f"HTTP 401 on /api/measurement masked as grid_power=0 "
            f"(got {second.grid_power}). Without a status check, an auth "
            f"failure looks identical to a balanced grid."
        )

    @pytest.mark.asyncio
    async def test_first_read_failure_returns_zeros(self):
        # No last-known to fall back to; defaults are correct.
        meter = HWP1Meter("1.2.3.4", "tok")
        session = _FakeMultiURLSession({
            "/api/measurement": [{"raise_on_get": ConnectionError("dead on arrival")}],
            "/api/batteries": [{"raise_on_get": ConnectionError("dead on arrival")}],
        })
        status = await meter.read(session)
        assert status.grid_power == 0.0
        assert status.pib_power == 0.0
        assert status.pib_count == 0


class TestOptionalHASensorStaleness:
    """Cached values shouldn't be returned forever after HA dies.

    Stale SOC values are dangerous: if HA goes down at 50% SOC and the
    PIB actually drains to 0%, the brain still sees 50% and won't exit
    PIB_DISCHARGE. After a max_stale_s window, the cache is invalidated
    so the brain at least sees a fresh-but-default 0 and can reach a
    safe state.
    """

    @pytest.mark.asyncio
    async def test_returns_cached_within_window(self):
        sensor = OptionalHASensor("http://ha:8123", "tok", "sensor.x", max_stale_s=60)
        session = _FakeStateSession([
            {"status": 200, "payload": {"state": "75"}},
            {"raise_on_get": ConnectionError("blip")},
        ])
        first = await sensor.read(session, now=0.0)
        assert first == 75.0

        # Within max_stale_s the cached value is still trusted.
        second = await sensor.read(session, now=30.0)
        assert second == 75.0

    @pytest.mark.asyncio
    async def test_returns_zero_after_stale_window(self):
        sensor = OptionalHASensor("http://ha:8123", "tok", "sensor.x", max_stale_s=60)
        session = _FakeStateSession([
            {"status": 200, "payload": {"state": "75"}},
            {"raise_on_get": ConnectionError("HA down")},
            {"raise_on_get": ConnectionError("HA still down")},
        ])
        await sensor.read(session, now=0.0)
        await sensor.read(session, now=30.0)

        stale = await sensor.read(session, now=120.0)
        assert stale == 0.0, (
            "After max_stale_s of failed reads the cache must invalidate; "
            "returning a 2-minute-old SOC could push the brain into a "
            "dangerous state when HA actually came back at a different value."
        )

    @pytest.mark.asyncio
    async def test_successful_read_resets_stale_window(self):
        sensor = OptionalHASensor("http://ha:8123", "tok", "sensor.x", max_stale_s=60)
        session = _FakeStateSession([
            {"status": 200, "payload": {"state": "75"}},
            {"raise_on_get": ConnectionError("blip")},
            {"status": 200, "payload": {"state": "60"}},
            {"raise_on_get": ConnectionError("blip")},
        ])
        await sensor.read(session, now=0.0)
        await sensor.read(session, now=30.0)
        # Recovery: new read at t=50, cache resets.
        v = await sensor.read(session, now=50.0)
        assert v == 60.0

        # 30s later the cache is still fresh (only 30s into a new 60s window).
        v = await sensor.read(session, now=80.0)
        assert v == 60.0
