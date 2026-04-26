#!/usr/bin/env python3
"""Battery Coordinator CLI. Reads live data, shows decisions, optionally controls Zendure.

Usage:
  uv run python coordinator_cli.py          # dry run (observe only)
  uv run python coordinator_cli.py --live   # actually control Zendure
"""

import asyncio
import argparse
import sys
import os
import time
from datetime import datetime

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "battery-coordinator", "app"))

import aiohttp
from coordinator_logic import Reading
from brains.permission_fsm import PermissionFSM as PermissionBrain

HA_URL = os.getenv("HA_URL", "")
HA_TOKEN = os.getenv("HA_TOKEN", "")


HA_READ_TIMEOUT = aiohttp.ClientTimeout(total=3)
HA_WRITE_TIMEOUT = aiohttp.ClientTimeout(total=5)

# HomeWizard P1 meter local API (v2, HTTPS)
HW_P1_IP = os.getenv("HW_P1_IP", "")
HW_P1_TOKEN = os.getenv("HW_P1_TOKEN", "")

# HA sensor entity IDs
ENTITY_P1 = "sensor.homewizard_p1_vermogen"
ENTITY_PIB1_POWER = "sensor.plug_in_battery_power"
ENTITY_PIB2_POWER = "sensor.plug_in_battery_power_2"
ENTITY_PIB1_SOC = "sensor.plug_in_battery_state_of_charge"
ENTITY_PIB2_SOC = "sensor.plug_in_battery_state_of_charge_2"
ENTITY_SOLAR = "sensor.solaredge_se6k_ac_power"
ENTITY_ZEN_POWER = "sensor.zendure_2400_ac_vermogen_aansturing"
ENTITY_ZEN_SOC = "sensor.zendure_2400_ac_laadpercentage"
ENTITY_ZEN_RELAY = "sensor.zendure_2400_ac_relais_schakelingen_totaal_vandaag"
ENTITY_ZEN_SN = "sensor.zendure_2400_ac_serienummer"


_last_values: dict[str, float] = {}  # cache last known good values


async def fetch_zen_sn(session, max_attempts: int = 60, delay_s: float = 5.0,
                      verbose: bool = False) -> str:
    """Poll HA for the Zendure SN until it returns a real value.

    After a host reboot the Zendure-HA integration may need ~1-2 minutes
    to populate the SN sensor. Without a retry the coordinator gets a
    blank SN once and locks itself into observe-only mode for the rest
    of the day.
    """
    url = f"{HA_URL}/api/states/{ENTITY_ZEN_SN}"
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}
    invalid = {"unknown", "unavailable", ""}
    for attempt in range(max_attempts):
        try:
            async with session.get(url, headers=headers) as r:
                data = await r.json()
                state = (data.get("state") or "").strip()
                if state and state not in invalid:
                    return state
        except Exception:
            pass
        if verbose:
            print(f"  Waiting for Zendure SN (attempt {attempt + 1})...")
        await asyncio.sleep(delay_s)
    return ""


async def _ha_get(session, entity_id):
    """Get state dict for an entity from HA."""
    try:
        url = f"{HA_URL}/api/states/{entity_id}"
        headers = {"Authorization": f"Bearer {HA_TOKEN}"}
        async with session.get(url, headers=headers, timeout=HA_READ_TIMEOUT) as r:
            return await r.json()
    except Exception:
        return {}


async def ha_float(session, entity_id):
    data = await _ha_get(session, entity_id)
    v = data.get("state")
    if v is not None and v not in ("unknown", "unavailable"):
        try:
            val = float(v)
            _last_values[entity_id] = val
            return val
        except (ValueError, TypeError):
            pass
    # API failed or returned junk — use last known good value
    return _last_values.get(entity_id, 0.0)


async def hw_set_pib_mode(session, mode, permissions=None):
    """Set PIB mode via HomeWizard P1 meter local API. mode: 'standby', 'zero', or 'charge' (to_full)."""
    if mode == "charge":
        mode = "to_full"
    try:
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        url = f"https://{HW_P1_IP}/api/batteries"
        headers = {"Authorization": f"Bearer {HW_P1_TOKEN}", "Content-Type": "application/json"}
        payload = {"mode": mode}
        if permissions is not None:
            payload["permissions"] = permissions
        async with session.put(url, headers=headers, json=payload, timeout=HA_WRITE_TIMEOUT, ssl=ctx) as r:
            return r.status == 200
    except Exception:
        return False


async def ha_service(session, service, data):
    try:
        domain, svc = service.split(".", 1)
        url = f"{HA_URL}/api/services/{domain}/{svc}"
        headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
        async with session.post(url, headers=headers, json=data, timeout=HA_WRITE_TIMEOUT) as r:
            return r.status == 200
    except Exception:
        return False


async def main():
    parser = argparse.ArgumentParser(description="Battery Coordinator")
    parser.add_argument("--live", action="store_true", help="Actually control the Zendure")
    args = parser.parse_args()

    brain = PermissionBrain()
    tick = 0
    last_relay_count = None

    mode = "LIVE" if args.live else "DRY RUN"
    print(f"\n  Battery Coordinator [{mode}] — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if args.live:
        print("  ** CONTROLLING ZENDURE **")
    print()

    async with aiohttp.ClientSession() as session:
        zen_sn = await fetch_zen_sn(session, verbose=args.live)
        print(f"  Zendure SN: {zen_sn}\n")
        if args.live and not zen_sn:
            print("  WARNING: no Zendure SN — running in observe-only mode")

        while True:
            t = time.monotonic()
            now = datetime.now().strftime("%H:%M:%S")

            # Read sensors
            p1, pib1, pib2, pib1_soc, pib2_soc, solar, zen_power, zen_soc, relay_count = await asyncio.gather(
                ha_float(session, ENTITY_P1),
                ha_float(session, ENTITY_PIB1_POWER),
                ha_float(session, ENTITY_PIB2_POWER),
                ha_float(session, ENTITY_PIB1_SOC),
                ha_float(session, ENTITY_PIB2_SOC),
                ha_float(session, ENTITY_SOLAR),
                ha_float(session, ENTITY_ZEN_POWER),
                ha_float(session, ENTITY_ZEN_SOC),
                ha_float(session, ENTITY_ZEN_RELAY),
            )

            r = Reading(
                p1=p1,
                pibs=[pib1, pib2],
                pib_socs=[pib1_soc, pib2_soc],
                zen_power=zen_power,
                zen_soc=zen_soc,
                solar=solar,
            )
            d = brain.decide(r, t)

            # Relay click detection
            relay_click = ""
            if last_relay_count is not None and relay_count > last_relay_count:
                relay_click = " \U0001f534 RELAY CLICK!"
            last_relay_count = relay_count

            # Target description
            if d.target > 0:
                tgt_s = f"charge {d.target}W"
            elif d.target < 0:
                tgt_s = f"discharge {abs(d.target)}W"
            else:
                tgt_s = "hold"

            diff = d.target - zen_power

            # Print
            tick += 1
            if tick == 1 or tick % 30 == 0:
                print(
                    f"{'Time':<9} {'P1':>7} {'PIB1':>5}/{' PIB2':<5} {'Solar':>5} {'Zen':>6} "
                    f"{'Zone':<5} {'Target':<20} {'Diff':>6}"
                )
                print("-" * 90)

            sent = ""
            # Send command if live
            if args.live and zen_sn and d.send:
                CB = PermissionBrain

                if d.target > 0:
                    mode_switch = brain.last_ac_mode != CB.AC_CHARGE
                    svc = "rest_command.zendure_x_laden" if mode_switch else "rest_command.zendure_x_laden_balanceren"
                    ok = await ha_service(session, svc, {"sn": zen_sn, "inputLimit": d.target})
                    brain.mark_sent(d.target, t)
                    sent = f" SENT charge {d.target}W" + (" (mode switch)" if mode_switch else "")
                elif d.target < 0:
                    mode_switch = brain.last_ac_mode != CB.AC_DISCHARGE
                    svc = (
                        "rest_command.zendure_x_ontladen"
                        if mode_switch
                        else "rest_command.zendure_x_ontladen_balanceren"
                    )
                    ok = await ha_service(session, svc, {"sn": zen_sn, "outputLimit": abs(d.target)})
                    brain.mark_sent(d.target, t)
                    sent = f" SENT discharge {abs(d.target)}W" + (" (mode switch)" if mode_switch else "")
                elif d.target == 0:
                    ok = await ha_service(session, "rest_command.zendure_standby", {"sn": zen_sn})
                    brain.mark_sent(0, t)
                    brain.last_ac_mode = None
                    sent = " SENT standby"
                if sent and not ok:
                    sent += " FAILED!"

            # PIB standby/wake control
            pib_sent = ""
            if args.live and (d.pib_mode or d.pib_permissions is not None):
                mode = d.pib_mode or "zero"
                ok = await hw_set_pib_mode(session, mode, d.pib_permissions)
                perms = ""
                if d.pib_permissions is not None:
                    perms = "(" + ",".join(p.replace("_allowed", "") for p in d.pib_permissions) + ")"
                pib_sent = f" PIB→{mode}{perms}" + ("" if ok else " FAILED!")

            print(
                f"{now}  "
                f"P1:{p1:>+6.0f}W  "
                f"\u2600\ufe0f{solar:>5.0f}W  "
                f"\U0001f50c{pib1:>+5.0f}/{pib2:>+5.0f}W ({pib1_soc:.0f}/{pib2_soc:.0f}%)  "
                f"\U0001faab{zen_power:>+6.0f}W ({zen_soc:.0f}%)  "
                f"[{d.zone:<5}] \u27a1 {tgt_s} ({diff:>+.0f}){relay_click}{sent}{pib_sent}"
            )

            await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
