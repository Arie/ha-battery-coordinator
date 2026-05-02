"""Direct device communication — no HA dependency.

Talks to Zendure (local REST) and HomeWizard P1 meter (local HTTPS) directly.
Optional HA connection for solar sensor only.
"""

import asyncio
import logging
import ssl
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
from config import Config
from coordinator_logic import Reading

log = logging.getLogger(__name__)


@dataclass
class ZendureStatus:
    """Parsed Zendure device status."""

    power: float  # actual output power (W), positive=charge, negative=discharge
    soc: float  # battery SOC (%)
    ac_mode: int  # 1=charge, 2=discharge
    input_limit: int  # current charge target (W)
    output_limit: int  # current discharge target (W)
    inverter_temp: float  # °C
    pack_count: int
    sn: str


@dataclass
class P1Status:
    """Parsed P1 meter + battery status."""

    grid_power: float  # P1 active power (W), positive=import, negative=export
    pib_power: float  # combined PIB power (W), positive=charge, negative=discharge
    pib_mode: str  # "zero", "standby", "to_full"
    pib_permissions: list[str]
    pib_count: int


_ZERO_STATUS = ZendureStatus(0, 0, 0, 0, 0, 0, 0, "")


class ZendureDevice:
    """Direct local REST API to Zendure 2400 AC."""

    def __init__(self, ip: str, timeout: float = 3):
        self._url = f"http://{ip}"
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._sn: str = ""
        # Cache the last successful read. The brain has no concept of stale
        # data — returning zeros on a transient HTTP error makes it see
        # "Zen drained, 0W output" and trigger spurious mode flips. Holding
        # the previous value across blips keeps the brain on a coherent
        # picture; the heartbeat / explicit timeouts handle real outages.
        self._last_status: ZendureStatus | None = None

    async def read(self, session: aiohttp.ClientSession) -> ZendureStatus:
        """Read current device status. On failure return last-known."""
        try:
            async with session.get(f"{self._url}/properties/report", timeout=self._timeout) as r:
                if r.status != 200:
                    log.warning("Zendure read: HTTP %s from %s", r.status, self._url)
                    return self._last_status or _ZERO_STATUS
                data = await r.json(content_type=None)
        except Exception as e:
            log.warning("Zendure read failed (%s): %s", type(e).__name__, e)
            return self._last_status or _ZERO_STATUS

        props = data.get("properties", {})
        self._sn = data.get("sn", self._sn)

        # Power: outputHomePower for discharge, gridInputPower for charge
        ac_mode = props.get("acMode", 0)
        if ac_mode == 2:  # noqa: SIM108  (a ternary loses the branch-side direction comments)
            power = -props.get("outputHomePower", 0)  # negative = discharge
        else:
            power = props.get("gridInputPower", 0)  # positive = charge

        # Temperature conversion: (raw - 2731) / 10.0
        raw_temp = props.get("hyperTmp", 2731)
        temp = (raw_temp - 2731) / 10.0

        status = ZendureStatus(
            power=power,
            soc=props.get("electricLevel", 0),
            ac_mode=ac_mode,
            input_limit=props.get("inputLimit", 0),
            output_limit=props.get("outputLimit", 0),
            inverter_temp=temp,
            pack_count=props.get("packNum", 0),
            sn=self._sn,
        )
        self._last_status = status
        return status

    async def fetch_sn(self, session: aiohttp.ClientSession, max_attempts: int = 60, delay_s: float = 5.0) -> str:
        """Poll until the device reports a non-empty SN.

        After a host reboot the device may take 1–2 minutes to populate
        the SN field. Without this retry the coordinator gets an empty
        SN once at startup and locks itself into observe-only mode for
        the rest of the day (every write requires the SN). Idempotent;
        safe to call repeatedly.
        """
        for _ in range(max_attempts):
            status = await self.read(session)
            if status.sn:
                return status.sn
            await asyncio.sleep(delay_s)
        return ""

    async def charge(self, session: aiohttp.ClientSession, watts: int, mode_switch: bool = False) -> bool:
        """Set charge power. mode_switch=True flips relay to charge mode.

        Sends `smartMode: 1` (RAM-only writes) so the rapid NOM updates
        the brain emits don't accumulate flash wear on the inverter.
        Standby will switch back to Flash so the device can deep-sleep.
        """
        props = {"inputLimit": watts, "smartMode": 1}
        if mode_switch:
            props["acMode"] = 1
        return await self._write(session, props)

    async def discharge(self, session: aiohttp.ClientSession, watts: int, mode_switch: bool = False) -> bool:
        """Set discharge power. mode_switch=True flips relay to discharge mode."""
        props = {"outputLimit": watts, "smartMode": 1}
        if mode_switch:
            props["acMode"] = 2
        return await self._write(session, props)

    async def standby(self, session: aiohttp.ClientSession) -> bool:
        """Deep standby — inverter off, persist to flash so it stays off
        across reboots and the device can drop into low-power mode.

        Flash writes wear the device's storage. Use this only for the
        FIRST entry into target=0; for heartbeat re-assertions of an
        already-zero target, call hold_zero() instead.
        """
        return await self._write(session, {"smartMode": 0, "outputLimit": 0, "inputLimit": 0})

    async def hold_zero(self, session: aiohttp.ClientSession) -> bool:
        """RAM-only zero — same effective state as standby (no charge,
        no discharge) but doesn't persist to flash. Used for heartbeat
        re-assertion of target=0 so we recover from drift (firmware
        reboot, external app toggle) without a flash write per beat."""
        return await self._write(session, {"smartMode": 1, "outputLimit": 0, "inputLimit": 0})

    async def _write(self, session: aiohttp.ClientSession, properties: dict[str, int]) -> bool:
        try:
            payload = {"sn": self._sn, "properties": properties}
            write_timeout = aiohttp.ClientTimeout(total=5)
            async with session.post(
                f"{self._url}/properties/write",
                json=payload,
                timeout=write_timeout,
                headers={"Content-Type": "application/json"},
            ) as r:
                return r.status == 200
        except Exception:
            return False


class HWP1Meter:
    """Direct local HTTPS API to HomeWizard P1 meter."""

    def __init__(self, ip: str, token: str, timeout: float = 3):
        self._url = f"https://{ip}"
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._ssl = ssl.create_default_context()
        self._ssl.check_hostname = False
        self._ssl.verify_mode = ssl.CERT_NONE
        # Last-known values per endpoint. Mirrors ZendureDevice's caching:
        # the brain treats every Reading as ground truth, so a transient
        # blip returning 0/standby/[] would propagate as a false "balanced
        # grid + PIBs idle" signal and reset transition holdoff timers.
        # None until the first successful read of that endpoint.
        self._last_grid_power: float | None = None
        self._last_pib_power: float | None = None
        self._last_pib_mode: str | None = None
        self._last_pib_permissions: list[str] | None = None
        self._last_pib_count: int | None = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    async def _get_json(self, session: aiohttp.ClientSession, path: str) -> dict[str, Any] | None:
        """GET path and return parsed JSON, or None on any failure.

        Centralises the status-check + exception-catch the brain depends on:
        a 401 (bad token) or 500 must NOT silently fall through as
        data.get("...", 0), which would mask auth failures as "balanced
        grid" or "PIBs at zero".
        """
        try:
            async with session.get(
                f"{self._url}{path}",
                headers=self._headers(),
                timeout=self._timeout,
                ssl=self._ssl,
            ) as r:
                if r.status != 200:
                    log.warning("HW P1 %s: HTTP %s", path, r.status)
                    return None
                data: dict[str, Any] = await r.json()
                return data
        except Exception as e:
            log.warning("HW P1 %s read failed (%s): %s", path, type(e).__name__, e)
            return None

    async def read(self, session: aiohttp.ClientSession) -> P1Status:
        """Read P1 grid power and battery status.

        Each endpoint caches independently — a /api/batteries blip while
        /api/measurement succeeds doesn't discard the fresh grid power.
        """
        measurement = await self._get_json(session, "/api/measurement")
        if measurement is not None:
            self._last_grid_power = measurement.get("power_w", 0)

        batteries = await self._get_json(session, "/api/batteries")
        if batteries is not None:
            self._last_pib_power = batteries.get("power_w", 0)
            self._last_pib_mode = batteries.get("mode", "standby")
            self._last_pib_permissions = batteries.get("permissions", [])
            self._last_pib_count = batteries.get("battery_count", 0)

        return P1Status(
            grid_power=self._last_grid_power if self._last_grid_power is not None else 0.0,
            pib_power=self._last_pib_power if self._last_pib_power is not None else 0.0,
            pib_mode=self._last_pib_mode if self._last_pib_mode is not None else "standby",
            pib_permissions=list(self._last_pib_permissions) if self._last_pib_permissions is not None else [],
            pib_count=self._last_pib_count if self._last_pib_count is not None else 0,
        )

    async def set_mode(self, session: aiohttp.ClientSession, mode: str, permissions: list[str] | None = None) -> bool:
        """Set PIB mode and permissions."""
        payload: dict[str, Any] = {"mode": mode}
        if permissions is not None:
            payload["permissions"] = permissions
        try:
            async with session.put(
                f"{self._url}/api/batteries",
                headers=self._headers(),
                json=payload,
                timeout=self._timeout,
                ssl=self._ssl,
            ) as r:
                if r.status != 200:
                    log.warning(
                        "HW P1 set_mode failed: HTTP %s (mode=%s perms=%s)",
                        r.status,
                        mode,
                        permissions,
                    )
                    return False
                return True
        except Exception as e:
            log.warning(
                "HW P1 set_mode failed (%s): %s (mode=%s perms=%s)",
                type(e).__name__,
                e,
                mode,
                permissions,
            )
            return False


class OptionalHASensor:
    """Read a single sensor from HA. Returns 0 if not configured or unavailable.

    Caches the last successful value across transient failures so a 1s
    network blip doesn't propagate as a 0 reading into the brain. The
    cache invalidates after `max_stale_s` of continuous failure — beyond
    that, a stale SOC is more dangerous than a default 0 (which the
    brain treats as "missing/empty" and reaches a safe state).
    """

    def __init__(self, ha_url: str, ha_token: str, entity_id: str, timeout: float = 3, max_stale_s: float = 60):
        self._url = f"{ha_url}/api/states/{entity_id}" if ha_url and entity_id else ""
        self._token = ha_token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._last_value: float = 0
        self._last_value_t: float | None = None
        self._max_stale_s = max_stale_s

    async def read(self, session: aiohttp.ClientSession, now: float | None = None) -> float:
        if not self._url:
            return 0.0
        if now is None:
            now = time.monotonic()
        try:
            headers = {"Authorization": f"Bearer {self._token}"}
            async with session.get(self._url, headers=headers, timeout=self._timeout) as r:
                if r.status != 200:
                    # An error page that happens to parse as JSON with a
                    # 'state' key (proxy template, supervisor error envelope)
                    # would silently overwrite the cached value with garbage
                    # without this guard. Fall through to the cache fallback
                    # below — it has its own staleness check.
                    log.warning("HA sensor %s: HTTP %s", self._url, r.status)
                else:
                    data = await r.json()
                    v = data.get("state")
                    if v is not None and v not in ("unknown", "unavailable"):
                        self._last_value = float(v)
                        self._last_value_t = now
                        return self._last_value
        except Exception:
            pass
        # Read failed. Use last-known if it's still fresh; beyond the
        # stale window, return 0 so the brain can reach a safe state
        # rather than acting on a multi-minute-old SOC.
        if self._last_value_t is not None and (now - self._last_value_t) <= self._max_stale_s:
            return self._last_value
        if self._last_value_t is not None:
            log.warning(
                "HA sensor %s stale > %ss; returning 0",
                self._url,
                self._max_stale_s,
            )
            self._last_value_t = None  # log once until next success
        return 0.0


class DeviceIO:
    """Unified device I/O layer. Reads all devices, produces a Reading."""

    def __init__(self, config: Config):
        self.zendure = ZendureDevice(config.zendure_ip, config.read_timeout)
        self.p1 = HWP1Meter(config.hw_p1_ip, config.hw_p1_token, config.read_timeout)
        self.solar = OptionalHASensor(config.ha_url, config.ha_token, config.solar_entity, config.read_timeout)
        # Per-PIB SOC + power via HA. The brain handles arbitrary N PIBs;
        # one OptionalHASensor per configured entity, in order.
        self.pib_socs = [
            OptionalHASensor(config.ha_url, config.ha_token, e, config.read_timeout) for e in config.pib_soc_entities
        ]
        self.pib_powers = [
            OptionalHASensor(config.ha_url, config.ha_token, e, config.read_timeout) for e in config.pib_power_entities
        ]
        if self.pib_powers and self.pib_socs and len(self.pib_powers) != len(self.pib_socs):
            # Config.validate() catches this and refuses to start; if we're
            # here something bypassed validation. Log an error so it's at
            # least visible — silently padding makes the brain see ghost
            # 0%-SOC PIBs and over-correct.
            log.error(
                "PIB power entities (%d) and SOC entities (%d) MUST be the "
                "same length — brain pairs them by index, so mismatched "
                "lists misalign PIBs to wrong SOCs.",
                len(self.pib_powers),
                len(self.pib_socs),
            )

    async def read_all(self, session: aiohttp.ClientSession) -> tuple[Reading, ZendureStatus, P1Status]:
        """Read all devices and return a Reading for the brain.

        All reads run concurrently. With 4 PIBs × 2 entities + Zen + P1
        + solar that's up to ~10 HTTPS requests per tick — sequential
        would easily blow past the 1Hz tick budget on a slow HA host.

        TaskGroup keeps the heterogeneous Zen/P1/solar reads and the
        homogeneous per-PIB list reads in one parallel batch while still
        type-checking — a flat asyncio.gather with star-args collapses
        every result to `object`.
        """
        async with asyncio.TaskGroup() as tg:
            zen_t = tg.create_task(self.zendure.read(session))
            p1_t = tg.create_task(self.p1.read(session))
            solar_t = tg.create_task(self.solar.read(session))
            pib_soc_ts = [tg.create_task(s.read(session)) for s in self.pib_socs]
            pib_power_ts = [tg.create_task(p.read(session)) for p in self.pib_powers]
        zen = zen_t.result()
        p1 = p1_t.result()
        solar = solar_t.result()
        pib_socs: list[float] = [t.result() for t in pib_soc_ts]
        pib_powers: list[float] = [t.result() for t in pib_power_ts]

        # Fallback when no per-PIB power entities are configured: split the
        # combined value from /api/batteries evenly across PIBs. Rough but
        # better than zero.
        if not pib_powers and p1.pib_count > 0:
            even = p1.pib_power / p1.pib_count
            pib_powers = [even] * p1.pib_count

        # If counts don't match, pad shorter list with 0 so the Reading
        # constructor's parallel-list invariant holds.
        n = max(len(pib_powers), len(pib_socs))
        pib_powers = pib_powers + [0.0] * (n - len(pib_powers))
        pib_socs = pib_socs + [0.0] * (n - len(pib_socs))

        reading = Reading(
            p1=p1.grid_power,
            pibs=pib_powers,
            pib_socs=pib_socs,
            zen_power=zen.power,
            zen_soc=zen.soc,
            solar=solar,
        )

        return reading, zen, p1
