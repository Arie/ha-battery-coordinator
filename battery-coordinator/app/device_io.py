"""Direct device communication — no HA dependency.

Talks to Zendure (local REST) and HomeWizard P1 meter (local HTTPS) directly.
Optional HA connection for solar sensor only.
"""

import asyncio
import logging
import ssl
from dataclasses import dataclass

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
            async with session.get(
                f"{self._url}/properties/report", timeout=self._timeout
            ) as r:
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
        if ac_mode == 2:
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

    async def fetch_sn(self, session: aiohttp.ClientSession,
                       max_attempts: int = 60, delay_s: float = 5.0) -> str:
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
        across reboots and the device can drop into low-power mode."""
        return await self._write(session, {"smartMode": 0, "outputLimit": 0, "inputLimit": 0})

    async def _write(self, session: aiohttp.ClientSession, properties: dict) -> bool:
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

    def _headers(self):
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    async def read(self, session: aiohttp.ClientSession) -> P1Status:
        """Read P1 grid power and battery status."""
        grid_power = 0.0
        pib_power = 0.0
        pib_mode = "standby"
        pib_permissions: list[str] = []
        pib_count = 0

        # Read grid power from /api/measurement
        try:
            async with session.get(
                f"{self._url}/api/measurement",
                headers=self._headers(),
                timeout=self._timeout,
                ssl=self._ssl,
            ) as r:
                data = await r.json()
                grid_power = data.get("power_w", 0)
        except Exception as e:
            log.warning("HW P1 /api/measurement read failed (%s): %s", type(e).__name__, e)

        # Read battery status from /api/batteries
        try:
            async with session.get(
                f"{self._url}/api/batteries",
                headers=self._headers(),
                timeout=self._timeout,
                ssl=self._ssl,
            ) as r:
                data = await r.json()
                pib_power = data.get("power_w", 0)
                pib_mode = data.get("mode", "standby")
                pib_permissions = data.get("permissions", [])
                pib_count = data.get("battery_count", 0)
        except Exception as e:
            log.warning("HW P1 /api/batteries read failed (%s): %s", type(e).__name__, e)

        return P1Status(
            grid_power=grid_power,
            pib_power=pib_power,
            pib_mode=pib_mode,
            pib_permissions=pib_permissions,
            pib_count=pib_count,
        )

    async def set_mode(self, session: aiohttp.ClientSession, mode: str, permissions: list[str] | None = None) -> bool:
        """Set PIB mode and permissions."""
        if mode == "charge":
            mode = "to_full"
        payload: dict = {"mode": mode}
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
                        r.status, mode, permissions,
                    )
                    return False
                return True
        except Exception as e:
            log.warning(
                "HW P1 set_mode failed (%s): %s (mode=%s perms=%s)",
                type(e).__name__, e, mode, permissions,
            )
            return False


class OptionalHASensor:
    """Read a single sensor from HA. Returns 0 if not configured or unavailable."""

    def __init__(self, ha_url: str, ha_token: str, entity_id: str, timeout: float = 3):
        self._url = f"{ha_url}/api/states/{entity_id}" if ha_url and entity_id else ""
        self._token = ha_token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._last_value: float = 0

    async def read(self, session: aiohttp.ClientSession) -> float:
        if not self._url:
            return 0.0
        try:
            headers = {"Authorization": f"Bearer {self._token}"}
            async with session.get(self._url, headers=headers, timeout=self._timeout) as r:
                data = await r.json()
                v = data.get("state")
                if v is not None and v not in ("unknown", "unavailable"):
                    self._last_value = float(v)
                    return self._last_value
        except Exception:
            pass
        return self._last_value  # return last known on failure


class DeviceIO:
    """Unified device I/O layer. Reads all devices, produces a Reading."""

    def __init__(self, config: Config):
        self.zendure = ZendureDevice(config.zendure_ip, config.read_timeout)
        self.p1 = HWP1Meter(config.hw_p1_ip, config.hw_p1_token, config.read_timeout)
        self.solar = OptionalHASensor(config.ha_url, config.ha_token, config.solar_entity, config.read_timeout)
        # Per-PIB SOC + power via HA. The brain handles arbitrary N PIBs;
        # one OptionalHASensor per configured entity, in order.
        self.pib_socs = [
            OptionalHASensor(config.ha_url, config.ha_token, e, config.read_timeout)
            for e in config.pib_soc_entities
        ]
        self.pib_powers = [
            OptionalHASensor(config.ha_url, config.ha_token, e, config.read_timeout)
            for e in config.pib_power_entities
        ]
        if self.pib_powers and self.pib_socs and len(self.pib_powers) != len(self.pib_socs):
            log.warning(
                "PIB power entities (%d) and SOC entities (%d) have different counts. "
                "Brain pairs them by index, so mismatched lists may misalign PIBs.",
                len(self.pib_powers), len(self.pib_socs),
            )

    async def read_all(self, session: aiohttp.ClientSession) -> tuple[Reading, ZendureStatus, P1Status]:
        """Read all devices and return a Reading for the brain.

        All reads run concurrently. With 4 PIBs × 2 entities + Zen + P1
        + solar that's up to ~10 HTTPS requests per tick — sequential
        would easily blow past the 1Hz tick budget on a slow HA host.
        """
        results = await asyncio.gather(
            self.zendure.read(session),
            self.p1.read(session),
            self.solar.read(session),
            *(s.read(session) for s in self.pib_socs),
            *(p.read(session) for p in self.pib_powers),
        )
        zen = results[0]
        p1 = results[1]
        solar = results[2]
        pib_soc_end = 3 + len(self.pib_socs)
        pib_socs = list(results[3:pib_soc_end])
        pib_powers = list(results[pib_soc_end:])

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
