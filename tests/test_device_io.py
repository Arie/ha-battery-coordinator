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
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "battery-coordinator" / "app"))

from coordinator_logic import Reading
from device_io import HANumberWriter, HWP1Meter, OptionalHABinarySensor, OptionalHASensor, ZendureDevice


class _FakeResponse:
    def __init__(
        self, *, status: int = 200, payload: dict[str, Any] | None = None, raise_on_get: Exception | None = None
    ):
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


_GOOD_REPORT: dict[str, Any] = {
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
        session = _FakeSession(
            [
                {"status": 200, "payload": _GOOD_REPORT},
                {"status": 500, "payload": {}},
            ]
        )

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
        session = _FakeSession(
            [
                {"status": 200, "payload": _GOOD_REPORT},
                {"raise_on_get": ConnectionError("network blip")},
            ]
        )

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
        session = _FakeSession(
            [
                {"status": 200, "payload": _GOOD_REPORT},
                {"raise_on_get": TimeoutError()},
                {"raise_on_get": TimeoutError()},
            ]
        )
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
        session = _FakeSession(
            [
                {"status": 200, "payload": empty},
                {"status": 200, "payload": empty},
                {"status": 200, "payload": _GOOD_REPORT},
            ]
        )
        sn = await zen.fetch_sn(session, max_attempts=10, delay_s=0)
        assert sn == "HEC-TEST"

    @pytest.mark.asyncio
    async def test_retries_past_exceptions(self):
        zen = ZendureDevice("1.2.3.4")
        session = _FakeSession(
            [
                {"raise_on_get": ConnectionError("dead")},
                {"raise_on_get": TimeoutError()},
                {"status": 200, "payload": _GOOD_REPORT},
            ]
        )
        sn = await zen.fetch_sn(session, max_attempts=10, delay_s=0)
        assert sn == "HEC-TEST"

    @pytest.mark.asyncio
    async def test_returns_empty_after_max_attempts(self):
        zen = ZendureDevice("1.2.3.4")
        session = _FakeSession(
            [
                {"raise_on_get": ConnectionError("nope")},
                {"raise_on_get": ConnectionError("nope")},
            ]
        )
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

    def __init__(self, status: int = 200, raise_on_post: Exception | None = None):
        self.writes: list[dict[str, Any]] = []
        self._status = status
        self._raise = raise_on_post

    def post(self, url, json=None, headers=None, timeout=None):
        self.writes.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(status=self._status, payload={}, raise_on_get=self._raise)


class TestZendureStandbyFlashWear:
    """standby() persists smartMode=0 to flash; hold_zero() is the RAM-only
    zero used in the active states. Idle heartbeats avoid the flash write
    through ensure_standby() (see TestZendureEnsureStandby)."""

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

    def __init__(self, scripts: dict[str, list[dict[str, Any]]]):
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
        session = _FakeMultiURLSession(
            {
                "/api/measurement": [
                    {"status": 200, "payload": _GOOD_MEASUREMENT},
                    {"raise_on_get": ConnectionError("blip")},
                ],
                "/api/batteries": [
                    {"status": 200, "payload": _GOOD_BATTERIES},
                    {"status": 200, "payload": _GOOD_BATTERIES},
                ],
            }
        )
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
        session = _FakeMultiURLSession(
            {
                "/api/measurement": [
                    {"status": 200, "payload": _GOOD_MEASUREMENT},
                    {"status": 200, "payload": _GOOD_MEASUREMENT},
                ],
                "/api/batteries": [
                    {"status": 200, "payload": _GOOD_BATTERIES},
                    {"raise_on_get": TimeoutError()},
                ],
            }
        )
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
        session = _FakeMultiURLSession(
            {
                "/api/measurement": [
                    {"status": 200, "payload": _GOOD_MEASUREMENT},
                    {"status": 401, "payload": {"error": "unauthorized"}},
                ],
                "/api/batteries": [
                    {"status": 200, "payload": _GOOD_BATTERIES},
                    {"status": 200, "payload": _GOOD_BATTERIES},
                ],
            }
        )
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
        session = _FakeMultiURLSession(
            {
                "/api/measurement": [{"raise_on_get": ConnectionError("dead on arrival")}],
                "/api/batteries": [{"raise_on_get": ConnectionError("dead on arrival")}],
            }
        )
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
        session = _FakeStateSession(
            [
                {"status": 200, "payload": {"state": "75"}},
                {"raise_on_get": ConnectionError("blip")},
            ]
        )
        first = await sensor.read(session, now=0.0)
        assert first == 75.0

        # Within max_stale_s the cached value is still trusted.
        second = await sensor.read(session, now=30.0)
        assert second == 75.0

    @pytest.mark.asyncio
    async def test_returns_zero_after_stale_window(self):
        sensor = OptionalHASensor("http://ha:8123", "tok", "sensor.x", max_stale_s=60)
        session = _FakeStateSession(
            [
                {"status": 200, "payload": {"state": "75"}},
                {"raise_on_get": ConnectionError("HA down")},
                {"raise_on_get": ConnectionError("HA still down")},
            ]
        )
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
        session = _FakeStateSession(
            [
                {"status": 200, "payload": {"state": "75"}},
                {"raise_on_get": ConnectionError("blip")},
                {"status": 200, "payload": {"state": "60"}},
                {"raise_on_get": ConnectionError("blip")},
            ]
        )
        await sensor.read(session, now=0.0)
        await sensor.read(session, now=30.0)
        # Recovery: new read at t=50, cache resets.
        v = await sensor.read(session, now=50.0)
        assert v == 60.0

        # 30s later the cache is still fresh (only 30s into a new 60s window).
        v = await sensor.read(session, now=80.0)
        assert v == 60.0


class TestOptionalHASensorHTTPStatus:
    """A non-200 response from HA must NOT update the cache, even if its
    body happens to parse as JSON with a 'state' key. Without an explicit
    status check, an error page that happens to contain `{"state": ...}`
    (proxy template, supervisor error envelope) silently overwrites the
    last-known value with garbage.
    """

    @pytest.mark.asyncio
    async def test_non_200_does_not_poison_cache(self):
        sensor = OptionalHASensor("http://ha:8123", "tok", "sensor.x", max_stale_s=60)
        session = _FakeStateSession(
            [
                {"status": 200, "payload": {"state": "75"}},
                # 500 with a JSON-shaped error body that has a 'state' key.
                {"status": 500, "payload": {"state": "999"}},
            ]
        )
        first = await sensor.read(session, now=0.0)
        assert first == 75.0

        second = await sensor.read(session, now=30.0)
        assert second == 75.0, (
            f"HTTP 500 with body={{'state': '999'}} poisoned the cache "
            f"(got {second}) — without a status check, an error page that "
            f"happens to be valid JSON silently overrides the real value."
        )

    @pytest.mark.asyncio
    async def test_401_falls_through_to_cache(self):
        sensor = OptionalHASensor("http://ha:8123", "tok", "sensor.x", max_stale_s=60)
        session = _FakeStateSession(
            [
                {"status": 200, "payload": {"state": "75"}},
                # 401 Unauthorized (e.g. token expired) — typical body has
                # 'message' but not 'state'. Even so, status check should
                # short-circuit before touching the body.
                {"status": 401, "payload": {"message": "API password missing"}},
            ]
        )
        await sensor.read(session, now=0.0)
        v = await sensor.read(session, now=30.0)
        assert v == 75.0, "401 response should fall through to last-known cache, not leak through as 0.0."

    @pytest.mark.asyncio
    async def test_non_200_first_read_returns_default_zero(self):
        # No prior cache; non-200 → default 0.0, no exception.
        sensor = OptionalHASensor("http://ha:8123", "tok", "sensor.x", max_stale_s=60)
        session = _FakeStateSession([{"status": 502, "payload": {}}])
        v = await sensor.read(session, now=0.0)
        assert v == 0.0


class _FakeStateOnlySession:
    """Serves a queue of HA /api/states responses for one entity."""

    def __init__(self, scripted):
        self._queue = list(scripted)

    def get(self, url, headers=None, timeout=None):
        if not self._queue:
            return _FakeResponse(status=200, payload={})
        return _FakeResponse(**self._queue.pop(0))


class TestOptionalHABinarySensor:
    """Gate entities decide whether the batteries run. Their failure mode
    has to be 'behave exactly as before the feature existed', never
    'start importing from the grid on a reading we could not confirm'."""

    def _sensor(self, scripted, **kw):
        return OptionalHABinarySensor("http://ha", "tok", "input_boolean.gate", **kw), _FakeStateOnlySession(scripted)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ("on", True),
            ("ON", True),
            ("true", True),
            ("1", True),
            ("yes", True),
            ("off", False),
            ("false", False),
            ("0", False),
            ("", False),
        ],
    )
    async def test_state_parsing(self, state, expected):
        sensor, session = self._sensor([{"status": 200, "payload": {"state": state}}])
        assert await sensor.read(session, now=0) is expected

    @pytest.mark.asyncio
    async def test_unconfigured_returns_false(self):
        sensor = OptionalHABinarySensor("http://ha", "tok", "")
        assert sensor.configured is False
        assert await sensor.read(_FakeStateOnlySession([]), now=0) is False

    @pytest.mark.asyncio
    async def test_unavailable_falls_back_to_cache(self):
        """A blip mid-EV-charge must not drop the gate and resume draining."""
        sensor, session = self._sensor(
            [
                {"status": 200, "payload": {"state": "on"}},
                {"status": 200, "payload": {"state": "unavailable"}},
            ]
        )
        assert await sensor.read(session, now=0) is True
        assert await sensor.read(session, now=10) is True

    @pytest.mark.asyncio
    async def test_stale_cache_falls_open_to_false(self):
        """Past the stale window, an indefinitely held gate is a stuck
        controller, not a policy — revert to pre-feature behaviour."""
        sensor, session = self._sensor(
            [
                {"status": 200, "payload": {"state": "on"}},
                {"raise_on_get": ConnectionError("HA down")},
            ],
            max_stale_s=60,
        )
        assert await sensor.read(session, now=0) is True
        assert await sensor.read(session, now=999) is False

    @pytest.mark.asyncio
    async def test_http_error_does_not_latch_true(self):
        """An error envelope that happens to parse must never read as 'on'."""
        sensor, session = self._sensor([{"status": 500, "payload": {"state": "on"}}])
        assert await sensor.read(session, now=0) is False

    @pytest.mark.asyncio
    async def test_never_returns_true_before_a_successful_read(self):
        """The force_charge case: no confirmed 'on' means no grid import."""
        sensor, session = self._sensor([{"raise_on_get": ConnectionError("HA down")}])
        assert await sensor.read(session, now=0) is False


class _CapturingSession:
    """Captures what set_mode/read actually put on the wire."""

    def __init__(self, status: int = 200, payload: dict[str, Any] | None = None):
        self.calls: list[dict[str, Any]] = []
        self._status = status
        self._payload = payload or {}

    def put(self, url, headers=None, json=None, timeout=None, ssl=None):
        self.calls.append({"verb": "PUT", "url": url, "headers": headers or {}, "json": json or {}})
        return _FakeResponse(status=self._status, payload=self._payload)

    def get(self, url, headers=None, timeout=None, ssl=None):
        self.calls.append({"verb": "GET", "url": url, "headers": headers or {}, "json": None})
        return _FakeResponse(status=self._status, payload=self._payload)

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1]


class TestPIBApiV2Migration:
    """HomeWizard deprecated `standby` and `to_full` in the v2 battery API.

    Permissions are the documented replacement and "will be the only option
    in the future". The brain keeps its own vocabulary — "standby" is a
    semantic state there, not a wire format — so the translation belongs at
    the I/O boundary, in one place.
    """

    def _meter(self):
        return HWP1Meter("1.2.3.4", "tok")

    @pytest.mark.asyncio
    async def test_standby_becomes_zero_with_empty_permissions(self):
        """Legacy standby -> zero mode forbidden from both directions."""
        m, s = self._meter(), _CapturingSession()
        assert await m.set_mode(s, "standby") is True
        assert s.last["json"]["mode"] == "zero"
        assert s.last["json"]["permissions"] == []

    @pytest.mark.asyncio
    async def test_standby_wins_over_a_contradictory_permission(self):
        """Standby means "don't participate"; a permission cannot sneak in.

        The device auto-switches out of standby the moment a permission is
        added, so sending both would silently produce a live battery.
        """
        m, s = self._meter(), _CapturingSession()
        await m.set_mode(s, "standby", ["discharge_allowed"])
        assert s.last["json"]["permissions"] == []

    @pytest.mark.asyncio
    async def test_to_full_becomes_the_charge_to_full_flag(self):
        m, s = self._meter(), _CapturingSession()
        await m.set_mode(s, "to_full", ["charge_allowed"])
        assert s.last["json"]["mode"] == "zero"
        assert s.last["json"]["charge_to_full"] is True
        assert s.last["json"]["permissions"] == ["charge_allowed"]

    @pytest.mark.asyncio
    async def test_zero_passes_through_unchanged(self):
        m, s = self._meter(), _CapturingSession()
        await m.set_mode(s, "zero", ["discharge_allowed"])
        assert s.last["json"]["mode"] == "zero"
        assert s.last["json"]["permissions"] == ["discharge_allowed"]

    @pytest.mark.asyncio
    async def test_charge_to_full_is_always_explicit(self):
        """charge_to_full is sticky on the device.

        It persists until something sets it false, so omitting it on the way
        out of GRID_CHARGE would leave the PIBs charging to 100% forever.
        Every call must state it.
        """
        m = self._meter()
        for mode, perms in (("zero", ["charge_allowed"]), ("standby", None), ("zero", None)):
            s = _CapturingSession()
            await m.set_mode(s, mode, perms)
            assert "charge_to_full" in s.last["json"], f"{mode} omitted charge_to_full"
            assert s.last["json"]["charge_to_full"] is False

    @pytest.mark.asyncio
    async def test_never_emits_a_deprecated_mode(self):
        m = self._meter()
        for mode in ("standby", "to_full", "zero"):
            s = _CapturingSession()
            await m.set_mode(s, mode, ["charge_allowed"] if mode != "standby" else None)
            assert s.last["json"]["mode"] not in ("standby", "to_full")

    @pytest.mark.asyncio
    async def test_api_version_header_on_write(self):
        m, s = self._meter(), _CapturingSession()
        await m.set_mode(s, "zero", ["charge_allowed"])
        assert s.last["headers"].get("X-Api-Version") == "2"

    @pytest.mark.asyncio
    async def test_api_version_header_on_read(self):
        m, s = self._meter(), _CapturingSession(payload=_GOOD_BATTERIES)
        await m.read(s)
        assert all(c["headers"].get("X-Api-Version") == "2" for c in s.calls), s.calls


class TestOptionalHANumber:
    """A numeric HA entity where "no value" must stay distinct from 0.

    The setpoint reader: 0 is a real instruction (hold), so absence has to
    be None, not a zero default like OptionalHASensor returns.
    """

    def _num(self, entity="sensor.sp", **kw):
        from device_io import OptionalHANumber

        return OptionalHANumber("http://ha:8123", "tok", entity, **kw)

    @pytest.mark.asyncio
    async def test_unconfigured_reads_none(self):
        num = self._num(entity="")
        assert await num.read(_FakeStateSession([]), now=0.0) is None

    @pytest.mark.asyncio
    async def test_numeric_state_reads_as_float(self):
        num = self._num()
        session = _FakeStateSession([{"status": 200, "payload": {"state": "-1500.0"}}])
        assert await num.read(session, now=0.0) == -1500.0

    @pytest.mark.asyncio
    async def test_zero_is_a_value_not_absence(self):
        num = self._num()
        session = _FakeStateSession([{"status": 200, "payload": {"state": "0"}}])
        assert await num.read(session, now=0.0) == 0.0

    @pytest.mark.asyncio
    async def test_unavailable_and_junk_read_none(self):
        num = self._num()
        session = _FakeStateSession(
            [
                {"status": 200, "payload": {"state": "unavailable"}},
                {"status": 200, "payload": {"state": "unknown"}},
                {"status": 200, "payload": {"state": "off"}},
            ]
        )
        assert await num.read(session, now=0.0) is None
        assert await num.read(session, now=1.0) is None
        assert await num.read(session, now=2.0) is None

    @pytest.mark.asyncio
    async def test_holds_last_value_briefly_then_falls_to_none(self):
        num = self._num(max_stale_s=60)
        session = _FakeStateSession(
            [
                {"status": 200, "payload": {"state": "800"}},
                {"raise_on_get": ConnectionError("blip")},
                {"raise_on_get": ConnectionError("HA down")},
            ]
        )
        assert await num.read(session, now=0.0) == 800.0
        assert await num.read(session, now=30.0) == 800.0
        assert await num.read(session, now=120.0) is None

    @pytest.mark.asyncio
    async def test_non_finite_states_read_none(self):
        num = self._num()
        session = _FakeStateSession(
            [
                {"status": 200, "payload": {"state": "nan"}},
                {"status": 200, "payload": {"state": "inf"}},
                {"status": 200, "payload": {"state": "-inf"}},
            ]
        )
        for now in (0.0, 1.0, 2.0):
            assert await num.read(session, now=now) is None


class _ReadWriteSession:
    """Serves one scripted Zendure report on get() and records post()s."""

    def __init__(self, report):
        self._report = report
        self.writes: list[dict[str, Any]] = []

    def get(self, url, timeout=None):
        return _FakeResponse(status=200, payload=self._report)

    def post(self, url, json=None, headers=None, timeout=None):
        self.writes.append({"url": url, "json": json})
        return _FakeResponse(status=200, payload={})


def _report(**props):
    return {"sn": "HEC-TEST", "properties": {**_GOOD_REPORT["properties"], **props}}


class TestZendureEnsureStandby:
    """The idle heartbeat re-asserts standby every 5 min. Writing smartMode=0
    each time is a flash write per beat (~100k a year); the device already
    reports whether it is in flash standby, so only write when it is not."""

    @pytest.mark.asyncio
    async def test_read_carries_smart_mode(self):
        zen = ZendureDevice("1.2.3.4")
        status = await zen.read(_ReadWriteSession(_report(smartMode=0)))
        assert status.smart_mode == 0

    @pytest.mark.asyncio
    async def test_smart_mode_unknown_when_not_reported(self):
        zen = ZendureDevice("1.2.3.4")
        status = await zen.read(_ReadWriteSession(_GOOD_REPORT))
        assert status.smart_mode == -1

    @pytest.mark.asyncio
    async def test_already_in_flash_standby_skips_the_write(self):
        zen = ZendureDevice("1.2.3.4")
        session = _ReadWriteSession(_report(smartMode=0, inputLimit=0, outputLimit=0))
        await zen.read(session)
        ok, written = await zen.ensure_standby(session)
        assert ok and not written
        assert session.writes == []

    @pytest.mark.parametrize(
        "props",
        [
            {"smartMode": 1, "inputLimit": 0, "outputLimit": 0},  # RAM zero
            {"smartMode": 0, "inputLimit": 800, "outputLimit": 0},  # drifted
            {"smartMode": 0, "inputLimit": 0, "outputLimit": 300},  # drifted
            {"inputLimit": 0, "outputLimit": 0},  # smartMode not reported
        ],
    )
    @pytest.mark.asyncio
    async def test_anything_else_writes_flash_standby(self, props):
        zen = ZendureDevice("1.2.3.4")
        session = _ReadWriteSession(_report(**props))
        await zen.read(session)
        ok, written = await zen.ensure_standby(session)
        assert ok and written
        assert session.writes[0]["json"]["properties"]["smartMode"] == 0

    @pytest.mark.asyncio
    async def test_no_read_yet_writes_flash_standby(self):
        zen = ZendureDevice("1.2.3.4")
        zen._sn = "TEST"
        session = _ReadWriteSession(_GOOD_REPORT)
        ok, written = await zen.ensure_standby(session)
        assert ok and written


class TestHANumberWriter:
    @pytest.mark.asyncio
    async def test_number_writer_posts_set_value(self):
        s = _RecordingSession()
        w = HANumberWriter("http://ha", "tok", "number.lim")
        assert w.configured
        assert await w.set_value(s, 42) is True
        assert s.writes[0]["url"] == "http://ha/api/services/number/set_value"
        assert s.writes[0]["json"] == {"entity_id": "number.lim", "value": 42}
        assert s.writes[0]["headers"]["Authorization"] == "Bearer tok"

    @pytest.mark.asyncio
    async def test_number_writer_non_200_is_failure(self):
        s = _RecordingSession(status=500)
        w = HANumberWriter("http://ha", "tok", "number.lim")
        assert await w.set_value(s, 42) is False

    @pytest.mark.asyncio
    async def test_number_writer_exception_is_failure(self):
        s = _RecordingSession(raise_on_post=RuntimeError("boom"))
        w = HANumberWriter("http://ha", "tok", "number.lim")
        assert await w.set_value(s, 42) is False

    @pytest.mark.asyncio
    async def test_number_writer_unconfigured_is_noop(self):
        s = _RecordingSession()
        w = HANumberWriter("http://ha", "tok", "")
        assert w.configured is False
        assert await w.set_value(s, 42) is False
        assert s.writes == []


def test_reading_carries_curtail_flags():
    r = Reading(p1=0, pibs=[], pib_socs=[], zen_power=0, zen_soc=50, curtail_export=True, import_pays=True)
    assert r.curtail_export and r.import_pays
    assert "curtail_export=True" in repr(r)
    d = Reading(p1=0, pibs=[], pib_socs=[], zen_power=0, zen_soc=50)
    assert not d.curtail_export and not d.import_pays
